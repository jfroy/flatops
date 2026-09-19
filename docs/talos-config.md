# The Talos patch tree

`talos/` is a topf patch tree in the Talos 1.14 multi-document format. This is how it is
laid out, how to check a change before applying it, and the things that bite.

## Layout

A kind that can only appear once is grouped with its neighbours; a repeatable (named) kind
gets its own file; a templated document gets its own file. Filename prefixes go in tens.

```txt
all/
  10-machine.yaml              # certSANs, UnattendedInstallConfig, KubeAPIServerConfig,
                               #   ResolverConfig, FilesystemScrubConfig, SysctlConfig
  20-hostname.yaml.tpl         # templated
  30-cluster-network.yaml.tpl  # templated
  40-discovery.yaml            # DiscoveryServiceConfig — named
  50-kubelet.yaml.tpl          # templated
  60-containerd.yaml           # CRICustomizationConfig — named, one doc per concern
  70-volumes.yaml              # UserVolumeConfig — named
control-plane/
  10-control-plane.yaml        # cluster.etcd + every Kube*Config singleton
node/<host>/
  10-node.yaml                 # UnattendedInstallConfig, KubeNodeConfig, SysctlConfig
  20-network.yaml              # DHCPv4Config + LinkConfig — named
  30-kernel-modules.yaml       # KernelModuleConfig — named
  40-volumes.yaml              # VolumeConfig — named (kantai1 only)
```

Unnamed singletons merge rather than collide — both across `all/`, `control-plane/` and
`node/<host>/`, and between two documents of the same kind inside one file. Grouping is
therefore free; duplicates are folded into one document per kind for readability.

## Checking a change before applying it

topf regenerates the machine config from scratch on every apply, against the node's
**running** Talos version. That whole pipeline can be replayed offline:

```sh
talosctl gen config kantai https://k8s.kantai.xyz:6443 --output-types controlplane -o cp.yaml
talosctl machineconfig patch cp.yaml -p @<each patch, in topf order> -o merged.yaml
talosctl validate -m metal -c merged.yaml
```

Two rules:

- **Render the templates with the real values.** A stand-in v6 pod CIDR wider than `/64`
  hides the `nodeCIDRMaskSizeIPv6` error below completely.
- **For a refactor, diff the merged output before and after**, rather than asserting the
  change is behaviour-neutral.

Then `task talos:render`, `talosctl validate --mode metal` against `talos/output`, and
`task talos:diff` before `task talos:apply`. `talos/output` holds plaintext secrets —
delete it when done.

## Traps

**`node-cidr-mask-size-ipv6` is not an extraArg.** `KubeNetworkConfig` has
`nodeCIDRMaskSizeIPv4` / `nodeCIDRMaskSizeIPv6` as real fields and validates `podSubnets`
against them. The IPv6 default is `/64`, so the cluster's `/100` v6 pod subnet is rejected
outright:

```
KubeNetworkConfig: pod subnets: invalid subnet: <cidr>/100 is smaller than the per-node pod CIDR mask size /64
```

`nodeCIDRMaskSizeIPv6: 108` lives on the document; there is no controller-manager flag.
The IPv4 default `/24` already matches what the cluster allocates.

**The `--oidc-*` kube-apiserver flags are rejected**, not deprecated:
`kube-apiserver extra argument "oidc-client-id" is not allowed: use KubeAuthenticationConfig`.
They live in structured `KubeAuthenticationConfig.configuration.jwt`, and
`claimMappings.username.prefix: ""` is load-bearing — kube-apiserver only defaults the
username prefix to `<issuer-url>#` when the claim is something other than `email`, and
this cluster's claim *is* `email`. Omitting `prefix` renames every OIDC subject and breaks
their RBAC bindings.

**`CRICustomizationConfig` rejects `name: customization`** — reserved for the legacy
`machine.files` path. Documents are merged lexicographically by name, so `60-containerd.yaml`
uses one per concern: `30-buildkit`, `30-cdi`, `30-spegel`, `30-unprivileged`.

**`cluster.allowSchedulingOnControlPlanes` has no document form.** The generator emits
`KubeNodeConfig.taints: {node-role.kubernetes.io/control-plane: NoSchedule}`, so allowing
scheduling is a per-node edit of that key — kantai1 `$patch: delete`s it, kantai2 and
kantai3 override it to `PreferNoSchedule`. Per node rather than once in `control-plane/`,
so the result does not depend on the order topf applies patches in.

**Taint value syntax.** `labels.ParseTaint` (`pkg/machinery/labels/taints.go`) is
`strings.Cut(s, ":")`, and with no colon the whole string becomes the *effect* with an
empty value. `PreferNoSchedule` and `:PreferNoSchedule` are identical; the generator
writes the bare form.

**The generator emits the exclude-from-LB label** under `KubeNodeConfig.labels`, not
`machine.nodeLabels`. A `$patch: delete` aimed at the old path fails with `lookup failed`.

**A whole generated document is removed with a document-level `$patch: delete`** — that is
how `KubeFlannelCNIConfig` is dropped, since omitting it is not enough.

**Disk selectors are CEL over `block.DiskSpec`** — `disk.wwid == "eui...."` for kantai1 and
kantai3, `disk.size > 50u * GB` for kantai2. Fields come from the DiskSpec proto (`size`,
`model`, `serial`, `wwid`, `transport`, `rotational`, `dev_path`, `uuid`, …); `system_disk`
and the unit constants `GB`/`GiB`/… are also in scope. Re-verify a wwid against
`talosctl -n <node> get disks -o yaml` whenever a disk is replaced — a miss selects a
different disk.

## Generator defaults not in the tree

topf regenerates from scratch, so these arrive without appearing in `talos/`:
`SecurityProfileConfig{workloadIsolation: true}`, `FilesystemTrimConfig{interval: 168h}`,
`VolumeConfig EPHEMERAL{mount.secure: true}`, `DiscoveryIdentityConfig`.

`workloadIsolation` puts containerd, the kubelet and pods in a dedicated PID/mount
namespace, which means the kubelet cannot reach host daemons and the **in-tree** iSCSI
volume plugin does not work. This cluster is CSI-only (Ceph RBD, openebs ZFS, SMB), so
nothing should be affected — but it is the first thing to check if a volume starts failing
to mount.

## Deliberate 1.14 choices

**`FilesystemScrubConfig: 168h`** in `all/10-machine.yaml`. `xfs_scrub` over the XFS system
volumes; off entirely unless the document is present. Talos derives a stable per-node,
per-volume time inside the interval so the three nodes do not scrub together.

**`encryption.allowDiscards: true`** on kantai1's `EPHEMERAL` and `STATE`. TRIM does not
cross a LUKS2 mapping without it, so the generator's `FilesystemTrimConfig` would be a
no-op on the only node with an encrypted system disk. The cost is the standard cryptsetup
information leak — used sectors become visible on the raw device. It applies at volume
open, so a reboot rather than a reformat.

**etcd metrics stay on 2381.** 1.14 moved the *default* to 2383, which is
`listen-client-http-urls` — a client listener, so it inherits `client-cert-auth: true`,
the etcd PKI and `tls-min-version: TLS1.3`, all of which are in Talos's etcd deny list and
cannot be overridden. Scraping it would need a client certificate signed by the etcd CA
rather than the bearer token vmagent uses. `listen-metrics-urls` is deliberately *not*
deny-listed — it is the supported escape hatch for an unauthenticated metrics-only
listener — and kube-prometheus-stack's `kubeEtcd` scrape defaults to 2381 as well, so both
ends already agree.

**Inherited without config change:** TLS 1.3 minimum on etcd and kube-apiserver, NRI
enabled by default, `send_redirects` off by default, Secure Boot `lockdown=integrity`
instead of `confidentiality`, XFS minimum allocation group 64 GiB.

**Not adopted:** dedicated system volumes (`ETCD`, `CRI`, `KUBELET`, `LOG` on their own
partitions). Worth revisiting — an `ETCD` volume separate from `EPHEMERAL` would survive a
kantai1 system-disk rebuild. The matching hazard is that once adopted, resetting
`EPHEMERAL` no longer clears etcd and the `ETCD` volume must be wiped explicitly.
