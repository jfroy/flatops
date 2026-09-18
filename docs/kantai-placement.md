# Spreading critical state off kantai1

kantai1 holds every Ceph OSD, every mon, both `pg18vc` instances and every
`openebs-hostpath` PV that matters. The [rebuild](kantai1-rebuild.md) makes that
concrete: one node's system disk takes all of it. This is what can actually be moved,
and what cannot.

Same conventions as the other docs: **Verified** means checked against source at the
pinned versions or read from the live cluster (2026-09-16); **Check** means it still
needs a look.

## 1. Scheduling nudges will not fix this

The instinct is to make workloads "naturally" spread. That does not get you what you
want here, for three reasons.

**The taint asymmetry is deliberate.** `talos/node/kantai2/35-node-taints.yaml` and
`talos/node/kantai3/35-node-taints.yaml` both set
`node-role.kubernetes.io/control-plane: :PreferNoSchedule`; kantai1 has no
`35-node-taints.yaml` at all. That is a design choice, and a defensible one — kantai1
is 48 CPU / 252 GiB, kantai3 is 8 threads (AMD family 23 model 17, a Zen APU) / 64 GiB,
kantai2 is 8 arm64 threads / 7890 MiB. Removing it would push a couple of hundred
stateless pods onto hardware chosen not to run them.

**`PreferNoSchedule` barely does anything anyway.** It is a score penalty in the
scheduler's `TaintToleration` plugin, not a filter. And roughly 60% of pods here are
BestEffort (see the descheduler notes in project memory `kantai2-capacity`), so
`NodeResourcesFit` and `NodeResourcesBalancedAllocation` cannot tell the nodes apart
either — kantai1 wins on raw capacity in nearly every scoring round.

**Most importantly, it is the wrong tool.** "Where does the only copy of the database
live" should not be decided by a scheduler score that can change when a plugin is
retuned. Pin the handful of stateful things explicitly and leave the stateless
majority exactly where it is.

Of the `openebs-hostpath` PVs on kantai1, only one set is genuinely critical:

| claim | verdict |
| --- | --- |
| `database/pg18vc-1`, `database/pg18vc-2` | **irreplaceable** — §3 |
| `default/buildkit-root-amd64` | build cache, refills |
| `observability/netronome-geoip` | re-downloadable |
| `observability/vmalertmanager-kantai-…-0` | alert state, disposable |
| `default/stash-scrapers`, `default/stash-plugins` | 100Mi each, re-fetchable |

## 2. Ceph mons and the quorum question

### The math

Quorum is `floor(N/2) + 1`. To survive losing node X, the mons *not* on X must be a
majority. With only two mon-eligible nodes, a majority always sits on one of them. So:

> **With two eligible nodes, no mon count tolerates an arbitrary single-node
> failure.** Not 3, not 5.

Enumerated, with eligible = {kantai1, kantai3}:

| layout | quorum | lose kantai1 | lose kantai3 |
| --- | --- | --- | --- |
| 3 / 0 (today, but on kantai1) | 2 | dead, **no store survives** | fine |
| 0 / 3 (all on kantai3) | 2 | fine | dead, no store survives |
| 1 / 2 (majority on kantai3) | 2 | fine | dead, but kantai1's store survives |
| 2 / 1 | 2 | dead, kantai3's store survives | fine |
| 5 across two nodes | 3 | same shape | same shape |

### The observation that settles it

**Mon placement on kantai1 buys zero availability.** Every OSD is on kantai1, so a
kantai1 outage takes Ceph down regardless of where the mons are. Mon placement only
buys *durability* — whether the config-key store (and therefore the OSD LUKS keys,
see [kantai1-rebuild.md](kantai1-rebuild.md) §5) survives losing that node.

So there is no argument for a mon majority on kantai1. The current 3/0 layout is the
worst cell in the table.

### Two-node answer: better, still not tolerant

`1 / 2` skewed toward kantai3 is the best two-node layout — losing kantai1 keeps
quorum, and losing kantai3 at least leaves a recoverable store on kantai1. Two
caveats:

- You cannot deterministically ask Rook for 1/2. With `allowMultiplePerNode: true`
  the scheduler decides. `placement.mon.topologySpreadConstraints` with `maxSkew: 1`
  over `kubernetes.io/hostname` forces a 2/1 split but not *which* node gets two, and
  kantai1 is the node with no scheduling penalty. A preferred `nodeAffinity` toward
  kantai3 makes the right split likely, not certain. **Check** the result with
  `ceph mon dump` after any such change rather than assuming.
- kantai3 reboots on every Talos upgrade, and with the majority there, quorum drops
  for the duration.

### Three-node answer: actually tolerant

One mon per node. Any single node loss leaves 2 of 3.

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

`allowMultiplePerNode: false` makes Rook require unique nodes, so with three eligible
nodes you get exactly one each.

### Changing this does not move existing mons

**Verified, Rook v1.20.7.** Mon-to-node assignment is sticky and lives outside the
CephCluster spec: `rook-ceph-mon-endpoints`'s `mapping` key records a node per mon,
because the mon store is a hostPath under `dataDirHostPath`. In `mon.go`, when a mon
has an entry there:

```go
} else {
	// Schedule the mon on a specific host if specified, or else allow it to be portable according to the PV
	p.PodAffinity = nil
	p.PodAntiAffinity = nil
	nodeSelector = map[string]string{k8sutil.LabelHostname(): schedule.Hostname}
}
```

So a pinned mon gets a hard `nodeSelector` **and has its anti-affinity stripped** —
flipping `allowMultiplePerNode` can never relocate it. Only a failover can:
`failoverMon()` builds `c.newMonConfig(c.maxMonID+1, zone)`, a brand-new mon id with
no `mapping` entry, so `schedule == nil`, `nodeSelector` stays nil, and the canary
goes through the real scheduler with the anti-affinity applied.

The automatic trigger for that is `evictMonIfMultipleOnSameNode()` in `health.go` —
but read its call site:

```go
// This should be a rare event to find them on the same node, so we just need to check
// once per operator restart.
if needToCheckMonsOnSameNode {
	needToCheckMonsOnSameNode = false
	return c.evictMonIfMultipleOnSameNode()
}
```

`needToCheckMonsOnSameNode` is a package-level var set true at process start. An
operator that was already running when you flipped `allowMultiplePerNode` consumed it
long ago — and at that time the function returned immediately, because its first line
is `if c.spec.Mon.AllowMultiplePerNode { return nil }`.

**So: restart `rook-ceph-operator` after the change.** The function `return`s on the
first collision it finds, so it evicts exactly **one mon per operator start** — three
co-located mons need two restarts. Wait for the failover to finish and `ceph -s` to
show three mons in quorum before the next one: `failoverMon` scales the old mon down
*before* starting the replacement, so you sit at 2 of 3 — quorum with no margin — for
the duration of each.

`mgr` has none of this. It keeps no hostPath store, so mgr deployments carry no
`nodeSelector` and `allowMultiplePerNode: false` takes effect on the next reconcile.

**Verified — the explicit `nodeAffinity` is required, not decorative.**
`pkg/apis/ceph.rook.io/v1/placement.go`, `Merge()` replaces `NodeAffinity` wholesale
(`if with.NodeAffinity != nil { ret.NodeAffinity = with.NodeAffinity }`), and
`GetMonPlacement` is `p.All().Merge(p[KeyMon])`. Leaving `placement.mon.nodeAffinity`
unset means mons inherit `placement.all`'s `kantai2 NotIn` exclusion and the whole
change silently does nothing. Tolerations are the opposite — `mergeTolerations`
appends — so the `all` tolerations survive.

### Is a mon on kantai2 safe?

**Cost.** The HelmRelease overrides only `resources.osd`, so mon keeps the chart
default: requests 1 CPU / 1 GiB, limit 2 GiB. It is the cheapest Ceph daemon.

**Performance — not a concern.** Mons are off the client data path. A RADOS client
fetches the osdmap and then talks to OSDs directly; mons serve maps, auth and the
config-key store. A slow mon costs map-propagation and connection-setup latency, not
throughput. arm64 is fine, `quay.io/ceph/ceph` is multi-arch.

**Memory — the real prerequisite.** kantai2's `kube-apiserver` has a 512 MiB request
and no limit against a 1.5–2.5 GiB working set, and is already the kernel's preferred
OOM victim there. Adding a 1 GiB mon before capping the apiserver makes an existing
problem worse. **Cap the apiserver first** — that is already the outstanding fix in
project memory `kantai2-capacity`.

**Ordering.** Tolerations are Flux-managed, the `node.kantai.xyz/low-memory` taint is
Talos-managed. Land the toleration and verify it on the mon deployments before
anything else, per the same note.

## 3. A Postgres replica on kantai3

The goal is narrow: **a kantai1 rebuild should not require a restore.** Today both
`pg18vc` instances live on kantai1's ephemeral partition, so wiping it means going to
the nightly R2 export and a point-in-time recovery — a one-day RPO and an hour of
nerves for what is otherwise routine maintenance. A streaming replica on kantai3 is a
second live copy of the data directory; the rebuild becomes a failover.

Three facts make this much cheaper than it looks.

**Replication is asynchronous.** `spec.postgresql.synchronous` is unset on `pg18vc`,
and CNPG defaults to async. A slow replica does not slow the primary — it just lags.
This is the whole answer to the CPU worry about kantai3.

**The primary does not wander.** `spec.primaryUpdateMethod` is unset, and the CNPG
default is `restart`, not `switchover` — the operator restarts the primary in place
during a rolling update. Only a genuine failover moves it. Leave it that way: with
`switchover` every image bump would hand the primary to the slow node.

**Nothing reads from the replica.** Every consumer in the repo — autobrr, prowlarr,
kite, netronome, gatus-sidecar and the rest — connects to `pg18vc-rw`. No app uses
`pg18vc-ro` or `pg18vc-r`. So the kantai3 instance serves zero client queries; its
only job is WAL replay.

**It fits.** `openebs-hostpath` is `WaitForFirstConsumer`, so the PV is created
wherever the pod lands — no pre-binding problem. kantai3 has 58 GiB allocatable memory
(against a 12Gi request) and ~897 GB allocatable ephemeral-storage (against a 100Gi
volume).

### Why not put it on Ceph instead

`ceph-block` would be network-attached and survive a node reboot, which sounds like
the obvious fix. It is the wrong one here: every OSD is on kantai1, so a kantai1
rebuild takes Ceph down with it and the PVC becomes unreachable at exactly the moment
you need it. Two node-local copies on two different nodes are strictly better than one
network copy whose storage lives on the node being rebuilt.

### The change

Landed in `kubernetes/apps/database/cnpg/pg18vc/cluster.yaml`, replacing the
`nodeSelector: kantai1` pin:

```yaml
affinity:
  enablePodAntiAffinity: true
  podAntiAffinityType: required
  topologyKey: kubernetes.io/hostname
  nodeSelector:
    kubernetes.io/arch: amd64
```

`required` rather than `preferred` because `preferred` would let both instances land
on kantai1 again, which is the situation this exists to prevent.

The `nodeSelector` is the constraint that is actually true, rather than a list of
hostnames that happen to satisfy it today. The `timescaledb` and
`timescaledb_toolkit` extension images under `registry.kantai.xyz` are amd64-only, so
this cluster cannot run on kantai2 at all — the memory shortfall (10 GiB against a
12Gi request) is the lesser problem. kantai2's `node.kantai.xyz/low-memory:NoSchedule`
taint keeps it out today, but that is incidental; architecture keeps holding if the
taint is relaxed, and an amd64 node added later needs no edit here.

Node labels today: kantai1 `amd64`, kantai2 `arm64`, kantai3 `amd64` — so the two
eligible nodes are kantai1 and kantai3, and `required` anti-affinity puts one instance
on each.

### Applying it — done 2026-09-17, and the config alone was not enough

Merging the spec moved nothing, and this is the part worth remembering. Both existing
PVs were node-affine to kantai1, and a bound PVC beats any affinity rule — the
anti-affinity is effectively a **one-shot placement decision taken when a PVC is first
created**. The replica only moved once CNPG was forced to create a new one:

```fish
# confirm instance 1 is primary first -- you are about to destroy the other one
kubectl -n database get cluster pg18vc -o jsonpath='{.status.currentPrimary}'

# delete instance 2's pod and PVC; CNPG re-creates it, and with no bound PVC the
# anti-affinity now forces it onto kantai3
kubectl cnpg destroy pg18vc 2
```

Result, verified live: `pg18vc-1` primary on kantai1 (PV `pvc-534ba97b`), `pg18vc-2`
replica on kantai3 (PV `pvc-7a3d6453`, freshly provisioned by the re-clone), both on
timeline 3, `ContinuousArchiving: True`, last backup succeeded 2026-09-17T00:06:55Z.
The old kantai1-affine `pvc-fbdfaced` was reclaimed automatically —
`openebs-hostpath` is `reclaimPolicy: Delete`.

The same command is the recovery path after a node rebuild: `kubectl cnpg destroy
pg18vc 1` re-clones the kantai1 instance from the live primary, and
`kubectl cnpg promote pg18vc pg18vc-1` hands the role back.

### What this costs, stated plainly

- **During a kantai1 outage the primary is on kantai3**, and every app talking to
  `pg18vc-rw` gets the slow node. That is now the expected path rather than an
  exception. Promote back with `kubectl cnpg promote pg18vc pg18vc-1` once kantai1 is
  healthy.
- **WAL archiving breaks while Ceph is down.** The `pg18vc-local-ceph` ObjectStore
  points at `https://s3.kantai.xyz`, the RGW on kantai1's own OSDs, so during the
  rebuild `archive_command` fails and Postgres retains WAL in `pg_wal` on the promoted
  primary. Fine for a short window; watch free space on kantai3's volume if it drags,
  and remember that `ContinuousArchiving: False` during the window is expected, not a
  new fault.
- **`required` anti-affinity means an instance stays Pending while a node is down.**
  Acceptable here — `enablePDB: false` and tuppr runs with `drain.enabled: false`, so
  nothing is trying to evict these pods — but it does mean no replica during a kantai3
  outage.

## 4. What to leave where it is

- **OSDs.** Bound to kantai1 by hardware. Nothing to do short of buying disks for
  another node — which would also be what finally makes `failureDomain: host`
  meaningful.
- **RGWs.** Useless without OSDs, so moving them protects nothing.
- **buildkit, netronome-geoip, vmalertmanager, stash-\*.** Rebuildable; not worth the
  placement complexity.
- **kantai1's missing `PreferNoSchedule` taint.** Intentional. Leave it.

## 5. Order of work

1. **Cap kantai2's `kube-apiserver` memory** — `talos/node/kantai2/45-apiserver-resources.yaml`,
   applied with `task talos:apply HOSTNAME=kantai2`. Everything else in §2 depends on
   it: a 1 GiB mon on top of the uncapped apiserver makes the OOM loop worse.
2. **Land the Ceph spread** — `mon.allowMultiplePerNode: false`,
   `mgr.allowMultiplePerNode: false`, and the permissive `placement.mon` with the
   low-memory toleration, all in
   `kubernetes/apps/rook-ceph/cluster/app/helmrelease.yaml`. Rook unwinds the
   co-located mons one per health check. Confirm with `ceph mon dump` that you ended
   up with one mon per node, all in quorum — a mon stuck Pending on kantai2 never
   joins the monmap and leaves you no better off.
3. ~~**Move the pg18vc replica to kantai3**~~ — §3, **done 2026-09-17**. Note it took
   both a spec change *and* `kubectl cnpg destroy pg18vc 2`; the config alone moves
   nothing.
4. **Then the kantai1 rebuild** ([kantai1-rebuild.md](kantai1-rebuild.md)) needs no
   temporary mon pin and no Postgres restore. Both are failovers.

Everything in §2 and §3 has landed, and the two remaining couplings —
`dataDirHostPath` on EPHEMERAL, and the pg18vc WAL archive targeting the RGW on
kantai1's own OSDs — were reviewed and **accepted** on 2026-09-17 rather than fixed.
The reasoning and the expected symptoms are in
[kantai1-rebuild.md](kantai1-rebuild.md) §10. Neither is open work.
