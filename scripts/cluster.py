"""Purpose: create one new, explicitly named Minikube profile through a reviewed, create-only plan."""

import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

import runtime
from io_utils import read_json, run, write_json
from prerequisites import _sources

START_TIMEOUT_SECONDS = 1800


# Inputs: none. Outputs: absolute minikube path and its reported version.
# Purpose: pin the executable used by the reviewed command. Dependencies: PATH lookup, minikube version.
def _minikube():
    path = shutil.which("minikube")
    if not path:
        raise RuntimeError(
            "minikube is not installed; install it (for example `brew install minikube`) and prepare a new plan."
        )
    path = str(Path(path).resolve())
    result = run([path, "version", "-o", "json"], timeout=30)
    try:
        version = json.loads(result.stdout).get("minikubeVersion", "")
    except json.JSONDecodeError:
        version = ""
    if result.returncode or not version:
        raise RuntimeError("Cannot read the minikube version; check the minikube installation.")
    return path, version


# Inputs: none. Outputs: Minikube home directory. Purpose: locate profile records without starting VMs.
# Dependencies: MINIKUBE_HOME convention (the variable may name the .minikube directory or its parent).
def _minikube_home():
    configured = os.environ.get("MINIKUBE_HOME")
    if not configured:
        return Path.home() / ".minikube"
    home = Path(os.path.normpath(configured))
    return home if home.name == ".minikube" else home / ".minikube"


# Inputs: record path. Outputs: whether a directory entry exists, including a dangling symlink.
# Purpose: block creation on incomplete records and propagate unreadable paths. Dependencies: lstat.
def _record_exists(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


# Inputs: minikube path, home, and profile name. Outputs: whether any profile record uses that name.
# Purpose: enforce create-only behavior for valid, stopped, and invalid profiles. Dependencies: minikube profile list.
def _profile_exists(minikube, home, name):
    profiles = home / "profiles"
    if _record_exists(profiles / name):
        return True
    result = run([minikube, "profile", "list", "-o", "json"], timeout=60)
    try:
        listing = json.loads(result.stdout)
    except json.JSONDecodeError:
        listing = None
    if isinstance(listing, dict) and ("valid" in listing or "invalid" in listing):
        profiles = (listing.get("valid") or []) + (listing.get("invalid") or [])
        return any(item.get("Name") == name for item in profiles)
    # Minikube exits nonzero when no profile exists at all; only accept that with no profile records.
    if result.returncode and (not profiles.exists() or not any(profiles.iterdir())):
        return False
    raise RuntimeError("Cannot list Minikube profiles; inspect the Minikube installation.")


# Inputs: kubeconfig path and profile name. Outputs: the kinds of conflicting kubeconfig entries.
# Purpose: preserve all entries Minikube could overwrite. Dependencies: non-raw kubectl config view.
def _kubeconfig_conflicts(kubeconfig, name):
    if not _record_exists(kubeconfig):
        return []
    result = run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "config", "view", "-o", "json"],
        timeout=20,
    )
    if result.returncode:
        raise RuntimeError("Cannot read the selected kubeconfig.")
    try:
        view = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Cannot decode the selected kubeconfig.") from None
    if not isinstance(view, dict) or view.get("kind") != "Config":
        raise RuntimeError("Cannot decode the selected kubeconfig.")
    conflicts = []
    for kind in ("context", "cluster", "user"):
        entries = view.get(kind + "s")
        if entries is None:
            continue
        if not isinstance(entries, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in entries
        ):
            raise RuntimeError("Cannot read names from the selected kubeconfig.")
        if any(item["name"] == name for item in entries):
            conflicts.append(kind)
    return conflicts


# Inputs: resolved configuration and absolute minikube path. Outputs: fixed argument list.
# Purpose: express every reviewed cluster property explicitly. Dependencies: minikube start flags.
def _command(config, minikube):
    cluster, create = config["cluster"], config["cluster"]["create"]
    derived = config["derived"]["cluster"]
    command = [
        "/usr/bin/env",
        f"KUBECONFIG={cluster['kubeconfig']}",
        minikube,
        "start",
        f"--profile={cluster['name']}",
        f"--driver={create['driver']}",
        f"--nodes={create['nodes']}",
        f"--cpus={create['cpus']}",
        f"--memory={create['memory_mib']}mb",
        f"--disk-size={create['disk_mib']}mb",
        f"--kubernetes-version={create['kubernetes_version']}",
        f"--container-runtime={create['container_runtime']}",
        f"--cni={derived['cni']}",
    ]
    command += [f"--addons={addon}" for addon in derived["addons"]]
    # Keep the user's current context; every later command selects the context explicitly.
    command += ["--keep-context=true", "--interactive=false", "--wait=all"]
    return command


# Inputs: resolved configuration. Outputs: read-only host and target evidence with the planned command.
# Purpose: bind a review to the host, tool, and absence of conflicting records. Dependencies: minikube, kubectl.
def _snapshot(config):
    minikube, version = _minikube()
    name = config["cluster"]["name"]
    home = _minikube_home()
    profile_exists = _profile_exists(minikube, home, name)
    machines = [
        node
        for node in config["derived"]["cluster"]["nodes"]
        if _record_exists(home / "machines" / node)
    ]
    kubeconfig_conflicts = _kubeconfig_conflicts(Path(config["cluster"]["kubeconfig"]), name)
    blockers = []
    if profile_exists:
        blockers.append(
            f"Minikube profile {name} already exists; start it yourself and use cluster.mode=reuse, or choose a new cluster.name."
        )
    if machines:
        blockers.append(
            f"Minikube machine records already exist for {', '.join(machines)}; choose a new cluster.name."
        )
    blockers.extend(
        f"Kubeconfig {kind} {name} already exists; choose a new cluster.name."
        for kind in kubeconfig_conflicts
    )
    return {
        "host": {"platform": sys.platform, "machine": os.uname().machine},
        "minikube": {"path": minikube, "version": version},
        "profile": name,
        "kubeconfig": config["cluster"]["kubeconfig"],
        "profile_exists": profile_exists,
        "machine_conflicts": machines,
        "kubeconfig_conflicts": kubeconfig_conflicts,
        "blockers": blockers,
        "command": _command(config, minikube),
    }


# Inputs: resolved configuration, workspace, and reviewed record. Outputs: creation report.
# Purpose: run the exact reviewed command once and preserve any partial result. Dependencies: minikube, kubectl.
def _execute(config, workspace, review):
    plan_id = review["plan_id"]
    consumed = workspace / "plans" / "cluster" / f"consumed-{plan_id}.json"
    report_path = workspace / "reports" / "cluster.json"
    record = {
        "status": "attempting",
        "plan_id": plan_id,
        "profile": config["cluster"]["name"],
        "context": config["cluster"]["context"],
        "runtime_acceptance": "pending",
    }
    write_json(consumed, record)
    write_json(report_path, record)
    try:
        result = run(review["snapshot"]["command"], timeout=START_TIMEOUT_SECONDS)
    except RuntimeError:
        record["status"] = "uncertain"
        write_json(consumed, record)
        write_json(report_path, record)
        raise RuntimeError(
            "minikube start did not finish; inspect the profile before any further step. Nothing was deleted."
        ) from None
    if result.returncode:
        record["status"] = "failed"
        write_json(consumed, record)
        write_json(report_path, record)
        raise RuntimeError(
            "minikube start failed; the partial profile was preserved. Review `minikube logs` for this profile before deciding on recovery."
        )
    try:
        identity = runtime._identity(config)
        if not runtime._bind(config, workspace, identity, initial_creation=True):
            raise RuntimeError("An existing workspace binding differs from the new cluster.")
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as error:
        record["status"] = "uncertain"
        record["post_check"] = str(error)
        write_json(consumed, record)
        write_json(report_path, record)
        raise RuntimeError(
            "Minikube started but its identity could not be verified and saved; inspect the preserved profile and creation report."
        ) from None
    record.update(status="created", identity=identity, binding_established=True)
    try:
        record["suggested_api_server_cidrs"] = sorted(runtime._api_peers(config))
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as error:
        record["post_check"] = str(error)
    write_json(consumed, record)
    write_json(report_path, record)
    return record


# Inputs: resolved configuration and workspace. Outputs: whether this workspace created the profile.
# Purpose: gate cluster-contacting actions in create mode. Dependencies: the private creation report.
def created(config, workspace):
    return runtime._creation_report(config, workspace) is not None


# Inputs: resolved create-mode configuration, workspace, optional reviewed ID, execution flag.
# Outputs: plan, preview, or creation report. Purpose: one create-only workflow. Dependencies: private plans.
def cluster(config, workspace, plan_id=None, execute=False):
    directory = workspace / "plans" / "cluster"
    if created(config, workspace):
        report = read_json(workspace / "reports" / "cluster.json")
        return {
            **report,
            "note": "This workspace already created the profile; no change is planned.",
        }
    if plan_id is None:
        review = {
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "config_sha256": runtime._digest(config),
            "source_hashes": _sources(),
            "snapshot": _snapshot(config),
        }
        review["plan_id"] = runtime._digest(review)
        write_json(directory / f"{review['plan_id']}.json", review)
    else:
        review = read_json(directory / f"{plan_id}.json")
        recorded = review.pop("plan_id")
        if recorded != plan_id or runtime._digest(review) != plan_id:
            raise RuntimeError("Cluster plan ID or contents changed; prepare a fresh plan.")
        review["plan_id"] = recorded
        if (directory / f"consumed-{plan_id}.json").exists():
            raise RuntimeError(
                "Cluster plan was consumed; inspect the profile and its report before any new plan."
            )
        if (
            review["config_sha256"] != runtime._digest(config)
            or review["source_hashes"] != _sources()
        ):
            raise RuntimeError("Configuration or sources changed; prepare a fresh cluster plan.")
        if review["snapshot"]["blockers"]:
            raise RuntimeError(
                "The reviewed cluster plan is blocked; resolve it and prepare a new plan."
            )
        if execute:
            if _snapshot(config) != review["snapshot"]:
                raise RuntimeError(
                    "Host, Minikube, profile, machine, or kubeconfig evidence changed since review; prepare a fresh plan."
                )
            return _execute(config, workspace, review)
    snapshot = review["snapshot"]
    return {
        "status": "blocked"
        if snapshot["blockers"]
        else ("planned" if plan_id is None else "preview"),
        "plan_id": review["plan_id"],
        "review": str(directory / f"{review['plan_id']}.json"),
        "profile": snapshot["profile"],
        "kubeconfig": snapshot["kubeconfig"],
        "minikube": snapshot["minikube"],
        "host": snapshot["host"],
        "cluster": {**config["cluster"]["create"], **config["derived"]["cluster"]},
        "blockers": snapshot["blockers"],
        "command": snapshot["command"],
        "requires_execute": not snapshot["blockers"],
    }
