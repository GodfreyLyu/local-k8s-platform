"""Purpose: estimate Pod memory and volume placement from read-only Kubernetes capacity evidence."""

import re
from decimal import ROUND_CEILING, Decimal


# Inputs: a Kubernetes memory quantity. Outputs: its size in fractional MiB.
# Purpose: interpret live Ki/Mi/Gi, decimal and exponent quantities. Dependencies: Decimal.
def quantity_mib(value):
    match = re.fullmatch(r"(\d+(?:\.\d*)?|\.\d+)([eE][+-]?\d+|[KMGTPE]i|[kKMGTPE]|m)?", str(value))
    if not match:
        raise ValueError("Unsupported Kubernetes memory quantity")
    number, suffix = Decimal(match[1]), match[2] or ""
    if re.fullmatch(r"[eE][+-]?\d+", suffix):
        factor = Decimal(10) ** int(suffix[1:])
    elif suffix.endswith("i"):
        factor = Decimal(1024) ** ("KMGTPE".index(suffix[0]) + 1)
    else:
        factor = (
            Decimal("0.001")
            if suffix == "m"
            else Decimal(1000) ** ("kMGTPE".index(suffix.replace("K", "k")) + 1)
            if suffix
            else Decimal(1)
        )
    return number * factor / 1048576


# Inputs: a container resource mapping. Outputs: requested memory in fractional MiB.
# Purpose: account for defaulted limit-only containers. Dependencies: Kubernetes resource conventions.
def _container_memory(container):
    resources = container.get("resources", {})
    return quantity_mib(
        resources.get("requests", {}).get("memory", resources.get("limits", {}).get("memory", "0"))
    )


# Inputs: an assigned Pod. Outputs: effective memory request rounded up to MiB.
# Purpose: include ordinary containers, init peaks, restartable sidecars and Pod overhead. Dependencies: live Pod specs.
def pod_memory(pod):
    spec = pod.get("spec", {})
    steady = sum((_container_memory(item) for item in spec.get("containers", [])), Decimal(0))
    sidecars = peak = Decimal(0)
    for item in spec.get("initContainers", []):
        memory = _container_memory(item)
        if item.get("restartPolicy") == "Always":
            sidecars += memory
            peak = max(peak, sidecars)
        else:
            peak = max(peak, sidecars + memory)
    pod_level = quantity_mib(spec.get("resources", {}).get("requests", {}).get("memory", "0"))
    requested = max(steady + sidecars, peak, pod_level) + quantity_mib(
        spec.get("overhead", {}).get("memory", "0")
    )
    return int(requested.to_integral_value(rounding=ROUND_CEILING))


# Inputs: Pod forecasts, allowed nodes and per-node remaining budgets. Outputs: placement or None, plus search-limit status.
# Purpose: find a joint memory/disk assignment without counting the same capacity twice. Dependencies: a bounded in-memory search.
def _place(workloads, candidates, memory, disk):
    ordered = sorted(
        workloads,
        key=lambda item: (len(candidates[item["name"]]), -item["memory_mib"], -item["disk_mib"]),
    )
    assignments = []
    attempts = 0

    # Inputs: next forecast index. Outputs: whether all remaining Pods fit.
    # Purpose: backtrack small local-cluster assignments. Dependencies: private budget copies and a 10000-step limit.
    def search(index):
        nonlocal attempts
        attempts += 1
        if attempts > 10000:
            return False
        if index == len(ordered):
            return True
        item = ordered[index]
        for node in sorted(
            candidates[item["name"]], key=lambda name: (-memory[name], -disk[name], name)
        ):
            if memory[node] < item["memory_mib"] or disk[node] < item["disk_mib"]:
                continue
            memory[node] -= item["memory_mib"]
            disk[node] -= item["disk_mib"]
            assignments.append(
                {
                    "name": item["name"],
                    "node": node,
                    "memory_mib": item["memory_mib"],
                    "disk_mib": item["disk_mib"],
                }
            )
            if search(index + 1):
                return True
            assignments.pop()
            memory[node] += item["memory_mib"]
            disk[node] += item["disk_mib"]
        return False

    return (assignments if search(0) else None), attempts > 10000


# Inputs: Nodes, assigned Pods, node measurements, resolved forecasts, storage nodes and disk reserve.
# Outputs: memory/disk checks, estimated assignments and remaining capacity. Purpose: assess local workload placement. Dependencies: measured node stats.
def assess(nodes, pods, measurements, workloads, storage_nodes, disk_reserve):
    requested = {item["metadata"]["name"]: 0 for item in nodes}
    for pod in pods:
        name = pod.get("spec", {}).get("nodeName")
        if name in requested and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}:
            requested[name] += pod_memory(pod)
    measured = {item["name"]: dict(item) for item in measurements}
    eligible = []
    for node in nodes:
        name = node["metadata"]["name"]
        measured[name]["memory_requested_mib"] = requested[name]
        measured[name]["memory_schedulable_mib"] = max(
            0, int(quantity_mib(node["status"]["allocatable"]["memory"])) - requested[name]
        )
        taints = node.get("spec", {}).get("taints", [])
        if not node.get("spec", {}).get("unschedulable") and not any(
            item.get("effect") in {"NoSchedule", "NoExecute"} for item in taints
        ):
            eligible.append(name)
    jobs = [
        {**item, "disk_mib": item.get("storage_mib", 0) + item.get("ephemeral_mib", 0)}
        for item in workloads
    ]
    candidates = {
        item["name"]: [
            name
            for name in eligible
            if (not item.get("storage_mib", 0) or not storage_nodes or name in storage_nodes)
            # DaemonSet forecasts name the one node their Pod must run on.
            and ("nodes" not in item or name in item["nodes"])
        ]
        for item in jobs
    }
    available = {
        name: min(measured[name]["memory_available_mib"], measured[name]["memory_schedulable_mib"])
        for name in eligible
    }
    memory = {name: max(0, value - 128) for name, value in available.items()}
    disk = {name: measured[name]["disk_available_mib"] - disk_reserve for name in eligible}
    allocations, exhausted = _place(jobs, candidates, dict(memory), dict(disk))
    disk_jobs = [{**item, "memory_mib": 0} for item in jobs]
    disk_fit, _ = _place(disk_jobs, candidates, {name: 0 for name in eligible}, dict(disk))
    total = sum(item["memory_mib"] for item in jobs)
    headroom = 256 if total else 0
    checks = {
        "measured_memory": sum(measured[name]["memory_available_mib"] for name in eligible)
        >= total + headroom,
        "scheduler_memory": sum(measured[name]["memory_schedulable_mib"] for name in eligible)
        >= total + headroom,
        "single_node_memory": all(
            any(memory[name] >= item["memory_mib"] for name in candidates[item["name"]])
            for item in jobs
        ),
        "measured_disk": disk_fit is not None,
        "pod_placement": allocations is not None,
    }
    for item in allocations or []:
        node = measured[item["node"]]
        node["planned_memory_mib"] = node.get("planned_memory_mib", 0) + item["memory_mib"]
        node["planned_disk_mib"] = node.get("planned_disk_mib", 0) + item["disk_mib"]
    for node in measured.values():
        node["memory_after_requests_mib"] = node["memory_available_mib"] - node.get(
            "planned_memory_mib", 0
        )
        node["memory_schedulable_after_requests_mib"] = node["memory_schedulable_mib"] - node.get(
            "planned_memory_mib", 0
        )
        node["disk_after_requests_mib"] = node["disk_available_mib"] - node.get(
            "planned_disk_mib", 0
        )
    return {
        "nodes": list(measured.values()),
        "new_memory_requests_mib": total,
        "new_storage_mib": sum(item["disk_mib"] for item in jobs),
        "workloads": jobs,
        "allocations": allocations,
        "checks": checks,
        "search_limit_reached": exhausted,
        "scope": "Memory/disk estimate; CPU, admission, images and runtime acceptance are separate",
    }
