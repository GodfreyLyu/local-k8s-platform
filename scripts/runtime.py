# Purpose: Inspect Minikube and execute individually reviewed Terraform stages without managing credentials.
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from capacity import assess
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


# Inputs: configuration and GET arguments. Outputs: decoded JSON or None for an absent named object.
# Purpose: Read cluster objects without printing their contents. Dependencies: kubectl, io_utils.run.
def _get(
    config: dict,
    resource: str,
    name: str | None = None,
    namespace: str | None = None,
    *,
    all_namespaces: bool = False,
) -> dict | None:
    command = _kube(config) + ["get", resource]
    if name is not None:
        command += [name, "--ignore-not-found=true"]
    if namespace is not None:
        command += ["--namespace", namespace]
    if all_namespaces:
        command += ["--all-namespaces"]
    result = run(command + ["-o", "json"], timeout=20)
    if result.returncode:
        raise RuntimeError(f"Cannot read {resource}; check API access and permissions.")
    return json.loads(result.stdout) if result.stdout.strip() else None


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


# Inputs: configuration and workspace. Outputs: a complete creation report for this target, or None.
# Purpose: require identity observed during creation before running later stages. Dependencies: private JSON report.
def _creation_report(config: dict, workspace: Path) -> dict | None:
    try:
        report = read_json(workspace / "reports" / "cluster.json")
    except FileNotFoundError:
        return None
    cluster = config["cluster"]
    identity = report.get("identity") if isinstance(report, dict) else None
    if (
        not isinstance(identity, dict)
        or report.get("status") != "created"
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


# Inputs: resolved configuration, workspace and optional stage. Outputs: remaining Pod forecasts.
# Purpose: omit unchanged applied workloads and reserve replacement headroom for increases. Dependencies: applied-stage records.
def _remaining_workloads(config: dict, workspace: Path, stage: str | None = None) -> list[dict]:
    result = []
    for name in config["derived"]["stages"]:
        if stage is not None and name != stage:
            continue
        items = [item for item in config["derived"]["workloads"] if item["stage"] == name]
        requested = sum(item["memory_mib"] for item in items)
        storage = sum(item["storage_mib"] for item in items)
        path = workspace / "reports" / f"applied-{name}.json"
        previous = read_json(path) if path.exists() else {}
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
        forecasts = (
            _remaining_workloads(config, workspace, stage) if workloads is None else workloads
        )
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
        return ready, "PostgreSQL StatefulSet needs one Ready replica at its current generation."
    if stage == "31-kafka":
        item = _get(
            config, "kafkas.kafka.strimzi.io", derived["kafka_name"], config["kafka"]["namespace"]
        )
        return bool(item) and _condition(item) and item.get("status", {}).get(
            "observedGeneration", 0
        ) >= item.get("metadata", {}).get(
            "generation", 1
        ), "Kafka must report Ready at its current generation."
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


# Inputs: private workspace. Outputs: recorded Kubernetes identities per stage state file.
# Purpose: Let cluster reconciliation compare live objects with Terraform state. Dependencies: local backend JSON.
def _state_resources(workspace: Path) -> list[dict]:
    result = []
    directory = workspace / "state"
    if not directory.exists():
        return result
    for path in sorted(directory.glob("*.tfstate")):
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
        "namespace": metadata.get("namespace"),
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
        old, new = before["manifest"], after["manifest"]
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
                previous = old["spec"].get("config", {}).get(key)
                current = new["spec"].get("config", {}).get(key)
                if (
                    previous is not None
                    and current is not None
                    and int(current) >= 0
                    and (int(previous) < 0 or int(current) < int(previous))
                ):
                    raise RuntimeError(
                        "Reducing existing Kafka retention requires a separate data migration."
                    )


# Inputs: normalized configuration and decoded Terraform plan. Outputs: safe resource-action summary.
# Purpose: Reject deletion, import, unsupported resources and collisions with resources outside this state. Dependencies: kubectl GET.
def _review_changes(config: dict, document: dict) -> list[dict]:
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
        changes.append(
            {
                "address": item["address"],
                "actions": actions,
                **{key: identity[key] for key in ("resource", "name", "namespace")},
                "uid": before["uid"] if actions != ["create"] else None,
            }
        )
    return changes


# Inputs: command, stage directory and private log path. Outputs: completed Terraform process.
# Purpose: Save diagnostics privately while keeping raw provider output out of the Agent response. Dependencies: Terraform, private JSON IO.
def _terraform(command: list[str], stage_directory: Path, log: Path):
    result = run(["terraform", f"-chdir={stage_directory}"] + command, timeout=600)
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
        result = _terraform(
            [
                "plan",
                "-input=false",
                "-no-color",
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
        changes = _review_changes(config, json.loads(result.stdout))
        review = {
            "stage": stage,
            "environment_id": config["environment_id"],
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "config_sha256": _digest(config),
            "identity": inventory["identity"],
            "source_hashes": _source_hashes(stage_directory),
            "prior_state_sha256": _state_hash(workspace, stage),
            "plan_sha256": sha256(saved),
            "changes": changes,
        }
        review["plan_id"] = _digest(review)
        write_json(directory / "review.json", review)
        return {
            "status": "planned",
            "stage": stage,
            "plan_id": review["plan_id"],
            "changes": changes,
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


# Inputs: normalized configuration, workspace, stage, exact plan ID and explicit execution flag.
# Outputs: read-only preview or application record. Purpose: Apply exactly one approved saved plan. Dependencies: Terraform, kubectl.
def apply(config: dict, workspace: Path, stage: str, plan_id: str, execute: bool = False) -> dict:
    review = _matching_plan(config, workspace, stage, plan_id)
    if not execute:
        return {
            "status": "preview",
            "stage": stage,
            "plan_id": plan_id,
            "changes": review["changes"],
            "requires_execute": True,
        }
    inventory = inspect(config, workspace, stage=stage)
    if inventory["status"] != "pass" or inventory["identity"] != review["identity"]:
        raise RuntimeError("Target identity or prerequisites changed; execution is blocked.")
    _prerequisites(config, workspace, stage)
    # Re-read ownership immediately before dispatch; reviewed metadata is not a live lock.
    for change in review["changes"]:
        live = _get(config, change["resource"], change["name"], change["namespace"])
        if change["actions"] == ["create"]:
            if live is not None:
                raise RuntimeError("A planned creation now exists; do not adopt it automatically.")
        else:
            metadata = live.get("metadata", {}) if live else {}
            if (
                metadata.get("labels", {}).get(ENVIRONMENT_LABEL) != config["environment_id"]
                or change.get("uid")
                and metadata.get("uid") != change["uid"]
            ):
                raise RuntimeError("Live resource ownership or UID changed after plan review.")
    _matching_plan(config, workspace, stage, plan_id)
    directory = workspace / "plans" / stage
    record = {
        "stage": stage,
        "plan_id": plan_id,
        "identity": review["identity"],
        "status": "attempting",
        "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "memory_requests_mib": config["derived"]["memory_requests_by_stage"][stage],
        "storage_mib": config["derived"]["storage_mib_by_stage"][stage],
    }
    # Consume before dispatch: interruption or timeout must not leave a reusable mutation plan.
    write_json(directory / "consumed.json", {"plan_id": plan_id, "status": "attempting"})
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
            directory / "apply-log.json",
        )
    except RuntimeError:
        record["status"] = "uncertain"
        write_json(directory / "consumed.json", {"plan_id": plan_id, "status": "uncertain"})
        write_json(workspace / "reports" / f"applied-{stage}.json", record)
        raise RuntimeError(
            "Terraform invocation failed or timed out; inspect actual state before creating another plan."
        ) from None
    record.update(
        status="applied" if result.returncode == 0 else "failed",
        state_sha256=_state_hash(workspace, stage),
    )
    write_json(directory / "consumed.json", {"plan_id": plan_id, "status": record["status"]})
    write_json(workspace / "reports" / f"applied-{stage}.json", record)
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


# Inputs: normalized configuration and workspace. Outputs: enabled-component baseline with explicit pending functional acceptance.
# Purpose: Report actual readiness without treating Ready Pods as protocol, policy or recovery proof. Dependencies: kubectl GET.
def verify(config: dict, workspace: Path) -> dict:
    report = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checks": [],
        "acceptance_result": "incomplete",
        "pending_acceptance": [],
    }
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
            ready, detail = _stage_ready(config, stage)
            # Kubelet registers the plugin resource shortly after the DaemonSet becomes Ready.
            for _ in range(GPU_REGISTRATION_RETRIES if stage == "20-gpu" else 0):
                if ready:
                    break
                time.sleep(2)
                ready, detail = _stage_ready(config, stage)
            _check(report, stage, ready, detail)
        if config["gpu"]["enabled"]:
            smoke = _gpu_smoke_current(config, workspace, identity)
            if smoke:
                _check(report, "gpu_smoke", True, smoke)
            else:
                report["pending_acceptance"].append(
                    "GPU Pod device injection: run gpu-smoke --execute against the current plugin"
                )
            report["pending_acceptance"].append(
                "Vulkan, model inference and GPU utilization inside the krunkit virtio GPU"
            )
        if config["postgres"]["enabled"]:
            pvc = _get(
                config,
                "persistentvolumeclaims",
                f"{config['derived']['postgres_name']}-data",
                config["derived"]["namespace"],
            )
            _check(
                report,
                "postgres_pvc",
                bool(pvc) and pvc.get("status", {}).get("phase") == "Bound",
                "PostgreSQL PVC must be Bound; this does not establish recovery.",
            )
            report["pending_acceptance"] += [
                "PostgreSQL verify-full connection and wrong-CA rejection using an isolated validation identity",
                "PostgreSQL non-superuser permission checks",
                "Host backup and isolated restore with data comparison",
            ]
        if config["kafka"]["enabled"]:
            report["pending_acceptance"] += [
                "Kafka TLS, metadata reachability and isolated produce/consume",
                "Kafka forbidden-topic and consumer-group ACL rejection",
            ]
        if config["monitoring"]["enabled"]:
            monitors = _get(
                config,
                "podmonitors.monitoring.coreos.com",
                namespace=config["monitoring"]["namespace"],
            )
            selected = [
                item
                for item in monitors["items"]
                if item["metadata"].get("labels", {}).get(ENVIRONMENT_LABEL)
                == config["environment_id"]
            ]
            _check(
                report,
                "podmonitors",
                bool(selected),
                "Owned PodMonitors must exist; scrape samples are checked separately.",
            )
            report["pending_acceptance"] += [
                "Actual Prometheus target health and database/Kafka series",
                "Isolated alert route and receiver delivery",
            ]
        report["pending_acceptance"].append(
            "Authorized and unauthorized cross-Pod NetworkPolicy traffic, including DNS"
        )
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        _check(
            report, "verification_access", False, "Verification data is unavailable or incomplete."
        )
    report["baseline_result"] = (
        "pass"
        if report["checks"] and all(item["status"] == "pass" for item in report["checks"])
        else "blocked"
    )
    report["status"] = report["baseline_result"]
    write_json(workspace / "reports" / "verification.json", report)
    return report
