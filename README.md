# genesys-mcp

A local stdio MCP server that gives Claude Code (or any MCP-compatible client) read-only access to a Genesys Cloud tenant.

Built so contact-centre operations and analytics work — queue performance, agent reviews, conversation deep-dives, presence/break analysis, repeat-caller reports — can be done by talking to Claude in plain English instead of clicking through Genesys Admin or Performance dashboards.

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
| `queue_performance` | Aggregates with derived `answered`, `abandoned`, `service_level_pct`, `avg_wait_s`, `avg_answer_s`, `avg_handle_s` (the values that match the Performance UI columns) |
| `agent_performance` | Per-agent presence/routing-status aggregates |
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
| `repeat_caller_report` | Pulls voice/message/callback details for an interval, groups by ANI, returns top-N repeat callers with queues touched and connect rate |
| `break_overrun_report` | Per-agent break/meal overruns vs configurable targets, ranked by overrun count |
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

### Escape hatch
| Tool | Purpose |
|---|---|
| `call_genesys_api` | Generic `/api/v2/*` call (GET/POST/PUT/PATCH/DELETE). Non-GET will 403 unless your OAuth client has write scopes (it shouldn't). |

## Example sessions

Once installed, just talk to Claude:

- *"Pull last week's abandon rate and SLA for our voice queues."*
- *"Find Jane Smith and show me her status right now."*
- *"What's the estimated wait time on the Sales queue?"*
- *"Which customers called us 3+ times this week?"*
- *"Did anyone go over their break or lunch this week?"*
- *"Pull a quality snapshot for agent X over the last 7 days, compared with their peers."*
- *"What's the live wallboard look like for these 6 queues?"*

## Design

- **OAuth at startup** — client credentials token fetched on lifespan init, auto-refreshed on 401 via the retry helper.
- **No write access** — even if the LLM tries to POST/PUT/DELETE, the OAuth scope refuses server-side.
- **id → name resolution cache** — internal `naming.Resolver` lazy-loads queue/user/wrap-up names so most responses are human-readable without follow-up calls.
- **Composition over wrappers** — `agent_quality_snapshot`, `repeat_caller_report` etc. chain multiple endpoints into single ops-ready reports rather than forcing the caller to do the joining.

## Companion skill

Pair with the [`platform-api`](https://github.com/MakingChatbots/genesys-cloud-skills) skill from MakingChatbots for endpoint discovery — useful when working with `call_genesys_api`.

## Contributing

PRs welcome. Things on the roadmap that someone might want to take a swing at:
- Web messaging transcript wrapper (the `/api/v2/conversations/messages/{id}/messages/bulk` flow, which currently needs the `call_genesys_api` escape hatch)
- Full async historical adherence (scheduled vs. actual percentages with shift overlay)
- Quality evaluations / scorecards (`quality:readonly`)
- Outbound campaign progress (`outbound:readonly`)

## Licence

MIT — see `LICENSE`.
