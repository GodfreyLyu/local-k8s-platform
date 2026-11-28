<!-- Purpose: Record completed checks, native Terraform results, and the limits of the evidence. -->

# Validation

This report includes build checks from 2026-11-14 for the small local profile, prerequisite automation, and existing-CSI storage, and live runs from 2026-11-22 and 2026-11-23 on the `skill-test` Minikube profile. The 2026-11-26 entries cover the verify checks, the functional acceptance runner, recovery, plan review records, and delete driver support, offline only. The 2026-11-28 entry covers overwrite driver support, offline only. The results cover implementation, static validation, a selected-cluster read-only preflight, cluster creation, prerequisites, and deployment of every stage with passing readiness checks. Each row keeps its own date and scope; counts in older rows describe the build of that date. Live functional acceptance remains pending.

## Results

| Check | Result |
|---|---|
| Offline Python tests | 134 passed on Python 3.13 on 2026-11-23, after the live-run fixes (previously 128, then 109). Coverage includes capacity placement, CSI checks, data migration guards, stage scopes, prerequisites, configuration, private I/O, credentials, startup, validation orchestration, cluster creation, image platforms, and container Pod Security settings. |
| Prerequisite orchestration | 24 tests passed with private fake kubectl, Homebrew, and OpenSSL tools. Cases include missing future storage, permissions before CRD discovery, reuse, conflicts, budgets, plan integrity, and partial recovery. Linux package commands were checked without installation. |
| Capacity placement | Eight isolated tests covered the three-node capacity pattern, effective Pod requests, shared disk accounting, taints, and joint allocation. A low-disk control node did not block worker volumes. |
| Vendored CRD schemas | All 10 Strimzi schemas and all 10 Prometheus Operator schemas matched their official downloaded bundles. Source and packaged checksums matched. |
| Minimal Prometheus schema | The rendered instance passed the packaged v1 JSON schema. An invalid replica type was rejected. This check did not evaluate Kubernetes CEL or admission. |
| Kafka manifest schemas | Exact NodePool and Topic HCL expressions were evaluated in a provider-free console. CSI and reused-class cases passed the packaged v1 schemas. The CSI NodePool included the selected worker affinity. |
| Python compatibility | All maintained Python files parsed with Python 3.10 grammar. The new behavior was executed on Python 3.13. |
| Local certificate preparation | OpenSSL generated and verified temporary certificates. A second run reused the credentials. Test material was removed afterward. |
| Earlier agent trial | The original build's PostgreSQL-only trial produced the expected stages and application settings. It made no calls to external tools. |
| Python lint and formatting | Ruff checks passed. Ruff is optional for development; the runtime needs no third-party Python packages. |
| Skill structure and documentation | The skill validator, relative links, and required file and function comments passed inspection. |
| Portable package | Reproducibility and runtime-file exclusions passed. All 64 source hashes were checked in a temporary archive. Its extracted CLI configured the CSI profile with an empty external-tool PATH. |
| Terraform formatting | All six stage roots passed recursive format checking. |
| Native provider check | Schema loading passed with Terraform `1.15.8` and Kubernetes provider `2.38.0`. |
| Native configuration matrix (2026-11-14) | All 18 profiles and 63 stage instances passed, with zero errors and zero warnings. The GPU profiles were added on 2026-11-23; see the GPU device plugin row for the 20-profile, 71-instance run. |
| Typed input contracts | All six exact stage variable declarations accepted normalized input in provider-free, resource-free console fixtures. Twelve negative cases rejected a missing kubeconfig or an invalid context type. |
| Selected-cluster preflight | The private small profile passed read-only inspection. The estimate placed PostgreSQL and its 1Gi volume on one worker, Kafka and its 1Gi volume on the other, and the Entity Operator on the control node. No resources were created. |
| Cluster creation (2026-11-22) | 11 isolated tests passed with private fake minikube and kubectl tools. They covered create-mode input checks, the exact reviewed `minikube start` arguments, blocking existing valid/stopped/invalid profiles and contexts, plan invalidation, consumed plans, failed starts without deletion, the pre-creation gate, and legacy workspace bindings. A read-only plan against the local minikube v1.39.0 produced the expected command and blocked the existing `minikube` profile. No cluster was created. |
| Live cluster creation (2026-11-22) | Reviewed `cluster --execute` created the three-node krunkit profile `skill-test` (Kubernetes v1.37.0, arm64). All nodes became Ready, Calico reached 3/3, and `hostpath.csi.k8s.io` registered. Read-only `inspect` passed every cluster check and blocked only on the missing Strimzi Operator. |
| Live prerequisites, first attempt (2026-11-22) | All 49 reviewed resources were created. Strimzi became Ready after a 191-second image pull, which exceeded the 180-second wait. Prometheus Operator v0.95.1 never started: its pinned image existed only for linux/amd64. Fixes: v0.95.2 pins, recorded image platforms with an `image_platforms` preflight check, and a 600-second readiness wait. |
| Image platforms | All nine pinned digests were read from their registry indexes. Each now supports linux/amd64 and linux/arm64. Upstream v0.95.2 CRD schemas and ClusterRole rules equal the vendored resources. |
| Live prerequisites repair (2026-11-22) | A fresh plan reported false drift on the Strimzi Deployment because the API server stores `1000m` as `1`; resource quantities are now compared numerically. The 15 label-verified v0.95.1 operator objects were deleted with separate authorization. During CRD deletion the 3072 MiB control-plane node ran out of memory (VM root filesystem in RAM, about 1.2 GiB) and kube-apiserver restarted. `cluster.create.memory_mib` now has a 4096 MiB minimum, and the profile is being recreated at that size. |
| Live rebuild (2026-11-22) | After the user deleted the 3072 MiB profile, a reviewed plan recreated `skill-test` at 4096 MiB per node (control plane 1402 MiB available before prerequisites). The reviewed prerequisites plan created all 49 resources: Strimzi 1.3.0 and Prometheus Operator v0.95.2 became Ready, and Prometheus v3.16.0 reconciled. A fresh plan afterwards reported zero changes. Minikube set the kubeconfig current context to the new profile despite `--keep-context=true`; the previous current context was most likely empty. |
| Live data stages (2026-11-22) | `00-bootstrap`, `05-storage-csi`, and PostgreSQL credentials applied as reviewed. The first `30-postgres` apply was rejected by the restricted Pod Security profile: kubernetes provider v2.38.0 sends an omitted container `run_as_non_root` as `false`, overriding the Pod value. Terraform waited until the 600-second limit, and the stage was recorded as `uncertain`. Container security contexts now set `run_as_non_root = true` (guarded by `tests/test_pod_security.py`). With authorization, the untracked StatefulSet (no Pod, no data) and the stale state lock were removed; a fresh plan created only the StatefulSet, and PostgreSQL 17.11 became Ready on `skill-test-m03` with a bound 1Gi volume. |
| Live Kafka and monitoring (2026-11-22) | `31-kafka` applied; Strimzi reconciled Kafka, both topics, and both users to Ready in 340 seconds, and the Kafka client handoff published two application Secrets. The `40-monitoring` preflight then counted the Strimzi entity operator (same image) as a second cluster Operator; operator detection now skips Deployments labeled `strimzi.io/cluster` or owned by a `kafka.strimzi.io` resource. After the fix, `40-monitoring` applied and `verify` passed every readiness check. Prometheus reported both PodMonitor targets up (`pg_up` = 1, Kafka series present) and evaluated all three rules as inactive. |
| GPU device plugin (2026-11-23) | 11 isolated tests in `tests/test_gpu.py` passed with fake kubectl and minikube. They covered GPU-only and combined configurations, input rejection, node-pinned capacity forecasts, foreign-plugin and stale-registration blocking, the render-device probe, rollout and slot registration, DaemonSet plan review, the restricted smoke Pod, smoke cleanup on success, failure and unschedulable cases, leftover-Pod blocking, and smoke evidence bound to the plugin generation. The full offline suite passed 145 tests on Python 3.13. The native matrix with a local provider mirror passed all 20 profiles and 71 stage instances, with zero errors and zero warnings, including both GPU profiles. The plugin image digest and platforms (linux/amd64, linux/arm64, linux/arm) were read from Docker Hub. No cluster was contacted during these offline checks. |
| GPU live acceptance (2026-11-23) | On the existing three-node krunkit `skill-test` profile (arm64, Kubernetes v1.37.0), `inspect` found `/dev/dri/renderD128` on all three nodes and no other plugin. The reviewed `20-gpu` plan created only the namespace, NetworkPolicy and DaemonSet. All three plugin Pods became Ready under the NetworkPolicy, and every node advertised `devic.es/dri` capacity and allocatable 1. `verify` passed `20-gpu` and all existing stages. `gpu-smoke --execute` passed on `skill-test-m02` and deleted its Pod. A second `verify` accepted the smoke evidence. Apply returned before the new DaemonSet was Ready, so `verify` is the readiness gate. Vulkan and inference remain pending. |
| Verify and functional acceptance (2026-11-26, offline) | 24 focused tests passed using fake kubectl and Kafka tools. They cover per-object verification after denied reads and timeouts, alert-only rule comparisons, pending sharded Prometheus evidence, Kafka metadata and offset-commit flags, consumer-group absence after deletion, UID-conditional cleanup under replacement, environment-scoped leftovers, source Secret rotation, and client image binding. The full suite passed 216 tests on Python 3.10.11. A loopback fake API confirmed that kubectl sends the UID precondition and returns nonzero on conflict. Checks against live client images and services remain pending. |
| PostgreSQL recovery and credential expiry (2026-11-26, offline) | 16 focused tests passed with fake Kubernetes and Docker commands and real temporary OpenSSL certificates. They cover fixture ownership, isolated replay of a private verified snapshot, incremental hashing, cleanup failures, pending evidence without a fixture, PVC and PV identity, and full CA bundle comparison. The full suite passed 232 tests on Python 3.10.11. Live Kubernetes and isolated PostgreSQL startup and replay remain pending. |
| Terraform plan review and apply records (2026-11-26, offline) | 32 runtime tests passed with fake Terraform and kubectl. Regression cases cover an intervening write during Terraform planning, sensitive descendants in saved reviews and previews, tight-cluster replanning without crediting an interrupted apply as successful, and stable diagnostics across plan archival. The full suite passed 241 tests on Python 3.10.11. Live Terraform interruption and drift against real controllers remain pending. |
| Delete driver support (2026-11-26, offline) | 4 new tests passed with fake Minikube and kubectl, alongside the existing delete tests. They cover a krunkit VM restart during the data-loss inventory, unsupported drivers (`vfkit`, `docker`, `qemu2`) blocked at planning for keep, archive, and purge with no delete command or cluster read, a saved plan without process IDs refused at execution, and timeout and Ctrl-C after `minikube delete` started: `uncertain` reports, kept files, a consumed plan, and cleanup blocked until the VM process is gone. Existing tests still cover krunkit success, a surviving VM process, residual records, and keep, archive, and purge. The full suite passed 245 tests on Python 3.13.13. Live deletion remains pending. |
| Overwrite driver support (2026-11-28, offline) | 4 new tests passed with fake Minikube and kubectl. They cover drifted `vfkit` and `docker` profiles blocked at planning with no delete or start, a krunkit PID change during the state comparison, a saved overwrite plan without PIDs refused at execution, and a surviving krunkit process, timeout, or Ctrl-C after `minikube delete`: `uncertain` records, no archive, no replacement start, and `recreate-missing` blocked until the process exits. The full suite passed 249 tests on Python 3.13.13. Live overwrite remains pending. |
| Overwrite recovery and late PID checks (2026-11-28, offline) | 17 overwrite-focused tests passed, including four new cases for a PID change after the execution comparison, surviving VM evidence from an earlier attempt, legacy records without PID evidence, and a readable unchanged profile with an unreadable PID file. A separate fake-tool run confirmed legacy recreation through the reviewed confirmation option. The full suite passed 253 tests on Python 3.13.13. Live overwrite remains pending. |
| Live acceptance | Readiness and scraping passed on `skill-test` (2026-11-22). Functional acceptance and the backup and restore drill are pending reviewed live runs. Alert delivery and NetworkPolicy traffic have no runner and remain pending. |

## Prerequisite checks

The fake Kubernetes runner used private temporary files. It simulated CRD
establishment, Operator rollouts, and Prometheus reconciliation. No test created
live Kubernetes resources or installed host packages. Real OpenSSL was used only
for temporary certificate generation and verification in the credential tests.

Prerequisite tests also checked that separate component plans count already managed
resources toward the memory budget. Create-only execution preserved successful
resources after an injected failure. A consumed plan could not run again. A fresh
plan resumed the unchanged owned resources.

The official Strimzi `1.3.0` and Prometheus Operator `v0.95.2` bundles were checked
before conversion to packaged JSON. Image digests were read from the official
release or registry. The local Prometheus profile uses `v3.16.0`. These source
checks establish provenance and schema consistency. They do not prove that the
images run correctly in a selected cluster.

The small profile requests 1088Mi for data services and exporters and 2Gi for its
two volumes. The selected cluster's existing Operators and CSI driver were reused
in the preflight. This estimate does not reserve the proposed placement. Startup,
connections, memory pressure, and training workloads still need runtime evidence.

## Native Terraform run (2026-11-14)

This section describes the 18-profile run before the GPU stage existed. The 2026-11-23 run with a local provider mirror passed 20 profiles and 71 stage instances; see the results table.

The report returned `status=pass`, `provider_probe=pass`, and `cases_passed=cases_planned=63`.

The audit checked every command result. All 63 initializations and all 63 validations exited 0. Each validation returned `valid=true`, no errors, no warnings, and an empty diagnostic list.

These static checks cover attribute references and configuration consistency. The additional console fixtures evaluate actual input conversion against each stage's explicit `object` type. They contain no providers, resources, or remote backend.

All 27 stage source files and lockfiles matched the copies used by the native run. Generated variables used the selected storage modes. Coverage included every Terraform root:

| Stage | Passing profile instances |
|---|---:|
| `00-bootstrap` | 18 |
| `05-storage` | 6 |
| `05-storage-csi` | 6 |
| `30-postgres` | 12 |
| `31-kafka` | 12 |
| `40-monitoring` | 9 |

[Native validation evidence](native-validation.json) lists profile coverage and source hashes. It stores the report digest and digests for all 126 command diagnostics. Private workspaces and raw TRACE logs are excluded from the package.

These results establish static Terraform and provider-schema consistency. They do not prove live CRD compatibility, service connectivity, permissions, network isolation, alert delivery, or recovery.

## I/O documentation review

The documentation review used `io_utils.py` and its actual callers as the reference. All 14 JSON storage tests and six boundary tests passed on Python 3.10. Seven temporary probe groups also passed. They covered literal arguments, stdin, child permissions, nonzero exits, timeouts, environment filtering, and path-check limits.

These checks do not establish crash recovery or complete protection against concurrent path changes. See [Artifact I/O and subprocesses](io.md) for the implemented limits.

## Run the checks

Run the offline suite from the repository root:

```sh
python3 -B -m unittest discover -s tests -v
```

Check Terraform formatting:

```sh
terraform fmt -check -recursive assets/infra
```

For the validation matrix, choose a new private report directory outside the repository:

```sh
python3 -B scripts/validate.py --report-dir "$HOME/local-k8s-validation/new-run"
```

The runner first checks provider communication. It then validates PostgreSQL-only, Kafka-only, and combined profiles. Each runs with monitoring off/on and storage reuse/create/csi. Two GPU profiles follow: GPU-only, and GPU with both data services, monitoring, and CSI storage. That gives 20 profiles and 71 selected stage instances.

The runner uses explicit variable files. It needs a Terraform release whose `validate -help` lists `-var-file`; the tested release is `1.15.8`.

The runner does not call Kubernetes or run plan, apply, or credential commands. Provider installation may contact the registry. Add `--plugin-dir /path/to/mirror` to use an offline provider mirror.

Check `report.json` and the private diagnostics after each run. Static validation and live acceptance are separate results.

## Provider startup in a restricted environment

The first run failed before loading the provider schema. TRACE logging showed this error; the temporary path is shortened here:

```text
plugin init error: listen unix <temporary-directory>/plugin<id>: bind: operation not permitted
```

A minimal configuration with only `required_providers` and the lockfile failed in the same way. It contained no platform resources or Kubernetes connection. The execution environment denied the local socket used for provider RPC.

The same provider loaded successfully in the user's native terminal. The complete matrix then passed without changes to the pinned provider or Terraform assets.

If the provider probe hits this permission error, the runner returns `blocked` with `local_socket_denied`. It stops before the stage matrix. A later stage failure is recorded against that case, and the runner continues with other cases. Other provider failures keep their own diagnostic category.

## Packaging

The archive includes a source manifest and the published validation evidence. It excludes private runtime files. You can unpack it and run offline configuration without the original project directory.

Generated application settings remain marked `planned-not-connectivity-verified`. Deployment and functional tests must provide the remaining evidence.
