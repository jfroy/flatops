# Migrating kantai's external secrets off 1Password

Status: decided 2026-09-05 (OpenBao on etincelle, public `bao.etincelle.cloud` behind Caddy, JWT auth with static keys; bootstrap secrets stay in 1Password / Apple Passwords / paper). Implementation status is in section 8. Scope: the `onepassword` `ClusterSecretStore` used by external-secrets (ESO) in flatops, plus the four `op://kantai/...` reads in etincelle's `provision-secrets.sh`.

## 1. What is there today

The cluster runs the external-secrets chart 2.10.0 with a single `ClusterSecretStore` named `onepassword`, provider `onepasswordSDK`, vault `kantai`, authenticated by a 1Password service-account token in Secret `external-secrets/onepassword-token`. That token is the one out-of-band bootstrap secret; nothing in the repo creates it. The store is configured with a 24h client-side cache, which exists mainly to stay under 1Password's API limits (AGENTS.md already forbids 1Password `PushSecret` for the same reason).

Inventory from the repo (`kubernetes/**/externalsecret.yaml`):

| Item | Count |
|---|---|
| `externalsecret.yaml` files | 78 |
| `ExternalSecret` documents | 160 (all Ready on the live cluster) |
| `secretStoreRef` → `onepassword` | 152 (in 77 files; the 78th file holds only generator-backed secrets) |
| `secretStoreRef` → `kubernetes` provider (litellm / memini pushes) | 4 (not affected) |
| Distinct 1Password keys referenced | 89 |
| Whole-item extracts (`dataFrom.extract.key: <item>`) | 59 references |
| Field references in `item/field` form (`key: cnpg-pg18vc/password`) | 73 references |
| Items with a colon in the name (`smb:jf`, `smb:media-owner`, `smb:homeassistant`) | 3 |
| `ExternalSecret`s that use the `Password` generator with `refreshInterval: "0"` (`*-keys`, `*-db`, salts, peppers) | 46 |

Two things in that list shape the plan more than anything else.

First, the data model. A 1Password item is a named bag of fields, and most manifests consume it as a bag (`dataFrom.extract` + a `template` that references `.API_KEY`, `.POSTGRES_PASSWORD`, and so on). Whatever replaces 1Password should preserve "one named object with N fields" or the migration turns into a rewrite of 160 resources instead of a mechanical find-and-replace.

Second, the 46 generator-backed secrets are not in 1Password at all. They live only in the cluster's etcd. AGENTS.md calls them out as non-rotatable (encryption-at-rest keys, peppers, salts). A cluster loss today means those values are gone, and the data they encrypt with them. That is a pre-existing DR gap, but the migration is the right moment to close it, because a store that tolerates `PushSecret` traffic (which 1Password does not) can hold a copy.

Also relevant on the etincelle side: `scripts/provision-secrets.sh` reads `image factory keys`, `cloudflare-etincelle`, `beszel-etincelle`, and `trove-etincelle` from `op://kantai`. If etincelle hosts the new store, those four cannot come from the store (it does not exist yet when etincelle is being provisioned). Decision: 1Password (or Apple Passwords, or paper) stays as the home for bootstrap secrets that have no other home; only the cluster's secrets move.

Good news for backups: the cluster already ships kopia backups to Cloudflare R2 (`kopiur-r2`), so an R2 bucket is an off-site target that costs nothing extra and needs no new account.

## 2. Requirements

- Cost: free, or a few dollars a month at most.
- No circular dependency: the store must not run on kantai, and the cluster must be rebuildable from zero with the store already up. etincelle is the intended host for anything self-hosted.
- Manifest churn proportional to a sed, not a rewrite. In practice this keeps ESO as the *consumer*: 160 `ExternalSecret`s carry templates, `rewrite` rules, and generators that nothing else reproduces. It does not require an ESO-native provider; the generic `webhook` provider, or a small bridge in front of any HTTP API, is fair game, and so is a store with no ESO integration at all if the bridge is cheap enough to own.
- Backups that live somewhere other than the store's host, and a written restore path for "etincelle is gone", "kantai is gone", and "both are gone".
- Tolerate etincelle rebooting on its own: bootc applies OS updates and reboots roughly every 8-10 hours when there is a new image. The store must come back without a human typing an unseal key.

## 3. Options evaluated

### 3.1 Self-hosted (on etincelle)

**OpenBao (Vault fork, LF project).** Single static binary, integrated Raft storage, KV v2 engine. ESO talks to it through the `vault` provider (the `openbao` provider page just points at that one); it was tested against OpenBao explicitly. Data model is exactly "path → map of fields", so `dataFrom.extract.key: radarr` becomes `bao kv get kantai/radarr` with the same field names and the same templates. Since 2.4 it has a `static` seal that auto-unseals from a 32-byte key in a file or environment variable, which solves the bootc-reboot problem without a cloud KMS. Backups are one command (`bao operator raft snapshot save`). Current release line is 2.6.x (2.6.2 shipped 2026-08-18). Runs comfortably in ~150 MB RAM. Note the `file` storage backend is deprecated and removed in 2.7, so use Raft from the start. Cost: $0.

**Infisical (self-hosted).** Nice UI, good ESO provider (ten auth methods, `find`/`extract`, push). But it needs Postgres and Redis alongside the app, so three containers on etincelle instead of one, backups become `pg_dump` plus the encryption key, and the data model is flat key/value per folder rather than named objects, so every whole-item extract becomes a `find.path` and the templates need reviewing. Workable, but more moving parts than OpenBao for no gain at this scale. Cost: $0 (community edition; enterprise features gated).

**Bitwarden Secrets Manager (self-hosted).** Requires an enterprise-licensed Bitwarden server. Out on cost and weight.

**Vaultwarden (Bitwarden-compatible server) via `bw serve` + ESO webhook provider.** Vaultwarden itself is a single ~50 MB Rust container with SQLite, and it is the only candidate that is also a full password manager for humans: browser extensions, mobile apps, family sharing, passkeys. If the reason for leaving 1Password extends to the personal/family vault, this is the option that replaces 1Password *once* instead of twice, and the data model (items with custom fields, notes, attachments) is the same shape as a 1Password item.

The cluster side is where it costs. Vaultwarden will not implement Bitwarden's Secrets Manager API (the maintainers have said it is not on the roadmap for licensing reasons), so the native ESO Bitwarden provider and Bitwarden's `sm-operator` do not apply. What does work, and is documented in the ESO examples with an explicit "also works with Vaultwarden", is a `bw serve` sidecar in the cluster: a `Deployment` running the Bitwarden CLI, logged in with an API key and unlocked with the master password, exposing an unauthenticated local REST API that `webhook` `ClusterSecretStore`s query. Specifics that matter for kantai:

- *Whole-item extract.* The webhook provider supports `dataFrom.extract` only when the JSONPath result is a JSON object (or a string containing one). `bw serve` returns custom fields as an array of `{name, value}`, which JSONPath cannot reshape into a map. Two ways out: store each item's fields as a JSON document in the item's **notes** (`jsonPath: "$.data.notes"` then parses as a map, so the existing templates work unchanged), or run a ~60-line bridge instead of hitting `bw serve` directly that returns `{field: value}` and also does name→id lookup. The notes approach is zero code but makes the vault UI show a JSON blob per item rather than editable fields; the bridge keeps fields editable but is code you own.
- *Item addressing.* `bw serve` looks items up by ID, or by a search term when it matches exactly one item; naming discipline (unique titles) makes the current keys usable as-is.
- *Bootstrap secrets in the cluster.* Three instead of one: API client id, client secret, master password. The master password is the encryption key for everything; it lives in the cluster as a `Secret` and in the break-glass bundle.
- *Operational texture.* `bw serve` is the Node CLI, ~150-250 MB RAM, must be poked at `/sync` to see new items (the ESO example abuses the liveness probe for this), has no auth so it needs a `NetworkPolicy` allowing only ESO, and its CLI has a history of breaking behaviours between releases (the current docs already carry a workaround for the 2026.6 `Host` allowlist change). It is a well-trodden path in the home-ops community, but it is the least "boring" component in this list.
- *Backups.* Vaultwarden is easy: `sqlite3 .backup` of `db.sqlite3` plus `attachments/`, `sends/`, `rsa_key*`, `config.json`, tarred, age-encrypted, to R2 on a timer. The database is already ciphertext under the master password, which is both a strength (a leaked backup is useless) and the reason the master password must be in the bundle. Restore is "copy the directory back".
- *Bootc reboots.* Nothing to unseal; Vaultwarden just starts.

Cost: $0. Verdict: viable, and the right choice if the personal vault is moving too; otherwise it carries a sidecar and a bridge that OpenBao does not need.

**Passbolt CE (self-hosted).** Has a native ESO provider, but is PHP + MySQL + per-user GPG keys, aimed at team password sharing rather than machine secrets. Heavier than Vaultwarden with none of its client ecosystem. Not pursued.

**Sealed Secrets / SOPS-style "no store" designs.** See SOPS below; sealed-secrets has the same manifest-rewrite problem plus a sealing key that must be backed up, with no advantage over SOPS given Flux already decrypts SOPS natively.

**A custom store behind the webhook provider.** Any HTTP service that returns JSON (an age-encrypted file served by a tiny Go binary on etincelle, for example) will satisfy ESO. This is the "own the whole thing" option: ~100 lines, no dependencies, no UI, no rotation tooling. It is not recommended as a primary, but it is worth knowing that the floor is that low if a vendor or project ever disappoints.

**SOPS in git (Flux native decryption).** No external store at all; the age key becomes the only bootstrap secret; git is the backup. It is already how `talos/` secrets are handled. As the *primary* store it fails the "sed not rewrite" test: every `ExternalSecret` with a `template` would have to become an encrypted `Secret` plus kustomize templating, generators would go away, rotations become commits, and old values live in git history forever. It is however the right answer for the handful of etincelle bootstrap secrets (see 5.7), and it remains a viable break-glass fallback.

### 3.2 Cloud

Free tiers checked in September 2026. All of these need one static credential in the cluster as the bootstrap secret, exactly like `onepassword-token` today, so none introduces a circular dependency.

| Service | ESO provider | Free tier / cost at kantai's scale (~90 objects, ~150 syncs, hourly refresh) | Data model fit | Notes |
|---|---|---|---|---|
| AWS SSM Parameter Store (standard tier) | `parameterstore` | $0. Standard parameters are free up to 10,000 × 4 KB; standard throughput is free. `SecureString` decrypts go through the AWS-managed `aws/ssm` KMS key (no monthly fee; ~$0.03 per 10k calls after 20k free, so cents). | Good: store one JSON document per item, `dataFrom.extract` decodes it into fields. `/kantai/radarr` ↔ `radarr`. | Cheapest cloud option. Needs an AWS account and an IAM user with a scoped policy. |
| AWS Secrets Manager | `secretsmanager` | $0.40 per secret per month → ~$36/mo. | Excellent | Too expensive. |
| Google Secret Manager | `gcpsm` | 6 versions free, then ~$0.06 per active version per month → ~$5-6/mo and creeping up as versions accumulate. | Good (JSON payloads) | Borderline on cost. |
| Azure Key Vault (standard) | `azurekv` | No per-secret fee; ~$0.03 per 10k operations → cents per month. | Flat secrets; JSON payload works with `extract`. | Cheap but an Azure tenant + service principal for a homelab is a lot of ceremony. |
| Bitwarden Secrets Manager (cloud, free org) | `bitwardensecretsmanager` | $0: unlimited secrets, 2 users, 3 projects, 3 machine accounts. | Flat name → value. ESO supports `find` by name; JSON-in-value for multi-field items. | Fits the limits. Needs the `bitwarden-sdk-server` sidecar deployed with the ESO chart. Free tier terms could change, as 1Password's did. |
| Infisical Cloud (free) | `infisical` | $0: unlimited projects, 5 identities, 3 environments per project, 10 native syncs. Pro is $20 per identity per month. | Flat per folder; `find.path` per app. | Fits. Same free-tier-longevity caveat. |
| Doppler (developer) | `doppler` | $0 for 3 users; 10 projects, 4 environments, 50 service tokens. | Flat, one config; everything as `RADARR_API_KEY`-style names. | Fits technically, awkward layout. |
| Pulumi ESC (individual) | `pulumi` | 25 secrets free, then $0.50 per secret per month. | — | Too small. |
| Scaleway Secret Manager | `scaleway` | Pricing not verifiable from docs during this review; historically per-secret-per-month. | Good | Not pursued. |
| Cloudflare Secrets Store | none | — | — | Workers-only; no ESO provider. Not usable despite Cloudflare already being in the stack. |

### 3.3 Verdict

The decision hinges on one question that this review cannot answer from the repos: **is the personal/family password vault also leaving 1Password?**

If the scope is cluster secrets only, OpenBao on etincelle is the recommendation. It is free, has no third-party terms that can change, matches the 1Password data model one-for-one (so the manifest change is mechanical), has no API rate limits (so the generator-backed secrets can be pushed into it), needs no sidecar or bridge in the cluster, and has a one-command backup that fits the existing R2 pipeline.

If the personal vault is moving too, Vaultwarden on etincelle is the better answer, because it is one migration instead of two and one thing to back up instead of two, and the 1Password items move over with their fields intact via the Bitwarden importer. The price is the in-cluster `bw serve` sidecar plus either JSON-in-notes or a small bridge (3.1), and three bootstrap credentials instead of zero. It is a reasonable price, but it should be a conscious one. A hybrid (Vaultwarden for people, OpenBao for machines) is also coherent and avoids the sidecar, at the cost of two systems to run and back up.

If you would rather not run a stateful service on etincelle at all, AWS SSM Parameter Store is the fallback: effectively free, JSON-per-item keeps the same `extract` semantics, and backup is an `aws ssm get-parameters-by-path --with-decryption` export.

Section 5 is written for the OpenBao branch. For the Vaultwarden branch, 5.1-5.4 become: deploy the Vaultwarden quadlet on etincelle behind Caddy (it needs HTTPS for the clients anyway, so public `vault.etincelle.cloud` with Cloudflare DNS-01 is the natural exposure, optionally restricted at Cloudflare to your IPs and the tailnet); import the `kantai` vault; deploy the `bw serve` `Deployment` + `NetworkPolicy` + three-key `Secret` in the `external-secrets` namespace; and create the webhook `ClusterSecretStore`(s). 5.5 changes shape: whole-item extracts keep working only after fields are moved into notes-as-JSON (a scripted one-off) or the bridge is in place, and `item/field` keys become `key: <item>` + `property: <field>` against a `bitwarden-fields`-style store. 5.6 has no `PushSecret` equivalent (the webhook provider is read-only), so the generated secrets would be archived by a script that reads them from the cluster and writes them into the vault with the `bw` CLI. 5.7-5.9 and all of section 6 apply unchanged, with the backup command in 6.2 swapped for the SQLite/attachments tarball. Everything in sections 5.5-5.8 applies unchanged to the Parameter Store fallback; only 5.1-5.4 differ.

## 4. Target architecture

```
                 tailnet / HTTPS                          R2 (existing account)
kantai ── ESO ──────────────────► etincelle: OpenBao ──► daily age-encrypted raft snapshot
 (vault provider, JWT auth)        quadlet, Raft, static seal      + weekly KV export
        ▲                                 ▲
        │ PushSecret: generated keys      │ seal key + Caddy token + image-factory keys
        │ (kantai/generated/<app>)        │ from SOPS/age file in the etincelle repo
        └─────────────────────────────────┘ (no dependency on kantai or on OpenBao)
```

- **OpenBao** runs as a quadlet on etincelle: `quay.io/openbao/openbao:2.6.x`, Raft storage under `/var/openbao/data` (tmpfiles.d entry, same pattern as `/var/zot`), config baked into the image at `/etc/openbao/config.hcl`, static seal key provisioned post-install at `/etc/etincelle/secrets/openbao-seal.key`.
- **Exposure (decided: public).** `bao.etincelle.cloud` is a Caddy block like `tif`/`ds`, TLS from Cloudflare DNS-01; OpenBao listens on `127.0.0.1:8200` with `x_forwarded_for_authorized_addrs = ["127.0.0.1"]` so audit logs see real client addresses. Authentication is the gate. (The tailnet-only alternative — bind to the Tailscale interface and give ESO a Tailscale egress `Service` — remains available later without touching the cluster manifests beyond the `server:` URL.)
- **Secrets engine.** KV v2 mounted at `kantai/`, `max_versions=10` so a bad write can be rolled back without touching a snapshot. Paths mirror 1Password item names one-to-one: `kantai/radarr`, `kantai/cnpg-pg18vc`, `kantai/smb:jf` (colons are fine in KV paths). A second prefix `kantai/generated/<app>` receives the pushed generator secrets. etincelle's own items (`cloudflare-etincelle`, etc.) are stored too, for reference, but are not read from there at provision time.
- **Auth for ESO (decided: JWT with static keys).** `jwt` auth with static validation keys: export the cluster's service-account issuer JWKS (`kubectl get --raw /openid/v1/jwks`) into an OpenBao `jwt` auth mount (`jwt_validation_pubkeys`), and bind a role to `sub = system:serviceaccount:external-secrets:external-secrets` with a fixed audience. ESO then authenticates with a projected SA token (`auth.jwt.kubernetesServiceAccountToken`) and there is **no bootstrap secret in the cluster at all**; OpenBao never calls back into kantai, so no circularity. AppRole (`secret_id_num_uses=0`, `secret_id_ttl=0`, stored as `external-secrets/openbao-approle`) remains the fallback if the JWKS export ever becomes a nuisance.
- **Policy.** `kantai-eso`: `read`/`list` on `kantai/data/*` and `kantai/metadata/*`; `create`/`update`/`read`/`delete` on `kantai/data/generated/*` and its metadata for `PushSecret`. Nothing else.
- **Audit.** File audit device to `/var/openbao/audit.log` with logrotate; it costs nothing and is the first thing you want when something is wrong.

## 5. Migration plan

Ordering is chosen so that at every step the cluster still works and 1Password is still authoritative until the final cut-over. Expected effort: two evenings, with the cut-over itself under an hour.

### 5.1 Build the OpenBao service into etincelle

In the etincelle repo: add `containers/systemd/openbao.container` (AutoUpdate=registry, `Network=host`, volumes for `/var/openbao/data`, `/etc/openbao/config.hcl:ro`, `/etc/etincelle/secrets/openbao-seal.key:ro`, `Exec=server -config=/etc/openbao/config.hcl`, plus `AddCapability=IPC_LOCK`), `openbao/config.hcl`, a tmpfiles.d line for `/var/openbao`, and the Caddy or tailscale-serve block. Config:

```hcl
ui = false
storage "raft" { path = "/var/openbao/data"  node_id = "etincelle" }
listener "tcp" { address = "127.0.0.1:8200"  tls_disable = true }   # TLS at Caddy / tailscale serve
seal "static" {
  current_key_id = "etincelle-2026-09"
  current_key    = "file:///etc/etincelle/secrets/openbao-seal.key"
}
api_addr     = "https://bao.etincelle.cloud"   # or the tailnet name
cluster_addr = "https://127.0.0.1:8201"
disable_mlock = false
```

Push to `main`, let the bootc image build, `bootc upgrade --apply`.

### 5.2 Provision and initialise

Extend `provision-secrets.sh`: generate the seal key (`openssl rand -out ... 32`), install it 0600, start `openbao.service`. Then once: `bao operator init -recovery-shares=1 -recovery-threshold=1` (with a static seal, init returns recovery keys rather than unseal keys, plus the root token). Enable `kv-v2` at `kantai`, the `file` audit device, the `jwt` auth mount and role, and write the `kantai-eso` policy. Script all of this into `scripts/openbao-init.py` in etincelle so a rebuild is repeatable. Put the recovery key and root token straight into the break-glass bundle (section 6.3); after setup, revoke the root token and generate a fresh one from the recovery key only when needed.

Verify the reboot path now, not later: `sudo systemctl reboot`, confirm `bao status` reports `Sealed: false` with no human input.

### 5.3 Copy the data

A one-shot script on the workstation, with both `op` and `bao` signed in:

```sh
op item list --vault kantai --format json | jq -r '.[].title' | while read -r item; do
  op item get "$item" --vault kantai --format json \
    | jq '[.fields[] | select(.value != null) | {(.label): .value}] | add' \
    | bao kv put "kantai/${item}" -
done
```

Two wrinkles: the 1Password SDK provider resolves `item/field` by field *label*, so labels are the keys to preserve; and file attachments (`image factory keys`) need `op read` per file and base64 into a field. `scripts/openbao-migrate.py copy` does this over the OpenBao HTTP API (`BAO_ADDR`/`BAO_TOKEN`; fields keyed by label, unlabelled/empty fields skipped, duplicate labels abort); `scripts/openbao-migrate.py verify` then diffs every item both ways and checks that every `remoteRef` in `kubernetes/` resolves (130 references across 74 items; the two `${APP}` placeholders in the OIDC component are skipped). That verify run is the rollback proof; keep it green before 5.5.

### 5.4 Add the new ClusterSecretStore alongside the old one

`kubernetes/apps/external-secrets/external-secrets/stores/openbao/clustersecretstore.yaml`:

```yaml
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: openbao
spec:
  provider:
    vault:
      server: https://bao.etincelle.cloud   # or http://openbao.external-secrets.svc via the tailscale egress Service
      path: kantai
      version: v2
      auth:
        jwt:
          path: jwt
          role: kantai-eso
          kubernetesServiceAccountToken:
            serviceAccountRef: { name: external-secrets, namespace: external-secrets }  # namespace is required in a ClusterSecretStore
            audiences: ["openbao"]
            expirationSeconds: 600
```

Add it to `stores/kustomization.yaml`, add a second `healthChecks` entry in `ks.yaml`, and if using the tailnet path, the egress `Service` lives in the same directory. Reconcile; the store should go Ready while every `ExternalSecret` still points at `onepassword`.

### 5.5 Rewrite the manifests

Two mechanical transforms over `kubernetes/**/externalsecret.yaml`, done by `scripts/openbao-rewrite-externalsecrets.py` (plain text edits, so formatting and comments survive; `--check` previews) and committed as one PR:

1. `secretStoreRef.name: onepassword` → `openbao` (152 occurrences in 77 files).
2. Every `remoteRef.key` of the form `item/field` → `key: item` + `property: field` (73 occurrences). The three `smb:*` keys and all bare item keys are unchanged.

Run `scripts/kubeconform.sh` and `flux-local`, then `diff_kubernetes_manifest` against the live cluster for a couple of namespaces. Because the target Secret names and the values are identical, ESO rewrites each Secret with the same content, so no `reloader` restarts and no pod churn; the only observable change is `status.binding` on the `ExternalSecret`s. Merge namespace by namespace if you want to watch it (`flux-system`, `network`, `cert-manager` first since they are the ones a rebuild depends on), or all at once; both are safe because the old store stays until 5.8.

The `PushSecret`s in `litellm` and `memini` use the `kubernetes` provider and are untouched.

### 5.6 Close the generator gap

54 `ExternalSecret`s generate their value with the `Password` generator, so those values exist only in etcd and a rebuilt cluster regenerates them. Most of that is harmless — `*-db` passwords are reset by `postgres-init`, service passwords are read by both sides from the same Secret, and session/JWT secrets cost a logout. The exception is any value that encrypts data at rest or authenticates credentials that cannot be reissued: regenerating one is a silent data-loss event.

Seven were adopted into OpenBao, each on documented upstream behaviour rather than on the look of the name:

| Adopted | Why |
|---|---|
| `litellm-salt` | `LITELLM_SALT_KEY` encrypts provider credentials in the database; upstream says never to change it |
| `pocket-id-keys` | `ENCRYPTION_KEY` encrypts the token-signing private keys |
| `open-webui-keys` | `OAUTH_*_ENCRYPTION_KEY` encrypt stored OAuth credentials; a wrong key crashes startup with no recovery path |
| `filebrowser-keys` | `FILEBROWSER_TOTP_SECRET` encrypts stored TOTP secrets; changing it locks out every 2FA user |
| `homebox-keys` | `HBOX_AUTH_API_KEY_PEPPER` — rotating it invalidates every issued API key |
| `openshell-cek` | key-encryption-key wrapping content keys |
| `kite` | `KITE_ENCRYPT_KEY`, plus the `JWT_SECRET` that shares its Secret. `DB_PASSWORD` stays generated — `postgres-init` resets the role to whatever the Secret holds — so this `ExternalSecret` keeps a generator alongside the extract, and keeps `refreshInterval: "0"` with it |

Deliberately left generated, on the same evidence: Paperless's `SECRET_KEY` and Zipline's `CORE_SECRET` (sessions only, per upstream), Dawarich's `SECRET_KEY_BASE` (its 2FA uses separate `OTP_ENCRYPTION_*` keys), Karakeep's `MEILI_MASTER_KEY` (Meilisearch documents a master-key reset), LiteLLM's master key (upstream documents rotation), and every `*-db`, valkey, CouchDB and RabbitMQ password.

Two of the seven — `filebrowser-keys` and `open-webui-keys` — belong to apps commented out of `kubernetes/apps/default/kustomization.yaml`, so they have no live Secret to adopt. Their manifests still read from the vault, which makes enabling either app require a vault value first (the `ExternalSecret` stays NotReady otherwise) rather than quietly minting an encryption key from a generator. `seed --create-missing` mints one matching the `password32` spec.

Mechanics: `scripts/openbao-adopt-generated.py seed` reads each live Secret with `kubectl`, maps the rendered keys back to the pre-template names the generator produced (`LITELLM_SALT_KEY` → `SALT_KEY_RAW`, `key-encryption-key` → base64-decoded `KEK_RAW`, and so on) and writes them to `kantai/<item>`; `verify` re-derives and diffs. The manifest change then replaces the `dataFrom` generator block with `extract: {key: <item>}`. Where no generator remains, `refreshInterval: "0"` is dropped: it existed to stop the generator running again, and now only prevents a vault-side rotation from propagating. `kite` is the exception — it still generates `DB_PASSWORD`, so it keeps both the generator entry and `refreshInterval: "0"`. Target Secret names, template keys and rendered values are unchanged, so no workload sees a new value. **Seed before merging the manifest change** — the reverse order leaves ESO reading a path that does not exist (it fails safe and keeps the existing Secret, but the ExternalSecret goes NotReady).

### 5.7 etincelle's own bootstrap secrets

`provision-secrets.sh` keeps reading from `op://kantai` (decision: 1Password stays for bootstrap secrets that have no other home; the 1Password *account* is not going away, only its role as the cluster's store). The OpenBao additions to that vault are `openbao-etincelle` (seal key, recovery key, root token), `openbao-backup-r2` (R2 credentials, endpoint, bucket), `openbao-backup-age` (backup private key) and `openbao-snapshot-etincelle` (AppRole for the backup job). Copy the seal key and recovery key to a second, offline place (Apple Passwords or paper) as well: these two are what stand between a lost 1Password account and unrecoverable snapshots.

### 5.8 Decommission the 1Password store

Once everything has been Ready on `openbao` for a week or so and one full snapshot/restore drill has passed (6.4): delete `stores/onepassword/`, remove its health check from `ks.yaml`, `kubectl delete secret -n external-secrets onepassword-token`, revoke the service account in 1Password, and update README.md and AGENTS.md (store name, the `item/field` → `property` convention, and drop the "no 1Password PushSecret" rule in favour of "push generated secrets to `generated/`"). The `kantai` vault itself stays, holding only bootstrap material.

### 5.9 Rollback

At any point before 5.8: revert the manifest PR; the `onepassword` store is still there and still authoritative; ESO rewrites identical Secrets again. After 5.8, rollback is a new 1Password service-account token plus a revert of the store deletion; the vault items are still there.

## 6. Backup and disaster recovery

### 6.1 What must survive

Three independent things: the OpenBao data (Raft), the static seal key (without it the data is ciphertext), and the credentials to reach the backup location. Never store the seal key in the same place as the snapshots.

### 6.2 Automated backups (all $0)

- **Daily Raft snapshot** from a systemd timer on etincelle: `bao operator raft snapshot save` (authenticated with a dedicated AppRole whose policy is only `read` on `sys/storage/raft/snapshot`, as in the openbao-snapshot-agent docs), then `age -r <backup-pubkey>` and `rclone copy` to a new R2 bucket `kantai-openbao` with a 90-day lifecycle rule. Snapshots are a few hundred KB; R2's free 10 GB and free egress cover this forever.
- **Weekly logical export** (`bao kv list -format=json` + `bao kv get` per path → one JSON), age-encrypted, same bucket. This restores into *anything*, including a fresh OpenBao of a different version, or Parameter Store if you ever change your mind.
- **Alerting.** VictoriaMetrics already scrapes etincelle's node-exporter; add a scrape of `/v1/sys/metrics?format=prometheus` and alert on the core "unsealed" gauge (`vault_core_unsealed` in Vault; check the exact name OpenBao emits) being 0, and on the snapshot timer's last-success age (push a timestamp to a textfile collector). ESO's own `externalsecret_status_condition{condition="Ready",status="False"}` is already scraped and should get an alert if it does not have one.

### 6.3 Break-glass bundle (offline)

Kept in 1Password (`kantai` vault) and, for the two items that matter most, also in Apple Passwords or on paper: OpenBao recovery key (regenerate a root token from it; the init script revokes the root token when done), the static seal key, the backup age private key, R2 credentials for the backup bucket, the etincelle SSH private key, and a Tailscale auth key or the tailnet admin login. Everything else is derivable from git.

### 6.4 Recovery scenarios

**etincelle is lost.** Bake a new qcow2 from the etincelle image, boot, `task provision` (which installs the seal key from 1Password and starts OpenBao), `bao operator raft snapshot restore -force <latest>`, confirm `bao status`. The cluster needs nothing: existing Secrets never left etcd, ESO reconnects on its next refresh, the JWT keys it validates against are inside the snapshot. RTO: about 30 minutes, dominated by the image bake. Test this once before 5.8 and then twice a year.

**kantai is lost.** Rebuild Talos and Flux as today. `external-secrets` comes up, authenticates with its projected SA token, and repopulates every Secret. If the rebuild regenerated the cluster's SA signing key, update the JWKS in OpenBao's `jwt` mount first (one command from the workstation). Generator-backed secrets come back identical if 5.6's second PR was done; otherwise they are regenerated and the encrypted data they protect is lost, exactly as today.

**Both are lost.** Bundle → rebuild etincelle → restore snapshot → rebuild kantai. The only way this becomes unrecoverable is losing the bundle, which is why it lives in two places.

**Bad write or deletion.** KV v2 keeps 10 versions and soft-deletes: `bao kv rollback` or `bao kv undelete`. Snapshots are the second line.

**OpenBao upgrade breaks something.** `podman-auto-update` pulls new images daily. Pin the tag in the quadlet to a minor line (`2.6`) and let Renovate propose the bump so an upgrade is a reviewed commit, and take a snapshot in the timer *before* the auto-update window. `bootc rollback` covers the OS; the snapshot covers the data.

### 6.5 Residual risks

etincelle is a single VM, so this is a single point of failure for *refreshing* secrets, not for running the cluster: Secrets persist in etcd, so an outage of a day costs nothing unless a rotation happens during it. Check which hypervisor etincelle runs on; if it shares a host with kantai2 (the Mac Mini), it shares that fault domain, and the R2 snapshots are what make that acceptable. The `static` seal is only as secret as the file on etincelle's disk; that is the same trust level as the Cloudflare token already there, and the documented caveat ("recommended when an existing source of trust exists") is met by the bundle. Free tiers were not chosen for the primary, so there is no vendor-terms risk on the critical path.

## 7. Decisions

Resolved 2026-09-05: scope is the cluster only (personal vault stays where it is) → OpenBao; public `bao.etincelle.cloud` behind Caddy; JWT auth with static keys; 1Password / Apple Passwords / paper remain the home for bootstrap secrets. Still open: whether to flip the generator-backed secrets to read from `kantai/generated/` (recommended) in the follow-up PR after 5.6.

## 8. Implementation status

**etincelle** (working copy, uncommitted): `containers/systemd/openbao.container` (ghcr.io/openbao/openbao:2.6.2, Raft, `U`-chowned volumes), `openbao/config.hcl` (static seal from `/etc/etincelle/secrets/openbao-seal.key`, `disable_mlock`, XFF from Caddy), Caddy block, tmpfiles entries, `scripts/bao` host wrapper, `scripts/openbao-init.py` + `openbaolib.py` + `openbao-token.py` (workstation-side Python, stdlib, drive the HTTP API at `bao.etincelle.cloud`; init is idempotent: init → recovery key + root token to 1Password, audit, KV v2 `kantai` max_versions=10, policies `kantai-eso` and `openbao-snapshot`, `jwt` auth with JWKS→PEM conversion and role `kantai-eso`, AppRole for the backup job, optional `oidc/` mount against Pocket ID for humans, revokes root at the end; `task openbao-token` mints a scoped token from the recovery key when the cluster is down), a separate `ghcr.io/jfroy/openbao-snapshot-etincelle` image (Alpine + python3/age/rclone + `openbao-snapshot/openbao-snapshot.py`, built by its own workflow like the Caddy image) run as a one-shot quadlet from a daily timer — nothing beyond config and unit files is added to the host (Raft snapshot + logical KV export, age-encrypted, rclone to R2, node-exporter textfile metric; refuses to run until `openbao/backup-age.pub` holds a real recipient), `provision-secrets.sh` additions (seal key with generate-and-store offer, R2 env, snapshot AppRole env), Taskfile tasks `openbao-init` and `openbao-snapshot-now`, README runbook, workflow `paths`. Renovate already tracks quadlet images through the existing bot, so `renovate.json` was left alone.

**flatops** (branch `openbao-secrets` off `main`, uncommitted): `stores/openbao/clustersecretstore.yaml` (vault provider, `path: kantai`, v2, JWT auth against role `kantai-eso`, audience `openbao`), registered in `stores/kustomization.yaml` and as a second health check in `ks.yaml`; `scripts/openbao-migrate.py copy|verify` (Python, stdlib, HTTP API: 1Password → KV by field label, then a two-way diff plus a check that every `remoteRef` in `kubernetes/` resolves); `scripts/openbao-rewrite-externalsecrets.py [--check]` (text-preserving rewrite: 152 store refs in 77 files, 73 `item/field` keys → `key` + `property`; validated on a scratch clone, not yet run on the branch). This doc lives at `docs/secrets-migration-plan.md`.

**Generator adoption (done):** `scripts/openbao-adopt-generated.py` plus the seven manifest changes in 5.6; AGENTS.md now carries the rule (never generate a secret that encrypts data at rest) and the adopted list.

**Manual prerequisites before `task provision`:** R2 bucket + scoped API token + lifecycle rule (e.g. `raft/` 30 d, `kv/` 90 d); `age-keygen`, recipient into `openbao/backup-age.pub`, private key into 1Password `openbao-backup-age`; 1Password item `openbao-backup-r2`; DNS for `bao.etincelle.cloud` if not covered by a wildcard. Then: push etincelle → bootc update → `task provision HOST=…` → `kubectl get --raw /openid/v1/jwks > kantai-jwks.json` → `task openbao-init HOST=… JWKS=kantai-jwks.json` → `task openbao-snapshot-now HOST=…` → `BAO_TOKEN=… scripts/openbao-migrate.py copy && … verify` → merge the flatops store PR → run the rewrite script (`--base64 nams-license-2026-02-05-2`: that Document item's `.lic` file is binary and is stored base64-encoded, so its ExternalSecret gets `decodingStrategy: Base64`), validate, merge (5.5) → 5.6 → 5.8. `copy` only migrates the 80 items `kubernetes/` references (128 direct references plus the 7 `<app>-oidc` pairs resolved from the `envoy-gateway-oidc` component); the other 35 vault items are bootstrap material and stay in 1Password.

## Sources

- flatops repo: `kubernetes/apps/external-secrets/external-secrets/{ks.yaml,stores/onepassword/clustersecretstore.yaml,app/*.yaml}`, `AGENTS.md`, `kubernetes/**/externalsecret.yaml`; live cluster state via flux-mcp (`ClusterSecretStore` and 160 `ExternalSecret`s Ready).
- etincelle repo: `README.md`, `Containerfile`, `containers/systemd/*.container`, `scripts/provision-secrets.sh`.
- [ESO Bitwarden Password Manager via webhook example](https://external-secrets.io/latest/examples/bitwarden/) (states Vaultwarden compatibility), ESO webhook provider source (`providers/v1/webhook/pkg/webhook/webhook.go`, `GetSecretMap` requires a JSON object), [Vaultwarden discussion #7457](https://github.com/dani-garcia/vaultwarden/discussions/7457) (Secrets Manager not planned), [Vaultwarden discussion #5483](https://github.com/dani-garcia/vaultwarden/discussions/5483).
- [ESO Vault provider](https://external-secrets.io/latest/provider/hashicorp-vault/), [ESO OpenBao page](https://external-secrets.io/latest/provider/openbao/), [ESO AWS Parameter Store](https://external-secrets.io/latest/provider/aws-parameter-store/), [ESO Infisical](https://external-secrets.io/latest/provider/infisical/), ESO provider directory (`docs/provider/` on GitHub, incl. `bitwarden-secrets-manager.md` sidecar requirement).
- [OpenBao static seal](https://openbao.org/docs/configuration/seal/static/), [OpenBao 2.6.x release notes](https://openbao.org/community/release-notes/2-6-0/), [openbao-snapshot-agent](https://github.com/openbao/openbao-snapshot-agent), [Raft storage](https://openbao.org/docs/configuration/storage/raft/).
- Pricing: [AWS Systems Manager](https://aws.amazon.com/systems-manager/pricing/), [Google Secret Manager](https://cloud.google.com/secret-manager/pricing), [Bitwarden Secrets Manager FAQ](https://bitwarden.com/help/secrets-manager-faqs/), [Infisical pricing](https://infisical.com/pricing), [Doppler pricing](https://www.doppler.com/pricing), [Pulumi pricing](https://www.pulumi.com/pricing/), [Azure Key Vault](https://azure.microsoft.com/en-us/pricing/details/key-vault/) (rates not rendered; treated as "cents").
