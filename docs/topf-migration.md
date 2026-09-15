# talhelper → topf cutover

[talhelper](https://github.com/budimanjojo/talhelper) was archived in August 2026. The
Talos config system moved to [topf](https://github.com/postfinance/topf)
([docs](https://postfinance.github.io/topf/main/)). This file is the cutover runbook;
once the steps below are done it can be deleted along with the talhelper files.

The migration is deliberately a **tooling change only** — still Talos v1.13.7, still the
single-document `machine:` / `cluster:` config format. Moving to the v1.14 multi-document
format is a separate change; doing both at once makes the first diff unreviewable because
every hunk could be either the tool or the format.

## What changed

| talhelper | topf |
| --- | --- |
| `talos/talconfig.yaml` | `talos/topf.yaml` + patch tree in `all/`, `control-plane/`, `node/<host>/` |
| inline `schematic:` blocks | `talos/schematics/<host>.yaml`, referenced as `schematicId: "@schematics/<host>.yaml"` |
| `talos/talsecret.sops.yaml` | `talos/secrets.sops.yaml` (same format, renamed; `secretsPath` points at it) |
| `talos/talenv.sops.yaml` + envsubst | SOPS-encrypted `data` block in `topf.yaml` + `{{ .Data.X }}` in `.tpl` patches |
| `talos/clusterconfig/` | `talos/output/` (gitignored; `topf render` only, not needed to apply) |
| `talos/clusterconfig/talosconfig` | `talos/talosconfig` |
| `talhelper genconfig` + `talosctl apply-config` | `topf apply` |
| `task talos:gen-mc` / `apply-mc` / `apply-node` | `task talos:render` / `diff` / `apply` / `upgrade` / `nodes` / `schematic-ids` / `talosconfig` |

## Cutover steps

### 1. Install topf

```sh
brew install postfinance/tap/topf     # or: go install github.com/postfinance/topf/cmd/topf@latest
topf --version                        # needs >= v0.6.0
```

### 2. Move the talenv values into `topf.yaml`

`talos/topf.yaml` ships with three empty placeholders under `data`. Fill them from
`talenv.sops.yaml` without ever writing plaintext to disk — encrypt the file first, then
`sops set` each value:

```sh
cd talos
sops encrypt -i topf.yaml
for k in CLUSTER_POD_V6_CIDR CLUSTER_SVC_V6_CIDR CLUSTER_NODE_V6_CIDR; do
  sops set topf.yaml "[\"data\"][\"$k\"]" "\"$(sops -d --extract "[\"$k\"]" talenv.sops.yaml)\""
done
sops filestatus topf.yaml              # {"encrypted":true}
git diff --stat topf.yaml              # only the data block should be ciphertext
```

`.sops.yaml` sets `encrypted_regex: ^data$` and `mac_only_encrypted: true` for
`talos/topf.yaml`, so the versions, node list and comments stay in plaintext, remain
diffable, and Renovate can still bump them in place.

An unset value renders as an empty list entry rather than an error — sprig has no
`required` function — so the render diff in step 3 is what catches a missed key.

### 3. Prove the render matches talhelper's

Take the talhelper baseline **before** deleting anything. The `talos/clusterconfig/`
files currently on disk are stale (they predate the v1.13.7, v1.36.2 and
`terminated-pod-gc-threshold` commits), so regenerate them:

```sh
talhelper genconfig --config-file talos/talconfig.yaml   # baseline, one last time
topf render -o /tmp/topf-out                             # from talos/
for h in kantai1 kantai2 kantai3; do
  diff <(yq -P . talos/clusterconfig/kantai-$h.yaml) <(yq -P . /tmp/topf-out/$h.yaml)
done
rm -rf /tmp/topf-out                                     # plaintext cluster secrets
```

Only two kinds of hunk are acceptable: multi-document **order** (topf emits documents in
patch order — Talos keys documents by kind and name, so order is irrelevant), and
whitespace or quoting inside values you can point at. Anything else is real.

This diff was already run against the stale baseline during the migration and came back
clean: the only differences were the five stale values above and the dummy IPv6 CIDRs
used in place of the real ones. `topf schematic-ids` also reproduced all three schematic
hashes byte-for-byte, so no node is pointed at a different installer image:

```
kantai1  aee0d5fc4efec81c84607d4bd2074670e675260f3421a413e52b6e808e33ee2b
kantai2  ebfcdcf1d645f3600833a76404f8f4d65b67676b2f5d585690c41422b0d32918
kantai3  0ec831fd7b3c18894977cedb8aa7c8d87cfb9a90eefeca2d2620241c90f4baba
```

### 4. Regenerate talosconfig

`TALOSCONFIG` moved from `talos/clusterconfig/talosconfig` to `talos/talosconfig`. The
secrets bundle and CA are unchanged, so the old file still works — copy it, or:

```sh
task talos:talosconfig
```

### 5. Diff against the live cluster, then apply

```sh
task talos:diff      # topf apply --dry-run; exit code 2 means changes
task talos:apply
```

`--dry-run` diffs against each node's **running** config, so anything that was committed
to `talconfig.yaml` but never applied surfaces here too. Review it rather than letting
the migration apply it unnoticed.

topf dials each node's `:50000` directly — there is no apid proxying through a control
plane — so run it from a network that reaches all three nodes.

### 6. Delete the talhelper files

Once step 5 is clean:

```sh
git rm talos/talconfig.yaml talos/talenv.sops.yaml
git rm -r talos/clusterconfig
git rm docs/topf-migration.md
```

## Behaviour notes carried over deliberately

**`node.kubernetes.io/exclude-from-external-load-balancers`.** talhelper *replaced* the
generated `machine.nodeLabels` map, which silently dropped the control-plane default
label Talos adds — so kantai1 and kantai2 do not carry it today, while kantai3 (which
declared no labels) does. topf patches *merge*, which would quietly hand the label back
to kantai1 and kantai2. To keep the cluster exactly as it is,
`node/kantai1/30-node-labels.yaml` and `node/kantai2/30-node-labels.yaml` delete it
explicitly with `$patch: delete`. Dropping those three lines is the way to adopt the
Talos default instead — worth considering, since the current inconsistency is an
accident of talhelper's semantics rather than a decision.

**`ignore: true` interfaces.** kantai1's `enp1s0f1np1`, `enp66s0f0` and `enp66s0f1` were
marked `ignore: true` in `talconfig.yaml`. talhelper emitted nothing for them — on Talos
>= 1.13 it renders `networkInterfaces` as `DHCPv4Config`/`LinkConfig` documents and skips
any interface with no addresses, routes or MTU. An interface with no config document is
left untouched by Talos, so they are simply absent from `node/kantai1/10-network.yaml`.

**`machineSpec`.** talhelper used it only for `genurl image`. `arch` has no topf
equivalent and needs none (factory installer images are multi-arch); `secureboot` became
the per-node `secureboot` field; `useUKI` became the explicit
`machine.install.grubUseUKICmdline: true`, since dropped as a no-op on sd-boot nodes (see [talos-1.14-migration.md](talos-1.14-migration.md)).

**`additionalMachineCertSans` / `additionalApiServerCertSans`.** talhelper fed these into
the Talos generator input. topf has no such knob, so `all/11-cert-sans.yaml` sets
`machine.certSANs` and `cluster.apiServer.certSANs` directly. The generator adds nothing
else to either field, so the result is identical.

**Renovate.** `.renovate/talosFactory.json5` already carries a matcher for topf's
`talosVersion:`, and its `managerFilePatterns` covers `talos/*.yaml`, so `topf.yaml` is
picked up unchanged. Note that this means the `talosVersion` line is matched by two
managers — that custom one (datasource `custom.talos-factory`, against
`tif.etincelle.cloud/versions`) and the generic `# renovate:` comment manager pointing at
`ghcr.io/siderolabs/imager`. That overlap predates this migration; the
`# renovate: ... imager` comment was carried over verbatim rather than resolved here.
