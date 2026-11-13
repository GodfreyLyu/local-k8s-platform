# Purpose: Inspect Minikube and execute individually reviewed Terraform stages without managing credentials.
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path

from io_utils import private_dir, read_json, run, sha256, write_json

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_LABEL = "local-k8s-platform/environment"
RESOURCE_TYPES = {
    "kubernetes_namespace_v1": "namespaces",
    "kubernetes_service_account_v1": "serviceaccounts",
    "kubernetes_config_map_v1": "configmaps",
    "kubernetes_service_v1": "services",
    "kubernetes_stateful_set_v1": "statefulsets.apps",
    "kubernetes_deployment_v1": "deployments.apps",
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
    config: dict, resource: str, name: str | None = None, namespace: str | None = None
) -> dict | None:
    command = _kube(config) + ["get", resource]
    if name is not None:
        command += [name, "--ignore-not-found=true"]
    if namespace is not None:
        command += ["--namespace", namespace]
    result = run(command + ["-o", "json"], timeout=20)
    if result.returncode:
        raise RuntimeError(f"Cannot read {resource}; check API access and permissions.")
    return json.loads(result.stdout) if result.stdout.strip() else None


# Inputs: normalized configuration. Outputs: cluster identity without authentication material.
# Purpose: Detect cluster replacement or retargeting under the same context name. Dependencies: kubectl, kubeconfig file.
def _identity(config: dict) -> dict:
    namespace = _get(config, "namespaces", "kube-system")
    if not namespace or not namespace.get("metadata", {}).get("uid"):
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
        "kube_system_uid": namespace["metadata"]["uid"],
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


# Inputs: normalized configuration, workspace, target identity. Outputs: whether the workspace binding matches.
# Purpose: Persist the first target and reject accidental reuse for another cluster. Dependencies: private JSON workspace.
def _bind(config: dict, workspace: Path, identity: dict) -> bool:
    path = workspace / "cluster-binding.json"
    durable = {key: value for key, value in identity.items() if key != "kubeconfig_sha256"}
    binding = {"environment_id": config["environment_id"], "identity": durable}
    if path.exists():
        return read_json(path) == binding
    write_json(path, binding)
    return True


# Inputs: normalized configuration, workspace. Outputs: remaining total memory, storage and largest stage request in MiB.
# Purpose: Avoid counting previously applied workloads as new capacity requests. Dependencies: applied-stage records.
def _remaining_demand(config: dict, workspace: Path) -> tuple[int, int, int]:
    memory = config["derived"]["memory_requests_by_stage"]
    storage = config["derived"]["storage_mib_by_stage"]
    remaining_memory = 0
    remaining_storage = 0
    largest_request = 0
    for stage, requested in memory.items():
        path = workspace / "reports" / f"applied-{stage}.json"
        previous = read_json(path) if path.exists() else {}
        prior_memory = (
            previous.get("memory_requests_mib", 0) if previous.get("status") == "applied" else 0
        )
        prior_storage = previous.get("storage_mib", 0) if previous.get("status") == "applied" else 0
        additional_memory = max(0, requested - prior_memory)
        remaining_memory += additional_memory
        largest_request = max(largest_request, additional_memory)
        remaining_storage += max(0, storage.get(stage, 0) - prior_storage)
    return remaining_memory, remaining_storage, largest_request


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


# Inputs: normalized configuration and private workspace. Outputs: persisted read-only inventory and prerequisite checks.
# Purpose: Measure the actual target before generating or executing plans. Dependencies: kubectl GET, kubelet summary access.
def inspect(config: dict, workspace: Path) -> dict:
    report = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checks": [],
        "status": "blocked",
    }
    try:
        report["identity"] = _identity(config)
        _check(
            report,
            "workspace_target",
            _bind(config, workspace, report["identity"]),
            "Workspace is bound to one environment and cluster identity.",
        )
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
        eligible = (
            set(config["storage"]["nodes"])
            if config["storage"]["mode"] == "create"
            else {item["metadata"]["name"] for item in nodes}
        )
        _check(
            report,
            "storage_nodes",
            eligible.issubset({item["metadata"]["name"] for item in nodes}),
            {"eligible": sorted(eligible)},
        )
        memory_needed, storage_needed, largest_request = _remaining_demand(config, workspace)
        measurements = []
        for node in nodes:
            name = node["metadata"]["name"]
            if name not in eligible:
                continue
            response = run(
                _kube(config) + ["get", "--raw", f"/api/v1/nodes/{name}/proxy/stats/summary"],
                timeout=20,
            )
            if response.returncode:
                raise RuntimeError(
                    "Measured node capacity is unavailable; allocatable capacity cannot replace it."
                )
            summary = json.loads(response.stdout)["node"]
            measurements.append(
                {
                    "name": name,
                    "memory_available_mib": int(summary["memory"]["availableBytes"]) // 1048576,
                    "disk_available_mib": int(summary["fs"]["availableBytes"]) // 1048576,
                }
            )
        report["capacity"] = {
            "nodes": measurements,
            "new_memory_requests_mib": memory_needed,
            "new_storage_mib": storage_needed,
        }
        _check(
            report,
            "measured_memory",
            bool(measurements)
            and sum(item["memory_available_mib"] for item in measurements) >= memory_needed + 256,
            "Measured aggregate memory needs new requests plus 256MiB headroom.",
        )
        _check(
            report,
            "single_node_memory",
            bool(measurements)
            and max(item["memory_available_mib"] for item in measurements) >= largest_request + 128,
            "At least one eligible node must fit the largest new stage request plus 128MiB headroom.",
        )
        _check(
            report,
            "measured_disk",
            bool(measurements)
            and all(
                item["disk_available_mib"] >= storage_needed + config["budget"]["min_disk_free_mib"]
                for item in measurements
            ),
            "Every eligible local-storage node must fit all new volumes plus disk headroom.",
        )
        if config["storage"]["mode"] == "reuse":
            storage_class = _get(
                config, "storageclasses.storage.k8s.io", config["storage"]["class"]
            )
            _check(
                report,
                "storage_class",
                bool(storage_class)
                and storage_class.get("volumeBindingMode") == "WaitForFirstConsumer"
                and storage_class.get("reclaimPolicy") == "Retain",
                "An explicit WFFC/Retain StorageClass is required.",
            )
        required_crds = []
        if config["kafka"]["enabled"]:
            required_crds += [
                "kafkas.kafka.strimzi.io",
                "kafkanodepools.kafka.strimzi.io",
                "kafkatopics.kafka.strimzi.io",
                "kafkausers.kafka.strimzi.io",
            ]
            _check(
                report,
                "kafka_namespace",
                any(
                    item["metadata"]["name"] == config["kafka"]["namespace"] for item in namespaces
                ),
                "The existing Strimzi namespace must be present.",
            )
            operators = _get(config, "deployments.apps", namespace=config["kafka"]["namespace"])[
                "items"
            ]
            strimzi = [
                item
                for item in operators
                if any(
                    "strimzi/operator" in container.get("image", "")
                    for container in item.get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                    .get("containers", [])
                )
            ]
            matching = [
                item
                for item in strimzi
                if any(
                    container.get("image", "").split("@")[0] == "quay.io/strimzi/operator:1.3.0"
                    for container in item["spec"]["template"]["spec"]["containers"]
                )
                and item.get("status", {}).get("availableReplicas", 0)
                >= item.get("spec", {}).get("replicas", 1)
                and item.get("status", {}).get("updatedReplicas", 0)
                >= item.get("spec", {}).get("replicas", 1)
                and item.get("status", {}).get("observedGeneration", 0)
                >= item.get("metadata", {}).get("generation", 1)
            ]
            _check(
                report,
                "strimzi_profile",
                len(strimzi) == len(matching) == 1,
                "One available Strimzi 1.3.0 Operator is required by the Kafka 4.3.1 profile; no Operator is installed or upgraded.",
            )
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
        if config["monitoring"]["enabled"]:
            required_crds += [
                "podmonitors.monitoring.coreos.com",
                "prometheusrules.monitoring.coreos.com",
            ]
            _check(
                report,
                "monitoring_namespace",
                any(
                    item["metadata"]["name"] == config["monitoring"]["namespace"]
                    for item in namespaces
                ),
                "The reused monitoring namespace must be present.",
            )
        for name in required_crds:
            crd = _get(config, "customresourcedefinitions.apiextensions.k8s.io", name)
            served = bool(crd) and any(
                version.get("name") == "v1" and version.get("served") is True
                for version in crd.get("spec", {}).get("versions", [])
            )
            _check(
                report,
                f"crd/{name}",
                bool(crd) and _condition(crd, "Established") and served,
                "Required existing CRD must be Established and serve the v1 API used by the assets.",
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
    if stage in ("00-bootstrap", "05-storage"):
        return []
    required = ["00-bootstrap"]
    if stage in ("30-postgres", "31-kafka") and config["storage"]["mode"] == "create":
        required.append("05-storage")
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
    if stage == "05-storage":
        item = _get(config, "storageclasses.storage.k8s.io", config["storage"]["class"])
        return bool(item) and item.get("volumeBindingMode") == "WaitForFirstConsumer" and item.get(
            "reclaimPolicy"
        ) == "Retain", "Owned WFFC/Retain StorageClass must be ready."
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
        inventory = inspect(config, workspace)
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
    inventory = inspect(config, workspace)
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
        _check(
            report,
            "workspace_target",
            _bind(config, workspace, identity),
            "Verification must target the bound cluster.",
        )
        for stage in config["derived"]["stages"]:
            ready, detail = _stage_ready(config, stage)
            _check(report, stage, ready, detail)
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
