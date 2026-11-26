<!-- Purpose: Explain the versioned environment configuration, supported combinations, and derived application contract. -->

# Environment configuration

`scripts/config.py` reads the JSON configuration, checks it, and fills defaults. `scripts/platform.py` then prepares the workspace and dispatches the action. The `configure` action does not contact Kubernetes. Start from [environment.example.json](../assets/environment.example.json). Its `$comment` explains the file's purpose.

The configuration path is expanded and resolved before reading. Relative kubeconfig paths use the resolved configuration file's directory. A configuration symlink can therefore be accepted. Direct artifact reads have a separate [no-follow rule](io.md#json-reads-and-hashes).

## Input contract

| Field | Meaning |
|---|---|
| `schema_version` | Configuration format version; this release uses `1`. |
| `environment_id` | Lowercase DNS-label name, up to 24 characters. It may start with a digit. Managed resource names use it as a prefix. |
| `cluster.type` / `cluster.mode` | `minikube` / `reuse` for an existing cluster, or `create` for a new profile made by the reviewed `cluster` action. |
| `cluster.name` | New Minikube profile name; required with `create` and rejected with `reuse`. Lowercase DNS label, up to 40 characters. |
| `cluster.context` | Kubeconfig context used by every Kubernetes request and Terraform provider. With `create`, omit it or set it to `cluster.name`. |
| `cluster.kubeconfig` | Kubeconfig path. Relative paths resolve from the configuration file's directory. A new cluster writes its context here. |
| `cluster.create.driver` | Required with `create`: `docker`, `podman`, `krunkit`, `vfkit`, `qemu2`, or `kvm2`. |
| `cluster.create.nodes` / `cpus` / `memory_mib` / `disk_mib` | Node count (1–5, default 3) and per-node CPUs (default 2), memory (default and minimum 4096 MiB; VM drivers keep the node root filesystem in RAM), and disk (default 20000 MiB). |
| `cluster.create.kubernetes_version` | `v1.35.x` through `v1.37.x`; default `v1.37.0`, matching the preflight profile. |
| `cluster.create.container_runtime` | `containerd` (default), `cri-o`, or `docker`. |
| `application.namespace` | Dedicated application namespace. Defaults to an environment-based name. System namespaces are rejected. |
| `application.service_account` | Application identity without general Kubernetes administration permissions. |
| `application.database` / `application.username` | PostgreSQL database and application login. The entry point checks these names before SQL or shell code uses them. |
| `postgres.enabled` | Select the single-instance PostgreSQL StatefulSet stage. |
| `postgres.storage` / `postgres.resources` | Persistent capacity and container CPU/memory `requests` and `limits`. |
| `kafka.enabled` | Select the single-node Strimzi KRaft stage. |
| `kafka.namespace` | Namespace where the reused or newly installed Strimzi Operator runs and watches Kafka. |
| `kafka.storage` / `kafka.resources` | Broker persistent capacity and CPU/memory resources. |
| `kafka.topics` / `kafka.consumer_group` | Application topics and consumer group. ACLs grant access to these names. |
| `kafka.heap_mib` | JVM heap in MiB. Defaults to `256`. It must leave space within the enabled broker's memory request. |
| `kafka.partitions` | Partitions per new Topic. Defaults to `1`. Existing partitions cannot be reduced by this workflow. |
| `kafka.retention_hours` / `kafka.retention_mib` | New message retention targets. Defaults: six hours and 64Mi per partition. |
| `kafka.segment_mib` | Log segment target. Defaults to `16`. It cannot exceed the retention-size target. |
| `monitoring.enabled` | Select monitoring prerequisites, monitors, and alert rules for enabled services. |
| `monitoring.namespace` / `monitoring.release` | Monitoring namespace and release selector for reused or newly prepared resources. |
| `monitoring.pod_labels` | Actual Prometheus Pod labels used by network policy. |
| `storage.mode` | `reuse` a reviewed class, `csi` to create a class using the existing Minikube CSI driver, or `create` a separate Local Path provisioner. |
| `storage.class` | Explicit class name, required for reuse. There is no default-class fallback. |
| `storage.nodes` | Selected volume nodes. Required for `csi` and `create`. The same names constrain PostgreSQL and Kafka Pod placement. |
| `gpu.enabled` / `gpu.nodes` / `gpu.slots` | Select the GPU device plugin stage, the nodes that expose the krunkit render device, and allocation slots per node (1–16, default 1). See [GPU device plugin](gpu.md). |
| `network.dns_namespace` / `network.dns_labels` | Actual DNS Pod location and selector used by egress policy. |
| `network.api_server_cidrs` | Reviewed IPv4 `/32` addresses for the API Service and endpoints. Used for operator egress. |
| `budget.max_memory_requests_mib` | Maximum service and managed prerequisite memory requests, excluding externally reused services. |
| `budget.min_disk_free_mib` | Required free disk headroom used by preflight. |

Most fields have defaults. Review the generated configuration before planning. The entry point rejects unknown keys, unsupported combinations, invalid quantities, and conflicting names.

`$comment` is optional and has no effect on execution. If input contains `derived`, the entry point ignores it and recomputes those values. This lets it read a generated workspace configuration again.

Resource defaults and minimum supported memory settings are:

| Component container | Default CPU request / limit | Default memory request / limit | Minimum memory request / limit |
|---|---|---|---|
| PostgreSQL | `250m` / `1000m` | `256Mi` / `512Mi` | `256Mi` / `512Mi` |
| Kafka broker/controller | `300m` / `1500m` | `512Mi` / `768Mi` | `512Mi` / `768Mi` |

Requests must not exceed limits. Memory minimums apply to enabled components. They do not prove that the cluster has enough capacity.

Memory and storage accept positive whole numbers with `Mi` or `Gi`, such as `384Mi` or `4Gi`. CPU accepts positive whole cores or millicores, such as `1` or `250m`.

The memory budget defaults to `2048` MiB. Disk headroom defaults to `512` MiB per used node. Each service's PVC defaults to `1Gi`. Service estimates include their helper containers. With monitoring enabled, PostgreSQL and its exporter request 320Mi in one Pod. Kafka requests 512Mi for its broker and 256Mi for its separate Entity Operator Pod. Their total is 1088Mi. With all new prerequisites, the total is 1844Mi. See [prerequisite capacity](prerequisites.md#operators-and-namespaces).

The `csi` mode creates only a non-default StorageClass. It requires the existing `hostpath.csi.k8s.io` driver and its node topology registrations. The class uses `WaitForFirstConsumer`, `Retain`, and the selected `topology.hostpath.csi/node` values. PostgreSQL and Kafka use matching hostname affinity. CSI installation and replacement are outside this mode.

### New cluster rules

With `cluster.mode=create`, node names follow Minikube's convention: `NAME`, `NAME-m02`, `NAME-m03`, and so on. `storage.nodes` must be among them, which catches typos before any tool runs. `storage.mode=csi` adds the `csi-hostpath-driver` and `volumesnapshots` addons. The CNI is always Calico. See [assets/environment.create.example.json](../assets/environment.create.example.json).

`network.api_server_cidrs` cannot be known before the cluster exists. The `cluster` result suggests them. Review and add them before enabling Kafka prerequisites or planning.

Keep passwords, private keys, connection strings with credentials, and shell commands out of this file. Packaged assets pin component versions. The configuration does not accept arbitrary version overrides.

## Supported combinations

Enable PostgreSQL, Kafka, the GPU device plugin, or any combination. Monitoring covers the data services you enable. A GPU-only environment must disable monitoring and keep `storage.mode=reuse`; it needs no StorageClass. A disabled component adds no stage, credential requirement, or required runtime check.

| Selection | Prerequisites and behavior |
|---|---|
| Cluster/network | Existing Minikube with Calico, or a new profile created with `--cni=calico`. The DNS selector must match Ready DNS Pods. |
| PostgreSQL | StatefulSet, TLS, dedicated PVC, application login, and separate monitoring login. No CNPG dependency. |
| Kafka | Reuse or prepare Strimzi 1.3.0 in the configured namespace. One broker/controller, RF1 and minISR1. |
| Monitoring | Reuse a compatible installation or prepare an Operator and minimal Prometheus. Match monitor selectors and Pod labels. |
| Reused storage | Reviewed `WaitForFirstConsumer` / `Retain` class and enough measured disk space. |
| Created storage | Separate Local Path provisioner on selected nodes, with its own non-default class. Existing classes and volumes remain unchanged. |
| Existing CSI | A new class using the current Minikube CSI Hostpath driver. No extra storage controller is installed. |
| GPU | A privileged plugin DaemonSet in its own namespace on the selected nodes. New clusters require the `krunkit` driver. |

This version does not start, restart, or resize clusters. It recreates or deletes only a profile this workspace created, through a separately reviewed `cluster --overwrite` or `delete` plan. It does not support HTTPS ingress, CNPG, replication, failover, object storage, WAL/PITR, operator upgrades, or application releases. Adding a field to the JSON cannot enable them.

## Derived resources and stages

| Resource | Derived name |
|---|---|
| Data namespace | `<environment_id>-data` |
| Validation namespace | `<environment_id>-validation` |
| PostgreSQL | `<environment_id>-postgres` |
| Kafka | `<environment_id>-kafka` |
| Default application namespace | `<environment_id>-app` |
| GPU namespace / plugin | `<environment_id>-gpu` / `<environment_id>-gpu-device-plugin` |

Managed resources carry two labels:

- `app.kubernetes.io/managed-by=local-k8s-platform`
- `local-k8s-platform/environment=<environment_id>`

Use one managed platform environment per cluster. Another workspace must not claim the same resources through a second Terraform state.

Default SQL names replace hyphens in `environment_id` with underscores. If the result starts with a digit, the entry point adds `app_`. For example, `1-demo` becomes `app_1_demo`. Custom database and login names must still pass the SQL name checks.

The selected components determine the stages:

1. `00-bootstrap`: dedicated namespaces, identities, and network isolation.
2. `05-storage-csi` for `storage.mode=csi`, or `05-storage` for `storage.mode=create`.
3. `20-gpu`: only for enabled GPU.
4. `30-postgres`: only for enabled PostgreSQL.
5. `31-kafka`: only for enabled Kafka.
6. `40-monitoring`: only for enabled monitoring.

The workspace receives `environment.json` with resolved values. Each stage receives `environment.auto.tfvars.json`. To change settings, edit the source configuration and rerun the entry point. Do not edit generated stage variables.

## Application handoff

The platform creates database accounts, Kafka topics and ACLs, storage, and infrastructure monitors. Applications own images, releases, schema migrations, runtime processes, and message formats.

PostgreSQL provides a stable Service. Clients use `sslmode=verify-full`. Kafka uses internal TLS, client certificates, and named topics and consumer groups. One Kafka node cannot replicate data across brokers. One PostgreSQL StatefulSet does not provide database failover.

With GPU enabled, the handoff adds the resource name `devic.es/dri`, the selected nodes, and slots per node. Containers request it with `resources.requests` and `resources.limits` of `devic.es/dri: 1`.

The handoff includes endpoints, account names, public CA references, application-local Secrets, and required network-policy labels:

| Client Secret | Keys |
|---|---|
| `<postgres_name>-client` | `username`, `password`, `ca.crt` |
| `<kafka_name>-producer-client` | `user.crt`, `user.key`, `ca.crt` |
| `<kafka_name>-consumer-client` | `user.crt`, `user.key`, `ca.crt` |

Kubernetes Secrets belong to one namespace. The credential helper copies approved client fields into the application namespace. Applications cannot directly mount Secrets from the data or Kafka namespace.

These copies do not update automatically. Application releases must mount them, and clients must reload them after a rotation. Test real connections before reporting readiness.

Generated interfaces keep `status=planned-not-connectivity-verified`. They describe the expected connection settings. They do not prove that the application can connect.
