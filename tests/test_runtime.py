# Purpose: Exercise plan integrity, ownership, stage readiness and read-only reporting with isolated fake processes.
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import runtime
from config import load_config
from delete_safety import _artifact
from io_utils import private_dir, read_json, write_json


# Inputs: temporary workspace. Outputs: valid normalized PostgreSQL-only configuration.
# Purpose: Build a real input-boundary fixture without using personal kubeconfig files. Dependencies: config.load_config.
def configuration(workspace):
    kubeconfig = workspace / "kubeconfig"
    kubeconfig.write_text("# Purpose: fake test kubeconfig; no credentials.\n")
    raw = {
        "environment_id": "example",
        "cluster": {"kubeconfig": str(kubeconfig)},
        "kafka": {"enabled": False},
        "monitoring": {"enabled": False},
        "storage": {"class": "local-retain"},
    }
    path = workspace / "input.json"
    write_json(path, raw)
    return load_config(path)


# Inputs: name and optional namespace. Outputs: Terraform namespace creation plan.
# Purpose: Provide a minimal non-sensitive resource change for plan review. Dependencies: Terraform JSON convention.
def creation(name="example-app", namespace=None):
    metadata = {"name": name, "labels": {runtime.ENVIRONMENT_LABEL: "example"}}
    if namespace:
        metadata["namespace"] = namespace
    return {
        "resource_changes": [
            {
                "address": "kubernetes_namespace_v1.app",
                "type": "kubernetes_namespace_v1",
                "mode": "managed",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"metadata": [metadata]},
                },
            }
        ]
    }


# Inputs: CPU requests before and after. Outputs: Terraform Deployment update plan.
# Purpose: exercise key-field review and live drift checks. Dependencies: Terraform JSON convention.
def deployment_update(before_cpu="100m", after_cpu="200m"):
    def values(cpu):
        return {
            "metadata": [{
                "name": "example-api", "namespace": "example",
                "labels": {runtime.ENVIRONMENT_LABEL: "example"}, "uid": "deploy-uid",
            }],
            "spec": [{
                "replicas": "1",
                "template": [{"spec": [{"container": [{
                    "name": "api",
                    "image": "example/api:1",
                    "env": [{"name": "TOKEN", "value": "never-shown"}],
                    "resources": [{"requests": {"cpu": cpu, "memory": "128Mi"}}],
                }]}]}],
            }],
        }
    return {
        "resource_changes": [{
            "address": "kubernetes_deployment_v1.api",
            "type": "kubernetes_deployment_v1",
            "mode": "managed",
            "change": {"actions": ["update"], "before": values(before_cpu), "after": values(after_cpu)},
        }]
    }


# Inputs: optional CPU request. Outputs: live Deployment as the API server returns it.
# Purpose: provide the live object an update is reviewed against. Dependencies: none.
def live_deployment(cpu="100m"):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "example-api", "namespace": "example", "uid": "deploy-uid",
            "labels": {runtime.ENVIRONMENT_LABEL: "example"},
            "annotations": {"deployment.kubernetes.io/revision": "1"},
            "resourceVersion": "100", "generation": 1, "managedFields": [{"manager": "terraform"}],
        },
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [{
                "name": "api", "image": "example/api:1",
                "resources": {"requests": {"cpu": cpu, "memory": "128Mi"}},
            }]}},
        },
        "status": {"observedGeneration": 1, "readyReplicas": 1},
    }


class RuntimeTests(unittest.TestCase):
    # Inputs: none. Outputs: isolated workspace and source fixture on self.
    # Purpose: Ensure every test uses disposable state and mock-only cluster access. Dependencies: tempfile and fixture helpers.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.config = configuration(self.workspace)
        self.identity = {
            "context": "minikube",
            "kubeconfig": self.config["cluster"]["kubeconfig"],
            "kubeconfig_sha256": "fixture",
            "server": "https://127.0.0.1:8443",
            "kube_system_uid": "cluster-one",
        }
        stage = private_dir(private_dir(self.workspace / "stages") / "00-bootstrap")
        (stage / "main.tf").write_text("# Purpose: disposable Terraform source fixture.\n")
        private_dir(self.workspace / "state")
        self.document = creation()

    # Inputs: Terraform command, stage directory, log path. Outputs: successful fake process.
    # Purpose: Materialize only the plan/state files requested by runtime. Dependencies: temporary workspace.
    def fake_terraform(self, command, stage_directory, log):
        if command[0] == "plan":
            output = next(
                value.removeprefix("-out=") for value in command if value.startswith("-out=")
            )
            Path(output).write_bytes(b"saved plan fixture")
        if command[0] == "apply":
            write_json(self.workspace / "state" / "00-bootstrap.tfstate", {"serial": 1})
        return subprocess.CompletedProcess(command, 0, "", "")

    # Inputs: command and optional runner kwargs. Outputs: fake Terraform show JSON.
    # Purpose: Supply structured external output without executing Terraform or kubectl. Dependencies: current test document.
    def fake_show(self, command, **kwargs):
        self.assertEqual(command[0], "terraform")
        self.assertIn("show", command)
        return subprocess.CompletedProcess(command, 0, json.dumps(self.document), "")

    # Inputs: none. Outputs: valid saved plan review.
    # Purpose: Share deterministic plan setup across integrity tests. Dependencies: mocked subprocess and inventory boundaries.
    def make_plan(self):
        with (
            patch.object(
                runtime, "inspect", return_value={"status": "pass", "identity": self.identity}
            ),
            patch.object(runtime, "_terraform", side_effect=self.fake_terraform),
            patch.object(runtime, "run", side_effect=self.fake_show),
            patch.object(runtime, "_get", return_value=None),
        ):
            return runtime.plan(self.config, self.workspace, "00-bootstrap")

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove review creation binds saved bytes and dry-run apply invokes no process. Dependencies: make_plan and apply preview.
    def test_saved_plan_preview_is_offline_and_private(self):
        result = self.make_plan()
        self.assertEqual(result["status"], "planned")
        saved = self.workspace / "plans" / "00-bootstrap" / "plan.tfplan"
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        with patch.object(
            runtime, "run", side_effect=AssertionError("Preview must not execute a process")
        ):
            preview = runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"])
        self.assertEqual(preview["status"], "preview")
        self.assertTrue(preview["requires_execute"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove source edits invalidate an otherwise reviewed plan. Dependencies: source digest checks.
    def test_source_modification_blocks_apply(self):
        result = self.make_plan()
        (self.workspace / "stages" / "00-bootstrap" / "main.tf").write_text(
            "# Purpose: changed fixture.\n"
        )
        with self.assertRaisesRegex(RuntimeError, "changed"):
            runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove normalized configuration changes invalidate saved plans. Dependencies: config digest checks.
    def test_configuration_modification_blocks_apply(self):
        result = self.make_plan()
        changed = deepcopy(self.config)
        changed["application"]["database"] = "another_database"
        with self.assertRaisesRegex(RuntimeError, "changed"):
            runtime.apply(changed, self.workspace, "00-bootstrap", result["plan_id"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove state changes invalidate a plan before external calls. Dependencies: state digest checks.
    def test_state_modification_blocks_apply(self):
        result = self.make_plan()
        write_json(self.workspace / "state" / "00-bootstrap.tfstate", {"serial": 99})
        with self.assertRaisesRegex(RuntimeError, "changed"):
            runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove failed replanning quarantines the old usable plan. Dependencies: stale-plan archive handling.
    def test_failed_replan_quarantines_previous_plan(self):
        previous = self.make_plan()
        with patch.object(runtime, "inspect", return_value={"status": "blocked"}):
            with self.assertRaisesRegex(RuntimeError, "Preflight"):
                runtime.plan(self.config, self.workspace, "00-bootstrap")
        directory = self.workspace / "plans" / "00-bootstrap"
        self.assertFalse((directory / "review.json").exists())
        self.assertTrue((directory / "failed.json").exists())
        self.assertEqual(len(list((self.workspace / "plans" / "archive").iterdir())), 1)
        with self.assertRaises(RuntimeError):
            runtime.apply(self.config, self.workspace, "00-bootstrap", previous["plan_id"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove an existing resource cannot be adopted even with a matching environment label. Dependencies: live collision query boundary.
    def test_create_collision_is_rejected(self):
        existing = {
            "metadata": {"name": "example-app", "labels": {runtime.ENVIRONMENT_LABEL: "example"}}
        }
        with patch.object(runtime, "_get", return_value=existing):
            with self.assertRaisesRegex(RuntimeError, "automatic adoption"):
                runtime._review_changes(self.config, creation())

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove deletion and Terraform Secret resources never become approved plans. Dependencies: change allowlist.
    def test_delete_and_secret_resources_are_rejected(self):
        document = creation()
        document["resource_changes"][0]["change"]["actions"] = ["delete", "create"]
        with self.assertRaisesRegex(RuntimeError, "Destructive"):
            runtime._review_changes(self.config, document)
        document = creation()
        document["resource_changes"][0]["type"] = "kubernetes_secret_v1"
        with self.assertRaisesRegex(RuntimeError, "Secret data"):
            runtime._review_changes(self.config, document)

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove a cluster recreated under the same context cannot execute an old plan. Dependencies: identity binding.
    def test_cluster_replacement_blocks_execute(self):
        result = self.make_plan()
        replacement = {**self.identity, "kube_system_uid": "cluster-two"}
        with (
            patch.object(
                runtime, "inspect", return_value={"status": "pass", "identity": replacement}
            ),
            patch.object(
                runtime, "_terraform", side_effect=AssertionError("Apply must be blocked")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "identity"):
                runtime.apply(
                    self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True
                )

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove successful execution consumes the exact plan and creates an applied-stage record. Dependencies: mocked Terraform apply.
    def test_execute_consumes_plan_once(self):
        result = self.make_plan()
        with (
            patch.object(
                runtime, "inspect", return_value={"status": "pass", "identity": self.identity}
            ),
            patch.object(runtime, "_get", return_value=None),
            patch.object(runtime, "_terraform", side_effect=self.fake_terraform),
        ):
            record = runtime.apply(
                self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True
            )
        self.assertEqual(record["status"], "applied")
        self.assertEqual(
            read_json(self.workspace / "reports" / "applied-00-bootstrap.json")["plan_id"],
            result["plan_id"],
        )
        with self.assertRaisesRegex(RuntimeError, "consumed"):
            runtime.apply(
                self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True
            )

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove a later stage cannot bypass required previous application. Dependencies: applied-stage prerequisite records.
    def test_postgres_requires_bootstrap_application(self):
        with self.assertRaisesRegex(RuntimeError, "00-bootstrap"):
            runtime._prerequisites(self.config, self.workspace, "30-postgres")

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove Secret readiness requests metadata only. Dependencies: mocked kubectl argument capture.
    def test_postgres_secret_readiness_does_not_read_data(self):
        write_json(self.workspace / "reports" / "applied-00-bootstrap.json", {"status": "applied"})
        with (
            patch.object(runtime, "_stage_ready", return_value=(True, "Ready")),
            patch.object(
                runtime, "run", return_value=subprocess.CompletedProcess([], 0, "secret-uid", "")
            ) as runner,
        ):
            runtime._prerequisites(self.config, self.workspace, "30-postgres")
        self.assertEqual(runner.call_count, 4)
        for call in runner.call_args_list:
            command = call.args[0]
            self.assertIn("jsonpath={.metadata.uid}", command)
            self.assertNotIn("json", command)
            self.assertIn(self.config["cluster"]["context"], command)
            self.assertIn(self.config["cluster"]["kubeconfig"], command)

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove profile-aware verification omits Kafka/CNPG while keeping functional acceptance incomplete. Dependencies: read-only GET mocks.
    def test_verify_keeps_baseline_separate_from_acceptance(self):
        with (
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_stage_ready", return_value=(True, "Ready")),
            patch.object(runtime, "_get", return_value={"status": {"phase": "Bound"}}) as getter,
        ):
            report = runtime.verify(self.config, self.workspace)
        self.assertEqual(report["baseline_result"], "pass")
        self.assertEqual(report["acceptance_result"], "incomplete")
        self.assertTrue(any("restore" in item for item in report["pending_acceptance"]))
        resources = [call.args[1] for call in getter.call_args_list]
        self.assertNotIn("kafkas.kafka.strimzi.io", resources)
        self.assertNotIn("clusters.postgresql.cnpg.io", resources)

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove an unreadable capacity summary blocks inspection rather than using allocatable values. Dependencies: mocked inventory GETs.
    def test_missing_measured_capacity_fails_closed(self):
        node = {
            "metadata": {"name": "minikube", "labels": {"minikube.k8s.io/name": "minikube"}},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "allocatable": {"memory": "999Gi"},
                "nodeInfo": {"operatingSystem": "linux", "architecture": "arm64"},
            },
        }

        # Inputs: configuration and resource query. Outputs: fake basic inventory.
        # Purpose: Supply only the reads needed before the missing summary. Dependencies: node fixture.
        def fake_get(config, resource, name=None, namespace=None):
            return {"items": [node]} if resource == "nodes" else {"items": []}

        with (
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_get", side_effect=fake_get),
            patch.object(
                runtime, "run", return_value=subprocess.CompletedProcess([], 0, '{"node": {}}', "")
            ),
        ):
            report = runtime.inspect(self.config, self.workspace)
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any(check["name"] == "inventory_access" for check in report["checks"]))

    # Inputs: none. Outputs: assertions only.
    # Purpose: Prove already-applied demand is subtracted from future capacity checks. Dependencies: applied accounting record.
    def test_remaining_demand_excludes_applied_stage(self):
        config = deepcopy(self.config)
        config["derived"]["memory_requests_by_stage"] = {"00-bootstrap": 0, "30-postgres": 384}
        config["derived"]["storage_mib_by_stage"] = {"00-bootstrap": 0, "30-postgres": 4096}
        write_json(
            self.workspace / "reports" / "applied-30-postgres.json",
            {"status": "applied", "memory_requests_mib": 384, "storage_mib": 4096},
        )
        self.assertEqual(runtime._remaining_workloads(config, self.workspace), [])

    # Inputs: shrinking Kafka manifests and a PVC. Outputs: blocked reviews before any live request.
    # Purpose: protect existing data when defaults and retention targets become smaller. Dependencies: Terraform change review.
    def test_smaller_defaults_cannot_shrink_existing_data(self):
        labels = {runtime.ENVIRONMENT_LABEL: "example"}
        manifests = [
            (
                {"kind": "KafkaTopic", "spec": {"partitions": 3}},
                {"kind": "KafkaTopic", "spec": {"partitions": 1}},
            ),
            (
                {
                    "kind": "KafkaTopic",
                    "spec": {"partitions": 1, "config": {"retention.ms": "86400000"}},
                },
                {
                    "kind": "KafkaTopic",
                    "spec": {"partitions": 1, "config": {"retention.ms": "21600000"}},
                },
            ),
            (
                {
                    "kind": "KafkaNodePool",
                    "spec": {"storage": {"volumes": [{"id": 0, "size": "4Gi"}]}},
                },
                {
                    "kind": "KafkaNodePool",
                    "spec": {"storage": {"volumes": [{"id": 0, "size": "1Gi"}]}},
                },
            ),
        ]
        for old, new in manifests:
            for item in (old, new):
                item["metadata"] = {"name": "data", "namespace": "kafka", "labels": labels}
            document = {
                "resource_changes": [
                    {
                        "type": "kubernetes_manifest",
                        "change": {
                            "actions": ["update"],
                            "before": {"manifest": old},
                            "after": {"manifest": new},
                        },
                    }
                ]
            }
            with (
                self.subTest(kind=old["kind"]),
                patch.object(runtime, "_get", side_effect=AssertionError("live request")),
                self.assertRaises(RuntimeError),
            ):
                runtime._review_changes(self.config, document)
        old = {
            "metadata": [{"name": "data", "labels": labels}],
            "spec": [{"resources": [{"requests": {"storage": "4Gi"}}]}],
        }
        new = deepcopy(old)
        new["spec"][0]["resources"][0]["requests"]["storage"] = "1Gi"
        with self.assertRaisesRegex(RuntimeError, "shrinking"):
            runtime._review_changes(
                self.config,
                {
                    "resource_changes": [
                        {
                            "type": "kubernetes_persistent_volume_claim_v1",
                            "change": {"actions": ["update"], "before": old, "after": new},
                        }
                    ]
                },
            )

    # Inputs: none. Outputs: assertions only.
    # Purpose: Allow fresh plans after kubeconfig credentials refresh without retargeting the workspace. Dependencies: durable identity binding.
    def test_workspace_binding_allows_kubeconfig_refresh(self):
        self.assertTrue(runtime._bind(self.config, self.workspace, self.identity))
        refreshed = {**self.identity, "kubeconfig_sha256": "new-authentication-material"}
        self.assertTrue(runtime._bind(self.config, self.workspace, refreshed))
        replaced = {**refreshed, "kube_system_uid": "another-cluster"}
        self.assertFalse(runtime._bind(self.config, self.workspace, replaced))

    # Inputs: none. Outputs: assertions only.
    # Purpose: Ensure an uncertain Terraform timeout consumes the plan before mutation dispatch. Dependencies: mocked timeout boundary.
    def test_apply_timeout_consumes_plan(self):
        result = self.make_plan()
        with (
            patch.object(
                runtime, "inspect", return_value={"status": "pass", "identity": self.identity}
            ),
            patch.object(runtime, "_get", return_value=None),
            patch.object(runtime, "_terraform", side_effect=RuntimeError("Command timed out")),
        ):
            with self.assertRaisesRegex(RuntimeError, "actual state"):
                runtime.apply(
                    self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True
                )
        self.assertEqual(
            read_json(self.workspace / "plans" / "00-bootstrap" / "consumed.json")["status"],
            "uncertain",
        )
        with self.assertRaisesRegex(RuntimeError, "consumed"):
            runtime.apply(
                self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True
            )

    # Inputs: optional live object before planning and after refresh. Outputs: reviewed update plan.
    # Purpose: share update-plan setup across drift tests. Dependencies: make_plan.
    def make_update_plan(self, live=None, *, after_refresh=None):
        self.document = deployment_update()
        item = self.document["resource_changes"][0]
        write_json(self.workspace / "state" / "00-bootstrap.tfstate", {
            "resources": [{
                "mode": "managed", "type": item["type"],
                "instances": [{"attributes": item["change"]["before"]}],
            }],
        })
        before = live or live_deployment()
        with patch.object(runtime, "_get", side_effect=[before, after_refresh or before]):
            with (
                patch.object(runtime, "inspect", return_value={"status": "pass", "identity": self.identity}),
                patch.object(runtime, "_terraform", side_effect=self.fake_terraform),
                patch.object(runtime, "run", side_effect=self.fake_show),
            ):
                return runtime.plan(self.config, self.workspace, "00-bootstrap")

    # Inputs: none. Outputs: assertions. Purpose: a concurrent edit between provider refresh
    # and review invalidates the plan before it can be previewed. Dependencies: fake live GETs.
    def test_review_rejects_drift_during_planning(self):
        changed = live_deployment(cpu="300m")
        changed["metadata"]["resourceVersion"] = "101"
        with self.assertRaisesRegex(RuntimeError, "changed during planning"):
            self.make_update_plan(after_refresh=changed)
        directory = self.workspace / "plans" / "00-bootstrap"
        self.assertTrue((directory / "failed.json").exists())
        self.assertFalse((directory / "review.json").exists())

    # Inputs: none. Outputs: assertions. Purpose: redact both values inside an allowed composite
    # field in the saved review, printed plan and preview. Dependencies: fake update plan.
    def test_nested_sensitive_fields_are_redacted_in_plan_and_preview(self):
        document = deployment_update()
        change = document["resource_changes"][0]["change"]
        mask = {"spec": [{"template": [{"spec": [{"container": [{
            "resources": [{"requests": {"memory": True}}],
        }]}]}]}]}
        change["after_sensitive"] = change["before_sensitive"] = mask
        with patch.object(sys.modules[__name__], "deployment_update", return_value=document):
            result = self.make_update_plan()
        preview = runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"])
        path = "spec.0.template.0.spec.0.container.0.resources"
        self.assertEqual(preview["changes"][0]["fields"][path]["after"], [
            {"requests": {"cpu": "200m", "memory": "(sensitive)"}},
        ])
        for output in (Path(result["review"]).read_text(), json.dumps(result), json.dumps(preview)):
            self.assertNotIn("128Mi", output)

    # Inputs: live object, Terraform stand-in. Outputs: apply result or raised error.
    # Purpose: run a reviewed execute against fake live state. Dependencies: apply.
    def execute(self, plan_id, live=None, terraform=None):
        with (
            patch.object(runtime, "inspect", return_value={"status": "pass", "identity": self.identity}),
            patch.object(runtime, "_get", return_value=live),
            patch.object(runtime, "_terraform", side_effect=terraform or self.fake_terraform),
        ):
            return runtime.apply(self.config, self.workspace, "00-bootstrap", plan_id, execute=True)

    # Inputs: none. Outputs: assertions only.
    # Purpose: prove reviews show key before/after values, never environment values, and record
    # the live configuration without copying it. Dependencies: make_update_plan.
    def test_update_review_shows_key_fields_without_secrets(self):
        result = self.make_update_plan()
        change = result["changes"][0]
        path = "spec.0.template.0.spec.0.container.0.resources"
        self.assertEqual(
            change["fields"][path]["before"], [{"requests": {"cpu": "100m", "memory": "128Mi"}}]
        )
        self.assertEqual(
            change["fields"][path]["after"], [{"requests": {"cpu": "200m", "memory": "128Mi"}}]
        )
        self.assertTrue(change["live_config_recorded"])
        self.assertNotIn("live_fingerprint", change)
        review_text = (self.workspace / "plans" / "00-bootstrap" / "review.json").read_text()
        self.assertNotIn("never-shown", review_text + json.dumps(result))
        self.assertNotIn('"100m"', json.dumps(read_json(Path(result["review"]))["changes"][0]["live_fingerprint"]))
        created = runtime._field_changes(
            "kubernetes_manifest",
            {
                "actions": ["create"],
                "after": {"manifest": {"spec": {"partitions": 3, "config": {
                    "retention.ms": "3600000", "min.insync.replicas": "1",
                }}}},
                "after_sensitive": {"manifest": {"spec": {"partitions": True}}},
            },
        )
        self.assertEqual(
            created,
            {"spec.config.retention.ms": {"after": "3600000"}, "spec.partitions": {"after": "(sensitive)"}},
        )
        updated = runtime._field_changes("kubernetes_manifest", {
            "actions": ["update"],
            "before": {
                "manifest": {"spec": {"partitions": 2}},
                "object": {"spec": {"partitions": 3}},
            },
            "after": {"manifest": {"spec": {"partitions": 4}}},
        })
        self.assertEqual(updated, {"spec.partitions": {"before": 3, "after": 4}})

    # Inputs: none. Outputs: assertions only.
    # Purpose: prove a same-UID, same-owner configuration change invalidates the plan with a kept
    # reason and never dispatches Terraform. Dependencies: execute.
    def test_same_uid_live_config_change_invalidates_plan(self):
        result = self.make_update_plan()
        changed = live_deployment(cpu="150m")
        with self.assertRaisesRegex(RuntimeError, "resources.requests.cpu"):
            self.execute(result["plan_id"], changed, AssertionError("Apply must be blocked"))
        directory = self.workspace / "plans" / "00-bootstrap"
        self.assertIn("changed after plan review", read_json(directory / "invalidated.json")["reason"])
        self.assertFalse((directory / "consumed.json").exists())
        with self.assertRaisesRegex(RuntimeError, "invalidated: Live configuration"):
            runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"])
        # Restoring the live value does not revive an invalidated plan.
        with self.assertRaisesRegex(RuntimeError, "invalidated"):
            self.execute(result["plan_id"], live_deployment(), AssertionError("Apply must be blocked"))

    # Inputs: none. Outputs: assertions only.
    # Purpose: prove status, metadata bookkeeping and controller annotations do not invalidate.
    # Dependencies: execute.
    def test_status_only_change_allows_apply(self):
        result = self.make_update_plan()
        noisy = live_deployment()
        noisy["status"] = {"observedGeneration": 2, "readyReplicas": 0, "conditions": [{"type": "Progressing"}]}
        noisy["metadata"].update(
            resourceVersion="250", generation=2, managedFields=[{"manager": "kube-controller-manager"}],
        )
        noisy["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "2"
        record = self.execute(result["plan_id"], noisy)
        self.assertEqual(record["status"], "applied")
        self.assertEqual(record["returncode"], 0)

    # Inputs: none. Outputs: assertions only.
    # Purpose: prove a definitive cluster replacement invalidates while a refreshed kubeconfig only
    # blocks. Dependencies: execute.
    def test_identity_replacement_invalidates_but_refresh_only_blocks(self):
        result = self.make_plan()
        refreshed = {**self.identity, "kubeconfig_sha256": "refreshed"}
        with (
            patch.object(runtime, "inspect", return_value={"status": "pass", "identity": refreshed}),
            patch.object(runtime, "_terraform", side_effect=AssertionError("blocked")),
        ):
            with self.assertRaisesRegex(RuntimeError, "identity or prerequisites"):
                runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True)
        directory = self.workspace / "plans" / "00-bootstrap"
        self.assertFalse((directory / "invalidated.json").exists())
        replaced = {**self.identity, "kube_system_uid": "cluster-two"}
        with (
            patch.object(runtime, "inspect", return_value={"status": "pass", "identity": replaced}),
            patch.object(runtime, "_terraform", side_effect=AssertionError("blocked")),
        ):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True)
        self.assertTrue((directory / "invalidated.json").exists())

    # Inputs: none. Outputs: assertions only.
    # Purpose: prove Ctrl-C during Terraform replaces an older `applied` report with this plan's
    # `uncertain` record, keeps the diagnostics path, and leaves the plan consumed. Dependencies: apply.
    def test_interrupt_supersedes_previous_applied_report(self):
        report = self.workspace / "reports" / "applied-00-bootstrap.json"
        write_json(report, {"stage": "00-bootstrap", "plan_id": "older", "status": "applied",
                            "applied_at": "2026-11-20T00:00:00+00:00"})
        result = self.make_plan()

        def interrupted(command, **kwargs):
            if "apply" in command:
                raise KeyboardInterrupt
            return self.fake_show(command, **kwargs)

        with (
            patch.object(runtime, "inspect", return_value={"status": "pass", "identity": self.identity}),
            patch.object(runtime, "_get", return_value=None),
            patch.object(runtime, "run", side_effect=interrupted),
            self.assertRaises(KeyboardInterrupt),
        ):
            runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True)
        record = read_json(report)
        self.assertEqual(record["plan_id"], result["plan_id"])
        self.assertEqual(record["status"], "uncertain")
        self.assertTrue(record["interrupted"])
        self.assertIsNone(record["returncode"])
        self.assertEqual(record["previous"]["plan_id"], "older")
        log = read_json(Path(record["diagnostics"]))
        self.assertIsNone(log["returncode"])
        self.assertIn("Interrupted", log["error"])
        consumed = read_json(self.workspace / "plans" / "00-bootstrap" / "consumed.json")
        self.assertEqual(consumed["status"], "uncertain")
        self.assertTrue(consumed["interrupted"])
        # The old success no longer satisfies dependent stages, and the plan cannot run again.
        with self.assertRaisesRegex(RuntimeError, "00-bootstrap must be applied"):
            runtime._prerequisites(self.config, self.workspace, "30-postgres")
        with self.assertRaisesRegex(RuntimeError, "consumed"):
            self.execute(result["plan_id"], None, AssertionError("Consumed plan must not run"))
        with self.assertRaisesRegex(RuntimeError, "consumed"):
            runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: prove timeouts and failed exits keep the exit code and diagnostics. Dependencies: apply.
    def test_timeout_and_failure_record_exit_code_and_diagnostics(self):
        result = self.make_plan()

        def timed_out(command, **kwargs):
            if "apply" in command:
                raise RuntimeError("Command timed out; partial output was suppressed")
            return self.fake_show(command, **kwargs)

        with (
            patch.object(runtime, "inspect", return_value={"status": "pass", "identity": self.identity}),
            patch.object(runtime, "_get", return_value=None),
            patch.object(runtime, "run", side_effect=timed_out),
            self.assertRaisesRegex(RuntimeError, "actual state"),
        ):
            runtime.apply(self.config, self.workspace, "00-bootstrap", result["plan_id"], execute=True)
        record = read_json(self.workspace / "reports" / "applied-00-bootstrap.json")
        self.assertEqual((record["status"], record["returncode"]), ("uncertain", None))
        self.assertIn("timed out", record["error"])
        self.assertIn("timed out", read_json(Path(record["diagnostics"]))["error"])

        result = self.make_plan()
        old_log = Path(record["diagnostics"])
        self.assertTrue(_artifact(old_log.relative_to(self.workspace).parts, False, ["00-bootstrap"]))
        archived = next((self.workspace / "plans" / "archive").glob("00-bootstrap-*"))
        archived_record = read_json(archived / "consumed.json")
        self.assertEqual(Path(archived_record["diagnostics"]), old_log)
        self.assertIn("timed out", read_json(old_log)["error"])

        def failed(command, stage_directory, log):
            return subprocess.CompletedProcess(command, 1, "", "")

        with self.assertRaisesRegex(RuntimeError, "apply failed"):
            self.execute(result["plan_id"], None, failed)
        record = read_json(self.workspace / "reports" / "applied-00-bootstrap.json")
        self.assertEqual((record["status"], record["returncode"]), ("failed", 1))
        self.assertEqual(record["previous"]["status"], "uncertain")
        self.assertNotEqual(Path(record["diagnostics"]), old_log)

    # Inputs: none. Outputs: assertions only.
    # Purpose: Reject updates if live metadata ownership or UID changed after Terraform refresh. Dependencies: live GET boundary.
    def test_live_update_uid_change_is_rejected(self):
        document = creation()
        change = document["resource_changes"][0]["change"]
        change["actions"] = ["update"]
        change["before"] = deepcopy(change["after"])
        change["before"]["metadata"][0]["uid"] = "old-uid"
        live = {
            "metadata": {"uid": "replacement-uid", "labels": {runtime.ENVIRONMENT_LABEL: "example"}}
        }
        with patch.object(runtime, "_get", return_value=live):
            with self.assertRaisesRegex(RuntimeError, "UID"):
                runtime._review_changes(self.config, document)

    # Inputs: none. Outputs: assertions only.
    # Purpose: Verify API egress derives internal peers and refuses unsupported actual endpoint ports. Dependencies: Service/Endpoints GET fixtures.
    def test_api_peers_are_actual_service_and_endpoint_addresses(self):
        service = {"spec": {"clusterIP": "10.96.0.1"}}
        endpoints = {
            "subsets": [{"addresses": [{"ip": "192.168.64.2"}], "ports": [{"port": 8443}]}]
        }
        with patch.object(runtime, "_get", side_effect=[service, endpoints]):
            self.assertEqual(runtime._api_peers(self.config), {"10.96.0.1/32", "192.168.64.2/32"})
        endpoints["subsets"][0]["ports"][0]["port"] = 9443
        with patch.object(runtime, "_get", side_effect=[service, endpoints]):
            with self.assertRaisesRegex(RuntimeError, "443/8443"):
                runtime._api_peers(self.config)

    # Inputs: optional per-node free MiB. Outputs: fake query and runner functions.
    # Purpose: Build a complete positive inventory response for capacity and version regressions. Dependencies: normalized PostgreSQL-only configuration.
    def inventory_fakes(self, free_mib=2048):
        node = {
            "metadata": {"name": "minikube", "labels": {"minikube.k8s.io/name": "minikube"}},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "allocatable": {"memory": "8Gi"},
                "nodeInfo": {"operatingSystem": "linux", "architecture": "arm64"},
            },
        }

        # Inputs: configuration and GET arguments. Outputs: fake inventory resource JSON.
        # Purpose: Supply explicit baseline prerequisites. Dependencies: node fixture.
        def getter(config, resource, name=None, namespace=None, **kwargs):
            if resource == "nodes":
                return {
                    "items": [
                        node,
                        {**node, "metadata": {**node["metadata"], "name": "minikube-m02"}},
                    ]
                }
            if resource == "namespaces":
                return {"items": []}
            if resource == "pods":
                return {"items": []}
            if resource == "storageclasses.storage.k8s.io":
                return {"volumeBindingMode": "WaitForFirstConsumer", "reclaimPolicy": "Retain"}
            if resource == "daemonsets.apps":
                return {"status": {"numberReady": 2, "desiredNumberScheduled": 2}}
            raise AssertionError(f"Unexpected GET resource: {resource}")

        # Inputs: process arguments and optional runner kwargs. Outputs: safe fake command result.
        # Purpose: Supply actual-capacity-shaped data and Ready DNS response. Dependencies: per-node free-memory fixture.
        def runner(command, **kwargs):
            if "/version" in command:
                value = {"gitVersion": "v1.37.0"}
            elif any("stats/summary" in item for item in command):
                value = {
                    "node": {
                        "memory": {"availableBytes": free_mib * 1048576},
                        "fs": {"availableBytes": 32768 * 1048576},
                    }
                }
            else:
                value = {
                    "items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]
                }
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

        return getter, runner

    # Inputs: none. Outputs: assertions only.
    # Purpose: Confirm a measured, supported PostgreSQL profile can pass baseline inspection. Dependencies: complete mocked inventory.
    def test_inspect_passes_measured_supported_profile(self):
        getter, runner = self.inventory_fakes()
        with (
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_get", side_effect=getter),
            patch.object(runtime, "run", side_effect=runner),
        ):
            report = runtime.inspect(self.config, self.workspace)
        self.assertEqual(report["status"], "pass", report["checks"])

    # Inputs: none. Outputs: assertions. Purpose: a tight cluster can replan the same footprint
    # after an interrupted apply while the original stage remains uncertain. Dependencies: inspect.
    def test_inspect_credits_running_workload_after_interruption(self):
        getter, runner = self.inventory_fakes(free_mib=128)
        stage = "30-postgres"
        item = next(item for item in self.config["derived"]["workloads"] if item["stage"] == stage)
        name = self.config["derived"]["postgres_name"]
        claim = f"{name}-data"
        pod = {
            "metadata": {
                "name": f"{name}-0", "namespace": self.config["derived"]["namespace"],
                "labels": {runtime.ENVIRONMENT_LABEL: "example", "app.kubernetes.io/name": name},
            },
            "status": {"phase": "Running"},
            "spec": {
                "nodeName": "minikube",
                "containers": [{"resources": {"requests": {"memory": f"{item['memory_mib']}Mi"}}}],
                "volumes": [{"persistentVolumeClaim": {"claimName": claim}}],
            },
        }

        # Inputs: GET arguments. Outputs: existing Pod and bound PVC alongside base inventory.
        def live(config, resource, *args, **kwargs):
            if resource == "pods":
                return {"items": [pod]}
            if resource == "persistentvolumeclaims":
                self.assertEqual(args[0], claim)
                return {"status": {"phase": "Bound", "capacity": {"storage": f"{item['storage_mib']}Mi"}}}
            return getter(config, resource, *args, **kwargs)

        path = self.workspace / "reports" / f"applied-{stage}.json"
        write_json(path, {"status": "uncertain"})
        with (
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_get", side_effect=live),
            patch.object(runtime, "run", side_effect=runner),
        ):
            report = runtime.inspect(self.config, self.workspace, stage=stage, check_dependencies=False)
            self.assertEqual(report["status"], "pass", report)
            self.assertEqual(report["capacity"]["new_memory_requests_mib"], 0)
            self.assertEqual(report["capacity"]["nodes"][0]["memory_requested_mib"], item["memory_mib"])
            pod["status"]["phase"] = "Pending"
            missing = runtime.inspect(self.config, self.workspace, stage=stage, check_dependencies=False)
            self.assertEqual(missing["status"], "blocked")
            self.assertEqual(missing["capacity"]["new_memory_requests_mib"], item["memory_mib"])
        self.assertEqual(read_json(path)["status"], "uncertain")

    # Inputs: bootstrap with insufficient space for future services. Outputs: a valid bootstrap-only inspection.
    # Purpose: defer database volume requirements to data stages. Dependencies: stage-scoped forecasts.
    def test_bootstrap_does_not_reserve_future_database_volumes(self):
        getter, runner = self.inventory_fakes(free_mib=100)
        with (
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_get", side_effect=getter) as calls,
            patch.object(runtime, "run", side_effect=runner),
        ):
            report = runtime.inspect(self.config, self.workspace, stage="00-bootstrap")
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(report["capacity"]["new_memory_requests_mib"], 0)
        self.assertEqual(report["capacity"]["new_storage_mib"], 0)
        self.assertFalse(
            any(call.args[1] == "storageclasses.storage.k8s.io" for call in calls.call_args_list)
        )

    # Inputs: CSI registration and owned or invalid class metadata. Outputs: dependency readiness checks.
    # Purpose: require the actual driver, selected topology and retained class before data stages. Dependencies: read-only API fixtures.
    def test_csi_storage_requires_registration_ownership_and_matching_topology(self):
        config = deepcopy(self.config)
        config["storage"] = {"mode": "csi", "class": "example-retain", "nodes": ["worker"]}
        nodes = [
            {
                "metadata": {
                    "name": "worker",
                    "labels": {
                        "kubernetes.io/hostname": "worker",
                        "topology.hostpath.csi/node": "worker",
                    },
                }
            }
        ]
        storage_class = {
            "metadata": {"labels": {runtime.ENVIRONMENT_LABEL: "example"}},
            "provisioner": "hostpath.csi.k8s.io",
            "volumeBindingMode": "WaitForFirstConsumer",
            "reclaimPolicy": "Retain",
            "allowedTopologies": [
                {
                    "matchLabelExpressions": [
                        {"key": "topology.hostpath.csi/node", "values": ["worker"]}
                    ]
                }
            ],
        }
        responses = {
            "csidrivers.storage.k8s.io": {"metadata": {"name": "hostpath.csi.k8s.io"}},
            "csinodes.storage.k8s.io": {
                "items": [
                    {
                        "metadata": {"name": "worker"},
                        "spec": {
                            "drivers": [
                                {
                                    "name": "hostpath.csi.k8s.io",
                                    "topologyKeys": ["topology.hostpath.csi/node"],
                                }
                            ]
                        },
                    }
                ]
            },
            "daemonsets.apps": {"status": {"desiredNumberScheduled": 1, "numberReady": 1}},
        }
        for case in ("ready", "missing", "foreign", "wrong-topology"):
            current = deepcopy(storage_class)
            if case == "missing":
                current = None
            elif case == "foreign":
                current["metadata"]["labels"] = {}
            elif case == "wrong-topology":
                current["allowedTopologies"][0]["matchLabelExpressions"][0]["values"] = ["other"]
            responses["storageclasses.storage.k8s.io"] = current
            with (
                self.subTest(case=case),
                patch.object(
                    runtime,
                    "_get",
                    side_effect=lambda config, resource, *args, **kwargs: responses[resource],
                ),
            ):
                checks = {
                    name: passed
                    for name, passed, detail in runtime._storage_checks(config, nodes, True)
                }
            self.assertTrue(checks["csi_driver"])
            self.assertEqual(checks["storage_class"], case == "ready")
        responses["csinodes.storage.k8s.io"] = {"items": []}
        with patch.object(
            runtime,
            "_get",
            side_effect=lambda config, resource, *args, **kwargs: responses[resource],
        ):
            checks = {
                name: passed
                for name, passed, detail in runtime._storage_checks(config, nodes, False)
            }
        self.assertFalse(checks["csi_driver"])

    # Inputs: none. Outputs: assertions only.
    # Purpose: Reject aggregate-only memory headroom when no individual node fits the stage. Dependencies: two small fake node summaries.
    def test_aggregate_memory_does_not_replace_single_node_fit(self):
        getter, runner = self.inventory_fakes(free_mib=300)
        with (
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_get", side_effect=getter),
            patch.object(runtime, "run", side_effect=runner),
        ):
            report = runtime.inspect(self.config, self.workspace)
        checks = {item["name"]: item["status"] for item in report["checks"]}
        self.assertEqual(checks["measured_memory"], "pass")
        self.assertEqual(checks["single_node_memory"], "blocked")


if __name__ == "__main__":
    unittest.main()
