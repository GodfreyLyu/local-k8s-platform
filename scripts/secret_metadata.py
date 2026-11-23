"""Purpose: read Secret metadata through a private, read-only kubectl proxy."""

import http.client
import json
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from runtime import _kube

ACCEPT = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"


# Inputs: normalized configuration. Outputs: a private Unix socket for the life of the context.
# Purpose: reuse kubeconfig authentication without copying credentials. Dependencies: kubectl proxy.
@contextmanager
def proxy(config):
    # Keep the path short enough for Unix sockets even when the workspace or TMPDIR is long.
    with tempfile.TemporaryDirectory(prefix="lkp-", dir="/tmp") as directory:
        path = Path(directory) / "api.sock"
        command = _kube(config) + [
            "proxy",
            f"--unix-socket={path}",
            "--accept-paths=^/api/v1/namespaces/[^/]+/secrets/[^/]+$",
            "--reject-methods=^(POST|PUT|PATCH|DELETE|CONNECT|OPTIONS|TRACE)$",
        ]
        with subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            umask=0o077,
        ) as process:
            try:
                deadline = time.monotonic() + 5
                while not path.exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Cannot start the private Secret metadata proxy.")
                    time.sleep(0.05)
                yield path
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


# Inputs: socket path, API route and media type. Outputs: HTTP status and decoded response.
# Purpose: send one bounded read through the private Unix socket. Dependencies: local HTTP proxy.
def _request(path, url, accept):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(20)
            connection.connect(str(path))
            connection.sendall(
                f"GET {url} HTTP/1.1\r\nHost: localhost\r\nAccept: {accept}\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            with http.client.HTTPResponse(connection) as response:
                response.begin()
                return response.status, json.load(response) if response.status in (200, 404) else {}
    except (OSError, ValueError, http.client.HTTPException):
        raise RuntimeError("Cannot read Secret metadata from the Kubernetes API.") from None


# Inputs: metadata proxy socket, namespace and name. Outputs: object metadata or None.
# Purpose: require PartialObjectMetadata and distinguish object absence from API errors.
def get(path, namespace, name):
    url = f"/api/v1/namespaces/{quote(namespace, safe='')}/secrets/{quote(name, safe='')}"
    status, body = _request(path, url, ACCEPT)
    if status not in (200, 404) or not isinstance(body, dict):
        raise RuntimeError("Cannot read Secret metadata. Check API access and permissions.")
    details = body.get("details") or {}
    if (
        status == 404
        and body.get("kind") == "Status"
        and body.get("reason") == "NotFound"
        and isinstance(details, dict)
        and details.get("name") == name
        and details.get("kind") == "secrets"
    ):
        return None
    if (
        status != 200
        or body.get("kind") != "PartialObjectMetadata"
        or body.get("apiVersion") != "meta.k8s.io/v1"
        or "data" in body
    ):
        raise RuntimeError("The Kubernetes API did not return a Secret metadata response.")
    metadata = body.get("metadata", {})
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != name
        or metadata.get("namespace") != namespace
    ):
        raise RuntimeError("The response does not identify the requested Secret.")
    return metadata
