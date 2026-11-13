"""Purpose: prepare PostgreSQL credentials and explicitly hand approved credentials to applications."""

import base64
import json
import os
import re
import secrets

from io_utils import private_dir, read_json, run, write_json


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


# Inputs: OpenSSL argument list and private directory. Outputs: stdout.
# Purpose: execute certificate tooling without printing key material. Dependencies: openssl.
def openssl(arguments, directory):
    result = run(["openssl", *arguments], cwd=directory)
    if result.returncode:
        raise RuntimeError(
            "Certificate operation failed; inspect protected material before retrying"
        )
    return result.stdout


# Inputs: normalized config and prepared credential directory. Outputs: verified password bundle.
# Purpose: verify external persisted material before reuse. Dependencies: OpenSSL and private files.
def verify_bundle(config, directory):
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
    openssl(["verify", "-CAfile", "ca.crt", "server.crt"], directory)
    for certificate, key in (("ca.crt", "ca.key"), ("server.crt", "server.key")):
        openssl(["x509", "-in", certificate, "-noout", "-checkend", "86400"], directory)
        public = openssl(["x509", "-in", certificate, "-noout", "-pubkey"], directory)
        if public != openssl(["pkey", "-in", key, "-pubout"], directory):
            raise ValueError("Certificate and private key do not match")
    for hostname in (expected["host"], "localhost"):
        openssl(["x509", "-in", "server.crt", "-noout", "-checkhost", hostname], directory)
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
    verify_bundle(config, directory)
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

        inventory = inspect(config, workspace)
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
        write_json(workspace / "reports" / f"credentials-{component}.json", report)
        try:
            for document in documents:
                metadata = document["metadata"]
                report["results"].append(
                    {
                        "reference": f"{metadata['namespace']}/{metadata['name']}",
                        "status": publish(config, document),
                    }
                )
                write_json(workspace / "reports" / f"credentials-{component}.json", report)
            report["status"] = "published"
        except (RuntimeError, ValueError, KeyError, OSError):
            report["status"] = "failed"
            write_json(workspace / "reports" / f"credentials-{component}.json", report)
            raise
    write_json(workspace / "reports" / f"credentials-{component}.json", report)
    return report
