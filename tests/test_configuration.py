"""Purpose: verify configuration boundaries, isolated materialization, and portable packaging offline."""

import importlib.util
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from config import load_config
from io_utils import read_json, workspace_lock, write_json
from package import build

SPEC = importlib.util.spec_from_file_location("platform_cli", SCRIPTS / "platform.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class ConfigurationTests(unittest.TestCase):
    # Inputs: none. Outputs: temporary fixture. Purpose: isolate every test. Dependencies: tempfile.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "input.json"

    # Inputs: config overrides. Outputs: validated config. Purpose: prepare user input. Dependencies: JSON boundary.
    def config(self, **overrides):
        value = {"environment_id": "atlas", "storage": {"class": "dev-retain"}, **overrides}
        self.source.write_text(json.dumps(value), encoding="utf-8")
        return load_config(self.source)

    # Inputs: PostgreSQL-only request. Outputs: assertions. Purpose: exclude disabled dependencies. Dependencies: load_config.
    def test_postgres_only_omits_kafka_and_monitoring(self):
        config = self.config(kafka={"enabled": False}, monitoring={"enabled": False})
        self.assertEqual(config["derived"]["stages"], ["00-bootstrap", "30-postgres"])
        self.assertEqual(config["derived"]["requests_memory_mib"], 384)
        self.assertEqual(config["derived"]["total_storage_mib"], 4096)

    # Inputs: supported component combinations. Outputs: assertions. Purpose: keep selection consistent. Dependencies: normalizer.
    def test_component_matrix_has_consistent_stages(self):
        for postgres, kafka in ((True, False), (False, True), (True, True)):
            for monitoring in (False, True):
                with self.subTest(postgres=postgres, kafka=kafka, monitoring=monitoring):
                    config = self.config(
                        postgres={"enabled": postgres},
                        kafka={"enabled": kafka},
                        monitoring={"enabled": monitoring},
                    )
                    stages = config["derived"]["stages"]
                    self.assertEqual("30-postgres" in stages, postgres)
                    self.assertEqual("31-kafka" in stages, kafka)
                    self.assertEqual("40-monitoring" in stages, monitoring)
                    self.assertEqual(
                        sum(config["derived"]["memory_requests_by_stage"].values()),
                        config["derived"]["requests_memory_mib"],
                    )

    # Inputs: malformed user fields. Outputs: errors. Purpose: reject mistakes before external tools. Dependencies: normalizer.
    def test_invalid_inputs_fail_at_boundary(self):
        cases = [
            {"secret_password": "never-accepted"},
            {"environment_id": "../escape"},
            {"postgres": {"enabled": "false"}},
            {"schema_version": True},
            {"application": {"username": "postgres"}},
            {"application": {"database": "a';DROP DATABASE postgres;--"}},
            {"postgres": {"enabled": False}, "kafka": {"enabled": False}},
            {"kafka": {"topics": ["duplicate", "duplicate"]}},
            {"postgres": {"resources": {"limits": {"memory": "128Mi"}}}},
            {"budget": {"max_memory_requests_mib": 100}},
            {"network": {"api_server_cidrs": ["0.0.0.0/0"]}},
            {"monitoring": {"pod_labels": {"bad label": "value"}}},
            {"storage": {"mode": "create", "nodes": []}},
            {
                "application": {"namespace": "atlas-storage"},
                "storage": {"mode": "create", "nodes": ["minikube"]},
            },
            {"kafka": {"namespace": "atlas-data"}},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config(**value)

    # Inputs: numeric-leading environment. Outputs: assertions. Purpose: derive legal SQL identifiers. Dependencies: normalizer.
    def test_numeric_environment_derives_safe_database_identity(self):
        self.assertEqual(
            self.config(environment_id="123-dev")["application"]["username"], "app_123_dev"
        )

    # Inputs: normalized persisted config with altered derived values. Outputs: regenerated values.
    # Purpose: prevent trusted derived-state injection. Dependencies: normalizer.
    def test_derived_values_are_recomputed(self):
        config = self.config()
        config["derived"]["namespace"] = "kube-system"
        self.source.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(load_config(self.source)["derived"]["namespace"], "atlas-data")

    # Inputs: relative kubeconfig. Outputs: absolute path. Purpose: avoid cwd-dependent targeting. Dependencies: config source path.
    def test_relative_kubeconfig_is_relative_to_config_file(self):
        config = self.config(cluster={"kubeconfig": "dev.kubeconfig"})
        self.assertEqual(
            config["cluster"]["kubeconfig"], str((self.root / "dev.kubeconfig").resolve())
        )

    # Inputs: enabled optional storage. Outputs: derived stage/class. Purpose: avoid default-class guessing. Dependencies: normalizer.
    def test_created_storage_uses_explicit_nodes_and_owned_class(self):
        config = self.config(storage={"mode": "create", "nodes": ["minikube"]})
        self.assertIn("05-storage", config["derived"]["stages"])
        self.assertEqual(config["storage"]["class"], "atlas-local-retain")

    # Inputs: two environment workspaces. Outputs: isolated generated roots. Purpose: separate state paths. Dependencies: configure.
    def test_configure_materializes_independent_private_workspaces(self):
        for env in ("atlas", "other"):
            config = self.config(environment_id=env)
            workspace = self.root / env
            with workspace_lock(workspace):
                report = CLI.configure(config, workspace)
            self.assertEqual(report["status"], "configured")
            self.assertEqual(read_json(workspace / "environment.json")["environment_id"], env)
            for path in workspace.rglob("*"):
                self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))
        with self.assertRaisesRegex(ValueError, "already bound"):
            CLI.configure(self.config(environment_id="wrong"), self.root / "atlas")

    # Inputs: disabled stage with existing state. Outputs: refusal. Purpose: preserve managed resources. Dependencies: configure.
    def test_configure_refuses_orphaning_state(self):
        workspace = self.root / "workspace"
        with workspace_lock(workspace):
            CLI.configure(self.config(), workspace)
            write_json(workspace / "state" / "31-kafka.tfstate", {"resources": []})
            with self.assertRaisesRegex(ValueError, "migration"):
                CLI.configure(self.config(kafka={"enabled": False}), workspace)

    # Inputs: a stage file removed from a newer engine. Outputs: refusal without file deletion.
    # Purpose: prevent stale Terraform files silently surviving upgrades. Dependencies: materialization boundary.
    def test_configure_refuses_stale_engine_files(self):
        workspace = self.root / "workspace"
        with workspace_lock(workspace):
            config = self.config()
            CLI.configure(config, workspace)
            stale = workspace / "stages" / "00-bootstrap" / "removed-in-new-version.tf"
            stale.write_text("# Purpose: simulate an old managed engine file.\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "engine migration"):
                CLI.configure(config, workspace)
            self.assertTrue(stale.exists())

    # Inputs: nonempty unrelated directory. Outputs: refusal. Purpose: avoid adopting user files. Dependencies: configure.
    def test_configure_refuses_unrelated_directory(self):
        workspace = self.root / "workspace"
        with workspace_lock(workspace):
            (workspace / "notes.txt").write_text("existing work", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty workspace"):
                CLI.configure(self.config(), workspace)

    # Inputs: offline CLI request with no external tools in PATH. Outputs: successful JSON.
    # Purpose: prove configure requires no cluster or tool installation. Dependencies: Python subprocess.
    def test_offline_cli_does_not_require_external_tools(self):
        self.config(kafka={"enabled": False}, monitoring={"enabled": False})
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "platform.py"),
                "--config",
                str(self.source),
                "--workspace",
                str(self.root / "workspace"),
                "configure",
            ],
            text=True,
            capture_output=True,
            env={"PATH": "/nonexistent", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["runtime_acceptance"], "not-executed")

    # Inputs: two builds of the same source. Outputs: identical archives. Purpose: prove portable reproducibility.
    # Dependencies: package builder and tarfile, without external project paths.
    def test_package_is_reproducible_and_excludes_runtime_artifacts(self):
        root = SCRIPTS.parent
        first, second = self.root / "one.tar.gz", self.root / "two.tar.gz"
        self.assertEqual(build(root, first)["sha256"], build(root, second)["sha256"])
        with tarfile.open(first) as archive:
            names = archive.getnames()
            self.assertIn("local-k8s-platform/manifest.json", names)
            self.assertTrue(
                all(
                    "/.local/" not in name
                    and "/.terraform/" not in name
                    and "/__pycache__/" not in name
                    for name in names
                )
            )


if __name__ == "__main__":
    unittest.main()
