# Updating Mellanox/NVIDIA NIC firmware on a Talos node

How to update firmware on a Mellanox/NVIDIA ConnectX NIC on a Talos node that boots
with SecureBoot enabled. Talos has no shell, no package manager, and no SSH, and
SecureBoot forces kernel lockdown, which rules out the usual `mstflint`/`mlxup` tools
entirely (see below). The working path is the kernel's `devlink` interface instead.

## Why the obvious tools don't work

Talos has no shell, so the standard first move is a privileged pod pinned to the
target node with `/sys` (and `/dev`) hostPath-mounted, matching the existing pattern
in `kubernetes/apps/kube-system/cpufreq/kantai1/helmrelease.yaml` and
`kubernetes/apps/storage/maintenance/kantai1/cronjob.yaml`. That gets a pod that
*can* run `mstflint`/`mlxup`, but both fail:

```
-E- No devices found or specified, mst might be stopped, run 'mst start' to load MST modules
```

Debian's `mstflint` package has no `mst` script (only ships in NVIDIA's full MFT
tarball), but even the real thing doesn't help -- querying the device directly fails
with `Cannot open Device ... MFE_CR_ERROR`, and dmesg on the node shows why:

```
kern: notice: Lockdown: mstflint: direct PCI access is restricted; see man kernel_lockdown.7
```

**Kernel lockdown**, forced to `integrity` mode by SecureBoot, blocks raw PCI BAR /
`/dev/mem` access outright, independent of the pod's privileges, capabilities, or
AppArmor state. If the target node relies on SecureBoot for TPM-sealed disk
encryption (`checkSecurebootStatusOnEnroll: true` in its Talos config),
**disabling SecureBoot to work around this is not an option** -- it breaks that
enrollment. Check the node's own `talos/node/<host>/*.yaml` before assuming
otherwise; see `docs/kantai1-rebuild.md` §7 for a worked example of how that's wired
up.

mstflint/mlxup's raw-PCI approach is a dead end on any node with lockdown active.
Update through the kernel driver instead.

## The working path: `devlink dev flash`

`mlx5_core` supports firmware flashing via `devlink`, which goes through the
driver's own regulated firmware-loading path (`request_firmware()`) rather than
mmap'ing the BAR directly. It's compatible with lockdown and needs no `mst`/MFT
kernel module.

### 1. Deploy a debug pod pinned to the node

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: nic-fw-<node>
  namespace: kube-system
spec:
  nodeName: <node>
  hostNetwork: true # devlink's netlink socket needs the host's netns; physical NICs never leave init netns
  restartPolicy: Never
  containers:
    - name: nic-fw
      image: debian:bookworm-slim
      command: ["sleep", "infinity"]
      securityContext:
        privileged: true
        runAsUser: 0
      volumeMounts:
        - mountPath: /sys
          name: sys
        - mountPath: /host-fw
          name: hostfw
        - mountPath: /sys/module/firmware_class/parameters/path
          name: fw-search-path
  volumes:
    - hostPath:
        path: /sys
        type: Directory
      name: sys
    - hostPath:
        path: /run/nic-fw
        type: DirectoryOrCreate
      name: hostfw
    - hostPath:
        path: /sys/module/firmware_class/parameters/path
        type: ""
      name: fw-search-path
```

`kube-system` is labelled `pod-security.kubernetes.io/enforce: privileged`
(`kubernetes/apps/kube-system/namespace.yaml`), so this pod is exempt from the
baseline VAP: `hostNetwork`, `privileged`, and the `/sys` hostPath are all fine there.

```sh
kubectl apply -f nic-fw-pod.yaml
kubectl exec -it -n kube-system nic-fw-<node> -- bash -c \
  'apt-get update -qq && apt-get install -y -qq iproute2'   # provides the devlink binary
```

### 2. Find the card and its current firmware

List Mellanox PCI functions on the node by vendor ID (`15b3`):

```sh
kubectl exec -it -n kube-system nic-fw-<node> -- bash -c '
  for d in /sys/bus/pci/devices/*; do grep -q 0x15b3 "$d/vendor" 2>/dev/null && basename "$d"; done
'
```

That prints one PCI address (BDF) per function -- a dual-port card shows two. Query
each with `devlink dev info`:

```sh
kubectl exec -it -n kube-system nic-fw-<node> -- devlink dev info pci/<bdf>
```

This reports `driver`, `versions.fixed.fw.psid` (needed to find matching firmware),
and `versions.running`/`stored.fw.version` (current firmware). A dual-port card's two
functions normally report the same PSID and version; see step 4 for flashing both.

### 3. Get the firmware, in MFA2 format

`devlink dev flash` / the in-kernel `mlxfw` driver only accepts the **MFA2** packaged
format, not the raw `.bin` from the legacy `mellanox.com/downloads/firmware` page
(the format `mstflint`/`mlxup` use for a direct burn):

```
Error: mlxfw: Firmware flash failed: Firmware file is not MFA2.
```

1. Download the raw `.bin` matching the PSID from step 2, from NVIDIA's PSID-matched
   firmware page (`network.nvidia.com/support/firmware/`) or the legacy per-OPN page --
   either works as `mlxarchive`'s input.
2. Package it as MFA2 with `mlxarchive`, using `containers/Dockerfile.mlxarchive`.

   ```sh
   docker build -t mlxarchive-builder -f containers/Dockerfile.mlxarchive containers/
   mkdir -p bins out && cp <downloaded-fw>.bin bins/fw.bin
   docker run --rm -v "$PWD/bins:/work/bins" -v "$PWD/out:/work/out" mlxarchive-builder \
     -v 1.1.1 --bins-dir /work/bins --out-file /work/out/fw.mfa2
   ```

### 4. Flash and activate

`devlink dev flash`'s `file` argument isn't a path the calling process reads. The
kernel driver receives a *name* over netlink and does its own `request_firmware()`
lookup on the host root filesystem's firmware search path (`/lib/firmware/` by
default, immutable on Talos). A path like `/tmp/fw.mfa2` inside the pod fails with
`failed to locate the requested firmware file` even though the file is clearly there:
it's the wrong filesystem, not a permissions problem. However we can register a writable
host directory as an extra search path via `/sys/module/firmware_class/parameters/path`,
put the file there, and flash by bare filename:

```sh
kubectl cp ./out/fw.mfa2 kube-system/nic-fw-<node>:/host-fw/fw.mfa2
kubectl exec -it -n kube-system nic-fw-<node> -- bash -c '
  echo -n /run/nic-fw > /sys/module/firmware_class/parameters/path
  devlink dev flash pci/<bdf> file fw.mfa2
'
```

A dual-port card shares one physical flash across both functions -- flash once, via
either BDF. Confirm the burn on both:

```sh
kubectl exec -it -n kube-system nic-fw-<node> -- devlink dev info pci/<bdf-port-0>
kubectl exec -it -n kube-system nic-fw-<node> -- devlink dev info pci/<bdf-port-1>
```

`stored.fw.version` should show the new version on both; `running.fw.version` stays
on the old one until activated. Activate in-band:

```sh
kubectl exec -it -n kube-system nic-fw-<node> -- devlink dev reload pci/<bdf> action fw_activate
```

This does a live PCI-level reset of the device, with no reboot needed. If the pod's
`hostNetwork: true` session (or the node's own network path) rides this same NIC, the
`kubectl exec` stream and the node's reachability both drop for the duration of the
reset and link renegotiation (tens of seconds for a ConnectX-5, not minutes). That's
expected, not a failure; give it a minute before treating it as stuck. To check node
status in the meantime, use a path that doesn't depend on this NIC: `kubectl get node
<node> -o wide` against a control-plane/gateway endpoint other than the node itself,
or the node's BMC/Redfish console if it has one documented (see
`docs/kantai1-rebuild.md` §7 for an example).

Once `reload_actions_performed: driver_reinit fw_activate` comes back,
`devlink dev info` should show `running` == `stored` on both functions.

**Do this with the node cordoned and Ceph `noout`/`norebalance` set beforehand** if
it's carrying OSD traffic over this NIC. A short enough flap shouldn't cross Ceph's
`mon_osd_down_out_interval` (10 min default) and trigger a rebalance, but don't rely
on that; check `ceph -s` after either way.

### 5. Cleanup

```sh
kubectl delete pod -n kube-system nic-fw-<node>
```

`/run/nic-fw` on the node is tmpfs and clears on the next reboot regardless -- no
need to clean it up by hand.
