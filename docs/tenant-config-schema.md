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

v1.0 of the schema (current) introduces three concepts to make the file safe to deploy on tenants whose operating model differs from the original built-in assumptions:

- **`schema_version`** — string. Optional; defaults to the installed version. Loader fails loud if the file's version is newer than the code.
- **`operating_model`** — toggles for the three biggest assumptions the skills make (pre-break presence, multi-brand structure, channel coverage).
- **`queues.name_pattern_match_required`** — toggle for tenants whose queue names mostly (not entirely) follow a pattern.

See *v1.0 migration notes* at the bottom of this doc for what changed and how to upgrade a 0.x config.

```yaml
schema_version: "1.0"          # Optional. Loader uses CURRENT_SCHEMA_VERSION when absent
                               # and logs a deprecation warning. Higher-than-current →
                               # hard fail with an "upgrade genesys-mcp" message.

tenant:
  name: string                 # Display name shown in report headlines (e.g. "Acme Contact Centre")
  short_name: string           # Used in filenames; lowercase + hyphens (e.g. "acme")

brands:
  names: list[string]          # Brand display names (case-sensitive; must match what
                               # appears in queue names per queues.name_pattern below)

queues:
  name_pattern: string | null  # Pattern that customer-facing queue names follow.
                               # Supported placeholders: {brand}, {channel}, {function}.
                               # The skill parses each queue name against this pattern
                               # to extract the brand it belongs to.
                               # Example: "{brand} - {channel} - {function}"
                               # Set to `null` for tenants without structured naming —
                               # every queue is then treated as a flat 'function' with
                               # empty brand/channel.
  name_pattern_match_required: bool  # v1.0. Default true (strict — non-matching queues
                               # are skipped silently). Set to false for tenants whose
                               # queues mostly follow the pattern but have legacy
                               # exceptions — non-matching queues fall back to using
                               # the full name as 'function'.
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
                               # specialists. **Required as of v1.0** (no in-code
                               # fallback — too tenant-specific). The workforce table
                               # filters to these roles by default. Auto-discoverable
                               # by genesys-tenant-setup from the active user list.

# v1.0: explicit toggles for tenant operating-model assumptions. Defaults match the
# original built-in assumptions; other tenants override.
operating_model:
  has_pre_break_presence: bool # Default true. When true, presence.pre_break_organisation_presence_id
                               # must be set. When false, all pre-break sections in
                               # reports render a "tracking disabled" callout.
  has_brand_structure: bool    # Default true. When false, brands.names must contain
                               # ≤1 entry. Reports collapse brand × channel grouping
                               # to channel-only (cleaner output for single-brand
                               # tenants).
  expected_channels: list[string]
                               # Default ["voice", "message"]. Headline KPI cards
                               # respect this — a message-only tenant doesn't see
                               # a misleading "voice SL 0%" headline. Allowed
                               # values: "voice", "message", "callback", "email", "chat".

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

# tenant block also accepts a timezone field (added in v0.6) and an optional
# genesys_app_base_url (added in v0.8):
#   tenant:
#     timezone: string         # IANA name (e.g. "Australia/Sydney",
#                              # "America/New_York"). Default "UTC".
#                              # Skills use this to convert period strings
#                              # ("April 2026", "last week") to ISO-8601 UTC
#                              # intervals. genesys-tenant-setup auto-suggests
#                              # from the org's defaultCountryCode.
#     genesys_app_base_url: string | null
#                              # Optional Genesys Cloud app base URL
#                              # (e.g. "https://apps.mypurecloud.com.au").
#                              # When set, conversation ids in HTML reports
#                              # become clickable deep-links to the
#                              # conversation detail view. When None,
#                              # falls back to a GENESYS_REGION env var
#                              # mapping (see src/genesys_mcp/conversation_links.py).
#                              # Set only for custom-domain tenants.

reports:
  output_dir: string           # Where to save generated HTML reports. Supports ~ expansion.
  filename_pattern: string     # Filename pattern. Placeholders:
                               #   {tenant} → tenant.short_name
                               #   {period} → period slug (e.g. "april-2026")

# Optional block — drives the agent_coaching_pack tool and cc-coaching-prep skill.
# All fields default to sensible values if you omit the block entirely.
coaching:
  peer_grouping: string        # 'role' | 'queue' | 'mu'. Default 'role'.
                               # How to auto-resolve the peer set for comparison.
  flagged_call_thresholds:
    sentiment_drop: float      # Negative-sentiment trend magnitude that flags a call. Default 0.5.
    silent_seconds: int        # Transcript silence (s) that flags a call. Default 30.
    aht_excess_pct: float      # Voice-AHT excess (% over target) that flags a call. Default 20.0.
  # v1.0 — cutoffs for the recommended-focus heuristics. Defaults match the pre-v1.0
  # hardcoded values. Tune per tenant operating model (e.g. transfer-heavy retention
  # teams probably want hold_ratio_threshold higher than 0.15).
  heuristics:
    hold_ratio_threshold: float          # Voice hold ratio that flags "Hold time". Default 0.15.
    peer_aht_multiplier: float           # AHT > peer_median × this flags "vs Peers". Default 1.15.
    negative_sentiment_call_threshold: float
                                          # Per-call sentiment that flags negative. Default -0.4.
    hold_ratio_call_threshold: float     # Per-call hold ratio that flags hold. Default 0.3.
    wrap_up_note_rate_threshold: float   # Wrap-up note rate that flags discipline. Default 0.7.
    qa_pass_mark: int                    # QA score below this flags QA. Default 80.
    voice_excess_hours_threshold: float  # Voice excess hours that flags voice AHT. Default 2.0.
    message_excess_hours_threshold: float
                                          # Message excess hours that flags msg AHT. Default 2.0.
  coaching_filename_pattern: string
                               # Coaching-brief filename. Required placeholders:
                               #   {agent_slug} → e.g. "anthony-kha"
                               #   {period} → period slug
                               # Default "coaching-{agent_slug}-{period}.html"

# Optional block — drives the cc-daily-brief skill (v0.7+). All fields default
# to sensible values if you omit the block entirely.
daily_brief:
  comparison_window_days: int  # Rolling baseline window in days. Default 7.
  flag_thresholds:
    sentiment_dip: float       # Sentiment drop magnitude per agent. Default 0.4.
    aht_excess_pct: float      # % over voice-AHT-target per agent. Default 15.
    sl_drop_pp: float          # pp drop in voice SL per queue. Default 10.
  output_filename_pattern: string
                               # Daily-brief filename. Required placeholder:
                               #   {date} → YYYY-MM-DD of target day
                               # Default "daily-brief-{date}.html"
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

## v1.0 migration notes

The v0.x → v1.0 transition formalised the tenant-agnostic posture. If you have an existing v0.x config:

- **No action required for existing v0.x tenants** — every v1.0 default matches the pre-v1.0 hardcoded behaviour. Configs without `schema_version`, `operating_model`, or `coaching.heuristics` load with sensible defaults and log a deprecation warning.
- **`specialist_roles` is now required** — pre-v1.0 it defaulted to `["Specialist", "Customer Service Specialist"]`. If your config relies on that default, add it explicitly.
- **Pre-break presence UUID** — the hardcoded fallback (a tenant-specific UUID) is gone. If `operating_model.has_pre_break_presence: true` (the default), `presence.pre_break_organisation_presence_id` must be set or load fails.
- **AHT / break / meal targets** — no in-code fallbacks; `targets` block now mandatory (it always had sensible defaults at the field level).

Run `python -m genesys_mcp.health_check --strict` to surface any remaining gaps in your v1.0 config.
