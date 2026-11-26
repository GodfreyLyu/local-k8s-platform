"""Purpose: back up PostgreSQL and verify restores in a disposable, network-isolated instance."""

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from pathlib import Path

import runtime
from io_utils import private_dir, read_json, run, sha256, stream, write_json

FIXTURE_SCHEMA = "local_k8s_platform_restore_check"
TRAILER = "-- PostgreSQL database dump complete"
COPY_LINE = re.compile(r"^COPY (.+?)(?: \(.*\))? FROM stdin;$")
MANIFEST_SCHEMA = 1
DUMP_TIMEOUT = 1800
RESERVED_DATABASES = {"postgres", "template0", "template1"}


# Inputs: resolved configuration. Outputs: kubectl exec prefix for the PostgreSQL container.
# Purpose: run tools inside the Pod as the postgres OS user, so peer authentication over the Unix
# socket needs no password. Dependencies: 30-postgres Pod and container names.
def _exec_prefix(config, pod):
    return runtime._kube(config) + [
        "exec", "-i", pod, "-c", "postgres", "--namespace", config["derived"]["namespace"], "--",
    ]


# Inputs: database, extra flags and Unix socket directory. Outputs: psql argv.
# Purpose: share the administrator connection form for live source and isolated server.
def _psql_argv(database, *extra, socket="/var/run/postgresql"):
    return [
        "psql", "--no-psqlrc", "--quiet", "--tuples-only", "--no-align",
        f"--host={socket}", "--username=postgres", f"--dbname={database}",
        "--set=ON_ERROR_STOP=1", *extra,
    ]


# Inputs: configuration, Pod, database and SQL text. Outputs: stdout lines.
# Purpose: run small administrative SQL from stdin, never from arguments. Dependencies: kubectl exec.
def psql(config, pod, database, sql):
    result = run(_exec_prefix(config, pod) + _psql_argv(database, "--file=-"), input_text=sql, timeout=120)
    if result.returncode:
        raise RuntimeError(f"PostgreSQL command failed in database {database}.")
    return [line for line in result.stdout.splitlines() if line]


# Inputs: text. Outputs: SQL string literal. Purpose: quote values embedded in SQL. Dependencies: none.
def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


# Inputs: configuration. Outputs: bound claim and volume UIDs. Dependencies: Kubernetes GET.
# Purpose: distinguish data replacements from ordinary Pod rollouts.
def data_instance(config):
    derived = config["derived"]
    claim_name = f"{derived['postgres_name']}-data"
    claim = runtime._get(config, "persistentvolumeclaims", claim_name, derived["namespace"]) or {}
    uid = claim.get("metadata", {}).get("uid")
    volume_name = claim.get("spec", {}).get("volumeName")
    if not uid or not volume_name or claim.get("status", {}).get("phase") != "Bound":
        raise RuntimeError("The PostgreSQL data claim is not bound.")
    volume = runtime._get(config, "persistentvolumes", volume_name) or {}
    volume_uid = volume.get("metadata", {}).get("uid")
    if not volume_uid or volume.get("spec", {}).get("claimRef", {}).get("uid") != uid:
        raise RuntimeError("The PostgreSQL data volume does not match the bound claim.")
    return {"pvc_uid": uid, "pv_uid": volume_uid}


# Inputs: resolved configuration. Outputs: Pod facts for the owned, Ready PostgreSQL Pod.
# Purpose: refuse to exec into a Pod this environment does not own. Dependencies: kubectl GET.
def source_pod(config):
    derived = config["derived"]
    statefulset = runtime._get(config, "statefulsets.apps", derived["postgres_name"], derived["namespace"])
    pod = runtime._get(config, "pods", f"{derived['postgres_name']}-0", derived["namespace"])
    if not statefulset or not pod:
        raise RuntimeError("The PostgreSQL StatefulSet or its Pod is missing.")
    owners = {item.get("uid") for item in pod["metadata"].get("ownerReferences", [])}
    claims = {
        (volume.get("persistentVolumeClaim") or {}).get("claimName")
        for volume in pod.get("spec", {}).get("volumes", [])
    }
    if (
        pod["metadata"].get("labels", {}).get(runtime.ENVIRONMENT_LABEL) != config["environment_id"]
        or statefulset["metadata"]["uid"] not in owners
        or not runtime._condition(pod)
        or f"{derived['postgres_name']}-data" not in claims
    ):
        raise RuntimeError("The PostgreSQL Pod is not Ready, owned, or using the expected data claim.")
    return {
        "pod": pod["metadata"]["name"],
        "pod_uid": pod["metadata"]["uid"],
        "statefulset_uid": statefulset["metadata"]["uid"],
        "data_instance": data_instance(config),
    }


# Inputs: resolved configuration and workspace. Outputs: (identity, Pod facts).
# Purpose: share binding and stage checks before any exec. Dependencies: runtime.
def preflight(config, workspace):
    identity = runtime._identity(config)
    if not runtime._bind(config, workspace, identity):
        raise RuntimeError("The target must match the workspace binding.")
    record = workspace / "reports" / "applied-30-postgres.json"
    if not record.exists() or read_json(record).get("status") != "applied":
        raise RuntimeError("Stage 30-postgres must be applied first.")
    ready, _ = runtime._stage_ready(config, "30-postgres")
    if not ready:
        raise RuntimeError("Stage 30-postgres is not ready; run verify first.")
    return identity, source_pod(config)


# Inputs: dump path. Outputs: completeness, per-table row counts, and fixture rows.
# Purpose: verify a plain-format dump offline: the completion trailer proves pg_dump finished, and
# COPY sections give exact row counts for restore comparison. Dependencies: pg_dump plain format.
def analyze(path, fixture_schema=FIXTURE_SCHEMA):
    tables, fixtures, current, tail = {}, [], None, []
    with open(path, "rb") as handle:
        for raw in handle:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if current is not None:
                if line == "\\.":
                    current = None
                else:
                    tables[current] += 1
                    if current == f"{fixture_schema}.fixtures":
                        fixtures.append(line.split("\t"))
                continue
            match = COPY_LINE.match(line)
            if match:
                current = match.group(1)
                tables.setdefault(current, 0)
            elif line.strip():
                tail = (tail + [line])[-5:]
    return {
        "complete": current is None and TRAILER in tail,
        "tables": tables,
        "fixture_rows": [row[:2] for row in fixtures],
    }


# Inputs: manifest without its digest. Outputs: SHA-256. Purpose: detect manifest edits.
# Dependencies: runtime._digest.
def manifest_digest(manifest):
    return runtime._digest({key: value for key, value in manifest.items() if key != "manifest_sha256"})


# Inputs: backup directory and optional private snapshot path. Outputs: verified manifest and dump path.
# Purpose: reject failed, incomplete, edited or corrupted backups before any restore write.
# Dependencies: manifest.json, dump.sql.
def verify_backup(directory, snapshot=None):
    directory = Path(directory)
    manifest_path, dump = directory / "manifest.json", directory / "dump.sql"
    for path in (directory, manifest_path, dump):
        if path.is_symlink():
            raise ValueError("Backup paths must not be symbolic links.")
    if not manifest_path.is_file():
        raise ValueError("The backup has no manifest; it is incomplete or failed.")
    manifest = read_json(manifest_path)
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("manifest_sha256") != manifest_digest(manifest):
        raise ValueError("The backup manifest is unreadable or was modified.")
    if manifest.get("status") != "complete":
        raise ValueError("The backup did not complete; it cannot be restored.")
    if not dump.is_file():
        raise ValueError("The backup dump file is missing.")
    if snapshot is not None:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(dump.name, flags, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("The backup dump must be a regular file.")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                with os.fdopen(os.open(snapshot, flags, 0o600), "wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        finally:
            os.close(directory_fd)
        dump = snapshot
    if dump.stat().st_size != manifest["bytes"] or sha256(dump) != manifest["sha256"]:
        raise ValueError("The backup dump does not match its recorded checksum; it is corrupted.")
    fixture = manifest.get("fixture") or {}
    schema = fixture.get("schema", FIXTURE_SCHEMA)
    if not isinstance(schema, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise ValueError("The backup has an invalid fixture schema.")
    found = analyze(dump, schema)
    if not found["complete"] or found["tables"] != manifest["tables"]:
        raise ValueError("The backup dump is incomplete or does not match its manifest.")
    return manifest, dump


# Inputs: workspace and plan group. Outputs: private plan directory. Dependencies: private_dir.
def _plans(workspace, group):
    return private_dir(private_dir(workspace / "plans") / group)


# Inputs: plan record. Outputs: plan ID over everything but the ID. Dependencies: runtime._digest.
def _plan_id(review):
    return runtime._digest({key: value for key, value in review.items() if key != "plan_id"})


# Inputs: workspace, group, plan ID, config and the options given now. Outputs: verified plan.
# Purpose: bind execution to the reviewed plan, configuration, sources and options.
# Dependencies: private plan files.
def _load_plan(workspace, group, plan_id, config, options):
    directory = _plans(workspace, group)
    path, consumed = directory / f"{plan_id}.json", directory / f"consumed-{plan_id}.json"
    if consumed.exists() or not path.is_file() or path.is_symlink():
        raise RuntimeError(f"The {group} plan is missing, unsafe or already used; prepare a new one.")
    review = read_json(path)
    if (
        review.get("plan_id") != plan_id
        or _plan_id(review) != plan_id
        or review.get("config_sha256") != runtime._digest(config)
        or review.get("source_sha256") != sha256(Path(__file__))
        or review.get("options") != options
    ):
        raise RuntimeError(f"The {group} plan no longer matches its inputs or options; prepare a new one.")
    return review, consumed


# Inputs: workspace, group and review. Outputs: saved review with plan ID. Dependencies: private JSON.
def _save_plan(workspace, group, review):
    review["plan_id"] = _plan_id(review)
    write_json(_plans(workspace, group) / f"{review['plan_id']}.json", review)
    return review


# Inputs: identity. Outputs: the durable identity fields compared at execution. Dependencies: none.
def _durable(identity):
    return {key: identity[key] for key in ("context", "kubeconfig", "server", "kube_system_uid")}


# Inputs: configuration, workspace, backup root, fixture flag, plan ID and execution flag.
# Outputs: plan, preview or backup report. Purpose: dump the application database to a new private
# directory with verifiable metadata. Dependencies: kubectl exec, pg_dump in the Pod.
def backup(config, workspace, backup_root, with_fixture=False, plan_id=None, execute=False):
    database = config["application"]["database"]
    options = {"backup_dir": str(backup_root), "with_fixture": with_fixture}
    if plan_id is None:
        identity, pod = preflight(config, workspace)
        size = psql(config, pod["pod"], "postgres", f"SELECT pg_database_size({literal(database)});")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = secrets.token_hex(8)
        fixture_schema = f"{FIXTURE_SCHEMA}_{run_id}"
        writes = [f"New private directory {backup_root}/{config['environment_id']}-{stamp}-{run_id}"]
        if with_fixture:
            writes.append(
                f"Create {fixture_schema}.fixtures in database {database}, insert one fixture row, "
                "then remove the owned table and schema after the dump"
            )
        review = _save_plan(workspace, "backup", {
            "action": "backup",
            "environment_id": config["environment_id"],
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "config_sha256": runtime._digest(config),
            "source_sha256": sha256(Path(__file__)),
            "options": options,
            "identity": _durable(identity),
            "source": {"database": database, **pod},
            "database_bytes": int(size[0]) if size else None,
            "output": f"{config['environment_id']}-{stamp}-{run_id}",
            "run_id": run_id,
            "fixture_schema": fixture_schema if with_fixture else None,
            "reads": [f"pg_dump of database {database} through kubectl exec into {pod['pod']}"],
            "writes": writes,
            "untouched": ["Application rows, roles and settings", "Minikube profile files"],
        })
        return {"status": "planned", "requires_execute": True, **review}
    review, consumed = _load_plan(workspace, "backup", plan_id, config, options)
    if not execute:
        return {"status": "preview", "requires_execute": True, **review}
    identity, pod = preflight(config, workspace)
    if _durable(identity) != review["identity"] or any(
        pod[key] != review["source"][key] for key in ("statefulset_uid", "data_instance")
    ):
        raise RuntimeError("Target identity or PostgreSQL instance changed after review.")
    output = Path(backup_root) / review["output"]
    if output.exists():
        raise RuntimeError("The reviewed backup directory already exists; prepare a new plan.")
    write_json(consumed, {"plan_id": plan_id, "status": "attempting"})
    private_dir(backup_root)
    private_dir(output)
    report = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "plan_id": plan_id,
        "directory": str(output),
        "status": "running",
    }
    report_path = workspace / "reports" / "backup.json"
    write_json(report_path, report)
    fixture = None
    try:
        if with_fixture:
            schema = review["fixture_schema"]
            report["fixture"] = {"run_id": review["run_id"], "schema": schema, "cleanup": "ownership-unconfirmed"}
            write_json(report_path, report)
            fixture = _write_fixture(config, pod["pod"], review["run_id"], schema)
            report["fixture"].update({key: fixture[key] for key in ("schema_oid", "table_oid")})
            write_json(report_path, report)
        partial = output / "dump.sql.partial"
        code, _ = stream(
            _exec_prefix(config, pod["pod"]) + [
                "pg_dump", "--host=/var/run/postgresql", "--username=postgres",
                f"--dbname={database}", "--format=plain", "--no-owner", "--no-privileges",
                "--encoding=UTF8",
            ],
            stdout_path=partial,
            timeout=DUMP_TIMEOUT,
        )
        if code:
            raise RuntimeError("pg_dump failed; the partial file is kept for inspection and has no manifest.")
        found = analyze(partial, review["fixture_schema"])
        if not found["complete"]:
            raise RuntimeError("The dump lacks pg_dump's completion trailer; it is incomplete.")
        if fixture and [fixture["run_id"], fixture["token"]] not in found["fixture_rows"]:
            raise RuntimeError("The fixture row is missing from the dump.")
        dump = output / "dump.sql"
        os.replace(partial, dump)
        dump.chmod(0o600)
        version = psql(config, pod["pod"], "postgres", "SHOW server_version;")
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "status": "complete",
            "created_at": report["captured_at"],
            "environment_id": config["environment_id"],
            "plan_id": plan_id,
            "source": {
                "kube_system_uid": identity["kube_system_uid"],
                "server": identity["server"],
                "namespace": config["derived"]["namespace"],
                "database": database,
                **pod,
            },
            "server_version": version[0] if version else None,
            "format": "pg_dump plain, --no-owner --no-privileges, UTF8",
            "file": "dump.sql",
            "bytes": dump.stat().st_size,
            "sha256": sha256(dump),
            "tables": found["tables"],
            "fixture": {key: fixture[key] for key in ("schema", "run_id", "token")} if fixture else None,
        }
        manifest["manifest_sha256"] = manifest_digest(manifest)
        write_json(output / "manifest.json", manifest)
        report.update(status="complete", sha256=manifest["sha256"], bytes=manifest["bytes"],
                      tables=len(found["tables"]))
    except (RuntimeError, ValueError, OSError) as error:
        report.update(status="failed", error=str(error) if isinstance(error, RuntimeError) else "Local file error.")
    finally:
        if fixture:
            report["fixture"]["cleanup"] = _remove_fixture(config, pod["pod"], fixture)
            if report["fixture"]["cleanup"] != "removed" and report["status"] == "complete":
                report.update(status="failed", error="Backup completed, but fixture cleanup is incomplete.")
        write_json(report_path, report)
        write_json(consumed, {"plan_id": plan_id, "status": report["status"]})
    return report


# Inputs: configuration, Pod, run ID and schema. Outputs: fixture record. Purpose: write one known row that
# the restore must reproduce. Dependencies: psql in the application database.
def _write_fixture(config, pod, run_id, schema):
    database = config["application"]["database"]
    token = secrets.token_hex(16)
    found = psql(config, pod, database, "\n".join([
        "BEGIN;",
        f"CREATE SCHEMA {schema};",
        f"CREATE TABLE {schema}.fixtures (run_id text PRIMARY KEY, token text NOT NULL);",
        f"INSERT INTO {schema}.fixtures VALUES ({literal(run_id)}, {literal(token)});",
        f"SELECT {literal(schema)}::regnamespace::oid, {literal(schema + '.fixtures')}::regclass::oid;",
        "COMMIT;",
    ]))
    if len(found) != 1 or not re.fullmatch(r"[0-9]+\|[0-9]+", found[0]):
        raise RuntimeError("Could not confirm the new backup fixture's ownership.")
    schema_oid, table_oid = found[0].split("|")
    return {"run_id": run_id, "schema": schema, "token": token, "schema_oid": schema_oid, "table_oid": table_oid}


# Inputs: configuration, Pod and fixture record. Outputs: cleanup state.
# Purpose: lock the fixture, check both recorded OIDs and the sole row, then drop with RESTRICT.
# Dependencies: psql and transactional DDL.
def _remove_fixture(config, pod, fixture):
    database = config["application"]["database"]
    schema = fixture["schema"]
    table = f"{schema}.fixtures"
    statements = [
        "BEGIN;",
        f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE NOWAIT;",
        "DO $$ BEGIN",
        # Keep a concurrent rename from changing the qualified name protected by the table lock.
        "PERFORM oid FROM pg_catalog.pg_namespace",
        f"WHERE nspname = {literal(schema)} AND oid = {fixture['schema_oid']} FOR UPDATE NOWAIT;",
        "IF NOT FOUND",
        f"OR {literal(table)}::regclass::oid <> {fixture['table_oid']}",
        f"OR (SELECT count(*) FROM {table}) <> 1",
        f"OR NOT EXISTS (SELECT FROM {table} WHERE run_id = {literal(fixture['run_id'])}",
        f"AND token = {literal(fixture['token'])}) THEN",
        "RAISE EXCEPTION 'Backup fixture ownership changed'; END IF; END $$;",
        f"DROP TABLE {table} RESTRICT;",
        f"DROP SCHEMA {schema} RESTRICT;",
        "COMMIT;",
    ]
    try:
        psql(config, pod, database, "\n".join(statements))
        return "removed"
    except RuntimeError:
        return "failed"


# Inputs: table name as pg_dump wrote it. Outputs: SQL returning that table's exact row count.
# Purpose: count rows without interpolating identifiers; the regclass cast quotes the name.
# Dependencies: query_to_xml.
def _count_sql(table):
    return (
        "SELECT (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %s', "
        f"{literal(table)}::regclass), false, true, '')))[1]::text;"
    )


# Inputs: Docker arguments and optional SQL. Outputs: stdout. Dependencies: local Docker CLI.
# Purpose: suppress engine and database output when a drill command fails.
def _docker(argv, *args, sql=None):
    result = run(argv + list(args), input_text=sql, timeout=120)
    if result.returncode:
        raise RuntimeError(f"Docker restore command failed ({args[0]}).")
    return result.stdout.strip()


# Inputs: none. Outputs: local Docker socket and daemon ID. Dependencies: Docker context and info.
# Purpose: bind a reviewed drill to the local engine handling the backup.
def _restore_engine():
    endpoint = None if os.environ.get("DOCKER_CONTEXT") else os.environ.get("DOCKER_HOST")
    endpoint = endpoint or _docker(["docker"], "context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    if not endpoint.startswith("unix:///") or "\n" in endpoint:
        raise RuntimeError("Restore requires a local Docker engine on a Unix socket.")
    daemon = _docker(["docker", "--host", endpoint], "info", "--format", "{{.ID}}")
    if not daemon:
        raise RuntimeError("Could not identify the local Docker engine.")
    return {"endpoint": endpoint, "id": daemon}


# Inputs: Docker exec prefix, database and SQL. Outputs: result rows. Dependencies: psql.
# Purpose: send administrative and comparison SQL only to the isolated instance.
def _isolated_sql(argv, database, sql):
    args = _psql_argv(database, "--file=-", socket="/tmp")
    return _docker(argv, *args, sql=sql).splitlines()


# Inputs: Docker exec arguments. Outputs: none. Purpose: wait for the standalone server.
# Dependencies: pg_isready and the container's Unix socket.
def _wait_isolated(exec_args):
    deadline = time.monotonic() + 60
    while True:
        result = run(exec_args + ["pg_isready", "-q", "-h", "/tmp", "-U", "postgres"], timeout=10)
        if result.returncode == 0:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("The isolated PostgreSQL instance did not become ready.")
        time.sleep(1)


# Inputs: proposed database name, configuration and source. Outputs: validated name.
# Purpose: limit the drill to a distinct SQL identifier. Dependencies: PostgreSQL naming rules.
def _target_name(name, config, source):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise ValueError("The restore target must be a lowercase SQL identifier of at most 63 characters.")
    if name in RESERVED_DATABASES or name in {source, config["application"]["database"]}:
        raise ValueError("The restore target cannot be the source, the application, or a system database.")
    return name


# Inputs: configuration, workspace, backup directory, target options and plan ID/execute flag.
# Outputs: plan, preview or report. Purpose: restore a verified backup in a fresh Docker container.
# Dependencies: a local Docker engine, an immutable PostgreSQL image, and read-only Kubernetes GETs.
def restore(config, workspace, backup_dir, target=None, keep_target=False, plan_id=None, execute=False):
    options = {"backup": str(backup_dir), "target_database": target, "keep_target": keep_target}
    image = runtime.IMAGES["postgres"]["reference"]
    if plan_id is None:
        manifest, _ = verify_backup(backup_dir)
        if manifest["environment_id"] != config["environment_id"]:
            raise ValueError("The backup belongs to another environment.")
        identity, pod = preflight(config, workspace)
        run_id = secrets.token_hex(6)
        name = _target_name(
            target or f"{config['application']['database'][:40]}_restore_{run_id}",
            config, manifest["source"]["database"],
        )
        container = f"lkp-{config['environment_id']}-restore-{run_id}"
        review = _save_plan(workspace, "restore", {
            "action": "restore",
            "environment_id": config["environment_id"],
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "config_sha256": runtime._digest(config),
            "source_sha256": sha256(Path(__file__)),
            "options": options,
            "identity": _durable(identity),
            "backup": {
                "directory": str(backup_dir),
                "created_at": manifest["created_at"],
                "source": manifest["source"],
                "manifest_sha256": manifest["manifest_sha256"],
                "sha256": manifest["sha256"],
                "bytes": manifest["bytes"],
                "tables": manifest["tables"],
                "fixture": bool(manifest["fixture"]),
            },
            "target": {
                "database": name, "container": container, "image": image, "engine": _restore_engine(),
                "statefulset_uid": pod["statefulset_uid"], "data_instance": pod["data_instance"],
            },
            "writes": [
                f"Create local container {container} with image {image} and networking disabled",
                f"Replay the checked dump into {name} inside that container",
                "Keep the isolated container and its writable layer" if keep_target else
                "Remove the isolated container and its writable layer by full ID",
            ],
            "untouched": ["Existing cluster databases, roles and volumes"],
            "capacity": "Restore needs space for a private host snapshot and the Docker writable layer.",
        })
        return {"status": "planned", "requires_execute": True, **review}
    review, consumed = _load_plan(workspace, "restore", plan_id, config, options)
    if not execute:
        return {"status": "preview", "requires_execute": True, **review}
    with tempfile.TemporaryDirectory(prefix="lkp-restore-") as directory:
        manifest, dump = verify_backup(backup_dir, Path(directory) / "dump.sql")
        if manifest["manifest_sha256"] != review["backup"]["manifest_sha256"]:
            raise RuntimeError("The backup changed after review; prepare a new plan.")
        identity, pod = preflight(config, workspace)
        destination = review["target"]
        if _durable(identity) != review["identity"] or any(
            pod[key] != destination[key] for key in ("statefulset_uid", "data_instance")
        ):
            raise RuntimeError("Target identity or PostgreSQL instance changed after review.")
        if image != destination["image"] or _restore_engine() != destination["engine"]:
            raise RuntimeError("The restore image or local Docker engine changed after review.")
        # An empty CLI config keeps configured proxy credentials out of the restore container.
        cli_config = private_dir(Path(directory) / "docker")
        argv = ["docker", "--config", str(cli_config), "--host", destination["engine"]["endpoint"]]
        write_json(consumed, {"plan_id": plan_id, "status": "attempting"})
        return _restore_snapshot(workspace, review, consumed, manifest, dump, argv)


# Inputs: workspace, reviewed plan, consumed marker, manifest, verified snapshot and Docker prefix.
# Outputs: drill report. Purpose: own the container by its immutable ID and preserve all check results.
# Dependencies: Docker create/start/exec/rm and container-local PostgreSQL tools.
def _restore_snapshot(workspace, review, consumed, manifest, dump, argv):
    destination = review["target"]
    name = destination["database"]
    plan_id = review["plan_id"]
    report = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "plan_id": plan_id,
        "backup_sha256": manifest["sha256"],
        "backup_created_at": manifest["created_at"],
        "kube_system_uid": review["identity"]["kube_system_uid"],
        "statefulset_uid": destination["statefulset_uid"],
        "data_instance": destination["data_instance"],
        "backup_data_instance": manifest["source"].get("data_instance"),
        "backup_kube_system_uid": manifest["source"]["kube_system_uid"],
        "target_database": name,
        "target_container": destination["container"],
        "engine": destination["engine"],
        "keep_target": review["options"]["keep_target"],
        "target_cleanup": "ownership-unconfirmed",
        "status": "running",
    }
    report_path = workspace / "reports" / "restore.json"
    write_json(report_path, report)
    container_id = None
    try:
        created = _docker(
            argv, "create", "--name", destination["container"], "--network", "none",
            "--user", "999:999", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            # Mask the image's VOLUME. All drill data disappears with the container by ID.
            "--mount", "type=tmpfs,destination=/var/lib/postgresql/data,readonly",
            "--env", "PGDATA=/tmp/restore-data",
            "--log-driver", "none", "--entrypoint", "sh", destination["image"], "-ec",
            'if [ ! -s "$PGDATA/PG_VERSION" ]; then '
            'initdb -D "$PGDATA" --auth-local=trust --auth-host=reject --encoding=UTF8; fi; '
            'exec postgres -D "$PGDATA" -c listen_addresses= -c unix_socket_directories=/tmp',
        )
        if not re.fullmatch(r"[0-9a-f]{64}", created):
            raise RuntimeError("The new restore container ID could not be confirmed.")
        container_id = created
        report["container_id"] = container_id
        write_json(report_path, report)
        _docker(argv, "start", container_id)
        exec_args = argv + ["exec", "-i", container_id]
        _wait_isolated(exec_args)
        _isolated_sql(exec_args, "postgres", f"""CREATE DATABASE "{name}" TEMPLATE template0 ENCODING 'UTF8';""")
        code, _ = stream(
            exec_args + _psql_argv(name, "--single-transaction", "--file=-", socket="/tmp"),
            stdin_path=dump,
            timeout=DUMP_TIMEOUT,
        )
        if code:
            raise RuntimeError("Loading the dump into the isolated server failed.")
        report["checks"] = _compare(exec_args, name, manifest)
        report["status"] = "fail" if any(item["status"] == "fail" for item in report["checks"]) else "pass"
    except (RuntimeError, ValueError, OSError, IndexError) as error:
        report.update(status="failed", error=str(error) if isinstance(error, RuntimeError) else "Unexpected response.")
    finally:
        if container_id:
            if report["keep_target"]:
                report["target_cleanup"] = "kept"
            else:
                try:
                    _docker(argv, "rm", "--force", container_id)
                    report["target_cleanup"] = "removed"
                except RuntimeError:
                    report["target_cleanup"] = "remove-failed"
                    if report["status"] == "pass":
                        report.update(status="failed", error="Data checks passed, but restore cleanup is incomplete.")
        write_json(report_path, report)
        write_json(consumed, {"plan_id": plan_id, "status": report["status"]})
    return report


# Inputs: isolated Docker exec prefix, target and manifest. Outputs: check list.
# Purpose: compare exact row counts per table and the fixture row. Dependencies: psql.
def _compare(exec_args, name, manifest):
    checks = []
    for table, rows in sorted(manifest["tables"].items()):
        found = _isolated_sql(exec_args, name, _count_sql(table))
        observed = int(found[0]) if found else None
        checks.append({
            "name": f"rows:{table}",
            "status": "pass" if observed == rows else "fail",
            "expected": rows,
            "observed": observed,
        })
    fixture = manifest.get("fixture")
    if fixture:
        schema = fixture.get("schema", FIXTURE_SCHEMA)
        found = _isolated_sql(exec_args, name, (
            f"SELECT token FROM {schema}.fixtures WHERE run_id = {literal(fixture['run_id'])};"
        ))
        digest = hashlib.sha256((found[0] if found else "").encode()).hexdigest()
        checks.append({
            "name": "fixture",
            "status": "pass" if found == [fixture["token"]] else "fail",
            "observed_sha256": digest,
        })
    else:
        checks.append({"name": "fixture", "status": "pending", "detail": "The backup was taken without a fixture."})
    return checks


# Inputs: CLI path. Outputs: resolved backup root. Purpose: keep backups out of the Minikube home,
# the workspace and the installed skill. Dependencies: cluster Minikube home resolution.
def backup_root(path, workspace, root):
    if path.is_symlink():
        raise ValueError("The backup directory cannot be a symbolic link.")
    resolved = path.expanduser().resolve()
    from cluster import _minikube_home

    homes = {_minikube_home().resolve(), (Path.home() / ".minikube").resolve()}
    try:
        recorded = read_json(workspace / "reports" / "cluster.json").get("minikube_home")
        if recorded:
            homes.add(Path(recorded).resolve())
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        pass
    for forbidden in (*homes, workspace, root):
        if resolved == forbidden or resolved.is_relative_to(forbidden):
            raise ValueError("Backups must live outside the Minikube home, the workspace and the skill.")
    return resolved


# Inputs: configuration, workspace and current identity. Outputs: one verify acceptance entry.
# Purpose: count a passing restore drill only for this cluster and PostgreSQL instance.
# Dependencies: reports/restore.json, kubectl GET.
def evidence(config, workspace, identity):
    path = workspace / "reports" / "restore.json"
    hint = "Run backup --with-fixture, then restore, each through a reviewed plan."
    if not path.exists():
        return runtime._acceptance("postgres_backup_restore", "pending", hint)
    report = read_json(path)
    derived = config["derived"]
    try:
        statefulset = runtime._get(config, "statefulsets.apps", derived["postgres_name"], derived["namespace"])
        storage = data_instance(config)
    except runtime.ReadError as error:
        return runtime._acceptance("postgres_backup_restore", "pending", f"PostgreSQL identity unreadable ({error.reason}).")
    except RuntimeError as error:
        return runtime._acceptance("postgres_backup_restore", "pending", str(error))
    current = (statefulset or {}).get("metadata", {}).get("uid")
    if (
        not current
        or report.get("kube_system_uid") != identity["kube_system_uid"]
        or report.get("backup_kube_system_uid") != identity["kube_system_uid"]
        or report.get("statefulset_uid") != current
        or report.get("data_instance") != storage
        or report.get("backup_data_instance") != storage
    ):
        return runtime._acceptance("postgres_backup_restore", "pending", "The recorded drill is for another instance. " + hint)
    detail = {
        "captured_at": report.get("captured_at"),
        "backup_created_at": report.get("backup_created_at"),
        "fixture": next((item["status"] for item in report.get("checks", []) if item["name"] == "fixture"), None),
    }
    status = {"pass": "pass", "fail": "fail"}.get(report.get("status"), "pending")
    expected_cleanup = "kept" if report.get("keep_target") else "removed"
    if status == "pass" and (
        detail["fixture"] != "pass" or report.get("target_cleanup") != expected_cleanup
    ):
        status = "pending"
    detail["target_cleanup"] = report.get("target_cleanup")
    return runtime._acceptance("postgres_backup_restore", status, detail)
