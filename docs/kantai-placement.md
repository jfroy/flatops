# Where kantai's critical state lives

kantai1 holds every Ceph OSD and both ZFS pools, so one node's system disk carries a
disproportionate share of the cluster's state. This is what is placed deliberately, why,
and what to check when it drifts.

## Scheduling nudges are the wrong tool

**The taint asymmetry is deliberate.** kantai2 and kantai3 set
`node-role.kubernetes.io/control-plane: PreferNoSchedule`; kantai1 does not. kantai1 is
48 CPU / 252 GiB, kantai3 is 8 threads (AMD family 23 model 17, a Zen APU) / 64 GiB,
kantai2 is 8 arm64 threads / 7890 MiB. Tainting kantai1 would push a couple of hundred
stateless pods onto hardware chosen not to run them.

**`PreferNoSchedule` barely does anything anyway.** It is a score penalty in the
scheduler's `TaintToleration` plugin, not a filter. Roughly 60% of pods here are
BestEffort, so `NodeResourcesFit` and `NodeResourcesBalancedAllocation` cannot tell the
nodes apart either — kantai1 wins on raw capacity in nearly every scoring round.

Where the only copy of the database lives should not be decided by a scheduler score that
changes when a plugin is retuned. Pin the handful of stateful things explicitly and leave
the stateless majority where it is.

Of the `openebs-hostpath` PVs on kantai1, only `database/pg18vc-*` is irreplaceable.
`default/buildkit-root-amd64` is build cache, `observability/netronome-geoip` is
re-downloadable, `observability/vmalertmanager-*` is alert state, and
`default/stash-scrapers` / `default/stash-plugins` are 100Mi each and re-fetchable.

## Ceph mons: one per node

### The quorum math

Quorum is `floor(N/2) + 1`. To survive losing node X, the mons *not* on X must be a
majority. With only two mon-eligible nodes, a majority always sits on one of them:

> **With two eligible nodes, no mon count tolerates an arbitrary single-node failure.**
> Not 3, not 5.

**Mon placement on kantai1 buys zero availability.** Every OSD is there, so a kantai1
outage takes Ceph down regardless of where the mons are. Mon placement buys only
*durability* — whether the config-key store, and with it every OSD LUKS key, survives
losing that node.

One mon per node is therefore the layout: any single node loss leaves 2 of 3.

```yaml
mon:
  count: 3
  allowMultiplePerNode: false
placement:
  mon:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: node-role.kubernetes.io/control-plane
                operator: Exists
    tolerations:
      - key: node.kantai.xyz/low-memory
        operator: Exists
        effect: NoSchedule
```

**The explicit `nodeAffinity` is required, not decorative.**
`pkg/apis/ceph.rook.io/v1/placement.go`, `Merge()` replaces `NodeAffinity` wholesale
(`if with.NodeAffinity != nil { ret.NodeAffinity = with.NodeAffinity }`), and
`GetMonPlacement` is `p.All().Merge(p[KeyMon])`. Leaving `placement.mon.nodeAffinity`
unset means mons inherit `placement.all`'s exclusions and the whole thing silently does
nothing. Tolerations are the opposite — `mergeTolerations` appends — so the `all`
tolerations survive.

### Config alone never moves an existing mon

Mon-to-node assignment is sticky and lives outside the CephCluster spec:
`rook-ceph-mon-endpoints`'s `mapping` key records a node per mon, because the mon store
is a hostPath under `dataDirHostPath`. In `mon.go`, when a mon has an entry there:

```go
} else {
	// Schedule the mon on a specific host if specified, or else allow it to be portable according to the PV
	p.PodAffinity = nil
	p.PodAntiAffinity = nil
	nodeSelector = map[string]string{k8sutil.LabelHostname(): schedule.Hostname}
}
```

A pinned mon gets a hard `nodeSelector` **and has its anti-affinity stripped**, so
flipping `allowMultiplePerNode` can never relocate it. Only a failover can:
`failoverMon()` builds `c.newMonConfig(c.maxMonID+1, zone)` — a brand-new mon id with no
`mapping` entry, so `schedule == nil`, `nodeSelector` stays nil, and the canary goes
through the real scheduler with the anti-affinity applied.

The automatic trigger is `evictMonIfMultipleOnSameNode()` in `health.go`, and its call
site is the catch:

```go
// This should be a rare event to find them on the same node, so we just need to check
// once per operator restart.
if needToCheckMonsOnSameNode {
	needToCheckMonsOnSameNode = false
	return c.evictMonIfMultipleOnSameNode()
}
```

`needToCheckMonsOnSameNode` is a package-level var set true at process start. An operator
already running when the spec changed consumed it long ago — and at that time the
function returned immediately, since its first line is
`if c.spec.Mon.AllowMultiplePerNode { return nil }`.

**So: restart `rook-ceph-operator`.** It `return`s on the first collision it finds, so it
evicts exactly **one mon per operator start** — three co-located mons need two restarts.
Wait for `ceph -s` to show three mons in quorum before the next one: `failoverMon` scales
the old mon down *before* starting the replacement, so you sit at 2 of 3 — quorum with no
margin — for the duration of each.

`mgr` has none of this. No hostPath store, so mgr deployments carry no `nodeSelector` and
`allowMultiplePerNode: false` takes effect on the next reconcile. Their required
`podAntiAffinity` plus kantai2's `node.kantai.xyz/low-memory:NoSchedule` taint pins one to
kantai1 and one to kantai3.

### The mon on kantai2

**Cost.** The HelmRelease overrides only `resources.osd`, so mon keeps the chart default:
requests 1 CPU / 1 GiB, limit 2 GiB. The cheapest Ceph daemon.

**Performance is not a concern.** Mons are off the client data path — a RADOS client
fetches the osdmap and then talks to OSDs directly, while mons serve maps, auth and the
config-key store. A slow mon costs map-propagation and connection-setup latency, not
throughput. arm64 is fine; `quay.io/ceph/ceph` is multi-arch.

**Memory is the real constraint**, and the reason `talos/node/kantai2/10-node.yaml` caps
`kube-apiserver`: unbounded, it is the kernel's preferred OOM victim on that node, and a
1 GiB mon on top makes it worse. See project memory `kantai2-capacity`.

The mon toleration is Flux-managed while the `node.kantai.xyz/low-memory` taint is
Talos-managed, so the toleration has to land and be visible on the mon deployments before
anything depends on it.

## The Postgres replica on kantai3

`pg18vc` runs two instances, one on kantai1 and one on kantai3, so a kantai1 rebuild is a
failover rather than a point-in-time restore from the nightly R2 export.

Four facts make this cheaper than it looks:

**Replication is asynchronous.** `spec.postgresql.synchronous` is unset and CNPG defaults
to async, so a slow replica does not slow the primary — it lags. That is the whole answer
to the CPU worry about kantai3.

**The primary does not wander.** `spec.primaryUpdateMethod` is unset and the CNPG default
is `restart`, not `switchover` — the operator restarts the primary in place during a
rolling update. Only a genuine failover moves it. Leave it that way: with `switchover`
every image bump would hand the primary to the slow node.

**Nothing reads from the replica.** Every consumer in the repo connects to `pg18vc-rw`;
nothing uses `pg18vc-ro` or `pg18vc-r`. The kantai3 instance serves zero client queries,
and its only job is WAL replay.

**It fits.** `openebs-hostpath` is `WaitForFirstConsumer`, so the PV is created wherever
the pod lands — no pre-binding problem. kantai3 has ~58 GiB allocatable memory against a
12Gi request and ~897 GB allocatable ephemeral-storage against a 100Gi volume.

### Why not Ceph instead

`ceph-block` would be network-attached and survive a node reboot, which sounds like the
obvious fix. It is the wrong one: every OSD is on kantai1, so a kantai1 rebuild takes Ceph
down with it and the PVC becomes unreachable at exactly the moment you need it. Two
node-local copies on two different nodes beat one network copy whose storage lives on the
node being rebuilt.

### The spec

In `kubernetes/apps/database/cnpg/pg18vc/cluster.yaml`:

```yaml
affinity:
  enablePodAntiAffinity: true
  podAntiAffinityType: required
  topologyKey: kubernetes.io/hostname
  nodeSelector:
    kubernetes.io/arch: amd64
```

`required` rather than `preferred`, because `preferred` would let both instances land on
kantai1 again — the situation this exists to prevent.

The `nodeSelector` is the constraint that is actually true rather than a list of hostnames
that happen to satisfy it today. The `timescaledb` and `timescaledb_toolkit` extension
images under `registry.kantai.xyz` are amd64-only, so this cluster cannot run on kantai2
at all; the memory shortfall is the lesser problem. kantai2's taint keeps it out today,
but that is incidental — architecture keeps holding if the taint is relaxed, and an amd64
node added later needs no edit here.

### Config alone never moves an existing instance either

A bound PVC is node-affine and beats any affinity rule, so the anti-affinity is
effectively a **one-shot placement decision taken when a PVC is first created**. An
instance only moves once CNPG is forced to create a new one:

```fish
# confirm instance 1 is primary first -- you are about to destroy the other one
kubectl -n database get cluster pg18vc -o jsonpath='{.status.currentPrimary}'
kubectl cnpg destroy pg18vc 2
```

The old PV is reclaimed automatically — `openebs-hostpath` is `reclaimPolicy: Delete`.
The same command is the recovery path after a node rebuild: `kubectl cnpg destroy pg18vc 1`
re-clones the kantai1 instance from the live primary, and
`kubectl cnpg promote pg18vc pg18vc-1` hands the role back.

### What it costs

- **During a kantai1 outage the primary is on kantai3**, and every app talking to
  `pg18vc-rw` gets the slow node. Expected path, not an exception.
- **WAL archiving breaks while Ceph is down.** `ObjectStore pg18vc-local-ceph` points at
  `https://s3.kantai.xyz`, the RGW on kantai1's own OSDs, so `archive_command` fails and
  Postgres retains WAL in `pg_wal` on the promoted primary. Fine for a short window; watch
  free space on kantai3's volume if it drags. `ContinuousArchiving: False` during the
  window is expected, not a new fault.
- **`required` anti-affinity means an instance stays Pending while a node is down.**
  Acceptable — `enablePDB: false` and tuppr runs with `drain.enabled: false`, so nothing
  is trying to evict these pods — but it does mean no replica during a kantai3 outage.

## What stays where it is

- **OSDs.** Bound to kantai1 by hardware. Nothing to do short of buying disks for another
  node — which would also be what finally makes `failureDomain: host` meaningful.
- **RGWs.** Useless without OSDs, so moving them protects nothing.
- **buildkit, netronome-geoip, vmalertmanager, stash-\*.** Rebuildable; not worth the
  placement complexity.
- **kantai1's missing `PreferNoSchedule` taint.** Intentional.
- **`dataDirHostPath` on EPHEMERAL**, and the pg18vc WAL archive targeting the RGW on
  kantai1's own OSDs. Both reviewed and accepted rather than fixed; reasoning and expected
  symptoms in [kantai1-rebuild.md](kantai1-rebuild.md).
