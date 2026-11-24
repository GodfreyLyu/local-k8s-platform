"""Purpose: test reviewed Minikube profile creation and overwrite with private fake tools."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cluster as cluster_module
import delete_safety
import runtime
import secret_metadata
from config import load_config
from io_utils import read_json, write_json

SPEC = importlib.util.spec_from_file_location("cluster_cli", SCRIPTS / "platform.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)

FAKE_MINIKUBE = """# Purpose: simulate minikube version, profile listing, and start in private files.
import json
import os
import sys
from pathlib import Path

path = Path(os.environ["FAKE_STATE"])
state = json.loads(path.read_text())
arguments = sys.argv[1:]
with Path(os.environ["FAKE_LOG"]).open("a") as log:
    log.write(json.dumps({"tool": "minikube", "argv": arguments, "kubeconfig": os.environ.get("KUBECONFIG"), "home": os.environ.get("MINIKUBE_HOME")}) + "\\n")
if arguments[:1] == ["version"]:
    print(json.dumps({"minikubeVersion": "v1.39.0", "commit": "fake"}))
elif arguments[:2] == ["profile", "list"]:
    if not state["valid"] and not state["invalid"]:
        print(json.dumps({"error": {"name": "MK_USAGE_NO_PROFILE"}}))
        sys.exit(85)
    print(json.dumps({
        "valid": [{"Name": name, "Status": "Stopped"} for name in state["valid"]],
        "invalid": [{"Name": name} for name in state["invalid"]],
    }))
elif arguments[:1] == ["start"]:
    name = next(item.split("=", 1)[1] for item in arguments if item.startswith("--profile="))
    if state.get("vmnet_failure"):
        machine = Path(os.environ["MINIKUBE_HOME"]) / "machines" / name
        machine.mkdir(parents=True)
        (machine / "vmnet-helper.log").write_text(
            "INFO  [main] running as uid: 501 gid: 20\\nERROR [main] vmnet_start_interface: VMNET_FAILURE\\n"
        )
        (Path(os.environ["MINIKUBE_HOME"]) / "logs").mkdir()
        (Path(os.environ["MINIKUBE_HOME"]) / "logs" / "lastStart.txt").write_text("start log\\n")
    if state.get("fail_start"):
        print("X Exiting due to GUEST_PROVISION: error provisioning guest", file=sys.stderr)
        sys.exit(1)
    version = next(item.split("=", 1)[1] for item in arguments if item.startswith("--kubernetes-version="))
    profile = Path(os.environ["MINIKUBE_HOME"]) / "profiles" / name
    profile.mkdir(parents=True)
    (profile / "config.json").write_text(json.dumps({"KubernetesConfig": {"KubernetesVersion": version}}))
    kubeconfig = Path(os.environ["KUBECONFIG"])
    if not kubeconfig.exists():
        kubeconfig.write_text("apiVersion: v1\\nkind: Config\\n")
    state["valid"].append(name)
    state["contexts"].append(name)
    state["clusters"].append(name)
    state["users"].append(name)
    state["vm_present"] = True
    path.write_text(json.dumps(state))
elif arguments[:1] == ["ssh"]:
    if state.get("stopped") or state.get("unreachable") or not any(arg.startswith("--profile=") for arg in arguments):
        sys.exit(1)
    if "--kubeconfig=/var/lib/minikube/kubeconfig" not in arguments:
        sys.exit(64)
    print(state.get("local_uid", state["uid"]))
elif arguments[:1] == ["delete"]:
    if state.get("fail_delete"):
        sys.exit(1)
    name = next(item.split("=", 1)[1] for item in arguments if item.startswith("--profile="))
    profile = Path(os.environ["MINIKUBE_HOME"]) / "profiles" / name
    (profile / "config.json").unlink()
    profile.rmdir()
    keys = ["valid", "invalid", "clusters", "users"]
    if not state.get("keep_context"):
        keys.append("contexts")
    for key in keys:
        state[key] = [item for item in state[key] if item != name]
    state["objects"] = {}
    if not state.get("retain_vm"):
        state["vm_present"] = False
    path.write_text(json.dumps(state))
else:
    sys.exit(64)
"""

FAKE_KUBECTL = """# Purpose: simulate kubeconfig and live identity reads from private files.
import json
import os
import sys
from pathlib import Path

state = json.loads(Path(os.environ["FAKE_STATE"]).read_text())
arguments = sys.argv[1:]
with Path(os.environ["FAKE_LOG"]).open("a") as log:
    log.write(json.dumps({"tool": "kubectl", "argv": arguments}) + "\\n")
if "view" in arguments:
    if "--minify" in arguments:
        print(state["server"])
    else:
        print(json.dumps({
            "kind": "Config",
            "contexts": [
                {"name": name, "context": state.get("context_refs", {}).get(name, {
                    "cluster": (state["clusters"] or [name])[0],
                    "user": (state["users"] or [name])[0],
                })}
                for name in state["contexts"]
            ],
            "clusters": [{"name": name} for name in state["clusters"]],
            "users": [{"name": name} for name in state["users"]],
        }))
elif state.get("unreachable"):
    sys.exit(1)
elif "api-resources" in arguments:
    if state.get("incomplete_discovery"):
        sys.exit(1)
    namespaced = "--namespaced=true" in arguments
    core = {"configmaps", "persistentvolumeclaims", "secrets"} if namespaced else {"namespaces", "persistentvolumes"}
    observed = {kind for kind, ns, _ in (key.split("/", 2) for key in state.get("objects", {})) if bool(ns) == namespaced}
    print("\\n".join(sorted(core | observed)))
elif "namespaces" in arguments and "kube-system" in arguments:
    print(json.dumps({"metadata": {"uid": state["uid"]}}))
elif "get" in arguments and arguments[arguments.index("get") + 2].startswith("-"):
    resource = arguments[arguments.index("get") + 1]
    if resource in state.get("denied_resources", []):
        sys.exit(1)
    items = []
    for key, item in sorted(state.get("objects", {}).items()):
        kind, namespace, name = key.split("/", 2)
        if kind == resource:
            metadata = {key: value for key, value in item.items() if key not in {"spec", "status"}}
            payload = {key: item[key] for key in ("spec", "status") if key in item}
            items.append({**payload, "metadata": {**metadata, "name": name, **({"namespace": namespace} if namespace else {})}})
    print(json.dumps({"kind": "List", "items": items}))
elif "get" in arguments:
    index = arguments.index("get")
    resource, name = arguments[index + 1], arguments[index + 2]
    namespace = arguments[arguments.index("--namespace") + 1] if "--namespace" in arguments else ""
    objects = state.get("objects", {})
    if resource in state.get("denied_resources", []) or resource == "secrets":
        sys.exit(1)
    if resource == "kafkatopics.kafka.strimzi.io" and f"customresourcedefinitions.apiextensions.k8s.io//{resource}" not in objects:
        sys.exit(1)
    item = objects.get(f"{resource}/{namespace}/{name}")
    if item is not None:
        if arguments[-1] == "name":
            print(f"{resource}/{name}")
        else:
            print(json.dumps({"metadata": {"name": name, **item}}))
else:
    sys.exit(64)
"""


FAKE_OTOOL = """# Purpose: report a libkrun load command for the fake krunkit executable.
import os
import sys

if len(sys.argv) != 3 or sys.argv[1] != "-L" or not sys.argv[2].endswith("/krunkit"):
    sys.exit(64)
print(sys.argv[2] + ":")
print("\\t" + os.environ["FAKE_LIBKRUN"] + " (compatibility version 1.0.0, current version 1.19.0)")
"""


class ClusterTests(unittest.TestCase):
    # Inputs: none. Outputs: private fake tools, state, and environment. Purpose: isolate every test.
    # Dependencies: tempfile and the current Python interpreter for fake executables.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, source in (
            ("minikube", FAKE_MINIKUBE),
            ("kubectl", FAKE_KUBECTL),
            ("otool", FAKE_OTOOL),
            ("krunkit", "# Purpose: represent the driver in PATH without starting it.\n"),
        ):
            tool = bin_dir / name
            tool.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
            tool.chmod(0o700)
        self.libkrun = self.root / "libkrun.1.dylib"
        self.link_libkrun("1.19.6")
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.write_state()
        self.kubeconfig = self.root / "kubeconfig"
        self.kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
        environment = patch.dict(
            os.environ,
            {
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "FAKE_STATE": str(self.state),
                "FAKE_LOG": str(self.log),
                "FAKE_LIBKRUN": str(self.libkrun),
                "MINIKUBE_HOME": str(self.root / "minikube-home"),
                "TMPDIR": "/tmp",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        proxy = patch.object(secret_metadata, "proxy", return_value=nullcontext(self.root / "api.sock"))
        request = patch.object(secret_metadata, "_request", side_effect=self.metadata_request)
        proxy.start()
        request.start()
        self.addCleanup(proxy.stop)
        self.addCleanup(request.stop)
        machine = patch.object(
            delete_safety, "machine_evidence", return_value={"driver": "krunkit", "pids": [12345]}
        )
        process = patch.object(
            delete_safety, "_process_exists",
            side_effect=lambda pid: json.loads(self.state.read_text()).get("vm_present", False),
        )
        machine.start()
        process.start()
        self.addCleanup(machine.stop)
        self.addCleanup(process.stop)
        # Temporary roots can exceed the real socket limit; the dedicated tests set exact limits.
        limit = patch.object(cluster_module, "SOCKET_PATH_MAX", 4096)
        limit.start()
        self.addCleanup(limit.stop)
        self.workspace = self.root / "workspace"
        self.source = self.root / "environment.json"

    # Inputs: installed libkrun release. Outputs: a new target for the same dylib load command.
    # Purpose: distinguish the installed library from its build-time version. Dependencies: private files.
    def link_libkrun(self, version):
        library = self.root / f"libkrun.{version}.dylib"
        library.touch()
        self.libkrun.unlink(missing_ok=True)
        self.libkrun.symlink_to(library.name)

    # Inputs: proxy socket path, API route and requested media type. Outputs: a fake metadata response.
    # Purpose: exercise content negotiation and error handling without binding a socket.
    def metadata_request(self, path, route, accept):
        self.assertEqual(path, self.root / "api.sock")
        if route == "/api/v1/secrets":
            self.assertEqual(accept, "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1")
            state = json.loads(self.state.read_text())
            if state.get("secret_api_denied"):
                return 403, {}
            items = []
            for key, item in state.get("objects", {}).items():
                resource, namespace, name = key.split("/", 2)
                if resource == "secrets":
                    items.append({"metadata": {**item, "name": name, "namespace": namespace}})
            return 200, {
                "kind": "PartialObjectMetadataList", "apiVersion": "meta.k8s.io/v1", "items": items
            }
        self.assertEqual(accept, "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1")
        _, api, version, namespaced, namespace, resource, name = route.split("/")
        self.assertEqual((api, version, namespaced, resource), ("api", "v1", "namespaces", "secrets"))
        with self.log.open("a") as log:
            log.write(json.dumps({"tool": "metadata", "argv": ["get", route, accept]}) + "\n")
        state = json.loads(self.state.read_text())
        if state.get("secret_api_denied"):
            return 403, {}
        item = state.get("objects", {}).get(f"secrets/{namespace}/{name}")
        if item is None:
            return 404, {
                "kind": "Status", "reason": "NotFound", "details": {"name": name, "kind": "secrets"}
            }
        return 200, {
            "kind": "PartialObjectMetadata", "apiVersion": "meta.k8s.io/v1",
            "metadata": {**item, "name": name, "namespace": namespace},
        }

    # Inputs: fake profile names, kubeconfig names and start/identity outcomes. Outputs: none.
    # Dependencies: private JSON state file read by the fake tools.
    def write_state(
        self,
        valid=(),
        invalid=(),
        contexts=(),
        fail_start=False,
        *,
        clusters=(),
        users=(),
        uid="cluster-a",
        server="https://cluster-a.invalid",
        **extra,
    ):
        self.state.write_text(
            json.dumps(
                {
                    "valid": list(valid),
                    "invalid": list(invalid),
                    "contexts": list(contexts),
                    "clusters": list(clusters),
                    "users": list(users),
                    "uid": uid,
                    "server": server,
                    "fail_start": fail_start,
                    **extra,
                }
            ),
            encoding="utf-8",
        )

    # Inputs: field overrides. Outputs: none. Purpose: change one fake-tool field and keep the rest.
    # Dependencies: private JSON state file read by the fake tools.
    def update_state(self, **fields):
        state = json.loads(self.state.read_text())
        state.update(fields)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    # Inputs: none. Outputs: creation report. Purpose: create the profile through the reviewed flow.
    # Dependencies: fake tools and mocked API peer discovery.
    def create(self):
        self.write_config()
        _, planned = self.cli("cluster")
        with patch.object(runtime, "_api_peers", return_value=set()):
            status, report = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual((status, report["status"]), (0, "created"), report)
        return report

    # Inputs: none. Outputs: none. Purpose: record a namespace, a manifest, and a published Secret,
    # and make the same objects live. Dependencies: local-backend state shape and private reports.
    def record_objects(self):
        label = {runtime.ENVIRONMENT_LABEL: "atlas"}
        manifest = {
            "apiVersion": "kafka.strimzi.io/v1beta2",
            "kind": "KafkaTopic",
            "metadata": {"name": "jobs", "namespace": "kafka", "labels": label},
        }
        write_json(
            self.workspace / "state" / "00-bootstrap.tfstate",
            {
                "version": 4,
                "resources": [
                    {
                        "mode": "managed",
                        "type": "kubernetes_namespace_v1",
                        "name": "platform",
                        "instances": [
                            {"attributes": {"metadata": [{"name": "atlas", "labels": label, "uid": "ns-1"}]}}
                        ],
                    },
                    {"mode": "data", "type": "kubernetes_namespace_v1", "name": "x", "instances": []},
                    {
                        "mode": "managed",
                        "type": "kubernetes_manifest",
                        "name": "topic",
                        "instances": [
                            {
                                "attributes": {
                                    "manifest": {"value": manifest, "type": ["object", {}]},
                                    "object": {
                                        "value": {"metadata": {"uid": "topic-1"}},
                                        "type": ["object", {}],
                                    },
                                }
                            }
                        ],
                    },
                ],
            },
        )
        write_json(
            self.workspace / "reports" / "credentials-postgres.json",
            {"status": "published", "results": [{"reference": "atlas-app/pg-client", "status": "created"}]},
        )
        self.update_state(
            objects={
                "namespaces//atlas": {"uid": "ns-1", "labels": label},
                "customresourcedefinitions.apiextensions.k8s.io//kafkatopics.kafka.strimzi.io": {"uid": "crd-1"},
                "kafkatopics.kafka.strimzi.io/kafka/jobs": {"uid": "topic-1", "labels": label},
                "secrets/atlas-app/pg-client": {"uid": "secret-1"},
            }
        )

    # Inputs: none. Outputs: fake tool calls by first argument. Purpose: count start/delete effects.
    # Dependencies: the private fake-tool log.
    def count(self, verb):
        return len([call for call in self.calls() if call["argv"][:1] == [verb]])

    # Inputs: optional cluster and storage overrides. Outputs: config path. Purpose: build user input.
    # Dependencies: JSON configuration boundary.
    def write_config(self, cluster=None, storage=None):
        value = {
            "environment_id": "atlas",
            "cluster": {
                "mode": "create",
                "name": "atlas-dev",
                "kubeconfig": str(self.kubeconfig),
                "create": {"driver": "krunkit"},
                **(cluster or {}),
            },
            "storage": storage
            or {
                "mode": "csi",
                "class": "atlas-retain",
                "nodes": ["atlas-dev-m02", "atlas-dev-m03"],
            },
        }
        self.source.write_text(json.dumps(value), encoding="utf-8")
        return self.source

    # Inputs: CLI arguments after the config/workspace prefix. Outputs: exit status and decoded report.
    # Purpose: exercise the real command boundary. Dependencies: platform.main.
    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = CLI.main(
                ["--config", str(self.source), "--workspace", str(self.workspace), *arguments]
            )
        text = stdout.getvalue() or stderr.getvalue()
        return status, json.loads(text)

    # Inputs: none. Outputs: logged fake tool calls. Purpose: assert exact external effects.
    # Dependencies: the private fake-tool log.
    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    # Inputs: create-mode configuration. Outputs: assertions. Purpose: check normalization and derivation.
    # Dependencies: load_config.
    def test_create_mode_normalizes_context_and_derives_nodes(self):
        config = load_config(self.write_config())
        self.assertEqual(config["cluster"]["context"], "atlas-dev")
        self.assertEqual(
            config["derived"]["cluster"]["nodes"], ["atlas-dev", "atlas-dev-m02", "atlas-dev-m03"]
        )
        self.assertEqual(
            config["derived"]["cluster"]["addons"], ["csi-hostpath-driver", "volumesnapshots"]
        )
        reuse = self.root / "reuse.json"
        reuse.write_text(json.dumps({"environment_id": "atlas", "storage": {"class": "x"}}))
        self.assertIsNone(load_config(reuse)["derived"]["cluster"])

    # Inputs: packaged create example. Outputs: assertions. Purpose: keep the example loadable.
    # Dependencies: load_config and the assets directory.
    def test_packaged_create_example_is_valid(self):
        example = SCRIPTS.parent / "assets" / "environment.create.example.json"
        config = load_config(example)
        self.assertEqual(config["cluster"]["mode"], "create")
        self.assertEqual(config["cluster"]["context"], config["cluster"]["name"])

    # Inputs: invalid create-mode fields. Outputs: errors. Purpose: reject mistakes at the boundary.
    # Dependencies: load_config.
    def test_invalid_create_inputs_fail_at_boundary(self):
        cases = [
            ({"name": ""}, None),
            ({"name": "Bad_Name"}, None),
            ({"context": "other"}, None),
            ({"create": {"driver": "hyperv"}}, None),
            ({"create": {"driver": "docker", "kubernetes_version": "v1.34.0"}}, None),
            ({"create": {"driver": "docker", "nodes": 0}}, None),
            ({"create": {"driver": "docker", "memory_mib": 3072}}, None),
            ({"create": {"driver": "docker", "container_runtime": "rkt"}}, None),
            ({}, {"mode": "csi", "class": "atlas-retain", "nodes": ["minikube-m02"]}),
            ({"mode": "reuse"}, None),
        ]
        for cluster, storage in cases:
            with self.subTest(cluster=cluster, storage=storage):
                with self.assertRaises(ValueError):
                    load_config(self.write_config(cluster, storage))

    # Inputs: no existing profile. Outputs: plan, preview, creation. Purpose: prove the reviewed flow.
    # Dependencies: fake minikube/kubectl and mocked API peer discovery.
    def test_plan_preview_execute_runs_exact_reviewed_command(self):
        self.write_config()
        status, planned = self.cli("cluster")
        self.assertEqual((status, planned["status"]), (0, "planned"))
        self.assertTrue(planned["requires_execute"])
        command = planned["command"]
        self.assertEqual(command[:2], ["/usr/bin/env", f"KUBECONFIG={self.kubeconfig}"])
        for flag in (
            "start",
            "TMPDIR=/tmp",
            "--profile=atlas-dev",
            "--driver=krunkit",
            "--nodes=3",
            "--kubernetes-version=v1.37.0",
            "--cni=calico",
            "--addons=csi-hostpath-driver",
            "--addons=volumesnapshots",
            "--keep-context=true",
            "--interactive=false",
        ):
            self.assertIn(flag, command)
        self.assertFalse(any(call["argv"][:1] == ["start"] for call in self.calls()))
        status, preview = self.cli("cluster", "--plan-id", planned["plan_id"])
        self.assertEqual((status, preview["status"]), (0, "preview"))
        self.assertFalse(any(call["argv"][:1] == ["start"] for call in self.calls()))
        with patch.object(
            runtime, "_api_peers", return_value={"10.96.0.1/32", "192.168.5.2/32"}
        ):
            status, report = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual((status, report["status"]), (0, "created"))
        identity = report["identity"]
        self.assertEqual(identity["kube_system_uid"], "cluster-a")
        self.assertEqual(
            read_json(self.workspace / "cluster-binding.json"),
            {
                "environment_id": "atlas",
                "identity": {
                    key: value for key, value in identity.items() if key != "kubeconfig_sha256"
                },
            },
        )
        self.assertEqual(report["suggested_api_server_cidrs"], ["10.96.0.1/32", "192.168.5.2/32"])
        starts = [call for call in self.calls() if call["argv"][:1] == ["start"]]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["kubeconfig"], str(self.kubeconfig))
        self.assertFalse(any(call["argv"][:1] == ["delete"] for call in self.calls()))
        status, again = self.cli("cluster")
        self.assertEqual((status, again["status"]), (0, "created"))
        status, replay = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(replay["status"], "created")
        self.assertEqual(len([c for c in self.calls() if c["argv"][:1] == ["start"]]), 1)

    # Inputs: reuse-class storage. Outputs: command without CSI addons. Purpose: scope addons to CSI mode.
    # Dependencies: fake tools.
    def test_non_csi_storage_requests_no_addons(self):
        self.write_config(storage={"mode": "reuse", "class": "standard"})
        _, planned = self.cli("cluster")
        self.assertFalse(any(item.startswith("--addons") for item in planned["command"]))

    # Inputs: existing valid, stopped, invalid profiles or contexts. Outputs: blocked plans, no start.
    # Purpose: enforce create-only behavior. Dependencies: fake tools and profile directories.
    def test_existing_profile_or_context_blocks(self):
        self.write_config()
        for state in (
            {"valid": ["atlas-dev"]},
            {"invalid": ["atlas-dev"]},
            {"contexts": ["atlas-dev"]},
        ):
            with self.subTest(state=state):
                self.write_state(**state)
                status, report = self.cli("cluster")
                self.assertEqual((status, report["status"]), (2, "blocked"))
                self.assertFalse(report["requires_execute"])
                status, refused = self.cli("cluster", "--plan-id", report["plan_id"], "--execute")
                self.assertEqual(status, 2)
                self.assertIn("blocked", refused["error"])
        self.write_state()
        (self.root / "minikube-home" / ".minikube" / "profiles" / "atlas-dev").mkdir(parents=True)
        status, report = self.cli("cluster")
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(any(call["argv"][:1] == ["start"] for call in self.calls()))

    # Inputs: socket limits around the longest krunkit machine socket. Outputs: blocked, then planned.
    # Purpose: stop a start whose vmnet-helper socket cannot bind. Dependencies: fake tools.
    def test_long_minikube_home_blocks_krunkit_before_start(self):
        nodes = load_config(self.write_config())["derived"]["cluster"]["nodes"]
        # The first longest node name wins a tie, and vmnet-helper.sock is the longer socket name.
        node = max(nodes, key=len)
        longest = self.root / "minikube-home" / ".minikube" / "machines" / node / "vmnet-helper.sock"
        size = len(os.fsencode(longest))
        with patch.object(cluster_module, "SOCKET_PATH_MAX", size - 1):
            status, blocked = self.cli("cluster")
            self.assertEqual((status, blocked["status"]), (2, "blocked"))
            self.assertFalse(blocked["requires_execute"])
            message = " ".join(blocked["blockers"])
            self.assertIn(str(longest), message)
            self.assertIn(f"{size} bytes, above the {size - 1}-byte", message)
            evidence = read_json(Path(blocked["review"]))["snapshot"]["machine_socket"]
            self.assertEqual(evidence, {"path": str(longest), "bytes": size, "limit": size - 1})
            status, refused = self.cli("cluster", "--plan-id", blocked["plan_id"], "--execute")
            self.assertEqual(status, 2)
        with patch.object(cluster_module, "SOCKET_PATH_MAX", size):
            status, planned = self.cli("cluster")
            self.assertEqual((status, planned["status"]), (0, "planned"), planned)
        self.assertEqual(self.count("start"), 0)

    # Inputs: a driver without known machine sockets and a tiny limit. Outputs: an unblocked plan.
    # Purpose: limit the check to drivers whose socket names are verified. Dependencies: fake tools.
    def test_socket_limit_ignores_drivers_without_machine_sockets(self):
        self.write_config(cluster={"create": {"driver": "docker"}})
        with patch.object(cluster_module, "SOCKET_PATH_MAX", 1):
            status, planned = self.cli("cluster")
        self.assertEqual((status, planned["status"]), (0, "planned"), planned)
        self.assertIsNone(read_json(Path(planned["review"]))["snapshot"]["machine_socket"])

    # Inputs: a created profile with drift and a home that is too long. Outputs: blocked plan, no delete.
    # Purpose: refuse to delete a profile when its replacement cannot start. Dependencies: fake tools.
    def test_long_minikube_home_blocks_overwrite_before_delete(self):
        self.create()
        self.record_objects()
        self.update_state(objects={})
        with patch.object(cluster_module, "SOCKET_PATH_MAX", 10):
            status, planned = self.cli("cluster", "--overwrite")
            self.assertEqual((status, planned["status"]), (2, "blocked"), planned)
            self.assertIn("Minikube home is too long", planned["blockers"][0])
            status, refused = self.cli(
                "cluster", "--overwrite", "--plan-id", planned["plan_id"], "--execute"
            )
            self.assertEqual(status, 2)
        self.assertEqual((self.count("start"), self.count("delete")), (1, 0))
        self.assertFalse((self.workspace / "archive").exists())

    # Inputs: a drifted profile and a legacy linked libkrun. Outputs: a blocked plan and no delete.
    # Purpose: count the legacy client socket suffix separately from Minikube's two sockets.
    def test_legacy_krunkit_client_socket_blocks_overwrite(self):
        self.create()
        self.record_objects()
        self.update_state(objects={})
        self.link_libkrun("1.18.0")
        config = load_config(self.source)
        home = Path(os.environ["MINIKUBE_HOME"]) / ".minikube"
        machine = cluster_module._longest_socket(config, home)
        with patch.object(cluster_module, "SOCKET_PATH_MAX", machine["bytes"]):
            status, blocked = self.cli("cluster", "--overwrite")
            self.assertEqual((status, blocked["status"]), (2, "blocked"), blocked)
            snapshot = read_json(Path(blocked["review"]))["snapshot"]
            self.assertEqual(snapshot["krunkit"]["libkrun"], str(self.libkrun.resolve()))
            socket = snapshot["client_sockets"]["MINIKUBE_HOME"]
            self.assertEqual(socket["path"], machine["path"] + "-krun.sock")
            self.assertEqual(socket["bytes"], machine["bytes"] + len("-krun.sock"))
            self.assertIn(socket["path"], " ".join(blocked["blockers"]))
            status, _ = self.cli("cluster", "--overwrite", "--plan-id", blocked["plan_id"], "--execute")
            self.assertEqual(status, 2)
        self.assertEqual((self.count("start"), self.count("delete")), (1, 0))
        self.assertFalse((self.workspace / "archive").exists())

    # Inputs: a modern linked library and a changed TMPDIR. Outputs: a stale plan and a blocked new one.
    # Purpose: recheck the client socket before deletion and pass the reviewed temp path to start.
    def test_modern_krunkit_tmpdir_is_reviewed_before_delete(self):
        self.create()
        self.record_objects()
        self.update_state(objects={})
        config = load_config(self.source)
        home = Path(os.environ["MINIKUBE_HOME"]) / ".minikube"
        limit = cluster_module._longest_socket(config, home)["bytes"]
        with patch.object(cluster_module, "SOCKET_PATH_MAX", limit):
            status, plan = self.cli("cluster", "--overwrite")
            self.assertEqual((status, plan["status"]), (0, "planned"), plan)
            self.assertIn("TMPDIR=/tmp", plan["command"])
            self.assertNotIn("TMPDIR=/tmp", plan["overwrite"]["delete_command"])
            evidence = read_json(Path(plan["review"]))["snapshot"]
            self.assertEqual(set(evidence["client_sockets"]), {"TMPDIR"})

            with patch.dict(os.environ, {"TMPDIR": str(self.root / ("x" * 150))}):
                status, refused = self.cli(
                    "cluster", "--overwrite", "--plan-id", plan["plan_id"], "--execute"
                )
                self.assertEqual(status, 2, refused)
                self.assertIn("changed since review", refused["error"])
                status, blocked = self.cli("cluster", "--overwrite")
                self.assertEqual((status, blocked["status"]), (2, "blocked"), blocked)
                self.assertIn("TMPDIR is too long", " ".join(blocked["blockers"]))

            self.link_libkrun("1.18.0")
            status, refused = self.cli(
                "cluster", "--overwrite", "--plan-id", plan["plan_id"], "--execute"
            )
            self.assertEqual(status, 2, refused)
            self.assertIn("changed since review", refused["error"])
        self.assertEqual((self.count("start"), self.count("delete")), (1, 0))
        self.assertFalse((self.workspace / "archive").exists())

    # Inputs: aliases with different lengths for one running profile. Outputs: unchanged or blocked.
    # Purpose: check startup sockets only after reconciliation proves a replacement is needed.
    def test_long_alias_does_not_block_an_unchanged_profile(self):
        target = self.root / "real-minikube-home"
        target.mkdir()
        short = self.root / "a" / ".minikube"
        short.parent.mkdir()
        short.symlink_to(target, target_is_directory=True)
        long = self.root / "long" / ".minikube"
        long.parent.mkdir()
        long.symlink_to(target, target_is_directory=True)
        config = load_config(self.write_config())
        limit = cluster_module._longest_socket(config, short)["bytes"]
        with patch.object(cluster_module, "SOCKET_PATH_MAX", limit):
            with patch.dict(os.environ, {"MINIKUBE_HOME": str(short)}):
                self.create()
            self.record_objects()
            with patch.dict(os.environ, {"MINIKUBE_HOME": str(long)}):
                status, result = self.cli("cluster", "--overwrite")
                self.assertEqual((status, result["status"]), (0, "unchanged"), result)
                self.assertEqual(result["overwrite"]["reconcile"]["differences"], [])
                self.update_state(objects={})
                status, blocked = self.cli("cluster", "--overwrite")
                self.assertEqual((status, blocked["status"]), (2, "blocked"), blocked)
                self.assertIn("Minikube home is too long", " ".join(blocked["blockers"]))
        self.assertEqual((self.count("start"), self.count("delete")), (1, 0))

    # Inputs: a valid TMPDIR at review and a changed environment after deletion starts.
    # Outputs: proof that the replacement inherits the reviewed directory. Dependencies: fake tools.
    def test_replacement_start_keeps_reviewed_tmpdir(self):
        self.create()
        self.record_objects()
        self.update_state(objects={})
        status, plan = self.cli("cluster", "--overwrite")
        self.assertEqual((status, plan["status"]), (0, "planned"), plan)
        run = cluster_module.run

        # Inputs: exact child argv and runner options. Outputs: fake run result.
        # Purpose: change the ambient temp directory after comparison but before replacement.
        def change_tmpdir(command, **options):
            if command == plan["overwrite"]["delete_command"]:
                os.environ["TMPDIR"] = str(self.root / ("x" * 150))
            if "start" in command:
                self.assertIn("TMPDIR=/tmp", command)
            return run(command, **options)

        with (
            patch.dict(os.environ),
            patch.object(cluster_module, "run", side_effect=change_tmpdir),
            patch.object(runtime, "_api_peers", return_value=set()),
        ):
            status, report = self.cli(
                "cluster", "--overwrite", "--plan-id", plan["plan_id"], "--execute"
            )
        self.assertEqual((status, report["status"]), (0, "created"), report)
        self.assertEqual((self.count("start"), self.count("delete")), (2, 1))

    # Inputs: a same-name cluster or user referenced by another context. Outputs: blocked plans.
    # Purpose: protect every kubeconfig name Minikube writes. Dependencies: fake kubeconfig reads.
    def test_existing_cluster_or_user_name_blocks_before_and_after_review(self):
        self.write_config()
        for kind in ("cluster", "user"):
            with self.subTest(kind=kind):
                conflict = {kind + "s": ["atlas-dev"]}
                self.write_state(contexts=["other"], **conflict)
                status, blocked = self.cli("cluster")
                self.assertEqual((status, blocked["status"]), (2, "blocked"))
                self.assertIn(kind, " ".join(blocked["blockers"]))
                self.write_state(contexts=["other"])
                status, planned = self.cli("cluster")
                self.assertEqual((status, planned["status"]), (0, "planned"))
                self.write_state(contexts=["other"], **conflict)
                status, refused = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
                self.assertEqual(status, 2)
                self.assertIn("changed since review", refused["error"])
        self.assertFalse(any(call["argv"][:1] == ["start"] for call in self.calls()))

    # Inputs: orphan machine directories or a dangling record. Outputs: blocked plans and no start.
    # Purpose: rule out reuse of a planned primary or worker. Dependencies: private Minikube home.
    def test_existing_machine_record_blocks_before_and_after_review(self):
        self.write_config()
        machines = self.root / "minikube-home" / ".minikube" / "machines"
        machines.mkdir(parents=True)
        primary = machines / "atlas-dev"
        primary.mkdir()
        status, blocked = self.cli("cluster")
        self.assertEqual((status, blocked["status"]), (2, "blocked"))
        self.assertIn("atlas-dev", " ".join(blocked["blockers"]))
        primary.rmdir()

        status, planned = self.cli("cluster")
        self.assertEqual((status, planned["status"]), (0, "planned"))
        (machines / "atlas-dev-m03").symlink_to("missing-record")
        status, refused = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2)
        self.assertIn("changed since review", refused["error"])
        status, blocked = self.cli("cluster")
        self.assertEqual((status, blocked["status"]), (2, "blocked"))
        self.assertIn("atlas-dev-m03", " ".join(blocked["blockers"]))
        self.assertFalse(any(call["argv"][:1] == ["start"] for call in self.calls()))

    # Inputs: a profile appearing after review, and a changed config. Outputs: refusals.
    # Purpose: bind execution to the reviewed evidence. Dependencies: fake tools.
    def test_changed_target_or_config_invalidates_plan(self):
        self.write_config()
        _, planned = self.cli("cluster")
        self.write_state(valid=["atlas-dev"])
        status, refused = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2)
        self.assertIn("changed since review", refused["error"])
        self.write_state()
        _, planned = self.cli("cluster")
        self.write_config({"create": {"driver": "krunkit", "cpus": 4}})
        status, refused = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2)
        self.assertFalse(any(call["argv"][:1] == ["start"] for call in self.calls()))

    # Inputs: failing minikube start. Outputs: failed record, consumed plan, no delete.
    # Purpose: preserve partial results without rollback. Dependencies: fake tools.
    def test_failed_start_is_recorded_and_never_deleted(self):
        self.write_config()
        self.write_state(fail_start=True)
        _, planned = self.cli("cluster")
        status, failed = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2)
        self.assertIn("preserved", failed["error"])
        self.assertEqual(read_json(self.workspace / "reports" / "cluster.json")["status"], "failed")
        self.assertFalse(any(call["argv"][:1] == ["delete"] for call in self.calls()))
        status, consumed = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertIn("consumed", consumed["error"])
        self.assertEqual(len([c for c in self.calls() if c["argv"][:1] == ["start"]]), 1)

    # Inputs: a start whose vmnet-helper logged VMNET_FAILURE. Outputs: failed record naming the network
    # stage, the helper error, the minikube reason, and existing logs. Dependencies: fake tools.
    def test_failed_start_reports_the_earliest_error_and_logs(self):
        self.write_config()
        self.write_state(fail_start=True, vmnet_failure=True)
        _, planned = self.cli("cluster")
        status, failed = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2, failed)
        failure = read_json(self.workspace / "reports" / "cluster.json")["failure"]
        home = Path(planned["minikube_home"])
        machine = home / "machines" / planned["profile"]
        self.assertEqual(failure["stage"], "network")
        self.assertIn("VMNET_FAILURE", failure["first_error"])
        self.assertEqual(failure["minikube_reason"], "GUEST_PROVISION")
        self.assertEqual(
            failure["logs"],
            [str(home / "logs" / "lastStart.txt"), str(machine / "vmnet-helper.log")],
        )
        self.assertIn("Failed stage: network", failed["error"])
        self.assertIn(str(machine / "vmnet-helper.log"), failed["error"])
        self.assertTrue(machine.is_dir())
        self.assertFalse(any(call["argv"][:1] == ["delete"] for call in self.calls()))

    # Inputs: Ctrl-C while minikube start runs. Outputs: interrupted CLI result and an uncertain,
    # consumed, preserved creation. Purpose: never leave the report at attempting. Dependencies: fake tools.
    def test_interrupted_start_is_recorded_as_unconfirmed(self):
        self.write_config()
        self.write_state(vmnet_failure=True)
        _, planned = self.cli("cluster")
        real_run = cluster_module.run

        def interrupt_start(argv, **options):
            if "start" in argv:
                real_run(argv, **options)
                raise KeyboardInterrupt
            return real_run(argv, **options)

        with patch.object(cluster_module, "run", side_effect=interrupt_start):
            status, interrupted = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual((status, interrupted["status"]), (130, "interrupted"), interrupted)
        for path in (
            self.workspace / "reports" / "cluster.json",
            self.workspace / "plans" / "cluster" / f"consumed-{planned['plan_id']}.json",
        ):
            record = read_json(path)
            self.assertEqual((record["status"], record["interrupted"]), ("uncertain", True))
            self.assertEqual(record["failure"]["stage"], "network")
        self.assertFalse(any(call["argv"][:1] == ["delete"] for call in self.calls()))
        self.assertEqual(self.cli("inspect")[0], 2)
        status, consumed = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertIn("consumed", consumed["error"])
        self.assertEqual(len([c for c in self.calls() if c["argv"][:1] == ["start"]]), 1)

    # Inputs: a successful start with an unreadable identity. Outputs: uncertain status and a consumed plan.
    # Purpose: block platform operations when the target could not be bound. Dependencies: fake tools.
    def test_unverified_creation_does_not_unlock_platform_operations(self):
        self.write_config()
        self.write_state(uid="")
        _, planned = self.cli("cluster")
        status, refused = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2, refused)
        self.assertIn("identity", refused["error"])
        record = read_json(self.workspace / "reports" / "cluster.json")
        self.assertEqual(record["status"], "uncertain")
        before = self.calls()
        self.assertEqual(self.cli("inspect")[0], 2)
        self.assertEqual(self.calls(), before)
        status, refused = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2)
        self.assertIn("consumed", refused["error"])
        self.assertEqual(len([c for c in self.calls() if c["argv"][:1] == ["start"]]), 1)

    # Inputs: a successful start and a failed binding save. Outputs: uncertain creation.
    # Purpose: require a durable binding before reporting success. Dependencies: fake tools and simulated I/O failure.
    def test_binding_write_failure_leaves_creation_unverified(self):
        self.write_config()
        _, planned = self.cli("cluster")
        with patch.object(runtime, "write_json", side_effect=PermissionError("Cannot save binding")):
            status, refused = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2, refused)
        record = read_json(self.workspace / "reports" / "cluster.json")
        self.assertEqual(record["status"], "uncertain")
        self.assertFalse((self.workspace / "cluster-binding.json").exists())
        self.assertEqual(self.cli("inspect")[0], 2)

    # Inputs: newly bound profile with a repointed context. Outputs: blocked operations and original binding.
    # Purpose: keep first inspection and prerequisite planning on the created cluster. Dependencies: fake live identity.
    def test_repointed_context_cannot_change_the_creation_binding(self):
        self.write_config()
        _, planned = self.cli("cluster")
        status, created = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 0, created)
        binding_path = self.workspace / "cluster-binding.json"
        original = read_json(binding_path)
        self.write_state(valid=["atlas-dev"], contexts=["atlas-dev"], uid="cluster-b")
        for arguments in (("inspect",), ("verify",), ("prerequisites",)):
            with self.subTest(arguments=arguments):
                before = len(self.calls())
                status, blocked = self.cli(*arguments)
                self.assertEqual(status, 2, blocked)
                self.assertIn("binding", json.dumps(blocked))
                self.assertEqual(read_json(binding_path), original)
                calls = self.calls()[before:]
                self.assertEqual(len(calls), 2)
                self.assertIn("kube-system", calls[0]["argv"])
                self.assertIn("--minify", calls[1]["argv"])

    # Inputs: a saved creation identity and a missing binding. Outputs: blocked or safely restored binding.
    # Purpose: migrate only legacy reports and only to the original target. Dependencies: private workspace.
    def test_only_matching_legacy_creation_report_restores_missing_binding(self):
        config = load_config(self.write_config())
        self.assertEqual(self.cli("configure")[0], 0)
        report_path = self.workspace / "reports" / "cluster.json"
        binding_path = self.workspace / "cluster-binding.json"
        identity = runtime._identity(config)
        record = {
            "status": "created",
            "profile": "atlas-dev",
            "identity": identity,
            "binding_established": True,
        }
        write_json(report_path, record)
        self.assertFalse(runtime._bind(config, self.workspace, identity))
        self.assertFalse(binding_path.exists())

        del record["binding_established"]
        write_json(report_path, record)
        repointed = {**identity, "kube_system_uid": "cluster-b"}
        self.assertFalse(runtime._bind(config, self.workspace, repointed))
        self.assertFalse(binding_path.exists())
        self.assertTrue(runtime._bind(config, self.workspace, identity))
        self.assertEqual(read_json(binding_path)["identity"]["kube_system_uid"], "cluster-a")
        self.assertFalse(runtime._bind(config, self.workspace, repointed))

    # Inputs: create mode before creation. Outputs: refusal without kubectl cluster reads.
    # Purpose: keep later stages from targeting a missing cluster. Dependencies: CLI gate.
    def test_cluster_actions_wait_for_creation(self):
        self.write_config()
        for arguments in (("inspect",), ("plan", "--stage", "00-bootstrap"), ("prerequisites",)):
            with self.subTest(arguments=arguments):
                status, report = self.cli(*arguments)
                self.assertEqual(status, 2)
                self.assertIn("not created yet", report["error"])
        self.assertEqual(self.calls(), [])
        status, _ = self.cli("configure")
        self.assertEqual(status, 0)

    # Inputs: reuse-mode configuration. Outputs: refusal. Purpose: keep creation an explicit choice.
    # Dependencies: CLI entry point.
    def test_cluster_action_requires_create_mode(self):
        self.source.write_text(json.dumps({"environment_id": "atlas", "storage": {"class": "x"}}))
        status, report = self.cli("cluster")
        self.assertEqual(status, 2)
        self.assertIn("cluster.mode=create", report["error"])
        self.assertEqual(self.calls(), [])

    # Inputs: a workspace saved before cluster fields existed. Outputs: accepted reuse configuration.
    # Purpose: keep existing environments usable after the upgrade. Dependencies: configure binding.
    def test_legacy_workspace_binding_remains_valid(self):
        self.source.write_text(json.dumps({"environment_id": "atlas", "storage": {"class": "x"}}))
        config = load_config(self.source)
        self.assertEqual(self.cli("configure")[0], 0)
        saved = self.workspace / "environment.json"
        legacy = read_json(saved)
        for key in ("name", "create"):
            del legacy["cluster"][key]
        saved.write_text(json.dumps(legacy))
        self.assertEqual(CLI.configure(config, self.workspace)["status"], "configured")

    # Inputs: a workspace without a creation report. Outputs: refusal without Minikube calls.
    # Purpose: limit overwrite to profiles this workspace created. Dependencies: CLI entry point.
    def test_overwrite_requires_a_created_profile(self):
        self.write_config()
        status, report = self.cli("cluster", "--overwrite")
        self.assertEqual(status, 2)
        self.assertIn("applies only", report["error"])
        self.assertEqual(self.calls(), [])

    # Inputs: local-backend state with resource and data entries. Outputs: recorded identities.
    # Purpose: read plain and dynamic (kubernetes_manifest) attributes. Dependencies: runtime helper.
    def test_state_resources_read_plain_and_manifest_identities(self):
        self.write_config()
        self.assertEqual(self.cli("configure")[0], 0)
        self.record_objects()
        resources = runtime._state_resources(self.workspace)
        self.assertEqual(
            [(item["resource"], item["namespace"], item["name"], item["uid"]) for item in resources],
            [
                ("namespaces", None, "atlas", "ns-1"),
                ("kafkatopics.kafka.strimzi.io", "kafka", "jobs", "topic-1"),
            ],
        )

    # Inputs: a created profile that the user deleted. Outputs: archived workspace and a new profile.
    # Purpose: recreate a missing profile only through a reviewed --overwrite plan. Dependencies: fake tools.
    def test_missing_profile_is_recreated_after_archiving_the_workspace(self):
        self.create()
        self.record_objects()
        profile = self.root / "minikube-home" / ".minikube" / "profiles" / "atlas-dev"
        (profile / "config.json").unlink()
        profile.rmdir()
        self.write_state(uid="cluster-b", server="https://cluster-b.invalid")
        status, plain = self.cli("cluster")
        self.assertEqual((status, plain["status"]), (0, "created"))
        self.assertIn("--overwrite", plain["note"])
        status, planned = self.cli("cluster", "--overwrite")
        self.assertEqual((status, planned["status"]), (0, "planned"), planned)
        self.assertEqual(planned["overwrite"]["action"], "recreate-missing")
        self.assertIsNone(planned["overwrite"]["delete_command"])
        self.assertTrue(planned["requires_execute"])
        status, refused = self.cli("cluster", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual((status, refused["status"]), (0, "created"))
        self.assertEqual(self.count("start"), 1)
        with patch.object(runtime, "_api_peers", return_value=set()):
            status, report = self.cli(
                "cluster", "--overwrite", "--plan-id", planned["plan_id"], "--execute"
            )
        self.assertEqual((status, report["status"]), (0, "created"), report)
        self.assertEqual(report["identity"]["kube_system_uid"], "cluster-b")
        self.assertEqual((self.count("start"), self.count("delete")), (2, 0))
        archive = Path(report["archive"])
        self.assertEqual(archive.parent, self.workspace / "archive")
        self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
        self.assertTrue((archive / "state" / "00-bootstrap.tfstate").is_file())
        previous = read_json(archive / "reports" / "cluster.json")
        self.assertEqual(previous["identity"]["kube_system_uid"], "cluster-a")
        self.assertFalse((self.workspace / "state" / "00-bootstrap.tfstate").exists())
        self.assertFalse((self.workspace / "reports" / "credentials-postgres.json").exists())
        self.assertEqual(
            read_json(self.workspace / "cluster-binding.json")["identity"]["kube_system_uid"],
            "cluster-b",
        )
        recreated = read_json(self.workspace / "reports" / "cluster-recreated.json")
        self.assertEqual((recreated["action"], recreated["archive"]), ("recreate-missing", str(archive)))
        self.assertEqual(self.cli("configure")[0], 0)
        status, replay = self.cli("cluster", "--overwrite", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2)
        self.assertIn("consumed", replay["error"])
        self.assertEqual(self.count("start"), 2)

    # Inputs: a running profile whose objects match recorded state. Outputs: unchanged, no writes.
    # Purpose: never overwrite a consistent cluster. Dependencies: fake live objects.
    def test_consistent_profile_is_left_unchanged(self):
        self.create()
        self.record_objects()
        status, planned = self.cli("cluster", "--overwrite")
        self.assertEqual((status, planned["status"]), (0, "unchanged"), planned)
        self.assertFalse(planned["requires_execute"])
        reconcile = planned["overwrite"]["reconcile"]
        self.assertEqual((reconcile["checked"], reconcile["differences"]), (3, []))
        resources = read_json(self.workspace / "reports" / "cluster-reconcile.json")["resources"]
        secret = next(item for item in resources if item["resource"] == "secrets")
        self.assertEqual(secret["observed_uid"], "secret-1")
        self.assertTrue(any(call["tool"] == "metadata" for call in self.calls()))
        status, refused = self.cli("cluster", "--overwrite", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2)
        self.assertIn("no change", refused["error"])
        self.assertEqual((self.count("start"), self.count("delete")), (1, 0))
        self.assertFalse((self.workspace / "archive").exists())

    # Inputs: drifted objects or a replaced cluster under the same profile. Outputs: delete, archive, start.
    # Purpose: overwrite only on definite differences, deleting before archiving. Dependencies: fake tools.
    def test_drifted_profile_is_deleted_archived_and_recreated(self):
        label = {runtime.ENVIRONMENT_LABEL: "atlas"}
        cases = {
            "missing": lambda: self.update_state(objects={}),
            "uid": lambda: self.update_state(
                objects={
                    "namespaces//atlas": {"uid": "ns-2", "labels": label},
                    "customresourcedefinitions.apiextensions.k8s.io//kafkatopics.kafka.strimzi.io": {"uid": "crd-1"},
                    "kafkatopics.kafka.strimzi.io/kafka/jobs": {"uid": "topic-1", "labels": label},
                    "secrets/atlas-app/pg-client": {"uid": "secret-1"},
                }
            ),
            "identity": lambda: self.update_state(uid="cluster-c"),
        }
        for name, drift in cases.items():
            with self.subTest(drift=name):
                self.setUp()
                self.create()
                self.record_objects()
                drift()
                status, planned = self.cli("cluster", "--overwrite")
                self.assertEqual((status, planned["status"]), (0, "planned"), planned)
                summary = planned["overwrite"]
                self.assertEqual(summary["action"], "overwrite")
                self.assertEqual(summary["delete_command"][-2:], ["delete", "--profile=atlas-dev"])
                self.assertIn("lost", summary["effects"])
                if name == "identity":
                    self.assertEqual(summary["reconcile"]["identity"]["status"], "mismatch")
                else:
                    self.assertTrue(summary["reconcile"]["differences"])
                self.assertEqual(self.count("delete"), 0)
                with patch.object(runtime, "_api_peers", return_value=set()):
                    status, report = self.cli(
                        "cluster", "--overwrite", "--plan-id", planned["plan_id"], "--execute"
                    )
                self.assertEqual((status, report["status"]), (0, "created"), report)
                verbs = [call["argv"][0] for call in self.calls() if call["tool"] == "minikube"]
                self.assertLess(verbs.index("delete"), len(verbs) - 1 - verbs[::-1].index("start"))
                self.assertEqual((self.count("start"), self.count("delete")), (2, 1))
                self.assertEqual(report["deletion"], "completed")
                for call in self.calls():
                    if call["argv"][0] in {"start", "delete", "ssh"}:
                        self.assertEqual(call["home"], report["minikube_home"])
                self.assertTrue((Path(report["archive"]) / "state" / "00-bootstrap.tfstate").exists())

    # Inputs: a stopped or unreachable profile. Outputs: blocked plan, no delete.
    # Purpose: an unreadable cluster may hold intact data and is never overwritten. Dependencies: fake tools.
    def test_unreadable_profile_is_never_overwritten(self):
        self.create()
        self.record_objects()
        self.update_state(unreachable=True)
        status, planned = self.cli("cluster", "--overwrite")
        self.assertEqual((status, planned["status"]), (2, "blocked"), planned)
        self.assertIn("minikube start -p atlas-dev", planned["blockers"][0])
        self.assertFalse(planned["requires_execute"])
        status, refused = self.cli("cluster", "--overwrite", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2)
        self.assertEqual(self.count("delete"), 0)

    # Inputs: drift that disappears after review. Outputs: stale-plan refusal, no delete.
    # Purpose: bind execution to the reviewed comparison. Dependencies: fake live objects.
    def test_changed_comparison_invalidates_overwrite_plan(self):
        self.create()
        self.record_objects()
        objects = json.loads(self.state.read_text())["objects"]
        self.update_state(objects={})
        _, planned = self.cli("cluster", "--overwrite")
        self.update_state(objects=objects)
        status, refused = self.cli("cluster", "--overwrite", "--plan-id", planned["plan_id"], "--execute")
        self.assertEqual(status, 2)
        self.assertIn("changed since review", refused["error"])
        self.assertEqual((self.count("start"), self.count("delete")), (1, 0))

    # Inputs: matching and repointed contexts and a stopped profile. Outputs: blocked plans and execution.
    # Purpose: allow comparison only against the API of the running local profile. Dependencies: fake Minikube SSH.
    def test_overwrite_refuses_a_repointed_context_and_a_stopped_profile(self):
        self.create()
        self.record_objects()
        self.update_state(objects={})
        status, approved = self.cli("cluster", "--overwrite")
        self.assertEqual((status, approved["status"]), (0, "planned"), approved)
        self.update_state(uid="foreign-cluster", local_uid="cluster-a")
        status, refused = self.cli("cluster", "--overwrite", "--plan-id", approved["plan_id"], "--execute")
        self.assertEqual(status, 2, refused)
        self.assertIn("changed since review", refused["error"])
        status, blocked = self.cli("cluster", "--overwrite")
        self.assertEqual(status, 2, blocked)
        self.assertIn("does not reach", blocked["blockers"][0])

        self.update_state(stopped=True)
        status, blocked = self.cli("cluster", "--overwrite")
        self.assertEqual(status, 2, blocked)
        self.assertIn("running local profile", blocked["blockers"][0])
        self.assertEqual(self.count("delete"), 0)

    # Inputs: two homes with the same profile and evidence. Outputs: stale plan and wrong-home refusal.
    # Purpose: scope deletion authorization to the creation home. Dependencies: fake profiles on disk.
    def test_overwrite_home_cannot_change_after_review(self):
        created = self.create()
        self.record_objects()
        self.update_state(objects={})
        status, planned = self.cli("cluster", "--overwrite")
        self.assertEqual((status, planned["status"]), (0, "planned"), planned)
        home = Path(created["minikube_home"])
        alternate = self.root / "other-home" / ".minikube"
        profile = alternate / "profiles" / "atlas-dev"
        profile.mkdir(parents=True)
        (profile / "config.json").write_bytes((home / "profiles" / "atlas-dev" / "config.json").read_bytes())
        self.assertIn(f"MINIKUBE_HOME={home}", planned["overwrite"]["delete_command"])
        self.assertIn(f"MINIKUBE_HOME={home}", planned["command"])
        with patch.dict(os.environ, {"MINIKUBE_HOME": str(alternate.parent)}):
            status, blocked = self.cli("cluster", "--overwrite", "--plan-id", planned["plan_id"], "--execute")
            self.assertEqual(status, 2, blocked)
            status, fresh = self.cli("cluster", "--overwrite")
            self.assertEqual(status, 2, fresh)
            self.assertIn("Minikube home differs", fresh["blockers"][0])
        self.assertTrue((home / "profiles" / "atlas-dev").is_dir())
        self.assertTrue(profile.is_dir())
        self.assertEqual(self.count("delete"), 0)

    # Inputs: legacy creation report without a home. Outputs: normal report and blocked overwrite.
    # Purpose: prevent a guess from authorizing deletion. Dependencies: private creation history.
    def test_legacy_creation_reports_cannot_authorize_overwrite(self):
        self.create()
        report_path = self.workspace / "reports" / "cluster.json"
        report = read_json(report_path)
        del report["minikube_home"]
        write_json(report_path, report)
        self.assertEqual(self.cli("cluster")[0], 0)
        status, blocked = self.cli("cluster", "--overwrite")
        self.assertEqual(status, 2, blocked)
        self.assertIn("no recorded Minikube home", blocked["blockers"][0])
        self.assertEqual(self.count("delete"), 0)

    # Inputs: new UID or owner with the same drift status. Outputs: stale approval and no delete.
    # Purpose: compare actual evidence on execution. Dependencies: fake Kubernetes GET.
    def test_changed_uid_or_owner_invalidates_same_status(self):
        self.create()
        self.record_objects()
        objects = json.loads(self.state.read_text())["objects"]
        atlas = "namespaces//atlas"
        first = {"uid": "ns-2", "labels": {runtime.ENVIRONMENT_LABEL: "foreign"}}
        objects[atlas] = first
        self.update_state(objects=objects)
        status, reviewed = self.cli("cluster", "--overwrite")
        self.assertEqual(status, 0, reviewed)
        difference = reviewed["overwrite"]["reconcile"]["differences"][0]
        self.assertEqual(
            (difference["status"], difference["observed_uid"], difference["observed_owner"]),
            ("uid_mismatch", "ns-2", "foreign"),
        )
        for live in (
            {**first, "uid": "ns-3"},
            {**first, "labels": {runtime.ENVIRONMENT_LABEL: "foreign-2"}},
        ):
            with self.subTest(live=live):
                objects[atlas] = live
                self.update_state(objects=objects)
                status, refused = self.cli("cluster", "--overwrite", "--plan-id", reviewed["plan_id"], "--execute")
                self.assertEqual(status, 2, refused)
                self.assertIn("changed since review", refused["error"])
        self.assertEqual(self.count("delete"), 0)

    # Inputs: a workspace containing the kubeconfig or Minikube home. Outputs: blocked archiving.
    # Purpose: preserve shared configuration paths. Dependencies: private fake home and kubeconfig.
    def test_overwrite_keeps_shared_configuration_out_of_archives(self):
        for shared in ("kubeconfig", "home"):
            with self.subTest(shared=shared):
                self.setUp()
                if shared == "kubeconfig":
                    self.kubeconfig = self.workspace / "shared-kubeconfig"
                else:
                    environment = patch.dict(os.environ, {
                        "MINIKUBE_HOME": str(self.workspace / "shared-home")
                    })
                    environment.start()
                    self.addCleanup(environment.stop)
                created = self.create()
                status, blocked = self.cli("cluster", "--overwrite")
                self.assertEqual(status, 2, blocked)
                self.assertIn("separate archive paths", blocked["blockers"][0])
                self.assertEqual(self.count("delete"), 0)
                self.assertTrue(Path(created["minikube_home"]).is_dir())
                self.assertTrue(self.kubeconfig.exists())

    # Inputs: an unwritable archive parent. Outputs: execution refusal before deletion.
    # Purpose: reserve the archive while the existing workspace is intact. Dependencies: file mode bits.
    def test_archive_reservation_failure_blocks_delete(self):
        self.create()
        self.record_objects()
        self.update_state(objects={})
        status, reviewed = self.cli("cluster", "--overwrite")
        self.assertEqual(status, 0, reviewed)
        archive = self.workspace / "archive"
        archive.mkdir(mode=0o500)
        self.addCleanup(archive.chmod, 0o700)
        status, refused = self.cli("cluster", "--overwrite", "--plan-id", reviewed["plan_id"], "--execute")
        self.assertEqual(status, 2, refused)
        self.assertIn("Cannot reserve the archive", refused["error"])
        self.assertTrue((self.workspace / "state" / "00-bootstrap.tfstate").exists())
        self.assertEqual((self.count("start"), self.count("delete")), (1, 0))

    # Inputs: missing CRD and denied reads. Outputs: verified absence or blocked comparison.
    # Purpose: distinguish missing APIs from permission and discovery errors. Dependencies: fake kubectl.
    def test_crd_absence_is_drift_but_unreadable_apis_block(self):
        self.create()
        self.record_objects()
        objects = json.loads(self.state.read_text())["objects"]
        crd = "customresourcedefinitions.apiextensions.k8s.io"
        topic = "kafkatopics.kafka.strimzi.io"
        key = f"{crd}//{topic}"
        schema = objects.pop(key)
        self.update_state(objects=objects)
        before = len(self.calls())
        status, reviewed = self.cli("cluster", "--overwrite")
        self.assertEqual(status, 0, reviewed)
        difference = reviewed["overwrite"]["reconcile"]["differences"][0]
        self.assertEqual((difference["resource"], difference["status"]), (topic, "missing"))
        self.assertFalse(any(
            argv[argv.index("get") + 1] == topic
            for argv in (call["argv"] for call in self.calls()[before:] if call["tool"] == "kubectl")
            if "get" in argv
        ))

        objects[key] = schema
        self.update_state(objects=objects)
        for denial in ({"denied_resources": [crd]}, {"denied_resources": [topic]},
                       {"denied_resources": [], "secret_api_denied": True}):
            with self.subTest(denial=denial):
                self.update_state(**denial)
                status, blocked = self.cli("cluster", "--overwrite")
                self.assertEqual(status, 2, blocked)
                self.assertIn("Cannot read", blocked["blockers"][0])
        self.assertEqual(self.count("delete"), 0)

    # Inputs: a legacy Secret publication and older prerequisite attempt. Outputs: retained drift evidence.
    # Purpose: preserve ownership records across previews and retries. Dependencies: private reports.
    def test_overwrite_uses_records_after_preview_and_prerequisite_retry(self):
        self.create()
        self.record_objects()
        self.assertEqual(self.cli("credentials")[0], 0)
        self.assertEqual(read_json(self.workspace / "reports" / "credentials-postgres.json")["status"], "preview")
        earlier = {"resource": "deployments.apps", "namespace": "kafka", "name": "previous"}
        write_json(self.workspace / "plans" / "prerequisites" / "consumed-previous.json", {
            "results": [{"status": "created", "reference": earlier}]
        })
        write_json(self.workspace / "reports" / "prerequisites-strimzi.json", {"results": []})
        objects = json.loads(self.state.read_text())["objects"]
        del objects["secrets/atlas-app/pg-client"]
        self.update_state(objects=objects)
        status, reviewed = self.cli("cluster", "--overwrite")
        self.assertEqual(status, 0, reviewed)
        differences = reviewed["overwrite"]["reconcile"]["differences"]
        self.assertEqual({item["name"]: item["status"] for item in differences},
                         {"pg-client": "missing", "previous": "missing"})

    # Inputs: a successful delete followed by a replacement timeout. Outputs: separate durable outcomes.
    # Purpose: make completed deletion visible after a failed replacement. Dependencies: fake Minikube and runner.
    def test_replacement_timeout_reports_completed_deletion_and_archive(self):
        self.create()
        self.record_objects()
        self.update_state(objects={})
        status, reviewed = self.cli("cluster", "--overwrite")
        self.assertEqual(status, 0, reviewed)
        real_run = cluster_module.run

        # Inputs: command and runner options. Outputs: result or simulated timeout.
        # Purpose: inject a timeout only at replacement start. Dependencies: fake runner.
        def timeout_start(command, **options):
            if "start" in command:
                raise RuntimeError("Command timed out")
            return real_run(command, **options)

        with patch.object(cluster_module, "run", side_effect=timeout_start):
            status, result = self.cli("cluster", "--overwrite", "--plan-id", reviewed["plan_id"], "--execute")
        self.assertEqual(status, 2, result)
        self.assertIn("previous profile was deleted", result["error"])
        self.assertIn("minikube start did not finish", result["error"])
        report = read_json(self.workspace / "reports" / "cluster.json")
        self.assertEqual((report["status"], report["deletion"]), ("uncertain", "completed"))
        self.assertIn(report["archive"], result["error"])
        self.assertTrue((Path(report["archive"]) / "state" / "00-bootstrap.tfstate").exists())

    # Inputs: a failing delete, or a delete that leaves a kubeconfig context. Outputs: refusals.
    # Purpose: keep the workspace unarchived and start nothing when the host is not clean. Dependencies: fake tools.
    def test_failed_or_incomplete_delete_keeps_the_workspace(self):
        for flag, message in (("fail_delete", "delete failed"), ("keep_context", "records remain")):
            with self.subTest(flag=flag):
                self.setUp()
                self.create()
                self.record_objects()
                self.update_state(objects={}, **{flag: True})
                _, planned = self.cli("cluster", "--overwrite")
                status, refused = self.cli(
                    "cluster", "--overwrite", "--plan-id", planned["plan_id"], "--execute"
                )
                self.assertEqual(status, 2, refused)
                self.assertIn(message, refused["error"])
                self.assertIn("not archived", refused["error"])
                self.assertEqual(list((self.workspace / "archive").iterdir()), [])
                self.assertTrue((self.workspace / "state" / "00-bootstrap.tfstate").exists())
                consumed = read_json(
                    self.workspace / "plans" / "cluster" / f"consumed-{planned['plan_id']}.json"
                )
                self.assertEqual(consumed["status"], "failed")
                self.assertEqual(self.count("start"), 1)

    # Inputs: delete arguments after the action name. Outputs: plan report. Purpose: plan one deletion.
    # Dependencies: the CLI boundary.
    def plan_delete(self, files, *extra):
        return self.cli("delete", "--workspace-files", files, *extra)

    # Inputs: reviewed plan, workspace-file mode, and extra flags. Outputs: exit status and report.
    # Purpose: execute exactly the reviewed deletion. Dependencies: the CLI boundary.
    def execute_delete(self, planned, files, *extra):
        return self.cli(
            "delete", "--workspace-files", files, *extra, "--plan-id", planned["plan_id"], "--execute"
        )

    # Inputs: none. Outputs: live and recorded data for a healthy delete target.
    # Purpose: ensure the fixture's CRD and Secret have the same ownership as installed resources.
    def record_delete_objects(self):
        self.record_objects()
        objects = json.loads(self.state.read_text())["objects"]
        labels = load_config(self.source)["derived"]["labels"]
        crd = "customresourcedefinitions.apiextensions.k8s.io"
        topic = "kafkatopics.kafka.strimzi.io"
        objects[f"{crd}//{topic}"]["labels"] = labels
        objects["secrets/atlas-app/pg-client"]["labels"] = labels
        self.update_state(objects=objects)
        write_json(self.workspace / "state" / "recorded-objects.json", [
            {"source": "prerequisites", "resource": crd, "namespace": None, "name": topic, "uid": None}
        ])

    # Inputs: none. Outputs: none. Purpose: remove the profile outside this workflow.
    # Dependencies: fake Minikube home layout and state.
    def remove_profile(self):
        profile = self.root / "minikube-home" / ".minikube" / "profiles" / "atlas-dev"
        (profile / "config.json").unlink()
        profile.rmdir()
        state = json.loads(self.state.read_text())
        for key in ("valid", "contexts", "clusters", "users"):
            state[key] = [item for item in state[key] if item != "atlas-dev"]
        self.state.write_text(json.dumps(state), encoding="utf-8")

    # Inputs: a verified profile. Outputs: deletion with kept files and a deleted report.
    # Purpose: keep mode closes cluster actions and allows recreation only through overwrite.
    # Dependencies: fake tools.
    def test_delete_keep_marks_the_report_and_blocks_cluster_actions(self):
        self.create()
        self.record_delete_objects()
        status, planned = self.plan_delete("keep")
        self.assertEqual((status, planned["status"]), (0, "planned"), planned)
        self.assertEqual(planned["action"], "delete")
        self.assertEqual(planned["delete_command"][-2:], ["delete", "--profile=atlas-dev"])
        self.assertIn("namespaces atlas", planned["data_loss"]["managed_objects"])
        self.assertIn("lost", planned["effects"])
        self.assertEqual(self.count("delete"), 0)
        status, report = self.execute_delete(planned, "keep")
        self.assertEqual((status, report["status"]), (0, "deleted"), report)
        self.assertEqual((report["deletion"], self.count("delete")), ("completed", 1))
        for call in self.calls():
            if call["argv"][0] in {"delete", "ssh"}:
                self.assertEqual(call["home"], str(self.root / "minikube-home" / ".minikube"))
        cluster_report = read_json(self.workspace / "reports" / "cluster.json")
        self.assertEqual((cluster_report["status"], cluster_report["delete_plan_id"]), ("deleted", planned["plan_id"]))
        self.assertTrue((self.workspace / "state" / "00-bootstrap.tfstate").exists())
        self.assertTrue((self.workspace / "cluster-binding.json").exists())
        self.assertEqual(read_json(self.workspace / "reports" / "cluster-delete.json")["status"], "deleted")
        status, refused = self.cli("inspect")
        self.assertEqual(status, 2)
        self.assertIn("not created yet", refused["error"])
        status, refused = self.cli("cluster")
        self.assertEqual(status, 2)
        self.assertIn("--overwrite", refused["error"])
        status, replay = self.execute_delete(planned, "keep")
        self.assertEqual(status, 2)
        self.assertIn("consumed", replay["error"])
        status, blocked = self.plan_delete("keep")
        self.assertEqual(status, 2, blocked)
        self.assertIn("already deleted", blocked["blockers"][0])
        status, recreate = self.cli("cluster", "--overwrite")
        self.assertEqual((status, recreate["overwrite"]["action"]), (0, "recreate-missing"), recreate)
        self.write_state(uid="cluster-b", server="https://cluster-b.invalid")
        with patch.object(runtime, "_api_peers", return_value=set()):
            status, created = self.cli(
                "cluster", "--overwrite", "--plan-id", recreate["plan_id"], "--execute"
            )
        self.assertEqual((status, created["status"]), (0, "created"), created)
        archived = read_json(Path(created["archive"]) / "reports" / "cluster.json")
        self.assertEqual(archived["status"], "deleted")

    # Inputs: a profile recreated elsewhere after a kept deletion. Outputs: blocked delete and overwrite.
    # Purpose: never touch a profile this workspace did not create. Dependencies: fake tools.
    def test_kept_deletion_never_claims_a_new_profile_with_the_name(self):
        self.create()
        _, planned = self.plan_delete("keep")
        self.assertEqual(self.execute_delete(planned, "keep")[0], 0)
        profile = self.root / "minikube-home" / ".minikube" / "profiles" / "atlas-dev"
        profile.mkdir(parents=True)
        (profile / "config.json").write_text(json.dumps({"KubernetesConfig": {"KubernetesVersion": "v1.34.0"}}))
        self.update_state(valid=["atlas-dev"], contexts=["atlas-dev"], uid="cluster-z")
        for arguments in (("delete", "--workspace-files", "purge"), ("cluster", "--overwrite")):
            with self.subTest(arguments=arguments):
                status, blocked = self.cli(*arguments)
                self.assertEqual(status, 2, blocked)
                self.assertIn("created elsewhere", blocked["blockers"][0])
        self.assertEqual(self.count("delete"), 1)

    # Inputs: a verified profile. Outputs: deletion, archived files, and a fresh workspace.
    # Purpose: archive mode preserves history and allows a new create plan. Dependencies: fake tools.
    def test_delete_archive_moves_files_and_allows_a_new_cluster(self):
        self.create()
        self.record_delete_objects()
        _, planned = self.plan_delete("archive")
        status, report = self.execute_delete(planned, "archive")
        self.assertEqual((status, report["status"]), (0, "deleted"), report)
        archive = Path(report["archive"])
        self.assertEqual((archive.parent, archive.stat().st_mode & 0o777), (self.workspace / "archive", 0o700))
        self.assertTrue((archive / "state" / "00-bootstrap.tfstate").is_file())
        self.assertTrue((archive / "plans" / "cluster-delete" / f"consumed-{planned['plan_id']}.json").is_file())
        self.assertFalse((self.workspace / "state" / "00-bootstrap.tfstate").exists())
        self.assertFalse((self.workspace / "cluster-binding.json").exists())
        self.assertEqual(read_json(self.workspace / "reports" / "cluster-deleted.json")["archive"], str(archive))
        status, refused = self.execute_delete(planned, "archive")
        self.assertEqual(status, 2)
        self.assertIn("applies only", refused["error"])
        status, fresh = self.cli("cluster")
        self.assertEqual((status, fresh["status"]), (0, "planned"), fresh)

    # Inputs: workspaces with and without archives or unknown files. Outputs: purge results.
    # Purpose: remove only skill-written files and keep archives. Dependencies: fake tools.
    def test_delete_purge_removes_only_skill_files(self):
        self.create()
        self.record_delete_objects()
        (self.workspace / "notes.txt").write_text("user data", encoding="utf-8")
        status, blocked = self.plan_delete("purge")
        self.assertEqual(status, 2, blocked)
        self.assertIn("notes.txt", blocked["blockers"][0])
        (self.workspace / "notes.txt").unlink()
        history = self.workspace / "archive" / "earlier"
        history.mkdir(parents=True, mode=0o700)
        (self.workspace / "archive").chmod(0o700)
        _, planned = self.plan_delete("purge")
        status, report = self.execute_delete(planned, "purge")
        self.assertEqual((status, report["status"]), (0, "deleted"), report)
        self.assertIn("credentials", report["removed"])
        self.assertEqual(sorted(item.name for item in self.workspace.iterdir()), [".operation.lock", "archive"])
        self.assertTrue(history.is_dir())
        self.assertEqual(self.count("delete"), 1)

        self.setUp()
        self.create()
        _, planned = self.plan_delete("purge")
        status, report = self.execute_delete(planned, "purge")
        self.assertEqual((status, report["status"]), (0, "deleted"), report)
        self.assertFalse(self.workspace.exists())
        self.assertTrue(self.kubeconfig.exists())

    # Inputs: stopped, repointed, and replaced profiles. Outputs: blocked plans and no deletion.
    # Purpose: delete only a profile whose identity matches the creation report. Dependencies: fake SSH.
    def test_delete_refuses_unverified_or_foreign_profiles(self):
        cases = {
            "stopped": ({"stopped": True}, "running local profile"),
            "repointed": ({"uid": "foreign", "local_uid": "cluster-a"}, "does not reach"),
            "replaced": ({"uid": "cluster-c"}, "not the cluster this workspace created"),
        }
        for name, (fields, message) in cases.items():
            with self.subTest(case=name):
                self.setUp()
                self.create()
                self.update_state(**fields)
                status, blocked = self.plan_delete("keep")
                self.assertEqual((status, blocked["status"]), (2, "blocked"), blocked)
                self.assertIn(message, blocked["blockers"][0])
                self.assertFalse(blocked["requires_execute"])
                status, refused = self.execute_delete(blocked, "keep")
                self.assertEqual(status, 2)
                self.assertIn("blocked", refused["error"])
                self.assertEqual(self.count("delete"), 0)
                self.assertEqual(read_json(self.workspace / "reports" / "cluster.json")["status"], "created")

    # Inputs: namespaces and claims outside managed state. Outputs: blocked plan until accepted.
    # Purpose: state unmanaged data loss and require explicit acceptance. Dependencies: fake list reads.
    def test_unmanaged_data_requires_explicit_acceptance(self):
        self.create()
        self.record_delete_objects()
        objects = json.loads(self.state.read_text())["objects"]
        objects.update({
            "namespaces//shop": {"uid": "ns-shop"},
            "persistentvolumeclaims/shop/orders": {"uid": "pvc-shop"},
            "persistentvolumeclaims/atlas/data-postgres-0": {"uid": "pvc-pg"},
        })
        self.update_state(objects=objects)
        status, blocked = self.plan_delete("archive")
        self.assertEqual(status, 2, blocked)
        self.assertIn("--include-unmanaged", blocked["blockers"][0])
        self.assertEqual(
            blocked["data_loss"]["unmanaged"],
            {
                "namespaces": ["shop"],
                "persistent_volume_claims": ["atlas/data-postgres-0", "shop/orders"],
                "persistent_volumes": [],
                "objects": [],
            },
        )
        self.assertIn("atlas/data-postgres-0", blocked["data_loss"]["persistent_volume_claims"])
        status, planned = self.plan_delete("archive", "--include-unmanaged")
        self.assertEqual((status, planned["status"]), (0, "planned"), planned)
        status, refused = self.execute_delete(planned, "archive")
        self.assertEqual(status, 2)
        self.assertIn("repeat them exactly", refused["error"])
        status, refused = self.execute_delete(planned, "keep", "--include-unmanaged")
        self.assertEqual(status, 2)
        self.assertIn("repeat them exactly", refused["error"])
        objects["persistentvolumeclaims/shop/invoices"] = {"uid": "pvc-new"}
        self.update_state(objects=objects)
        status, refused = self.execute_delete(planned, "archive", "--include-unmanaged")
        self.assertEqual(status, 2)
        self.assertIn("changed since review", refused["error"])
        self.assertEqual(self.count("delete"), 0)
        _, planned = self.plan_delete("archive", "--include-unmanaged")
        status, report = self.execute_delete(planned, "archive", "--include-unmanaged")
        self.assertEqual((status, report["status"]), (0, "deleted"), report)

    # Inputs: a failed delete and residual kubeconfig records. Outputs: unchanged workspace files.
    # Purpose: change workspace files only after a confirmed, clean deletion. Dependencies: fake tools.
    def test_failed_or_incomplete_profile_delete_keeps_workspace_files(self):
        for flag, message in (("fail_delete", "delete failed"), ("keep_context", "records remain")):
            with self.subTest(flag=flag):
                self.setUp()
                self.create()
                self.record_delete_objects()
                self.update_state(**{flag: True})
                _, planned = self.plan_delete("archive")
                status, refused = self.execute_delete(planned, "archive")
                self.assertEqual(status, 2, refused)
                self.assertIn(message, refused["error"])
                self.assertIn("unchanged", refused["error"])
                self.assertEqual(list((self.workspace / "archive").iterdir()), [])
                self.assertTrue((self.workspace / "state" / "00-bootstrap.tfstate").exists())
                self.assertEqual(read_json(self.workspace / "reports" / "cluster.json")["status"], "created")
                result = read_json(self.workspace / "reports" / "cluster-delete.json")
                self.assertEqual((result["status"], result["plan_id"]), ("failed", planned["plan_id"]))

    # Inputs: a profile removed outside this workflow, with and without leftover records.
    # Outputs: workspace-only handling without minikube delete. Dependencies: fake tools.
    def test_already_absent_profile_changes_only_workspace_files(self):
        self.create()
        self.remove_profile()
        self.update_state(contexts=["atlas-dev"])
        status, blocked = self.plan_delete("keep")
        self.assertEqual(status, 2, blocked)
        self.assertIn("kubeconfig context atlas-dev", blocked["blockers"][0])
        self.update_state(contexts=[])
        status, planned = self.plan_delete("keep")
        self.assertEqual((status, planned["action"], planned["delete_command"]), (0, "already-absent", None))
        status, report = self.execute_delete(planned, "keep")
        self.assertEqual((status, report["deletion"]), (0, "not-needed"), report)
        status, planned = self.plan_delete("purge")
        self.assertEqual((status, planned["action"]), (0, "already-absent"), planned)
        self.assertEqual(self.execute_delete(planned, "purge")[0], 0)
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.count("delete"), 0)

    # Inputs: workspaces without a creation report or minikube home. Outputs: refusals before any effect.
    # Purpose: limit deletion to profiles this workspace created. Dependencies: CLI boundary.
    def test_delete_requires_a_created_profile_with_a_recorded_home(self):
        self.write_config()
        status, refused = self.plan_delete("keep")
        self.assertEqual(status, 2)
        self.assertIn("applies only", refused["error"])
        self.assertEqual(self.calls(), [])
        self.create()
        report_path = self.workspace / "reports" / "cluster.json"
        report = read_json(report_path)
        del report["minikube_home"]
        write_json(report_path, report)
        status, blocked = self.plan_delete("keep")
        self.assertEqual(status, 2, blocked)
        self.assertIn("no recorded Minikube home", blocked["blockers"][0])
        self.assertEqual(self.count("delete"), 0)

    # Inputs: a nested backup before or after review. Outputs: blocked plan or stale approval.
    # Purpose: keep unknown workspace data out of purge. Dependencies: fake Minikube and private files.
    def test_delete_purge_refuses_unknown_nested_artifacts(self):
        self.create()
        backup = self.workspace / "reports" / "manual-backup.txt"
        backup.write_text("manual backup")
        status, blocked = self.plan_delete("purge")
        self.assertEqual(status, 2, blocked)
        self.assertIn("reports/manual-backup.txt", blocked["blockers"][0])
        backup.unlink()
        status, planned = self.plan_delete("purge")
        self.assertEqual(status, 0, planned)
        backup.write_text("added after review")
        status, stale = self.execute_delete(planned, "purge")
        self.assertEqual(status, 2, stale)
        self.assertIn("changed since review", stale["error"])
        self.assertEqual(backup.read_text(), "added after review")
        backup.unlink()
        status, planned = self.plan_delete("purge")
        self.assertEqual(status, 0, planned)
        write_json(self.workspace / "reports" / "cluster.json", {
            **read_json(self.workspace / "reports" / "cluster.json"),
            "post_check": "changed report",
        })
        status, stale = self.execute_delete(planned, "purge")
        self.assertEqual(status, 2, stale)
        self.assertIn("changed since review", stale["error"])
        self.assertEqual(self.count("delete"), 0)

    # Inputs: manual claim, retained PV, default ConfigMap, Secret and CR. Outputs: data requiring consent.
    # Purpose: inventory data across namespaces and API resources without returning payloads.
    def test_delete_lists_unmanaged_data_in_managed_and_default_namespaces(self):
        self.create()
        self.record_delete_objects()
        objects = json.loads(self.state.read_text())["objects"]
        objects.update({
            "persistentvolumeclaims/atlas/user-claim": {"uid": "uid-claim"},
            "persistentvolumes//retained": {
                "uid": "uid-pv",
                "spec": {"persistentVolumeReclaimPolicy": "Retain"},
                "status": {"phase": "Released"},
            },
            "configmaps/default/notes": {"uid": "uid-config"},
            "secrets/kube-system/custom": {"uid": "uid-secret"},
            "widgets.example.com/default/example": {"uid": "uid-widget"},
        })
        self.update_state(objects=objects)
        status, blocked = self.plan_delete("keep")
        self.assertEqual(status, 2, blocked)
        self.assertIn("--include-unmanaged", blocked["blockers"][0])
        unmanaged = blocked["data_loss"]["unmanaged"]
        self.assertEqual(unmanaged["persistent_volume_claims"], ["atlas/user-claim"])
        self.assertEqual(unmanaged["persistent_volumes"], ["retained"])
        self.assertEqual(
            {entry["uid"] for entry in unmanaged["objects"]},
            {"uid-config", "uid-secret", "uid-widget"},
        )
        status, approved = self.plan_delete("keep", "--include-unmanaged")
        self.assertEqual(status, 0, approved)
        objects["persistentvolumeclaims/atlas/user-claim"]["uid"] = "uid-replacement"
        self.update_state(objects=objects)
        status, stale = self.execute_delete(approved, "keep", "--include-unmanaged")
        self.assertEqual(status, 2, stale)
        self.assertIn("changed since review", stale["error"])
        self.assertEqual(self.count("delete"), 0)

    # Inputs: a recorded controller, verified descendant claim and matching bound PV.
    # Outputs: managed storage and rejection of a stale controller UID. Dependencies: fake API.
    def test_delete_verifies_controller_and_pv_binding(self):
        self.create()
        self.record_delete_objects()
        records_path = self.workspace / "state" / "recorded-objects.json"
        records = read_json(records_path)
        records.append({
            "source": "prerequisites", "resource": "statefulsets.apps", "namespace": "atlas",
            "name": "database", "uid": "uid-controller",
        })
        write_json(records_path, records)
        objects = json.loads(self.state.read_text())["objects"]
        objects.update({
            "statefulsets.apps/atlas/database": {"uid": "uid-controller"},
            "persistentvolumeclaims/atlas/db": {
                "uid": "uid-claim", "spec": {"volumeName": "db-pv"},
                "ownerReferences": [{
                    "name": "database", "kind": "StatefulSet", "uid": "uid-controller",
                    "controller": True,
                }],
            },
            "persistentvolumes//db-pv": {
                "uid": "uid-pv", "status": {"phase": "Bound"},
                "spec": {"claimRef": {"namespace": "atlas", "name": "db", "uid": "uid-claim"}},
            },
        })
        self.update_state(objects=objects)
        status, planned = self.plan_delete("keep")
        self.assertEqual(status, 0, planned)
        self.assertIn("persistentvolumeclaims atlas/db", planned["data_loss"]["managed_objects"])
        self.assertIn("persistentvolumes db-pv", planned["data_loss"]["managed_objects"])
        objects["persistentvolumeclaims/atlas/db"]["ownerReferences"][0]["uid"] = "stale"
        self.update_state(objects=objects)
        status, blocked = self.plan_delete("keep")
        self.assertEqual(status, 2, blocked)
        self.assertEqual(blocked["data_loss"]["unmanaged"]["persistent_volume_claims"], ["atlas/db"])
        self.assertEqual(blocked["data_loss"]["unmanaged"]["persistent_volumes"], ["db-pv"])

    # Inputs: failed discovery or denied list access. Outputs: blocked deletion even with consent.
    # Purpose: avoid treating an unreadable inventory as empty. Dependencies: fake API.
    def test_delete_blocks_incomplete_inventory(self):
        self.create()
        for failure in ("incomplete_discovery", "secret_api_denied"):
            with self.subTest(failure=failure):
                self.update_state(**{failure: True})
                status, blocked = self.plan_delete("keep", "--include-unmanaged")
                self.assertEqual(status, 2, blocked)
                self.assertIn("complete", blocked["blockers"][0])
                self.update_state(**{failure: False})
        self.assertEqual(self.count("delete"), 0)

    # Inputs: a zero exit code and vanished Minikube records with a running VM.
    # Outputs: uncertain removal and preserved workspace, including on retry. Dependencies: fake driver.
    def test_delete_keeps_workspace_when_vm_survives(self):
        self.create()
        self.record_delete_objects()
        self.update_state(retain_vm=True)
        status, planned = self.plan_delete("purge")
        self.assertEqual(status, 0, planned)
        status, result = self.execute_delete(planned, "purge")
        self.assertEqual(status, 2, result)
        self.assertIn("VM removal could not be confirmed", result["error"])
        report = read_json(self.workspace / "reports" / "cluster-delete.json")
        self.assertEqual((report["status"], report["deletion"]), ("uncertain", "uncertain"))
        self.assertTrue((self.workspace / "state" / "00-bootstrap.tfstate").exists())
        status, refused = self.plan_delete("purge")
        self.assertEqual(status, 2, refused)
        self.assertIn("unconfirmed VM removal", " ".join(refused["blockers"]))
        self.assertEqual(self.count("delete"), 1)

    # Inputs: another context that uses the target's cluster or user. Outputs: blocked delete.
    # Purpose: preserve shared kubeconfig references. Dependencies: fake kubeconfig.
    def test_delete_refuses_shared_kubeconfig_entries(self):
        self.create()
        status, planned = self.plan_delete("keep")
        self.assertEqual(status, 0, planned)
        for key in ("cluster", "user"):
            with self.subTest(key=key):
                other = {"cluster": "other-cluster", "user": "other-user", key: "atlas-dev"}
                self.update_state(contexts=["atlas-dev", "other"], context_refs={"other": other})
                status, blocked = self.plan_delete("keep", "--include-unmanaged")
                self.assertEqual(status, 2, blocked)
                self.assertIn("Other kubeconfig contexts use profile entries: other", blocked["blockers"])
        status, stale = self.execute_delete(planned, "keep")
        self.assertEqual(status, 2, stale)
        self.assertIn("changed since review", stale["error"])
        self.assertEqual(self.count("delete"), 0)


class DeleteSafetyTests(unittest.TestCase):
    # Inputs: synthetic profile and PID files. Outputs: both actual VM IDs and removal checks.
    # Purpose: verify native probes are independent of the profile records. Dependencies: private files.
    def test_krunkit_machine_evidence_tracks_each_profile_node(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profile = home / "profiles" / "atlas"
            profile.mkdir(parents=True, mode=0o700)
            write_json(profile / "config.json", {
                "Driver": "krunkit", "Nodes": [{"Name": ""}, {"Name": "m02"}]
            })
            for machine, pid in (("atlas", 12345), ("atlas-m02", 12346)):
                folder = home / "machines" / machine
                folder.mkdir(parents=True)
                (folder / "krunkit.pid").write_text(str(pid))
            with patch.object(delete_safety, "_krunkit_process", return_value=True) as native:
                evidence = delete_safety.machine_evidence(home, "atlas")
            self.assertEqual(evidence, {"driver": "krunkit", "pids": [12345, 12346]})
            self.assertEqual([call.args[0] for call in native.call_args_list], [12345, 12346])
            with patch.object(delete_safety, "_process_exists", side_effect=lambda pid: pid == 12346):
                self.assertFalse(delete_safety.machines_removed(evidence))
            with patch.object(delete_safety, "_process_exists", return_value=False):
                self.assertTrue(delete_safety.machines_removed(evidence))
            self.assertFalse(delete_safety.machines_removed({"driver": "vfkit", "pids": None}))


class SecretMetadataTests(unittest.TestCase):
    # Inputs: a synthetic API response stream. Outputs: exact request and decoded metadata.
    # Purpose: verify HTTP negotiation without opening a socket. Dependencies: mocked socket and HTTP parser.
    def test_get_sends_a_metadata_only_accept_header(self):
        metadata = {"name": "pg-client", "namespace": "atlas-app", "uid": "secret-1"}
        body = json.dumps({
            "kind": "PartialObjectMetadata", "apiVersion": "meta.k8s.io/v1",
            "metadata": metadata,
        }).encode()
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        with patch.object(secret_metadata.socket, "socket") as factory:
            connection = factory.return_value.__enter__.return_value
            connection.makefile.return_value = io.BytesIO(response)
            result = secret_metadata.get(Path("/unused/api.sock"), "atlas-app", "pg-client")
        self.assertEqual(result, metadata)
        connection.connect.assert_called_once_with("/unused/api.sock")
        request = connection.sendall.call_args.args[0]
        self.assertIn(b"GET /api/v1/namespaces/atlas-app/secrets/pg-client HTTP/1.1\r\n", request)
        self.assertIn(b"Accept: application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1\r\n", request)


if __name__ == "__main__":
    unittest.main()
