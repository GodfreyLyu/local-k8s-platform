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
    if state.get("fail_start"):
        sys.exit(1)
    name = next(item.split("=", 1)[1] for item in arguments if item.startswith("--profile="))
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
                {"name": name, "context": {
                    "cluster": (state["clusters"] or [name])[0],
                    "user": (state["users"] or [name])[0],
                }}
                for name in state["contexts"]
            ],
            "clusters": [{"name": name} for name in state["clusters"]],
            "users": [{"name": name} for name in state["users"]],
        }))
elif state.get("unreachable"):
    sys.exit(1)
elif "namespaces" in arguments and "kube-system" in arguments:
    print(json.dumps({"metadata": {"uid": state["uid"]}}))
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


class ClusterTests(unittest.TestCase):
    # Inputs: none. Outputs: private fake tools, state, and environment. Purpose: isolate every test.
    # Dependencies: tempfile and the current Python interpreter for fake executables.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, source in (("minikube", FAKE_MINIKUBE), ("kubectl", FAKE_KUBECTL)):
            tool = bin_dir / name
            tool.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
            tool.chmod(0o700)
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
                "MINIKUBE_HOME": str(self.root / "minikube-home"),
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
        self.workspace = self.root / "workspace"
        self.source = self.root / "environment.json"

    # Inputs: proxy socket path, API route and requested media type. Outputs: a fake metadata response.
    # Purpose: exercise content negotiation and error handling without binding a socket.
    def metadata_request(self, path, route, accept):
        self.assertEqual(path, self.root / "api.sock")
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
