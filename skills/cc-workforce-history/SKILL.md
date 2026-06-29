---
name: cc-workforce-history
description: "Use when the user asks for a contact-centre workforce history, headcount trend, joiners and leavers, tenure trend, or 'who was on the floor in [past quarter]' — e.g. 'workforce history report', 'headcount per quarter for the last 3 years', 'who joined and left each quarter', 'average tenure trend', 'first and last active date per agent'. Produces an HTML report with three sections: (1) active-agent headcount + joiners + leavers per quarter, (2) average and median tenure trend per quarter, (3) per-person first/last active table flagging joiners and leavers. Backed by the v1.12 user_activity_history tool which reconstructs each user's first/last handled-interaction date from Genesys analytics across active + inactive + deleted users. Reads tenant timezone + output knobs from ~/.config/genesys-mcp/tenant.yaml. Requires the genesys MCP to be connected."
metadata:
  version: 1.0.0
---

# Contact-Centre Workforce History

You are producing a **workforce history** report — a multi-year retrospective of who handled interactions, when they joined, when they left, and how tenure has trended. Scope is dominated by long historical windows (typically the last 3 years) rather than the day/month windows the other CC skills cover.

**Tenant-agnostic.** Timezone for bucket boundaries comes from `cfg.tenant.timezone`. Output filename pattern uses `cfg.reports.filename_pattern` with a workforce-specific slug.

## Before starting

1. **Confirm `genesys` MCP is connected** — `mcp__genesys__user_activity_history` available.
2. **Confirm tenant.yaml exists**; if not, run `genesys-tenant-setup`.
3. **Read the tenant config**:

   ```bash
   cd ~/code/genesys-mcp && .venv/bin/python -c "
   from genesys_mcp.tenant import load_config
   import json
   cfg = load_config()
   print(json.dumps({
       'tenant_name': cfg.tenant.name,
       'short_name': cfg.tenant.short_name,
       'timezone': cfg.tenant.timezone,
   }, indent=2))
   "
   ```

4. **Confirm the period.** Default = last 3 years ending at local-midnight today. Accept overrides like:
   - "since Jul 2023" / "from Q3 2023 to Q2 2026"
   - An explicit ISO interval (`2023-07-01T00:00:00.000Z/2026-07-01T00:00:00.000Z`)
5. **Confirm the bucket size.** Default = `quarter`. Accept `month` for finer granularity.

## Procedure

### Step 1 — Resolve the interval

Convert the period to a UTC ISO interval. The build script's defaults (3-year window ending today, Australia/Sydney timezone) are reasonable for Prvidr-shaped tenants. For custom windows:

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from genesys_mcp.tenant import load_config

cfg = load_config()
tz = ZoneInfo(cfg.tenant.timezone)

# E.g. "Jul 2023 → Jun 2026":
start_local = datetime(2023, 7, 1, 0, 0, tzinfo=tz)
end_local   = datetime(2026, 7, 1, 0, 0, tzinfo=tz)

start_iso = start_local.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S.000Z')
end_iso   = end_local.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S.000Z')
interval = f"{start_iso}/{end_iso}"
```

Save the interval + a `period_slug` (e.g. `2023-07-to-2026-06`).

### Step 2 — Pull workforce data (one call)

Single tool call — `user_activity_history` does the work:

```
mcp__genesys__user_activity_history(
    interval="<resolved interval>",
    bucket="quarter",                 # or "month"
    tz_name=cfg.tenant.timezone,      # default Australia/Sydney
    include_inactive=True,
    include_deleted=True,
)
```

Save the JSON payload to `/tmp/cc-workforce-history-{period-slug}/result.json`. Don't paginate, don't loop — the tool handles all chunking + concurrent fetches internally.

**Retention note:** Genesys analytics retention is typically ~13 months for most regions. The tool surfaces `data_starts_at` (the earliest YYYY-MM with any activity). If the user asked for "Jul 2023 → now" and `data_starts_at` lands at "2025-06" or later, the older quarters in the report show zero headcount because that data isn't retrievable, not because the CC was empty. The build script renders a callout noting this.

**Soft-fail handling (v1.12.1+):** if the tool returns a canonical soft-fail envelope (`status >= 400`, e.g. `{"status": 403, "kind": "user_activity_history", "message": "... grant '...' ..."}`), save it to the file as-is. The build script renders a visible "⚠️ data not retrieved" callout automatically. Do NOT write narrative paragraphs explaining the gap in chat.

### Step 3 — Run the build script

```bash
python ~/code/genesys-mcp/skills/cc-workforce-history/build_report.py \
  --data /tmp/cc-workforce-history-{period-slug}/result.json \
  --period "{human period label}" \
  --period-slug "{period-slug}" \
  --output ~/Documents/{tenant-short-name}-workforce-history-{period-slug}.html
```

The script reads the JSON and renders a single-file HTML report.

### Step 4 — Confirm + brief

After the script succeeds, post a short summary in chat:

- Output path
- Headcount in the most recent quarter
- Number of joiners + leavers across the window
- Average tenure trend direction (growing / shrinking / flat)
- `data_starts_at` if it's later than the requested window start

Keep it tight — one paragraph plus a 3-4 bullet list.

## What the HTML contains

Three sections:

1. **Headcount by quarter** — table + small bar visual. Columns: quarter, active agents, joiners, leavers (joiners green pill, leavers red pill).
2. **Tenure trend** — table of mean + median tenure (months) per quarter with sample size (`n`).
3. **Per-person first/last active table** — every user in scope. Columns: name, state, first active date, last active date, total handled, is_joiner_in_window, is_leaver_in_window. Sorted by total_handled desc.

Plus a data-coverage callout at the top showing `data_starts_at` when it's later than the requested interval start.

## When NOT to use this skill

- For the current month's CC performance, use `cc-monthly-report`.
- For "yesterday's brief", use `cc-daily-brief`.
- For a single agent's coaching prep, use `cc-coaching-prep`.
- If the user wants raw data for Excel pivoting, call `user_activity_history` directly — the JSON has everything.

## Configurable behaviour

| Knob | Source | Notes |
|---|---|---|
| Timezone | `cfg.tenant.timezone` | Defaults Australia/Sydney; passed straight through to the tool |
| Output filename | `cfg.reports.filename_pattern` | Period slug substitutes for `{period}` |
| Default window | hardcoded to last 3 years | Override via the CLI / chat |
