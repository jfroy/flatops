# Pocket ID: SQLite → PostgreSQL migration

**Status: done, 2026-10-05.** Pocket ID (`kubernetes/apps/default/pocket-id`) runs on the
shared CNPG cluster `pg18vc` (role and database `pocket_id`, client-certificate auth,
tenant `kubernetes/apps/database/cnpg/tenants/pocket-id`). Phase 1 was #3074, the
cutover #3075. Downtime was about 12 minutes, plus about 25 minutes of HTTP 500s on
Envoy-OIDC routes (see "Envoy Gateway OIDC policies" below).

This file records how it was done and what went wrong, for anyone repeating it here or
for another app with an export/import CLI.

## Approach

Pocket ID's own `export` / `import` CLI is the only supported path; pgloader loses
passkeys and OIDC clients (pocket-id/pocket-id#980).

- `DB_CONNECTION_STRING` selects the provider: a `postgres://` / `postgresql://` prefix
  means PostgreSQL, anything else SQLite. pgx parses the libpq `ssl*` URL parameters.
- `export` writes `database.json`, `francis.bin` (actor-host state) and `uploads/` into a
  zip. `import` restores Francis's tables, drops every Pocket ID table (not the schema,
  not extensions), migrates to the exported version, and inserts all rows in **one
  transaction**, so a failed import leaves the Pocket ID tables empty, not half-filled.
  It works against a brand-new database.
- `import` takes an exclusive lease and refuses to run while a server is attached.
  `--forcefully-acquire-lock` kills the running instance, so run it from a separate pod
  with the deployment at 0.
- The migrations run `CREATE EXTENSION IF NOT EXISTS citext`; the tenant's `Database`
  declares `citext` so CNPG owns it.
- Encrypted data (`jwt_private_key.json`, `session_key.json` in `kv`) is exported as
  ciphertext, so `ENCRYPTION_KEY` (`pocket-id-keys`, OpenBao) must not change.
  `instance_id` is also in `kv`, which keeps the Francis PSK derivation stable.
- The distroless image has no shell or `tar`; the migration pod used the alpine image
  of the same version.

## What went wrong

### The SQLite WAL was never checkpointed

After scaling to 0, `/data` held a 12 MB `pocket-id.db` and a 9.9 MB `pocket-id.db-wal`.
Opening the database (which `export` does, and it also runs migrations) checkpoints the
WAL into the `.db`, so "the SQLite file is only read" is false. Take the raw copy first,
all three files together:

```sh
kubectl -n default exec pocket-id-migrate -- \
  tar cf - -C /data pocket-id.db pocket-id.db-wal pocket-id.db-shm uploads > raw-sqlite.tar
```

Check `sha256sum` on both sides, and run `pragma integrity_check` plus per-table row
counts on a scratch copy, never on the raw one.

### Two importer bugs in v2.18.0

Both fail the import's insert transaction, which rolls back cleanly.

1. `oauth2_sessions.rotated_at` (migration `20260929120000_refresh_token_rotation_grace`)
   is `INTEGER` unix seconds in SQLite but `TIMESTAMPTZ` in Postgres. The importer requires
   timestamps as strings: `value for column 'oauth2_sessions/rotated_at' was expected to
   be a string, but was 'float64'`.
2. `webauthn_sessions.extensions` is exported as raw JSON (`{}`), but the importer expects
   base64 for `jsonb`: `failed to decode value for column 'webauthn_sessions/extensions'
   from base64`.

The workaround was to patch a copy of the export. Validate the whole thing against the
real Postgres column types first, so problems surface in one pass instead of one per
import attempt:

```sh
# Column types of the (already migrated) target, read-only
kubectl -n database exec <pg18vc primary> -c postgres -- psql -U postgres -d pocket_id \
  -tAF $'\t' -c "select table_name, column_name, data_type from information_schema.columns
                 where table_schema='public' and table_name not like 'francis_%'" > pg-columns.tsv
```

```python
import json, zipfile, base64, binascii, datetime

def b64ok(v):
    try:
        base64.b64decode(v, validate=True); return True
    except (binascii.Error, ValueError):
        return False

types = {tuple(l.rstrip("\n").split("\t")[:2]): l.rstrip("\n").split("\t")[2]
         for l in open("pg-columns.tsv")}
with zipfile.ZipFile("pocket-id-export.zip") as zin:
    db = json.loads(zin.read("database.json"))
    for r in db["tables"].get("oauth2_sessions", []):
        if isinstance(r.get("rotated_at"), (int, float)):
            r["rotated_at"] = datetime.datetime.fromtimestamp(
                int(r["rotated_at"]), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in db["tables"].get("webauthn_sessions", []):
        if isinstance(r.get("extensions"), str) and not b64ok(r["extensions"]):
            r["extensions"] = base64.b64encode(r["extensions"].encode()).decode()

    # Same rules as normalizeRowWithSchema: timestamps are strings, bytea/jsonb are base64
    for t, rows in db["tables"].items():
        if t.startswith("francis_") or t == "schema_migrations":
            continue
        for r in rows:
            for c, v in r.items():
                ty = types.get((t, c))
                if v is None or ty is None:
                    continue
                assert not ("timestamp" in ty and not isinstance(v, str)), (t, c, v)
                assert not (ty in ("bytea", "jsonb") and not b64ok(v)), (t, c, v)

    with zipfile.ZipFile("pocket-id-export.patched.zip", "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = (json.dumps(db, separators=(",", ":")).encode()
                    if info.filename == "database.json" else zin.read(info.filename))
            zout.writestr(info, data)
```

Re-check upstream before reusing this; a fixed release makes it unnecessary.

### Envoy Gateway OIDC policies

Every route using `components/envoy-gateway-oidc` (radarr, sonarr, prowlarr, sabnzbd,
victoria-metrics, victoria-logs, dozzle) returned HTTP 500 after the cutover, while apps
doing their own OIDC (grafana, kite) were fine.

Envoy Gateway (v1.9) fetches the issuer's discovery document when it translates a
`SecurityPolicy`, and does not retry a failure. It re-translated 7 seconds after the new
Pocket ID pod started, before `pid.kantai.xyz` answered, and marked every policy
`Accepted=False`: `Invalid: OIDC: Get "https://pid.kantai.xyz/.well-known/openid-configuration":
context deadline exceeded`. Nothing re-triggered translation until the controller was
restarted:

```sh
kubectl -n network rollout restart deploy/envoy-gateway
```

**After any Pocket ID outage**, check:

```sh
kubectl get securitypolicy -A -o json | jq -r '.items[] | select(.spec.oidc)
  | [.metadata.namespace + "/" + .metadata.name,
     (.status.ancestors[0].conditions[] | select(.type == "Accepted") | .status)] | @tsv'
```

## Procedure as run

Context `kantai.hyakutake-universe.ts.net`. Steps 2, 3, 5 and 7 mutate the cluster.

1. **Baseline.** A fresh kopiur snapshot of `pocket-id` (`pocket-id-20261005030500`).
   Kopiur had been stalled cluster-wide; that was fixed first.
2. **Stop.** `flux suspend kustomization pocket-id -n default`,
   `flux suspend helmrelease pocket-id -n default`,
   `kubectl -n default scale deploy/pocket-id --replicas=0`.
3. **Migration pod** on the same PVC (RWO, so only after step 2), with the app's secrets,
   the `cluster-ca.crt` ConfigMap and `pocket-id-pg` (`0440`) mounted at the same paths as
   the app, `fsGroup: 1000`, the Postgres DSN as `DB_CONNECTION_STRING`, and
   `command: ["sleep", "86400"]`. Dry-run it server-side before step 2.
4. **Raw copy** (see the WAL section), then **export** from SQLite:

   ```sh
   SQLITE='file:/data/pocket-id.db?_pragma=journal_mode(WAL)&_pragma=busy_timeout(2500)&_txlock=immediate'
   kubectl -n default exec pocket-id-migrate -- \
     env DB_CONNECTION_STRING="$SQLITE" /app/pocket-id export --path - > pocket-id-export.zip
   ```

   Compare `database.json` row counts and the upload count with the source.
5. **Import** the (patched) export:

   ```sh
   kubectl -n default exec -i pocket-id-migrate -- \
     /app/pocket-id import --path - --yes < pocket-id-export.patched.zip
   ```

6. **Verify in Postgres** on the primary (`-l cnpg.io/cluster=pg18vc,cnpg.io/instanceRole=primary`):
   row counts for every table against the source, `max(version)` of `schema_migrations`,
   and that the `kv` values for `jwt_private_key.json`, `session_key.json` and
   `instance_id` are byte-identical to the export.
7. **Cut over.** Merge the DSN change, delete the migration pod,
   `flux resume helmrelease` and `kustomization`, `flux reconcile ... --with-source`.
   Check the pod logs `provider=postgres`, the JWKS `kid` is unchanged, the OIDC
   `SecurityPolicy` statuses (above), then passkey login and a few downstream apps.

## Rollback

Until step 7, delete the migration pod and resume Flux: Pocket ID returns on SQLite.
After it, revert the DSN change; the SQLite files stay on the PVC. The `pocket_id`
database is retained by CNPG's default reclaim policy.

## Leftovers

- `pocket-id.db`, `pocket-id.db-wal`, `pocket-id.db-shm`, `backend/` and
  `backup-20260810/` on the `pocket-id` PVC are unused and can be removed.
- Going fully stateless (`FILE_BACKEND: database`, GeoLite on an `emptyDir`, then drop
  the PVC and kopiur) is possible but was kept out of scope.
