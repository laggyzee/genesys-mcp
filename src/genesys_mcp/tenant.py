"""Tenant configuration loader for Genesys-aware skills.

Skills like `cc-monthly-report` read their tenant-specific knobs (brand list,
AHT targets, WFM MU ids, pre-break presence id, etc.) from a single YAML file
rather than hardcoding them. This module owns the schema, the loader, and the
default file-resolution rules.

The schema lives in [docs/tenant-config-schema.md](../../docs/tenant-config-schema.md);
the canonical generic example is in
[skills/cc-monthly-report/tenant.example.yaml](../../skills/cc-monthly-report/tenant.example.yaml).

Resolution order (first hit wins):

1. ``$GENESYS_MCP_CONFIG`` env var (absolute or `~`-expanded path)
2. ``$XDG_CONFIG_HOME/genesys-mcp/tenant.yaml`` (if XDG_CONFIG_HOME is set)
3. ``~/.config/genesys-mcp/tenant.yaml``
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class TenantConfigError(RuntimeError):
    """Raised when the tenant config can't be loaded or fails validation."""


class _Tenant(BaseModel):
    name: str = Field(..., description="Display name shown in report headlines.")
    short_name: str = Field(..., description="Used in filenames; lowercase + hyphens.")
    timezone: str = Field(
        default="UTC",
        description=(
            "IANA timezone name (e.g. 'Australia/Sydney', 'America/New_York'). "
            "Skills use this to convert period strings ('April 2026', 'last week') "
            "into ISO-8601 UTC intervals. Default UTC keeps existing behaviour "
            "for tenants that omit the field."
        ),
    )
    genesys_app_base_url: str | None = Field(
        default=None,
        description=(
            "Optional Genesys Cloud app base URL (e.g. "
            "'https://apps.mypurecloud.com.au'). When set, every conversation "
            "id in generated HTML reports becomes a clickable deep-link to "
            "the conversation detail view. When unset, falls back to the "
            "GENESYS_REGION env var → app-host mapping in conversation_links.py. "
            "Set explicitly only for tenants with custom domains."
        ),
    )

    @field_validator("short_name")
    @classmethod
    def _validate_short_name(cls, v: str) -> str:
        if not v or any(c in v for c in " /\\"):
            raise ValueError(
                "short_name must be lowercase with hyphens (no spaces or slashes), "
                "e.g. 'acme' or 'acme-cc'"
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        # Defer the actual zone resolution to zoneinfo at use-site; here we
        # just guard against obvious typos like leading/trailing whitespace.
        v = v.strip()
        if not v:
            return "UTC"
        # Basic shape: 'Area/Location' or 'UTC'
        if v != "UTC" and "/" not in v:
            raise ValueError(
                f"tenant.timezone {v!r} doesn't look like an IANA zone "
                "(e.g. 'Australia/Sydney', 'America/New_York'). Use 'UTC' "
                "as a safe default if unsure."
            )
        return v


class _Brands(BaseModel):
    names: List[str] = Field(
        ..., min_length=1,
        description="Brand display names — must match queue-name `{brand}` placeholder.",
    )


class _Queues(BaseModel):
    name_pattern: str | None = Field(
        default="{brand} - {channel} - {function}",
        description=(
            "Pattern customer-facing queue names follow. Supports {brand}, "
            "{channel}, {function}. Set to ``null`` for tenants with no "
            "structured naming — the skills then render flat per-queue lists "
            "with no brand/channel parsing."
        ),
    )
    name_pattern_match_required: bool = Field(
        default=True,
        description=(
            "When True (default), queues not matching ``name_pattern`` are "
            "skipped silently. When False, non-matching queues fall back to "
            "using the full queue name as ``function`` (with empty brand/"
            "channel). Useful for tenants that mostly follow the pattern "
            "but have some legacy queues that don't."
        ),
    )
    channels: List[str] = Field(default_factory=lambda: ["Voice", "Chat"])
    functions: List[str] = Field(
        default_factory=lambda: [
            "Activation", "Billing", "Complaints", "General",
            "Retention", "Sales", "Technical Support",
        ],
    )
    skip_substrings: List[str] = Field(
        default_factory=lambda: ["Holding", "Internal", "Outbound Email", "ZZZ_"],
        description="Queues whose name contains any of these substrings are excluded.",
    )

    @field_validator("name_pattern")
    @classmethod
    def _validate_name_pattern(cls, v: str | None) -> str | None:
        # ``null`` (None) is legal — tenants with no structured naming skip
        # brand/channel parsing entirely. Otherwise {brand} is required.
        if v is None:
            return v
        if "{brand}" not in v:
            raise ValueError(
                "queues.name_pattern must contain at least the {brand} "
                "placeholder, or be set to null to disable pattern parsing"
            )
        return v


class _ManagementUnits(BaseModel):
    ids: List[str] = Field(
        default_factory=list,
        description="WFM management unit UUIDs. Empty = auto-discover at runtime.",
    )


class _BusinessUnit(BaseModel):
    id: str | None = Field(
        default=None,
        description="WFM business unit UUID. Optional; auto-discovered from MUs if absent.",
    )


class _Presence(BaseModel):
    pre_break_organisation_presence_id: str | None = Field(
        default=None,
        description="Org-level 'Pre Break' presence UUID. Optional; tools that need it warn if absent.",
    )


class _OperatingModel(BaseModel):
    """High-level toggles for tenant operating-model assumptions.

    The skills' default reporting shape assumes a familiar CC operating
    model: brand-structured queues, pre-break-presence-as-drain, multi-channel
    voice + message. Other Genesys deployers fit different shapes — this
    block lets the skills cleanly degrade rather than silently produce
    misleading sections.
    """

    has_pre_break_presence: bool = Field(
        default=True,
        description=(
            "Whether the tenant uses an org-level 'Pre Break' presence as "
            "a drain state before scheduled breaks. When False, reports "
            "omit the pre-break overrun sections (rendering 'Pre-break "
            "tracking disabled for this tenant' instead of zero-valued "
            "rows). When True, ``presence.pre_break_organisation_presence_id`` "
            "must be set."
        ),
    )
    has_brand_structure: bool = Field(
        default=True,
        description=(
            "Whether the tenant has multiple brands sharing the same CC "
            "infrastructure. When False, the monthly report's brand × channel "
            "funnel collapses to channel-only — 'All queues' instead of "
            "per-brand rows. Single-brand tenants get a cleaner report."
        ),
    )
    expected_channels: List[str] = Field(
        default_factory=lambda: ["voice", "message"],
        description=(
            "Channels the tenant actually operates. Tools that compute "
            "headline KPIs respect this list — a message-only tenant won't "
            "see a 'voice SL 0%' headline. Values are lowercase Genesys "
            "media-type strings: 'voice', 'message', 'callback', 'email'."
        ),
    )

    @field_validator("expected_channels")
    @classmethod
    def _normalise_channels(cls, v: List[str]) -> List[str]:
        # Genesys uses lowercase media types ('voice', 'message', ...);
        # accept the common variants but normalise to canonical form.
        valid = {"voice", "message", "callback", "email", "chat"}
        out = []
        for ch in v:
            normalised = ch.strip().lower()
            if normalised not in valid:
                raise ValueError(
                    f"operating_model.expected_channels: unknown channel "
                    f"{ch!r}. Must be one of {sorted(valid)}."
                )
            out.append(normalised)
        if not out:
            raise ValueError(
                "operating_model.expected_channels must contain at least one channel"
            )
        return out


class _Survey(BaseModel):
    """Post-call / post-interaction survey attribute keys (v1.8).

    Survey responses (NPS, agent rating, experience rating, etc.) land on
    conversation participants as ``attributes: {key: value}``. The exact
    key names vary by tenant — this org calls them ``"NPS Score"``,
    ``"Agent Score"``, ``"Experience Score"``; others might use
    ``"nps_score"`` / ``"csat"`` / whatever the survey integration writes.

    When ``nps_attribute_key`` is set, callers (and v1.9+ skills) can
    surface the NPS rollup automatically via the v1.8
    ``search_conversations_by_attribute`` tool. When ``None``, sections
    that depend on NPS are silently omitted (graceful degradation, like
    ``operating_model``).
    """

    nps_attribute_key: str | None = Field(
        default=None,
        description=(
            "The exact participant-attribute key (case + spaces) your "
            "tenant uses to store the customer's NPS score. Typically "
            "an integer 0-10. Example: 'NPS Score'. Leave None to opt out."
        ),
    )
    agent_score_attribute_key: str | None = Field(
        default=None,
        description=(
            "The participant-attribute key for the agent-rating score "
            "from your post-call survey. Example: 'Agent Score'. Optional."
        ),
    )
    experience_score_attribute_key: str | None = Field(
        default=None,
        description=(
            "The participant-attribute key for the customer-experience "
            "score. Example: 'Experience Score'. Optional."
        ),
    )


class _Targets(BaseModel):
    voice_aht_s: int = Field(default=285, ge=1, description="Voice AHT target in seconds.")
    message_aht_s: int = Field(default=660, ge=1, description="Message AHT target in seconds.")
    acw_s: int = Field(default=15, ge=1, description="After-call work target in seconds.")
    pre_break_min: int = Field(default=10, ge=1, description="Pre-break drain target in minutes.")
    fte_hours_per_month: int = Field(
        default=160, ge=1,
        description="Productive handle hours per FTE per month (40h/wk × 4 wks × ~0.85 occupancy).",
    )


class _Reports(BaseModel):
    output_dir: str = Field(default="~/Documents")
    filename_pattern: str = Field(default="{tenant}-CC-{period}.html")

    @field_validator("filename_pattern")
    @classmethod
    def _validate_filename(cls, v: str) -> str:
        if "{tenant}" not in v or "{period}" not in v:
            raise ValueError("reports.filename_pattern must contain {tenant} and {period}")
        return v


class _CoachingThresholds(BaseModel):
    sentiment_drop: float = Field(
        default=0.5, ge=0.0, le=2.0,
        description="Minimum negative-sentiment delta on a call to flag it for review.",
    )
    silent_seconds: int = Field(
        default=30, ge=1,
        description="Continuous silence on transcript above this duration flags the call.",
    )
    aht_excess_pct: float = Field(
        default=20.0, ge=0.0,
        description="% over AHT target on a single call that flags it for review.",
    )


class _DailyBriefThresholds(BaseModel):
    sentiment_dip: float = Field(
        default=0.4, ge=0.0, le=2.0,
        description="Avg-sentiment drop magnitude per agent that flags them in the daily brief.",
    )
    aht_excess_pct: float = Field(
        default=15.0, ge=0.0,
        description="% over voice-AHT-target per agent that flags them.",
    )
    sl_drop_pp: float = Field(
        default=10.0, ge=0.0,
        description="Percentage-point drop in voice SL vs the rolling median that flags a queue.",
    )


class _DailyBrief(BaseModel):
    """Knobs for the ``cc-daily-brief`` skill (v0.7).

    All defaults are sane; tenants tighten or loosen as needed.
    """

    comparison_window_days: int = Field(
        default=7, ge=2, le=28,
        description=(
            "Rolling-median lookback window for KPI comparison (excluding "
            "the target day itself)."
        ),
    )
    flag_thresholds: _DailyBriefThresholds = Field(
        default_factory=_DailyBriefThresholds,
    )
    output_filename_pattern: str = Field(
        default="daily-brief-{date}.html",
        description=(
            "Daily-brief output filename. {date} is required and resolves "
            "to the brief's target date (YYYY-MM-DD format)."
        ),
    )

    @field_validator("output_filename_pattern")
    @classmethod
    def _validate_filename(cls, v: str) -> str:
        if "{date}" not in v:
            raise ValueError(
                "daily_brief.output_filename_pattern must contain {date}"
            )
        return v


class _CoachingHeuristics(BaseModel):
    """Numeric cutoffs for the recommended-focus + per-call flagging heuristics.

    These were hardcoded in ``coaching.py`` pre-v1.0. Moving them to
    tenant.yaml lets sales-heavy or message-only deployments tune the
    flags without forking the code — e.g. a transfer-heavy retention
    team probably wants `hold_ratio_threshold` higher than 0.15.
    """

    hold_ratio_threshold: float = Field(
        default=0.15, ge=0.0, le=1.0,
        description="Voice hold ratio above this flags 'Hold time' as a coaching focus.",
    )
    peer_aht_multiplier: float = Field(
        default=1.15, ge=1.0,
        description="Agent voice AHT > peer_median × this multiplier flags 'vs Peers — voice handle'.",
    )
    negative_sentiment_call_threshold: float = Field(
        default=-0.4, le=0.0,
        description="Per-call sentiment at or below this flags the call as negative.",
    )
    hold_ratio_call_threshold: float = Field(
        default=0.3, ge=0.0, le=1.0,
        description="Per-call hold ratio above this flags the call for review.",
    )
    wrap_up_note_rate_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0,
        description="Agent wrap-up note rate below this flags 'Wrap-up discipline'.",
    )
    qa_pass_mark: int = Field(
        default=80, ge=0, le=100,
        description="QA avg score below this flags 'QA score' as a coaching focus.",
    )
    voice_excess_hours_threshold: float = Field(
        default=2.0, ge=0.0,
        description="Voice AHT excess hours per period above this flags 'Voice AHT' as coachable.",
    )
    message_excess_hours_threshold: float = Field(
        default=2.0, ge=0.0,
        description="Message AHT excess hours per period above this flags 'Message AHT' as coachable.",
    )


class _Coaching(BaseModel):
    """Knobs for ``cc-coaching-prep`` and ``agent_coaching_pack``.

    All defaults are sane for a generic CC; tenants tighten or loosen as needed.
    """

    peer_grouping: str = Field(
        default="role",
        description=(
            "How to auto-resolve the peer set for comparison: 'role' (same "
            "specialist role + same management unit), 'queue' (same primary "
            "queue), or 'mu' (same management unit only)."
        ),
    )
    flagged_call_thresholds: _CoachingThresholds = Field(
        default_factory=_CoachingThresholds,
    )
    heuristics: _CoachingHeuristics = Field(
        default_factory=_CoachingHeuristics,
        description=(
            "Numeric cutoffs for the recommended-focus + per-call flagging "
            "heuristics. Defaults are sane for inbound-heavy CCs; sales/"
            "retention teams may want higher hold_ratio / lower QA pass mark."
        ),
    )
    coaching_filename_pattern: str = Field(
        default="coaching-{agent_slug}-{period}.html",
        description="Coaching-prep skill output filename. Supports {agent_slug} and {period}.",
    )

    @field_validator("peer_grouping")
    @classmethod
    def _validate_peer_grouping(cls, v: str) -> str:
        if v not in ("role", "queue", "mu"):
            raise ValueError(
                "coaching.peer_grouping must be one of 'role', 'queue', or 'mu'"
            )
        return v

    @field_validator("coaching_filename_pattern")
    @classmethod
    def _validate_coaching_filename(cls, v: str) -> str:
        if "{agent_slug}" not in v or "{period}" not in v:
            raise ValueError(
                "coaching.coaching_filename_pattern must contain {agent_slug} and {period}"
            )
        return v


# Current tenant.yaml schema version. Bumped on breaking changes.
# 0.x: pre-versioning era (legacy fields like in-code specialist_roles defaults).
# 1.0: v1.0 release — operating_model block, required specialist_roles,
#      coaching.heuristics block, no in-code AHT/role fallbacks.
CURRENT_SCHEMA_VERSION = "1.0"


class TenantConfig(BaseModel):
    """Validated tenant config — the single source of truth for skills."""

    schema_version: str = Field(
        default=CURRENT_SCHEMA_VERSION,
        description=(
            "Tenant config schema version. Optional in tenant.yaml — when "
            "absent, the loader assumes the file pre-dates versioning (0.x) "
            "and logs a deprecation warning. When set to a version newer "
            "than the installed code supports, the loader hard-fails with "
            "an 'upgrade genesys-mcp' message rather than silently misreading."
        ),
    )
    tenant: _Tenant
    brands: _Brands
    queues: _Queues = Field(default_factory=_Queues)
    management_units: _ManagementUnits = Field(default_factory=_ManagementUnits)
    business_unit: _BusinessUnit = Field(default_factory=_BusinessUnit)
    presence: _Presence = Field(default_factory=_Presence)
    specialist_roles: List[str] = Field(
        ..., min_length=1,
        description=(
            "Role names identifying customer-facing specialists. Required — "
            "tenants vary widely on title conventions ('Specialist', "
            "'Customer Service Specialist', 'Agent Level 1', 'CSR', etc.). "
            "The genesys-tenant-setup skill auto-discovers these from your "
            "active user list."
        ),
    )
    targets: _Targets = Field(default_factory=_Targets)
    reports: _Reports = Field(default_factory=_Reports)
    coaching: _Coaching = Field(default_factory=_Coaching)
    daily_brief: _DailyBrief = Field(default_factory=_DailyBrief)
    operating_model: _OperatingModel = Field(
        default_factory=_OperatingModel,
        description=(
            "Toggles for tenant operating-model assumptions. Defaults assume "
            "the built-in shape (multi-brand, pre-break presence, voice "
            "+ message). Other tenants override per their shape."
        ),
    )
    survey: _Survey = Field(
        default_factory=_Survey,
        description=(
            "Post-call / post-interaction survey attribute keys (v1.8). "
            "Set the per-attribute keys your tenant uses to opt into "
            "automatic NPS / agent-score / experience-score surfacing in "
            "the daily-brief and monthly-report skills. All fields default "
            "to None — leaving the block out entirely is the same as "
            "opting out (sections silently omitted, no broken renders)."
        ),
    )

    @model_validator(mode="after")
    def _validate_operating_model_consistency(self) -> "TenantConfig":
        """Pre-break-enabled tenants must supply the presence UUID."""
        if (
            self.operating_model.has_pre_break_presence
            and not self.presence.pre_break_organisation_presence_id
        ):
            raise ValueError(
                "operating_model.has_pre_break_presence is True but "
                "presence.pre_break_organisation_presence_id is unset. "
                "Either set the presence id (run genesys-tenant-setup, which "
                "auto-discovers it) or set has_pre_break_presence: false."
            )
        if not self.operating_model.has_brand_structure and len(self.brands.names) > 1:
            raise ValueError(
                "operating_model.has_brand_structure is False but "
                f"brands.names lists {len(self.brands.names)} brands "
                f"({self.brands.names}). Either set has_brand_structure: true "
                "or trim brands.names to a single entry."
            )
        return self

    # Convenience accessors used by skills.
    def report_output_path(self, period_slug: str) -> Path:
        """Resolve {tenant}/{period} placeholders in reports.filename_pattern.

        Returns an absolute, ``~``-expanded path.
        """
        filename = self.reports.filename_pattern.format(
            tenant=self.tenant.short_name, period=period_slug,
        )
        return Path(self.reports.output_dir).expanduser() / filename

    def daily_brief_output_path(self, date_slug: str) -> Path:
        """Resolve {date} placeholder for cc-daily-brief output.

        Drops into the same ``reports.output_dir`` as the other report skills.
        ``date_slug`` should be ``YYYY-MM-DD`` (the brief's target day).
        """
        filename = self.daily_brief.output_filename_pattern.format(date=date_slug)
        return Path(self.reports.output_dir).expanduser() / filename

    def coaching_output_path(self, agent_slug: str, period_slug: str) -> Path:
        """Resolve {agent_slug}/{period} placeholders for cc-coaching-prep output.

        Drops into the same ``reports.output_dir`` as the monthly report — one
        documents folder per tenant rather than two.
        """
        filename = self.coaching.coaching_filename_pattern.format(
            agent_slug=agent_slug, period=period_slug,
        )
        return Path(self.reports.output_dir).expanduser() / filename


def default_config_path() -> Path:
    """Resolve where the config file should live by default.

    Honours, in order: ``$GENESYS_MCP_CONFIG``, ``$XDG_CONFIG_HOME/genesys-mcp/``,
    or ``~/.config/genesys-mcp/``. Returns the path even if the file doesn't yet
    exist (callers test for existence themselves).
    """
    explicit = os.environ.get("GENESYS_MCP_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(config_home).expanduser() / "genesys-mcp" / "tenant.yaml"


def _parse_version_tuple(v: str) -> tuple[int, ...]:
    """Parse a 'major.minor[.patch]' string to a comparable tuple."""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        raise TenantConfigError(
            f"schema_version {v!r} is not a valid 'major.minor' string"
        )


def _check_schema_version(raw: dict, path: Path) -> None:
    """Validate the schema_version field against the installed code.

    Three outcomes:

    - Missing version (pre-1.0 config) → log a deprecation warning, accept.
    - Higher version (config written by newer genesys-mcp) → hard fail.
    - Lower version → accept (the loader applies defaults for new fields).
    """
    import logging

    log = logging.getLogger(__name__)
    raw_version = raw.get("schema_version")
    if raw_version is None:
        log.warning(
            "Tenant config at %s has no schema_version. Assuming pre-1.0 "
            "(0.x). The config will load with v1.0 defaults applied for any "
            "fields it omits — but consider running genesys-tenant-setup "
            "to migrate it cleanly to the v1.0 shape.",
            path,
        )
        return

    config_v = _parse_version_tuple(str(raw_version))
    current_v = _parse_version_tuple(CURRENT_SCHEMA_VERSION)
    if config_v > current_v:
        raise TenantConfigError(
            f"Tenant config at {path} uses schema {raw_version}, but the "
            f"installed genesys-mcp only supports up to {CURRENT_SCHEMA_VERSION}. "
            f"Upgrade with `cd ~/code/genesys-mcp && git pull && uv sync`, "
            f"or pin tenant.yaml to a supported schema version."
        )


def load_config(path: Path | str | None = None) -> TenantConfig:
    """Load + validate the tenant config. Raises ``TenantConfigError`` on failure.

    ``path`` defaults to :func:`default_config_path`. Pass a different path to
    load from a non-default location (useful for testing or per-tenant configs).
    """
    if path is None:
        path = default_config_path()
    path = Path(path).expanduser()

    if not path.exists():
        raise TenantConfigError(
            f"No tenant config at {path}. Either:\n"
            f"  - Run the genesys-tenant-setup skill to generate one (auto-discovers + interviews), or\n"
            f"  - Copy skills/cc-monthly-report/tenant.example.yaml to {path} and edit by hand.\n"
            f"  - See docs/tenant-config-schema.md for the full schema."
        )

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise TenantConfigError(f"YAML parse error in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise TenantConfigError(
            f"Tenant config at {path} must be a YAML mapping at the top level, "
            f"got {type(raw).__name__}"
        )

    _check_schema_version(raw, path)

    try:
        return TenantConfig(**raw)
    except ValidationError as exc:
        # Pydantic's error display is verbose but precise; surface the path-by-path errors.
        details = "\n".join(
            f"  - {'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        raise TenantConfigError(
            f"Tenant config at {path} failed validation:\n{details}\n"
            f"See docs/tenant-config-schema.md for the schema."
        ) from exc


def dump_config(config: TenantConfig, path: Path | str) -> Path:
    """Write a TenantConfig to YAML, creating parent directories if needed.

    Used by the genesys-tenant-setup skill to persist the interview output.
    Returns the resolved path that was written.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json", exclude_none=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    return path
