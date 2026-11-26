"""Purpose: run reviewed, isolated PostgreSQL and Kafka functional acceptance with fixtures it creates
and removes, and turn current results into verify evidence."""

import base64
import datetime as dt
import json
import re
import secrets
import tempfile
import time
from pathlib import Path

import runtime
from io_utils import private_dir, read_json, run, sha256, write_json

RUN_LABEL = "local-k8s-platform/acceptance-run"
COMPONENT_LABEL = "local-k8s-platform/acceptance-component"
POD_WAIT_SECONDS = 600
READY_WAIT_SECONDS = 180
CLEANUP_WAIT_SECONDS = 60
RESULT_LINE = re.compile(r"^(ACCEPTANCE|CLEANUP) ([a-z_]+) (PASS|FAIL) ([a-z0-9-]+)$")
# Each verify item passes only when every runner check in its group passes.
GROUPS = {
    "postgres": {
        "postgres_tls": ("verify_full", "wrong_ca_rejected", "plaintext_rejected"),
        "postgres_permissions": (
            "app_role_attributes",
            "app_write_rolled_back",
            "privilege_escalation_denied",
            "admin_remote_rejected",
            "other_database_rejected",
        ),
    },
    "kafka": {
        "kafka_mtls": ("mtls_metadata", "client_certificate_required"),
        "kafka_produce_consume": ("produce_consume",),
        "kafka_acl_rejection": (
            "fixture_forbidden_topic",
            "producer_forbidden_topic",
            "consumer_forbidden_group",
        ),
    },
}
STAGES = {"postgres": "30-postgres", "kafka": "31-kafka"}
TOPICS = "kafkatopics.kafka.strimzi.io"
USERS = "kafkausers.kafka.strimzi.io"
FIXTURE_API_PATHS = {
    "pods": "/api/v1",
    "secrets": "/api/v1",
    TOPICS: "/apis/kafka.strimzi.io/v1",
    USERS: "/apis/kafka.strimzi.io/v1",
}


# Inputs: resolved configuration, component and run ID. Outputs: fixture names and namespaces.
# Purpose: derive every fixture name from one random run ID. Dependencies: derived names.
def names(config, component, run_id):
    base = f"{config['environment_id']}-acc-{run_id}"
    result = {
        "run_id": run_id,
        "validation_namespace": config["derived"]["validation_namespace"],
        "pod": f"{base}-{component}",
        "secret": f"{base}-{component}",
    }
    if component == "kafka":
        result.update(
            kafka_namespace=config["kafka"]["namespace"],
            topic_resource=base,
            topic=f"{config['environment_id']}.acceptance.{run_id}",
            user=base,
            group=f"{base}-group",
            forbidden_group=f"{base}-forbidden",
        )
    return result


# Inputs: resolved configuration, component and run ID. Outputs: labels for fixtures.
# Purpose: mark ownership and this run so cleanup can prove each object is ours. Dependencies: none.
def labels(config, component, run_id):
    return {**config["derived"]["labels"], RUN_LABEL: run_id, COMPONENT_LABEL: component}


# Inputs: configuration and a Secret reference. Outputs: {"uid", "resource_version"} or None.
# Purpose: bind evidence to credential versions without reading Secret data. Dependencies: kubectl jsonpath.
def _secret_version(config, namespace, name):
    message = f"Cannot read Secret metadata {namespace}/{name}."
    try:
        result = run(
            runtime._kube(config)
            + ["get", "secret", name, "--namespace", namespace, "--ignore-not-found=true"]
            + ["-o", "jsonpath={.metadata.uid} {.metadata.resourceVersion}"],
            timeout=20,
        )
    except (RuntimeError, ValueError) as error:
        raise runtime.ReadError(message, "read-failed") from error
    if result.returncode:
        raise runtime.ReadError(message, runtime._read_failure(result))
    fields = result.stdout.split()
    return {"uid": fields[0], "resource_version": fields[1]} if len(fields) == 2 else None


# Inputs: object or None. Outputs: {"uid", "generation"} or None.
# Purpose: record which rollout a result belongs to. Dependencies: Kubernetes metadata.
def _version(item):
    if not item:
        return None
    return {"uid": item["metadata"].get("uid"), "generation": item["metadata"].get("generation")}


# Inputs: resolved configuration and component. Outputs: current target versions.
# Purpose: tie evidence to the service rollout and the credentials it tested. Dependencies: kubectl GET.
def target_state(config, component):
    derived, application = config["derived"], config["application"]["namespace"]
    if component == "postgres":
        return {
            "statefulset": _version(
                runtime._get(
                    config, "statefulsets.apps", derived["postgres_name"], derived["namespace"]
                )
            ),
            "client_secret": _secret_version(config, application, f"{derived['postgres_name']}-client"),
        }
    namespace, name = config["kafka"]["namespace"], derived["kafka_name"]
    return {
        "kafka": _version(runtime._get(config, "kafkas.kafka.strimzi.io", name, namespace)),
        **{
            f"{role}_user": _version(runtime._get(config, USERS, f"{name}-{role}", namespace))
            for role in ("producer", "consumer")
        },
        **{
            f"{role}_client_secret": _secret_version(config, application, f"{name}-{role}-client")
            for role in ("producer", "consumer")
        },
        "cluster_ca": _secret_version(config, namespace, f"{name}-cluster-ca-cert"),
    }


# Inputs: resolved configuration and component. Outputs: "namespace/resource/name" strings.
# Purpose: find leftovers owned by this environment. Dependencies: kubectl GET with label selector.
def leftovers(config, component):
    scopes = [(config["derived"]["validation_namespace"], ("pods", "secrets"))]
    if component == "kafka":
        scopes.append((config["kafka"]["namespace"], (TOPICS, USERS, "secrets")))
    selector = f"{RUN_LABEL},{runtime.ENVIRONMENT_LABEL}={config['environment_id']}"
    found = []
    for namespace, resources in scopes:
        for resource in resources:
            items = runtime._get(config, resource, namespace=namespace, selector=selector)
            found += [
                f"{namespace}/{resource}/{item['metadata']['name']}"
                for item in (items or {}).get("items", [])
            ]
    return found


# Inputs: resolved configuration, workspace and component. Outputs: (identity, target state).
# Purpose: check binding, stage readiness, source credentials and leftovers before any fixture
# write. Dependencies: runtime checks, read-only kubectl.
def preflight(config, workspace, component):
    identity = runtime._identity(config)
    if not runtime._bind(config, workspace, identity):
        raise RuntimeError("The target must match the workspace binding.")
    for stage in ("00-bootstrap", STAGES[component]):
        record = workspace / "reports" / f"applied-{stage}.json"
        if not record.exists() or read_json(record).get("status") != "applied":
            raise RuntimeError(f"Stage {stage} must be applied before acceptance.")
        ready, _ = runtime._stage_ready(config, stage)
        if not ready:
            raise RuntimeError(f"Stage {stage} is not ready; run verify first.")
    target = target_state(config, component)
    missing = sorted(key for key, value in target.items() if value is None)
    if missing:
        hint = (
            "run credentials --execute"
            if component == "postgres"
            else "run credentials --component kafka --execute"
        )
        raise RuntimeError(f"Acceptance sources are missing ({', '.join(missing)}); {hint} first.")
    left = leftovers(config, component)
    if left:
        raise RuntimeError(
            "Fixtures from an earlier acceptance run remain; inspect and remove them first: "
            + ", ".join(left)
        )
    return identity, target


# Inputs: resolved configuration, component and run ID. Outputs: non-sensitive review summary.
# Purpose: list target, resources, read/write effects and cleanup scope before authorization.
# Dependencies: fixture naming.
def summary(config, component, run_id):
    n = names(config, component, run_id)
    validation, application = n["validation_namespace"], config["application"]["namespace"]
    derived = config["derived"]
    fixtures = [
        {"resource": "secrets", "namespace": validation, "name": n["secret"]},
        {"resource": "pods", "namespace": validation, "name": n["pod"]},
    ]
    if component == "postgres":
        reads = [f"Secret {application}/{derived['postgres_name']}-client (copied, not printed)"]
        writes = [
            "Create the fixture Secret: the application login, the server CA, and a throwaway wrong CA",
            "Create one restricted client Pod with the PostgreSQL access label",
            "Open TLS sessions as the application role; the table write runs inside a transaction "
            "that is rolled back, and privilege probes run inside rolled-back transactions",
            "Attempt rejected logins: wrong CA, plaintext, the admin role over the network, "
            "and the application role on the postgres database",
        ]
        cleanup = [
            "Delete the fixture Pod and Secret when their UID and run label match this run",
            "The wrong-CA private key exists only in a private local temporary directory and is removed",
        ]
        untouched = ["Application tables and data", "PostgreSQL roles, passwords and certificates"]
    else:
        kafka = n["kafka_namespace"]
        fixtures = [
            {"resource": TOPICS, "namespace": kafka, "name": n["topic_resource"]},
            {"resource": USERS, "namespace": kafka, "name": n["user"]},
            *fixtures,
        ]
        reads = [
            f"Secret {kafka}/{n['user']} issued by Strimzi for the fixture user",
            f"Secret {kafka}/{derived['kafka_name']}-cluster-ca-cert (public CA)",
            *(
                f"Secret {application}/{derived['kafka_name']}-{role}-client (copied, not printed)"
                for role in ("producer", "consumer")
            ),
        ]
        writes = [
            f"Create KafkaTopic {n['topic']} (1 partition, 1 hour retention) and KafkaUser "
            f"{n['user']} limited to that topic and group {n['group']}",
            "Create the fixture Secret with the fixture, producer and consumer client certificates",
            "Create one restricted client Pod with the Kafka access label",
            "Produce and consume one message on the fixture topic only",
            "Attempt rejected operations: no client certificate, the fixture user reading an "
            "application topic, the application producer writing to the fixture topic, and the "
            f"application consumer joining group {n['forbidden_group']}",
        ]
        cleanup = [
            "Delete the fixture consumer group from inside the Pod and confirm it is absent",
            "Delete the Pod, fixture Secret, KafkaUser and KafkaTopic when their UID and run "
            "label match this run; deleting the KafkaTopic removes the fixture topic and its message",
            "Strimzi removes the fixture user's Secret through its owner reference; the run waits for it",
        ]
        untouched = [
            "Application topics, messages, committed offsets and consumer groups",
            "Application KafkaUsers, ACLs and client Secrets",
        ]
    return {
        "component": component,
        "run_id": run_id,
        "fixtures": fixtures,
        "reads": reads,
        "writes": writes,
        "cleanup": cleanup,
        "untouched": untouched,
        "time_limits": {
            "pod_seconds": POD_WAIT_SECONDS,
            "fixture_ready_seconds": READY_WAIT_SECONDS,
        },
        "checks": {group: list(items) for group, items in GROUPS[component].items()},
    }


# Inputs: none. Outputs: PEM text of a fresh CA certificate unrelated to the platform.
# Purpose: give the wrong-CA probe a real, never-trusted CA; the key never leaves a private temp
# directory that is removed at once. Dependencies: OpenSSL through the credential helpers.
def wrong_ca():
    import credentials

    executable = credentials.certificate_tool()
    with tempfile.TemporaryDirectory(prefix="lkp-acceptance-") as directory:
        credentials.openssl(
            [
                "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                "-nodes", "-keyout", "wrong-ca.key", "-out", "wrong-ca.crt", "-days", "1",
                "-subj", "/CN=local-k8s-platform-acceptance-wrong-ca",
            ],
            Path(directory),
            executable,
        )
        return (Path(directory) / "wrong-ca.crt").read_text(encoding="utf-8")


# Inputs: container name, image, script, environment and user ID. Outputs: restricted container.
# Purpose: share the Pod Security restricted settings of test clients. Dependencies: none.
def _container(name, image, script, environment, user):
    return {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["sh", "-c", script],
        "env": environment,
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "runAsNonRoot": True,
            "runAsUser": user,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [
            {"name": "acceptance", "mountPath": "/acceptance", "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
    }


# Inputs: configuration, component, run ID, container, access label and deadline. Outputs: Pod.
# Purpose: build one non-root client Pod that mounts only the fixture Secret. Dependencies: labels.
def _pod(config, component, run_id, container, access_label, deadline, resources):
    n = names(config, component, run_id)
    container["resources"] = resources
    user = container["securityContext"]["runAsUser"]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": n["pod"],
            "namespace": n["validation_namespace"],
            "labels": {**labels(config, component, run_id), access_label: "true"},
        },
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": deadline,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": user,
                "runAsGroup": user,
                "fsGroup": user,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [container],
            "volumes": [
                {
                    "name": "acceptance",
                    "secret": {"secretName": n["secret"], "defaultMode": 0o440},
                },
                {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
            ],
        },
    }


# Shell helpers shared by client scripts: results print only fixed tokens, never command output.
REPORT = 'report() { echo "$1 $2 $3 $4"; }\n'


# Inputs: resolved configuration and run ID. Outputs: PostgreSQL client Pod manifest.
# Purpose: check verify-full, CA and login rejection, and role limits as the application role.
# Dependencies: pinned PostgreSQL image (psql), pg_hba rules in 30-postgres.
def postgres_pod(config, run_id):
    derived = config["derived"]
    host = f"{derived['postgres_name']}-rw.{derived['namespace']}.svc.cluster.local"
    database, user = config["application"]["database"], config["application"]["username"]
    probe = f"acceptance_probe_{run_id}"
    # Database and role names are validated identifiers ([a-z][a-z0-9_]*), so literal quoting is safe.
    script = REPORT + f"""
umask 077
export HOME=/tmp
H={host}
classify() {{
  case "$1" in
    *"certificate verify failed"*) echo certificate-rejected ;;
    *"no pg_hba.conf entry"*|*"pg_hba.conf rejects connection"*) echo hba-rejected ;;
    *"permission denied"*|*"must be"*) echo permission-denied ;;
    *"password authentication failed"*) echo password-rejected ;;
    *"timeout expired"*|*"could not connect"*|*"Connection refused"*) echo unreachable ;;
    *) echo unexpected ;;
  esac
}}
conn() {{ echo "host=$H port=5432 dbname=$1 user=$2 sslmode=$3 sslrootcert=$4 connect_timeout=10"; }}
sql() {{ psql "$(conn {database} "$PGUSER" verify-full /acceptance/ca.crt)" -XAtq -v ON_ERROR_STOP=1 "$@" 2>&1; }}
expect_reject() {{
  name=$1; wanted=$2; shift 2
  if out=$("$@" 2>&1); then report ACCEPTANCE "$name" FAIL accepted
  else got=$(classify "$out"); [ "$got" = "$wanted" ] && report ACCEPTANCE "$name" PASS "$got" || report ACCEPTANCE "$name" FAIL "$got"; fi
}}
out=$(sql -c "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
[ "$out" = t ] && report ACCEPTANCE verify_full PASS tls || report ACCEPTANCE verify_full FAIL "$(classify "$out")"
expect_reject wrong_ca_rejected certificate-rejected psql "$(conn {database} "$PGUSER" verify-full /acceptance/wrong-ca.crt)" -XAtqc "SELECT 1"
expect_reject plaintext_rejected hba-rejected psql "$(conn {database} "$PGUSER" disable /acceptance/ca.crt)" -XAtqc "SELECT 1"
expect_reject admin_remote_rejected hba-rejected psql "$(conn {database} postgres verify-full /acceptance/ca.crt)" -XAtqc "SELECT 1"
expect_reject other_database_rejected hba-rejected psql "$(conn postgres "$PGUSER" verify-full /acceptance/ca.crt)" -XAtqc "SELECT 1"
out=$(sql -c "SELECT current_database()='{database}' AND current_user='{user}' AND NOT (SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname=current_user) AND NOT pg_has_role(current_user,'pg_monitor','MEMBER')")
[ "$out" = t ] && report ACCEPTANCE app_role_attributes PASS limited || report ACCEPTANCE app_role_attributes FAIL "$( [ "$out" = f ] && echo excess-privilege || classify "$out")"
out=$(printf '%s\\n' 'BEGIN;' 'CREATE TABLE {probe} (id integer);' 'INSERT INTO {probe} VALUES (1);' 'SELECT count(*) FROM {probe};' 'ROLLBACK;' "SELECT to_regclass('{probe}') IS NULL;" | sql)
[ "$out" = "1
t" ] && report ACCEPTANCE app_write_rolled_back PASS rolled-back || report ACCEPTANCE app_write_rolled_back FAIL "$(classify "$out")"
denied=0
for statement in "CREATE ROLE {probe}" "SELECT pg_read_file('PG_VERSION')" "SET ROLE postgres"; do
  out=$(printf '%s\\n' 'BEGIN;' "$statement;" 'ROLLBACK;' | sql) && continue
  [ "$(classify "$out")" = permission-denied ] && denied=$((denied + 1))
done
[ "$denied" = 3 ] && report ACCEPTANCE privilege_escalation_denied PASS permission-denied || report ACCEPTANCE privilege_escalation_denied FAIL allowed-or-unexpected
exit 0
"""
    environment = [
        {"name": key, "valueFrom": {"secretKeyRef": {"name": names(config, "postgres", run_id)["secret"], "key": field}}}
        for key, field in (("PGUSER", "username"), ("PGPASSWORD", "password"))
    ]
    container = _container(
        "postgres-acceptance", runtime.IMAGES["postgres"]["reference"], script, environment, 999
    )
    resources = {
        "requests": {"cpu": "10m", "memory": "32Mi"},
        "limits": {"cpu": "200m", "memory": "128Mi"},
    }
    return _pod(config, "postgres", run_id, container, "local-k8s-platform/access-postgres", 300, resources)


# Inputs: resolved configuration and run ID. Outputs: Kafka client Pod manifest.
# Purpose: check mTLS, fixture produce/consume and ACL rejection for application identities.
# Dependencies: pinned Strimzi Kafka image (/opt/kafka/bin tools), 31-kafka ACLs.
def kafka_pod(config, run_id):
    n = names(config, "kafka", run_id)
    bootstrap = f"{config['derived']['kafka_name']}-kafka-bootstrap.{n['kafka_namespace']}.svc.cluster.local:9093"
    application_topic = config["kafka"]["topics"][0]
    # Command output stays in shell variables or /dev/null so application messages never reach the log.
    # Per-command limits sum to 510 seconds, below the Pod deadline of POD_WAIT_SECONDS - 30.
    script = REPORT + f"""
umask 077
export HOME=/tmp LOG_DIR=/tmp/logs KAFKA_HEAP_OPTS=-Xmx192m
K=/opt/kafka/bin; B={bootstrap}; T={n['topic']}; G={n['group']}; X={n['forbidden_group']}; A={application_topic}
props() {{
  {{
    echo security.protocol=SSL
    echo ssl.truststore.type=PEM
    echo ssl.truststore.location=/acceptance/ca.crt
    echo request.timeout.ms=10000
    echo default.api.timeout.ms=20000
    if [ -n "$2" ]; then
      cat "/acceptance/$2.key" "/acceptance/$2.crt" > "/tmp/$1.pem"
      echo ssl.keystore.type=PEM
      echo "ssl.keystore.location=/tmp/$1.pem"
    fi
  }} > "/tmp/$1.properties"
}}
props fixture fixture; props producer producer; props consumer consumer; props anonymous ""
classify() {{
  case "$1" in
    *GroupAuthorizationException*|*"Not authorized to access group"*) echo group-denied ;;
    *TopicAuthorizationException*|*"Not authorized to access topics"*) echo topic-denied ;;
    *SslAuthenticationException*|*"SSL handshake failed"*) echo tls-rejected ;;
    *TimeoutException*|*"timed out"*) echo timeout ;;
    *) echo unexpected ;;
  esac
}}
check() {{ got=$(classify "$3"); [ "$got" = "$2" ] && report ACCEPTANCE "$1" PASS "$got" || report ACCEPTANCE "$1" FAIL "$got"; }}
# Listing reads topic metadata using the fixture's Describe ACL.
if out=$(timeout 60 $K/kafka-topics.sh --bootstrap-server $B --command-config /tmp/fixture.properties --list 2>&1) &&
   printf '%s\\n' "$out" | grep -Fxq -- "$T"; then
  report ACCEPTANCE mtls_metadata PASS metadata
else report ACCEPTANCE mtls_metadata FAIL "$(classify "$out")"; fi
if out=$(timeout 60 $K/kafka-topics.sh --bootstrap-server $B --command-config /tmp/anonymous.properties --list 2>&1); then
  report ACCEPTANCE client_certificate_required FAIL accepted
else check client_certificate_required tls-rejected "$out"; fi
message="acceptance-{run_id}"
printf '%s\\n' "$message" | timeout 60 $K/kafka-console-producer.sh --bootstrap-server $B --command-config /tmp/fixture.properties --topic $T >/dev/null 2>&1
got=$(timeout 90 $K/kafka-console-consumer.sh --bootstrap-server $B --command-config /tmp/fixture.properties --topic $T --group $G --from-beginning --max-messages 1 --timeout-ms 45000 2>/dev/null)
[ "$got" = "$message" ] && report ACCEPTANCE produce_consume PASS round-trip || report ACCEPTANCE produce_consume FAIL no-message
err=$(timeout 60 $K/kafka-console-consumer.sh --bootstrap-server $B --command-config /tmp/fixture.properties --command-property enable.auto.commit=false --topic $A --group $G --max-messages 1 --timeout-ms 20000 2>&1 >/dev/null)
check fixture_forbidden_topic topic-denied "$err"
err=$(printf 'forbidden\\n' | timeout 60 $K/kafka-console-producer.sh --bootstrap-server $B --command-config /tmp/producer.properties --topic $T 2>&1 >/dev/null)
check producer_forbidden_topic topic-denied "$err"
err=$(timeout 60 $K/kafka-console-consumer.sh --bootstrap-server $B --command-config /tmp/consumer.properties --command-property enable.auto.commit=false --topic $A --group $X --max-messages 1 --timeout-ms 20000 2>&1 >/dev/null)
check consumer_forbidden_group group-denied "$err"
groups() {{
  timeout 30 $K/kafka-consumer-groups.sh --bootstrap-server $B --command-config /tmp/fixture.properties --timeout 20000 --group "$G" "$@"
}}
# The delete CLI can print an error and exit zero. Require an explicit absence from Describe.
groups --delete >/dev/null 2>&1
if out=$(groups --describe --state 2>&1); then
  case "$out" in
    *"Error: Consumer group '$G' does not exist."*|*"Caused by: org.apache.kafka.common.errors.GroupIdNotFoundException"*)
      report CLEANUP consumer_group PASS absent; exit 0 ;;
  esac
fi
report CLEANUP consumer_group FAIL absence-unconfirmed
exit 0
"""
    container = _container(
        "kafka-acceptance", runtime.IMAGES["kafka"]["reference"], script, [], 1001
    )
    resources = {
        "requests": {"cpu": "100m", "memory": "320Mi"},
        "limits": {"cpu": "1000m", "memory": "512Mi"},
    }
    return _pod(config, "kafka", run_id, container, "local-k8s-platform/access-kafka", POD_WAIT_SECONDS - 30, resources)


# Inputs: resolved configuration and run ID. Outputs: fixture KafkaTopic and KafkaUser manifests.
# Purpose: keep produce/consume and the fixture identity away from application topics. Dependencies: Strimzi v1 CRDs.
def kafka_fixtures(config, run_id):
    n = names(config, "kafka", run_id)
    metadata = {
        "namespace": n["kafka_namespace"],
        "labels": {
            **labels(config, "kafka", run_id),
            "strimzi.io/cluster": config["derived"]["kafka_name"],
        },
    }
    topic = {
        "apiVersion": "kafka.strimzi.io/v1",
        "kind": "KafkaTopic",
        "metadata": {**metadata, "name": n["topic_resource"]},
        "spec": {
            "topicName": n["topic"],
            "partitions": 1,
            "replicas": 1,
            "config": {"retention.ms": "3600000", "retention.bytes": "1048576"},
        },
    }
    acls = [
        {
            "resource": {"type": "topic", "name": n["topic"], "patternType": "literal"},
            "operations": ["Write", "Read", "Describe"],
            "host": "*",
        },
        {
            "resource": {"type": "group", "name": n["group"], "patternType": "literal"},
            "operations": ["Read", "Describe", "Delete"],
            "host": "*",
        },
    ]
    user = {
        "apiVersion": "kafka.strimzi.io/v1",
        "kind": "KafkaUser",
        "metadata": {**metadata, "name": n["user"]},
        "spec": {
            "authentication": {"type": "tls"},
            "authorization": {"type": "simple", "acls": acls},
        },
    }
    return topic, user


# Inputs: configuration, component, run ID and base64 data. Outputs: fixture Secret manifest.
# Purpose: give the client Pod only the fields it needs. Dependencies: labels.
def fixture_secret(config, component, run_id, data):
    n = names(config, component, run_id)
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": n["secret"],
            "namespace": n["validation_namespace"],
            "labels": labels(config, component, run_id),
        },
        "type": "Opaque",
        "data": data,
    }


class Run:
    """One acceptance execution: its report, fixture ledger and saving."""

    # Inputs: configuration, report path and initial report. Outputs: instance.
    # Purpose: keep fixture records on disk after every change. Dependencies: write_json.
    def __init__(self, config, path, report):
        self.config, self.path, self.report = config, path, report

    # Inputs: none. Outputs: none. Purpose: persist progress so an interruption leaves evidence.
    # Dependencies: private JSON.
    def save(self):
        write_json(self.path, self.report)

    # Inputs: manifest and kubectl resource name. Outputs: created UID.
    # Purpose: create one fixture atomically and record it before and after. Dependencies: kubectl create.
    def create(self, document, resource):
        metadata = document["metadata"]
        entry = {
            "resource": resource,
            "namespace": metadata["namespace"],
            "name": metadata["name"],
            "uid": None,
            "state": "creating",
        }
        self.report["fixtures"].append(entry)
        self.save()
        try:
            result = run(
                runtime._kube(self.config) + ["create", "-f", "-", "-o", "json"],
                input_text=json.dumps(document),
                timeout=30,
            )
        except RuntimeError:
            # A timed-out create may still have happened, without a UID recorded for cleanup.
            entry["state"] = "uncertain"
            self.save()
            raise RuntimeError(f"Creating {resource}/{metadata['name']} timed out.") from None
        if result.returncode:
            entry["state"] = "not-created"
            self.save()
            raise RuntimeError(f"Creating {resource}/{metadata['name']} was rejected.")
        entry.update(uid=json.loads(result.stdout)["metadata"]["uid"], state="created")
        self.save()
        return entry["uid"]

    # Inputs: resource, name and namespace. Outputs: Ready object.
    # Purpose: wait for Strimzi to reconcile a fixture. Dependencies: kubectl GET.
    def wait_ready(self, resource, name, namespace):
        deadline = time.monotonic() + READY_WAIT_SECONDS
        while True:
            item = runtime._get(self.config, resource, name, namespace)
            if item and runtime._object_state("present", item, self.config["environment_id"]) == "ready":
                return item
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Fixture {resource}/{name} did not become Ready in time.")
            time.sleep(2)

    # Inputs: Pod manifest. Outputs: (Pod state, log text). Purpose: wait for the client Pod.
    # Dependencies: kubectl get/logs, gpu._pod_state.
    def wait_pod(self, document):
        from gpu import _pod_state

        metadata = document["metadata"]
        state = {"phase": "Unknown"}
        deadline = time.monotonic() + POD_WAIT_SECONDS
        while time.monotonic() < deadline:
            state = _pod_state(
                runtime._get(self.config, "pods", metadata["name"], metadata["namespace"])
            )
            if state["phase"] in {"Succeeded", "Failed", "Absent"}:
                break
            time.sleep(2)
        logs = run(
            runtime._kube(self.config)
            + ["logs", metadata["name"], "--namespace", metadata["namespace"], "--tail=100"],
            timeout=30,
        )
        return state, logs.stdout if logs.returncode == 0 else ""

    # Inputs: none. Outputs: none. Purpose: delete, newest first, only fixtures whose run label and
    # recorded UID prove this run created them. Dependencies: kubectl GET/delete.
    def cleanup(self):
        run_id = self.report["run_id"]
        for entry in reversed(self.report["fixtures"]):
            if entry["state"] == "not-created":
                entry["cleanup"] = "not-needed"
                continue
            try:
                live = runtime._get(self.config, entry["resource"], entry["name"], entry["namespace"])
            except RuntimeError:
                entry["cleanup"] = "read-failed"
                continue
            if live is None:
                entry["cleanup"] = "absent"
                continue
            metadata = live["metadata"]
            ours = (
                entry["uid"]
                and metadata.get("uid") == entry["uid"]
                and metadata.get("labels", {}).get(RUN_LABEL) == run_id
                and metadata.get("labels", {}).get(runtime.ENVIRONMENT_LABEL)
                == self.config["environment_id"]
            )
            if not ours:
                entry["cleanup"] = "skipped-not-owned"
                continue
            resource = entry["resource"].split(".", 1)[0]
            api = FIXTURE_API_PATHS[entry["resource"]]
            path = f"{api}/namespaces/{entry['namespace']}/{resource}/{entry['name']}"
            options = {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": entry["uid"]},
                "propagationPolicy": "Background",
            }
            try:
                # The server checks ownership even if the name is reused after our GET.
                result = run(
                    runtime._kube(self.config) + ["delete", "--raw", path, "-f", "-"],
                    input_text=json.dumps(options),
                    timeout=20,
                )
                entry["cleanup"] = "delete-failed"
                if result.returncode == 0:
                    state = self._wait_absent(entry)
                    entry["cleanup"] = "deleted" if state == "absent" else state
            except RuntimeError:
                entry["cleanup"] = "delete-failed"
            self.save()
        dependents = self.report.get("dependents", [])
        for dependent in dependents:
            dependent["cleanup"] = self._wait_absent(dependent)
        fixtures = self.report["fixtures"]
        group_verified = True
        if self.report["component"] == "kafka" and any(
            entry["resource"] == "pods" and entry["state"] != "not-created" for entry in fixtures
        ):
            group_cleanup = self.report.setdefault("pod_cleanup", {}).setdefault(
                "consumer_group", {"status": "fail", "reason": "no-result"}
            )
            group_verified = group_cleanup["status"] == "pass"
        done = {"deleted", "absent", "not-needed"}
        self.report["cleanup"] = (
            "complete"
            if group_verified and all(item.get("cleanup") in done for item in fixtures + dependents)
            else "incomplete"
        )
        self.save()

    # Inputs: fixture or dependent object reference. Outputs: cleanup state.
    # Purpose: confirm deletion has finished. Dependencies: kubectl GET.
    def _wait_absent(self, entry):
        deadline = time.monotonic() + CLEANUP_WAIT_SECONDS
        while True:
            try:
                live = runtime._get(
                    self.config, entry["resource"], entry["name"], entry["namespace"]
                )
            except RuntimeError:
                return "read-failed"
            if live is None:
                return "absent"
            if time.monotonic() >= deadline:
                return "still-present"
            time.sleep(2)


# Inputs: log text and component. Outputs: (checks, cleanup lines).
# Purpose: accept only fixed result tokens for the expected checks. Dependencies: RESULT_LINE.
def parse(log, component):
    expected = [name for group in GROUPS[component].values() for name in group]
    found, cleanup = {}, {}
    for line in log.splitlines():
        match = RESULT_LINE.match(line.strip())
        if not match:
            continue
        kind, name, result, reason = match.groups()
        target = found if kind == "ACCEPTANCE" else cleanup
        if kind == "CLEANUP" or name in expected:
            target.setdefault(name, {"status": result.lower(), "reason": reason})
    checks = [
        {"name": name, **found.get(name, {"status": "not-run", "reason": "no-result"})}
        for name in expected
    ]
    return checks, cleanup


# Inputs: configuration, source Secret, fields, reviewed version and ownership label/value.
# Outputs: copied base64 fields. Purpose: require ownership and the reviewed version of the same
# object that supplies the data. Dependencies: Kubernetes metadata.
def _owned_data(config, item, fields, expected_version, label=runtime.ENVIRONMENT_LABEL, value=None):
    value = config["environment_id"] if value is None else value
    metadata = (item or {}).get("metadata", {})
    if metadata.get("labels", {}).get(label) != value:
        raise RuntimeError("A source Secret for acceptance is missing or not owned.")
    actual_version = {"uid": metadata.get("uid"), "resource_version": metadata.get("resourceVersion")}
    if actual_version != expected_version:
        source = f"{metadata['namespace']}/{metadata['name']}"
        raise RuntimeError(f"Source Secret {source} changed after review; prepare a new plan.")
    return {field: item["data"][field] for field in fields}


# Inputs: configuration, Run and reviewed Pod. Outputs: Pod state and log. Purpose: run PostgreSQL checks.
# Dependencies: application client Secret, OpenSSL, kubectl.
def _run_postgres(config, execution, pod):
    run_id = execution.report["run_id"]
    derived = config["derived"]
    source = runtime._get(
        config, "secrets", f"{derived['postgres_name']}-client", config["application"]["namespace"]
    )
    data = _owned_data(
        config, source, ("username", "password", "ca.crt"), execution.report["target"]["client_secret"]
    )
    data["wrong-ca.crt"] = base64.b64encode(wrong_ca().encode()).decode()
    execution.create(fixture_secret(config, "postgres", run_id, data), "secrets")
    execution.create(pod, "pods")
    return execution.wait_pod(pod)


# Inputs: configuration, Run and reviewed Pod. Outputs: Pod state and log. Purpose: run Kafka checks.
# Dependencies: Strimzi fixtures, application client Secrets, kubectl.
def _run_kafka(config, execution, pod):
    run_id, target = execution.report["run_id"], execution.report["target"]
    n = names(config, "kafka", run_id)
    namespace, name = n["kafka_namespace"], config["derived"]["kafka_name"]
    topic, user = kafka_fixtures(config, run_id)
    execution.create(topic, TOPICS)
    user_uid = execution.create(user, USERS)
    execution.report["dependents"] = [
        {"resource": "secrets", "namespace": namespace, "name": n["user"], "owner": "KafkaUser"}
    ]
    execution.wait_ready(TOPICS, n["topic_resource"], namespace)
    execution.wait_ready(USERS, n["user"], namespace)
    issued = runtime._get(config, "secrets", n["user"], namespace)
    owners = {item.get("uid") for item in (issued or {}).get("metadata", {}).get("ownerReferences", [])}
    if user_uid not in owners:
        raise RuntimeError("The fixture user's Secret is not owned by the fixture KafkaUser.")
    ca = runtime._get(config, "secrets", f"{name}-cluster-ca-cert", namespace)
    data = {
        "ca.crt": _owned_data(
            config, ca, ("ca.crt",), target["cluster_ca"], "strimzi.io/cluster", name
        )["ca.crt"],
        "fixture.crt": issued["data"]["user.crt"],
        "fixture.key": issued["data"]["user.key"],
    }
    for role in ("producer", "consumer"):
        client = runtime._get(
            config, "secrets", f"{name}-{role}-client", config["application"]["namespace"]
        )
        fields = _owned_data(config, client, ("user.crt", "user.key"), target[f"{role}_client_secret"])
        data[f"{role}.crt"], data[f"{role}.key"] = fields["user.crt"], fields["user.key"]
    execution.create(fixture_secret(config, "kafka", run_id, data), "secrets")
    execution.create(pod, "pods")
    return execution.wait_pod(pod)


# Inputs: review without plan_id. Outputs: plan ID. Purpose: bind authorization to exact content.
# Dependencies: runtime._digest.
def _plan_id(review):
    return runtime._digest({key: value for key, value in review.items() if key != "plan_id"})


# Inputs: configuration, workspace, component, optional plan ID and execution flag.
# Outputs: plan, preview or result report. Purpose: preview, review and run one isolated
# acceptance. Dependencies: preflight, fixture runners, private plan files.
def acceptance(config, workspace, component, plan_id=None, execute=False):
    directory = private_dir(private_dir(workspace / "plans") / "acceptance")
    source = sha256(Path(__file__))
    build_pod = postgres_pod if component == "postgres" else kafka_pod
    if plan_id is None:
        identity, target = preflight(config, workspace, component)
        run_id = secrets.token_hex(3)
        pod = build_pod(config, run_id)
        review = {
            "environment_id": config["environment_id"],
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "config_sha256": runtime._digest(config),
            "source_sha256": source,
            "identity": {key: identity[key] for key in ("context", "kubeconfig", "server", "kube_system_uid")},
            "target": target,
            "client_image": pod["spec"]["containers"][0]["image"],
            **summary(config, component, run_id),
        }
        review["plan_id"] = _plan_id(review)
        write_json(directory / f"{review['plan_id']}.json", review)
        return {"status": "planned", "requires_execute": True, **review}
    path, consumed = directory / f"{plan_id}.json", directory / f"consumed-{plan_id}.json"
    if consumed.exists() or not path.is_file() or path.is_symlink():
        raise RuntimeError("The acceptance plan is missing, unsafe or already used; prepare a new one.")
    review = read_json(path)
    if (
        review.get("plan_id") != plan_id
        or _plan_id(review) != plan_id
        or review.get("component") != component
        or review.get("config_sha256") != runtime._digest(config)
        or review.get("source_sha256") != source
    ):
        raise RuntimeError("The acceptance plan no longer matches its inputs; prepare a new one.")
    run_id = review["run_id"]
    pod = build_pod(config, run_id)
    if pod["spec"]["containers"][0]["image"] != review.get("client_image"):
        raise RuntimeError("The acceptance client image changed after review; prepare a new plan.")
    if not execute:
        return {"status": "preview", "requires_execute": True, **review}
    identity, target = preflight(config, workspace, component)
    if {key: identity[key] for key in review["identity"]} != review["identity"]:
        raise RuntimeError("Target identity changed after review; acceptance is blocked.")
    if target != review["target"]:
        raise RuntimeError("The service or its credentials changed after review; prepare a new plan.")
    # Consume before the first write so an interruption never leaves a reusable plan.
    write_json(consumed, {"plan_id": plan_id, "status": "attempting"})
    report = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "component": component,
        "plan_id": plan_id,
        "run_id": run_id,
        "kube_system_uid": identity["kube_system_uid"],
        "target": target,
        "client_image": review["client_image"],
        "status": "running",
        "fixtures": [],
        "checks": [],
    }
    execution = Run(config, workspace / "reports" / f"acceptance-{component}.json", report)
    execution.save()
    try:
        runner = _run_postgres if component == "postgres" else _run_kafka
        state, log = runner(config, execution, pod)
        report["pod"] = state
        report["checks"], report["pod_cleanup"] = parse(log, component)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as error:
        report["error"] = str(error) if isinstance(error, RuntimeError) else "Unexpected response."
    finally:
        execution.cleanup()
        statuses = {item["status"] for item in report["checks"]}
        report["result"] = (
            "fail" if "fail" in statuses else "pass" if statuses == {"pass"} else "incomplete"
        )
        report["status"] = (
            "pass" if report["result"] == "pass" and report["cleanup"] == "complete" else "fail"
        )
        execution.save()
        write_json(consumed, {"plan_id": plan_id, "status": report["status"]})
    return report


# Inputs: configuration, workspace, current identity and component. Outputs: verify entries.
# Purpose: count a result only for this cluster and the reviewed target versions, per group.
# Dependencies: acceptance report, target_state, runtime._acceptance.
def evidence(config, workspace, identity, component):
    groups = GROUPS[component]
    path = workspace / "reports" / f"acceptance-{component}.json"
    hint = f"Run acceptance --component {component}, review the plan, then execute it."
    if not path.exists():
        return [runtime._acceptance(group, "pending", hint) for group in groups]
    report = read_json(path)
    reason = None
    if report.get("kube_system_uid") != identity["kube_system_uid"]:
        reason = "The recorded result belongs to another cluster. " + hint
    else:
        try:
            if report.get("target") != target_state(config, component):
                reason = "The service or its credentials changed since the recorded run. " + hint
        except runtime.ReadError as error:
            reason = f"Current target could not be read ({error.reason})."
    if reason:
        return [runtime._acceptance(group, "pending", reason) for group in groups]
    results = {item["name"]: item for item in report.get("checks", [])}
    entries = []
    for group, members in groups.items():
        found = [results.get(name, {"status": "not-run"}) for name in members]
        detail = {
            "captured_at": report.get("captured_at"),
            "run_id": report.get("run_id"),
            "checks": {name: item.get("reason", item["status"]) for name, item in zip(members, found)},
        }
        status = (
            "fail"
            if any(item["status"] == "fail" for item in found)
            else "pass"
            if all(item["status"] == "pass" for item in found)
            else "pending"
        )
        entries.append(runtime._acceptance(group, status, detail))
    return entries
