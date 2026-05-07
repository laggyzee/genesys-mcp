# Release Notes

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
Anthony Kha voice 97 / msg 801 in our test tenant for April matched the UI exactly).

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
