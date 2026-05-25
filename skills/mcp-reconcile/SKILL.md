---
name: mcp-reconcile
description: "Use when the user wants to verify the MCP's numbers still match the Genesys 'Performance' UI — e.g. 'reconcile the MCP', 'validate the numbers for last week', 'is the MCP still matching the UI', 'check the MCP is correct', 'run a reconciliation', 'verify monthly report against Genesys UI'. Pulls the MCP tool stack for a chosen period (default: last completed week) and writes a Markdown checklist of side-by-side comparisons (MCP value vs Genesys UI navigation path), so a human can click through and tick each row off. Run before each release and after any material refactor — belt-and-braces over the pytest suite."
metadata:
  version: 1.0.0
---

# MCP ↔ Genesys UI Reconciliation

You are producing a **reconciliation checklist** — a Markdown document listing every key number the MCP reports for a chosen period, paired with concrete *"go to this view in Genesys, look for this value, should match exactly"* instructions. The user clicks through the live Genesys UI, ticks off each row, and gets confidence that the MCP is still in parity.

This is the **release-time validation** that pairs with the `make test` pytest suite. Tests prove the aggregator maths is stable; reconciliation proves the source numbers still match the UI. Both are needed — the test suite can't tell you if Genesys changed an endpoint's semantics, and reconciliation can't tell you if a refactor silently broke an aggregator.

## When to run

- Before tagging a release (v0.x.0)
- After any material refactor touching `analytics.py`, `coaching.py`, or the aggregators in `build_report.py`
- When a user reports numbers that "look off" — generate a fresh reconciliation, click through, see where the drift is
- Quarterly even if nothing changed — catches silent Genesys SDK / endpoint changes

## Before starting

1. **`genesys` MCP connected** with `analytics:readonly`, `users:readonly`, `routing:readonly`, `quality:readonly` (the canonical-parity tools).
2. **Tenant config present** at `~/.config/genesys-mcp/tenant.yaml`.
3. **Confirm the period.** Default: last completed week (Mon-Sun in tenant timezone). Accept "last week", "last month", an ISO interval, or a date range. Shorter periods are easier to verify by hand — a week takes ~5 minutes to tick through; a month takes ~30.

## Procedure

### Step 1 — Resolve the interval and tenant config

Use `cfg.tenant.timezone` to convert the period to ISO-8601 UTC (same pattern as `cc-monthly-report` Step 1). Save as `INTERVAL`. Resolve `QUEUE_IDS`, `USER_IDS`, `BU_ID`, `MU_IDS`, `PRE_BREAK_PRESENCE_ID` from `list_queues`, `list_users`, and the tenant config (same pattern as `cc-monthly-report` Step 2).

### Step 2 — Pull the canonical MCP outputs in parallel

Issue these calls in a **single batch** (parallel tool-use blocks):

```
queue_performance(queue_ids=QUEUE_IDS, interval=INTERVAL, granularity="P1M")
agent_performance(user_ids=USER_IDS, interval=INTERVAL, granularity="P1M")
break_overrun_report(user_ids=USER_IDS, interval=INTERVAL,
                     pre_break_organization_presence_id=PRE_BREAK_PRESENCE_ID,
                     pre_break_target_min=cfg.targets.pre_break_min)
qa_evaluations(user_ids=USER_IDS, interval=INTERVAL)
```

Save each result to `/tmp/cc-reconcile-{period-slug}/{tool}.json`. Also save `qmap.json` and `user_roles.json` (same shape as cc-monthly-report).

### Step 3 — Generate the reconciliation checklist

Run `build_checklist.py`:

```bash
OUTPUT_PATH=$(cd ~/code/genesys-mcp && .venv/bin/python -c "
from genesys_mcp.tenant import load_config
import sys
period_slug = sys.argv[1]
cfg = load_config()
print(f'{cfg.reports.output_dir}/reconcile-{period_slug}.md'.replace('~', '$HOME'))
" "{period-slug}")

python ~/code/genesys-mcp/skills/mcp-reconcile/build_checklist.py \
  --period "{period}" \
  --interval "{INTERVAL}" \
  --data-dir /tmp/cc-reconcile-{period-slug} \
  --qmap-json /tmp/cc-reconcile-{period-slug}/qmap.json \
  --user-roles-json /tmp/cc-reconcile-{period-slug}/user_roles.json \
  --output "$OUTPUT_PATH"
```

The script reads each tool's JSON, extracts the key reconciliation numbers (voice + message answered per queue, AHT per agent, QA scores per agent, pre-break overrun per agent), and writes them as a Markdown checklist with concrete Genesys UI navigation hints alongside each value.

### Step 4 — Confirm and brief

Tell the user:

- Output path
- Number of reconciliation rows (queues × media + agents + QA rows)
- The Genesys UI views they'll need: Performance → Queues, Performance → Agents, Quality → Reporting, Workforce → Adherence
- A reminder that any mismatch is the signal to investigate before merging the next release

Don't paste the whole Markdown. Point at the file.

## What gets reconciled

The checklist covers the four highest-stakes numbers in every MCP output:

1. **Voice answered per queue** — from `queue_performance` → `derived.answered` (sourced from `tAnswered.count`). Verify in Genesys UI: **Performance → Queues → filter to period → Voice tab → "Answered" column**. Should be exact match per queue per media.
2. **Agent voice AHT** — from `agent_performance` → per-user voice `avg_handle_s`. Verify in Genesys UI: **Performance → Agents → filter to period → Voice tab → "Avg Handle" column**. Match per agent.
3. **QA scores per agent** — from `qa_evaluations` → `summary.avg_score` and `summary.n_evaluations`. Verify in: **Quality → Reporting → by agent → period range**. Match per agent.
4. **Pre-break overrun per agent** — from `break_overrun_report` → `pre_break_overrun_total_min`. Verify in: **WFM → Adherence → per-agent presence sessions → filter to pre-break presence**. Approximate match (rounding may differ).

Anything else in the MCP outputs is derived from these four primitives — if these match, downstream aggregations (the brand/channel rolls, the workforce table, the performance-leverage section) are correct by construction.

## Configurable behaviour

| Knob | Source | Notes |
|---|---|---|
| Period default | `last completed week` | Override via user input |
| Output dir | `cfg.reports.output_dir` | Same as cc-monthly-report |
| Filename | `reconcile-{period-slug}.md` | Hardcoded — keep these together |

## When NOT to use this skill

- For ongoing monitoring of report quality — that's the `cc-daily-brief` job
- For investigating *why* a number is off — once reconciliation flags a mismatch, you need to dive into the specific tool/aggregator and trace through
- For generating a leadership-facing report — use `cc-monthly-report` instead
