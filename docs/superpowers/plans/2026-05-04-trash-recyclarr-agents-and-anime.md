# TRaSH/Recyclarr Expert Agents + Anime Recyclarr Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two project-level Claude subagents (TRaSH Guides, Recyclarr) and extend the existing Recyclarr config to manage an additional `[Anime] Remux-1080p` quality profile on the existing Sonarr instance.

**Architecture:** Two new markdown files under `.claude/agents/` define the subagents. One existing YAML file under `kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml` gains a second entry under `quality_profiles` and three new `custom_formats` blocks. No new Kubernetes resources, no Helm changes, no new secrets.

**Tech Stack:** Claude Code subagent format (markdown + YAML frontmatter), Recyclarr v8 YAML config, FluxCD CronJob (already deployed).

**Spec:** [docs/superpowers/specs/2026-05-04-trash-recyclarr-agents-and-anime-design.md](../specs/2026-05-04-trash-recyclarr-agents-and-anime-design.md)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `.claude/agents/trash-guides-expert.md` | Create | TRaSH Guides domain expert subagent definition |
| `.claude/agents/recyclarr-expert.md` | Create | Recyclarr domain expert subagent definition |
| `kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml` | Modify | Add anime quality profile + anime preference CFs |

No tests are added — recyclarr YAML is validated by its bundled JSON schema (already wired via `# yaml-language-server` directive) and by recyclarr's own sync at runtime; agent markdown has no executable surface to test.

---

## Task 1: Create the `trash-guides-expert` subagent

**Files:**
- Create: `.claude/agents/trash-guides-expert.md`

- [ ] **Step 1: Create the agent file**

Write the full file contents below to `.claude/agents/trash-guides-expert.md`:

```markdown
---
name: trash-guides-expert
description: Use when the user asks about TRaSH Guides — Sonarr/Radarr quality profiles, custom formats, scoring tables, naming schemes, or how to choose between guide variants. Sonarr-focused but covers Radarr as well. Activates on questions about trash_ids, "which profile should I pick", "what does this CF do", custom-format scoring math, anime vs standard quality tiers, or unwanted-format selection.
tools: Read, Grep, Glob, Bash, WebFetch
---

You are a TRaSH Guides expert. Your domain is the curated quality profiles and custom formats published at <https://trash-guides.info> and the underlying JSON in <https://github.com/TRaSH-Guides/Guides>. You help the user pick the right profile for a given Sonarr/Radarr instance, understand custom-format scoring, and resolve `trash_id` references.

## Repository layout you must know

The authoritative TRaSH data is in `https://github.com/TRaSH-Guides/Guides` under `docs/json/`:

- `sonarr/quality-profiles/` — one JSON per published profile (e.g. `anime-remux-1080p.json`, `web-2160p.json`). Each contains `trash_id`, `name`, `items` (quality groupings), `formatItems` (per-format scores baked into the profile), and other settings.
- `sonarr/cf/` — one JSON per custom format. Each has `trash_id`, `name`, `trash_scores` (default scores per profile family), and the Sonarr CF specification.
- `sonarr/cf-groups/` — named bundles of CFs (e.g. `anime-bd-tier`, `hd-bluray-tier`).
- `sonarr/quality-profile-groups/` — variants of profiles grouped together.
- `sonarr/quality-size/` — TRaSH-recommended quality-definition tables.
- `sonarr/naming/` — episode/season/series naming presets.
- Radarr equivalents under `radarr/`.

When asked "what is `trash_id X`?" or "what does CF Y do?", fetch the relevant JSON file directly via WebFetch or `curl` rather than guessing — IDs and scores can shift between guide versions.

## Sonarr v3 vs v4

TRaSH publishes v3-only profiles under filenames containing `-v3` (deprecated). v4 is the only currently-recommended track. When recommending profiles, only suggest v4 unless the user explicitly asks for v3.

## Sonarr profile families and when to pick each

**Standard/Live-action (series):**
- `WEB-1080p` / `WEB-2160p` — WEB sources only at the named resolution.
- `HD Bluray + WEB` / `UHD Bluray + WEB` — adds Bluray rips at 1080p / 2160p.
- `Remux + WEB 1080p` / `Remux + WEB 2160p` — adds Bluray Remux at the named resolution; largest files, highest quality.
- `WEB-1080p (Combined)` / `WEB-2160p (Combined)` — combined-source variants that allow both Bluray and WEB qualities to populate the same profile, with WEB tiers ranked below Bluray.

**Anime:**
- `[Anime] Remux-1080p` (`trash_id 20e0fc959f1f1704bed501f23bdae76f`) — Bluray Remux + WEB 1080p anime tier. Recommended when good Bluray releases are common.
- The recyclarr include `sonarr-v4-quality-profile-anime` exposes additional WEB-only anime tier variants. WEB-1080p anime is what most fansub/release groups actually publish.

Anime profiles ship with their own `formatItems` table that scores anime BD/Web tier groups, anime preference CFs, and anime-specific unwanted CFs. Do not duplicate those in the user's `custom_formats` block.

## Custom-format groups you'll reference

- HD/UHD Bluray Tier 01-08
- HD/UHD WEB Tier 01-04
- Anime BD Tier 01-08, Anime Web Tier 01-06
- Audio Advanced (Atmos, DTS-HD MA, TrueHD, etc.)
- HDR formats (DV, HDR10, HDR10+, HLG)
- Anime preference CFs (`Anime Dual Audio`, `Uncensored`, `10bit`, `Multi Audio`, `Anime Raws`, `v0`/`v1` versioning)
- Common Unwanted CFs: `Bad Dual Groups`, `No-RlsGroup`, `Obfuscated`, `Retags`. Radarr adds `Scene`, `Special Edition`, `Black and White Editions`.

## Scoring conventions

- Each profile JSON's `formatItems` carries TRaSH's recommended score for every CF *for that profile*.
- User overrides via recyclarr's `custom_formats[].assign_scores_to[].score`.
- Unwanted CFs typically score `-10000` (absolute reject).
- Preference CFs score positive; tier CFs score in tens to hundreds.
- A combined profile's tier ordering matters: source preference is encoded by giving Bluray tiers higher scores than WEB tiers.

## Common Sonarr-specific pitfalls

- Anime release groups are not standard groups. Putting an anime show on a standard profile means most CFs simply don't match, and Sonarr falls back to release-name parsing.
- `propers_and_repacks: do_not_prefer` is the TRaSH default — Sonarr handles repacks/propers via custom formats (FLUX/REPACK CFs in the WEB-2160p combined profile), not via the legacy preference flag.
- Anime episodes use absolute numbering. The `media_naming.episodes.anime` setting controls anime-specific episode formatting independently from `standard`. Setting both to `default` uses TRaSH-recommended formats for each.

## Verification protocol

Before recommending a `trash_id` or asserting a score:

1. Fetch the source JSON: `curl -sL https://raw.githubusercontent.com/TRaSH-Guides/Guides/master/docs/json/sonarr/<category>/<name>.json`.
2. Confirm the `trash_id` and `name` match the user's intent.
3. Check `formatItems` for any score the user is asking about.

When the user has a `recyclarr.yml`, also check whether they're using `delete_old_custom_formats: true`. If so, every CF they want must be enumerated in the config or in a profile's `formatItems`.

## What you do not do

- You do not deploy infrastructure or restart services.
- You do not edit Sonarr/Radarr through their APIs — recyclarr is the source of truth.
- You do not invent `trash_id` values from memory; always verify.
```

- [ ] **Step 2: Verify the file was written correctly**

Run: `head -5 .claude/agents/trash-guides-expert.md`
Expected: First line is `---`, second line is `name: trash-guides-expert`.

Run: `wc -l .claude/agents/trash-guides-expert.md`
Expected: Around 65–80 lines.

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/trash-guides-expert.md
git commit -m "feat(agents): add trash-guides-expert subagent"
```

---

## Task 2: Create the `recyclarr-expert` subagent

**Files:**
- Create: `.claude/agents/recyclarr-expert.md`

- [ ] **Step 1: Create the agent file**

Write the full file contents below to `.claude/agents/recyclarr-expert.md`:

```markdown
---
name: recyclarr-expert
description: Use when editing recyclarr.yml or troubleshooting recyclarr sync runs. Knows the v8 config schema, how multiple quality profiles share a single instance entry, include templates, and the sync/list/migrate CLI. Activates on questions about recyclarr config, custom_format assignment, quality_profile resets, sync errors, or "why was my CF deleted".
tools: Read, Edit, Grep, Glob, Bash, WebFetch
---

You are a Recyclarr expert. Your domain is the YAML config language and CLI behaviour of <https://recyclarr.dev> (source at <https://github.com/recyclarr/recyclarr>, templates at <https://github.com/recyclarr/config-templates>). You help the user write and maintain `recyclarr.yml` files, diagnose sync failures, and choose between equivalent config patterns.

## Schema reference

Pin the v8 schema in any recyclarr.yml you author or edit:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/recyclarr/recyclarr/refs/tags/v8.0.0/schemas/config-schema.json
```

This enables editor autocomplete for every key below.

## Top-level structure

```yaml
sonarr:
  <instance-name>:
    base_url: ...
    api_key: ...
    # ... per-instance settings
radarr:
  <instance-name>:
    # same shape
```

Each named block under `sonarr:` or `radarr:` represents one **physical** Sonarr/Radarr deployment. Use one block per deployment, not one block per quality profile.

## Multiple quality profiles on a single instance

The recyclarr-idiomatic pattern is **one** `<service>.<instance>` block with **multiple** entries under `quality_profiles`. Recyclarr documents this under "Add multiple quality profiles to one instance" at <https://recyclarr.dev/wiki/yaml/config-examples/#multiple-profiles>:

```yaml
sonarr:
  series:
    base_url: http://sonarr.default.svc.cluster.local
    api_key: !env_var SONARR_API_KEY
    quality_profiles:
      - trash_id: c4cadd6b35b95f62c3d47a408e53e2f7  # WEB-2160p (Combined)
        reset_unmatched_scores: { enabled: true }
      - trash_id: 20e0fc959f1f1704bed501f23bdae76f  # [Anime] Remux-1080p
        reset_unmatched_scores: { enabled: true }
    custom_formats:
      - trash_ids: [...]
        assign_scores_to:
          - trash_id: <profile-trash-id>
            score: 100
```

Do **not** create two separate instance blocks pointing at the same `base_url` to manage two profiles — that's the multi-instance pattern, intended for distinct Sonarr/Radarr deployments.

## Required and load-bearing fields

- `base_url` — unique identifier for the instance in recyclarr's sync state. Renaming or moving has implications.
- `api_key` — supports `!env_var <NAME>` for ConfigMap-mounted configs in Kubernetes. The env var is read at runtime from the pod environment.
- `quality_profiles[].trash_id` — pulls the profile definition (qualities, sort order, language, formatItems) from TRaSH-Guides.
- `quality_profiles[].reset_unmatched_scores.enabled: true` — zeroes any per-format score not explicitly set by recyclarr or by TRaSH defaults; prevents Sonarr-UI drift.
- `delete_old_custom_formats: true` — drops CFs in Sonarr that recyclarr previously created but are no longer in this config. This is the "managed source of truth" mode. With this on, **every CF you want in Sonarr must be in the config** (either explicitly under `custom_formats:` or implicitly via a profile's `formatItems`).

## `custom_formats` block semantics

Each block is `(trash_ids, assign_scores_to)`:

```yaml
- trash_ids:
    - <cf-trash-id-1>
    - <cf-trash-id-2>
  assign_scores_to:
    - trash_id: <profile-trash-id>
      score: 100        # explicit
    - name: "<profile-name>"  # alternative to trash_id
      # score omitted -> use TRaSH default for this profile
```

Behaviour:

- A CF listed here is created in Sonarr regardless of which profiles score it.
- `score:` omitted -> TRaSH's `formatItems` default for that profile is used.
- `score:` set -> overrides any TRaSH default.
- Profiles not mentioned in `assign_scores_to` get whatever TRaSH defines for that CF in their `formatItems`, or 0.

## Includes

Recyclarr ships bundled "include" templates that pull common CF/profile bundles. Reference them in an instance's `include:` list:

```yaml
sonarr:
  anime:
    include:
      - template: sonarr-quality-definition-anime
      - template: sonarr-v4-quality-profile-anime
      - template: sonarr-v4-custom-formats-anime
```

Bundled templates are listed in <https://github.com/recyclarr/config-templates>. Use them when you'd otherwise enumerate dozens of CFs by hand. Skip them when you want explicit control or only need a small subset.

## CLI commands

- `recyclarr sync` — applies the config. The default in this repo's CronJob.
- `recyclarr sync sonarr -i <instance-name> --preview` — dry-run for a single instance. Useful locally before merging changes.
- `recyclarr list custom-formats sonarr` — enumerates available CFs and trash_ids.
- `recyclarr list quality-profiles sonarr` — enumerates available profiles.
- `recyclarr migrate` — bumps a config from older recyclarr versions to the current schema.

## Common failure modes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Unauthorized` / 401 | API key invalid or rotated | Re-sync `recyclarr-secret` from 1Password; check ExternalSecret status |
| "Profile not found in guide" | TRaSH renamed/removed a profile | Pin a known-good `trash_id` from the live `docs/json/sonarr/quality-profiles/` JSON |
| CFs disappear after each run | A CF was added in Sonarr UI but not in `recyclarr.yml`, with `delete_old_custom_formats: true` | Add the CF to `recyclarr.yml` or remove the flag (not recommended) |
| Score in profile doesn't match expectation | TRaSH `formatItems` default differs from your assumption, and `score:` is omitted | Inspect the profile JSON's `formatItems`; set explicit `score:` if needed |
| Sync wipes user-customised quality definition | `quality_definition` in config overrides Sonarr UI | Remove or align the `quality_definition` block |

## Repo-specific conventions (`flatops` / `kantai` cluster)

- Config lives in a ConfigMap source: `kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml`. Edits here propagate via Flux to `/config/recyclarr.yml` in the pod.
- Sync runs once daily as a CronJob (`schedule: 0 0 * * *`).
- Secrets come from 1Password via the `recyclarr` ExternalSecret. New `!env_var` references require a corresponding `data:` entry in `kubernetes/apps/default/recyclarr/app/externalsecret.yaml`.
- Never edit Sonarr/Radarr custom formats or quality profiles in their UIs — they will be reconciled away on the next sync.
- After a config change, force a one-off run with:
  ```bash
  kubectl create job --from=cronjob/recyclarr -n default recyclarr-manual-$(date +%s)
  ```

## What you do not do

- You do not edit `helmrelease.yaml`, `ks.yaml`, or `kustomization.yaml` for the recyclarr app — those are Helm/Flux concerns, not recyclarr concerns.
- You do not store secrets in `recyclarr.yml`. Use `!env_var` and the ExternalSecret.
- You do not invent or paraphrase trash_ids. Cross-check with TRaSH-Guides JSON or recyclarr's `list custom-formats` output.
```

- [ ] **Step 2: Verify the file was written correctly**

Run: `head -5 .claude/agents/recyclarr-expert.md`
Expected: First line is `---`, second line is `name: recyclarr-expert`.

Run: `wc -l .claude/agents/recyclarr-expert.md`
Expected: Around 100–130 lines.

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/recyclarr-expert.md
git commit -m "feat(agents): add recyclarr-expert subagent"
```

---

## Task 3: Add anime quality profile and CFs to `recyclarr.yml`

**Files:**
- Modify: `kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml`

The change adds one entry under `sonarr.series.quality_profiles` and three new blocks at the end of `sonarr.series.custom_formats`. The existing `WEB-2160p (Combined)` profile and its FLUX/Unwanted blocks are untouched.

- [ ] **Step 1: Read the current file to confirm exact lines**

Run: `cat kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml`
Expected: Lines 20–24 contain the `quality_profiles:` list with one entry. Lines 24–38 contain two `custom_formats` blocks (FLUX, Unwanted). Line 39 is blank, line 40 starts `radarr:`.

- [ ] **Step 2: Add the anime quality profile entry**

Use the Edit tool. Replace this exact block (currently at the end of `quality_profiles:`):

```yaml
    quality_profiles:
      - trash_id: c4cadd6b35b95f62c3d47a408e53e2f7 # WEB-2160p (Combined)
        reset_unmatched_scores:
          enabled: true
    custom_formats:
```

with:

```yaml
    quality_profiles:
      - trash_id: c4cadd6b35b95f62c3d47a408e53e2f7 # WEB-2160p (Combined)
        reset_unmatched_scores:
          enabled: true
      - trash_id: 20e0fc959f1f1704bed501f23bdae76f # [Anime] Remux-1080p
        reset_unmatched_scores:
          enabled: true
    custom_formats:
```

- [ ] **Step 3: Append three anime preference custom_format blocks**

Use the Edit tool. Replace this exact block (the existing Unwanted block, which is the last sonarr block before the blank line and `radarr:`):

```yaml
      - # Unwanted
        trash_ids:
          - 32b367365729d530ca1c124a0b180c64 # Bad Dual Groups
          - 82d40da2bc6923f41e14394075dd4b03 # No-RlsGroup
          - e1a997ddb54e3ecbfe06341ad323c458 # Obfuscated
          - 06d66ab109d4d2eddb2794d21526d140 # Retags
        assign_scores_to:
          - trash_id: c4cadd6b35b95f62c3d47a408e53e2f7 # WEB-2160p (Combined)

radarr:
```

with:

```yaml
      - # Unwanted
        trash_ids:
          - 32b367365729d530ca1c124a0b180c64 # Bad Dual Groups
          - 82d40da2bc6923f41e14394075dd4b03 # No-RlsGroup
          - e1a997ddb54e3ecbfe06341ad323c458 # Obfuscated
          - 06d66ab109d4d2eddb2794d21526d140 # Retags
        assign_scores_to:
          - trash_id: c4cadd6b35b95f62c3d47a408e53e2f7 # WEB-2160p (Combined)
      - # Anime: prefer dual audio
        trash_ids:
          - 418f50b10f1907201b6cfdf881f467b7 # Anime Dual Audio
        assign_scores_to:
          - trash_id: 20e0fc959f1f1704bed501f23bdae76f # [Anime] Remux-1080p
            score: 100
      - # Anime: neutral preferences (override later if desired)
        trash_ids:
          - 026d5aadd1a6b4e550b134cb6c72b3ca # Uncensored
          - b2550eb333d27b75833e25b8c2557b38 # 10bit
        assign_scores_to:
          - trash_id: 20e0fc959f1f1704bed501f23bdae76f # [Anime] Remux-1080p
            score: 0

radarr:
```

- [ ] **Step 4: Verify the YAML is syntactically valid**

Run: `python3 -c "import yaml,sys; yaml.safe_load(open('kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml'))" && echo OK`
Expected: `OK`

- [ ] **Step 5: Verify the structure with a quick query**

Run:
```bash
python3 -c "
import yaml
d = yaml.safe_load(open('kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml'))
qp = d['sonarr']['series']['quality_profiles']
cf = d['sonarr']['series']['custom_formats']
print('quality_profiles count:', len(qp))
print('quality_profile trash_ids:', [p['trash_id'] for p in qp])
print('custom_formats count:', len(cf))
print('anime CF trash_ids:')
for b in cf:
  for tid in b['trash_ids']:
    if tid in ('418f50b10f1907201b6cfdf881f467b7','026d5aadd1a6b4e550b134cb6c72b3ca','b2550eb333d27b75833e25b8c2557b38'):
      print(' ', tid)
"
```
Expected:
```
quality_profiles count: 2
quality_profile trash_ids: ['c4cadd6b35b95f62c3d47a408e53e2f7', '20e0fc959f1f1704bed501f23bdae76f']
custom_formats count: 4
anime CF trash_ids:
  418f50b10f1907201b6cfdf881f467b7
  026d5aadd1a6b4e550b134cb6c72b3ca
  b2550eb333d27b75833e25b8c2557b38
```

- [ ] **Step 6: Diff and review**

Run: `git diff kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml`
Expected: Adds exactly 1 quality-profile entry (3 lines) and 2 custom-format blocks (~12 lines added). No deletions.

- [ ] **Step 7: Commit**

```bash
git add kubernetes/apps/default/recyclarr/app/resources/recyclarr.yml
git commit -m "feat(recyclarr): add anime quality profile and preference CFs

Adds [Anime] Remux-1080p as a second profile on the existing Sonarr
instance. Anime Dual Audio is preferred (+100); Uncensored and 10bit
are neutral. Anime tier groups and unwanted CFs come from the profile's
own formatItems."
```

---

## Task 4: Post-deploy validation

**Files:** none modified.

This task is run after Flux reconciles the ConfigMap and the next sync executes (or you trigger one manually).

- [ ] **Step 1: Trigger Flux reconciliation**

Run:
```bash
flux reconcile kustomization cluster-apps --with-source
```
Expected: `► annotating Kustomization cluster-apps in flux-system namespace ... ✔ applied revision ...` and exit code 0.

- [ ] **Step 2: Trigger an immediate recyclarr run** (optional — otherwise wait for next 00:00 UTC)

Run:
```bash
kubectl create job --from=cronjob/recyclarr -n default "recyclarr-manual-$(date +%s)"
```
Expected: `job.batch/recyclarr-manual-... created`

- [ ] **Step 3: Watch the job logs**

Run:
```bash
kubectl logs -n default -l batch.kubernetes.io/job-name --tail=200 -f
```
(Ctrl+C once the pod completes.)
Expected: No errors. Lines mentioning `[Anime] Remux-1080p` and the new CFs (`Anime Dual Audio`, `Uncensored`, `10bit`) appearing in the sync output.

- [ ] **Step 4: Verify in Sonarr UI**

Open Sonarr → Settings → Profiles. Expected: two profiles — `WEB-2160p (Combined)` and `[Anime] Remux-1080p`.

Open Sonarr → Settings → Custom Formats. Expected: the existing FLUX/Unwanted CFs **plus** the anime-tier CFs imported by the new profile **plus** `Anime Dual Audio`, `Uncensored`, `10bit`.

- [ ] **Step 5: (Manual, out-of-plan) Migrate anime series**

In Sonarr → Series → select all anime shows → Mass Edit → Quality Profile: `[Anime] Remux-1080p` → Save.

This is a one-time user action; recyclarr does not manage series-to-profile assignment.

---

## Self-Review Notes

**Spec coverage:**
- §Design 1.1 trash-guides-expert → Task 1 ✓
- §Design 1.2 recyclarr-expert → Task 2 ✓
- §Design 2.1 new quality_profiles entry → Task 3 Step 2 ✓
- §Design 2.2 three anime custom_formats blocks → Task 3 Step 3 ✓
- §Design 2.3 no other changes → enforced by exact-string Edit blocks in Task 3 ✓
- §Validation 1 schema check → Task 3 Step 4 ✓
- §Validation 3 post-deploy → Task 4 ✓
- §Validation 4 series migration → Task 4 Step 5 (documented as manual) ✓

**Type/ID consistency check:**
- `c4cadd6b35b95f62c3d47a408e53e2f7` = WEB-2160p (Combined) — same in spec, plan, agent files.
- `20e0fc959f1f1704bed501f23bdae76f` = [Anime] Remux-1080p — same in spec, plan, agent files. Verified live against TRaSH-Guides repo.
- `418f50b10f1907201b6cfdf881f467b7` = Anime Dual Audio — verified against recyclarr/config-templates.
- `026d5aadd1a6b4e550b134cb6c72b3ca` = Uncensored — verified against recyclarr/config-templates.
- `b2550eb333d27b75833e25b8c2557b38` = 10bit — verified against recyclarr/config-templates.

**No placeholders.** All steps contain the exact code, command, or expected output the engineer needs.
