---
name: local-k8s-platform
description: Prepare missing prerequisites and configure PostgreSQL and Kafka on an existing Minikube cluster. Use for local platform configuration, reviewed installation plans, and readiness checks. Cluster creation and application releases are outside its scope.
---
<!-- Purpose: Route local platform requests through validated configuration and packaged execution tools. -->

# Local Kubernetes Platform

Configure one platform environment per existing Minikube cluster. This release requires Calico. Reuse compatible Strimzi and Prometheus installations. Prepare missing Operators, a minimal Prometheus instance, and local OpenSSL through reviewed prerequisite plans. Required manifests, Terraform files, and Python scripts are included.

Use English for maintained documentation and code comments. Respond in the user's preferred language.

## Select the work

- For configuration, defaults, or component choices, read [configuration.md](references/configuration.md).
- For small-model local training and existing Minikube CSI storage, read [local-training.md](references/local-training.md).
- Before inspecting a cluster, deploying resources, or writing credentials, read the relevant procedure in [operations.md](references/operations.md).
- For missing Operators, monitoring setup, or local OpenSSL, read [prerequisites.md](references/prerequisites.md).
- For implementation changes or scope decisions, read [design.md](references/design.md).
- For offline Terraform checks or provider startup errors, read [validation.md](references/validation.md) and use `scripts/validate.py`.

Support a single PostgreSQL StatefulSet and a single Strimzi KRaft node. Local storage and monitoring integration are optional. Cluster creation, operator replacement, HTTPS ingress, CNPG, PITR, and application releases are outside this version's scope. Explain unsupported requests rather than trying to enable them through configuration.

## Form an explicit configuration

1. Identify the context, environment ID, data services, storage, application namespace, and resource budget. Use the request and available evidence.
2. Copy `assets/environment.example.json` outside the packaged assets. Keep passwords and private keys out of the copy.
3. Use `scripts/platform.py`. It checks inputs and fills in defaults. Pass that resolved configuration to later steps. Do not add a second set of input checks or unrelated shell commands.
4. Use one private workspace for the environment and cluster. Keep packaged assets unchanged. Apply later configuration changes through the same workspace.

Examples run from this skill's root. Replace `environment.json` and `/private/path/foo-dev` with the selected configuration and workspace.

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev configure
```

`configure` is offline. Review selected stages, resource estimates, names, and planned interfaces before contacting Kubernetes.

## Prepare prerequisites

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev prerequisites
```

This prepares a plan for enabled components. It reuses compatible installations and lists missing resources and local package commands. It does not install anything. Use `--component openssl` for local preparation without contacting Kubernetes. If no changes are needed, continue to `inspect`.

Present the target, reused resources, namespace, versions, requested memory, and exact changes. Include cluster-scoped CRDs and RBAC. Obtain any missing authorization for that plan. Then execute it with `prerequisites --plan-id REVIEWED_PLAN_ID --execute`. This authorization is separate from later Terraform changes and Secret writes.

Do not replace, upgrade, or adopt existing Operators. Stop on conflicts or stale plans. Preserve partial installations and prepare a fresh plan before resuming. An installed Operator or a Ready Prometheus Pod does not establish scrape health or alert delivery.

## Inspect and plan

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev inspect
```

Check current capacity, StorageClass behavior, ownership, namespaces, and required CRDs. Stop when the evidence is missing. Do not set an ownership flag or treat resource requests as measured free capacity.

Plan a stage only after its prerequisites are ready. Begin with `00-bootstrap`. Use `05-storage-csi` for an existing CSI driver, or `05-storage` for a new Local Path controller. Prepare PostgreSQL credentials before planning that stage. Add monitors and rules after the selected data services are ready.

Use the small defaults for new local environments: PostgreSQL and Kafka each request a 1Gi PVC. Read the capacity report by Pod and storage node. Reuse current Operators and CSI where compatible. Keep model files, training data, and checkpoint budgets separate. Existing volumes, Topic partitions, and retention settings need their own migration review before any reduction.

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev plan --stage 00-bootstrap
```

Present actual changes, target cluster, stage, plan ID, prerequisites, and unresolved checks. A successful plan does not establish runtime readiness.

## Execute the reviewed change

Follow the session's authorization and the plan review procedure in `operations.md`. Selecting the skill or preparing files does not authorize Kubernetes changes. Replace `REVIEWED_PLAN_ID` below with the ID from the saved plan.

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev apply --stage 00-bootstrap --plan-id REVIEWED_PLAN_ID
```

This shows a preview. Add `--execute` only when that exact change is authorized. Stop on stale-plan, ownership, capacity, or target failures. If a write fails, report partial results and inspect them before retrying.

`credentials` previews PostgreSQL Secret references. `credentials --prepare` creates local material. `credentials --execute` writes source and application Secrets. Use `credentials --component kafka --execute` after Strimzi has prepared the selected users. Read the credential procedure first. Rotation and application rollout are separate operations.

## Verify and hand off

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev verify
```

`verify` runs read-only readiness checks. This release has no automated functional acceptance runner. Ready Pods and bound PVCs do not prove TLS, permissions, ACLs, network isolation, alerts, or recovery. Leave those results pending until authorized tests provide evidence.

Return the resolved configuration, executed stages, plan IDs, and endpoints. Include required application labels, application-local Secret references, and remaining acceptance work. Keep credential values out of responses, logs, Terraform inputs, and state. Mark unexecuted checks as pending. Report full usability only after the relevant acceptance checks pass.
