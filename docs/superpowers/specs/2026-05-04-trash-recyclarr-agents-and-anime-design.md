# TRaSH/Recyclarr Expert Agents + Recyclarr Anime Support

**Date:** 2026-05-04
**Repo:** `flatops` (kantai cluster)
**Author:** Claude (brainstormed with @jfroy)

## Goal

1. Add two project-level expert subagents that encode domain knowledge for editing
   the Recyclarr config in this repo:
   - `trash-guides-expert` — TRaSH Guides (https://trash-guides.info)
   - `recyclarr-expert` — Recyclarr (https://recyclarr.dev)
2. Extend the existing Recyclarr config to manage an additional anime quality
   profile on the existing single Sonarr instance.

Out of scope: deploying a second Sonarr container, modifying Radarr config,
moving existing series between profiles in Sonarr's UI (a manual user action).

## Non-Goals

- No new Helm releases, namespaces, or Kubernetes manifests.
- No changes to `helmrelease.yaml`, `externalsecret.yaml`, or `ks.yaml` for the
  recyclarr app — only the YAML config in the ConfigMap source changes.
- No changes to the Radarr instance entry.
- No new SOPS-encrypted material; the existing `SONARR_API_KEY` secret already
  covers the new profile.

## Background

The repo manages Recyclarr via Flux:

- HelmRelease at `kubernetes/apps/default/recyclarr/app/helmrelease.yaml`
  (app-template chart, CronJob, daily sync).
- Config source at `kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml`,
  mounted via ConfigMap into `/config/recyclarr.yml`.
- ExternalSecret pulls `SONARR_API_KEY` and `RADARR_API_KEY` from 1Password.

The current Sonarr config defines a single quality profile —
`WEB-2160p (Combined)` (`trash_id c4cadd6b35b95f62c3d47a408e53e2f7`) —
with the FLUX preference and a small "Unwanted" CF set.

## Design

### 1. Project-level subagents

Both agents live under `.claude/agents/` (project-scoped, checked into the repo).
They are research/advisory agents — they edit Recyclarr config files and answer
questions, but do not deploy infrastructure.

#### 1.1 `trash-guides-expert`

**File:** `.claude/agents/trash-guides-expert.md`

**Frontmatter:**

```yaml
---
name: trash-guides-expert
description: Use when the user asks about TRaSH Guides — Sonarr/Radarr quality profiles, custom formats, scoring tables, naming schemes, or how to choose between guide variants. Sonarr-focused but covers Radarr as well. Activates on questions about trash_ids, "which profile should I pick", "what does this CF do", custom-format scoring math, anime vs standard quality tiers, or unwanted-format selection.
tools: Read, Grep, Glob, Bash, WebFetch
---
```

**Body covers (high-level):**

- TRaSH Guides repo layout
  (`docs/json/sonarr/{cf,cf-groups,quality-profiles,quality-profile-groups,quality-size,naming}`,
  Radarr equivalents). Authoritative source for `trash_id` lookups.
- Sonarr v3 vs v4 separation; v4 is current. Radarr separation by HD/UHD tier.
- Quality-profile families and when to pick each:
  - Series: `WEB-1080p`, `WEB-2160p`, `HD Bluray + WEB`, `UHD Bluray + WEB`,
    `Remux + WEB 1080p`, `Remux + WEB 2160p`, plus the `(Combined)` variants.
  - Anime: `[Anime] Remux-1080p` (`20e0fc959f1f1704bed501f23bdae76f`) and
    related anime-specific profiles. WEB-only anime tiers exist as profile
    variants pulled in via the recyclarr include `sonarr-v4-quality-profile-anime`.
- Custom-format groups: HD/UHD Bluray Tier 01-08, HD/UHD WEB Tier 01-04,
  Anime BD/Web Tier groups, Audio Advanced (Atmos, DTS-HD MA, etc.), HDR
  formats (DV, HDR10, HDR10+), Anime preference CFs (Dual Audio, Uncensored,
  10bit, Multi Audio, Anime Raws, etc.).
- Unwanted CFs commonly used: `Bad Dual Groups`, `No-RlsGroup`, `Obfuscated`,
  `Retags`, `Scene` (Radarr), `Special Edition` (Radarr), `Black and White
  Editions` (Radarr).
- Scoring conventions: TRaSH-defined `formatItems` per profile carry the
  default scores; user overrides via `custom_formats[].assign_scores_to[].score`.
  Unwanted CFs typically score `-10000`. Preference CFs score positive.
- "Combined" profiles vs single-source profiles: combined profiles include
  both Bluray and WEB qualities with WEB tiers downstream of Remux, so a
  single profile can match either source.
- Naming conventions: Sonarr `series: plex-tvdb`, Radarr `folder: plex-tmdb`.
  TRaSH publishes the canonical anime/standard/daily episode formats.
- Common Sonarr-specific pitfalls:
  - Anime release groups are not standard groups; using a non-anime profile
    on an anime show wastes scoring on irrelevant CFs.
  - `propers_and_repacks: do_not_prefer` is the TRaSH default — Sonarr handles
    repacks/propers via custom formats (FLUX/REPACK CFs).
  - Anime episodes use absolute numbering; the `media_naming.episodes.anime`
    field controls the format independently from standard.
- Verification protocol: when uncertain about a `trash_id`, fetch it from
  https://raw.githubusercontent.com/TRaSH-Guides/Guides/master/docs/json/sonarr/...
  rather than guessing. Same applies to score values inside `formatItems`.

#### 1.2 `recyclarr-expert`

**File:** `.claude/agents/recyclarr-expert.md`

**Frontmatter:**

```yaml
---
name: recyclarr-expert
description: Use when editing recyclarr.yml or troubleshooting recyclarr sync runs. Knows the v8 config schema, how multiple quality profiles share a single instance entry, include templates, and the sync/list/migrate CLI. Activates on questions about recyclarr config, custom_format assignment, quality_profile resets, sync errors, or "why was my CF deleted".
tools: Read, Edit, Grep, Glob, Bash, WebFetch
---
```

**Body covers:**

- Config schema reference: https://raw.githubusercontent.com/recyclarr/recyclarr/refs/tags/v8.0.0/schemas/config-schema.json
- Top-level keys: `sonarr:`, `radarr:`, each containing one or more named
  instance blocks. Multiple blocks generally mean multiple physical instances,
  not multiple profiles on one — for multiple profiles on the same instance,
  use one block with multiple entries under `quality_profiles`.
- Required fields on an instance: `base_url`, `api_key` (supports `!env_var`
  for ConfigMap-mounted configs).
- Key behaviour flags:
  - `delete_old_custom_formats: true` — drops CFs in Sonarr that recyclarr
    previously created but are no longer in the config. Required for clean
    state; means **all** CFs you want must be in the config every sync.
  - `quality_profiles[].reset_unmatched_scores.enabled: true` — zeroes any
    profile-format scores not explicitly set by recyclarr or TRaSH defaults.
- `custom_formats[]` block semantics:
  - Each block is a `(trash_ids, assign_scores_to)` pair.
  - `assign_scores_to` lists target profiles by `trash_id` or `name`.
  - Omitting `score:` falls back to the TRaSH-defined default in
    `formatItems` for that profile.
  - A CF is created in Sonarr regardless of whether it has a non-zero score
    in any profile.
- Multiple quality profiles in one instance: each profile gets its own
  `formatItems` from TRaSH, scored independently. CFs only assigned to
  profile A get score 0 in profile B (or whatever TRaSH baked in).
- Includes (`include: [{ template: ... }]`) reference recyclarr's bundled
  templates (e.g. `sonarr-v4-quality-profile-anime`, `sonarr-quality-definition-anime`).
  Useful for avoiding manual enumeration of large CF sets.
- CLI commands: `recyclarr sync`, `recyclarr list custom-formats`,
  `recyclarr list quality-profiles`, `recyclarr migrate`. The container
  in this repo runs `args: [sync]`.
- Env-var injection: `!env_var SONARR_API_KEY` pulls from the pod env, which
  comes from `recyclarr-secret` via `envFrom`. Adding new secrets means
  adding them to the ExternalSecret first.
- Common failure modes:
  - "API key invalid" — secret rotated; re-sync `recyclarr-secret`.
  - "Profile not found" — TRaSH renamed/removed a profile; check the live
    JSON in TRaSH-Guides repo.
  - CFs disappearing every sync — a CF was added in Sonarr UI but isn't in
    `recyclarr.yml`, and `delete_old_custom_formats: true` is dropping it.
- Repo-specific conventions: config lives in a ConfigMap under
  `kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml`; sync
  runs once daily via CronJob; never edit Sonarr CFs/profiles by hand.

### 2. Recyclarr config changes

Single edit to
`kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml`. The existing
`sonarr.series` block stays; only its `quality_profiles` and `custom_formats`
lists grow.

#### 2.1 New entry under `quality_profiles`

```yaml
- trash_id: 20e0fc959f1f1704bed501f23bdae76f # [Anime] Remux-1080p
  reset_unmatched_scores:
    enabled: true
```

This pulls the anime profile's full quality-item table and TRaSH-default
`formatItems` scores. The existing `WEB-2160p (Combined)` entry is unchanged.

#### 2.2 New `custom_formats` blocks (anime preferences)

Appended after the existing FLUX/Unwanted blocks. Each anime CF is assigned
**only** to the anime profile, so the combined-2160p profile's scoring is
unchanged.

```yaml
- # Anime preferences
  trash_ids:
    - 418f50b10f1907201b6cfdf881f467b7 # Anime Dual Audio
  assign_scores_to:
    - trash_id: 20e0fc959f1f1704bed501f23bdae76f
      score: 100

- trash_ids:
    - 026d5aadd1a6b4e550b134cb6c72b3ca # Uncensored
  assign_scores_to:
    - trash_id: 20e0fc959f1f1704bed501f23bdae76f
      score: 0

- trash_ids:
    - b2550eb333d27b75833e25b8c2557b38 # 10bit
  assign_scores_to:
    - trash_id: 20e0fc959f1f1704bed501f23bdae76f
      score: 0
```

Notes:

- The CFs that ship inside `[Anime] Remux-1080p`'s `formatItems` (Anime BD
  Tier 01-08, Anime Web Tier 01-06, Anime Raws, anime-version CFs, etc.) are
  pulled in automatically by the profile's `trash_id` — they do not need to
  be enumerated here.
- `Anime Dual Audio` at `100` makes recyclarr prefer dual-audio releases.
- `Uncensored` and `10bit` are kept at neutral `0` so the profile doesn't
  bias against them; user can adjust later.
- The existing Unwanted CFs (`Bad Dual Groups`, `No-RlsGroup`, `Obfuscated`,
  `Retags`) remain assigned only to the combined profile. The anime profile's
  TRaSH-baked `formatItems` already includes anime-appropriate unwanted
  formats; duplicating them here would be redundant.

#### 2.3 No other changes

- `media_management`, `media_naming`, `quality_definition`, `delete_old_custom_formats`,
  `base_url`, `api_key`: all unchanged.
- Kustomization and HelmRelease: unchanged.
- ExternalSecret: unchanged (existing `SONARR_API_KEY` covers the new profile).

## Data Flow

```
recyclarr CronJob (daily)
  └─ reads /config/recyclarr.yml (ConfigMap-mounted)
     └─ for each quality_profile trash_id:
        ├─ fetches profile JSON from TRaSH-Guides
        ├─ creates/updates Quality Profile in Sonarr
        ├─ creates/updates referenced Custom Formats in Sonarr
        └─ applies formatItems scores
     └─ for each custom_formats block:
        ├─ creates/updates each CF in Sonarr
        └─ overrides scores per assign_scores_to
     └─ delete_old_custom_formats=true:
        └─ removes CFs created by recyclarr no longer in config
```

After sync, two profiles exist in Sonarr: `WEB-2160p (Combined)` and `[Anime]
Remux-1080p`. User must manually move existing anime series to the new
profile via Sonarr UI / Mass Editor.

## Validation

1. **Local schema check (optional):** Open `recyclarr.yml` in an editor with
   the YAML language server pinned at the v8.0.0 schema — already declared
   in the file's `# yaml-language-server` directive. No new errors should
   appear.
2. **Dry-run sync (manual):** `recyclarr sync sonarr -i series --preview`
   from a local recyclarr install with the same config to verify trash_ids
   resolve and no CFs would be deleted unexpectedly. Optional — the CronJob
   will surface any error in the next daily run.
3. **Post-deploy verification:**
   - `flux reconcile kustomization cluster-apps --with-source` triggers
     ConfigMap update.
   - Wait for next CronJob run, or trigger manually:
     `kubectl create job --from=cronjob/recyclarr -n default recyclarr-manual`.
   - Sonarr UI → Settings → Profiles: confirm `[Anime] Remux-1080p` exists.
   - Sonarr UI → Settings → Custom Formats: confirm `Anime Dual Audio`,
     `Uncensored`, `10bit` appear.
4. **Series migration (manual):** In Sonarr's Mass Editor, filter by anime
   shows and reassign their quality profile. Out of scope for this change
   but documented here for completeness.

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| TRaSH renames/removes the anime profile under the cited trash_id | Verified live against `docs/json/sonarr/quality-profiles/anime-remux-1080p.json` on 2026-05-04. The `recyclarr-expert` agent's verification protocol catches future drift. |
| Sync fails due to a typo in a new trash_id | CronJob has `failedJobsHistory: 1` and `backoffLimit: 0`; failure surfaces in the next pod's logs without retry storm. |
| Existing series CFs accidentally rescored on the anime profile | None of the existing `assign_scores_to` blocks reference the anime profile's trash_id; they are isolated. |
| User manually adds a CF in Sonarr UI and expects it to persist | Existing behaviour: `delete_old_custom_formats: true` removes anything not in `recyclarr.yml`. Documented in the recyclarr-expert agent. |

## Implementation Order

1. Write `.claude/agents/trash-guides-expert.md`.
2. Write `.claude/agents/recyclarr-expert.md`.
3. Edit `kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml` —
   add anime profile entry under `quality_profiles`, append three anime
   `custom_formats` blocks.
4. Commit, let Flux reconcile, watch the next CronJob run.

No tests; this is config-only and validated through the CronJob's own
sync output.
