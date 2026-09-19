# Pocket ID: SQLite → PostgreSQL migration

Pocket ID (`kubernetes/apps/default/pocket-id`) runs on SQLite at `/data/pocket-id.db`
on the `pocket-id` PVC. This runbook moves it to the shared CNPG cluster `pg18vc`
using Pocket ID's own `export` / `import` CLI, which is the only supported path
(pgloader loses passkeys and OIDC clients — see pocket-id/pocket-id#980).

Pocket ID is the IdP for every OIDC-protected app in the cluster. While it is down,
nothing that redirects to `pid.kantai.xyz` can log in; sessions that are already
established keep working. Budget a 15–30 minute window.

## Facts that shape the procedure

- `DB_CONNECTION_STRING` selects the provider: a `postgres://` / `postgresql://`
  prefix means PostgreSQL, anything else is SQLite. There is no `DB_PROVIDER` knob.
  The Postgres path goes through `pgxpool.ParseConfig`, so the libpq-style
  `sslmode`/`sslrootcert`/`sslcert`/`sslkey` URL parameters work as for any other
  pgx app here.
- `pocket-id export` walks the DB **and** the file storage (`uploads/` in the zip),
  so the zip is a complete copy. `pocket-id import` drops every Pocket ID table
  (not the schema, not extensions, not Francis's `francis_*` tables), re-runs the
  migrations to the exported version, loads the rows and rewrites the uploads.
  It works against a brand-new empty database.
- Pocket ID's Postgres migrations run `CREATE EXTENSION IF NOT EXISTS citext`. The
  tenant declares `citext` on the `Database`, so CNPG installs it and the migration
  is a no-op.
- `import` takes an exclusive cluster lease and refuses to run while a server is
  connected to the same DB. `--forcefully-acquire-lock` exists but kills the running
  instance — if that instance is PID 1 of the container you are `exec`ed into, the
  exec dies with it. Run the import from a separate pod with the server scaled to 0.
- Encrypted data (token signing keys, client secrets) is exported as ciphertext.
  `ENCRYPTION_KEY` in the `pocket-id-keys` Secret **must not change**. It lives in
  OpenBao and is already on the never-regenerate list; nothing in this migration touches it.
- The distroless image has no shell, no `env`, no `sleep`, no `tar`, so `kubectl cp`
  and env overrides are impossible in it. The migration pod uses the alpine variant of
  the same version (`ghcr.io/pocket-id/pocket-id:v2.18.0`) — same binary, plus a shell.
  Bump it to whatever the deployment runs on the day.
- The `oauth2_sessions/created_at ... float64` import failure reported in #980 was fixed
  in 1a032a8 (Jan 2026). Do not migrate on anything older than v2.14.0.
- Authentication is by CNPG client certificate (see "PostgreSQL Apps" in `AGENTS.md`).
  The connection string carries no secret, so it lives in the HelmRelease and there
  is no `pocket-id-db` ExternalSecret. PG identifiers use underscores: role and
  database are both `pocket_id`.

## Phase 1 — PR: provision the database, keep SQLite

Purely additive; Pocket ID keeps running on SQLite after this reconciles.

- `kubernetes/apps/database/cnpg/tenants/pocket-id/` — tenant Kustomization
  `pg18vc-pocket-id` with `DatabaseRole pocket-id` (`pocket_id`, cert login),
  `Database pocket-id` (`pocket_id`, owner `pocket_id`, extension `citext`) and
  `PushSecret pocket-id` copying `tls.crt`/`tls.key` from `pocket-id-client-cert` to
  `default/pocket-id-pg`. Registered in `kubernetes/apps/database/kustomization.yaml`.
- `kubernetes/apps/default/pocket-id/ks.yaml` — `dependsOn: pg18vc-pocket-id`.
- `kubernetes/apps/default/pocket-id/app/helmrelease.yaml` — mounts `cluster-ca.crt`
  at `/etc/postgresql/ca` and `pocket-id-pg` (`0440`) at `/etc/postgresql/client`.
  Unused while `DB_CONNECTION_STRING` still points at SQLite, but it makes Phase 2 a
  one-line change and proves the Secret and its permissions are right ahead of time.

After merge, check (read-only):

```sh
flux --context kantai.xyz get kustomization -n database pg18vc-pocket-id   # Ready
kubectl --context kantai.xyz -n database get databaserole,database pocket-id
kubectl --context kantai.xyz -n default get secret pocket-id-pg
kubectl --context kantai.xyz -n default get po -l app.kubernetes.io/name=pocket-id  # rolled, Running
```

## Phase 2 — PR: switch the connection string (do not merge yet)

Prepare it now so the window is just a merge and a reconcile.

- Replace `DB_CONNECTION_STRING` in `env` with:

  ```text
  postgres://pocket_id@pg18vc-rw.database.svc.cluster.local:5432/pocket_id?sslmode=verify-full&sslrootcert=/etc/postgresql/ca/ca.crt&sslcert=/etc/postgresql/client/tls.crt&sslkey=/etc/postgresql/client/tls.key
  ```

- Remove the `/app/backend/data` `subPath: backend` mount — a v1 leftover, unused since v2.
- Leave the PVC, `UPLOAD_PATH`, `GEOLITE_DB_PATH` and the kopiur component alone for now
  (see "Afterwards").

## Phase 3 — maintenance window

Every command below is a live mutation except the `get`/`logs`/`exec ... export`
steps; each one is called out. Context is `kantai.xyz` throughout.

1. **Baseline.** Confirm a kopiur snapshot of `pocket-id` from today exists, or trigger one.
   Note the current image tag on the running pod; the migration pod must match it.

2. **Freeze Flux and stop the server** *(mutating — authorize first)*:

   ```sh
   flux --context kantai.xyz suspend kustomization pocket-id
   flux --context kantai.xyz suspend helmrelease pocket-id -n default
   kubectl --context kantai.xyz -n default scale deploy/pocket-id --replicas=0
   kubectl --context kantai.xyz -n default get po -l app.kubernetes.io/name=pocket-id   # expect none
   ```

3. **Start the migration pod** *(mutating — authorize first)*. It mounts the same PVC,
   the same secrets and the Postgres client certificate, and has the Postgres DSN as
   its default `DB_CONNECTION_STRING`. Save as `/tmp/pocket-id-migrate.yaml`:

   ```yaml
   apiVersion: v1
   kind: Pod
   metadata:
     name: pocket-id-migrate
     namespace: default
   spec:
     restartPolicy: Never
     securityContext:
       runAsNonRoot: true
       runAsUser: 1000
       runAsGroup: 1000
       fsGroup: 1000
       seccompProfile: { type: RuntimeDefault }
     containers:
       - name: pocket-id
         image: ghcr.io/pocket-id/pocket-id:v2.18.0   # alpine variant, same version as the deployment
         command: ["sleep", "infinity"]
         env:
           - name: DB_CONNECTION_STRING
             value: postgres://pocket_id@pg18vc-rw.database.svc.cluster.local:5432/pocket_id?sslmode=verify-full&sslrootcert=/etc/postgresql/ca/ca.crt&sslcert=/etc/postgresql/client/tls.crt&sslkey=/etc/postgresql/client/tls.key
           - { name: APP_URL, value: https://pid.kantai.xyz }
           - { name: UPLOAD_PATH, value: /data/uploads }
           - { name: GEOLITE_DB_PATH, value: /data/GeoLite2-City.mmdb }
           - { name: ANALYTICS_DISABLED, value: "true" }
           - { name: VERSION_CHECK_DISABLED, value: "true" }
         envFrom:
           - secretRef: { name: pocket-id }
           - secretRef: { name: pocket-id-keys }
         securityContext:
           allowPrivilegeEscalation: false
           capabilities: { drop: ["ALL"] }
         volumeMounts:
           - { name: data, mountPath: /data }
           - { name: pg-ca, mountPath: /etc/postgresql/ca, readOnly: true }
           - { name: pg-client, mountPath: /etc/postgresql/client, readOnly: true }
     volumes:
       - name: data
         persistentVolumeClaim: { claimName: pocket-id }
       - name: pg-ca
         configMap: { name: cluster-ca.crt }
       - name: pg-client
         secret: { secretName: pocket-id-pg, defaultMode: 0440 }
   ```

   ```sh
   kubectl --context kantai.xyz apply -f /tmp/pocket-id-migrate.yaml
   kubectl --context kantai.xyz -n default wait --for=condition=Ready pod/pocket-id-migrate
   ```

4. **Export from SQLite** (read-only; the server is stopped, so the snapshot is consistent):

   ```sh
   SQLITE='file:/data/pocket-id.db?_pragma=journal_mode(WAL)&_pragma=busy_timeout(2500)&_txlock=immediate'
   kubectl --context kantai.xyz -n default exec pocket-id-migrate -- \
     env DB_CONNECTION_STRING="$SQLITE" /app/pocket-id export --path - > pocket-id-export.zip
   unzip -l pocket-id-export.zip     # expect database json + uploads/... entries
   ```

   Keep this zip until the migration has been declared done. It contains ciphertext,
   not plaintext keys, but it is still the complete identity database.

5. **Import into Postgres** *(mutating, on the DB only)*:

   ```sh
   kubectl --context kantai.xyz -n default exec -i pocket-id-migrate -- \
     /app/pocket-id import --path - --yes < pocket-id-export.zip
   # expect: "Import completed successfully."
   ```

   If it complains that Pocket ID must be stopped, a server is still attached to the DB:
   go back to step 2 and check for leftover pods. Do not reach for
   `--forcefully-acquire-lock` unless you have confirmed what is holding the lease.
   A TLS or `certificate` error here means the mounts or `sslmode` parameters are wrong,
   and Phase 2 would fail the same way — fix it before cutting over.

6. **Spot-check the target** (read-only, on the primary):

   ```sh
   PRIMARY=$(kubectl --context kantai.xyz -n database get po \
     -l cnpg.io/cluster=pg18vc,cnpg.io/instanceRole=primary -o name)
   kubectl --context kantai.xyz -n database exec "$PRIMARY" -c postgres -- \
     psql -U postgres -d pocket_id -c '\dt' \
       -c 'select count(*) from users' -c 'select count(*) from oidc_clients' -c 'select count(*) from webauthn_credentials'
   ```

   Compare against what you expect (or against the same queries run through the
   migration pod on the SQLite file with `sqlite3` — not in the image; skip unless needed).

7. **Cut over.** Merge the Phase 2 PR, then *(mutating)*:

   ```sh
   kubectl --context kantai.xyz delete pod -n default pocket-id-migrate
   flux --context kantai.xyz resume helmrelease pocket-id -n default
   flux --context kantai.xyz resume kustomization pocket-id
   flux --context kantai.xyz reconcile kustomization pocket-id --with-source
   ```

   Resuming the HelmRelease reapplies the chart at the new values, which restores
   `replicas: 1` — the manual scale-to-0 is overwritten, not remembered.

8. **Verify.**
   - Pod logs show a Postgres connection and no migration errors.
   - `https://pid.kantai.xyz` loads, you can sign in with your passkey, and the admin UI
     lists all users, groups and OIDC clients.
   - Log in to two or three downstream apps (e.g. one on `envoy-internal`, one on
     `envoy-external`). Existing client secrets and the JWKS must still validate — if the
     signing key had not round-tripped, every OIDC app would fail with a signature error.
   - Profile pictures and client logos still render (`uploads/` round trip).

## Rollback

Everything up to step 7 is reversible by deleting the migration pod, resuming Flux
without merging Phase 2 and reconciling — the SQLite file was only ever read.
After step 7: revert the Phase 2 commit and reconcile. The SQLite file is untouched
on the PVC; the `pocket_id` database is retained (CNPG reclaim policy `retain`) and can
be dropped later, or re-imported into on a second attempt.

## Afterwards

- Remove `pocket-id.db`, `pocket-id.db-wal`, `pocket-id.db-shm` from the PVC once you
  are satisfied (a follow-up exec in a throwaway alpine pod, or leave it; it is 1Gi).
- Consider going fully stateless in a later PR: `FILE_BACKEND: database` moves uploads
  into Postgres (import them via export/import again, or just re-upload the handful of
  logos), `GEOLITE_DB_PATH` can point at an `emptyDir` since the app re-downloads it,
  and then the PVC and the `components/kopiur/backup` component go away — CNPG's
  barman-cloud backups cover the DB. Not part of this migration; keep the blast radius small.
- Konflate will flag the removed `subPath` mount and the env change; that is expected.
