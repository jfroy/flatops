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
- Postgres apps add `dependsOn: cnpg-pg18vc` in `cnpg-system`

**`helmrelease.yaml` key points:**

- Do not add install/upgrade/rollback boilerplate. `kubernetes/cluster/ks.yaml` injects global defaults into every HelmRelease via a nested Kustomization patch.
  - To opt a `HelmRelease` out of global defaults (e.g. needs `crds: Skip` or `driftDetection.mode: disabled`), add `labels: { kantai.xyz/no-hr-defaults: "true" }` to the `HelmRelease` `metadata` and set all required fields explicitly.
- Annotate the resource that owns `Pods` (e.g. a `Deployment`, `StatefulSet`, `DaemonSet`, etc) with `reloader.stakater.com/auto: "true"` when secrets are used.
- Lock down the security context: `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities: {drop: ["ALL"]}`, `readOnlyRootFilesystem: true`.
- Routes use `parentRefs: [{name: envoy-internal, namespace: network}]` for LAN/tailnet-only services, or `envoy-external` for public internet.
- Postgres apps use `ghcr.io/home-operations/postgres-init` as `initContainers.init-db` with complimentary secrets.

**`externalsecret.yaml` key points:**

- `ClusterSecretStore` name: `onepassword`.
- App secret uses `dataFrom.extract.key: <appname>`.
- Postgres `-db` secret generates a password via `generators.external-secrets.io/v1alpha1/Password/password32` and populates CNPG connection vars.
- Postgres `-initdb` secret pulls the CNPG superuser password from `cnpg-pg18vc/password`.

**Registering a new `Kustomization`:** Add `- ./<appname>/ks.yaml` to `kubernetes/apps/<namespace>/kustomization.yaml` in alphabetical order.

## Secrets & SOPS

`.sops.yaml` covers `bootstrap/` and `talos/` directories only. These use SOPS age encryption. Kubernetes secrets come entirely from 1Password via `external-secrets`; there are no SOPS-encrypted files under `kubernetes/`.

## Networking Architecture

- **Internal routes** → `envoy-internal` Gateway → Cilium BGP LB → accessible on LAN + tailnet
- **External routes** → `envoy-external` Gateway → Cloudflare Tunnel → public internet
- **DNS:** internal routes auto-registered in Unifi via `external-dns-unifi-webhook`; external routes auto-registered in Cloudflare
- **Domain:** `*.kantai.xyz` with wildcard cert from Let's Encrypt (DNS-01 via Cloudflare)
- All internal service hostnames follow `${APP_SUBDOMAIN:-${APP}}.kantai.xyz`

## Object Storage

Rook-Ceph provides S3-compatible object storage. It can be used with path-style or virtual-host-style (preferred) via `<bucket>.s3.kantai.xyz`. It is available from inside and outside the cluster (via Tailscale).

## PostgreSQL Apps

CNPG cluster: `pg18vc-rw.database.svc.cluster.local`. Apps provision their own database via the `init-db` init container. Each app needs three ExternalSecrets: `<app>`, `<app>-db`, `<app>-initdb`.

## Talos Configuration

Managed by [talhelper](https://github.com/budimanjojo/talhelper). Edit `talos/talconfig.yaml`, then run `task talos:gen-mc` to regenerate configs, then `task talos:apply-mc` or `task talos:apply-node` to apply. Secrets are in `talsecret.sops.yaml` and `talenv.sops.yaml` (SOPS-encrypted).

## Renovate

Renovate automatically opens PRs for container image and Helm chart updates. Minor/patch updates auto-merge; major updates require manual approval. Image tags in `helmrelease.yaml` should include digest pins. Renovate runs in-cluster and is triggered by a GitHub webhook and polling.

## CI

Konflate renders the cluster with and without a PR's changes and posts the diff and a sentiment analysis to the PR. Konflate runs in-cluster and is triggered by a GitHub webhook and polling.
