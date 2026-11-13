"""Purpose: verify provider diagnosis and the complete offline Terraform profile matrix with fake tools."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate as module


class ValidationTests(unittest.TestCase):
    # Inputs: none. Outputs: isolated report path. Purpose: keep test evidence private. Dependencies: tempfile.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.report_dir = self.root / "report"

    # Inputs: Terraform command and subprocess options. Outputs: successful fake result.
    # Purpose: inspect actual generated roots without Terraform or cluster access. Dependencies: generated JSON files.
    def fake_success(self, command, **options):
        self.assertEqual(command[0], "terraform")
        directory = Path(command[1].split("=", 1)[1])
        operation = command[2]
        if operation == "version":
            output = json.dumps({"terraform_version": "1.15.8"})
        elif operation == "validate" and command[3:] == ["-help"]:
            output = "Usage: validate [-var-file=filename]"
        elif operation == "init":
            self.assertIn("-backend=false", command)
            self.assertIn("-lockfile=readonly", command)
            output = "initialized"
        elif operation == "providers":
            self.assertEqual(command[3:], ["schema", "-json"])
            self.assertNotIn('provider "kubernetes"', (directory / "main.tf").read_text())
            output = json.dumps(
                {"provider_schemas": {"registry.terraform.io/hashicorp/kubernetes": {}}}
            )
        elif operation == "validate":
            self.assertEqual(command[3:], ["-json", "-var-file=environment.auto.tfvars.json"])
            config = json.loads((directory / "environment.auto.tfvars.json").read_text())[
                "environment"
            ]
            self.assertIn(directory.name, config["derived"]["stages"])
            kubeconfig = json.loads(Path(config["cluster"]["kubeconfig"]).read_text())
            self.assertEqual(kubeconfig["clusters"][0]["cluster"]["server"], "https://127.0.0.1:1")
            self.assertEqual(kubeconfig["users"][0]["user"], {})
            output = json.dumps({"valid": True, "error_count": 0, "warning_count": 0})
        else:
            self.fail(f"Unexpected operation: {operation}")
        return subprocess.CompletedProcess(command, 0, output, "")

    # Inputs: successful provider and stage fixtures. Outputs: 40 passing cases over 12 profiles.
    # Purpose: prove traversal matches supported combinations without live commands. Dependencies: fake_success.
    def test_complete_profile_matrix_and_private_artifacts(self):
        with patch.object(module.subprocess, "run", side_effect=self.fake_success) as process:
            report = module.validate(self.report_dir)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["cases_passed"], 40)
        self.assertEqual(len({case["profile"] for case in report["cases"]}), 12)
        self.assertEqual(process.call_count, 84)
        self.assertEqual(report["terraform_version"], "1.15.8")
        for path in self.report_dir.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))

    # Inputs: exact provider Unix-socket denial in a private trace. Outputs: classified early stop.
    # Purpose: distinguish host policy from HCL errors. Dependencies: mocked provider launch.
    def test_socket_denial_stops_before_profile_validation(self):
        # Inputs: command/options. Outputs: simulated launch result. Purpose: create exact diagnostic evidence.
        # Dependencies: the runner's private trace path.
        def blocked(command, **options):
            if command[2] != "providers":
                return self.fake_success(command, **options)
            Path(options["env"]["TF_LOG_PATH"]).write_text(
                "listen unix /private/tmp/plugin123: bind: operation not permitted"
            )
            return subprocess.CompletedProcess(command, 1, "", "Failed to load plugin schemas")

        with patch.object(module.subprocess, "run", side_effect=blocked) as process:
            report = module.validate(self.report_dir)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["cause"], "local_socket_denied")
        self.assertEqual(report["cases"], [])
        self.assertEqual(process.call_count, 4)
        self.assertFalse((self.report_dir / "profiles").exists())

    # Inputs: different provider launch error. Outputs: distinct generic provider cause.
    # Purpose: avoid attributing every schema failure to socket permissions. Dependencies: fake subprocess.
    def test_unrelated_provider_failure_is_not_socket_denial(self):
        # Inputs: command/options. Outputs: unrelated schema error. Purpose: separate protocol failures.
        # Dependencies: fake_success for all earlier command phases.
        def protocol_error(command, **options):
            return (
                subprocess.CompletedProcess(command, 1, "", "unsupported protocol")
                if command[2] == "providers"
                else self.fake_success(command, **options)
            )

        with patch.object(module.subprocess, "run", side_effect=protocol_error):
            report = module.validate(self.report_dir)
        self.assertEqual(report["cause"], "provider_schema_failed")

    # Inputs: ambient transport and execution overrides. Outputs: sanitized command environment.
    # Purpose: isolate diagnosis from unrelated Terraform configuration. Dependencies: subprocess interception.
    def test_environment_strips_transport_and_cli_overrides(self):
        environment = {
            "PATH": "/tools",
            "TF_REATTACH_PROVIDERS": "other-process",
            "TF_CLI_CONFIG_FILE": "/other",
            "TF_CLI_ARGS": "-target=other",
            "PLUGIN_PROTOCOL_VERSIONS": "99",
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(
                module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, "", "init failed"),
            ) as process,
        ):
            module.validate(self.report_dir)
        passed = process.call_args.kwargs["env"]
        self.assertEqual(passed["PATH"], "/tools")
        self.assertFalse(
            {
                "TF_REATTACH_PROVIDERS",
                "TF_CLI_CONFIG_FILE",
                "TF_CLI_ARGS",
                "PLUGIN_PROTOCOL_VERSIONS",
            }
            & set(passed)
        )
        self.assertEqual(passed["TF_LOG"], "TRACE")
        self.assertEqual(passed["CHECKPOINT_DISABLE"], "1")

    # Inputs: a Terraform CLI without variable-file validation support. Outputs: clear early block.
    # Purpose: avoid claiming profile evaluation on an older CLI. Dependencies: fake help output.
    def test_unsupported_variable_file_validation_is_explicit(self):
        # Inputs: command/options. Outputs: older CLI help. Purpose: simulate unsupported feature.
        # Dependencies: fake_success for the version command.
        def old_help(command, **options):
            return (
                subprocess.CompletedProcess(command, 0, "Usage: validate [-json]", "")
                if command[2] == "validate"
                else self.fake_success(command, **options)
            )

        with patch.object(module.subprocess, "run", side_effect=old_help) as process:
            report = module.validate(self.report_dir)
        self.assertEqual(report["cause"], "validate_variable_files_unsupported")
        self.assertEqual(process.call_count, 2)
        self.assertEqual(report["cases"], [])

    # Inputs: existing report content. Outputs: refusal without subprocess calls.
    # Purpose: preserve prior evidence. Dependencies: report-directory boundary.
    def test_nonempty_report_directory_is_preserved(self):
        self.report_dir.mkdir(mode=0o700)
        marker = self.report_dir / "old-report.json"
        marker.write_text("original")
        with patch.object(module.subprocess, "run") as process:
            with self.assertRaisesRegex(ValueError, "empty"):
                module.validate(self.report_dir)
        process.assert_not_called()
        self.assertEqual(marker.read_text(), "original")

    # Inputs: a report nested under installation. Outputs: rejection before directory creation.
    # Purpose: keep installed skill assets immutable. Dependencies: path boundary.
    def test_reports_cannot_be_written_inside_installed_skill(self):
        installation = self.root / "installation"
        installation.mkdir()
        nested = installation / "diagnostics"
        with (
            patch.object(module, "ROOT", installation),
            self.assertRaisesRegex(ValueError, "outside"),
        ):
            module.validate(nested)
        self.assertFalse(nested.exists())


if __name__ == "__main__":
    unittest.main()
