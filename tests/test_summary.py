"""Purpose: verify the uniform current/add/change/delete step summary and its text view offline."""

import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import platform as CLI
import summary

IDENTITY = {
    "context": "atlas-dev",
    "kubeconfig": "/private/kubeconfig",
    "server": "https://192.168.105.2:8443",
    "kube_system_uid": "uid-1",
}


# Inputs: action and option overrides. Outputs: argparse-like namespace. Purpose: mirror the CLI
# arguments that summarize reads. Dependencies: argparse.Namespace.
def args(action, **options):
    defaults = {
        "config": Path("environment.json"),
        "workspace": Path("/private/ws"),
        "overwrite": False,
        "confirm_legacy_vm_removal": False,
        "workspace_files": "archive",
        "include_unmanaged": False,
        "component": "postgres",
        "stage": "30-postgres",
        "expiry": False,
        "backup_dir": Path("/private/backups"),
        "with_fixture": False,
        "backup": Path("/private/backups/b1"),
        "target_database": None,
        "keep_target": False,
    }
    return Namespace(action=action, **{**defaults, **options})


class SummaryTests(unittest.TestCase):
    # Inputs: none. Outputs: assertions. Purpose: a stage plan lists creates and field-level updates,
    # states that it never deletes, and points at the preview. Dependencies: summarize, render.
    def test_stage_plan_lists_additions_updates_and_no_deletions(self):
        report = {
            "status": "planned",
            "stage": "30-postgres",
            "plan_id": "a" * 64,
            "identity": IDENTITY,
            "changes": [
                {"actions": ["create"], "resource": "statefulsets.apps", "name": "pg",
                 "namespace": "data", "fields": {"spec.replicas": {"after": 1}}},
                {"actions": ["update"], "resource": "configmaps", "name": "pg-config",
                 "namespace": "data", "fields": {"data.max": {"before": "50", "after": "100"}}},
                {"actions": ["no-op"], "resource": "namespaces", "name": "data", "namespace": None},
            ],
        }
        result = summary.summarize(args("plan"), report)
        self.assertEqual(result["add"], [{"item": "statefulsets.apps data/pg", "detail": "spec.replicas=1"}])
        self.assertEqual(result["change"][0]["fields"], [{"field": "data.max", "before": "50", "after": "100"}])
        self.assertEqual(result["delete"], [])
        self.assertEqual(result["unchanged"], [{"item": "namespaces data", "detail": "no-op"}])
        text = summary.render(result)
        self.assertIn("Target: context atlas-dev", text)
        self.assertIn("  + statefulsets.apps data/pg: spec.replicas=1", text)
        self.assertIn("      data.max: 50 -> 100", text)
        self.assertIn("Will delete: none; stage plans that delete or import are rejected", text)
        self.assertIn(f"apply --stage 30-postgres --plan-id {'a' * 64}", text)

    # Inputs: none. Outputs: assertions. Purpose: a delete plan lists the profile, each PVC and PV,
    # unmanaged data, and workspace handling, and repeats the reviewed options. Dependencies: summarize.
    def test_delete_plan_lists_data_loss_and_repeats_options(self):
        report = {
            "status": "planned",
            "plan_id": "b" * 64,
            "profile": "atlas-dev",
            "minikube_home": "/private/mk/.minikube",
            "action": "delete",
            "delete_command": ["minikube", "delete", "--profile=atlas-dev"],
            "workspace_files": "purge",
            "data_loss": {
                "managed_objects": ["namespaces data"],
                "persistent_volume_claims": ["data/pg-data"],
                "objects": [{"resource": "persistentvolumes", "name": "pv-1", "uid": "u-pv"}],
                "unmanaged": {"namespaces": ["scratch"], "objects": []},
            },
            "effects": "All data on the profile is lost.",
            "blockers": [],
            "requires_execute": True,
        }
        options = args("delete", workspace_files="purge", include_unmanaged=True)
        result = summary.summarize(options, report)
        deleted = [entry["item"] for entry in result["delete"]]
        self.assertEqual(deleted, [
            "Minikube profile atlas-dev",
            "PersistentVolumeClaim data/pg-data",
            "PersistentVolume pv-1",
            "objects this environment manages",
            "unmanaged namespace scratch",
            "workspace files written by this skill",
        ])
        self.assertEqual(result["add"], [])
        self.assertIn("--workspace-files purge --include-unmanaged --plan-id", result["next"])
        self.assertTrue(result["next"].endswith("--execute  (needs your authorization)"))

    # Inputs: none. Outputs: assertions. Purpose: an overwrite plan shows drift, the deleted profile,
    # the archived workspace and the replacement. Dependencies: summarize.
    def test_overwrite_plan_shows_drift_deletion_and_replacement(self):
        report = {
            "status": "planned",
            "plan_id": "c" * 64,
            "profile": "atlas-dev",
            "kubeconfig": "/private/kubeconfig",
            "minikube": {"path": "/bin/minikube", "version": "v1.37.0"},
            "minikube_home": "/private/mk/.minikube",
            "cluster": {"driver": "krunkit", "nodes": 1, "cpus": 4, "memory_mib": 8192,
                        "disk_mib": 40960, "kubernetes_version": "v1.35.0", "cni": "calico", "addons": []},
            "blockers": [],
            "command": ["minikube", "start", "--profile=atlas-dev"],
            "requires_execute": True,
            "overwrite": {
                "action": "overwrite",
                "archive_directory": "/private/ws/archive",
                "reconcile": {
                    "identity": {"status": "match", "recorded": {"kube_system_uid": "u1"},
                                 "observed": {"kube_system_uid": "u1"}},
                    "checked": 3,
                    "differences": [{"resource": "secrets", "name": "pg-tls", "namespace": "data",
                                     "status": "missing"}],
                },
                "effects": "Data is lost.",
            },
        }
        result = summary.summarize(args("cluster", overwrite=True), report)
        self.assertIn({"item": "secrets data/pg-tls", "detail": "missing"}, result["current"])
        self.assertEqual(result["delete"][0]["item"], "Minikube profile atlas-dev")
        self.assertEqual(result["delete"][1]["detail"], "moved to /private/ws/archive")
        self.assertEqual(result["add"][0]["item"], "Minikube profile atlas-dev")
        self.assertIn("cluster --overwrite --plan-id", result["next"])

    # Inputs: none. Outputs: assertions. Purpose: a blocked plan offers no execute command.
    # Dependencies: summarize, render.
    def test_blocked_plan_shows_blockers_without_execute_command(self):
        report = {"status": "blocked", "plan_id": "d" * 64, "profile": "atlas-dev", "action": "none",
                  "workspace_files": "keep", "blockers": ["Profile atlas-dev is stopped."],
                  "requires_execute": False}
        result = summary.summarize(args("delete", workspace_files="keep"), report)
        text = summary.render(result)
        self.assertIn("  ! Profile atlas-dev is stopped.", text)
        self.assertNotIn("--execute", text)

    # Inputs: none. Outputs: assertions. Purpose: prerequisite plans separate reused installs from
    # new objects. Dependencies: summarize.
    def test_prerequisites_plan_separates_reused_and_created(self):
        report = {
            "status": "planned", "plan_id": "e" * 64, "target": IDENTITY, "reused": ["prometheus"],
            "changes": [{"action": "create", "reference": {"resource": "namespaces", "name": "strimzi",
                                                           "namespace": None}}],
            "managed_memory_mib": 256, "requires_execute": True,
        }
        result = summary.summarize(args("prerequisites", component="all"), report)
        self.assertEqual(result["add"], [{"item": "namespaces strimzi"}])
        self.assertEqual(result["unchanged"][0]["item"], "prometheus")
        self.assertEqual(result["delete_note"], summary.NEVER_DELETES)

    # Inputs: none. Outputs: assertions. Purpose: acceptance plans list writes and cleanup, and keep
    # untouched data visible. Dependencies: summarize.
    def test_acceptance_plan_lists_writes_cleanup_and_untouched(self):
        report = {"status": "planned", "requires_execute": True, "plan_id": "f" * 64,
                  "identity": IDENTITY, "target": {"uid": "s1"}, "reads": ["Read the Service"],
                  "writes": ["Create one Pod"], "cleanup": ["Delete the Pod"],
                  "untouched": ["Application rows"]}
        result = summary.summarize(args("acceptance"), report)
        self.assertEqual(result["add"], [{"item": "Create one Pod"}])
        self.assertEqual(result["delete"], [{"item": "Delete the Pod"}])
        self.assertEqual(result["unchanged"], [{"item": "Application rows"}])

    # Inputs: none. Outputs: assertions. Purpose: read-only steps say so and list current checks.
    # Dependencies: summarize, render.
    def test_inspect_is_read_only(self):
        report = {"status": "pass", "identity": IDENTITY,
                  "checks": [{"name": "kubernetes_profile", "status": "pass", "detail": {}}]}
        text = summary.render(summary.summarize(args("inspect"), report))
        self.assertIn("  * kubernetes_profile: pass", text)
        self.assertIn("Changes: none; this step is read-only", text)

    # Inputs: none. Outputs: assertions. Purpose: execution results use past tense and report errors.
    # Dependencies: summarize, render.
    def test_execution_result_uses_past_tense(self):
        report = {"status": "created", "plan_id": "a" * 64, "profile": "atlas-dev",
                  "suggested_api_server_cidrs": ["192.168.105.2/32"]}
        text = summary.render(summary.summarize(args("cluster"), report))
        self.assertIn("Added (1)", text)
        self.assertIn("  + Minikube profile atlas-dev: created and identity recorded", text)
        self.assertIn("Deleted: none", text)

    # Inputs: none. Outputs: assertions. Purpose: credential summaries name Secrets, never values.
    # Dependencies: summarize.
    def test_credentials_preview_names_secrets_only(self):
        report = {"status": "preview", "component": "postgres", "cluster_writes": False,
                  "secret_references": ["data/pg-admin"]}
        result = summary.summarize(args("credentials"), report)
        self.assertEqual(result["add"][0]["item"], "Secret data/pg-admin")
        self.assertIn("credentials --component postgres --prepare", result["next"])


class CommandBoundaryTests(unittest.TestCase):
    # Inputs: none. Outputs: private workspace and configuration. Dependencies: temporary storage.
    # Purpose: run the real CLI offline without touching real workspaces.
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.source = root / "environment.json"
        self.source.write_text(json.dumps({"environment_id": "atlas", "storage": {"class": "standard"}}))
        self.workspace = root / "workspace"

    # Inputs: none. Outputs: stdout, stderr and status. Purpose: run configure through main.
    # Dependencies: platform.main.
    def configure(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = CLI.main(["--config", str(self.source), "--workspace", str(self.workspace), "configure"])
        return status, stdout.getvalue(), stderr.getvalue()

    # Inputs: none. Outputs: assertions. Purpose: stdout stays one JSON document with the summary,
    # and the readable block goes to stderr. Dependencies: platform.main.
    def test_configure_prints_json_and_summary_block(self):
        status, stdout, stderr = self.configure()
        self.assertEqual(status, 0)
        report = json.loads(stdout)
        self.assertEqual(report["summary"]["phase"], "read-only")
        self.assertIn("== configure [configured] ==", stderr)
        self.assertIn("Kubernetes not contacted", stderr)

    # Inputs: none. Outputs: assertions. Purpose: a summary defect never changes the outcome.
    # Dependencies: platform.main.
    def test_summary_failure_keeps_report_and_status(self):
        with patch.object(CLI, "summarize", side_effect=KeyError("x")):
            status, stdout, stderr = self.configure()
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout)["status"], "configured")
        self.assertIn("Summary unavailable", stderr)


if __name__ == "__main__":
    unittest.main()
