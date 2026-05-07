# genesys-mcp

A local stdio MCP server that gives Claude Code (or any MCP-compatible client) read-only access to a Genesys Cloud tenant.

Built so contact-centre operations and analytics work — queue performance, agent reviews, conversation deep-dives, repeat-caller root-cause analysis, presence/break/away analysis, demand-vs-capacity vs WFM, monthly contact-centre reports — can be done by talking to Claude in plain English instead of clicking through Genesys Admin or Performance dashboards.

> **v0.2.0 — May 2026:** numbers from `agent_performance` and `queue_performance` now match the Genesys "Performance" UI exactly (the v0.1 figures were materially off). New tools include `repeat_caller_deep_dive` (AI-themed root cause), `wfm_schedule` (scheduled vs forecast-required hours), AWAY + PRE_BREAK tracking. Companion skill `cc-monthly-report` produces a fully-stitched HTML report from one prompt. See [RELEASE-NOTES.md](RELEASE-NOTES.md).

## What it does

Curated tools for ops/analytics work — queues, agents, conversations, recordings, speech analytics, external contacts, workforce management — plus a generic `call_genesys_api` escape hatch for anything not yet wrapped.

**Read-only by design.** The server expects a Client Credentials OAuth client whose role only has `*:readonly` scopes. Even if Claude tried to POST/PUT/DELETE through the escape hatch, Genesys refuses server-side. There are no write tools.

## Setup

Requires Python 3.12+ (developed against 3.14).

### 1. Create a Genesys OAuth client

Genesys Admin → Integrations → OAuth → Add Client.
- **Grant type:** Client Credentials
- **Roles:** create or attach a role with these readonly permissions:
  - **Required for the core tools:** `analytics`, `conversations`, `recordings`, `users`, `routing`
  - **Optional (Wave 3 tools):** `speech-and-text-analytics`, `external-contacts`, `workforce-management`

Copy the Client ID and Client Secret somewhere safe.

### 2. Configure environment variables

```bash
git clone https://github.com/laggyzee/genesys-mcp.git
cd genesys-mcp
cp .env.example ~/.config/genesys-mcp.env
chmod 600 ~/.config/genesys-mcp.env
# Edit ~/.config/genesys-mcp.env and paste your client_id / client_secret.
# Set GENESYS_REGION to your tenant's region (see list below).
```

Supported regions: `ap-southeast-2` (Sydney), `us-east-1` (Virginia), `eu-west-1` (Ireland). Add more in `client.py` if you need them — the SDK supports all Genesys public regions.

### 3. Install

```bash
uv sync   # or: pip install -e .
```

### 4. Wire into Claude Code

Edit `~/.claude/mcp.json` (or your platform's equivalent) and add:

```json
{
  "mcpServers": {
    "genesys": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/genesys-mcp", "python", "-m", "genesys_mcp.server"],
      "env": {
        "GENESYS_CLIENT_ID": "...",
        "GENESYS_CLIENT_SECRET": "...",
        "GENESYS_REGION": "ap-southeast-2"
      }
    }
  }
}
```

Restart Claude Code and the `genesys` MCP server will start automatically.

## Tool surface

### Directory & lookups
| Tool | Purpose |
|---|---|
| `list_queues` | List routing queues, optionally filtered by name |
| `list_users` | List active/inactive users |
| `find_user` | Free-text search by name or email (uses /api/v2/users/search) |
| `find_user_by_email` | Exact email lookup |
| `get_queue_members` | Who's a member of a given queue, with routing status |
| `list_wrapup_codes` | Resolve disposition UUIDs to names |
| `list_routing_skills` / `get_user_skills` | Skill catalogue and per-user mapping |
| `get_user_routing_status` / `get_user_presence_now` | Real-time per-user status |
| `get_user_queues` | Which queues an agent is joined to |

### Real-time & analytics
| Tool | Purpose |
|---|---|
| `queue_observation` | Live snapshot — waiting / interacting / on-queue agents |
| `queue_estimated_wait_time` | Genesys' own AI-adjusted EWT model |
| `queue_performance` | Per-queue × media aggregates that **match the Genesys "Performance > Queues" UI exactly**. Derived fields: `answered` (`tAnswered.count`), `abandoned`, `service_level_pct`, `avg_wait_s`, `avg_answer_s`, `avg_handle_s`. Filter shape mirrors the UI's canonical `and+or+or` form. |
| `agent_performance` | Per-agent productivity that **matches the Genesys "Performance > Agents" UI exactly**, split per media (voice / message / email / callback). Headline fields: `answered` (`tAnswered.count`), `handled`, `avg_handle_s`, `avg_talk_s`, `avg_acw_s`, `transfer_rate_pct`, plus a `by_media` breakdown. *(Was materially wrong in v0.1 — see release notes.)* |
| `presence_sessions` | Per-user break/meal/away sessions over an interval — wraps the analytics/users/details async-jobs flow into a single call |

### Conversations & recordings
| Tool | Purpose |
|---|---|
| `search_conversations` | Search by ANI, queue, agent, direction, time window |
| `get_conversation` | Full conversation detail |
| `list_recordings` | Recording metadata (and region for residency checks) |
| `get_recording_url` | Signed URL for downloading a single recording |

### Composition reports
| Tool | Purpose |
|---|---|
| `repeat_caller_report` | Pulls voice/message/callback details for an interval, groups by ANI, splits the funnel into IVR-only / ACD-offered / answered / abandoned-in-queue per repeater, plus an org-wide funnel block |
| `repeat_caller_deep_dive` | The *why* layer on top of `repeat_caller_report`. Enriches the top repeaters with conversation summaries, AI outcomes (`Resolved` / `Mid Flight` / `Unresolved Chat` / `Escalated`), expected-fix tags, sentiment trajectory, and a heuristic `recommended_action` (`callback_recommended` / `escalate_to_retention` / `route_review` / `monitor`). Org rollup includes top dispositions and the `unresolved_repeaters` priority list. |
| `break_overrun_report` | Per-agent break / meal / **AWAY** / **PRE_BREAK** signals over an interval. AWAY tracked as raw count + total minutes (no target). PRE_BREAK overruns vs configurable target (default 10 min) — handles the auto-applied pre-break presence and quantifies time spent over the drain window. |
| `agent_quality_snapshot` | One-shot agent review combining handle stats, hold-ratio flags, silent-transcript detection, wrap-up note discipline, and optional peer comparison |
| `live_wallboard` | Per-queue real-time view combining observation + EWT + agents-on-queue in one call |

### Speech & text analytics *(needs `speech-and-text-analytics:readonly`)*
| Tool | Purpose |
|---|---|
| `get_conversation_summary` | AI-generated summary (topics, key issues) |
| `get_conversation_sentiment` | Per-speaker sentiment timeline |
| `get_transcript_url` | Signed URL to the full transcript JSON |

### External contacts (CRM) *(needs `external-contacts:readonly`)*
| Tool | Purpose |
|---|---|
| `lookup_external_contact` | Phone/email → CRM record with custom fields |

### Workforce management *(needs `workforce-management:readonly`)*
| Tool | Purpose |
|---|---|
| `list_management_units` / `get_user_management_unit` | WFM topology |
| `query_agent_adherence_explanations` | Why an agent was off-schedule |
| `agent_adherence_review` | Combines presence overruns with WFM explanations side-by-side |
| `wfm_schedule` | Per-day **scheduled hours** (sum of paid-time activities across user shifts) vs **WFM-forecast required hours** (from headcountforecast `requiredPerInterval`). The headline answer to "do we need more staff or just better scheduling shape?" — compares scheduled capacity against demand on every day of the period and flags understaffed days. |

### Escape hatch
| Tool | Purpose |
|---|---|
| `call_genesys_api` | Generic `/api/v2/*` call (GET/POST/PUT/PATCH/DELETE). Non-GET will 403 unless your OAuth client has write scopes (it shouldn't). |

## Example sessions

Once installed, just talk to Claude:

- *"Pull last week's answer rate and SLA for our voice queues."*
- *"Find Jane Smith and show me her status right now."*
- *"What's the estimated wait time on the Sales queue?"*
- *"Run the repeat-caller deep dive for last week — top 25 ANIs."*
- *"Who's spending the most time over the 10-minute pre-break this month?"*
- *"How does our scheduled capacity compare to the WFM forecast for April?"*
- *"Pull a quality snapshot for agent X over the last 7 days, compared with their peers."*
- *"What's the live wallboard look like for these 6 queues?"*

## Companion skill: `cc-monthly-report`

A user-installable Claude Code skill that produces a self-contained HTML contact-centre report from one prompt — *"do the monthly CC report for May 2026"* — and drops it at `~/Documents/Prvidr-CC-{period}.html`.

What the report contains:

1. Executive summary (KPI cards + headline findings)
2. Volume & funnel (brand × channel totals, per-queue tables, daily voice service-level chart)
3. Themes (top dispositions, AI outcome distribution, top expected-fix tags)
4. Repeat callers — actionable priority list of unresolved repeaters with summaries
5. Workforce — per-agent productivity (specialists only), AHT vs targets (voice / message / ACW), break / AWAY / pre-break behaviour
6. Performance leverage — quantifies "phantom capacity" (handle hours that would be freed if every agent hit AHT target) + "FCR drag" (handle hours wasted on repeat calls), then compares against the WFM-derived peak-demand shortfall to give a single synthesised verdict: *"more staff or better staff?"*

Living at `~/.agents/skills/cc-monthly-report/` (symlinked under `~/.claude/skills/`). The skill markdown describes the workflow; a Python script does the aggregation and HTML rendering. Reproducible — the same skill against the same period gives the same report.

## Design

- **OAuth at startup** — client credentials token fetched on lifespan init, auto-refreshed on 401 via the retry helper.
- **No write access from the MCP** — even if the LLM tries to POST/PUT/DELETE, the OAuth scope refuses server-side. Out-of-band administrative writes (e.g. bulk agent provisioning, see [Danger Zone](#-danger-zone--out-of-band-write-scripts) below) use a separate, locally-invoked script with its own write-scoped OAuth client; the MCP server never loads those credentials.
- **id → name resolution cache** — internal `naming.Resolver` lazy-loads queue/user/wrap-up names so most responses are human-readable without follow-up calls.
- **Composition over wrappers** — `agent_quality_snapshot`, `repeat_caller_report` etc. chain multiple endpoints into single ops-ready reports rather than forcing the caller to do the joining.

## ⚠️ Danger Zone — out-of-band write scripts

The MCP server is read-only by design. For the rare administrative tasks that genuinely require writes against your tenant, the [scripts/](scripts/) directory contains **standalone CLIs** that:

- Are **not** registered as MCP tools — Claude cannot reach them, regardless of prompt
- Use a **separate, write-scoped OAuth client** (`GENESYS_WRITE_CLIENT_ID/SECRET`) — the read-only MCP client is unaware of it and the server's startup code warns loudly if write credentials leak into the MCP process
- Default to **`--dry-run`** — explicit `--confirm` is required to write
- Ship a **`--self-test`** mode that exercises every write step against a throwaway user before you point the script at real data
- Track per-user state in a **ledger** at `/tmp/provision_users/<run-id>/` so a partial failure can resume from the failing step

**Read [scripts/README.md](scripts/README.md) before running anything in this directory.** It documents the one-off Genesys admin setup (a separate OAuth client + a tightly-scoped custom role), the tenant assumptions the scripts make (e.g. roles inherited from group membership), and the precise list of permissions to grant.

### Currently shipped

| Script | What it does |
|---|---|
| [`provision_users.py`](scripts/provision_users.py) | Bulk-create agents from a template agent (clones division, manager, location, ACD auto-answer, addresses, title/department, profile skills, routing skills + proficiency, routing languages, group memberships, WFM management unit; sends activation invite). |

Quick reference (see [scripts/README.md](scripts/README.md) for the full setup and tenant-assumption notes):

```bash
# 1. Verify the OAuth role has every required scope (creates a throwaway user; you delete it manually)
python scripts/provision_users.py --self-test --template-email <existing-agent>@example.com

# 2. Dry-run a real batch (default — writes nothing)
python scripts/provision_users.py --template-email <template>@example.com --emails new_starters.txt

# 3. Actually execute
python scripts/provision_users.py --template-email <template>@example.com --emails new_starters.txt --confirm
```

## Companion skill

Pair with the [`platform-api`](https://github.com/MakingChatbots/genesys-cloud-skills) skill from MakingChatbots for endpoint discovery — useful when working with `call_genesys_api`.

## Contributing

PRs welcome. Things on the roadmap that someone might want to take a swing at:
- Web messaging transcript wrapper (the `/api/v2/conversations/messages/{id}/messages/bulk` flow, which currently needs the `call_genesys_api` escape hatch)
- Half-hourly intra-day staffing in `wfm_schedule` (currently rolls up to daily)
- Forecast-vs-actual analysis (compare WFM forecast to historical conversation volumes)
- Quality evaluations / scorecards (`quality:readonly`)
- Outbound campaign progress (`outbound:readonly`)
- Skill-based routing analysis (which agents have which skills × queue requirements)

## Licence

MIT — see `LICENSE`.
