# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Agent Safety Rules

- This repository is the source of truth. Make changes in Git, validate locally, and let Flux reconcile them.
- Do not run mutating `kubectl` commands unless the user explicitly authorizes the exact action first.
- Forbidden without prior authorization: `kubectl apply`, `create`, `delete`, `replace`, `patch`, `edit`, `scale`, `rollout restart`, `annotate`, `label`, `cordon`, `drain`, and any other command that changes live cluster state.
- Read-only inspection is allowed: `kubectl get`, `describe`, `logs`, `events`, `top`, `auth can-i`, and `diff` or `apply --dry-run=server`.
- Prefer the Flux MCP server, `flux` read commands, and local rendering/diff tools for troubleshooting. If a live change is necessary, stop and ask first with the exact command and reason.
- `flux reconcile ...` is allowed only to ask Flux to apply committed Git state or when explicitly requested by the user; do not use Flux as a substitute for direct manifest application.

## Cluster

This is **kantai**, a Kubernetes cluster running Talos Linux, with a mix of bare-metal and virtual nodes, managed entirely through GitOps via FluxCD and Flux Operator. All services live under `kantai.xyz`. Current repo configuration targets Talos `v1.13.2` and Kubernetes `v1.36.1`. Flux syncs `refs/heads/main` from `https://github.com/jfroy/flatops` at `kubernetes/cluster`.

## Flux MCP Server

A Flux MCP server may be configured. Always use the `kantai.hyakutake-universe.ts.net` kubeconfig context. Use it to inspect live cluster state when troubleshooting — check HelmRelease/Kustomization status, events, and inventory before editing files.

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
  cluster/          # Root Flux entrypoint (cluster-vap -> cluster-apps)
  components/       # Reusable Kustomize components
    common/         # Adds NS-level alerts + ExternalSecret + prune annotation
    volsync/        # Adds VolSync ReplicationSource + PVC to an app
    volsync-ns/     # Adds VolSync ExternalSecret at namespace level
  transformers/     # NamespaceTransformer applied globally
  vap/              # ValidatingAdmissionPolicies applied before apps
talos/              # talhelper config (talconfig.yaml + SOPS-encrypted secrets)
bootstrap/          # One-time cluster bootstrap (currently broken/unused)
```

## Helm Chart Strategy

Two categories of deployments exist in this cluster:

- **app-template apps** — containerized applications without their own Helm chart. These use the `bjw-s-labs/app-template` chart, which provides a generic, highly-configurable template for deploying arbitrary containers. This covers most user-facing apps under `kubernetes/apps/default/`.
- **official-chart apps** — cloud-native projects and infrastructure components that ship their own Helm chart (e.g. cert-manager, external-secrets, cilium, CNPG, Flux itself). Always prefer the upstream chart for these; only fall back to app-template if the official chart has a serious problem.

OCIRepository sources are strongly preferred over `HelmRepository` (HTTP/HTTPS) sources. When an upstream chart is not yet available as an OCI artifact, check the [home-operations/charts-mirror](https://github.com/home-operations/charts-mirror) community registry first — it mirrors many popular charts as OCI images at `ghcr.io/home-operations/charts-mirror`.

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

- References `components/volsync` to wire up daily Kopia backups to Cloudflare R2
- Uses `postBuild.substitute` with `APP: *app` — this variable names the PVC and backup target
- Optionally `VOLSYNC_CAPACITY` (PVC size) and `APP_SUBDOMAIN` (if subdomain ≠ app name)
- Postgres apps add `dependsOn: cnpg-pg18vc` in `cnpg-system`

**`helmrelease.yaml` key points:**

- app-template apps: `chartRef.kind: OCIRepository`, name `app-template`, namespace `flux-system`; official-chart apps: `chartRef.kind: OCIRepository` pointing to the upstream OCI registry or `ghcr.io/home-operations/charts-mirror` — fall back to `HelmRepository` only if no OCI source exists
- **Do not add install/upgrade/rollback boilerplate** — `kubernetes/cluster/ks.yaml` injects global defaults into every HelmRelease via a nested Kustomization patch: `driftDetection.mode: enabled`, `install.crds: CreateReplace`, `rollback.cleanupOnFail: true`, `upgrade.cleanupOnFail: true`, `upgrade.crds: CreateReplace`, `upgrade.strategy.name: RemediateOnFailure`, `upgrade.remediation.remediateLastFailure: true`, `upgrade.remediation.retries: 2`
- To opt a HelmRelease out of global defaults (e.g. needs `crds: Skip` or `driftDetection.mode: disabled`), add `labels: { kantai.xyz/no-hr-defaults: "true" }` to the HelmRelease `metadata` and set all required fields explicitly
- All containers get `reloader.stakater.com/auto: "true"` (restarts on secret change)
- Security context: `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities: {drop: ["ALL"]}`, `readOnlyRootFilesystem: true`
- Routes use `parentRefs: [{name: envoy-internal, namespace: network}]` for LAN/tailnet-only services, `envoy-external` for public internet
- Postgres apps add an `initContainers.init-db` using `ghcr.io/home-operations/postgres-init`

**`externalsecret.yaml` key points:**

- `ClusterSecretStore` name: `onepassword`
- App secret uses `dataFrom.extract.key: <appname>`
- Postgres `-db` secret generates a password via `generators.external-secrets.io/v1alpha1/Password/password32` and populates CNPG connection vars
- Postgres `-initdb` secret pulls the CNPG superuser password from `cnpg-pg18vc/password`

**Registering a new app:** Add `- ./<appname>/ks.yaml` to `kubernetes/apps/default/kustomization.yaml` in alphabetical order.

## Secrets & SOPS

`.sops.yaml` covers `bootstrap/` and `talos/` directories only — these use SOPS age encryption. Kubernetes secrets come entirely from 1Password via `external-secrets`; there are no SOPS-encrypted files under `kubernetes/`.

The age private key is at `./age.key` in the repo root. `SOPS_AGE_KEY_FILE` is set in `Taskfile.yaml`.

## Networking Architecture

- **Internal routes** → `envoy-internal` Gateway → Cilium BGP LB → accessible on LAN + tailnet
- **External routes** → `envoy-external` Gateway → Cloudflare Tunnel → public internet
- **DNS:** internal routes auto-registered in Unifi via `external-dns-unifi-webhook`; external routes auto-registered in Cloudflare
- **Domain:** `*.kantai.xyz` with wildcard cert from Let's Encrypt (DNS-01 via Cloudflare)
- All internal service hostnames follow `${APP_SUBDOMAIN:-${APP}}.kantai.xyz`

## Object Storage

Rook-Ceph provides S3-compatible object storage. Recent configuration exposes in-cluster virtual-host-style S3 through `s3.kantai.xyz`; apps should prefer that endpoint when talking to Ceph object buckets from inside the cluster.

## PostgreSQL Apps

CNPG cluster: `pg18vc-rw.database.svc.cluster.local` (PostgreSQL 18 with vchord extensions). Apps provision their own database via the `init-db` initContainer. Each postgres app needs three ExternalSecrets: `<app>`, `<app>-db`, `<app>-initdb`.

## Talos Configuration

Managed by [talhelper](https://github.com/budimanjojo/talhelper). Edit `talos/talconfig.yaml`, then run `task talos:gen-mc` to regenerate configs, then `task talos:apply-mc` or `task talos:apply-node` to apply. Secrets are in `talsecret.sops.yaml` and `talenv.sops.yaml` (SOPS-encrypted).

## Renovate

Renovate automatically opens PRs for container image and Helm chart updates. Minor/patch updates auto-merge; major updates require manual approval. Image tags in `helmrelease.yaml` include digest pins — Renovate manages both the tag and digest together.

## CI

- `flate.yaml` — runs `flate diff` for changed Kubernetes manifests on PRs and Renovate pushes, uploading/commenting HelmRelease and Kustomization diffs
