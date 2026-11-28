# Purpose: Check PostgreSQL backup and restore orchestration with a fake in-Pod SQL engine, and the
# read-only credential expiry report with real temporary OpenSSL certificates.
import base64
import importlib.util
import io
import json
import re
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
import recovery
import runtime
from config import load_config
from io_utils import private_dir, read_json, write_json
from prerequisites import find_openssl

SPEC = importlib.util.spec_from_file_location("platform_recovery_cli", SCRIPTS / "platform.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)

IDENTITY = {
    "context": "minikube",
    "kubeconfig": "",
    "kubeconfig_sha256": "fixture",
    "server": "https://127.0.0.1:8443",
    "kube_system_uid": "cluster-one",
}
POD = {
    "pod": "atlas-postgres-0", "pod_uid": "pod-uid", "statefulset_uid": "sts-uid",
    "data_instance": {"pvc_uid": "pvc-uid", "pv_uid": "pv-uid"},
}
ENGINE = {"endpoint": "unix:///test/docker.sock", "id": "local-engine"}
CONTAINER_ID = "a" * 64


class FakeDatabase:
    """The PostgreSQL instance as seen through kubectl exec: databases, rows and the fixture table."""

    # Inputs: none. Outputs: instance. Purpose: hold source rows and restored copies. Dependencies: none.
    def __init__(self):
        self.databases = {"postgres": {}, "app": {"public.items": 3, "public.events": 0}}
        self.fixtures, self.restored_fixtures = {}, {}
        self.dump_mode, self.load_mode, self.sql = "ok", "ok", []
        self.docker_commands, self.isolated, self.fixture_schema = [], {}, None
        self.cleanup_fails = self.fixture_exists = self.fixture_replaced = False

    # Inputs: config, Pod, database and SQL. Outputs: result lines. Purpose: answer the statements
    # recovery issues. Dependencies: recovery SQL shapes.
    def psql(self, config, pod, database, sql):
        self.sql.append((database, sql))
        if "pg_database_size" in sql:
            return ["8192"]
        if "SHOW server_version" in sql:
            return ["17.11"]
        if "CREATE SCHEMA" in sql:
            if self.fixture_exists:
                raise RuntimeError("Schema already exists")
            assert "IF NOT EXISTS" not in sql
            self.fixture_schema = re.search(r"CREATE SCHEMA (\w+);", sql)[1]
            run_id, token = re.search(r"VALUES \('(\w+)', '(\w+)'\)", sql).groups()
            self.fixtures[run_id] = token
            return ["20000|20001"]
        if "DROP SCHEMA" in sql:
            assert "LOCK TABLE" in sql and "20000" in sql and "20001" in sql
            if self.fixture_replaced:
                raise RuntimeError("Fixture ownership changed")
            self.fixtures.clear()
            return []
        raise AssertionError(f"Unexpected live SQL: {sql}")

    # Inputs: Docker prefix, arguments and optional SQL. Outputs: command stdout.
    # Purpose: emulate a disposable instance distinct from the live source.
    def docker(self, argv, *args, sql=None):
        self.docker_commands.append(args)
        if args[0] == "create":
            assert "--network" in args and args[args.index("--network") + 1] == "none"
            assert "--privileged" not in args and "--volume" not in args
            return CONTAINER_ID
        if args[0] == "start":
            assert args[1] == CONTAINER_ID
            return CONTAINER_ID
        if args[0] == "rm":
            assert args[-1] == CONTAINER_ID
            if self.cleanup_fails:
                raise RuntimeError("Docker rm failed")
            self.isolated.clear()
            return CONTAINER_ID
        assert argv[-3:] == ["exec", "-i", CONTAINER_ID]
        database = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--dbname="))
        if sql.startswith("CREATE DATABASE"):
            self.isolated[re.search(r'"(\w+)"', sql)[1]] = {}
            return ""
        if "query_to_xml" in sql:
            table = re.search(r"FROM %s', '([^']+)'::regclass", sql).group(1)
            return str(self.isolated[database].get(table, 0))
        if "SELECT token FROM" in sql:
            run_id = re.search(r"run_id = '(\w+)'", sql).group(1)
            token = self.restored_fixtures.get(database, {}).get(run_id)
            return token or ""
        raise AssertionError(f"Unexpected SQL: {sql}")

    # Inputs: none. Outputs: plain dump text for the app database. Purpose: mimic pg_dump.
    # Dependencies: pg_dump plain-format layout.
    def dump(self):
        lines = ["--", "-- PostgreSQL database dump", "--", "\\restrict key", "SET statement_timeout = 0;"]
        for table, rows in sorted(self.databases["app"].items()):
            lines += [f"COPY {table} (id) FROM stdin;", *[str(i) for i in range(rows)], "\\."]
        if self.fixtures:
            lines.append(f"COPY {self.fixture_schema}.fixtures (run_id, token) FROM stdin;")
            lines += [f"{run_id}\t{token}" for run_id, token in self.fixtures.items()] + ["\\."]
        if self.dump_mode != "truncated":
            lines += ["--", "-- PostgreSQL database dump complete", "--", "\\unrestrict key"]
        return "\n".join(lines) + "\n"

    # Inputs: argv and file paths. Outputs: (code, stderr). Purpose: run pg_dump or the restore load.
    # Dependencies: recovery.analyze.
    def stream(self, argv, *, stdin_path=None, stdout_path=None, timeout=600):
        if "pg_dump" in argv:
            Path(stdout_path).write_text(self.dump()[: 40 if self.dump_mode == "fail" else None])
            return (1, "pg_dump: error: connection lost") if self.dump_mode == "fail" else (0, "")
        target = next(item.removeprefix("--dbname=") for item in argv if item.startswith("--dbname="))
        if self.load_mode == "fail":
            return 3, "ERROR: relation already exists"
        assert "kubectl" not in argv and CONTAINER_ID in argv
        found = recovery.analyze(stdin_path, self.fixture_schema)
        delta = -1 if self.load_mode == "short" else 0
        self.isolated[target] = {table: max(0, rows + delta) for table, rows in found["tables"].items()}
        self.restored_fixtures[target] = dict(found["fixture_rows"])
        return 0, ""


class RecoveryTests(unittest.TestCase):
    # Inputs: none. Outputs: private workspace, backup root and fake database. Dependencies: tempfile.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        kubeconfig = self.root / "kubeconfig"
        kubeconfig.write_text("# Purpose: fake test kubeconfig; no credentials.\n")
        self.source = self.root / "input.json"
        write_json(self.source, {
            "environment_id": "atlas",
            "cluster": {"kubeconfig": str(kubeconfig)},
            "kafka": {"enabled": False},
            "monitoring": {"enabled": False},
            "storage": {"class": "dev-retain"},
            "application": {"database": "app"},
        })
        self.config = load_config(self.source)
        self.workspace = private_dir(self.root / "workspace")
        private_dir(self.workspace / "reports")
        self.backups = self.root / "backups"
        self.db = FakeDatabase()

    # Inputs: callable and arguments. Outputs: its result. Purpose: run recovery with fakes.
    # Dependencies: FakeDatabase.
    def call(self, function, *args, **kwargs):
        with (
            patch.object(recovery, "preflight", return_value=({**IDENTITY}, dict(POD))),
            patch.object(recovery, "psql", side_effect=self.db.psql),
            patch.object(recovery, "stream", side_effect=self.db.stream),
            patch.object(recovery, "_restore_engine", return_value=ENGINE),
            patch.object(recovery, "_docker", side_effect=self.db.docker),
            patch.object(recovery, "_wait_isolated"),
        ):
            return function(self.config, self.workspace, *args, **kwargs)

    # Inputs: fixture flag. Outputs: (plan, report). Purpose: plan and run one backup.
    # Dependencies: recovery.backup.
    def take_backup(self, with_fixture=True):
        plan = self.call(recovery.backup, self.backups, with_fixture)
        return plan, self.call(recovery.backup, self.backups, with_fixture, plan["plan_id"], True)

    # Inputs: none. Outputs: assertions. Purpose: a backup writes a private, checksummed dump and
    # manifest with exact row counts and the fixture, then removes the fixture row and schema.
    # Dependencies: recovery.backup.
    def test_backup_records_verifiable_metadata(self):
        plan = self.call(recovery.backup, self.backups, True)
        self.assertEqual(plan["status"], "planned")
        self.assertTrue(any("fixture row" in item for item in plan["writes"]))
        self.assertFalse(self.backups.exists(), "planning must not write outside the workspace")
        self.assertEqual(self.db.fixtures, {}, "planning must not write the fixture")
        report = self.call(recovery.backup, self.backups, True, plan["plan_id"], True)
        self.assertEqual(report["status"], "complete", report)
        directory = Path(report["directory"])
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / "dump.sql").stat().st_mode & 0o777, 0o600)
        manifest, _ = recovery.verify_backup(directory)
        self.assertEqual(manifest["tables"]["public.items"], 3)
        self.assertEqual(manifest["source"]["kube_system_uid"], "cluster-one")
        self.assertIsNotNone(manifest["fixture"])
        self.assertEqual(self.db.fixtures, {})
        self.assertEqual(report["fixture"]["cleanup"], "removed")
        self.assertTrue(any("DROP SCHEMA" in sql for _, sql in self.db.sql))
        with self.assertRaisesRegex(RuntimeError, "already used"):
            self.call(recovery.backup, self.backups, True, plan["plan_id"], True)

    # Inputs: none. Outputs: assertions. Purpose: a failed or truncated pg_dump leaves no manifest,
    # still removes the fixture, and cannot be restored. Dependencies: recovery.
    def test_failed_and_incomplete_backups_are_not_restorable(self):
        for mode, message in (("fail", "pg_dump failed"), ("truncated", "completion trailer")):
            with self.subTest(mode=mode):
                self.db.dump_mode = mode
                _, report = self.take_backup()
                self.assertEqual(report["status"], "failed")
                self.assertIn(message, report["error"])
                self.assertEqual(report["fixture"]["cleanup"], "removed")
                directory = Path(report["directory"])
                self.assertFalse((directory / "manifest.json").exists())
                with self.assertRaisesRegex(ValueError, "no manifest"):
                    recovery.verify_backup(directory)

    # Inputs: none. Outputs: assertions. Purpose: edited manifests, changed bytes and a dump whose
    # tables differ from the manifest are rejected. Dependencies: recovery.verify_backup.
    def test_corrupted_backups_are_rejected(self):
        _, report = self.take_backup()
        directory = Path(report["directory"])
        dump, manifest_path = directory / "dump.sql", directory / "manifest.json"
        original, manifest = dump.read_bytes(), read_json(manifest_path)
        dump.write_bytes(original.replace(b"\n1\n", b"\n9\n", 1))
        with self.assertRaisesRegex(ValueError, "corrupted"):
            recovery.verify_backup(directory)
        dump.write_bytes(original)
        write_json(manifest_path, {**manifest, "bytes": manifest["bytes"] + 1})
        with self.assertRaisesRegex(ValueError, "modified"):
            recovery.verify_backup(directory)
        # A consistent but truncated dump with a re-signed manifest is still incomplete.
        truncated = original.split(b"-- PostgreSQL database dump complete")[0]
        dump.write_bytes(truncated)
        forged = {**manifest, "bytes": len(truncated), "sha256": recovery.sha256(dump)}
        forged["manifest_sha256"] = recovery.manifest_digest(forged)
        write_json(manifest_path, forged)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            recovery.verify_backup(directory)

    # Inputs: none. Outputs: assertions. Purpose: restore compares counts and the fixture, removes
    # its container, and invalidates evidence if the same StatefulSet loses its volume.
    def test_restore_verifies_and_drops_its_target(self):
        _, backup = self.take_backup()
        directory = Path(backup["directory"])
        plan = self.call(recovery.restore, directory)
        target = plan["target"]["database"]
        self.assertTrue(target.startswith("app_restore_"))
        self.assertEqual(plan["backup"]["source"]["database"], "app")
        self.assertNotIn(target, self.db.databases)
        report = self.call(recovery.restore, directory, None, False, plan["plan_id"], True)
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual({item["name"] for item in report["checks"]},
                         {"rows:public.items", "rows:public.events", f"rows:{self.db.fixture_schema}.fixtures", "fixture"})
        self.assertEqual(report["target_cleanup"], "removed")
        self.assertNotIn(target, self.db.databases)
        self.assertEqual(self.db.databases["app"]["public.items"], 3)
        objects = {
            "statefulsets.apps": {"metadata": {"uid": POD["statefulset_uid"]}},
            "persistentvolumeclaims": {
                "metadata": {"uid": "pvc-uid"}, "spec": {"volumeName": "pg-volume"},
                "status": {"phase": "Bound"},
            },
            "persistentvolumes": {
                "metadata": {"uid": "pv-uid"}, "spec": {"claimRef": {"uid": "pvc-uid"}},
            },
        }

        # Inputs: resource GET. Outputs: fake object. Purpose: exercise live volume identity lookup.
        def get_object(config, resource, *args):
            return objects[resource]

        with patch.object(runtime, "_get", side_effect=get_object):
            entry = recovery.evidence(self.config, self.workspace, IDENTITY)
            self.assertEqual((entry["status"], entry["detail"]["fixture"]), ("pass", "pass"))
            objects["persistentvolumes"]["metadata"]["uid"] = "replacement"
            self.assertEqual(recovery.evidence(self.config, self.workspace, IDENTITY)["status"], "pending")

    # Inputs: none. Outputs: assertions. Purpose: reject source and system names and other
    # environments; a namesake in the live server remains untouched. Dependencies: recovery.restore.
    def test_restore_rejects_source_and_target_conflicts(self):
        _, backup = self.take_backup()
        directory = Path(backup["directory"])
        for name in ("app", "postgres"):
            with self.subTest(target=name), self.assertRaisesRegex(ValueError, "cannot be the source"):
                self.call(recovery.restore, directory, name)
        self.db.databases["existing_copy"] = {"public.other": 1}
        plan = self.call(recovery.restore, directory, "existing_copy")
        report = self.call(recovery.restore, directory, "existing_copy", False, plan["plan_id"], True)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(self.db.databases["existing_copy"], {"public.other": 1})
        manifest = read_json(directory / "manifest.json")
        manifest["environment_id"] = "other"
        manifest["manifest_sha256"] = recovery.manifest_digest(manifest)
        write_json(directory / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "another environment"):
            self.call(recovery.restore, directory)

    # Inputs: none. Outputs: assertions. Purpose: a failed load or row-count mismatch still
    # removes the new container by ID. Dependencies: recovery.restore.
    def test_restore_failures_are_reported_and_cleaned(self):
        _, backup = self.take_backup()
        directory = Path(backup["directory"])
        for mode, status in (("fail", "failed"), ("short", "fail")):
            with self.subTest(mode=mode):
                self.db.load_mode = mode
                plan = self.call(recovery.restore, directory)
                report = self.call(recovery.restore, directory, None, False, plan["plan_id"], True)
                self.assertEqual(report["status"], status)
                self.assertEqual(report["target_cleanup"], "removed")
        report_checks = [item for item in report["checks"] if item["status"] == "fail"]
        self.assertTrue(any(item["name"] == "rows:public.items" for item in report_checks))

    # Inputs: none. Outputs: assertions. Purpose: a backup changed after review blocks execution.
    # Dependencies: recovery.restore.
    def test_restore_blocks_backup_changed_after_review(self):
        _, backup = self.take_backup()
        directory = Path(backup["directory"])
        plan = self.call(recovery.restore, directory)
        dump = directory / "dump.sql"
        dump.write_bytes(dump.read_bytes() + b"-- trailing edit\n")
        with self.assertRaisesRegex(ValueError, "corrupted"):
            self.call(recovery.restore, directory, None, False, plan["plan_id"], True)
        self.assertEqual(self.db.docker_commands, [])

    # Inputs: none. Outputs: assertions. Purpose: replay uses the verified snapshot even when
    # the original file is replaced after checks. Dependencies: fake Docker and io_utils.
    def test_restore_replays_private_snapshot_after_source_replacement(self):
        _, backup = self.take_backup()
        directory = Path(backup["directory"])
        plan = self.call(recovery.restore, directory)
        docker = self.db.docker

        # Inputs: Docker arguments. Outputs: simulated response. Purpose: replace the original
        # dump after snapshot verification, before the target database exists.
        def replace_dump(argv, *args, sql=None):
            if args[0] == "create":
                dump = directory / "dump.sql"
                dump.unlink()
                dump.symlink_to(directory / "manifest.json")
            return docker(argv, *args, sql=sql)

        with patch.object(self.db, "docker", side_effect=replace_dump):
            report = self.call(recovery.restore, directory, None, False, plan["plan_id"], True)
        self.assertEqual(report["status"], "pass")

    # Inputs: none. Outputs: assertions. Purpose: a schema collision or replacement is left
    # intact. Dependencies: transactional fixture DDL.
    def test_fixture_cleanup_requires_creation_and_matching_identity(self):
        self.db.fixture_exists = True
        _, collision = self.take_backup()
        self.assertEqual(collision["status"], "failed")
        self.assertFalse(any("DROP SCHEMA" in sql for _, sql in self.db.sql))
        self.db.fixture_exists, self.db.fixture_replaced = False, True
        _, replaced = self.take_backup()
        self.assertEqual(replaced["status"], "failed")
        self.assertEqual(replaced["fixture"]["cleanup"], "failed")
        self.assertTrue(self.db.fixtures)

    # Inputs: none. Outputs: assertions. Purpose: a fixture-free restore or failed cleanup
    # never supplies passing recovery evidence. Dependencies: evidence.
    def test_restore_requires_fixture_and_cleanup_for_evidence(self):
        with (
            patch.object(runtime, "_get", return_value={"metadata": {"uid": POD["statefulset_uid"]}}),
            patch.object(recovery, "data_instance", return_value=POD["data_instance"]),
        ):
            for fixture, cleanup_fails in ((False, False), (True, True)):
                with self.subTest(fixture=fixture, cleanup_fails=cleanup_fails):
                    _, backup = self.take_backup(fixture)
                    directory = Path(backup["directory"])
                    self.db.cleanup_fails = cleanup_fails
                    plan = self.call(recovery.restore, directory)
                    report = self.call(recovery.restore, directory, None, False, plan["plan_id"], True)
                    result = recovery.evidence(self.config, self.workspace, IDENTITY)
                    self.assertEqual(result["status"], "pending")
                    self.assertEqual(result["detail"]["fixture"], "pass" if fixture else "pending")
                    self.assertEqual(report["status"], "failed" if cleanup_fails else "pass")
                    self.assertEqual(report["target_cleanup"], "remove-failed" if cleanup_fails else "removed")

    # Inputs: none. Outputs: assertions. Purpose: backup hashing and restore verification use
    # bounded reads. Dependencies: io_utils.sha256 and recovery.
    def test_dump_hashing_does_not_use_read_bytes(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded file read")):
            _, report = self.take_backup()
            manifest, _ = recovery.verify_backup(Path(report["directory"]))
        self.assertEqual(manifest["sha256"], report["sha256"])

    # Inputs: none. Outputs: assertions. Purpose: backups stay outside the Minikube home, the
    # workspace and the skill; CLI options are checked at the entry point. Dependencies: CLI.
    def test_backup_location_and_cli_boundaries(self):
        home = self.root / "mk"
        with patch.dict("os.environ", {"MINIKUBE_HOME": str(home)}):
            for path in (home / ".minikube" / "backups", self.workspace / "backups", CLI.ROOT / "backups"):
                with self.subTest(path=str(path)), self.assertRaisesRegex(ValueError, "outside"):
                    recovery.backup_root(path, self.workspace, CLI.ROOT)
            self.assertEqual(recovery.backup_root(self.backups, self.workspace, CLI.ROOT), self.backups)
        for argv in (
            ["restore", "--backup", str(self.backups), "--target-database", "Bad-Name"],
            ["credentials", "--expiry", "--component", "kafka"],
        ):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                CLI.arguments(["--config", str(self.source), "--workspace", str(self.workspace), *argv])

    # Inputs: none. Outputs: assertions. Purpose: stream creates a new 0600 output exclusively and
    # passes a file to stdin. Dependencies: io_utils.stream, /bin/cat.
    def test_stream_writes_private_new_files_only(self):
        from io_utils import stream

        source, target = self.root / "in.sql", self.root / "out.sql"
        source.write_text("SELECT 1;\n")
        self.assertEqual(stream(["/bin/cat"], stdin_path=source, stdout_path=target), (0, ""))
        self.assertEqual(target.read_text(), "SELECT 1;\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            stream(["/bin/cat"], stdin_path=source, stdout_path=target)


@unittest.skipIf(find_openssl() is None, "OpenSSL is required for certificate expiry tests")
class ExpiryTests(unittest.TestCase):
    # Inputs: none. Outputs: certificates and a Kafka-and-PostgreSQL configuration. Dependencies: OpenSSL.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        kubeconfig = self.root / "kubeconfig"
        kubeconfig.write_text("# Purpose: fake test kubeconfig; no credentials.\n")
        source = self.root / "input.json"
        write_json(source, {
            "environment_id": "atlas",
            "cluster": {"kubeconfig": str(kubeconfig)},
            "monitoring": {"enabled": False},
            "storage": {"class": "dev-retain"},
        })
        self.config = load_config(source)
        self.workspace = private_dir(self.root / "workspace")
        private_dir(self.workspace / "reports")
        self.openssl = find_openssl()["path"]
        self.long = self.certificate("long", 400)
        self.short = self.certificate("short", 5)
        self.other = self.certificate("other", 400)

    # Inputs: name and validity days. Outputs: PEM text. Purpose: build a throwaway certificate.
    # Dependencies: OpenSSL.
    def certificate(self, name, days):
        subprocess.run(
            [self.openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.crt", "-days", str(days),
             "-subj", f"/CN={name}"],
            cwd=self.root, check=True, capture_output=True,
        )
        return (self.root / f"{name}.crt").read_text()

    # Inputs: Secret fields keyed by (namespace, name, key) and Pods. Outputs: report.
    # Purpose: run expiry against fake jsonpath reads. Dependencies: credentials.expiry.
    def run_expiry(self, fields, pods):
        commands = []

        # Inputs: kubectl argv. Outputs: completed process. Purpose: serve one field or the Pod list.
        # Dependencies: fields and pods above.
        def fake(command, timeout=None, input_text=None):
            args = command[6:]
            commands.append(args)
            if args[:2] == ["get", "pods"]:
                return subprocess.CompletedProcess(args, 0, json.dumps({"items": pods}), "")
            key = re.search(r"jsonpath=\{\.data\.(.+)\}", args[-1]).group(1).replace("\\.", ".")
            namespace = args[args.index("--namespace") + 1]
            value = fields.get((namespace, args[2], key))
            if value == "denied":
                return subprocess.CompletedProcess(args, 1, "", "Error from server (Forbidden)")
            text = base64.b64encode(value.encode()).decode() if value else ""
            return subprocess.CompletedProcess(args, 0, text, "")

        with (
            patch.object(runtime, "run", side_effect=fake),
            patch.object(runtime, "_identity", return_value=IDENTITY),
            patch.object(runtime, "_bind", return_value=True),
        ):
            report = credentials.expiry(self.config, self.workspace)
        return report, commands

    # Inputs: none. Outputs: Secret fields where every copy matches its source.
    # Dependencies: credential naming.
    def consistent(self):
        fields = {}
        for group, namespace, name, key in credentials._certificate_sources(self.config):
            fields[(namespace, name, key)] = self.short if group == "kafka-producer" else self.long
        return fields

    # Inputs: none. Outputs: assertions. Purpose: report remaining time per certificate, the affected
    # client Pods, and never request private key or password fields. Dependencies: credentials.expiry.
    def test_expiry_reports_lifetimes_and_client_pods(self):
        directory = private_dir(private_dir(self.workspace / "credentials") / "postgres")
        (directory / "ca.crt").write_text(self.long)
        (directory / "server.crt").write_text(self.long)
        pods = [{
            "metadata": {"name": "worker-0"},
            "spec": {"volumes": [{"secret": {"secretName": "atlas-kafka-producer-client"}}],
                     "containers": [{"env": [{"valueFrom": {"secretKeyRef": {"name": "atlas-postgres-client"}}}]}]},
        }]
        report, commands = self.run_expiry(self.consistent(), pods)
        self.assertEqual(report["status"], "warning", report["mismatched_copies"])
        by_reference = {item["reference"]: item for item in report["certificates"]}
        producer = by_reference["atlas-app/atlas-kafka-producer-client:user.crt"]
        self.assertEqual(producer["status"], "critical")
        self.assertLess(producer["remaining_days"], 6)
        self.assertEqual(producer["client_pods"], ["worker-0"])
        self.assertEqual(by_reference["atlas-app/atlas-postgres-client:ca.crt"]["client_pods"], ["worker-0"])
        self.assertEqual(by_reference["workspace:credentials/postgres/ca.crt"]["status"], "ok")
        requested = " ".join(command[-1] for command in commands if command[:2] == ["get", "secret"])
        for forbidden in ("tls\\.key", "user\\.key", "password"):
            self.assertNotIn(forbidden, requested)
        self.assertFalse(report["cluster_writes"])

    # Inputs: none. Outputs: assertions. Purpose: a client copy that differs from its source fails,
    # and unreadable fields are listed rather than hidden. Dependencies: credentials.expiry.
    def test_expiry_detects_stale_copies_and_unreadable_fields(self):
        fields = self.consistent()
        fields[("atlas-app", "atlas-kafka-consumer-client", "ca.crt")] = self.other
        fields[("kafka", "atlas-kafka-clients-ca-cert", "ca.crt")] = "denied"
        fields.pop(("atlas-data", "atlas-postgres-tls", "tls.crt"))
        report, _ = self.run_expiry(fields, [])
        self.assertEqual(report["status"], "fail")
        self.assertEqual([item["group"] for item in report["mismatched_copies"]], ["kafka-cluster-ca"])
        by_reference = {item["reference"]: item for item in report["certificates"]}
        self.assertEqual(by_reference["kafka/atlas-kafka-clients-ca-cert:ca.crt"]["state"], "access-denied")
        self.assertEqual(by_reference["atlas-data/atlas-postgres-tls:tls.crt"]["state"], "missing")
        self.assertIn("kafka/atlas-kafka-clients-ca-cert:ca.crt", report["unavailable"])

    # Inputs: none. Outputs: assertions. Purpose: compare the whole rollover bundle independent
    # of order and include the earliest expiry. Dependencies: real OpenSSL certificates.
    def test_expiry_compares_all_certificates_in_rollover_bundle(self):
        fields = self.consistent()
        source = ("atlas-data", "atlas-postgres-tls", "ca.crt")
        client = ("atlas-app", "atlas-postgres-client", "ca.crt")
        fields[source] = self.long + self.short
        report, _ = self.run_expiry(fields, [])
        self.assertEqual([item["group"] for item in report["mismatched_copies"]], ["postgres-ca"])
        fields[client] = self.short + self.long
        report, _ = self.run_expiry(fields, [])
        self.assertEqual(report["mismatched_copies"], [])
        entry = next(item for item in report["certificates"] if item["reference"] == "atlas-app/atlas-postgres-client:ca.crt")
        self.assertEqual((entry["status"], len(entry["fingerprints_sha256"])), ("critical", 2))


if __name__ == "__main__":
    unittest.main()
