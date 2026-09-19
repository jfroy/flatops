# GPU sharing via DRA

kantai serves its single GPU (kantai1) through Kubernetes Dynamic Resource
Allocation instead of the `nvidia.com/gpu` device plugin. Everything NVIDIA
lives in the privileged `nvidia-system` namespace.

## Components

| Component | Source | Role |
| --- | --- | --- |
| `dra-driver-nvidia-gpu` | `oci://registry.k8s.io/dra-driver-nvidia/charts/dra-driver-nvidia-gpu` (kubernetes-sigs) | Kubelet plugin publishing the `gpu.nvidia.com` ResourceSlice and preparing CDI specs |
| `gpu-operator` | `oci://nvcr.io/nvidia/cloud-native-charts/gpu-operator` | GPU feature discovery, driver/toolkit validator, dcgm-exporter. Device plugin **disabled** |
| Talos extensions | `talos/schematics/kantai1.yaml` | Driver (`nvidia-open-gpu-kernel-modules`) and container toolkit; driver root `/usr/local` |
| `any-nvidia-gpu` | `kubernetes/apps/default/gpu-claim-templates` | Namespace-scoped `ResourceClaimTemplate` every GPU pod references |

## Why the standalone chart and not gpu-operator's GPUCluster CR

The first attempt (`2dd77d99f`, reverted in `e01344615`) used gpu-operator's
`GPUCluster` CR. That path derives the driver root from the `nvidia-validator`
init container's `driver-ready` contract, whose pre-installed-driver branch
hardcodes host root `/`. On Talos the driver lives at `/usr/local`, and there
is no values-level fix. `/` is also explicitly rejected by NVIDIA's own
reference bundle ([AICR](https://github.com/NVIDIA/aicr) `CheckDriverOwnershipCoherence`,
the #1106 regression: runc refuses a bind-mount destination of `/`).

The standalone chart takes the root as a plain value
(`nvidiaDriverRoot` → `NVIDIA_DRIVER_ROOT`), with no validator and no contract
file. This is the combination Sidero documents for Talos
(<https://docs.siderolabs.com/kubernetes-guides/advanced-guides/dynamic-resource-allocation>)
and the architecture every AICR recipe uses; nothing in AICR uses `GPUCluster`.
`gpuCluster.deployCR` stays at its default `false`.

## Invariants

**Exactly one whole-GPU advertiser per node.** The device plugin and the DRA
kubelet plugin keep independent ledgers; enabling both can double-allocate the
same card. Three values flip together and must stay consistent:

```text
gpu-operator:           devicePlugin.enabled=false
dra-driver-nvidia-gpu:  resources.gpus.enabled=true
dra-driver-nvidia-gpu:  gpuResourcesEnabledOverride=true
```

`gpuResourcesEnabledOverride` is only an acknowledgement gate in the chart's
`templates/validation.yaml`; it exists so a stray `gpus.enabled=true` fails the
install rather than silently dual-advertising. The `dra-driver-nvidia-gpu`
Kustomization `dependsOn: gpu-operator` so the device plugin is gone before
the kubelet plugin comes up. With `devicePlugin.enabled=false` the operator
also drops the validator's `plugin-validation` init container
(`controllers/object_controls.go`, case `"plugin"`), so the validator does not
wait for a plugin that never arrives.

**Driver-root coherence.** `nvidiaDriverRoot` and gpu-operator
`hostPaths.driverInstallDir` are both `/usr/local`. AICR relaxes the equality
rule for preinstalled drivers, but keeping them equal costs nothing.

**Sharing.** The device plugin's `replicas: 8` time-slicing has no equivalent
in the DRA driver's `sharing.strategy`, which only sets the CUDA time-slice
duration. Oversubscription comes from the `ConsumableShares` feature gate with
`consumableShares: unlimited`: the GPU is published with
`allowMultipleAllocations: true` and a `memory` capacity whose request policy
defaults to 0, so claims that omit `capacity.requests.memory` consume nothing
and any number co-allocate. A claim may still request memory explicitly to
reserve scheduler-visible capacity (it is accounting, not runtime isolation).
`consumableShares: N` would be the literal equivalent of `replicas: N`;
`memory` would cap sharing at one claim per GPU for claims without a request.

`computeDomains.enabled: false` — that manages multi-node NVLink and kantai
has no NVSwitch fabric.

**Extended-resource bridging is not used.** The `gpu.nvidia.com` DeviceClass
carries `extendedResourceName: nvidia.com/gpu`, which would let `nvidia.com/gpu`
requests be satisfied by DRA once KEP-5004 graduates. AICR has not validated it
and its policy is reserved pending graduation, so workloads use explicit
`resourceClaims` rather than relying on it.

**Prerequisites** are met on Kubernetes 1.36: DRA is GA and locked on,
`DRAConsumableCapacity` and `KubeletPodResourcesDynamicResources` are on by
default. No Talos change is needed.

## Workload pattern

```yaml
controllers:
  main:
    containers:
      app:
        resources:
          claims:
            - name: gpu
    pod:
      resourceClaims:
        - name: gpu
          resourceClaimTemplateName: any-nvidia-gpu
```

Each pod gets its own generated `ResourceClaim`. A workload that needs a
particular card should get its own template with a CEL `selectors` block on
device attributes rather than narrowing `any-nvidia-gpu`.
`ResourceClaimTemplate.spec` is immutable; changing the request later needs
`force: true` on the Kustomization or a one-time manual delete.

The `resources` exception in `AGENTS.md` ("a device the scheduler must
allocate") now means `resources.claims`, not `nvidia.com/gpu` limits.

## Observability

dcgm-exporter runs with `KUBERNETES_ENABLE_DRA=true` so metrics carry
`pod`/`namespace`/`container` labels resolved through DRA. That mapping
(`internal/pkg/transformation/kubernetes.go`) needs an API client: a Pod
informer filtered to `NODE_NAME` and a `ResourceSlice` informer to translate
the pod-resources API's device names into GPU UUIDs. Two things make that
work, and gpu-operator v26.7.0 supplies neither on its own:

- **A ServiceAccount token.** The operator's DaemonSet asset sets
  `automountServiceAccountToken: false` and only flips it when
  `dcgmExporter.enablePodLabels` or `enablePodUID` is on; it does not
  recognise `KUBERNETES_ENABLE_DRA` as needing the API. `enablePodUID: true`
  is the cheapest switch: it mounts the token, creates the operator's own
  `nvidia-dcgm-exporter-read-pods` ClusterRole and binding, and adds a
  `pod_uid` label.
- **`resourceslices` read access.** The operator's asset carries a `TODO` for
  this, so `kubernetes/apps/nvidia-system/gpu-operator/app/rbac.yaml` binds
  a ClusterRole granting `get/list/watch` on exactly that. Drop it once the
  operator does it itself.

Without the token the exporter logs `Failed to get in-cluster config, pod
labels will not be available` at startup and emits per-GPU metrics with no
pod attribution. (`KUBERNETES_VIRTUAL_GPUS`, the previous setting, only maps
device-plugin time-sliced replicas.)

The `nvidia.com/gpu.*` node labels GFD leaves on kantai1 describe the device
plugin's view until GFD resyncs; nothing in this repo selects on them. The DRA
kubelet plugin places via NFD's `feature.node.kubernetes.io/pci-0300_10de.present`.

## Known noise: health stream errors on Kubernetes < 1.37

The kubelet plugin logs this every 5 seconds:

```text
"handling stream failed" err="rpc error: code = Unimplemented desc = device
health reporting is not supported by this driver" method="/v1alpha1.DRAResourceHealth/NodeWatchResources"
```

It is a kubelet/driver version skew, not a misconfiguration. Kubelet 1.36 has
`ResourceHealthStatus` on by default and opens a health stream to every DRA
driver inside `wait.UntilWithContext(…, 5s)`, a fixed retry. The NVIDIA
driver hard-codes `WatchHealthStatus → ErrHealthNotSupported`
(`cmd/gpu-kubelet-plugin/driver.go`), which the helper maps to
`Unimplemented`. The kubelet only treats that as "stop watching" from 1.37
(kubernetes/kubernetes#139477). Allocation is unaffected.

No chart value fixes it: `NVMLDeviceHealthCheck` publishes health as
ResourceSlice device taints, not over this stream, and the line is
Error-level so `logVerbosity` cannot hide it. Decision: tolerate it until the
cluster reaches Kubernetes 1.37. If it needs silencing sooner, a
`KubeletConfig` patch in `talos/node/kantai1/` setting
`config.featureGates.ResourceHealthStatus: false` does it at no functional
cost today (nothing on the node provides device health), and the upstream fix
would be for the driver to pass `kubeletplugin.HealthService(false)`.

## Cutover runbook

This is not a merge-and-walk-away change. Switching direction has a drain
boundary in both directions (AICR): stop full-GPU claim workloads, confirm no
allocated claims remain, flip the bundle, wait for the new advertiser, then
start workloads.

The namespace move also means a fresh Helm release of gpu-operator. Its
cluster-scoped objects (CRDs, `ClusterPolicy`, `ClusterRole`s) carry
`meta.helm.sh/release-namespace: gpu-operator`, and Helm refuses to adopt them
into `nvidia-system` until the old release is uninstalled. Flux applies the new
tree before pruning the old, so the first install attempt is expected to fail
on ownership and succeed on retry after the prune. Do it in order instead:

1. Scale GPU consumers to zero. They are all in `default`: `immich`
   (`machine-learning`, `microservices`), `immich-pet-tagger`, `jellyfin`,
   `plex`, `stash`, plus `docling` and `ollama` if running. Suspend their
   HelmReleases or Kustomizations rather than editing Git.
2. `flux --context kantai.xyz suspend kustomization gpu-operator -n gpu-operator`
   then `flux delete helmrelease gpu-operator -n gpu-operator` — uninstalls the
   old release and its ClusterPolicy, taking the device plugin down. The
   suspend matters: main still holds the old tree until the merge, and an
   unsuspended Kustomization recreates the HelmRelease on its next reconcile.
   Confirm `kubectl get node kantai1 -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'`
   reads `0`. Kubelet deliberately keeps a departed device plugin's resource
   key with value 0 rather than deleting it (`kubelet_node_status/setters.go`);
   allocatable drops to 0 as soon as the plugin socket vanishes, capacity
   follows after a 5-minute grace period, and the key only disappears on a
   kubelet restart. `0` is the signal, not absence.
3. Merge; `flux reconcile kustomization cluster-apps --with-source`.
   `nvidia-system` comes up, gpu-operator installs cleanly, then
   `dra-driver-nvidia-gpu` follows once the operator HelmRelease is Ready.
4. Wait for `kubectl get resourceslices` to show a `gpu.nvidia.com` slice for
   kantai1 with `allowMultipleAllocations: true` and a `memory` capacity
   carrying a `requestPolicy`.
5. Resume the consumers. `kubectl get resourceclaims -n default` should show
   one allocated claim per pod, each with a `shareID`.
6. `kubectl delete namespace gpu-operator` — the Namespace object is
   prune-disabled by `components/common`, so Flux leaves it behind.

### Rollback

Reverse the drain boundary: stop claim workloads, confirm no allocated
`gpu.nvidia.com` claims, revert the commit, wait for the ResourceSlice to
disappear and `nvidia.com/gpu` to reappear in allocatable, then start
workloads. The 2026-09-12 revert was trivially safe only because the kubelet
plugin never published a slice; a partially-up DRA driver must be drained.

## Resolved open items from the roll-forward plan

- **Chart coordinates**: `oci://registry.k8s.io/dra-driver-nvidia/charts/dra-driver-nvidia-gpu:0.5.0`,
  `kubeVersion >=1.32.0-0`. The project moved from `NVIDIA/k8s-dra-driver-gpu`
  to `kubernetes-sigs/dra-driver-nvidia-gpu`; `ghcr.io/nvidia/k8s-dra-driver-gpu`
  only carries stale `v26.4.0-dev` tags. registry.k8s.io publishes no cosign
  signature for the chart, so no `verify` block.
- **Namespace**: `nvidia-system`, PSA `enforce: privileged`, replacing
  `gpu-operator`. The DRA kubelet plugin runs `privileged: true` containers
  with hostPath mounts of `/usr/local`, `/var/lib/kubelet/plugins*` and
  `/var/run/cdi`; `baseline` cannot admit it. Both components share the one
  privileged namespace rather than doubling the blast radius.
- **PriorityClass**: the chart's `system-node-critical` default is kept. Nothing
  on kantai restricts that class to `kube-system`, and the kubelet plugin
  should outlive workloads under node pressure.
- **konflate**: nothing needed; `helmApiVersions` already exposes the real
  capability set so the chart's `resource.k8s.io/v1` detection works offline.
