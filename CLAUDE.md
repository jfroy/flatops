# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Cluster

This is **kantai**, a 3-node bare-metal Kubernetes cluster running Talos Linux, managed entirely through GitOps via FluxCD. The cluster name is `kantai`; all services live under `kantai.xyz`.

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

Flux reconciliation (use `kubectl` or `flux` CLI against `kantai.hyakutake-universe.ts.net`):

```sh
flux reconcile kustomization cluster-apps --with-source
flux reconcile helmrelease <name> -n <namespace>
```

## Repository Structure

```
kubernetes/
  cluster/          # Root Flux entrypoint (cluster-vap → cluster-apps)
  vap/              # ValidatingAdmissionPolicies (applied first)
  apps/             # One subdirectory per namespace
    default/        # Most user-facing applications
  components/       # Reusable Kustomize components
    common/         # Adds NS-level alerts + ExternalSecret + prune annotation
    volsync/        # Adds VolSync ReplicationSource + PVC to an app
    volsync-ns/     # Adds VolSync ExternalSecret at namespace level
  transformers/     # NamespaceTransformer applied globally
talos/              # talhelper config (talconfig.yaml + SOPS-encrypted secrets)
bootstrap/          # One-time cluster bootstrap (currently broken/unused)
```

## App Pattern (kubernetes/apps/default/)

Most apps follow the same four-file layout:

```
<appname>/
  ks.yaml               # Flux Kustomization — registers the app with Flux
  app/
    helmrelease.yaml    # HelmRelease using bjw-s-labs/app-template via OCIRepository
    kustomization.yaml  # Kustomize manifest listing resources in app/
    externalsecret.yaml # Secrets pulled from 1Password via external-secrets
```

**`ks.yaml` key points:**
- References `components/volsync` to wire up daily Kopia backups to Cloudflare R2
- Uses `postBuild.substitute` with `APP: *app` — this variable names the PVC and backup target
- Optionally `VOLSYNC_CAPACITY` (PVC size) and `APP_SUBDOMAIN` (if subdomain ≠ app name)
- Postgres apps add `dependsOn: cnpg-pg18vc` in `cnpg-system`

**`helmrelease.yaml` key points:**
- Use `chartRef.kind: OCIRepository`, name `app-template`, namespace `flux-system`
- Standard boilerplate: `driftDetection.mode: enabled`, `install.remediation.retries: -1`, `upgrade.cleanupOnFail: true`
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

## PostgreSQL Apps

CNPG cluster: `pg18vc-rw.database.svc.cluster.local` (PostgreSQL 18 with vchord extensions). Apps provision their own database via the `init-db` initContainer. Each postgres app needs three ExternalSecrets: `<app>`, `<app>-db`, `<app>-initdb`.

## Talos Configuration

Managed by [talhelper](https://github.com/budimanjojo/talhelper). Edit `talos/talconfig.yaml`, then run `task talos:gen-mc` to regenerate configs, then `task talos:apply-mc` or `task talos:apply-node` to apply. Secrets are in `talsecret.sops.yaml` and `talenv.sops.yaml` (SOPS-encrypted).

## Renovate

Renovate automatically opens PRs for container image and Helm chart updates. Minor/patch updates auto-merge; major updates require manual approval. Image tags in `helmrelease.yaml` include digest pins — Renovate manages both the tag and digest together.

## CI

- `flux-diff.yaml` — runs `flux-local diff` on PRs touching `kubernetes/`; comments rendered diffs
