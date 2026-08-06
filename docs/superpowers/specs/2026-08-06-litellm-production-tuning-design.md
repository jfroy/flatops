# litellm production tuning design

## Goal

Apply litellm's production-readiness recommendations (https://docs.litellm.ai/docs/proxy/prod)
that are actually applicable to this internal-only, low-traffic, single-replica deployment,
plus two low-risk additions from the earlier follow-up list: the Rust AI gateway (mode 1) and
Postgres read-replica routing (now that we know a real replica endpoint exists).

## Scope

- Logging: `ERROR` level, `set_verbose: false`, JSON logs, `LITELLM_MODE=PRODUCTION`.
- Timeouts/pooling: `request_timeout`, `database_connection_pool_limit`, `proxy_batch_write_at`.
- Read-replica routing: `DATABASE_URL_READ_REPLICA` → `pg18vc-ro.database.svc.cluster.local`
  (confirmed live: a replica-only CNPG service, distinct from `pg18vc-r` which load-balances
  across all instances, including the primary).
- Rust gateway mode 1: `rust: true` per model in `proxy_config.model_list`.

Explicitly out of scope / decided against:
- `disable_error_logs: true` — the prod doc recommends it to cut DB bloat, but it directly
  fights this deployment's stated primary goal (auditing/spend logs). Not setting it.
- Resource requests/limits — left unset (chart default), per explicit decision. Matches most
  small apps in this repo; revisit if real usage data suggests a need.
- `/health/drain` graceful-shutdown hook — the token it needs is a literal string baked into
  `lifecycle.preStop.httpGet.httpHeaders[].value`, which Kubernetes does not expand from env
  vars the way `env[].value` does. That would make it the one hardcoded-in-git secret-shaped
  value in an otherwise all-1Password setup, for a hook that's only a refinement on top of the
  default (already-bounded) SIGTERM graceful shutdown. Skipped, per explicit decision.
- `num_workers`/core-count-based scaling — litellm's own Kubernetes guidance is one worker per
  pod + horizontal scaling, not vertical. kantai1's high core count doesn't argue for more
  workers per pod; chasing it would contradict their own recommendation. No change.

## Changes

### `kubernetes/apps/litellm/litellm/app/helmrelease.yaml`

```yaml
    logLevel: ERROR   # was implicit chart default (INFO); use the chart's own knob so it
                       # doesn't fight the "skip injection if envVars already has LITELLM_LOG"
                       # logic in deployment.yaml

    envVars:
      TZ: America/Los_Angeles
      S3_BUCKET_NAME: litellm-s3
      S3_REGION_NAME: us-west-1
      S3_ENDPOINT_URL: https://s3.kantai.xyz
      LITELLM_MODE: PRODUCTION   # new — disables automatic .env loading

    db:
      useExisting: true
      database: litellm
      endpoint: pg18vc-rw.database.svc.cluster.local
      secret:
        name: litellm-db
        usernameKey: DATABASE_USERNAME
        passwordKey: DATABASE_PASSWORD
        readReplicaUrlKey: DATABASE_URL_READ_REPLICA   # new
      deployStandalone: false

    proxy_config:
      model_list:
        - model_name: claude-sonnet-5
          litellm_params:
            model: anthropic/claude-sonnet-5
            api_key: os.environ/ANTHROPIC_API_KEY
            rust: true   # new, all three models
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
      general_settings:
        master_key: os.environ/PROXY_MASTER_KEY
        store_model_in_db: false
        database_connection_pool_limit: 10   # new
        proxy_batch_write_at: 60             # new
      litellm_settings:
        callbacks: ["prometheus", "s3_v2"]
        s3_callback_params: { ... unchanged ... }
        request_timeout: 600   # new
        set_verbose: false     # new
        json_logs: true        # new
```

### `kubernetes/apps/litellm/litellm/app/externalsecret.yaml`

Add one key to the existing `litellm-db` ExternalSecret's `target.template.data` (same
generated password already used for `DATABASE_PASSWORD`/`INIT_POSTGRES_PASS`, just a different
host in the connection string):

```yaml
        DATABASE_URL_READ_REPLICA: "postgresql://litellm:{{ .DB_PASSWORD }}@pg18vc-r.database.svc.cluster.local:5432/litellm"
```

## Risks

- Read replica: per litellm's own docs, no read-after-write consistency guarantee — a
  write-then-immediately-read-same-row flow could see stale data if replication lag is
  meaningful. Low risk for this deployment's actual read patterns (virtual-key lookups, spend
  queries), and the proxy falls back to the writer automatically if the reader is ever
  unreachable at startup.
- Rust gateway is beta upstream, but every unsupported/failing path falls back to Python
  automatically per their docs — cannot regress a request Python already handles correctly.
- Everything else is a straight config value change with no structural risk.
