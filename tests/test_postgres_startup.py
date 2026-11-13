"""Purpose: Exercise PostgreSQL startup safety with isolated files and fake database binaries."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "assets/infra/stacks/30-postgres/config/start.sh"
PASSWORD = "a" * 64


class PostgresStartup(unittest.TestCase):
    # Inputs: Data state and optional credential failure. Outputs: Startup status and observable events.
    # Purpose: Run the real shell control flow without host-path writes. Dependencies: Bash and fake binaries.
    def run_startup(self, state, *, reject_credentials=False, wrong_username=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "/var/lib/postgresql/data": root / "data",
                "/var/run/postgresql": root / "runtime",
                "/secrets": root / "secrets",
                "/tls-source": root / "tls-source",
                "/run/tls": root / "tls-runtime",
                "/config": root / "config",
            }
            for path in paths.values():
                path.mkdir(parents=True)
            for role in ("admin", "app", "metrics"):
                directory = root / "secrets" / role
                directory.mkdir()
                (directory / "password").write_text(PASSWORD)
            (root / "secrets/app/username").write_text(
                "another_user" if wrong_username else "custom_user"
            )
            (root / "secrets/metrics/username").write_text("postgres_exporter")
            for filename in ("tls.crt", "tls.key", "ca.crt"):
                (root / "tls-source" / filename).write_text("nonsecret-test-fixture")
            data = root / "data/pgdata"
            data.mkdir()
            if state != "fresh":
                (data / "preserve-me").write_text("existing data fixture")
            if state in {"complete", "unsealed", "wrong-version"}:
                (data / "PG_VERSION").write_text("18" if state == "wrong-version" else "17")
            marker = data / ".local-k8s-platform-initialized"
            if state in {"complete", "wrong-version"}:
                marker.write_text("local-k8s-platform-v1")
            ready = root / "runtime/platform-startup-validated"
            ready.touch()
            binaries = root / "bin"
            binaries.mkdir()
            scripts = {
                "id": "#!/bin/bash\n# Purpose: Report the expected PostgreSQL image identity.\nprintf 999\n",
                "pg_ctl": "#!/bin/bash\n# Purpose: Record bootstrap server transitions.\nprintf 'pg_ctl\\n' >> \"$TEST_LOG\"\n",
                "initdb": '#!/bin/bash\n# Purpose: Simulate fresh data creation.\nprintf \'initdb\\n\' >> "$TEST_LOG"\nprintf 17 > "$PGDATA/PG_VERSION"\n',
                "psql": """#!/bin/bash
# Purpose: Record initialization and independently fail runtime authentication checks.
for arg in "$@"; do
  if [[ "$arg" == --file=*/bootstrap.sql ]]; then
    printf 'initialization-sql\\n' >> "$TEST_LOG"
    exit 0
  fi
done
printf 'readonly-auth-check\\n' >> "$TEST_LOG"
[[ -r "$PGPASSFILE" ]] || exit 1
[[ "$TEST_REJECT_CREDENTIALS" == false ]] || exit 1
printf 't\\n'
""",
                "postgres": "#!/bin/bash\n# Purpose: Record the final server handoff.\nprintf 'final-postgres\\n' >> \"$TEST_LOG\"\n",
            }
            for name, content in scripts.items():
                executable = binaries / name
                executable.write_text(content)
                executable.chmod(0o700)
            text = SOURCE.read_text()
            for original, replacement in paths.items():
                text = text.replace(original, str(replacement))
            script = root / "start.sh"
            script.write_text(text)
            environment = {
                **os.environ,
                "PATH": f"{binaries}:{os.environ['PATH']}",
                "TEST_LOG": str(root / "events"),
                "TEST_REJECT_CREDENTIALS": str(reject_credentials).lower(),
                "APP_USERNAME": "custom_user",
                "APP_DATABASE": "custom_database",
            }
            result = subprocess.run(
                ["/bin/bash", str(script)],
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
            )
            events = (
                (root / "events").read_text().splitlines() if (root / "events").exists() else []
            )
            self.assertNotIn(PASSWORD, result.stdout + result.stderr)
            self.assertFalse(
                any(
                    (root / "runtime" / f"{role}.pgpass").exists()
                    for role in ("admin", "app", "metrics")
                )
            )
            if state != "fresh":
                self.assertEqual((data / "preserve-me").read_text(), "existing data fixture")
            return result.returncode, events, ready.exists(), marker.exists()

    # Inputs: Fresh empty PGDATA. Outputs: Assertions on initialization and final readiness.
    # Purpose: Confirm one initialization and three real credential-check attempts. Dependencies: run_startup.
    def test_fresh_data_initializes_then_checks_all_identities(self):
        status, events, ready, sealed = self.run_startup("fresh")
        self.assertEqual(status, 0)
        self.assertEqual(events.count("initdb"), 1)
        self.assertEqual(events.count("initialization-sql"), 1)
        self.assertEqual(events.count("readonly-auth-check"), 3)
        self.assertEqual(events[-1], "final-postgres")
        self.assertTrue(ready and sealed)

    # Inputs: Previously initialized data. Outputs: Assertions on restart operations.
    # Purpose: Prove restarts never initialize or reset role passwords. Dependencies: run_startup.
    def test_restart_only_verifies_existing_credentials(self):
        status, events, ready, sealed = self.run_startup("complete")
        self.assertEqual(status, 0)
        self.assertNotIn("initdb", events)
        self.assertNotIn("initialization-sql", events)
        self.assertEqual(events.count("readonly-auth-check"), 3)
        self.assertTrue(ready and sealed)

    # Inputs: Partial, unsealed, and other-major data fixtures. Outputs: Rejection assertions.
    # Purpose: Preserve unknown data and remove stale readiness after failed restart. Dependencies: run_startup.
    def test_incomplete_or_other_major_data_is_never_reset(self):
        for state in ("partial", "unsealed", "wrong-version"):
            with self.subTest(state=state):
                status, events, ready, _ = self.run_startup(state)
                self.assertNotEqual(status, 0)
                self.assertNotIn("initdb", events)
                self.assertNotIn("initialization-sql", events)
                self.assertNotIn("final-postgres", events)
                self.assertFalse(ready)

    # Inputs: Initialized data and rejected mounted credentials. Outputs: Rejection assertions.
    # Purpose: Refuse credential drift without changing existing identities. Dependencies: run_startup.
    def test_rejected_credentials_preserve_data_and_initialization_marker(self):
        status, events, ready, sealed = self.run_startup("complete", reject_credentials=True)
        self.assertNotEqual(status, 0)
        self.assertNotIn("initialization-sql", events)
        self.assertNotIn("final-postgres", events)
        self.assertFalse(ready)
        self.assertTrue(sealed)

    # Inputs: Fresh data and a mismatched mounted username. Outputs: Rejection assertions.
    # Purpose: Stop before touching data when Secret identity differs from configuration. Dependencies: run_startup.
    def test_wrong_secret_identity_blocks_before_initialization(self):
        status, events, ready, sealed = self.run_startup("fresh", wrong_username=True)
        self.assertNotEqual(status, 0)
        self.assertNotIn("initdb", events)
        self.assertFalse(ready or sealed)


if __name__ == "__main__":
    unittest.main()
