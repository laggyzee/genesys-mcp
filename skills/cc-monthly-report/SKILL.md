---
name: cc-monthly-report
description: "Use when the user asks to generate a Prvidr contact-centre report for a period — e.g. 'do the monthly CC report for May', 'run the contact centre deep dive for last week', 'CC report for April 2026', 'pull a Genesys workforce report for this week'. Produces an HTML report at ~/Documents/Prvidr-CC-{period}.html with funnel, themes, repeat callers, workforce, and recommended actions. Requires the genesys MCP to be connected."
metadata:
  version: 1.0.0
---

# Prvidr Contact-Centre Monthly Report

You are producing the Prvidr Contact-Centre report — a self-contained HTML document the Operations team sends to leadership. The report has a fixed structure (executive summary, coverage caveats, volume & funnel, what worked / went wrong, themes, repeat callers, workforce, recommended actions) and pulls from the `genesys` MCP server.

## Before starting

1. **Confirm `genesys` MCP is connected.** Run `claude mcp list` (or check the available tools) and confirm the `mcp__genesys__*` tools are present. If not, stop and ask the user to start the MCP server.
2. **Confirm the period.** Ask if not given. Accept any of:
   - A month: "April 2026", "May 2026"
   - A week: "this week", "last week"
   - An ISO interval: "2026-05-01T00:00:00.000Z/2026-05-31T23:59:59.000Z"
   - A date range: "1 May to 31 May 2026"
3. **All times are AEST (UTC+10) unless the user specifies otherwise.** The Genesys API takes UTC, so an AEST month-start of 2026-04-01 00:00 = 2026-03-31T14:00:00.000Z UTC. Use UTC+10 year-round for simplicity (close enough; the Sydney DST boundary is rarely on a month edge).

## Inputs to gather

| Input | Default | Notes |
|---|---|---|
| `period` | required | e.g. "April 2026" |
| `output_path` | `~/Documents/Prvidr-CC-{period-slug}.html` | where to save the HTML |
| `max_repeater_anis` | 25 | how deep to enrich repeat callers (more = slower + more API calls) |

Don't pad with optional questions — just confirm the period and start.

## Procedure

### Step 1 — Resolve the interval

Convert the period to ISO-8601 UTC. Examples (AEST = UTC+10):

| Period | Start (UTC) | End (UTC) |
|---|---|---|
| April 2026 | 2026-03-31T14:00:00.000Z | 2026-04-30T14:00:00.000Z |
| May 2026 | 2026-04-30T14:00:00.000Z | 2026-05-31T14:00:00.000Z |
| this week (Mon→now) | last Mon 00:00 AEST → now UTC | |
| last week (Mon→Sun) | Mon 00:00 AEST → next Mon 00:00 AEST | |

For a custom range "1 May to 31 May 2026 AEST", convert each side to UTC. Use Python in a Bash call to compute it precisely if needed:

```python
from datetime import datetime, timedelta, timezone
aest = timezone(timedelta(hours=10))
start_aest = datetime(2026, 5, 1, 0, 0, tzinfo=aest)
end_aest   = datetime(2026, 6, 1, 0, 0, tzinfo=aest)
print(start_aest.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
print(end_aest.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
```

Save the interval as a single ISO string `"<start>/<end>"`.

### Step 2 — Pull queue + agent inventories

Call **`list_queues`** (page_size 200) and **`list_users`** (state=active, page_size 200) in parallel.

Filter:

- **Queues**: customer-facing only. Take the queues whose name matches `{Brand} - {Function}` where Function ∈ {Activation, Billing, Complaints, General, Retention, Sales, Technical Support} and Brand ∈ {Coles, OnePass, Members Mobile, Spriggy}. Skip Holding / Internal / Jira / Outbound Email / Documents / Supervisor and any ZZZ_ test queues.
- **Users**: keep agents whose `title` is "Customer Service Specialist", "Customer Service Team Leader", "Senior Team Leader", or "Customer Service Manager". Skip wallboards, integration-generic, and external (qpcaustralia.com) users — they don't take customer interactions in this tenant. Tag each user's role as one of `Specialist` / `Team Leader` / `Senior TL` / `Manager` in the `user_roles.json` you write — the build script uses this to filter the workforce table to specialists only (TLs and Managers have different productivity expectations and would skew the peer ranking).

Save the filtered ID lists as `QUEUE_IDS` and `USER_IDS`. Build a `QMAP` of `{queueId: (brand, queue_name)}` and a `NAME_ROLE` dict of `{userId: (display_name, role)}`. Pass these to the build script in step 4.

### Step 3 — Pull all the data in parallel

Issue these tool calls **in parallel**:

```
queue_performance(queue_ids=QUEUE_IDS, interval=INTERVAL, granularity="P1M")        # monthly totals
queue_performance(queue_ids=QUEUE_IDS, interval=INTERVAL, granularity="P1D")        # daily SL trend
agent_performance(user_ids=USER_IDS, interval=INTERVAL, granularity="P1M")
break_overrun_report(user_ids=USER_IDS, interval=INTERVAL)
repeat_caller_deep_dive(queue_ids=[], interval=INTERVAL, media_type="voice", min_calls=3, max_anis=25)
wfm_schedule(business_unit_id=BU_ID, management_unit_ids=[MU_ID], user_ids=USER_IDS, interval=INTERVAL)
```

**For the wfm_schedule call** (the new staffing/forecast layer):

- `business_unit_id`: in the Prvidr tenant the Coles+OnePass BU is `a3ad1347-a0e8-44ab-b216-807005b60680`. If the period covers a different brand mix or you're unsure, run `list_management_units` and pick the BU whose MUs serve the queues you're reporting on.
- `management_unit_ids`: in the Prvidr tenant pass `["1c459932-435f-488a-9223-16c00624c36a"]` (Agents_Coles_Catch). Pass an empty list to auto-discover from the user list.
- `user_ids`: the same specialist user-id list. The endpoint returns nothing without it.

If any of these returns "result exceeds maximum allowed tokens" and saves to a file, that's fine — note the file path. The build script reads from disk anyway. Save each tool's text result to:

```
/tmp/cc-report-{period-slug}/queue_performance.json          # P1M result
/tmp/cc-report-{period-slug}/queue_performance_daily.json    # P1D result
/tmp/cc-report-{period-slug}/agent_performance.json
/tmp/cc-report-{period-slug}/break_overrun_report.json
/tmp/cc-report-{period-slug}/repeat_caller_deep_dive.json
/tmp/cc-report-{period-slug}/wfm_schedule.json               # WFM scheduled vs forecast
```

The daily SL file feeds the voice service-level chart in section 2. The wfm_schedule file feeds the demand-vs-capacity table and the synthesised "more staff vs better staff" recommendation in section 6. Either can be skipped if you don't need that section, but the report is much more useful with both.

### Step 4 — Run the build script

```bash
python ~/.agents/skills/cc-monthly-report/build_report.py \
  --period "{period}" \
  --interval "{ISO interval}" \
  --data-dir /tmp/cc-report-{period-slug} \
  --qmap-json /tmp/cc-report-{period-slug}/qmap.json \
  --user-roles-json /tmp/cc-report-{period-slug}/user_roles.json \
  --output ~/Documents/Prvidr-CC-{period-slug}.html
```

The script:

1. Reads each tool's JSON output from the data directory
2. Aggregates queue_performance by brand × media using `derived.answered` (which comes from `tAnswered.count` — matches the Genesys UI)
3. Builds the workforce table from agent_performance + break_overrun_report, **excluding email** (email handle times span days and inflate AHT). Splits AHT into voice / message columns.
4. Extracts org-level themes (top dispositions, AI outcomes, expected fixes) from the deep-dive
5. Writes a single self-contained HTML file (inline CSS, print-friendly)

### Step 5 — Confirm and report

After the script succeeds, post a short confirmation:

- Output path
- Total interactions, headline answer rates per channel
- Top performer
- One or two notable findings (e.g. pre-break overrun total, biggest unresolved repeater)

Don't paste the whole HTML. Just point at the file.

## Caveats to mention if the data warrants it

- **Email is excluded** from agent productivity totals (handle times can span days).
- **Voice AHT and message AHT are separate columns** — don't compare directly across channels.
- **Genesys UI parity**: Answered counts match the Genesys "Performance > Agents" / "Performance > Queues" UI. If the user spot-checks against the UI, expect exact matches per agent per media.
- **Multi-handler convs in the deep-dive**: a conversation handled by two agents counts +1 for each in their per-agent tallies. Org-wide repeater totals will exceed unique-conversation totals by ~10–20%.
- **Pre-break / AWAY tracking** depends on the org-level "Pre Break" presence id. The default `e3bedde6-f747-4dbb-bb76-45684b9180b6` is correct for the Prvidr tenant; on another tenant you'd pass `pre_break_organization_presence_id`.

## When NOT to use this skill

- For a single ad-hoc question ("how many calls did Anthony take last week?") — just call the relevant MCP tool directly.
- For non-Prvidr tenants — the brand mapping and presence id are tenant-specific.
- If the user wants different metrics or layout — discuss the change first; don't silently customise the HTML in a way that diverges from the standard report.
