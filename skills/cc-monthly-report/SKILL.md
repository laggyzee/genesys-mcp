---
name: cc-monthly-report
description: "Use when the user asks to generate a contact-centre report for a period — e.g. 'do the monthly CC report for May', 'run the contact centre deep dive for last week', 'CC report for April 2026', 'pull a Genesys workforce report for this week'. Produces an HTML report with funnel, themes, repeat callers, workforce, and recommended actions. Reads tenant-specific knobs (brands, AHT targets, WFM unit, pre-break presence, output filename) from ~/.config/genesys-mcp/tenant.yaml. Requires the genesys MCP to be connected; run the genesys-tenant-setup skill first if the tenant config doesn't exist yet."
metadata:
  version: 2.0.0
---

# Contact-Centre Monthly Report

You are producing a Contact-Centre report — a self-contained HTML document the Operations team sends to leadership. The report has a fixed structure (executive summary, coverage caveats, volume & funnel, what worked / went wrong, themes, repeat callers, workforce, recommended actions) and pulls from the `genesys` MCP server.

**This skill is tenant-agnostic.** All tenant-specific knobs (brand list, queue naming pattern, WFM management unit, AHT targets, pre-break presence, output filename pattern) come from `~/.config/genesys-mcp/tenant.yaml`. No brand names, organisation names, or tenant UUIDs are hard-coded in this file or in `build_report.py` — it works for any Genesys Cloud tenant once a tenant config exists.

## Before starting

1. **Confirm `genesys` MCP is connected.** Run `claude mcp list` (or check the available tools) and confirm the `mcp__genesys__*` tools are present. If not, stop and ask the user to start the MCP server.

2. **Confirm a tenant config exists** at `~/.config/genesys-mcp/tenant.yaml` (or the path in `$GENESYS_MCP_CONFIG`). Quick check via Bash:

   ```bash
   test -f "${GENESYS_MCP_CONFIG:-$HOME/.config/genesys-mcp/tenant.yaml}" && echo "exists" || echo "missing"
   ```

   If missing, **stop and tell the user to run the `genesys-tenant-setup` skill first** — that's the guided onboarding that auto-discovers most values from their tenant and produces the config file. (Or they can copy `skills/cc-monthly-report/tenant.example.yaml` into place and edit by hand.)

3. **Read the tenant config** so you have the values you'll need (brand list, queue pattern, skip list, WFM unit ids, output filename pattern). One Bash + Python call is enough:

   ```bash
   cd ~/code/genesys-mcp && .venv/bin/python -c "
   from genesys_mcp.tenant import load_config
   import json
   cfg = load_config()
   print(json.dumps({
       'tenant_name': cfg.tenant.name,
       'short_name': cfg.tenant.short_name,
       'brands': cfg.brands.names,
       'queue_pattern': cfg.queues.name_pattern,
       'queue_channels': cfg.queues.channels,
       'queue_functions': cfg.queues.functions,
       'queue_skip': cfg.queues.skip_substrings,
       'specialist_roles': cfg.specialist_roles,
       'mu_ids': cfg.management_units.ids,
       'bu_id': cfg.business_unit.id,
       'pre_break_presence_id': cfg.presence.pre_break_organisation_presence_id,
       'voice_aht_s': cfg.targets.voice_aht_s,
       'message_aht_s': cfg.targets.message_aht_s,
       'pre_break_min': cfg.targets.pre_break_min,
   }, indent=2))
   "
   ```

   Hold the parsed values in mind for the rest of the workflow.

4. **Confirm the period.** Ask if not given. Accept any of:
   - A month: "April 2026", "May 2026"
   - A week: "this week", "last week"
   - An ISO interval: "2026-05-01T00:00:00.000Z/2026-05-31T23:59:59.000Z"
   - A date range: "1 May to 31 May 2026"

5. **Period strings are interpreted in `cfg.tenant.timezone`** (which the wizard auto-detected from the org's default country code; defaults to `UTC` if the field is missing). The Genesys API takes UTC, so a local month-start needs converting via Python's `zoneinfo`. See Step 1 for the recipe. If the user specifies an explicit ISO interval with a `Z` suffix, take it as-is.

## Inputs to gather

| Input | Default | Notes |
|---|---|---|
| `period` | required | e.g. "April 2026" |
| `output_path` | derived from config: `cfg.report_output_path(period_slug)` resolves to `<reports.output_dir>/<reports.filename_pattern>` | usually `~/Documents/<short_name>-CC-<period>.html` |
| `max_repeater_anis` | 25 | how deep to enrich repeat callers (more = slower + more API calls) |

Don't pad with optional questions — just confirm the period and start.

## Procedure

### Step 1 — Resolve the interval

Convert the period to ISO-8601 UTC using the tenant's timezone from `cfg.tenant.timezone`. Use Python's `zoneinfo`:

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from genesys_mcp.tenant import load_config

cfg = load_config()
tz = ZoneInfo(cfg.tenant.timezone)  # e.g. 'Australia/Sydney', 'America/New_York', 'UTC'

# Month example: "April 2026"
start_local = datetime(2026, 4, 1, 0, 0, tzinfo=tz)
end_local   = datetime(2026, 5, 1, 0, 0, tzinfo=tz)

start_iso = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z").replace("00Z", "00.000Z")
end_iso   = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z").replace("00Z", "00.000Z")
print(f"{start_iso}/{end_iso}")
```

For "this week" / "last week" use the tenant-local week boundary (Mon→Mon) then convert. For an explicit ISO interval the user provided, take it verbatim — they've already picked their convention.

Save the interval as a single ISO string `"<start>/<end>"`.

### Step 2 — Pull queue + agent inventories

Call **`list_queues`** (page_size 200) and **`list_users`** (state=active, page_size 200) in parallel.

Filter using values from the tenant config:

- **Queues**: customer-facing only.
  - For each queue, parse its `name` against `cfg.queues.name_pattern` (e.g. `"{brand} - {channel} - {function}"`).
  - Keep the queue if the parsed `brand` matches one in `cfg.brands.names`, the `channel` is in `cfg.queues.channels`, and the `function` is in `cfg.queues.functions`.
  - Drop queues whose name contains any substring in `cfg.queues.skip_substrings`.

- **Users**: keep agents whose `title` (or role) appears in `cfg.specialist_roles` plus any team-leader / manager titles you want shown alongside specialists. Tag each user's role for the `user_roles.json` you write — the build script uses this to filter the workforce table to specialists only by default.

Save the filtered ID lists as `QUEUE_IDS` and `USER_IDS`. Build a `QMAP` of `{queueId: [brand, queue_name]}` and a `NAME_ROLE` dict of `{userId: [display_name, role]}`. Pass these to the build script in step 4.

### Step 3 — Pull all the data in parallel

Issue these tool calls **in parallel**:

```
queue_performance(queue_ids=QUEUE_IDS, interval=INTERVAL, granularity="P1M")        # monthly totals
queue_performance(queue_ids=QUEUE_IDS, interval=INTERVAL, granularity="P1D")        # daily SL trend
agent_performance(user_ids=USER_IDS, interval=INTERVAL, granularity="P1M")
break_overrun_report(user_ids=USER_IDS, interval=INTERVAL,
                     pre_break_organization_presence_id=PRE_BREAK_PRESENCE_ID,
                     pre_break_target_min=PRE_BREAK_MIN)
repeat_caller_deep_dive(queue_ids=[], interval=INTERVAL, media_type="voice", min_calls=3, max_anis=25)
wfm_schedule(business_unit_id=BU_ID, management_unit_ids=MU_IDS, user_ids=USER_IDS, interval=INTERVAL)
```

Where `PRE_BREAK_PRESENCE_ID`, `PRE_BREAK_MIN`, `BU_ID`, and `MU_IDS` come from the tenant config you read in step 0:

- `PRE_BREAK_PRESENCE_ID` = `cfg.presence.pre_break_organisation_presence_id` (omit the parameter if `None` — `break_overrun_report` falls back to its own defaults)
- `PRE_BREAK_MIN` = `cfg.targets.pre_break_min`
- `BU_ID` = `cfg.business_unit.id` (or run `list_management_units` to discover it if `None`)
- `MU_IDS` = `cfg.management_units.ids` (pass empty list to auto-discover from the user list)

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

The build script auto-loads the tenant config and rebinds its targets/specialist roles before any aggregator runs. Resolve the output path from the config (it honours `cfg.reports.output_dir` and `cfg.reports.filename_pattern`):

```bash
OUTPUT_PATH=$(cd ~/code/genesys-mcp && .venv/bin/python -c "
from genesys_mcp.tenant import load_config
print(load_config().report_output_path('{period-slug}'))
")

python ~/code/genesys-mcp/skills/cc-monthly-report/build_report.py \
  --period "{period}" \
  --interval "{ISO interval}" \
  --data-dir /tmp/cc-report-{period-slug} \
  --qmap-json /tmp/cc-report-{period-slug}/qmap.json \
  --user-roles-json /tmp/cc-report-{period-slug}/user_roles.json \
  --output "$OUTPUT_PATH"
```

(If the user has installed the skill via the symlink convention at `~/.claude/skills/cc-monthly-report/`, that path also works for `build_report.py` — pick whichever is on disk.)

The script:

1. Loads the tenant config (`--tenant-config` overrides; default is `~/.config/genesys-mcp/tenant.yaml`)
2. Reads each tool's JSON output from the data directory
3. Aggregates queue_performance by brand × media using `derived.answered` (which comes from `tAnswered.count` — matches the Genesys UI)
4. Builds the workforce table from agent_performance + break_overrun_report, **excluding email** (email handle times span days and inflate AHT). Splits AHT into voice / message columns.
5. Extracts org-level themes (top dispositions, AI outcomes, expected fixes) from the deep-dive
6. Writes a single self-contained HTML file (inline CSS, print-friendly) at the resolved output path

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
- **Pre-break / AWAY tracking** depends on the org-level "Pre Break" presence id (`cfg.presence.pre_break_organisation_presence_id` in the tenant config). If your tenant doesn't have one, the break_overrun_report falls back to its own heuristic, but the pre-break overrun callout in the report becomes less reliable.

## When NOT to use this skill

- **No tenant config yet.** Run the `genesys-tenant-setup` skill first — it auto-discovers most values from the read-only MCP and asks for the rest, then writes `~/.config/genesys-mcp/tenant.yaml`.
- **Single ad-hoc question** ("how many calls did Anthony take last week?") — just call the relevant MCP tool directly.
- **Different metrics or layout requested** — discuss the change first; don't silently customise the HTML in a way that diverges from the standard report.
- **Tenant doesn't follow the configured queue-naming pattern** — if `cfg.queues.name_pattern` doesn't actually match the queue names you see, edit the config (or rerun `genesys-tenant-setup`) before running the report.
