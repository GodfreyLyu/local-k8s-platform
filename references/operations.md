<!-- Purpose: Describe command effects, staged deployment, credential handling, and evidence-based verification. -->

# Platform operations

Run commands from the skill's root. Supply a configuration and a private workspace outside the repository. Use one workspace for the managed environment in a cluster. Keep packaged scripts and assets unchanged.

The helpers need Python 3.10 or later on macOS or Linux. They use only the Python standard library. Cluster operations also need Terraform `>=1.10,<2`, kubectl, and a Minikube kubeconfig context. A new cluster also needs minikube and a working driver. Pinned providers and images must be available. Use the [prerequisite workflow](prerequisites.md) to prepare missing Operators, minimal monitoring, or local OpenSSL.

## Command effects

Every `platform.py` action loads the configuration and locks the workspace. It runs `configure` before the selected action. This refreshes stage copies, variable files, `environment.json`, and interface records. A preview can therefore update local files without changing Kubernetes.

| Command | Local effects | Cluster effects |
|---|---|---|
| `configure` | Fill in defaults and copy selected stage files into the workspace. | None; offline. |
| `cluster` | Check profile, machine, and kubeconfig names. Save a review and plan ID. | None. |
| `cluster --plan-id ID` | Preview a saved cluster plan. | None. |
| `cluster --plan-id ID --execute` | Save the verified cluster binding, creation result, and suggested API addresses. | Run the reviewed `minikube start` once for the new profile. |
| `cluster --overwrite` | Compare the created profile with the workspace records; save a review and plan ID. | Read-only Minikube and API checks. |
| `cluster --overwrite --plan-id ID --execute` | Archive the previous workspace files and record the replacement. | `minikube delete` (only for the reviewed `overwrite` action), then `minikube start`. |
| `delete --workspace-files MODE` | Save a review with the data-loss inventory and plan ID. | Read-only Minikube and API inventory. |
| `delete ... --plan-id ID --execute` | Keep, archive, or purge the workspace files as reviewed. | `minikube delete` for the reviewed profile. |
| `prerequisites` | Save a review and plan ID for selected dependencies. | Read inventory and create permissions. No writes. |
| `prerequisites --plan-id ID` | Preview a saved dependency plan. | None. |
| `prerequisites --plan-id ID --execute` | Install reviewed local packages and record partial results. | Create missing reviewed namespaces, CRDs, RBAC, and workloads. |
| `inspect` | Save structured inventory and blockers. | Read-only API requests. |
| `plan --stage NAME` | Initialize/check one stage; save plan and review manifest. | Read current objects; no apply. |
| `apply --stage NAME --plan-id ID` | Check a saved plan and show execution preview. | No mutation without `--execute`. |
| `apply ... --execute` | Record stage result. | Apply the matching reviewed plan. |
| `credentials` | Show PostgreSQL references and mappings. | None. |
| `credentials --prepare` | Create or reuse protected local PostgreSQL material. | None. |
| `credentials --execute` | Use the prepared bundle; record results. | Write fixed source Secrets and the application client Secret. |
| `credentials --component kafka` | Show Kafka credential mappings. | None. |
| `credentials --component kafka --execute` | Record handoff results. | Read selected Strimzi client fields and write application client Secrets. |
| `credentials --expiry` | Record `reports/credentials-expiry.json`. | Read-only: public certificate fields and application Pods. |
| `backup --backup-dir DIR [--with-fixture]` | Save a backup plan and plan ID. | Read-only checks and the database size. |
| `backup ... --plan-id ID --execute` | Write `dump.sql` and `manifest.json` under `DIR`; record `reports/backup.json`. | `pg_dump` through `kubectl exec`; with `--with-fixture`, insert and then remove one fixture row. |
| `restore --backup PATH [--target-database NAME] [--keep-target]` | Verify the backup offline; save a restore plan and plan ID. | Read-only backup, PostgreSQL identity, and local Docker engine checks. |
| `restore ... --plan-id ID --execute` | Record `reports/restore.json`. | Create a local isolated Docker container, load and compare the dump, then remove the container unless `--keep-target`. |
| `verify` | Save baseline results, functional acceptance results, and pending items. | Read-only checks for enabled components, including Prometheus queries through the API server pod proxy. |
| `acceptance --component C` | Save an acceptance plan with its target, fixtures, effects, cleanup scope, and plan ID. | Read-only checks of identity, readiness, source Secrets, and leftover fixtures. |
| `acceptance --component C --plan-id ID` | Show the saved plan. | None. |
| `acceptance --component C --plan-id ID --execute` | Record `reports/acceptance-C.json`. | Create the reviewed fixtures, run one client Pod, then delete the fixtures this run created. |
| `gpu-smoke` | Show the smoke-test Pod and its effects. | None. |
| `gpu-smoke --execute` | Record `reports/gpu-smoke.json`. | Create one test Pod in the validation namespace, read its log, and delete it. |

The CLI has no destroy, import, restart, or general cleanup command. Without `--overwrite`, `cluster` only creates a profile that does not exist yet. `cluster --overwrite` and `delete` act only on a profile this workspace created, each through its own reviewed plan; see [Create a Minikube cluster](#create-a-minikube-cluster) and [Delete a created cluster](#delete-a-created-cluster). Acceptance and the GPU smoke test remove only the test objects they created. Configuration and preview commands do not authorize later writes. Building this project does not install the skill globally.

To build a portable archive, choose an output path outside the maintained source directories:

```sh
python3 -B scripts/package.py --output /private/path/local-k8s-platform.tar.gz
```

The archive contains the maintained skill files, tests, and a checksum manifest. Identical source produces identical archive bytes. Private workspaces and runtime files are excluded. Packaging does not install or deploy anything.

## Create a Minikube cluster

Use this only with `cluster.mode=create`. The configuration names the new profile in `cluster.name`; the kubeconfig context gets the same name. Reuse mode rejects the `cluster` action.

1. Run `cluster`. It records the Minikube executable and version, the host platform, and collisions with the planned profile, machines, and kubeconfig entries.
2. Review the exact `minikube start` command: profile, driver, node count, CPUs, memory, disk, Kubernetes version, container runtime, `--cni=calico`, and CSI addons when `storage.mode=csi`. `KUBECONFIG` is set to the configured file. `--keep-context=true` leaves the current context unchanged.
3. After authorization of that plan, run `cluster --plan-id ID --execute`. Execution checks again that nothing changed since review, consumes the plan, and starts the cluster once, with a 30-minute timeout.
4. Copy the reviewed `suggested_api_server_cidrs` from `reports/cluster.json` into `network.api_server_cidrs` when Kafka is enabled. Then continue with `prerequisites` and `inspect`.

After a successful start, the CLI records the API endpoint and the `kube-system` namespace UID in `cluster-binding.json` before marking `reports/cluster.json` as `created`. Later cluster operations require the target to match the creation identity and the workspace binding. A legacy creation report with a complete identity can establish a missing binding after the live identity matches it. Offline actions still work.

An existing profile with this name blocks, whether it is running, stopped, or invalid. A machine record for any planned node or a matching context, cluster, or user entry in the selected kubeconfig also blocks. To use an existing profile, start it outside this workflow and switch to `cluster.mode=reuse`.

With the krunkit driver, each machine directory holds `krunkit.sock` and `vmnet-helper.sock`. The plan checks the longest machine socket and the client socket against the Unix socket limit of 103 bytes on macOS and 107 on Linux. Before libkrun 1.19 the client path adds `-krun.sock` to the helper socket. Starting with libkrun 1.19 the client path is under `TMPDIR`, which the reviewed start command sets explicitly. If the linked version cannot be identified, the plan checks both layouts. Socket lengths use the paths passed to the process, including any home alias that Minikube receives. Execution rechecks this evidence before any delete or start.

A failed start records `failed`. A timeout, an interruption before the cluster identity is verified and bound, or an identity verification or binding failure records `uncertain`. These outcomes preserve the profile and block later stages. Ctrl-C sets `interrupted: true` and the CLI exits with status 130. The report and consumed plan record `created` as soon as the identity is verified and bound. If Ctrl-C interrupts the optional API server CIDR discovery, the reports still record `created` and the suggested CIDRs may be unavailable.

A failed, timed-out, or interrupted Minikube start records its outcome before collecting a `failure` diagnostic. The diagnostic includes the stage, the first recognized error, the Minikube exit reason, and the paths of available logs. These include `logs/lastStart.txt` and, for krunkit, each machine's `vmnet-helper.log`, `krunkit.log`, and `serial.log`. An `ERROR` line in the vmnet helper log identifies a network-stage failure. Without a vmnet interface, the VM has no address and later SSH attempts to `:22` can fail. `vmnet_start_interface: VMNET_FAILURE` from an unprivileged helper usually means the calling process cannot reach the macOS vmnet service, for example inside a sandboxed agent shell. Run the start from an unrestricted shell.

An interrupted start can leave krunkit or vmnet-helper processes running. Inspect the profile with `minikube status -p NAME` and `minikube logs -p NAME` before deciding on recovery. A consumed plan cannot run again.

### Overwrite a created cluster

Without `--overwrite`, `cluster` on a workspace with a `created` report returns that report. Overwrite also requires the resolved Minikube home recorded at creation. Legacy reports without a home still support ordinary platform operations. Both the deletion and the replacement command use the reviewed home.

The overwrite plan compares the live cluster with this workspace. The result goes to `reports/cluster-reconcile.json`. If the profile matches, the plan returns `unchanged`. Startup checks apply when the comparison calls for a replacement or the profile is missing.

1. **Identity.** Minikube SSH reads the `kube-system` UID from the running local profile's control plane. The configured context must return that same UID. Only then can a difference from the stored creation identity count as drift. A verified identity difference skips object checks.
2. **Terraform state.** Every managed resource in `state/*.tfstate` must exist. It must keep its recorded UID and carry this environment's ownership label. `kubernetes_manifest` entries use the UID from the stored object.
3. **Other recorded objects.** Successfully created prerequisites and published Secrets must exist. The workspace keeps their references in `state/recorded-objects.json` across previews and retries. Earlier reports and consumed prerequisite plans also supply references. Secret reads request `PartialObjectMetadata` from the API through a temporary private Unix socket.

For resources backed by CRDs, the comparison verifies the CRD before reading the instance. A confirmed missing CRD counts as absence. Discovery and permission errors block the plan. Review evidence includes the observed UID and environment ownership label of each object.

Results map to plan actions:

| Live evidence | Action | Execution |
|---|---|---|
| No profile with this name | `recreate-missing` | Archive the workspace, then start once. |
| Readable profile, all checks match | `unchanged` | Nothing to execute. |
| Readable profile, identity or any object differs | `overwrite` | `minikube delete --profile=NAME`, then archive and start once. |
| Profile cannot be read (stopped, invalid, API down) | blocked | Start it outside this workflow and plan again. |

Machine records or kubeconfig entries left without a profile still block `recreate-missing`, and the plan does not clean them up. Comparisons cover existence, UID, and ownership only. They ignore spec drift, which stage plans handle without deleting anything.

Execution requires `--overwrite --plan-id ID --execute`, with authorization for the stated data loss. It repeats the comparison and refuses stale evidence. Keep the kubeconfig and Minikube home outside the workspace, and the workspace outside Minikube home. Execution reserves a writable private archive on the workspace filesystem before consuming the plan and deleting anything. A failed delete, timeout, or residual profile, machine, or kubeconfig record leaves the workspace files in place.

Archiving moves workspace entries other than `.operation.lock` and `archive/` into `archive/<UTC time>-<plan prefix>/`, mode `0700`. The new workspace records the archive and deletion outcome in `reports/cluster-recreated.json`. The replacement start and its binding are reported separately in `reports/cluster.json`, including an uncertain start after a completed deletion. Earlier state, credentials, and plans remain in the archive. Prerequisites, stage plans, and credentials start again from new reviewed plans.

## Delete a created cluster

Use this only with `cluster.mode=create` and a workspace whose creation report records the Minikube home. Choose how to handle the workspace files with `--workspace-files`, which has no default:

| Mode | Workspace files after deletion | Next step |
|---|---|---|
| `keep` | Stay in place. `reports/cluster.json` becomes `deleted`, and `reports/cluster-delete.json` records the outcome. | Cluster actions and plain `cluster` refuse. `cluster --overwrite` plans `recreate-missing`, which archives these files first. A later `delete --workspace-files archive` or `purge` handles them without touching Minikube. |
| `archive` | Move to `archive/<UTC time>-<plan prefix>/`, mode `0700`. The new workspace holds `environment.json`, empty directories, and `reports/cluster-deleted.json`. | A plain `cluster` plan creates a new profile. |
| `purge` | `environment.json`, `cluster-binding.json`, `stages/`, `state/`, `plans/`, `reports/`, and `credentials/` are removed. When nothing else remains, the workspace directory is removed too. | The result exists only in the command output. Removal is an ordinary unlink, not a secure erase. |

`archive/` is never removed. Purge blocks while the workspace holds any other entry or a symbolic link in place of a known entry. Move those away yourself first.

1. Run `delete --workspace-files MODE`. The plan reports one action:

   | Live evidence | Action | Execution |
   |---|---|---|
   | Running profile. Minikube SSH, the configured context, and the creation report agree on the `kube-system` UID and endpoint. | `delete` | `minikube delete --profile=NAME`, then the workspace mode. |
   | No profile with this name, and no machine or kubeconfig records remain | `already-absent` | The workspace mode only. |
   | Stopped, invalid, or unreadable profile | blocked | Start it yourself with `minikube start -p NAME` and plan again. |
   | Identity differs from the creation report | blocked | The profile is not this workspace's cluster. Delete it yourself if that is intended. |
   | Profile present after a `keep` deletion | blocked | It was created elsewhere. |
   | Leftover machine or kubeconfig records without a profile | blocked | Inspect and remove them yourself. |

2. Review `data_loss`. It records live UIDs from listable Kubernetes APIs, including PVCs, PVs, ConfigMaps, Secret metadata, and custom resources. A claim needs its own workspace record or a verified live controller reference. A PV needs a verified binding to a managed claim. The presence of an object in a managed or system namespace gives no ownership evidence. Unmanaged objects, including system objects without ownership evidence, require `--include-unmanaged`. Incomplete discovery or list access blocks the plan. A context that references a profile-named cluster or user also blocks deletion.
3. After authorization of that plan, run `delete --workspace-files MODE [--include-unmanaged] --plan-id ID --execute` with the same options. Execution repeats the live inventory and detects replaced objects by UID. `archive` reserves its directory before deleting anything. `purge` reviews nested artifacts, blocks on unknown entries, and unlinks only the reviewed files.

A failed or timed-out delete, or any profile, machine, or kubeconfig record left afterward, leaves the workspace files in place and the creation report `created`. Krunkit cleanup also requires independent confirmation that every recorded VM process is gone. Other drivers currently report `uncertain` and preserve the workspace files after a delete attempt. The consumed plan preserves this evidence, so absent Minikube records alone cannot authorize cleanup on a retry. If handling the workspace files fails after a confirmed deletion, the error reports both outcomes.

## Workspace and identity

Use a private directory, for example `/private/path/foo-dev`. New helper-created directories request mode `0700`. JSON files and new lock files request `0600`. The directory check requires the current user's UID and no group or other permissions. It does not require exact owner permission bits or change existing permissions.

Stage copies and saved plan files are explicitly set to `0600`. Child commands receive a `077` umask. This affects new files, not existing ones. Keep the workspace out of source control and distribution archives. See [Artifact I/O and subprocesses](io.md) for path checks and storage limits.

The workspace lock is advisory and non-blocking. A competing operation using the same lock is rejected immediately. The lock is released when the CLI action exits; `.operation.lock` remains on disk. It does not coordinate separate workspaces or unrelated tools.

Each stage has its own Terraform working directory and state path. The CLI sets that path during initialization. Terraform input variables cannot set a backend path. Do not share state between environments or let separate states own the same Kubernetes resources.

Use one workspace for the cluster's managed environment. Concurrent first deployments from different workspaces are unsupported. Ownership labels cannot prevent a race before resources exist. Use the same workspace and new plans for later changes.

Every Kubernetes request uses the configured kubeconfig and context. Commands leave the global current context alone. Inspection records the live cluster identity, including the `kube-system` namespace UID. Reusing a context name after recreating a cluster does not preserve that identity.

## Local files and command failures

JSON writes use a temporary file in the target directory. The helper flushes and syncs that file before atomic replacement. It does not sync the parent directory or promise persistence after power loss. Other file writes and stage copies do not use this JSON replacement path.

Failed JSON writes attempt to remove their temporary file. Cleanup can fail and leave a `.write-*` file. The original error retains the cleanup error as its cause. CLI JSON errors show only the primary message. Inspect the affected private directory before deciding how to recover. The helpers do not perform stale-file cleanup at startup.

Commands have per-call timeouts. The shared runner uses 60 seconds by default. Runtime Terraform init, plan, and apply use 600 seconds. The runner captures output in memory and does not redact it. Keep raw command logs private. A timeout does not prove that an external operation had no effect.

Before invoking Terraform apply, the runtime consumes the reviewed plan. A runner timeout or startup error records the result as `uncertain`. Inspect actual state and create a new plan before another attempt. See the [subprocess reference](io.md#subprocess-execution) for exact environment and error handling.

## Preparation and inventory

1. Run offline `configure`. Review components, small resource requests, names, and stages. Use `storage.mode=csi` when the existing Minikube CSI driver is ready.
2. Run `prerequisites` when dependency preparation is needed. Review and execute its concrete plan within the authorized scope. Compatible installations are reused.
3. Run `inspect`. Check ownership, node readiness, DNS, storage, operators, and required CRDs. Review per-Pod allocations and remaining node capacity. Monitoring also needs a ready Prometheus instance with matching selectors and Pod labels.
4. Resolve blockers with current evidence. A configured budget does not prove that RAM or disk space is available. Missing statistics, permissions, and ownership information block planning.
5. Do not automatically resize VMs, stop workloads, adopt objects, install duplicate operators, or clear data to pass a check.

Capacity checks use measured free memory and allocatable memory minus assigned Pod requests. They include init peaks, restartable init sidecars, and Pod overhead. The placement estimate reserves 128Mi per used node and 256Mi of aggregate memory headroom. Each volume is assigned once within the selected storage nodes. The algorithm tries at most 10000 assignments and reports a blocker if it finds none.

An unused node with less disk space does not need to fit every volume. Read the report's `allocations` and remaining-capacity fields. These are estimates, not scheduling reservations. CPU, image pulls, admission, concurrent work, and runtime behavior need separate evidence.

`image_platforms` compares every node's `linux/<arch>` with the platforms recorded for each pinned image the configuration may deploy. A missing platform blocks planning, because a Pod on that node could never pull its image.

Check free disk space on selected storage nodes and verify binding and retention. Local volumes belong to their nodes. PVC size is neither a host disk quota nor a backup.

## Plan and execute by stage

Generate a plan for the next stage whose prerequisites are ready. Each plan and apply checks that stage's new Pods and volumes. Bootstrap creates dedicated namespaces and identities. `20-gpu` needs bootstrap, the render device on every selected node, and no other plugin for `devic.es/dri` ([GPU device plugin](gpu.md)). `05-storage-csi` creates a class using the existing CSI driver; `05-storage` installs a separate Local Path controller. PostgreSQL needs its namespace, storage class, and source Secrets. Kafka needs ready Strimzi CRDs and an Operator in its configured namespace. The monitoring stage needs a ready monitoring installation, matching selectors, and ready data services.

Unchanged applied stages are excluded from new capacity demand. A resource increase retains full replacement-Pod headroom. Data-stage state blocks automatic volume shrinking or storage-node/class changes. Plan review also rejects shrinking Kafka volumes, reducing existing Topic partitions, or shortening existing message retention.

Deploy in stages. After applying a prerequisite, check its result and create a fresh plan for the next stage.

The saved review records the target, resolved configuration, and source hashes. It also records provider lockfiles, prior state, and the saved plan's digest and ID. Review actual additions and updates. This workflow rejects deletion, replacement, unknown ownership, and target mismatches. Failed and superseded plans cannot be applied.

Present the plan and any related operations outside Terraform, such as Secret writes. Before execution, the tool checks the saved plan against its inputs and target. Changes to configuration, source, state, identity, or plan require a new plan and review.

For a preview, replace `REVIEWED_PLAN_ID` with the ID from the saved plan:

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev apply --stage 00-bootstrap --plan-id REVIEWED_PLAN_ID
```

To execute that exact change within the authorized scope:

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev apply --stage 00-bootstrap --plan-id REVIEWED_PLAN_ID --execute
```

After a failed write, report what completed and what is uncertain. Inspect actual state before preparing a new plan. Do not automatically roll back data resources or retry before resolving the cause.

## PostgreSQL credentials

The database stage references these Secrets in the data namespace. Terraform does not manage their values:

| Secret | Fields / role |
|---|---|
| `<postgres_name>-admin` | `username`, `password`; administrator. |
| `<postgres_name>-app` | `username`, `password`; configured application login. |
| `<postgres_name>-metrics` | `username`, `password`; separate monitoring login. |
| `<postgres_name>-tls` | `ca.crt`, `tls.crt`, `tls.key`; server TLS. |

The helper also writes `<postgres_name>-client` in the application namespace. It contains only `username`, `password`, and public `ca.crt`. The private CA signing key stays on the protected host. Applications receive only the public trust certificate. Server certificates cover the Service DNS name and the exporter's connection name.

Passwords and private keys stay out of the environment configuration, Terraform inputs/state, command arguments, and printed reports.

Preview references offline:

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev credentials
```

Prepare local material without changing Kubernetes:

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev credentials --prepare
```

After bootstrap, write the prepared Secrets within the authorized scope:

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev credentials --execute
```

Preparation reuses a valid existing bundle. Stop if ownership is unknown, files are incomplete, certificates have expired, or identities have changed. Inspect the affected files and objects. The helper does not silently reset passwords. Some Secret writes may succeed before another fails. They are not a transaction across namespaces. Check the recorded result before retrying.

Changing a Kubernetes Secret alone does not change a database role password. Certificate renewal, password rotation, client reloads, and rollout are separate changes. The helper does not automate rotation, and `credentials --execute` keeps refusing to overwrite a Secret whose contents differ. See [Credential expiry and renewal](#credential-expiry-and-renewal) for the manual order.

## Kafka client handoff

Strimzi creates `<kafka_name>-producer` and `<kafka_name>-consumer` in the Kafka namespace. Its public CA is in `<kafka_name>-cluster-ca-cert`. The helper copies only the selected user certificate, user key, and public CA. It writes `<kafka_name>-producer-client` and `<kafka_name>-consumer-client` in the application namespace.

Preview mappings offline:

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev credentials --component kafka
```

After Strimzi and its users are ready, execute the authorized handoff:

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev credentials --component kafka --execute
```

Kafka has no `--prepare` operation. Strimzi issues its source credentials. Never copy cluster CA private keys or broker identities to applications. Reject destinations with unknown ownership. The handoff may succeed only in part. Copies do not rotate automatically, and clients need to reload credentials after future changes.

## PostgreSQL backup and restore

Backups are logical `pg_dump` files of the application database. They run inside the PostgreSQL Pod as the `postgres` OS user over the Unix socket, so peer authentication needs no password. The output streams through `kubectl exec` into a private host directory.

`--backup-dir` must lie outside the Minikube home (`MINIKUBE_HOME`, `~/.minikube`, and the home recorded at cluster creation), the workspace, and the installed skill. A `delete` or `cluster --overwrite` therefore never removes a backup, and `purge` never sees one. Each backup gets a new directory `<environment>-<UTC time>-<run>/`, mode `0700`:

| File | Contents |
|---|---|
| `dump.sql` | Plain SQL dump with `--no-owner --no-privileges`, mode `0600`. |
| `manifest.json` | Source cluster UID, API server, namespace, database, Pod, StatefulSet, PVC and PV UIDs, server version, byte size, SHA-256, exact rows per table from the dump's `COPY` sections, the fixture row, and a digest of the manifest itself. |

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev backup --backup-dir /private/path/backups --with-fixture
```

Present the plan's source, output directory, reads, and writes. After authorization, repeat the command with `--plan-id REVIEWED_PLAN_ID --execute`. `--with-fixture` creates a run-specific schema and table in one transaction and inserts one random row in the application database. Cleanup locks the table, checks the schema and table OIDs and the sole fixture row, and drops both objects in one transaction with `RESTRICT`. Without the flag, the backup writes nothing to the database.

A restorable dump requires pg_dump to exit 0, the completion trailer to be present, and any requested fixture row to appear in the dump. Incomplete dumps are kept as `dump.sql.partial` without a manifest. If fixture cleanup fails after a verified dump, the backup report records the failure and the verified backup remains available.

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev restore --backup /private/path/backups/atlas-20261126T101500Z-1a2b3c
```

Restore requires a running local Docker engine reachable through a Unix socket and enough disk space for the snapshot and the isolated container. Planning verifies the manifest, dump size, SHA-256, trailer and table row counts, then records the source, engine, image digest and new container name. The target database defaults to `<database>_restore_<run>`. Use `--target-database NAME` to choose another name inside the isolated server. After authorization, execute with the reviewed plan ID. Execution makes a private snapshot and verifies it against that plan before creating the container.

The container runs PostgreSQL and psql as a non-root OS user. It has no external network, host data mounts, cluster credentials, or published ports. PostgreSQL accepts only container-local Unix socket connections. Restore loads the snapshot and checks row counts and the fixture token. Cleanup removes the container by its recorded full ID. With `--keep-target`, the report gives the ID of the retained container. Failed cleanup makes the drill exit nonzero even if the data checks passed.

`verify` requires a matching fixture and completed cleanup or reviewed retention. The backup and the drill must both match the current cluster and PostgreSQL PVC and PV UIDs. A fixture-free restore can succeed and leaves the recovery check pending.

Limits:

- An isolated restore drill proves the dump loads into the pinned image and that its recorded row counts and fixture match. Losing the volume or the cluster requires restoring into a new environment: create and deploy it, then load the dump manually after review. That path has not been automated.
- Logical dumps are point-in-time copies. There is no WAL archiving or PITR. Roles and passwords are not part of the dump; they come from the credential bundle.
- Backups are not encrypted at rest. Protect the backup directory as you protect the credentials, and copy it off the host for real durability.

## Credential expiry and renewal

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev credentials --expiry
```

This read-only check covers every enabled component. It reads only public certificate fields through jsonpath, never private keys or passwords. For each certificate it reports the subject, `not_after`, remaining days, and a status: `ok`, `warning` (under 30 days), `critical` (under 7 days), or `expired`. Certificates checked:

| Group | Source and copies |
|---|---|
| `postgres-ca` | Local `ca.crt`, `<postgres_name>-tls:ca.crt`, `<app namespace>/<postgres_name>-client:ca.crt`. |
| `postgres-server` | Local `server.crt` and `<postgres_name>-tls:tls.crt` (365-day validity from `credentials --prepare`). |
| `kafka-cluster-ca`, `kafka-clients-ca` | Strimzi CA Secrets and the `ca.crt` in each application client Secret. |
| `kafka-producer`, `kafka-consumer` | Strimzi user Secret and its application copy. |

Copies in one group must have the same fingerprint. A difference is listed under `mismatched_copies` and fails the check. It usually means Strimzi renewed a certificate but the application copy was never updated. For application-namespace Secrets, `client_pods` lists Pods that mount the Secret or reference it from an environment variable. Unreadable or missing fields are listed under `unavailable` and make the status at least `warning`.

The renewal steps below are manual and need their own authorization. Each says where to resume after a failure.

**PostgreSQL password rotation.** PostgreSQL holds one password per role, so clients fail authentication between steps 3 and 5.

1. Take a backup. This is the recovery point for data.
2. Generate the new value privately and update `bundle.json`. Keep the previous file until step 6.
3. Change the role password in the database with `ALTER ROLE` from stdin through `kubectl exec`, never as a command argument.
4. Replace `<postgres_name>-app` (or `-admin`, `-metrics`) and, for the application role, `<postgres_name>-client`. `credentials --execute` refuses to overwrite them, so replace them by a reviewed delete and recreate.
5. Restart the clients listed by `credentials --expiry`.
6. Restart PostgreSQL once to confirm startup. Its startup checks log in with every mounted password and refuse to start on a mismatch.

Recovery point: if step 4 fails, the database has the new password and the Secrets have the old one. PostgreSQL then refuses to restart. Either finish step 4, or set the old password back with `ALTER ROLE` from the previous bundle.

**PostgreSQL server certificate renewal (same CA).** Clients trust the CA, not the server certificate, so they need no change.

1. Issue a new server certificate from the local CA with the same `server.ext`, and keep the previous pair.
2. Replace `tls.crt` and `tls.key` in `<postgres_name>-tls`.
3. Restart the PostgreSQL Pod. TLS files are copied at startup, and the startup check connects with `verify-full`.

Recovery point: a bad certificate keeps the new Pod unready, and the previous Pod's data is unaffected. Restore the previous pair into the Secret and restart.

**PostgreSQL CA renewal.**

1. Add the new CA next to the old one in every client trust file: the `ca.crt` in `<postgres_name>-tls` and in `<postgres_name>-client`.
2. Restart the clients.
3. Issue a server certificate from the new CA, then renew it as above.
4. Remove the old CA after all clients have reloaded.

Recovery point: until step 3, the old certificate still works for every client.

**Kafka client Secret update.** Strimzi renews user certificates and CAs before they expire and updates its own Secrets. The application copies do not follow.

1. When `credentials --expiry` reports a `kafka-*` mismatch, replace `<kafka_name>-producer-client` and `-consumer-client` by a reviewed delete and `credentials --component kafka --execute`. The publisher refuses to overwrite different contents.
2. Restart the clients listed in `client_pods`.

Recovery point: the Strimzi Secret is the source of truth, so a broken copy is fixed by copying again. During a CA renewal Strimzi trusts both CAs for a while. Finish both steps within that window.

**Client reload.** Secret volumes update in running Pods after a kubelet sync, but most clients read certificates and passwords only at startup. Environment variables never update. Plan a rollout restart for every listed Pod.

## Kafka data recovery boundary

Kafka runs as one broker on one local volume, with no replication, MirrorMaker, or backup. Topics, users, ACLs, and NetworkPolicies are declarative: `31-kafka` recreates them. Messages, consumer offsets, and the Strimzi CAs live only in the cluster.

| Data | Recovery |
|---|---|
| Events that an upstream system still holds, such as rows in PostgreSQL or files at their source | Replay from upstream after recreating Kafka. Consumers must be idempotent. |
| Messages whose only copy is in Kafka, such as dead letters or results not stored elsewhere | Not recoverable. Persist them outside Kafka, for example in PostgreSQL, if they matter. |
| Consumer group offsets | Lost with the volume. Consumers restart from their configured reset policy, so plan for replays or gaps. |
| Strimzi cluster and clients CAs | Regenerated when Kafka is recreated. Hand off new client Secrets and restart clients. |

The default retention (`kafka.retention_hours=6`, `kafka.retention_mib=64`) also bounds what a running broker keeps. Treat Kafka as transport, not as a system of record. This release backs up PostgreSQL only.

## Verification and acceptance

`verify` selects checks from enabled components. PostgreSQL-only operation does not require Kafka CRDs. Plain PostgreSQL does not require CNPG, cert-manager, or Gateway API. It is read-only and reports three separate results:

- `baseline_result` and `checks`: controller status, ownership, Pod readiness, and PVC binding.
- `acceptance_result` and `acceptance_checks`: functional evidence, each item `pass`, `fail`, or `pending` with its reason.
- `pending_acceptance`: descriptions of the items still pending.

A failed functional check sets `status` to `fail`. Pending items alone keep the baseline status.

### Baseline checks

| Component | Checked objects |
|---|---|
| Stages | Each enabled stage's controller at its current generation, with the environment ownership label. |
| PostgreSQL | The data PVC is `Bound`. |
| Kafka | Every configured KafkaTopic (`Ready`, matching `topicName`), both KafkaUsers, the node pool, and each node's data PVC. |
| Monitoring | One PodMonitor per enabled data service and the `PrometheusRule` with the exact alert set, each owned and carrying the `release` label. |

Each object reports its own state: `ready`, `missing`, `not-ready`, `not-bound`, `not-owned`, `not-selected`, `mismatch`, `access-denied`, or `read-failed`. One failed read no longer hides the other results. With PostgreSQL and Kafka both enabled, each needs its own PodMonitor.

### Functional acceptance

| Item | Evidence | Source |
|---|---|---|
| `postgres_tls` | `verify-full` session; a never-trusted CA and plaintext are rejected. | `acceptance --component postgres` |
| `postgres_permissions` | Application role has no administrative or monitoring role. A table write in a rolled-back transaction succeeds and leaves nothing behind. `CREATE ROLE`, `pg_read_file`, and `SET ROLE postgres` are denied. The admin role over the network and the application role on `postgres` are rejected. | `acceptance --component postgres` |
| `kafka_mtls` | The fixture user reads metadata over mTLS; a client without a certificate is rejected. | `acceptance --component kafka` |
| `kafka_produce_consume` | One message round-trips on a fixture topic with a fixture user and group. | `acceptance --component kafka` |
| `kafka_acl_rejection` | The fixture user cannot read an application topic, the application producer cannot write to the fixture topic, and the application consumer cannot join another group. | `acceptance --component kafka` |
| `prometheus_targets`, `prometheus_samples`, `prometheus_rules` | Active targets for each PodMonitor are up, `pg_up` is 1, Kafka under-replicated partitions are 0, and the platform rules are loaded and not firing. | `verify`, through the API server pod proxy |
| `gpu_smoke` | `gpu-smoke --execute` passed against the current plugin. | `gpu-smoke` |
| `alert_delivery` | An isolated alert reaches its intended receiver. | Pending; no runner. |
| `network_policy_allowed`, `network_policy_denied` | Allowed traffic succeeds, and unlabeled or wrong-namespace clients are refused, including DNS. Acceptance Pods use the allowed path only, so they do not close these items. | Pending; no runner. |
| `postgres_backup_restore` | An isolated restore matched row counts and the fixture for a backup from the current cluster and data volume. | `backup` and `restore` |
| `gpu_workload` | Vulkan and inference workloads. | Pending; no runner. |

When a Prometheus read fails, for example because `pods/proxy` access is denied or a NetworkPolicy blocks the API server, the affected items stay `pending` with that reason. For a sharded instance, all three items stay `pending` until evidence can be collected across every shard.

`verify` accepts an acceptance report only for the same cluster (`kube-system` UID) and the same tested versions: the PostgreSQL StatefulSet and client Secret, or the Kafka cluster, application KafkaUsers, client Secrets, and cluster CA. A rollout or credential change puts the items back on the pending list.

### Run functional acceptance

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev acceptance --component postgres
```

The plan checks the workspace binding, the applied and ready stages, the application client Secrets from `credentials --execute`, and leftover fixtures in this environment. It records a random run ID, the target identity, the tested versions, and the digest-pinned `client_image`. Present the image with the `fixtures`, `reads`, `writes`, `cleanup`, `untouched`, and `time_limits`. Obtain authorization for that plan, then run `acceptance --component postgres --plan-id REVIEWED_PLAN_ID --execute`. Kafka works the same way with `--component kafka`.

Execution rechecks the identity, target versions, and client image before consuming the plan and writing fixtures. It checks each source Secret's UID and resource version again when reading its data. Fixtures live in the validation namespace and, for Kafka, a fixture KafkaTopic and KafkaUser in the Kafka namespace. Each carries the environment labels and `local-k8s-platform/acceptance-run=<run ID>`. The client Pod is non-root and restricted, carries the access label the NetworkPolicies expect, and mounts only the fixture Secret. Its log contains fixed result tokens, never command output or messages.

Cleanup runs after success, failure, or interruption. It deletes fixtures newest first after checking the run and environment labels. Each DELETE carries the UID recorded at creation as a server-side precondition. Deleting the KafkaUser removes the Strimzi-issued Secret, and the run waits for that. Deleting the KafkaTopic removes the fixture topic and its message. The Kafka client Pod also confirms the fixture consumer group is absent after attempting deletion. Remaining fixtures or unconfirmed consumer-group cleanup make `cleanup` `incomplete`. Leftover Kubernetes fixtures block new plans in the owning environment and appear as unmanaged objects in a `delete` plan.

The run copies application client credentials into the fixture Secret for its duration. It never prints them or writes them to reports. The wrong CA's private key exists only in a private local temporary directory during preparation.

### Remaining limits

Functional probes create Pods and Secrets, write fixture data, or would send alerts. Read-only verification does not authorize those actions. Do not change business topics, credentials, replicas, or application data to demonstrate a test.

Build checks and offline tests cover the implementation. They do not establish deployment or runtime acceptance. Mark unexecuted checks as pending, including backups and restores. Single instances and local volumes remain single points of failure after tests pass.

## Handoff and data preservation

Return the configuration, target identity, applied plan IDs, and endpoints. Include required Pod labels, identity names, application-local Secret references, and the acceptance report. Generated interface records keep the status `planned-not-connectivity-verified`. Record live readiness in a separate verification report. Applications still own their schemas, migrations, and releases.

Preserve PVCs, data, state, and credentials after failures. Platform stages have no teardown command; only a reviewed `delete` removes a created profile with everything in it. Review ownership and backups before volume moves, major upgrades, adoption, or destructive cleanup.
