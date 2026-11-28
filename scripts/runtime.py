# Purpose: Inspect Minikube and execute individually reviewed Terraform stages without managing credentials.
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import time
import urllib.parse
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path

from capacity import assess, pod_memory, quantity_mib
from config import memory_mib
from io_utils import private_dir, read_json, run, sha256, write_json

ROOT = Path(__file__).resolve().parents[1]
IMAGES = read_json(ROOT / "assets" / "infra" / "versions.json")["images"]
ENVIRONMENT_LABEL = "local-k8s-platform/environment"
GPU_REGISTRATION_RETRIES = 15
RESOURCE_TYPES = {
    "kubernetes_namespace_v1": "namespaces",
    "kubernetes_service_account_v1": "serviceaccounts",
    "kubernetes_config_map_v1": "configmaps",
    "kubernetes_service_v1": "services",
    "kubernetes_stateful_set_v1": "statefulsets.apps",
    "kubernetes_deployment_v1": "deployments.apps",
    "kubernetes_daemon_set_v1": "daemonsets.apps",
    "kubernetes_persistent_volume_claim_v1": "persistentvolumeclaims",
    "kubernetes_network_policy_v1": "networkpolicies.networking.k8s.io",
    "kubernetes_storage_class_v1": "storageclasses.storage.k8s.io",
    "kubernetes_role_v1": "roles.rbac.authorization.k8s.io",
    "kubernetes_role_binding_v1": "rolebindings.rbac.authorization.k8s.io",
    "kubernetes_cluster_role_v1": "clusterroles.rbac.authorization.k8s.io",
    "kubernetes_cluster_role_binding_v1": "clusterrolebindings.rbac.authorization.k8s.io",
}
MANIFEST_KINDS = {
    "Kafka": "kafkas.kafka.strimzi.io",
    "KafkaNodePool": "kafkanodepools.kafka.strimzi.io",
    "KafkaTopic": "kafkatopics.kafka.strimzi.io",
    "KafkaUser": "kafkausers.kafka.strimzi.io",
    "PodMonitor": "podmonitors.monitoring.coreos.com",
    "PrometheusRule": "prometheusrules.monitoring.coreos.com",
}
# Spec keys whose before/after values are safe to show in a plan review: resource requests,
# storage, replicas, images and network rules. Environment variables are never walked.
KEY_FIELDS = {
    "resources", "storage", "storage_class_name", "storageClassName", "replicas", "partitions",
    "image", "ingress", "egress", "policy_types", "policyTypes", "pod_selector", "podSelector",
}
RETENTION_PREFIXES = ("retention.", "log.retention.", "cleanup.policy")
# Annotation prefixes that controllers or kubectl maintain; changes there are not configuration.
CONTROLLER_ANNOTATIONS = (
    "deployment.kubernetes.io/", "kubectl.kubernetes.io/", "pv.kubernetes.io/",
    "volume.kubernetes.io/", "volume.beta.kubernetes.io/", "control-plane.alpha.kubernetes.io/",
)
# Alert names rendered by 40-monitoring for each enabled component.
POSTGRES_ALERTS = {"LocalPlatformPostgresUnavailable", "LocalPlatformPostgresCollectorIncomplete"}
KAFKA_ALERTS = {"LocalPlatformKafkaUnavailable"}
# Stable functional acceptance items reported by verify, independent of baseline checks.
ACCEPTANCE_ITEMS = {
    "gpu_smoke": "GPU Pod device injection: run gpu-smoke --execute against the current plugin",
    "gpu_workload": "Vulkan, model inference and GPU utilization inside the krunkit virtio GPU",
    "postgres_tls": "PostgreSQL verify-full connection and wrong-CA rejection using an isolated validation identity",
    "postgres_permissions": "PostgreSQL non-superuser permission checks",
    "postgres_backup_restore": "Host backup and isolated restore with data comparison",
    "kafka_mtls": "Kafka mutual TLS and metadata reachability",
    "kafka_produce_consume": "Kafka produce/consume on an isolated fixture topic",
    "kafka_acl_rejection": "Kafka forbidden-topic and consumer-group ACL rejection",
    "prometheus_targets": "Actual Prometheus target health for each enabled PodMonitor",
    "prometheus_samples": "Actual database/Kafka series in Prometheus",
    "prometheus_rules": "Prometheus loaded the platform alert rules and none are firing",
    "alert_delivery": "Isolated alert route and receiver delivery",
    "network_policy_allowed": "Authorized cross-Pod NetworkPolicy traffic, including DNS",
    "network_policy_denied": "Unauthorized cross-Pod traffic rejected by NetworkPolicy",
}


# Inputs: JSON-compatible value. Outputs: stable SHA256 string.
# Purpose: Bind reviewed inputs and reports to their exact contents. Dependencies: hashlib, json.
def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# Inputs: normalized configuration. Outputs: explicit kubectl argument prefix.
# Purpose: Avoid global context changes or ambient kubeconfig selection. Dependencies: none.
def _kube(config: dict) -> list[str]:
    cluster = config["cluster"]
    return [
        "kubectl",
        "--kubeconfig",
        cluster["kubeconfig"],
        "--context",
        cluster["context"],
        "--request-timeout=10s",
    ]


class ReadError(RuntimeError):
    """A failed read, classified as `access-denied` or `read-failed`, without the API response text."""

    # Inputs: safe message and reason token. Outputs: exception. Purpose: let checks tell a
    # permission denial from an unavailable API without printing kubectl output. Dependencies: none.
    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


# Inputs: completed kubectl process. Outputs: `access-denied` or `read-failed`.
# Purpose: classify a failed read from its status text only. Dependencies: kubectl error wording.
def _read_failure(result) -> str:
    text = (result.stderr or "").lower()
    return "access-denied" if "forbidden" in text or "unauthorized" in text else "read-failed"


# Inputs: configuration and GET arguments. Outputs: decoded JSON or None for an absent named object.
# Purpose: Read cluster objects without printing their contents. Dependencies: kubectl, io_utils.run.
def _get(
    config: dict,
    resource: str,
    name: str | None = None,
    namespace: str | None = None,
    *,
    all_namespaces: bool = False,
    selector: str | None = None,
) -> dict | None:
    command = _kube(config) + ["get", resource]
    if name is not None:
        command += [name, "--ignore-not-found=true"]
    if namespace is not None:
        command += ["--namespace", namespace]
    if all_namespaces:
        command += ["--all-namespaces"]
    if selector is not None:
        command += ["--selector", selector]
    message = f"Cannot read {resource}; check API access and permissions."
    try:
        result = run(command + ["-o", "json"], timeout=20)
    except (RuntimeError, ValueError) as error:
        raise ReadError(message, "read-failed") from error
    if result.returncode:
        raise ReadError(message, _read_failure(result))
    if not result.stdout.strip():
        return None
    try:
        item = json.loads(result.stdout)
    except ValueError as error:
        raise ReadError(message, "read-failed") from error
    if not isinstance(item, dict):
        raise ReadError(message, "read-failed")
    return item


# Inputs: configuration and one named object reference. Outputs: (state, object or None).
# Purpose: separate missing objects from denied or failed reads per check. Dependencies: _get.
def _observe(config: dict, resource: str, name: str, namespace: str | None = None):
    try:
        item = _get(config, resource, name, namespace)
    except ReadError as error:
        return error.reason, None
    return ("present" if item else "missing"), item


# Inputs: observed state, object, environment ID, and whether a Ready condition is required.
# Outputs: one of missing/access-denied/read-failed/not-owned/not-ready/ready.
# Purpose: share the per-object vocabulary of verify checks. Dependencies: ownership label convention.
def _object_state(state: str, item: dict | None, environment_id: str, ready: bool = True) -> str:
    if state != "present":
        return state
    metadata = item.get("metadata", {})
    if metadata.get("labels", {}).get(ENVIRONMENT_LABEL) != environment_id:
        return "not-owned"
    current = item.get("status", {}).get("observedGeneration", 0) >= metadata.get("generation", 1)
    if ready and not (current and _condition(item)):
        return "not-ready"
    return "ready"


# Inputs: normalized configuration. Outputs: cluster identity without authentication material.
# Purpose: Detect cluster replacement or retargeting under the same context name. Dependencies: kubectl, kubeconfig file.
def _identity(config: dict) -> dict:
    namespace = _get(config, "namespaces", "kube-system")
    uid = (namespace or {}).get("metadata", {}).get("uid")
    if not isinstance(uid, str) or not uid:
        raise RuntimeError("Cannot establish the kube-system UID for the target cluster.")
    result = run(
        _kube(config)
        + ["config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"],
        timeout=20,
    )
    server = result.stdout.strip()
    if result.returncode or not server.startswith("https://"):
        raise RuntimeError(
            "Cannot establish the HTTPS API endpoint from the selected kubeconfig context."
        )
    return {
        "context": config["cluster"]["context"],
        "kubeconfig": config["cluster"]["kubeconfig"],
        "kubeconfig_sha256": sha256(Path(config["cluster"]["kubeconfig"])),
        "server": server,
        "kube_system_uid": uid,
    }


# Inputs: Kubernetes object and optional condition name. Outputs: condition truth value.
# Purpose: Interpret controller conditions once for readiness checks. Dependencies: Kubernetes status convention.
def _condition(item: dict, name: str = "Ready") -> bool:
    return any(
        value.get("type") == name and value.get("status") == "True"
        for value in item.get("status", {}).get("conditions", [])
    )


# Inputs: report, check name, result and safe detail. Outputs: none.
# Purpose: Keep all checks machine-readable with a consistent result vocabulary. Dependencies: none.
def _check(report: dict, name: str, passed: bool, detail: object) -> None:
    report["checks"].append(
        {"name": name, "status": "pass" if passed else "blocked", "detail": detail}
    )


# Inputs: configuration, workspace, and the required report status. Outputs: a complete creation report
# for this target, or None. Purpose: require identity observed during creation before later stages;
# "deleted" reads a report whose profile this workspace deleted. Dependencies: private JSON report.
def _creation_report(config: dict, workspace: Path, status: str = "created") -> dict | None:
    try:
        report = read_json(workspace / "reports" / "cluster.json")
    except FileNotFoundError:
        return None
    cluster = config["cluster"]
    identity = report.get("identity") if isinstance(report, dict) else None
    if (
        not isinstance(identity, dict)
        or report.get("status") != status
        or report.get("profile") != cluster["name"]
        or identity.get("context") != cluster["context"]
        or identity.get("kubeconfig") != cluster["kubeconfig"]
        or not isinstance(identity.get("server"), str)
        or not identity["server"].startswith("https://")
        or not isinstance(identity.get("kube_system_uid"), str)
        or not identity["kube_system_uid"]
    ):
        return None
    return report


# Inputs: configuration, workspace, target identity, and whether this is initial creation.
# Outputs: whether the target matches the workspace. Dependencies: private report and binding files.
def _bind(
    config: dict, workspace: Path, identity: dict, *, initial_creation: bool = False
) -> bool:
    path = workspace / "cluster-binding.json"
    durable = {key: value for key, value in identity.items() if key != "kubeconfig_sha256"}
    binding = {"environment_id": config["environment_id"], "identity": durable}
    creation = None
    if config["cluster"]["mode"] == "create" and not initial_creation:
        creation = _creation_report(config, workspace)
        if creation is None:
            return False
        recorded = {
            key: value for key, value in creation["identity"].items() if key != "kubeconfig_sha256"
        }
        if recorded != durable:
            return False
    try:
        return read_json(path) == binding
    except FileNotFoundError:
        # Only legacy reports can restore a missing binding from the matching creation identity.
        if creation and creation.get("binding_established"):
            return False
    write_json(path, binding)
    return True


# Inputs: configuration, one forecast and running assigned Pods. Outputs: live memory and storage.
# Purpose: identify the stage's existing footprint independently of a successful apply report.
def _live_workload(config: dict, item: dict, pods: Sequence[dict]) -> tuple[int, int]:
    derived, stage, name = config["derived"], item["stage"], item["name"]
    env = config["environment_id"]
    label, value = "app.kubernetes.io/name", name
    if stage == "31-kafka":
        namespace = config["kafka"]["namespace"]
        label, value = (
            ("strimzi.io/pool-name", f"{name}-combined")
            if name == derived["kafka_name"] else ("strimzi.io/name", name)
        )
        owner_label, owner = "strimzi.io/cluster", derived["kafka_name"]
    else:
        owner_label, owner = ENVIRONMENT_LABEL, env
        namespace = {
            "05-storage": f"{env}-storage",
            "20-gpu": derived["gpu_namespace"],
            "30-postgres": derived["namespace"],
        }[stage]
        if stage == "20-gpu":
            value = derived["gpu_plugin_name"]
    for pod in pods:
        metadata, spec = pod["metadata"], pod["spec"]
        labels = metadata.get("labels", {})
        if (
            metadata["namespace"] != namespace
            or labels.get(label) != value
            or labels.get(owner_label) != owner
            or (stage == "30-postgres" and metadata["name"] != f"{name}-0")
            or ("nodes" in item and spec["nodeName"] not in item["nodes"])
        ):
            continue
        storage = 0
        if item["storage_mib"]:
            claim_name = (
                f"data-0-{metadata['name']}" if stage == "31-kafka" else f"{name}-data"
            )
            if any(
                (volume.get("persistentVolumeClaim") or {}).get("claimName") == claim_name
                for volume in spec.get("volumes", [])
            ):
                claim = _get(config, "persistentvolumeclaims", claim_name, namespace) or {}
                status = claim.get("status", {})
                if status.get("phase") == "Bound":
                    storage = int(quantity_mib(status.get("capacity", {}).get("storage", "0")))
        return pod_memory(pod), storage
    return 0, 0


# Inputs: configuration, workspace, optional stage and running assigned Pods. Outputs: forecasts.
# Purpose: count live demand after an unsuccessful attempt and retain replacement headroom.
def _remaining_workloads(
    config: dict, workspace: Path, stage: str | None = None, *, pods: Sequence[dict] = ()
) -> list[dict]:
    result = []
    for name in config["derived"]["stages"]:
        if stage is not None and name != stage:
            continue
        items = [item for item in config["derived"]["workloads"] if item["stage"] == name]
        requested = sum(item["memory_mib"] for item in items)
        storage = sum(item["storage_mib"] for item in items)
        path = workspace / "reports" / f"applied-{name}.json"
        previous = read_json(path) if path.exists() else {}
        if previous.get("status") in {"attempting", "uncertain", "failed"}:
            for item in items:
                memory, allocated = _live_workload(config, item, pods)
                if item["memory_mib"] > memory or item["storage_mib"] > allocated:
                    result.append({**item, "storage_mib": max(0, item["storage_mib"] - allocated)})
            continue
        prior_memory = (
            previous.get("memory_requests_mib", 0) if previous.get("status") == "applied" else 0
        )
        prior_storage = previous.get("storage_mib", 0) if previous.get("status") == "applied" else 0
        if requested <= prior_memory and storage <= prior_storage:
            continue
        result.extend(
            {**item, "storage_mib": max(0, item["storage_mib"] - prior_storage)} for item in items
        )
    return result


# Inputs: resolved config, Nodes and whether the class must already exist. Outputs: structured storage checks.
# Purpose: validate the existing CSI driver and selected topology without installing a provisioner. Dependencies: read-only Kubernetes APIs.
def _storage_checks(config: dict, nodes: list[dict], require_class: bool) -> list[tuple]:
    storage = config["storage"]
    selected = set(storage["nodes"]) or {item["metadata"]["name"] for item in nodes}
    checks = [
        (
            "storage_nodes",
            selected.issubset({item["metadata"]["name"] for item in nodes}),
            {"eligible": sorted(selected)},
        )
    ]
    if storage["mode"] == "csi":
        driver = _get(config, "csidrivers.storage.k8s.io", "hostpath.csi.k8s.io")
        registrations = _get(config, "csinodes.storage.k8s.io")["items"]
        registered = {
            item["metadata"]["name"]
            for item in registrations
            if any(
                entry["name"] == "hostpath.csi.k8s.io"
                and "topology.hostpath.csi/node" in entry.get("topologyKeys", [])
                for entry in item["spec"].get("drivers", [])
            )
        }
        labeled = {
            item["metadata"]["name"]
            for item in nodes
            if item["metadata"].get("labels", {}).get("topology.hostpath.csi/node")
            == item["metadata"]["name"]
            and item["metadata"].get("labels", {}).get("kubernetes.io/hostname")
            == item["metadata"]["name"]
        }
        daemonset = _get(config, "daemonsets.apps", "csi-hostpathplugin", "kube-system") or {}
        status = daemonset.get("status", {})
        desired = status.get("desiredNumberScheduled", 0)
        checks.append(
            (
                "csi_driver",
                bool(driver)
                and selected.issubset(registered & labeled)
                and desired > 0
                and status.get("numberReady", 0) == desired,
                {
                    "driver": "hostpath.csi.k8s.io",
                    "selected_nodes": sorted(selected),
                    "ready": status.get("numberReady", 0),
                    "desired": desired,
                },
            )
        )
    if require_class:
        item = _get(config, "storageclasses.storage.k8s.io", storage["class"])
        valid = (
            bool(item)
            and item.get("volumeBindingMode") == "WaitForFirstConsumer"
            and item.get("reclaimPolicy") == "Retain"
        )
        if storage["mode"] != "reuse":
            valid = (
                valid
                and item["metadata"].get("labels", {}).get(ENVIRONMENT_LABEL)
                == config["environment_id"]
            )
        if storage["mode"] == "csi" and item:
            allowed = [
                expression
                for term in item.get("allowedTopologies", [])
                for expression in term.get("matchLabelExpressions", [])
            ]
            valid = (
                valid
                and item.get("provisioner") == "hostpath.csi.k8s.io"
                and len(allowed) == 1
                and allowed[0].get("key") == "topology.hostpath.csi/node"
                and set(allowed[0].get("values", [])) == selected
            )
        checks.append(
            (
                "storage_class",
                bool(valid),
                {
                    "name": storage["class"],
                    "present": bool(item),
                    "binding": item.get("volumeBindingMode") if item else None,
                    "reclaim": item.get("reclaimPolicy") if item else None,
                },
            )
        )
    return checks


# Inputs: resolved configuration. Outputs: per-node advertised capacity and allocatable GPU slots.
# Purpose: show what kubelet registered for the plugin resource. Dependencies: kubectl GET nodes.
def _gpu_registration(config: dict) -> dict:
    resource = config["derived"]["gpu_resource"]
    return {
        item["metadata"]["name"]: {
            key: int(item["status"].get(key, {}).get(resource, "0"))
            for key in ("capacity", "allocatable")
        }
        for item in _get(config, "nodes")["items"]
    }


# Inputs: DaemonSet object and GPU resource name. Outputs: whether it runs a device plugin for that domain.
# Purpose: find another plugin that would register the same resource. Dependencies: generic-device-plugin flags.
def _registers_gpu(item: dict, resource: str) -> bool:
    domain = resource.split("/")[0]
    containers = item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    return any(
        "generic-device-plugin" in container.get("image", "")
        or f"--domain={domain}" in container.get("args", [])
        for container in containers
    )


# Inputs: resolved configuration and Node objects. Outputs: structured GPU preflight checks.
# Purpose: confirm selected nodes, the render device, and the absence of a foreign plugin. Dependencies: kubectl GET, minikube ssh.
def _gpu_checks(config: dict, nodes: list[dict]) -> list[tuple]:
    derived = config["derived"]
    by_name = {item["metadata"]["name"]: item for item in nodes}
    selected = config["gpu"]["nodes"]
    missing = [name for name in selected if name not in by_name]
    checks = [("gpu_nodes", not missing, {"selected": selected, "missing": missing})]
    owned = (derived["gpu_namespace"], derived["gpu_plugin_name"])
    daemonsets = _get(config, "daemonsets.apps", all_namespaces=True)["items"]
    foreign = sorted(
        f"{item['metadata']['namespace']}/{item['metadata']['name']}"
        for item in daemonsets
        if _registers_gpu(item, derived["gpu_resource"])
        and (
            (item["metadata"]["namespace"], item["metadata"]["name"]) != owned
            or item["metadata"].get("labels", {}).get(ENVIRONMENT_LABEL) != config["environment_id"]
        )
    )
    ours = any(
        (item["metadata"]["namespace"], item["metadata"]["name"]) == owned for item in daemonsets
    )
    # Capacity without our plugin means another or a removed plugin registered the resource.
    advertised = sorted(
        name
        for name, item in by_name.items()
        if int(item["status"].get("capacity", {}).get(derived["gpu_resource"], "0")) > 0
    )
    checks.append(
        (
            "gpu_plugin_owner",
            not foreign and (ours or not advertised),
            {
                "resource": derived["gpu_resource"],
                "other_plugins": foreign,
                "nodes_already_advertising": [] if ours else advertised,
            },
        )
    )
    minikube = shutil.which("minikube")
    if not minikube:
        raise RuntimeError("minikube is required to check the GPU render device on each node.")
    present = {}
    for name in selected:
        if name not in by_name:
            continue
        profile = by_name[name]["metadata"].get("labels", {}).get("minikube.k8s.io/name", "")
        # Read-only probe; ssh runs `test`, which changes nothing on the node.
        result = run(
            [minikube, "--profile", profile, "ssh", "--node", name, "--"]
            + ["test", "-c", derived["gpu_device"]],
            timeout=60,
        )
        present[name] = bool(profile) and result.returncode == 0
    checks.append(
        (
            "gpu_device",
            bool(present) and all(present.values()),
            {"device": derived["gpu_device"], "present": present},
        )
    )
    return checks


# Inputs: normalized configuration. Outputs: required in-cluster Kubernetes API /32 peers.
# Purpose: Match restrictive Operator egress against actual Service and endpoint addresses. Dependencies: Kubernetes Service/Endpoints GET.
def _api_peers(config: dict) -> set[str]:
    service = _get(config, "services", "kubernetes", "default")
    endpoints = _get(config, "endpoints", "kubernetes", "default")
    addresses = {service["spec"]["clusterIP"]}
    addresses.update(
        address["ip"]
        for subset in endpoints.get("subsets", [])
        for address in subset.get("addresses", [])
    )
    ports = {
        port["port"] for subset in endpoints.get("subsets", []) for port in subset.get("ports", [])
    }
    if not endpoints.get("subsets") or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", value)
        for value in addresses
    ):
        raise RuntimeError(
            "Actual API Service and endpoint IPv4 addresses are required for restrictive Operator egress."
        )
    if not ports or not ports.issubset({443, 8443}):
        raise RuntimeError("The Kafka profile permits only discovered API endpoint ports 443/8443.")
    return {f"{address}/32" for address in addresses}


# Inputs: resolved configuration. Outputs: pinned image keys this configuration may deploy.
# Purpose: scope architecture checks to enabled components. Dependencies: versions.json image keys.
def _required_images(config: dict) -> list[str]:
    images = []
    if config["storage"]["mode"] == "create":
        images += ["local_path", "storage_helper"]
    if config["postgres"]["enabled"]:
        images += ["postgres"] + (["postgres_exporter"] if config["monitoring"]["enabled"] else [])
    if config["kafka"]["enabled"]:
        # Strimzi is listed even when reused; a reused Operator still runs the pinned Kafka image.
        images += ["kafka", "strimzi_operator"]
    if config["monitoring"]["enabled"]:
        images += ["prometheus_operator", "prometheus_config_reloader", "prometheus"]
    if config["gpu"]["enabled"]:
        # The smoke-test Pod reuses the pinned BusyBox helper image.
        images += ["gpu_device_plugin", "storage_helper"]
    return list(dict.fromkeys(images))


# Inputs: resolved configuration and node objects. Outputs: check result and safe detail.
# Purpose: block plans whose pinned images cannot run on the nodes. Dependencies: recorded image platforms.
def _image_platforms(config: dict, nodes: list[dict]) -> tuple[bool, dict]:
    platforms = sorted(
        {
            f"{node['status']['nodeInfo']['operatingSystem']}/{node['status']['nodeInfo']['architecture']}"
            for node in nodes
        }
    )
    # Any node may host a Pod, so every pinned image must support every node platform.
    unsupported = {
        name: [item for item in platforms if item not in IMAGES[name]["platforms"]]
        for name in _required_images(config)
    }
    unsupported = {name: missing for name, missing in unsupported.items() if missing}
    return not unsupported, {"node_platforms": platforms, "unsupported_images": unsupported}


# Inputs: resolved configuration, workspace, dependency scope, optional stage or prerequisite forecasts. Outputs: read-only inventory.
# Purpose: Measure the actual target before generating or executing plans. Dependencies: kubectl GET, kubelet summary access.
def inspect(
    config: dict,
    workspace: Path,
    *,
    check_dependencies: bool = True,
    stage: str | None = None,
    workloads: list[dict] | None = None,
) -> dict:
    report = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checks": [],
        "status": "blocked",
        "scope": stage or ("platform" if workloads is None else "prerequisite-foundation"),
    }
    try:
        report["identity"] = _identity(config)
        bound = _bind(config, workspace, report["identity"])
        _check(
            report,
            "workspace_target",
            bound,
            "The target must match the workspace binding.",
        )
        if not bound:
            write_json(workspace / "reports" / "inventory.json", report)
            return report
        version = run(_kube(config) + ["get", "--raw", "/version"], timeout=20)
        git_version = (
            json.loads(version.stdout).get("gitVersion", "") if version.returncode == 0 else ""
        )
        _check(
            report,
            "kubernetes_profile",
            bool(re.match(r"^v1\.(35|36|37)\.", git_version)),
            {
                "version": git_version,
                "supported_profile": "Kubernetes 1.35-1.37; runtime acceptance remains separate",
            },
        )
        namespaces = _get(config, "namespaces")["items"]
        conflicts = [
            item["metadata"]["name"]
            for item in namespaces
            if item["metadata"].get("labels", {}).get(ENVIRONMENT_LABEL)
            not in (None, config["environment_id"])
        ]
        _check(
            report, "single_environment", not conflicts, {"other_environment_namespaces": conflicts}
        )
        nodes = _get(config, "nodes")["items"]
        _check(
            report,
            "minikube_nodes",
            bool(nodes)
            and all("minikube.k8s.io/name" in item["metadata"].get("labels", {}) for item in nodes),
            "Only existing Minikube nodes are supported.",
        )
        _check(
            report,
            "nodes_ready",
            bool(nodes) and all(_condition(item) for item in nodes),
            {"nodes": [item["metadata"]["name"] for item in nodes]},
        )
        _check(report, "image_platforms", *_image_platforms(config, nodes))
        measurements = []
        for node in nodes:
            name = node["metadata"]["name"]
            response = run(
                _kube(config) + ["get", "--raw", f"/api/v1/nodes/{name}/proxy/stats/summary"],
                timeout=20,
            )
            if response.returncode:
                raise RuntimeError("Measured node capacity is unavailable; planning is blocked.")
            summary = json.loads(response.stdout)["node"]
            measurements.append(
                {
                    "name": name,
                    "memory_available_mib": int(summary["memory"]["availableBytes"]) // 1048576,
                    "disk_available_mib": int(summary["fs"]["availableBytes"]) // 1048576,
                }
            )
        pods = _get(config, "pods", all_namespaces=True)["items"]
        node_names = {item["metadata"]["name"] for item in nodes}
        running = [
            pod for pod in pods
            if pod.get("status", {}).get("phase") == "Running"
            and pod.get("spec", {}).get("nodeName") in node_names
            and not pod["metadata"].get("deletionTimestamp")
        ]
        forecasts = (
            _remaining_workloads(config, workspace, stage, pods=running)
            if workloads is None else workloads
        )
        report["capacity"] = assess(
            nodes,
            pods,
            measurements,
            forecasts,
            config["storage"]["nodes"],
            config["budget"]["min_disk_free_mib"],
        )
        for name, passed in report["capacity"]["checks"].items():
            _check(
                report,
                name,
                passed,
                {
                    "memory_requests_mib": report["capacity"]["new_memory_requests_mib"],
                    "storage_mib": report["capacity"]["new_storage_mib"],
                    "disk_reserve_mib": config["budget"]["min_disk_free_mib"],
                    "placement": "Pod memory includes assigned requests and measured availability; each volume uses one eligible node.",
                },
            )
        data_services = config["postgres"]["enabled"] or config["kafka"]["enabled"]
        if (
            workloads is None
            and data_services
            and (stage is None or stage in {"05-storage-csi", "30-postgres", "31-kafka"})
        ):
            require_class = config["storage"]["mode"] == "reuse" or stage in {
                "30-postgres",
                "31-kafka",
            }
            for name, passed, detail in _storage_checks(config, nodes, require_class):
                _check(report, name, passed, detail)
        if workloads is None and config["gpu"]["enabled"] and stage in {None, "20-gpu"}:
            for name, passed, detail in _gpu_checks(config, nodes):
                _check(report, name, passed, detail)
        if check_dependencies:
            from prerequisites import installed

            for component, enabled in (
                ("strimzi", config["kafka"]["enabled"]),
                ("monitoring", config["monitoring"]["enabled"]),
            ):
                if enabled and (
                    stage is None
                    or stage == "40-monitoring"
                    or component == "strimzi"
                    and stage == "31-kafka"
                ):
                    installed(config, component)
                    _check(
                        report,
                        f"{component}_installation",
                        True,
                        "Compatible installation is ready.",
                    )
        if (
            check_dependencies
            and config["kafka"]["enabled"]
            and (stage is None or stage in {"31-kafka", "40-monitoring"})
        ):
            required_peers = _api_peers(config)
            _check(
                report,
                "api_server_egress",
                required_peers.issubset(set(config["network"]["api_server_cidrs"])),
                {
                    "required_cidrs": sorted(required_peers),
                    "configured_cidrs": config["network"]["api_server_cidrs"],
                },
            )
        selector = ",".join(
            f"{key}={value}" for key, value in sorted(config["network"]["dns_labels"].items())
        )
        dns = run(
            _kube(config)
            + [
                "get",
                "pods",
                "--namespace",
                config["network"]["dns_namespace"],
                "--selector",
                selector,
                "-o",
                "json",
            ],
            timeout=20,
        )
        dns_items = json.loads(dns.stdout).get("items", []) if dns.returncode == 0 else []
        _check(
            report,
            "dns_selector",
            bool(dns_items) and any(_condition(item) for item in dns_items),
            "Configured DNS labels must select Ready DNS Pods.",
        )
        calico = _get(config, "daemonsets.apps", "calico-node", "kube-system")
        desired = calico.get("status", {}).get("desiredNumberScheduled", 0) if calico else 0
        _check(
            report,
            "network_policy_controller",
            desired > 0 and calico.get("status", {}).get("numberReady", 0) == desired,
            "First version requires the existing Calico controller on every scheduled node; traffic acceptance remains separate.",
        )
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as error:
        detail = (
            str(error)
            if isinstance(error, RuntimeError)
            else "Inventory response is incomplete or unreadable."
        )
        _check(report, "inventory_access", False, detail)
    report["status"] = (
        "pass"
        if report["checks"] and all(item["status"] == "pass" for item in report["checks"])
        else "blocked"
    )
    write_json(workspace / "reports" / "inventory.json", report)
    return report


# Inputs: normalized configuration, selected stage. Outputs: required earlier stages.
# Purpose: Keep optional-component ordering explicit without a generic workflow framework. Dependencies: enabled stage list.
def _dependencies(config: dict, stage: str) -> list[str]:
    if stage in ("00-bootstrap", "05-storage", "05-storage-csi"):
        return []
    required = ["00-bootstrap"]
    if stage in ("30-postgres", "31-kafka") and config["derived"]["storage_stage"]:
        required.append(config["derived"]["storage_stage"])
    if stage == "40-monitoring":
        required += [
            name for name in ("30-postgres", "31-kafka") if name in config["derived"]["stages"]
        ]
    return required


# Inputs: normalized configuration and stage. Outputs: readiness result plus safe detail.
# Purpose: Check required live resources before dependent plans. Dependencies: kubectl GET.
def _stage_ready(config: dict, stage: str) -> tuple[bool, str]:
    derived = config["derived"]
    if stage == "00-bootstrap":
        names = (
            derived["namespace"],
            derived["validation_namespace"],
            config["application"]["namespace"],
        )
        objects = [_get(config, "namespaces", name) for name in names]
        return all(
            item
            and item["metadata"].get("labels", {}).get(ENVIRONMENT_LABEL)
            == config["environment_id"]
            for item in objects
        ), "Owned application, data and validation namespaces must exist."
    if stage in {"05-storage", "05-storage-csi"}:
        item = _get(config, "storageclasses.storage.k8s.io", config["storage"]["class"])
        return bool(item) and item.get("metadata", {}).get("labels", {}).get(
            ENVIRONMENT_LABEL
        ) == config["environment_id"] and item.get(
            "volumeBindingMode"
        ) == "WaitForFirstConsumer" and item.get(
            "reclaimPolicy"
        ) == "Retain", "Owned WFFC/Retain StorageClass must be ready."
    if stage == "20-gpu":
        item = _get(config, "daemonsets.apps", derived["gpu_plugin_name"], derived["gpu_namespace"])
        status = (item or {}).get("status", {})
        nodes = config["gpu"]["nodes"]
        rolled_out = (
            bool(item)
            and item["metadata"].get("labels", {}).get(ENVIRONMENT_LABEL)
            == config["environment_id"]
            and status.get("observedGeneration", 0) >= item["metadata"].get("generation", 1)
            and status.get("desiredNumberScheduled") == len(nodes)
            and status.get("numberReady") == len(nodes)
            and status.get("updatedNumberScheduled") == len(nodes)
        )
        registration = _gpu_registration(config)
        expected = {"capacity": config["gpu"]["slots"], "allocatable": config["gpu"]["slots"]}
        registered = all(registration.get(name) == expected for name in nodes)
        # Allocatable is total schedulable capacity; it does not drop while Pods hold slots.
        return rolled_out and registered, {
            "summary": "Owned plugin DaemonSet must be Ready on every selected node, and kubelet must advertise the configured slots.",
            "resource": derived["gpu_resource"],
            "expected_slots": config["gpu"]["slots"],
            "registration": {name: registration.get(name) for name in nodes},
        }
    if stage == "30-postgres":
        item = _get(config, "statefulsets.apps", derived["postgres_name"], derived["namespace"])
        ready = bool(item) and item.get("status", {}).get("readyReplicas", 0) == 1
        ready = ready and item.get("status", {}).get("observedGeneration", 0) >= item.get(
            "metadata", {}
        ).get("generation", 1)
        ready = ready and item["metadata"].get("labels", {}).get(
            ENVIRONMENT_LABEL
        ) == config["environment_id"]
        return ready, "Owned PostgreSQL StatefulSet needs one Ready replica at its current generation."
    if stage == "31-kafka":
        item = _get(
            config, "kafkas.kafka.strimzi.io", derived["kafka_name"], config["kafka"]["namespace"]
        )
        state = _object_state("present" if item else "missing", item, config["environment_id"])
        return state == "ready", "Owned Kafka must report Ready at its current generation."
    return True, "No further controller dependency is required."


# Inputs: normalized configuration, workspace and stage. Outputs: none or a precise blocked exception.
# Purpose: Prevent planning stages whose prior resources or PostgreSQL Secret references are absent. Dependencies: applied records, kubectl.
def _prerequisites(config: dict, workspace: Path, stage: str) -> None:
    for dependency in _dependencies(config, stage):
        record = workspace / "reports" / f"applied-{dependency}.json"
        if not record.exists() or read_json(record).get("status") != "applied":
            raise RuntimeError(f"Stage {dependency} must be applied before planning {stage}.")
        ready, detail = _stage_ready(config, dependency)
        if not ready:
            raise RuntimeError(detail)
    if stage == "30-postgres":
        for suffix in ("admin", "app", "metrics", "tls"):
            name = f"{config['derived']['postgres_name']}-{suffix}"
            result = run(
                _kube(config)
                + [
                    "get",
                    "secret",
                    name,
                    "--namespace",
                    config["derived"]["namespace"],
                    "--ignore-not-found=true",
                    "-o",
                    "jsonpath={.metadata.uid}",
                ],
                timeout=20,
            )
            if result.returncode or not result.stdout.strip():
                raise RuntimeError(
                    f"PostgreSQL Secret reference {name} is not ready; prepare credentials outside Terraform."
                )


# Inputs: selected materialized stage directory. Outputs: source and runtime hashes.
# Purpose: Detect modifications after plan review while excluding Terraform runtime files. Dependencies: immutable packaged scripts/assets.
def _source_hashes(stage_directory: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(stage_directory.rglob("*")):
        relative = path.relative_to(stage_directory)
        if ".terraform" in relative.parts:
            continue
        if path.is_symlink():
            raise RuntimeError("Stage sources must not contain symbolic links.")
        if path.is_file():
            hashes[f"stage/{relative.as_posix()}"] = sha256(path)
    for path in sorted((ROOT / "scripts").glob("*.py")):
        hashes[f"scripts/{path.name}"] = sha256(path)
    return hashes


# Inputs: workspace and stage. Outputs: state file hash or None.
# Purpose: Bind plans to the exact pre-apply local state. Dependencies: private Terraform state path.
def _state_hash(workspace: Path, stage: str) -> str | None:
    path = workspace / "state" / f"{stage}.tfstate"
    if path.is_symlink():
        raise RuntimeError("Terraform state must not be a symbolic link.")
    return sha256(path) if path.exists() else None


# Inputs: Terraform state attribute value. Outputs: the plain value.
# Purpose: Read dynamic attributes, which local state stores as {"value", "type"}. Dependencies: none.
def _state_value(value: object) -> object:
    if isinstance(value, dict) and set(value) == {"value", "type"}:
        return value["value"]
    return value


# Inputs: private workspace and optional stage. Outputs: recorded Kubernetes identities.
# Purpose: Let cluster reconciliation compare live objects with Terraform state. Dependencies: local backend JSON.
def _state_resources(workspace: Path, stage: str | None = None) -> list[dict]:
    result = []
    directory = workspace / "state"
    if not directory.exists():
        return result
    for path in sorted(directory.glob("*.tfstate")):
        if stage is not None and path.stem != stage:
            continue
        if path.is_symlink():
            raise RuntimeError("Terraform state must not be a symbolic link.")
        for item in read_json(path).get("resources", []):
            if item.get("mode", "managed") != "managed":
                continue
            for instance in item.get("instances", []):
                attributes = instance.get("attributes") or {}
                if item["type"] == "kubernetes_manifest":
                    values = {"manifest": _state_value(attributes.get("manifest")) or {}}
                    live = _state_value(attributes.get("object")) or {}
                    uid = live.get("metadata", {}).get("uid")
                else:
                    values, uid = attributes, None
                identity = _resource_identity(item["type"], values)
                result.append(
                    {
                        "source": f"terraform:{path.stem}",
                        **{key: identity[key] for key in ("resource", "name", "namespace")},
                        "uid": identity["uid"] or uid,
                    }
                )
    return result


# Inputs: operation report path. Outputs: successfully created or published object references.
# Purpose: retain evidence from earlier releases and partial operations. Dependencies: private reports.
def _reported_objects(path: Path) -> list[dict]:
    try:
        report = read_json(path)
    except FileNotFoundError:
        return []
    references = []
    for item in report.get("results", []):
        if path.stem.startswith("credentials-"):
            if item["status"] in {"created", "unchanged"}:
                namespace, name = item["reference"].split("/", 1)
                references.append({
                    "source": path.stem, "resource": "secrets", "namespace": namespace,
                    "name": name, "uid": None,
                })
        elif item.get("status") == "created" and item.get("reference"):
            references.append({"source": "prerequisites", **item["reference"], "uid": None})
    return references


# Inputs: workspace and references. Outputs: none. Dependencies: private JSON and the workspace lock.
# Purpose: persist positive object evidence independently of the latest operation report.
def _remember_objects(workspace: Path, references: list[dict]) -> None:
    if not references:
        return
    path = workspace / "state" / "recorded-objects.json"
    try:
        saved = read_json(path)
    except FileNotFoundError:
        saved = []
    combined = saved + [item for item in references if item not in saved]
    if combined != saved:
        write_json(path, combined)


# Inputs: workspace. Outputs: unique references for non-Terraform objects.
# Purpose: reconcile durable and legacy evidence. Dependencies: object inventory and private reports.
def _recorded_objects(workspace: Path) -> list[dict]:
    try:
        objects = read_json(workspace / "state" / "recorded-objects.json")
    except FileNotFoundError:
        objects = []
    for path in (
        *sorted((workspace / "reports").glob("prerequisites-*.json")),
        *sorted((workspace / "reports").glob("credentials-*.json")),
        *sorted((workspace / "plans" / "prerequisites").glob("consumed-*.json")),
    ):
        objects.extend(_reported_objects(path))
    unique = {}
    for item in objects:
        key = (item["resource"], item["namespace"] or "", item["name"])
        unique.setdefault(key, item)
    return [unique[key] for key in sorted(unique)]


# Inputs: Terraform change values and resource type. Outputs: Kubernetes resource identity and labels.
# Purpose: Translate allowed plan resources for ownership and collision checks. Dependencies: Terraform Kubernetes provider shapes.
def _resource_identity(resource_type: str, values: dict) -> dict:
    if resource_type == "kubernetes_manifest":
        manifest = values.get("manifest", {})
        kind = manifest.get("kind")
        if kind not in MANIFEST_KINDS:
            raise RuntimeError("Plan contains an unsupported Kubernetes manifest kind.")
        resource = MANIFEST_KINDS[kind]
        metadata = manifest.get("metadata", {})
    else:
        if resource_type not in RESOURCE_TYPES:
            raise RuntimeError(
                "Plan contains an unsupported resource type; Secret data is never managed by Terraform."
            )
        resource = RESOURCE_TYPES[resource_type]
        metadata = values.get("metadata", [])
        metadata = metadata[0] if isinstance(metadata, list) and metadata else metadata
    if not isinstance(metadata, dict) or not metadata.get("name"):
        raise RuntimeError(
            "Plan resource identity is unknown; explicit names are required before approval."
        )
    return {
        "resource": resource,
        "name": metadata["name"],
        "namespace": metadata.get("namespace") or None,
        "labels": metadata.get("labels", {}),
        "uid": metadata.get("uid"),
    }


# Inputs: Terraform resource type and a before/after change. Outputs: none or a migration blocker.
# Purpose: preserve existing volumes, partitions and retained messages when defaults become smaller. Dependencies: provider plan shapes.
def _protect_data(resource_type: str, change: dict) -> None:
    before, after = change.get("before"), change.get("after")
    if not before or not after:
        return
    if resource_type == "kubernetes_persistent_volume_claim_v1":
        old = before["spec"][0]["resources"][0]["requests"]["storage"]
        new = after["spec"][0]["resources"][0]["requests"]["storage"]
        if memory_mib(new) < memory_mib(old):
            raise RuntimeError("PVC shrinking requires a separate data migration.")
    if resource_type == "kubernetes_manifest":
        old, new = before.get("object") or before["manifest"], after["manifest"]
        if old["kind"] == "KafkaNodePool":
            previous = {item["id"]: item["size"] for item in old["spec"]["storage"]["volumes"]}
            current = {item["id"]: item["size"] for item in new["spec"]["storage"]["volumes"]}
            if any(
                key not in current or memory_mib(current[key]) < memory_mib(size)
                for key, size in previous.items()
            ):
                raise RuntimeError(
                    "Kafka volume removal or shrinking requires a separate data migration."
                )
        if old["kind"] == "KafkaTopic":
            if new["spec"]["partitions"] < old["spec"]["partitions"]:
                raise RuntimeError("Existing Kafka partitions cannot be reduced by this workflow.")
            for key in ("retention.ms", "retention.bytes"):
                previous = (old["spec"].get("config") or {}).get(key)
                current = (new["spec"].get("config") or {}).get(key)
                if (
                    previous is not None
                    and current is not None
                    and int(current) >= 0
                    and (int(previous) < 0 or int(current) < int(previous))
                ):
                    raise RuntimeError(
                        "Reducing existing Kafka retention requires a separate data migration."
                    )


# Inputs: Terraform marker and path. Outputs: the mask for a selected subtree.
# Purpose: preserve ancestor sensitivity and carry descendant markers into rendering.
def _sensitivity_at(marker: object, path: tuple) -> object:
    for key in path:
        if marker is True:
            return True
        if isinstance(marker, dict):
            marker = marker.get(key)
        elif isinstance(marker, list) and isinstance(key, int) and key < len(marker):
            marker = marker[key]
        else:
            return None
    return marker


# Inputs: selected review value and its mask. Outputs: safe value with the same composite shape.
# Purpose: redact sensitive descendants of fields allowed in plan reviews.
def _redact(value: object, marker: object) -> object:
    if marker is True:
        return "(sensitive)"
    if isinstance(value, dict):
        return {
            key: _redact(item, _sensitivity_at(marker, (key,)))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, _sensitivity_at(marker, (index,))) for index, item in enumerate(value)]
    return value


# Inputs: Terraform type, values, sensitivity mask and whether to use the refreshed API object.
# Outputs: {path: value}.
# Purpose: extract only reviewable key fields from the desired spec. Dependencies: KEY_FIELDS.
def _key_fields(
    resource_type: str, values: dict | None, marker: object = None, *, refreshed: bool = False
) -> dict:
    if not values:
        return {}
    if resource_type == "kubernetes_manifest":
        # The manifest attribute retains prior configuration. Refresh updates object.
        attribute = "object" if refreshed and values.get("object") else "manifest"
        root, prefix = (values.get(attribute) or {}).get("spec"), (attribute, "spec")
    else:
        root, prefix = values.get("spec"), ("spec",)
    fields = {}

    # Inputs: spec subtree and path. Outputs: selected, redacted entries in fields.
    # Purpose: traverse the provider's nested blocks and lists.
    def walk(value, path):
        items = value.items() if isinstance(value, dict) else (
            enumerate(value) if isinstance(value, list) else ()
        )
        for key, item in items:
            here = path + (key,)
            if key == "env":
                continue
            if isinstance(key, str) and (
                key in KEY_FIELDS or (path and path[-1] == "config" and key.startswith(RETENTION_PREFIXES))
            ):
                label = ".".join(str(part) for part in here[len(prefix) - 1:])
                value = _redact(item, _sensitivity_at(marker, here))
                if here[0] == "object":
                    value = _redact(value, _sensitivity_at(marker, ("manifest", *here[1:])))
                fields[label] = value
            else:
                walk(item, here)

    walk(root, prefix)
    return fields


# Inputs: Terraform resource type and change. Outputs: {path: {before?, after}} for key fields.
# Purpose: show reviewers what a create sets and what an update changes. Dependencies: _key_fields.
def _field_changes(resource_type: str, change: dict) -> dict:
    after = _key_fields(resource_type, change.get("after"), change.get("after_sensitive"))
    if change["actions"] == ["create"]:
        return {path: {"after": value} for path, value in sorted(after.items())}
    before = _key_fields(
        resource_type, change.get("before"), change.get("before_sensitive"), refreshed=True
    )
    return {
        path: {"before": before.get(path), "after": after.get(path)}
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    }


# Inputs: live Kubernetes object. Outputs: configuration without status or controller noise.
# Purpose: compare what an update depends on, not what controllers report. Dependencies: none.
def _live_config(item: dict) -> dict:
    metadata = item.get("metadata", {})
    config = {
        key: deepcopy(value)
        for key, value in item.items()
        if key not in {"apiVersion", "kind", "metadata", "status"}
    }
    config["metadata"] = {
        "labels": metadata.get("labels", {}),
        "annotations": {
            key: value
            for key, value in metadata.get("annotations", {}).items()
            if not key.startswith(CONTROLLER_ANNOTATIONS)
        },
    }
    if item.get("kind") == "PersistentVolumeClaim":
        # Binding fills volumeName once; the claim's requested configuration is unchanged.
        config.get("spec", {}).pop("volumeName", None)
    return config


# Inputs: JSON value and path prefix. Outputs: (path, leaf) pairs.
# Purpose: let fingerprints name changed paths without storing values. Dependencies: none.
def _leaves(value: object, path: str = ""):
    if isinstance(value, dict) and value:
        for key in sorted(value):
            yield from _leaves(value[key], f"{path}.{key}" if path else str(key))
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from _leaves(item, f"{path}[{index}]")
    else:
        yield path, value


# Inputs: live Kubernetes object. Outputs: {path: short digest}. Purpose: record the live
# configuration an update was reviewed against without copying values. Dependencies: _live_config.
def _live_fingerprint(item: dict) -> dict:
    return {path: _digest(value)[:16] for path, value in _leaves(_live_config(item))}


# Inputs: reviewed and current fingerprints. Outputs: sorted changed paths.
# Purpose: explain why a plan became stale. Dependencies: none.
def _changed_paths(reviewed: dict, current: dict) -> list[str]:
    return sorted(path for path in set(reviewed) | set(current) if reviewed.get(path) != current.get(path))


# Inputs: change summaries from a review. Outputs: summaries without fingerprints.
# Purpose: keep CLI output readable while the review keeps the binding. Dependencies: none.
def _public_changes(changes: list[dict]) -> list[dict]:
    return [
        {
            **{key: value for key, value in item.items() if key != "live_fingerprint"},
            **({"live_config_recorded": True} if "live_fingerprint" in item else {}),
        }
        for item in changes
    ]


class PlanInvalid(RuntimeError):
    """A definitive mismatch with the reviewed plan; transient read failures never raise this."""


# Inputs: configuration, Terraform plan and pre-plan versions. Outputs: safe resource-action summary.
# Purpose: Reject deletion, import, unsupported resources and collisions with resources outside this state;
# record key field values and, for updates, the live configuration fingerprint. Dependencies: kubectl GET.
def _review_changes(config: dict, document: dict, versions: dict | None = None) -> list[dict]:
    changes = []
    for item in document.get("resource_changes", []):
        if item.get("mode", "managed") != "managed":
            raise RuntimeError("Terraform data sources are not permitted in platform stage plans.")
        change = item["change"]
        actions = change["actions"]
        if "delete" in actions or change.get("importing"):
            raise RuntimeError("Destructive or importing plans are not supported by this skill.")
        _protect_data(item["type"], change)
        identity = _resource_identity(
            item["type"], change.get("after") or change.get("before") or {}
        )
        if identity["labels"].get(ENVIRONMENT_LABEL) != config["environment_id"]:
            raise RuntimeError(
                "Every managed resource must carry the current environment ownership label."
            )
        if actions == ["create"]:
            existing = _get(config, identity["resource"], identity["name"], identity["namespace"])
            if existing is not None:
                raise RuntimeError(
                    f"Creation collides with existing {identity['resource']}/{identity['name']}; automatic adoption is disabled."
                )
        else:
            before = _resource_identity(item["type"], change.get("before") or {})
            if before["labels"].get(ENVIRONMENT_LABEL) != config["environment_id"]:
                raise RuntimeError(
                    "A planned update would claim a resource without matching ownership."
                )
            live = _get(config, identity["resource"], identity["name"], identity["namespace"])
            metadata = live.get("metadata", {}) if live else {}
            if (
                metadata.get("labels", {}).get(ENVIRONMENT_LABEL) != config["environment_id"]
                or before["uid"]
                and metadata.get("uid") != before["uid"]
            ):
                raise RuntimeError(
                    "Live resource ownership or UID no longer matches the planned state."
                )
        summary = {
            "address": item["address"],
            "actions": actions,
            **{key: identity[key] for key in ("resource", "name", "namespace")},
            "uid": before["uid"] if actions != ["create"] else None,
            "fields": _field_changes(item["type"], change),
        }
        if "update" in actions:
            key = (identity["resource"], identity["name"], identity["namespace"])
            version = metadata.get("uid"), metadata.get("resourceVersion")
            if not all(version) or (versions or {}).get(key) != version:
                raise RuntimeError(
                    f"Live resource {identity['resource']}/{identity['name']} changed during planning "
                    "or has no confirmed planning version. Generate a new plan."
                )
            # The same API version bracketed Terraform refresh. Apply can ignore later status noise.
            summary["live_uid"] = metadata.get("uid")
            summary["live_fingerprint"] = _live_fingerprint(live)
        changes.append(summary)
    return changes


# Inputs: configuration, workspace and stage. Outputs: resource versions before Terraform refresh.
# Purpose: bracket planning with consistent GETs without comparing provider-shaped values to API JSON.
def _planning_versions(config: dict, workspace: Path, stage: str) -> dict:
    versions = {}
    for ref in _state_resources(workspace, stage):
        key = (ref["resource"], ref["name"], ref["namespace"])
        if key not in versions:
            item = _get(config, *key) or {}
            metadata = item.get("metadata", {})
            versions[key] = metadata.get("uid"), metadata.get("resourceVersion")
    return versions


# Inputs: command, stage directory and private log path. Outputs: completed Terraform process.
# Purpose: Save diagnostics privately while keeping raw provider output out of the Agent response. Dependencies: Terraform, private JSON IO.
def _terraform(command: list[str], stage_directory: Path, log: Path):
    try:
        result = run(["terraform", f"-chdir={stage_directory}"] + command, timeout=600)
    except RuntimeError as error:
        write_json(log, {"returncode": None, "error": str(error)})
        raise
    except KeyboardInterrupt:
        # subprocess.run terminates the child; its output and exit status are not available.
        write_json(log, {"returncode": None, "error": "Interrupted; Terraform was terminated."})
        raise
    write_json(
        log, {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    )
    return result


# Inputs: workspace and selected stage. Outputs: new private plan directory.
# Purpose: Quarantine superseded plans before any fresh attempt can fail. Dependencies: same-filesystem rename.
def _fresh_plan_directory(workspace: Path, stage: str) -> Path:
    directory = workspace / "plans" / stage
    private_dir(workspace / "plans")
    if directory.is_symlink():
        raise RuntimeError("Plan directories must not be symbolic links.")
    if directory.exists():
        archive = private_dir(workspace / "plans" / "archive")
        directory.rename(archive / f"{stage}-{time.time_ns()}")
    return private_dir(directory)


# Inputs: normalized configuration, private workspace, stage and optional offline provider mirror.
# Outputs: a saved, hash-bound plan review. Purpose: Produce one concrete stage for review. Dependencies: Terraform, kubectl.
def plan(config: dict, workspace: Path, stage: str, plugin_dir: Path | None = None) -> dict:
    directory = _fresh_plan_directory(workspace, stage)
    try:
        inventory = inspect(config, workspace, stage=stage)
        if inventory["status"] != "pass":
            raise RuntimeError(
                "Preflight is blocked; inspect the inventory report before planning."
            )
        _prerequisites(config, workspace, stage)
        stage_directory = workspace / "stages" / stage
        state = private_dir(workspace / "state") / f"{stage}.tfstate"
        init = [
            "init",
            "-input=false",
            "-no-color",
            "-lockfile=readonly",
            f"-backend-config=path={state}",
        ]
        if plugin_dir is not None:
            init.append(f"-plugin-dir={plugin_dir}")
        result = _terraform(init, stage_directory, directory / "init-log.json")
        if result.returncode:
            raise RuntimeError(
                "Terraform initialization failed; diagnostics are in the private plan directory."
            )
        prior_state = _state_hash(workspace, stage)
        versions = _planning_versions(config, workspace, stage)
        result = _terraform(
            [
                "plan",
                "-input=false",
                "-no-color",
                "-refresh=true",
                "-lock-timeout=20s",
                "-detailed-exitcode",
                f"-out={directory / 'plan.tfplan'}",
            ],
            stage_directory,
            directory / "plan-log.json",
        )
        if result.returncode not in (0, 2):
            raise RuntimeError(
                "Terraform planning failed; diagnostics are in the private plan directory."
            )
        saved = directory / "plan.tfplan"
        if not saved.is_file() or saved.is_symlink():
            raise RuntimeError("Terraform did not produce a regular saved plan.")
        saved.chmod(0o600)
        result = run(
            ["terraform", f"-chdir={stage_directory}", "show", "-json", str(saved)], timeout=60
        )
        if result.returncode:
            raise RuntimeError("Terraform could not decode the saved plan for review.")
        changes = _review_changes(config, json.loads(result.stdout), versions)
        if _state_hash(workspace, stage) != prior_state:
            raise RuntimeError("Terraform state changed during planning; generate a new plan.")
        review = {
            "stage": stage,
            "environment_id": config["environment_id"],
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "config_sha256": _digest(config),
            "identity": inventory["identity"],
            "source_hashes": _source_hashes(stage_directory),
            "prior_state_sha256": prior_state,
            "plan_sha256": sha256(saved),
            "changes": changes,
        }
        review["plan_id"] = _digest(review)
        write_json(directory / "review.json", review)
        return {
            "status": "planned",
            "stage": stage,
            "plan_id": review["plan_id"],
            "changes": _public_changes(changes),
            "review": str(directory / "review.json"),
        }
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        saved = directory / "plan.tfplan"
        if saved.exists() and not saved.is_symlink():
            saved.chmod(0o600)
        write_json(directory / "failed.json", {"stage": stage, "status": "failed", "usable": False})
        raise


# Inputs: normalized configuration, workspace, stage and exact reviewed plan ID.
# Outputs: saved review after all local bindings match. Purpose: Reject stale or altered artifacts. Dependencies: private plan/source/state files.
def _matching_plan(config: dict, workspace: Path, stage: str, plan_id: str) -> dict:
    directory = workspace / "plans" / stage
    review_path, saved = directory / "review.json", directory / "plan.tfplan"
    invalidated = directory / "invalidated.json"
    if invalidated.is_file() and not invalidated.is_symlink() and not directory.is_symlink():
        reason = read_json(invalidated).get("reason", "unknown")
        raise RuntimeError(f"Plan was invalidated: {reason} Generate a new plan.")
    if (
        directory.is_symlink()
        or review_path.is_symlink()
        or saved.is_symlink()
        or (directory / "failed.json").exists()
        or (directory / "consumed.json").exists()
    ):
        raise RuntimeError("Plan is failed, consumed or unsafe; generate a new plan.")
    if not review_path.is_file() or not saved.is_file():
        raise RuntimeError("A complete current saved plan is required.")
    review = read_json(review_path)
    recorded_id = review.pop("plan_id", None)
    if recorded_id != plan_id or _digest(review) != plan_id:
        raise RuntimeError("Plan ID does not match the current saved review.")
    expected = {
        "stage": stage,
        "environment_id": config["environment_id"],
        "config_sha256": _digest(config),
        "source_hashes": _source_hashes(workspace / "stages" / stage),
        "prior_state_sha256": _state_hash(workspace, stage),
        "plan_sha256": sha256(saved),
    }
    if any(review.get(key) != value for key, value in expected.items()):
        raise RuntimeError(
            "Plan inputs, sources, state or saved bytes changed; generate a new plan."
        )
    review["plan_id"] = recorded_id
    return review


# Inputs: normalized configuration, reviewed changes. Outputs: none or PlanInvalid.
# Purpose: re-read ownership, UID and live configuration immediately before dispatch;
# reviewed metadata is not a live lock. Dependencies: kubectl GET.
def _recheck_live(config: dict, changes: list[dict]) -> None:
    for change in changes:
        label = f"{change['resource']}/{change['name']}"
        live = _get(config, change["resource"], change["name"], change["namespace"])
        if change["actions"] == ["create"]:
            if live is not None:
                raise PlanInvalid(f"Planned creation {label} now exists; automatic adoption is disabled.")
            continue
        metadata = live.get("metadata", {}) if live else {}
        expected_uid = change.get("live_uid") or change.get("uid")
        if (
            metadata.get("labels", {}).get(ENVIRONMENT_LABEL) != config["environment_id"]
            or expected_uid
            and metadata.get("uid") != expected_uid
        ):
            raise PlanInvalid(f"Live ownership or UID of {label} changed after plan review.")
        if "live_fingerprint" in change:
            paths = _changed_paths(change["live_fingerprint"], _live_fingerprint(live))
            if paths:
                shown = ", ".join(paths[:10]) + (f" and {len(paths) - 10} more" if len(paths) > 10 else "")
                raise PlanInvalid(f"Live configuration of {label} changed after plan review: {shown}.")


# Inputs: normalized configuration, workspace, stage, exact plan ID and explicit execution flag.
# Outputs: read-only preview or application record. Purpose: Apply exactly one approved saved plan. Dependencies: Terraform, kubectl.
def apply(config: dict, workspace: Path, stage: str, plan_id: str, execute: bool = False) -> dict:
    review = _matching_plan(config, workspace, stage, plan_id)
    if not execute:
        return {
            "status": "preview",
            "stage": stage,
            "plan_id": plan_id,
            "changes": _public_changes(review["changes"]),
            "requires_execute": True,
        }
    directory = workspace / "plans" / stage
    inventory = inspect(config, workspace, stage=stage)
    try:
        current = inventory.get("identity") or {}
        if current and current.get("kube_system_uid") != review["identity"]["kube_system_uid"]:
            raise PlanInvalid("Target cluster identity changed after plan review.")
        # Other differences (for example a refreshed kubeconfig) block without invalidating.
        if inventory["status"] != "pass" or current != review["identity"]:
            raise RuntimeError("Target identity or prerequisites changed; execution is blocked.")
        _prerequisites(config, workspace, stage)
        _recheck_live(config, review["changes"])
    except PlanInvalid as error:
        # Keep the reason so a later preview or execute explains why a new plan is required.
        write_json(directory / "invalidated.json", {
            "plan_id": plan_id,
            "invalidated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "reason": str(error),
        })
        raise RuntimeError(f"{error} Execution is blocked; generate a new plan.") from None
    _matching_plan(config, workspace, stage, plan_id)
    report = workspace / "reports" / f"applied-{stage}.json"
    previous = read_json(report) if report.is_file() and not report.is_symlink() else None
    log = workspace / "reports" / f"apply-{stage}-{plan_id}.json"
    record = {
        "stage": stage,
        "plan_id": plan_id,
        "identity": review["identity"],
        "status": "attempting",
        "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "memory_requests_mib": config["derived"]["memory_requests_by_stage"][stage],
        "storage_mib": config["derived"]["storage_mib_by_stage"][stage],
        "diagnostics": str(log),
        "returncode": None,
    }
    if previous:
        record["previous"] = {
            key: previous.get(key) for key in ("plan_id", "status", "applied_at")
        }

    # Inputs: outcome fields. Outputs: none. Purpose: keep the consumed marker and the stage
    # report in step, so an older `applied` report never stands for this attempt.
    def save(**fields):
        record.update(fields)
        write_json(directory / "consumed.json", {
            "plan_id": plan_id,
            **{key: record.get(key) for key in ("status", "returncode", "diagnostics", "interrupted", "error")
               if key in record},
        })
        write_json(report, record)

    # Consume before dispatch: interruption or timeout must not leave a reusable mutation plan.
    save()
    result = None
    try:
        result = _terraform(
            [
                "apply",
                "-input=false",
                "-no-color",
                "-lock-timeout=20s",
                str(directory / "plan.tfplan"),
            ],
            workspace / "stages" / stage,
            log,
        )
        save(
            status="applied" if result.returncode == 0 else "failed",
            returncode=result.returncode,
            state_sha256=_state_hash(workspace, stage),
        )
    except KeyboardInterrupt:
        save(
            status="uncertain",
            interrupted=True,
            returncode=result.returncode if result is not None else None,
            error="Interrupted during Terraform apply; actual state is unknown.",
        )
        raise
    except RuntimeError as error:
        save(status="uncertain", error=str(error))
        raise RuntimeError(
            "Terraform invocation failed or timed out; inspect actual state before creating another plan."
        ) from None
    if result.returncode:
        raise RuntimeError(
            "Terraform apply failed; inspect private diagnostics and actual state before creating another plan."
        )
    return record


# Inputs: resolved configuration, workspace and current identity. Outputs: passing smoke summary or None.
# Purpose: accept a smoke result only for this cluster and the current plugin rollout. Dependencies: gpu-smoke report, kubectl GET.
def _gpu_smoke_current(config: dict, workspace: Path, identity: dict) -> dict | None:
    path = workspace / "reports" / "gpu-smoke.json"
    if not path.exists():
        return None
    report = read_json(path)
    derived = config["derived"]
    plugin = _get(config, "daemonsets.apps", derived["gpu_plugin_name"], derived["gpu_namespace"])
    current = {
        "uid": (plugin or {}).get("metadata", {}).get("uid"),
        "generation": (plugin or {}).get("metadata", {}).get("generation"),
    }
    if (
        report.get("status") != "pass"
        or report.get("kube_system_uid") != identity["kube_system_uid"]
        or report.get("plugin") != current
    ):
        return None
    return {"captured_at": report.get("captured_at"), "node": report.get("node")}


# Inputs: resolved configuration. Outputs: {name: (kind, object name, expected alerts or None)}.
# Purpose: derive the exact monitoring objects 40-monitoring renders for enabled components.
# Dependencies: naming in assets/infra/stacks/40-monitoring/main.tf.
def _expected_monitoring(config: dict) -> dict:
    env = config["environment_id"]
    expected, alerts = {}, set()
    if config["postgres"]["enabled"]:
        expected["podmonitor_postgres"] = ("podmonitors.monitoring.coreos.com", f"{env}-postgres")
        alerts |= POSTGRES_ALERTS
    if config["kafka"]["enabled"]:
        expected["podmonitor_kafka"] = ("podmonitors.monitoring.coreos.com", f"{env}-kafka")
        alerts |= KAFKA_ALERTS
    expected["prometheusrule"] = ("prometheusrules.monitoring.coreos.com", f"{env}-data", alerts)
    return expected


# Inputs: resolved configuration. Outputs: (name, passed, detail) per expected monitoring object.
# Purpose: require each enabled component's PodMonitor and the exact alert rule set, owned and
# selectable by Prometheus. Dependencies: kubectl GET.
def _monitoring_checks(config: dict) -> list[tuple]:
    namespace = config["monitoring"]["namespace"]
    checks = []
    for check, (resource, name, *rest) in _expected_monitoring(config).items():
        state, item = _observe(config, resource, name, namespace)
        state = _object_state(state, item, config["environment_id"], ready=False)
        detail = {"object": f"{namespace}/{name}", "state": state}
        if state == "ready":
            labels = item["metadata"].get("labels", {})
            if labels.get("release") != config["monitoring"]["release"]:
                state = detail["state"] = "not-selected"
            elif rest:
                found = {
                    rule["alert"]
                    for group in item.get("spec", {}).get("groups", [])
                    for rule in group.get("rules", [])
                    if "alert" in rule
                }
                detail["alerts"] = sorted(found)
                if found != rest[0]:
                    state = detail["state"] = "mismatch"
                    detail["expected_alerts"] = sorted(rest[0])
        checks.append((check, state == "ready", detail))
    return checks


# Inputs: resolved configuration. Outputs: (name, passed, detail) per Kafka sub-resource.
# Purpose: check every configured Topic, both Users, the node pool and its PVCs separately, and
# tell missing, not-ready, and unreadable objects apart. Dependencies: kubectl GET, Strimzi naming.
def _kafka_checks(config: dict) -> list[tuple]:
    env, namespace = config["environment_id"], config["kafka"]["namespace"]
    name = config["derived"]["kafka_name"]
    checks = []
    for topic in config["kafka"]["topics"]:
        # Mirrors the 31-kafka resource name: environment ID plus the topic's SHA-256 prefix.
        resource_name = f"{env}-{hashlib.sha256(topic.encode()).hexdigest()[:12]}"
        state, item = _observe(config, "kafkatopics.kafka.strimzi.io", resource_name, namespace)
        state = _object_state(state, item, env)
        if state == "ready" and item.get("spec", {}).get("topicName") != topic:
            state = "mismatch"
        checks.append((f"kafka_topic:{topic}", state == "ready", {"object": resource_name, "state": state}))
    for role in ("producer", "consumer"):
        state, item = _observe(config, "kafkausers.kafka.strimzi.io", f"{name}-{role}", namespace)
        state = _object_state(state, item, env)
        checks.append((f"kafka_user:{role}", state == "ready", {"object": f"{name}-{role}", "state": state}))
    pool = f"{name}-combined"
    state, item = _observe(config, "kafkanodepools.kafka.strimzi.io", pool, namespace)
    state = _object_state(state, item, env, ready=False)
    nodes = (item or {}).get("status", {}).get("nodeIds") or []
    current = (item or {}).get("status", {}).get("observedGeneration", 0) >= (item or {}).get(
        "metadata", {}
    ).get("generation", 1)
    if state == "ready" and (not current or len(nodes) != item.get("spec", {}).get("replicas")):
        state = "not-ready"
    checks.append(("kafka_node_pool", state == "ready", {"object": pool, "state": state}))
    if state != "ready":
        checks.append(("kafka_pvc", False, {"state": "unknown", "reason": "node pool is not ready"}))
        return checks
    for node in nodes:
        # Strimzi names JBOD volume 0 of Pod <cluster>-<pool>-<id> as data-0-<pod>.
        claim = f"data-0-{name}-{pool}-{node}"
        state, item = _observe(config, "persistentvolumeclaims", claim, namespace)
        if state == "present":
            labels = item["metadata"].get("labels", {})
            state = (
                "not-owned"
                if labels.get("strimzi.io/cluster") != name
                else "ready"
                if item.get("status", {}).get("phase") == "Bound"
                else "not-bound"
            )
        checks.append((f"kafka_pvc:{claim}", state == "ready", {"object": claim, "state": state}))
    return checks


# Inputs: configuration and a Prometheus HTTP API path with query string. Outputs: (data, reason).
# Purpose: query Prometheus read-only through the API server pod proxy. Dependencies: kubectl get --raw.
def _prometheus_api(config: dict, base: str, path: str) -> tuple[object, str | None]:
    try:
        result = run(_kube(config) + ["get", "--raw", base + path], timeout=20)
    except RuntimeError:
        return None, "Prometheus API request timed out or kubectl could not start."
    if result.returncode:
        if _read_failure(result) == "access-denied":
            return None, "Access denied: reading Prometheus needs get on pods/proxy in its namespace."
        return None, "Prometheus API is unreachable through the API server pod proxy."
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "Prometheus API returned an unreadable response."
    if document.get("status") != "success":
        return None, "Prometheus API returned an error status."
    return document.get("data"), None


# Inputs: resolved configuration. Outputs: acceptance entries for targets, samples and rules.
# Purpose: compare actual scrape and rule state with the enabled components, or keep each item
# pending with the reason Prometheus could not be read. Dependencies: prerequisites._prometheus, kubectl.
def _prometheus_checks(config: dict) -> list[dict]:
    from prerequisites import _prometheus

    names = ("prometheus_targets", "prometheus_samples", "prometheus_rules")
    try:
        instance = _prometheus(config)
        reason = None if instance else "No compatible Prometheus instance was found."
    except ReadError as error:
        instance, reason = None, f"Prometheus instance could not be read ({error.reason})."
    except RuntimeError as error:
        instance, reason = None, str(error)
    if instance is None:
        return [_acceptance(name, "pending", reason) for name in names]
    shards = instance.get("spec", {}).get("shards") or 1
    if shards > 1:
        reason = f"Prometheus has {shards} shards; verification requires evidence across all shards."
        return [_acceptance(name, "pending", reason) for name in names]
    namespace = instance["metadata"]["namespace"]
    pod = f"prometheus-{instance['metadata']['name']}-0"
    base = f"/api/v1/namespaces/{namespace}/pods/http:{pod}:9090/proxy/api/v1/"
    targets, reason = _prometheus_api(config, base, "targets?state=active")
    if reason:
        entries = [_acceptance("prometheus_targets", "pending", reason)]
    else:
        monitoring = config["monitoring"]["namespace"]
        expected = [
            name for kind, name, *_ in _expected_monitoring(config).values()
            if kind.startswith("podmonitors")
        ]
        pools = {}
        for target in targets.get("activeTargets", []):
            pools.setdefault(target.get("scrapePool", ""), []).append(target.get("health"))
        detail = {}
        for name in expected:
            health = [
                value for pool, values in pools.items()
                if pool.startswith(f"podMonitor/{monitoring}/{name}/")
                for value in values
            ]
            detail[name] = {"active": len(health), "up": health.count("up")}
        up = all(item["active"] and item["up"] == item["active"] for item in detail.values())
        entries = [_acceptance("prometheus_targets", "pass" if up else "fail", detail)]
    derived = config["derived"]
    queries = {}
    if config["postgres"]["enabled"]:
        queries["pg_up"] = (
            f'pg_up{{namespace="{derived["namespace"]}",pod=~"{derived["postgres_name"]}-[0-9]+"}}',
            "1",
        )
    if config["kafka"]["enabled"]:
        queries["kafka_under_replicated_partitions"] = (
            f'kafka_under_replicated_partitions{{namespace="{config["kafka"]["namespace"]}",'
            f'pod=~"{derived["kafka_name"]}-.+"}}',
            "0",
        )
    samples, failed = {}, None
    for metric, (query, wanted) in queries.items():
        data, reason = _prometheus_api(
            config, base, "query?" + urllib.parse.urlencode({"query": query})
        )
        if reason:
            failed = reason
            break
        values = [item.get("value", [None, None])[1] for item in data.get("result", [])]
        samples[metric] = {"series": len(values), "expected": wanted, "matching": values.count(wanted)}
    if failed:
        entries.append(_acceptance("prometheus_samples", "pending", failed))
    else:
        ok = all(item["series"] and item["matching"] == item["series"] for item in samples.values())
        entries.append(_acceptance("prometheus_samples", "pass" if ok else "fail", samples))
    data, reason = _prometheus_api(config, base, "rules?type=alert")
    if reason:
        entries.append(_acceptance("prometheus_rules", "pending", reason))
        return entries
    group = f"{config['environment_id']}.data"
    rules = {
        rule.get("name"): rule.get("state")
        for item in data.get("groups", [])
        if item.get("name") == group
        for rule in item.get("rules", [])
    }
    wanted = _expected_monitoring(config)["prometheusrule"][2]
    firing = sorted(name for name, state in rules.items() if state == "firing")
    ok = set(rules) == wanted and not firing
    entries.append(
        _acceptance(
            "prometheus_rules", "pass" if ok else "fail", {"loaded": rules, "firing": firing}
        )
    )
    return entries


# Inputs: check name, status (pass/fail/pending), and safe detail. Outputs: acceptance entry.
# Purpose: keep functional results separate from baseline checks. Dependencies: ACCEPTANCE_ITEMS.
def _acceptance(name: str, status: str, detail: object) -> dict:
    return {
        "name": name,
        "description": ACCEPTANCE_ITEMS[name],
        "status": status,
        "detail": detail,
    }


# Inputs: normalized configuration and workspace. Outputs: baseline results, functional acceptance
# results and pending items, each reported separately. Purpose: report actual readiness without
# treating Ready Pods as protocol, policy or recovery proof. Dependencies: kubectl GET (read-only),
# acceptance evidence reports.
def verify(config: dict, workspace: Path) -> dict:
    import acceptance

    report = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checks": [],
        "acceptance_checks": [],
        "acceptance_result": "incomplete",
        "pending_acceptance": [],
    }
    functional = report["acceptance_checks"]
    try:
        identity = _identity(config)
        bound = _bind(config, workspace, identity)
        _check(
            report,
            "workspace_target",
            bound,
            "Verification must match the workspace binding.",
        )
        if not bound:
            report.update(baseline_result="blocked", status="blocked")
            write_json(workspace / "reports" / "verification.json", report)
            return report
        for stage in config["derived"]["stages"]:
            try:
                ready, detail = _stage_ready(config, stage)
                # Kubelet registers the plugin resource shortly after the DaemonSet becomes Ready.
                for _ in range(GPU_REGISTRATION_RETRIES if stage == "20-gpu" else 0):
                    if ready:
                        break
                    time.sleep(2)
                    ready, detail = _stage_ready(config, stage)
            except ReadError as error:
                ready, detail = False, {"state": error.reason, "reason": str(error)}
            _check(report, stage, ready, detail)
        if config["gpu"]["enabled"]:
            try:
                smoke = _gpu_smoke_current(config, workspace, identity)
            except ReadError as error:
                _check(report, "gpu_smoke", False, {"state": error.reason, "reason": str(error)})
                functional.append(_acceptance("gpu_smoke", "pending", str(error)))
            else:
                if smoke:
                    _check(report, "gpu_smoke", True, smoke)
                functional.append(
                    _acceptance("gpu_smoke", "pass", smoke)
                    if smoke
                    else _acceptance(
                        "gpu_smoke", "pending", "Run gpu-smoke --execute against the current plugin."
                    )
                )
            functional.append(
                _acceptance("gpu_workload", "pending", "Needs a separate workload test.")
            )
        if config["postgres"]["enabled"]:
            state, pvc = _observe(
                config,
                "persistentvolumeclaims",
                f"{config['derived']['postgres_name']}-data",
                config["derived"]["namespace"],
            )
            if state == "present":
                state = "ready" if pvc.get("status", {}).get("phase") == "Bound" else "not-bound"
            _check(
                report,
                "postgres_pvc",
                state == "ready",
                {"state": state, "note": "A Bound PVC does not establish recovery."},
            )
            functional += acceptance.evidence(config, workspace, identity, "postgres")
            import recovery

            functional.append(recovery.evidence(config, workspace, identity))
        if config["kafka"]["enabled"]:
            for name, passed, detail in _kafka_checks(config):
                _check(report, name, passed, detail)
            functional += acceptance.evidence(config, workspace, identity, "kafka")
        if config["monitoring"]["enabled"]:
            for name, passed, detail in _monitoring_checks(config):
                _check(report, name, passed, detail)
            functional += _prometheus_checks(config)
            functional.append(
                _acceptance("alert_delivery", "pending", "Needs an isolated route and receiver.")
            )
        for name in ("network_policy_allowed", "network_policy_denied"):
            functional.append(_acceptance(name, "pending", "No automated runner."))
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        _check(
            report, "verification_access", False, "Verification data is unavailable or incomplete."
        )
    report["pending_acceptance"] = [
        item["description"] for item in functional if item["status"] == "pending"
    ]
    report["baseline_result"] = (
        "pass"
        if report["checks"] and all(item["status"] == "pass" for item in report["checks"])
        else "blocked"
    )
    statuses = {item["status"] for item in functional}
    report["acceptance_result"] = (
        "fail" if "fail" in statuses else "pass" if statuses == {"pass"} else "incomplete"
    )
    # A failed functional check fails the command; pending items alone keep the baseline status.
    report["status"] = (
        "fail"
        if report["baseline_result"] == "pass" and report["acceptance_result"] == "fail"
        else report["baseline_result"]
    )
    write_json(workspace / "reports" / "verification.json", report)
    return report
