---
name: cc-daily-brief
description: "Use when the user asks for a contact-centre daily brief, morning brief, daily standup data, or 'what happened yesterday' — e.g. 'daily brief', 'morning brief for yesterday', 'what happened in the CC yesterday', 'how did we go yesterday', 'daily CC report'. Produces a one-page HTML brief covering yesterday's KPIs vs the rolling 7-day median, top flagged agents, worst routes (queues with SL drops), repeat-caller hotlist, and adherence flags. Reads tenant-specific knobs (flag thresholds, comparison window, output filename) from ~/.config/genesys-mcp/tenant.yaml. Requires the genesys MCP to be connected; run the genesys-tenant-setup skill first if the tenant config doesn't exist yet."
metadata:
  version: 1.0.0
---

# Contact-Centre Daily Brief

You are producing a **daily** contact-centre brief — a one-page HTML document a supervisor reads at start-of-day to know yesterday's headline numbers, where issues are, and what to act on today. Tighter scope than `cc-monthly-report` (which is leadership-facing and covers a month).

**Tenant-agnostic.** All thresholds (sentiment dip, AHT excess, SL drop) come from `cfg.daily_brief.flag_thresholds`. Comparison window is `cfg.daily_brief.comparison_window_days` (default 7).

## Before starting

1. **Confirm `genesys` MCP is connected** — `mcp__genesys__*` tools available.
2. **Confirm tenant.yaml exists** at the resolved config path; if missing, stop and tell the user to run `genesys-tenant-setup` first.
3. **Read the tenant config knobs you'll need:**

   ```bash
   cd ~/code/genesys-mcp && .venv/bin/python -c "
   from genesys_mcp.tenant import load_config
   import json
   cfg = load_config()
   print(json.dumps({
       'tenant_name': cfg.tenant.name,
       'timezone': cfg.tenant.timezone,
       'brands': cfg.brands.names,
       'mu_ids': cfg.management_units.ids,
       'bu_id': cfg.business_unit.id,
       'specialist_roles': cfg.specialist_roles,
       'targets': {'voice_aht_s': cfg.targets.voice_aht_s, 'message_aht_s': cfg.targets.message_aht_s},
       'daily_brief': {
           'comparison_window_days': cfg.daily_brief.comparison_window_days,
           'thresholds': cfg.daily_brief.flag_thresholds.model_dump(),
       },
   }, indent=2))
   "
   ```

4. **Confirm the target day.** Default is "yesterday" in `cfg.tenant.timezone`. Accept: "yesterday", "today", "last Monday", "2026-05-20". If the user just says *"daily brief"* with no day, default to yesterday.

## Procedure

### Step 1 — Resolve the target day's interval + the comparison window's interval

Convert the target day to `[start_local 00:00, end_local 24:00)` in `cfg.tenant.timezone`, then to UTC for the Genesys API. Use Python's `zoneinfo`:

```python
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from genesys_mcp.tenant import load_config

cfg = load_config()
tz = ZoneInfo(cfg.tenant.timezone)

# target day
day_local_start = datetime(2026, 5, 20, 0, 0, tzinfo=tz)  # parsed from user input
day_local_end   = day_local_start + timedelta(days=1)
day_interval = f"{day_local_start.astimezone(timezone.utc).isoformat().replace('+00:00','.000Z')}/{day_local_end.astimezone(timezone.utc).isoformat().replace('+00:00','.000Z')}"

# rolling-N-day comparison interval (excluding the target day itself)
window = cfg.daily_brief.comparison_window_days
window_end_local = day_local_start
window_start_local = window_end_local - timedelta(days=window)
window_interval = f"{window_start_local.astimezone(timezone.utc).isoformat().replace('+00:00','.000Z')}/{window_end_local.astimezone(timezone.utc).isoformat().replace('+00:00','.000Z')}"
```

Save both intervals plus a `date_slug` (`YYYY-MM-DD`) for the output filename.

### Step 2 — Pull data in parallel

Make all of these calls in parallel — they're independent. Save each result's JSON payload to `/tmp/cc-daily-brief-{date-slug}/`:

| File | Tool call | Notes |
|---|---|---|
| `queue_perf_day.json` | `mcp__genesys__queue_performance(interval=<day>, queue_ids=[])` | yesterday's voice + message SL by queue |
| `queue_perf_window.json` | `mcp__genesys__queue_performance(interval=<window>, queue_ids=[])` | rolling-N-day baseline |
| `agent_perf_day.json` | `mcp__genesys__agent_performance(interval=<day>, user_ids=[])` | yesterday's per-agent AHT/answered |
| `repeat_callers.json` | `mcp__genesys__repeat_caller_deep_dive(queue_ids=[], interval=<day>, top_n=10)` | unresolved-from-yesterday hotlist |
| `break_overrun.json` | `mcp__genesys__break_overrun_report(interval=<day>, user_ids=[])` | break/pre-break overruns |
| `nps.json` *(v1.11, optional)* | `mcp__genesys__search_conversations_by_attribute(attribute_key=cfg.survey.nps_attribute_key, interval=<day>)` | **Only call when `cfg.survey.nps_attribute_key` is set in tenant.yaml.** Powers the NPS card. Omit the file entirely (don't write `null`) when the tenant hasn't opted in — the build script gates on file presence. |
| `wrap_up_distribution.json` *(v1.11)* | `mcp__genesys__wrap_up_code_distribution(interval=<day>, include_trend=True, top_n=5)` | Top wrap-up codes + largest mover (vs immediately prior day). Section auto-omits if zero conversations. |

The skill's pattern matches `cc-monthly-report` Step 3: parallel tool calls, save raw JSON, then a single Python build script does aggregation + HTML rendering.

**Soft-fail handling (v1.12.1):** If `wrap_up_code_distribution` (or any other v1.11 sidecar tool) returns a canonical soft-fail envelope (`status >= 400`), save it to the file as-is. The build script renders a visible "⚠️ data not retrieved" callout automatically — do NOT write narrative paragraphs explaining the gap in chat.

### Step 3 — Run the build script

Resolve the output path from the tenant config and run `build_report.py`:

```bash
OUTPUT_PATH=$(cd ~/code/genesys-mcp && .venv/bin/python -c "
from genesys_mcp.tenant import load_config
print(load_config().daily_brief_output_path('{date-slug}'))
")

python ~/code/genesys-mcp/skills/cc-daily-brief/build_report.py \
  --target-date "{date-slug}" \
  --day-interval "{day-interval}" \
  --window-interval "{window-interval}" \
  --data-dir /tmp/cc-daily-brief-{date-slug} \
  --output "$OUTPUT_PATH"
```

The script loads tenant.yaml, reads each JSON file from the data directory, computes yesterday-vs-rolling-median deltas per queue, surfaces flagged agents per `flag_thresholds`, picks worst routes by SL drop, builds the repeat-caller hotlist, and writes the HTML.

### Step 4 — Synthesise narrative sections (v0.9+)

Open the freshly-generated HTML and skim the data sections — headline KPIs, flagged routes, flagged agents, callbacks, adherence. Use those numbers (not your prior assumptions) to draft 2 short narrative sections that go at the top of the brief for the supervisor's morning glance:

```markdown
## Headline

One paragraph (≤ 80 words). What's the headline of yesterday? Use the **rolling median** as the comparison anchor — *"Voice SL 65% (rolling 78%), driven by a Tuesday 10am drop where 3 of 4 eligible specialists were on extended interactions"*. Name the **one or two things that explain most of the variance**. Don't list every flag — that's what the data sections below are for.

## Today's priorities

Top 3 actions for **today**, not yesterday. Sorted by impact-per-effort. Each one bullet:

- **Action — owner / effort** — *brief evidence from the data*
- **Action — owner / effort** — *brief evidence*
- **Action — owner / effort** — *brief evidence*
```

Save to `/tmp/cc-daily-brief-{date-slug}/narrative.md`, then re-run the build script with the `--with-narrative` flag pointing at it:

```bash
python ~/code/genesys-mcp/skills/cc-daily-brief/build_report.py \
  --target-date "{date-slug}" \
  --day-interval "{day-interval}" \
  --window-interval "{window-interval}" \
  --data-dir /tmp/cc-daily-brief-{date-slug} \
  --output "$OUTPUT_PATH" \
  --with-narrative /tmp/cc-daily-brief-{date-slug}/narrative.md
```

The build script parses by `## Heading`, runs each section's body through a minimal markdown subset (paragraphs, **bold**, *italic*, `code`, [links], `- bullets`), and slots the combined narrative as a single "Daily summary" section at the top of the brief, above the data sections.

If the user explicitly says *"skip the narrative"* or yesterday's data is genuinely unremarkable (everything within the rolling median bands), omit the `--with-narrative` flag and ship the data-only brief — that's the v0.7-era behaviour and it's still a valid output.

### Step 5 — Confirm + brief

After the script succeeds, post a short summary in chat (don't paste HTML):

- Output path
- Yesterday's headline: voice SL %, total interactions, vs the rolling median
- The top-3 flagged agents (one-liner each, with the flag reason)
- 1-2 worst routes if any (queue + SL drop)
- Any unresolved repeaters that should get callbacks today

Keep it tight — one paragraph plus a 3-5-item bullet list. The HTML is the deliverable; the chat summary is the "what should I do first today" hook.

## What the HTML contains

Single-page, ~700px wide, designed to fit a laptop screen or a Slack share without scrolling much. Sections:

1. **Headline KPIs** — voice SL, message SL, total interactions, vs the rolling-N-day median (colour-coded `.vs-target` pills)
2. **Worst routes** — top 3-5 queues by SL drop vs their rolling median, with current eligible-agent counts where helpful
3. **Flagged agents** — top 3-5 agents by composite flag score (AHT excess + sentiment dip + adherence)
4. **Repeat-caller hotlist** — unresolved-from-yesterday repeaters who should get a callback today
5. **Adherence flags** — agents with break/pre-break overruns > 30 min

Same visual idiom as `cc-monthly-report` (CSS reused) — colour-coded vs-target pills, KPI cards, no JavaScript. Print-friendly.

## When NOT to use this skill

- If the user wants a multi-day or week-level view, use `cc-monthly-report` or wait for explicit weekly variants
- If the user wants a deep dive on one agent, use `cc-coaching-prep`
- If the user wants the data raw (e.g. for Excel pivot), call the underlying MCP tools directly

## Configurable behaviour

| Knob | Source | Notes |
|---|---|---|
| Sentiment dip threshold | `cfg.daily_brief.flag_thresholds.sentiment_dip` | default 0.4 |
| AHT-excess threshold | `cfg.daily_brief.flag_thresholds.aht_excess_pct` | default 15% over voice AHT target |
| SL-drop threshold | `cfg.daily_brief.flag_thresholds.sl_drop_pp` | default 10 percentage points |
| Comparison window | `cfg.daily_brief.comparison_window_days` | default 7 days |
| Output filename | `cfg.daily_brief.output_filename_pattern` | default `daily-brief-{date}.html` |
