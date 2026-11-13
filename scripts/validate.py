#!/usr/bin/env python3
"""Purpose: isolate provider startup and validate packaged Terraform profiles without cluster access."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from config import load_config
from io_utils import private_dir, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


# Inputs: target directory, Terraform arguments, diagnostic label. Outputs: captured command result.
# Purpose: run only offline Terraform operations with private diagnostics. Dependencies: installed Terraform.
def terraform(directory, arguments, label):
    diagnostics = private_dir(directory / "diagnostics")
    trace = diagnostics / f"{label}.trace.log"
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("TF_", "PLUGIN_"))
    }
    environment.update(
        TF_IN_AUTOMATION="1",
        TF_INPUT="0",
        TF_WORKSPACE="default",
        TF_LOG="TRACE",
        TF_LOG_PATH=str(trace),
        CHECKPOINT_DISABLE="1",
    )
    try:
        process = subprocess.run(
            ["terraform", f"-chdir={directory}", *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            umask=0o077,
            env=environment,
        )
        result = {
            "returncode": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
    except subprocess.TimeoutExpired:
        result = {
            "returncode": None,
            "stdout": "",
            "stderr": "Command timed out",
            "cause": "command_timeout",
        }
    except OSError:
        result = {
            "returncode": None,
            "stdout": "",
            "stderr": "Terraform could not start",
            "cause": "executable_unavailable",
        }
    write_json(diagnostics / f"{label}.json", result)
    result["diagnostic_path"] = str(diagnostics / f"{label}.json")
    result["trace"] = trace.read_text(encoding="utf-8", errors="replace") if trace.exists() else ""
    return result


# Inputs: captured Terraform result and fallback cause. Outputs: precise failure category.
# Purpose: distinguish local transport denial from configuration errors. Dependencies: private trace evidence.
def failure_cause(result, fallback):
    diagnostic = "\n".join(result.get(key, "") for key in ("stdout", "stderr", "trace"))
    if "listen unix" in diagnostic and "bind: operation not permitted" in diagnostic:
        return "local_socket_denied"
    return result.get("cause", fallback)


# Inputs: packaged stage directory and private destination. Outputs: none.
# Purpose: copy immutable stage sources into isolated validation workdirs. Dependencies: filesystem and packaged assets.
def copy_stage(source, destination):
    private_dir(destination)
    for path in source.rglob("*"):
        target = destination / path.relative_to(source)
        if path.is_symlink():
            raise ValueError("Validation assets cannot contain symbolic links")
        if path.is_dir():
            private_dir(target)
        else:
            private_dir(target.parent)
            shutil.copyfile(path, target)
            target.chmod(0o600)


# Inputs: private report root. Outputs: normalized named environment profiles.
# Purpose: cover supported data-service, monitoring, and storage combinations. Dependencies: config.load_config boundary.
def profiles(report_dir):
    private_dir(report_dir / "profiles")
    for data, postgres, kafka in (
        ("postgres", True, False),
        ("kafka", False, True),
        ("both", True, True),
    ):
        for monitoring in (False, True):
            for storage in ("reuse", "create"):
                name = f"{data}-monitoring-{'on' if monitoring else 'off'}-storage-{storage}"
                directory = private_dir(report_dir / "profiles" / name)
                source = directory / "input.json"
                write_json(
                    source,
                    {
                        "environment_id": "validation",
                        "cluster": {"kubeconfig": str(report_dir / "offline.kubeconfig")},
                        "postgres": {"enabled": postgres},
                        "kafka": {"enabled": kafka},
                        "monitoring": {"enabled": monitoring},
                        "storage": {
                            "mode": storage,
                            "class": "dev-retain" if storage == "reuse" else "",
                            "nodes": ["offline-node"] if storage == "create" else [],
                        },
                    },
                )
                yield name, directory, load_config(source)


# Inputs: report directory and optional provider mirror. Outputs: persisted validation summary.
# Purpose: diagnose provider startup before evaluating all supported stage profiles. Dependencies: packaged provider pins and Terraform.
def validate(report_dir, plugin_dir=None):
    report_dir = Path(report_dir).expanduser()
    if report_dir.is_symlink():
        raise ValueError("Report directory cannot be a symbolic link")
    report_dir = report_dir.resolve()
    if report_dir == ROOT or ROOT.is_relative_to(report_dir) or report_dir.is_relative_to(ROOT):
        raise ValueError(
            "Report directory must be outside the installed skill and cannot contain it"
        )
    if report_dir.exists() and (not report_dir.is_dir() or any(report_dir.iterdir())):
        raise ValueError(
            "Report directory must be empty; existing evidence will not be overwritten"
        )
    if plugin_dir is not None:
        plugin_dir = Path(plugin_dir).expanduser().resolve()
        if not plugin_dir.is_dir():
            raise ValueError("Provider mirror must be an existing directory")
    private_dir(report_dir)
    assets = ROOT / "assets" / "infra"
    provider = read_json(assets / "versions.json")["provider"]
    report = {
        "status": "running",
        "provider": {"source": provider["source"], "version": provider["version"]},
        "profiles_planned": 12,
        "cases_planned": 40,
        "cases": [],
        "validation_scope": "Static Terraform/provider schema; live CRD and runtime acceptance are not checked",
    }
    init = ["init", "-backend=false", "-lockfile=readonly", "-input=false", "-no-color"]
    if plugin_dir is not None:
        init.append(f"-plugin-dir={plugin_dir}")
    probe = private_dir(report_dir / "provider-probe")
    (probe / "main.tf").write_text(
        "# Purpose: isolate the pinned provider schema without resources or cluster configuration.\n"
        f'terraform {{\n  required_providers {{\n    kubernetes = {{ source = "{provider["source"]}", version = "= {provider["version"]}" }}\n  }}\n}}\n',
        encoding="utf-8",
    )
    (probe / "main.tf").chmod(0o600)
    shutil.copyfile(
        assets / "stacks" / "00-bootstrap" / ".terraform.lock.hcl", probe / ".terraform.lock.hcl"
    )
    (probe / ".terraform.lock.hcl").chmod(0o600)
    result = terraform(probe, ["version", "-json"], "version")
    try:
        if result["returncode"] != 0:
            raise ValueError("Version command failed")
        report["terraform_version"] = json.loads(result["stdout"])["terraform_version"]
    except (ValueError, KeyError, TypeError):
        report.update(
            status="blocked",
            cause=failure_cause(result, "terraform_version_unavailable"),
            diagnostic_path=result["diagnostic_path"],
        )
        write_json(report_dir / "report.json", report)
        return report
    result = terraform(probe, ["validate", "-help"], "validate-help")
    if result["returncode"] != 0 or "-var-file" not in result["stdout"]:
        report.update(
            status="blocked",
            cause="validate_variable_files_unsupported",
            diagnostic_path=result["diagnostic_path"],
        )
        write_json(report_dir / "report.json", report)
        return report
    for label, arguments in (("init", init), ("schema", ["providers", "schema", "-json"])):
        result = terraform(probe, arguments, label)
        if result["returncode"] != 0:
            report.update(
                status="blocked",
                cause=failure_cause(result, f"provider_{label}_failed"),
                diagnostic_path=result["diagnostic_path"],
            )
            write_json(report_dir / "report.json", report)
            return report
    try:
        schema = json.loads(result["stdout"])
        if f"registry.terraform.io/{provider['source']}" not in schema["provider_schemas"]:
            raise ValueError("Pinned provider is absent from schema output")
    except (ValueError, KeyError, TypeError):
        report.update(
            status="blocked",
            cause="provider_schema_output_invalid",
            diagnostic_path=result["diagnostic_path"],
        )
        write_json(report_dir / "report.json", report)
        return report
    report["provider_probe"] = "pass"
    write_json(
        report_dir / "offline.kubeconfig",
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": "offline", "cluster": {"server": "https://127.0.0.1:1"}}],
            "contexts": [
                {"name": "minikube", "context": {"cluster": "offline", "user": "offline"}}
            ],
            "users": [{"name": "offline", "user": {}}],
            "current-context": "minikube",
        },
    )
    for name, directory, config in profiles(report_dir):
        private_dir(directory / "stages")
        for stage in config["derived"]["stages"]:
            target = directory / "stages" / stage
            copy_stage(assets / "stacks" / stage, target)
            write_json(target / "environment.auto.tfvars.json", {"environment": config})
            case = {"profile": name, "stage": stage, "status": "blocked"}
            initialized = terraform(target, init, "init")
            if initialized["returncode"] != 0:
                case.update(
                    cause=failure_cause(initialized, "stage_initialization_failed"),
                    diagnostic_path=initialized["diagnostic_path"],
                )
            else:
                result = terraform(
                    target,
                    ["validate", "-json", "-var-file=environment.auto.tfvars.json"],
                    "validate",
                )
                case["diagnostic_path"] = result["diagnostic_path"]
                try:
                    validation = json.loads(result["stdout"])
                    case.update(
                        status="pass"
                        if result["returncode"] == 0 and validation["valid"] is True
                        else "failed",
                        error_count=validation["error_count"],
                        warning_count=validation["warning_count"],
                    )
                except (ValueError, KeyError, TypeError):
                    case.update(cause=failure_cause(result, "validation_output_invalid"))
                cause = failure_cause(result, "")
                if result["returncode"] != 0 and cause in {
                    "local_socket_denied",
                    "command_timeout",
                    "executable_unavailable",
                }:
                    case.update(status="blocked", cause=cause)
            report["cases"].append(case)
            write_json(report_dir / "report.json", report)
    report["status"] = (
        "blocked"
        if any(case["status"] == "blocked" for case in report["cases"])
        else "failed"
        if any(case["status"] == "failed" for case in report["cases"])
        else "pass"
    )
    report["cases_passed"] = sum(case["status"] == "pass" for case in report["cases"])
    write_json(report_dir / "report.json", report)
    return report


# Inputs: CLI argv. Outputs: process status. Purpose: expose one non-mutating validation run.
# Dependencies: argparse and the validate runner; no kubectl, plan, apply, or provider modifications.
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--plugin-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate(args.report_dir, args.plugin_dir)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] == "pass" else 2
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
