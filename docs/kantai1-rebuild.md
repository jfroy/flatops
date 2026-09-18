# Rebuilding kantai1 for a 2 GiB EFI system partition

kantai1 failed the Talos v1.14.1 upgrade with a full EFI system partition. This is
the plan to fix it.

Claims marked **Verified** were checked against source at the pinned versions —
Talos `v1.13.7`/`v1.14.1`, Rook `v1.20.7`, Ceph `v20.2` (`ceph-volume`) — or read
from the live cluster through the read-only Flux MCP server, most recently on
2026-09-17 ~00:15 UTC. Claims marked **Check** still need a look, mostly because they need the Talos
API, which is not reachable from a Cowork sandbox.

## 0. Live state, 2026-09-17 00:15 UTC

**Verified, read-only.** Nothing in this section was changed by reading it.

- **All three nodes are still on Talos v1.13.7.** `parallelism: 1` meant the batch
  stopped at kantai1 and kantai2/kantai3 were never attempted. There is no mixed
  version state, which makes §2 straightforward.
- `TalosUpgrade/talos` is `phase: Failed`, `failedNodes: [kantai1]`, `lastError:
  "Upgrade Job failed while node remained at v1.13.7; expected v1.14.1"`, at
  2026-09-16T15:52:08Z. The upgrade Job is gone, so the ESP error itself is only in
  the Talos logs on the node.
- All three nodes carry `tuppr.home-operations.com/outdated:PreferNoSchedule`.
- Ceph is `HEALTH_OK`, fsid `07dc685c-e470-40ab-9cff-cde8d2d0dded`, 3 mon / 2 mgr /
  3 OSD / 2 rgw, all on 20.2.4. 11.52 TB raw, 4.42 TB used. The only warnings are
  the three muted `AUTH_INSECURE_*` ones.
- `pg18vc` is healthy, 2/2 ready, **and now spread**: primary `pg18vc-1` on kantai1
  (PV `pvc-534ba97b`), replica `pg18vc-2` on kantai3 (PV `pvc-7a3d6453`, created by the
  re-clone). Both on timeline 3, `ContinuousArchiving: True`, last backup succeeded
  2026-09-17T00:06:55Z. The old kantai1-affine `pvc-fbdfaced` is gone.
- Flux is fully green: 0 failing Kustomizations, 0 failing HelmReleases.

### Ceph daemon topology — changed since the first draft

The mon spread from [kantai-placement.md](kantai-placement.md) §2 has landed:

| daemon | kantai1 | kantai2 | kantai3 |
| --- | --- | --- | --- |
| mon | `a` | `l` | `k` |
| mgr | `b` | — | `a` |
| OSD | 0, 1, 2 | — | — |
| RGW | 1 | — | 1 |
| operator, toolbox | yes | — | — |

`rook-ceph-mon-endpoints` `mapping` reads `a:kantai1, k:kantai3, l:kantai2`,
`maxMonId: 11`, `outOfQuorum: ""`, and `status.ceph.versions.mon` counts 3. The mgr
deployments carry a required `podAntiAffinity` on `kubernetes.io/hostname` and no
`node.kantai.xyz/low-memory` toleration, so they spread across kantai1 and kantai3
while the taint keeps them off kantai2 — which is the intent.

**This is the single biggest change to the plan.** Losing kantai1 now costs one mon
of three; quorum holds on kantai2 and kantai3 throughout the rebuild, so the mon
config-key store — and with it every OSD LUKS key (§5) — survives by construction.
§6.2 becomes a verification step rather than a prerequisite, and §6.1 drops from
"mandatory, the only thing between you and unrecoverable OSDs" to defence-in-depth.

### Standing observation

kantai1 is the only node without a `node-role.kubernetes.io/control-plane:PreferNoSchedule`
taint (kantai2 and kantai3 both have it), deliberately, per
`talos/node/kantai{2,3}/35-node-taints.yaml`. That is why everything not explicitly
placed still lands there — the operator, the toolbox, both CSI controlplugins. See §10.

## 1. Why the partition is too small, and why 1.14 is not the cause

**Verified.** `pkg/machinery/imager/quirks/partitions.go`:

```go
func (p PartitionSizes) UKIEFISize() uint64 {
	return p.GrubEFISize() + p.GrubBIOSSize() + p.GrubBootSize() // 100 MiB + 1 MiB + bootSize
}
```

and `quirks.go:231`:

```go
var minTalosVersionBoot2G = semver.MustParse("1.11.0")
// bootSize = 2000 MiB, or 1000 MiB when the version is < 1.11.0
```

So a UKI/sd-boot ESP is **1101 MiB** when created by a pre-1.11 installer and
**2101 MiB** from 1.11 onward. kantai1's ESP is the legacy 1101 MiB, i.e. the node
was installed before 1.11 and has been carried forward by upgrades ever since.
**Check** the exact figure with `talosctl -n 10.1.1.1 get discoveredvolumes`.

Nothing in 1.14 requires 2 GiB. The threshold moved at **1.11**; what changed at
1.14 is only that the UKI grew past what fits.

**Verified — two UKIs have to fit at once.**
`internal/app/machined/pkg/runtime/v1alpha1/bootloader/sdboot/sdboot.go`, `Upgrade()`
globs `EFI/Linux/Talos-*.efi`, deletes every one *except the currently booted one*
(kept as the rollback fallback), and only then copies the new UKI in. The ESP must
therefore hold `booted UKI + new UKI` simultaneously. kantai1 is the node that
cannot do this: its schematic carries `zfs`, `kata-containers`,
`nvidia-open-gpu-kernel-modules-production`, `nvidia-gdrdrv-device` and
`nvidia-container-toolkit-production`. kantai2 (`util-linux-tools` only) and kantai3
(`amd-ucode`, `kata-containers`, `util-linux-tools`) have far smaller UKIs and are
unlikely to hit this even at 1101 MiB — but neither was attempted, so that is
untested.

**Verified — an upgrade never repartitions.** `PrepareBootPartitions()` is the only
caller of `partition.NewPartitionOptions` for the ESP, it runs in install/image mode
only, and it takes the size from `quirks.New(opts.Version)` of the *installer*.
`Upgrade()` touches files, never the GPT. A bigger ESP requires a fresh install onto
a wiped system disk.

### Growing the ESP in place is strictly worse than a reinstall

ESP is partition 1. Growing it shifts META and STATE right and moves the **start** of
EPHEMERAL. EPHEMERAL is XFS — it cannot be shrunk or moved — and on kantai1 it is
LUKS2 with a TPM-sealed key. So EPHEMERAL is destroyed either way, which means
`/var/lib/etcd`, `/var/lib/rook` and the openebs PVs are lost either way. The only
thing in-place repartitioning preserves over a reinstall is STATE — the machine
config — which `task talos:apply HOSTNAME=kantai1` regenerates in one command.

There is no version of "grow the partition" that is cheaper than "reinstall". Do the
reinstall.

## 2. Reinstall at 1.13.7, not at 1.14.1

Because the size threshold is 1.11, **the currently pinned 1.13.7 installer already
produces the 2101 MiB layout**. Rebuilding at 1.13.7 gets the partition fix without
touching the config tree, and since no node has moved to 1.14 yet, it leaves the
cluster uniformly on 1.13.7 throughout.

Rebuilding at 1.14.x does not work today. topf generates the machine config against
the node's *running* version at apply time, so a node booted from a 1.14 maintenance
image makes topf emit `KubeletConfig`, `UnattendedInstallConfig`, `KubeNetworkConfig`,
`DiscoveryServiceConfig` and the rest — every one of which collides with the v1alpha1
patches still in `talos/`. That is the 13-error dry run in
[talos-1.14-migration.md](talos-1.14-migration.md). The node would come up in
maintenance mode and could not be configured back into the cluster.

So: **partition fix first, at 1.13.7, with the tree untouched. The 1.14 flag day
happens afterwards, separately, with all three nodes having room.**

## 3. What a kantai1 system-disk wipe actually destroys

Reset must target **only** the system disk
(`wwid: eui.e8238fa6bf530001001b448b4a49b465`, per `talos/node/kantai1/00-install.yaml`).

**Destroyed — EPHEMERAL (`/var`):**

- `/var/lib/rook` — `mon-a`'s store (the other two now live on kantai2 and kantai3,
  see §0) and the OSD lockbox keyrings, which Rook regenerates on every OSD start.
- `/var/lib/etcd` — kantai1's etcd member.
- **`/var/zfs/citerne.key` and `/var/zfs/reservoir.key`** — the native-ZFS encryption
  keys for both pools. The pools are on other disks and survive; without these files
  they stay locked and every `zfs-static` PV is unusable. See §6.4 and §9.
- `/var/mnt/openebs` — every `openebs-hostpath` PV pinned to kantai1. Verified list:

  | PV | claim | size | matters? |
  | --- | --- | --- | --- |
  | `pvc-534ba97b` | `database/pg18vc-1` | 100Gi | the primary — `pg18vc-2` on kantai3 takes over |
  | `pvc-0b82739d` | `default/buildkit-root-amd64` | 100Gi | no, build cache |
  | `pvc-82ce1960` | `observability/netronome-geoip` | 100Gi | no, re-downloadable |
  | `pvc-5fb2536c` | `default/stash-scrapers` | 100Mi | minor |
  | `pvc-e7e44bf1` | `default/stash-plugins` | 100Mi | minor |
  | `pvc-7edd220c` | `observability/vmalertmanager-kantai-…-0` | 1Gi | no, alert state |

  (`buildkit-root-arm64` is on kantai2 and is unaffected.)
- container images, logs, everything else under `/var`.

**Destroyed — system partitions:** STATE and META, regenerated by `task talos:apply`.

**Survives — other disks:** the three Ceph OSD NVMes (LVM metadata, LUKS headers and
bluestore data are all on-device), and the `citerne` and `reservoir` ZFS pools —
`default-media1`, `default-media2`, `default-photos` (246Ti each, `zfs-static`),
`storage/kantai1-samba`, `storage/storage-pv`, immich's `reservoir/photos`, and
stash's `/mnt/citerne/media`.

**Survives but is locked:** the ZFS data is intact on disk and is *not* readable until
the keys above are put back. Treat the pools as "survives, conditionally" — §6.4 is
what makes the condition true.

## 4. The Ceph situation, stated plainly

`kubernetes/apps/rook-ceph/cluster/app/helmrelease.yaml`:

- `storage.useAllNodes: false`, and `storage.nodes` lists **only kantai1**, with three
  NVMe devices.
- Every pool — `.mgr`, `ceph-blockpool`, and both object-store pools — uses
  `failureDomain: osd`, because there is only one host to put OSDs on. The OSD
  deployments confirm it: all three are labelled `failure-domain: kantai1`,
  `topology-location-host: kantai1`, `encrypted: "true"`.
- `placement.all` now carries only a `control-plane` toleration; kantai2 is kept
  clear of mgrs and OSDs by its `node.kantai.xyz/low-memory:NoSchedule` taint, which
  only mons tolerate (§0).

Consequences to be clear-eyed about:

1. **This is not a degraded-node scenario.** kantai1 holds 100% of the Ceph data. For
   the whole rebuild window, `ceph-block` and `ceph-bucket` are unavailable
   cluster-wide, and every app on the default storage class is down. Plan a
   maintenance window, not a rolling operation.
2. **Mon quorum is no longer a hazard.** It was, when all three mons sat on kantai1.
   With one mon per node, wiping kantai1 costs one mon of three and quorum holds on
   kantai2 and kantai3 throughout. Confirm it still looks that way before you start
   (§6.2).
3. **The etcd snapshot lands on Ceph.** `talos-admin/talos-backup` writes
   `/data/etcd.boltdb` to a `ceph-block` PVC, and only reaches R2 through kopiur's
   nightly snapshot of that PVC. So a snapshot taken during the outage has nowhere
   to go — take it and copy it off *before* Ceph goes down.

## 5. Where the dm-crypt keys actually live

The working assumption going in was that the OSD LUKS keys live on the ephemeral
partition under `/var/lib/rook`. **They do not.** The authoritative copy is in the
**Ceph mon config-key store**.

**Verified — Rook `pkg/operator/ceph/cluster/osd/spec.go`**, `activateOSDOnNodeCode`
(the node/non-PVC OSD path, which is what `storage.nodes` produces):

```sh
ceph --name client.admin auth get-or-create "client.osd-lockbox.$OSD_UUID" \
	mon 'allow command "config-key get" with key="dm-crypt/osd/'$OSD_UUID'/luks"' \
	--keyring /etc/ceph/admin-keyring-store/keyring > /tmp/lockbox.keyring
...
ceph --cluster ceph --name "$LOCKBOX_USER" --keyring "$LOCKBOX_KEYRING_FILE" \
	config-key get "dm-crypt/osd/$OSD_UUID/luks" > "$KEY_FILE"
```

**Verified — `ceph_volume/util/encryption.py`**, `get_dmcrypt_key()` does the same
`config-key get dm-crypt/osd/<fsid>/luks`, and `luks_format()` feeds the secret to
`cryptsetup --key-file -` over stdin. The secret itself is
`base64.b64encode(os.urandom(128))` — an ASCII string, written with **no trailing
newline** (`process.call(..., stdin=key)` does not add one). That matters in §6.1.

**Verified — the LUKS container is the LV, not the raw NVMe.** All three OSD
deployments have `ROOK_CV_MODE: lvm`, matching `pkg/daemon/ceph/osd/volume.go`
appending `--dmcrypt` to `ceph-volume lvm batch --prepare`. The live values:

| OSD | `ROOK_OSD_UUID` (the `<fsid>` in the config-key path) | `ROOK_BLOCK_PATH` |
| --- | --- | --- |
| 0 | `4829a8a8-9440-49b4-bff2-7f09fa41772d` | `/dev/ceph-eedc0296-1858-4fe9-b44f-d992ae611538/osd-block-4829a8a8-9440-49b4-bff2-7f09fa41772d` |
| 1 | `05b25267-6a3b-461f-a4a7-3cda1b6a6f2d` | `/dev/ceph-5286f846-851a-4f3a-a24e-afaf46dea499/osd-block-05b25267-6a3b-461f-a4a7-3cda1b6a6f2d` |
| 2 | `46514b49-556d-4776-8998-64fee2878eed` | `/dev/ceph-b09f2e25-e050-4a0c-8848-41ad7dda07aa/osd-block-46514b49-556d-4776-8998-64fee2878eed` |

Both the LVM metadata and the LUKS header live on the NVMe, which the reset does not
touch.

What `/var/lib/rook` holds for the OSDs is the **lockbox keyring**, and Rook
re-creates it from `client.admin` on every OSD start (the `auth get-or-create` above),
falling back to the on-disk copy only if the mons are unreachable. Losing it is a
non-event.

So the real dependency chain is:

```
k8s Secret rook-ceph-admin-keyring  (etcd)
  -> client.osd-lockbox.<osd-fsid>  (mon auth db)
    -> config-key dm-crypt/osd/<osd-fsid>/luks   (mon store.db)
      -> cryptsetup open of /dev/<vg>/osd-block-<uuid>
        -> bluestore data on the NVMe
```

One surviving mon is enough to recover every OSD, and since the spread landed (§0)
two of them survive a kantai1 wipe. That is what took §6.1 from mandatory to
defence-in-depth.

## 6. Pre-flight — make the rebuild survivable

Snippets below are fish. The bash in §5 is quoted from the Rook tree, not something to
run.

6.2 and 6.3 used to be the hard work of this plan; both have since landed as permanent
configuration, so they are now verifications that should pass without effort. Run them
anyway — each is a thing whose absence turns a routine rebuild into an incident, and
the point of checking is to notice if something drifted back.

6.1 and 6.4 are the two that still take real work, and they are the same shape: an
encryption key that lives on the disk you are about to erase. 6.1 has a second copy
inside the cluster; **6.4 does not**, so it is the one step here with no safety net.

### 6.1 Dump the dm-crypt keys, and prove they work

Now that the mons are spread (§0), losing kantai1 no longer loses the config-key
store, so this is defence-in-depth rather than the last line. Do it anyway: it costs
one command, and it is the only thing that survives losing the mon store to something
other than this rebuild.

```fish
function ceph-tool
    kubectl -n rook-ceph exec deploy/rook-ceph-tools -- $argv
end

for fsid in 4829a8a8-9440-49b4-bff2-7f09fa41772d \
            05b25267-6a3b-461f-a4a7-3cda1b6a6f2d \
            46514b49-556d-4776-8998-64fee2878eed
    printf '%s %s\n' $fsid (ceph-tool ceph config-key get "dm-crypt/osd/$fsid/luks")
end
```

Three lines out. Read them off the screen and put them straight into OpenBao rather
than redirecting to a file — there is no reason for the keys to touch kantai1's disk,
or yours.

Verify each key actually opens its device, from inside the OSD pod (non-destructive —
`--test-passphrase` creates no mapping). For OSD 0:

```fish
set pod (kubectl -n rook-ceph get pod -l app=rook-ceph-osd,ceph-osd-id=0 -o name)
set key '<the key for 4829a8a8-...>'
set lv /dev/ceph-eedc0296-1858-4fe9-b44f-d992ae611538/osd-block-4829a8a8-9440-49b4-bff2-7f09fa41772d

printf %s $key | kubectl -n rook-ceph exec -i $pod -c osd -- \
    cryptsetup luksOpen --test-passphrase --key-file - $lv
and echo KEY-OK
```

`printf %s`, not `echo` — a trailing newline is not part of the passphrase and will
fail the test. Piping into `kubectl exec -i` keeps the key out of an inner `bash -c`
string, so there is no second round of quoting to get wrong. Repeat for OSDs 1 and 2
with their rows from the §5 table.

Store the dump **off the cluster**: OpenBao is the natural home, but put a copy
somewhere that does not depend on Ceph, etcd or kantai1 being up. A key backup that
only exists inside the cluster you are about to take down is not a backup.

### 6.2 Confirm the mons are still spread

Done, as of 2026-09-16 (§0) — `mon-a` on kantai1, `mon-l` on kantai2, `mon-k` on
kantai3, via commit `784cda2ce`. This is now a pre-flight check, not work:

```fish
kubectl -n rook-ceph get pod -l app=rook-ceph-mon -o wide
kubectl -n rook-ceph get cm rook-ceph-mon-endpoints -o jsonpath='{.data.mapping}'
ceph-tool ceph mon dump
```

What you need to see: three mons, one per node, all in quorum, and `outOfQuorum`
empty in the ConfigMap. A mon that cannot schedule never joins the monmap, so a
Pending mon would leave a two-mon monmap whose quorum is 2 — and then losing kantai1
*would* still drop quorum, exactly the situation the spread was meant to remove.

**If they have drifted back onto one node**, the config alone will not fix it. Mon
node assignment is sticky — Rook records it in that `mapping` key because the store is
a hostPath, gives the mon a hard `nodeSelector`, and strips its anti-affinity
(`p.PodAntiAffinity = nil` in `mon.go`). The only thing that relocates a mon is a
failover, and the automatic trigger for that, `evictMonIfMultipleOnSameNode()`, sits
behind `needToCheckMonsOnSameNode` — a package-level var set at operator process start
and cleared on the first healthy pass. So: **restart `rook-ceph-operator`**, once per
mon you need moved, waiting for `ceph -s` to show three mons in quorum in between
(`failoverMon` scales the old mon down before starting its replacement, so you sit at
2 of 3 during each). The full mechanism is in
[kantai-placement.md](kantai-placement.md) §2.

The mgrs need nothing: no hostPath store, so no `nodeSelector`, and their required
`podAntiAffinity` plus kantai2's taint already pins one to kantai1 and one to kantai3.
The RGWs are irrelevant either way — they are useless without OSDs, and every OSD is
on kantai1.

### 6.3 Confirm the Postgres replica is live off kantai1, and take a fresh offsite copy

**Done, 2026-09-17.** `pg18vc` used to have `instances: 2` with both pinned to kantai1
by `affinity.nodeSelector`, on `openebs-hostpath` — so both copies sat on the partition
being wiped, and a node rebuild meant a point-in-time restore from the nightly R2
export with a one-day RPO. The spec now spreads them
(`enablePodAntiAffinity: true`, `podAntiAffinityType: required`,
`topologyKey: kubernetes.io/hostname`, `nodeSelector: {kubernetes.io/arch: amd64}`) and
`kubectl cnpg destroy pg18vc 2` forced the re-clone onto kantai3. **A kantai1 rebuild
is now a failover, not a restore.**

Pre-flight check:

```fish
kubectl -n database get cluster pg18vc \
  -o jsonpath='{.status.currentPrimary}{"\n"}{.status.instancesStatus}{"\n"}'
kubectl -n database get pod -l cnpg.io/cluster=pg18vc -o wide
```

What you need: `pg18vc-1` primary on kantai1, `pg18vc-2` healthy **on kantai3**, both
reporting the same `timeLineID`, and `ContinuousArchiving: True`. If both pods are on
kantai1 the config has been merged but never acted on — bound PVCs are node-affine and
beat any affinity rule, so the anti-affinity is only a one-shot decision taken when a
PVC is created. Re-run `kubectl cnpg destroy pg18vc 2` and wait for the
`pg_basebackup` re-clone. Reasoning in [kantai-placement.md](kantai-placement.md) §3.

**Still take a fresh offsite copy.** A replica protects against losing the node; it
does not protect against losing the data logically, and WAL archiving stops for the
duration of the outage by design (§10). So before the rebuild:

1. Trigger a CNPG backup and let the WAL archive catch up.
2. Run the `pg18vc-backup` CronJob manually and confirm it completes.
3. **Verify the R2 side independently** — list `kantai-pg18vc` and check the newest
   base backup is the one you just took. `recovery-job.yaml` remains the last resort if
   everything else fails.

Everything else with real data is on `ceph-block` and covered by kopiur, whose
repository is Cloudflare R2 (`kantai-kopiur-1`), i.e. genuinely offsite. Confirm the
most recent kopiur snapshots are green before starting.

### 6.4 Verify the ZFS encryption keys before you erase them

**This is the one irreversible step in the plan.** Both ZFS pools use native
encryption, and the keys live at `/var/zfs/citerne.key` and `/var/zfs/reservoir.key` —
on EPHEMERAL, on the disk being wiped. Unlike the dm-crypt keys in §6.1, there is **no
second copy anywhere in the cluster**. 1Password is the only other copy. If it is wrong
or incomplete, the wipe turns roughly 246 TiB into ciphertext with no recovery path.

So the pre-flight is not "check the keys are in 1Password" — it is "prove the 1Password
copies are byte-identical to what is on the node."

**Verified — this is what actually unlocks the pools at boot.** The Talos zfs extension
service (`siderolabs/extensions`, `storage/zfs/zfs-service/main.go`) runs exactly:

```go
cmd := exec.Command("/usr/local/sbin/zpool", "import", "-fal")
```

The `-l` is what loads encryption keys. It resolves each encryption root's
`keylocation`, which points at `file:///var/zfs/<pool>.key`. That is why reboots work
today and why a wiped `/var` breaks it: `zpool import -fal` still imports both pools,
but the datasets come up with `keystatus=unavailable`, unmounted, and every
`zfs-static` PV fails to mount.

Talos has no shell, so this runs through a privileged debug pod — the same route the
extension's own README uses:

```fish
kubectl -n kube-system debug -it --profile sysadmin --image=alpine node/kantai1 \
  -- chroot /host sh -c '
    sha256sum /var/zfs/citerne.key /var/zfs/reservoir.key
    ls -l /var/zfs/
    zfs get -H keyformat,keylocation,keystatus citerne reservoir
  '
```

Capture all three outputs and keep them with the rebuild notes:

- **The hashes** are what you compare the 1Password copies against. Do the comparison
  now, not after the wipe.
- **`ls -l`** gives you size and mode to restore faithfully.
- **`keyformat` / `keylocation`** tell you what the files must be. If `keyformat` is
  `raw`, the key is 32 raw bytes and **must** round-trip byte-exact — a trailing
  newline, a charset conversion, or 1Password storing it as a text field instead of a
  file attachment will silently corrupt it and the hash will not match. Fix that now
  if the stored copy is a text field.

Do not proceed past this step until both hashes match what 1Password gives back.

## 7. Pre-flight facts and boot media

### Facts to collect locally

The Talos API is not reachable from a Cowork sandbox, so these have to be run locally:

```fish
task talos:kubeconfig                       # prints to stdout; it does not write ./kubeconfig
task talos:render HOSTNAME=kantai1          # then read machine.install.image; see "Boot media"

talosctl -n 10.1.1.1 get discoveredvolumes  # confirm ESP == 1101 MiB, system disk, the 3 OSD NVMes
talosctl -n 10.1.1.1 dmesg | grep -i efi    # the actual upgrade error; the tuppr Job is gone
```

### Boot media

After the reset kantai1 has no bootloader, so it needs an external image to reach
maintenance mode. Both options below are the *same build* from the self-hosted factory
at `tif.etincelle.cloud` — they differ only in how the firmware gets hold of it.

**Verified** against `siderolabs/image-factory` and
`siderolabs/talos/pkg/machinery/platforms/platforms.go`, the asset routes are
`/image/<schematic-id>/<version>/<artifact>`, with these names for `metal`/`amd64`:

| artifact | use |
| --- | --- |
| `metal-amd64-secureboot.iso` | BMC virtual media |
| `metal-amd64-secureboot-uki.efi` | UEFI HTTP boot — a single signed EFI binary |
| `metal-amd64-secureboot` (under `/pxe/…`) | iPXE script, if you ever wire up netboot |

A leading `v` on the version is optional; the handler adds it.

**kantai1's schematic ID is `aee0d5fc4efec81c84607d4bd2074670e675260f3421a413e52b6e808e33ee2b`.**

```
https://tif.etincelle.cloud/image/aee0d5fc4efec81c84607d4bd2074670e675260f3421a413e52b6e808e33ee2b/v1.13.7/metal-amd64-secureboot.iso
https://tif.etincelle.cloud/image/aee0d5fc4efec81c84607d4bd2074670e675260f3421a413e52b6e808e33ee2b/v1.13.7/metal-amd64-secureboot-uki.efi
```

Schematic IDs are **version-independent** — a hash of the schematic document — and
`talos/schematics/kantai1.yaml` has not changed since `d513ceb47`, so this ID is as
valid for 1.13.7 as it was for the 1.14.1 attempt. Three independent confirmations:
[topf-migration.md](topf-migration.md) records all three hashes by hostname from the
migration verification; the `TalosUpgrade` status recorded
`metal-installer-secureboot/aee0d5fc…` as kantai1's pre-pulled image (kantai2 was
`ebfcdcf1…`, kantai3 `0ec831fd…`, the last under `metal-installer` because it is not
secureboot); and both agree.

**Do not use `task talos:schematic-ids` to identify a node's ID.** It ignores
`--nodes-filter` and prints bare, deduplicated, lexicographically sorted IDs with no
hostnames. **Verified** in topf: `cmd/topf/schematicids.go` calls
`schematicids.Execute(ctx, t, os.Stdout)`, and `internal/cmd/schematicids` iterates
`t.Config().Nodes` — the raw config — rather than `t.FilteredNodes(ctx)`, which is the
method that applies the regex. Its own usage string says "for all nodes". The Taskfile
advertises `HOSTNAME=optional` for it, which is misleading.

For a labelled, per-node answer, render the config instead — `machine.install.image` is
generated by topf from `factory` + the node's `schematicId` + `talosVersion` +
`secureboot`:

```fish
task talos:render HOSTNAME=kantai1
grep -m1 'image:' talos/output/kantai1.yaml
rm -rf talos/output   # it holds cluster secrets in plaintext
```

Note the version in that image reference follows `talosVersion:` in `topf.yaml`, which
is currently `v1.14.1` — only the schematic ID portion matters here.

**Pre-warm the URL before pointing firmware at it.** The factory builds assets on
demand and caches them; the first request for a given schematic + version + artifact
has to assemble the whole image, and kantai1's is the fat one (nvidia, zfs, kata). The
v1.14.1 installer pre-pull to this node failed four times with `no pull progress after
1m30s (0 layers, 0 bytes received)` before succeeding — same factory, same schematic,
same cold-build problem. Firmware HTTP boot has a much shorter fuse than tuppr did, so:

```fish
curl -fL -o /dev/null -w '%{http_code} %{size_download} %{time_total}\n' \
  "https://tif.etincelle.cloud/image/<schematic-id>/v1.13.7/metal-amd64-secureboot-uki.efi"
```

Once that returns 200 quickly with a sane size, the asset is cached and the firmware
will get it promptly.

#### The hardware, corrected

kantai1 is **not** a Dell. The console shows AMI Aptio Setup Utility 2.21.1280 on an
AMD platform (AMD CBS/PBS, PSP Firmware Versions), three Micron 9300 NVMe SSDs — the
Ceph OSDs — and Mellanox NICs, behind a MegaRAC-style BMC at `kantai1-ipmi`. The
`observability/idrac-exporter` app misled an earlier draft of this document: it is
`mrlhansen/idrac_exporter`, a **generic Redfish** exporter, and its OpenBao key is even
named `kantai1-redfish`. So there is no iDRAC here, but there *is* Redfish, with
credentials already in OpenBao.

#### UEFI HTTP boot

The UKI is exactly what HTTP boot wants: one signed EFI application containing kernel,
initramfs and the schematic's `extraKernelArgs`, which the firmware downloads and
executes directly. No TFTP, no iPXE chain.

In BIOS setup the entry is *Advanced → `MAC:<mac>-HTTP Boot Configuration`*, one per
NIC. **Note it does not appear in the BMC's web BIOS-settings page**, and probably will
not appear in Redfish `Bios.Attributes` either: that form is published by the Mellanox
UEFI driver through HII at POST, not part of the static AMI setup attribute map that
those interfaces expose. Setting the URI from the console means typing it, and the KVM
viewer has no paste — see "Driving it over Redfish" below for the ways around that.

Things that decide whether HTTP boot works at all:

- **HTTPS trust.** The firmware validates the TLS certificate itself. If
  `tif.etincelle.cloud` presents an internal CA rather than a publicly-trusted one, you
  must upload that CA to the BIOS HTTP-boot trust store, or serve the UKI over plain
  HTTP on the LAN for the duration. **Check** which one applies before relying on it.
- **SecureBoot signature.** The firmware verifies the UKI against `db`. kantai1
  already secure-boots this factory's UKIs, and SecureBoot keys live in motherboard
  NVRAM rather than on disk, so the reset did not disturb the enrollment — the same
  UKI over HTTP should verify. A failure here is a clean refusal to boot, not a
  dangerous state.
- **Do not "fix" a boot failure by disabling SecureBoot.** kantai1's `EPHEMERAL` and
  `STATE` volumes use TPM-sealed LUKS with `checkSecurebootStatusOnEnroll: true`
  (`talos/node/kantai1/50-disk-encryption.yaml`). Installing with SecureBoot off would
  fail enrollment or bind the volumes in a state you do not want. If HTTP boot will not
  verify, fall back to virtual media — do not relax the firmware.
- **It is one-shot by design**, which is what you want here. Once `task talos:apply`
  installs to disk, the installer writes sd-boot and the UKI to the fresh 2101 MiB ESP
  and creates its own EFI boot entry. Check the boot order afterwards so the HTTP entry
  has not stayed ahead of the disk.

#### Driving it over Redfish (avoids typing the URL)

The BMC speaks Redfish and the credentials are in OpenBao under `kantai1-redfish`
(`username` / `password`). Discover first — do not assume paths or supported fields:

```fish
set BMC kantai1-ipmi
set RF "curl -sk -u <user>:<pass>"          # -k: the BMC cert is self-signed

curl -sk -u $U:$P https://$BMC/redfish/v1/Systems | jq .
curl -sk -u $U:$P https://$BMC/redfish/v1/Systems/1 | jq '.Boot, .Actions'
curl -sk -u $U:$P https://$BMC/redfish/v1/Managers/1/VirtualMedia | jq .
```

Two things to look for in `.Boot`:

- **`HttpBootUri`** and `UefiHttp` in
  `BootSourceOverrideTarget@Redfish.AllowableValues`. That pair is the DMTF-standard
  way to do exactly this, and it goes through the BMC/boot manager rather than the NIC
  HII form — which is why it can work even though the form is invisible to the web UI:

  ```fish
  curl -sk -u $U:$P -X PATCH https://$BMC/redfish/v1/Systems/1 \
    -H 'Content-Type: application/json' -H 'If-Match: *' \
    -d '{"Boot":{"HttpBootUri":"https://tif.etincelle.cloud/image/aee0d5fc4efec81c84607d4bd2074670e675260f3421a413e52b6e808e33ee2b/v1.13.7/metal-amd64-secureboot-uki.efi","BootSourceOverrideTarget":"UefiHttp","BootSourceOverrideEnabled":"Once"}}'
  ```

- **VirtualMedia with `InsertMedia`**, which is the higher-probability path on MegaRAC
  and sidesteps HTTP boot entirely. Unlike the KVM viewer's "Browse File" (which
  uploads from your laptop), `InsertMedia` hands the **BMC** a URL and it fetches the
  ISO itself — so there is nothing to type or upload:

  ```fish
  curl -sk -u $U:$P -X POST \
    "https://$BMC/redfish/v1/Managers/1/VirtualMedia/CD1/Actions/VirtualMedia.InsertMedia" \
    -H 'Content-Type: application/json' \
    -d '{"Image":"https://tif.etincelle.cloud/image/aee0d5fc4efec81c84607d4bd2074670e675260f3421a413e52b6e808e33ee2b/v1.13.7/metal-amd64-secureboot.iso","Inserted":true,"WriteProtected":true}'

  curl -sk -u $U:$P -X PATCH https://$BMC/redfish/v1/Systems/1 \
    -H 'Content-Type: application/json' -H 'If-Match: *' \
    -d '{"Boot":{"BootSourceOverrideTarget":"Cd","BootSourceOverrideEnabled":"Once"}}'

  curl -sk -u $U:$P -X POST \
    "https://$BMC/redfish/v1/Systems/1/Actions/ComputerSystem.Reset" \
    -H 'Content-Type: application/json' -d '{"ResetType":"ForceRestart"}'
  ```

Notes that bite: the system path is `/redfish/v1/Systems/1` on AMI/MegaRAC rather than
Dell's `System.Embedded.1`, so take it from the `Systems` collection; MegaRAC often
requires `If-Match` on PATCH (use the resource's ETag, or `*` if it accepts it); and
**pre-warm the factory URL first** — the BMC's own download will time out on a cold
build exactly like firmware would.

#### If none of that works

Shorten the URL instead of fighting the console. `*.kantai.xyz` and the Envoy gateway
are already there, so a redirect like `http://boot.kantai.xyz/k1` → the factory asset
makes manual entry trivial and is reusable for the other two nodes. Plain HTTP also
avoids the firmware TLS-trust question entirely.

Failing that, the KVM viewer's own virtual media (*CD Image → Browse File → Start
Media*) still works with a locally-downloaded ISO — slower, but no URL to type.

## 8. Procedure

1. §6 in full. 6.2 (mons spread) and 6.3 (Postgres replica on kantai3) are checks now
   and should pass without work. 6.1 (dm-crypt key dump) and **6.4 (ZFS key
   verification)** still take real work — and 6.4 is a hard gate: if the 1Password
   copies do not hash-match the files on the node, stop, because nothing later in this
   plan recovers from that.
2. Take a fresh etcd snapshot and **copy it off the cluster** — the `talos-backup`
   PVC is on `ceph-block` (§4.3).
3. Cordon kantai1 and scale down or stop everything using `ceph-block` /
   `ceph-bucket`. This is a full storage outage; quiesce clients rather than letting
   them error. `pg18vc` is the exception — leave it running. Losing kantai1 fails the
   primary over to `pg18vc-2` on kantai3, which is the intended behaviour, and every
   app on `pg18vc-rw` follows the service. Expect slower queries for the window, and
   expect `ContinuousArchiving` to go False while the RGW is down — that is accepted,
   not a fault (§10). While the window is open, keep an eye on free space on
   `pvc-7a3d6453`: unarchived WAL accumulates in `pg_wal` on the promoted primary
   until the RGW is back.
4. `ceph osd set noout` — with all OSDs on one host there is nowhere to rebalance to,
   and you want the OSDs to come back as the same OSDs.
5. Let the OSD pods stop cleanly
   (`kubectl -n rook-ceph scale deploy -l app=rook-ceph-osd --replicas=0`).
6. **Scale the Rook operator to 0** for the duration
   (`kubectl -n rook-ceph scale deploy rook-ceph-operator --replicas=0`). Optional but
   tidy: `mon-a`'s store dies with the disk, so once kantai1 is gone the operator will
   see `a` out of quorum, try to fail it over after `ROOK_MON_OUT_OF_QUORUM_TIMEOUT`
   (10 min by default), find no node free for the replacement — one mon per node, and
   kantai1 is the only free slot — and revert. Harmless churn, but it fills the log
   and makes the real signals harder to read. Quorum is held by `k` and `l` either
   way.
7. Remove kantai1's etcd member: `talosctl -n 10.1.1.1 reset --graceful` does the
   etcd leave. With 3 control planes, 2 remain and quorum holds — but there is no
   further failure tolerance until kantai1 is back, so do not start this while
   kantai2 is unhealthy.
8. Reset the **system disk only**:
   `talosctl -n 10.1.1.1 reset --graceful --wipe-mode system-disk --reboot`.
   Wiping all disks would take the OSDs and the ZFS pool with it.
9. The node now has no bootloader. Boot the **1.13.7** secureboot image into
   maintenance mode — UEFI HTTP boot from the factory UKI, or BMC virtual media with
   the ISO, either of them driven over Redfish so there is no URL to type. All covered
   in §7, including the pre-warm step; do not skip it.
10. `task talos:apply HOSTNAME=kantai1`, with `talos/` unchanged. The node installs
   at 1.13.7 and gets a 2101 MiB ESP.
11. Confirm before going further: `talosctl -n 10.1.1.1 get discoveredvolumes` shows
    EFI at 2101 MiB. If it does not, stop — everything after this is wasted work.
12. Node rejoins, TPM re-enrolls EPHEMERAL/STATE, etcd member re-adds. Scale the
    Rook operator back to 1 once kantai1 is `Ready`.
13. **Restore the ZFS keys** (§9, "ZFS: restore the keys first") before anything
    expects the pools. Do this
    early — the `zfs-static` PVs will not mount until the datasets are unlocked, so
    samba, immich, media and stash will sit there failing until it is done.
14. §9.
15. Re-run the 1.14 upgrade via tuppr (it will start from kantai1 again), and plan the
    multi-doc migration as its own change. Nothing from §6 needs reverting — the mon
    spread is the permanent configuration, not a temporary pin.

## 9. Bringing kantai1's storage back

### ZFS: restore the keys first

Nothing that touches the pools works until this is done, and several apps will sit in
a mount-failure loop in the meantime — `storage/kantai1-samba`, immich
(`reservoir/photos`), the media PVs, `storage/storage-pv` and stash's
`/mnt/citerne/media`. Do it as soon as kantai1 is `Ready`.

`zpool import -fal` at boot will have imported both pools and failed to load their
keys, so the datasets are present but `keystatus=unavailable` and unmounted. Put the
files back, then load and mount:

```fish
# repeat for reservoir.key. Piping through base64 keeps the bytes intact and keeps the
# key off the command line; substitute however you get it out of 1Password.
op read "op://<vault>/kantai1-zfs/citerne.key" | base64 | tr -d '\n' | \
  kubectl -n kube-system debug -i --profile sysadmin --image=alpine node/kantai1 \
    -- chroot /host sh -c 'mkdir -p /var/zfs && base64 -d > /var/zfs/citerne.key && chmod 0400 /var/zfs/citerne.key'
```

Then verify against the hashes captured in §6.4 — this is the check that catches a
mangled round-trip — and unlock:

```fish
kubectl -n kube-system debug -it --profile sysadmin --image=alpine node/kantai1 \
  -- chroot /host sh -c '
    sha256sum /var/zfs/citerne.key /var/zfs/reservoir.key
    zfs load-key -a
    zfs mount -a
    zfs get -H keystatus citerne reservoir
  '
```

Both encryption roots must report `keystatus available`. A reboot works too — the
extension service re-runs `zpool import -fal` — but `load-key` + `mount` avoids one.
Restore the mode and ownership you recorded in §6.4; ZFS will read a key file with
looser permissions, so a wrong mode fails quietly as a security problem rather than
loudly as a broken one.

Once the datasets are mounted, the `zfs-static` PVs bind normally — the driver
bind-mounts each dataset's own `mountpoint` under `/host`, so there is nothing else to
reconcile.

### Ceph: the OSDs re-activate themselves

With `/var/lib/rook` gone, Rook treats kantai1 as a fresh node: the `osd-prepare` job
runs `ceph-volume lvm list` against the three devices, finds the existing OSDs from
the on-device LVM tags, and the operator creates OSD deployments that run
`activateOSDOnNodeCode` — which recreates the lockbox keyring from `client.admin` and
pulls the LUKS key from the mons. The OSDs keep IDs 0, 1 and 2.

**Expect `mon-a` to be replaced, not repaired.** Its store was on the wiped
partition, and its deployment still carries a hard `kubernetes.io/hostname: kantai1`
nodeSelector from the `mapping` ConfigMap. Rook will fail it over to a fresh mon id
(`m`, since `maxMonId` is 11) which schedules back onto kantai1 — the only node
without a mon once `a` is scaled down — and syncs its store from `k` and `l`. A new
mon letter here is normal; what would not be normal is the replacement staying
Pending, which would mean the anti-affinity has nowhere to put it.

Watch for, in order:

1. `rook-ceph-osd-prepare-kantai1` logs listing three **existing** OSDs — not
   proposing to create new ones. If it proposes new OSDs, stop immediately and do not
   let it run; that is the one failure mode that destroys data.
2. `ceph -s` returning to quorum with all three OSDs `up`/`in`, and three mons
   again — one per node. Re-check `rook-ceph-mon-endpoints` `mapping`: it should read
   one mon per host, with the kantai1 entry pointing at the new id.
3. `ceph osd unset noout`, then let recovery settle to `HEALTH_OK`.
4. The tuppr `CephCluster` health check (`status.ceph.health in ['HEALTH_OK']`) will
   hold the 1.14 upgrade until Ceph is clean, which is the behaviour you want. Note
   it gates on `HEALTH_OK` exactly, and the three `AUTH_INSECURE_*` warnings are
   muted rather than fixed — if muting ever lapses, the upgrade blocks.

### If the OSDs do not come back

In order of escalation:

1. **Mons lost quorum but one store survives** — Rook's disaster-recovery guide covers
   restoring quorum from a single mon (`ceph-mon --extract-monmap`, edit, inject).
   The config-key store, and therefore the keys, come back with it.
2. **All mon stores lost** — rebuild the mon store from the OSDs with
   `ceph-monstore-tool ... rebuild`, then re-inject the dm-crypt keys from the §6.1
   dump with `ceph config-key set dm-crypt/osd/<fsid>/luks <key>`.
3. **Manual open** — with the §6.1 keys you can always
   `cryptsetup luksOpen --key-file - /dev/<vg>/osd-block-<uuid> <name>` by hand and
   read bluestore directly. This is why 6.1 is not optional.

### Postgres: re-clone, do not restore

Assuming §6.3 was done, `pg18vc-2` on kantai3 carried the database through the outage
and is now the primary. `pg18vc-1`'s PVC pointed at a directory on the wiped partition,
so its pod will not start — that is expected, and it is not a reason to reach for
`recovery-job.yaml`. Delete the instance and let CNPG rebuild it from the live primary:

```fish
kubectl cnpg destroy pg18vc 1
```

The anti-affinity puts it back on kantai1 (the only node without a `pg18vc` pod), it
re-clones via `pg_basebackup`, and once it is healthy you can hand the primary back:

```fish
kubectl cnpg promote pg18vc pg18vc-1
```

Then check `ContinuousArchiving` returns to True once the RGW is serving again, and
confirm the next `pg18vc-backup` CronJob run lands in R2 — the WAL gap during the
outage means the offsite copy is stale until then.

The small stash PVCs come back from their kopiur snapshots; the buildkit,
netronome-geoip and alertmanager PVs just refill.

## 10. Accepted design, not open work

Everything this exercise exposed has either landed or been decided against. Recorded
here so it is not re-opened as a finding next time someone reads this.

### Landed

**Critical state is spread.** Mons and mgrs in `784cda2ce`, pg18vc on 2026-09-17 —
one mon per node, one mgr each on kantai1 and kantai3, and a live Postgres replica on
kantai3. That is what turned this rebuild from a restore into two failovers.

kantai1 still has no `node-role.kubernetes.io/control-plane:PreferNoSchedule` taint
while kantai2 and kantai3 do, per `talos/node/kantai{2,3}/35-node-taints.yaml`. That is
deliberate and stays: everything without explicit placement lands on the big node,
which is where it belongs. The answer for state that must not be there is explicit
placement, not a scheduler nudge.

### Accepted: `dataDirHostPath` stays on EPHEMERAL

`/var/lib/rook` is the chart default and is not being moved to a `UserVolumeConfig` on
a non-system disk. **Decided 2026-09-17.**

What that means in practice: a system-disk wipe takes the mon store of whichever mon
lives on that node. With one mon per node that costs one of three, quorum holds on the
other two, and Rook rebuilds the lost mon by failover onto a fresh id — §9 covers it.
The cost is bounded precisely because the mons are spread.

The one thing that would make this expensive again is concentrating mons back onto a
single node. If mon placement ever changes, re-read this decision before assuming a
node rebuild is still cheap.

### Accepted: WAL archiving pauses during the rebuild

`ObjectStore pg18vc-local-ceph` points at `https://s3.kantai.xyz`, the RGW on kantai1's
own OSDs, and is not being repointed. **Decided 2026-09-17.**

What to expect, so it is not mistaken for a fault:

- `ContinuousArchiving` goes `False` shortly after the OSDs stop, and the
  `LastBackupSucceeded` condition goes stale. Both are expected for the window.
- Postgres retains WAL in `pg_wal` on the promoted primary (kantai3) for as long as
  `archive_command` keeps failing. **This is the one number worth watching**: free
  space on `pvc-7a3d6453`. A short maintenance window is fine; an unplanned multi-day
  kantai1 outage is where this would bite.
- The offsite copy in `cloudflare:kantai-pg18vc` is frozen at the last pre-rebuild
  sync until archiving resumes and the nightly `pg18vc-backup` CronJob runs again.
  §6.3 says to force a fresh backup and verify it in R2 before starting, which is what
  makes the freeze acceptable.
- Nothing needs doing to recover: once the RGW is serving again, archiving resumes and
  drains the retained WAL. §9 says to confirm `ContinuousArchiving` returns to True.

### Accepted, with a procedural mitigation: the ZFS keys live only on EPHEMERAL

`/var/zfs/citerne.key` and `/var/zfs/reservoir.key` are on the partition every rebuild
erases, and unlike the dm-crypt keys they have no in-cluster replica — 1Password is the
only other copy. Putting them somewhere that survives a wipe (a `UserVolumeConfig` on a
non-system disk, or `machine.files` from a SOPS-encrypted secret) would remove the
manual step, and is deliberately not being done.

The mitigation is procedural and it is the strictest gate in this document: §6.4
hash-verifies the 1Password copies against the node **before** the wipe, and §9
restores and re-verifies them after. There is no recovery from getting this wrong, so
unlike the other accepted items this one is not "expect some noise" — it is "do not
proceed until the hashes match."

### Accepted, with a procedural mitigation: the etcd snapshot lands on Ceph

`talos-admin/talos-backup` writes `/data/etcd.boltdb` to a `ceph-block` PVC and reaches
R2 only through kopiur's snapshot of that PVC, so no new snapshot can be taken or
shipped while Ceph is down. Same class of coupling as the WAL archive, and accepted on
the same grounds.

The mitigation is in the procedure rather than the architecture: §8 step 2 takes a
fresh snapshot **and copies it off the cluster** before anything is stopped. That is a
manual step and it is load-bearing — do not skip it on the grounds that talos-backup
runs nightly.
