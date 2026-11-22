"""Purpose: test reviewed, create-only Minikube profile creation with private fake tools."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import runtime
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
    log.write(json.dumps({"tool": "minikube", "argv": arguments, "kubeconfig": os.environ.get("KUBECONFIG")}) + "\\n")
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
    state["valid"].append(name)
    state["contexts"].append(name)
    state["clusters"].append(name)
    state["users"].append(name)
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
elif "namespaces" in arguments and "kube-system" in arguments:
    print(json.dumps({"metadata": {"uid": state["uid"]}}))
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
        self.workspace = self.root / "workspace"
        self.source = self.root / "environment.json"

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
                }
            ),
            encoding="utf-8",
        )

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


if __name__ == "__main__":
    unittest.main()
