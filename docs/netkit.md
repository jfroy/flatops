# Cilium netkit datapath

Research notes and migration plan for switching kantai's pod datapath from
`veth` to Cilium [netkit](https://docs.cilium.io/en/stable/operations/performance/tuning/#netkit-device-mode).
Findings are as of 2026-10-05 (Cilium 1.20.2, Talos 1.14.1, kernel
6.18.51, Kubernetes 1.37.1, containerd 2.3.5). Re-check the upstream issues
listed under [Sources](#sources) before acting on this.

## Summary

netkit is viable on kantai **except for Kata Containers**, which is a hard
blocker on kantai1, the node that runs almost every workload:

1. **Kata cannot start pods on a netkit node.** The Talos `kata-containers`
   extension ships kata 3.32 built from the Go runtime, whose endpoint scan
   fails with `Unsupported network interface: netkit`. Upstream support is an
   unmerged PR ([kata#12279](https://github.com/kata-containers/kata-containers/pull/12279)),
   which covers `netkit-l2` only. Kata 4.0 deprecated the Go runtime, and
   runtime-rs has no netkit support. Kata on kantai1 is not negotiable right
   now (renovate jobs and the ARC runner use it), so see
   [Kata and microVM runtimes](#kata-and-microvm-runtimes).
2. **`endpointRoutes` must be disabled.** kantai runs exactly the combination
   in [cilium#47913](https://github.com/cilium/cilium/issues/47913) (open):
   netkit + `endpointRoutes.enabled` + `socketLB.hostNamespaceOnly`. Same-node
   Service replies get reclassified as new flows, which breaks network policy
   and Hubble. The `cluster-ingress-allow` CCNP puts every endpoint into
   ingress default-deny, so this would bite. `endpointRoutes` is inherited
   from the original cluster template and nothing depends on it.
   `hostNamespaceOnly` stays, because the Tailscale operator requires it.
3. **The switch cannot be done in place.** Cilium cannot convert existing veth
   pods, and veth and netkit cannot coexist on one node. With
   `rollOutCiliumPods: true`, editing `bpf.datapathMode` in Helm values
   restarts every agent at once and switches every node in place. That is
   the common factor in both earlier failures. Roll out per node with a
   `CiliumNodeConfig` and a reboot instead.

## History

| When | Commit | Stack | Outcome |
| --- | --- | --- | --- |
| 2024-08-19 | `33202cb13` → `eac589740` | Cilium 1.16.1, custom Talos 1.7.5 kernel 6.10.6 | Reverted after about 9h: "seems to block ARP" ([flatops#273](https://github.com/jfroy/flatops/issues/273)) |
| 2024-12-12 | `d75c060f9` | Cilium 1.16.4, custom Talos 1.8.3000 kernel 6.12.4 | Ran until 2025-02-24 |
| 2025-02-24 | `291219231` | Cilium 1.17.1, Talos 1.9.10004 kernel 6.13.8 | Reverted ("hack") during the homelab network re-addressing and IPv6 disable |
| 2026-08-14 | `335a98edf` | Cilium 1.20.0 | `bpf.tproxy: false`, removing the render-time blocker |

The root cause was never captured. Likely contributors, in hindsight:

- Every attempt flipped the global Helm value, so every node switched in
  place with veth pods still attached.
- The 6.10 and 6.12 kernels predate netkit scrub attributes (6.13). Without
  them, netkit + endpoint routes misclassifies policy
  ([#35060](https://github.com/cilium/cilium/issues/35060),
  [#35306](https://github.com/cilium/cilium/issues/35306)). Cilium only started
  refusing that combination in 1.20 ([#44960](https://github.com/cilium/cilium/pull/44960)).
- 1.16.0 and 1.16.1 had netkit bugs fixed in later patches: probes dropped under
  policy ([#34042](https://github.com/cilium/cilium/issues/34042)), pods stuck
  in ContainerCreating ([#33878](https://github.com/cilium/cilium/issues/33878)),
  bandwidth manager and BBR ([#34543](https://github.com/cilium/cilium/issues/34543)),
  and the host firewall blocking ARP ([#34230](https://github.com/cilium/cilium/issues/34230)).
- `bpf.tproxy: true` and the default L7 proxy were on during both attempts.
  netkit + tproxy is a known incompatibility ([#39892](https://github.com/cilium/cilium/issues/39892)).

## Upstream state

### Cilium

- Requirements: kernel ≥ 6.8 and BPF host routing. Cilium 1.20 refuses netkit
  under legacy host routing ([#44713](https://github.com/cilium/cilium/pull/44713)),
  fails agent startup on an incompatible datapath mode
  ([#42482](https://github.com/cilium/cilium/pull/42482)), refuses netkit +
  tproxy ([#44048](https://github.com/cilium/cilium/pull/44048)), and refuses
  netkit + endpoint routes without kernel scrub support.
- Modes: `netkit` (L3, recommended), `netkit-l2`, and `auto`. `auto` errors if
  existing veth pods are found, so it does not help a migration.
- netkit e2e tests now cover only the tuning-guide shape (native routing, KPR,
  BPF masquerade). Endpoint-routes and host-firewall permutations were removed
  ([#44998](https://github.com/cilium/cilium/pull/44998)).
- The upstream migration path is a per-node `CiliumNodeConfig`, then drain and
  restart the agent (or reboot) node by node
  ([per-node config](https://docs.cilium.io/en/stable/configuration/per-node-config/)).
  A CNC takes effect only when the agent restarts.

Open netkit issues and how they apply here:

| Issue | Applies? |
| --- | --- |
| [#47913](https://github.com/cilium/cilium/issues/47913) netkit + endpointRoutes + hostNamespaceOnly reclassifies Service replies | **Yes**. Disable `endpointRoutes`. The fix is under discussion in [#47914](https://github.com/cilium/cilium/pull/47914). |
| [#46553](https://github.com/cilium/cilium/issues/46553) / [#47896](https://github.com/cilium/cilium/issues/47896) L7 LB and L7 policy failures with netkit | No: `l7Proxy: false`, Envoy disabled, no L7 policy |
| [#39892](https://github.com/cilium/cilium/issues/39892) netkit + tproxy drops (`SOCKET_LOOKUP_FAILED`) | No: `bpf.tproxy: false` |
| [#49009](https://github.com/cilium/cilium/issues/49009) socket termination disabled without `CONFIG_INET_DIAG_DESTROY` | No: Talos sets it, and the agents log "Creating BPF socket destroyer" |

### Talos

- `CONFIG_NETKIT=y` and `CONFIG_INET_DIAG_DESTROY=y` on amd64 and arm64
  (`siderolabs/pkgs` `release-1.14`). 6.18 has scrub attributes and netkit
  BIG TCP.
- The 6.18 verifier regression that broke Cilium
  ([cilium#44216](https://github.com/cilium/cilium/issues/44216),
  [talos#12726](https://github.com/siderolabs/talos/issues/12726)) is fixed.
- No open Talos issues mention netkit.
  [talos#9181](https://github.com/siderolabs/talos/issues/9181) is closed, and
  [talos#10163](https://github.com/siderolabs/talos/issues/10163), the
  node-readiness report filed while testing netkit, was a hostname bug.

## Configuration review

| Area | Status |
| --- | --- |
| KPR, native routing, BPF masquerade, `ipam: kubernetes`, KubePrism | ✅ Matches the tuning guide. The agents log `enable-host-legacy-routing=false`. |
| `bpf.tproxy: false`, `l7Proxy: false`, `envoy.enabled: false` | ✅ Required for netkit |
| `endpointRoutes.enabled: true` | ❌ Must be off (#47913) |
| `socketLB.hostNamespaceOnly: true` | ✅ Keep. The Tailscale operator (and microVM runtimes) need it. |
| Bandwidth manager + BBR, BIG TCP v4/v6, `pmtuDiscovery` | ✅ Supported with netkit. A reboot recreates sockets, which BBR needs. |
| `loadBalancer.mode: dsr`, maglev, BGP control plane, LB-IPAM | ✅ No known issues. Test `externalTrafficPolicy: Local` on Envoy. |
| `localRedirectPolicies.enabled` | ✅ No LRPs exist |
| CCNP `cluster-ingress-allow` (ingress default-deny everywhere) | ⚠️ Amplifies any policy-classification bug. This is why #47913 matters. |
| Talos hostDNS, `forwardKubeDNSToHost: false` | ✅ Unchanged (disabled because of BPF masquerade, [talos#8836](https://github.com/siderolabs/talos/issues/8836)) |
| Multus macvlan (`esphome`, `kantai1-samba`) | ✅ Secondary interfaces are not touched. In L3 mode the primary `eth0` has no ARP, and macvlan does its own L2. Test it. |
| Gluetun (WireGuard, `NET_ADMIN`, iptables in the pod) | ⚠️ Expected to work. Verify that it still finds the default gateway on `eth0`. |
| Tailscale operator proxies, connector, `tailscale-dns` | ⚠️ Expected to work once endpoint routes are off. Test it. |
| hostPort (spegel 29999, plex 22400, esphome 6052) | ✅ Handled by KPR. Test image pulls through spegel. |
| buildkit, openshell, agent-sandbox | ✅ Netns or veths they create inside the pod do not depend on the outer link type |
| Kata (`kata` RuntimeClass, renovate jobs, ARC runner) | ❌ Blocker on kantai1 |
| `RuntimeClass kata` | ✅ `scheduling.nodeSelector: node.kantai.xyz/kata` (set by Talos on kantai1 and kantai3) keeps kata pods off kantai2, which has no kata extension |

Rendering chart 1.20.2 with the current values shows that the only
`cilium-config` changes are `datapath-mode` and `enable-endpoint-routes`.

## Kata and microVM runtimes

Requirement: a runtime that runs the container in a microVM on kantai1 and
works with a netkit (or netkit-l2) pod interface.

Most microVM runtimes find the CNI-created interface in the pod netns and
connect it to a tap device, so they must recognise the link type. Runtimes
that proxy sockets instead of bridging L2 do not care about the link type.

| Runtime | netkit | Notes |
| --- | --- | --- |
| Kata 3.x Go runtime (Talos extension) | ❌ | `Unsupported network interface: netkit`. Talos's `kata` handler uses Cloud Hypervisor with `internetworking_model="tcfilter"`. The `kata-qemu` handler uses QEMU. |
| Kata Go runtime + [kata#12279](https://github.com/kata-containers/kata-containers/pull/12279) | ⚠️ `netkit-l2` only | Unmerged (conflicting, awaiting review since 2026-06). It adds a netkit endpoint sharing veth's tcfilter path and rejects L3 netkit, since QEMU needs a MAC. Tested by its author with QEMU only. It changes about 600 lines, all in the host-side shim (`src/runtime/virtcontainers`) plus a guest kernel config fragment. |
| Kata runtime-rs (4.x default) | ❌ | No netkit link type. The 4.1 `l3forwarding` model targets ambient service meshes, not netkit. |
| crun + libkrun (`krun`) | ✅ (by design, untested here) | By default (TSI) the VMM proxies the guest's TCP/UDP sockets through vsock into the pod netns, so no host-side interface is bridged and the link type is irrelevant. The optional `krun.use_passt` mode is socket-based too. crun adds `/dev/kvm` to the container itself. |
| gVisor (`runsc`, Talos extension) | ✅ (by design, untested here) | Link-agnostic (AF_PACKET on the pod interface). **Not a microVM**: a userspace kernel, optionally on the KVM platform. Fallback only. |
| firecracker-containerd | ❌ | No CRI path of its own on Kubernetes; used through kata-fc. The netkit report ([#802](https://github.com/firecracker-microvm/firecracker-containerd/issues/802)) is kata's error. |
| Kuasar (vmm sandboxer) | ❌ | Its link enumeration (`vmm/sandbox/src/network/link.rs`) knows veth, tap, macvtap, ipvlan and the like, but not netkit |
| Edera | ? | Has its own hypervisor host (Xen dom0, or KVM in early access) and CRI shim. Not deployable on Talos. |
| urunc | n/a | Unikernel runtime, not general Linux containers |

### Option K1: patched Kata + `netkit-l2` (closest to today)

Backport kata#12279 onto the 3.32 Go shim in the
[`jfroy/siderolabs-extensions`](https://github.com/jfroy/siderolabs-extensions)
fork, publish the extension through the self-hosted image factory, and run
Cilium in `netkit-l2` instead of `netkit`.

- Pros: no workload changes. Keeps VM isolation and the current RuntimeClass.
  Cilium CI covers `netkit-l2`.
- Cons: carries a patch against a deprecated runtime until upstream decides.
  It was tested with QEMU, while Talos's default `kata` handler is Cloud
  Hypervisor. The PR also bumps the guest kernel config, which is probably
  only needed for its tests. L2 mode keeps Cilium answering pod ARP/NDP,
  giving up part of the L3 simplification.
- Validate on kantai3 (it has the kata extension) with a test pod pinned via
  `nodeName` before touching kantai1. Try both the `kata` (CLH) and
  `kata-qemu` handlers.

### Option K2: crun + libkrun for the kata workloads

Add a `krun` RuntimeClass backed by containerd's runc shim with
`BinaryName` pointing at a libkrun-enabled crun, and move renovate and ARC to
it. Kata can stay installed but unused.

- Pros: works with plain `netkit` (L3). Each container gets its own KVM
  microVM, and the VMM itself stays confined by the container's namespaces,
  cgroups and seccomp, whereas kata's VMM runs on the host.
- Cons:
  - **No Talos extension exists.** The official `crun` extension is the
    static `disable-systemd` binary without libkrun. A custom extension needs
    a dynamically linked crun plus `libkrun.so`, `libkrunfw.so` and their libc.
  - libkrun's [security model](https://github.com/containers/libkrun#security-model)
    treats guest and VMM as one security context, so the outer container
    sandbox is the real boundary.
  - TSI supports only TCP and UDP (no raw sockets or ICMP, no UDP listen).
  - `kubectl exec` runs in the outer container, not inside the VM
    ([crun#1098](https://github.com/containers/crun/issues/1098)).
  - Little Kubernetes production mileage.
- Validate on kantai3 with renovate first.

### Option K3: kantai1 stays on veth

Use a `CiliumNodeConfig` for netkit on kantai2 and kantai3 only. Nothing
changes for kata, but kantai1 carries about 90% of the pods, so most of the
benefit is lost. This is the fallback if K1 and K2 both fail, or the state
to hold while waiting for upstream kata.

Recommendation: prototype K1 first, since it changes the least. Keep K2 as
the path that does not depend on kata upstream.

## Migration plan

All steps that mutate live state (reboots, `talosctl apply`, `kubectl exec`
for `cilium-dbg`, `cilium connectivity test`) need explicit authorization
per [AGENTS.md](../AGENTS.md).

### Phase 0: preflight

- On every agent, run `cilium-dbg status --verbose` and check
  `Host Routing: BPF` and `Device Mode: veth`.
- Record baselines: `cilium_drop_count_total` by reason, BPF map pressure, and
  iperf for pod→pod (same node and cross-node) and LAN → `envoy-internal`.
- Choose the kata option and the target mode: `netkit` for K2 or K3,
  `netkit-l2` for K1. Prove the runtime on kantai3 first.

### Phase 1: CiliumNodeConfig (inert until an agent restarts)

`kubernetes/apps/kube-system/cilium/config/ciliumnodeconfig.yaml`, added to
that kustomization:

```yaml
apiVersion: cilium.io/v2
kind: CiliumNodeConfig
metadata:
  name: netkit
spec:
  nodeSelector:
    matchExpressions:
      - key: kubernetes.io/hostname
        operator: In
        values: ["kantai2"]
  defaults:
    datapath-mode: netkit
    enable-endpoint-routes: "false"
```

### Phase 2: per-node rollout

Order: kantai2 (arm64 VM, about 17 pods, no kata) → kantai3 → kantai1.
Convert kantai1 in a maintenance window, ideally with a `just talos upgrade`
reboot, because its GPU, ZFS and local-volume pods cannot move.

For each node:

1. Add the hostname to the CNC, push, then run
   `flux reconcile kustomization cilium-config -n kube-system --with-source`.
   Reboot immediately afterwards: any agent restart before the reboot would
   switch the node in place.
2. Run `talosctl -n <node> reboot`. Talos cordons and drains first. On boot,
   the agent's config init container applies the CNC before any pod gets
   networking.
3. Run the [checks](#checks), then soak (24–48h after the first node).

### Phase 3: make it the default

Use three separate commits. `cilium` and `cilium-config` are different Flux
Kustomizations, so combined changes would race.

1. If any node stays on veth (K3), add a CNC for it pinning the values it
   runs today (`datapath-mode: veth`, `enable-endpoint-routes: "true"`). That
   is a no-op when applied.
2. In `helm-values.yaml`, set `bpf.datapathMode` and drop `endpointRoutes`.
   The tproxy comment can lose its "sole blocker" sentence. The resulting
   agent rollout changes no node's effective config.
3. Delete the `netkit` CNC.

Optional later and as separate changes: `bpfClockProbe` from the tuning
guide. Skip `bpf.distributedLRU.enabled` while CT and NAT maxima are pinned
near upstream's floors. A distributed LRU splits each map's capacity across
CPUs (48 on kantai1), so the tuning guide pairs it with a much larger
`mapDynamicSizeRatio`, which reintroduces the RAM-scaled maps that pinning
removed.

## Checks

- `cilium-dbg status`: `Device Mode: netkit` (or `netkit-l2`) and
  `Host Routing: BPF`. `talosctl get links` shows `lxc*` as netkit.
- All pods Ready. That also exercises kubelet probes under the CCNP.
- Pod→pod and pod→ClusterIP traffic, same node and cross-node (including
  mixed veth/netkit pairs), over IPv4 and IPv6.
- `hubble observe --verdict DROPPED`: no `Policy denied` on replies, and no
  ingress flows to ephemeral destination ports (the #47913 symptom).
- LAN → `envoy-internal` and `envoy-external` LB IPs (BGP, DSR,
  `eTP: Local`). Public access through cloudflared.
- Image pulls through spegel, plex on 22400, esphome on 6052.
- Multus macvlan: esphome and SMB to `kantai1-samba` from the LAN.
- Gluetun VPN and port forwarding in qbittorrent and sabnzbd.
- Tailscale proxies, connector and `tailscale-dns`.
- Ceph `HEALTH_OK` and RBD mounts. pg18vc client connections.
- The kata (or replacement) RuntimeClass: renovate jobs and the ARC runner
  start, resolve DNS and reach GitHub.
- Drop counters, map pressure and iperf compared with the Phase 0 baseline.

## Rollback

Switching back is not in place either. Remove the node from the CNC (or
revert the Helm change once Phase 3 is done) and reboot the node.

Before rolling back, capture `cilium-dbg status --verbose`,
`hubble observe --verdict DROPPED`, `cilium-dbg monitor -t drop` and a
`cilium sysdump`, so the next attempt has a root cause.

## Sources

- Cilium tuning guide, netkit section:
  <https://docs.cilium.io/en/stable/operations/performance/tuning/#netkit-device-mode>
- Cilium per-node configuration:
  <https://docs.cilium.io/en/stable/configuration/per-node-config/>
- Open Cilium netkit issues:
  <https://github.com/cilium/cilium/issues?q=is%3Aissue+is%3Aopen+label%3Afeature%2Fnetkit>
- Kata netkit tracking issue:
  <https://github.com/kata-containers/kata-containers/issues/12159>
- Kata netkit PR: <https://github.com/kata-containers/kata-containers/pull/12279>
- Talos kata extension (3.32, Go shim, CLH + QEMU):
  <https://github.com/siderolabs/extensions/tree/release-1.14/container-runtime/kata-containers>
- libkrun: <https://github.com/containers/libkrun>
- crun krun handler:
  <https://github.com/containers/crun/blob/main/src/libcrun/handlers/krun.c>
