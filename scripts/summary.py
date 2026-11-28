"""Purpose: turn each action's report into one uniform current/add/change/delete summary and text view."""

import json
import shlex

# Text shown when a step cannot delete anything by design, so reviewers never infer it from silence.
NEVER_DELETES = "none; this step never deletes resources"
VALUE_CHARS = 80


# Inputs: resource, namespace and name. Outputs: display label. Purpose: one object naming style.
# Dependencies: none.
def _object(resource, name, namespace=None):
    return f"{resource} {namespace + '/' if namespace else ''}{name}"


# Inputs: any JSON value. Outputs: compact, bounded text. Purpose: keep long values readable.
# Dependencies: json.
def _value(value):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return text if len(text) <= VALUE_CHARS else text[: VALUE_CHARS - 1] + "…"


# Inputs: identity record or None. Outputs: one target line or None. Purpose: name the cluster
# a step reads or changes. Dependencies: runtime identity fields.
def _target(identity):
    if not identity:
        return None
    parts = [f"context {identity['context']}"] if identity.get("context") else []
    if identity.get("server"):
        parts.append(f"server {identity['server']}")
    if identity.get("kube_system_uid"):
        parts.append(f"kube-system uid {identity['kube_system_uid']}")
    return " · ".join(parts) or None


# Inputs: parsed CLI arguments and extra arguments. Outputs: a full platform.py command line.
# Purpose: show the exact next command with the reviewed options. Dependencies: shlex.
def _command(args, *extra):
    prefix = ["python3", "-B", "scripts/platform.py", "--config", str(args.config)]
    return shlex.join(prefix + ["--workspace", str(args.workspace), *map(str, extra)])


# Inputs: parsed CLI arguments. Outputs: the action's option list without plan/execute flags.
# Purpose: repeat the reviewed options exactly when executing a plan. Dependencies: argparse names.
def _options(args):
    action = args.action
    options = [action]
    if action == "cluster":
        options += ["--overwrite"] * args.overwrite
        options += ["--confirm-legacy-vm-removal"] * args.confirm_legacy_vm_removal
    elif action == "delete":
        options += ["--workspace-files", args.workspace_files]
        options += ["--include-unmanaged"] * args.include_unmanaged
        options += ["--confirm-legacy-vm-removal"] * args.confirm_legacy_vm_removal
    elif action in {"prerequisites", "acceptance"}:
        options += ["--component", args.component]
    elif action == "apply":
        options += ["--stage", args.stage]
    elif action == "backup":
        options += ["--backup-dir", args.backup_dir] + ["--with-fixture"] * args.with_fixture
    elif action == "restore":
        options += ["--backup", args.backup]
        options += ["--target-database", args.target_database] if args.target_database else []
        options += ["--keep-target"] * args.keep_target
    return options


# Inputs: empty summary. Outputs: none. Purpose: helpers that append one entry to a section.
# Dependencies: none.
def _add(summary, section, label, detail=None):
    summary[section].append({"item": label, **({"detail": detail} if detail else {})})


# Inputs: summary, report. Outputs: none. Purpose: summarize a create or overwrite cluster report.
# Dependencies: cluster.py report fields.
def _cluster(summary, args, report):
    if "note" in report:
        summary["phase"] = "read-only"
        _add(summary, "current", f"Minikube profile {report.get('profile')}", report.get("status"))
        summary["notes"].append(report["note"])
        return
    if report.get("status") not in {"planned", "preview", "blocked", "unchanged"}:
        _execution(summary, report)
        profile = report.get("profile")
        if report.get("status") == "created":
            _add(summary, "add", f"Minikube profile {profile}", "created and identity recorded")
        if report.get("deletion") == "completed":
            _add(summary, "delete", f"previous Minikube profile {profile}", "deleted")
        if report.get("archive"):
            _add(summary, "delete", "workspace files", f"moved to {report['archive']}")
        if report.get("suggested_api_server_cidrs"):
            summary["notes"].append(
                "Review suggested network.api_server_cidrs: "
                + ", ".join(report["suggested_api_server_cidrs"])
            )
        return
    cluster = report.get("cluster", {})
    profile = report.get("profile")
    minikube = report.get("minikube") or {}
    _add(summary, "current", "minikube", f"{minikube.get('version')} at {minikube.get('path')}")
    _add(summary, "current", "Minikube home", report.get("minikube_home"))
    overwrite = report.get("overwrite")
    action = overwrite["action"] if overwrite else "create"
    shape = (
        f"driver {cluster.get('driver')}, {cluster.get('nodes')} node(s), {cluster.get('cpus')} CPUs, "
        f"{cluster.get('memory_mib')} MiB memory, {cluster.get('disk_mib')} MiB disk, Kubernetes "
        f"{cluster.get('kubernetes_version')}, CNI {cluster.get('cni')}, addons "
        f"{', '.join(cluster.get('addons') or []) or 'none'}"
    )
    if overwrite:
        reconcile = overwrite.get("reconcile")
        state = {
            "recreate-missing": "absent",
            "overwrite": "running, differs from the workspace records",
            "none": "matches the workspace records" if reconcile else "not compared",
        }[action]
        _add(summary, "current", f"Minikube profile {profile}", state)
        if reconcile:
            identity = reconcile["identity"]
            _add(
                summary, "current", "cluster identity",
                f"{identity['status']}: recorded {identity['recorded'].get('kube_system_uid')}, "
                f"observed {identity['observed'].get('kube_system_uid')}",
            )
            _add(summary, "current", "recorded objects checked", str(reconcile["checked"]))
            for item in reconcile["differences"]:
                _add(
                    summary, "current",
                    _object(item["resource"], item["name"], item.get("namespace")), item["status"],
                )
        if action == "none":
            summary["unchanged"].append({"item": f"Minikube profile {profile}"})
        else:
            if action == "overwrite":
                _add(summary, "delete", f"Minikube profile {profile}", "all cluster data, including PersistentVolumes")
            _add(summary, "delete", "workspace files", f"moved to {overwrite['archive_directory']}")
            _add(summary, "add", f"Minikube profile {profile}", shape)
            _add(summary, "add", f"kubeconfig context {profile}", report.get("kubeconfig"))
        if overwrite.get("effects"):
            summary["notes"].append(overwrite["effects"])
    else:
        _add(summary, "current", f"Minikube profile {profile}", "absent" if not report.get("blockers") else "conflict")
        _add(summary, "add", f"Minikube profile {profile}", shape)
        _add(summary, "add", f"kubeconfig context {profile}", report.get("kubeconfig"))
    if action != "none" and report.get("command"):
        summary["notes"].append("Start command: " + shlex.join(report["command"]))


# Inputs: summary, report. Outputs: none. Purpose: summarize a delete plan or deletion record.
# Dependencies: cluster.py and delete_safety.py report fields.
def _delete(summary, args, report):
    profile = report.get("profile")
    if report.get("status") not in {"planned", "preview", "blocked"}:
        _execution(summary, report)
        if report.get("deletion") == "completed":
            _add(summary, "delete", f"Minikube profile {profile}", "deleted")
        handled = {
            "keep": "kept; creation report marked deleted",
            "archive": f"moved to {report.get('archive')}",
            "purge": f"{len(report.get('removed') or [])} file(s) removed",
        }[report.get("workspace_files", "keep")]
        _add(summary, "delete", "workspace files", handled)
        return
    action = report.get("action")
    state = {"delete": "running, identity matches the creation report",
             "already-absent": "absent"}.get(action, "not verified")
    _add(summary, "current", f"Minikube profile {profile}", state)
    _add(summary, "current", "Minikube home", report.get("minikube_home"))
    identity = report.get("identity")
    if identity and "recorded" in identity:
        _add(
            summary, "current", "cluster identity",
            f"recorded {identity['recorded'].get('kube_system_uid')}, "
            f"observed {identity['observed'].get('kube_system_uid')}",
        )
    if action == "delete":
        _add(summary, "delete", f"Minikube profile {profile}", "the VM and every object on it")
    loss = report.get("data_loss") or {}
    for claim in loss.get("persistent_volume_claims", []):
        _add(summary, "delete", f"PersistentVolumeClaim {claim}", "data lost")
    for item in loss.get("objects", []):
        if item["resource"] == "persistentvolumes":
            _add(summary, "delete", f"PersistentVolume {item['name']}", f"uid {item['uid']}")
    if loss.get("managed_objects"):
        _add(summary, "delete", "objects this environment manages", f"{len(loss['managed_objects'])} object(s)")
    unmanaged = loss.get("unmanaged") or {}
    for namespace in unmanaged.get("namespaces", []):
        _add(summary, "delete", f"unmanaged namespace {namespace}", "not managed by this environment")
    if unmanaged.get("objects"):
        _add(summary, "delete", "other unmanaged objects", f"{len(unmanaged['objects'])} object(s)")
    files = report.get("workspace_files")
    handling = {
        "keep": ("workspace files", "kept; creation report becomes deleted"),
        "archive": ("workspace files", "moved to archive/"),
        "purge": ("workspace files written by this skill", "removed, including credentials"),
    }.get(files)
    if handling:
        target = "unchanged" if files == "keep" else "delete"
        _add(summary, target, *handling)
    if report.get("effects"):
        summary["notes"].append(report["effects"])
    if report.get("delete_command"):
        summary["notes"].append("Delete command: " + shlex.join(report["delete_command"]))


# Inputs: summary, report. Outputs: none. Purpose: summarize a prerequisite plan or execution.
# Dependencies: prerequisites.py report fields.
def _prerequisites(summary, args, report):
    if report.get("status") not in {"planned", "preview", "blocked"}:
        _execution(summary, report)
        for result in report.get("results", []):
            label = (
                "OpenSSL package" if result["action"] == "install-openssl"
                else _object(**result["reference"])
            )
            _add(summary, "add", label, result["status"])
        return
    target = report.get("target") or {}
    summary["target"] = _target(target) or (
        f"host {target.get('hostname')} ({target.get('platform')})" if target else None
    )
    for name in report.get("reused", []):
        summary["unchanged"].append({"item": name, "detail": "reused as installed"})
    for change in report.get("changes", []):
        if change["action"] == "install-openssl":
            _add(summary, "add", "OpenSSL package", shlex.join(change["command"]))
        else:
            _add(summary, "add", _object(**change["reference"]))
    if report.get("managed_memory_mib") is not None:
        _add(summary, "current", "memory requested by managed prerequisites", f"{report['managed_memory_mib']} MiB")
    summary["delete_note"] = NEVER_DELETES


# Inputs: summary, report. Outputs: none. Purpose: summarize a Terraform stage plan or apply.
# Dependencies: runtime.py plan/apply report fields.
def _stage(summary, args, report):
    summary["target"] = _target(report.get("identity"))
    summary["title"] += f" {report.get('stage')}"
    summary["delete_note"] = "none; stage plans that delete or import are rejected"
    if "changes" not in report:
        _execution(summary, report)
        _add(summary, "current", "Terraform apply", report.get("status"))
        if report.get("diagnostics"):
            _add(summary, "current", "diagnostics", report["diagnostics"])
        return
    for change in report["changes"]:
        label = _object(change["resource"], change["name"], change.get("namespace"))
        actions = change["actions"]
        fields = change.get("fields") or {}
        if actions == ["create"]:
            detail = ", ".join(f"{path}={_value(value['after'])}" for path, value in fields.items())
            _add(summary, "add", label, detail or None)
        elif "update" in actions:
            summary["change"].append({
                "item": label,
                "fields": [
                    {"field": path, "before": value.get("before"), "after": value.get("after")}
                    for path, value in fields.items()
                ],
            })
        else:
            summary["unchanged"].append({"item": label, "detail": "/".join(actions)})


# Inputs: summary, report. Outputs: none. Purpose: summarize credential preview, preparation,
# publication or expiry. Dependencies: credentials.py report fields.
def _credentials(summary, args, report):
    if args.expiry:
        summary["phase"] = "read-only"
        for entry in report.get("certificates", []):
            detail = entry.get("status") or entry.get("state")
            if entry.get("remaining_days") is not None:
                detail += f", {entry['remaining_days']} days left"
            _add(summary, "current", entry["reference"], detail)
        for group in report.get("mismatched_copies", []):
            summary["blockers"].append(f"Copies of {group['group']} differ from their source")
        return
    status = report.get("status")
    if status == "prepared":
        _add(summary, "add", "local credential material", report.get("directory"))
        summary["notes"].append("No cluster writes; Secrets are written by credentials --execute.")
        summary["phase"] = "result"
    elif status in {"published", "failed", "publishing"}:
        _execution(summary, report)
        for result in report.get("results", []):
            section = "add" if result["status"] == "created" else "unchanged"
            _add(summary, section, f"Secret {result['reference']}", result["status"])
    else:
        for reference in report.get("secret_references", []):
            _add(summary, "add", f"Secret {reference}", "created by credentials --execute")
    summary["delete_note"] = "none; existing Secrets are never overwritten or removed"


# Inputs: summary, report. Outputs: none. Purpose: summarize a reviewed write/cleanup plan for
# acceptance, backup, or restore. Dependencies: their report fields.
def _reviewed(summary, args, report):
    summary["target"] = _target(report.get("identity"))
    if report.get("status") in {"planned", "preview"}:
        if report.get("target") and args.action == "acceptance":
            _add(summary, "current", "acceptance target", _value(report["target"]))
        if report.get("source"):
            _add(summary, "current", "source", _value(report["source"]))
        if report.get("backup"):
            backup = report["backup"]
            _add(summary, "current", "backup", f"{backup['directory']} ({backup['bytes']} bytes)")
        for line in report.get("reads", []):
            _add(summary, "current", "reads", line)
        for line in report.get("writes", []):
            _add(summary, "add", line)
        for line in report.get("cleanup", []):
            _add(summary, "delete", line)
        for line in report.get("untouched", []):
            summary["unchanged"].append({"item": line})
        if not report.get("cleanup"):
            summary["delete_note"] = "none; only the new items listed above are written"
        return
    _execution(summary, report)
    for check in report.get("checks", []):
        _add(summary, "current", f"check {check.get('name')}", check.get("status"))
    if report.get("directory"):
        _add(summary, "add", "backup directory", report["directory"])
    cleanup = report.get("cleanup") or report.get("target_cleanup") or (report.get("fixture") or {}).get("cleanup")
    if cleanup:
        _add(summary, "delete", "fixtures and temporary objects", cleanup)


# Inputs: summary, report. Outputs: none. Purpose: summarize the GPU smoke-test preview or result.
# Dependencies: gpu.py report fields.
def _gpu(summary, args, report):
    pod = report.get("pod")
    if report.get("status") == "preview":
        _add(summary, "add", f"Pod {pod}", f"image {report.get('image')}")
        _add(summary, "delete", f"Pod {pod}", "removed after the log is read")
        return
    _execution(summary, report)
    if report.get("detail"):
        _add(summary, "current", "detail", _value(report["detail"]))


# Inputs: summary, report. Outputs: none. Purpose: summarize read-only inspect or verify checks.
# Dependencies: runtime.py check records.
def _checks(summary, args, report):
    summary["phase"] = "read-only"
    summary["target"] = _target(report.get("identity"))
    for check in report.get("checks", []):
        _add(summary, "current", check["name"], check["status"])
    for check in report.get("acceptance_checks", []):
        _add(summary, "current", f"acceptance {check['name']}", check["status"])


# Inputs: summary, report. Outputs: none. Purpose: summarize the offline configure step.
# Dependencies: platform.configure report fields.
def _configure(summary, args, report):
    summary["phase"] = "read-only"
    summary["target"] = "Kubernetes not contacted (offline step)"
    _add(summary, "current", "workspace", report.get("workspace"))
    for stage in report.get("stages", []):
        _add(summary, "current", f"stage {stage}", "materialized locally")


# Inputs: summary, report. Outputs: none. Purpose: mark a report as an execution result.
# Dependencies: none.
def _execution(summary, report):
    summary["phase"] = "result"
    for key in ("error", "post_check"):
        if report.get(key):
            summary["blockers"].append(str(report[key]))
    failure = report.get("failure")
    if failure:
        summary["blockers"].append(f"Failed stage {failure.get('stage')}: {failure.get('first_error')}")


# Inputs: parsed CLI arguments and summary. Outputs: next-command text or None.
# Purpose: point at the exact follow-up and its authorization need. Dependencies: _command.
def _next(args, report, summary):
    status, plan_id = report.get("status"), report.get("plan_id")
    if status == "blocked" or summary["blockers"] and summary["phase"] != "result":
        return "Resolve the blockers, then prepare a new plan."
    if args.action == "plan" and plan_id:
        return _command(args, "apply", "--stage", args.stage, "--plan-id", plan_id) + "  (preview)"
    if args.action == "gpu-smoke" and status == "preview":
        return _command(args, "gpu-smoke", "--execute") + "  (needs your authorization)"
    if args.action == "credentials" and not args.expiry and status in {"preview", "prepared"}:
        mode = "--prepare" if status == "preview" and args.component == "postgres" else "--execute"
        note = "  (needs your authorization)" if mode == "--execute" else ""
        return _command(args, "credentials", "--component", args.component, mode) + note
    if report.get("requires_execute") and plan_id:
        return (
            _command(args, *_options(args), "--plan-id", plan_id, "--execute")
            + "  (needs your authorization)"
        )
    return None


SUMMARIZERS = {
    "configure": _configure,
    "cluster": _cluster,
    "delete": _delete,
    "prerequisites": _prerequisites,
    "inspect": _checks,
    "verify": _checks,
    "plan": _stage,
    "apply": _stage,
    "credentials": _credentials,
    "acceptance": _reviewed,
    "backup": _reviewed,
    "restore": _reviewed,
    "gpu-smoke": _gpu,
}


# Inputs: parsed CLI arguments and the action's report. Outputs: uniform summary dictionary.
# Purpose: one current/add/change/delete view for every step. Dependencies: the summarizers above.
def summarize(args, report):
    summary = {
        "title": args.action,
        "status": report.get("status"),
        "phase": "plan",
        "target": None,
        "current": [],
        "add": [],
        "change": [],
        "delete": [],
        "unchanged": [],
        "blockers": list(report.get("blockers") or []),
        "notes": [],
        "delete_note": None,
    }
    SUMMARIZERS[args.action](summary, args, report)
    summary["next"] = _next(args, report, summary)
    if summary["phase"] == "read-only":
        summary["delete_note"] = "none; this step is read-only"
    return {key: value for key, value in summary.items() if value is not None}


# Inputs: summary dictionary from summarize. Outputs: multi-line text block.
# Purpose: show the step in a fixed, scannable layout. Dependencies: none.
def render(summary):
    phase = summary["phase"]
    verbs = {
        "plan": ("Will add", "Will change", "Will delete"),
        "result": ("Added", "Changed", "Deleted"),
        "read-only": ("Add", "Change", "Delete"),
    }[phase]
    lines = [f"== {summary['title']} [{summary.get('status')}] =="]
    if summary.get("target"):
        lines.append(f"Target: {summary['target']}")

    # Inputs: heading, entries and marker. Outputs: none. Purpose: append one section.
    def section(heading, entries, marker, empty="none"):
        lines.append(f"{heading} ({len(entries)})" if entries else f"{heading}: {empty}")
        for entry in entries:
            text = f"  {marker} {entry['item']}"
            if entry.get("detail"):
                text += f": {entry['detail']}"
            lines.append(text)
            for field in entry.get("fields", []):
                lines.append(
                    f"      {field['field']}: {_value(field['before'])} -> {_value(field['after'])}"
                )

    section("Current state", summary["current"], "*", "none reported")
    if phase != "read-only":
        section(verbs[0], summary["add"], "+")
        section(verbs[1], summary["change"], "~")
        section(verbs[2], summary["delete"], "-", summary.get("delete_note", "none"))
        if summary["unchanged"]:
            section("Unchanged", summary["unchanged"], "=")
    else:
        lines.append("Changes: none; this step is read-only")
    if summary["blockers"]:
        section("Blockers", [{"item": item} for item in summary["blockers"]], "!")
    for note in summary["notes"]:
        lines.append(f"Note: {note}")
    if summary.get("next"):
        lines.append(f"Next: {summary['next']}")
    return "\n".join(lines)
