"""Purpose: run one authorized GPU smoke-test Pod that checks device allocation and injection, then remove it."""

import datetime as dt
import json
import time

import runtime
from io_utils import read_json, run, write_json

WAIT_SECONDS = 150
PASS_MARKER = "PASS: GPU device allocated and render node opened as non-root."


# Inputs: resolved configuration. Outputs: smoke-test Pod manifest.
# Purpose: request one plugin slot from a restricted, non-root Pod. Dependencies: pinned BusyBox image.
def pod_manifest(config):
    derived = config["derived"]
    device = derived["gpu_device"]
    resource = derived["gpu_resource"]
    name = f"{config['environment_id']}-gpu-smoke"
    script = "\n".join(
        [
            f"test -c {device}",
            f"test -r {device}",
            f"test -w {device}",
            f"exec 3<>{device}",
            "exec 3>&-",
            f"echo '{PASS_MARKER}'",
        ]
    )
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": derived["validation_namespace"],
            "labels": {**derived["labels"], "app.kubernetes.io/name": name},
        },
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 120,
            "automountServiceAccountToken": False,
            "nodeSelector": {"kubernetes.io/os": "linux"},
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "check-device",
                    "image": runtime.IMAGES["storage_helper"]["reference"],
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["sh", "-ec"],
                    "args": [script],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "runAsNonRoot": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "16Mi", resource: "1"},
                        "limits": {"cpu": "100m", "memory": "32Mi", resource: "1"},
                    },
                }
            ],
        },
    }


# Inputs: Pod object. Outputs: phase and the scheduling reason, if any.
# Purpose: explain a Pending Pod, such as all slots being held. Dependencies: Pod status conditions.
def _pod_state(item):
    status = (item or {}).get("status", {})
    scheduled = next(
        (entry for entry in status.get("conditions", []) if entry.get("type") == "PodScheduled"),
        {},
    )
    return {
        "phase": status.get("phase", "Absent"),
        "scheduling": scheduled.get("reason"),
        "node": (item or {}).get("spec", {}).get("nodeName"),
    }


# Inputs: resolved configuration, the created Pod manifest and its UID.
# Outputs: final state, log tail and deletion result. Dependencies: kubectl get/logs/delete.
def _run_pod(config, manifest, uid):
    metadata = manifest["metadata"]
    state, output = {"phase": "Unknown"}, ""
    try:
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            state = _pod_state(
                runtime._get(config, "pods", metadata["name"], metadata["namespace"])
            )
            if state["phase"] in {"Succeeded", "Failed", "Absent"}:
                break
            time.sleep(2)
        logs = run(
            runtime._kube(config)
            + ["logs", metadata["name"], "--namespace", metadata["namespace"], "--tail=20"],
            timeout=30,
        )
        output = logs.stdout[-2000:] if logs.returncode == 0 else ""
    finally:
        # Always remove the Pod this run created; a different UID means someone replaced it.
        live = runtime._get(config, "pods", metadata["name"], metadata["namespace"])
        deleted = live is None
        if live and live["metadata"].get("uid") == uid:
            result = run(
                runtime._kube(config)
                + ["delete", "pod", metadata["name"], "--namespace", metadata["namespace"]]
                + ["--wait=true", "--timeout=60s"],
                timeout=90,
            )
            deleted = result.returncode == 0
    return state, output, deleted


# Inputs: resolved configuration, private workspace and explicit execution flag.
# Outputs: preview or smoke result. Purpose: prove Pod device injection. Dependencies: runtime checks, kubectl.
def smoke(config, workspace, execute=False):
    manifest = pod_manifest(config)
    metadata = manifest["metadata"]
    summary = {
        "pod": f"{metadata['namespace']}/{metadata['name']}",
        "image": manifest["spec"]["containers"][0]["image"],
        "request": {config["derived"]["gpu_resource"]: "1"},
        "effects": [
            "Create one non-root Pod in the validation namespace",
            "Wait up to 150 seconds, read its log, then delete that Pod",
        ],
        "scope": "Device allocation and injection only; Vulkan and inference are not tested.",
    }
    if not execute:
        return {"status": "preview", "requires_execute": True, **summary}
    report = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "blocked",
        **summary,
    }
    path = workspace / "reports" / "gpu-smoke.json"
    identity = runtime._identity(config)
    if not runtime._bind(config, workspace, identity):
        report["detail"] = "The target must match the workspace binding."
        write_json(path, report)
        return report
    applied = workspace / "reports" / "applied-20-gpu.json"
    ready, detail = runtime._stage_ready(config, "20-gpu")
    if not applied.exists() or read_json(applied).get("status") != "applied" or not ready:
        report["detail"] = {"summary": "Apply 20-gpu and wait for registration first.", **detail}
        write_json(path, report)
        return report
    if runtime._get(config, "pods", metadata["name"], metadata["namespace"]) is not None:
        report["detail"] = (
            "A previous smoke-test Pod exists; inspect and remove it before retrying."
        )
        write_json(path, report)
        return report
    plugin = runtime._get(
        config,
        "daemonsets.apps",
        config["derived"]["gpu_plugin_name"],
        config["derived"]["gpu_namespace"],
    )
    report.update(
        kube_system_uid=identity["kube_system_uid"],
        plugin={
            "uid": plugin["metadata"].get("uid"),
            "generation": plugin["metadata"].get("generation"),
        },
    )
    created = run(
        runtime._kube(config) + ["create", "-f", "-", "-o", "json"],
        input_text=json.dumps(manifest),
        timeout=30,
    )
    if created.returncode:
        report.update(status="failed", detail="The smoke-test Pod could not be created.")
        write_json(path, report)
        return report
    uid = json.loads(created.stdout)["metadata"]["uid"]
    state, output, deleted = _run_pod(config, manifest, uid)
    passed = state["phase"] == "Succeeded" and PASS_MARKER in output
    report.update(
        status="pass" if passed else "failed",
        node=state.get("node"),
        state=state,
        log_tail=output,
        pod_deleted=deleted,
    )
    if not passed and state.get("scheduling") == "Unschedulable":
        report["detail"] = "No free GPU slot; another Pod may hold it. Retry when the slot is free."
    if not deleted:
        report["detail"] = "The smoke-test Pod was not deleted; inspect it before retrying."
    write_json(path, report)
    return report
