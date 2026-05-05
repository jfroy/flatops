# Grimmory deployment design

**Date:** 2026-05-05
**Status:** Approved (pending implementation)
**App:** [grimmory](https://github.com/grimmory-tools/grimmory) v3.0.3 — self-hosted digital library / e-book reader, successor to booklore by the same author

## Goal

Deploy grimmory to the kantai cluster as a single-pod, internally-routed app following the booklore precedent (deleted in commit `6751a4d62`, see also `27157dae4`). Books library lives on the existing shared `media2` PVC.

## Non-goals

- BookDrop / ingest workflow (`/bookdrop` mount intentionally omitted; can be added later)
- Public internet exposure (internal-only via `envoy-internal`)
- External / shared MariaDB (pod-local sidecar instead)
- ExternalSecret for DB credentials (pod-local DB, isolated by NetworkPolicy)

## Architecture

A single bjw-s `app-template` HelmRelease deploys one pod with two containers:

1. **`grimmory`** (main container) — Java backend + Angular frontend, listens on port 6060
2. **`mariadb`** (sidecar via `initContainers` with `restartPolicy: Always`) — pod-local MariaDB reachable only at `localhost:3306`

The pod-local DB is correct here because grimmory is the only consumer, the cluster has no shared MariaDB, and bundling matches booklore's proven pattern. A `CiliumNetworkPolicy` denies all ingress to TCP/3306 on the pod, so weak DB credentials are acceptable.

The upstream grimmory Helm chart (`deploy/helm/grimmory/`) is not used because:
- It isn't published as an OCI artifact anywhere reachable
- Its `appVersion` (`v0.38.2`) is severely behind the current app (`v3.0.3`)
- It bundles a separate-StatefulSet MariaDB via the cloudpirates subchart, which adds moving parts for no benefit at this scale
- It diverges from the cluster's "OCI sources only, app-template by default" convention

The cloudpirates MariaDB chart was reviewed for inspiration; we adopt its image (`docker.io/library/mariadb:12.2.2`) but not its deployment shape.

## File layout

```text
kubernetes/apps/default/grimmory/
  ks.yaml                       # Flux Kustomization with VolSync component
  app/
    helmrelease.yaml             # bjw-s app-template HelmRelease (single pod, two containers)
    ciliumnetworkpolicy.yaml     # Deny all ingress to TCP/3306 on grimmory pod
    kustomization.yaml           # Lists helmrelease + ciliumnetworkpolicy
```

No `externalsecret.yaml` — there are no app-level secrets and the pod-local MariaDB uses inline credentials, matching booklore's resolved configuration after commit `27157dae4`.

Register by adding `- ./grimmory/ks.yaml` to `kubernetes/apps/default/kustomization.yaml` in alphabetical order.

## `ks.yaml`

Same shape as the booklore `ks.yaml` from commit `6751a4d62`:

- `metadata.name: &app grimmory`
- `commonMetadata.labels.app.kubernetes.io/name: *app`
- `components: [../../../../components/volsync]`
- `path: ./kubernetes/apps/default/grimmory/app`
- `prune: true`
- `sourceRef: flux-system / flux-system / GitRepository`
- `interval: 1h`, `retryInterval: 2m`, `timeout: 5m`
- `postBuild.substitute`:
  - `APP: *app`
  - `VOLSYNC_CAPACITY: 1Gi`

The `${APP}` PVC (named `grimmory`, ceph-blockpool, 1Gi) holds both grimmory's `/app/data` and mariadb's `/var/lib/mysql` — both small. VolSync replicates it daily to Cloudflare R2 via Kopia.

## `helmrelease.yaml`

bjw-s `app-template` via OCIRepository `app-template` in `flux-system`. Standard cluster boilerplate (`driftDetection.mode: enabled`, `install.remediation.retries: -1`, `upgrade.cleanupOnFail: true`, `upgrade.remediation.retries: 3`).

### Controllers

One controller `grimmory` with `annotations.reloader.stakater.com/auto: "true"`.

### `mariadb` initContainer (sidecar)

- **Image:** `docker.io/library/mariadb:12.2.2-noble@sha256:<digest>` (Renovate-managed)
- **`restartPolicy: Always`** — makes it a sidecar that starts before the main container and is restarted independently
- **Env:**
  ```yaml
  MARIADB_DATABASE: grimmory
  MARIADB_USER: grimmory
  MARIADB_PASSWORD: grimmory
  MARIADB_ROOT_PASSWORD: root
  TZ: America/Los_Angeles
  ```
- **Liveness probe:** `exec mariadb-admin ping --socket=/run/mysqld/mysqld.sock --user=root --password=root`
- **Container securityContext:**
  ```yaml
  allowPrivilegeEscalation: false
  capabilities: { drop: ["ALL"] }
  readOnlyRootFilesystem: true
  seccompProfile: { type: Unconfined }   # io_uring is blocked by containerd v2 default seccomp; see containerd/containerd#9320
  ```
- **Resources:** `requests: {cpu: 50m, memory: 256Mi}`, `limits: {memory: 512Mi}`

### `grimmory` container (main)

- **Image:** `ghcr.io/grimmory-tools/grimmory:v3.0.3@sha256:<digest>` (Renovate-managed)
- **Env:**
  ```yaml
  DATABASE_URL: jdbc:mariadb://localhost:3306/grimmory
  DATABASE_USERNAME: grimmory
  DATABASE_PASSWORD: grimmory
  USER_ID: "1000"
  GROUP_ID: "1000"
  APP_USER: grimmory
  TZ: America/Los_Angeles
  ```
  - `DISK_TYPE` is omitted; the default `LOCAL` (set in `application.yaml`) is correct for the local-ZFS-bound `media2` PVC.
  - `BOOKLORE_PORT` is omitted; `application.yaml` already defaults `server.port` to 6060.
- **Probes** (all on `httpGet /api/v1/healthcheck` against port 6060):
  ```yaml
  liveness:  enabled, custom, httpGet /api/v1/healthcheck : 6060
  readiness: same as liveness
  startup:   same, failureThreshold: 10   # JVM cold start
  ```
- **Container securityContext:**
  ```yaml
  allowPrivilegeEscalation: false
  capabilities: { drop: ["ALL"] }
  readOnlyRootFilesystem: true
  ```
- **Resources:** `requests: {cpu: 100m, memory: 1Gi}`, `limits: {memory: 2Gi}` — JVM `MaxRAMPercentage=60` keeps heap bounded by container memory.

### Pod

```yaml
labels:
  pod-security.kantai.xyz/allow-seccomp-unconfined: "true"
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
  fsGroupChangePolicy: OnRootMismatch
```

### Persistence

| Volume key | Type | Source | Mounts |
|---|---|---|---|
| `data` | persistentVolumeClaim, existingClaim `${APP}` | ceph-blockpool, 1Gi, VolSync→R2 | grimmory: `/app/data` (subPath `app`); mariadb: `/var/lib/mysql` (subPath `mysql`) |
| `media2` | persistentVolumeClaim, existingClaim `media2` | local ZFS bind on kantai1 | grimmory: `/books` (subPath `library/books`) |
| `mysqld` | emptyDir, `medium: Memory`, `sizeLimit: 1Mi` | n/a | mariadb: `/run/mysqld` (UNIX socket) |
| `tmp` | emptyDir, `medium: Memory`, `sizeLimit: 256Mi` | n/a | grimmory: `/tmp`; mariadb: `/tmp` |

Use `advancedMounts.<controller>.<container>` form (booklore precedent) so each container gets only the mounts it needs.

### Service

```yaml
service:
  grimmory:
    controller: grimmory
    ports:
      http:
        port: 6060
```

### Route

```yaml
route:
  grimmory:
    hostnames: ["grimmory.kantai.xyz"]
    parentRefs:
      - name: envoy-internal
        namespace: network
```

External-DNS will publish `grimmory.kantai.xyz` to Unifi automatically; the wildcard cert covers it.

## `ciliumnetworkpolicy.yaml`

Direct copy of booklore's pattern, retargeted to grimmory:

```yaml
apiVersion: "cilium.io/v2"
kind: CiliumNetworkPolicy
metadata:
  name: "deny-grimmory-mariadb-ingress"
spec:
  endpointSelector:
    matchLabels:
      app.kubernetes.io/name: grimmory
      app.kubernetes.io/instance: grimmory
  ingressDeny:
    - fromEndpoints:
        - {}
      toPorts:
        - ports:
            - port: "3306"
              protocol: TCP
```

## `kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./ciliumnetworkpolicy.yaml
  - ./helmrelease.yaml
```

## Differences from booklore

| Aspect | booklore | grimmory |
|---|---|---|
| nginx wrapper | yes (port 8080 → 6060 reverse proxy with envsubst init) | **no** — grimmory listens on 6060 directly |
| Books mount | `media-smb-kantai1` PVC | `media2` PVC |
| Bookdrop mount | included (`library/books/ingest`) | **omitted** |
| `nginx-config` initContainer | yes | **no** |
| `nginx` container | yes | **no** |
| `nginx`/`nginx-run` emptyDirs | yes | **no** |

These removals collapse the pod from four containers (booklore: nginx-config init, mariadb sidecar, grimmory, nginx) to two (mariadb sidecar, grimmory).

## Validation criteria

After Flux reconciles:
1. `flux get hr -n default grimmory` shows Ready=True
2. Pod `grimmory-*` in `default` namespace has both containers Ready
3. `kubectl exec -n default grimmory-<pod> -c grimmory -- curl -fsS http://localhost:6060/api/v1/healthcheck` returns 200
4. `https://grimmory.kantai.xyz` resolves on LAN/tailnet, returns the grimmory UI, allows admin account creation
5. `kubectl get cnp -n default deny-grimmory-mariadb-ingress` exists
6. From a different pod in the cluster, `nc -zv grimmory.default.svc.cluster.local 3306` is denied/blocked
7. VolSync ReplicationSource for `grimmory` is created and runs its first backup successfully

## Risks and mitigations

- **Entrypoint requires root for user creation:** the grimmory entrypoint calls `addgroup`/`adduser` which need root. Booklore ran successfully with `runAsUser: 1000` because either (a) the alpine base image has UID/GID 1000 already mapped, or (b) `getent` short-circuits the create paths when the IDs already exist. We mirror booklore's exact security context. If startup fails on user-creation, fallback is to drop `runAsNonRoot`/`runAsUser` and let the entrypoint run as root then `su-exec` down to 1000.
- **MariaDB upgrades coupled to grimmory upgrades:** acceptable; Renovate manages each pin separately and we can stage upgrades.
- **No DB backup beyond VolSync of `/var/lib/mysql`:** acceptable for a personal library; restore = restore the PVC from R2 and let grimmory reconnect.
- **`/tmp` is memory-backed (256Mi):** if the JVM OOMs and writes a heap dump, the dump is bounded by `tmp.sizeLimit`. If that's insufficient on a real OOM we'll see truncation; mitigation is to raise `sizeLimit` or switch `/tmp` to disk-backed emptyDir.

## Out of scope (future work)

- Re-add bookdrop mount when ingest workflow is wanted
- Migrate to a shared MariaDB or to the upstream chart if it becomes OCI-published and current
- Public route via `envoy-external` if remote access becomes a need
