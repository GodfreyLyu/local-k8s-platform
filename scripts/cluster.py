"""Purpose: create one explicitly named Minikube profile through a reviewed plan, and recreate it only
through a separately reviewed overwrite plan for a profile this workspace created."""

import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

import runtime
from io_utils import private_dir, read_json, run, write_json
from prerequisites import _sources

START_TIMEOUT_SECONDS = 1800
DELETE_TIMEOUT_SECONDS = 600
# Workspace entries that survive an archive: the held lock and earlier archives.
ARCHIVE_KEEP = {".operation.lock", "archive"}


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


# Inputs: resolved configuration and absolute minikube path. Outputs: fixed delete argument list.
# Purpose: express the reviewed overwrite deletion explicitly. Dependencies: minikube delete flags.
def _delete_command(config, minikube):
    return [
        "/usr/bin/env",
        f"KUBECONFIG={config['cluster']['kubeconfig']}",
        minikube,
        "delete",
        f"--profile={config['cluster']['name']}",
    ]


# Inputs: resolved configuration and one recorded reference. Outputs: live uid/labels, or None when absent.
# Purpose: read existence and ownership without loading Secret data. Dependencies: kubectl GET.
def _live(config, reference):
    if reference["resource"] == "secrets":
        command = runtime._kube(config) + ["get", "secrets", reference["name"]]
        command += ["--namespace", reference["namespace"], "--ignore-not-found=true", "-o", "name"]
        result = run(command, timeout=20)
        if result.returncode:
            raise RuntimeError("Cannot read secrets; check API access and permissions.")
        return {"uid": None, "labels": None} if result.stdout.strip() else None
    item = runtime._get(config, reference["resource"], reference["name"], reference["namespace"])
    if item is None:
        return None
    metadata = item.get("metadata", {})
    return {"uid": metadata.get("uid"), "labels": metadata.get("labels", {})}


# Inputs: private workspace. Outputs: references created outside Terraform by this workspace.
# Purpose: include prerequisite objects and published Secrets in reconciliation. Dependencies: private reports.
def _recorded_extras(workspace):
    references = []
    for path in sorted((workspace / "reports").glob("prerequisites-*.json")):
        for item in read_json(path).get("results", []):
            if item.get("status") == "created" and item.get("reference"):
                references.append({"source": path.stem, **item["reference"], "uid": None})
    for path in sorted((workspace / "reports").glob("credentials-*.json")):
        for item in read_json(path).get("results", []):
            namespace, name = item["reference"].split("/", 1)
            references.append(
                {
                    "source": path.stem,
                    "resource": "secrets",
                    "name": name,
                    "namespace": namespace,
                    "uid": None,
                }
            )
    return references


# Inputs: resolved configuration and a workspace whose creation report exists. Outputs: comparison record.
# Purpose: compare the live cluster with the recorded identity, Terraform state, and other objects this
# workspace created; raises RuntimeError when the cluster cannot be read. Dependencies: kubectl, private files.
def _reconcile(config, workspace):
    recorded = runtime._creation_report(config, workspace)["identity"]
    observed = runtime._identity(config)
    # The kubeconfig hash changes with unrelated profiles; identity is the endpoint and kube-system UID.
    keys = ("context", "kubeconfig", "server", "kube_system_uid")
    identity = {
        "status": "match" if all(recorded.get(k) == observed[k] for k in keys) else "mismatch",
        "recorded": {key: recorded.get(key) for key in ("server", "kube_system_uid")},
        "observed": {key: observed[key] for key in ("server", "kube_system_uid")},
    }
    resources = []
    # Objects on a different cluster say nothing about this workspace's state, so skip them.
    if identity["status"] == "match":
        for reference in runtime._state_resources(workspace) + _recorded_extras(workspace):
            live = _live(config, reference)
            if live is None:
                status = "missing"
            elif reference["uid"] and live["uid"] != reference["uid"]:
                status = "uid_mismatch"
            elif (
                reference["source"].startswith("terraform:")
                and live["labels"].get(runtime.ENVIRONMENT_LABEL) != config["environment_id"]
            ):
                status = "owner_mismatch"
            else:
                status = "ok"
            resources.append({**reference, "status": status})
    return {
        "identity": identity,
        "resources": resources,
        "consistent": identity["status"] == "match"
        and all(item["status"] == "ok" for item in resources),
    }


# Inputs: resolved configuration and a workspace that created the profile. Outputs: overwrite evidence.
# Purpose: choose no change, recreation of a missing profile, or overwrite of a drifted one.
# Dependencies: _snapshot, _reconcile.
def _overwrite_snapshot(config, workspace):
    snapshot = _snapshot(config)
    name = config["cluster"]["name"]
    snapshot.update(reconcile=None, delete_command=None)
    if not snapshot["profile_exists"]:
        # Leftover machine or kubeconfig records stay blockers; they are not ours to clean.
        snapshot["action"] = "recreate-missing"
        return snapshot
    try:
        reconcile = _reconcile(config, workspace)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as error:
        # A stopped or unreachable cluster may still hold intact data, so it never counts as drift.
        snapshot.update(
            action="none",
            blockers=[
                f"Cannot compare profile {name} with recorded state: {error} Start it yourself with "
                f"`minikube start -p {name}` and prepare a new plan; an unreadable cluster is never overwritten."
            ],
        )
        return snapshot
    write_json(workspace / "reports" / "cluster-reconcile.json", reconcile)
    snapshot["reconcile"] = reconcile
    if reconcile["consistent"]:
        snapshot.update(action="none", blockers=[])
        return snapshot
    # The delete removes this profile's own machine and kubeconfig records; execution rechecks them.
    snapshot.update(
        action="overwrite",
        blockers=[],
        delete_command=_delete_command(config, snapshot["minikube"]["path"]),
    )
    return snapshot


# Inputs: workspace and plan ID. Outputs: archive directory path.
# Purpose: keep every previous workspace file privately instead of deleting it. Dependencies: same-filesystem rename.
def _archive_workspace(workspace, plan_id):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = private_dir(workspace / "archive") / f"{stamp}-{plan_id[:12]}"
    archive.mkdir(mode=0o700)
    for item in sorted(workspace.iterdir()):
        if item.name not in ARCHIVE_KEEP:
            item.rename(archive / item.name)
    return archive


# Inputs: resolved configuration, workspace, reviewed overwrite record, and a materialize callback.
# Outputs: creation report. Purpose: delete (overwrite only), archive, re-materialize, then start once.
# Dependencies: minikube, kubectl; materialize is the CLI's configure, passed in to avoid an import cycle.
def _execute_overwrite(config, workspace, review, materialize):
    plan_id, snapshot = review["plan_id"], review["snapshot"]
    consumed = workspace / "plans" / "cluster" / f"consumed-{plan_id}.json"
    record = {"status": "attempting", "plan_id": plan_id, "action": snapshot["action"]}
    write_json(consumed, record)
    if snapshot["action"] == "overwrite":
        try:
            result = run(snapshot["delete_command"], timeout=DELETE_TIMEOUT_SECONDS)
        except RuntimeError:
            record["status"] = "uncertain"
            write_json(consumed, record)
            raise RuntimeError(
                "minikube delete did not finish; inspect the profile. The workspace was not archived."
            ) from None
        if result.returncode:
            record["status"] = "failed"
            write_json(consumed, record)
            raise RuntimeError(
                "minikube delete failed; the workspace was not archived. Inspect the profile before a new plan."
            )
    # Creation needs a clean host, whether the profile was deleted here or by the user.
    residual = _snapshot(config)["blockers"]
    if residual:
        record.update(status="failed", residual=residual)
        write_json(consumed, record)
        raise RuntimeError(
            "Profile, machine, or kubeconfig records remain; the workspace was not archived. "
            + " ".join(residual)
        )
    record["status"] = "archiving"
    write_json(consumed, record)
    archive = _archive_workspace(workspace, plan_id)
    try:
        materialize(config, workspace)
        write_json(workspace / "plans" / "cluster" / f"{plan_id}.json", review)
        write_json(
            workspace / "reports" / "cluster-recreated.json",
            {
                "plan_id": plan_id,
                "action": snapshot["action"],
                "archive": str(archive),
                "reconcile": snapshot["reconcile"],
                "recreated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
    except (RuntimeError, ValueError, OSError) as error:
        raise RuntimeError(
            f"The previous workspace was archived at {archive}, but the new workspace could not be "
            f"prepared ({error}); inspect it before any new plan. No cluster was started."
        ) from None
    report = _execute(config, workspace, review)
    return {**report, "action": snapshot["action"], "archive": str(archive)}


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


# Inputs: workspace and overwrite snapshot. Outputs: review fields that describe the overwrite.
# Purpose: show the deletion, data loss, and drift evidence before authorization. Dependencies: none.
def _overwrite_summary(workspace, snapshot):
    reconcile = snapshot["reconcile"]
    summary = {
        "action": snapshot["action"],
        "delete_command": snapshot["delete_command"],
        "archive_directory": str(workspace / "archive"),
        "reconcile": None,
    }
    if reconcile is not None:
        summary["reconcile"] = {
            "identity": reconcile["identity"],
            "checked": len(reconcile["resources"]),
            "differences": [item for item in reconcile["resources"] if item["status"] != "ok"],
            "report": str(workspace / "reports" / "cluster-reconcile.json"),
        }
    if snapshot["action"] != "none":
        summary["effects"] = (
            "Every workspace file moves to the archive directory. The new cluster starts empty: "
            "data on the deleted profile, including PersistentVolumes, is lost. Prerequisites, "
            "stages, and credentials need new reviewed plans."
        )
    return summary


# Inputs: resolved create-mode configuration, workspace, optional reviewed ID, execution and overwrite
# flags, and the CLI's configure function. Outputs: plan, preview, or creation report.
# Purpose: one reviewed create workflow plus an explicit overwrite path. Dependencies: private plans.
def cluster(config, workspace, plan_id=None, execute=False, overwrite=False, materialize=None):
    directory = workspace / "plans" / "cluster"
    is_created = created(config, workspace)
    if overwrite and not is_created:
        raise RuntimeError(
            "--overwrite applies only to a profile this workspace created; without a creation report, "
            "plan a normal creation and inspect any earlier failed attempt first."
        )
    if is_created and not overwrite:
        report = read_json(workspace / "reports" / "cluster.json")
        return {
            **report,
            "note": "This workspace already created the profile; no change is planned. Use "
            "`cluster --overwrite` to compare it with recorded state and plan a recreation.",
        }

    # Inputs: none. Outputs: current evidence for the selected plan kind. Dependencies: the snapshots above.
    def current():
        return _overwrite_snapshot(config, workspace) if overwrite else _snapshot(config)

    if plan_id is None:
        review = {
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "config_sha256": runtime._digest(config),
            "source_hashes": _sources(),
            "snapshot": current(),
        }
        # Only overwrite reviews carry the flag, so earlier create plan IDs keep their contents.
        if overwrite:
            review["overwrite"] = True
        review["plan_id"] = runtime._digest(review)
        write_json(directory / f"{review['plan_id']}.json", review)
    else:
        review = read_json(directory / f"{plan_id}.json")
        recorded = review.pop("plan_id")
        if recorded != plan_id or runtime._digest(review) != plan_id:
            raise RuntimeError("Cluster plan ID or contents changed; prepare a fresh plan.")
        review["plan_id"] = recorded
        if review.get("overwrite", False) != overwrite:
            raise RuntimeError(
                "This cluster plan was prepared with a different --overwrite setting; repeat it exactly."
            )
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
            if overwrite and review["snapshot"]["action"] == "none":
                raise RuntimeError("The reviewed overwrite plan has no change to execute.")
            if current() != review["snapshot"]:
                raise RuntimeError(
                    "Host, Minikube, profile, machine, kubeconfig, or recorded-state evidence changed since review; prepare a fresh plan."
                )
            if overwrite:
                return _execute_overwrite(config, workspace, review, materialize)
            return _execute(config, workspace, review)
    snapshot = review["snapshot"]
    unchanged = overwrite and snapshot["action"] == "none"
    if snapshot["blockers"]:
        status = "blocked"
    elif unchanged:
        status = "unchanged"
    else:
        status = "planned" if plan_id is None else "preview"
    result = {
        "status": status,
        "plan_id": review["plan_id"],
        "review": str(directory / f"{review['plan_id']}.json"),
        "profile": snapshot["profile"],
        "kubeconfig": snapshot["kubeconfig"],
        "minikube": snapshot["minikube"],
        "host": snapshot["host"],
        "cluster": {**config["cluster"]["create"], **config["derived"]["cluster"]},
        "blockers": snapshot["blockers"],
        "command": snapshot["command"],
        "requires_execute": not snapshot["blockers"] and not unchanged,
    }
    if overwrite:
        result["overwrite"] = _overwrite_summary(workspace, snapshot)
    return result
