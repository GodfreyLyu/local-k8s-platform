"""Purpose: validate user configuration once and derive a consistent local-platform contract."""

import ipaddress
import re
from copy import deepcopy
from pathlib import Path

from io_utils import read_json

DEFAULTS = {
    "schema_version": 1,
    "environment_id": "",
    "cluster": {
        "type": "minikube",
        "mode": "reuse",
        "context": "minikube",
        "kubeconfig": "~/.kube/config",
    },
    "application": {"namespace": "", "service_account": "", "database": "", "username": ""},
    "postgres": {
        "enabled": True,
        "storage": "4Gi",
        "resources": {
            "requests": {"cpu": "250m", "memory": "384Mi"},
            "limits": {"cpu": "1000m", "memory": "768Mi"},
        },
    },
    "kafka": {
        "enabled": True,
        "namespace": "kafka",
        "storage": "4Gi",
        "topics": [],
        "consumer_group": "",
        "resources": {
            "requests": {"cpu": "300m", "memory": "768Mi"},
            "limits": {"cpu": "1500m", "memory": "1280Mi"},
        },
    },
    "monitoring": {
        "enabled": True,
        "namespace": "monitoring",
        "release": "monitoring",
        "pod_labels": {"app.kubernetes.io/name": "prometheus"},
    },
    "storage": {"mode": "reuse", "class": "", "nodes": []},
    "network": {
        "dns_namespace": "kube-system",
        "dns_labels": {"k8s-app": "kube-dns"},
        "api_server_cidrs": [],
    },
    "budget": {"max_memory_requests_mib": 2048, "min_disk_free_mib": 2048},
}
DNS = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
SQL = r"[a-z][a-z0-9_]{0,30}"
LABEL_MAPS = {"network.dns_labels", "monitoring.pod_labels"}


# Inputs: user mapping, default mapping, field path. Outputs: merged configuration.
# Purpose: reject unknown keys and wrong types at the input boundary. Dependencies: DEFAULTS.
def merge(value, defaults, prefix=""):
    if not isinstance(value, dict):
        raise ValueError(f"{prefix or 'configuration'} must be an object")
    unknown = set(value) - set(defaults)
    if unknown:
        raise ValueError(f"Unknown configuration field: {prefix}.{sorted(unknown)[0]}")
    result = deepcopy(defaults)
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if type(item) is not type(defaults[key]):
            raise ValueError(f"{name} has the wrong type")
        result[key] = (
            merge(item, defaults[key], name)
            if isinstance(item, dict) and name not in LABEL_MAPS
            else item
        )
    return result


# Inputs: string, regex, field name, maximum length. Outputs: none.
# Purpose: validate named configuration values. Dependencies: Python regular expressions.
def identifier(value, pattern, name, maximum=63):
    if not re.fullmatch(pattern, value) or len(value) > maximum:
        raise ValueError(f"{name} is not a supported identifier")


# Inputs: Kubernetes-style memory/storage quantity. Outputs: integer MiB.
# Purpose: normalize supported binary quantities once. Dependencies: Mi/Gi units.
def memory_mib(value):
    match = re.fullmatch(r"([1-9][0-9]*)(Mi|Gi)", value)
    if not match:
        raise ValueError("Memory and storage quantities must use positive integral Mi or Gi")
    return int(match[1]) * (1024 if match[2] == "Gi" else 1)


# Inputs: Kubernetes CPU quantity. Outputs: integer millicores.
# Purpose: normalize supported CPU quantities once. Dependencies: integral cores or millicores.
def cpu_millis(value):
    if not re.fullmatch(r"[1-9][0-9]*m?", value):
        raise ValueError("CPU quantities must use positive integral cores or millicores")
    return int(value[:-1]) if value.endswith("m") else int(value) * 1000


# Inputs: selector map and field name. Outputs: none. Purpose: validate explicit label selectors.
# Dependencies: Kubernetes label-key and label-value syntax.
def selector(value, name):
    if not value:
        raise ValueError(f"{name} cannot be empty")
    for key, item in value.items():
        if not isinstance(item, str):
            raise ValueError(f"{name} values must be strings")
        parts = key.split("/")
        if (
            len(parts) > 2
            or len(parts[-1]) > 63
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", parts[-1])
        ):
            raise ValueError(f"{name} contains an invalid label key")
        if len(parts) == 2 and (
            len(parts[0]) > 253 or any(not re.fullmatch(DNS, part) for part in parts[0].split("."))
        ):
            raise ValueError(f"{name} contains an invalid label prefix")
        if (
            len(item) > 63
            or item
            and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", item)
        ):
            raise ValueError(f"{name} contains an invalid label value")


# Inputs: user JSON path. Outputs: fully normalized, validated configuration.
# Purpose: establish the single trusted configuration boundary. Dependencies: DEFAULTS and helpers.
def load_config(path):
    path = Path(path).expanduser().resolve()
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a JSON object")
    raw = {key: value for key, value in raw.items() if key not in {"$comment", "derived"}}
    config = merge(raw, DEFAULTS)
    if (
        config["schema_version"] != 1
        or config["cluster"]["type"] != "minikube"
        or config["cluster"]["mode"] != "reuse"
    ):
        raise ValueError("Version 1 supports only an existing Minikube cluster")
    env = config["environment_id"]
    identifier(env, DNS, "environment_id", 24)
    for key in ("context", "kubeconfig"):
        text = config["cluster"][key]
        if not text.strip() or any(ord(char) < 32 for char in text):
            raise ValueError(f"cluster.{key} must be a nonempty single-line value")
    kubeconfig = Path(config["cluster"]["kubeconfig"]).expanduser()
    config["cluster"]["kubeconfig"] = str((path.parent / kubeconfig).resolve())
    app = config["application"]
    sql_name = env.replace("-", "_")
    sql_name = sql_name if sql_name[0].isalpha() else f"app_{sql_name}"
    for key, default in {
        "namespace": f"{env}-app",
        "service_account": f"{env}-app",
        "database": sql_name,
        "username": sql_name,
    }.items():
        app[key] = app[key] or default
        identifier(app[key], SQL if key in {"database", "username"} else DNS, f"application.{key}")
    if app["username"] in {"postgres", "postgres_exporter"}:
        raise ValueError("The application cannot use an administrative or monitoring database role")
    if app["database"] in {"postgres", "template0", "template1"}:
        raise ValueError("The application database cannot replace an initdb system database")
    if app["namespace"].startswith("kube-") or app["namespace"] in {
        "default",
        "kafka",
        "monitoring",
    }:
        raise ValueError("The application namespace must be dedicated to this environment")
    if not config["postgres"]["enabled"] and not config["kafka"]["enabled"]:
        raise ValueError("Enable at least PostgreSQL or Kafka")
    for component in ("kafka", "monitoring"):
        identifier(config[component]["namespace"], DNS, f"{component}.namespace")
    identifier(config["monitoring"]["release"], DNS, "monitoring.release")
    if app["namespace"] in {
        config["kafka"]["namespace"],
        config["monitoring"]["namespace"],
        f"{env}-data",
        f"{env}-validation",
    }:
        raise ValueError("Application and infrastructure namespaces must be distinct")
    storage = config["storage"]
    if storage["mode"] not in {"reuse", "create"}:
        raise ValueError("storage.mode must be reuse or create")
    storage["class"] = storage["class"] or (
        f"{env}-local-retain" if storage["mode"] == "create" else ""
    )
    identifier(storage["class"], DNS, "storage.class")
    managed_namespaces = {app["namespace"], f"{env}-data", f"{env}-validation"}
    if storage["mode"] == "create":
        if f"{env}-storage" in managed_namespaces:
            raise ValueError("The storage namespace cannot also be an application namespace")
        managed_namespaces.add(f"{env}-storage")
    for component in ("kafka", "monitoring"):
        if config[component]["enabled"] and config[component]["namespace"] in managed_namespaces:
            raise ValueError("Reused infrastructure namespaces cannot also be managed namespaces")
    if storage["mode"] == "create" and not storage["nodes"]:
        raise ValueError("Creating Local Path requires explicitly selected node names")
    for node in storage["nodes"]:
        if not isinstance(node, str):
            raise ValueError("storage.nodes must contain node names")
        identifier(node, DNS, "storage.nodes")
    if len(set(storage["nodes"])) != len(storage["nodes"]):
        raise ValueError("storage.nodes cannot contain duplicates")
    kafka = config["kafka"]
    kafka["topics"] = kafka["topics"] or [f"{env}.jobs.v1", f"{env}.jobs.dlq.v1"]
    kafka["consumer_group"] = kafka["consumer_group"] or f"{env}-worker"
    identifier(kafka["consumer_group"], r"[a-zA-Z0-9][a-zA-Z0-9._-]*", "kafka.consumer_group", 128)
    for topic in kafka["topics"]:
        if not isinstance(topic, str):
            raise ValueError("kafka.topics must contain topic names")
        identifier(topic, r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", "kafka.topics", 63)
    if len(set(kafka["topics"])) != len(kafka["topics"]):
        raise ValueError("kafka.topics cannot contain duplicates")
    network = config["network"]
    identifier(network["dns_namespace"], DNS, "network.dns_namespace")
    selector(network["dns_labels"], "network.dns_labels")
    selector(config["monitoring"]["pod_labels"], "monitoring.pod_labels")
    for cidr in network["api_server_cidrs"]:
        if not isinstance(cidr, str):
            raise ValueError("network.api_server_cidrs must contain CIDR strings")
        try:
            parsed = ipaddress.ip_network(cidr, strict=True)
        except ValueError:
            raise ValueError("network.api_server_cidrs contains an invalid CIDR") from None
        if parsed.version != 4 or parsed.prefixlen != 32:
            raise ValueError("API server egress requires explicit IPv4 /32 addresses")
    requested = 64 if storage["mode"] == "create" else 0
    total_storage = 0
    for name in ("postgres", "kafka"):
        component = config[name]
        size = memory_mib(component["storage"])
        for unit, parser in (("cpu", cpu_millis), ("memory", memory_mib)):
            request = parser(component["resources"]["requests"][unit])
            limit = parser(component["resources"]["limits"][unit])
            if request > limit:
                raise ValueError(f"{name} {unit} request exceeds its limit")
        if component["enabled"]:
            minimum_request, minimum_limit = (256, 512) if name == "postgres" else (512, 768)
            if (
                memory_mib(component["resources"]["requests"]["memory"]) < minimum_request
                or memory_mib(component["resources"]["limits"]["memory"]) < minimum_limit
            ):
                raise ValueError(f"{name} memory is below the supported startup minimum")
            requested += memory_mib(component["resources"]["requests"]["memory"])
            total_storage += size
    if config["postgres"]["enabled"] and config["monitoring"]["enabled"]:
        requested += 64
    if kafka["enabled"]:
        requested += 256
    if any(value <= 0 for value in config["budget"].values()):
        raise ValueError("Resource budgets must be positive integers")
    if requested > config["budget"]["max_memory_requests_mib"]:
        raise ValueError("Selected component requests exceed the configured memory budget")
    stages = ["00-bootstrap"]
    if storage["mode"] == "create":
        stages.append("05-storage")
    stages += [
        stage
        for component, stage in (
            ("postgres", "30-postgres"),
            ("kafka", "31-kafka"),
            ("monitoring", "40-monitoring"),
        )
        if config[component]["enabled"]
    ]
    memory_by_stage = {
        "00-bootstrap": 0,
        "05-storage": 64,
        "30-postgres": memory_mib(config["postgres"]["resources"]["requests"]["memory"])
        + (64 if config["monitoring"]["enabled"] else 0),
        "31-kafka": memory_mib(kafka["resources"]["requests"]["memory"]) + 256,
        "40-monitoring": 0,
    }
    storage_by_stage = {
        "00-bootstrap": 0,
        "05-storage": 0,
        "30-postgres": memory_mib(config["postgres"]["storage"]),
        "31-kafka": memory_mib(kafka["storage"]),
        "40-monitoring": 0,
    }
    config["derived"] = {
        "namespace": f"{env}-data",
        "validation_namespace": f"{env}-validation",
        "postgres_name": f"{env}-postgres",
        "kafka_name": f"{env}-kafka",
        "labels": {
            "app.kubernetes.io/managed-by": "local-k8s-platform",
            "local-k8s-platform/environment": env,
        },
        "stages": stages,
        "requests_memory_mib": requested,
        "total_storage_mib": total_storage,
        "memory_requests_by_stage": {stage: memory_by_stage[stage] for stage in stages},
        "storage_mib_by_stage": {stage: storage_by_stage[stage] for stage in stages},
    }
    return config
