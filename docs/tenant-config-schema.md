# Tenant configuration schema

Skills that produce tenant-aware artefacts (e.g. `cc-monthly-report`) read their
tenant-specific knobs from a YAML file rather than hardcoding them. This makes the
skills portable across Genesys Cloud tenants without touching Python.

## Where the file lives

The default location is **`~/.config/genesys-mcp/tenant.yaml`** (respecting `XDG_CONFIG_HOME` if set). Override with the env var `GENESYS_MCP_CONFIG=/some/other/path.yaml`.

The file is **per-user, not per-clone** — it survives `git pull` because it lives outside the repo. It is gitignored in this repo by way of being outside it; never commit a populated tenant config.

## How to populate it

Two options:

1. **Run the setup wizard** — invoke the [`genesys-tenant-setup`](../skills/genesys-tenant-setup/SKILL.md) skill via Claude. It auto-discovers what it can from the read-only MCP and asks you for the rest, then writes the file.
2. **Edit by hand** — copy [`skills/cc-monthly-report/tenant.example.yaml`](../skills/cc-monthly-report/tenant.example.yaml) to `~/.config/genesys-mcp/tenant.yaml` and fill in the values.

## Schema

```yaml
tenant:
  name: string                 # Display name shown in report headlines (e.g. "Acme Contact Centre")
  short_name: string           # Used in filenames; lowercase + hyphens (e.g. "acme")

brands:
  names: list[string]          # Brand display names (case-sensitive; must match what
                               # appears in queue names per queues.name_pattern below)

queues:
  name_pattern: string         # Pattern that customer-facing queue names follow.
                               # Supported placeholders: {brand}, {channel}, {function}.
                               # The skill parses each queue name against this pattern
                               # to extract the brand it belongs to.
                               # Example: "{brand} - {channel} - {function}"
  channels: list[string]       # Channel labels to recognise (e.g. ["Voice", "Chat"]).
                               # Queues whose channel doesn't match are excluded.
  functions: list[string]      # Function labels to recognise. Anything else is grouped as "Other".
  skip_substrings: list[string]  # Skip queues whose name contains any of these substrings.

management_units:
  ids: list[uuid]              # WFM management unit UUIDs to include in workforce reports.
                               # Auto-discoverable: pick MUs whose member-list overlaps
                               # with your customer-facing agents.

business_unit:
  id: uuid                     # WFM business unit UUID. The MUs above should belong to this BU.

presence:
  pre_break_organisation_presence_id: uuid
                               # Org-level "Pre Break" / drain presence used for
                               # break-overrun analysis. Auto-detectable by name
                               # match (looks for "Pre Break", "Drain", "Wind Down").

specialist_roles:
  list[string]                 # User role names that identify customer-facing
                               # specialists. The workforce table filters to these
                               # roles by default. Auto-discoverable from the role list.

targets:
  voice_aht_s: int             # Voice Average Handle Time target (total tHandle in
                               # seconds; matches Genesys "Performance" UI).
  message_aht_s: int           # Message AHT target.
  acw_s: int                   # After-call work target.
  pre_break_min: int           # Pre-break drain window in minutes — agents auto-set
                               # to "Pre Break" presence this many minutes before
                               # scheduled breaks. Going past this is wasted handle time.
  fte_hours_per_month: int     # Productive handle hours per FTE per month.
                               # Default 160 (40h/wk × 4 wks × ~0.85 occupancy ≈ 136h, rounded up).

reports:
  output_dir: string           # Where to save generated HTML reports. Supports ~ expansion.
  filename_pattern: string     # Filename pattern. Placeholders:
                               #   {tenant} → tenant.short_name
                               #   {period} → period slug (e.g. "april-2026")
```

## Validation

The loader (`genesys_mcp.tenant.load_config`) validates the file against a Pydantic model on load. Missing required fields, malformed UUIDs, or unknown keys raise a `TenantConfigError` with the offending path so you can fix the file by hand.

## Example

A fully-filled-in generic example lives at [`skills/cc-monthly-report/tenant.example.yaml`](../skills/cc-monthly-report/tenant.example.yaml). Copy it to `~/.config/genesys-mcp/tenant.yaml` to start.

## What's NOT in the config

Things deliberately kept hardcoded in scripts because they're either tenant-agnostic or carry safety implications:

- OAuth credentials (live in `~/.config/genesys-mcp.env` or shell env)
- Genesys region (`GENESYS_REGION` env var)
- The provisioning script's write-only OAuth client (`GENESYS_WRITE_CLIENT_ID/SECRET`)
- The list of write permissions a custom OAuth role needs (documented in [`scripts/README.md`](../scripts/README.md))
