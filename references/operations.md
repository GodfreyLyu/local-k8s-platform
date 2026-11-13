<!-- Purpose: Describe command effects, staged deployment, credential handling, and evidence-based verification. -->

# Platform operations

Run commands from the skill's root. Supply a configuration and a private workspace outside the repository. Use one workspace for the managed environment in a cluster. Keep packaged scripts and assets unchanged.

The helpers need Python 3.10 or later on macOS or Linux. They use only the Python standard library. Cluster operations also need Terraform `>=1.10,<2`, kubectl, and an existing Minikube kubeconfig. Pinned providers and images must be available. PostgreSQL credential preparation uses OpenSSL.

## Command effects

| Command | Local effects | Cluster effects |
|---|---|---|
| `configure` | Fill in defaults and copy selected stage files into the workspace. | None; offline. |
| `inspect` | Save structured inventory and blockers. | Read-only API requests. |
| `plan --stage NAME` | Initialize/check one stage; save plan and review manifest. | Read current objects; no apply. |
| `apply --stage NAME --plan-id ID` | Check a saved plan and show execution preview. | No mutation without `--execute`. |
| `apply ... --execute` | Record stage result. | Apply the matching reviewed plan. |
| `credentials` | Show PostgreSQL references and mappings. | None. |
| `credentials --prepare` | Create or reuse protected local PostgreSQL material. | None. |
| `credentials --execute` | Use the prepared bundle; record results. | Write fixed source Secrets and the application client Secret. |
| `credentials --component kafka` | Show Kafka credential mappings. | None. |
| `credentials --component kafka --execute` | Record handoff results. | Read selected Strimzi client fields and write application client Secrets. |
| `verify` | Save baseline results and pending acceptance items. | Read-only checks for enabled components. |

The CLI has no destroy, import, cluster creation, or automatic cleanup action. Configuration and preview commands do not authorize later writes. Building this project does not install the skill globally.

To build a portable archive, choose an output path outside the maintained source directories:

```sh
python3 -B scripts/package.py --output /private/path/local-k8s-platform.tar.gz
```

The archive contains the maintained skill files, tests, and a checksum manifest. Identical source produces identical archive bytes. Private workspaces and runtime files are excluded. Packaging does not install or deploy anything.

## Workspace and identity

Use a private directory, for example `/private/path/foo-dev`. Runtime directories use mode `0700`; files that may contain state, plans, or credentials use `0600`. Keep the workspace out of source control and distribution archives.

Each stage has its own Terraform working directory and state path. The CLI sets that path during initialization. Terraform input variables cannot set a backend path. Do not share state between environments or let separate states own the same Kubernetes resources.

Use one workspace for the cluster's managed environment. Concurrent first deployments from different workspaces are unsupported. Ownership labels cannot prevent a race before resources exist. Use the same workspace and new plans for later changes.

Every Kubernetes request uses the configured kubeconfig and context. Commands leave the global current context alone. Inspection records the live cluster identity, including the `kube-system` namespace UID. Reusing a context name after recreating a cluster does not preserve that identity.

## Preparation and inventory

1. Run offline `configure`. Review components, resource requests, names, and stages.
2. Run `inspect`. Check ownership, node readiness, DNS, storage, operators, and required CRDs.
3. Resolve blockers with current evidence. A configured budget does not prove that RAM or disk space is available. Missing statistics, permissions, and ownership information block planning.
4. Do not automatically resize VMs, stop workloads, adopt objects, install duplicate operators, or clear data to pass a check.

Check free disk space on eligible nodes and verify storage binding and retention. Local volumes belong to their nodes. PVC size is neither a host disk quota nor a backup.

## Plan and execute by stage

Generate a plan for the next stage whose prerequisites are ready. Bootstrap creates dedicated namespaces and identities. Add storage creation when selected. PostgreSQL needs its namespace, storage class, and source Secrets. Kafka needs existing Strimzi CRDs and a namespace watched by the operator. Monitoring needs existing CRDs, matching selectors, and ready data services.

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

Changing a Kubernetes Secret alone does not change a database role password. Certificate renewal, password rotation, client reloads, and rollout are separate changes. This helper does not automate rotation.

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

## Verification and acceptance

`verify` selects checks from enabled components. PostgreSQL-only operation does not require Kafka CRDs. Plain PostgreSQL does not require CNPG, cert-manager, or Gateway API.

This release checks baseline readiness only. The table below also lists evidence needed for functional acceptance. Those functional tests are not automated in this release.

| Result | Evidence |
|---|---|
| Baseline readiness | Controller status, Pod readiness, PVC binding, and selected monitoring object existence. Actual scrape health remains a separate check. |
| PostgreSQL acceptance | Dedicated client connection with `verify-full`, expected permissions, wrong-CA/account rejection. |
| Kafka acceptance | TLS authentication, reachable advertised addresses, isolated produce/consume, forbidden ACL rejection. |
| Network-policy acceptance | Allowed traffic succeeds and denied traffic fails from scoped clients. |
| Monitoring acceptance | Actual samples and an isolated alert delivered through the intended route. |
| Recovery acceptance | Create a protected backup, restore it to isolated resources, and verify the test data. |

Functional probes may create Pods, write data, or send alerts. Read-only verification does not authorize those actions. Use isolated fixtures and dedicated identities. Set time limits and review cleanup. Do not change business topics, credentials, replicas, or application data to demonstrate a test.

Build checks and offline tests cover the implementation. They do not establish deployment or runtime acceptance. Mark unexecuted checks as pending, including backups and restores. Single instances and local volumes remain single points of failure after tests pass.

## Handoff and data preservation

Return the configuration, target identity, applied plan IDs, and endpoints. Include required Pod labels, identity names, application-local Secret references, and the acceptance report. Generated interface records keep the status `planned-not-connectivity-verified`. Record live readiness in a separate verification report. Applications still own their schemas, migrations, and releases.

Preserve PVCs, data, state, and credentials after failures. This version has no automatic teardown. Review ownership and backups before volume moves, major upgrades, adoption, or destructive cleanup.
