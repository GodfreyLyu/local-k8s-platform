"""Purpose: verify local TLS preparation and explicit credential handoff without live API changes."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import credentials as module
from config import load_config
from io_utils import private_dir, read_json


class CredentialTests(unittest.TestCase):
    # Inputs: none. Outputs: private fixture and config. Purpose: isolate sensitive test material. Dependencies: tempfile.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        private_dir(self.workspace / "credentials")
        private_dir(self.workspace / "reports")
        source = self.workspace / "input.json"
        source.write_text(
            json.dumps(
                {"environment_id": "atlas-development-long", "storage": {"class": "dev-retain"}}
            ),
            encoding="utf-8",
        )
        self.config = load_config(source)

    # Inputs: default credential request. Outputs: reference-only preview. Purpose: prove offline default. Dependencies: helper.
    def test_preview_does_not_generate_or_access_credentials(self):
        with patch.object(module, "run", side_effect=AssertionError("external command")):
            report = module.credentials(self.config, self.workspace)
        self.assertEqual(report["status"], "preview")
        self.assertEqual(len(report["secret_references"]), 5)
        self.assertFalse((self.workspace / "credentials" / "postgres").exists())

    # Inputs: local prepare repeated twice. Outputs: identical verified bundle. Purpose: prevent implicit rotation.
    # Dependencies: actual local OpenSSL; no kubectl/Terraform calls.
    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required for certificate integration")
    def test_prepare_reuses_passwords_and_validates_real_certificates(self):
        directory = module.prepare_postgres(self.config, self.workspace)
        original = (directory / "bundle.json").read_bytes()
        self.assertEqual(module.prepare_postgres(self.config, self.workspace), directory)
        self.assertEqual((directory / "bundle.json").read_bytes(), original)
        documents = module.postgres_documents(self.config, self.workspace)
        self.assertEqual(len(documents), 5)
        self.assertEqual(set(documents[-1]["data"]), {"username", "password", "ca.crt"})
        self.assertNotIn("tls.key", documents[-1]["data"])
        for path in directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        bundle = read_json(directory / "bundle.json")
        for password in (
            bundle["admin_password"],
            bundle["app_password"],
            bundle["metrics_password"],
        ):
            self.assertNotIn(password, json.dumps(module.credentials(self.config, self.workspace)))

    # Inputs: incomplete external material. Outputs: refusal. Purpose: avoid resetting partial credentials. Dependencies: filesystem.
    def test_partial_material_is_not_overwritten(self):
        directory = private_dir(self.workspace / "credentials" / "postgres")
        (directory / "ca.key").write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Partial"):
            module.prepare_postgres(self.config, self.workspace)
        self.assertEqual((directory / "ca.key").read_text(), "existing")

    # Inputs: unowned existing Secret. Outputs: refusal without write. Purpose: prevent credential adoption. Dependencies: mocked API.
    def test_publish_refuses_unknown_owner(self):
        document = module.secret_document(
            self.config, "atlas-app", "client", {"password": "not-a-real-password"}
        )
        with patch.object(
            module, "kube", return_value={"metadata": {"labels": {}}, "data": document["data"]}
        ) as kube:
            with self.assertRaisesRegex(ValueError, "different owner"):
                module.publish(self.config, document)
            self.assertEqual(kube.call_count, 1)

    # Inputs: owned but changed Secret data. Outputs: refusal without write. Purpose: require explicit rotation. Dependencies: mocked API.
    def test_publish_refuses_silent_password_reset(self):
        document = module.secret_document(
            self.config, "atlas-app", "client", {"password": "new-value"}
        )
        existing = {
            "metadata": {"labels": self.config["derived"]["labels"]},
            "data": {"password": "old-value"},
        }
        with patch.object(module, "kube", return_value=existing) as kube:
            with self.assertRaisesRegex(ValueError, "rotation"):
                module.publish(self.config, document)
            self.assertEqual(kube.call_count, 1)

    # Inputs: absent destination. Outputs: atomic create call. Purpose: avoid applying over races. Dependencies: mocked API.
    def test_publish_creates_through_stdin_only(self):
        document = module.secret_document(
            self.config, "atlas-app", "client", {"password": "test-value"}
        )
        with patch.object(module, "kube", side_effect=[{}, {}]) as kube:
            self.assertEqual(module.publish(self.config, document), "created")
            self.assertEqual(kube.call_args.args[1], ["create", "-f", "-", "-o", "json"])
            self.assertEqual(kube.call_args.args[2], document)

    # Inputs: unchanged owned destination. Outputs: no write. Purpose: make handoff idempotent. Dependencies: mocked API.
    def test_publish_accepts_matching_owned_values(self):
        document = module.secret_document(
            self.config, "atlas-app", "client", {"password": "test-value"}
        )
        with patch.object(module, "kube", return_value=document) as kube:
            self.assertEqual(module.publish(self.config, document), "unchanged")
            self.assertEqual(kube.call_count, 1)


if __name__ == "__main__":
    unittest.main()
