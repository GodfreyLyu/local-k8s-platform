"""Purpose: plan missing platform prerequisites and create only the reviewed resources or packages."""

import datetime as dt
import json
import os
import re
import shutil
import socket
import sys
from copy import deepcopy
from fractions import Fraction
from pathlib import Path

import runtime
from config import memory_mib
from io_utils import read_json, run, sha256, write_json

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "prerequisites"
VERSIONS = read_json(ROOT / "assets" / "infra" / "versions.json")
RESOURCES = {
    "Namespace": "namespaces",
    "CustomResourceDefinition": "customresourcedefinitions.apiextensions.k8s.io",
    "ClusterRole": "clusterroles.rbac.authorization.k8s.io",
    "ClusterRoleBinding": "clusterrolebindings.rbac.authorization.k8s.io",
    "RoleBinding": "rolebindings.rbac.authorization.k8s.io",
    "ServiceAccount": "serviceaccounts",
    "ConfigMap": "configmaps",
    "Deployment": "deployments.apps",
    "Service": "services",
    "Prometheus": "prometheuses.monitoring.coreos.com",
}
READY_TIMEOUT_SECONDS = 600
ORDER = {kind: index for index, kind in enumerate(RESOURCES)}
CRDS = {
    "strimzi": [
        "kafkas.kafka.strimzi.io",
        "kafkanodepools.kafka.strimzi.io",
        "kafkatopics.kafka.strimzi.io",
        "kafkausers.kafka.strimzi.io",
    ],
    "monitoring": [
        "podmonitors.monitoring.coreos.com",
        "prometheusrules.monitoring.coreos.com",
        "prometheuses.monitoring.coreos.com",
    ],
}


# Inputs: Kubernetes document. Outputs: resource, name and optional namespace.
# Purpose: address only the packaged resource kinds. Dependencies: RESOURCES.
def _reference(document):
    metadata = document["metadata"]
    return {
        "resource": RESOURCES[document["kind"]],
        "name": metadata["name"],
        "namespace": metadata.get("namespace"),
    }


# Inputs: live object and desired subset. Outputs: whether all desired fields match.
# Purpose: allow API defaults without accepting drift in owned installation resources. Dependencies: JSON values.
def _matches(live, desired):
    if isinstance(desired, dict):
        return isinstance(live, dict) and all(
            key in live and _matches(live[key], value) for key, value in desired.items()
        )
    if isinstance(desired, list):
        return (
            isinstance(live, list)
            and len(live) == len(desired)
            and all(_matches(actual, expected) for actual, expected in zip(live, desired))
        )
    return live == desired


# Inputs: live resource and desired manifest. Outputs: whether the desired fields match.
# Purpose: compare Deployment env values after API serialization. Dependencies: Kubernetes EnvVar defaults.
def _matches_document(live, desired):
    if desired["kind"] == "Deployment":
        live, desired = deepcopy(live), deepcopy(desired)
        pod_spec = live.get("spec", {}).get("template", {}).get("spec", {})
        for container in pod_spec.get("containers", []) + pod_spec.get("initContainers", []):
            for entry in container.get("env", []):
                # Kubernetes omits empty literals. A source supplies a separate value.
                if entry.get("valueFrom") is None:
                    entry.setdefault("value", "")
                else:
                    entry.pop("value", None)
        # The API server stores canonical quantities, for example 1000m as 1.
        for document in (live, desired):
            spec = document.get("spec", {}).get("template", {}).get("spec", {})
            for container in spec.get("containers", []) + spec.get("initContainers", []):
                for group in container.get("resources", {}).values():
                    if isinstance(group, dict):
                        for key, value in group.items():
                            group[key] = _quantity(value)
    return _matches(live, desired)


# Inputs: Kubernetes quantity string. Outputs: exact numeric value, or the input if unparseable.
# Purpose: compare equivalent quantities after API canonicalization. Dependencies: Kubernetes suffixes.
def _quantity(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(m|k|M|G|T|Ki|Mi|Gi|Ti)?", str(value))
    if not match:
        return value
    scale = {
        None: 1,
        "m": Fraction(1, 1000),
        "k": 10**3,
        "M": 10**6,
        "G": 10**9,
        "T": 10**12,
        "Ki": 2**10,
        "Mi": 2**20,
        "Gi": 2**30,
        "Ti": 2**40,
    }[match[2]]
    return Fraction(match[1]) * scale


# Inputs: live object and resolved configuration. Outputs: whether this environment owns it.
# Purpose: distinguish reuse from recovery of our own partial installation. Dependencies: ownership labels.
def _owned(item, config):
    metadata = item.get("metadata", {})
    return not metadata.get("ownerReferences") and _matches(
        metadata.get("labels", {}), config["derived"]["labels"]
    )


# Inputs: live object. Outputs: stable identity and content evidence without status.
# Purpose: bind reused resources to a review while ignoring controller progress. Dependencies: SHA-256.
def _observation(item):
    metadata = item["metadata"]
    content = {key: value for key, value in item.items() if key not in {"metadata", "status"}}
    content["metadata"] = {
        key: metadata.get(key)
        for key in (
            "name",
            "namespace",
            "uid",
            "labels",
            "ownerReferences",
        )
    }
    return {"uid": metadata.get("uid"), "sha256": runtime._digest(content)}


# Inputs: a controller object. Outputs: whether the current nonzero replica set is available.
# Purpose: reject stale or scaled-down controllers. Dependencies: Kubernetes rollout status fields.
def controller_ready(item):
    replicas = item.get("spec", {}).get("replicas", 1)
    status = item.get("status", {})
    keys = ("readyReplicas", "updatedReplicas") + (
        ("availableReplicas",) if item.get("kind") == "Deployment" else ()
    )
    return (
        replicas > 0
        and all(status.get(key, 0) >= replicas for key in keys)
        and status.get("observedGeneration", 0) >= item["metadata"].get("generation", 1)
    )


# Inputs: container and long flag name. Outputs: its value or the default.
# Purpose: read both common argument forms from existing operators. Dependencies: argv-style container args.
def _flag(container, name, default=None):
    arguments = container.get("args", [])
    for index, argument in enumerate(arguments):
        if argument.startswith(name + "="):
            return argument.split("=", 1)[1]
        if argument == name:
            return arguments[index + 1] if index + 1 < len(arguments) else ""
    return default


# Inputs: one Deployment object. Outputs: whether a Strimzi custom resource manages it.
# Purpose: keep Kafka operands out of operator detection. Dependencies: Strimzi labels and owner references.
def _strimzi_operand(item):
    metadata = item.get("metadata", {})
    return "strimzi.io/cluster" in (metadata.get("labels") or {}) or any(
        owner.get("apiVersion", "").startswith("kafka.strimzi.io/")
        for owner in metadata.get("ownerReferences") or []
    )


# Inputs: all Deployment objects and selected component. Outputs: matching operator Deployments.
# Purpose: detect installations outside the configured namespace before creating a duplicate. Dependencies: image/name conventions.
def _operators(deployments, component):
    image = "strimzi/operator" if component == "strimzi" else "prometheus-operator"
    return [
        item
        for item in deployments
        # Strimzi runs its per-cluster operands (entity, topic and user operators) from the same
        # image; they belong to a Kafka resource and are not cluster operators.
        if not _strimzi_operand(item)
        and any(
            image in container.get("image", "") or image == container.get("name")
            for container in item.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
    ]


# Inputs: operator candidates, component, resolved config and readiness requirement. Outputs: one compatible Deployment.
# Purpose: enforce the Kafka namespace/version and the monitoring watch scope. Dependencies: live Deployment specs.
def _operator(candidates, component, config, ready=True):
    if len(candidates) != 1:
        raise RuntimeError(
            f"{component}: exactly one compatible Operator is required; found {len(candidates)}."
        )
    item = candidates[0]
    namespace = config["kafka" if component == "strimzi" else "monitoring"]["namespace"]
    containers = item["spec"]["template"]["spec"]["containers"]
    if component == "strimzi":
        pinned = VERSIONS["images"]["strimzi_operator"]["reference"]
        accepted = {
            pinned,
            pinned.split("@")[0],
            "quay.io/strimzi/operator@" + pinned.split("@")[1],
        }
        containers = [container for container in containers if container.get("image") in accepted]
        valid = item["metadata"].get("namespace") == namespace and len(containers) == 1
        environment = (
            {entry["name"]: entry for entry in containers[0].get("env", [])} if containers else {}
        )
        watched = environment.get("STRIMZI_NAMESPACE", {})
        valid = valid and (
            namespace in watched.get("value", "").split(",")
            or watched.get("value") == "*"
            or watched.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath")
            == "metadata.namespace"
        )
    else:
        containers = [
            container
            for container in containers
            if "prometheus-operator" in container.get("image", "")
        ]
        valid = len(containers) == 1
        if valid:
            for name in ("--namespaces", "--prometheus-instance-namespaces"):
                watched = _flag(containers[0], name)
                valid = valid and (watched is None or namespace in watched.split(","))
            valid = valid and namespace not in _flag(containers[0], "--deny-namespaces", "").split(
                ","
            )
    if not valid or ready and not controller_ready(item):
        raise RuntimeError(
            f"{component}: Operator version, namespace, watch scope or readiness is incompatible."
        )
    return item


# Inputs: resolved config and component. Outputs: established CRDs serving the required v1 API.
# Purpose: prove API compatibility independently of an Operator image. Dependencies: kubectl GET.
def _crds(config, component):
    result = []
    for name in CRDS[component]:
        item = runtime._get(config, RESOURCES["CustomResourceDefinition"], name)
        if (
            not item
            or not runtime._condition(item, "Established")
            or not any(
                version["name"] == "v1" and version.get("served") is True
                for version in item["spec"].get("versions", [])
            )
        ):
            raise RuntimeError(f"{component}: {name} must be Established and serve v1.")
        result.append(item)
    return result


# Inputs: Kubernetes label selector and labels. Outputs: whether the selector matches.
# Purpose: verify existing Prometheus selectors against the planned monitors and rules. Dependencies: standard selector operators.
def _selects(selector, labels):
    if selector is None or not _matches(labels, selector.get("matchLabels", {})):
        return False
    for expression in selector.get("matchExpressions", []):
        key, operation = expression["key"], expression["operator"]
        present, value = key in labels, labels.get(key)
        values = expression.get("values", [])
        if not {
            "In": present and value in values,
            "NotIn": not present or value not in values,
            "Exists": present,
            "DoesNotExist": not present,
        }.get(operation, False):
            return False
    return True


# Inputs: resolved config. Outputs: compatible Prometheus or None.
# Purpose: avoid creating a second instance when an existing one has incompatible selectors. Dependencies: Prometheus and namespace GETs.
def _prometheus(config):
    namespace = config["monitoring"]["namespace"]
    items = runtime._get(config, RESOURCES["Prometheus"], namespace=namespace)["items"]
    ns = runtime._get(config, "namespaces", namespace) or {}
    labels = {**config["derived"]["labels"], "release": config["monitoring"]["release"]}
    matching = []
    for item in items:
        spec = item["spec"]
        if not spec.get("paused", False) and all(
            _selects(spec.get(selector), labels)
            and (
                spec.get(scope) is None
                or _selects(spec[scope], ns.get("metadata", {}).get("labels", {}))
            )
            for selector, scope in (
                ("podMonitorSelector", "podMonitorNamespaceSelector"),
                ("ruleSelector", "ruleNamespaceSelector"),
            )
        ):
            matching.append(item)
    if items and len(matching) != 1:
        raise RuntimeError(
            "monitoring: existing Prometheus selectors are incompatible or ambiguous; no second instance will be created."
        )
    return matching[0] if matching else None


# Inputs: resolved config and existing Prometheus. Outputs: its Ready StatefulSet.
# Purpose: verify actual Prometheus Pods carry the labels used by network policies. Dependencies: live StatefulSet/Pod GETs.
def _prometheus_ready(config, item):
    namespace, name = item["metadata"]["namespace"], item["metadata"]["name"]
    statefulset = runtime._get(config, "statefulsets.apps", "prometheus-" + name, namespace)
    pods = runtime._get(config, "pods", namespace=namespace)["items"]
    ready = [
        pod
        for pod in pods
        if pod["metadata"].get("labels", {}).get("prometheus") == name
        and _matches(pod["metadata"].get("labels", {}), config["monitoring"]["pod_labels"])
        and runtime._condition(pod)
    ]
    if (
        not statefulset
        or not controller_ready(statefulset)
        or len(ready) < item["spec"].get("replicas", 1)
    ):
        raise RuntimeError("monitoring: Prometheus rollout or configured Pod labels are not ready.")
    return statefulset


# Inputs: resolved config and cluster component. Outputs: verified installation objects.
# Purpose: share strict dependency checks with ordinary platform inspection. Dependencies: read-only Kubernetes API access.
def installed(config, component):
    deployments = runtime._get(config, "deployments.apps", all_namespaces=True)["items"]
    operator = _operator(_operators(deployments, component), component, config)
    objects = [operator, *_crds(config, component)]
    if component == "monitoring":
        prometheus = _prometheus(config)
        if prometheus is None:
            raise RuntimeError(
                "monitoring: a Prometheus instance is required; prepare prerequisites first."
            )
        objects.extend([prometheus, _prometheus_ready(config, prometheus)])
    return objects


# Inputs: resolved config and asset name. Outputs: namespaced, ownership-labeled resource documents.
# Purpose: render fixed assets without introducing a template engine or runtime YAML dependency. Dependencies: packaged JSON.
def _documents(config, name):
    component = "strimzi" if name == "strimzi" else "monitoring"
    namespace = config["kafka" if component == "strimzi" else "monitoring"]["namespace"]
    path = ASSETS / f"{name}.json"
    if sha256(path) != VERSIONS["prerequisites"][name]["sha256"]:
        raise RuntimeError(f"Packaged prerequisite asset checksum mismatch: {name}.")
    text = path.read_text(encoding="utf-8")
    for token, value in {
        "__NAMESPACE__": namespace,
        "__ENVIRONMENT__": config["environment_id"],
        "__RELEASE__": config["monitoring"]["release"],
    }.items():
        text = text.replace(token, value)
    documents = json.loads(text)["items"]
    documents.insert(0, {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})
    for document in documents:
        metadata = document["metadata"]
        metadata.setdefault("labels", {}).update(config["derived"]["labels"])
        metadata["labels"]["local-k8s-platform/prerequisite"] = (
            "namespace" if document["kind"] == "Namespace" else component
        )
        if document["kind"] == "Prometheus":
            document["spec"].setdefault("podMetadata", {})["labels"] = config["monitoring"][
                "pod_labels"
            ]
    return documents


# Inputs: candidate executable. Outputs: supported OpenSSL identity or None.
# Purpose: check the certificate capabilities once before credential operations. Dependencies: local OpenSSL help/version commands.
def _openssl_candidate(path):
    if not path or not Path(path).is_file():
        return None
    result = run([path, "version"])
    if result.returncode or not result.stdout.startswith("OpenSSL "):
        return None
    for command, flags in (
        ("req", ("-addext",)),
        ("x509", ("-checkhost", "-checkend")),
        ("pkey", ("-pubout",)),
    ):
        help_result = run([path, command, "-help"])
        if not all(flag in help_result.stdout + help_result.stderr for flag in flags):
            return None
    return {
        "path": str(Path(path).resolve()),
        "version": result.stdout.strip(),
        "sha256": sha256(Path(path)),
    }


# Inputs: none. Outputs: supported OpenSSL identity or None.
# Purpose: resolve PATH first, then a keg-only Homebrew installation without changing the shell. Dependencies: local executables.
def find_openssl():
    candidate = _openssl_candidate(shutil.which("openssl"))
    if candidate:
        return candidate
    brew = shutil.which("brew")
    if brew:
        result = run([brew, "--prefix", "openssl@3"])
        if result.returncode == 0 and Path(result.stdout.strip()).is_absolute():
            return _openssl_candidate(Path(result.stdout.strip()) / "bin" / "openssl")
    return None


# Inputs: none. Outputs: fixed package command with absolute executable paths.
# Purpose: install OpenSSL through an existing package manager only. Dependencies: Homebrew, apt-get or dnf; optional non-interactive sudo.
def _openssl_install():
    if sys.platform == "darwin":
        manager, arguments = shutil.which("brew"), ["install", "openssl@3"]
    elif sys.platform.startswith("linux"):
        manager = shutil.which("apt-get") or shutil.which("dnf")
        arguments = (
            ["install", "--yes", "--no-install-recommends", "openssl"]
            if manager and Path(manager).name == "apt-get"
            else ["install", "--assumeyes", "openssl"]
        )
    else:
        manager = None
    if not manager:
        raise RuntimeError(
            "OpenSSL is missing; install Homebrew on macOS, or use an existing apt-get/dnf installation on Linux."
        )
    command = [str(Path(manager).resolve()), *arguments]
    if sys.platform.startswith("linux") and os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            raise RuntimeError(
                "OpenSSL installation needs root access or existing sudo; no elevation was attempted."
            )
        command = [str(Path(sudo).resolve()), "-n", *command]
    if sys.platform == "darwin":
        command = [
            "/usr/bin/env",
            "HOMEBREW_NO_AUTO_UPDATE=1",
            "HOMEBREW_NO_INSTALL_CLEANUP=1",
            *command,
        ]
    return command


# Inputs: resolved config and resource reference. Outputs: whether creation is authorized.
# Purpose: evaluate permissions before CRD discovery exists. Dependencies: non-persistent Kubernetes authorization reviews.
def _can_create(config, reference):
    resource, _, group = reference["resource"].partition(".")
    review = {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {
            "resourceAttributes": {
                "verb": "create",
                "group": group,
                "resource": resource,
                "namespace": reference["namespace"] or "",
            }
        },
    }
    # kubectl auth can-i can lose the API group when a CRD is not discoverable yet.
    result = run(
        runtime._kube(config)
        + [
            "create",
            "--raw",
            "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
            "-f",
            "-",
        ],
        input_text=json.dumps(review),
    )
    return (
        result.returncode == 0
        and json.loads(result.stdout).get("status", {}).get("allowed") is True
    )


# Inputs: resolved config, documents and snapshot under construction. Outputs: none.
# Purpose: list absent resources and accept only unchanged owned partial resources. Dependencies: live GET and permission checks.
def _plan_documents(config, documents, snapshot):
    namespaces = {
        item["metadata"]["namespace"] for item in documents if "namespace" in item["metadata"]
    }
    namespaces.update(item["metadata"]["name"] for item in documents if item["kind"] == "Namespace")
    existing_namespaces = {name: runtime._get(config, "namespaces", name) for name in namespaces}
    permissions = set()
    for document in documents:
        reference = _reference(document)
        key = json.dumps(reference, sort_keys=True)
        if key in snapshot["observations"] or any(
            change.get("reference") == reference for change in snapshot["changes"]
        ):
            continue
        namespace = reference["namespace"]
        ns_present = namespace is None or existing_namespaces[namespace] is not None
        api_present = (
            document["kind"] != "Prometheus"
            or runtime._get(
                config, RESOURCES["CustomResourceDefinition"], "prometheuses.monitoring.coreos.com"
            )
            is not None
        )
        live = (
            existing_namespaces[reference["name"]]
            if document["kind"] == "Namespace"
            else (runtime._get(config, **reference) if ns_present and api_present else None)
        )
        if live:
            if document["kind"] != "Namespace" and (
                not _owned(live, config) or not _matches_document(live, document)
            ):
                raise RuntimeError(
                    f"Prerequisite collision or drift: {reference['resource']}/{reference['name']}; no resources were adopted or replaced."
                )
            snapshot["observations"][key] = _observation(live)
        else:
            permission_key = (reference["resource"], namespace)
            if permission_key not in permissions:
                if not _can_create(config, reference):
                    raise RuntimeError(
                        f"Cannot create prerequisite {reference['resource']}; review cluster permissions."
                    )
                permissions.add(permission_key)
            snapshot["changes"].append(
                {"action": "create", "reference": reference, "document": document}
            )


# Inputs: resolved config, component, Deployments and snapshot. Outputs: none.
# Purpose: reuse external Operators, resume owned resources, and plan a missing Prometheus instance. Dependencies: official assets and live inventory.
def _cluster_plan(config, component, deployments, snapshot):
    candidates = _operators(deployments, component)
    operator = _operator(candidates, component, config, ready=False) if candidates else None
    if operator is not None and not _owned(operator, config):
        if not controller_ready(operator):
            raise RuntimeError(f"{component}: existing Operator is not ready.")
        reused = [operator, *_crds(config, component)]
        for item in reused:
            snapshot["observations"][json.dumps(_reference(item), sort_keys=True)] = _observation(
                item
            )
        snapshot["reused"].append(component + "-operator")
        snapshot["waits"].extend(_reference(item) for item in reused)
    else:
        documents = _documents(config, component)
        _plan_documents(config, documents, snapshot)
        snapshot["waits"].extend(
            _reference(item)
            for item in documents
            if item["kind"] in {"CustomResourceDefinition", "Deployment"}
        )
    if component == "monitoring":
        crd = runtime._get(
            config, RESOURCES["CustomResourceDefinition"], "prometheuses.monitoring.coreos.com"
        )
        prometheus = _prometheus(config) if crd else None
        if prometheus is not None and not _owned(prometheus, config):
            _prometheus_ready(config, prometheus)
            snapshot["observations"][json.dumps(_reference(prometheus), sort_keys=True)] = (
                _observation(prometheus)
            )
            snapshot["reused"].append("prometheus")
        else:
            deployment = operator or next(
                item for item in documents if item["kind"] == "Deployment"
            )
            container = next(
                item
                for item in deployment["spec"]["template"]["spec"]["containers"]
                if "prometheus-operator" in item.get("image", "")
            )
            if _flag(container, "--prometheus-instance-selector") or _flag(
                container, "--controller-id"
            ):
                raise RuntimeError(
                    "Existing Operator filters require a separate Prometheus instance configuration."
                )
            reloader = _flag(container, "--config-reloader-memory-request", "50Mi")
            reloader_memory = 0 if reloader == "0" else memory_mib(reloader)
            prometheus_documents = _documents(config, "prometheus")
            _plan_documents(config, prometheus_documents, snapshot)
            if any(
                change.get("document", {}).get("kind") == "Prometheus"
                for change in snapshot["changes"]
            ):
                snapshot["new_reloader_memory_mib"] = reloader_memory
                snapshot["new_storage_mib"] = 512
            snapshot["waits"].append(
                {
                    "resource": "statefulsets.apps",
                    "name": "prometheus-" + config["environment_id"] + "-prometheus",
                    "namespace": config["monitoring"]["namespace"],
                }
            )


# Inputs: resolved config and current Deployments. Outputs: memory requested by already managed prerequisites.
# Purpose: preserve the budget across separate component installs. Dependencies: live ownership and Prometheus resource requests.
def _managed_memory(config, deployments):
    memory = sum(
        _memory(item)
        for item in deployments
        if _owned(item, config)
        and item["metadata"].get("labels", {}).get("local-k8s-platform/prerequisite")
        in {"strimzi", "monitoring"}
    )
    crd = runtime._get(
        config, RESOURCES["CustomResourceDefinition"], "prometheuses.monitoring.coreos.com"
    )
    if crd:
        operators = _operators(deployments, "monitoring")
        reloader = "16Mi"
        if len(operators) == 1:
            container = next(
                item
                for item in operators[0]["spec"]["template"]["spec"]["containers"]
                if "prometheus-operator" in item.get("image", "")
            )
            reloader = _flag(container, "--config-reloader-memory-request", "50Mi")
        for item in runtime._get(config, RESOURCES["Prometheus"], all_namespaces=True)["items"]:
            if _owned(item, config):
                memory += _memory(item) + (0 if reloader == "0" else memory_mib(reloader))
    return memory


# Inputs: fixed Deployment or Prometheus document. Outputs: requested memory in MiB.
# Purpose: account for newly managed prerequisites in the configured budget. Dependencies: normalized asset quantities.
def _memory(document):
    if document["kind"] == "Deployment":
        return sum(
            memory_mib(container["resources"]["requests"]["memory"])
            for container in document["spec"]["template"]["spec"]["containers"]
        )
    if document["kind"] == "Prometheus":
        return memory_mib(document["spec"]["resources"]["requests"]["memory"])
    return 0


# Inputs: resolved config, private workspace and selected component. Outputs: deterministic changes and live bindings.
# Purpose: build a concrete install plan without requiring the missing dependencies. Dependencies: local tools and foundational inspection.
def _snapshot(config, workspace, component):
    snapshot = {
        "changes": [],
        "observations": {},
        "reused": [],
        "waits": [],
        "components": [],
        "managed_memory_mib": 0,
        "new_storage_mib": 0,
        "new_reloader_memory_mib": 0,
    }
    selected = (
        [component]
        if component != "all"
        else [
            name
            for name, enabled in (
                ("openssl", config["postgres"]["enabled"]),
                ("strimzi", config["kafka"]["enabled"]),
                ("monitoring", config["monitoring"]["enabled"]),
            )
            if enabled
        ]
    )
    cluster_components = [name for name in selected if name != "openssl"]
    if cluster_components and config["cluster"]["mode"] == "create":
        if not runtime._bind(config, workspace, runtime._identity(config)):
            raise RuntimeError("The prerequisite target must match the workspace binding.")
    if "openssl" in selected:
        snapshot["host"] = {
            "hostname": socket.gethostname(),
            "platform": sys.platform,
            "machine": os.uname().machine,
            "uid": os.getuid(),
        }
        tool = find_openssl()
        snapshot["openssl"] = tool
        if tool:
            snapshot["reused"].append("openssl")
        else:
            snapshot["changes"].append({"action": "install-openssl", "command": _openssl_install()})
    deployments = (
        runtime._get(config, "deployments.apps", all_namespaces=True)["items"]
        if cluster_components
        else []
    )
    for name in cluster_components:
        _cluster_plan(config, name, deployments, snapshot)
        snapshot["components"].append(name)
    if snapshot["components"]:
        forecasts = []
        for change in snapshot["changes"]:
            document = change.get("document", {})
            if document.get("kind") in {"Deployment", "Prometheus"}:
                prometheus = document["kind"] == "Prometheus"
                forecasts.append(
                    {
                        "name": document["metadata"]["namespace"]
                        + "/"
                        + document["metadata"]["name"],
                        "memory_mib": _memory(document)
                        + (snapshot["new_reloader_memory_mib"] if prometheus else 0),
                        "storage_mib": 0,
                        "ephemeral_mib": 512 if prometheus else 0,
                    }
                )
        new_memory = sum(item["memory_mib"] for item in forecasts)
        snapshot["managed_memory_mib"] = _managed_memory(config, deployments) + new_memory
        required = config["derived"]["requests_memory_mib"] + snapshot["managed_memory_mib"]
        if required > config["budget"]["max_memory_requests_mib"]:
            raise RuntimeError(
                f"Prerequisites and data services request {required}Mi; increase budget.max_memory_requests_mib before planning."
            )
        inventory = runtime.inspect(
            config,
            workspace,
            check_dependencies=False,
            workloads=forecasts,
        )
        if inventory["status"] != "pass":
            raise RuntimeError(
                f"Foundational inspection is blocked; review {workspace / 'reports' / 'inventory.json'} before preparing prerequisites."
            )
        snapshot["identity"] = inventory["identity"]
    snapshot["changes"].sort(
        key=lambda change: (
            -1 if change["action"] == "install-openssl" else ORDER[change["document"]["kind"]]
        )
    )
    return snapshot


# Inputs: none. Outputs: packaged runtime source checksums.
# Purpose: invalidate reviews after code, pin or manifest changes. Dependencies: maintained scripts and assets.
def _sources():
    paths = [
        *sorted((ROOT / "scripts").glob("*.py")),
        *sorted(ASSETS.glob("*.json")),
        ROOT / "assets" / "infra" / "versions.json",
    ]
    return {str(path.relative_to(ROOT)): sha256(path) for path in paths}


# Inputs: resolved config and reviewed wait references. Outputs: none.
# Purpose: wait for APIs before workloads and for current controller rollouts. Dependencies: bounded read-only kubectl operations.
def _wait(config, references):
    # First image pulls on a local VM can take minutes; 220MB took 191s on krunkit.
    limit = f"--timeout={READY_TIMEOUT_SECONDS}s"
    for reference in references:
        resource, name = reference["resource"], reference["name"]
        if resource == "statefulsets.apps":
            creation = [
                "wait",
                "--for=create",
                f"{resource}/{name}",
                "--namespace",
                reference["namespace"],
                limit,
            ]
            if run(runtime._kube(config) + creation, timeout=READY_TIMEOUT_SECONDS + 30).returncode:
                raise RuntimeError(f"Prometheus has not created {name}; inspect Operator events.")
        arguments = (
            ["wait", "--for=condition=Established", f"{resource}/{name}", limit]
            if resource == RESOURCES["CustomResourceDefinition"]
            else ["rollout", "status", f"{resource}/{name}", limit]
        )
        if reference["namespace"]:
            arguments += ["--namespace", reference["namespace"]]
        if run(runtime._kube(config) + arguments, timeout=READY_TIMEOUT_SECONDS + 30).returncode:
            raise RuntimeError(
                f"Prerequisite readiness is pending after {READY_TIMEOUT_SECONDS}s: {resource}/{name}. "
                "Check its Pod events; an image may still be pulling or unavailable for the node platform."
            )


# Inputs: resolved config, workspace and saved review. Outputs: installation result with completed references.
# Purpose: consume the plan before writes and preserve partial outcomes without rollback. Dependencies: kubectl create and fixed package commands.
def _execute(config, workspace, review):
    snapshot, plan_id = review["snapshot"], review["plan_id"]
    record = {
        "status": "attempting",
        "plan_id": plan_id,
        "results": [],
        "runtime_acceptance": "pending",
    }
    consumed = workspace / "plans" / "prerequisites" / f"consumed-{plan_id}.json"
    report = workspace / "reports" / f"prerequisites-{review['component']}.json"
    runtime._remember_objects(workspace, runtime._reported_objects(report))
    write_json(consumed, record)
    write_json(report, record)
    crds = [
        item
        for item in snapshot["waits"]
        if item["resource"] == RESOURCES["CustomResourceDefinition"]
    ]
    waited = False
    try:
        for change in snapshot["changes"]:
            record["results"].append(
                {
                    "action": change["action"],
                    "reference": change.get("reference"),
                    "status": "attempting",
                }
            )
            write_json(report, record)
            if change["action"] == "install-openssl":
                if run(change["command"], timeout=600).returncode:
                    raise RuntimeError(
                        "OpenSSL package installation failed; check package-manager access and non-interactive privileges before a new plan."
                    )
                tool = find_openssl()
                if tool is None:
                    raise RuntimeError(
                        "Package command completed but a supported OpenSSL is still unavailable."
                    )
                record["openssl"] = tool
            else:
                document, reference = change["document"], change["reference"]
                if document["kind"] not in {"Namespace", "CustomResourceDefinition"} and not waited:
                    _wait(config, crds)
                    waited = True
                if (
                    sha256(Path(config["cluster"]["kubeconfig"]))
                    != snapshot["identity"]["kubeconfig_sha256"]
                ):
                    raise RuntimeError("Kubeconfig changed during prerequisite execution.")
                # A named create remains atomic if an object appears after this read.
                if runtime._get(config, **reference) is not None:
                    raise RuntimeError(
                        "A planned prerequisite creation now exists; prepare a fresh plan."
                    )
                result = run(
                    runtime._kube(config) + ["create", "-f", "-", "-o", "name"],
                    input_text=json.dumps(document),
                )
                if result.returncode:
                    raise RuntimeError(
                        f"Prerequisite creation failed: {reference['resource']}/{reference['name']}."
                    )
            record["results"][-1]["status"] = (
                "created" if change["action"] == "create" else "installed"
            )
            if change["action"] == "create":
                runtime._remember_objects(
                    workspace, [{"source": "prerequisites", **change["reference"], "uid": None}]
                )
            write_json(report, record)
        if not waited:
            _wait(config, crds)
        _wait(
            config,
            [
                item
                for item in snapshot["waits"]
                if item["resource"] != RESOURCES["CustomResourceDefinition"]
            ],
        )
        for component in snapshot["components"]:
            installed(config, component)
        record["status"] = "installed"
    except (RuntimeError, ValueError, OSError, KeyError):
        record["status"] = (
            "uncertain"
            if any(item["status"] == "attempting" for item in record["results"])
            else "failed"
        )
        write_json(consumed, record)
        write_json(report, record)
        raise
    write_json(consumed, record)
    write_json(report, record)
    return record


# Inputs: resolved config, workspace, component, optional reviewed ID and execution flag. Outputs: safe plan/preview/result.
# Purpose: expose one prerequisite workflow after CLI validation. Dependencies: private plans and current external evidence.
def prerequisites(config, workspace, component="all", plan_id=None, execute=False):
    directory = workspace / "plans" / "prerequisites"
    if plan_id is None:
        review = {
            "component": component,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "config_sha256": runtime._digest(config),
            "source_hashes": _sources(),
            "snapshot": _snapshot(config, workspace, component),
        }
        review["plan_id"] = runtime._digest(review)
        write_json(directory / f"{review['plan_id']}.json", review)
    else:
        review = read_json(directory / f"{plan_id}.json")
        recorded = review.pop("plan_id")
        if recorded != plan_id or runtime._digest(review) != plan_id:
            raise RuntimeError("Prerequisite plan ID or contents changed; prepare a fresh plan.")
        review["plan_id"] = recorded
        if (directory / f"consumed-{plan_id}.json").exists():
            raise RuntimeError(
                "Prerequisite plan was consumed; inspect partial results and prepare a fresh plan."
            )
        if (
            review["component"] != component
            or review["config_sha256"] != runtime._digest(config)
            or review["source_hashes"] != _sources()
        ):
            raise RuntimeError(
                "Prerequisite selection, configuration or sources changed; prepare a fresh plan."
            )
        if execute:
            if _snapshot(config, workspace, component) != review["snapshot"]:
                raise RuntimeError(
                    "Prerequisite target, ownership or live inputs changed; prepare a fresh plan."
                )
            return _execute(config, workspace, review)
    return {
        "status": "planned" if plan_id is None else "preview",
        "plan_id": review["plan_id"],
        "review": str(directory / f"{review['plan_id']}.json"),
        "target": review["snapshot"].get("identity", review["snapshot"].get("host")),
        "reused": review["snapshot"]["reused"],
        "changes": [
            {key: value for key, value in change.items() if key != "document"}
            for change in review["snapshot"]["changes"]
        ],
        "managed_memory_mib": review["snapshot"]["managed_memory_mib"],
        "packaged_versions": {
            name: VERSIONS["prerequisites"][name]["version"]
            for name in (
                review["snapshot"]["components"]
                + (["prometheus"] if "monitoring" in review["snapshot"]["components"] else [])
            )
        },
        "openssl": review["snapshot"].get("openssl"),
        "requires_execute": bool(review["snapshot"]["changes"]),
    }
