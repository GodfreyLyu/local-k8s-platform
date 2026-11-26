"""Purpose: collect cluster data, native VM, and workspace removal evidence for reviewed deletion."""

import os
import re
import stat
from pathlib import Path

import runtime
import secret_metadata
from io_utils import read_json, run

SYSTEM_NAMESPACES = {"default", "kube-system", "kube-public", "kube-node-lease"}
# These APIs expose transient coordination, events, or synthesized health and metrics.
TRANSIENT = {
    "componentstatuses", "events", "events.events.k8s.io", "leases.coordination.k8s.io",
    "nodes.metrics.k8s.io", "pods.metrics.k8s.io",
}
STORAGE = {"persistentvolumeclaims", "persistentvolumes"}
CONTROLLERS = {
    "deployments.apps": "Deployment",
    "replicasets.apps": "ReplicaSet",
    "daemonsets.apps": "DaemonSet",
    "statefulsets.apps": "StatefulSet",
    "pods": "Pod",
    "jobs.batch": "Job",
    "cronjobs.batch": "CronJob",
    "kafkas.kafka.strimzi.io": "Kafka",
    "kafkanodepools.kafka.strimzi.io": "KafkaNodePool",
    "strimzipodsets.core.strimzi.io": "StrimziPodSet",
    "prometheuses.monitoring.coreos.com": "Prometheus",
}
CLAIM_CONTROLLERS = {"StatefulSet", "Pod", "Kafka", "KafkaNodePool", "StrimziPodSet"}
REPORTS = {
    "cluster", "cluster-delete", "cluster-deleted", "cluster-reconcile", "cluster-recreated",
    "acceptance-postgres", "acceptance-kafka",
    "credentials-postgres", "credentials-kafka", "gpu-smoke", "interfaces", "inventory",
    "verification",
}
REPORTS.update(f"prerequisites-{name}" for name in ("all", "strimzi", "monitoring", "openssl"))
PLAN_FILES = {
    "init-log.json", "plan-log.json", "apply-log.json", "review.json", "plan.tfplan",
    "failed.json", "consumed.json",
}
PLAN_GROUPS = {"acceptance", "cluster", "cluster-delete", "prerequisites"}
STAGE_ROOT = runtime.ROOT / "assets" / "infra" / "stacks"


# Inputs: cluster config and resource scope. Outputs: sorted listable resource names.
# Purpose: discover user and custom data and reject incomplete API discovery. Dependencies: kubectl.
def _resources(config, namespaced):
    result = run(
        runtime._kube(config) + [
            "api-resources", "--cached=false", "--verbs=list",
            f"--namespaced={str(namespaced).lower()}", "-o", "name",
        ],
        timeout=25,
    )
    names = result.stdout.split()
    core = {"configmaps", "secrets", "persistentvolumeclaims"} if namespaced else {
        "namespaces", "persistentvolumes"
    }
    if (
        result.returncode
        or not core.issubset(names)
        or any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name) for name in names)
    ):
        raise RuntimeError("Cannot establish a complete Kubernetes API inventory.")
    return sorted(set(names) - TRANSIENT)


# Inputs: configuration. Outputs: live objects by resource, namespace, and name.
# Purpose: inventory one complete cluster without reading Secret data. Dependencies: discovery and API.
def _live_objects(config):
    objects = {}
    for namespaced in (False, True):
        for resource in _resources(config, namespaced):
            if resource == "secrets":
                with secret_metadata.proxy(config) as socket:
                    items = secret_metadata.list_all(socket)
            else:
                listing = runtime._get(config, resource, all_namespaces=namespaced)
                items = (listing or {}).get("items")
                if not isinstance(items, list) or (listing.get("metadata") or {}).get("continue"):
                    raise RuntimeError(f"Cannot establish a complete {resource} inventory.")
            for item in items:
                metadata = item["metadata"]
                name, uid = metadata["name"], metadata["uid"]
                namespace = metadata.get("namespace", "")
                key = (resource, namespace, name)
                if (
                    not all(isinstance(value, str) and value for value in (name, uid))
                    or bool(namespace) != namespaced
                    or key in objects
                ):
                    raise RuntimeError(f"Cannot establish unique live identities for {resource}.")
                objects[key] = item
    return objects


# Inputs: live objects, recorded references, and environment labels. Outputs: verified managed keys.
# Purpose: require object evidence or an unbroken live controller UID chain. Dependencies: metadata.
def _managed_objects(objects, references, labels):
    managed = set()
    for key, uid in references.items():
        item = objects.get(key)
        if item is None:
            continue
        metadata = item["metadata"]
        observed = metadata.get("labels") or {}
        if uid and uid == metadata["uid"]:
            if observed.get(runtime.ENVIRONMENT_LABEL, labels[runtime.ENVIRONMENT_LABEL]) == labels[runtime.ENVIRONMENT_LABEL]:
                managed.add(key)
        elif not uid and all(observed.get(label) == value for label, value in labels.items()):
            managed.add(key)
    by_uid = {item["metadata"]["uid"]: key for key, item in objects.items()}
    while True:
        added = set()
        for key in objects.keys() - managed:
            if key[0] in {"namespaces", "persistentvolumes"}:
                continue
            for owner in objects[key]["metadata"].get("ownerReferences") or []:
                parent = by_uid.get(owner.get("uid"))
                kind = owner.get("kind")
                if (
                    owner.get("controller") is True and parent in managed
                    and parent[1] in ("", key[1]) and parent[2] == owner.get("name")
                    and kind and CONTROLLERS.get(parent[0]) == kind
                    and (key[0] != "persistentvolumeclaims" or kind in CLAIM_CONTROLLERS)
                ):
                    added.add(key)
        if not added:
            break
        managed.update(added)
    # A generated PV is managed only for a verified, currently bound claim.
    for key, item in objects.items():
        if key[0] != "persistentvolumes" or item.get("status", {}).get("phase") != "Bound":
            continue
        claim = item.get("spec", {}).get("claimRef") or {}
        claim_key = ("persistentvolumeclaims", claim.get("namespace"), claim.get("name"))
        if claim_key in managed and claim.get("uid") == objects[claim_key]["metadata"]["uid"]:
            if objects[claim_key].get("spec", {}).get("volumeName") == key[2]:
                managed.add(key)
    return managed


# Inputs: configuration and workspace. Outputs: names, UIDs and unmanaged data for plan review.
# Purpose: classify data by live object evidence, independently of namespace ownership.
def data_loss(config, workspace):
    references = {}
    for item in runtime._state_resources(workspace) + runtime._recorded_objects(workspace):
        references.setdefault(
            (item["resource"], item["namespace"] or "", item["name"]), item["uid"]
        )
    objects = _live_objects(config)
    managed = _managed_objects(objects, references, config["derived"]["labels"])
    unmanaged = {"namespaces": [], "persistent_volume_claims": [], "persistent_volumes": [], "objects": []}
    inventory = []
    for key, item in sorted(objects.items()):
        resource, namespace, name = key
        metadata = item["metadata"]
        evidence = {"resource": resource, "namespace": namespace, "name": name, "uid": metadata["uid"]}
        if resource in STORAGE:
            spec = item.get("spec") or {}
            if resource == "persistentvolumeclaims":
                evidence["volume"] = spec.get("volumeName")
            else:
                claim = spec.get("claimRef") or {}
                evidence["claim"] = {field: claim.get(field) for field in ("namespace", "name", "uid")}
        inventory.append(evidence)
        if key in managed or (resource == "namespaces" and name in SYSTEM_NAMESPACES):
            continue
        if resource == "namespaces":
            unmanaged["namespaces"].append(name)
        elif resource == "persistentvolumeclaims":
            unmanaged["persistent_volume_claims"].append(f"{namespace}/{name}")
        elif resource == "persistentvolumes":
            unmanaged["persistent_volumes"].append(name)
        else:
            unmanaged["objects"].append(evidence)
    return {
        "managed_objects": [
            f"{resource} {namespace + '/' if namespace else ''}{name}"
            for resource, namespace, name in sorted(managed)
        ],
        "persistent_volume_claims": [
            f"{namespace}/{name}" for resource, namespace, name in sorted(objects)
            if resource == "persistentvolumeclaims"
        ],
        "objects": inventory,
        "unmanaged": unmanaged,
    }


# Inputs: a process ID from a validated machine record. Outputs: whether it can still exist.
# Purpose: probe native krunkit process removal independently of Minikube records. Dependencies: POSIX.
def _process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# Inputs: a recorded PID. Outputs: whether a krunkit process owns it. Dependencies: POSIX ps.
# Purpose: reject a stale machine PID that now identifies a different executable.
def _krunkit_process(pid):
    if not _process_exists(pid):
        return False
    process = run(["ps", "-p", str(pid), "-o", "comm="], timeout=10)
    return not process.returncode and Path(process.stdout.strip()).name == "krunkit"


# Inputs: reviewed Minikube home and profile name. Outputs: live driver removal evidence.
# Purpose: capture independent identifiers before Minikube can remove its records. Dependencies: driver.
def machine_evidence(home, name):
    profile = read_json(home / "profiles" / name / "config.json")
    driver = profile["Driver"]
    if driver != "krunkit":
        return {"driver": driver, "pids": None}
    nodes = [node["Name"] for node in profile["Nodes"]]
    if not nodes or any(not re.fullmatch(r"[a-z0-9-]*", node) for node in nodes):
        raise RuntimeError("Cannot establish the krunkit profile's machine names.")
    machines = [name if not node or len(nodes) == 1 else f"{name}-{node}" for node in nodes]
    pids = []
    for machine in machines:
        path = home / "machines" / machine / "krunkit.pid"
        pid_text = path.read_text().strip()
        if not re.fullmatch(r"[1-9][0-9]{0,9}", pid_text):
            raise RuntimeError(f"Cannot establish the krunkit process for {machine}.")
        pid = int(pid_text)
        if pid <= 1 or pid > 2147483647 or not _krunkit_process(pid) or pid in pids:
            raise RuntimeError(f"Cannot verify the running krunkit process for {machine}.")
        pids.append(pid)
    return {"driver": driver, "pids": pids}


# Inputs: saved driver evidence. Outputs: whether every identified VM is gone.
# Purpose: treat unsupported drivers and unconfirmed processes as uncertain. Dependencies: POSIX.
def machines_removed(evidence):
    pids = (evidence or {}).get("pids")
    return bool(pids) and all(
        isinstance(pid, int) and 1 < pid <= 2147483647 and not _process_exists(pid) for pid in pids
    )


# Inputs: workspace-relative parts, entry kind, and packaged stage names. Outputs: known artifact.
# Purpose: recognize the exact shapes written by the CLI, Terraform, or the credential helper.
def _artifact(parts, directory, stages):
    root, *tail = parts
    if not tail:
        if directory:
            return root in {"stages", "state", "plans", "reports", "credentials"}
        return root in {"environment.json", "cluster-binding.json"}
    if root == "reports" and not directory and len(tail) == 1:
        name = Path(tail[0])
        return name.suffix == ".json" and (
            name.stem in REPORTS or name.stem in {f"applied-{stage}" for stage in stages}
        )
    if root == "state" and not directory and len(tail) == 1:
        return tail[0] == "recorded-objects.json" or any(
            tail[0] in (f"{stage}.tfstate", f"{stage}.tfstate.backup") for stage in stages
        )
    if root == "credentials":
        return tail == ["postgres"] if directory else (
            len(tail) == 2 and tail[0] == "postgres"
            and tail[1] in {
                "bundle.json", "ca.crt", "ca.key", "ca.srl", "server.crt", "server.csr",
                "server.ext", "server.key",
            }
        )
    if root == "plans":
        group, *rest = tail
        if group in PLAN_GROUPS:
            return not rest if directory else (
                len(rest) == 1 and bool(re.fullmatch(r"(?:consumed-)?[a-f0-9]{64}\.json", rest[0]))
            )
        if group == "archive":
            if not rest:
                return directory
            stage, _, stamp = rest.pop(0).rpartition("-")
            if stage not in stages or not stamp.isdecimal():
                return False
        elif group not in stages:
            return False
        return not rest if directory else len(rest) == 1 and rest[0] in PLAN_FILES
    if root != "stages" or tail[0] not in stages:
        return False
    stage, *rest = tail
    if not rest:
        return directory
    if rest[0] != ".terraform":
        source = STAGE_ROOT / stage / Path(*rest)
        return source.is_dir() if directory else (
            rest == ["environment.auto.tfvars.json"] or source.is_file()
        )
    if not directory and rest[1:] == ["terraform.tfstate"]:
        return True
    provider = (".terraform", "providers", "registry.terraform.io", "hashicorp", "kubernetes")
    if directory and len(rest) <= len(provider):
        return tuple(rest) == provider[:len(rest)]
    if len(rest) <= len(provider) or tuple(rest[:len(provider)]) != provider:
        return False
    backend = rest[len(provider):]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", backend[0]):
        return False
    if len(backend) == 1:
        return directory
    if not re.fullmatch(r"(?:darwin|linux)_(?:amd64|arm64)", backend[1]):
        return False
    return len(backend) == 2 if directory else (
        len(backend) == 3 and backend[2] in {
            "LICENSE.txt", f"terraform-provider-kubernetes_v{backend[0]}_x5"
        }
    )


# Inputs: workspace and optional plan files written during this operation. Outputs: nested inventory.
# Purpose: protect unknown files and links while excluding the lock and external archives.
def purge_inventory(workspace, *, exclude=()):
    stages = {stage.name for stage in STAGE_ROOT.iterdir() if stage.is_dir()}
    inventory = {"files": [], "directories": [], "unknown": []}
    stamps = {}
    pending = [workspace]
    while pending:
        for entry in sorted(pending.pop().iterdir()):
            relative = entry.relative_to(workspace)
            if relative.parts[0] in {".operation.lock", "archive"}:
                continue
            info = entry.lstat()
            mode = info.st_mode
            directory = stat.S_ISDIR(mode)
            if not (directory or stat.S_ISREG(mode)) or not _artifact(relative.parts, directory, stages):
                inventory["unknown"].append(str(relative))
            elif directory:
                inventory["directories"].append(str(relative))
                pending.append(entry)
            elif str(relative) not in exclude:
                inventory["files"].append(str(relative))
                # configure rewrites these sources and interface records for every CLI action.
                materialized = relative.parts[0] == "stages" and ".terraform" not in relative.parts
                if not materialized and str(relative) not in {"environment.json", "reports/interfaces.json"}:
                    stamps[str(relative)] = _file_stamp(info)
    return {**{key: sorted(paths) for key, paths in inventory.items()}, "stamps": stamps}


# Inputs: stat record. Outputs: review-stable identity and modification evidence. Dependencies: POSIX.
# Purpose: reject a substituted artifact even when it uses an allowed name.
def _file_stamp(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


# Inputs: open directory, relative prefix, verified paths and file stamps. Outputs: none.
# Purpose: unlink only inventoried files without following symlinks or removing a nonempty directory.
def _purge_entries(fd, parent, files, directories, stamps):
    for name in sorted(os.listdir(fd)):
        path = parent / name
        if str(path) in files:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or (
                str(path) in stamps and _file_stamp(info) != stamps[str(path)]
            ):
                raise RuntimeError(f"Workspace artifact changed after review: {path}.")
            os.unlink(name, dir_fd=fd)
        elif str(path) in directories:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                _purge_entries(child, path, files, directories, stamps)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)


# Inputs: workspace and reviewed inventory including files written by this operation. Outputs: none.
# Purpose: remove exact artifacts with directory-relative operations. Dependencies: POSIX filesystem.
def purge(workspace, inventory):
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _purge_entries(
            fd, Path(), set(inventory["files"]), set(inventory["directories"]), inventory["stamps"]
        )
    finally:
        os.close(fd)
