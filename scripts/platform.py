#!/usr/bin/env python3
"""Purpose: expose the local-platform workflow through a single validated command boundary."""

import argparse
import json
import shutil
import sys
from pathlib import Path

from config import load_config
from io_utils import private_dir, read_json, workspace_lock, write_json

ROOT = Path(__file__).resolve().parents[1]


# Inputs: normalized config and private workspace. Outputs: configuration summary.
# Purpose: materialize isolated Terraform roots without modifying packaged assets. Dependencies: assets/infra.
def configure(config, workspace):
    saved = workspace / "environment.json"
    if saved.exists():
        previous = read_json(saved)
        binding = (previous["environment_id"], previous["cluster"])
        if binding != (config["environment_id"], config["cluster"]):
            raise ValueError(
                "This workspace is already bound to another environment or cluster configuration"
            )
        removed = set(previous["derived"]["stages"]) - set(config["derived"]["stages"])
        if any((workspace / "state" / f"{stage}.tfstate").exists() for stage in removed):
            raise ValueError(
                "Disabling a managed stage requires a separate data and ownership migration"
            )
    elif any(item.name != ".operation.lock" for item in workspace.iterdir()):
        raise ValueError("Choose an empty workspace; existing files will not be adopted")
    source = ROOT / "assets" / "infra" / "stacks"
    for directory in ("stages", "state", "plans", "reports", "credentials"):
        private_dir(workspace / directory)
    for stage in config["derived"]["stages"]:
        destination = private_dir(workspace / "stages" / stage)
        expected = {
            path.relative_to(source / stage)
            for path in (source / stage).rglob("*")
            if path.is_file()
        }
        existing = {
            path.relative_to(destination)
            for path in destination.rglob("*")
            if path.is_file() and ".terraform" not in path.relative_to(destination).parts
        }
        unexpected = existing - expected - {Path("environment.auto.tfvars.json")}
        if unexpected:
            raise ValueError(
                "Materialized stage contains files absent from this engine version; review the engine migration before proceeding"
            )
        for path in (source / stage).rglob("*"):
            relative = path.relative_to(source / stage)
            target = destination / relative
            if path.is_symlink() or target.is_symlink():
                raise ValueError("Stage assets cannot contain symbolic links")
            if path.is_dir():
                private_dir(target)
            else:
                private_dir(target.parent)
                shutil.copyfile(path, target)
                target.chmod(0o600)
        write_json(destination / "environment.auto.tfvars.json", {"environment": config})
    write_json(saved, config)
    interfaces = connection_contract(config)
    write_json(workspace / "reports" / "interfaces.json", interfaces)
    return {
        "status": "configured",
        "environment_id": config["environment_id"],
        "stages": config["derived"]["stages"],
        "workspace": str(workspace),
        "interfaces": interfaces,
        "runtime_acceptance": "not-executed",
    }


# Inputs: normalized config. Outputs: non-sensitive application integration contract.
# Purpose: provide consistent endpoints and credential references. Dependencies: naming contract.
def connection_contract(config):
    derived = config["derived"]
    result = {
        "application_namespace": config["application"]["namespace"],
        "service_account": config["application"]["service_account"],
        "status": "planned-not-connectivity-verified",
    }
    if config["postgres"]["enabled"]:
        result["postgres"] = {
            "host": f"{derived['postgres_name']}-rw.{derived['namespace']}.svc.cluster.local",
            "port": 5432,
            "database": config["application"]["database"],
            "sslmode": "verify-full",
            "secret": f"{derived['postgres_name']}-client",
            "secret_keys": ["username", "password", "ca.crt"],
            "required_pod_labels": {"local-k8s-platform/access-postgres": "true"},
        }
    if config["kafka"]["enabled"]:
        name = derived["kafka_name"]
        result["kafka"] = {
            "bootstrap_servers": f"{name}-kafka-bootstrap.{config['kafka']['namespace']}.svc.cluster.local:9093",
            "security_protocol": "SSL",
            "topics": config["kafka"]["topics"],
            "consumer_group": config["kafka"]["consumer_group"],
            "producer_secret": f"{name}-producer-client",
            "consumer_secret": f"{name}-consumer-client",
            "secret_keys": ["user.crt", "user.key", "ca.crt"],
            "required_pod_labels": {"local-k8s-platform/access-kafka": "true"},
        }
    return result


# Inputs: CLI arguments. Outputs: argparse namespace. Purpose: validate the command interface once.
# Dependencies: argparse; configuration contents are validated by load_config.
def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("configure", "inspect", "verify"):
        actions.add_parser(name)
    plan = actions.add_parser("plan")
    plan.add_argument("--stage", required=True)
    plan.add_argument("--plugin-dir", type=Path)
    apply = actions.add_parser("apply")
    apply.add_argument("--stage", required=True)
    apply.add_argument("--plan-id", required=True)
    apply.add_argument("--execute", action="store_true")
    credentials = actions.add_parser("credentials")
    credentials.add_argument("--component", choices=("postgres", "kafka"), default="postgres")
    mode = credentials.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "credentials" and args.component == "kafka" and args.prepare:
        parser.error("Kafka source credentials are prepared by Strimzi, not this tool")
    return args


# Inputs: optional CLI argv. Outputs: process exit status. Purpose: dispatch one locked operation.
# Dependencies: config validation, private workspace, and the requested runtime module.
def main(argv=None):
    try:
        args = arguments(argv)
        config = load_config(args.config)
        if args.workspace.is_symlink():
            raise ValueError("Workspace cannot be a symbolic link")
        workspace = args.workspace.expanduser().resolve()
        if workspace == ROOT or ROOT.is_relative_to(workspace) or workspace.is_relative_to(ROOT):
            raise ValueError("Workspace must be outside the installed skill and cannot contain it")
        if args.action in {"plan", "apply"} and args.stage not in config["derived"]["stages"]:
            raise ValueError("Stage is not enabled by this configuration")
        if args.action == "credentials" and not config[args.component]["enabled"]:
            raise ValueError("The requested credential component is disabled")
        with workspace_lock(workspace):
            configured = configure(config, workspace)
            if args.action == "configure":
                report = configured
            elif args.action == "credentials":
                from credentials import credentials

                report = credentials(
                    config, workspace, args.component, prepare=args.prepare, execute=args.execute
                )
            else:
                import runtime

                if args.action == "plan":
                    report = runtime.plan(config, workspace, args.stage, args.plugin_dir)
                elif args.action == "apply":
                    report = runtime.apply(
                        config, workspace, args.stage, args.plan_id, args.execute
                    )
                else:
                    report = getattr(runtime, args.action)(config, workspace)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2 if report.get("status") in {"blocked", "failed", "fail"} else 0
    except (ValueError, RuntimeError, OSError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
