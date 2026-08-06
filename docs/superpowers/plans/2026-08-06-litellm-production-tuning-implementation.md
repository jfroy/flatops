# litellm Production Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply litellm's applicable production-readiness settings (logging, timeouts,
connection pooling), enable the Rust AI gateway per model, and wire Postgres read-replica
routing to the CNPG cluster's existing replica-only service.

**Architecture:** Values-only edits to the existing `HelmRelease` (`kubernetes/apps/litellm/litellm/app/helmrelease.yaml`)
plus one new key in the existing `litellm-db` `ExternalSecret`
(`kubernetes/apps/litellm/litellm/app/externalsecret.yaml`) for the read-replica connection
string. No new files, no structural change.

**Tech Stack:** Flux (HelmRelease), external-secrets (1Password), litellm-helm chart, CNPG.

## Global Constraints

- Do not run mutating `kubectl`/`helm`/`flux apply` commands — verification is local rendering
  only (`flate build`), per `AGENTS.md`.
- Work happens on the existing branch `feat/litellm-proxy` (already checked out), in place, no
  worktree — matching the prior two rounds of work on this app.
- Do **not** set `disable_error_logs: true` — it fights this deployment's stated auditing goal.
  Do **not** add resource requests/limits or the `/health/drain` hook — both were explicitly
  decided against for this round (see spec).
- The read-replica connection string reuses the *same* generated password already used for
  `DATABASE_PASSWORD`/`INIT_POSTGRES_PASS` in the `litellm-db` `ExternalSecret` — do not
  introduce a second password/generator.

---

## Task 1: Logging, timeouts, connection pooling, Rust gateway

**Files:**
- Modify: `kubernetes/apps/litellm/litellm/app/helmrelease.yaml`

**Interfaces:**
- No new interfaces — this is pure `values:` content change, no secret/service names introduced
  or consumed.

- [ ] **Step 1: Add `logLevel: ERROR`**

Add a top-level `logLevel: ERROR` key under `spec.values`, right after `replicaCount: 1`:

```yaml
    replicaCount: 1
    logLevel: ERROR
    podAnnotations:
```

- [ ] **Step 2: Add `LITELLM_MODE` to `envVars`**

Current block:

```yaml
    envVars:
      TZ: America/Los_Angeles
      S3_BUCKET_NAME: litellm-s3
      S3_REGION_NAME: us-west-1
      S3_ENDPOINT_URL: https://s3.kantai.xyz
```

New block:

```yaml
    envVars:
      TZ: America/Los_Angeles
      S3_BUCKET_NAME: litellm-s3
      S3_REGION_NAME: us-west-1
      S3_ENDPOINT_URL: https://s3.kantai.xyz
      LITELLM_MODE: PRODUCTION
```

- [ ] **Step 3: Add `rust: true` to all three `model_list` entries**

Current block:

```yaml
      model_list:
        - model_name: claude-sonnet-5
          litellm_params:
            model: anthropic/claude-sonnet-5
            api_key: os.environ/ANTHROPIC_API_KEY
        - model_name: claude-opus-5
          litellm_params:
            model: anthropic/claude-opus-5
            api_key: os.environ/ANTHROPIC_API_KEY
        - model_name: claude-fable-5
          litellm_params:
            model: anthropic/claude-fable-5
            api_key: os.environ/ANTHROPIC_API_KEY
```

New block:

```yaml
      model_list:
        - model_name: claude-sonnet-5
          litellm_params:
            model: anthropic/claude-sonnet-5
            api_key: os.environ/ANTHROPIC_API_KEY
            rust: true
        - model_name: claude-opus-5
          litellm_params:
            model: anthropic/claude-opus-5
            api_key: os.environ/ANTHROPIC_API_KEY
            rust: true
        - model_name: claude-fable-5
          litellm_params:
            model: anthropic/claude-fable-5
            api_key: os.environ/ANTHROPIC_API_KEY
            rust: true
```

- [ ] **Step 4: Add pooling/batching settings to `general_settings`**

Current block:

```yaml
      general_settings:
        master_key: os.environ/PROXY_MASTER_KEY
        store_model_in_db: false
```

New block:

```yaml
      general_settings:
        master_key: os.environ/PROXY_MASTER_KEY
        store_model_in_db: false
        database_connection_pool_limit: 10
        proxy_batch_write_at: 60
```

- [ ] **Step 5: Add timeout/logging settings to `litellm_settings`**

Current block:

```yaml
      litellm_settings:
        callbacks: ["prometheus", "s3_v2"]
        s3_callback_params:
          s3_bucket_name: os.environ/S3_BUCKET_NAME
          s3_region_name: os.environ/S3_REGION_NAME
          s3_endpoint_url: os.environ/S3_ENDPOINT_URL
          s3_use_virtual_hosted_style: true
```

New block:

```yaml
      litellm_settings:
        callbacks: ["prometheus", "s3_v2"]
        s3_callback_params:
          s3_bucket_name: os.environ/S3_BUCKET_NAME
          s3_region_name: os.environ/S3_REGION_NAME
          s3_endpoint_url: os.environ/S3_ENDPOINT_URL
          s3_use_virtual_hosted_style: true
        request_timeout: 600
        set_verbose: false
        json_logs: true
```

- [ ] **Step 6: Render locally to verify**

Run: `flate build hr litellm -n litellm --allow-missing-secrets`

Expected: no error. Confirm in the rendered `ConfigMap/litellm-config`'s `config.yaml` that
`general_settings` includes `database_connection_pool_limit: 10` and
`proxy_batch_write_at: 60`; `litellm_settings` includes `request_timeout: 600`,
`set_verbose: false`, `json_logs: true`; all three `model_list` entries have `rust: true` under
`litellm_params`. Confirm the rendered `Deployment/litellm` container env includes
`LITELLM_MODE=PRODUCTION` and `LITELLM_LOG=ERROR` (the latter comes from the chart's `logLevel`
value, not `envVars`). Quote the relevant rendered snippets in your report.

- [ ] **Step 7: Commit**

```bash
git add kubernetes/apps/litellm/litellm/app/helmrelease.yaml
git commit -m "feat(litellm): apply production logging, timeout, and pooling settings; enable rust gateway"
```

---

## Task 2: Postgres read-replica routing

**Files:**
- Modify: `kubernetes/apps/litellm/litellm/app/externalsecret.yaml`
- Modify: `kubernetes/apps/litellm/litellm/app/helmrelease.yaml`

**Interfaces:**
- Consumes: the `DB_PASSWORD` value already generated inside the `litellm-db` `ExternalSecret`
  (Task 1 of the original deployment plan) — reuse it, do not add a second generator.
- Produces: `Secret/litellm-db` key `DATABASE_URL_READ_REPLICA`, consumed by the HelmRelease's
  `db.secret.readReplicaUrlKey`.

- [ ] **Step 1: Add the read-replica key to the `litellm-db` `ExternalSecret`'s template**

Current block (in `kubernetes/apps/litellm/litellm/app/externalsecret.yaml`):

```yaml
  target:
    name: litellm-db
    template:
      data:
        DATABASE_USERNAME: litellm
        DATABASE_PASSWORD: "{{ .DB_PASSWORD }}"
        INIT_POSTGRES_DBNAME: litellm
        INIT_POSTGRES_HOST: pg18vc-rw.database.svc.cluster.local
        INIT_POSTGRES_USER: litellm
        INIT_POSTGRES_PASS: "{{ .DB_PASSWORD }}"
```

New block (one line added):

```yaml
  target:
    name: litellm-db
    template:
      data:
        DATABASE_USERNAME: litellm
        DATABASE_PASSWORD: "{{ .DB_PASSWORD }}"
        DATABASE_URL_READ_REPLICA: "postgresql://litellm:{{ .DB_PASSWORD }}@pg18vc-ro.database.svc.cluster.local:5432/litellm"
        INIT_POSTGRES_DBNAME: litellm
        INIT_POSTGRES_HOST: pg18vc-rw.database.svc.cluster.local
        INIT_POSTGRES_USER: litellm
        INIT_POSTGRES_PASS: "{{ .DB_PASSWORD }}"
```

`pg18vc-ro.database.svc.cluster.local` is CNPG's replica-only service for the `pg18vc` cluster
(confirmed live: `selector: {cnpg.io/instanceRole: replica}`, distinct from `pg18vc-r` which
load-balances across all instances, including the primary, via `selector: {cnpg.io/podRole:
instance}`). `{{ .DB_PASSWORD }}` is the same templated value already used two lines below for
`DATABASE_PASSWORD` — this `ExternalSecret`'s `dataFrom` generator block is unchanged, don't
touch it.

- [ ] **Step 2: Point the HelmRelease at the new key**

Current block (in `kubernetes/apps/litellm/litellm/app/helmrelease.yaml`):

```yaml
    db:
      useExisting: true
      database: litellm
      endpoint: pg18vc-rw.database.svc.cluster.local
      secret:
        name: litellm-db
        usernameKey: DATABASE_USERNAME
        passwordKey: DATABASE_PASSWORD
      deployStandalone: false
```

New block (one line added):

```yaml
    db:
      useExisting: true
      database: litellm
      endpoint: pg18vc-rw.database.svc.cluster.local
      secret:
        name: litellm-db
        usernameKey: DATABASE_USERNAME
        passwordKey: DATABASE_PASSWORD
        readReplicaUrlKey: DATABASE_URL_READ_REPLICA
      deployStandalone: false
```

- [ ] **Step 3: Render locally to verify**

Run: `flate build ks litellm -n litellm --allow-missing-secrets`

Expected: no error; the rendered `Secret/litellm-db` (from the `ExternalSecret`) includes a
`DATABASE_URL_READ_REPLICA` key whose value is a `postgresql://litellm:...@pg18vc-r.database.svc.cluster.local:5432/litellm`
connection string (the templated password portion renders as whatever the local generator
produces — that's expected, not a failure).

Run: `flate build hr litellm -n litellm --allow-missing-secrets`

Expected: no error; the rendered `Deployment/litellm` container env includes a
`DATABASE_URL_READ_REPLICA` entry sourced via `secretKeyRef: {name: litellm-db, key:
DATABASE_URL_READ_REPLICA}`. Quote the relevant rendered snippets in your report.

- [ ] **Step 4: Commit**

```bash
git add kubernetes/apps/litellm/litellm/app/externalsecret.yaml kubernetes/apps/litellm/litellm/app/helmrelease.yaml
git commit -m "feat(litellm): route read-only DB queries to the CNPG replica"
```
