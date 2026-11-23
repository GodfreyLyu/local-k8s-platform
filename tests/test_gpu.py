"""Purpose: test GPU configuration, plugin preflight, readiness, and smoke-test orchestration with fake tools."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import gpu
import runtime
from capacity import assess
from config import load_config
from io_utils import private_dir, read_json, write_json

SPEC = importlib.util.spec_from_file_location("platform_cli", SCRIPTS / "platform.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)
RESOURCE = "devic.es/dri"


# Inputs: node name, advertised slots and optional profile label. Outputs: Node object.
# Purpose: supply Minikube node evidence for GPU checks. Dependencies: Kubernetes Node shape.
def node(name, slots=0, profile="minikube"):
    status = {"capacity": {RESOURCE: str(slots)}, "allocatable": {RESOURCE: str(slots)}}
    labels = {"kubernetes.io/hostname": name, "minikube.k8s.io/name": profile}
    return {"metadata": {"name": name, "labels": labels}, "status": status}


# Inputs: namespace, name, labels and container args. Outputs: DaemonSet object with status.
# Purpose: represent owned or foreign device plugins. Dependencies: Kubernetes DaemonSet shape.
def daemonset(namespace, name, labels=None, ready=1, uid="plugin-uid"):
    return {
        "metadata": {
            "namespace": namespace,
            "name": name,
            "labels": labels or {},
            "uid": uid,
            "generation": 2,
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "image": "docker.io/squat/generic-device-plugin:0.2.0",
                            "args": ["--domain=devic.es"],
                        }
                    ]
                }
            }
        },
        "status": {
            "observedGeneration": 2,
            "desiredNumberScheduled": 1,
            "numberReady": ready,
            "updatedNumberScheduled": 1,
        },
    }


class GpuTests(unittest.TestCase):
    # Inputs: none. Outputs: isolated workspace and GPU-only configuration.
    # Purpose: keep every test in a private temporary directory. Dependencies: tempfile.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = private_dir(self.root / "workspace")
        private_dir(self.workspace / "reports")
        self.config = self.load()
        self.labels = self.config["derived"]["labels"]
        self.identity = {"kube_system_uid": "cluster-one"}

    # Inputs: configuration overrides. Outputs: resolved configuration.
    # Purpose: build GPU-only input at the real entry point. Dependencies: load_config.
    def load(self, **overrides):
        value = {
            "environment_id": "atlas",
            "postgres": {"enabled": False},
            "kafka": {"enabled": False},
            "monitoring": {"enabled": False},
            "gpu": {"enabled": True, "nodes": ["minikube"]},
            **overrides,
        }
        source = self.root / "input.json"
        source.write_text(json.dumps(value), encoding="utf-8")
        return load_config(source)

    # Inputs: none. Outputs: assertions. Purpose: GPU-only selects bootstrap and the plugin stage only.
    # Dependencies: load_config.
    def test_gpu_only_derives_plugin_stage_and_forecast(self):
        derived = self.config["derived"]
        self.assertEqual(derived["stages"], ["00-bootstrap", "20-gpu"])
        self.assertEqual(derived["gpu_namespace"], "atlas-gpu")
        self.assertEqual(derived["requests_memory_mib"], 32)
        self.assertEqual(derived["workloads"][0]["nodes"], ["minikube"])
        both = self.load(
            postgres={"enabled": True},
            kafka={"enabled": True},
            monitoring={"enabled": True},
            storage={"class": "dev-retain"},
        )
        self.assertEqual(
            both["derived"]["stages"],
            ["00-bootstrap", "20-gpu", "30-postgres", "31-kafka", "40-monitoring"],
        )

    # Inputs: malformed GPU inputs. Outputs: errors. Purpose: reject mistakes before external tools.
    # Dependencies: load_config.
    def test_invalid_gpu_inputs_fail_at_boundary(self):
        krunkit = {"mode": "create", "name": "gpu", "context": "gpu", "create": {"nodes": 1}}
        cases = [
            {"gpu": {"enabled": True}},
            {"gpu": {"enabled": True, "nodes": ["a", "a"]}},
            {"gpu": {"enabled": True, "nodes": ["minikube"], "slots": 0}},
            {"gpu": {"enabled": True, "nodes": ["minikube"], "slots": 17}},
            {"gpu": {"enabled": True, "nodes": ["minikube"], "slots": True}},
            {"gpu": {"enabled": False, "nodes": ["minikube"]}},
            {"gpu": {"enabled": False}},
            {"monitoring": {"enabled": True}},
            {"storage": {"mode": "csi", "nodes": ["minikube"]}},
            {"application": {"namespace": "atlas-gpu"}},
            {"cluster": {**krunkit, "create": {"driver": "docker", "nodes": 1}}},
            {
                "cluster": {**krunkit, "create": {"driver": "krunkit", "nodes": 1}},
                "gpu": {"enabled": True, "nodes": ["gpu-m02"]},
            },
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(**value)
        created = self.load(
            cluster={**krunkit, "create": {"driver": "krunkit", "nodes": 1}},
            gpu={"enabled": True, "nodes": ["gpu"]},
        )
        self.assertEqual(created["derived"]["stages"], ["00-bootstrap", "20-gpu"])

    # Inputs: a node-bound forecast and a roomier other node. Outputs: blocked placement.
    # Purpose: DaemonSet Pods cannot move to another node. Dependencies: capacity.assess.
    def test_node_bound_forecast_uses_only_its_node(self):
        nodes = [
            {"metadata": {"name": name}, "status": {"allocatable": {"memory": "8Gi"}}}
            for name in ("small", "large")
        ]
        measurements = [
            {"name": "small", "memory_available_mib": 140, "disk_available_mib": 4096},
            {"name": "large", "memory_available_mib": 4096, "disk_available_mib": 4096},
        ]
        workload = {"name": "plugin", "memory_mib": 32, "storage_mib": 0, "nodes": ["small"]}
        report = assess(nodes, [], measurements, [workload], [], 512)
        self.assertFalse(report["checks"]["pod_placement"])
        workload["nodes"] = ["large"]
        report = assess(nodes, [], measurements, [workload], [], 512)
        self.assertEqual(report["allocations"][0]["node"], "large")

    # Inputs: DaemonSet list and device probe result. Outputs: preflight check results.
    # Purpose: run GPU preflight against fake kubectl and minikube. Dependencies: runtime._gpu_checks.
    def gpu_checks(self, daemonsets, nodes, device_present=True):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0 if device_present else 1, "", "")

        with (
            patch.object(runtime, "_get", return_value={"items": daemonsets}),
            patch.object(runtime, "run", side_effect=fake_run),
            patch.object(runtime.shutil, "which", return_value="/test-tools/minikube"),
        ):
            checks = {name: passed for name, passed, _ in runtime._gpu_checks(self.config, nodes)}
        return checks, commands

    # Inputs: owned, foreign and stale plugin states. Outputs: assertions.
    # Purpose: block a second plugin for the same resource and a missing render device. Dependencies: gpu_checks.
    def test_preflight_detects_foreign_plugins_and_missing_device(self):
        checks, commands = self.gpu_checks([], [node("minikube")])
        self.assertTrue(all(checks.values()), checks)
        self.assertEqual(
            commands[0],
            ["/test-tools/minikube", "--profile", "minikube", "ssh", "--node", "minikube", "--"]
            + ["test", "-c", "/dev/dri/renderD128"],
        )
        foreign = daemonset("gpu-system", "generic-device-plugin")
        checks, _ = self.gpu_checks([foreign], [node("minikube", 1)])
        self.assertFalse(checks["gpu_plugin_owner"])
        checks, _ = self.gpu_checks([], [node("minikube", 1)])
        self.assertFalse(checks["gpu_plugin_owner"])
        owned = daemonset("atlas-gpu", "atlas-gpu-device-plugin", self.labels)
        checks, _ = self.gpu_checks([owned], [node("minikube", 1)])
        self.assertTrue(checks["gpu_plugin_owner"])
        checks, _ = self.gpu_checks([], [node("minikube")], device_present=False)
        self.assertFalse(checks["gpu_device"])
        checks, _ = self.gpu_checks([], [node("other")])
        self.assertFalse(checks["gpu_nodes"])

    # Inputs: DaemonSet and node registration. Outputs: readiness result.
    # Purpose: evaluate 20-gpu readiness with fake reads. Dependencies: runtime._stage_ready.
    def stage_ready(self, plugin, nodes):
        def fake_get(config, resource, name=None, namespace=None, **kwargs):
            return plugin if resource == "daemonsets.apps" else {"items": nodes}

        with patch.object(runtime, "_get", side_effect=fake_get):
            return runtime._stage_ready(self.config, "20-gpu")

    # Inputs: rollout and registration variants. Outputs: assertions.
    # Purpose: require both a Ready owned plugin and the configured slots. Dependencies: stage_ready.
    def test_stage_ready_requires_rollout_and_registered_slots(self):
        owned = daemonset("atlas-gpu", "atlas-gpu-device-plugin", self.labels)
        ready, detail = self.stage_ready(owned, [node("minikube", 1)])
        self.assertTrue(ready, detail)
        self.assertFalse(self.stage_ready(owned, [node("minikube", 0)])[0])
        self.assertFalse(self.stage_ready(owned, [node("minikube", 2)])[0])
        unready = daemonset("atlas-gpu", "atlas-gpu-device-plugin", self.labels, ready=0)
        self.assertFalse(self.stage_ready(unready, [node("minikube", 1)])[0])
        self.assertFalse(self.stage_ready(None, [node("minikube", 1)])[0])

    # Inputs: plan resource of the plugin type. Outputs: Kubernetes identity.
    # Purpose: allow the DaemonSet through plan review. Dependencies: runtime._resource_identity.
    def test_plan_review_accepts_daemonset(self):
        identity = runtime._resource_identity(
            "kubernetes_daemon_set_v1",
            {"metadata": [{"name": "atlas-gpu-device-plugin", "namespace": "atlas-gpu"}]},
        )
        self.assertEqual(identity["resource"], "daemonsets.apps")

    # Inputs: none. Outputs: assertions. Purpose: keep the smoke Pod restricted and preview offline.
    # Dependencies: gpu.pod_manifest and gpu.smoke.
    def test_smoke_manifest_is_restricted_and_preview_is_offline(self):
        manifest = gpu.pod_manifest(self.config)
        self.assertEqual(manifest["metadata"]["namespace"], "atlas-validation")
        container = manifest["spec"]["containers"][0]
        self.assertEqual(container["resources"]["limits"][RESOURCE], "1")
        self.assertTrue(container["securityContext"]["runAsNonRoot"])
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["image"], runtime.IMAGES["storage_helper"]["reference"])
        with patch.object(gpu, "run", side_effect=AssertionError("Preview must not run tools")):
            preview = gpu.smoke(self.config, self.workspace)
        self.assertEqual(preview["status"], "preview")
        self.assertTrue(preview["requires_execute"])

    # Inputs: final Pod phase, log text and whether a Pod already exists. Outputs: smoke report and commands.
    # Purpose: run the smoke flow against fake kubectl. Dependencies: gpu.smoke.
    def run_smoke(self, phase="Succeeded", log=gpu.PASS_MARKER, leftover=False, reason=None):
        write_json(self.workspace / "reports" / "applied-20-gpu.json", {"status": "applied"})
        commands = []
        state = {"created": leftover, "deleted": False}
        plugin = daemonset("atlas-gpu", "atlas-gpu-device-plugin", self.labels)

        def fake_get(config, resource, name=None, namespace=None, **kwargs):
            if resource == "daemonsets.apps":
                return plugin
            if resource == "nodes":
                return {"items": [node("minikube", 1)]}
            if not state["created"] or state["deleted"]:
                return None
            conditions = [{"type": "PodScheduled", "reason": reason}] if reason else []
            return {
                "metadata": {"uid": "pod-uid"},
                "spec": {"nodeName": "minikube"},
                "status": {"phase": phase, "conditions": conditions},
            }

        def fake_run(command, **kwargs):
            commands.append(command)
            if "create" in command:
                state["created"] = True
                return subprocess.CompletedProcess(
                    command, 0, json.dumps({"metadata": {"uid": "pod-uid"}}), ""
                )
            if "delete" in command:
                state["deleted"] = True
            return subprocess.CompletedProcess(command, 0, log if "logs" in command else "", "")

        with (
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_bind", return_value=True),
            patch.object(runtime, "_get", side_effect=fake_get),
            patch.object(gpu, "run", side_effect=fake_run),
            patch.object(gpu, "WAIT_SECONDS", 0.5),
            patch.object(gpu.time, "sleep"),
        ):
            report = gpu.smoke(self.config, self.workspace, execute=True)
        return report, commands

    # Inputs: successful, failed, unschedulable and leftover cases. Outputs: assertions.
    # Purpose: prove the smoke Pod is always deleted and results are recorded. Dependencies: run_smoke.
    def test_smoke_creates_checks_and_deletes_its_pod(self):
        report, commands = self.run_smoke()
        self.assertEqual(report["status"], "pass", report)
        self.assertTrue(report["pod_deleted"])
        self.assertTrue(any("delete" in command for command in commands))
        self.assertEqual(report["plugin"], {"uid": "plugin-uid", "generation": 2})
        self.assertEqual(read_json(self.workspace / "reports" / "gpu-smoke.json"), report)
        report, commands = self.run_smoke(phase="Failed", log="test: permission denied")
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["pod_deleted"])
        report, _ = self.run_smoke(phase="Pending", log="", reason="Unschedulable")
        self.assertEqual(report["status"], "failed")
        self.assertIn("No free GPU slot", report["detail"])
        self.assertTrue(report["pod_deleted"])
        report, commands = self.run_smoke(leftover=True)
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(any("create" in command or "delete" in command for command in commands))

    # Inputs: stored smoke reports. Outputs: whether verify accepts them.
    # Purpose: accept smoke evidence only for the same cluster and plugin rollout. Dependencies: runtime._gpu_smoke_current.
    def test_verify_accepts_smoke_only_for_current_plugin(self):
        plugin = daemonset("atlas-gpu", "atlas-gpu-device-plugin", self.labels)
        report = {
            "status": "pass",
            "kube_system_uid": "cluster-one",
            "plugin": {"uid": "plugin-uid", "generation": 2},
        }
        path = self.workspace / "reports" / "gpu-smoke.json"
        with patch.object(runtime, "_get", return_value=plugin):
            self.assertIsNone(
                runtime._gpu_smoke_current(self.config, self.workspace, self.identity)
            )
            write_json(path, report)
            self.assertIsNotNone(
                runtime._gpu_smoke_current(self.config, self.workspace, self.identity)
            )
            write_json(path, {**report, "plugin": {"uid": "plugin-uid", "generation": 1}})
            self.assertIsNone(
                runtime._gpu_smoke_current(self.config, self.workspace, self.identity)
            )

    # Inputs: GPU-disabled configuration. Outputs: CLI refusal and the application contract.
    # Purpose: reject the smoke action at the entry point and publish the resource name. Dependencies: platform CLI.
    def test_cli_rejects_smoke_without_gpu_and_publishes_contract(self):
        source = self.root / "disabled.json"
        source.write_text(json.dumps({"environment_id": "atlas", "storage": {"class": "x"}}))
        status = CLI.main(
            ["--config", str(source), "--workspace", str(self.root / "other"), "gpu-smoke"]
        )
        self.assertEqual(status, 2)
        self.assertFalse((self.root / "other").exists())
        contract = CLI.connection_contract(self.config)
        self.assertEqual(contract["gpu"]["resource_name"], RESOURCE)
        self.assertEqual(contract["gpu"]["container_resources"]["limits"], {RESOURCE: "1"})

    # Inputs: packaged stacks. Outputs: assertions. Purpose: only the GPU stage runs privileged Pods,
    # in its own privileged namespace. Dependencies: Terraform sources.
    def test_privileged_pods_are_limited_to_the_gpu_stage(self):
        stacks = ROOT / "assets" / "infra" / "stacks"
        privileged = sorted(
            path.parent.name
            for path in stacks.rglob("*.tf")
            if "privileged = true" in path.read_text()
        )
        self.assertEqual(privileged, ["20-gpu"])
        source = (stacks / "20-gpu" / "main.tf").read_text()
        self.assertIn('"pod-security.kubernetes.io/enforce" = "privileged"', source)
        self.assertIn(runtime.IMAGES["gpu_device_plugin"]["reference"], source)


if __name__ == "__main__":
    unittest.main()
