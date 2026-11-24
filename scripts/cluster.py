"""Purpose: create one explicitly named Minikube profile through a reviewed plan, and recreate it only
through a separately reviewed overwrite plan for a profile this workspace created."""

import datetime as dt
import json
import os
import re
import shutil
import sys
from contextlib import nullcontext, suppress
from pathlib import Path

import runtime
import secret_metadata
from io_utils import private_dir, read_json, run, sha256, write_json
from prerequisites import _sources

START_TIMEOUT_SECONDS = 1800
DELETE_TIMEOUT_SECONDS = 600
# Workspace entries that survive an archive: the held lock and earlier archives.
ARCHIVE_KEEP = {".operation.lock", "archive"}
CRD_RESOURCES = {*runtime.MANIFEST_KINDS.values(), "prometheuses.monitoring.coreos.com"}
# Unix sockets that a driver creates in each machine directory. krunkit binds its REST API and
# vmnet-helper network sockets there; other drivers are not checked because their names are unverified.
MACHINE_SOCKETS = {"krunkit": ("krunkit.sock", "vmnet-helper.sock")}
# Longest bindable Unix socket path in bytes: sun_path is 104 bytes on macOS and 108 on Linux,
# including the terminating NUL. A longer path makes vmnet-helper exit before the VM gets an address.
SOCKET_PATH_MAX = 103 if sys.platform == "darwin" else 107
# libkrun 1.19 formats both a u32 process ID and a u32 socket counter in this filename.
KRUN_TEMP_SOCKET = "krun-net-4294967295-4294967295.sock"


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
    home = Path(os.path.abspath(configured))
    return home if home.name == ".minikube" else home / ".minikube"


# Inputs: configuration, Minikube binary, home, and optional TMPDIR. Outputs: command prefix.
# Purpose: select the same paths for inspection and execution. Dependencies: Minikube environment.
def _minikube_command(config, minikube, home, *, tmpdir=None):
    kubeconfig = config["cluster"]["kubeconfig"]
    if os.pathsep in kubeconfig:
        raise ValueError("Minikube requires one kubeconfig path without a path-list separator.")
    command = ["/usr/bin/env", f"KUBECONFIG={kubeconfig}", f"MINIKUBE_HOME={home}"]
    if tmpdir is not None:
        command.append(f"TMPDIR={tmpdir}")
    return command + [minikube]


# Inputs: record path. Outputs: whether a directory entry exists, including a dangling symlink.
# Purpose: block creation on incomplete records and propagate unreadable paths. Dependencies: lstat.
def _record_exists(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


# Inputs: Minikube command prefix, home, and profile name. Outputs: whether any profile uses that name.
# Purpose: enforce create-only behavior for valid, stopped, and invalid profiles. Dependencies: minikube profile list.
def _profile_exists(command, home, name):
    profiles = home / "profiles"
    if _record_exists(profiles / name):
        return True
    result = run(command + ["profile", "list", "-o", "json"], timeout=60)
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


# Inputs: resolved configuration and Minikube command prefix. Outputs: fixed argument list.
# Purpose: express every reviewed cluster property explicitly. Dependencies: minikube start flags.
def _command(config, command):
    cluster, create = config["cluster"], config["cluster"]["create"]
    derived = config["derived"]["cluster"]
    command = command + [
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
    command += [
        "--keep-context=true",
        "--interactive=false",
        "--delete-on-failure=false",
        "--wait=all",
    ]
    return command


# Inputs: configuration and the home path Minikube receives. Outputs: longest socket path, its byte length,
# and the limit, or None. Purpose: find the machine socket that must fit the OS limit. Dependencies: MACHINE_SOCKETS.
def _longest_socket(config, home):
    sockets = MACHINE_SOCKETS.get(config["cluster"]["create"]["driver"])
    if not sockets:
        return None
    # Minikube appends ".minikube" unless the argument already has that basename.
    data = home if home.name == ".minikube" else home / ".minikube"
    paths = [
        data / "machines" / node / socket
        for node in config["derived"]["cluster"]["nodes"]
        for socket in sockets
    ]
    longest = max(paths, key=lambda path: len(os.fsencode(path)))
    return _socket_evidence(longest)


# Inputs: socket path. Outputs: byte length and host limit for plan review. Dependencies: OS encoding.
# Purpose: apply the limit to the path actually passed to the socket API.
def _socket_evidence(path):
    return {"path": str(path), "bytes": len(os.fsencode(path)), "limit": SOCKET_PATH_MAX}


# Inputs: socket evidence and owning environment variable. Outputs: blocker text, or None.
# Purpose: report a path that cannot bind and identify the setting used to shorten it.
def _socket_blocker(socket, variable):
    if not socket or socket["bytes"] <= socket["limit"]:
        return None
    location = "Minikube home" if variable == "MINIKUBE_HOME" else "TMPDIR"
    return (
        f"{location} is too long for driver krunkit. The "
        f"socket path {socket['path']} is {socket['bytes']} bytes, above the {socket['limit']}-byte "
        f"Unix socket limit. Set {variable} to a shorter private directory and prepare a new plan."
    )


# Inputs: resolved krunkit executable. Outputs: the resolved linked libkrun path, if identifiable.
# Purpose: inspect the installed library rather than the krunkit build-time dylib version.
def _linked_libkrun(krunkit):
    otool = shutil.which("otool")
    if not otool or any(name.startswith("DYLD_") for name in os.environ):
        return None
    try:
        result = run([otool, "-L", str(krunkit)], timeout=10)
        linked = set()
        for line in result.stdout.splitlines():
            path = line.strip().partition(" (compatibility version ")[0]
            if not re.fullmatch(r"libkrun(?:\.[0-9.]+)?\.dylib", Path(path).name):
                continue
            if path.startswith(("@loader_path/", "@executable_path/")):
                path = krunkit.parent / path.split("/", 1)[1]
            if not Path(path).is_absolute():
                return None
            linked.add(Path(path).resolve(strict=True))
    except (RuntimeError, OSError):
        return None
    return linked.pop() if not result.returncode and len(linked) == 1 else None


# Inputs: configuration and the target snapshot. Outputs: the snapshot with startup evidence.
# Purpose: check krunkit sockets and pin the temp directory before any create or replacement.
def _check_startup(config, snapshot):
    machine = _longest_socket(config, Path(snapshot["home_argument"]))
    snapshot["machine_socket"] = machine
    if machine is None:
        return snapshot
    blockers = snapshot["blockers"]
    if blocker := _socket_blocker(machine, "MINIKUBE_HOME"):
        blockers.append(blocker)
    executable = shutil.which("krunkit")
    if not executable:
        blockers.append("Install krunkit before planning a start with the krunkit driver.")
        return snapshot
    krunkit = Path(executable).resolve()
    library = _linked_libkrun(krunkit)
    version = re.fullmatch(r"libkrun\.1\.([0-9]+)\.[0-9]+\.dylib", library.name) if library else None
    minor = int(version[1]) if version else None
    snapshot["krunkit"] = {"path": str(krunkit), "libkrun": str(library) if library else None}
    sockets = {}
    # With unresolved linkage, both known client socket layouts must fit.
    if minor is None or minor < 19:
        sockets["MINIKUBE_HOME"] = _socket_evidence(
            Path(machine["path"]).with_name("vmnet-helper.sock-krun.sock")
        )
    if minor is None or minor >= 19:
        tmpdir = Path(os.path.abspath(os.environ.get("TMPDIR") or "/tmp"))
        sockets["TMPDIR"] = _socket_evidence(tmpdir / KRUN_TEMP_SOCKET)
        if sockets["TMPDIR"]["bytes"] <= SOCKET_PATH_MAX and (
            not tmpdir.is_dir() or not os.access(tmpdir, os.W_OK | os.X_OK)
        ):
            blockers.append("TMPDIR must be an existing writable directory for the krunkit socket.")
        command = _minikube_command(
            config, snapshot["minikube"]["path"], snapshot["home_argument"], tmpdir=tmpdir
        )
        snapshot["command"] = _command(config, command)
    snapshot["client_sockets"] = sockets
    for variable, socket in sockets.items():
        if blocker := _socket_blocker(socket, variable):
            blockers.append(blocker)
    return snapshot


# Inputs: configuration and optional reviewed home argument. Outputs: host and target evidence.
# Purpose: bind a review to the host, tool, and absence of conflicting records. Dependencies: minikube, kubectl.
def _snapshot(config, *, home=None):
    minikube, version = _minikube()
    name = config["cluster"]["name"]
    home = home or _minikube_home()
    resolved_home = home.resolve()
    # Minikube appends ".minikube" unless the argument already has that basename.
    if resolved_home.name == ".minikube":
        home = resolved_home
    command = _minikube_command(config, minikube, home)
    profile_exists = _profile_exists(command, resolved_home, name)
    machines = [
        node
        for node in config["derived"]["cluster"]["nodes"]
        if _record_exists(resolved_home / "machines" / node)
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
        "minikube_home": str(resolved_home),
        "home_argument": str(home),
        "profile": name,
        "kubeconfig": config["cluster"]["kubeconfig"],
        "profile_exists": profile_exists,
        "machine_conflicts": machines,
        "kubeconfig_conflicts": kubeconfig_conflicts,
        "blockers": blockers,
        "command": _command(config, command),
    }


# Inputs: configuration and host snapshot. Outputs: the profile's in-guest kube-system UID.
# Purpose: verify the running local profile independently of the host context. Dependencies: Minikube SSH.
def _profile_uid(config, snapshot):
    name = snapshot["profile"]
    path = Path(snapshot["minikube_home"]) / "profiles" / name / "config.json"
    version = read_json(path)["KubernetesConfig"]["KubernetesVersion"]
    if not isinstance(version, str) or not re.fullmatch(
        r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.+-]+)?", version
    ):
        raise RuntimeError("Cannot establish the Kubernetes version of the local profile.")
    command = _minikube_command(config, snapshot["minikube"]["path"], snapshot["home_argument"])
    # Minikube's kubeadm bootstrapper installs both files in the guest's persistent directory.
    result = run(
        command + [
            "ssh",
            f"--profile={name}",
            "--",
            "sudo",
            "-n",
            f"/var/lib/minikube/binaries/{version}/kubectl",
            "--kubeconfig=/var/lib/minikube/kubeconfig",
            "--request-timeout=10s",
            "get",
            "namespace",
            "kube-system",
            "-o=jsonpath='{.metadata.uid}'",
        ],
        timeout=30,
    )
    uid = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[A-Za-z0-9-]+", uid):
        raise RuntimeError("Cannot read the API identity from the running local profile.")
    return uid


# Inputs: configuration, one recorded reference, CRD cache and optional metadata socket.
# Outputs: live metadata, or None when absent.
# Purpose: read existence and ownership without loading Secret data. Dependencies: kubectl GET.
def _live(config, reference, crds, socket_path):
    resource, name, namespace = (reference[key] for key in ("resource", "name", "namespace"))
    if resource == "secrets":
        return secret_metadata.get(socket_path, namespace, name)
    if resource in CRD_RESOURCES:
        if resource not in crds:
            crd = runtime._get(config, "customresourcedefinitions.apiextensions.k8s.io", resource)
            crds[resource] = crd is not None
        if not crds[resource]:
            return None
    item = runtime._get(config, resource, name, namespace)
    return item.get("metadata", {}) if item is not None else None


# Inputs: configuration, workspace, host snapshot, and creation report. Outputs: comparison record.
# Purpose: compare the live cluster with the recorded identity, Terraform state, and other objects this
# workspace created; raises RuntimeError when the cluster cannot be read. Dependencies: kubectl, private files.
def _reconcile(config, workspace, snapshot, creation):
    profile_uid = _profile_uid(config, snapshot)
    recorded = creation["identity"]
    kubeconfig = Path(snapshot["kubeconfig"])
    config_hash = sha256(kubeconfig)
    observed = runtime._identity(config)
    if profile_uid != observed["kube_system_uid"]:
        raise RuntimeError("The selected context does not reach the running local profile.")
    # The kubeconfig hash changes with unrelated profiles; identity is the endpoint and kube-system UID.
    keys = ("context", "kubeconfig", "server", "kube_system_uid")
    identity = {
        "status": "match" if all(recorded.get(k) == observed[k] for k in keys) else "mismatch",
        "recorded": {key: recorded.get(key) for key in ("server", "kube_system_uid")},
        "observed": {key: observed[key] for key in ("server", "kube_system_uid")},
    }
    resources = []
    crds = {}
    # Objects on a different cluster say nothing about this workspace's state, so skip them.
    if identity["status"] == "match":
        references = runtime._state_resources(workspace) + runtime._recorded_objects(workspace)
        needs_proxy = any(item["resource"] == "secrets" for item in references)
        with (secret_metadata.proxy(config) if needs_proxy else nullcontext()) as socket_path:
            for reference in references:
                live = _live(config, reference, crds, socket_path)
                observed_uid = live.get("uid") if live is not None else None
                if live is not None and (not isinstance(observed_uid, str) or not observed_uid):
                    raise RuntimeError(
                        f"Cannot establish the UID of {reference['resource']}/{reference['name']}."
                    )
                labels = (live or {}).get("labels") or {}
                owner = labels.get(runtime.ENVIRONMENT_LABEL)
                if live is None:
                    status = "missing"
                elif reference["uid"] and observed_uid != reference["uid"]:
                    status = "uid_mismatch"
                elif (
                    reference["source"].startswith("terraform:")
                    and owner != config["environment_id"]
                ):
                    status = "owner_mismatch"
                else:
                    status = "ok"
                resources.append(
                    {
                        **reference,
                        "status": status,
                        "observed_uid": observed_uid,
                        "observed_owner": owner,
                    }
                )
    if config_hash != sha256(kubeconfig):
        raise RuntimeError("The selected kubeconfig changed during comparison.")
    return {
        "identity": identity,
        "resources": resources,
        "crd_presence": crds,
        "kubeconfig_sha256": config_hash,
        "consistent": identity["status"] == "match"
        and all(item["status"] == "ok" for item in resources),
    }


# Inputs: resolved configuration and a workspace that created the profile. Outputs: overwrite evidence.
# Purpose: choose no change, recreation of a missing profile, or overwrite of a drifted one.
# Dependencies: _snapshot, _reconcile.
def _overwrite_snapshot(config, workspace):
    snapshot = _snapshot(config)
    name = config["cluster"]["name"]
    creation = runtime._creation_report(config, workspace)
    snapshot.update(reconcile=None, delete_command=None)
    home = Path(snapshot["minikube_home"])
    kubeconfig = Path(snapshot["kubeconfig"])
    blocker = None
    if creation is None or not creation.get("minikube_home"):
        blocker = "The creation report has no recorded Minikube home to verify for overwrite."
    elif creation["minikube_home"] != str(home):
        blocker = "The resolved Minikube home differs from the creation report."
    elif (
        home.is_relative_to(workspace)
        or workspace.is_relative_to(home)
        or kubeconfig.is_relative_to(workspace)
        or Path(snapshot["home_argument"]).is_relative_to(workspace)
    ):
        blocker = (
            "Overwrite requires separate archive paths. Keep Minikube home and kubeconfig "
            "outside the workspace and the workspace outside Minikube home."
        )
    if blocker:
        snapshot.update(action="none", blockers=[blocker])
        return snapshot
    if not snapshot["profile_exists"]:
        # Leftover machine or kubeconfig records stay blockers; they are not ours to clean.
        snapshot["action"] = "recreate-missing"
        return _check_startup(config, snapshot)
    try:
        reconcile = _reconcile(config, workspace, snapshot, creation)
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
    snapshot.update(action="none", blockers=[])
    _check_startup(config, snapshot)
    if snapshot["blockers"]:
        return snapshot
    # The delete removes this profile's own machine and kubeconfig records; execution rechecks them.
    snapshot.update(
        action="overwrite",
        blockers=[],
        delete_command=(
            _minikube_command(config, snapshot["minikube"]["path"], snapshot["home_argument"])
            + ["delete", f"--profile={name}"]
        ),
    )
    return snapshot


# Inputs: workspace and plan ID. Outputs: reserved private archive directory.
# Purpose: verify archive placement before delete. Dependencies: writable directories on one filesystem.
def _reserve_archive(workspace, plan_id):
    parent = private_dir(workspace / "archive")
    if any((path.stat().st_mode & 0o300) != 0o300 for path in (workspace, parent)):
        raise RuntimeError(
            "Cannot reserve the archive. The workspace and archive directory must be writable."
        )
    if parent.stat().st_dev != workspace.stat().st_dev:
        raise RuntimeError("The archive must be on the same filesystem as the workspace.")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = parent / f"{stamp}-{plan_id[:12]}"
    archive.mkdir(mode=0o700)
    return archive


# Inputs: workspace and reserved archive. Outputs: none.
# Purpose: preserve workspace files after deletion is confirmed. Dependencies: same-filesystem rename.
def _archive_workspace(workspace, archive):
    for item in sorted(workspace.iterdir()):
        if item.name not in ARCHIVE_KEEP:
            item.rename(archive / item.name)


# Inputs: reviewed snapshot. Outputs: the pinned Minikube home argument.
# Purpose: ensure a home alias still resolves to the reviewed directory. Dependencies: local paths.
def _reviewed_home(snapshot):
    home = Path(snapshot["home_argument"])
    if str(home.resolve()) != snapshot["minikube_home"]:
        raise RuntimeError("The resolved Minikube home changed since review.")
    return home


# Inputs: resolved configuration, workspace, reviewed overwrite record, and a materialize callback.
# Outputs: creation report. Purpose: delete (overwrite only), archive, re-materialize, then start once.
# Dependencies: minikube, kubectl; materialize is the CLI's configure, passed in to avoid an import cycle.
def _execute_overwrite(config, workspace, review, materialize):
    plan_id, snapshot = review["plan_id"], review["snapshot"]
    consumed = workspace / "plans" / "cluster" / f"consumed-{plan_id}.json"
    archive = _reserve_archive(workspace, plan_id)
    record = {
        "status": "attempting",
        "plan_id": plan_id,
        "action": snapshot["action"],
        "deletion": "not-needed",
        "archive": str(archive),
    }
    try:
        home = _reviewed_home(snapshot)
        write_json(consumed, record)
        if snapshot["action"] == "overwrite":
            if sha256(Path(snapshot["kubeconfig"])) != snapshot["reconcile"]["kubeconfig_sha256"]:
                raise RuntimeError("The selected kubeconfig changed since review.")
            record["deletion"] = "uncertain"
            write_json(consumed, record)
            try:
                result = run(snapshot["delete_command"], timeout=DELETE_TIMEOUT_SECONDS)
            except RuntimeError:
                record["status"] = "uncertain"
                raise RuntimeError(
                    "minikube delete did not finish. Inspect the profile. "
                    "The workspace was not archived."
                ) from None
            if result.returncode:
                record["deletion"] = "failed"
                raise RuntimeError(
                    "minikube delete failed. The workspace was not archived. "
                    "Inspect the profile before a new plan."
                )
            record["deletion"] = "completed"
            write_json(consumed, record)
        # Inspect the reviewed home even if the ambient environment has changed.
        _reviewed_home(snapshot)
        residual = _snapshot(config, home=home)["blockers"]
        if residual:
            record["residual"] = residual
            raise RuntimeError(
                "Profile, machine, or kubeconfig records remain. The workspace was not archived. "
                + " ".join(residual)
            )
    except (RuntimeError, ValueError, OSError):
        if record["status"] == "attempting":
            record["status"] = "failed"
        with suppress(OSError):
            archive.rmdir()
        write_json(consumed, record)
        raise
    record["status"] = "archiving"
    write_json(consumed, record)
    progress = {key: record[key] for key in ("action", "deletion", "archive")}
    deletion = "The previous profile was deleted. " if record["deletion"] == "completed" else ""
    try:
        _archive_workspace(workspace, archive)
        materialize(config, workspace)
        write_json(workspace / "plans" / "cluster" / f"{plan_id}.json", review)
        write_json(
            workspace / "reports" / "cluster-recreated.json",
            {
                "plan_id": plan_id,
                **progress,
                "reconcile": snapshot["reconcile"],
                "recreated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
    except (RuntimeError, ValueError, OSError) as error:
        raise RuntimeError(
            f"{deletion}Preparing the archive at {archive} or the new workspace failed "
            f"({error}). Inspect both directories before any new plan."
        ) from None
    try:
        return _execute(config, workspace, review, previous=progress)
    except (RuntimeError, ValueError, OSError) as error:
        raise RuntimeError(
            f"{deletion}The previous workspace was archived at {archive}. Replacement: {error}"
        ) from None


# Inputs: configuration, workspace, reviewed record, and optional overwrite progress. Outputs: creation report.
# Purpose: run the exact reviewed command once and preserve any partial result. Dependencies: minikube, kubectl.
def _execute(config, workspace, review, *, previous=None):
    plan_id, snapshot = review["plan_id"], review["snapshot"]
    _reviewed_home(snapshot)
    consumed = workspace / "plans" / "cluster" / f"consumed-{plan_id}.json"
    report_path = workspace / "reports" / "cluster.json"
    record = {
        "status": "attempting",
        "plan_id": plan_id,
        "profile": config["cluster"]["name"],
        "context": config["cluster"]["context"],
        "minikube_home": snapshot["minikube_home"],
        "runtime_acceptance": "pending",
        **(previous or {}),
    }
    write_json(consumed, record)
    write_json(report_path, record)
    try:
        result = run(snapshot["command"], timeout=START_TIMEOUT_SECONDS)
    except RuntimeError:
        record["status"] = "uncertain"
        write_json(consumed, record)
        write_json(report_path, record)
        raise RuntimeError(
            "minikube start did not finish. Inspect the profile before any further step."
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
        if overwrite:
            return _overwrite_snapshot(config, workspace)
        return _check_startup(config, _snapshot(config))

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
        "minikube_home": snapshot["minikube_home"],
        "host": snapshot["host"],
        "cluster": {**config["cluster"]["create"], **config["derived"]["cluster"]},
        "blockers": snapshot["blockers"],
        "command": snapshot["command"],
        "requires_execute": not snapshot["blockers"] and not unchanged,
    }
    if overwrite:
        result["overwrite"] = _overwrite_summary(workspace, snapshot)
    return result
