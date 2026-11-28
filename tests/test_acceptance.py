# Purpose: Check verify's per-object monitoring and Kafka checks, Prometheus evidence, and the isolated
# acceptance runner (plan, execution, cleanup and evidence binding) against an in-memory fake kubectl.
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import acceptance
import prerequisites
import runtime
from config import load_config
from io_utils import read_json, write_json

SPEC = importlib.util.spec_from_file_location("platform_acceptance_cli", SCRIPTS / "platform.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)

KINDS = {
    "Pod": "pods",
    "Secret": "secrets",
    "KafkaTopic": acceptance.TOPICS,
    "KafkaUser": acceptance.USERS,
}
IDENTITY = {
    "context": "minikube",
    "kubeconfig": "",
    "kubeconfig_sha256": "fixture",
    "server": "https://127.0.0.1:8443",
    "kube_system_uid": "cluster-one",
}


# Inputs: a log of result tokens. Outputs: tokens for every expected check of a component.
# Purpose: simulate a client Pod in which every check passes. Dependencies: acceptance.GROUPS.
def passing_log(component):
    names = [name for group in acceptance.GROUPS[component].values() for name in group]
    lines = [f"ACCEPTANCE {name} PASS ok" for name in names]
    if component == "kafka":
        lines.append("CLEANUP consumer_group PASS absent")
    return "\n".join(["noise that must be ignored", *lines])


class FakeCluster:
    """A minimal kubectl API: named objects, label selectors, create/delete, logs and raw GETs."""

    # Inputs: none. Outputs: empty cluster. Purpose: hold objects and test hooks. Dependencies: none.
    def __init__(self):
        self.objects, self.commands, self.serial = {}, [], 0
        self.denied, self.reject_create, self.fail_reads = set(), set(), set()
        self.timed_out_reads, self.replace_on_delete = set(), set()
        self.raw, self.log, self.pod_phase, self.never_ready = {}, "", "Succeeded", set()

    # Inputs: resource, namespace, name and object fields. Outputs: stored object.
    # Purpose: seed cluster state. Dependencies: none.
    def add(self, resource, namespace, name, labels=None, **fields):
        self.serial += 1
        item = {
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels or {},
                "uid": f"uid-{self.serial}",
                "generation": 1,
                "resourceVersion": str(self.serial),
            },
            **fields,
        }
        self.objects[(resource, namespace, name)] = item
        return item

    # Inputs: return code, stdout and stderr. Outputs: completed process. Dependencies: subprocess.
    @staticmethod
    def result(code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    # Inputs: kubectl argv and runner keywords. Outputs: completed process.
    # Purpose: dispatch the kubectl verbs the code under test uses. Dependencies: in-memory state.
    def __call__(self, command, timeout=None, input_text=None, cwd=None):
        args = command[6:]
        self.commands.append(args)
        verb = args[0]
        if verb == "get" and args[1] == "--raw":
            if args[2] in self.raw:
                code, body = self.raw[args[2]]
                return self.result(code, json.dumps(body), "" if code == 0 else "Error from server (Forbidden)")
            return self.result(1, "", "Error from server (ServiceUnavailable)")
        namespace = args[args.index("--namespace") + 1] if "--namespace" in args else None
        if verb == "get":
            resource = "secrets" if args[1] == "secret" else args[1]
            if resource in self.denied:
                return self.result(1, "", 'Error from server (Forbidden): cannot list "x"')
            if resource in self.fail_reads:
                return self.result(1, "", "Unable to connect to the server")
            if resource in self.timed_out_reads:
                raise RuntimeError("Command timed out; partial output was suppressed")
            name = args[2] if len(args) > 2 and not args[2].startswith("-") else None
            if name is not None:
                item = self.objects.get((resource, namespace, name))
                jsonpath = next((value for value in args if value.startswith("jsonpath=")), None)
                if jsonpath:
                    meta = (item or {}).get("metadata", {})
                    return self.result(0, f"{meta['uid']} {meta['resourceVersion']}" if item else "")
                return self.result(0, json.dumps(item) if item else "")
            selector = args[args.index("--selector") + 1] if "--selector" in args else None
            requirements = [part.partition("=") for part in selector.split(",")] if selector else []
            items = [
                item
                for (kind, space, _), item in sorted(self.objects.items())
                if kind == resource
                and (namespace is None or space == namespace)
                and all(
                    label in item["metadata"]["labels"]
                    and (not equals or item["metadata"]["labels"][label] == value)
                    for label, equals, value in requirements
                )
            ]
            return self.result(0, json.dumps({"items": items}))
        if verb == "create":
            document = json.loads(input_text)
            resource, metadata = KINDS[document["kind"]], document["metadata"]
            if resource in self.reject_create:
                return self.result(1, "", "admission webhook denied the request")
            key = (resource, metadata["namespace"], metadata["name"])
            if key in self.objects:
                return self.result(1, "", "AlreadyExists")
            item = self.add(resource, metadata["namespace"], metadata["name"], metadata.get("labels"))
            item.update({k: v for k, v in document.items() if k != "metadata"})
            if resource in {acceptance.TOPICS, acceptance.USERS} and resource not in self.never_ready:
                item["status"] = {"observedGeneration": 1, "conditions": [{"type": "Ready", "status": "True"}]}
            if resource == acceptance.USERS:
                secret = self.add(
                    "secrets", metadata["namespace"], metadata["name"], dict(metadata.get("labels", {})),
                    data={"user.crt": "Y3J0", "user.key": "a2V5"},
                )
                secret["metadata"]["ownerReferences"] = [{"uid": item["metadata"]["uid"]}]
            if resource == "pods":
                item["status"] = {"phase": self.pod_phase}
            return self.result(0, json.dumps(item))
        if verb == "delete":
            assert args[1] == "--raw" and args[3:] == ["-f", "-"], args
            prefix, target = args[2].split("/namespaces/")
            namespace, resource, name = target.split("/")
            if prefix == "/apis/kafka.strimzi.io/v1":
                resource += ".kafka.strimzi.io"
            else:
                assert prefix == "/api/v1", prefix
            key = resource, namespace, name
            item = self.objects.get(key)
            if item is None:
                return self.result(1, "", "NotFound")
            if resource in self.replace_on_delete:
                item = self.add(resource, namespace, name, item["metadata"]["labels"])
            options = json.loads(input_text)
            if options["preconditions"]["uid"] != item["metadata"]["uid"]:
                return self.result(1, "", "Conflict")
            self.objects.pop(key)
            for key, other in list(self.objects.items()):
                owners = other["metadata"].get("ownerReferences", [])
                if any(owner["uid"] == item["metadata"]["uid"] for owner in owners):
                    self.objects.pop(key)
            return self.result(0, "deleted")
        if verb == "logs":
            return self.result(0, self.log)
        raise AssertionError(f"Unexpected kubectl call: {args}")


class AcceptanceTests(unittest.TestCase):
    # Inputs: none. Outputs: private workspace and fake cluster. Purpose: isolate every test.
    # Dependencies: tempfile.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cluster = FakeCluster()

    # Inputs: enabled components. Outputs: normalized configuration and its source path.
    # Purpose: build real boundary-checked configurations. Dependencies: config.load_config.
    def load(self, postgres=True, kafka=True, monitoring=True):
        kubeconfig = self.root / "kubeconfig"
        kubeconfig.write_text("# Purpose: fake test kubeconfig; no credentials.\n")
        source = self.root / "input.json"
        write_json(
            source,
            {
                "environment_id": "atlas",
                "cluster": {"kubeconfig": str(kubeconfig)},
                "postgres": {"enabled": postgres},
                "kafka": {"enabled": kafka},
                "monitoring": {"enabled": monitoring},
                "storage": {"class": "dev-retain"},
            },
        )
        self.config = load_config(source)
        self.labels = self.config["derived"]["labels"]
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(exist_ok=True, mode=0o700)
        (self.workspace / "reports").mkdir(exist_ok=True, mode=0o700)
        self.identity = {**IDENTITY, "kubeconfig": self.config["cluster"]["kubeconfig"]}
        return source

    # Inputs: none. Outputs: none. Purpose: seed owned, ready monitoring objects for enabled data.
    # Dependencies: 40-monitoring naming.
    def seed_monitoring(self, skip=()):
        labels = {**self.labels, "release": "monitoring"}
        alerts = set()
        for component, names in (("postgres", runtime.POSTGRES_ALERTS), ("kafka", runtime.KAFKA_ALERTS)):
            if self.config[component]["enabled"]:
                alerts |= names
                if component not in skip:
                    self.cluster.add(
                        "podmonitors.monitoring.coreos.com", "monitoring", f"atlas-{component}", labels
                    )
        rules = [{"alert": name} for name in sorted(alerts)]
        self.cluster.add(
            "prometheusrules.monitoring.coreos.com", "monitoring", "atlas-data", labels,
            spec={"groups": [{"name": "atlas.data", "rules": rules}]},
        )

    # Inputs: optional topics/users to omit or leave unready. Outputs: none.
    # Purpose: seed Kafka sub-resources as 31-kafka and Strimzi create them. Dependencies: naming.
    def seed_kafka(self, missing_topic=False, unready_user=False):
        ready = {"observedGeneration": 1, "conditions": [{"type": "Ready", "status": "True"}]}
        for index, topic in enumerate(self.config["kafka"]["topics"]):
            if missing_topic and index == 0:
                continue
            name = f"atlas-{hashlib.sha256(topic.encode()).hexdigest()[:12]}"
            self.cluster.add(acceptance.TOPICS, "kafka", name, self.labels, spec={"topicName": topic}, status=ready)
        for role in ("producer", "consumer"):
            status = {"observedGeneration": 1, "conditions": []} if unready_user and role == "consumer" else ready
            self.cluster.add(acceptance.USERS, "kafka", f"atlas-kafka-{role}", self.labels, status=status)
        self.cluster.add(
            "kafkanodepools.kafka.strimzi.io", "kafka", "atlas-kafka-combined", self.labels,
            spec={"replicas": 1}, status={"observedGeneration": 1, "nodeIds": [0]},
        )
        self.cluster.add(
            "persistentvolumeclaims", "kafka", "data-0-atlas-kafka-atlas-kafka-combined-0",
            {"strimzi.io/cluster": "atlas-kafka"}, status={"phase": "Bound"},
        )

    # Inputs: discovered Prometheus and whether to read stage readiness. Outputs: verify report.
    # Purpose: run verify against the fake API. Dependencies: FakeCluster.
    def verify(self, prometheus=None, *, read_stages=False):
        readiness = (
            nullcontext() if read_stages else patch.object(runtime, "_stage_ready", return_value=(True, "Ready"))
        )
        with (
            patch.object(runtime, "run", side_effect=self.cluster),
            patch.object(acceptance, "run", side_effect=self.cluster),
            patch.object(runtime, "_identity", return_value=self.identity),
            readiness,
            patch.object(prerequisites, "_prometheus", return_value=prometheus),
        ):
            return runtime.verify(self.config, self.workspace)

    # Inputs: report and check name. Outputs: the named baseline check. Dependencies: none.
    @staticmethod
    def check(report, name, key="checks"):
        return next(item for item in report[key] if item["name"] == name)

    # Inputs: none. Outputs: assertions. Purpose: PostgreSQL-only monitoring expects only its own
    # PodMonitor and alerts, and reads no Kafka resources. Dependencies: verify.
    def test_postgres_only_monitoring_checks_exact_objects(self):
        self.load(kafka=False)
        self.seed_monitoring()
        self.cluster.add("persistentvolumeclaims", "atlas-data", "atlas-postgres-data", status={"phase": "Bound"})
        report = self.verify()
        self.assertEqual(report["baseline_result"], "pass", report["checks"])
        names = {item["name"] for item in report["checks"]}
        self.assertIn("podmonitor_postgres", names)
        self.assertNotIn("podmonitor_kafka", names)
        self.assertFalse(any("kafka" in " ".join(command) for command in self.cluster.commands))
        self.assertEqual(report["acceptance_result"], "incomplete")
        self.assertEqual(self.check(report, "prometheus_targets", "acceptance_checks")["status"], "pending")

    # Inputs: none. Outputs: assertions. Purpose: Kafka-only verify checks each Topic, User, the
    # node pool and its PVC. Dependencies: verify.
    def test_kafka_only_checks_every_sub_resource(self):
        self.load(postgres=False)
        self.seed_monitoring()
        self.seed_kafka()
        report = self.verify()
        self.assertEqual(report["baseline_result"], "pass", report["checks"])
        names = {item["name"] for item in report["checks"]}
        for topic in self.config["kafka"]["topics"]:
            self.assertIn(f"kafka_topic:{topic}", names)
        self.assertTrue({"kafka_user:producer", "kafka_user:consumer", "kafka_node_pool",
                         "kafka_pvc:data-0-atlas-kafka-atlas-kafka-combined-0", "podmonitor_kafka"} <= names)
        self.assertNotIn("podmonitor_postgres", names)
        self.assertNotIn("postgres_pvc", names)

    # Inputs: none. Outputs: assertions. Purpose: regression - with both services enabled, one
    # PodMonitor no longer satisfies the check, and the wrong alert set is a mismatch. Dependencies: verify.
    def test_both_services_require_both_podmonitors_and_rules(self):
        self.load()
        self.seed_monitoring(skip=("kafka",))
        self.seed_kafka()
        self.cluster.add("persistentvolumeclaims", "atlas-data", "atlas-postgres-data", status={"phase": "Bound"})
        report = self.verify()
        self.assertEqual(report["baseline_result"], "blocked")
        self.assertEqual(self.check(report, "podmonitor_postgres")["status"], "pass")
        missing = self.check(report, "podmonitor_kafka")
        self.assertEqual((missing["status"], missing["detail"]["state"]), ("blocked", "missing"))
        rule = self.cluster.objects[("prometheusrules.monitoring.coreos.com", "monitoring", "atlas-data")]
        rule["spec"]["groups"][0]["rules"].pop()
        rule["metadata"]["labels"][runtime.ENVIRONMENT_LABEL] = "atlas"
        self.cluster.add("podmonitors.monitoring.coreos.com", "monitoring", "atlas-kafka", self.labels)
        report = self.verify()
        self.assertEqual(self.check(report, "podmonitor_kafka")["detail"]["state"], "not-selected")
        self.assertEqual(self.check(report, "prometheusrule")["detail"]["state"], "mismatch")

    # Inputs: none. Outputs: assertions. Purpose: tell missing, not-ready, unreadable and denied
    # Kafka objects apart. Dependencies: _kafka_checks.
    def test_kafka_sub_resource_states_are_distinct(self):
        self.load(postgres=False, monitoring=False)
        self.seed_kafka(missing_topic=True, unready_user=True)
        claim = ("persistentvolumeclaims", "kafka", "data-0-atlas-kafka-atlas-kafka-combined-0")
        self.cluster.objects[claim]["status"]["phase"] = "Pending"
        with patch.object(runtime, "run", side_effect=self.cluster):
            states = {name: detail["state"] for name, _, detail in runtime._kafka_checks(self.config)}
        topics = self.config["kafka"]["topics"]
        self.assertEqual(states[f"kafka_topic:{topics[0]}"], "missing")
        self.assertEqual(states[f"kafka_topic:{topics[1]}"], "ready")
        self.assertEqual(states["kafka_user:consumer"], "not-ready")
        self.assertEqual(states["kafka_pvc:data-0-atlas-kafka-atlas-kafka-combined-0"], "not-bound")
        self.cluster.denied.add(acceptance.USERS)
        self.cluster.fail_reads.add(acceptance.TOPICS)
        with patch.object(runtime, "run", side_effect=self.cluster):
            states = {name: detail["state"] for name, _, detail in runtime._kafka_checks(self.config)}
        self.assertEqual(states["kafka_user:producer"], "access-denied")
        self.assertEqual(states[f"kafka_topic:{topics[1]}"], "read-failed")

    # Inputs: none. Outputs: assertions. Purpose: denied and timed-out reads leave later checks
    # available, including Kafka and monitoring. Dependencies: verify and FakeCluster.
    def test_verify_continues_after_stage_denial_and_object_timeout(self):
        self.load()
        self.seed_kafka()
        self.seed_monitoring()
        self.cluster.add(
            "kafkas.kafka.strimzi.io", "kafka", "atlas-kafka", self.labels,
            status={"observedGeneration": 1, "conditions": [{"type": "Ready", "status": "True"}]},
        )
        self.cluster.denied.add("statefulsets.apps")
        self.cluster.timed_out_reads.add("persistentvolumeclaims")
        write_json(self.workspace / "reports" / "acceptance-postgres.json", self.identity)
        report = self.verify(read_stages=True)
        self.assertEqual(self.check(report, "30-postgres")["detail"]["state"], "access-denied")
        self.assertEqual(self.check(report, "31-kafka")["status"], "pass")
        self.assertEqual(self.check(report, "postgres_pvc")["detail"]["state"], "read-failed")
        self.assertEqual(self.check(report, "kafka_user:producer")["status"], "pass")
        self.assertEqual(self.check(report, "podmonitor_kafka")["status"], "pass")
        self.assertNotIn("verification_access", {item["name"] for item in report["checks"]})

    # Inputs: none. Outputs: assertions. Purpose: mixed recording and alert rules keep the
    # expected alert set. Dependencies: verify.
    def test_recording_rule_is_ignored_in_alert_comparison(self):
        self.load(kafka=False)
        self.seed_monitoring()
        item = self.cluster.objects[("prometheusrules.monitoring.coreos.com", "monitoring", "atlas-data")]
        item["spec"]["groups"][0]["rules"].append({"record": "atlas:pg_up", "expr": "pg_up"})
        rule = self.check(self.verify(), "prometheusrule")
        self.assertEqual(rule["status"], "pass")
        self.assertEqual(rule["detail"]["alerts"], sorted(runtime.POSTGRES_ALERTS))

    # Inputs: none. Outputs: assertions. Purpose: a denied Prometheus proxy keeps items pending with
    # the reason; readable targets and samples become pass or fail. Dependencies: _prometheus_checks.
    def test_prometheus_targets_and_samples_or_pending_reason(self):
        self.load()
        instance = {"metadata": {"namespace": "monitoring", "name": "atlas-prometheus"}}
        base = "/api/v1/namespaces/monitoring/pods/http:prometheus-atlas-prometheus-0:9090/proxy/api/v1/"
        self.cluster.raw[base + "targets?state=active"] = (1, {})
        with patch.object(runtime, "run", side_effect=self.cluster), patch.object(
            prerequisites, "_prometheus", return_value=instance
        ):
            entries = runtime._prometheus_checks(self.config)
        self.assertEqual({item["status"] for item in entries}, {"pending"})
        self.assertIn("Access denied", entries[0]["detail"])
        targets = [
            {"scrapePool": "podMonitor/monitoring/atlas-postgres/0", "health": "up"},
            {"scrapePool": "podMonitor/monitoring/atlas-kafka/0", "health": "down"},
        ]
        self.cluster.raw[base + "targets?state=active"] = (0, {"status": "success", "data": {"activeTargets": targets}})
        sample = {"status": "success", "data": {"result": [{"value": [0, "1"]}]}}
        kafka_sample = {"status": "success", "data": {"result": [{"value": [0, "0"]}]}}
        derived = self.config["derived"]
        pg = f'pg_up{{namespace="{derived["namespace"]}",pod=~"atlas-postgres-[0-9]+"}}'
        kafka = 'kafka_under_replicated_partitions{namespace="kafka",pod=~"atlas-kafka-.+"}'
        self.cluster.raw[base + "query?" + urllib.parse.urlencode({"query": pg})] = (0, sample)
        self.cluster.raw[base + "query?" + urllib.parse.urlencode({"query": kafka})] = (0, kafka_sample)
        rules = [{"name": name, "state": "inactive"} for name in runtime.POSTGRES_ALERTS | runtime.KAFKA_ALERTS]
        self.cluster.raw[base + "rules?type=alert"] = (
            0, {"status": "success", "data": {"groups": [{"name": "atlas.data", "rules": rules}]}}
        )
        with patch.object(runtime, "run", side_effect=self.cluster), patch.object(
            prerequisites, "_prometheus", return_value=instance
        ):
            entries = {item["name"]: item for item in runtime._prometheus_checks(self.config)}
        self.assertEqual(entries["prometheus_targets"]["status"], "fail")
        self.assertEqual(entries["prometheus_targets"]["detail"]["atlas-kafka"], {"active": 1, "up": 0})
        self.assertEqual(entries["prometheus_samples"]["status"], "pass")
        self.assertEqual(entries["prometheus_rules"]["status"], "pass")
        self.cluster.raw[base + "targets?state=active"] = (1, {})
        with patch.object(runtime, "run", side_effect=self.cluster), patch.object(
            prerequisites, "_prometheus", return_value=instance
        ):
            entries = {item["name"]: item for item in runtime._prometheus_checks(self.config)}
        self.assertEqual(entries["prometheus_targets"]["status"], "pending")
        self.assertEqual(entries["prometheus_samples"]["status"], "pass")
        self.assertEqual(entries["prometheus_rules"]["status"], "pass")

    # Inputs: none. Outputs: assertions. Purpose: keep sharded evidence pending without
    # assuming one Pod has every target or sample. Dependencies: _prometheus_checks.
    def test_sharded_prometheus_evidence_remains_pending(self):
        self.load()
        instance = {
            "metadata": {"namespace": "monitoring", "name": "atlas-prometheus"},
            "spec": {"shards": 2},
        }
        with (
            patch.object(runtime, "run", side_effect=AssertionError("unexpected pod proxy request")),
            patch.object(prerequisites, "_prometheus", return_value=instance),
        ):
            entries = runtime._prometheus_checks(self.config)
        self.assertEqual(len(entries), 3)
        self.assertEqual({item["status"] for item in entries}, {"pending"})
        self.assertTrue(all("2 shards" in item["detail"] for item in entries))

    # Inputs: none. Outputs: assertions. Purpose: a failed functional check fails verify while the
    # baseline result stays separate. Dependencies: verify, _prometheus_checks.
    def test_failed_functional_check_is_reported_separately(self):
        self.load(kafka=False)
        self.seed_monitoring()
        self.cluster.add("persistentvolumeclaims", "atlas-data", "atlas-postgres-data", status={"phase": "Bound"})
        failing = [runtime._acceptance("prometheus_targets", "fail", {})]
        with patch.object(runtime, "_prometheus_checks", return_value=failing):
            report = self.verify()
        self.assertEqual(report["baseline_result"], "pass")
        self.assertEqual(report["acceptance_result"], "fail")
        self.assertEqual(report["status"], "fail")
        self.assertIn("Unauthorized cross-Pod traffic rejected by NetworkPolicy", report["pending_acceptance"])
        self.assertIn("Isolated alert route and receiver delivery", report["pending_acceptance"])

    # Inputs: component. Outputs: none. Purpose: seed applied stages and source Secrets.
    # Dependencies: credential and Strimzi naming.
    def seed_sources(self, component):
        for stage in ("00-bootstrap", acceptance.STAGES[component]):
            write_json(self.workspace / "reports" / f"applied-{stage}.json", {"status": "applied"})
        if component == "postgres":
            self.cluster.add("statefulsets.apps", "atlas-data", "atlas-postgres", self.labels)
            self.cluster.add(
                "secrets", "atlas-app", "atlas-postgres-client", self.labels,
                data={"username": "YXBw", "password": "c2VjcmV0", "ca.crt": "Y2E="},
            )
            return
        self.cluster.add("kafkas.kafka.strimzi.io", "kafka", "atlas-kafka", self.labels)
        for role in ("producer", "consumer"):
            self.cluster.add(acceptance.USERS, "kafka", f"atlas-kafka-{role}", self.labels)
            self.cluster.add(
                "secrets", "atlas-app", f"atlas-kafka-{role}-client", self.labels,
                data={"user.crt": "Y3J0", "user.key": "a2V5", "ca.crt": "Y2E="},
            )
        self.cluster.add(
            "secrets", "kafka", "atlas-kafka-cluster-ca-cert", {"strimzi.io/cluster": "atlas-kafka"},
            data={"ca.crt": "Y2E="},
        )

    # Inputs: component, plan ID and execute flag. Outputs: acceptance result.
    # Purpose: run the entry point against the fake cluster. Dependencies: FakeCluster.
    def run_acceptance(self, component, plan_id=None, execute=False):
        with (
            patch.object(runtime, "run", side_effect=self.cluster),
            patch.object(acceptance, "run", side_effect=self.cluster),
            patch.object(runtime, "_identity", return_value=self.identity),
            patch.object(runtime, "_stage_ready", return_value=(True, "Ready")),
            patch.object(acceptance, "wrong_ca", return_value="-----BEGIN CERTIFICATE-----\nwrong\n"),
            patch.object(acceptance.time, "sleep"),
            patch.object(acceptance, "READY_WAIT_SECONDS", 0),
            patch.object(acceptance, "POD_WAIT_SECONDS", 1),
            patch.object(acceptance, "CLEANUP_WAIT_SECONDS", 0),
        ):
            return acceptance.acceptance(self.config, self.workspace, component, plan_id, execute)

    # Inputs: none. Outputs: assertions. Purpose: the plan lists target, fixtures, reads, writes and
    # cleanup without writing to the cluster; a saved preview needs no cluster. Dependencies: acceptance.
    def test_plan_lists_effects_and_writes_nothing(self):
        self.load()
        self.seed_sources("kafka")
        plan = self.run_acceptance("kafka")
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["identity"]["kube_system_uid"], "cluster-one")
        resources = {item["resource"] for item in plan["fixtures"]}
        self.assertEqual(resources, {acceptance.TOPICS, acceptance.USERS, "secrets", "pods"})
        for key in ("reads", "writes", "cleanup", "untouched", "time_limits"):
            self.assertTrue(plan[key])
        self.assertFalse(any(command[0] in {"create", "delete"} for command in self.cluster.commands))
        with patch.object(acceptance, "run", side_effect=AssertionError("preview is offline")):
            preview = acceptance.acceptance(self.config, self.workspace, "kafka", plan["plan_id"])
        self.assertEqual(preview["status"], "preview")
        self.assertNotIn("c2VjcmV0", json.dumps(plan))

    # Inputs: none. Outputs: assertions. Purpose: a PostgreSQL run creates its fixtures, records
    # every check, deletes only its fixtures, and cannot be repeated. Dependencies: acceptance.
    def test_postgres_run_records_checks_and_cleans_up(self):
        self.load(kafka=False, monitoring=False)
        self.seed_sources("postgres")
        self.cluster.log = passing_log("postgres")
        plan = self.run_acceptance("postgres")
        report = self.run_acceptance("postgres", plan["plan_id"], execute=True)
        self.assertEqual((report["status"], report["result"], report["cleanup"]), ("pass", "pass", "complete"))
        self.assertEqual({item["cleanup"] for item in report["fixtures"]}, {"deleted"})
        self.assertEqual(len([c for c in self.cluster.commands if c[0] == "create"]), 2)
        self.assertFalse(any(key[2].startswith("atlas-acc-") for key in self.cluster.objects))
        self.assertNotIn("c2VjcmV0", json.dumps(read_json(self.workspace / "reports" / "acceptance-postgres.json")))
        with self.assertRaisesRegex(RuntimeError, "already used"):
            self.run_acceptance("postgres", plan["plan_id"], execute=True)
        with (
            patch.object(runtime, "run", side_effect=self.cluster),
            patch.object(acceptance, "run", side_effect=self.cluster),
        ):
            entries = acceptance.evidence(self.config, self.workspace, self.identity, "postgres")
        self.assertEqual({item["status"] for item in entries}, {"pass"})
        self.cluster.objects[("statefulsets.apps", "atlas-data", "atlas-postgres")]["metadata"]["generation"] = 2
        with (
            patch.object(runtime, "run", side_effect=self.cluster),
            patch.object(acceptance, "run", side_effect=self.cluster),
        ):
            entries = acceptance.evidence(self.config, self.workspace, self.identity, "postgres")
        self.assertEqual({item["status"] for item in entries}, {"pending"})

    # Inputs: none. Outputs: assertions. Purpose: Kafka mTLS, produce/consume and ACL results are
    # grouped, a failed check fails its group, and Strimzi's user Secret is confirmed removed.
    # Dependencies: acceptance.
    def test_kafka_run_groups_results_and_waits_for_dependents(self):
        self.load()
        self.seed_sources("kafka")
        self.cluster.log = passing_log("kafka").replace(
            "ACCEPTANCE consumer_forbidden_group PASS ok", "ACCEPTANCE consumer_forbidden_group FAIL unexpected"
        )
        plan = self.run_acceptance("kafka")
        report = self.run_acceptance("kafka", plan["plan_id"], execute=True)
        self.assertEqual((report["result"], report["status"], report["cleanup"]), ("fail", "fail", "complete"))
        self.assertEqual(report["dependents"][0]["cleanup"], "absent")
        self.assertEqual(report["pod_cleanup"]["consumer_group"]["status"], "pass")
        with (
            patch.object(runtime, "run", side_effect=self.cluster),
            patch.object(acceptance, "run", side_effect=self.cluster),
        ):
            entries = {item["name"]: item["status"] for item in acceptance.evidence(self.config, self.workspace, self.identity, "kafka")}
        self.assertEqual(entries, {"kafka_mtls": "pass", "kafka_produce_consume": "pass", "kafka_acl_rejection": "fail"})

    # Inputs: none. Outputs: assertions. Purpose: fail the run when the Kafka Pod did not prove
    # the group is absent, even when all functionality passed and Kubernetes fixtures are gone.
    # Dependencies: acceptance and FakeCluster.
    def test_kafka_cleanup_requires_group_absence_evidence(self):
        self.load()
        self.seed_sources("kafka")
        for cleanup in ("", "CLEANUP consumer_group FAIL absence-unconfirmed"):
            with self.subTest(cleanup=cleanup):
                self.cluster.log = passing_log("kafka").replace("CLEANUP consumer_group PASS absent", cleanup)
                plan = self.run_acceptance("kafka")
                report = self.run_acceptance("kafka", plan["plan_id"], execute=True)
                self.assertEqual((report["result"], report["status"], report["cleanup"]),
                                 ("pass", "fail", "incomplete"))
                self.assertEqual(report["pod_cleanup"]["consumer_group"]["status"], "fail")
                self.assertFalse(any(key[2].startswith("atlas-acc-") for key in self.cluster.objects))

    # Inputs: none. Outputs: assertions. Purpose: a failure mid-run removes the fixtures created so
    # far, never one it did not create, and leaves the result incomplete. Dependencies: acceptance.
    def test_mid_run_failure_cleans_only_owned_fixtures(self):
        self.load()
        self.seed_sources("kafka")
        plan = self.run_acceptance("kafka")
        self.cluster.never_ready.add(acceptance.USERS)
        report = self.run_acceptance("kafka", plan["plan_id"], execute=True)
        self.assertEqual((report["result"], report["status"]), ("incomplete", "fail"))
        self.assertIn("did not become Ready", report["error"])
        self.assertEqual([item["cleanup"] for item in report["fixtures"]], ["deleted", "deleted"])
        self.assertFalse(any(key[2].startswith("atlas-acc-") for key in self.cluster.objects))
        # A Pod rejected by admission was never created, so cleanup leaves the name alone.
        self.cluster.never_ready.clear()
        self.cluster.reject_create.add("pods")
        plan = self.run_acceptance("kafka")
        report = self.run_acceptance("kafka", plan["plan_id"], execute=True)
        states = {item["resource"]: item["cleanup"] for item in report["fixtures"]}
        self.assertEqual(states["pods"], "not-needed")
        self.assertEqual(states["secrets"], "deleted")
        self.assertEqual(report["cleanup"], "complete")

    # Inputs: none. Outputs: assertions. Purpose: an object replaced under the same name (new UID)
    # is not deleted and makes cleanup incomplete. Dependencies: Run.cleanup.
    def test_cleanup_skips_replaced_object(self):
        self.load(kafka=False, monitoring=False)
        self.seed_sources("postgres")
        plan = self.run_acceptance("postgres")
        original = acceptance.Run.wait_pod

        # Inputs: Run and Pod manifest. Outputs: Pod result. Purpose: replace the Pod mid-run.
        # Dependencies: FakeCluster.
        def replace(execution, document):
            metadata = document["metadata"]
            self.cluster.objects.pop(("pods", metadata["namespace"], metadata["name"]))
            self.cluster.add("pods", metadata["namespace"], metadata["name"], metadata["labels"])
            return original(execution, document)

        with patch.object(acceptance.Run, "wait_pod", replace):
            report = self.run_acceptance("postgres", plan["plan_id"], execute=True)
        states = {item["resource"]: item["cleanup"] for item in report["fixtures"]}
        self.assertEqual(states, {"pods": "skipped-not-owned", "secrets": "deleted"})
        self.assertEqual((report["cleanup"], report["status"]), ("incomplete", "fail"))
        # The leftover now blocks new plans until someone inspects it.
        with self.assertRaisesRegex(RuntimeError, "earlier acceptance run remain"):
            self.run_acceptance("postgres")

    # Inputs: none. Outputs: assertions. Purpose: the server rejects deletion of a replacement
    # created after cleanup's ownership read. Dependencies: API UID precondition and FakeCluster.
    def test_cleanup_uid_precondition_preserves_replacement(self):
        self.load(kafka=False, monitoring=False)
        self.seed_sources("postgres")
        self.cluster.log = passing_log("postgres")
        self.cluster.replace_on_delete.add("pods")
        plan = self.run_acceptance("postgres")
        report = self.run_acceptance("postgres", plan["plan_id"], execute=True)
        pod = next(item for item in report["fixtures"] if item["resource"] == "pods")
        self.assertEqual((report["cleanup"], report["status"]), ("incomplete", "fail"))
        self.assertEqual(pod["cleanup"], "delete-failed")
        replacement = self.cluster.objects[("pods", pod["namespace"], pod["name"])]
        self.assertNotEqual(replacement["metadata"]["uid"], pod["uid"])

    # Inputs: none. Outputs: assertions. Purpose: the shared Kafka namespace's cleanup gate only
    # considers fixtures from this environment. Dependencies: acceptance and label selectors.
    def test_kafka_leftovers_are_scoped_to_current_environment(self):
        self.load()
        self.seed_sources("kafka")
        other = {**self.labels, runtime.ENVIRONMENT_LABEL: "other", acceptance.RUN_LABEL: "abc123"}
        self.cluster.add(acceptance.TOPICS, "kafka", "other-fixture", other)
        self.assertEqual(self.run_acceptance("kafka")["status"], "planned")
        ours = {**self.labels, acceptance.RUN_LABEL: "def456"}
        self.cluster.add(acceptance.TOPICS, "kafka", "our-fixture", ours)
        with self.assertRaisesRegex(RuntimeError, r"earlier acceptance run remain.*our-fixture") as raised:
            self.run_acceptance("kafka")
        self.assertNotIn("other-fixture", str(raised.exception))

    # Inputs: none. Outputs: assertions. Purpose: detect source Secret rotation after preflight
    # but before copying credentials. Dependencies: Strimzi wait and fake Secret metadata.
    def test_kafka_reads_only_the_reviewed_secret_versions(self):
        self.load()
        self.seed_sources("kafka")
        wait_ready = acceptance.Run.wait_ready
        changes = [
            ("kafka", "atlas-kafka-cluster-ca-cert", "resourceVersion"),
            ("atlas-app", "atlas-kafka-consumer-client", "uid"),
            ("atlas-app", "atlas-kafka-producer-client", "resourceVersion"),
        ]

        # Inputs: fixture wait arguments. Outputs: Ready fixture. Purpose: rotate the chosen
        # source during reconciliation. Dependencies: FakeCluster and original wait.
        def rotate(execution, resource, name, namespace):
            if resource == acceptance.USERS:
                self.cluster.objects[("secrets", source_namespace, source_name)]["metadata"][field] += "-new"
            return wait_ready(execution, resource, name, namespace)

        for source_namespace, source_name, field in changes:
            with self.subTest(source=source_name):
                plan = self.run_acceptance("kafka")
                with patch.object(acceptance.Run, "wait_ready", rotate):
                    report = self.run_acceptance("kafka", plan["plan_id"], execute=True)
                self.assertEqual(report["status"], "fail")
                self.assertIn(f"{source_name} changed after review", report["error"])
                self.assertEqual({item["resource"] for item in report["fixtures"]},
                                 {acceptance.TOPICS, acceptance.USERS})
                self.assertEqual(report["cleanup"], "complete")

    # Inputs: none. Outputs: assertions. Purpose: a change to either client image digest
    # invalidates authorization before any fixture write. Dependencies: image catalog and acceptance.
    def test_plan_binds_client_image_for_each_component(self):
        self.load()
        for component in ("postgres", "kafka"):
            with self.subTest(component=component):
                self.seed_sources(component)
                plan = self.run_acceptance(component)
                current = runtime.IMAGES[component]["reference"]
                self.assertEqual(plan["client_image"], current)
                replacement = current.split("@")[0] + "@sha256:" + "0" * 64
                with patch.dict(runtime.IMAGES[component], reference=replacement), self.assertRaisesRegex(
                    RuntimeError, "client image changed after review"
                ):
                    self.run_acceptance(component, plan["plan_id"], execute=True)
        self.assertFalse(any(command[0] in {"create", "delete"} for command in self.cluster.commands))

    # Inputs: none. Outputs: assertions. Purpose: execution rechecks the target identity and the
    # reviewed service versions, and refuses denied source reads before any write. Dependencies: acceptance.
    def test_execution_rechecks_identity_and_access(self):
        self.load(kafka=False, monitoring=False)
        self.seed_sources("postgres")
        plan = self.run_acceptance("postgres")
        self.identity = {**self.identity, "server": "https://10.0.0.1:8443"}
        with patch.object(runtime, "_bind", return_value=True), self.assertRaisesRegex(
            RuntimeError, "identity changed"
        ):
            self.run_acceptance("postgres", plan["plan_id"], execute=True)
        self.identity["server"] = IDENTITY["server"]
        self.cluster.objects[("statefulsets.apps", "atlas-data", "atlas-postgres")]["metadata"]["generation"] = 3
        with self.assertRaisesRegex(RuntimeError, "changed after review"):
            self.run_acceptance("postgres", plan["plan_id"], execute=True)
        self.cluster.denied.add("statefulsets.apps")
        with self.assertRaises(runtime.ReadError) as raised:
            self.run_acceptance("postgres")
        self.assertEqual(raised.exception.reason, "access-denied")
        self.assertFalse(any(command[0] == "create" for command in self.cluster.commands))

    # Inputs: none. Outputs: assertions. Purpose: client Pods meet the restricted profile, carry the
    # access label NetworkPolicy expects, and never place credentials in the spec. Dependencies: manifests.
    def test_client_pods_are_restricted_and_reference_secrets(self):
        self.load()
        for component, label in (("postgres", "access-postgres"), ("kafka", "access-kafka")):
            pod = getattr(acceptance, f"{component}_pod")(self.config, "abc123")
            spec, container = pod["spec"], pod["spec"]["containers"][0]
            self.assertEqual(pod["metadata"]["namespace"], "atlas-validation")
            self.assertEqual(pod["metadata"]["labels"][f"local-k8s-platform/{label}"], "true")
            self.assertEqual(pod["metadata"]["labels"][acceptance.RUN_LABEL], "abc123")
            self.assertTrue(spec["securityContext"]["runAsNonRoot"])
            self.assertEqual(spec["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
            self.assertFalse(spec["automountServiceAccountToken"])
            self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
            self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
            self.assertTrue(container["securityContext"]["runAsNonRoot"])
            for variable in container["env"]:
                self.assertIn("valueFrom", variable)
            self.assertEqual(spec["volumes"][0]["secret"]["secretName"], f"atlas-acc-abc123-{component}")
        script = acceptance.kafka_pod(self.config, "abc123")["spec"]["containers"][0]["command"][2]
        self.assertIn(">/dev/null", script)
        topic, user = acceptance.kafka_fixtures(self.config, "abc123")
        granted = {acl["resource"]["name"] for acl in user["spec"]["authorization"]["acls"]}
        self.assertEqual(granted, {"atlas.acceptance.abc123", "atlas-acc-abc123-group"})
        self.assertNotIn(topic["spec"]["topicName"], self.config["kafka"]["topics"])

    # Inputs: none. Outputs: assertions. Purpose: execute the Kafka Pod shell with fake tools
    # to check metadata-only ACLs, disabled commits, and zero-exit group deletion errors.
    # Dependencies: /bin/sh and temporary directory.
    def test_kafka_pod_probes_and_verifies_cleanup(self):
        self.load()
        pod = acceptance.kafka_pod(self.config, "abc123")
        script = pod["spec"]["containers"][0]["command"][2].replace("/tmp", ".").replace("/acceptance/", "./")
        fake_tools = """
describe_result=$1; listed_topic_suffix=$2
cat() { :; }
timeout() {
  shift
  tool=${1##*/}
  shift
  case "$tool $*" in
    kafka-topics.sh*"./fixture.properties --list") echo "atlas.acceptance.abc123$listed_topic_suffix" ;;
    kafka-topics.sh*"./anonymous.properties --list") echo SslAuthenticationException >&2; return 1 ;;
    kafka-console-producer.sh*"--command-config ./fixture.properties --topic "*) read -r message ;;
    kafka-console-producer.sh*"--command-config ./producer.properties --topic "*) echo TopicAuthorizationException >&2 ;;
    kafka-console-consumer.sh*"--command-config ./fixture.properties"*"--from-beginning"*) echo acceptance-abc123 ;;
    kafka-console-consumer.sh*"./fixture.properties --command-property enable.auto.commit=false"*) echo TopicAuthorizationException >&2 ;;
    kafka-console-consumer.sh*"./consumer.properties --command-property enable.auto.commit=false"*) echo GroupAuthorizationException >&2 ;;
    kafka-consumer-groups.sh*"--group atlas-acc-abc123-group --delete")
      echo 'Error: Deletion failed'; return 0 ;;
    kafka-consumer-groups.sh*"--group atlas-acc-abc123-group --describe --state")
      case "$describe_result" in
        absent) echo "Error: Consumer group 'atlas-acc-abc123-group' does not exist." ;;
        missing) echo 'Caused by: org.apache.kafka.common.errors.GroupIdNotFoundException: The group id does not exist.' >&2 ;;
        present) echo 'atlas-acc-abc123-group coordinator Empty 0' ;;
        denied) echo 'Error: GroupAuthorizationException' ;;
      esac ;;
    *) return 1 ;;
  esac
}
"""
        for outcome in ("absent", "missing", "present", "denied"):
            with self.subTest(outcome=outcome):
                result = subprocess.run(
                    ["/bin/sh", "-c", fake_tools + script, "kafka-probe", outcome, "-extra" if outcome == "denied" else ""],
                    cwd=self.root, capture_output=True, text=True, check=True, timeout=5,
                )
                checks, cleanup = acceptance.parse(result.stdout, "kafka")
                self.assertEqual(
                    {item["name"] for item in checks if item["status"] != "pass"},
                    {"mtls_metadata"} if outcome == "denied" else set(),
                )
                self.assertEqual(cleanup["consumer_group"]["status"],
                                 "pass" if outcome in {"absent", "missing"} else "fail")

    # Inputs: none. Outputs: assertions. Purpose: only fixed result lines for expected checks count.
    # Dependencies: acceptance.parse.
    def test_parse_accepts_only_expected_tokens(self):
        log = "ACCEPTANCE verify_full PASS tls\nACCEPTANCE injected PASS ok\nACCEPTANCE wrong_ca_rejected PASS has space\n"
        checks, _ = acceptance.parse(log, "postgres")
        states = {item["name"]: item["status"] for item in checks}
        self.assertEqual(states["verify_full"], "pass")
        self.assertEqual(states["wrong_ca_rejected"], "not-run")
        self.assertNotIn("injected", states)

    # Inputs: none. Outputs: assertions. Purpose: the CLI rejects acceptance for a disabled
    # component before touching the workspace or cluster. Dependencies: platform CLI.
    def test_cli_rejects_disabled_component(self):
        source = self.load(kafka=False, monitoring=False)
        error = io.StringIO()
        with redirect_stderr(error), redirect_stdout(io.StringIO()):
            status = CLI.main(
                ["--config", str(source), "--workspace", str(self.root / "cli"), "acceptance", "--component", "kafka"]
            )
        self.assertEqual(status, 2)
        self.assertIn("disabled", error.getvalue())
        self.assertFalse((self.root / "cli").exists())


if __name__ == "__main__":
    unittest.main()
