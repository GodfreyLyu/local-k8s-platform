# Purpose: Exercise plan integrity, ownership, stage readiness and read-only reporting with isolated fake processes.
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import runtime
from config import load_config
from io_utils import private_dir, read_json, write_json


# Inputs: temporary workspace. Outputs: valid normalized PostgreSQL-only configuration.
# Purpose: Build a real input-boundary fixture without using personal kubeconfig files. Dependencies: config.load_config.
def configuration(workspace):
    kubeconfig = workspace / "kubeconfig"
    kubeconfig.write_text("# Purpose: fake test kubeconfig; no credentials.\n")
    raw = {
        "environment_id": "example",
        "cluster": {"kubeconfig": str(kubeconfig)},
        "kafka": {"enabled": False},
        "monitoring": {"enabled": False},
        "storage": {"class": "local-retain"},
    }
    path = workspace / "input.json"
    write_json(path, raw)
    return load_config(path)


# Inputs: name and optional namespace. Outputs: Terraform namespace creation plan.
# Purpose: Provide a minimal non-sensitive resource change for plan review. Dependencies: Terraform JSON convention.
def creation(name="example-app", namespace=None):
    metadata = {"name": name, "labels": {runtime.ENVIRONMENT_LABEL: "example"}}
    if namespace:
        metadata["namespace"] = namespace
    return {
        "resource_changes": [
            {
                "address": "kubernetes_namespace_v1.app",
                "type": "kubernetes_namespace_v1",
                "mode": "managed",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"metadata": [metadata]},
                },
            }
        ]
    }


class RuntimeTests(unittest.TestCase):
    # Inputs: none. Outputs: isolated workspace and source fixture on self.
    # Purpose: Ensure every test uses disposable state and mock-only cluster access. Dependencies: tempfile and fixture helpers.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.config = configuration(self.workspace)
        self.identity = {
            "context": "minikube",
            "kubeconfig": self.config["cluster"]["kubeconfig"],
            "kubeconfig_sha256": "fixture",
            "server": "https://127.0.0.1:8443",
            "kube_system_uid": "cluster-one",
        }
        stage = private_dir(private_dir(self.workspace / "stages") / "00-bootstrap")
        (stage / "main.tf").write_text("# Purpose: disposable Terraform source fixture.\n")
        private_dir(self.workspace / "state")
        self.document = creation()

    # Inputs: Terraform command, stage directory, log path. Outputs: successful fake process.
    # Purpose: Materialize only the plan/state files requested by runtime. Dependencies: temporary workspace.
    def fake_terraform(self, command, stage_directory, log):
        if command[0] == "plan":
            output = next(
                value.removeprefix("-out=") for value in command if value.startswith("-out=")
            )
            Path(output).write_bytes(b"saved plan fixture")
        if command[0] == "apply":
            write_json(self.workspace / "state" / "00-bootstrap.tfstate", {"serial": 1})
        return subprocess.CompletedProcess(command, 0, "", "")

    # Inputs: command and optional runner kwargs. Outputs: fake Terraform show JSON.
    # Purpose: Supply structured external output without executing Terraform or kubectl. Dependencies: current test document.
    def fake_show(self, command, **kwargs):
        self.assertEqual(command[0], "terraform")
        self.assertIn("show", command)
        return subprocess.CompletedProcess(command, 0, json.dumps(self.document), "")

    # Inputs: none. Outputs: valid saved plan review.
    # Purpose: Share deterministic plan setup across integrity tests. Dependencies: mocked subprocess and inventory boundaries.
    def make_plan(self):
        with (
            patch.object(
                runtime, "inspect", return_value={"status": "pass", "identity": self.identity}
            ),
            patch.object(runtime, "_terraform", side_effect=self.fake_terraform),
            patch.object(runtime, "run", side_effect=self.fake_show),
            patch.object(runtime, "_get", return_value=None),
        ):
            return runtime.plan(self.config, self.workspace, "00-bootstrap")

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove review creation binds saved bytes and dry-run apply invokes no process. Dependencies: make_plan and apply preview.
    def test_saved_plan_preview_is_offline_and_private(self):
        result = self.make_plan()
        self.assertEqual(result["status"], "planned")
        saved = self.workspace / "plans" / "00-bootstrap" / "plan.tfplan"
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        with patch.object(
            runtime, "run", side_effect=AssertionError("Preview must not execute a process")
        ):
            preview = runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"])
        self.assertEqual(preview["status"], "preview")
        self.assertTrue(preview["requires_execute"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove source edits invalidate an otherwise reviewed plan. Dependencies: source digest checks.
    def test_source_modification_blocks_apply(self):
        result = self.make_plan()
        (self.workspace / "stages" / "00-bootstrap" / "main.tf").write_text(
            "# Purpose: changed fixture.\n"
        )
        with self.assertRaisesRegex(RuntimeError, "changed"):
            runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove normalized configuration changes invalidate saved plans. Dependencies: config digest checks.
    def test_configuration_modification_blocks_apply(self):
        result = self.make_plan()
        changed = deepcopy(self.config)
        changed["application"]["database"] = "another_database"
        with self.assertRaisesRegex(RuntimeError, "changed"):
            runtime.apply(changed, self.workspace, "00-bootstrap", result["plan_id"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove state changes invalidate a plan before external calls. Dependencies: state digest checks.
    def test_state_modification_blocks_apply(self):
        result = self.make_plan()
        write_json(self.workspace / "state" / "00-bootstrap.tfstate", {"serial": 99})
        with self.assertRaisesRegex(RuntimeError, "changed"):
            runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove failed replanning quarantines the old usable plan. Dependencies: stale-plan archive handling.
    def test_failed_replan_quarantines_previous_plan(self):
        previous = self.make_plan()
        with patch.object(runtime, "inspect", return_value={"status": "blocked"}):
            with self.assertRaisesRegex(RuntimeError, "Preflight"):
                runtime.plan(self.config, self.workspace, "00-bootstrap")
        directory = self.workspace / "plans" / "00-bootstrap"
        self.assertFalse((directory / "review.json").exists())
        self.assertTrue((directory / "failed.json").exists())
        self.assertEqual(len(list((self.workspace / "plans" / "archive").iterdir())), 1)
        with self.assertRaises(RuntimeError):
            runtime.apply(self.config, self.workspace, "00-bootstrap", previous["plan_id"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove an existing resource cannot be adopted even with a matching environment label. Dependencies: live collision query boundary.
    def test_create_collision_is_rejected(self):
        existing = {
            "metadata": {"name": "example-app", "labels": {runtime.ENVIRONMENT_LABEL: "example"}}
        }
        with patch.object(runtime, "_get", return_value=existing):
            with self.assertRaisesRegex(RuntimeError, "automatic adoption"):
                runtime._review_changes(self.config, creation())

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove deletion and Terraform Secret resources never become approved plans. Dependencies: change allowlist.
    def test_delete_and_secret_resources_are_rejected(self):
        document = creation()
        document["resource_changes"][0]["change"]["actions"] = ["delete", "create"]
        with self.assertRaisesRegex(RuntimeError, "Destructive"):
            runtime._review_changes(self.config, document)
        document = creation()
        document["resource_changes"][0]["type"] = "kubernetes_secret_v1"
        with self.assertRaisesRegex(RuntimeError, "Secret data"):
            runtime._review_changes(self.config, document)

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove a cluster recreated under the same context cannot execute an old plan. Dependencies: identity binding.
    def test_cluster_replacement_blocks_execute(self):
        result = self.make_plan()
        replacement = {**self.identity, "kube_system_uid": "cluster-two"}
        with (
            patch.object(
                runtime, "inspect", return_value={"status": "pass", "identity": replacement}
            ),
            patch.object(
                runtime, "_terraform", side_effect=AssertionError("Apply must be blocked")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "identity"):
                runtime.apply(
                    self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True
                )

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove successful execution consumes the exact plan and creates an applied-stage record. Dependencies: mocked Terraform apply.
    def test_execute_consumes_plan_once(self):
        result = self.make_plan()
        with (
            patch.object(
                runtime, "inspect", return_value={"status": "pass", "identity": self.identity}
            ),
            patch.object(runtime, "_get", return_value=None),
            patch.object(runtime, "_terraform", side_effect=self.fake_terraform),
        ):
            record = runtime.apply(
                self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True
            )
        self.assertEqual(record["status"], "applied")
        self.assertEqual(
            read_json(self.workspace / "reports" / "applied-00-bootstrap.json")["plan_id"],
            result["plan_id"],
        )
        with self.assertRaisesRegex(RuntimeError, "consumed"):
            runtime.apply(
                self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True
            )

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove a later stage cannot bypass required previous application. Dependencies: applied-stage prerequisite records.
    def test_postgres_requires_bootstrap_application(self):
        with self.assertRaisesRegex(RuntimeError, "00-bootstrap"):
            runtime._prerequisites(self.config, self.workspace, "30-postgres")

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove Secret readiness requests metadata only. Dependencies: mocked kubectl argument capture.
    def test_postgres_secret_readiness_does_not_read_data(self):
        write_json(self.workspace / "reports" / "applied-00-bootstrap.json", {"status": "applied"})
        with (
            patch.object(runtime, "_stage_ready", return_value=(True, "Ready")),
            patch.object(
                runtime, "run", return_value=subprocess.CompletedProcess([], 0, "secret-uid", "")
            ) as runner,
        ):
            runtime._prerequisites(self.config, self.workspace, "30-postgres")
        self.assertEqual(runner.call_count, 4)
        for call in runner.call_args_list:
            command = call.args[0]
            self.assertIn("jsonpath={.metadata.uid}", command)
            self.assertNotIn("json", command)
            self.assertIn(self.config["cluster"]["context"], command)
            self.assertIn(self.config["cluster"]["kubeconfig"], command)

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove profile-aware verification omits Kafka/CNPG while keeping functional acceptance incomplete. Dependencies: read-only GET mocks.
    def test_verify_keeps_baseline_separate_from_acceptance(self):
        with (
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_stage_ready", return_value=(True, "Ready")),
            patch.object(runtime, "_get", return_value={"status": {"phase": "Bound"}}) as getter,
        ):
            report = runtime.verify(self.config, self.workspace)
        self.assertEqual(report["baseline_result"], "pass")
        self.assertEqual(report["acceptance_result"], "incomplete")
        self.assertTrue(any("restore" in item for item in report["pending_acceptance"]))
        resources = [call.args[1] for call in getter.call_args_list]
        self.assertNotIn("kafkas.kafka.strimzi.io", resources)
        self.assertNotIn("clusters.postgresql.cnpg.io", resources)

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove an unreadable capacity summary blocks inspection rather than using allocatable values. Dependencies: mocked inventory GETs.
    def test_missing_measured_capacity_fails_closed(self):
        node = {
            "metadata": {"name": "minikube", "labels": {"minikube.k8s.io/name": "minikube"}},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "allocatable": {"memory": "999Gi"},
            },
        }

        # Inputs: configuration and resource query. Outputs: fake basic inventory.
        # Purpose: Supply only the reads needed before the missing summary. Dependencies: node fixture.
        def fake_get(config, resource, name=None, namespace=None):
            return {"items": [node]} if resource == "nodes" else {"items": []}

        with (
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_get", side_effect=fake_get),
            patch.object(
                runtime, "run", return_value=subprocess.CompletedProcess([], 0, '{"node": {}}', "")
            ),
        ):
            report = runtime.inspect(self.config, self.workspace)
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any(check["name"] == "inventory_access" for check in report["checks"]))

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove already-applied demand is subtracted from future capacity checks. Dependencies: applied accounting record.
    def test_remaining_demand_excludes_applied_stage(self):
        config = deepcopy(self.config)
        config["derived"]["memory_requests_by_stage"] = {"00-bootstrap": 0, "30-postgres": 384}
        config["derived"]["storage_mib_by_stage"] = {"00-bootstrap": 0, "30-postgres": 4096}
        write_json(
            self.workspace / "reports" / "applied-30-postgres.json",
            {"status": "applied", "memory_requests_mib": 384, "storage_mib": 4096},
        )
        self.assertEqual(runtime._remaining_demand(config, self.workspace), (0, 0, 0))

    # Inputs: none. Outputs: assertions only.
    # Purpose: Allow fresh plans after kubeconfig credentials refresh without retargeting the workspace. Dependencies: durable identity binding.
    def test_workspace_binding_allows_kubeconfig_refresh(self):
        self.assertTrue(runtime._bind(self.config, self.workspace, self.identity))
        refreshed = {**self.identity, "kubeconfig_sha256": "new-authentication-material"}
        self.assertTrue(runtime._bind(self.config, self.workspace, refreshed))
        replaced = {**refreshed, "kube_system_uid": "another-cluster"}
        self.assertFalse(runtime._bind(self.config, self.workspace, replaced))

    # Inputs: none. Outputs: assertions only.
    # Purpose: Ensure an uncertain Terraform timeout consumes the plan before mutation dispatch. Dependencies: mocked timeout boundary.
    def test_apply_timeout_consumes_plan(self):
        result = self.make_plan()
        with (
            patch.object(
                runtime, "inspect", return_value={"status": "pass", "identity": self.identity}
            ),
            patch.object(runtime, "_get", return_value=None),
            patch.object(runtime, "_terraform", side_effect=RuntimeError("Command timed out")),
        ):
            with self.assertRaisesRegex(RuntimeError, "actual state"):
                runtime.apply(
                    self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True
                )
        self.assertEqual(
            read_json(self.workspace / "plans" / "00-bootstrap" / "consumed.json")["status"],
            "uncertain",
        )
        with self.assertRaisesRegex(RuntimeError, "consumed"):
            runtime.apply(
                self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True
            )

    # Inputs: none. Outputs: assertions only.
    # Purpose: Reject updates if live metadata ownership or UID changed after Terraform refresh. Dependencies: live GET boundary.
    def test_live_update_uid_change_is_rejected(self):
        document = creation()
        change = document["resource_changes"][0]["change"]
        change["actions"] = ["update"]
        change["before"] = deepcopy(change["after"])
        change["before"]["metadata"][0]["uid"] = "old-uid"
        live = {
            "metadata": {"uid": "replacement-uid", "labels": {runtime.ENVIRONMENT_LABEL: "example"}}
        }
        with patch.object(runtime, "_get", return_value=live):
            with self.assertRaisesRegex(RuntimeError, "UID"):
                runtime._review_changes(self.config, document)

    # Inputs: none. Outputs: assertions only.
    # Purpose: Verify API egress derives internal peers and refuses unsupported actual endpoint ports. Dependencies: Service/Endpoints GET fixtures.
    def test_api_peers_are_actual_service_and_endpoint_addresses(self):
        service = {"spec": {"clusterIP": "10.96.0.1"}}
        endpoints = {
            "subsets": [{"addresses": [{"ip": "192.168.64.2"}], "ports": [{"port": 8443}]}]
        }
        with patch.object(runtime, "_get", side_effect=[service, endpoints]):
            self.assertEqual(runtime._api_peers(self.config), {"10.96.0.1/32", "192.168.64.2/32"})
        endpoints["subsets"][0]["ports"][0]["port"] = 9443
        with patch.object(runtime, "_get", side_effect=[service, endpoints]):
            with self.assertRaisesRegex(RuntimeError, "443/8443"):
                runtime._api_peers(self.config)

    # Inputs: optional per-node free MiB. Outputs: fake query and runner functions.
    # Purpose: Build a complete positive inventory response for capacity and version regressions. Dependencies: normalized PostgreSQL-only configuration.
    def inventory_fakes(self, free_mib=2048):
        node = {
            "metadata": {"name": "minikube", "labels": {"minikube.k8s.io/name": "minikube"}},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }

        # Inputs: configuration and GET arguments. Outputs: fake inventory resource JSON.
        # Purpose: Supply explicit baseline prerequisites. Dependencies: node fixture.
        def getter(config, resource, name=None, namespace=None):
            if resource == "nodes":
                return {
                    "items": [
                        node,
                        {**node, "metadata": {**node["metadata"], "name": "minikube-m02"}},
                    ]
                }
            if resource == "namespaces":
                return {"items": []}
            if resource == "storageclasses.storage.k8s.io":
                return {"volumeBindingMode": "WaitForFirstConsumer", "reclaimPolicy": "Retain"}
            if resource == "daemonsets.apps":
                return {"status": {"numberReady": 2, "desiredNumberScheduled": 2}}
            raise AssertionError(f"Unexpected GET resource: {resource}")

        # Inputs: process arguments and optional runner kwargs. Outputs: safe fake command result.
        # Purpose: Supply actual-capacity-shaped data and Ready DNS response. Dependencies: per-node free-memory fixture.
        def runner(command, **kwargs):
            if "/version" in command:
                value = {"gitVersion": "v1.37.0"}
            elif any("stats/summary" in item for item in command):
                value = {
                    "node": {
                        "memory": {"availableBytes": free_mib * 1048576},
                        "fs": {"availableBytes": 32768 * 1048576},
                    }
                }
            else:
                value = {
                    "items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]
                }
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

        return getter, runner

    # Inputs: none. Outputs: assertions only.
    # Purpose: Confirm a measured, supported PostgreSQL profile can pass baseline inspection. Dependencies: complete mocked inventory.
    def test_inspect_passes_measured_supported_profile(self):
        getter, runner = self.inventory_fakes()
        with (
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_get", side_effect=getter),
            patch.object(runtime, "run", side_effect=runner),
        ):
            report = runtime.inspect(self.config, self.workspace)
        self.assertEqual(report["status"], "pass", report["checks"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Reject aggregate-only memory headroom when no individual node fits the stage. Dependencies: two small fake node summaries.
    def test_aggregate_memory_does_not_replace_single_node_fit(self):
        getter, runner = self.inventory_fakes(free_mib=400)
        with (
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_get", side_effect=getter),
            patch.object(runtime, "run", side_effect=runner),
        ):
            report = runtime.inspect(self.config, self.workspace)
        checks = {item["name"]: item["status"] for item in report["checks"]}
        self.assertEqual(checks["measured_memory"], "pass")
        self.assertEqual(checks["single_node_memory"], "blocked")


if __name__ == "__main__":
    unittest.main()
