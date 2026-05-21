# Release Notes

## v0.7.0 — 22 May 2026

The **depth-over-breadth** release. No new domain wrappers — instead a 2x performance win on the slowest existing tool, a new WFM tool that closes the demand/capacity triangle, a new daily-cadence skill, and LLM narrative synthesis for the monthly report (closing a 3-release backlog).

### Concurrent fetches in `agent_coaching_pack` (2x speedup)

[`src/genesys_mcp/tools/coaching.py`](src/genesys_mcp/tools/coaching.py). The per-conversation enrichment walk (wrap-up + STA) ran ~400 sequential HTTPs for a 200-conv week — ~30s wall time. v0.7 collapses both endpoints into a bounded `ThreadPoolExecutor` (8 workers, well under Genesys's 300 req/min rate limit). Same wall-clock work, parallel I/O.

Verified on a live tenant: **14.6s vs ~30s baseline**, output JSON byte-identical to v0.6. The two-pass design (local extract → concurrent fetch → scoring) keeps the aggregation logic identical to v0.6, so no race conditions.

### LLM narrative synthesis for `cc-monthly-report`

[`skills/cc-monthly-report/SKILL.md`](skills/cc-monthly-report/SKILL.md) and [`skills/cc-monthly-report/build_report.py`](skills/cc-monthly-report/build_report.py).

Closes the v0.4 pre-announced item that deferred through v0.5 and v0.6. After the build script writes the 6 data-driven sections, the skill now instructs Claude to:

1. Read the freshly-generated HTML to ground in the actual numbers
2. Synthesise 4 narrative sections per a tight template (~120 words each): **Coverage & caveats** · **What worked** · **What went wrong** · **Recommended actions**
3. Pass the markdown back to `build_report.py --with-narrative <md-file>` (new flag) which parses `## Heading` boundaries and slots each section into the HTML with TOC links auto-added

build_report.py has a minimal markdown→HTML pass: paragraphs, `**bold**`, `*italic*`, `` `code` ``, `[links](url)`, `- bullets`. No full markdown engine — the LLM follows a tight template. Output uses a new `.narrative` CSS class (subtle accent left-border) so readers can tell at a glance which sections are LLM commentary vs. data tables.

Backwards-compatible: omitting `--with-narrative` produces the v0.6 data-only report.

### New tool — `volume_vs_forecast`

[`src/genesys_mcp/tools/wfm.py`](src/genesys_mcp/tools/wfm.py). Closes the WFM demand/capacity triangle:

| Tool | Compares |
|---|---|
| `wfm_schedule` (v0.2) | forecast required hours vs **scheduled** hours |
| `volume_vs_forecast` (v0.7) | forecast volume + AHT vs **actual** (this release) |

Per-bucket comparison at 15min / 30min / 1h / 1d granularity. Returns per-interval `{forecast_offered, actual_offered, volume_variance_pct, forecast_aht_s, actual_aht_s, aht_variance_pct}`, plus rolled-up forecast accuracy as MAPE (mean absolute percentage error) and the top-5 worst-forecast buckets.

WFM endpoint archaeology: short-term forecasts span multiple weeks but the `/data` endpoint returns one week at a time, indexed via `?weekNumber=N` (1-indexed). The tool iterates `weekCount` calls, joins per-week 96-quarter-hour arrays via `referenceStartDate` as the time origin, and aggregates into the requested bucket granularity.

Verified against a live tenant for a 7-day window: forecast under-counted volume by 20% (4702 forecast vs 5650 actual) and underestimated AHT by ~80% (forecast 484-525s vs actual 589-1197s) — real WFM analyst signal that the team currently builds in Excel.

### New skill — `cc-daily-brief`

[`skills/cc-daily-brief/`](skills/cc-daily-brief/). Fills the gap between `cc-monthly-report` (monthly cadence) and `cc-coaching-prep` (per-agent, periodic) — a **daily** brief for supervisors at start-of-day.

One prompt: *"daily brief"*, *"morning brief for yesterday"*, *"how did we go yesterday"*. Drops a one-page HTML at `<output_dir>/daily-brief-<YYYY-MM-DD>.html`. Sections:

1. Headline KPIs — voice + message SL today vs rolling-N-day median (defaults 7 days, configurable via `daily_brief.comparison_window_days`)
2. Worst routes — queues by voice SL drop vs their rolling median
3. Flagged agents — top agents by voice AHT excess vs target
4. Repeat-caller callback list — unresolved-from-yesterday repeaters
5. Adherence flags — agents over the combined break/pre-break/meal overrun threshold

Narrower visual idiom than the monthly report (~700px wide, designed for laptop screens or Slack shares without scrolling). Tenant-aware: all flag thresholds (`sentiment_dip`, `aht_excess_pct`, `sl_drop_pp`) read from `daily_brief.flag_thresholds.*` in tenant.yaml.

Install via `make link-skills` (the v0.6 Makefile target picks up new skills automatically).

### Tenant schema additions

[`src/genesys_mcp/tenant.py`](src/genesys_mcp/tenant.py) gained a `daily_brief:` block with `comparison_window_days`, `flag_thresholds.{sentiment_dip, aht_excess_pct, sl_drop_pp}`, and `output_filename_pattern`. All defaults sane; the block is fully optional.

New convenience accessor `cfg.daily_brief_output_path(date_slug)` mirrors `cfg.report_output_path()` and `cfg.coaching_output_path()`.

### Migration notes

- **Existing tenant configs**: keep working unchanged. The new `daily_brief:` block defaults sensibly when omitted.
- **`pyproject.toml`** bumped from 0.6.0 to 0.7.0.
- **Tool count**: 39 → 40 (`volume_vs_forecast`).
- **Skill count**: 3 → 4 (`cc-daily-brief`).
- The 2x speedup in `agent_coaching_pack` is automatic — no config or scope changes.

### Known limitations / out-of-scope

- **`cc-daily-brief` adherence/sentiment flags** — v0.7 surfaces AHT-excess flagged agents only. Sentiment-dip and per-agent adherence are tenant-config knobs that the build script doesn't yet compute (would require an extra round of per-agent STA fetches). v0.7.x extension if signal warrants.
- **`volume_vs_forecast` AHT mismatch interpretation** — the analytics aggregates query is media-agnostic; if the forecast was scoped to voice only but the actuals include message + callback, the AHT MAPE will look much worse than the underlying accuracy. The tool surfaces the numbers; analysts interpret. Filtering by forecast planning-group → media-type is a v0.7.x consideration.
- **`routing_diagnostic` aggregate mode** — still deferred. The new `cc-daily-brief` partially overlaps with it (worst-routes section), so re-evaluating priority post-v0.7.
- **Outbound campaign coverage** — still deferred.

---

## v0.6.0 — 21 May 2026

The **first-run experience** release. Cuts time-from-clone-to-working-report by ~70% via a one-command installer, an end-to-end health check, smarter auto-discovery in the tenant-setup wizard, and timezone awareness across the report skills.

### New tool — `mcp_health_check`

[`src/genesys_mcp/tools/health.py`](src/genesys_mcp/tools/health.py) + CLI entry at [`src/genesys_mcp/health_check.py`](src/genesys_mcp/health_check.py).

Probes one cheap representative endpoint per OAuth scope (the same workloads `cc-monthly-report` actually exercises), validates `tenant.yaml` against the Pydantic schema, and checks every companion skill is symlinked into the Claude Code skills dir. Returns a structured report:

```
genesys-mcp health check
Verdict: READY WITH WARNINGS

OAuth scopes (region: ap-southeast-2)
  ✓ analytics:readonly                     Required — powers queue_performance, agent_performance, ...
  ✓ conversations:readonly                 Required — powers get_conversation, search_conversations, ...
  ✓ users:readonly                         Required — powers find_user, list_users, ...
  ✓ routing:readonly                       Required — powers list_queues, get_queue_members, ...
  ✓ recordings:readonly                    Optional — powers list_recordings, get_recording_url
  ✓ speech-and-text-analytics:readonly     Optional but recommended — powers get_conversation_summary, ...
  ✗ quality:readonly                       Optional (v0.5+) — powers qa_evaluations and the QA section of agent_coaching_pack
      → Genesys Admin → Integrations → OAuth → your client's role → add Quality > readonly
  ✓ workforce-management:readonly          Optional — powers wfm_schedule, list_management_units, ...
  ...

Tenant config
  path: /Users/.../tenant.yaml
  ✓ loaded: tenant='Acme CC' brands=3 MUs=1

Companion skills
  ✓ cc-monthly-report        /Users/.../skills/cc-monthly-report
  ✓ cc-coaching-prep         /Users/.../skills/cc-coaching-prep
  ✓ genesys-tenant-setup     /Users/.../skills/genesys-tenant-setup
```

Each gap comes with a concrete remediation string. Required scopes (analytics / conversations / users / routing) flag as blockers; optional scopes only as warnings. Exposed both as an MCP tool (LLM-callable when a workflow fails) and a CLI (`python -m genesys_mcp.health_check`) invoked by `install.sh` after onboarding.

### One-command installer — `install.sh`

New [`install.sh`](install.sh) at the repo root. Single command does:

1. Clone (or `git pull` if already cloned)
2. `uv sync`
3. Prompt for OAuth creds → write `~/.config/genesys-mcp.env`
4. `claude mcp add genesys` (or print the JSON snippet if `claude` CLI is missing)
5. Symlink every `skills/*/` into `~/.claude/skills/` (or `~/.agents/skills/`, auto-detected)
6. Run the health check; exits non-zero if blocked

Idempotent — re-run any time to upgrade or re-link. Replaces the README's 5-step manual install for the common case.

New [`Makefile`](Makefile) covers repeat-use targets: `make sync`, `make link-skills`, `make health`.

### Auto-discovery improvements (`genesys-tenant-setup` wizard)

[`skills/genesys-tenant-setup/setup.py`](skills/genesys-tenant-setup/setup.py) gained two new probes and meaningfully smarter behaviour on two existing ones — closing the two v0.4 known limitations and grounding more answers in real tenant data.

- **`probe_organisation()`** — pulls `/organizations/me`, maps `defaultCountryCode` to a sensible IANA timezone via an 18-country lookup table (AU → Australia/Sydney, US → America/New_York, GB → Europe/London, DE → Europe/Berlin, …). Powers the new `tenant.timezone` config field.
- **`probe_aht_baselines()`** — pulls 60 days of per-user `tHandle` + `tAnswered` aggregates for the discovered specialist roles, then computes p10/p25/p50/p75/p90 of per-user AHT (voice and message, plus ACW for voice). The wizard now prompts with the actual data:

  ```
  Voice AHT — your tenant's actuals (last 60 days, specialists with ≥20 calls):
    p25 (top-performer median): 240s   p50 (team median): 312s   p75: 401s
  Suggested target: 240s   Use 240s? (y / enter your own)
  ```

  Tenants whose performance differs materially from the 285s "industry default" now get a starting point grounded in their own data, not a guess.
- **Queue separator auto-detection** — `probe_queues()` no longer hardcodes `" - "`. Samples queue names, scores each of six common separators (` - `, ` / `, ` | `, ` :: `, `_`, `:`), and picks the dominant one. Closes the v0.4 known limitation. Confidence surfaced as `separator_confidence` for the wizard to flag low-signal cases.
- **Multi-locale pre-break presence** — `probe_pre_break_presence()` now iterates **every** language label on each presence (not just `en_US`) and matches against an expanded keyword set covering English, French (`pré-pause`, `avant pause`), German (`vor pause`), and Spanish (`prepausa`, `antes de la pausa`). Closes the v0.4 known limitation.

### Tenant schema — new `tenant.timezone` field

[`src/genesys_mcp/tenant.py`](src/genesys_mcp/tenant.py) gained a `timezone` field on the `_Tenant` sub-model. Optional with a default of `"UTC"` (existing configs keep working). IANA-name validated (light check — `Area/Location` shape).

The two report skills (`cc-monthly-report` and `cc-coaching-prep`) now read `cfg.tenant.timezone` and use Python's `zoneinfo.ZoneInfo` for period-to-UTC conversion instead of hardcoding AEST/UTC+10. Non-AU tenants no longer need to specify the offset on every prompt.

### Migration notes

- **Existing tenant configs**: keep working unchanged. The new `tenant.timezone` field defaults to `"UTC"`. To benefit from the timezone-aware skills, either re-run `genesys-tenant-setup` (auto-discovers from country code) or add `timezone: "Your/Zone"` under the `tenant:` block by hand.
- **`pyproject.toml`** bumped from 0.5.0 to 0.6.0.
- **Tool count**: 38 → 39 (`mcp_health_check`).
- **New files at repo root**: `install.sh`, `Makefile`. No new runtime dependencies.

### Known limitations / out-of-scope

- **LLM narrative synthesis for the monthly report's 4 hand-written sections** ("Coverage & caveats", "What worked", "What went wrong", "Recommended actions") — still pre-announced, still deferred. Planned for v0.6.1 or v0.7.
- **`routing_diagnostic` aggregate mode** — still deferred from v0.5; v0.6.1 candidate.
- **Outbound campaign coverage** — still deferred.
- **AHT baseline percentiles**: when fewer than 5 specialists have ≥20 answered calls in the 60-day window, the wizard falls back to static defaults (285 / 660 / 15s) rather than show noisy percentiles. Small / brand-new tenants won't get auto-suggestions until they have more activity.

---

## v0.5.0 — 18 May 2026

The **coaching ecosystem** release. Three new MCP tools plus a new tenant-aware skill (`cc-coaching-prep`) that turns 1:1 prep into a one-prompt HTML brief. Built on the v0.4 tenant-config plumbing — portable from day 1.

### New tool — `qa_evaluations`

[`src/genesys_mcp/tools/quality.py`](src/genesys_mcp/tools/quality.py). First coverage of the `/api/v2/quality/*` surface. For a list of users + interval, returns avg score, pass rate, critical-pass rate, last-evaluated, plus per-evaluation rows (form, evaluator, total score, conversation id). Optional per-question detail + evaluator comments behind `include_question_detail=True` (opt-in because comments can be PII).

Soft-fails on 403 with `scope_available: false` when the OAuth client doesn't have `quality:readonly` — same graceful-degrade pattern as the speech & text analytics tools. Always requests `expand_answer_total_scores=True` internally because without it Genesys returns evaluations with an empty `answers` block (so the SDK helper-method behaviour silently returns no scores — easy gotcha if you build your own).

New OAuth scope required to use this: `quality:readonly`. Without it the tool soft-fails and downstream tools (`agent_coaching_pack`'s QA section) gracefully skip the QA section.

### New tool — `agent_coaching_pack`

[`src/genesys_mcp/tools/coaching.py`](src/genesys_mcp/tools/coaching.py). One-shot composition tool for Team-Leader 1:1 prep. Single call returns volume + AHT/ACW vs target, peer-median comparison, sentiment trajectory, QA score summary, wrap-up discipline (note rate + top dispositions), top flagged calls, and a heuristic top-3 recommended coaching focus with concrete evidence (*"Voice AHT 330s vs target 285s (+15.8%) — 14 handle-hours over target this period"*).

Composes existing tools rather than duplicating their logic: `agent_performance` (via the same canonical UI-matching aggregates filter), the conversation-details job (for the per-call walk), `_sta_details` from `reports.py` (for sentiment), and the new `qa_evaluations`.

Tenant-aware via `~/.config/genesys-mcp/tenant.yaml`:

- Loads AHT/ACW targets from `targets.*`
- Loads flagged-call thresholds (sentiment-drop magnitude, silent seconds, AHT-excess %) from the new `coaching.flagged_call_thresholds.*` block
- Falls back to in-code defaults (voice 285s / message 660s / ACW 15s; sentiment 0.5 / silent 30s / aht-excess 20%) when no config file present, so the tool also works standalone via the MCP

Gracefully degrades: no `quality:readonly` → QA section reports `scope_available: false`; no speech-and-text-analytics → sentiment section reports empty; the rest always populates.

### New tool — `routing_diagnostic`

[`src/genesys_mcp/tools/routing.py`](src/genesys_mcp/tools/routing.py). Answers *"why did this conversation end up where it did?"* for a specific conversation. Returns:

- **outcome**: answered / abandoned (+ reason) / other, with explanation
- **path**: chronological IVR → queue → agent path with per-segment durations, eligible-agent counts surfaced from session-level `eligibleAgentCounts` (Genesys-provided at routing time, not a current-state proxy), active skill ids, requested routings
- **queues_visited**: each unique queue touched with routing config (skill requirements, evaluation method, ACW settings, auto-answer flag) plus current eligible-agent counts broken down by `IDLE` state
- **timing**: total time-in-ACD-queue, time-to-first-answer, transfer count

Uses `get_analytics_conversation_details` (not the live `get_conversation` endpoint, which exposes participants but doesn't surface segments the same way). Session-level `eligibleAgentCounts` from the analytics view are accurate for the moment of the call — the queue-level `eligibility_now` is current-state and most useful for recent failures.

v0.5 ships conversation_id mode only. Aggregate mode (*"show me all this week's abandons by failure-mode"*) planned for v0.5.x — needs a different endpoint shape.

### New skill — `cc-coaching-prep`

[`skills/cc-coaching-prep/`](skills/cc-coaching-prep/). One-prompt 1:1 coaching brief for a single agent — *"prep coaching for [agent] for the last 4 weeks"*. Pattern mirrors `cc-monthly-report`: SKILL.md drives orchestration, `build_report.py` does the HTML render. Drops the brief at `<reports.output_dir>/<coaching_filename_pattern>` — typically `~/Documents/coaching-<agent-slug>-<period>.html`.

The HTML uses the same visual idiom as `cc-monthly-report` — colour-coded vs-target pills (`+15%` green/amber/red), peer-comparison badges, KPI cards, section cards, no JavaScript. Talking points (the LLM-synthesised conversation-starter list on top of the data) are emitted by Claude in chat at the end of the run, not embedded in the HTML, so they don't fossilise between runs.

Install via symlink:

```bash
ln -s "$(pwd)/skills/cc-coaching-prep" ~/.claude/skills/cc-coaching-prep
```

### Tenant config — new `coaching` block

[`src/genesys_mcp/tenant.py`](src/genesys_mcp/tenant.py) gained a `_Coaching` sub-model:

- `coaching.peer_grouping`: `role` / `queue` / `mu` — strategy for resolving the comparison peer set (default `role`)
- `coaching.flagged_call_thresholds.{sentiment_drop, silent_seconds, aht_excess_pct}` — knobs that decide which calls get flagged for review
- `coaching.coaching_filename_pattern` — output filename pattern for the new skill

All fields have sensible defaults. Existing tenant.yaml files keep working unchanged; the block can be omitted entirely. The example at [`skills/cc-monthly-report/tenant.example.yaml`](skills/cc-monthly-report/tenant.example.yaml) and the schema doc at [`docs/tenant-config-schema.md`](docs/tenant-config-schema.md) now show the optional block.

### Migration notes

- **OAuth scope change**: to enable `qa_evaluations` and the QA section in `agent_coaching_pack`, add `quality:readonly` to your OAuth client's role. The tools soft-fail gracefully without it.
- **Existing tenant configs**: keep working unchanged. The new `coaching:` block is fully optional with sane defaults.
- **`pyproject.toml`** bumped from 0.4.0 to 0.5.0.
- **Tool count**: 35 → 38.

### Known limitations / out-of-scope

- **`routing_diagnostic` aggregate mode** — *"show me all this week's abandons"* — deferred to v0.5.x; v0.5 ships conversation_id mode only.
- **LLM narrative synthesis for the monthly report's 4 hand-written sections** ("Coverage & caveats", "What worked", "What went wrong", "Recommended actions") — pre-announced in v0.4 notes, still planned for its own v0.5.x slot rather than this release.
- **`cc-coaching-prep` for message-only agents** — sentiment section will be empty because Genesys STA on message channels is partial; works as designed but flagged for transparency.
- **Outbound campaign performance** — deferred. Large slice of community but a separate domain wrapper; would be its own v0.6 conversation.

---

## v0.4.0 — 8 May 2026

Makes the companion skills **tenant-agnostic**. Adds a per-user tenant config (`~/.config/genesys-mcp/tenant.yaml`) plus a guided setup wizard that auto-discovers most values from the read-only OAuth client. Anyone cloning this repo can now run `cc-monthly-report` against their own tenant without editing Python or skill prose.

### New: `~/.config/genesys-mcp/tenant.yaml` — tenant-specific knobs in one place

Brand list, queue naming pattern, WFM management unit, business unit, pre-break presence, specialist role list, AHT/ACW/pre-break targets, FTE-hours-per-month, output directory and filename pattern — everything that was previously hardcoded somewhere in the cc-monthly-report skill or `build_report.py` now lives in a single per-user YAML file.

- Schema documented at [`docs/tenant-config-schema.md`](docs/tenant-config-schema.md).
- Generic example at [`skills/cc-monthly-report/tenant.example.yaml`](skills/cc-monthly-report/tenant.example.yaml) — copy this to `~/.config/genesys-mcp/tenant.yaml` and edit by hand if you'd rather not use the wizard.
- Pydantic-validated by [`genesys_mcp.tenant.load_config()`](src/genesys_mcp/tenant.py); malformed configs surface path-by-path errors before any skill runs.
- File-resolution honours `$GENESYS_MCP_CONFIG`, `$XDG_CONFIG_HOME`, then defaults to `~/.config/genesys-mcp/tenant.yaml`. Per-user, never committed.

### New: `genesys-tenant-setup` skill — auto-discover + interview wizard

[`skills/genesys-tenant-setup/`](skills/genesys-tenant-setup/) — invoke via *"set up genesys mcp for my tenant"*. The skill:

1. **Auto-discovers** what it can from the read-only MCP via `setup.py --discover`:
   - Detects queue naming pattern (2-segment vs 3-segment) by parsing real queue names — confidence rating included
   - Extracts brand list from queue prefixes (only brands that appear with multiple known-function values, filtering out one-off rows)
   - Pulls customer-facing function list (filtering out internal-queue labels like Holding / Internal / Supervisor)
   - Suggests skip-substrings from queue-name shapes that don't match the dominant pattern
   - Lists WFM management units with business-unit ids
   - Finds pre-break / drain presence by fuzzy name match on org-level presences
   - Builds a title histogram from active users to suggest specialist-role candidates
2. **Interviews** for the policy/judgement bits (tenant display name, AHT targets, which MUs to include, output filename pattern), using AskUserQuestion for genuine multi-choice picks and conversational prompts for free-text.
3. **Validates and saves** the result via `setup.py --save`, which Pydantic-checks the dict before writing to the resolved config path.

The discovery script reads only — it never writes to the Genesys tenant. The only thing it modifies on disk is the user's `~/.config/genesys-mcp/tenant.yaml`.

### Refactored: `cc-monthly-report` is now tenant-agnostic

[`skills/cc-monthly-report/build_report.py`](skills/cc-monthly-report/build_report.py) and [`skills/cc-monthly-report/SKILL.md`](skills/cc-monthly-report/SKILL.md) had every hard-coded brand name, queue prefix, WFM/BU/presence UUID, and AHT target removed. The build script now:

- Takes `--tenant-config` (defaults to `~/.config/genesys-mcp/tenant.yaml`)
- Loads + validates the config in `main()` and rebinds `VOICE_AHT_TARGET_S`/`MSG_AHT_TARGET_S`/`ACW_TARGET_S`/`FTE_HOURS_PER_MONTH`/`SPECIALIST_ROLES` from it before any aggregator runs
- Passes the config to `render_html()` for the HTML headlines, brand footer, pre-break callouts, and AHT-target text
- Removed four lines of dead tenant-specific synthesis scaffolding (per-brand KPI variables that were never read downstream)

`SKILL.md` v2.0.0 instructs Claude to read the tenant config first, parse queue names against `cfg.queues.name_pattern`, filter by `cfg.brands.names` and `cfg.queues.skip_substrings`, and resolve the output path via `cfg.report_output_path()`.

**Verified end-to-end** by running the skill against the development tenant for 1–7 May 2026 — all six data sections produced correctly with the auto-discovered config.

### Internal — `genesys_mcp.tenant` module

New module exposes:

- `TenantConfig` — Pydantic model with nested sub-models for tenant / brands / queues / management_units / business_unit / presence / specialist_roles / targets / reports
- `load_config(path=None)` — file resolution + parse + validate, raises `TenantConfigError` with path-by-path errors
- `dump_config(config, path)` — validated round-trip writer (used by `genesys-tenant-setup --save`)
- `default_config_path()` — XDG-aware resolution
- Convenience: `cfg.report_output_path(period_slug)` resolves `<output_dir>/<filename_pattern>` with the tenant short-name baked in

### Migration notes

- **Existing users with a working setup** keep working unchanged — when you next pull and run, the skill will look for `~/.config/genesys-mcp/tenant.yaml`. Run the `genesys-tenant-setup` skill to generate it automatically, or copy the example yaml and edit by hand.
- **Forks/new clones** now have a clear onboarding path: run `genesys-tenant-setup`, answer ~6 questions, and the cc-monthly-report skill works against their tenant.
- **Adding `PyYAML>=6.0`** as a runtime dep — required for the YAML config loader. Pulled in automatically by `uv sync`.
- **`pyproject.toml`** bumped from 0.3.0 to 0.4.0.

### Known limitations / out-of-scope

- **Multi-language presence labels** — auto-discovery picks `en_US` first; tenants with non-English primary locales may need to set the pre-break presence id manually.
- **Non-`" - "` queue separators** — currently hard-coded; tenants using `_` or `/` as queue-name separators will fall through to the "no pattern detected" branch and need to provide a pattern manually.
- **The 4 narrative sections in cc-monthly-report's leadership-circulated outputs** ("Coverage & caveats", "What worked", "What went wrong", "Recommended actions") are still hand-written on top of the skill's 6 data sections. v0.5.0 may add stub generation or LLM-driven narrative synthesis.

---

## v0.3.0 — 7 May 2026

Adds an out-of-band **write capability** alongside the read-only MCP, plus a small back-compat refactor to support it.

### New: bulk agent provisioning script (Danger Zone)

[`scripts/provision_users.py`](scripts/provision_users.py) creates new Genesys Cloud users that mirror an existing template agent — same division, manager, location, ACD auto-answer, addresses, title/department, profile skills, routing skills + proficiency, routing languages, group memberships, and WFM management unit. Sends each new agent a Genesys activation email at the end.

Designed for the recurring "I need to onboard 5 new contact-centre agents and clone all their settings from an existing agent" task that's otherwise ~10 clicks per agent across multiple Genesys admin screens.

**Trust model is the load-bearing decision:**

- The script is **not** an MCP tool — Claude cannot reach it. The operator runs it deliberately from a terminal.
- It uses a **separate write-scoped OAuth client** (`GENESYS_WRITE_CLIENT_ID/SECRET`). The read-only MCP client is unchanged and unaware of it; the server's startup warns if write creds leak into the MCP process.
- **`--dry-run` is the default**; explicit `--confirm` is required to write. Interactive `[y/N]:` prompt before any writes when on a TTY.
- **`--self-test`** creates a throwaway user (`@example.invalid` — RFC 2606 reserved TLD, never resolves), exercises every write step, and leaves the user in place by default for manual deletion (so the OAuth role doesn't need `directory:user:delete`).
- **Per-user ledger** at `/tmp/provision_users/<run-id>/<email>.json` enables resume on partial failure. Idempotency pre-check skips users that already exist (with `--reconcile` opt-in to overwrite).
- **`--template-allowlist`** flag refuses any `--template-email` not in a configured list — defends against typos that might silently elevate every new hire by cloning the wrong template's role set.

**Tenant assumptions** (see [`scripts/README.md`](scripts/README.md#tenant-assumptions) — the script will need adapting if these don't match):

1. Authorisation roles inherit from group membership (`rolesEnabled: true` on the relevant groups). The script never calls `PUT /users/{id}/roles`.
2. Queue membership flows from group→queue auto-assignment. The script never calls `/api/v2/routing/queues/{id}/members`.
3. Voice is WebRTC-only — Genesys auto-provisions stations on first sign-in.

**OAuth role** for the write client (granular, no `admin`):

| Operation                          | Permission                       |
|------------------------------------|----------------------------------|
| Create user                        | `directory:user:add`             |
| Edit user                          | `directory:user:edit`            |
| Bulk-assign routing skills         | `routing:skill:assign`           |
| Bulk-assign routing languages      | `routing:language:assign`        |
| Add to group                       | `directory:group:edit`           |
| Move agent into WFM management unit | `wfm:agent:edit`                |
| Send invite                        | `directory:user:setPassword`     |

### Internal — `client.py` two-client refactor

The shared client module now supports loading a non-default OAuth client without touching the read-only singleton:

- `_read_config(prefix=…)` reads from any `GENESYS_*_CLIENT_ID/SECRET` family.
- New `init_named_api(suffix)` and `get_named_api(suffix)` for non-default clients (e.g. `init_named_api("WRITE")` reads `GENESYS_WRITE_CLIENT_*`). Cached in a separate `_named_clients` dict.
- New `with_retry_for(refresh_callable)(fn)` decorator so 401-refresh knows which client to refresh. The original `with_retry(fn)` is preserved as a thin shim — every existing tool keeps working unchanged.
- Retry list extended to include 409 (optimistic-concurrency races on group `version` etc.) and 502/503/504 (transient gateway errors) on top of the existing 401/429 handling.
- New `assert_mcp_env_clean()` is called from the MCP server's lifespan to warn if `GENESYS_WRITE_CLIENT_*` is set in the same process and to refuse to start if `GENESYS_CLIENT_ID == GENESYS_WRITE_CLIENT_ID`.

This is a pure-additive change for read-only consumers. All 9 existing tool modules import unchanged.

### Documentation

- [`scripts/README.md`](scripts/README.md) — full Phase 0 admin setup, day-to-day usage, troubleshooting table, tenant assumptions, ledger format.
- [`README.md`](README.md) — new prominent "⚠️ Danger Zone" section that re-states the read-only MCP boundary and links into the scripts directory.
- [`.env.example`](.env.example) — commented-out write-client env vars.

### Migration notes

- **Nothing breaks** if you don't set `GENESYS_WRITE_CLIENT_*`. The read-only MCP behaves identically to v0.2.1.
- If you happen to have `GENESYS_WRITE_CLIENT_*` already exported in the shell that launches the MCP server, you'll see a new startup warning. Move those exports to a separate shell (or to `.env.write`) — the MCP doesn't need them.
- `pyproject.toml` version bumped from `0.1.0` to `0.3.0` to match the actual release line (the v0.2.x series shipped without bumping pyproject; this catches up).

---

## v0.2.1 — 7 May 2026

Small follow-up to v0.2.0. Moves the companion skill into this repo and tidies the
workforce table in the generated report.

### `cc-monthly-report` skill now lives in this repo

The skill previously lived outside the MCP repo. It now sits under
[`skills/cc-monthly-report/`](skills/cc-monthly-report/) and is installed via
symlink:

```bash
ln -s "$(pwd)/skills/cc-monthly-report" ~/.claude/skills/cc-monthly-report
```

Skills depend tightly on the MCP tool surface — specific tool names, specific
response shapes — so co-locating them avoids cross-repo version drift every
time a tool's response changes. See [`skills/README.md`](skills/README.md) for
rationale and the convention for adding more skills.

### Workforce table refactor — 17 columns → 12

The per-agent workforce table in the generated HTML report was overflowing
horizontally on standard laptop widths. Combined related columns:

- **AHT and "vs target %"** are now a single cell each (e.g. `329s +15%` with the
  badge colour-coded by deviation). Same for ACW.
- **Break-overrun and away-time** counts and total minutes share a cell each
  (e.g. `3 / 47 min`).

Same data, more readable, fits on a single screen.

### Internal

- Inline `_aht_with_target` / `_acw_with_target` / `_count_and_min_cell` helpers
  in `build_report.py`
- New `.vs-target.{good,warn,bad}` CSS classes for inline coloured badges

---

## v0.2.0 — 6 May 2026

A month of intensive iteration since the initial public release. Many tools have been
materially corrected against the Genesys "Performance" UI; one big new tool was added;
several data quality bugs that were silently producing wrong numbers have been fixed.

### New tools

#### `repeat_caller_deep_dive` — root-cause analysis on top of the funnel report

Builds on `repeat_caller_report` by enriching the top repeaters with conversation
summaries, AI outcomes, expected-fix tags, sentiment trajectory and a recommended
next action. For each repeater you get:

- IVR / ACD-offered / answered / abandoned-in-queue / IVR-only counts
- AI disposition counter (`Auto Recharge Query`, `Activation Porting Assistance`, …)
- AI outcome counter (`Resolved` / `Mid Flight` / `Unresolved Chat` / `Escalated`)
- Expected-fix counter (`Simpack Recharge`, `CHOWN`, `Roaming`, …)
- Sentiment trajectory (per-call score) and aggregate trend label
- Last-call summary text from the wrap-up notes
- Heuristic `recommended_action` (`callback_recommended`, `escalate_to_retention`,
  `route_review`, `monitor`)

Plus an org-level rollup with top dispositions, top expected fixes, and the priority
list of `unresolved_repeaters` (≥50% of answered calls not Resolved).

### Existing tools improved

#### `repeat_caller_report` — split funnel + org-wide rollup

The funnel now distinguishes IVR-only abandons from ACD-queue abandons. Each repeater
row carries `acd_offered_count`, `answered_count`, `abandoned_in_queue_count`,
`ivr_only_count`. Response now includes an `org_funnel` block with the same breakdown
across every conversation pulled (not just repeaters), surfacing the org-wide
IVR-drop-off lever alongside the per-customer view.

#### `agent_performance` — now matches the Genesys "Performance > Agents" UI exactly

Major rewrite. The old implementation was wrong in two ways:

1. **Endpoint mismatch.** Was using `post_analytics_users_aggregates_query`, which
   only accepts presence-state metrics (`tAgentRoutingStatus` etc.) and rejected
   `tHandle` / `tTalk` / etc. with HTTP 400.
2. **Filter shape mismatch.** A flat OR of `userId` predicates only captured a
   subset of conversations (mostly outbound), missing most inbound traffic.

Now uses `post_analytics_conversations_aggregates_query` with the same filter shape
the Genesys UI sends — outer `and` of `or` clauses (userId list, optional mediaType
list) — and `groupBy=[userId, mediaType]` for the auto-split. Canonical metrics:
`tAnswered.count` for "Answer", `tHandle.count` for "Handle", plus `tTalkComplete`,
`tHeldComplete`, `tAcw`, `nTransferred`, `nOutbound`, `nBlindTransferred`,
`nConsultTransferred`.

Verified against the live UI: per-agent per-media counts match to the unit (e.g.
a sample specialist's voice 97 / msg 801 in a test tenant for April matched the UI exactly).

#### `queue_performance` — filter aligned to canonical shape

Same filter shape now used by `agent_performance` and the Genesys UI — outer `and` of
`or` clauses. Metric set extended to include `tTalkComplete`, `tHeldComplete`, `tAcw`,
`tShortAbandon`. The derived `answered` field has always come from `tAnswered.count`
(matches the UI's "Answer" column), but the filter alignment makes the tool
internally consistent and ready for cross-media filter clauses.

#### `break_overrun_report` — added AWAY tracking and PRE_BREAK overruns

Two new behavioural signals per agent:

- **AWAY**: every time the agent went on AWAY presence, plus total minutes (raw
  negative — no target). Surfaces inefficiency that the break/meal-only view was
  hiding.
- **PRE_BREAK**: agents are auto-set to a "Pre Break" org-level presence
  (`systemPresence=Busy`, `organizationPresenceId` parameter) ~10 minutes before
  scheduled breaks to drain in-flight interactions. Going over that 10-min target
  is wasted handle time. New fields: `pre_break_count`, `pre_break_overrun_count`,
  `pre_break_overrun_total_min` (sum of duration − 10 min for overrun instances).

The classifier now tracks four presence categories: BREAK, MEAL, AWAY, PRE_BREAK.
AWAY has no target (count + total only). PRE_BREAK target is parameterised
(`pre_break_target_min`, default 10) and uses an `pre_break_organization_presence_id`
parameter so the tool ports cleanly to other tenants.

### Bug fixes

#### Speech-and-Text-Analytics enrichment endpoint

The `/speechandtextanalytics/conversations/{id}/summaries` and
`/speechandtextanalytics/conversations/{id}/sentiments` endpoints exposed by the
Python SDK helpers consistently return 404 / empty even when STA is fully enabled.
Switched to `GET /api/v2/speechandtextanalytics/conversations/{id}` — the
underscored "details" endpoint — which has the real data:
`sentimentScore`, `sentimentTrend`, `sentimentTrendClass`, `empathyScores`, and
`participantMetrics` (agent / customer / silence / ACD / IVR duration percentages).

In one tenant, STA coverage on answered calls jumped from 0% to 99% with no other
change.

#### Wrap-up notes / AI outcomes path

The analytics endpoints (`get_analytics_conversation_details`, conversation details
jobs) do **not** surface wrap-up data — that only appears on the live
`GET /api/v2/conversations/{id}` endpoint, even for completed calls. In tenants
where an external AI writes summaries to wrap-up notes (and structured outcomes to
participant attributes), this previously returned empty for every call.

Per-conversation enrichment now reads:

- `participants[].wrapup.code` / `name` / `notes`
- `participants[].attributes.aiOutcome` (e.g. `Resolved` / `Mid Flight`)
- `participants[].attributes.expectedFix` (e.g. `Simpack Recharge` / `CHOWN`)

#### Sentiment trend labels

For ANIs with only one answered call, `sentiment_trend` was always `insufficient_data`,
which was wasteful — Genesys' own `sentimentTrendClass` on the single call already
reflects the intra-call trajectory. Single-call ANIs now derive their trend from
that field. `NotCalculated` is normalised to `unknown` everywhere it surfaces.

#### Users-details job pagination cap

Three tools (`presence_sessions`, `agent_adherence_review`, `break_overrun_report`)
shared the same job pagination loop with `page_size=100` and `max_pages=20`. For
multi-user month-long pulls this overflowed the 2000-record window — when running
break/adherence for 28 agents, only the first 2 returned data; the other 26
silently came back empty.

Bumped to `page_size=1000` / `max_pages=50` everywhere. Verified: the same 28-agent
April pull now returns data for 22 of them (the remaining 6 are real zeros — new
starters, leadership, or users without WFM Management Unit assignment).

### Notable removals / deprecations

- The previous details-walk implementation in `agent_performance` was correct in
  spirit (counting agent participants with interact segments) but produced numbers
  that didn't match the Genesys UI. Replaced by the aggregates-based implementation
  documented above.

### Migration notes

If you've been calling `agent_performance` and parsing the response shape:

- The summary now uses `answered` and `handled` fields (was `conversations` /
  `connected`).
- The `by_media` map now has `answered` and `handled` per media (was just
  `conversations`).
- `outbound_interactions` is preserved as before; `transferred` now comes from
  `nTransferred.count` directly.

If you've been calling `break_overrun_report` and parsing user records:

- Existing fields (`overrun_count`, `total_overrun_min`, `break_count`, `meal_count`,
  `avg_break_min`, `avg_meal_min`, `overrun_sessions`) are unchanged.
- New fields: `away_count`, `away_total_min`, `pre_break_count`,
  `pre_break_overrun_count`, `pre_break_overrun_total_min`,
  `pre_break_overrun_sessions`, `away_sessions`.

If you've been calling `queue_performance` and parsing the request body:

- Filter shape changed from a flat `or` of queueId predicates to `and` containing
  one `or` clause. Functionally equivalent for queueId-only filters; the new shape
  is what the Genesys UI sends and prepares the tool for cross-media filtering.
- Metric set added `tTalkComplete`, `tHeldComplete`, `tAcw`, `tShortAbandon`.
  Derived fields under `bucket["derived"]` are unchanged.

### Tool inventory

34 tools registered as of this release:

```
list_queues            list_users             find_user_by_email
find_user              list_wrapup_codes      get_user_routing_status
get_user_queues        list_routing_skills    get_user_skills
get_user_presence_now  get_queue_members      queue_observation
queue_performance      queue_estimated_wait_time
agent_performance      search_conversations   get_conversation
list_recordings        get_recording_url      presence_sessions
repeat_caller_report   repeat_caller_deep_dive
break_overrun_report   agent_quality_snapshot live_wallboard
get_conversation_summary  get_conversation_sentiment  get_transcript_url
lookup_external_contact list_management_units  get_user_management_unit
query_agent_adherence_explanations  agent_adherence_review
call_genesys_api
```

---

## v0.1.0 — 29 April 2026

Initial public release. Local stdio MCP server giving Claude Code (or any MCP client)
read-only access to a Genesys Cloud tenant via Client Credentials OAuth.
