<!-- Purpose: Explain the versioned environment configuration, supported combinations, and derived application contract. -->

# Environment configuration

`scripts/config.py` reads the JSON configuration, checks it, and fills defaults. `scripts/platform.py` then prepares the workspace and dispatches the action. The `configure` action does not contact Kubernetes. Start from [environment.example.json](../assets/environment.example.json). Its `$comment` explains the file's purpose.

The configuration path is expanded and resolved before reading. Relative kubeconfig paths use the resolved configuration file's directory. A configuration symlink can therefore be accepted. Direct artifact reads have a separate [no-follow rule](io.md#json-reads-and-hashes).

## Input contract

| Field | Meaning |
|---|---|
| `schema_version` | Configuration format version; this release uses `1`. |
| `environment_id` | Lowercase DNS-label name, up to 24 characters. It may start with a digit. Managed resource names use it as a prefix. |
| `cluster.type` / `cluster.mode` | `minikube` / `reuse`; the cluster already exists. |
| `cluster.context` | Kubeconfig context used by every Kubernetes request and Terraform provider. |
| `cluster.kubeconfig` | Kubeconfig path. Relative paths resolve from the configuration file's directory. |
| `application.namespace` | Dedicated application namespace. Defaults to an environment-based name. System namespaces are rejected. |
| `application.service_account` | Application identity without general Kubernetes administration permissions. |
| `application.database` / `application.username` | PostgreSQL database and application login. The entry point checks these names before SQL or shell code uses them. |
| `postgres.enabled` | Select the single-instance PostgreSQL StatefulSet stage. |
| `postgres.storage` / `postgres.resources` | Persistent capacity and container CPU/memory `requests` and `limits`. |
| `kafka.enabled` | Select the single-node Strimzi KRaft stage. |
| `kafka.namespace` | Existing namespace watched by the reused Strimzi operator. |
| `kafka.storage` / `kafka.resources` | Broker persistent capacity and CPU/memory resources. |
| `kafka.topics` / `kafka.consumer_group` | Application topics and consumer group. ACLs grant access to these names. |
| `monitoring.enabled` | Add monitors and alert rules for enabled services to existing Prometheus. |
| `monitoring.namespace` / `monitoring.release` | Existing monitoring namespace and release selector. |
| `monitoring.pod_labels` | Actual Prometheus Pod labels used by network policy. |
| `storage.mode` | `reuse` an audited class, or `create` a separate local provisioner and class. |
| `storage.class` | Explicit class name, required for reuse. There is no default-class fallback. |
| `storage.nodes` | Eligible existing nodes for a newly created local provisioner. |
| `network.dns_namespace` / `network.dns_labels` | Actual DNS Pod location and selector used by egress policy. |
| `network.api_server_cidrs` | Reviewed IPv4 `/32` addresses for the API Service and endpoints. Used for operator egress. |
| `budget.max_memory_requests_mib` | Maximum new configured memory requests, excluding reused platform services. |
| `budget.min_disk_free_mib` | Required free disk headroom used by preflight. |

Most fields have defaults. Review the generated configuration before planning. The entry point rejects unknown keys, unsupported combinations, invalid quantities, and conflicting names.

`$comment` is optional and has no effect on execution. If input contains `derived`, the entry point ignores it and recomputes those values. This lets it read a generated workspace configuration again.

Resource defaults and minimum supported memory settings are:

| Component container | Default CPU request / limit | Default memory request / limit | Minimum memory request / limit |
|---|---|---|---|
| PostgreSQL | `250m` / `1000m` | `384Mi` / `768Mi` | `256Mi` / `512Mi` |
| Kafka broker/controller | `300m` / `1500m` | `768Mi` / `1280Mi` | `512Mi` / `768Mi` |

Requests must not exceed limits. Memory minimums apply to enabled components. They do not prove that the cluster has enough capacity.

Memory and storage accept positive whole numbers with `Mi` or `Gi`, such as `384Mi` or `4Gi`. CPU accepts positive whole cores or millicores, such as `1` or `250m`.

Both budget fields default to `2048` MiB. Memory estimates also include the enabled helper containers and operators.

Keep passwords, private keys, connection strings with credentials, and shell commands out of this file. Packaged assets pin component versions. The configuration does not accept arbitrary version overrides.

## Supported combinations

Enable PostgreSQL, Kafka, or both. Monitoring covers the services you enable. A disabled component adds no stage, credential requirement, or required runtime check.

| Selection | Prerequisites and behavior |
|---|---|
| Cluster/network | Existing Minikube with Calico. The DNS selector must match Ready DNS Pods. |
| PostgreSQL | StatefulSet, TLS, dedicated PVC, application login, and separate monitoring login. No CNPG dependency. |
| Kafka | Existing compatible Strimzi operator and CRDs in a watched namespace. One broker/controller, RF1 and minISR1. |
| Monitoring | Existing Prometheus Operator CRDs and matching selectors. No second monitoring stack is installed. |
| Reused storage | Reviewed `WaitForFirstConsumer` / `Retain` class and enough measured disk space. |
| Created storage | Separate Local Path provisioner on selected nodes, with its own non-default class. Existing classes and volumes remain unchanged. |

This version does not support HTTPS ingress, cluster creation, CNPG, replication, failover, object storage, WAL/PITR, operator upgrades, or application releases. Adding a field to the JSON cannot enable them.

## Derived resources and stages

| Resource | Derived name |
|---|---|
| Data namespace | `<environment_id>-data` |
| Validation namespace | `<environment_id>-validation` |
| PostgreSQL | `<environment_id>-postgres` |
| Kafka | `<environment_id>-kafka` |
| Default application namespace | `<environment_id>-app` |

Managed resources carry two labels:

- `app.kubernetes.io/managed-by=local-k8s-platform`
- `local-k8s-platform/environment=<environment_id>`

Use one managed platform environment per cluster. Another workspace must not claim the same resources through a second Terraform state.

Default SQL names replace hyphens in `environment_id` with underscores. If the result starts with a digit, the entry point adds `app_`. For example, `1-demo` becomes `app_1_demo`. Custom database and login names must still pass the SQL name checks.

The selected components determine the stages:

1. `00-bootstrap`: dedicated namespaces, identities, and network isolation.
2. `05-storage`: only for `storage.mode=create`.
3. `30-postgres`: only for enabled PostgreSQL.
4. `31-kafka`: only for enabled Kafka.
5. `40-monitoring`: only for enabled monitoring.

The workspace receives `environment.json` with resolved values. Each stage receives `environment.auto.tfvars.json`. To change settings, edit the source configuration and rerun the entry point. Do not edit generated stage variables.

## Application handoff

The platform creates database accounts, Kafka topics and ACLs, storage, and infrastructure monitors. Applications own images, releases, schema migrations, runtime processes, and message formats.

PostgreSQL provides a stable Service. Clients use `sslmode=verify-full`. Kafka uses internal TLS, client certificates, and named topics and consumer groups. One Kafka node cannot replicate data across brokers. One PostgreSQL StatefulSet does not provide database failover.

The handoff includes endpoints, account names, public CA references, application-local Secrets, and required network-policy labels:

| Client Secret | Keys |
|---|---|
| `<postgres_name>-client` | `username`, `password`, `ca.crt` |
| `<kafka_name>-producer-client` | `user.crt`, `user.key`, `ca.crt` |
| `<kafka_name>-consumer-client` | `user.crt`, `user.key`, `ca.crt` |

Kubernetes Secrets belong to one namespace. The credential helper copies approved client fields into the application namespace. Applications cannot directly mount Secrets from the data or Kafka namespace.

These copies do not update automatically. Application releases must mount them, and clients must reload them after a rotation. Test real connections before reporting readiness.

Generated interfaces keep `status=planned-not-connectivity-verified`. They describe the expected connection settings. They do not prove that the application can connect.
