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
from pydantic import BaseModel, Field, ValidationError, field_validator


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
    name_pattern: str = Field(
        default="{brand} - {channel} - {function}",
        description="Pattern customer-facing queue names follow. Supports {brand}, {channel}, {function}.",
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
    def _validate_name_pattern(cls, v: str) -> str:
        if "{brand}" not in v:
            raise ValueError("queues.name_pattern must contain at least the {brand} placeholder")
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


class TenantConfig(BaseModel):
    """Validated tenant config — the single source of truth for skills."""

    tenant: _Tenant
    brands: _Brands
    queues: _Queues = Field(default_factory=_Queues)
    management_units: _ManagementUnits = Field(default_factory=_ManagementUnits)
    business_unit: _BusinessUnit = Field(default_factory=_BusinessUnit)
    presence: _Presence = Field(default_factory=_Presence)
    specialist_roles: List[str] = Field(
        default_factory=lambda: ["Specialist", "Customer Service Specialist"],
        description="Role names identifying customer-facing specialists.",
    )
    targets: _Targets = Field(default_factory=_Targets)
    reports: _Reports = Field(default_factory=_Reports)
    coaching: _Coaching = Field(default_factory=_Coaching)

    # Convenience accessors used by skills.
    def report_output_path(self, period_slug: str) -> Path:
        """Resolve {tenant}/{period} placeholders in reports.filename_pattern.

        Returns an absolute, ``~``-expanded path.
        """
        filename = self.reports.filename_pattern.format(
            tenant=self.tenant.short_name, period=period_slug,
        )
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
