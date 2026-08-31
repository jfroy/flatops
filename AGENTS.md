# AGENTS.md

This file provides guidance to agents when working in this repo (`flatops`) which controls the `kantai` homelab cluster.

## Cluster

This is **kantai**, a Kubernetes cluster running Talos Linux, with a mix of bare-metal and virtual nodes, managed entirely through GitOps via FluxCD. The FluxC instance syncs `refs/heads/main` from `https://github.com/jfroy/flatops` at `kubernetes/cluster`, which contains the top-level `Kustomizations`.

## Agent Safety Rules

- This repository is the source of truth. Make changes in Git, validate locally, and let Flux reconcile them.
- Do not run mutating `kubectl` commands unless the user explicitly authorizes the exact action first.
- Forbidden without prior authorization: `kubectl apply`, `create`, `delete`, `replace`, `patch`, `edit`, `scale`, `rollout restart`, `annotate`, `label`, `cordon`, `drain`, and any other command that changes live cluster state.
- Read-only inspection is allowed: `kubectl get`, `describe`, `logs`, `events`, `top`, `auth can-i`, and `diff` or `apply --dry-run=server`.
- Prefer the Flux MCP server, `flux` read commands, and local rendering/diff tools for troubleshooting. If a live change is necessary, stop and ask first with the exact command and reason.
- `flux reconcile ...` is allowed only to ask Flux to apply committed Git state or when explicitly requested by the user; do not use Flux as a substitute for direct manifest application.
- Always specify the context when using `flux` or `kubectl`.

## Flux MCP Server

A Flux MCP server may be available. Use it to inspect live cluster state when troubleshooting.

## Maintenance Commands

```sh
# Talos: regenerate node configs from talconfig.yaml and apply
task talos:gen-mc
task talos:apply-mc

# Apply to a single node
task talos:apply-node HOSTNAME=kantai1
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
talos/              # talhelper config (talconfig.yaml + SOPS-encrypted secrets)
bootstrap/          # One-time cluster bootstrap (currently broken/unused)
```

## Helm Chart Strategy

Two categories of deployments exist in this cluster:

- **app-template apps** — containerized applications without their own Helm chart. These use the `bjw-s-labs/app-template` chart, which provides a generic, highly-configurable template for deploying arbitrary containers. This covers most user-facing apps under `kubernetes/apps/default/`.
- **official-chart apps** — cloud-native projects and infrastructure components that ship their own Helm chart (e.g. cert-manager, external-secrets, cilium, CNPG, Flux itself). Always prefer the upstream chart for these; only fall back to app-template if the official chart has a serious problem.

`OCIRepository` sources are strongly preferred over `HelmRepository` sources. When an upstream chart is not available as an OCI artifact, pull it via the cluster's `ocharted` on-demand OCI mirror.

`OCIRepository` has no `tag@digest` form — `ref.tag` and `ref.digest` are separate fields and the digest wins. To pin a chart by digest, set both: the tag keeps the pin readable and gives Renovate something to bump. See `kubernetes/apps/openshell/openshell/app/ocirepository.yaml`.

When a project ships release manifests instead of a chart, reference the release URL directly from `app/kustomization.yaml` with a `# renovate: datasource=github-releases depName=org/repo` comment above it. Apply upstream unmodified — including its own namespace — rather than relocating it: a kustomize `namespace:` directive rewrites `ClusterRoleBinding` subjects and webhook service references but silently leaves `cert-manager.io/inject-ca-from` annotations pointing at the old namespace. Delete only upstream's bare `Namespace` object (`$patch: delete`) so this repo's `namespace.yaml` owns it with PSA labels and the common component's prune-disabled annotation. See `kubernetes/apps/agent-sandbox-system/agent-sandbox/app/` and `kubernetes/apps/cnpg-system/barman-cloud/app/`.

When an upstream chart offers no hook for something this repo needs — most often an init container — inject it with `spec.postRenderers[].kustomize.patches` rather than forking the chart. Prefer a strategic-merge patch over JSON6902 when the target path may not exist in the rendered output (a JSON6902 `add` on a child of an absent map fails). See `kubernetes/apps/openshell/openshell/app/helmrelease.yaml`, which patches in the standard `postgres-init` container this way.

## App Pattern (kubernetes/apps/default/)

App-template apps follow the same four-file layout:

```txt
<appname>/
  ks.yaml               # Flux Kustomization — registers the app with Flux
  app/
    helmrelease.yaml    # HelmRelease — app-template or upstream chart via OCIRepository/HelmRepository
    kustomization.yaml  # Kustomize manifest listing resources in app/
    externalsecret.yaml # Secrets pulled from 1Password via external-secrets
```

**`ks.yaml` key points:**

- Use `components/kopiur` to wire up daily Kopia backups to Cloudflare R2.
  - Set `postBuild.substitute` with `APP: *app` at minimum when using this component.
  - Override `KOPIUR_UID` / `KOPIUR_GID` when the app's PVC is owned by something other than 1000, or the mover cannot read it.
- Postgres apps add `dependsOn: {name: pg18vc, namespace: database}` — that is the CNPG cluster's own `Kustomization`, not the operator's.

**`helmrelease.yaml` key points:**

- Do not add install/upgrade/rollback boilerplate. `kubernetes/cluster/ks.yaml` injects global defaults into every HelmRelease via a nested Kustomization patch.
  - To opt a `HelmRelease` out of global defaults (e.g. needs `crds: Skip` or `driftDetection.mode: disabled`), add `labels: { kantai.xyz/no-hr-defaults: "true" }` to the `HelmRelease` `metadata` and set all required fields explicitly.
- Annotate the resource that owns `Pods` (e.g. a `Deployment`, `StatefulSet`, `DaemonSet`, etc) with `reloader.stakater.com/auto: "true"` when secrets are used.
- Lock down the security context: `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities: {drop: ["ALL"]}`, `readOnlyRootFilesystem: true`.
- Routes use `parentRefs: [{name: envoy-internal, namespace: network}]` for LAN/tailnet-only services, or `envoy-external` for public internet.
- Postgres apps use `ghcr.io/home-operations/postgres-init` as `initContainers.init-db` with complimentary secrets.

**Resources and probes:**

Both default to unset. Add either one only when there is a specific reason to, and let the reason be visible in the manifest.

- Do not set `resources`. Most workloads here run without CPU or memory requests and limits, and land in the `BestEffort` QoS class on purpose. Add `resources` only for a device the scheduler must allocate (`nvidia.com/gpu`), to shape scheduling for a genuinely large workload, or to cap a workload with a known appetite.
- Do not override probe timings. Set `enabled: true` plus, for a `custom: true` probe, the `httpGet` or `exec` block, and stop there — Kubernetes supplies the rest (`periodSeconds: 10`, `timeoutSeconds: 1`, `failureThreshold: 3`). Restating those values adds noise without changing behavior. The override that does earn its place is a `startup` probe `failureThreshold` for an app that is genuinely slow to come up, which buys startup time without slackening liveness afterwards.

`photon` shows both exceptions together: its liveness and readiness probes are bare `httpGet`, its startup probe raises `failureThreshold` to 720 for a two-hour startup budget while the geocoding index loads, and it declares `memory` requests and limits (4Gi/10Gi) for that index. See `kubernetes/apps/default/photon/app/helmrelease.yaml`.

**`externalsecret.yaml` key points:**

- `ClusterSecretStore` name: `onepassword`.
- App secret uses `dataFrom.extract.key: <appname>`.
- Postgres `-db` secret generates a password via `generators.external-secrets.io/v1alpha1/Password/password32` and populates CNPG connection vars.
- Postgres `-initdb` secret pulls the CNPG superuser password from `cnpg-pg18vc/password`.

**Registering a new `Kustomization`:** Add `- ./<appname>/ks.yaml` to `kubernetes/apps/<namespace>/kustomization.yaml` in alphabetical order.

**Adding a namespace:** create `kubernetes/apps/<namespace>/` with `namespace.yaml` (name `.invalid` — the global `NamespaceTransformer` rewrites it), `kustomization.yaml` listing the namespace and each app's `ks.yaml`, and `transformers/kustomization.yaml` setting `namespace: <namespace>`. Add `components/common` always, and `components/kopiur/secret` when any app in it takes backups. There is no top-level `kubernetes/apps/kustomization.yaml`; Flux discovers namespace directories on its own. Infrastructure gets its own namespace rather than being pooled into `default`.

## Pod Security

`kubernetes/vap/` binds a `ValidatingAdmissionPolicy` to every namespace labelled `pod-security.kubernetes.io/enforce: restricted`. A separate baseline policy applies to every namespace not labelled `privileged`.

Label a namespace `restricted` only when every image in it can meet that bar. Use `baseline` when an image starts as root and drops privileges itself, or when its runtime uid is undocumented. Use `privileged` only where the workload genuinely needs it and keep unrelated workloads out of that namespace, since it is the blast radius.

Both PSA and the policy read the pod *spec*, not the running process, so an upstream manifest that declares no `securityContext` fails even when its image already runs unprivileged. That is a missing declaration, not a real capability requirement: patch the fields in rather than downgrading the namespace. `agent-sandbox` is a good example. See `kubernetes/apps/agent-sandbox-system/agent-sandbox/app/kustomization.yaml`.

## Secrets & SOPS

`.sops.yaml` covers `bootstrap/` and `talos/` directories only. These use SOPS age encryption. Kubernetes secrets come entirely from 1Password via `external-secrets`; there are no SOPS-encrypted files under `kubernetes/`.

**Non-rotatable secrets.** Some generated values encrypt data at rest, and regenerating one makes everything it encrypted permanently undecryptable — with no error at the time, only later failures to read. Keep each in its own `ExternalSecret` with `refreshInterval: "0"`, separate from any rotatable value in the same app, so that deleting a Secret to rotate one thing cannot take the other with it. Current members of this set:

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

CNPG cluster: `pg18vc-rw.database.svc.cluster.local`. Apps provision their own database via the `init-db` init container. Each app needs three ExternalSecrets: `<app>`, `<app>-db`, `<app>-initdb`.

`postgres-init` also runs arbitrary SQL as the superuser after creating the role and database: mount a ConfigMap at `/initdb` and it executes `/initdb/<INIT_POSTGRES_DBNAME>.sql`. **The filename must match the database name** — rename one without the other and the SQL is silently skipped. This is the only hook that can install extensions, since `CREATE EXTENSION` needs superuser while `postgres-init` otherwise leaves the app user as a plain owner. `pg18vc` carries `postgis`, `timescaledb`, `timescaledb-toolkit` and `vchord`; any app wanting one needs this file. See `kubernetes/apps/default/immich/app/immich.sql` and `kubernetes/apps/memini/memini/app/memini.sql`.

## Inference (LiteLLM)

All in-cluster inference goes through the LiteLLM proxy at `http://litellm.litellm.svc.cluster.local:4000/v1`. Apps must not hold upstream provider keys directly. Models are declared as `LiteLLMModel` resources in `kubernetes/apps/litellm/litellm/app/litellmmodels.yaml`; embeddings are served by `text-embedding-3-small` at 1536 dimensions.

Per-consumer keys are `LiteLLMVirtualKey` resources. The operator resolves `spec.proxyRef` in the resource's *own* namespace and writes the generated Secret there, so these must live in the `litellm` namespace beside the `LiteLLMProxy` — they cannot be declared in the consuming app's namespace.

Delivery across the namespace boundary uses external-secrets' `kubernetes` provider, **pushing outward from `litellm`** rather than letting consumers pull:

1. `LiteLLMVirtualKey` in `litellm` generates Secret `litellm-vk-<app>`, key `LITELLM_API_KEY`.
2. A `SecretStore` in `litellm` (`push-<app>`) sets `remoteNamespace: <app>` and authenticates as the `litellm-key-push` ServiceAccount.
3. A `PushSecret` in `litellm` (`deletionPolicy: Delete`) writes Secret `<app>-litellm` into that namespace. `remoteRef.property` renames the key to whatever the app expects (`OPENAI_API_KEY`, `MEMINI_EMBED_API_KEY`, …), so no templating and no `ExternalSecret` on the consumer side.
4. The consumer namespace grants the push by adding `components/litellm-key-push` to its `kustomization.yaml` — a `Role`/`RoleBinding` for that ServiceAccount. It goes on the *namespace* kustomization, not the app's, so it is applied by `cluster-apps` directly and does not create a dependency cycle with the app's own `dependsOn: litellm`.

The direction is the point. A `SecretStore` grants `get`/`list`/`watch` on **every** Secret in its `remoteNamespace`, and RBAC `resourceNames` cannot restrict `list`/`watch`. Pointing consumers at `remoteNamespace: litellm` would therefore hand every consumer the proxy master key and the upstream provider keys, defeating virtual keys entirely. Pushing instead gives the trusted namespace write access to the untrusted ones and gives consumers no read access at all. Apply the same reasoning to any future cross-namespace secret: push from the owner, never pull from the vault.

Do not use 1Password `PushSecret` for this. 1Password's API limits are low, and writes invalidate the `onepassword` store's read cache, so a push loop costs far more than its own call count.

`memini` pushes its `MEMINI_API_KEY` to `opencode` the same way (`components/memini-key-push`), so the MCP bearer token is never hand-copied.

Always set `spec.models` on a virtual key — an empty list grants every model on the proxy. Deleting the `LiteLLMVirtualKey` revokes the key in LiteLLM, and `deletionPolicy: Delete` removes the pushed Secret, so the consumer fails loudly rather than serving a key the proxy no longer honours.

Apps that are themselves gateways (`omniroute`) are the exception: their upstream providers live in their own encrypted store and are configured through their UI, not from the manifest.

Every chat model on this proxy is a reasoning model, and hidden reasoning tokens are spent inside the completion budget. An app that caps completion length — and most default to something near 4096 — will get truncated responses back (`finish_reason: "length"`) rather than an error, so raise that cap explicitly when wiring one up. `memini` sets `MEMINI_LLM_MAX_TOKENS` for this reason.

**memini** pins `MEMINI_EMBED_MODEL` and `MEMINI_EMBED_DIMS`. Vectors from different embedding models are not comparable, so memini records which model produced a store's vectors and refuses to start when the model changes. Changing the model means running `memini reembed`; changing the dimensionality means a fresh store (`memini export`, then `import`). Its `MEMINI_LLM_BASE_URL` enables write-time distillation, background consolidation, `POST /v1/answer` and the `memory_answer` MCP tool; `MEMINI_RERANK` stays `off`, since that one is priced per recall rather than per write.

## Talos Configuration

Managed by [talhelper](https://github.com/budimanjojo/talhelper). Edit `talos/talconfig.yaml`, then run `task talos:gen-mc` to regenerate configs, then `task talos:apply-mc` or `task talos:apply-node` to apply. Secrets are in `talsecret.sops.yaml` and `talenv.sops.yaml` (SOPS-encrypted).

## Renovate

Renovate automatically opens PRs for container image and Helm chart updates. Minor/patch updates auto-merge; major updates require manual approval. Image tags in `helmrelease.yaml` should include digest pins. Renovate runs in-cluster and is triggered by a GitHub webhook and polling.

## CI

Konflate renders the cluster with and without a PR's changes and posts the diff and a sentiment analysis to the PR. Konflate runs in-cluster and is triggered by a GitHub webhook and polling.
