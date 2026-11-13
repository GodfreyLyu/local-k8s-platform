"""Purpose: check external filesystem, command-environment, and deployment-record trust boundaries."""

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import credentials
import io_utils
import runtime
from config import load_config

SPEC = importlib.util.spec_from_file_location("platform_boundary_cli", SCRIPTS / "platform.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class BoundaryTests(unittest.TestCase):
    # Inputs: none. Outputs: isolated config/workspace. Purpose: isolate boundary tests. Dependencies: tempfile.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "input.json"
        self.source.write_text(
            json.dumps({"environment_id": "atlas", "storage": {"class": "dev-retain"}}),
            encoding="utf-8",
        )
        self.config = load_config(self.source)

    # Inputs: workspace nested in installed assets. Outputs: refusal and no directory creation.
    # Purpose: prevent recursive source copying. Dependencies: CLI path boundary and mocked installation root.
    def test_workspace_cannot_live_under_installed_skill(self):
        installation = self.root / "installed-skill"
        installation.mkdir()
        workspace = installation / "assets" / "nested-workspace"
        with patch.object(CLI, "ROOT", installation), redirect_stderr(io.StringIO()):
            status = CLI.main(
                ["--config", str(self.source), "--workspace", str(workspace), "configure"]
            )
        self.assertEqual(status, 2)
        self.assertFalse(workspace.exists())

    # Inputs: hostile ambient Terraform variables. Outputs: controlled subprocess environment.
    # Purpose: preserve reviewed state/arguments and private logs. Dependencies: subprocess interception.
    def test_terraform_environment_cannot_redirect_state_or_arguments(self):
        environment = {
            "TF_DATA_DIR": "/other",
            "TF_WORKSPACE": "other",
            "TF_CLI_ARGS_plan": "-target=unreviewed",
            "TF_VAR_environment": "unreviewed",
            "TF_LOG_PATH": "/public/log",
            "TF_REATTACH_PROVIDERS": "unreviewed-provider-connection",
            "PLUGIN_PROTOCOL_VERSIONS": "unreviewed-protocol",
            "PATH": "/test-tools",
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(
                io_utils.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ) as process,
        ):
            io_utils.run(["terraform", "-chdir=/private/stage", "plan"])
        passed = process.call_args.kwargs
        self.assertEqual(passed["env"]["TF_WORKSPACE"], "default")
        self.assertEqual(passed["umask"], 0o077)
        self.assertFalse(
            {
                "TF_DATA_DIR",
                "TF_CLI_ARGS_plan",
                "TF_VAR_environment",
                "TF_LOG_PATH",
                "TF_REATTACH_PROVIDERS",
                "PLUGIN_PROTOCOL_VERSIONS",
            }
            & set(passed["env"])
        )
        self.assertEqual(passed["env"]["PATH"], "/test-tools")

    # Inputs: a second operation in the same workspace. Outputs: rejection then successful reuse.
    # Purpose: serialize mutation and materialization. Dependencies: POSIX advisory locking.
    def test_workspace_lock_prevents_overlapping_operations(self):
        workspace = self.root / "workspace"
        with io_utils.workspace_lock(workspace):
            with self.assertRaisesRegex(ValueError, "already using"):
                with io_utils.workspace_lock(workspace):
                    self.fail("Second operation acquired the lock")
        with io_utils.workspace_lock(workspace):
            self.assertTrue(workspace.is_dir())

    # Inputs: symlinked artifact destination. Outputs: refusal without changing target.
    # Purpose: prevent private writes escaping the workspace. Dependencies: filesystem symlinks.
    def test_artifact_writes_reject_symbolic_links(self):
        target = self.root / "target.json"
        target.write_text("unchanged", encoding="utf-8")
        link = self.root / "link.json"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            io_utils.write_json(link, {"change": True})
        self.assertEqual(target.read_text(), "unchanged")

    # Inputs: failed bootstrap record and an otherwise available cluster. Outputs: no credential API calls.
    # Purpose: prevent partially created namespaces authorizing Secret writes. Dependencies: mocked inspect.
    def test_failed_bootstrap_record_blocks_credentials(self):
        workspace = self.root / "workspace"
        io_utils.private_dir(workspace)
        io_utils.write_json(
            workspace / "reports" / "applied-00-bootstrap.json", {"status": "failed"}
        )
        with (
            patch.object(runtime, "inspect", return_value={"status": "pass"}),
            patch.object(credentials, "kube", side_effect=AssertionError("credential API call")),
        ):
            with self.assertRaisesRegex(ValueError, "bootstrap"):
                credentials.credentials(self.config, workspace, execute=True)

    # Inputs: one successful Secret and a later refusal. Outputs: preserved partial report without values.
    # Purpose: make interrupted multi-Secret handoff reviewable. Dependencies: mocked cluster access.
    def test_partial_credential_publication_records_completed_references(self):
        workspace = self.root / "workspace"
        io_utils.private_dir(workspace)
        io_utils.write_json(
            workspace / "reports" / "applied-00-bootstrap.json", {"status": "applied"}
        )
        documents = [
            credentials.secret_document(
                self.config, "atlas-data", "one", {"password": "never-print"}
            ),
            credentials.secret_document(
                self.config, "atlas-app", "two", {"password": "never-print"}
            ),
        ]
        owned_namespace = {"metadata": {"labels": self.config["derived"]["labels"]}}
        with (
            patch.object(runtime, "inspect", return_value={"status": "pass"}),
            patch.object(credentials, "kube", return_value=owned_namespace),
            patch.object(credentials, "postgres_documents", return_value=documents),
            patch.object(credentials, "publish", side_effect=["created", ValueError("collision")]),
        ):
            with self.assertRaisesRegex(ValueError, "collision"):
                credentials.credentials(self.config, workspace, execute=True)
        report = io_utils.read_json(workspace / "reports" / "credentials-postgres.json")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["results"], [{"reference": "atlas-data/one", "status": "created"}])
        self.assertNotIn("never-print", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
