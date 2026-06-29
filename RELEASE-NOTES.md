# Release Notes

## v1.13.0 — 24 June 2026

**cc-workforce-history skill.** Wraps the v1.12 `user_activity_history` tool with a multi-year HTML report so the *"who was on the floor over the last 3 years?"* class of question stops being a manual Excel pivot job.

### What it produces

Single-file HTML with three sections:

1. **Active agents per quarter** — table + horizontal bar trend visualisation. Columns: quarter, active count, trend bar, joiners (green pill), leavers (red pill).
2. **Tenure trend** — mean + median tenure (months) per quarter with sample size `n`. Measured from each agent's first-active month to the bucket start.
3. **Per-person first/last active table** — every user in scope (active + inactive + deleted). Columns: name, state pill, first active, last active, total handled, joiner/leaver flag pills. Sorted by total_handled desc.

Plus a **data-coverage callout** at the top showing `data_starts_at` when it's later than the requested interval start — distinguishes "no agents handled interactions in Q3 2023" from "Q3 2023 is past Genesys analytics retention" so callers don't read missing data as empty headcount.

### Soft-fail handling (v1.12.1 integration)

If the underlying `user_activity_history` tool returns a canonical soft-fail envelope (`status >= 400`), the build script renders a single-callout page naming the missing scope and the exact remediation — same pattern as the v1.12.1 wrap-up fix. No silent omission, no LLM-narrative fallback.

### Skill flow

1. Confirm tenant config.
2. Resolve the period (default: last 3 years, Australia/Sydney).
3. **One** MCP call: `user_activity_history(interval=..., bucket="quarter", tz_name=...)`.
4. Save result JSON to `/tmp/cc-workforce-history-{period-slug}/result.json`.
5. Run `python skills/cc-workforce-history/build_report.py --data ... --period ... --output ...`.
6. Confirm + brief in chat (headcount, joiners/leavers, tenure direction, `data_starts_at`).

### Files

- `skills/cc-workforce-history/SKILL.md` — runtime prompt + procedure
- `skills/cc-workforce-history/build_report.py` — pure-Python renderer with 4 sections + soft-fail page

### Tests

- `tests/test_workforce_history_skill.py` — 11 new tests covering each section renderer, the data-coverage callout (3 paths: after-window-start warn, at-window-start clean, no-data bad), an end-to-end success render, and the soft-fail render path.

Plus a new `build_report_workforce_history` session-scoped fixture in `tests/conftest.py` following the existing `build_report_*` pattern.

**563 tests** total (552 → 563). All passing.

### Example use

User asks: *"Workforce history report for all users (active, inactive, deleted), reconstruct first/last handled-interaction date, build (a) quarterly active-agent headcount, (b) average tenure trend, (c) per-person first/last active table with joiner/leaver flags. Jul 2023 → Jun 2026, Australia/Sydney."*

Skill makes one MCP call, runs one build script, hands back an HTML file with all three sections plus the `data_starts_at` callout naming the actual retention floor.

### Out of scope

- **Embedding the workforce-history section inside `cc-monthly-report`** — different cadence (multi-year vs single-month) and audience (HR-flavoured vs ops-flavoured). Kept as a separate skill.
- **`dateHired` reconciliation** — Genesys stores `dateHired` on user records; surfaced as `date_created` per-user but not used to override the activity-derived `first_active_date`. For users hired but never active, `dateHired` is the truer signal.
- **Cross-quarter retention rate** — the per-quarter joiner/leaver counts are there but the report doesn't compute "of N joiners in Q3 2024, M were still active in Q3 2025". Easy follow-up.

---

## v1.12.1 — 24 June 2026

**Wrap-up soft-fail envelope + visible "data not retrieved" callout.** Closes a silent-failure → LLM-narrative bug class that surfaced shortly after v1.11.0 shipped.

### What was happening

A user re-ran `cc-monthly-report` against v1.11.0 and got this paragraph in the report:

> *"Wrap-code dataset could not be retrieved for this preview run … those values are shown as Unavailable below. Re-running the report once full Genesys analytics access is available will populate every section."*

That text exists nowhere in the codebase. The `wrap_up_code_distribution` tool errored (likely 403 — `analytics:conversationAggregate:view` missing from the OAuth role), the SDK exception propagated unhandled, and the agent — finding no real wrap-up data to render — *invented narrative* to explain the gap. The build script renders no fallback text; it either shows real data or silently omits the section, leaving a section-shaped hole the agent felt obliged to fill.

### What changed

1. **Tool now returns a canonical soft-fail envelope on `ApiException`** (`src/genesys_mcp/tools/wrapup.py`). Mirrors the v1.3 pattern already used by `qa_evaluations`, `voice_call_quality`, etc. Aggregates 403 → `{"status": 403, "kind": "wrap_up_code_distribution", "message": "... grant 'analytics:conversationAggregate:view' (typically bundled into 'analytics:readonly') ...", "interval": "<echo>"}`. Catalogue 403 (`routing:wrapupCode:view`) → logged warning, falls back to `<unknown {cid[:8]}>` labels but aggregates still render.

2. **Skill renderers detect the envelope and show a visible callout.** `aggregate_wrap_up_section` (monthly) and `aggregate_wrap_up_mini` (daily) recognise `status >= 400` and return a tagged `{_soft_fail: True, status, kind, message}` dict. The render functions then emit a visible "⚠️ Wrap-up data not retrieved (status 403). <message>" callout in the HTML instead of returning empty string.

3. **SKILL.md instructions tightened** in both `cc-monthly-report/SKILL.md` and `cc-daily-brief/SKILL.md`: *"If the tool returns a canonical soft-fail envelope, save it to the data file as-is. Do NOT write narrative paragraphs explaining the gap — the build script renders a visible callout automatically."*

### Result

Every "wrap-up data didn't come back" outcome now produces sourced, visible, remediation-aware text in the report — naming the missing scope, echoing the failing interval. No more LLM-narrative fallback because there's no longer a gap for the narrative to fill.

### Tests

- `tests/test_wrap_up_distribution.py` — +1 test (`TestSoftFailEnvelope`): simulates `ApiException(status=403)` from the SDK, verifies the tool returns the canonical envelope shape.
- `tests/test_render.py` — +1 test: soft-fail envelope into `aggregate_wrap_up_section` → visible callout HTML with status + message + scope name.
- `tests/test_daily_brief.py` — +1 test: same for `aggregate_wrap_up_mini` / `render_wrap_up_mini_card`.

**552 tests** total (549 → 552). All passing.

### Backwards compatibility

Successful-call response shape is unchanged. Skills not yet updated to the v1.12.1 build scripts will see the envelope as input and (per the v1.11 `aggregate_wrap_up_section` behaviour) silently omit the section — same as the v1.11 outcome, just no more LLM-narrative pollution. Updated skills get the visible callout.

### Reserved for v1.13.0

- New `cc-workforce-history` skill wrapping the v1.12.0 `user_activity_history` tool — shipping next.
- Audit other v1.11 tools (NPS attribute search, agent_utilization, wfm_time_off_requests) for the same silent-failure class. They're all read-scope only and may have similar gaps.

---

## v1.12.0 — 24 June 2026

**Workforce history.** New `user_activity_history` tool answers the long-standing *"who was on the floor in Q3 2023, who joined / left each quarter, and what's the tenure trend over the last 3 years?"* class of question — questions that previously required Excel-pivots over per-user `agent_performance` runs spanning every month.

### Why this matters

Two existing tools got you close but not all the way:

- `list_users(state="active"|"inactive"|"deleted")` returned users one state at a time.
- `agent_performance(user_ids, interval=...)` showed activity for *the interval you asked about*, with no way to surface "first ever handled interaction" without N×M probing.

Neither supported the dominant workforce-history shape: *"per-quarter active-agent count + joiners + leavers + tenure trend over a multi-year window across every user who's ever worked here, whether they're still active or not."*

### What `user_activity_history` does

```python
user_activity_history(
    user_ids: list[str] | None = None,    # default: all users state=any
    interval: str | None = None,          # default: last 3 years
    bucket: str = "quarter",              # "quarter" | "month"
    tz_name: str = "Australia/Sydney",
    include_inactive: bool = True,
    include_deleted: bool = True,
    max_workers: int = 4,
)
```

Returns three surfaces in one call:

- **`per_user`** — one row per user: `{user_id, name, email, state, first_active_month, last_active_month, first_active_date, last_active_date, total_handled, active_buckets, is_joiner_in_window, is_leaver_in_window}` sorted by total_handled desc.
- **`headcount_by_bucket`** — per quarter (or month): `{bucket, active_agents, joiners, leavers}`. Joiner = first_active_month falls in the bucket. Leaver = last_active_month falls in the bucket AND it's not the final bucket (final-bucket users are still active).
- **`tenure_trend`** — per bucket: `{bucket, mean_tenure_months, median_tenure_months, n}` where `n` is the active-agent count and tenure is measured from each agent's first_active_month to the bucket start.

### How it works under the hood

- **Directory:** one paginated `GET /api/v2/users?state=any&pageSize=200` call returns every user — active, inactive, and deleted — in one round-trip. No three-pass merging.
- **Activity:** long intervals are chunked into ~yearly slices and the user list into ≤50-id batches (the Genesys OR-clause safe bound). Chunks fire concurrently via `ThreadPoolExecutor`. Each chunk uses `POST /api/v2/analytics/conversations/aggregates/query` with `groupBy=["userId"]`, `granularity="P1M"`, `metric="tAnswered"` and a media-type filter mirroring `cc-monthly-report` (voice + message + callback; email excluded). Three years = three concurrent calls.
- **Reduce:** server-side, find first and last non-zero month per user; map months → quarter buckets in the configured timezone; compute joiner/leaver/tenure in pure Python.

### Retention caveat (surfaced as `data_starts_at`)

Genesys conversations/aggregates retention is typically ~13 months for most regions. Older months come back as zero results without erroring. The tool surfaces `data_starts_at` (earliest YYYY-MM with any activity across any user) so the caller can distinguish *"no agents handled interactions in Q3 2023"* from *"Q3 2023 is past Genesys retention and we couldn't tell"*. Reports should treat anything before `data_starts_at` as unknown.

### Permissions

- `users:user:view`
- `analytics:conversationAggregate:view` (typically bundled into `analytics:readonly`)

If you're using the same OAuth role as the rest of the MCP, no scope changes needed.

### Tests

- `tests/test_workforce_history.py` — 25 new tests covering bucket-key enumeration (quarterly + monthly), month-to-quarter mapping, months-between math, interval chunking, bucket start derivation, joiner/leaver math against synthetic month-count fixtures, and the default-interval resolver.

**549 tests** total (524 → 549). All passing.

### Example use

```python
user_activity_history(
    interval="2023-07-01T00:00:00.000Z/2026-07-01T00:00:00.000Z",
    bucket="quarter",
    tz_name="Australia/Sydney",
)
```

Returns the full Jul 2023 → Jun 2026 workforce-history dataset in one call, with per-person first/last-active rows + quarterly rollups ready to drop into Excel or a dashboard.

### Out of scope (for v1.13+)

- **Skill wiring** — no skill (cc-monthly-report / cc-daily-brief) calls this yet. Currently ad-hoc via direct MCP calls. A `cc-workforce-history` skill or an additional section in `cc-monthly-report` is the natural next step.
- **`dateHired` reconciliation** — Genesys stores a `dateHired` field on user records; the tool surfaces it as `date_created` per-user but doesn't use it to override the activity-derived `first_active_date`. For agents hired but not yet active, `dateHired` is the truer first-day signal.
- **Wrap-up tool soft-fail envelope** — still deferred (separate from this release).

---

## v1.11.0 — 24 June 2026

**Skill wiring catch-up.** Four MCP tools shipped between v1.6 and v1.10 (`agent_utilization`, `wfm_time_off_requests`, `search_conversations_by_attribute`, `wrap_up_code_distribution`) but were never wired into the three reporting skills — each release deferred the integration to "the next one" and the deferral kept compounding. v1.11 closes every deferred wiring in one focused sweep. **No new MCP tools.** Tool count stays at 50.

### Why this happened

- v1.6 added `agent_utilization` (on-queue / occupancy / interactions-per-hour) — never reached the workforce table.
- v1.7 added `wfm_time_off_requests` — leave summaries stayed chat-only.
- v1.8 added `search_conversations_by_attribute` + the `survey` block in tenant.yaml — NPS / agent-score / experience-score tiles never landed.
- v1.10 added `wrap_up_code_distribution` — closed the *"Wrap-up code distribution not available in this run"* placeholder at the tool layer, but the placeholder text still surfaced because no skill called the tool yet.

Result: skills were silently leaving data on the table. Users asking *"what's the NPS today?"* in a daily brief still got the v1.10 brief, not the answer.

### cc-daily-brief — two new cards

- **NPS card** (gated on `cfg.survey.nps_attribute_key`) — score + promoter / passive / detractor counts. Industry NPS bands: ≥30 green, 0–29 amber, <0 red.
- **Wrap-up mini-section** — top 5 wrap-up codes by count + the single largest mover (delta vs immediately prior day, with an up/down arrow).

Both omit silently when the tool wasn't called or returned zero conversations.

### cc-monthly-report — four new sections

- **Customer Experience section** (between Repeat callers and Workforce) — three KPI tiles: NPS, Agent Score, Experience Score. Each gated on the matching `cfg.survey.*_attribute_key`.
- **Wrap-up Codes section** — full distribution table (code, calls, share, prior period, Δ) + Largest movers callout + New / Retired codes callouts.
- **Leave summary** inside the Workforce section (gated on `cfg.business_unit.id`) — *"X day(s) / Yh across N agents this period"* + top 3 leave types + top 3 by hours.
- **Occupancy column** in the workforce table (gated on the `agent_utilization` payload being present) — uses the standard 70-85% band for colour, 60-70% / 85-92% warn, outside that bad.

Workforce table column count: **12 → 13** when occupancy is wired; **12** unchanged when not.

### cc-coaching-prep — per-agent NPS + per-agent disposition mix

- **Section 4a — NPS, this agent** (gated on `cfg.survey.nps_attribute_key`). Calls the org-wide attribute search once, groups by `agent_user_id`, surfaces the target's NPS score + promoter/passive/detractor split + a clickable list of detractor `conversation_id` values for listen-back.
- **Section 4b — Disposition mix vs team.** Two `wrap_up_code_distribution` calls (`user_ids=[target]` + `user_ids=team`) — table of agent % vs team % per code, with codes flagged where the deviation ≥ 10pp.

Both sections sit between section 4 (Wrap-up & handling) and section 5 (Flagged calls). Both omit silently when the input flag isn't passed.

### Graceful-when-absent — verified end-to-end

New `no_survey` weird-tenants fixture exercises a config where:

- `survey:` block is absent entirely → Pydantic defaults all-None.
- `business_unit:` block is absent → `id` is None, leave summary skipped.
- `management_units:` block is absent → `ids` is empty.

The three skills' renders all complete without crashes, without empty placeholders, without TOC entries that go nowhere. Mirrors the v1.0 `operating_model` precedent.

### Tests

- `tests/test_render.py` — +13 tests: CX section, wrap-up section, leave summary, occupancy column (gated + unflagged + colour bands + missing-user case).
- `tests/test_daily_brief.py` — +14 tests: NPS aggregator, NPS card render, wrap-up mini aggregator, wrap-up mini card render.
- `tests/test_coaching_prep.py` — +13 tests: per-agent NPS rollup, per-agent NPS section render, disposition mix aggregation, disposition mix section render.
- `tests/test_weird_tenants.py` — +4 tests + 1 new fixture (`no_survey`): smoke tests for the graceful-when-absent paths across daily brief, monthly report, and coaching prep.

**524 tests** total (482 → 524). All passing.

### What you have to do to adopt v1.11

If your tenant.yaml already has the `survey` block from v1.8 and `business_unit.id` set, the new sections light up automatically next time you run a skill — no migration needed.

If you want NPS / Agent Score / Experience Score tiles, add the relevant key(s) to tenant.yaml:

```yaml
survey:
  nps_attribute_key: "NPS Score"             # exact key your tenant uses
  agent_score_attribute_key: "Agent Score"
  experience_score_attribute_key: "Experience Score"
```

Run a daily brief or monthly report — the matching tiles appear. Leave summary needs `business_unit.id`; occupancy column needs nothing beyond the existing `analytics:readonly` scope.

### Reserved for v1.12

- **Hour-of-day × channel wrap-up heatmap** — needs its own design decision (new MCP tool vs new mode on `wrap_up_code_distribution` with multi-dim groupBy) and a dedicated heatmap render. Out of v1.11 scope.

---

## v1.10.0 — 24 June 2026

**Wrap-up code distribution + period-over-period trend.** Closes the gap that surfaced as *"Wrap-up code distribution not available in this run"* in `cc-monthly-report` and `cc-daily-brief`. Pre-v1.10 no MCP tool aggregated wrap-up codes via Genesys analytics — the only path that produced a wrap-up rollup (`repeat_caller_deep_dive.org_rollup.top_dispositions`) walked one conversation at a time and only covered the repeat-caller cohort. A skill asking *"wrap-up code share across all conversations this month"* had no good data source, so it rendered the "Not available" placeholder.

### Why this happened

- `list_wrapup_codes` (directory tool) returns the catalogue but not usage counts.
- `_fetch_wrapup(conversation_id)` is per-conversation N+1 — fine for a few hundred deep-dive conversations, useless for a month of org-wide traffic.
- Genesys analytics aggregates support `groupBy: ["wrapUpCode"]` natively (confirmed via the platform-api schema), but nothing in the MCP exercised it. v1.10 fixes that.

### New tool: `wrap_up_code_distribution`

```python
wrap_up_code_distribution(
    interval: str | None = None,           # default last 7 days UTC
    queue_ids: list[str] | None = None,    # optional filter (multi-queue OR)
    user_ids: list[str] | None = None,     # optional filter (multi-user OR)
    media_types: list[str] | None = None,  # optional ['voice','message','callback','email']
    include_trend: bool = True,            # compute prior-period comparison
    top_n: int = 25,                       # cap on rows (with "Other (truncated)" rollup)
    mode: str = "summary",                 # "summary" | "full"
)
```

Returns:

```yaml
interval: "..."
as_of_utc: "..."
filters: {queue_ids: null, user_ids: null, media_types: null}

totals:
  conversation_count: 12450
  distinct_code_count: 23
  truncated: false

distribution:                              # sorted by count desc; "Other (truncated)" last
  - wrapup_code_id: "..."
    name: "Customer Resolved"
    count: 4200
    percentage: 33.7
    prior_count: 3900                      # only when include_trend=true
    delta: 300
    delta_pct: 7.7
    movement: "up"                         # "up" | "down" | "flat" (|delta_pct| < 2%)

trend:                                     # null when include_trend=false
  prior_interval: "..."
  largest_movers:                          # top 5 by absolute delta_pct
    - {name: "Callback Requested", delta_pct: 61.5, movement: "up"}
    - {name: "Wrong Number",       delta_pct: -45.2, movement: "down"}
  new_codes_this_period: ["..."]
  retired_codes:        ["..."]
```

### Implementation notes

- **Endpoint**: `POST /api/v2/analytics/conversations/aggregates/query` with `groupBy: ["wrapUpCode"]` and metric `tHandle.count` (count of handled conversations carrying each wrap-up code). One API call returns the whole distribution.
- **Trend**: when `include_trend=True`, a second parallel call against the immediately-prior interval (same length) feeds delta / movement / largest-movers / new + retired-codes detection. `ThreadPoolExecutor(max_workers=2)` — wall time is the slower of the two calls, not the sum.
- **Filter shape**: the canonical outer-`and` of `or` clauses (queue / user / media), matching `agent_performance` / `queue_performance` so wrap-code counts reconcile with the Genesys UI.
- **Code-name resolution**: process-lifetime cache of `RoutingApi.get_routing_wrapupcodes`. First call hits Genesys; subsequent calls hit the cache. Restart the MCP server to refresh after an admin rename. Same pattern as `directory.list_org_presences` (v1.3) and `wfm_activity_codes` (v1.7).
- **Top-N cap**: distribution truncates to `top_n` rows with an `Other (truncated)` rollup row carrying the residual count + prior + delta.

### Tests

`tests/test_wrap_up_distribution.py` — 16 tests covering request body shape, prior-interval computation, distribution sort + percentages, code-name resolution from catalogue, top-N truncation + "Other" rollup, trend (delta_pct + movement classification, largest movers sorted by `|delta_pct|`, new/retired-codes detection), v1.5 envelope contract, and empty-result safety.

**482 tests passing, 50 tools** (was 466 / 49 in v1.8; v1.9 was the relicensing release — no tool changes).

### Skill wiring — coming in v1.11

The full v1.10 plan included wiring this into `cc-monthly-report` (replace the "Not available" placeholder with the new table + chart + largest-movers callout) and `cc-daily-brief` (mini-card with top 5 codes + single largest mover). That landed as a follow-on (**v1.11**) to keep this release scoped to the foundational MCP tool. The skill wiring is independent and can ship without re-touching the tool — the skill build_report.py files just need to add the new fetch + section render.

### OAuth scope

Needs `analytics:conversationAggregate:view` (typically bundled into `analytics:readonly`) plus `routing:wrapupCode:view` for the catalogue resolution.

### Upgrading

`uv sync` for fresh deps. No required config changes. The tool is ready to query ad-hoc against any tenant — the "Not available" message in skills will go away in v1.11 once the build scripts wire it in.

---

## v1.9.0 — 24 June 2026

**Relicensed: MIT → PolyForm Noncommercial 1.0.0.** The project moves to a source-available, dual-licensed model. Noncommercial use — personal projects, research, evaluation, education, and use by nonprofits/public-research/government — remains free. **Commercial use now requires a separate licence from the maintainer**, including building a product or service on the MCP or offering it as a hosted/managed service.

No functional changes: the tool surface, behaviour, and read-only-by-design posture are identical to v1.8.0.

### Licence
- `LICENSE` is now PolyForm Noncommercial 1.0.0.
- Releases up to and including **v1.8.0 remain available under MIT** — rights already granted are not revoked. v1.9.0 and later are under PolyForm Noncommercial 1.0.0.
- Commercial licensing enquiries: <designsbylwd@gmail.com>, or open a GitHub issue labelled `commercial-license`.

### Contributing
- New `CONTRIBUTING.md` and `DCO`. Contributions now require a Developer Certificate of Origin sign-off (`git commit -s`) **plus a contribution licence grant**, so contributions can be included in commercially licensed versions while contributors keep their copyright.

## v1.8.0 — 24 June 2026

**Conversation attribute search + NPS auto-detection.** Closes a reporting gap surfaced by *"can you tell me what the NPS is today?"* and *"how many conversations had outcome = Resolved?"* Pre-v1.8 the MCP had **zero** path to question conversations by participant attribute — `search_conversations` only filtered by ANI / queue / agent / direction, and the async-jobs predicate dimensions don't target `participants[].attributes`. The dedicated Genesys "search by participant attribute" endpoint was completely unwrapped.

### New tool: `search_conversations_by_attribute`

Wraps `POST /api/v2/conversations/participants/attributes/search` and returns four layers in one call: totals, value distribution, numeric summary (with auto-detected NPS when values are integers 0-10), and one row per matching conversation (with `agent_user_id` extracted for downstream per-agent rollups).

```python
search_conversations_by_attribute(
    attribute_key: str,                          # tenant-specific — "NPS Score", "Agent Score",
                                                 # "Experience Score", "outcome", "csat", etc.
    attribute_value: str | None = None,          # exact match; omit to default to NPS enumeration ['0',…,'10']
    interval: str | None = None,                 # default last 7 days UTC
    max_results: int = 1000,
    mode: str = "summary",                       # "summary" | "full"
)
```

Response:

```yaml
interval: "..."
as_of_utc: "..."
attribute_key: "NPS Score"
attribute_value: null
mode: "summary"

totals:
  conversation_count: 142
  truncated: false

value_distribution:                              # sorted by count desc
  - value: "10"
    count: 45
    percentage: 31.7
  - value: "9"
    count: 40
    percentage: 28.2

numeric_summary:                                 # null when values aren't all numeric
  count: 142
  mean: 8.3
  median: 9.0
  min: 0
  max: 10
  nps:                                           # null unless values are integers 0-10
    score: 51.4                                  # (%promoters - %detractors) × 100
    detractors_0_6: 12
    passives_7_8: 45
    promoters_9_10: 85

conversations:                                   # capped at max_results
  - conversation_id: "..."
    conversation_start: "..."
    queue_id: "..."
    agent_user_id: "..."                         # for per-agent NPS rollups
    attribute_value: "9"
```

### Auto-detected NPS rollup

When every matched value parses as an integer in `[0, 10]`, the tool computes the standard NPS automatically:

- **detractors** = count of 0-6
- **passives** = count of 7-8
- **promoters** = count of 9-10
- **score** = `(promoters - detractors) / total × 100`

For non-NPS numeric attributes (e.g. Agent Score 1-5, Experience Score floats), `numeric_summary.nps` is `null` but `count / mean / median / min / max` still populate. For non-numeric attributes (e.g. `Resolved` / `Unresolved`), `numeric_summary` is entirely `null` and `value_distribution` carries the answer.

### New optional `survey` block in `tenant.yaml`

Tenant config gains a new optional top-level `survey` block following the `operating_model` precedent (fully backward-compatible, graceful when absent):

```yaml
survey:
  nps_attribute_key: "NPS Score"             # leave None to opt out
  agent_score_attribute_key: "Agent Score"
  experience_score_attribute_key: "Experience Score"
```

This is the **discovery aid** — callers (and v1.9+ skills) read these keys instead of hardcoding. The tool itself doesn't require the block; it accepts any `attribute_key` string.

### Implementation notes

- **Endpoint**: `POST /api/v2/conversations/participants/attributes/search` (distinct from the analytics async-jobs path).
- **Permission**: `conversation:participant:attributesview` (typically bundled into `conversation:readonly`).
- **Predicate shape**: `query` array with one `DATE_RANGE` criterion (`fields: ["segments.start"]`) AND one `EXACT` criterion (`fields: ["participantData.<key>"]`).
- **Pagination**: 100 results per page, up to `max_results` or 100 pages (10,000), whichever first.
- **Default value enumeration**: when `attribute_value` is None, defaults to `["0","1",…,"10"]` covering NPS. For non-NPS attributes pass the specific value — unbounded scans aren't supported by the underlying endpoint (no exists operator).

### Tests

`tests/test_attribute_search.py` — 13 tests covering request-body shape (default NPS enumeration vs explicit value, DATE_RANGE criterion, endpoint path), NPS detection (positive + 3 negative paths), value distribution sort + percentage math, v1.5 envelope contract, empty-result safety, and `agent_user_id` extraction. **466 tests passing, 49 tools** (was 453 / 48 in v1.7).

### Skill wiring — coming in v1.9

The full v1.8 plan included wiring NPS into `cc-daily-brief` (top-line KPI card), `cc-monthly-report` (Customer Experience section), and `cc-coaching-prep` (per-agent NPS rollup). That landed as a follow-on (**v1.9**) to keep this release scoped to the foundational MCP tool + tenant.yaml schema. The skill wiring is independent and can ship without re-touching the tool.

### Upgrading

`uv sync` for fresh deps. No required config changes. Tenants wanting to opt into the upcoming v1.9 NPS surfacing can set the `survey` block now; it's harmless until v1.9 ships.

---

## v1.7.0 — 22 June 2026

**Leave / time-off reporting.** Closes a reporting gap surfaced by *"can you give me a report on any leave/time off etc over the last 4 weeks?"* Pre-v1.7 the MCP had **zero** access to Genesys' time-off-request endpoint family — the closest signal was `query_agent_adherence_explanations` (post-hoc commentary on off-schedule events, not the approved leave record). When the user asked "who's been off", there was no obvious tool to call.

### New tool: `wfm_time_off_requests`

Per-agent leave / time-off requests over an interval, with activity codes resolved to human-readable names (no UUIDs in the response table) and pre-computed rollups by user and by leave type.

```python
wfm_time_off_requests(
    business_unit_id: str,
    interval: str | None = None,                   # default last 28 days UTC
    user_ids: list[str] | None = None,             # filter to specific agents
    statuses: list[str] | None = None,             # default ["APPROVED", "PENDING"]
    mode: str = "summary",                         # "summary" | "full"
)
```

Four-layer response:

```yaml
interval: "..."
as_of_utc: "..."
business_unit_id: "..."
statuses_queried: ["APPROVED", "PENDING"]

totals:
  request_count: 23
  approved_count: 19
  pending_count: 4
  total_hours: 312.0
  total_days: 39

by_activity:                                       # sorted by total_hours desc
  - activity_name: "Annual Leave"
    request_count: 14
    total_hours: 224.0
    total_days: 28
  - activity_name: "Sick Leave"
    request_count: 6
    total_hours: 48.0
    total_days: 6

by_user:                                           # sorted by total_hours desc
  - user_id: "..."
    user_name: "Jane Doe"
    request_count: 3
    total_hours: 56.0
    total_days: 7
    activities: ["Annual Leave", "Sick Leave"]

requests:                                          # most recent first
  - id: "..."
    user_id: "..."
    user_name: "Jane Doe"
    activity_code_id: "..."
    activity_name: "Annual Leave"
    activity_category: "TimeOff"
    status: "APPROVED"
    is_full_day: true
    start_date: "2026-06-08"
    end_date: "2026-06-12"
    dates: ["2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12"]
    days: 5
    hours: 40.0
    notes: "..."
    modified_by_name: "Supervisor X"
    modified_at: "..."
    submitted_at: "..."
```

**Full-day and partial-day request normalisation.** Genesys emits two different shapes:

- `isFullDayRequest=true` with `fullDayManagementUnitDates: ["2026-06-08", ...]`
- `isFullDayRequest=false` with `partialDayStartDateTimes: ["2026-06-15T09:00:00Z", ...]` + `dailyDurationMinutes`

The tool flattens both into the same `start_date / end_date / dates / days / hours` shape — no caller needs to know which form the API returned.

**Defaults reflect the retrospective lens** (confirmed with the user): interval = last 28 days UTC, statuses = `["APPROVED", "PENDING"]`. Cover both leave actually taken and leave currently in the approval pipeline. Pass `statuses=["APPROVED"]` for confirmed-only, or `["APPROVED","PENDING","DENIED","CANCELED"]` to audit the approval workflow itself.

### New tool: `wfm_activity_codes`

WFM activity-code catalogue for a business unit — the leave-type definitions plus all the other activities (On Queue Time, Break, Meal, Training, etc.). Each row carries the `id`, `name`, `category` (`OnQueueTime` / `OffQueueTime` / `TimeOff` / `Meeting` / `Break` / `Meal` / `Training` / `Unavailable`), `paid` flag, and default `length_minutes`.

**Process-lifetime cached** (pattern from `directory.list_org_presences` v1.3) — first call hits Genesys, subsequent calls hit the in-process cache. Restart the MCP server to refresh after an admin change. The cache also powers `wfm_time_off_requests`'s name resolution — so a follow-up time-off-requests call against the same BU is free of the catalogue lookup.

Answers *"what leave types does this org track?"* in one call.

### Top-level envelope (v1.5 contract held)

Both new tools echo `interval` (where applicable) and `as_of_utc` at the top of the response so persisted-file readers see the window in the first lines. No buried-field hallucination risk.

### OAuth scope

Both tools need `workforce-management:readonly` (typically bundled into the WFM permissions you already have for `wfm_schedule` / `agent_adherence_review`).

### Tests

- `tests/test_timeoff.py` — 17 tests covering activity-code catalogue + cache, request-body shape (YYYY-MM-DD `dateRange`, status default, user-filter propagation), full-day and partial-day normalisation, rollups (totals, by_activity sorted by hours desc, by_user sorted by hours desc with activity-set union), v1.5 envelope contract, cache reuse across multiple `wfm_time_off_requests` calls, and the empty-result safe path.
- **453 tests passing, 48 tools** (was 431 / 46 in v1.6).

### Upgrading

`uv sync` for fresh deps. No config changes required. The two new tools work for any tenant that has WFM enabled — there's no tenant-specific scaffolding.

---

## v1.6.0 — 22 June 2026

**Agent utilization.** Closes a real reporting gap: *"give me all agents, their on-queue time, calls and messages they took, and a ratio."* Pre-v1.6 the MCP could answer "how many answered" via `agent_performance` (conversations/aggregates endpoint) but had no way to answer "how long were they available" — nothing in the codebase queried `/api/v2/analytics/users/aggregates/query` to fetch routing-status durations. Without on-queue time, occupancy and interactions-per-hour were uncomputable.

### New tool: `agent_utilization`

One row per agent, combining routing-status durations + answered counts + three pre-computed productivity ratios.

```python
agent_utilization(
    user_ids: list[str],          # required; resolve via list_users / find_user
    interval: str | None = None,  # default last 7 days UTC; use compute_interval for tz-aware windows
    mode: str = "summary",        # "summary" (default) | "full" (adds raw aggregates under _raw)
)
```

Per-user block:

```yaml
user_id: "..."
user_name: "Jane Doe"

# Routing-status durations (from tAgentRoutingStatus)
on_queue_seconds: 21600        # 6h available-to-route
interacting_seconds: 14400     # 4h actually working an interaction
idle_seconds: 7200             # 2h on-queue but no interaction
not_responding_seconds: 0
off_queue_seconds: 3600        # break / meal / admin (rolled up)

# Answered counts (from tAnswered.count — matches Genesys "Performance > Agents" UI)
voice_answered: 22
message_answered: 14
callback_answered: 0
total_answered: 36

voice_handle_seconds: 7200
message_handle_seconds: 4200
total_handle_seconds: 11400

# Three productivity ratios
interactions_per_on_queue_hour: 6.0   # ← HEADLINE: total_answered / (on_queue / 3600)
occupancy_pct: 52.8                   # total_handle / on_queue × 100
voice_to_message_ratio: 1.57          # voice_answered / message_answered (null if no messages)
```

Top-level (v1.5 contract): `interval`, `as_of_utc`, `mode`, `sort_by`, `routing_status_scope_available`, `user_count`, `users`.

Sorted by `interactions_per_on_queue_hour` descending — high-throughput agents at the top, agents with no on-queue time (null rate) at the bottom.

### Two API calls, fired concurrently

| Call | Endpoint | Purpose |
|---|---|---|
| Routing status | `post_analytics_users_aggregates_query` (groupBy=[userId, routingStatus], metric=tAgentRoutingStatus) | per-user × status seconds |
| Conversations | `post_analytics_conversations_aggregates_query` (mirrors `agent_performance` body) | per-user × media answered + handle |

Both fire in a `ThreadPoolExecutor(max_workers=2)` — wall time is the slower call, not the sum.

### Soft-fail on routing-status 403

Some tenants restrict `analytics:agentRouting:view`. When the routing-status query fails with 403, the tool degrades gracefully:

- `routing_status_scope_available: false` at the top
- A `routing_status_unavailable_note` explaining the degraded state
- Routing-status seconds and derived ratios → null/0
- Conversation-side answered counts still populate — the response is still partially useful

### Divide-by-zero guards

- `interactions_per_on_queue_hour` and `occupancy_pct` are `null` when `on_queue_seconds == 0`
- `voice_to_message_ratio` is `null` when `message_answered == 0` (instead of Infinity)

### Shape validator

New `assert_users_aggregates_envelope` in `genesys_mcp.shapes` — pins the `{results: [{group: {userId, routingStatus}, data: [...]}]}` envelope so any future Genesys-API rename can't silently emit zeros. Five tests pin the validator's behaviour.

### Tests

- `tests/test_agent_utilization.py` — 14 tests covering request shape, response composition, ratio math, divide-by-zero edge cases, sort order, v1.5 envelope contract, and the 403 soft-fail path
- `tests/test_shapes.py` — 5 new tests for `assert_users_aggregates_envelope`
- **431 tests passing, 46 tools** (was 409 / 45 in v1.5)

### OAuth scope

Needs `analytics:agentRouting:view` (typically bundled into `analytics:readonly`). Tenants without it still get answered counts but no routing-status data.

---

## v1.5.0 — 18 June 2026

**Interval clarity + cross-app robustness.** Closes a real cross-app failure mode: when wired into a non-Claude-Code client, a foreign LLM hallucinated a non-existent constraint (*"Genesys can't slice to a calendar day"*) and read stale persisted-output files because the response top didn't surface the interval. Five deliverables — one new tool, top-level response echoes on the four analytical tools, a single-source docstring fragment across twelve tools, and a `_intervals` module that deduplicates timezone helpers previously copy-pasted across five tool modules. **409 tests; 45 tools.**

### New tool: `compute_interval`

The missing path from *"today, AEST"* to a paste-ready ISO interval. Any client (Claude Code, Claude Desktop, Cursor, a custom MCP harness) calls this **once** to convert a period keyword to a tenant-timezone-aware UTC interval, then passes the returned `interval` string to any analytical tool. No more timezone math in client prompts.

```python
compute_interval(
    period: str,                 # 'today' | 'yesterday' | 'this_week' | 'last_week'
                                 # | 'this_month' | 'last_month' | 'last_7_days' | 'last_28_days'
    timezone: str | None = None, # defaults to cfg.tenant.timezone
)
```

Returns:

```yaml
period: "today"
timezone: "Australia/Sydney"
start_local: "2026-06-18T00:00:00+10:00"
end_local: "2026-06-19T00:00:00+10:00"
start_utc: "2026-06-17T14:00:00.000Z"
end_utc: "2026-06-18T14:00:00.000Z"
interval: "2026-06-17T14:00:00.000Z/2026-06-18T14:00:00.000Z"   # paste-ready
weekday_anchor: "Mon"                                            # for week-based periods
```

Spec is intentionally tight: eight enumerated keywords, no natural-language parsing, no month-year strings. Weeks anchor to Monday 00:00 local (ISO 8601 / Australian convention). Months snap to the 1st. `last_7_days` is rolling 7 × 24h ending now (distinct from `this_week` which anchors to Monday). `last_28_days` matches the coaching pack's default window.

Error envelope: `{status: "error", kind: "invalid_argument", message, supported_periods}` on unknown period or unresolvable timezone — no exceptions raised across the MCP boundary.

### Top-level `interval` + `as_of_utc` echo on every analytical tool

The persisted-output bug that triggered this release: a foreign client harness saved a `queue_performance` response to disk and the LLM kept re-reading it instead of calling fresh, because the interval used was buried at `results[].data[].interval` — 4 levels deep. v1.5 surfaces it (plus a generation timestamp) at the very **top** of every analytical response:

```yaml
interval: "<the interval that was queried>"        # always present at top
as_of_utc: "<ISO-8601 UTC at response generation>" # always present at top
granularity: "P1D"                                  # queue_performance only
results: [...]                                       # existing payload below
```

Four tools updated:

| Tool | Pre-v1.5 | v1.5 |
|---|---|---|
| `queue_performance` | **neither** at top | **both** at top |
| `agent_performance` | `interval` at top | + `as_of_utc` |
| `repeat_caller_deep_dive` | `interval` at top | + `as_of_utc` |
| `break_overrun_report` | `interval` at top | + `as_of_utc` |

Reader-of-persisted-file test: `head -20` on a saved response now shows the window in the first lines. The foreign-LLM hallucination depended on the field being invisible; making it visible is the fix.

### Docstring sweep: single source of truth for `interval:` help

Every tool that takes an `interval:` parameter (twelve tools across six modules) now interpolates the same `INTERVAL_HELP_STRING` constant. Pre-v1.5 each tool's `interval:` description drifted independently — a foreign LLM introspecting the tool catalogue couldn't tell whether calendar-day boundaries were supported (they always have been). The new shared string is explicit about it:

```
ISO-8601 interval "startISO/endISO" in UTC. Accepts ANY window — calendar day,
arbitrary range, multi-month. To get a tenant-timezone-aware ISO interval for
a period like "today" or "last_week", call `compute_interval` first.
Example for a calendar day in Australia/Sydney:
  "2026-06-17T14:00:00.000Z/2026-06-18T14:00:00.000Z"
Defaults to the last 7 days UTC if omitted.
```

Tools swept: `queue_performance`, `agent_performance`, `search_conversations`, `repeat_caller_report`, `repeat_caller_deep_dive`, `break_overrun_report`, `agent_quality_snapshot`, `presence_sessions`, `wfm_schedule`, `volume_vs_forecast`, `query_agent_adherence_explanations`, `agent_adherence_review`. `agent_coaching_pack` uses a customised variant that mentions `compute_interval` and notes the 28-day default.

### Shared `_intervals` module — deduplication, no behaviour change

`_default_interval()` was copy-pasted in five tool modules; `_parse_iso()` in three. v1.5 consolidates them in `genesys_mcp._intervals` and replaces the five copies with re-import aliases that preserve the original symbol names (so `coaching.py`'s `from genesys_mcp.tools.reports import _default_interval, _parse_iso` keeps working unchanged).

New module exports:

- `INTERVAL_HELP_STRING` — single canonical docstring fragment
- `SUPPORTED_PERIODS` — `("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "last_7_days", "last_28_days")`
- `default_interval(days: int = 7) -> str` — dedup of the 5 copies
- `parse_iso(s: str) -> datetime` — dedup of the 3 copies
- `now_utc()` — extracted for monkey-patchable tests
- `compute_period_interval(period, timezone_name, *, now=None) -> dict` — the engine behind `compute_interval`

All 375 existing tests still pass; +34 new tests in `tests/test_intervals.py` (25) and `tests/test_response_envelopes.py` (9) lift the suite to **409 passing**.

### Why this matters

The MCP can't stop a foreign LLM from hallucinating, but it can make hallucination strictly harder. Three reinforcing changes — a discovery tool for periods, top-level response echoes, and a docstring sweep that's explicit about what's supported — change the surface area an unprompted client sees so the *"calendar day not supported"* hallucination has nothing to anchor on. Real-world test: from the other (non-Claude-Code) app that triggered this plan, ask *"what's the right interval for queues today, AEST?"* — the assistant now calls `compute_interval` first, then `queue_performance` with the returned string, and returns correct numbers.

### Upgrading

`uv sync` for fresh deps; no config changes required. Existing skills (`cc-monthly-report`, `cc-daily-brief`, `cc-coaching-prep`) continue to use the explicit-ISO-interval path they always have — `compute_interval` is for foreign clients that don't have the skill scaffolding. The five migrated tool modules import from `_intervals` instead of having local copies, but their public behaviour is byte-identical.

---

## v1.4.0 — 3 June 2026

**Call quality + batch ergonomics.** Four deliverables: MOS scores for voice-call quality (closes the gap vs the MakingChatbots MCP), presence-label resolution on `get_user_presence_now`, batch mode for `find_user`, and concurrent fan-out for `agent_adherence_review` (5-10× faster on large tenants).

### New tool: `voice_call_quality`

Per-conversation MOS (Mean Opinion Score) for voice-call quality triage. MOS is the *"was it the network or the agent?"* signal — calls with min MOS < 3 are nearly always network-impacted (jitter, packet loss, codec) rather than agent-skill issues, so a coaching brief that includes a poor-MOS call should flag the network angle before suggesting agent coaching.

```python
voice_call_quality(
    conversation_ids: list[str],  # up to 100
    low_mos_threshold: float = 3.5,
)
```

Returns per conversation:

```yaml
- conversation_id: "..."
  min_mos: 3.2
  avg_mos: 4.1
  segments_evaluated: 12
  segments_with_low_mos: 2
  quality_label: "fair"   # good ≥4.0, fair 3.0-4.0, poor <3.0
```

Soft-fails on 404 (deleted / privacy-filtered / retention-expired) using the canonical envelope from v1.3. Non-voice conversations return `{conversation_id, no_voice_segments: true}` so batch loops over mixed-media lists don't break.

Endpoint: `GET /api/v2/analytics/conversations/{id}/details`. Needs `analytics:conversationDetail:view`.

### `get_user_presence_now` resolves presence labels

Pre-v1.4: returned `presence_definition_id: <uuid>` only — every caller followed up with another lookup to learn whether the agent was on "Coaching" vs "Training" vs "Pre Break".

v1.4: each row gains `presence_label: "Pre Break"` resolved from the presence definition UUID. Uses a process-lifetime cache of `/api/v2/presence/definitions` — typically one load per MCP server lifetime since presences change rarely.

```python
get_user_presence_now(
    user_ids: list[str],
    include_label: bool = True,  # set False to skip lookup if missing presence:definition:view
)
```

Cache invalidation: never (restart the MCP to refresh). If a tenant adds a presence mid-session and queries it before the cache loads, they'll see `presence_label: None` — accepted trade-off for no-thinking caching.

### `find_user` batch variant

Pre-v1.4: `find_user(query)` resolved one name at a time. TL workflows that need a target + peer set (10-20 names) forced 10-20 serial calls.

v1.4: optional `name_contains_list: list[str]` runs N searches concurrently and groups results per input query.

```yaml
matches:
  - name_query: "Jane Smith"
    candidates: [{id, name, email, title, ...}]
  - name_query: "Bob Jones"
    candidates: [...]
unmatched: ["Typo McGoo"]
total_queries: 3
matched_queries: 2
mode: "batch"
```

Bounded thread pool (max 8 workers). Single-mode (`query: str`) unchanged. Mutex — pass exactly one of the two parameters.

### `agent_adherence_review` concurrent fan-out

The per-user adherence-explanations query was happening sequentially in a loop. On a 30-agent tenant that meant ~30 sequential round-trips. v1.4 fans them out via `ThreadPoolExecutor` (max 8 workers).

**Expected speedup: 5-10× on a 30-agent tenant** (single ~10s pool batch vs 30 sequential).

Behaviour is unchanged otherwise — per-user failures still surface as empty explanations (logged warning); the response shape is byte-identical to v1.3.

### Tool count: 43 → 44

`voice_call_quality` is the only addition.

### Tests

345 → **361 tests** (+16 in [`tests/test_v14.py`](tests/test_v14.py)):

- **`find_user`** (4): single mode unchanged, batch groups per query, both mutex error cases
- **`get_user_presence_now`** (4): label resolved inline, `include_label: false` skips the API call entirely, unknown UUID returns `None`, second call hits the cache without re-loading
- **`voice_call_quality`** (6): good / fair / poor labels at correct boundaries, non-voice convs return `no_voice_segments`, 404 → canonical envelope, custom `low_mos_threshold` shifts the low-segment count
- **`agent_adherence_review`** (2): each user gets exactly one adherence call regardless of pool concurrency, per-user failures surface as empty explanations rather than breaking the tool

### Upgrade

```bash
cd ~/code/genesys-mcp && git pull && uv sync
make test                  # 361 tests should pass
```

No tenant.yaml changes required; no breaking response-shape changes. Existing callers of `find_user` (single mode), `get_user_presence_now` (just gets the extra label field), `agent_adherence_review` (same shape, faster), and every other tool continue to work unchanged.

---

## v1.3.0 — 3 June 2026

**Soft-fail consistency + a missing discovery tool.** Came out of a structured review of all 42 MCP tools after v1.2 shipped. Six concrete fixes, one new tool, one shared envelope helper. No breaking changes for callers reading the canonical response shape.

### New: canonical soft-fail envelope (`src/genesys_mcp/_envelopes.py`)

Pre-v1.3 every tool that soft-failed invented its own envelope shape:

- `speech_analytics`: `{"status": 404, "conversation_id": cid, "message": ...}`
- `lookup_external_contact`: `{"status": 404, "value": v, "type": t, "match": None}`
- `queue_estimated_wait_time`: `{"queue_id": q, "error": str(exc)}` (no `status` key)

v1.3 standardises on `soft_fail_envelope(status, kind, message, **id_fields)`. Every soft-fail across the codebase now produces:

```json
{"status": 404, "kind": "transcript url", "message": "transcript url not found",
 "conversation_id": "..."}
```

Plus a companion `is_soft_fail(result)` predicate composition tools use when iterating per-call enrichment.

### Fixes

**1. `agent_coaching_pack` docstring corrected** — `coaching.py`
Said *"Falls back to in-code defaults (voice 285s / message 660s / ACW 15s) when the config file is absent"* but v1.0 made tenant.yaml mandatory and the code raises. Doc now matches behaviour with a "run genesys-tenant-setup" remediation note.

**2. `queue_estimated_wait_time` proper error handling** — `analytics.py`
Was wrapping every exception in `{"error": str(exc)}`, making auth failures look identical to "no agents skilled" 404s. Now:
- 404 → canonical soft-fail envelope (`status: 404, kind: "estimated wait time", message: "no EWT available (queue inactive or no agents skilled)"`)
- Anything else → propagates with a warning log so debugging stays possible

**3. `get_conversation` soft-fail** — `conversations.py`
Was raising on deleted convs, privacy-filtered convs, and retention-expired records, breaking any batch loop that iterates over conversation lists. Now returns `{status: 404, kind: "conversation", ...}` for 404s; other errors propagate as before.

**4. `lookup_external_contact` envelope migration** — `external_contacts.py`
Was returning bespoke `{status: 404, value, type, match: None}`; now uses the canonical helper while keeping `match: None` as a back-compat id field for existing callers.

### New tool: `list_org_presences`

Closes the v1.0 *"where do I find the pre-break presence id?"* gap. The `genesys-tenant-setup` wizard auto-discovers it; interactive users hitting the MCP fresh needed a way to look it up by label.

```python
list_org_presences(name_contains="Pre Break")
# → {"count": 1, "presences": [{
#       "id": "e3bedde6-...", "system_presence": "BUSY",
#       "label": "Pre Break", "language_labels": {"en": "Pre Break"},
#       "deactivated": false
#   }]}
```

Two args: optional `name_contains` (case-insensitive substring on label) and `deactivated: bool` (include deactivated, default false). Endpoint: `GET /api/v2/presence/definitions`. Needs `presence:definition:view`.

Use cases:
- Tenant setup: *"what's the UUID for our 'Pre Break' presence?"*
- `break_overrun_report` config: pass the returned id as `pre_break_organization_presence_id`
- General audit: see every custom presence the org has defined (Coaching, Training, Project Work, etc.)

### `presence_sessions` gains `pre_break_organization_presence_id`

Mirrors `break_overrun_report`'s behaviour. When set, BUSY sessions carrying the configured `organizationPresenceId` are re-labelled as `PRE_BREAK` (and included even when BUSY isn't in `presence_filter`). Pass `cfg.presence.pre_break_organisation_presence_id` from tenant.yaml; when `None`, pre-break sessions fall under generic BUSY as before.

Closes a sibling-tool consistency gap — both presence tools now use the same pre-break detection logic.

### Tool count: 42 → 43

`list_org_presences` is the only addition.

### Tests

326 → **345 tests** (+19 in [`tests/test_envelopes_v13.py`](tests/test_envelopes_v13.py)):

- Envelope helper: canonical shape, default 404, custom status, arbitrary id fields, key ordering
- `is_soft_fail` predicate: positive, non-dict, status < 400, missing status
- `_soft_404` in speech_analytics adopts the canonical envelope
- `get_conversation` 404 → envelope, 500 → propagates
- `lookup_external_contact` 404 → envelope with `match: None` preserved
- `queue_estimated_wait_time` 404 → per-row envelope, 500 → propagates
- `list_org_presences`: shape + filter behaviour
- `presence_sessions`: parameter signature includes `pre_break_organization_presence_id`

### Upgrade

```bash
cd ~/code/genesys-mcp && git pull && uv sync
make test                  # 345 tests should pass
```

Mostly transparent change. The only callers that need to update are any that read tool-specific soft-fail field names — those still work (the canonical envelope preserves them as id fields), but the `kind` + `message` + `status` keys are now consistent. Worth a one-pass review of any code branching on response shape from `lookup_external_contact` or `queue_estimated_wait_time`.

---

## v1.2.0 — 2 June 2026

**Inline transcripts.** New `get_conversation_transcript` tool returns a structured, time-aligned list of utterances attributed to customer / agent / IVR / ACD with optional per-utterance sentiment. The coaching pack uses it automatically — every flagged call now ships with an inline transcript excerpt so TLs can read what was said without a per-call round-trip.

### New tool: `get_conversation_transcript`

Resolves a conversation id → recording session ids via `recording:recording:view`, then for each session pulls the STA transcript URL and downloads the JSON. Returns a flat list of speaker-attributed utterances with start times and (in `full` mode) per-utterance sentiment.

Parameters:

- **`conversation_id`** — required.
- **`mode: "summary" | "full"`** — default `summary` returns `{speaker, start_s, text}` per utterance; `full` adds `{sentiment, sentiment_label}` for sentiment-progression diagnosis.
- **`max_utterances`** — default 200, range 10–2000. When truncated, the response includes `truncated_at: N` plus `total_utterances_dropped` so callers know what was clipped.

Response shape:

```json
{
  "conversation_id": "...",
  "media_type": "voice",
  "duration_s": 547.2,
  "participants": {"customer": "+614...", "agent": "agent-uuid"},
  "sessions_processed": 1,
  "sessions_no_transcript": 0,
  "total_utterances": 87,
  "truncated_at": null,
  "total_utterances_dropped": 0,
  "utterances": [
    {"speaker": "customer", "start_s": 0.5, "text": "Hi, I'm calling about my bill."},
    {"speaker": "agent", "start_s": 4.2, "text": "Sure, no problem. Let me pull that up."},
    ...
  ]
}
```

Speaker labels are normalised from Genesys's `participantPurpose`: `external` → `customer`, `internal`/`user`/`agent` → `agent`, plus `ivr`, `acd`, `voicemail`, `fax` pass through unchanged.

### `agent_coaching_pack` now embeds excerpts

Two new optional params on the coaching pack:

- **`include_flagged_transcripts: bool = True`** — when on, each flagged call gets a `transcript_excerpt` field attached automatically.
- **`transcript_max_utterances_per_call: int = 40`** — default 40 captures the opening exchange (where most coaching friction surfaces). 40 utterances × 10 flagged calls ≈ 50KB chunk added to a typical coaching pack — chunky but that's the deepest read.

Concurrent fetch under the existing `ThreadPoolExecutor` so wall time stays bounded (~3-5s for 10 flagged calls).

### Why this matters for coaching

Pre-v1.2: a coaching brief showed flagged call IDs + reasons (sentiment dip, hold ratio, AHT excess) but the TL had to leave chat context, fetch the transcript URL separately, parse the JSON, and figure out which speaker was the agent. 30+ round-trips for a typical brief.

Post-v1.2: the brief reads end-to-end — *"Joan's flagged call from 14 May, AHT +349% over target — the customer asked twice about porting; Joan suggested portal reset instead of escalating to the porting queue (utterance at 6:23)."* The transcript is right there. TL spends time on the coaching conversation, not on data wrangling.

### Tests

302 → **326 tests** (+24 in [`tests/test_transcript.py`](tests/test_transcript.py)):

- Speaker normalisation (external→customer, internal→agent, ivr/acd pass-through, unknown→lowercased)
- Sentiment label translation (-1 → negative, 0 → neutral, +1 → positive)
- Utterance flattening (prefers `decoratedText` over raw `text`, attaches sentiment by `phraseIndex`, skips empty phrases)
- Participant summarisation (first-identifier-per-role wins)
- Mode trimming (`summary` strips sentiment, `full` keeps it)
- End-to-end pipeline mocked (recordings → transcript URL → HTTP fetch → utterance list) for happy path, no-recordings 404, truncation, invalid mode
- Token budget: 40-utterance summary excerpt fits under 8KB

### Distribution comparison context

For anyone wondering about the [MakingChatbots/genesys-cloud-mcp-server](https://github.com/MakingChatbots/genesys-cloud-mcp-server) project — their `conversation_transcript` tool was the direct inspiration for this. Their TypeScript implementation handles the same recording-session resolution + URL fetch flow; this Python port adds the v1.1 `mode: "summary" | "full"` pattern, the `max_utterances` truncation contract, and the coaching-pack integration. Both projects are read-only and complementary in scope.

### Upgrade

```bash
cd ~/code/genesys-mcp && git pull && uv sync
make test                  # 326 tests should pass
```

If you don't want transcripts in coaching packs (cheaper runs, less data flowing into chat), pass `include_flagged_transcripts: false` on the `agent_coaching_pack` call.

---

## v1.1.0 — 29 May 2026

**The token-budget release.** Four heavy tools (`queue_performance`, `agent_performance`, `repeat_caller_deep_dive`, `break_overrun_report`) gain a `mode: "summary" | "full"` parameter, defaulting to summary. Routine interactive queries now use roughly 25–40% of the tokens they used in v1.0 with zero loss of signal for any current skill — every dropped field was either a histogram bucket, a percentile, debug scaffolding, or a per-session detail array nothing in the codebase actually reads.

### The contract

Every heavy tool now has a `mode` parameter:

- **`mode: "summary"` (new default)** — slim shape. Each tool defines its own contract for what stays vs gets dropped (see below). Optimised for routine interactive use.
- **`mode: "full"`** — the v1.0 shape with histograms, percentiles, and per-session detail. Use only when you genuinely need them (*"what's the p95 wait time?"*, *"when exactly did Joan go over on Monday?"*).

### Per-tool slim contracts

**`queue_performance`**

- **Keeps** in summary: `tAnswered`, `tHandle`, `tWait`, `tAbandon` with `{count, sum}` each; `nOffered`, `nOverSla`, `nConnected` with `{count}` only; full `derived` block (answered, abandoned, SL, ASA, AHT).
- **Drops** in summary: `nTransferred`, `tTalkComplete`, `tHeldComplete`, `tAcw`, `tShortAbandon`. Plus min/max/current/p50/p75/p90/p95/p99 and full histogram bucket arrays on every metric.

**`agent_performance`**

- **Keeps** in summary: `tAnswered`, `tHandle`, `tTalkComplete`, `tAcw`, `tHeldComplete` with `{count, sum}` each; `nTransferred` with `{count}`; full `summary` array (per-agent + per-media rollups).
- **Drops** in summary: `nOutbound`, `nBlindTransferred`, `nConsultTransferred` plus all percentile/histogram stats.

**`repeat_caller_deep_dive`**

- **Keeps** in summary: full `org_rollup` (already aggregated); per-repeater core fields including `evidence_conversation_ids` (essential drill-down primitives — conversation IDs are tiny and let callers fetch recordings/transcripts).
- **Drops** in summary: debug scaffolding from `scope` (`sta_calls_made`, `wrapup_coverage_pct`, etc.); caps `queues_offered` / `dispositions` / `expected_fixes` / `topics` to top 3 per repeater; collapses per-call `sentiment_trajectory` array to a 4-key summary `{initial, final, trend, samples}`.

**`break_overrun_report`**

- **Keeps** in summary: every per-user aggregate counter — break/meal/pre-break/away counts + totals + averages, plus `pre_break_tracking_available` flag.
- **Drops** in summary: the three per-session detail arrays (`overrun_sessions`, `pre_break_overrun_sessions`, `away_sessions`). Use `presence_sessions(user_id, interval)` for per-session drill-downs, or pass `mode: "full"`.

### Measured impact

Against the live tenant data the four skills consume:

| Tool | Per-bucket fields before → after | Synthesised payload reduction |
|---|---|---|
| `queue_performance` | ~30 per metric × 11 metrics → 1–2 stats × 7 metrics | ~75% smaller with histograms present |
| `agent_performance` | Same shape | Similar |
| `repeat_caller_deep_dive` | Full per-call sentiment + dispositions / queues | ~60% smaller on 10-repeater payloads |
| `break_overrun_report` | Per-session arrays of 5–10 entries per user | ~80% smaller when sessions are present |

The captured test fixtures don't have full histograms baked in (they pre-date v1.1) so the synthesised tests confirm the real-world reduction — see `tests/test_token_budgets.py`.

### Tests

286 → **302 tests** (+16 in [`tests/test_token_budgets.py`](tests/test_token_budgets.py)):

- One test per tool asserting the slim version fits under a tight regression budget (catches a histogram or percentile field creeping back in).
- One test per tool asserting the dropped fields *actually get dropped* when synthesised inputs include them.
- Plus shape-preservation tests: `derived` block stays intact on `queue_performance`, `evidence_conversation_ids` always present on deep-dive repeaters, aggregate counters always present on break-overrun user rows.

### Upgrade

```bash
cd ~/code/genesys-mcp && git pull && uv sync
make test                  # 302 tests should pass
```

Pure additive change for the default user — every existing tool call returns smaller data with the same fields any skill actually reads. If you have a custom workflow that depends on histograms or per-session detail, add `mode="full"` to those specific calls.

---

## v1.0.0 — 29 May 2026

**The tenant-agnostic release.** Every tenant-specific assumption that pre-v1.0 was baked into Python now lives in tenant.yaml, and the skills cleanly degrade when a tenant's operating model differs from the defaults. Combined with the v0.10 correctness floor (shape validators, snapshot tests, untested-skill coverage), v1.0 is the first version a fresh deployer can confidently point at any Genesys Cloud tenant without forking the code.

### Hardcoded values moved to `tenant.yaml`

- **Pre-break presence UUID** removed from `break_overrun_report` default. Pass `pre_break_organization_presence_id` explicitly, or set `cfg.presence.pre_break_organisation_presence_id` in tenant.yaml. When the id is unset and `operating_model.has_pre_break_presence: false`, the tool returns `pre_break_tracking_available: false` and skills render a "tracking disabled" callout instead of zero rows.
- **Coaching heuristic thresholds** — hold ratio (0.15), peer-AHT multiplier (1.15), per-call sentiment / hold / wrap-up cutoffs, QA pass mark (80), excess-hours thresholds (2.0h) — all move to the new `coaching.heuristics` block. Defaults match the pre-v1.0 hardcoded values so existing configs work unchanged; transfer-heavy retention teams can now raise the hold-ratio threshold without forking the code.
- **`specialist_roles`** is now a required tenant.yaml field. Pre-v1.0 it defaulted to `["Specialist", "Customer Service Specialist"]`, which silently filtered out tenants whose role titles differ. The `genesys-tenant-setup` wizard already discovers these from the active user list.
- **AHT / break / meal targets in `coaching.py`** — pre-v1.0 in-code tenant-specific fallbacks dropped. Absent tenant.yaml is a hard fail with a clear "run genesys-tenant-setup" remediation.

### New `operating_model` block

```yaml
operating_model:
  has_pre_break_presence: true        # false → skip pre-break sections
  has_brand_structure: true           # false → collapse brand × channel to channel-only
  expected_channels: [voice, message] # message-only / voice-only tenants get cleaner KPIs
```

Model-level validator catches inconsistent configs at load time — `has_pre_break_presence: true` without the presence id fails loud; `has_brand_structure: false` with > 1 brand fails loud.

### Queue naming pattern fallback

The default `{brand} - {channel} - {function}` pattern fits the pre-v1.0 shape but not every CC. v1.0 adds two knobs to `queues`:

- `name_pattern: null` — no structured naming; every queue is a flat function.
- `name_pattern_match_required: false` — non-matching queues fall back to using the full name as function (instead of being silently dropped from reports).

New module [`src/genesys_mcp/queue_parser.py`](src/genesys_mcp/queue_parser.py) consolidates the parsing + a `compute_pattern_match_rate` helper. The `mcp_health_check` tool samples a page of `/routing/queues` and warns if the configured pattern matches < 80% of real queue names.

### tenant.yaml schema versioning

New top-level `schema_version: "1.0"` field. `load_config()` validates it:

- Missing → loader assumes pre-1.0, applies v1.0 defaults for new fields, logs a deprecation warning.
- Newer than installed code → hard fail with `"upgrade with git pull && uv sync"`.
- Same/older → loads cleanly.

So a tenant.yaml written by a future v1.5 genesys-mcp can't silently misload on a v1.0 install.

### `mcp_health_check` upgrades

Three new v1.0 checks; one new CLI flag:

- **Queue pattern match rate** — samples `/routing/queues` against `queues.name_pattern`. Surfaces a remediation when match rate < 80%.
- **Specialist role resolution** — cross-checks `specialist_roles` against `/users?state=active` titles. Lists a sample of actual titles when no match found.
- **Schema version** — visible in the report under `tenant_config.schema_version`.
- **`--strict`** CLI flag — exits 2 (not 0) on any warning. Use for CI / scripted release-gate validation: `python -m genesys_mcp.health_check --strict`.

### Synthetic "weird tenant" fixtures

[`tests/fixtures/weird_tenants/`](tests/fixtures/weird_tenants/) ships three reference configs:

- `single_brand/` — `has_brand_structure: false`, one brand
- `no_pre_break/` — `has_pre_break_presence: false`
- `message_only/` — `expected_channels: [message]`

Each has a corresponding end-to-end test in [`tests/test_weird_tenants.py`](tests/test_weird_tenants.py) driving the daily-brief renderer to confirm the right "tracking disabled" / "voice-card-omitted" hooks fire. Locks in the tenant-agnostic contract.

### Documentation

- [`docs/tenant-config-schema.md`](docs/tenant-config-schema.md) — schema doc updated for v1.0 (schema_version, operating_model, queue-pattern fallback, coaching.heuristics) + a "v1.0 migration notes" section for pre-1.0 configs.
- README — new **"Will this work on my tenant?"** section near Setup with a per-assumption table and explicit pointers to `mcp_health_check --strict` as the validation gate.

### Tests

- 231 → **286 tests** (+55 in v1.0 alone, 165 → 286 overall since v0.9.2 — a +73% growth across the v0.10 + v1.0 work).
- New test files: `test_coaching_heuristics.py` (14), `test_operating_model.py` (11), `test_queue_parser.py` (12), `test_schema_versioning.py` (5), `test_health_check_upgrades.py` (7), `test_weird_tenants.py` (6).

### Upgrade — existing v0.x tenants

No tenant.yaml changes required. v1.0 defaults match the pre-v1.0 hardcoded values, so:

```bash
cd ~/code/genesys-mcp && git pull && uv sync
make test                  # 286 tests should pass
python -m genesys_mcp.health_check --strict   # green if your config is clean
```

### Upgrade — fresh tenants with a different shape

Run `genesys-tenant-setup` to auto-discover what changed. If your tenant.yaml is hand-written, add `schema_version: "1.0"` and the `operating_model` block per your shape; the rest is backward-compatible. The schema doc has a dedicated v1.0 migration section.

---

## v0.10.0 — 29 May 2026

**Correctness floor**, first half of the road to v1.0. No new features. The goal: make the silent-filter bug class — the same shape the four v0.9.1/v0.9.2 fixes all addressed — structurally hard to reintroduce. After v0.10 a future regression that flips a number trips a test loudly, instead of shipping silently-wrong output to a real day's report.

### One remaining silent-filter site fixed; pattern factored to a shared helper

The audit found one more instance of the v0.9.1 P7D-bucket-overwrite bug, in [`src/genesys_mcp/tools/reports.py`](src/genesys_mcp/tools/reports.py) (`agent_quality_snapshot`'s peer-aggregation loop). Same shape — `agg[uid][media] = stats` instead of accumulating across the ~4 weekly buckets a multi-week interval produces. Peer-comparison columns in the snapshot were truncated to the last week.

Fix consolidates the accumulator into a new shared module [`src/genesys_mcp/_aggregates.py`](src/genesys_mcp/_aggregates.py) — `accumulate_metric_stats()`. Both `coaching.py` (v0.9.1) and `reports.py` (v0.10) now route through it. If a third site ever needs the same pattern, it imports rather than re-implements.

### Genesys response-shape validators

New module [`src/genesys_mcp/shapes.py`](src/genesys_mcp/shapes.py) — five lightweight envelope validators for the response shapes the skills depend on:

- `assert_aggregates_envelope` (with `expect_derived=True/False` to distinguish `queue_performance` from `agent_performance` — the exact distinction the three v0.9.1/v0.9.2 `derived`-block bugs all blew through silently)
- `assert_conversation_detail_list`
- `assert_repeat_caller_deep_dive` — pins the `repeaters` key (the v0.9.2 mis-key bug class)
- `assert_break_overrun_report` — pins all four overrun fields present per user (the v0.9.2 `pre_break_overrun_total_min` ignored-field bug class)

Validators are O(1) envelope checks called once at the top of each consumer (build scripts + aggregator entrypoints). They raise `ShapeError` with a path-style message naming the missing field — no more silently-empty sections.

Wired into all four skills' build scripts: `cc-monthly-report`, `cc-daily-brief`, `mcp-reconcile`. (`cc-coaching-prep` consumes a coaching-pack output, a different shape — gets its own validator in v1.0.)

### Test coverage parity across skills

Previously untested skills now have regression test files:

- [`tests/test_coaching_prep.py`](tests/test_coaching_prep.py) — 26 tests covering formatters, vs-target/vs-peers pill class boundaries, soft-degrade paths (no peers, QA scope unavailable, no sentiment data), narrative-markdown parsing, and end-to-end render.
- [`tests/test_mcp_reconcile.py`](tests/test_mcp_reconcile.py) — 13 tests for each of the four row generators, including the v0.9.1 derived-block bug regression case.
- [`tests/test_shapes.py`](tests/test_shapes.py) — 18 tests pinning every validator behaviour.
- [`tests/test_aggregate_helpers.py`](tests/test_aggregate_helpers.py) — 5 tests pinning `accumulate_metric_stats` directly so both sites are guarded by the same contract.

### Snapshot tests for the four core aggregators

[`tests/test_snapshots.py`](tests/test_snapshots.py) + [`tests/fixtures/_snapshots/`](tests/fixtures/_snapshots/) — golden-JSON pins on the numeric output of `aggregate_queue_performance`, `aggregate_agents`, `aggregate_daily_voice_sl`, and `extract_themes`.

Any one-character change in reduce logic that flips a number now fails the snapshot diff. Intentional updates: re-run [`tests/_generate_snapshots.py`](tests/_generate_snapshots.py), inspect the git diff line-by-line, commit the new snapshots alongside the behaviour change. No `--snapshot-update` flag — drift must be visible in PR review.

### README truthfulness pass

The `queue_performance` / `agent_performance` tool descriptions previously claimed they "match the Genesys UI exactly." Calibrated to: raw metrics (`tAnswered.count`, `tHandle.sum`) match the UI exactly; aggregations (across buckets, per-media split, brand grouping) are MCP logic and should be cross-checked with `mcp-reconcile` before any release.

### Tests

- 165 → **231 tests** (+66). Roughly +40% coverage growth in one release.
- Every test file added or extended cites the bug it pins, so future readers know *why* the assertion exists.

### Upgrade

```bash
cd ~/code/genesys-mcp && git pull && uv sync
make test                  # 231 tests should pass
```

Pure defensive work — no SKILL.md changes, no aggregator behaviour changes (a snapshot regression on upgrade would be a bug, not a deliberate update). If `mcp-reconcile` against your tenant now succeeds where the v0.9.x build silently emitted empty rows, that's the validators catching what the build script previously swallowed.

### What's next — v1.0 (tenant-agnostic + ship polish)

v0.10 closes the correctness floor. **v1.0** pulls every tenant-specific assumption out of code:
- Pre-break presence UUID + coaching heuristic thresholds → `tenant.yaml`
- `operating_model` config block — pre-break-optional, brand-optional, channel-list-aware
- Queue naming pattern fallback for tenants that don't follow `{brand} - {channel} - {function}`
- `tenant.yaml` schema versioning + auto-migration
- `mcp_health_check` upgrades that surface tenant-config gaps with actionable remediation
- Synthetic "weird tenant" fixtures so future refactors can't silently break deployers with different shapes

See the plan at [`~/.claude/plans/i-need-to-build-immutable-pebble.md`](~/.claude/plans/i-need-to-build-immutable-pebble.md) for the full v1.0 spec.

---

## v0.9.2 — 26 May 2026

Four bug fixes in `cc-daily-brief` — every Section 3 / 4 / 5 row was silently filtering out signal that was sitting in the raw data. Found while running the brief for a real day on a live tenant and noticing the "no flagged agents / no callbacks / no adherence issues" callouts couldn't possibly be right.

### Section 3 (Flagged agents) — same `derived`-block bug as v0.9.1

[`skills/cc-daily-brief/build_report.py`](skills/cc-daily-brief/build_report.py) — `flagged_agents` read `data[0].derived.{answered, avg_handle_s}`, but `agent_performance` results don't carry a `derived` block (only `queue_performance` does — and v0.9.1 fixed the equivalent bug in `mcp-reconcile` and `coaching.py`). This file was missed. Result: 5 voice specialists each running 19–60 calls at +66% to +330% over AHT target were all dropped to "no agents flagged."

Fix reads raw `tAnswered.count` + `tHandle.sum/count` and computes AHT directly.

### Section 4 (Repeat-caller callbacks) — wrong dict key

`repeat_caller_hotlist` read `deep.get("unresolved_repeaters")` but `repeat_caller_deep_dive` returns rows under `repeaters`. Always returned `[]`. Fix reads `repeaters` (with `unresolved_repeaters` legacy fallback) and now filters to high-unresolved or explicit-action rows so 23-call repeat patterns don't disappear from the brief.

### Section 5 (Adherence flags) — gated on break/meal, ignored pre-break

`adherence_flags` only checked `total_overrun_min` (break + meal) and never read `pre_break_overrun_total_min`. Agents with a 40-minute parking session on PRE_BREAK but no break/meal overrun (a common real-tenant pattern) slipped through. Fix combines both before applying the 30-min threshold.

### New: org-level adherence summary line

Even with the thresholds working, the 30-min per-agent filter is a "surface the worst offenders" cut. On a real day we saw 215 min of total overrun spread across 18 sessions / 15 agents — most of it under the 30-min per-agent line. The brief now always prints a summary line above Section 5:

> **Total overrun yesterday: 216 min** across 18 sessions / 15 agents (break+meal 23 min, pre-break 193 min). Per-agent threshold for the table below: ≥30 min combined.

So even when no individual agent crosses the threshold the supervisor still sees the accumulated org-level drain.

### Tests

- 154 → 165 (+11 regression tests in [`tests/test_daily_brief.py`](tests/test_daily_brief.py)).
- Coverage: raw-metric reads vs derived-block payloads, pre-break-only agents over threshold, hotlist key fallback, summary-line totals across mixed-overrun shapes.

### Upgrade

```bash
cd ~/code/genesys-mcp && git pull && uv sync
make test                  # 165 tests should pass
```

Pure build-script changes — no SKILL.md or report-shape changes. Re-running `cc-daily-brief` after upgrade produces correct numbers without re-pulling data.

---

## v0.9.1 — 26 May 2026

Two bug fixes caught while running a full report stack against a live tenant over a 24-day window.

### `agent_coaching_pack` was undercounting Section 1 by ~Nx for any multi-bucket interval

[`src/genesys_mcp/tools/coaching.py`](src/genesys_mcp/tools/coaching.py) — `_aggregates_for_users` queries with `granularity="P7D"`. A 24-day interval returns ~4 buckets per (userId, mediaType). The reduction loop was overwriting per-bucket stats instead of accumulating them, so only the last bucket's counts and sums survived — Section 1 of the coaching brief showed roughly 1/N of the true volume.

Empirical: a top-volume specialist over a 24-day window read voice=45, message=125, total handle 35h. After the fix: voice=395, message=1033, total handle 309h — matches the monthly report's headline answered count exactly and lines up with the per-conversation walk total in Section 3.

Fix accumulates `count` and `sum` across buckets; `min` / `max` combine via `min()` / `max()`. Pinned by two new regression tests in [`tests/test_analytics_filters.py`](tests/test_analytics_filters.py) (`TestAggregatesForUsersAccumulation`).

### `mcp-reconcile` was emitting 0 agent rows

[`skills/mcp-reconcile/build_checklist.py`](skills/mcp-reconcile/build_checklist.py) — `_agent_voice_rows` expected a `derived` block on the `agent_performance` result, but that tool only produces raw metric stats (no derived layer; the derived block lives on `queue_performance`). Reading from the wrong shape filtered every row out.

Fix reads `tAnswered.count` and `tHandle.sum` directly from the raw metrics. The reconcile checklist now populates the per-agent voice section as intended.

### Tests

- 152 → 154 tests (+2 for the accumulation regression).
- No fixture refresh required — the fixes are pure reduction-logic changes; aggregator outputs are unchanged where the prior code happened to land on a single bucket.

### Upgrade

```bash
cd ~/code/genesys-mcp && git pull && uv sync
make test                  # 154 tests should pass
```

No SKILL.md or report-shape changes — running cc-coaching-prep or mcp-reconcile after upgrade produces correct numbers without re-capturing fixtures or changing inputs.

---

## v0.9.0 — 25 May 2026

The **visual upgrade + close the v0.5 promise** release. Three deliverables: an intra-day heatmap + per-agent sparklines that answer questions the prior reports couldn't, the long-deferred routing aggregate mode (promised in v0.5, deferred through v0.6, v0.7, v0.8 — finally shipped), and LLM narrative synthesis extended to `cc-daily-brief` + `cc-coaching-prep`. Plus an explicit "what we keep deferring and why" section in the plan so the backlog doesn't grow in silence.

### New visualisation — hour-of-day × day-of-week heatmap

[`skills/cc-monthly-report/build_report.py`](skills/cc-monthly-report/build_report.py) — new `aggregate_hourly_heatmap` aggregator + `render_hourly_heatmap` SVG renderer.

The gap: every report answered *"how did we do, total?"* and *"how did we do, daily?"* but not *"when in the day were we understaffed?"*. CC supervisors ask the intra-day question constantly.

v0.9: a 7×N inline-SVG heatmap (rows = Mon-Sun, columns = hours-of-day, cell colour = voice SL%, cell label = offered volume). Reveals patterns instantly that the daily line chart averages away.

Verified on a 7-day window of live tenant data: surfaces Mon 10am SL 11%, Tue 2pm SL 41%, weekend understaffing — all invisible in the daily SL trend chart. Pure SVG, no JS, print-friendly. Tenant timezone resolved via `zoneinfo.ZoneInfo(cfg.tenant.timezone)` so the bucketing is in local time, not UTC.

Powered by a new `queue_performance.json` pull at `granularity=PT1H` — added to the cc-monthly-report Step 3 parallel batch.

### New visualisation — per-agent voice-AHT sparklines

[`skills/cc-monthly-report/build_report.py`](skills/cc-monthly-report/build_report.py) — new `aggregate_agent_voice_sparklines` + `render_voice_aht_sparkline` (~70×20px inline SVG).

The gap: the workforce table shows headline voice AHT (e.g. "330s") but not *direction of travel*. *"330s and trending down"* and *"330s but actually worsening"* are very different signals; the prior table couldn't tell them apart.

v0.9: a tiny SVG trend line next to each agent's voice AHT cell. Green polyline = improving (final < first), amber = worsening. Dashed horizontal line at the voice-AHT target so the relation-to-target is read at a glance. Gaps in days-without-voice break the line cleanly (no false trend across off days).

Powered by a new `agent_performance.json` pull at `granularity=P1D` — also added to Step 3's parallel batch.

### Long-deferred — `routing_diagnostic_aggregate` mode (the v0.5 promise)

[`src/genesys_mcp/tools/routing.py`](src/genesys_mcp/tools/routing.py) — new tool alongside the existing per-call `routing_diagnostic`.

Promised in v0.5 release notes. Deferred through v0.6 ("re-evaluate post-v0.7"). Deferred through v0.7 ("re-evaluate post-v0.8"). Deferred through v0.8 ("partial overlap with cc-daily-brief"). Four deferrals is one too many — v0.9 ships it.

```
routing_diagnostic_aggregate(
    queue_id="...", interval="2026-05-17/2026-05-24",
    outcome_filter="abandoned", bucket_size="15min",
)
```

Returns: counts per failure mode (`no_eligible_agents` / `all_eligible_busy` / `abandoned_in_ivr`), top-5 worst time-buckets, affected skills, and 10 sample conversation ids for drill-down via the per-call mode.

Verified against a live tenant's general inbound queue over a 7-day window: surfaced 147 abandons, all classified as `no_eligible_agents` — matches v0.7's manual finding that the tenant's issue is staffing levels, not skill routing. Worst hours bunched on Mon 18:00 and Wed 18:00 local time (the evening-rush staffing dip).

**Sharp edge worth knowing about**: `flaggedReason` on customer sessions is empty in many tenants. The classifier uses *absence of an agent-purpose interact segment* as the abandon detector instead — reconciles cleanly against `nOffered - nAnswered` counts from queue_performance.

Pairs with the v0.7 cc-daily-brief "worst routes" section: daily-brief surfaces *which* queues failed; aggregate mode surfaces *why*.

### LLM narrative synthesis — extended to `cc-daily-brief` + `cc-coaching-prep`

v0.7 shipped narrative synthesis for `cc-monthly-report` only. v0.9 extends the pattern, with skill-specific section shapes:

- **`cc-daily-brief`** — 2 sections: *Headline* (1 paragraph) + *Today's priorities* (3 bullets). Daily briefs are meant to be glanced at; the 4-section monthly shape would be overkill. Renders as a single combined "Daily summary" panel at the top of the brief.
- **`cc-coaching-prep`** — 3 sections: *Strengths to acknowledge* + *Areas to coach* + *Suggested talking points*. Renders as a new section 6 ("Coaching narrative") after the Recommended Focus section. **The v0.5-era talking points were chat-only; v0.9 makes them part of the HTML brief** — so they survive between runs and the TL can re-read them before the 1:1 instead of scrolling chat history.

Each skill gets the same `--with-narrative <md-file>` flag pattern as the v0.7 monthly implementation. The `_md_inline` / `_md_section_body_to_html` / `parse_narrative_md` helpers are duplicated across the three skills (~50 lines × 3) rather than extracted to a shared module — three copies of stable code is cheaper than a shared-package abstraction at this scale. Revisit if it grows.

### Step 3 expanded to 7 parallel pulls

[`skills/cc-monthly-report/SKILL.md`](skills/cc-monthly-report/SKILL.md) Step 3 now lists 7 tool calls instead of 5: the existing 5 plus `queue_performance(granularity=PT1H)` and `agent_performance(granularity=P1D)` for the new visualisations. Same strict parallel-batching requirement as v0.8 — single message, multiple tool blocks.

### Tests: 137 → 152

[`tests/`](tests/) gains coverage for the two new aggregators (`aggregate_hourly_heatmap`, `aggregate_agent_voice_sparklines`), the two new renderers (`render_hourly_heatmap`, `render_voice_aht_sparkline`), and the workforce-table sparkline integration. Real-data edge case captured: hourly SL can exceed 100% when calls offered in one bucket answer in the next (Genesys quirk, not our bug — test assertion now allows ≤120%).

### Migration notes

- **Existing tenant configs**: unchanged. No new schema fields.
- **`pyproject.toml`** bumped from 0.8.0 to 0.9.0.
- **Tool count**: 40 → 41 (`routing_diagnostic_aggregate`).
- **Skill count**: 5 (unchanged — the new narrative-synthesis support extends existing skills).
- **`tests/fixtures/`** gained `queue_performance_hourly.json` (2.1 MB) and `agent_performance_daily.json` (147 KB). Re-capture via `tests/_capture_fixtures.py` against a live tenant to refresh.

### What we explicitly removed from the backlog (honest accounting)

| Item | Status |
|---|---|
| **Outbound campaign coverage** | **Removed from active backlog** after 4 deferrals. Re-add if/when an outbound-shop user opens an issue. The MCP author's tenant is inbound-heavy and can't self-smoke-test outbound features; deferring further is more honest than pretending it's next. |
| **CI/CD on GitHub Actions** | Still deferred, but with an explicit trigger now: *"when the first external PR lands"*. Until then, local `make test` is enough. |
| **Mobile / PDF polish** | Quietly dropped unless someone reports it. Real ask hasn't materialised. |

The reverse — what's getting added to the backlog going forward — is **distribution / discoverability**. The MCP doesn't lack features; it lacks visibility. v0.10 should probably be "GitHub Pages site with anonymised sample reports" rather than another feature.

### Known limitations / out-of-scope

- **`routing_diagnostic_aggregate` failure-mode classifier** is heuristic and doesn't yet cross-ref against WFM. `no_eligible_agents` might mean *"nobody scheduled"* or *"everyone scheduled was logged out"*. v0.10 candidate.
- **`affected_skills`** in the aggregate mode requires `activeSkillIds` to be populated on the session — empty for tenants where skill routing isn't the bottleneck.
- **Heatmap timezone** uses `zoneinfo` if the IANA name resolves; falls back to UTC otherwise. Tenants with custom `cfg.tenant.timezone` strings that aren't IANA-resolvable will see UTC-bucketed heatmaps.

---

## v0.8.0 — 25 May 2026

The **confidence in correctness** release. Numbers have been verified by hand-spot-check against the Genesys UI since v0.2; v0.8 finally captures that correctness in an automated test suite so future refactors can't silently break it. Plus a release-time reconciliation skill, clickable conversation deep-links, and a faster monthly-report fetch.

### 137-test pytest suite (`make test`)

[`tests/`](tests/) — first automated tests in the repo. Run via `make test`.

What's tested, in order of leverage:

- **Canonical Genesys-UI filter shapes** ([`tests/test_analytics_filters.py`](tests/test_analytics_filters.py)) — the v0.2 UI-parity fix is now pinned. Monkey-patched SDK tests assert every analytics-backed tool builds its filter as the canonical `and+or+or` shape (not the pre-v0.2 flat OR that silently undercounted by up to 8x). Sentinel verified: a flat-OR body fails `_filter_has_canonical_shape`; the and+or shape passes.
- **Aggregators** ([`tests/test_aggregators.py`](tests/test_aggregators.py)) — golden fixtures captured from a real tenant week (`tests/fixtures/*.json` via `tests/_capture_fixtures.py`). Structural assertions on `aggregate_queue_performance`, `aggregate_agents`, `aggregate_daily_voice_sl`, `compute_performance_leverage`, `extract_themes`, `aggregate_staffing`. Reconciles brand-row totals against per-queue sums to catch double-counting; pins the specialist-role filter; pins 12-column workforce-table count from the v0.2.1 refactor.
- **Helpers** ([`tests/test_helpers.py`](tests/test_helpers.py)) — parameterised tests for `fmt_secs`, `fmt_int`, `fmt_pct`, `bar_class`, `_vs_target_pct`, `_sentiment_label`, `_trend_label`, `_recommend_action`, the `_aht_with_target` / `_acw_with_target` / `_count_and_min_cell` cell helpers. 75 cases total.
- **HTML rendering** ([`tests/test_render.py`](tests/test_render.py)) — BeautifulSoup-based structural assertions (NOT byte-identical snapshots — too brittle on CSS tweaks). Column counts pinned, vs-target pill colour bands pinned to thresholds, daily SL chart bars + 80% target line pinned, narrative-synthesis section structure pinned.
- **Conversation deep-link helper** ([`tests/test_conversation_links.py`](tests/test_conversation_links.py)) — resolution priority, region mapping, fallback rendering.

What's deliberately **not** tested at this layer:

- Raw MCP tool wrappers — they mostly call the SDK 1:1. Testing them mostly tests the SDK. (Exception: filter shapes, which are our logic.)
- End-to-end live-tenant calls — covered by the new `mcp-reconcile` skill (below) instead.
- CI/CD on GitHub Actions — solo project for now. Local `make test` before release is sufficient until contributor count > 1.

Fixture refresh: `python tests/_capture_fixtures.py --interval "..." --queue-name-substring "..."` against a live tenant. Re-run when intentionally adding fields to a tool's output. Captured fixtures are tenant-scoped (queue-name substring filter) and cap users to 10 — small, anonymous-enough to commit.

### New skill — `mcp-reconcile`

[`skills/mcp-reconcile/`](skills/mcp-reconcile/). Trigger: *"reconcile the MCP against the Genesys UI"*, *"validate the numbers for last week"*, *"is the MCP still matching the UI?"*.

Pulls the canonical MCP outputs (`queue_performance`, `agent_performance`, `break_overrun_report`, `qa_evaluations`) for a chosen period and writes a Markdown checklist of side-by-side comparisons:

```markdown
| ✓ | Queue | Media | MCP answered | MCP SL% | MCP avg handle | Notes |
|---|---|---|---:|---:|---:|---|
| ☐ | Brand A - Activation | voice | 1,247 | 82.4% | 5m 30s | |
| ☐ | Brand A - Billing | voice | 891 | 78.1% | 6m 12s | |
```

Each section's intro names the **exact Genesys UI navigation path** to verify the values against. Cover queues × media, agent voice AHT, QA scores, pre-break overruns — the four highest-stakes numbers. Anything else in the MCP outputs derives from these primitives.

**Pairs with `make test`:** tests prove the aggregator maths is stable across releases; reconciliation proves the source numbers still match the UI. The test suite can't detect a silent Genesys SDK endpoint-semantics change; reconciliation can.

Run before each release and after material refactors.

### Clickable conversation deep-links in coaching briefs

[`src/genesys_mcp/conversation_links.py`](src/genesys_mcp/conversation_links.py) (new) + [`skills/cc-coaching-prep/build_report.py`](skills/cc-coaching-prep/build_report.py).

Today's behaviour: every flagged conversation_id in `cc-coaching-prep`'s flagged-calls table renders as a non-clickable truncated `<code>` string (`83461ea6…`). A supervisor reading the brief and wanting to listen to the call has to copy the id, switch to Genesys, paste it in.

v0.8: each conversation_id becomes a clickable link to the Genesys Cloud conversation detail view:

```
https://apps.{region}.pure.cloud/directory/#/analytics/interactions/{conv_id}/admin/details
```

Region resolution priority:

1. `tenant.genesys_app_base_url` in tenant.yaml (explicit override; for custom domains)
2. `GENESYS_REGION` env var, mapped via a hardcoded 18-region table in `conversation_links.py`
3. None → falls back to the v0.7 `<code>` rendering (backwards-compatible)

The cc-monthly-report and cc-daily-brief skills currently display ANIs (phone numbers) in their repeat-caller sections, not conversation ids — deep-links don't apply there. The win is concentrated where it matters: the per-call flagged table in coaching prep.

### Parallel data pulls in `cc-monthly-report` Step 3

[`skills/cc-monthly-report/SKILL.md`](skills/cc-monthly-report/SKILL.md). Step 3's six tool calls (`queue_performance` × 2, `agent_performance`, `break_overrun_report`, `repeat_caller_deep_dive`, `wfm_schedule`) are now explicitly required to go out in a single assistant message with parallel tool-use blocks.

The pre-v0.8 wording said *"in parallel"* but didn't make the strict-batching requirement obvious — Claude could (and sometimes did) issue them sequentially. The v0.8 wording is unambiguous: **single message, multiple tool blocks, no waiting between calls.**

Expected wall time: 30-60s → ~10-15s on a typical month (bounded by the slowest call's `_run_conv_details_job` polling, not by serial accumulation).

### Tenant config — `tenant.genesys_app_base_url`

Optional new field. Default `None` — falls back to region-based resolution. Existing configs keep working unchanged.

### Migration notes

- **Existing tenant configs**: keep working unchanged. The new `tenant.genesys_app_base_url` field is fully optional.
- **`pyproject.toml`** bumped from 0.7.0 to 0.8.0.
- **New dev dependency group**: `[dependency-groups.test]` with `pytest>=8.0` and `beautifulsoup4>=4.12`. Installed via `uv sync --group test` or implicitly by `make test`.
- **Tool count**: 40 (unchanged). **Skill count**: 4 → 5 (`mcp-reconcile`).
- **Fixture data** under `tests/fixtures/` is committed (small, tenant-anonymous after the substring filter). Re-capture with the script when refreshing intentionally.

### Known limitations / out-of-scope

- **CI/CD on GitHub Actions** — deferred until contributor count > 1. Local `make test` works.
- **Full MCP-tool unit coverage** — most tools are SDK wrappers; deliberately skipped. The filter-shape assertions cover the load-bearing logic.
- **Routing diagnostic aggregate mode** — still deferred from v0.5.
- **Outbound campaign coverage** — still deferred.
- **AHT MAPE in `volume_vs_forecast`** can be inflated when the forecast was scoped to a subset of media types but the actuals query is media-agnostic. v0.7 issue; not addressed in v0.8.

---

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
