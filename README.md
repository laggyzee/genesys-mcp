# genesys-mcp

A local stdio MCP server that gives Claude Code (or any MCP-compatible client) read-only access to a Genesys Cloud tenant.

Built so contact-centre operations and analytics work — queue performance, agent reviews, conversation deep-dives, repeat-caller root-cause analysis, presence/break/away analysis, demand-vs-capacity vs WFM, monthly contact-centre reports — can be done by talking to Claude in plain English instead of clicking through Genesys Admin or Performance dashboards.

> **v1.0 — May 2026:** the **tenant-agnostic + correctness floor** release. Two themes:
> - **Hardening** (v0.10 floor): 286 tests; response-shape validators wired into every skill build script; numeric snapshot tests pin the four core aggregator outputs; a shared accumulator de-duplicates the v0.9.1 P7D-bucket fix; coverage parity across all four skills.
> - **Tenant-agnostic** (v1.0): every tenant-specific assumption that used to be baked into Python now lives in [`tenant.yaml`](docs/tenant-config-schema.md). Pre-break UUID, coaching heuristic thresholds (hold ratio, QA pass mark, peer multiplier, etc.), and specialist roles are all config-driven. A new `operating_model` block lets single-brand, message-only, and no-pre-break tenants get clean degraded reports instead of misleading zeros. Queue-name pattern is optional and the loader hard-fails on schema-version drift.
>
> See [RELEASE-NOTES.md](RELEASE-NOTES.md) for the full history.

## What it does

Curated tools for ops/analytics work — queues, agents, conversations, recordings, speech analytics, external contacts, workforce management — plus a generic `call_genesys_api` escape hatch for anything not yet wrapped.

**Read-only by design.** The server expects a Client Credentials OAuth client whose role only has `*:readonly` scopes. Even if Claude tried to POST/PUT/DELETE through the escape hatch, Genesys refuses server-side. There are no write tools.

## Will this work on my tenant?

v1.0 was the explicit shift from "works on one specific Genesys setup" to "works on any Genesys Cloud tenant — and tells you up-front where your shape differs." Three categories of assumption you can toggle via [`tenant.yaml`](docs/tenant-config-schema.md):

| Assumption | Default | Toggle off if |
|---|---|---|
| **Pre-break presence as drain state** — agents go BUSY/"Pre Break" before scheduled breaks to drain interactions | enabled | your CC doesn't use a pre-break presence; reports render a "tracking disabled" callout instead of zero rows |
| **Multi-brand structure** — multiple brand display names share the same CC | enabled | single-brand tenant; reports collapse brand × channel rollup to channel-only |
| **Voice + message channels** | both | message-only / voice-only tenant; headline KPI cards skip the irrelevant channel |
| **Queue naming pattern** — `{brand} - {channel} - {function}` | matches > 80% | your queues use a different convention; set `name_pattern_match_required: false` to fall back per-queue, or `name_pattern: null` for no structured parsing |
| **Specialist role title** | `["Specialist", "Customer Service Specialist"]` (no in-code default in v1.0 — discovered by the setup wizard) | your active users' titles differ; the wizard discovers what's actually in use |

Run `python -m genesys_mcp.health_check --strict` after setup. It samples a page of queues to compute the pattern-match rate, cross-checks `specialist_roles` against active user titles, and validates every other tenant.yaml field against the live data. Exit code 0 (ready), 2 (warnings — `--strict`), or 1 (blocked). Use the warnings to decide which `operating_model` toggles to flip.

## Setup

Requires Python 3.11+ (developed against 3.14).

### Quickstart (v0.6+) — one-command install

After creating your OAuth client (step 1 below), the fastest path is:

```bash
git clone https://github.com/laggyzee/genesys-mcp.git
cd genesys-mcp
./install.sh
```

The installer syncs deps, prompts for OAuth creds, registers the MCP with Claude Code, symlinks every skill into `~/.claude/skills/`, and runs the health check. Idempotent — re-running it upgrades cleanly.

If you'd rather do it manually, follow the numbered steps below.

### 1. Create a Genesys OAuth client

Genesys Admin → Integrations → OAuth → Add Client.
- **Grant type:** Client Credentials
- **Roles:** create or attach a role with these readonly permissions:
  - **Required for the core tools:** `analytics`, `conversations`, `recordings`, `users`, `routing`
  - **Optional (Wave 3 tools):** `speech-and-text-analytics`, `external-contacts`, `workforce-management`
  - **Optional (v0.5 coaching tools):** `quality` — enables the `qa_evaluations` tool and the QA section of `agent_coaching_pack` / the `cc-coaching-prep` skill. Without it, the QA section soft-fails (returns `scope_available: false`) and the rest still works.

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

### 5. (Optional) Set up the tenant config — needed for `cc-monthly-report` and other tenant-aware skills

Skills that produce tenant-aware artefacts (currently just `cc-monthly-report`, more in future) read tenant-specific knobs — brand list, queue naming pattern, WFM management unit, AHT targets, pre-break presence, output filename — from a single YAML file at `~/.config/genesys-mcp/tenant.yaml` (or `$GENESYS_MCP_CONFIG`). The MCP server itself doesn't need this; only the dependent skills do.

Easiest way to populate it: ask Claude *"set up genesys mcp for my tenant"* — that triggers the [`genesys-tenant-setup`](skills/genesys-tenant-setup/SKILL.md) skill, which:

1. Auto-discovers everything it can from your read-only OAuth client (queue naming pattern, brand list, WFM units, pre-break presence, specialist roles)
2. Asks you a short list of policy questions for the rest (tenant display name, AHT targets, output filename)
3. Validates the result against the schema and writes it to `~/.config/genesys-mcp/tenant.yaml`

Or by hand: copy [`skills/cc-monthly-report/tenant.example.yaml`](skills/cc-monthly-report/tenant.example.yaml) to `~/.config/genesys-mcp/tenant.yaml` and fill in the values. Schema reference: [`docs/tenant-config-schema.md`](docs/tenant-config-schema.md).

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
| `queue_performance` | Per-queue × media aggregates. **Raw metrics match the Genesys "Performance > Queues" UI exactly** (`tAnswered.count` = UI "Answer" column, etc.). Derived fields computed by the MCP — verify via [`mcp-reconcile`](#mcp-reconcile) before any release: `answered`, `abandoned`, `service_level_pct`, `avg_wait_s`, `avg_answer_s`, `avg_handle_s`. Filter shape mirrors the UI's canonical `and+or+or` form. |
| `agent_performance` | Per-agent productivity, split per media (voice / message / email / callback). **Raw metrics match "Performance > Agents" UI exactly** (`tAnswered.count`, `tHandle.sum/count`). Headline fields: `answered`, `handled`, `avg_handle_s`, `avg_talk_s`, `avg_acw_s`, `transfer_rate_pct`, plus a `by_media` breakdown. *(Was materially wrong in v0.1 — see release notes.)* Aggregation across buckets / per-media split is MCP logic; cross-check with `mcp-reconcile`. |
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
| `agent_coaching_pack` | One-shot 1:1 prep brief: volume / AHT vs targets, peer-median comparison, sentiment + QA, wrap-up discipline, top flagged calls, and heuristic top-3 recommended coaching focus. Tenant-aware (loads AHT targets and flagged-call thresholds from `tenant.yaml` when present, falls back to in-code defaults). Drives the `cc-coaching-prep` skill. |
| `live_wallboard` | Per-queue real-time view combining observation + EWT + agents-on-queue in one call |

### Quality management *(needs `quality:readonly`)*
| Tool | Purpose |
|---|---|
| `qa_evaluations` | Per-agent Quality Management evaluation summary over an interval: avg score, pass rate, critical-pass rate, last-evaluated, per-evaluation rows (form, evaluator, score, conversation id). Optional per-question detail + evaluator comments via `include_question_detail=True`. Soft-fails with `scope_available: false` when the scope isn't granted. |

### Routing diagnostics
| Tool | Purpose |
|---|---|
| `routing_diagnostic` | Explains why a specific conversation routed (or didn't) as expected: IVR → queue → outcome path with durations, queue routing rules, eligible-agent counts (session-level from Genesys, current-state for the queue), abandon / answer / transfer classification. |
| `routing_diagnostic_aggregate` | (v0.9) Aggregate failure-mode analysis for a queue over an interval. *"Show me all this week's abandons on our general inbound queue — how many were because nobody was eligible vs. all-busy? Which 15-minute windows were worst?"* Closes the v0.5 promise. Heuristic classification: `no_eligible_agents` / `all_eligible_busy` / `abandoned_in_ivr`. Pairs with the v0.7 cc-daily-brief 'worst routes' section — daily-brief surfaces *which* queues failed; this surfaces *why*. |

### Health check (v0.6+)
| Tool | Purpose |
|---|---|
| `mcp_health_check` | End-to-end onboarding check: probes one representative endpoint per OAuth scope (green/red per scope), validates `tenant.yaml` against the schema, verifies the companion skills are symlinked into the Claude Code skills dir. Returns a `verdict` of `ready` / `ready_with_warnings` / `blocked` plus concrete remediation strings for each gap. Also available as a CLI: `python -m genesys_mcp.health_check`. |

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
| `volume_vs_forecast` | (v0.7) Per-interval **forecast vs actual** comparison — closes the WFM loop alongside `wfm_schedule`. Pulls the published short-term forecast for an interval (`/businessunits/{buId}/weeks/{weekDate}/shorttermforecasts`), pulls actual conversation volume + handle time via analytics aggregates, computes per-bucket variance, and rolls up forecast accuracy (MAPE). Answers *"how good was last week's forecast?"* — WFM analysts currently build this in Excel. |

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

## Companion skills

Five user-installable Claude Code skills ship with this repo. All depend on a per-tenant config at `~/.config/genesys-mcp/tenant.yaml` — see Setup step 5 above for how to populate it (the easiest path is the `genesys-tenant-setup` wizard).

### `cc-monthly-report`

Produces a self-contained HTML contact-centre report from one prompt — *"do the monthly CC report for May 2026"* — and drops it at `<output_dir>/<short_name>-CC-<period>.html` (paths configurable per tenant).

What the report contains:

1. **LLM narrative synthesis** (v0.7) — four sections written by Claude on top of the data: *Coverage & caveats*, *What worked*, *What went wrong*, *Recommended actions*. Opt-in via `--with-narrative <md-file>`; omit the flag for a data-only build.
2. Executive summary (KPI cards + headline findings)
3. Volume & funnel (brand × channel totals, per-queue tables, daily voice service-level chart, plus the v0.9 **hour-of-day × day-of-week heatmap** that surfaces intra-day staffing patterns the daily chart averages away)
4. Themes (top dispositions, AI outcome distribution, top expected-fix tags)
5. Repeat callers — actionable priority list of unresolved repeaters with summaries
6. Workforce — per-agent productivity (specialists only), AHT vs targets (voice / message / ACW), break / AWAY / pre-break behaviour. v0.9 adds inline-SVG **voice-AHT sparklines** next to each headline AHT so direction of travel is visible at a glance.
7. Performance leverage — quantifies "phantom capacity" (handle hours that would be freed if every agent hit AHT target) + "FCR drag" (handle hours wasted on repeat calls), then compares against the WFM-derived peak-demand shortfall to give a single synthesised verdict: *"more staff or better staff?"*

Tenant-agnostic by design: every brand name, queue naming pattern, AHT target, WFM unit ID and presence ID is read from the tenant config. No tenant-specific values are hard-coded in the skill or build script.

Living at [`skills/cc-monthly-report/`](skills/cc-monthly-report/) (symlinked under `~/.claude/skills/`). The skill markdown describes the workflow; a Python script does the aggregation and HTML rendering. Reproducible — the same skill against the same period and tenant config gives the same report.

### `cc-coaching-prep`

One-prompt 1:1 prep brief for a single agent — *"prep coaching for [agent name] for the last 4 weeks"* — drops a self-contained HTML at `<output_dir>/coaching-<agent-slug>-<period>.html`.

What the brief contains:

1. Performance vs targets (KPI cards + colour-coded vs-target pills for voice / message AHT and ACW)
2. Peer comparison table (same-role peers, peer-median for each KPI with delta-vs-peers badges)
3. Sentiment & quality (avg sentiment, QA score / pass rate / critical-pass / last-evaluated, recent evaluations table)
4. Wrap-up & handling (note rate, top dispositions)
5. Top flagged calls (heuristic: sentiment drop, hold ratio, AHT excess, no wrap-up notes — colour-coded reason pills with transcript links)
6. Recommended coaching focus (heuristic top-3 with concrete evidence)

v0.9 adds optional LLM narrative synthesis in three sections — *Strengths to acknowledge*, *Areas to coach*, *Suggested talking points* — embedded in the HTML so the TL can re-read them right before walking into the 1:1 instead of scrolling chat history.

Tenant-aware: AHT targets, peer-grouping strategy (`role` / `queue` / `mu`), and flagged-call thresholds (sentiment drop, silent seconds, AHT-excess %) all read from `coaching.*` in tenant.yaml. Soft-degrades cleanly: no quality scope → QA section empty; no STA scope → sentiment section empty; the rest still populates.

Living at [`skills/cc-coaching-prep/`](skills/cc-coaching-prep/).

### `cc-daily-brief`

(v0.7) One-prompt **daily** brief for supervisors at start-of-day — *"daily brief"*, *"morning brief"*, *"how did we go yesterday"*. Drops a one-page HTML at `<output_dir>/daily-brief-<YYYY-MM-DD>.html`.

What the brief contains:

1. Headline KPIs — voice + message SL today vs the rolling 7-day median
2. Worst routes — top queues by SL drop vs their rolling median
3. Flagged agents — top agents by voice AHT excess
4. Repeat-caller callback list — unresolved repeaters from yesterday
5. Adherence flags — agents over the combined break/pre-break/meal overrun threshold

v0.9 adds optional LLM narrative synthesis in two sections — *Headline* (≤80-word paragraph framing the day vs the rolling median) + *Today's priorities* (top 3 actions for the supervisor's morning standup). Daily briefs are meant to be glanced at; the 4-section monthly shape would be overkill.

Tenant-aware: flag thresholds (sentiment dip, AHT excess %, SL drop pp) and the comparison window all read from `daily_brief.*` in tenant.yaml. Designed to fit a laptop screen or Slack share without scrolling — narrower than the monthly report.

Living at [`skills/cc-daily-brief/`](skills/cc-daily-brief/).

### `mcp-reconcile`

(v0.8) Release-time reconciliation. *"reconcile the MCP against the Genesys UI"* pulls the MCP tool stack for a chosen period (default: last completed week) and outputs a Markdown checklist of side-by-side comparisons:

```markdown
| ✓ | Queue | Media | MCP answered | Verify in Genesys UI |
|---|---|---|---:|---|
| ☐ | Brand A - Activation | voice | 1,247 | Performance → Queues → 18-25 May → Voice column |
```

You click through the live Genesys UI, tick each row off, and either confirm parity or flag a mismatch. Pairs with `make test`: the test suite proves the aggregator maths is stable; reconciliation proves the source numbers still match the UI. Run before each release.

Living at [`skills/mcp-reconcile/`](skills/mcp-reconcile/).

### `genesys-tenant-setup`

Generates the per-tenant config (`~/.config/genesys-mcp/tenant.yaml`) by auto-discovering whatever it can from the read-only OAuth client (queue patterns, brand list, WFM units, pre-break presence, specialist roles, AHT baselines, timezone) and asking Claude conversational questions for the rest. Writes a validated YAML file when done. Run it once per tenant — anyone forking the repo will use this.

Living at [`skills/genesys-tenant-setup/`](skills/genesys-tenant-setup/).

## Testing (v0.8+)

The aggregation layer + canonical Genesys-UI filter shapes are covered by a pytest suite:

```bash
make test
```

What's tested:

- **Aggregators** in `skills/cc-monthly-report/build_report.py` — pure dict→dict functions. Golden fixtures captured from a live tenant (via `tests/_capture_fixtures.py`); structural assertions ensure refactors don't silently change output counts/totals.
- **Canonical filter shapes** (`tests/test_analytics_filters.py`) — the v0.2 UI-parity fix is pinned via monkey-patched-SDK tests. A regression to the pre-v0.2 flat-OR shape would fail these tests immediately.
- **Helper functions** (formatters, threshold classifiers, sentiment/trend labellers) — parameterised unit tests; no fixtures required.
- **HTML rendering** — BeautifulSoup-based structural assertions on column counts, vs-target pill colours, narrative-synthesis sections.
- **Conversation deep-link helper** — resolution priority + region mapping + fallback rendering.

What's deliberately **not** tested at this layer:

- Raw MCP tool wrappers in `tools/*.py` — they mostly call the SDK 1:1. Testing them mostly tests the SDK. The filter-shape assertions cover our own logic; the SDK's correctness isn't our problem.
- End-to-end live-tenant calls — covered by the `mcp-reconcile` skill instead: run a reconciliation checklist before each release and click each row off against the live Genesys UI.

If a refactor breaks something, the test suite catches structural drift; if Genesys changes endpoint semantics, the reconciliation checklist catches numerical drift. Both are needed.

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
- Half-hourly intra-day staffing in `wfm_schedule` (currently rolls up to daily; the v0.9 hour-of-day heatmap covers the *demand* side, but scheduled-capacity bucketing is still daily)
- Skill-based routing analysis (which agents have which skills × queue requirements)
- GitHub Pages site with anonymised sample reports — distribution/discoverability is the v0.10 candidate, not another feature release

Explicitly removed from the backlog: **outbound campaign coverage** (deferred across v0.5–v0.8, dropped in v0.9 — re-add if an outbound-shop user opens an issue). **Forecast-vs-actual analysis** shipped in v0.7 as `volume_vs_forecast`.

## Licence

MIT — see `LICENSE`.
