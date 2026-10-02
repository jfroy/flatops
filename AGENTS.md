# AGENTS.md

This file provides guidance to agents when working in this repo (`flatops`) which controls the `kantai` homelab cluster.

## Cluster

This is **kantai**, a Kubernetes cluster running Talos Linux, with a mix of bare-metal and virtual nodes, managed entirely through GitOps via FluxCD. The FluxC instance syncs `refs/heads/main` from `https://github.com/jfroy/flatops` at `kubernetes/cluster`, which contains the top-level `Kustomizations`.

## Agent Safety Rules

- This repository is the source of truth. Make changes in Git, validate locally, and let Flux reconcile them.
- Do not run mutating `kubectl` commands unless the user explicitly authorizes the exact action first.
- Forbidden without prior authorization: `kubectl apply`, `create`, `delete`, `replace`, `patch`, `edit`, `scale`, `rollout restart`, `annotate`, `label`, `cordon`, `drain`, and any other command that changes live cluster state.
- Read-only inspection is allowed: `kubectl get`, `describe`, `logs`, `events`, `top`, `auth can-i`, and `diff` or `apply --dry-run=server`.
- Prefer Flux MCP tools and local rendering/diff tools for troubleshooting. If a live change is necessary, stop and ask first with the exact command and reason.
- `flux reconcile ...` is allowed only to ask Flux to apply committed Git state or when explicitly requested by the user; do not use Flux as a substitute for direct manifest application.
- Always specify the context when using `flux` or `kubectl`. A tailscale context is likely available, but may not work on some hosts due to VPN. In such situations, a kantai.xyz context is likely available.

## Flux MCP

Flux MCP tools are likely available. Use them to inspect live cluster state when troubleshooting.

## Maintenance Commands

Just (>= 1.55.0) is the command runner. `.justfile` registers `talos/mod.just`,
`scripts/rook/mod.just`, and `scripts/sops/mod.just`; run `just` or `just talos` to
list recipes. Node names are positional arguments, with `--online` for rendering
and `--mode <mode>` for applying.

```sh
# Talos: render machine configs to talos/output for inspection (contains secrets)
just talos render

# Diff the rendered config against the live nodes without applying
just talos diff

# Apply machine configs, or limit to one node
just talos apply
just talos apply kantai1

# Upgrade Talos to the version pinned in talos/topf.yaml
just talos upgrade
```

Flux reconciliation, when appropriate and after Git state is ready:

```sh
flux reconcile kustomization cluster-apps --with-source
flux reconcile helmrelease <name> -n <namespace>
```

Do not use `kubectl apply -k`, `kubectl apply -f`, or direct Helm installs/upgrades to deploy repo resources unless the user has explicitly authorized that live mutation.

## Repository Structure

```txt
kubernetes/
  apps/             # One subdirectory per namespace
    default/        # Most user-facing applications
  cluster/          # Top-level Kustomizations
  components/       # Reusable Kustomize components
  transformers/     # NamespaceTransformer applied globally
  vap/              # ValidatingAdmissionPolicies applied before apps
talos/              # topf config (topf.yaml + patch tree + SOPS-encrypted secrets)
  all/              # Patches applied to every node
  control-plane/    # Patches applied to control-plane nodes
  node/<host>/      # Per-node patches
  schematics/       # Per-node image factory schematics, hashed by topf
bootstrap/          # One-time cluster bootstrap (currently broken/unused)
```

## Helm Chart Strategy

Two categories of deployments exist in this cluster:

- **app-template apps** — containerized applications without their own Helm chart. These use the `bjw-s-labs/app-template` chart, which provides a generic, highly-configurable template for deploying arbitrary containers. This covers most user-facing apps under `kubernetes/apps/default/`.
- **official-chart apps** — cloud-native projects and infrastructure components that ship their own Helm chart (e.g. cert-manager, external-secrets, cilium, CNPG, Flux itself). Always prefer the upstream chart for these; only fall back to app-template if the official chart has a serious problem.

`OCIRepository` sources are strongly preferred over `HelmRepository` sources. When an upstream chart is not available as an OCI artifact, pull it via the cluster's `ocharted` on-demand OCI mirror.

`OCIRepository` has no `tag@digest` form — `ref.tag` and `ref.digest` are separate fields and the digest wins. To pin a chart by digest, set both: the tag keeps the pin readable and gives Renovate something to bump. See `kubernetes/apps/openshell/openshell/app/ocirepository.yaml`.

When a project ships release manifests instead of a chart, reference the release URL directly from `app/kustomization.yaml` with a `# renovate: datasource=github-releases depName=org/repo` comment above it. Apply upstream unmodified — including its own namespace — rather than relocating it: a kustomize `namespace:` directive rewrites `ClusterRoleBinding` subjects and webhook service references but silently leaves `cert-manager.io/inject-ca-from` annotations pointing at the old namespace. Delete only upstream's bare `Namespace` object (`$patch: delete`) so this repo's `namespace.yaml` owns it with PSA labels and the common component's prune-disabled annotation. See `kubernetes/apps/agent-sandbox-system/agent-sandbox/app/` and `kubernetes/apps/cnpg-system/barman-cloud/app/`.

When an upstream chart offers no hook for something this repo needs — most often an init container — inject it with `spec.postRenderers[].kustomize.patches` rather than forking the chart. Prefer a strategic-merge patch over JSON6902 when the target path may not exist in the rendered output (a JSON6902 `add` on a child of an absent map fails). See `kubernetes/apps/openshell/openshell/app/helmrelease.yaml`, which patches the gateway Deployment this way.

## App Pattern (kubernetes/apps/default/)

App-template apps follow the same four-file layout:

```txt
<appname>/
  ks.yaml               # Flux Kustomization — registers the app with Flux
  app/
    helmrelease.yaml    # HelmRelease — app-template or upstream chart via OCIRepository/HelmRepository
    kustomization.yaml  # Kustomize manifest listing resources in app/
    externalsecret.yaml # Secrets pulled from OpenBao via external-secrets
```

**`ks.yaml` key points:**

- Use `components/kopiur` to wire up daily Kopia backups to Cloudflare R2.
  - Set `postBuild.substitute` with `APP: *app` at minimum when using this component.
  - Override `KOPIUR_UID` / `KOPIUR_GID` when the app's PVC is owned by something other than 1000, or the mover cannot read it.
- Postgres apps add `dependsOn: {name: pg18vc-tenants, namespace: database}` — the `Kustomization` holding every app's `Database`, `DatabaseRole` and pushed credentials, which itself depends on the CNPG cluster's `pg18vc`.

**`helmrelease.yaml` key points:**

- Do not add install/upgrade/rollback boilerplate. `kubernetes/cluster/ks.yaml` injects global defaults into every HelmRelease via a nested Kustomization patch.
  - To opt a `HelmRelease` out of global defaults (e.g. needs `crds: Skip` or `driftDetection.mode: disabled`), add `labels: { kantai.xyz/no-hr-defaults: "true" }` to the `HelmRelease` `metadata` and set all required fields explicitly.
- Annotate the resource that owns `Pods` (e.g. a `Deployment`, `StatefulSet`, `DaemonSet`, etc) with `reloader.stakater.com/auto: "true"` when secrets are used.
- Lock down the security context: `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities: {drop: ["ALL"]}`, `readOnlyRootFilesystem: true`.
- Routes use `parentRefs: [{name: envoy-internal, namespace: network}]` for LAN/tailnet-only services, or `envoy-external` for public internet.
- Postgres apps mount the `<app>-pg` client certificate and connect with `sslmode=verify-full`; see PostgreSQL Apps.

**Resources and probes:**

Both default to unset. Add either one only when there is a specific reason to, and let the reason be visible in the manifest.

- Do not set `resources`. Most workloads here run without CPU or memory requests and limits, and land in the `BestEffort` QoS class on purpose. Add `resources` only for a device the scheduler must allocate (`resources.claims` against the `any-nvidia-gpu` ResourceClaimTemplate; see `docs/gpu-dra.md`), to shape scheduling for a genuinely large workload, or to cap a workload with a known appetite.
- Do not override probe timings. Set `enabled: true` plus, for a `custom: true` probe, the `httpGet` or `exec` block, and stop there — Kubernetes supplies the rest (`periodSeconds: 10`, `timeoutSeconds: 1`, `failureThreshold: 3`). Restating those values adds noise without changing behavior. The override that does earn its place is a `startup` probe `failureThreshold` for an app that is genuinely slow to come up, which buys startup time without slackening liveness afterwards.

`photon` shows both exceptions together: its liveness and readiness probes are bare `httpGet`, its startup probe raises `failureThreshold` to 720 for a two-hour startup budget while the geocoding index loads, and it declares `memory` requests and limits (4Gi/10Gi) for that index. See `kubernetes/apps/default/photon/app/helmrelease.yaml`.

**`externalsecret.yaml` key points:**

- `ClusterSecretStore` name: `openbao`.
- App secret uses `dataFrom.extract.key: <appname>`.
- A single field is `key: <item>` plus `property: <field>`. The 1Password `key: <item>/<field>` form no longer resolves.

**Registering a new `Kustomization`:** Add `- ./<appname>/ks.yaml` to `kubernetes/apps/<namespace>/kustomization.yaml` in alphabetical order.

**Adding a namespace:** create `kubernetes/apps/<namespace>/` with `namespace.yaml` (name `.invalid` — the global `NamespaceTransformer` rewrites it), `kustomization.yaml` listing the namespace and each app's `ks.yaml`, and `transformers/kustomization.yaml` setting `namespace: <namespace>`. Add `components/common` always, and `components/kopiur/secret` when any app in it takes backups. There is no top-level `kubernetes/apps/kustomization.yaml`; Flux discovers namespace directories on its own. Infrastructure gets its own namespace rather than being pooled into `default`.

## Comments in Manifests

Comments should almost never appear in manifests under `kubernetes/`. The manifest, its Git history and this file explain the common case; a comment that restates a field, labels a section, or narrates why a change was made is noise that rots.

A comment earns its place in only two cases:

- **A tracked upstream problem.** A `TODO` that cites the specific issue, bug or PR by URL and says what to undo once it is fixed, e.g. `# TODO: drop once https://github.com/org/repo/issues/123 ships`.
- **A genuinely unusual set of lines.** Something a reader would otherwise "fix" back to the convention: a deliberate deviation from the patterns above, a non-obvious workaround, or a value whose reason cannot be recovered from the manifest itself. Keep it to one or two lines at that spot.

Machine-read directives are not comments in this sense and stay: `# yaml-language-server: $schema=…` and `# renovate: …`. Do not leave commented-out YAML behind; delete it, since Git keeps it.

## Pod Security

`kubernetes/vap/` binds a `ValidatingAdmissionPolicy` to every namespace labelled `pod-security.kubernetes.io/enforce: restricted`. A separate baseline policy applies to every namespace not labelled `privileged`.

Label a namespace `restricted` only when every image in it can meet that bar. Use `baseline` when an image starts as root and drops privileges itself, or when its runtime uid is undocumented. Use `privileged` only where the workload genuinely needs it and keep unrelated workloads out of that namespace, since it is the blast radius.

Both PSA and the policy read the pod *spec*, not the running process, so an upstream manifest that declares no `securityContext` fails even when its image already runs unprivileged. That is a missing declaration, not a real capability requirement: patch the fields in rather than downgrading the namespace. `agent-sandbox` is a good example. See `kubernetes/apps/agent-sandbox-system/agent-sandbox/app/kustomization.yaml`.

## Secrets & SOPS

`.sops.yaml` covers `bootstrap/` and `talos/` directories only. These use SOPS age encryption. Kubernetes secrets come entirely from OpenBao via `external-secrets`; there are no SOPS-encrypted files under `kubernetes/`.

OpenBao runs on [etincelle](https://github.com/jfroy/etincelle), outside the cluster, and serves KV v2 mounted at `kantai/` from `https://bao.etincelle.cloud`. The `openbao` `ClusterSecretStore` authenticates with a projected ServiceAccount token (`external-secrets/external-secrets`, audience `openbao`) that OpenBao validates against a static copy of the cluster's JWKS: no bootstrap secret in the cluster, and no callback from OpenBao to the API server. `serviceAccountRef` in a `ClusterSecretStore` must carry `namespace` — without it, every `ExternalSecret` outside `external-secrets` fails while the store still reports Ready.

Bootstrap secrets that cannot come from OpenBao — its seal key, recovery key and root token, the R2 backup credentials, etincelle's own provisioning items — stay in 1Password (vault `kantai`). Rotating the cluster's ServiceAccount signing key means re-running `task openbao-init` on etincelle with a fresh `kubectl get --raw /openid/v1/jwks`.

**Never generate a secret that encrypts data at rest.** Regenerating one makes everything it encrypted permanently undecryptable — with no error at the time, only later failures to read — and a rebuilt cluster regenerates every `Password` generator by definition. Encryption keys, peppers and salts live in OpenBao like any other secret and are read with `dataFrom.extract`. The generator is for values that can be reissued, where the worst case is a logout or a service restart.

Judge a value by what breaks when it changes, not by its name. `LITELLM_SALT_KEY` encrypts provider credentials in LiteLLM's database and its documentation says never to change it; Paperless's `SECRET_KEY` and Zipline's `CORE_SECRET` have the same shape but only sign sessions, so those stay generated. Where upstream does not say, assume it encrypts something.

Already adopted for this reason: `homebox-keys`, `pocket-id-keys`, `filebrowser-keys`, `open-webui-keys`, `litellm-salt`, `openshell-cek`, `kite`. `scripts/openbao-adopt-generated.py` records why each one moved and re-checks that OpenBao still matches the live Secret.

A generated value that is genuinely rotatable still belongs in its own `ExternalSecret`, separate from any other generated value in the same app, so that deleting a Secret to rotate one thing cannot take the other with it.

## Networking Architecture

- **Internal routes** → `envoy-internal` Gateway → Cilium BGP LB → accessible on LAN + tailnet
- **External routes** → `envoy-external` Gateway → Cloudflare Tunnel → public internet
- **DNS:** internal routes auto-registered in Unifi via `external-dns-unifi-webhook`; external routes auto-registered in Cloudflare
- **Domain:** `*.kantai.xyz` with wildcard cert from Let's Encrypt (DNS-01 via Cloudflare)
- All internal service hostnames follow `${APP_SUBDOMAIN:-${APP}}.kantai.xyz`

**Route kinds and DNS.** Each external-dns instance watches an explicit list of source types, so a route kind that is not listed simply gets no record.

`GRPCRoute` is deliberately **not** registered by `edns-cf-httproute-proxied`. That instance forces `--default-targets=external.kantai.xyz`, which is the Cloudflare Tunnel, and Cloudflare supports gRPC over Tunnel only via private subnet routing — public hostname deployments are not supported. gRPC on the internal path works because those records are DNS-only (grey cloud), so Cloudflare never proxies them and the zone's gRPC setting is irrelevant. Anything gRPC therefore belongs on `envoy-internal`.

## Object Storage

Rook-Ceph provides S3-compatible object storage. It can be used with path-style or virtual-host-style (preferred) via `<bucket>.s3.kantai.xyz`. It is available from inside and outside the cluster (via Tailscale).

## PostgreSQL Apps

CNPG cluster: `pg18vc-rw.database.svc.cluster.local`. Roles and databases are declared with CNPG's `DatabaseRole` and `Database` resources, one file per app in `kubernetes/apps/database/cnpg/tenants/<app>.yaml`, reconciled by the `pg18vc-tenants` `Kustomization`. Nothing in an app's namespace holds the superuser password.

Apps authenticate with client certificates, not passwords. Each app's `DatabaseRole` sets `clientCertificate: {}`, `disablePassword: true` and membership in `cert_login`, and `pg18vc` carries `hostssl all +cert_login all cert` in `pg_hba`. CNPG signs the certificate with the cluster's client CA (`pg18vc-ca`, operator-managed), stores it in `<app>-client-cert` beside the role, and renews it before its 90 days run out.

CNPG requires the role, the database and that Secret to live in the `Cluster`'s namespace, so the certificate is pushed outward the same way LiteLLM keys are:

1. `PushSecret <app>` copies `tls.crt` and `tls.key` from `<app>-client-cert` to `<app>-pg` in the app's namespace through the `push-<namespace>` `SecretStore`. Its `dataTo` entry pushes only those keys, and `targetMergePolicy: Ignore` keeps the source's labels off the copy.
2. The consumer namespace adds `components/pg18vc-push` to its `kustomization.yaml`, granting the `database/pg18vc-push` ServiceAccount write access to its Secrets.
3. The app mounts `<app>-pg` at `/etc/postgresql/client` with `defaultMode: 0440`, and trust-manager's `cluster-ca.crt` ConfigMap at `/etc/postgresql/ca`. It connects with `sslmode=verify-full`, `sslrootcert=/etc/postgresql/ca/ca.crt`, `sslcert=/etc/postgresql/client/tls.crt` and `sslkey=/etc/postgresql/client/tls.key`, as connection-string parameters or the `PGSSLMODE`/`PGSSLROOTCERT`/`PGSSLCERT`/`PGSSLKEY` variables that libpq, lib/pq, pgx, asyncpg and Npgsql all read. Connection strings carry no secret, so they live in the HelmRelease.

The key file mode matters: libpq and lib/pq refuse a key readable by anyone but its owner and group, and a Secret volume is owned by root, so `0440` plus the pod's `fsGroup` is the combination that is both accepted and readable. Every pod that mounts the key needs an `fsGroup`. Reloader restarts apps when the certificate renews; drivers that load it once at startup (node-postgres, postgres.js) depend on that.

Prisma (LiteLLM) speaks its own dialect: `sslmode=require&sslaccept=strict`, `sslcert` as the *server root* and `sslidentity` as a PKCS#12 client identity. `sslcert` loads exactly one PEM certificate, so LiteLLM mounts trust-manager's single-root `cluster-ca-root.crt` instead of the default-CA bundle, and its `PushSecret` templates `identity.p12` from the CNPG certificate with ESO's `pemToPkcs12` (go-pkcs12 `Modern` encoding, which OpenSSL 3 reads; empty password, Prisma's default). See `tenants/litellm.yaml` and `kubernetes/apps/litellm/litellm/app/litellmproxy.yaml`.

Extensions go in `Database.spec.extensions`, listed dependencies first: CNPG runs a plain `CREATE EXTENSION` with no `CASCADE`, so `vchord` needs `vector` before it and `earthdistance` needs `cube`. `pg18vc` carries `postgis`, `timescaledb`, `timescaledb-toolkit` and `vchord`. See `tenants/immich.yaml`.

Leave `databaseReclaimPolicy` and `databaseRoleReclaimPolicy` at their `retain` default, so deleting a manifest never drops data. Deleting a `DatabaseRole` always deletes its certificate Secret, whatever the policy. A `DatabaseRole` pointed at an existing role adopts it and forces every omitted attribute back to its default, revoking memberships not listed in `inRoles`; declare any group role the app depends on (`tenants/toolhive-registry.yaml`) rather than letting the app create it.

## Inference (LiteLLM)

All in-cluster inference goes through the LiteLLM proxy at `http://litellm.litellm.svc.cluster.local:4000/v1`. Apps must not hold upstream provider keys directly. Models are declared as `LiteLLMModel` resources in `kubernetes/apps/litellm/litellm/app/litellmmodels.yaml`; embeddings are served by `text-embedding-3-small` at 1536 dimensions.

Per-consumer keys are `LiteLLMVirtualKey` resources. The operator resolves `spec.proxyRef` in the resource's *own* namespace and writes the generated Secret there, so these must live in the `litellm` namespace beside the `LiteLLMProxy` — they cannot be declared in the consuming app's namespace.

Delivery across the namespace boundary uses external-secrets' `kubernetes` provider, **pushing outward from `litellm`** rather than letting consumers pull:

1. `LiteLLMVirtualKey` in `litellm` generates Secret `litellm-vk-<app>`, key `LITELLM_API_KEY`.
2. A `SecretStore` in `litellm` (`push-<app>`) sets `remoteNamespace: <app>` and authenticates as the `litellm-key-push` ServiceAccount.
3. A `PushSecret` in `litellm` (`deletionPolicy: Delete`) writes Secret `<app>-litellm` into that namespace. `remoteRef.property` renames the key to whatever the app expects (`OPENAI_API_KEY`, `MEMINI_EMBED_API_KEY`, …), so no templating and no `ExternalSecret` on the consumer side.
4. The consumer namespace grants the push by adding `components/litellm-key-push` to its `kustomization.yaml` — a `Role`/`RoleBinding` for that ServiceAccount. It goes on the *namespace* kustomization, not the app's, so it is applied by `cluster-apps` directly and does not create a dependency cycle with the app's own `dependsOn: litellm`.

The direction is the point. A `SecretStore` grants `get`/`list`/`watch` on **every** Secret in its `remoteNamespace`, and RBAC `resourceNames` cannot restrict `list`/`watch`. Pointing consumers at `remoteNamespace: litellm` would therefore hand every consumer the proxy master key and the upstream provider keys, defeating virtual keys entirely. Pushing instead gives the trusted namespace write access to the untrusted ones and gives consumers no read access at all. Apply the same reasoning to any future cross-namespace secret: push from the owner, never pull from the vault.

Cross-namespace delivery uses the `kubernetes` provider, not the vault. The 1Password rate limit that used to forbid a push loop is gone, but routing through OpenBao would be worse: the `kantai-eso` policy grants the single ESO ServiceAccount read on all of `kantai/data/*`, so a consumer's `ExternalSecret` could read any secret in the vault. Pushing is what keeps the master key out of consumer namespaces.

`memini` pushes its `MEMINI_API_KEY` to `opencode` the same way (`components/memini-key-push`), so the MCP bearer token is never hand-copied.

Always set `spec.models` on a virtual key — an empty list grants every model on the proxy. Deleting the `LiteLLMVirtualKey` revokes the key in LiteLLM, and `deletionPolicy: Delete` removes the pushed Secret, so the consumer fails loudly rather than serving a key the proxy no longer honours.

Apps that are themselves gateways (`omniroute`) are the exception: their upstream providers live in their own encrypted store and are configured through their UI, not from the manifest.

Every chat model on this proxy is a reasoning model, and hidden reasoning tokens are spent inside the completion budget. An app that caps completion length — and most default to something near 4096 — will get truncated responses back (`finish_reason: "length"`) rather than an error, so raise that cap explicitly when wiring one up. `memini` sets `MEMINI_LLM_MAX_TOKENS` for this reason.

**memini** pins `MEMINI_EMBED_MODEL` and `MEMINI_EMBED_DIMS`. Vectors from different embedding models are not comparable, so memini records which model produced a store's vectors and refuses to start when the model changes. Changing the model means running `memini reembed`; changing the dimensionality means a fresh store (`memini export`, then `import`). Its `MEMINI_LLM_BASE_URL` enables write-time distillation, background consolidation, `POST /v1/answer` and the `memory_answer` MCP tool; `MEMINI_RERANK` stays `off`, since that one is priced per recall rather than per write.

## Talos Configuration

Managed by [topf](https://github.com/postfinance/topf) ([docs](https://postfinance.github.io/topf/main/)), which replaced talhelper after it was archived.

`talos/topf.yaml` holds cluster identity, the Talos and Kubernetes versions, and the node list — nothing else. Everything that shapes a machine config is a strategic-merge patch file, merged per node in this order: `all/`, then `control-plane/` (or `worker/`), then `node/<host>/`, lexicographically within each directory. Filename prefixes go in tens (`10-`, `20-`, `30-`…) per directory. Group kinds that can only appear once; give a repeatable (named) kind or a `.tpl` its own file.

- `*.yaml` patches are read through SOPS and vals; `*.yaml.tpl` patches are Go templates (sprig, `missingkey=error`) and skip that pipeline. Template context: `.ClusterName`, `.Data.<key>`, `.Node.Host`, `.Node.IP`, `.Node.Role`, `.Node.Data.<key>`.
- RFC 6902 JSON patches are not supported. Remove a field with `$patch: delete`.
- A patch file that is only comments is skipped, so a disabled block can stay in the tree.
- `machine.install.image` is generated by topf from `factory` + node `schematicId` + `talosVersion` + `secureboot` and applied before any patch, so a patch can override it. Node `secureboot: true` ORs with the cluster value — it cannot turn secure boot off.
- topf does **not** set the hostname from `host`; `all/20-hostname.yaml.tpl` does that. Without it Talos renames nodes to `talos-xxx-xxx`.
- Schematics live in `talos/schematics/<host>.yaml` and are referenced as `schematicId: "@schematics/<host>.yaml"`. topf hashes them locally; a brand-new schematic needs `topf schematic-ids --submit-to-factory` once so `tif.etincelle.cloud` can build it.
- Secrets: `talos/secrets.sops.yaml` (the Talos secrets bundle, pointed at by `secretsPath`). The former `talenv.sops.yaml` values live in the SOPS-encrypted `data` block of `topf.yaml` and are referenced as `{{ .Data.<KEY> }}` from `.tpl` patches; only `data` is encrypted, so versions and node definitions stay diffable.
- `topf render` writes plaintext secrets to `talos/output/`. Delete it when done.
- Config format: the tree is Talos 1.14 multi-document throughout. A v1alpha1 field and its replacement document are mutually exclusive, so a conversion is a move, not an addition. `cluster.etcd` and `machine.certSANs` are the two things still written as v1alpha1, because 1.14 deprecates neither and provides no document kind for them. See [docs/talos-config.md](docs/talos-config.md).

topf talks to each node's `:50000` directly — there is no apid proxying through a control plane, so it must run from a network that reaches every node.

## Renovate

Renovate automatically opens PRs for container image and Helm chart updates. Minor/patch updates auto-merge; major updates require manual approval. Image tags in `helmrelease.yaml` should include digest pins. Renovate runs in-cluster and is triggered by a GitHub webhook and polling.

## CI

Konflate renders the cluster with and without a PR's changes and posts the diff and a sentiment analysis to the PR. Konflate runs in-cluster and is triggered by a GitHub webhook and polling.
