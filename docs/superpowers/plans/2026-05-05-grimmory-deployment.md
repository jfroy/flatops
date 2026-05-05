# Grimmory Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy [grimmory](https://github.com/grimmory-tools/grimmory) v3.0.3 (self-hosted digital library) to the kantai cluster as a single-pod, internally-routed app, mirroring the booklore precedent (commit `6751a4d62`).

**Architecture:** A single bjw-s `app-template` HelmRelease in the `default` namespace with two containers: `grimmory` (main, port 6060) and `mariadb` (sidecar via `initContainers` + `restartPolicy: Always`, reachable only at `localhost:3306`). DB isolated by a `CiliumNetworkPolicy` denying ingress to TCP/3306. `${APP}` PVC (ceph-block, 1Gi, VolSync→R2) holds both `/app/data` and `/var/lib/mysql`. Books library mounted from the existing shared `media2` PVC at `/books` (subPath `library/books`). No bookdrop.

**Tech Stack:** FluxCD GitOps, bjw-s `app-template` Helm chart via OCIRepository, Cilium NetworkPolicy, VolSync (Kopia → Cloudflare R2), Gateway API HTTPRoute via `envoy-internal`.

**Reference spec:** [docs/superpowers/specs/2026-05-05-grimmory-deployment-design.md](../specs/2026-05-05-grimmory-deployment-design.md)

**Image digests resolved 2026-05-05** (re-verify in Task 1):
- `ghcr.io/grimmory-tools/grimmory:v3.0.3@sha256:a903a2b44c308bd1738b6f7cdb5a2e5a2a1ae23a092f30eb68581e2be1af50cd`
- `docker.io/library/mariadb:12.2.2-noble@sha256:e0236fc6386e7eacd9359e59d0a078bd7aa0d18280d36d13061121bedeaee903`

**Workflow note:** This repo deploys via direct push to `main`; Flux watches the `main` branch. There is no PR-merge gate. CI (`flux-diff.yaml`) only runs on PRs and `renovate/**` branches, so local validation before push is the safety net.

---

## File Structure

| File | Purpose |
|---|---|
| `kubernetes/apps/default/grimmory/ks.yaml` | Flux `Kustomization` registering the app, with VolSync component |
| `kubernetes/apps/default/grimmory/app/helmrelease.yaml` | bjw-s `app-template` HelmRelease (single pod, two containers) |
| `kubernetes/apps/default/grimmory/app/ciliumnetworkpolicy.yaml` | Deny all ingress to TCP/3306 on grimmory pods |
| `kubernetes/apps/default/grimmory/app/kustomization.yaml` | Lists helmrelease + ciliumnetworkpolicy |
| `kubernetes/apps/default/kustomization.yaml` | **Modify** — add `- ./grimmory/ks.yaml` alphabetically (between `gluetun` and `homebox`) |

No `externalsecret.yaml`, no `namespace.yaml` (default namespace already exists).

---

## Task 1: Verify image digests are still current

**Files:** none (read-only check)

- [ ] **Step 1.1: Re-resolve grimmory image digest**

Run:

```bash
crane digest ghcr.io/grimmory-tools/grimmory:v3.0.3
```

Expected: `sha256:a903a2b44c308bd1738b6f7cdb5a2e5a2a1ae23a092f30eb68581e2be1af50cd`

If different, **use the new digest in Task 3** instead.

- [ ] **Step 1.2: Re-resolve mariadb image digest**

Run:

```bash
crane digest docker.io/library/mariadb:12.2.2-noble
```

Expected: `sha256:e0236fc6386e7eacd9359e59d0a078bd7aa0d18280d36d13061121bedeaee903`

If different, **use the new digest in Task 3** instead.

- [ ] **Step 1.3: Confirm `media2` PVC exists**

Run:

```bash
kubectl --context kantai.hyakutake-universe.ts.net get pvc media2 -n default
```

Expected: `STATUS=Bound`. If missing, halt and investigate — `media2` is a hard dependency.

---

## Task 2: Create `ks.yaml`

**Files:**
- Create: `kubernetes/apps/default/grimmory/ks.yaml`

- [ ] **Step 2.1: Make the directory**

Run:

```bash
mkdir -p kubernetes/apps/default/grimmory/app
```

- [ ] **Step 2.2: Write the Flux Kustomization**

Create `kubernetes/apps/default/grimmory/ks.yaml` with:

```yaml
---
# yaml-language-server: $schema=https://raw.githubusercontent.com/fluxcd-community/flux2-schemas/main/kustomization-kustomize-v1.json
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app grimmory
spec:
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  components:
    - ../../../../components/volsync
  path: ./kubernetes/apps/default/grimmory/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
    namespace: flux-system
  interval: 1h
  retryInterval: 2m
  timeout: 5m
  postBuild:
    substitute:
      APP: *app
      VOLSYNC_CAPACITY: 1Gi
```

- [ ] **Step 2.3: Validate YAML syntax**

Run:

```bash
yq eval '.' kubernetes/apps/default/grimmory/ks.yaml > /dev/null && echo OK
```

Expected: `OK` (and no parse errors).

---

## Task 3: Write the HelmRelease

**Files:**
- Create: `kubernetes/apps/default/grimmory/app/helmrelease.yaml`

- [ ] **Step 3.1: Write the HelmRelease**

Create `kubernetes/apps/default/grimmory/app/helmrelease.yaml` with:

```yaml
---
# yaml-language-server: $schema=https://raw.githubusercontent.com/bjw-s-labs/helm-charts/refs/heads/main/charts/other/app-template/schemas/helmrelease-helm-v2.schema.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: grimmory
spec:
  interval: 1h
  chartRef:
    kind: OCIRepository
    name: app-template
    namespace: flux-system
  driftDetection:
    mode: enabled
  install:
    remediation:
      retries: -1
  upgrade:
    cleanupOnFail: true
    remediation:
      retries: 3
  values:
    controllers:
      grimmory:
        annotations:
          reloader.stakater.com/auto: "true"
        initContainers:
          mariadb:
            image:
              repository: docker.io/library/mariadb
              tag: 12.2.2-noble@sha256:e0236fc6386e7eacd9359e59d0a078bd7aa0d18280d36d13061121bedeaee903
            env:
              MARIADB_DATABASE: grimmory
              MARIADB_USER: grimmory
              MARIADB_PASSWORD: grimmory
              MARIADB_ROOT_PASSWORD: root
              TZ: America/Los_Angeles
            probes:
              liveness:
                enabled: true
                custom: true
                spec:
                  exec:
                    command:
                      - mariadb-admin
                      - ping
                      - --socket=/run/mysqld/mysqld.sock
                      - --user=root
                      - --password=root
            restartPolicy: Always
            securityContext:
              allowPrivilegeEscalation: false
              capabilities: { drop: ["ALL"] }
              readOnlyRootFilesystem: true
              # NOTE: io_uring is blocked by the default containerd v2 seccomp profile
              # https://github.com/containerd/containerd/pull/9320
              seccompProfile: { type: Unconfined }
            resources:
              requests:
                cpu: 50m
                memory: 256Mi
              limits:
                memory: 512Mi
        containers:
          grimmory:
            image:
              repository: ghcr.io/grimmory-tools/grimmory
              tag: v3.0.3@sha256:a903a2b44c308bd1738b6f7cdb5a2e5a2a1ae23a092f30eb68581e2be1af50cd
            env:
              DATABASE_URL: jdbc:mariadb://localhost:3306/grimmory
              DATABASE_USERNAME: grimmory
              DATABASE_PASSWORD: grimmory
              USER_ID: "1000"
              GROUP_ID: "1000"
              APP_USER: grimmory
              TZ: America/Los_Angeles
            probes:
              liveness: &probe
                enabled: true
                custom: true
                spec:
                  httpGet:
                    path: /api/v1/healthcheck
                    port: &port 6060
              readiness: *probe
              startup:
                enabled: true
                custom: true
                spec:
                  httpGet:
                    path: /api/v1/healthcheck
                    port: *port
                  failureThreshold: 30
                  periodSeconds: 10
            securityContext:
              allowPrivilegeEscalation: false
              capabilities: { drop: ["ALL"] }
              readOnlyRootFilesystem: true
            resources:
              requests:
                cpu: 100m
                memory: 1Gi
              limits:
                memory: 2Gi
        pod:
          labels:
            pod-security.kantai.xyz/allow-seccomp-unconfined: "true"
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            runAsGroup: 1000
            fsGroup: 1000
            fsGroupChangePolicy: OnRootMismatch
    persistence:
      data:
        existingClaim: "${APP}"
        advancedMounts:
          grimmory:
            grimmory:
              - path: /app/data
                subPath: app
            mariadb:
              - path: /var/lib/mysql
                subPath: mysql
      media2:
        type: persistentVolumeClaim
        existingClaim: media2
        advancedMounts:
          grimmory:
            grimmory:
              - path: /books
                subPath: library/books
      mysqld:
        type: emptyDir
        medium: Memory
        sizeLimit: 1Mi
        advancedMounts:
          grimmory:
            mariadb:
              - path: /run/mysqld
      tmp:
        type: emptyDir
        medium: Memory
        sizeLimit: 256Mi
        advancedMounts:
          grimmory:
            grimmory:
              - path: /tmp
            mariadb:
              - path: /tmp
    route:
      grimmory:
        hostnames: ["grimmory.kantai.xyz"]
        parentRefs:
          - name: envoy-internal
            namespace: network
    service:
      grimmory:
        controller: grimmory
        ports:
          http:
            port: *port
```

**Notes for the engineer:**

- `restartPolicy: Always` on an initContainer is the Kubernetes "sidecar container" pattern (KEP-753). The container starts before the main container and is restarted independently if it crashes.
- The `tag:` field includes the digest after `@` — Renovate parses this format and bumps both atomically.
- `DISK_TYPE` and `BOOKLORE_PORT` are intentionally omitted; their defaults (`LOCAL` and `6060` respectively) are already correct for our setup.
- `failureThreshold: 30` × `periodSeconds: 10` gives the JVM up to 5 minutes to come up cold.

- [ ] **Step 3.2: Validate YAML syntax**

Run:

```bash
yq eval '.' kubernetes/apps/default/grimmory/app/helmrelease.yaml > /dev/null && echo OK
```

Expected: `OK`.

---

## Task 4: Write the CiliumNetworkPolicy

**Files:**
- Create: `kubernetes/apps/default/grimmory/app/ciliumnetworkpolicy.yaml`

- [ ] **Step 4.1: Write the policy**

Create `kubernetes/apps/default/grimmory/app/ciliumnetworkpolicy.yaml` with:

```yaml
---
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

`app.kubernetes.io/name` is set by `commonMetadata.labels` in `ks.yaml` (Task 2). `app.kubernetes.io/instance` is set automatically by app-template based on the HelmRelease name (`grimmory`). Both labels appear on the pod, so this selector matches.

- [ ] **Step 4.2: Validate YAML syntax**

Run:

```bash
yq eval '.' kubernetes/apps/default/grimmory/app/ciliumnetworkpolicy.yaml > /dev/null && echo OK
```

Expected: `OK`.

---

## Task 5: Write the app-level `kustomization.yaml`

**Files:**
- Create: `kubernetes/apps/default/grimmory/app/kustomization.yaml`

- [ ] **Step 5.1: Write the kustomization**

Create `kubernetes/apps/default/grimmory/app/kustomization.yaml` with:

```yaml
---
# yaml-language-server: $schema=https://json.schemastore.org/kustomization
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./ciliumnetworkpolicy.yaml
  - ./helmrelease.yaml
```

- [ ] **Step 5.2: Validate kustomize build**

Run:

```bash
kustomize build kubernetes/apps/default/grimmory/app > /tmp/grimmory-build.yaml && echo OK
```

Expected: `OK`. Inspect `/tmp/grimmory-build.yaml`:

```bash
yq eval '.kind' /tmp/grimmory-build.yaml
```

Expected: two documents — `CiliumNetworkPolicy` and `HelmRelease`.

---

## Task 6: Register grimmory in the default namespace `kustomization.yaml`

**Files:**
- Modify: `kubernetes/apps/default/kustomization.yaml` (add one line, alphabetical position between `gluetun` and `homebox`)

- [ ] **Step 6.1: Add the entry**

Apply this edit to `kubernetes/apps/default/kustomization.yaml`:

Find the line:

```yaml
  - ./gluetun/ks.yaml
  - ./homebox/ks.yaml
```

Replace with:

```yaml
  - ./gluetun/ks.yaml
  - ./grimmory/ks.yaml
  - ./homebox/ks.yaml
```

- [ ] **Step 6.2: Validate the parent kustomize build**

Run:

```bash
kustomize build kubernetes/apps/default > /tmp/default-build.yaml && echo OK
```

Expected: `OK`. Then confirm the grimmory Kustomization is present:

```bash
yq eval 'select(.kind == "Kustomization" and .metadata.name == "grimmory")' /tmp/default-build.yaml
```

Expected: a YAML block showing the grimmory Flux `Kustomization` with `path: ./kubernetes/apps/default/grimmory/app` and `postBuild.substitute.APP: grimmory`.

---

## Task 7: Local cluster-wide validation with flux-local

**Files:** none (read-only)

- [ ] **Step 7.1: Run flux-local test**

Run:

```bash
docker run --rm -v "$(pwd):/workspace" -w /workspace \
  ghcr.io/allenporter/flux-local:v8.2.0 \
  test --enable-helm --all-namespaces --path /workspace/kubernetes/cluster -v
```

Expected: all tests pass with no errors mentioning `grimmory`. (This is the same command CI runs.)

If it fails on grimmory:
- YAML schema errors → re-check Tasks 2–5
- Templating errors → re-check `${APP}` and `VOLSYNC_CAPACITY` are correctly substituted (they should be, via `postBuild.substitute` in `ks.yaml`)

- [ ] **Step 7.2: Run flux-local diff against the live cluster**

Run:

```bash
docker run --rm \
  -v "$(pwd):/workspace" \
  -v "$HOME/.kube:/root/.kube:ro" \
  -e KUBECONFIG=/root/.kube/config \
  -w /workspace \
  ghcr.io/allenporter/flux-local:v8.2.0 \
  diff hr --all-namespaces --path /workspace/kubernetes/cluster
```

Expected: a diff showing **only** the addition of HelmRelease `default/grimmory`, the `ciliumnetworkpolicy`, the VolSync `ReplicationSource`/`ReplicationDestination`, and the PVC `grimmory`. No unrelated diffs.

If unrelated diffs appear, they may be pre-existing drift — confirm with the user before proceeding.

---

## Task 8: Commit and push

**Files:** the four created files plus the modified `kustomization.yaml`

- [ ] **Step 8.1: Stage**

Run:

```bash
git add \
  kubernetes/apps/default/grimmory/ \
  kubernetes/apps/default/kustomization.yaml
```

- [ ] **Step 8.2: Verify staged tree**

Run:

```bash
git status --short
```

Expected:

```text
A  kubernetes/apps/default/grimmory/app/ciliumnetworkpolicy.yaml
A  kubernetes/apps/default/grimmory/app/helmrelease.yaml
A  kubernetes/apps/default/grimmory/app/kustomization.yaml
A  kubernetes/apps/default/grimmory/ks.yaml
M  kubernetes/apps/default/kustomization.yaml
```

- [ ] **Step 8.3: Commit**

Run:

```bash
git commit -m "$(cat <<'EOF'
feat(grimmory): deploy grimmory v3.0.3 to default namespace

Single-pod app-template deployment mirroring the booklore precedent
(deleted in 6751a4d62). MariaDB runs as a sidecar via initContainer
with restartPolicy: Always; isolated from the rest of the cluster
by a CiliumNetworkPolicy denying ingress to TCP/3306. Books library
mounted from the existing media2 PVC. /bookdrop intentionally
omitted; can be added later when ingest is wanted.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 8.4: Push to main**

Run:

```bash
git push origin main
```

Flux watches `main` and will detect the change within its source poll interval (~1m).

---

## Task 9: Trigger reconciliation and watch it land

**Files:** none (cluster operations)

- [ ] **Step 9.1: Force the cluster-apps Kustomization to reconcile**

Run:

```bash
flux --context kantai.hyakutake-universe.ts.net reconcile kustomization cluster-apps --with-source
```

Expected: completes without error in 30–60s.

- [ ] **Step 9.2: Reconcile the grimmory Flux Kustomization**

Run:

```bash
flux --context kantai.hyakutake-universe.ts.net reconcile kustomization grimmory -n flux-system
```

Expected: `applied revision`, no error.

- [ ] **Step 9.3: Reconcile the HelmRelease**

Run:

```bash
flux --context kantai.hyakutake-universe.ts.net reconcile helmrelease grimmory -n default
```

Expected: `applied revision`. First install can take 1–3 minutes (image pull + container init + JVM cold start).

- [ ] **Step 9.4: Watch the pod come up**

Run:

```bash
kubectl --context kantai.hyakutake-universe.ts.net -n default get pods -l app.kubernetes.io/name=grimmory -w
```

Expected within ~3 minutes: `STATUS=Running`, `READY=2/2` (grimmory + mariadb sidecar both ready).

If pod fails:
- `CrashLoopBackOff` on mariadb → check logs: `kubectl -n default logs -l app.kubernetes.io/name=grimmory -c mariadb --tail=200`. The most likely failure is the entrypoint refusing to chown `/var/lib/mysql` because it's already populated with wrong ownership; this is normally resolved by `fsGroup: 1000`.
- `CrashLoopBackOff` on grimmory → check logs for the user-creation step (`addgroup`/`adduser`). If it fails with permission errors, see "Risk fallback" below.
- `ImagePullBackOff` → re-verify digests in Task 1; both images are public.

**Risk fallback (entrypoint root requirement):** If grimmory's entrypoint fails because user creation requires root, edit `kubernetes/apps/default/grimmory/app/helmrelease.yaml` to remove `runAsNonRoot: true` and `runAsUser: 1000` from the pod-level securityContext (keep `fsGroup: 1000`). The entrypoint will then run as root and `su-exec` down to UID 1000 itself. Commit, push, reconcile.

---

## Task 10: Validate the running deployment

**Files:** none (cluster operations)

- [ ] **Step 10.1: Confirm HelmRelease ready**

Run:

```bash
flux --context kantai.hyakutake-universe.ts.net get hr -n default grimmory
```

Expected: `READY=True`, `STATUS=Helm install/upgrade succeeded`.

- [ ] **Step 10.2: Verify the health endpoint via in-cluster curl**

Run:

```bash
POD=$(kubectl --context kantai.hyakutake-universe.ts.net -n default get pod -l app.kubernetes.io/name=grimmory -o jsonpath='{.items[0].metadata.name}')
kubectl --context kantai.hyakutake-universe.ts.net -n default exec "$POD" -c grimmory -- \
  wget -qO- --tries=1 --timeout=5 http://localhost:6060/api/v1/healthcheck
```

Expected: HTTP 200 response body. (The image has `wget` from BusyBox/alpine; `curl` is not installed.)

- [ ] **Step 10.3: Verify external HTTPS access**

Run:

```bash
curl -fsS -o /dev/null -w "%{http_code}\n" https://grimmory.kantai.xyz/
```

Expected: `200` from the LAN/tailnet. If `000` or DNS error, check that the HTTPRoute is admitted:

```bash
kubectl --context kantai.hyakutake-universe.ts.net -n default get httproute grimmory -o yaml | yq '.status'
```

External-DNS picks up new HTTPRoutes within ~1m and publishes to Unifi.

- [ ] **Step 10.4: Confirm CiliumNetworkPolicy isolates MariaDB**

First, verify the CNP exists:

```bash
kubectl --context kantai.hyakutake-universe.ts.net -n default get cnp deny-grimmory-mariadb-ingress
```

Expected: resource exists.

Then test isolation by hitting the **pod IP directly** on 3306 from a separate pod (the Service does not expose 3306, so the only way to actually exercise the CNP is via pod IP):

```bash
POD_IP=$(kubectl --context kantai.hyakutake-universe.ts.net -n default get pod -l app.kubernetes.io/name=grimmory -o jsonpath='{.items[0].status.podIP}')
echo "grimmory pod IP: $POD_IP"

kubectl --context kantai.hyakutake-universe.ts.net -n default run nettest --rm -i --restart=Never \
  --image=ghcr.io/nicolaka/netshoot:latest \
  --command -- nc -zv -w 3 "$POD_IP" 3306
```

Expected: `nc: connect to <POD_IP> port 3306 (tcp) failed` or timeout — the CNP drops the SYN at Cilium's eBPF layer.

Sanity check that 6060 *is* reachable (the CNP only denies 3306):

```bash
kubectl --context kantai.hyakutake-universe.ts.net -n default run nettest --rm -i --restart=Never \
  --image=ghcr.io/nicolaka/netshoot:latest \
  --command -- nc -zv -w 3 "$POD_IP" 6060
```

Expected: `Connection to <POD_IP> 6060 port [tcp/*] succeeded!`

- [ ] **Step 10.5: Confirm VolSync ReplicationSource exists and has run**

Run:

```bash
kubectl --context kantai.hyakutake-universe.ts.net -n default get replicationsource grimmory -o yaml | yq '.status'
```

Expected: `lastSyncTime` is set after the first scheduled sync (default schedule is daily; first sync may take up to 24h unless triggered manually). The presence of the ReplicationSource itself is sufficient to declare success at deploy time.

- [ ] **Step 10.6: Open the UI and create the admin account**

Open `https://grimmory.kantai.xyz/` in a browser. Confirm:

- The grimmory landing page renders
- The first-run flow asks to create an admin account
- Account creation succeeds (this exercises the database write path end-to-end)

If the UI loads but admin creation fails with a database error, MariaDB is not reachable from grimmory — re-check the env vars in Task 3 step 3.1 (`DATABASE_URL` must be `jdbc:mariadb://localhost:3306/grimmory`).

---

## Acceptance criteria

All of the following are true:

1. `flux get hr -n default grimmory` → READY=True
2. Pod `grimmory-*` → READY=2/2
3. In-pod `wget http://localhost:6060/api/v1/healthcheck` → 200
4. `https://grimmory.kantai.xyz/` returns the UI on LAN/tailnet
5. CiliumNetworkPolicy `deny-grimmory-mariadb-ingress` exists and isolates 3306
6. VolSync `ReplicationSource grimmory` exists in the `default` namespace
7. Admin account creation in the UI succeeds (DB write end-to-end)
