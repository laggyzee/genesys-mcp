---
name: cc-coaching-prep
description: "Use when the user asks to prepare a coaching session, 1:1, or performance review for a contact-centre agent — e.g. 'prep coaching for Anthony for the last 4 weeks', 'do a coaching brief on Amelia', 'run a 1:1 prep for Agent X', 'coaching report for Jane'. Produces a self-contained HTML brief covering performance vs targets, peer comparison, QA scores, sentiment, adherence, wrap-up discipline, top flagged calls, and a recommended top-3 coaching focus. Reads tenant-specific knobs (AHT targets, specialist roles, flagged-call thresholds, output filename) from ~/.config/genesys-mcp/tenant.yaml. Requires the genesys MCP to be connected; run the genesys-tenant-setup skill first if the tenant config doesn't exist yet."
metadata:
  version: 1.0.0
---

# Contact-Centre Coaching Prep

You are producing a 1:1 coaching brief for a single agent — a self-contained HTML document a Team Leader uses to walk into a coaching session prepared. The report covers performance vs targets, peer comparison, QA scores, sentiment trajectory, adherence behaviour, wrap-up discipline, top flagged calls worth reviewing together, and a heuristic top-3 recommended coaching focus.

**This skill is tenant-agnostic.** All tenant-specific knobs (AHT targets, specialist roles, peer-grouping strategy, flagged-call thresholds, output filename pattern) come from `~/.config/genesys-mcp/tenant.yaml`. Works for any Genesys Cloud tenant once the config exists.

## Before starting

1. **Confirm `genesys` MCP is connected.** The `mcp__genesys__*` tools must be available. If not, stop and ask the user to start the MCP server.

2. **Confirm a tenant config exists** at `~/.config/genesys-mcp/tenant.yaml`. Quick check:

   ```bash
   test -f "${GENESYS_MCP_CONFIG:-$HOME/.config/genesys-mcp/tenant.yaml}" && echo "exists" || echo "missing"
   ```

   If missing, stop and tell the user to run the `genesys-tenant-setup` skill first.

3. **Read the relevant tenant config knobs:**

   ```bash
   cd ~/code/genesys-mcp && .venv/bin/python -c "
   from genesys_mcp.tenant import load_config
   import json
   cfg = load_config()
   print(json.dumps({
       'tenant_name': cfg.tenant.name,
       'short_name': cfg.tenant.short_name,
       'specialist_roles': cfg.specialist_roles,
       'mu_ids': cfg.management_units.ids,
       'targets': {
           'voice_aht_s': cfg.targets.voice_aht_s,
           'message_aht_s': cfg.targets.message_aht_s,
           'acw_s': cfg.targets.acw_s,
       },
       'coaching': {
           'peer_grouping': cfg.coaching.peer_grouping,
           'thresholds': cfg.coaching.flagged_call_thresholds.model_dump(),
       },
   }, indent=2))
   "
   ```

4. **Confirm the inputs.** Ask only if not given:
   - **Agent**: a name like "Anthony Kha" or an email. You'll resolve to a user_id via `find_user`.
   - **Period**: defaults to the last 28 days (typical 4-week coaching cadence). Accept "last 4 weeks", "last 2 weeks", "April 2026", an ISO interval, etc.

5. **Period strings are interpreted in `cfg.tenant.timezone`** — typically auto-detected by `genesys-tenant-setup` from the org's default country code; defaults to `UTC` if the field is missing. Same convention as `cc-monthly-report`. Use Python's `zoneinfo.ZoneInfo(cfg.tenant.timezone)` to convert local period boundaries to UTC for the Genesys API.

## Procedure

### Step 1 — Resolve the agent

Use **`mcp__genesys__find_user`** with the name string. If multiple candidates, ask which one. Save the `user_id`, `name`, `title`, and `email`.

### Step 2 — Resolve the interval

Convert the period to an ISO-8601 UTC interval string `"<start>/<end>"`. For "last 4 weeks", use the current UTC datetime as the end and subtract 28 days.

### Step 3 — Resolve the peer set

Based on `cfg.coaching.peer_grouping`:

- **`role`** (default): peers are other active users with the same `title` as the target, EXCLUDING the target. Filter to users whose `title` is in `cfg.specialist_roles` if the target's title is in that list — otherwise just match on exact title.
- **`mu`**: peers are users in the same management unit (use `mcp__genesys__get_user_management_unit` + `mcp__genesys__list_users` to find that MU's members).
- **`queue`**: peers are members of the agent's primary queue (use `mcp__genesys__get_user_queues` then `mcp__genesys__get_queue_members`).

Cap the peer set at 20 (peer-median computation gets noisy past that, and it slows the aggregates query). Save the peer `user_ids` list.

### Step 4 — Call `agent_coaching_pack`

Single call:

```
mcp__genesys__agent_coaching_pack(
    user_id=<target>,
    interval=<iso interval>,
    peer_user_ids=<peers>,
    flagged_calls_limit=10,
)
```

Save the result JSON to `/tmp/cc-coaching-{agent_slug}-{period_slug}/coaching_pack.json`. (Use a slug like `anthony-kha` / `april-2026`.)

### Step 5 — Render the HTML

Run `build_report.py`:

```bash
cd ~/code/genesys-mcp/skills/cc-coaching-prep
.venv/bin/python build_report.py \
    --coaching-pack /tmp/cc-coaching-{agent_slug}-{period_slug}/coaching_pack.json \
    --agent-slug "{agent_slug}" \
    --period "{period_label}" \
    --period-slug "{period_slug}"
```

The script reads `cfg.coaching_output_path(agent_slug, period_slug)` to resolve the output path — typically `~/Documents/coaching-<agent>-<period>.html`.

### Step 6 — Report

Tell the user the output path. If `quality.scope_available` was `false` in the pack, mention that the QA section is empty because the OAuth client doesn't have `quality:readonly` (and how to grant it).

If the top-3 recommended focus is empty (rare — happens for top performers), tell the user that's the actual finding: the agent is broadly on target.

## What the HTML contains

The skill produces a single HTML file with these sections (mirroring the visual idiom of `cc-monthly-report` — same colour-coded vs-target pills, section cards, no JavaScript):

1. **Header card** — agent name, title, manager, period, peer-set size
2. **Performance vs targets** — voice / message / callback volume, AHT, ACW, hold ratio, all colour-coded vs targets and vs peers
3. **Sentiment & quality** — avg sentiment, sample size, QA score summary (avg, pass rate, n_evaluations)
4. **Wrap-up discipline** — note rate, top dispositions handled
5. **Top flagged calls** — table with conversation_id, started_at, media, queue, handle_s, hold_s, sentiment, flag reasons (one row per call)
6. **Recommended coaching focus** — heuristic top-3 with concrete evidence (e.g. *"Voice AHT 330s vs target 285s (+15.8%) — 0.7 handle-hours over target this period"*)
7. **Talking points** (synthesised by you, the LLM, from the data) — short, specific, actionable. Suggested format: 2-3 bullets per recommended-focus area, framed as questions the TL might ask.

## Tone for talking points

Talking points are the LLM-synthesised section — there's no MCP tool for them. Use the structured `agent_coaching_pack` payload to write 2-3 bullets per recommended-focus area. Each bullet should:

- Reference a specific number from the pack ("your AHT was X seconds on N calls")
- Be a question, not a statement ("what was going on with that 12-minute call to the activation queue on…?")
- Acknowledge what's going well too — don't list only negatives, it makes for a worse coaching session

Embed them as a `<section>` after Recommended Focus in the rendered HTML.

## Configurable behaviour

| Knob | Source | Notes |
|---|---|---|
| Voice AHT target | `cfg.targets.voice_aht_s` | colour-codes the voice AHT pill |
| Message AHT target | `cfg.targets.message_aht_s` | colour-codes the message AHT pill |
| ACW target | `cfg.targets.acw_s` | colour-codes the ACW pill |
| Peer-grouping strategy | `cfg.coaching.peer_grouping` | `role` / `queue` / `mu` |
| Flagged-call thresholds | `cfg.coaching.flagged_call_thresholds` | sentiment_drop, silent_seconds, aht_excess_pct |
| Output filename | `cfg.coaching.coaching_filename_pattern` | supports `{agent_slug}` and `{period}` |

## Troubleshooting

- **QA section empty**: `quality:readonly` not granted on the OAuth client. Add the scope in Genesys Admin → Integrations → OAuth.
- **No peers found**: tenant's `specialist_roles` list may not match any active users' titles. Override via `peer_user_ids` arg or fix the role list in tenant.yaml.
- **Sentiment empty**: speech-and-text analytics not enabled on the tenant, or the OAuth client lacks `speech-and-text-analytics:readonly`.
- **Flagged calls list is empty**: agent is performing well within thresholds — that's a legitimate finding, not a bug.
