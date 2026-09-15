# Replacing `machine.kubelet.extraMounts`

`all/31-kubelet-mounts.yaml` is the one thing standing between this repo and
`KubeletConfig` at Talos 1.14: `extraMounts` is *"removed in multi-doc config"*
with no replacement document, and `KubeletConfig`'s conflict validator rejects
any `.machine.kubelet` at all, so it is all-or-nothing for the whole block.

This is the plan to get rid of it. All three phases can be done on 1.13.7,
before the 1.14 upgrade, and should be — none of them depend on it.

Claims below marked **verified** were checked against the Talos source at tags
`v1.13.7` and `v1.14.0` and against `jfroy/zfs-static-csi` at HEAD. Claims marked
**check** need a look at the live nodes first.

## What actually needs the mounts

The patch mounts two paths. They are not in the same situation.

### `/var/mnt` — already redundant, delete it

**Verified.** Talos mounts it into the kubelet itself, in both 1.13.7 and 1.14.0
(`internal/app/machined/pkg/system/services/kubelet.go`):

```go
{Type: "bind", Destination: constants.UserVolumeMountPoint, Source: constants.UserVolumeMountPoint,
 Options: []string{"rbind", "rslave", "ro"}},
```

with `UserVolumeMountPoint = "/var/mnt"`. `/var/mnt` is a Talos-managed volume in
its own right — `UserVolumeConfig`, `ExistingVolumeConfig` and
`ExternalVolumeConfig` all mount at `/var/mnt/<name>` as children of it
(`Mount.ParentID`). The patch re-binds that same path `bind,rshared,rw` on top of
Talos's `rbind,rslave,ro`.

The `ro` is not a problem for volumes: it applies to the `/var/mnt` bind itself,
not recursively (Talos passes `ro`, not `rro`), so each volume mounted underneath
keeps its own read-write flags. It *is* a problem for plain directories created
under `/var/mnt` by hand — they are inside the read-only bind and on EPHEMERAL.
That is the practical reason not to use `/var/mnt` as a general-purpose base.

Nothing in this repo or the cluster references `/var/mnt`: no hostPath, no PV
path, no chart value. **Check** that with `talosctl -n <node> read /proc/mounts`
before deleting, then delete the entry.

### `/var/openebs/local` — genuinely load-bearing

`openebs-hostpath` PVs are `local` PVs with `spec.local.path:
/var/openebs/local/pvc-<uid>`. The kubelet performs that bind mount, so the path
has to exist in the kubelet's mount namespace. Nothing mounts it there except
this patch. This is the part that needs real work — see phase 3.

## ZFS does not need either mount

**Verified.** `zfs-static-csi` never goes through the kubelet's view. Its
daemonset mounts `/` at `/host` (`HostToContainer`) and `/var/lib/kubelet`
(`Bidirectional`). `NodePublishVolume` resolves the dataset's own `mountpoint`
property under that prefix and bind-mounts it at the target:

```go
source := filepath.Join(s.driver.cfg.HostPrefix, ds.Mountpoint)
...
s.driver.mounter.Mount(source, targetPath, "", options)
```

`/var/lib/kubelet` is already mounted `rbind,rshared,rw` into the kubelet by
Talos, so the published volume reaches the pod that way. Removing the `/var/mnt`
extraMount changes nothing for ZFS.

The pool base should still move off `/var/mnt`, but because that path is Talos's
volume namespace, not because the kubelet needs it. The useful consequence: PVs
reference dataset *names* (`volumeHandle: reservoir/media1`) and the driver reads
the mountpoint at publish time, so **relocating the pool requires no PV changes**.

## Phase 1 — drop the `/var/mnt` extraMount

On 1.13.7, no upgrade needed.

1. **Check** nothing is mounted under `/var/mnt` that only this bind exposes:
   `talosctl -n kantai1,kantai2,kantai3 mounts | grep /var/mnt`
2. Remove the `/var/mnt` entry from `all/31-kubelet-mounts.yaml`, leaving
   `/var/openebs/local`.
3. `task talos:diff`, then `task talos:apply`. The kubelet restarts.
4. Verify a `zfs-static` PVC still mounts — bounce one pod in `storage` or
   `default` and confirm it comes back.

## Phase 2 — move the ZFS pool base

Only kantai1 has the pool (every `zfs-static` PV has `nodeAffinity` on kantai1),
and Ceph OSDs are on three dedicated NVMe devices there, untouched by any of this.

1. **Check** where the datasets sit today (see *Running zfs commands* below):
   `zfs get -r -o name,value mountpoint,mounted reservoir`
2. Pick a base outside `/var/mnt`. `/var/zfs/reservoir` works: `/var` is writable
   and persists across reboots on EPHEMERAL, it is not Talos's volume namespace,
   and it is not `/var/lib` where Talos and Kubernetes components keep state. The
   directory itself is created by ZFS at mount time, so nothing has to pre-exist.
3. Scale down every workload holding a `zfs-static` PVC — `storage/{jf,media1,
   media2,homeassistant-backup}` and `default/{media1,media2,photos}` consumers,
   including `storage/kantai1-samba`, which serves the `citerne` datasets.

   **`mounted: yes` does not mean "in use".** The zfs extension mounts these at
   boot and nothing in Kubernetes ever unmounts them, so every dataset stays
   mounted no matter how much you scale down. What blocks the move is a bind
   mount on top (a CSI publish) or an open file. Check that instead — the debug
   container has the host PID namespace, so host init's mount table is readable
   without entering its mount namespace:

   ```sh
   # host's own mounts and any CSI bind mounts on top of them
   grep reservoir /proc/1/mountinfo
   # which processes' mount namespaces still contain one (i.e. which pods)
   grep -l /var/mnt/reservoir /proc/*/mountinfo
   ```

4. **Check the property sources before setting anything.** Only datasets that
   *inherit* `mountpoint` follow a change to the pool root:

   ```sh
   chroot /host /usr/local/sbin/zfs get -r -o name,property,value,source mountpoint,canmount reservoir
   ```

   `reservoir/dynamic` has `mountpoint=none` set locally and must stay that way.
   `reservoir` itself reports `mounted: no`, which is expected if `canmount=off`.
5. Unmount first, so a busy dataset fails before the property changes rather
   than half way through it. Each `nsenter` runs exactly one host binary — there
   is no shell in the host mount namespace, so no `sh -c` pipelines:

   ```sh
   nsenter --mount=/proc/1/ns/mnt -- /usr/local/sbin/zfs unmount -a
   nsenter --mount=/proc/1/ns/mnt -- /usr/local/sbin/zfs set mountpoint=/var/zfs/reservoir reservoir
   nsenter --mount=/proc/1/ns/mnt -- /usr/local/sbin/zfs mount -a
   ```

6. Verify the new layout, and that the driver's opt-in property survived — it is
   `com.github.jfroy.zfs-static-csi:share`, and a dataset without it set to `on`
   is refused at publish time:

   ```sh
   chroot /host /usr/local/sbin/zfs get -r -o name,value \
     mountpoint,mounted,com.github.jfroy.zfs-static-csi:share reservoir
   grep /var/zfs /proc/1/mountinfo
   ```

7. Remove the now-empty tree under the old base with `rmdir`, not `rm -rf` —
   `rmdir` refuses a non-empty directory, which is exactly the safety property
   you want if something failed to remount. File operations do not need the host
   mount namespace, so this works on `/host` directly:

   ```sh
   find /host/var/mnt/reservoir -depth -type d -empty -delete
   ```

8. Scale back up. The driver republishes against the new mountpoint; no PV or PVC
   changes.
9. Reboot kantai1 once and confirm the pool re-imports and remounts at the new
   base — the mountpoint property lives in pool metadata, so it should survive.

### Running zfs commands

Talos has no shell and `talosctl` has no `exec`. The tool for this is
`talosctl debug`, which runs a container on the node:

```sh
talosctl -n kantai1 debug docker.io/library/debian:stable-slim --args /bin/sh
```

**Verified** in `internal/app/debug/debug.go`, that container gets the host
**network, PID and IPC** namespaces, the host `/` rbind-mounted at `/host` rw,
every grantable capability, all devices allowed, and seccomp/AppArmor/SELinux
unconfined. It runs in an in-memory containerd namespace by default, so it leaves
nothing behind on the node.

What it does **not** get is the host **mount** namespace — `oci.WithHostNamespace`
is applied to Network, PID and IPC only — and `/host` is rbind-mounted without a
shared propagation option. So mounts made inside it stay inside it. That splits
the commands into two kinds:

**Read-only** (`zfs get`, `zfs list`, `zpool status`, `zpool list`) — chroot is
enough, and is needed so the host's dynamic linker and libraries resolve:

```sh
chroot /host /usr/local/sbin/zfs get -r -o name,value mountpoint,mounted reservoir
```

**Mount-affecting** (`zfs set mountpoint`, `zfs mount`/`unmount`, `zpool
import`/`export`) — these must run in the host's mount namespace or the remount
lands in the container and the host never sees it. Because the debug container
*does* have the host PID namespace, `/proc/1/ns/mnt` is the host init's mount
namespace:

```sh
nsenter --mount=/proc/1/ns/mnt -- /usr/local/sbin/zfs set mountpoint=/var/zfs/reservoir reservoir
```

After `nsenter --mount`, `/` is already the host root, so no chroot is needed and
the host binary and libraries resolve normally.

Two practical notes. The debug image must contain `nsenter`
(`debian:stable-slim` does; Alpine's busybox build may not, so check before
relying on it). And confirm the zfs binary path first — the CSI driver searches
`/usr/sbin/zfs`, `/sbin/zfs`, `/usr/local/sbin/zfs`, `/usr/local/bin/zfs`, and
the commented-out `zfs-scrub` CronJob in `kubernetes/apps/storage/maintenance/`
uses `/usr/local/sbin/zpool`, which is the expected location for an extension.

That CronJob is a correct pattern for a scrub, which touches no mounts. It would
be **wrong** if reused for a mountpoint change: `chroot /host` alone does not
cross the mount namespace. A committed Job needs `hostPID: true` plus `nsenter`
for that.

`talosctl debug` is a live action on a node, which `AGENTS.md` puts behind
explicit authorization. Treat this migration as that authorization, or do it with
a committed one-shot Job instead and delete it afterwards.

## Phase 3 — openebs-hostpath onto a user volume

The target shape: a `UserVolumeConfig` named e.g. `openebs`, which Talos mounts at
`/var/mnt/openebs` and already exposes to the kubelet, with
`localpv.basePath: /var/mnt/openebs`.

The catch: **changing `basePath` only affects newly provisioned PVs.** The ten
existing ones have `/var/openebs/local/pvc-<uid>` baked into `spec.local.path` and
break the moment the extraMount goes away. They have to be recreated.

### What is actually on openebs-hostpath

| namespace | PVC | size | data |
| --- | --- | --- | --- |
| database | `pg18vc-2` | 100Gi | CNPG member — rebuilds from the primary |
| database | `pg18vc-3` | 100Gi | CNPG member — rebuilds from the primary |
| default | `buildkit-root-amd64` | 100Gi | build cache — disposable |
| default | `buildkit-root-arm64` | 100Gi | build cache — disposable |
| default | `stash-plugins` | 100Mi | re-fetchable |
| default | `stash-scrapers` | 100Mi | re-fetchable |
| observability | `alertmanager-kps-db-alertmanager-kps-0` | 1Gi | silences — disposable |
| observability | `vmalertmanager-kantai-db-vmalertmanager-kantai-0` | 1Gi | silences — disposable |
| observability | `netronome-geoip` | 100Gi | re-downloadable |
| observability | `server-volume-victoria-logs-server-0` | 10Gi | logs — disposable |

Eight of ten are disposable or reconstructible, and the two CNPG members rebuild
from the primary one at a time. So this is "delete and let it come back", not a
data copy — which makes the whole phase much cheaper than it looks.

### The volume

`all/70-openebs-volume.yaml`, applied to every node:

```yaml
apiVersion: v1alpha1
kind: UserVolumeConfig
name: openebs
volumeType: directory
```

A directory volume needs no disk. Talos creates `/var/mnt/openebs` and bind-mounts
it (`BindTarget` is set for directory user volumes, so `handleDirectoryMountOperation`
performs a real `open_tree` bind, not a bare `mkdir`). That matters: because it is
a genuine mount under `/var/mnt`, it is a submount of the kubelet's
`rbind,rslave,ro` bind and keeps its own read-write flags. A plain directory there
would have been read-only to the kubelet, which is the whole failure mode this
migration exists to avoid.

The minimal form above is the only valid one — `provisioning` and `filesystem` are
rejected outright:

```
UserVolumeConfig/openebs: filesystem spec is invalid for volumeType directory
```

So there is no `filesystem.type: xfs` and no `projectQuotaSupport` on this path.

### What the directory type costs

The backing store is `/var/mnt`, which is on EPHEMERAL. So openebs-hostpath data
stays on the same filesystem it is on today at `/var/openebs/local` — this change
buys kubelet visibility and nothing else. That is exactly the problem that needed
solving, and it is worth being clear about what it does not solve:

- **No capacity isolation.** A runaway buildkit cache can still fill EPHEMERAL.
  No worse than today, but a disk-backed volume would have fixed it. kantai2 is
  the tight one: 105 GB EPHEMERAL shared with everything else on that node.
- **No XFS project quotas**, per the validation error above.
- **An EPHEMERAL wipe takes the data with it** — unchanged from today, and still
  fine given everything on openebs-hostpath is disposable or rebuildable.

In exchange it is one file instead of three, with no disk selectors to keep
correct when hardware changes. The free devices (kantai1 `/dev/nvme1n1` 2.0 TB,
kantai2 `/dev/vdb` 215 GB, kantai3 `/dev/nvme1n1` 500 GB) are still unused, so
switching to a partition-backed volume later is a config change plus recreating
the PVs — the same work as phase 3 itself. kantai2's `vdb` was grown from 2.1 GB
for the partition-backed plan and is no longer needed for this.

### basePath is global — every node needs the volume

`localpv.basePath` is a single helm value, so it cannot be rolled out per node.
Once it points at `/var/mnt/openebs`, a node without that volume has the
provisioner trying to create the directory inside Talos-managed `/var/mnt`, which
the kubelet sees read-only — provisioning then fails on that node alone while the
others look healthy. The `all/` placement handles this, but confirm
`/var/mnt/openebs` is mounted on all three (`talosctl mounts | grep openebs`)
before touching the helm value.

### Steps once the route is picked

1. Add the `UserVolumeConfig` to `all/` (or per node), apply, confirm
   `/var/mnt/openebs` is mounted and visible to the kubelet.
2. Point both openebs HelmReleases at the new `basePath`. Note there are
   currently two releases — `openebs` and `openebs-localpv` — both declaring
   `basePath: /var/openebs/local` and the same `openebs-hostpath` class name;
   sort out which one owns the class while you are here.
3. Per PVC, in order of least valuable first: scale the workload down, delete the
   PVC and PV, let it re-provision on the new path, scale up. Do the CNPG members
   last, using the sequence below.
4. Once no PV references `/var/openebs/local`, delete
   `all/31-kubelet-mounts.yaml` entirely.

### Cycling the CNPG members

`pg18vc` in `database` runs 2 instances, both pinned to kantai1 by
`spec.affinity.nodeSelector`, with barman-cloud WAL archiving to
`pg18vc-local-ceph`. The storage class is unchanged — only its `BasePath`
annotation moved — so a recreated PVC lands on the new path automatically and the
Cluster manifest needs no edit.

`kubectl cnpg destroy` deletes the instance's pod **and** its PVC, and the class
has `reclaimPolicy: Delete`, so the old directory under `/var/openebs/local` is
removed for you. Instance numbers are not reused: `status.latestGeneratedNode` is
the counter, so the replacements come back as `pg18vc-4` and `pg18vc-5`.

The primary cannot be destroyed directly, so the order is replica, switchover,
old primary:

```sh
kubectl cnpg backup pg18vc                  # fresh base backup before starting
kubectl cnpg destroy pg18vc 3               # replica -> recreated as pg18vc-4 on the new path
kubectl cnpg status pg18vc                  # wait for 2/2 ready before continuing
kubectl cnpg promote pg18vc 4               # switchover; pg18vc-2 becomes a replica
kubectl cnpg destroy pg18vc 2               # old primary -> recreated as pg18vc-5
kubectl cnpg status pg18vc
```

Verify the paths after each rebuild:

```sh
kubectl get pv -o custom-columns=NAME:.metadata.name,PATH:.spec.local.path,CLAIM:.spec.claimRef.name \
  | grep pg18vc
```

Each `destroy` leaves the cluster at one ready instance until the basebackup
finishes, so run them one at a time and never back to back. Continuous archiving
plus a current backup means the worst case in that window is a restore rather
than data loss. If you would rather not have a single-instance window at all,
raise `spec.instances` to 3 first, cycle, then drop back to 2 — it costs another
100Gi on kantai1's EPHEMERAL and another 12Gi memory request.

Two things to watch. EPHEMERAL free space on kantai1 during the clone, since the
directory-backed volume shares `/var` with everything else on that node. And
`wal_level` is `logical` here — if anything subscribes to this cluster over
logical replication, confirm that before the `promote`, since a switchover moves
the primary.

## What this unblocks

With the patch gone, `machine.kubelet` has no `extraMounts` left, and the 1.14
conversion of `all/30-kubelet.yaml.tpl` and the kubelet half of
`control-plane/04-feature-gates.yaml` to `KubeletConfig` + `KubeNodeConfig`
becomes mechanical. See [talos-1.14-migration.md](talos-1.14-migration.md).
