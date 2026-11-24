---
name: local-k8s-platform
description: Create a new, explicitly named Minikube cluster or reuse an existing one, prepare missing prerequisites, and configure PostgreSQL, Kafka, and an optional GPU device plugin on it. Use for local Kubernetes platform setup, GPU plugin installation on krunkit nodes, reviewed installation plans, and readiness checks. Application releases are outside its scope.
---
<!-- Purpose: Route local platform requests through validated configuration and packaged execution tools. -->

# Local Kubernetes Platform

Configure one platform environment per Minikube cluster. Reuse an existing cluster, or create a new profile with an explicit name through a reviewed plan. This release requires Calico. Reuse compatible Strimzi and Prometheus installations. Prepare missing Operators, a minimal Prometheus instance, and local OpenSSL through reviewed prerequisite plans. Required manifests, Terraform files, and Python scripts are included.

Use English for maintained documentation and code comments. Respond in the user's preferred language.

## Select the work

- For configuration, defaults, or component choices, read [configuration.md](references/configuration.md).
- To create a new Minikube cluster, read [Create a Minikube cluster](references/operations.md#create-a-minikube-cluster).
- To delete a cluster this workspace created, read [Delete a created cluster](references/operations.md#delete-a-created-cluster).
- For small-model local training and existing Minikube CSI storage, read [local-training.md](references/local-training.md).
- To install the GPU device plugin or run the GPU smoke test, read [gpu.md](references/gpu.md).
- Before inspecting a cluster, deploying resources, or writing credentials, read the relevant procedure in [operations.md](references/operations.md).
- For missing Operators, monitoring setup, or local OpenSSL, read [prerequisites.md](references/prerequisites.md).
- For implementation changes or scope decisions, read [design.md](references/design.md).
- For offline Terraform checks or provider startup errors, read [validation.md](references/validation.md) and use `scripts/validate.py`.

Support a single PostgreSQL StatefulSet, a single Strimzi KRaft node, and an optional GPU device plugin that advertises the krunkit render node as `devic.es/dri`. Enable any combination of these; a GPU-only environment disables monitoring and storage. Local storage and monitoring integration are optional. Cluster creation never starts an existing profile. It recreates a profile only through an explicitly requested, separately authorized `cluster --overwrite` plan, and deletes one only through such a `cluster --overwrite` or `delete` plan, in both cases for a profile this workspace created. Operator replacement, HTTPS ingress, CNPG, PITR, and application releases are outside this version's scope. Explain unsupported requests rather than trying to enable them through configuration.

## Form an explicit configuration

1. Identify the cluster mode, context or new cluster name, environment ID, data services, storage, application namespace, and resource budget. Use the request and available evidence. Never invent a cluster name; ask for one when a new cluster is wanted.
2. Copy `assets/environment.example.json` (existing cluster) or `assets/environment.create.example.json` (new cluster) outside the packaged assets. Keep passwords and private keys out of the copy.
3. Use `scripts/platform.py`. It checks inputs and fills in defaults. Pass that resolved configuration to later steps. Do not add a second set of input checks or unrelated shell commands.
4. Use one private workspace for the environment and cluster. Keep packaged assets unchanged. Apply later configuration changes through the same workspace.

Examples run from this skill's root. Replace `environment.json` and `/private/path/foo-dev` with the selected configuration and workspace.

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev configure
```

`configure` is offline. Review selected stages, resource estimates, names, and planned interfaces before contacting Kubernetes.

## Create the cluster (new clusters only)

With `cluster.mode=create`, the explicit `cluster.name` becomes the Minikube profile and the kubeconfig context. Later cluster actions require the identity recorded during creation.

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev cluster
```

The plan reads the Minikube version, profile list, machine records, and kubeconfig entry names. Present the profile name, kubeconfig, driver, nodes, CPUs, memory, disk, Kubernetes version, CNI, addons, and the exact command. A conflicting profile, machine record, or kubeconfig entry blocks creation. A krunkit start also requires its machine and client socket paths to fit the Unix socket limit. Review `MINIKUBE_HOME` and, for libkrun 1.19 or later, `TMPDIR`. Obtain authorization for that exact plan, then run `cluster --plan-id REVIEWED_PLAN_ID --execute`. This authorization is separate from prerequisites, Terraform changes, and Secret writes.

The result suggests `network.api_server_cidrs` for the new cluster. Review them and add them to the configuration before Kafka prerequisites or planning. A failed or interrupted start or an unverified identity keeps the profile for inspection. Report the outcome and stop.

### Overwrite a created cluster

Use this only when the user asks to recreate or overwrite the cluster. It applies only to a profile this workspace created.

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev cluster --overwrite
```

The plan compares the profile with the workspace's records and reports one action:

- `recreate-missing`: the profile no longer exists.
- `overwrite`: the profile is readable, but its identity or a recorded object differs. The plan includes `minikube delete`.
- `unchanged`: everything matches. There is nothing to execute.

A stopped or unreachable profile blocks. For a running profile, the API queried from its control plane and the chosen context must return the same cluster UID. Present the reviewed Minikube home, action, differences, delete command, and effects. A deleted profile loses its data, and prerequisites, stages, and credentials need new plans. Obtain authorization for that plan, then run `cluster --overwrite --plan-id REVIEWED_PLAN_ID --execute`. Execution rechecks the evidence, reserves the archive, and confirms deletion and clean host records before moving the previous workspace files and starting the replacement.

### Delete a created cluster

Use this only when the user asks to delete the cluster. It applies only to a profile this workspace created. Ask how to handle the workspace files when the user has not said:

- `keep`: leave them in place and mark the creation report `deleted`. Cluster actions stop. `cluster --overwrite` can later archive them and recreate the profile.
- `archive`: move them to `archive/` and leave an empty workspace for a new `cluster` plan.
- `purge`: remove the files this skill wrote, including credentials. `archive/` and unknown files are never removed, and unknown files block the plan.

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev delete --workspace-files archive
```

The plan reports `delete` for a running profile whose identity matches the creation report, or `already-absent` when the profile no longer exists and cleanup evidence allows it. A stopped, unreadable, or mismatched profile blocks, along with leftover machine or kubeconfig records and shared kubeconfig references. Unmanaged live objects require `--include-unmanaged`. Review the data-loss inventory, including PVCs, PVs and object UIDs. Purge also requires a verified nested inventory. Present the action, delete command, `data_loss`, and effects. Obtain authorization for that plan, then repeat the same options with `--plan-id REVIEWED_PLAN_ID --execute`. Preserve the workspace when native VM removal is uncertain.

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

Plan a stage only after its prerequisites are ready. Begin with `00-bootstrap`. Use `05-storage-csi` for an existing CSI driver, or `05-storage` for a new Local Path controller. Plan `20-gpu` after bootstrap when GPU is enabled. Prepare PostgreSQL credentials before planning that stage. Add monitors and rules after the selected data services are ready.

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

`verify` runs read-only readiness checks. With GPU enabled, `gpu-smoke --execute` creates and then deletes one test Pod to check device injection; it needs its own authorization. This release has no other automated functional acceptance runner. Ready Pods and bound PVCs do not prove TLS, permissions, ACLs, network isolation, alerts, or recovery. Leave those results pending until authorized tests provide evidence.

Return the resolved configuration, created cluster profile if any, executed stages, plan IDs, and endpoints. Include required application labels, application-local Secret references, and remaining acceptance work. Keep credential values out of responses, logs, Terraform inputs, and state. Mark unexecuted checks as pending. Report full usability only after the relevant acceptance checks pass.
