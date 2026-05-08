---
name: genesys-tenant-setup
description: "Use when the user wants to set up the tenant config for the genesys-mcp skills (cc-monthly-report and any future tenant-aware skills) — e.g. 'set up genesys mcp for my tenant', 'configure cc-monthly-report', 'I cloned the genesys-mcp repo, what now', 'create the tenant.yaml'. Auto-discovers what it can from the read-only MCP (queue naming pattern, brand list, WFM management units, pre-break presence, specialist roles), conducts a guided interview for the rest (tenant display name, AHT targets, output filename), and writes ~/.config/genesys-mcp/tenant.yaml. Requires the read-only OAuth credentials (GENESYS_CLIENT_ID/SECRET) to be set; the genesys MCP itself doesn't need to be connected."
metadata:
  version: 1.0.0
---

# Genesys MCP — tenant setup wizard

You are guiding the user through producing `~/.config/genesys-mcp/tenant.yaml` — the per-user config file that drives every tenant-aware skill in this repo. Anyone who clones the repo and wants to run `cc-monthly-report` (or any future skill that depends on brand/queue/WFM knowledge) needs this file populated for their tenant.

The guiding principle: **auto-discover what we can, ask only what we must**. The companion script `setup.py` probes the tenant via the read-only OAuth client and produces a discovery JSON; your job is to show that to the user, ask them to confirm/refine each piece, and write the final config.

## Before starting

1. **Confirm read-only credentials are available.** The script needs `GENESYS_CLIENT_ID/SECRET` (and optionally `GENESYS_REGION`). One Bash check:

   ```bash
   cd ~/code/genesys-mcp && .venv/bin/python -c "
   from genesys_mcp.client import _read_config
   try:
       cid, _, region = _read_config()
       print(f'✓ read creds present, region={region}')
   except Exception as exc:
       print(f'✗ MISSING: {exc}')
   "
   ```

   If credentials are missing, stop and tell the user to populate them per the [main README's Setup section](../../README.md#setup) (either in `~/.config/genesys-mcp.env` or in a `.env` next to the repo).

2. **Check whether a tenant config already exists** at `~/.config/genesys-mcp/tenant.yaml`. If yes, ask the user whether they want to overwrite it or back it up first. Suggest:

   ```bash
   cp ~/.config/genesys-mcp/tenant.yaml ~/.config/genesys-mcp/tenant.yaml.bak.$(date +%Y%m%d-%H%M%S)
   ```

   Don't proceed to the discovery step without confirmation — overwriting the file means anyone who's been running `cc-monthly-report` against the old config will see different output until they're informed.

## Procedure

### Step 1 — Run auto-discovery

```bash
cd ~/code/genesys-mcp && \
  .venv/bin/python skills/genesys-tenant-setup/setup.py \
    --discover \
    --draft /tmp/genesys-mcp-tenant-draft.yaml \
  > /tmp/genesys-mcp-tenant-discovery.json
```

Read both files. The JSON has the structured discovery; the YAML draft is a populated-but-not-yet-validated starting point with `__SETUP__` placeholders for the bits the interview must fill in.

Display a short summary to the user:

> *I probed your tenant and found:*
>
> - `<N>` queues; the naming pattern looks like `<pattern>` (`<confidence>` confidence)
> - `<N>` brand candidates: `<brand1>`, `<brand2>`, …
> - `<N>` WFM management unit candidates
> - `<N>` pre-break presence candidates
> - `<N>` active users across `<N>` distinct titles; `<N>` look like specialists

If `pattern_confidence` is `low` or `none`, say so explicitly — the auto-detected pattern probably isn't trustworthy and the user will need to provide one manually.

### Step 2 — Ask only what discovery couldn't determine

Conduct the interview in this order. Don't dump every question at once — pace them naturally. Use AskUserQuestion **only** for genuine multiple-choice picks (MUs, presences); use plain conversational prompts for free-text or numeric values.

#### 2a. Tenant display name + short name (free-text, required)

Ask: *"What should I call your tenant in report headlines (e.g. `Acme Contact Centre`)?"*

Then derive a `short_name` from it (lowercase, hyphens for spaces, no slashes), confirm with the user, and let them override:

> *I'll use `acme-contact-centre` as the short name (used in filenames). OK or override?*

#### 2b. Brand list (confirm + edit)

Show the auto-discovered brand list. Ask: *"These are the brand names I extracted from queue names. Add any I missed, remove any that aren't actual brands."*

If the user adds a brand that doesn't appear in any queue name, warn them — the report's brand-aggregation step will produce zero rows for it.

#### 2c. Queue pattern (verify by example)

Show 5 example queue names per brand and the parsed `(brand, function)` extraction. Ask: *"Do these parse correctly?"*

If not, the auto-detected `name_pattern` is wrong. Ask the user to describe their pattern in words and translate it to a `{brand} - {function}` / `{brand} - {channel} - {function}` template manually.

#### 2d. Customer-facing functions + skip list (review + edit)

Show the discovered functions and skip_substrings side by side:

> *Functions I'd include in the report:* `Activation, Billing, Complaints, General, Retention, Sales, Technical Support`
>
> *Skip-substrings (queues containing these strings get excluded):* `Holding, Internal, Jira, Outbound Email, Documents, Supervisor, ZZZ_`
>
> *Anything to add or remove?*

#### 2e. WFM management units (multi-select)

Show the discovered MUs with their names and BU. Use AskUserQuestion if there are 2–4 candidates; show as a numbered list and ask "which apply?" if more.

For each chosen MU, derive `business_unit.id` from its `business_unit_id` field (warn if multiple chosen MUs span different BUs — that's unusual and might mean the user picked something they shouldn't have).

If the script returned a `_error` entry indicating no WFM read perm, skip this step and tell the user the WFM section of the report will be empty until they grant `wfm:managementUnit:view` to the OAuth client and re-run.

#### 2f. Pre-break presence (single-select if multiple candidates, auto-confirm if one)

Show the candidates' labels. If exactly one match → auto-confirm with the user (*"I found a presence labelled `Pre Break` (id: `<uuid>`). Use this for pre-break/drain tracking?"*). If multiple → AskUserQuestion. If zero or `_error` → warn the user that pre-break tracking will be unreliable and ask if they want to set the id manually.

#### 2g. Specialist roles (multi-select)

Show the title_counts histogram. Auto-suggest titles containing "Specialist" / "Agent" / "Consultant". Ask: *"Which of these titles count as 'customer-facing specialists' for the workforce table?"*

Encourage them to be inclusive of leadership-adjacent titles (e.g. include "Senior Specialist" but NOT "Team Leader") — the workforce table filters to specialists by default but does so against this list.

#### 2h. Targets (with sensible defaults)

Show:

> *Defaults: voice AHT 285s, message AHT 660s, ACW 15s, pre-break drain 10 min, FTE 160 productive hours/month.*
>
> *Hit enter to accept all defaults, or override any.*

Don't make the user think about each one unless they want to. Most tenants stay close to defaults.

#### 2i. Output dir + filename pattern (confirm)

Default `~/Documents/{tenant}-CC-{period}.html`. Ask once if they want to change it; if not, accept the default.

### Step 3 — Save the config

Build the final config as a Python dict (using everything you collected), then save via the setup script's `--save` mode (which validates against the `TenantConfig` Pydantic model before writing):

```bash
cd ~/code/genesys-mcp && \
  .venv/bin/python skills/genesys-tenant-setup/setup.py \
    --save \
    --config-json '<JSON-encoded-final-config>'
```

The default save location is `~/.config/genesys-mcp/tenant.yaml` (or whatever `$GENESYS_MCP_CONFIG` / `$XDG_CONFIG_HOME` resolves to). On success, print:

> *✓ Saved your tenant config to `<path>`. You can now run any tenant-aware skill (e.g. `cc-monthly-report`) and it will pick this up automatically. Edit the file directly any time, or rerun this skill to regenerate.*

If the validate step fails (e.g. `short_name` has spaces, or `name_pattern` doesn't contain `{brand}`), the script will print the error path-by-path; relay that, ask the user to fix the offending value, and rerun the save.

### Step 4 — Smoke-test the saved config

After saving, do a one-liner sanity check:

```bash
cd ~/code/genesys-mcp && .venv/bin/python -c "
from genesys_mcp.tenant import load_config
cfg = load_config()
print(f'tenant: {cfg.tenant.name}')
print(f'brands: {cfg.brands.names}')
print(f'output filename pattern: {cfg.report_output_path(\"example-period\")}')
"
```

If this loads without error, the config is good.

## When NOT to use this skill

- **Read-only MCP credentials aren't set up yet** — the user needs to do the `~/.config/genesys-mcp.env` setup first (see [main README](../../README.md#setup)). Without those, this script can't probe the tenant.
- **The user already has a working tenant.yaml and doesn't want to regenerate** — overwriting silently would change the behaviour of every dependent skill. Always prompt for confirmation in step 0.
- **The user wants to set up the write OAuth client for `scripts/provision_users.py`** — that's a different setup ([scripts/README.md](../../scripts/README.md), Phase 0). This skill is only for the read-only tenant config.

## Caveats

- **Auto-discovery is best-effort.** The script can be confidently right about queue patterns and pre-break presence (single uniquely-named match), reasonably right about brands and management units (depending on the tenant's naming hygiene), and unhelpful about anything that's a policy choice (tenant display name, AHT targets, which MUs to include in workforce reports). Treat the auto-discovered values as drafts to confirm, not authorities.
- **The script reads only. It does not write to the tenant.** The only thing it writes to disk is `~/.config/genesys-mcp/tenant.yaml` on the user's machine. Nothing in Genesys is modified.
- **Some probes need extra OAuth scopes**:
  - `presence:presenceDefinition:view` for pre-break presence detection
  - `wfm:managementUnit:view` for management unit listing
  If the role doesn't have these, the script returns a `_error` entry for that section and the interview gracefully skips it. The user can grant these scopes later and rerun.
