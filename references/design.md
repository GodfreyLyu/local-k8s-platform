<!-- Purpose: Record architecture, ownership, validation boundaries, and the first release's extension points. -->

# Architecture and design decisions

This skill configures a local development platform on an existing Minikube cluster. The Agent interprets requests and explains results. Terraform and Python handle the repeatable steps. Build checks do not prove that services are deployed or working.

## Responsibilities

| Layer | Responsibility | Output |
|---|---|---|
| `SKILL.md` | Interpret requests, choose operations, and explain evidence. | Configuration and results for review. |
| Configuration entry point | Check input, fill defaults, and derive names and stages. | Validated environment configuration. |
| Command entry point | Create the workspace and run the selected action. | Structured result. |
| Terraform assets | Manage selected resources with separate state for each stage. | Saved plans and resources. |
| Credential helper | Prepare credentials and write fixed Secrets outside Terraform. | Protected credentials and application-local Secret references. |
| Runtime checks | Check cluster identity, ownership, prerequisites, and readiness. | Inventory, plan review, and acceptance status. |

The Agent runs the packaged command through its shell tools. This skill needs no custom server, MCP connection, background daemon, or scheduler.

## Source and workspace separation

`assets/infra` contains the Terraform source for this skill. It was adapted from reference infrastructure and runs without sibling projects. The package includes only the supported local stages. It keeps published validation summaries, but excludes private logs, production-reference stacks, state, credentials, and saved plans.

`configure` copies selected stages into the environment workspace and writes their variables. Terraform initialization and backend setup run there too. This keeps `.terraform`, state, and plans outside the packaged assets. File hashes record each plan's source and configuration.

Keep the workspace outside the installed skill directory. If it contains stage files absent from the current engine, configuration stops. It refuses to run with leftover Terraform code and leaves those files untouched. Removing engine files or managed stages requires a separate migration review.

`scripts/package.py --output <archive.tar.gz>` builds a reproducible archive. It includes `agents`, `references`, `scripts`, `assets`, and `tests`, plus `SKILL.md`, `AGENTS.md`, and `pyproject.toml`. The generated `manifest.json` lists source hashes. Fixed file order and archive metadata make identical source produce identical bytes. Private runtime directories and Terraform caches are excluded. Packaging works offline and does not install the skill globally.

Use one workspace and one managed platform environment per cluster. Separate state files cannot prevent conflicts over shared StorageClasses, namespaces, or resource names. Two workspaces can both try to create the same object before either has recorded ownership. Concurrent initial deployment from different workspaces is unsupported.

Support for multiple environments needs clear ownership of shared cluster resources. It also needs distinct names, selectors, Secret mappings, and application interfaces.

## Configuration and validation

`scripts/config.py` defines the configuration rules. It checks allowed fields, required values, names, quantities, and component combinations, then fills defaults. Internal functions use those results directly. There is no separate JSON Schema to maintain.

Later checks cover things that can change outside the input file:

- Files or plans may change after preparation.
- A context may point to a recreated cluster.
- Kubernetes objects may have external owners or concurrent updates.
- Capacity and readiness depend on live observations.
- A credential bundle must still match its intended identity and certificate names.

These checks protect external state. They do not repeat the original input checks.

The selected components determine stages, prerequisites, resource estimates, credentials, monitors, and readiness checks. A PostgreSQL-only configuration has no Kafka or CNPG dependency.

## Ownership and integration

The skill manages its dedicated namespaces, identities, policies, data services, optional storage, and monitoring objects. Existing Minikube, CNI, Strimzi, Prometheus, Grafana, Alertmanager, and Argo CD keep their owners. The skill does not adopt or deploy application resources.

Storage retention and deletion protection reduce accidental deletion through Terraform. They cannot prevent an administrator from deleting data, or protect against disk or host failure. Multiple VMs on one computer do not provide independent availability zones.

The platform creates database accounts and Kafka access rules. Applications own schemas, migrations, release settings, and business behavior. Copying client credentials into the application namespace is a separate step from creating a Service.

## Plans and execution

Apply uses a saved stage plan and its review record. That record covers configuration, source files, provider lockfiles, prior state, cluster identity, and plan bytes. Changed inputs or a failed attempt make the plan ineligible. An old successful plan must not appear current.

Plan each stage after its prerequisites exist. The provider needs installed CRDs to read their schemas. `depends_on` and targeted apply cannot make a missing API available during planning.

Cluster changes require `--execute`. The Agent follows the session's authorization and presents the concrete change before asking for any missing approval. The review also covers Secret writes and probes that change cluster resources or test data.

After a failure, stop writes and report what completed. Inspect the current state before recovery. The skill does not automatically roll back, reset passwords, adopt objects, delete PVCs, or retry failed writes.

## Verification strategy

Offline tests cover configuration, workspace creation, state isolation, plan integrity, command effects, credentials, and checks for the selected components. They use fake external tools where needed. Terraform formatting and provider validation check the packaged assets.

The native validation run passed 40 stage instances across 12 profiles. This checks static Terraform and provider schemas, not deployed services. See [build validation](validation.md) for the evidence and remaining limits.

`verify` reads baseline readiness and storage status. Functional acceptance needs separate checks for authentication, permissions, allowed and denied traffic, metrics, alert delivery, and restore results. This release has no automated SQL, Kafka, network, or backup acceptance runner.

## Extension points

New capabilities need an explicit change to the configuration version or supported combinations. Update their stages, prerequisites, credentials, and verification at the same time. Possible extensions include cluster creation, other local distributions, multiple environments per cluster, credential rotation, HTTPS ingress, and recovery tools. Converting existing single-instance services to replicated deployments requires a separate migration plan.
