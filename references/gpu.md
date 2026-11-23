<!-- Purpose: Explain the optional GPU device plugin stage, its preflight checks, readiness evidence, and the smoke test. -->

# GPU device plugin

The optional `20-gpu` stage runs the generic device plugin on selected Minikube nodes. The plugin advertises the krunkit virtio-gpu render node `/dev/dri/renderD128` as the schedulable resource `devic.es/dri`. Workloads request it like any other extended resource. The plugin does not install drivers, Vulkan, or inference runtimes.

The design follows a read-only reference platform. Its Terraform DaemonSet, verification script, and smoke-test Pod became this stage, the `20-gpu` readiness check, and the `gpu-smoke` action.

## Configuration

```json
"gpu": { "enabled": true, "nodes": ["minikube"], "slots": 1 }
```

| Field | Meaning |
|---|---|
| `gpu.enabled` | Select the `20-gpu` stage. Defaults to `false`. |
| `gpu.nodes` | Required when enabled. Nodes that expose the render device. The DaemonSet runs only on these nodes, because a missing `/dev/dri` would fail its hostPath mount. |
| `gpu.slots` | Allocation slots per node, from 1 to 16. Defaults to `1`. Values above 1 let Pods share one device without GPU memory isolation. |

The resource name, device path, and plugin image are fixed by the package. With `cluster.mode=create`, the driver must be `krunkit`, and `gpu.nodes` must be nodes of the new cluster.

GPU can be the only component. A GPU-only environment disables PostgreSQL, Kafka, and monitoring. It keeps `storage.mode=reuse` and needs no StorageClass. See [environment.gpu.example.json](../assets/environment.gpu.example.json). Each plugin Pod requests 32Mi, which counts against `budget.max_memory_requests_mib`.

## Resources and ownership

| Resource | Name |
|---|---|
| Namespace | `<environment_id>-gpu`, labeled `pod-security.kubernetes.io/enforce=privileged` |
| NetworkPolicy | `<environment_id>-gpu-plugin`: no egress; ingress only on the plugin health port 8080 |
| DaemonSet | `<environment_id>-gpu-device-plugin`, privileged, with hostPath mounts for `/var/lib/kubelet/device-plugins` and `/dev/dri` |

Only this namespace is privileged. The plugin needs host access to discover the device and register with kubelet. Pods that request `devic.es/dri` run in restricted namespaces and need no privileges. Every resource carries the environment ownership labels. The namespace has `prevent_destroy`.

The stage depends on `00-bootstrap`. It is independent of storage and data services.

## Preflight

`inspect` and `plan --stage 20-gpu` add these read-only checks:

| Check | Passes when |
|---|---|
| `gpu_nodes` | Every selected node exists. |
| `gpu_device` | `minikube --profile P ssh --node N -- test -c /dev/dri/renderD128` succeeds on every selected node. The profile comes from the node's `minikube.k8s.io/name` label. |
| `gpu_plugin_owner` | No other DaemonSet runs a generic device plugin or the `devic.es` domain. No node advertises `devic.es/dri` unless this environment's plugin already exists. |
| `image_platforms` | The plugin and BusyBox smoke images support every node platform. |

A second plugin for the same resource would conflict with this one at kubelet. The workflow never adopts or replaces it. Remove or reconfigure the other plugin outside this workflow, or keep it and leave `gpu.enabled` off.

## Plan, apply and verify

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev plan --stage 20-gpu
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev apply --stage 20-gpu --plan-id REVIEWED_PLAN_ID --execute
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev verify
```

Apply can return before a new DaemonSet is Ready, because the provider may treat a just-created DaemonSet with no scheduled Pods as rolled out. Treat `verify` as the readiness gate. It reports `20-gpu` as passing only after both conditions hold:

- The owned DaemonSet is Ready and updated on every selected node at its current generation.
- Kubelet advertises `capacity` and `allocatable` equal to `gpu.slots` on every selected node. Registration can lag the rollout, so verify retries for about 30 seconds.

`allocatable` is total schedulable capacity. It does not drop while Pods hold slots, so it does not show free capacity.

## Smoke test

```sh
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev gpu-smoke
python3 -B scripts/platform.py --config environment.json --workspace /private/path/foo-dev gpu-smoke --execute
```

Without `--execute`, the action prints the Pod and its effects and contacts nothing. With `--execute`, it does the following:

1. Checks the workspace binding, the applied `20-gpu` record, and the stage's readiness.
2. Creates `<environment_id>-gpu-smoke` in the validation namespace. The Pod runs as non-root under the restricted profile and requests one `devic.es/dri`.
3. Waits up to 150 seconds. The Pod checks that the render node exists, that it is readable and writable, and that it opens.
4. Reads the log tail. Then it deletes the Pod it created, matched by UID, even after a failure.
5. Writes `reports/gpu-smoke.json`.

The smoke test creates and deletes a cluster object, so it needs its own authorization. A leftover Pod with the same name blocks the run; inspect it before retrying. With `slots=1`, the Pod cannot schedule while another workload holds the device. The report then says that no slot is free.

`verify` accepts a passing smoke report only for the same cluster and the same plugin DaemonSet UID and generation. A plugin update or a new cluster puts the smoke test back on the pending list.

## Limits

- The smoke test checks device allocation and injection only. Vulkan, model inference, and GPU utilization stay pending.
- Inference workloads need userspace drivers and an image that supports the krunkit virtio GPU.
- Slots share one device. There is no memory isolation or fair scheduling between Pods.
- Disabling `gpu.enabled` after `20-gpu` has state is rejected. Removing the plugin requires a separate reviewed operation.
