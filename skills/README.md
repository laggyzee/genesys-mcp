# skills/

Claude Code skills built on top of the genesys-mcp tool surface. A skill is a
markdown procedure file (plus optional helper scripts) that turns a
multi-step workflow into a one-prompt command.

## What's here

| Skill | What it does |
|---|---|
| [`cc-monthly-report/`](cc-monthly-report/) | One-prompt monthly contact-centre report. Pulls from `queue_performance`, `agent_performance`, `break_overrun_report`, `repeat_caller_deep_dive`, and `wfm_schedule`; produces a single self-contained HTML report with funnel, themes, repeat callers, workforce, performance leverage, demand-vs-capacity, and recommended actions. |

## Installing a skill

Skills live in your Claude Code skills directory (typically `~/.claude/skills/`
on macOS, may be `~/.agents/skills/` depending on your setup). To install
one from this repo:

```bash
# from the repo root
ln -s "$(pwd)/skills/cc-monthly-report" ~/.claude/skills/cc-monthly-report
```

Then restart Claude Code (or open a fresh session) and the skill is
auto-discovered by name. The skill description in `SKILL.md` tells Claude
when to invoke it.

## Why skills live here (and not in their own repo)

These skills depend tightly on the MCP tool surface — they call specific
tools by name, expect specific response shapes, and are versioned together
with the MCP. Splitting them across repos would mean coordinating two
release tags every time a tool's response shape changes. Co-locating keeps
the contract honest.

If you fork this repo to use the MCP at a different tenant, you'll likely
also fork the skills and tweak the tenant-specific bits (queue brand
patterns, WFM business unit / management unit IDs, etc). Each skill's
`SKILL.md` calls out where those defaults are.

## Adding a new skill

1. Create a new directory under `skills/`
2. Add a `SKILL.md` with frontmatter (`name`, `description`, optional `metadata`)
3. Add any helper scripts (Python, shell, etc) the skill needs
4. Document tenant-specific defaults clearly (or derive them at runtime)
5. Add a row to the table above
6. PR welcome
