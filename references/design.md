<!-- Purpose: Record architecture, ownership, validation boundaries, and the first release's extension points. -->

# Architecture and design decisions

This skill configures a local development platform on an existing or newly created Minikube cluster. The Agent interprets requests and explains results. Terraform and Python handle the repeatable steps. Build checks do not prove that services are deployed or working.

## Responsibilities

| Layer | Responsibility | Output |
|---|---|---|
| `SKILL.md` | Interpret requests, choose operations, and explain evidence. | Configuration and results for review. |
| Configuration entry point | Check input, fill defaults, and derive names and stages. | Validated environment configuration. |
| Command entry point | Create the workspace and run the selected action. | Structured result. |
| I/O helpers | Check directories, replace JSON files, lock the workspace, and invoke commands. | Local artifacts and captured command results. |
| Terraform assets | Manage selected resources with separate state for each stage. | Saved plans and resources. |
| Credential helper | Prepare credentials and write fixed Secrets outside Terraform. | Protected credentials and application-local Secret references. |
| Cluster helper | Plan and run one `minikube start` for an explicitly named profile. It also handles reviewed overwrites and deletions of a profile this workspace created. | Review record, creation report, reconciliation report, deletion report, workspace archive, and suggested API addresses. |
| Prerequisite helper | Reuse compatible dependencies and create missing reviewed resources or local packages. | Saved manifests, plan IDs, ownership evidence, and partial-result reports. |
| Runtime checks | Check cluster identity, ownership, prerequisites, and readiness. | Inventory, plan review, and acceptance status. |
| Capacity helper | Estimate joint Pod memory and volume placement from current evidence. | Suggested allocations and remaining node capacity. |
| GPU smoke helper | Create one restricted test Pod that requests the GPU resource, read its result, and delete it. | Smoke report bound to the cluster and plugin rollout. |
| Acceptance helper | Plan, then run one restricted client Pod against labeled PostgreSQL or Kafka fixtures, and delete only the fixtures its run created. | Plan ID, acceptance report bound to the cluster and the tested versions. |
| Recovery helper | Plan and take a logical PostgreSQL dump through `kubectl exec`, then compare an isolated restore in local Docker. | Private backup directory with a checksummed manifest; backup and restore reports. |

The agent invokes the packaged CLI through its execution tool. Python starts child commands without a shell wrapper. This skill needs no custom server, MCP connection, background daemon, or scheduler.

## Source and workspace separation

`assets/infra` contains the Terraform source for this skill. It was adapted from reference infrastructure and runs without sibling projects. The package includes only the supported local stages. It keeps published validation summaries, but excludes private logs, production-reference stacks, state, credentials, and saved plans.

Every `platform.py` action loads the configuration and acquires the workspace lock. It then calls `configure` before the selected action. This copies selected stages and writes their variables, `environment.json`, and interface records. Preview and read-only cluster actions can therefore update local files.

Terraform initialization and backend setup run in the workspace. This keeps `.terraform`, state, and plans outside the packaged assets. File hashes record each plan's source and configuration.

Keep the workspace outside the installed skill directory. If it contains stage files absent from the current engine, configuration stops. It refuses to run with leftover Terraform code and leaves those files untouched. Removing engine files or managed stages requires a separate migration review.

`scripts/package.py --output <archive.tar.gz>` builds a reproducible archive. It includes `agents`, `references`, `scripts`, `assets`, and `tests`, plus `SKILL.md`, `AGENTS.md`, and `pyproject.toml`. The generated `manifest.json` lists source hashes. Fixed file order and archive metadata make identical source produce identical bytes. Private runtime directories and Terraform caches are excluded. Packaging works offline and does not install the skill globally.

Use one workspace and one managed platform environment per cluster. Separate state files cannot prevent conflicts over shared StorageClasses, namespaces, or resource names. Two workspaces can both try to create the same object before either has recorded ownership. Concurrent initial deployment from different workspaces is unsupported.

Support for multiple environments needs clear ownership of shared cluster resources. It also needs distinct names, selectors, Secret mappings, and application interfaces.

## Configuration and validation

`scripts/config.py` defines the configuration rules. It checks allowed fields, required values, names, quantities, and component combinations, then fills defaults. Internal functions use those results directly. There is no separate JSON Schema to maintain.

Each Terraform stage declares an explicit `object` type containing only the environment fields it consumes. These types document the Python-to-Terraform interface and support static field checks. Defaults, naming rules, and component-combination validation remain in Python.

Later checks cover things that can change outside the input file:

- Files or plans may change after preparation.
- A context may point to a recreated cluster.
- Kubernetes objects may have external owners or concurrent updates.
- Capacity and readiness depend on live observations.
- A credential bundle must still match its intended identity and certificate names.

These checks protect external state. They do not repeat the original input checks.

The selected components determine stages, prerequisites, resource estimates, credentials, monitors, and readiness checks. A PostgreSQL-only configuration has no Kafka or CNPG dependency.

Small defaults apply to new services. Data volumes are 1Gi each. Memory forecasts
follow Pod boundaries: PostgreSQL includes its exporter, while the Kafka broker
and Entity Operator are separate Pods. Training data, model weights, checkpoints,
and training jobs keep their own budgets.

CSI mode creates a class using the existing Minikube CSI driver. The selected
workers appear in both StorageClass topology and data-Pod affinity. The Local Path
mode remains available for environments that need a separate provisioner.

## I/O and subprocess boundaries

The shared helpers check local permissions and replace individual JSON files atomically. They do not make workspace preparation a transaction. Their subprocess runner uses argument lists, per-call timeouts, and a child umask. It does not provide a command sandbox or cap captured output.

The standalone Terraform validator uses a separate subprocess wrapper. Its environment filtering and diagnostic files differ from the runtime runner. See [Artifact I/O and subprocesses](io.md) for both implementations.

## Ownership and integration

The skill manages its dedicated namespaces, identities, policies, data services, optional storage, and monitoring objects. It can also create missing Strimzi and Prometheus prerequisites through a reviewed plan. Existing Minikube, CNI, Strimzi, Prometheus, Grafana, Alertmanager, and Argo CD keep their owners. The skill does not adopt or deploy application resources.

Prerequisites use packaged official JSON manifests and atomic create calls. They stay outside Terraform state. Their review records bind configuration, source hashes, target identity, and reused resource contents. Execution consumes the plan before writes and records each result. A fresh plan can resume unchanged owned resources after a partial failure. There is no automatic upgrade, rollback, adoption, or cleanup. See [prerequisite operations](prerequisites.md).

Storage retention and deletion protection reduce accidental deletion through Terraform. They cannot prevent an administrator from deleting data, or protect against disk or host failure. Multiple VMs on one computer do not provide independent availability zones.

The platform creates database accounts and Kafka access rules. Applications own schemas, migrations, release settings, and business behavior. Copying client credentials into the application namespace is a separate step from creating a Service.

## GPU device plugin

The GPU plugin is a Terraform stage, not a prerequisite. This environment owns it, and a reviewed plan can update its slots. A device plugin is node-level and cluster-wide: two plugins for the same resource conflict at kubelet. Preflight therefore blocks when another plugin or an unexplained `devic.es/dri` registration exists. The workflow never adopts or replaces a plugin.

The plugin needs privileged host access. Only the dedicated `<environment_id>-gpu` namespace is privileged; the bootstrap namespaces stay restricted. Node selection is explicit, like storage nodes, because nodes without the render device would fail the hostPath mount. Capacity forecasts pin each plugin Pod to its node.

The smoke Pod stays outside Terraform state, as in the reference design. The `gpu-smoke` action creates it and always deletes it by UID. Its report counts as evidence only for the current cluster and plugin generation. See [GPU device plugin](gpu.md).

## Cluster creation

`cluster.mode=create` adds one create-only operation. The user names the profile explicitly. The review binds the configuration, sources, Minikube path and version, host platform, and absence of conflicting profile, machine, and kubeconfig entries. A stopped or invalid profile also blocks creation.

The command fixes Calico, CSI addons for CSI storage, the Kubernetes version range, and `--keep-context=true`. A successful start must establish a workspace binding with the cluster's API endpoint and `kube-system` UID. Later inspections and writes compare the observed identity with the binding and creation report. Restart and resize stay operator decisions.

`cluster --overwrite` is the only path that recreates a profile. It and `delete` are the only paths that delete one. It is an operation flag, not a configuration field, because `configure` binds the workspace to its cluster configuration. Turning the flag on must not look like a configuration change. Overwrite reviews record the flag, so plain and overwrite plans cannot stand in for each other.

Deletion requires a verified difference. The configured context and the API queried inside the running profile must return the same cluster UID. The creation and review records pin the resolved Minikube home. Resource evidence includes the observed UID and ownership label. Stage plans handle spec drift.

Execution reserves a private archive on the workspace filesystem before deletion. After a confirmed delete and a clean host check, it moves the workspace files, prepares the new workspace, and starts the replacement. Shared kubeconfig and Minikube home paths must sit outside the archive. The archive, deletion, and replacement outcomes remain available independently.

`delete` requires a verified match between the running profile, the configured context, and the creation report. The review binds `--workspace-files` and `--include-unmanaged`, live object identities, and the workspace inventory. Namespace membership provides no evidence of an object's ownership. Purge adds a nested artifact inventory. The workflow preserves the workspace when the driver cannot confirm VM removal. Delete and overwrite retain separate plan directories.

Workspace files change only after a confirmed delete and a clean host check. `keep` marks the creation report `deleted` instead of removing the binding. That status closes every cluster-contacting action. A later overwrite treats the profile as missing and archives the kept files first. A profile that appears under the same name after a kept deletion was created elsewhere, so it is never deleted or overwritten. `purge` removes only the entries `configure` and the helpers write. It is the one path that removes workspace history, and it still leaves `archive/` alone.

## Plans and execution

Apply uses a saved stage plan and its review record. That record covers configuration, source files, provider lockfiles, prior state, cluster identity, and plan bytes. Changed inputs or a failed attempt make the plan ineligible. An old successful plan must not appear current.

Plan each stage after its prerequisites exist. The provider needs installed CRDs to read their schemas. `depends_on` and targeted apply cannot make a missing API available during planning.

Cluster changes require `--execute`. The Agent follows the session's authorization and presents the concrete change before asking for any missing approval. The review also covers Secret writes and probes that change cluster resources or test data.

After a failure, stop writes and report what completed. Inspect the current state before recovery. The skill does not automatically roll back, reset passwords, adopt objects, delete PVCs, or retry failed writes.

## Verification strategy

Offline tests cover configuration, workspace creation, state isolation, plan integrity, command effects, credentials, and checks for the selected components. They use fake external tools where needed. Terraform formatting and provider validation check the packaged assets.

The latest native validation run (2026-11-23) passed 71 stage instances across 20 profiles, including existing-CSI storage and both GPU profiles. This checks static Terraform and provider schemas, not deployed services. See [build validation](validation.md) for the evidence and remaining limits.

`verify` reads baseline readiness per object and stays read-only. It also reads actual Prometheus targets, samples, and rules when the API server pod proxy allows it. Functional results stay separate from baseline results.

Backups use the in-Pod Unix socket with peer authentication, so no password leaves the cluster or appears in arguments. A plain-format dump is verifiable without PostgreSQL tools on the host: the completion trailer proves pg_dump finished, and `COPY` sections give exact row counts. Restore compares those counts instead of counts taken from the live source, which may change during the dump. Restore replays the verified private snapshot inside a network-isolated local Docker container. Cleanup uses the container ID returned at creation. Verification ties evidence to the source PVC and PV UIDs and requires a matching fixture. Credential renewal stays manual because each step changes live authentication; the expiry check gives the inputs and the documented order gives the recovery points.

The acceptance helper is a separate, reviewed write path, like the GPU smoke test. Fixtures carry a random run label and live in the validation namespace, plus a fixture topic and user for Kafka. Cleanup trusts only that label together with the UID recorded at creation, so it never deletes an object it did not create. Application credentials are copied into a fixture Secret for the run instead of placing test Pods in the application namespace. Results count only for the same cluster and tested versions. NetworkPolicy traffic and alert delivery have no runner. The acceptance Pods exercise only allowed paths, so the NetworkPolicy items stay separate.

## Extension points

New capabilities need an explicit change to the configuration version or supported combinations. Update their stages, prerequisites, credentials, and verification at the same time. Possible extensions include other local distributions, multiple environments per cluster, credential rotation, HTTPS ingress, and recovery tools. Converting existing single-instance services to replicated deployments requires a separate migration plan.
