# Release Notes

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
