"""Purpose: test reviewed prerequisite installation with private fake tools and no live cluster changes."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import prerequisites as module
import runtime
from config import load_config
from io_utils import private_dir, read_json, write_json

SPEC = importlib.util.spec_from_file_location("prerequisite_cli", SCRIPTS / "platform.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)

FAKE_KUBECTL = """# Purpose: simulate Kubernetes reads, atomic creates, and controller readiness in private files.
import json
import os
import sys
from pathlib import Path

path = Path(os.environ["FAKE_CLUSTER_STATE"])
state = json.loads(path.read_text())
arguments = sys.argv[1:]
for flag in ("--kubeconfig", "--context"):
    index = arguments.index(flag)
    del arguments[index:index + 2]
arguments = [item for item in arguments if not item.startswith("--request-timeout=")]
with Path(os.environ["FAKE_CLUSTER_LOG"]).open("a") as log:
    log.write(json.dumps(arguments) + "\\n")

# Inputs: resource, namespace and name. Outputs: fake storage key.
# Purpose: keep fake objects uniquely addressed. Dependencies: strings only.
def key(resource, namespace, name):
    return resource + "|" + (namespace or "") + "|" + name

# Inputs: object and resource type. Outputs: none.
# Purpose: simulate server defaults and immediate controller progress. Dependencies: private state.
def store(item, resource):
    metadata = item["metadata"]
    metadata.setdefault("uid", "uid-" + str(len(state["objects"])))
    metadata.setdefault("generation", 1)
    if item["kind"] == "Namespace":
        metadata.setdefault("labels", {})["kubernetes.io/metadata.name"] = metadata["name"]
    if item["kind"] == "CustomResourceDefinition":
        item["status"] = {"conditions": [{"type": "Established", "status": "True"}]}
    if item["kind"] == "Deployment":
        pod_spec = item["spec"]["template"]["spec"]
        for container in pod_spec["containers"] + pod_spec.get("initContainers", []):
            for entry in container.get("env", []):
                if entry.get("value") == "":
                    del entry["value"]
    if item["kind"] in ("Deployment", "StatefulSet"):
        item["status"] = {"readyReplicas": 1, "updatedReplicas": 1, "availableReplicas": 1, "observedGeneration": 1}
    state["objects"][key(resource, metadata.get("namespace"), metadata["name"])] = item

verb = arguments[0]
namespace = arguments[arguments.index("--namespace") + 1] if "--namespace" in arguments else None
if verb == "config":
    print("https://fake-cluster.invalid:8443")
elif verb == "create" and "--raw" in arguments:
    review = json.load(sys.stdin)
    attributes = review["spec"]["resourceAttributes"]
    state.setdefault("authorization_reviews", []).append(attributes)
    path.write_text(json.dumps(state))
    resource = attributes["resource"] + ("." + attributes["group"] if attributes["group"] else "")
    print(json.dumps({"status": {"allowed": resource != state.get("deny_resource")}}))
elif verb in ("wait", "rollout"):
    target = arguments[2]
    resource, name = target.split("/", 1)
    sys.exit(0 if key(resource, namespace, name) in state["objects"] else 1)
elif verb == "get" and arguments[1] == "--raw":
    if arguments[2] == "/version":
        print(json.dumps({"gitVersion": "v1.37.0"}))
    else:
        print(json.dumps({"node": {"memory": {"availableBytes": state.get("free_memory_mib", 8192) * 1048576}, "fs": {"availableBytes": 65536 * 1048576}}}))
elif verb == "get":
    resource = arguments[1]
    name = arguments[2] if len(arguments) > 2 and not arguments[2].startswith("-") else None
    if name:
        item = state["objects"].get(key(resource, namespace, name))
        if item is not None:
            print(json.dumps(item))
    else:
        items = [item for object_key, item in state["objects"].items() if object_key.split("|")[0] == resource]
        if "--all-namespaces" not in arguments:
            items = [item for item in items if not item["metadata"].get("namespace") or item["metadata"]["namespace"] == (namespace or "default")]
        print(json.dumps({"items": items}))
elif verb == "create":
    item = json.load(sys.stdin)
    resource = state["resources"][item["kind"]]
    metadata = item["metadata"]
    state["attempts"] = state.get("attempts", 0) + 1
    path.write_text(json.dumps(state))
    if state.get("fail_at") == state["attempts"]:
        sys.exit(1)
    if key(resource, metadata.get("namespace"), metadata["name"]) in state["objects"]:
        sys.exit(1)
    store(item, resource)
    if item["kind"] == "Prometheus":
        name, namespace = metadata["name"], metadata["namespace"]
        labels = {"prometheus": name, **item["spec"].get("podMetadata", {}).get("labels", {})}
        store({"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "prometheus-" + name, "namespace": namespace}, "spec": {"replicas": 1}}, "statefulsets.apps")
        store({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "prometheus-" + name + "-0", "namespace": namespace, "labels": labels}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}, "pods")
    path.write_text(json.dumps(state))
    print(resource + "/" + metadata["name"])
else:
    sys.exit(9)
"""

FAKE_BREW = """# Purpose: simulate a keg-only OpenSSL package without changing host packages.
import os
import sys
from pathlib import Path

prefix = Path(os.environ["FAKE_OPENSSL_PREFIX"])
if sys.argv[1:] == ["--prefix", "openssl@3"]:
    print(prefix)
elif sys.argv[1:] == ["install", "openssl@3"]:
    binary = prefix / "bin" / "openssl"
    binary.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary.write_text("#!" + sys.executable + "\\n# Purpose: simulate required OpenSSL capabilities.\\nimport sys\\nprint('OpenSSL 3.5.9 fake' if sys.argv[1] == 'version' else '-addext -checkhost -checkend -pubout')\\n")
    binary.chmod(0o700)
else:
    sys.exit(9)
"""


class PrerequisiteTests(unittest.TestCase):
    # Inputs: none. Outputs: private fake tool directory and cluster fixture.
    # Purpose: isolate every test from real tools, data and Kubernetes. Dependencies: tempfile and environment patching.
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tools = private_dir(self.root / "tools")
        self.workspace = self.root / "workspace"
        self.source = self.root / "input.json"
        self.state_path = self.root / "cluster.json"
        self.log = self.root / "commands.jsonl"
        self.kubeconfig = self.root / "test.kubeconfig"
        self.kubeconfig.write_text("fake kubectl does not read authentication material\n")
        self.executable("kubectl", FAKE_KUBECTL)
        state = {"objects": {}, "resources": module.RESOURCES, "attempts": 0}
        write_json(self.state_path, state)
        self.add(
            {"kind": "Namespace", "metadata": {"name": "kube-system", "uid": "cluster-1"}},
            "namespaces",
        )
        self.add(
            {
                "kind": "Node",
                "metadata": {"name": "minikube", "labels": {"minikube.k8s.io/name": "minikube"}},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "allocatable": {"memory": "16Gi"},
                    "nodeInfo": {"operatingSystem": "linux", "architecture": "arm64"},
                },
            },
            "nodes",
        )
        self.add(
            {
                "kind": "DaemonSet",
                "metadata": {"name": "calico-node", "namespace": "kube-system"},
                "status": {"numberReady": 1, "desiredNumberScheduled": 1},
            },
            "daemonsets.apps",
        )
        self.add(
            {
                "kind": "StorageClass",
                "metadata": {"name": "dev-retain"},
                "volumeBindingMode": "WaitForFirstConsumer",
                "reclaimPolicy": "Retain",
            },
            "storageclasses.storage.k8s.io",
        )
        self.add(
            {
                "kind": "Pod",
                "metadata": {
                    "name": "dns",
                    "namespace": "kube-system",
                    "labels": {"k8s-app": "kube-dns"},
                },
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
            "pods",
        )
        self.add(
            {
                "kind": "Service",
                "metadata": {"name": "kubernetes", "namespace": "default"},
                "spec": {"clusterIP": "10.96.0.1"},
            },
            "services",
        )
        self.add(
            {
                "kind": "Endpoints",
                "metadata": {"name": "kubernetes", "namespace": "default"},
                "subsets": [{"addresses": [{"ip": "192.168.49.2"}], "ports": [{"port": 8443}]}],
            },
            "endpoints",
        )
        environment = patch.dict(
            os.environ,
            {
                "PATH": str(self.tools),
                "FAKE_CLUSTER_STATE": str(self.state_path),
                "FAKE_CLUSTER_LOG": str(self.log),
                "FAKE_OPENSSL_PREFIX": str(self.root / "keg"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.config = self.configuration()

    # Inputs: executable name and body. Outputs: executable private path.
    # Purpose: run fake commands through the real subprocess boundary. Dependencies: current Python interpreter.
    def executable(self, name, body):
        path = self.tools / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)
        return path

    # Inputs: object and optional API resource name. Outputs: none.
    # Purpose: seed live fixtures with the same server defaults used by fake creates. Dependencies: private cluster state.
    def add(self, document, resource=None):
        document = deepcopy(document)
        state = read_json(self.state_path)
        resource = resource or module.RESOURCES[document["kind"]]
        metadata = document["metadata"]
        metadata.setdefault("uid", "uid-" + str(len(state["objects"])))
        metadata.setdefault("generation", 1)
        if document["kind"] == "Namespace":
            metadata.setdefault("labels", {})["kubernetes.io/metadata.name"] = metadata["name"]
        if document["kind"] == "CustomResourceDefinition":
            document["status"] = {"conditions": [{"type": "Established", "status": "True"}]}
        if document["kind"] in {"Deployment", "StatefulSet"}:
            document["status"] = {
                "readyReplicas": 1,
                "updatedReplicas": 1,
                "availableReplicas": 1,
                "observedGeneration": 1,
            }
        key = "|".join((resource, metadata.get("namespace", ""), metadata["name"]))
        state["objects"][key] = document
        write_json(self.state_path, state)

    # Inputs: top-level configuration overrides. Outputs: normalized configuration.
    # Purpose: select meaningful dependency profiles. Dependencies: actual configuration entry point.
    def configuration(self, **overrides):
        value = {
            "environment_id": "atlas",
            "cluster": {"kubeconfig": str(self.kubeconfig)},
            "storage": {"class": "dev-retain"},
            "postgres": {"enabled": False},
            "monitoring": {"enabled": False},
            "budget": {"max_memory_requests_mib": 3072},
            "network": {"api_server_cidrs": ["10.96.0.1/32", "192.168.49.2/32"]},
            **overrides,
        }
        self.source.write_text(json.dumps(value))
        self.config = load_config(self.source)
        return self.config

    # Inputs: component and action flags. Outputs: exit code and structured CLI result.
    # Purpose: exercise dispatch, workspace isolation and real fake-tool orchestration. Dependencies: CLI.main.
    def request(self, component="strimzi", *flags):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            status = CLI.main(
                [
                    "--config",
                    str(self.source),
                    "--workspace",
                    str(self.workspace),
                    "prerequisites",
                    "--component",
                    component,
                    *flags,
                ]
            )
        return status, json.loads(output.getvalue() or error.getvalue())

    # Inputs: component and external ownership choice. Outputs: none.
    # Purpose: seed a full compatible or recoverable Operator installation. Dependencies: packaged manifests.
    def seed(self, component="strimzi", external=False):
        for document in module._documents(self.config, component):
            if external:
                document["metadata"]["labels"] = {}
            self.add(document)

    # Inputs: none. Outputs: assertions about preview and complete installation.
    # Purpose: prove pinned Strimzi setup and normal inspection work through fake kubectl. Dependencies: full CLI workflow.
    def test_strimzi_plan_execute_and_inspect(self):
        status, plan = self.request()
        self.assertEqual(status, 0, plan)
        self.assertEqual(read_json(self.state_path)["attempts"], 0)
        self.assertNotIn("openAPIV3Schema", json.dumps(plan))
        self.assertEqual(plan["managed_memory_mib"], 384)
        status, result = self.request("strimzi", "--plan-id", plan["plan_id"], "--execute")
        self.assertEqual(status, 0, result)
        self.assertEqual(result["status"], "installed")
        self.assertEqual(runtime.inspect(self.config, self.workspace)["status"], "pass")
        status, reused = self.request()
        self.assertEqual(status, 0, reused)
        self.assertEqual(reused["changes"], [])

    # Inputs: missing future CSI resources and a valid Operator target. Outputs: an Operator-only prerequisite plan.
    # Purpose: keep data storage preparation out of unrelated dependency checks. Dependencies: real CLI scope selection.
    def test_operator_preparation_does_not_require_a_future_storage_class(self):
        self.configuration(storage={"mode": "csi", "nodes": ["minikube"]})
        status, plan = self.request()
        self.assertEqual(status, 0, plan)
        self.assertFalse(
            any(
                item["reference"]["resource"] == "storageclasses.storage.k8s.io"
                for item in plan["changes"]
            )
        )
        report = read_json(self.workspace / "reports" / "inventory.json")
        self.assertEqual(report["capacity"]["new_memory_requests_mib"], 384)
        self.assertEqual(report["capacity"]["new_storage_mib"], 0)

    # Inputs: existing externally owned Strimzi. Outputs: no resource changes.
    # Purpose: preserve ownership and avoid duplicate installation. Dependencies: complete compatible live fixture.
    def test_external_strimzi_is_reused_without_claiming_resources(self):
        self.seed(external=True)
        status, plan = self.request()
        self.assertEqual(status, 0, plan)
        self.assertEqual(plan["changes"], [])
        self.assertEqual(plan["managed_memory_mib"], 0)
        self.assertIn("strimzi-operator", plan["reused"])

    # Inputs: an Operator in a different namespace. Outputs: blocked without writes.
    # Purpose: enforce deployment location before installation. Dependencies: cluster-wide discovery.
    def test_wrong_operator_namespace_blocks_duplicate_creation(self):
        document = next(
            item
            for item in module._documents(self.config, "strimzi")
            if item["kind"] == "Deployment"
        )
        document["metadata"]["namespace"] = "other"
        self.add(document)
        status, result = self.request()
        self.assertEqual(status, 2)
        self.assertIn("namespace", result["error"])
        self.assertEqual(read_json(self.state_path)["attempts"], 0)

    # Inputs: invalid watch scope or version. Outputs: blocked plans.
    # Purpose: reject apparently running but incompatible Operators. Dependencies: real Deployment profile checks.
    def test_operator_version_and_watch_scope_are_checked(self):
        for wrong_version in (True, False):
            with self.subTest(wrong_version=wrong_version):
                self.seed(external=True)
                state = read_json(self.state_path)
                operator = state["objects"]["deployments.apps|kafka|strimzi-cluster-operator"]
                container = operator["spec"]["template"]["spec"]["containers"][0]
                if wrong_version:
                    container["image"] = "quay.io/strimzi/operator:1.2.0"
                else:
                    container["env"][0] = {"name": "STRIMZI_NAMESPACE", "value": "other"}
                write_json(self.state_path, state)
                self.assertEqual(self.request()[0], 2)

    # Inputs: two live Operators. Outputs: rejection.
    # Purpose: prevent ambiguous ownership and reconciliation. Dependencies: all-namespace Deployment inventory.
    def test_multiple_operators_are_rejected(self):
        self.seed(external=True)
        document = next(
            item
            for item in module._documents(self.config, "strimzi")
            if item["kind"] == "Deployment"
        )
        document["metadata"]["name"] = "second-operator"
        self.add(document)
        self.assertEqual(self.request()[0], 2)

    # Inputs: an installed operator plus the entity operator Strimzi creates for a Kafka cluster.
    # Outputs: one detected operator. Purpose: Kafka operands share the operator image but are not
    # cluster operators (found live after 31-kafka). Dependencies: Strimzi labels and owner references.
    def test_strimzi_operands_are_not_counted_as_operators(self):
        self.seed(external=True)
        document = next(
            item
            for item in module._documents(self.config, "strimzi")
            if item["kind"] == "Deployment"
        )
        operand = json.loads(json.dumps(document))
        operand["metadata"]["name"] = "atlas-kafka-entity-operator"
        operand["metadata"]["labels"] = {
            "strimzi.io/cluster": "atlas-kafka",
            "strimzi.io/kind": "Kafka",
        }
        operand["metadata"]["ownerReferences"] = [
            {"apiVersion": "kafka.strimzi.io/v1", "kind": "Kafka", "name": "atlas-kafka"}
        ]
        deployments = [document, operand]
        self.assertEqual(module._operators(deployments, "strimzi"), [document])
        del operand["metadata"]["labels"]
        self.assertEqual(module._operators(deployments, "strimzi"), [document])
        operand["metadata"]["ownerReferences"] = []
        self.assertEqual(len(module._operators(deployments, "strimzi")), 2)

    # Inputs: an isolated foreign CRD. Outputs: collision without adoption.
    # Purpose: preserve resources from incomplete external installations. Dependencies: named GET and ownership checks.
    def test_existing_unowned_crd_is_not_adopted(self):
        document = next(
            item
            for item in module._documents(self.config, "strimzi")
            if item["kind"] == "CustomResourceDefinition"
        )
        document["metadata"]["labels"] = {}
        self.add(document)
        status, result = self.request()
        self.assertEqual(status, 2)
        self.assertIn("collision", result["error"])
        self.assertEqual(read_json(self.state_path)["attempts"], 0)

    # Inputs: drifted owned Deployment. Outputs: refusal without patching.
    # Purpose: require explicit migration instead of silently upgrading installed resources. Dependencies: subset comparison.
    def test_owned_resource_drift_is_not_overwritten(self):
        self.seed()
        state = read_json(self.state_path)
        state["objects"]["deployments.apps|kafka|strimzi-cluster-operator"]["spec"]["replicas"] = 2
        write_json(self.state_path, state)
        self.assertEqual(self.request()[0], 2)

    # Inputs: owned Deployment with literal or referenced feature gates. Outputs: matching or drift.
    # Purpose: allow an empty value while preserving real EnvVar drift checks. Dependencies: full CLI.
    def test_owned_feature_gates_drift_is_rejected(self):
        self.seed()
        status, unchanged = self.request()
        self.assertEqual(status, 0, unchanged)
        self.assertEqual(unchanged["changes"], [])

        state = read_json(self.state_path)
        deployment = state["objects"]["deployments.apps|kafka|strimzi-cluster-operator"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        feature_gates = next(
            item for item in container["env"] if item["name"] == "STRIMZI_FEATURE_GATES"
        )
        source = {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.namespace"}}
        for fields in ({"value": "+ExampleGate"}, {"valueFrom": source}):
            with self.subTest(fields=fields):
                feature_gates.clear()
                feature_gates.update(name="STRIMZI_FEATURE_GATES", **fields)
                write_json(self.state_path, state)
                status, blocked = self.request()
                self.assertEqual(status, 2, blocked)
                self.assertIn("Prerequisite collision or drift", blocked["error"])
        self.assertEqual(read_json(self.state_path)["attempts"], 0)

    # Inputs: failure after several successful creates. Outputs: preserved resources, consumed plan and resumable new plan.
    # Purpose: verify safe recovery without rollback or reuse of an uncertain mutation plan. Dependencies: persistent fake API state.
    def test_partial_failure_requires_a_new_plan_and_preserves_completed_resources(self):
        status, plan = self.request()
        self.assertEqual(status, 0, plan)
        state = read_json(self.state_path)
        state["fail_at"] = 4
        write_json(self.state_path, state)
        status, result = self.request("strimzi", "--plan-id", plan["plan_id"], "--execute")
        self.assertEqual(status, 2, result)
        report = read_json(self.workspace / "reports" / "prerequisites-strimzi.json")
        self.assertEqual(report["status"], "uncertain")
        self.assertEqual(sum(item["status"] == "created" for item in report["results"]), 3)
        attempts = read_json(self.state_path)["attempts"]
        self.assertEqual(self.request("strimzi", "--plan-id", plan["plan_id"], "--execute")[0], 2)
        self.assertEqual(read_json(self.state_path)["attempts"], attempts)
        status, fresh = self.request()
        self.assertEqual(status, 0, fresh)
        self.assertLess(len(fresh["changes"]), len(plan["changes"]))
        self.assertEqual(self.request("strimzi", "--plan-id", fresh["plan_id"], "--execute")[0], 0)
        inventory = read_json(self.workspace / "state" / "recorded-objects.json")
        self.assertEqual(
            [{key: item[key] for key in ("resource", "namespace", "name")} for item in inventory],
            [change["reference"] for change in plan["changes"]],
        )

    # Inputs: failure after creating Strimzi. Outputs: a fresh plan for only the remaining resources.
    # Purpose: recover an owned Deployment after empty EnvVar values are omitted. Dependencies: fake API.
    def test_partial_installation_resumes_after_strimzi_deployment(self):
        self.configuration(monitoring={"enabled": True})
        status, plan = self.request("all")
        self.assertEqual(status, 0, plan)
        completed = next(
            count
            for count, change in enumerate(plan["changes"], start=1)
            if change["reference"]["resource"] == "deployments.apps"
            and change["reference"]["name"] == "strimzi-cluster-operator"
        )
        state = read_json(self.state_path)
        state["fail_at"] = completed + 1
        write_json(self.state_path, state)
        status, blocked = self.request("all", "--plan-id", plan["plan_id"], "--execute")
        self.assertEqual(status, 2, blocked)
        self.assertIn("Prerequisite creation failed", blocked["error"])
        state = read_json(self.state_path)
        self.assertIn("deployments.apps|kafka|strimzi-cluster-operator", state["objects"])

        status, fresh = self.request("all")
        self.assertEqual(status, 0, fresh)
        self.assertEqual(fresh["changes"], plan["changes"][completed:])
        status, resumed = self.request("all", "--plan-id", fresh["plan_id"], "--execute")
        self.assertEqual(status, 0, resumed)
        self.assertEqual(resumed["status"], "installed")

    # Inputs: new live resource after plan review. Outputs: stale-plan rejection.
    # Purpose: stop before the first write when live assumptions change. Dependencies: repeated snapshot.
    def test_live_collision_invalidates_review_before_execution(self):
        _, plan = self.request()
        self.add({"kind": "Namespace", "metadata": {"name": "kafka"}})
        status, result = self.request("strimzi", "--plan-id", plan["plan_id"], "--execute")
        self.assertEqual(status, 2)
        self.assertIn("changed", result["error"])
        self.assertEqual(read_json(self.state_path)["attempts"], 0)

    # Inputs: altered saved resource bytes. Outputs: rejection before reads or writes.
    # Purpose: bind execution to the exact review. Dependencies: private JSON and plan digest.
    def test_tampered_plan_cannot_execute(self):
        _, plan = self.request()
        path = Path(plan["review"])
        review = read_json(path)
        review["snapshot"]["changes"] = []
        write_json(path, review)
        status, result = self.request("strimzi", "--plan-id", plan["plan_id"], "--execute")
        self.assertEqual(status, 2)
        self.assertIn("contents", result["error"])

    # Inputs: changed engine sources. Outputs: stale-plan rejection.
    # Purpose: bind a review to packaged behavior without editing source files. Dependencies: source checksum interception.
    def test_source_changes_invalidate_plan(self):
        _, plan = self.request()
        with patch.object(module, "_sources", return_value={"changed": "source"}):
            self.assertEqual(
                self.request("strimzi", "--plan-id", plan["plan_id"], "--execute")[0], 2
            )

    # Inputs: insufficient install budget or capacity. Outputs: blocked before writes.
    # Purpose: account for prerequisite requests in both configured and measured limits. Dependencies: actual foundation inspection.
    def test_operator_memory_counts_toward_budget_and_live_capacity(self):
        self.configuration(budget={"max_memory_requests_mib": 1100})
        status, result = self.request()
        self.assertEqual(status, 2)
        self.assertIn("1152Mi", result["error"])
        self.configuration()
        state = read_json(self.state_path)
        state["free_memory_mib"] = 600
        write_json(self.state_path, state)
        self.assertEqual(self.request()[0], 2)
        inventory = read_json(self.workspace / "reports" / "inventory.json")
        self.assertEqual(inventory["capacity"]["new_memory_requests_mib"], 384)

    # Inputs: owned Strimzi and a later monitoring request. Outputs: combined-budget rejection.
    # Purpose: prevent separate install commands from bypassing the memory budget. Dependencies: live owned-resource accounting.
    def test_separate_component_plans_count_previously_managed_prerequisites(self):
        self.configuration(
            postgres={"enabled": True},
            monitoring={"enabled": True},
            budget={"max_memory_requests_mib": 1700},
        )
        self.seed()
        status, result = self.request("monitoring")
        self.assertEqual(status, 2)
        self.assertIn("1844Mi", result["error"])
        self.assertEqual(read_json(self.state_path)["attempts"], 0)

    # Inputs: denied create permission. Outputs: blocked plan.
    # Purpose: check required API permissions without a persisted test write. Dependencies: fake authorization API.
    def test_missing_create_permission_blocks_plan(self):
        state = read_json(self.state_path)
        state["deny_resource"] = "customresourcedefinitions.apiextensions.k8s.io"
        write_json(self.state_path, state)
        self.assertEqual(self.request()[0], 2)
        self.assertEqual(read_json(self.state_path)["attempts"], 0)

    # Inputs: a custom resource whose CRD does not exist yet. Outputs: explicit API-group permission evaluation.
    # Purpose: avoid kubectl discovery fallback denying a valid first installation. Dependencies: non-persistent fake authorization reviews.
    def test_permission_review_keeps_the_group_before_crd_installation(self):
        self.assertTrue(
            module._can_create(
                self.config,
                {
                    "resource": "prometheuses.monitoring.coreos.com",
                    "name": "atlas-prometheus",
                    "namespace": "monitoring",
                },
            )
        )
        state = read_json(self.state_path)
        self.assertEqual(state["attempts"], 0)
        self.assertEqual(
            state["authorization_reviews"][-1],
            {
                "verb": "create",
                "group": "monitoring.coreos.com",
                "resource": "prometheuses",
                "namespace": "monitoring",
            },
        )

    # Inputs: monitoring without Kafka. Outputs: ready minimal monitoring and no Kafka resources.
    # Purpose: verify CRD order, selectors, labels and strict ordinary inspection. Dependencies: fake monitoring controller.
    def test_monitoring_bootstrap_includes_a_selectable_prometheus_instance(self):
        self.configuration(
            postgres={"enabled": True},
            kafka={"enabled": False},
            monitoring={
                "enabled": True,
                "namespace": "observability",
                "release": "metrics",
                "pod_labels": {"app.kubernetes.io/name": "prometheus", "team": "platform"},
            },
        )
        status, plan = self.request("monitoring")
        self.assertEqual(status, 0, plan)
        self.assertEqual(plan["managed_memory_mib"], 372)
        status, result = self.request("monitoring", "--plan-id", plan["plan_id"], "--execute")
        self.assertEqual(status, 0, result)
        state = read_json(self.state_path)
        prometheus = state["objects"][
            "prometheuses.monitoring.coreos.com|observability|atlas-prometheus"
        ]
        self.assertEqual(
            prometheus["spec"]["podMonitorSelector"], {"matchLabels": {"release": "metrics"}}
        )
        self.assertEqual(runtime.inspect(self.config, self.workspace)["status"], "pass")
        self.assertFalse(any("strimzi" in key for key in state["objects"]))
        arguments = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertFalse(
            any(command[0] in {"apply", "delete", "patch", "replace"} for command in arguments)
        )

    # Inputs: externally installed Operator with a chart-specific name. Outputs: only minimal Prometheus creates.
    # Purpose: reuse existing installation identities and avoid re-installing its CRDs. Dependencies: actual reused-resource waits.
    def test_existing_prometheus_operator_can_prepare_only_the_missing_instance(self):
        self.configuration(
            postgres={"enabled": True}, kafka={"enabled": False}, monitoring={"enabled": True}
        )
        for document in module._documents(self.config, "monitoring"):
            document["metadata"]["labels"] = {}
            if document["kind"] == "Deployment":
                document["metadata"]["name"] = "chart-operator"
            self.add(document)
        status, plan = self.request("monitoring")
        self.assertEqual(status, 0, plan)
        self.assertFalse(
            any(
                item["reference"]["resource"]
                in {"deployments.apps", "customresourcedefinitions.apiextensions.k8s.io"}
                for item in plan["changes"]
            )
        )
        self.assertEqual(
            self.request("monitoring", "--plan-id", plan["plan_id"], "--execute")[0], 0
        )

    # Inputs: an existing Prometheus with incompatible monitor selectors. Outputs: blocked plan.
    # Purpose: prevent a second monitoring instance from masking bad integration. Dependencies: live selectors.
    def test_incompatible_prometheus_selectors_block_a_second_instance(self):
        self.configuration(
            postgres={"enabled": True}, kafka={"enabled": False}, monitoring={"enabled": True}
        )
        self.seed("monitoring", external=True)
        document = next(
            item
            for item in module._documents(self.config, "prometheus")
            if item["kind"] == "Prometheus"
        )
        document["metadata"]["labels"] = {}
        document["spec"]["podMonitorSelector"] = {"matchLabels": {"release": "other"}}
        self.add(document)
        status, result = self.request("monitoring")
        self.assertEqual(status, 2)
        self.assertIn("selectors", result["error"])
        self.assertEqual(read_json(self.state_path)["attempts"], 0)

    # Inputs: an installed but unready monitoring stack. Outputs: ordinary inspection remains blocked.
    # Purpose: prove CRDs alone cannot establish monitoring readiness. Dependencies: strict shared installation checks.
    def test_monitoring_crds_do_not_replace_an_operator_and_instance(self):
        self.configuration(
            postgres={"enabled": True}, kafka={"enabled": False}, monitoring={"enabled": True}
        )
        for document in module._documents(self.config, "monitoring"):
            if document["kind"] in {"Namespace", "CustomResourceDefinition"}:
                self.add(document)
        private_dir(self.workspace)
        self.assertEqual(runtime.inspect(self.config, self.workspace)["status"], "blocked")

    # Inputs: missing local OpenSSL with an existing fake Homebrew. Outputs: local-only install and subsequent reuse.
    # Purpose: verify keg-only resolution without editing PATH or contacting Kubernetes. Dependencies: fake brew and OpenSSL.
    def test_openssl_install_and_reuse_never_contact_kubernetes(self):
        self.configuration(postgres={"enabled": True}, kafka={"enabled": False})
        self.executable("brew", FAKE_BREW)
        with patch.object(module.sys, "platform", "darwin"):
            status, plan = self.request("openssl")
            self.assertEqual(status, 0, plan)
            self.assertEqual(plan["changes"][0]["action"], "install-openssl")
            status, result = self.request("openssl", "--plan-id", plan["plan_id"], "--execute")
            self.assertEqual(status, 0, result)
            self.assertEqual(result["openssl"]["path"], str(self.root / "keg" / "bin" / "openssl"))
            self.assertEqual(self.request("openssl")[1]["changes"], [])
        self.assertFalse(self.log.exists())
        self.assertEqual(os.environ["PATH"], str(self.tools))

    # Inputs: unsupported local package-manager state. Outputs: clear blocker without installation.
    # Purpose: avoid bootstrapping package managers as an implicit side effect. Dependencies: hermetic PATH.
    def test_missing_package_manager_reports_a_local_blocker(self):
        self.configuration(postgres={"enabled": True}, kafka={"enabled": False})
        status, result = self.request("openssl")
        self.assertEqual(status, 2)
        self.assertIn("OpenSSL is missing", result["error"])
        self.assertFalse(self.log.exists())

    # Inputs: existing Linux package managers. Outputs: fixed install arguments and non-interactive elevation.
    # Purpose: validate package commands and symlink resolution. Dependencies: private fake tools, mocked discovery and UID.
    def test_linux_package_commands_are_fixed_and_noninteractive(self):
        fake_tool = "# Purpose: stand in for a Linux package tool during command planning.\n"
        sudo_target = self.executable("sudo-wrapper", fake_tool)
        sudo_link = self.tools / "sudo"
        sudo_link.symlink_to(sudo_target.name)
        for manager, flags in (
            ("apt-get", ["--yes", "--no-install-recommends"]),
            ("dnf", ["--assumeyes"]),
        ):
            with self.subTest(manager=manager):
                executable = self.executable(manager, fake_tool)
                paths = {manager: str(executable), "sudo": str(sudo_link)}
                with (
                    patch.object(module.sys, "platform", "linux"),
                    patch.object(module.shutil, "which", side_effect=paths.get),
                    patch.object(module.os, "geteuid", return_value=1000),
                ):
                    command = module._openssl_install()
                self.assertEqual(
                    command, [str(sudo_target), "-n", str(executable), "install", *flags, "openssl"]
                )

    # Inputs: malformed CLI requests and a disabled component. Outputs: entry-point rejection.
    # Purpose: reject invalid execution requests before subprocesses. Dependencies: argparse and normalized component selection.
    def test_invalid_requests_stop_at_the_entry_point(self):
        for arguments in (("--execute",), ("--plan-id", "../escape")):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                CLI.arguments(
                    [
                        "--config",
                        str(self.source),
                        "--workspace",
                        str(self.workspace),
                        "prerequisites",
                        *arguments,
                    ]
                )
        self.assertEqual(self.request("openssl")[0], 2)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
