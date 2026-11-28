"""Purpose: prepare PostgreSQL credentials and explicitly hand approved credentials to applications."""

import base64
import datetime as dt
import json
import os
import re
import secrets

import runtime
from io_utils import private_dir, read_json, run, write_json
from prerequisites import find_openssl


# Inputs: normalized config and kubectl arguments/stdin. Outputs: decoded JSON or empty mapping.
# Purpose: access only the explicitly configured cluster. Dependencies: kubectl and admin kubeconfig.
def kube(config, arguments, document=None):
    cluster = config["cluster"]
    result = run(
        [
            "kubectl",
            "--kubeconfig",
            cluster["kubeconfig"],
            "--context",
            cluster["context"],
            "--request-timeout=15s",
            *arguments,
        ],
        input_text=json.dumps(document) if document is not None else None,
    )
    if result.returncode:
        raise RuntimeError("Credential API operation failed; command output was suppressed")
    return json.loads(result.stdout) if result.stdout.strip() else {}


# Inputs: normalized config. Outputs: credential naming contract.
# Purpose: bind prepared material to identities and certificate names. Dependencies: derived names.
def contract(config):
    derived = config["derived"]
    return {
        "environment_id": config["environment_id"],
        "namespace": derived["namespace"],
        "name": derived["postgres_name"],
        "username": config["application"]["username"],
        "database": config["application"]["database"],
        "host": f"{derived['postgres_name']}-rw.{derived['namespace']}.svc.cluster.local",
    }


# Inputs: OpenSSL argument list, private directory and resolved executable. Outputs: stdout.
# Purpose: execute certificate tooling without printing key material. Dependencies: openssl.
def openssl(arguments, directory, executable):
    result = run([executable, *arguments], cwd=directory)
    if result.returncode:
        raise RuntimeError(
            "Certificate operation failed; inspect protected material before retrying"
        )
    return result.stdout


# Inputs: none. Outputs: a resolved OpenSSL path or a preparation hint.
# Purpose: resolve local certificate tooling once per credential operation. Dependencies: prerequisite tool discovery.
def certificate_tool():
    tool = find_openssl()
    if tool is None:
        raise RuntimeError(
            "A supported OpenSSL is required; run prerequisites --component openssl first"
        )
    return tool["path"]


# Inputs: normalized config, credential directory and optional resolved executable. Outputs: verified password bundle.
# Purpose: verify external persisted material before reuse. Dependencies: OpenSSL and private files.
def verify_bundle(config, directory, executable=None):
    expected = contract(config)
    for filename in ("bundle.json", "ca.key", "ca.crt", "server.key", "server.crt"):
        path = directory / filename
        info = path.lstat()
        if (
            not path.is_file()
            or path.is_symlink()
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("Credential files must be private regular files owned by this user")
    bundle = read_json(directory / "bundle.json")
    if bundle.get("contract") != expected:
        raise ValueError("Prepared credentials belong to a different database identity")
    if not all(
        isinstance(bundle.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", bundle[key])
        for key in ("admin_password", "app_password", "metrics_password")
    ):
        raise ValueError("Prepared password material is invalid; no password was reset")
    executable = executable or certificate_tool()
    openssl(["verify", "-CAfile", "ca.crt", "server.crt"], directory, executable)
    for certificate, key in (("ca.crt", "ca.key"), ("server.crt", "server.key")):
        openssl(["x509", "-in", certificate, "-noout", "-checkend", "86400"], directory, executable)
        public = openssl(["x509", "-in", certificate, "-noout", "-pubkey"], directory, executable)
        if public != openssl(["pkey", "-in", key, "-pubout"], directory, executable):
            raise ValueError("Certificate and private key do not match")
    for hostname in (expected["host"], "localhost"):
        openssl(
            ["x509", "-in", "server.crt", "-noout", "-checkhost", hostname], directory, executable
        )
    return bundle


# Inputs: normalized config and private workspace. Outputs: local credential directory.
# Purpose: generate once or verify existing material without rotation. Dependencies: OpenSSL, CSPRNG.
def prepare_postgres(config, workspace):
    directory = private_dir(workspace / "credentials" / "postgres")
    if (directory / "bundle.json").exists():
        verify_bundle(config, directory)
        return directory
    if any(directory.iterdir()):
        raise ValueError("Partial credential material exists; review it before recovery")
    executable = certificate_tool()
    identity = contract(config)
    host = identity["host"]
    extensions = directory / "server.ext"
    extensions.write_text(
        f"# Purpose: constrain the local PostgreSQL server certificate.\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:{host},DNS:localhost,IP:127.0.0.1\n",
        encoding="utf-8",
    )
    extensions.chmod(0o600)
    previous_umask = os.umask(0o077)
    try:
        openssl(
            [
                "req",
                "-new",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "3650",
                "-subj",
                f"/CN={identity['environment_id']}-postgres-ca",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-keyout",
                "ca.key",
                "-out",
                "ca.crt",
            ],
            directory,
            executable,
        )
        openssl(
            [
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                f"/CN={identity['name']}",
                "-keyout",
                "server.key",
                "-out",
                "server.csr",
            ],
            directory,
            executable,
        )
        openssl(
            [
                "x509",
                "-req",
                "-in",
                "server.csr",
                "-CA",
                "ca.crt",
                "-CAkey",
                "ca.key",
                "-CAcreateserial",
                "-days",
                "365",
                "-extfile",
                "server.ext",
                "-out",
                "server.crt",
            ],
            directory,
            executable,
        )
    finally:
        os.umask(previous_umask)
    write_json(
        directory / "bundle.json",
        {
            "contract": identity,
            "admin_password": secrets.token_hex(32),
            "app_password": secrets.token_hex(32),
            "metrics_password": secrets.token_hex(32),
        },
    )
    verify_bundle(config, directory, executable)
    return directory


# Inputs: namespace/name, plaintext approved fields, config. Outputs: Kubernetes Secret document.
# Purpose: encode only approved fields for stdin transport. Dependencies: base64 and ownership labels.
def secret_document(config, namespace, name, data):
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"namespace": namespace, "name": name, "labels": config["derived"]["labels"]},
        "type": "Opaque",
        "data": {key: base64.b64encode(value.encode()).decode() for key, value in data.items()},
    }


# Inputs: config and desired Secret. Outputs: creation/unchanged status.
# Purpose: create atomically or accept exact owned data; never adopt or reset unknown credentials.
# Dependencies: metadata-checked namespace and kubectl API access.
def publish(config, document):
    metadata = document["metadata"]
    response = kube(
        config,
        [
            "get",
            "secret",
            metadata["name"],
            "-n",
            metadata["namespace"],
            "--ignore-not-found",
            "-o",
            "json",
        ],
    )
    if response:
        existing = response["metadata"]
        owned = all(
            existing.get("labels", {}).get(key) == value
            for key, value in config["derived"]["labels"].items()
        )
        if not owned or existing.get("ownerReferences"):
            raise ValueError(
                "An existing Secret has a different owner; no credentials were adopted"
            )
        if response.get("data") != document["data"] or response.get("type", "Opaque") != "Opaque":
            raise ValueError(
                "Credential drift requires an explicit rotation plan; no values were overwritten"
            )
        return "unchanged"
    kube(config, ["create", "-f", "-", "-o", "json"], document)
    return "created"


# Inputs: config and private workspace. Outputs: approved PostgreSQL Secret documents.
# Purpose: build source and application handoff credentials in memory. Dependencies: verified local bundle.
def postgres_documents(config, workspace):
    directory = workspace / "credentials" / "postgres"
    bundle = verify_bundle(config, directory)
    identity = contract(config)
    namespace, name = identity["namespace"], identity["name"]
    ca = (directory / "ca.crt").read_text(encoding="utf-8")
    return [
        secret_document(
            config,
            namespace,
            f"{name}-admin",
            {"username": "postgres", "password": bundle["admin_password"]},
        ),
        secret_document(
            config,
            namespace,
            f"{name}-app",
            {"username": identity["username"], "password": bundle["app_password"]},
        ),
        secret_document(
            config,
            namespace,
            f"{name}-metrics",
            {"username": "postgres_exporter", "password": bundle["metrics_password"]},
        ),
        secret_document(
            config,
            namespace,
            f"{name}-tls",
            {
                "ca.crt": ca,
                "tls.crt": (directory / "server.crt").read_text(encoding="utf-8"),
                "tls.key": (directory / "server.key").read_text(encoding="utf-8"),
            },
        ),
        secret_document(
            config,
            config["application"]["namespace"],
            f"{name}-client",
            {"username": identity["username"], "password": bundle["app_password"], "ca.crt": ca},
        ),
    ]


# Inputs: normalized config. Outputs: approved Kafka application Secret documents.
# Purpose: copy fixed Strimzi user fields and public CA only. Dependencies: running Strimzi users.
def kafka_documents(config):
    namespace, name = config["kafka"]["namespace"], config["derived"]["kafka_name"]
    ca = kube(config, ["get", "secret", f"{name}-cluster-ca-cert", "-n", namespace, "-o", "json"])
    documents = []
    for role in ("producer", "consumer"):
        user = kube(config, ["get", "secret", f"{name}-{role}", "-n", namespace, "-o", "json"])
        if (
            user["metadata"].get("labels", {}).get("strimzi.io/cluster") != name
            or ca["metadata"].get("labels", {}).get("strimzi.io/cluster") != name
        ):
            raise ValueError("Kafka source credentials do not match the selected cluster")
        data = {
            key: base64.b64decode(user["data"][key], validate=True).decode()
            for key in ("user.crt", "user.key")
        }
        data["ca.crt"] = base64.b64decode(ca["data"]["ca.crt"], validate=True).decode()
        documents.append(
            secret_document(
                config, config["application"]["namespace"], f"{name}-{role}-client", data
            )
        )
    return documents


# Inputs: normalized config, workspace, component and execution flags. Outputs: non-sensitive report.
# Purpose: preview, prepare locally, or explicitly publish credentials. Dependencies: selected component tools.
def credentials(config, workspace, component="postgres", *, prepare=False, execute=False):
    derived = config["derived"]
    report_path = workspace / "reports" / f"credentials-{component}.json"
    runtime._remember_objects(workspace, runtime._reported_objects(report_path))
    if component == "postgres":
        refs = [
            f"{derived['namespace']}/{derived['postgres_name']}-{suffix}"
            for suffix in ("admin", "app", "metrics", "tls")
        ]
        refs.append(f"{config['application']['namespace']}/{derived['postgres_name']}-client")
    else:
        refs = [
            f"{config['application']['namespace']}/{derived['kafka_name']}-{role}-client"
            for role in ("producer", "consumer")
        ]
    report = {
        "status": "preview",
        "component": component,
        "secret_references": refs,
        "cluster_writes": False,
    }
    if prepare:
        report.update(status="prepared", directory=str(prepare_postgres(config, workspace)))
    if execute:
        from runtime import inspect

        inventory = inspect(config, workspace, stage="00-bootstrap")
        if inventory.get("status") in {"blocked", "failed"}:
            raise ValueError("Cluster inspection is blocked; no credentials were written")
        bootstrap = workspace / "reports" / "applied-00-bootstrap.json"
        if not bootstrap.is_file() or read_json(bootstrap).get("status") != "applied":
            raise ValueError("Apply the reviewed bootstrap stage before publishing credentials")
        for namespace in {derived["namespace"], config["application"]["namespace"]}:
            response = kube(config, ["get", "namespace", namespace, "-o", "json"])
            if not all(
                response["metadata"].get("labels", {}).get(key) == value
                for key, value in derived["labels"].items()
            ):
                raise ValueError(
                    "Credential destination namespace is not owned by this environment"
                )
        documents = (
            postgres_documents(config, workspace)
            if component == "postgres"
            else kafka_documents(config)
        )
        report.update(status="publishing", cluster_writes=True, results=[])
        write_json(report_path, report)
        try:
            for document in documents:
                metadata = document["metadata"]
                status = publish(config, document)
                report["results"].append(
                    {
                        "reference": f"{metadata['namespace']}/{metadata['name']}",
                        "status": status,
                    }
                )
                runtime._remember_objects(workspace, [{
                    "source": report_path.stem, "resource": "secrets",
                    "namespace": metadata["namespace"], "name": metadata["name"], "uid": None,
                }])
                write_json(report_path, report)
            report["status"] = "published"
        except (RuntimeError, ValueError, KeyError, OSError):
            report["status"] = "failed"
            write_json(report_path, report)
            raise
    write_json(report_path, report)
    return report


WARNING_DAYS = 30
CRITICAL_DAYS = 7


# Inputs: configuration, namespace, Secret name and data key. Outputs: (state, PEM text or None).
# Purpose: read one public certificate field without fetching private keys or passwords.
# Dependencies: kubectl jsonpath, runtime read-failure classification.
def _certificate_field(config, namespace, name, key):
    escaped = key.replace(".", "\\.")
    result = runtime.run(
        runtime._kube(config)
        + ["get", "secret", name, "--namespace", namespace, "--ignore-not-found=true"]
        + ["-o", f"jsonpath={{.data.{escaped}}}"],
        timeout=20,
    )
    if result.returncode:
        return runtime._read_failure(result), None
    if not result.stdout.strip():
        return "missing", None
    try:
        return "present", base64.b64decode(result.stdout.strip(), validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return "unreadable", None


# Inputs: PEM text, OpenSSL path and current time. Outputs: expiry facts.
# Purpose: read notAfter and a fingerprint without writing the certificate anywhere.
# Dependencies: openssl x509.
def _certificate_facts(pem, executable, now):
    result = run(
        [executable, "x509", "-noout", "-enddate", "-subject", "-fingerprint", "-sha256"],
        input_text=pem,
        timeout=20,
    )
    if result.returncode:
        return {"state": "unreadable"}
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    not_after = dt.datetime.strptime(
        " ".join(fields["notAfter"].split()), "%b %d %H:%M:%S %Y %Z"
    ).replace(tzinfo=dt.timezone.utc)
    remaining = (not_after - now).total_seconds() / 86400
    status = (
        "expired" if remaining < 0
        else "critical" if remaining < CRITICAL_DAYS
        else "warning" if remaining < WARNING_DAYS
        else "ok"
    )
    fingerprint = next(value for key, value in fields.items() if "Fingerprint" in key)
    return {
        "state": "present",
        "status": status,
        "subject": fields.get("subject", "").strip(),
        "not_after": not_after.isoformat(),
        "remaining_days": round(remaining, 1),
        "fingerprint_sha256": fingerprint.strip(),
    }


# Inputs: PEM bundle, OpenSSL path and time. Outputs: earliest expiry and all certificate identities.
# Purpose: compare trust stores independently of PEM ordering. Dependencies: _certificate_facts.
def _bundle_facts(pem, executable, now):
    begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----", pem)
    if not blocks or len(blocks) != pem.count(begin) or len(blocks) != pem.count(end):
        return {"state": "unreadable"}
    facts = [_certificate_facts(block, executable, now) for block in blocks]
    if any(item["state"] != "present" for item in facts):
        return {"state": "unreadable"}
    return {
        **min(facts, key=lambda item: item["not_after"]),
        "fingerprints_sha256": sorted({item["fingerprint_sha256"] for item in facts}),
    }


# Inputs: configuration. Outputs: {secret name: [Pod names]} in the application namespace.
# Purpose: list clients that must reload after a credential change. Dependencies: kubectl GET pods.
def _secret_consumers(config):
    pods = runtime._get(config, "pods", namespace=config["application"]["namespace"]) or {}
    consumers = {}
    for pod in pods.get("items", []):
        spec = pod.get("spec", {})
        names = set()
        for volume in spec.get("volumes", []):
            names.add((volume.get("secret") or {}).get("secretName"))
            for source in (volume.get("projected") or {}).get("sources", []):
                names.add((source.get("secret") or {}).get("name"))
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            for variable in container.get("env", []):
                names.add(((variable.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name"))
            for source in container.get("envFrom", []):
                names.add((source.get("secretRef") or {}).get("name"))
        for name in names - {None}:
            consumers.setdefault(name, []).append(pod["metadata"]["name"])
    return {name: sorted(items) for name, items in consumers.items()}


# Inputs: configuration. Outputs: certificate sources as (group, reference, namespace, Secret, key).
# Purpose: list each certificate and the copy it must match. Dependencies: credential naming.
def _certificate_sources(config):
    derived, application = config["derived"], config["application"]["namespace"]
    sources = []
    if config["postgres"]["enabled"]:
        name, namespace = derived["postgres_name"], derived["namespace"]
        sources += [
            ("postgres-ca", namespace, f"{name}-tls", "ca.crt"),
            ("postgres-server", namespace, f"{name}-tls", "tls.crt"),
            ("postgres-ca", application, f"{name}-client", "ca.crt"),
        ]
    if config["kafka"]["enabled"]:
        name, namespace = derived["kafka_name"], config["kafka"]["namespace"]
        sources += [
            ("kafka-cluster-ca", namespace, f"{name}-cluster-ca-cert", "ca.crt"),
            ("kafka-clients-ca", namespace, f"{name}-clients-ca-cert", "ca.crt"),
        ]
        for role in ("producer", "consumer"):
            sources += [
                (f"kafka-{role}", namespace, f"{name}-{role}", "user.crt"),
                (f"kafka-{role}", application, f"{name}-{role}-client", "user.crt"),
                ("kafka-cluster-ca", application, f"{name}-{role}-client", "ca.crt"),
            ]
    return sources


# Inputs: configuration and workspace. Outputs: read-only expiry report.
# Purpose: report certificate lifetimes, copies that no longer match their source, and the client
# Pods affected. Dependencies: OpenSSL, kubectl GET; no writes.
def expiry(config, workspace):
    tool = find_openssl()
    if tool is None:
        raise RuntimeError("A supported OpenSSL is required; run prerequisites --component openssl first")
    identity = runtime._identity(config)
    if not runtime._bind(config, workspace, identity):
        raise RuntimeError("The target must match the workspace binding.")
    now = dt.datetime.now(dt.timezone.utc)
    certificates = []
    local = workspace / "credentials" / "postgres"
    if config["postgres"]["enabled"]:
        for filename, group in (("ca.crt", "postgres-ca"), ("server.crt", "postgres-server")):
            path = local / filename
            entry = {"group": group, "reference": f"workspace:credentials/postgres/{filename}"}
            entry.update(
                _bundle_facts(path.read_text(encoding="utf-8"), tool["path"], now)
                if path.is_file() and not path.is_symlink()
                else {"state": "missing"}
            )
            certificates.append(entry)
    for group, namespace, name, key in _certificate_sources(config):
        state, pem = _certificate_field(config, namespace, name, key)
        entry = {"group": group, "reference": f"{namespace}/{name}:{key}"}
        entry.update(_bundle_facts(pem, tool["path"], now) if pem else {"state": state})
        certificates.append(entry)
    try:
        consumers, consumer_error = _secret_consumers(config), None
    except runtime.ReadError as error:
        consumers, consumer_error = {}, error.reason
    application = config["application"]["namespace"]
    for entry in certificates:
        namespace, _, rest = entry["reference"].partition("/")
        if namespace == application:
            entry["client_pods"] = consumers.get(rest.split(":")[0], [])
    # Copies must carry the same certificate as their source; a difference means a renewal was
    # not handed off, or a handoff happened without a server change.
    stale = []
    groups = {}
    for entry in certificates:
        if entry.get("fingerprints_sha256"):
            groups.setdefault(entry["group"], set()).add(tuple(entry["fingerprints_sha256"]))
    for group, prints in sorted(groups.items()):
        if len(prints) > 1:
            stale.append({
                "group": group,
                "references": [item["reference"] for item in certificates if item["group"] == group],
            })
    statuses = {entry.get("status") for entry in certificates}
    unreadable = [entry["reference"] for entry in certificates if entry["state"] != "present"]
    status = (
        "fail" if statuses & {"expired"} or stale
        else "warning" if statuses & {"critical", "warning"} or unreadable
        else "pass"
    )
    report = {
        "captured_at": now.isoformat(),
        "status": status,
        "thresholds_days": {"warning": WARNING_DAYS, "critical": CRITICAL_DAYS},
        "certificates": certificates,
        "mismatched_copies": stale,
        "unavailable": unreadable,
        "client_pods_unavailable": consumer_error,
        "cluster_writes": False,
        "next_steps": "See the credential renewal order in references/operations.md.",
    }
    write_json(workspace / "reports" / "credentials-expiry.json", report)
    return report
