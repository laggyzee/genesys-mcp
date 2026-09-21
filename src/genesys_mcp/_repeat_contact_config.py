"""Runtime-editable config for the Repeat Contacts & FCR report (v1.24+).

The report's scope and identity rules are tenant decisions, and consumers
need to change them without a redeploy — so unlike the rest of tenant.yaml
this section is *writable at runtime* via ``set_repeat_contact_config``.

Section name: ``repeatContactReport``. Effective config is layered, lowest
precedence first:

1. Built-in defaults (the model below)
2. A ``repeatContactReport:`` mapping in tenant.yaml, if one exists
   (hand-edited, read-only from the MCP's point of view)
3. The runtime store — a small JSON file written by
   ``set_repeat_contact_config``:
   ``$GENESYS_MCP_REPEAT_CONTACT_CONFIG``, else
   ``<tenant.yaml dir>/repeat-contact-report.json``
4. ``GENESYS_MCP_REPEAT_*`` environment overrides (deployment pins)

Env overrides win over the runtime store on purpose: an operator pinning a
value in the deployment should not have it silently changed from a UI. The
config tools report which keys are env-pinned so a consumer can show them
as locked rather than appearing to ignore a save.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, List, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from genesys_mcp.tenant import default_config_path

logger = logging.getLogger(__name__)

SECTION = "repeatContactReport"
MAX_WINDOW_DAYS = 400
_MESSAGING_KEYS = ("email", "sms", "web_messaging_user", "social")

_write_lock = threading.Lock()


class RepeatContactConfigError(ValueError):
    """Raised when a config layer or an update fails validation."""


class _Strict(BaseModel):
    # Unknown keys are rejected so a typo in an update ("windows_day") fails
    # loudly instead of being silently dropped.
    model_config = ConfigDict(extra="forbid")


class PhoneNormalisation(_Strict):
    format: Literal["E.164"] = "E.164"
    default_country: str = Field(
        default="AU",
        description="ISO 3166-1 alpha-2 country used to expand national-format numbers.",
    )

    @field_validator("default_country")
    @classmethod
    def _country(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if len(v) != 2 or not v.isalpha():
            raise ValueError("default_country must be a 2-letter ISO country code, e.g. 'AU'")
        return v


class IdentityConfig(_Strict):
    mode: Literal["external_contact_first", "raw_only"] = "external_contact_first"
    enable_lookup: bool = Field(
        default=True,
        description="Identity step 2: search External Contacts by raw identifier. Adds API volume.",
    )
    resolve_canonical: bool = Field(
        default=True,
        description="Resolve merged External Contacts to their canonical (surviving) id.",
    )
    lookup_search_fallback: bool = Field(
        default=True,
        description=(
            "When the exact identifier lookup misses, fall back to a contact "
            "search and accept it only if exactly one contact matches."
        ),
    )
    max_lookups_per_run: int = Field(default=5000, ge=0, le=100000)
    phone_normalisation: PhoneNormalisation = Field(default_factory=PhoneNormalisation)
    messaging_keys: List[str] = Field(default_factory=lambda: list(_MESSAGING_KEYS))
    low_coverage_warning_pct: float = Field(
        default=70.0, ge=0.0, le=100.0,
        description=(
            "Warn that the Combined rate is likely understated when the "
            "External Contact share of Combined contacts is below this."
        ),
    )

    @field_validator("messaging_keys")
    @classmethod
    def _keys(cls, v: List[str]) -> List[str]:
        out: list[str] = []
        for item in v:
            key = str(item).strip().lower()
            if key not in _MESSAGING_KEYS:
                raise ValueError(
                    f"unknown messaging key {item!r}; must be one of {list(_MESSAGING_KEYS)}"
                )
            if key not in out:
                out.append(key)
        return out


class MediaTypes(_Strict):
    voice: List[str] = Field(default_factory=lambda: ["voice"])
    messaging: List[str] = Field(
        default_factory=lambda: ["message"],
        description="Genesys mediaType values counted as Messaging (all message subtypes).",
    )

    @field_validator("voice", "messaging")
    @classmethod
    def _media(cls, v: List[str]) -> List[str]:
        out = [str(m).strip().lower() for m in v if str(m).strip()]
        if not out:
            raise ValueError("each channel needs at least one Genesys mediaType")
        return list(dict.fromkeys(out))


class CacheConfig(_Strict):
    enabled: bool = True
    dir: str | None = Field(
        default=None,
        description="Cache directory. Default: $GENESYS_MCP_CACHE_DIR or ~/.cache/genesys-mcp.",
    )
    settled_after_hours: int = Field(
        default=24, ge=0, le=720,
        description="A day's contacts are cached to disk only once the day ended this long ago.",
    )
    retention_days: int = Field(default=120, ge=1, le=800)
    identity_ttl_hours: int = Field(default=24, ge=0, le=720)


class RepeatContactReportConfig(_Strict):
    direction: Literal["inbound", "outbound", "all"] = "inbound"
    include_abandoned: bool = Field(
        default=False,
        description="Include contacts that queued but were never handled by an agent.",
    )
    include_pre_queue: bool = Field(
        default=False,
        description=(
            "Include contacts that ended in the IVR / bot before reaching a "
            "queue or an agent (self-service and pre-queue hang-ups)."
        ),
    )
    media_types: MediaTypes = Field(default_factory=MediaTypes)
    queue_ids: List[str] = Field(default_factory=list, description="Empty = all queues.")
    windows_days: List[int] = Field(default_factory=lambda: [7, 45])
    timezone: str = "Australia/Sydney"
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    drilldown_max_rows: int = Field(default=2000, ge=1, le=50000)
    cache: CacheConfig = Field(default_factory=CacheConfig)

    @field_validator("windows_days", mode="before")
    @classmethod
    def _windows(cls, v: Any) -> list[int]:
        return validate_windows(v)

    @field_validator("queue_ids")
    @classmethod
    def _queues(cls, v: List[str]) -> List[str]:
        return list(dict.fromkeys(str(q).strip() for q in v if str(q).strip()))

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        v = (v or "").strip()
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(f"timezone {v!r} is not a valid IANA zone") from exc
        return v


def validate_windows(value: Any) -> list[int]:
    """Windows must be a non-empty list of positive whole-number days."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError("windows_days must be a list of positive integers, e.g. [7, 45]")
    out: list[int] = []
    for item in value:
        # bool is an int subclass; 7.0 / "7" are rejected rather than coerced
        # so a malformed update can never quietly become a different window.
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"windows_days entries must be positive integers, got {item!r}")
        if item <= 0:
            raise ValueError(f"windows_days entries must be positive integers, got {item!r}")
        if item > MAX_WINDOW_DAYS:
            raise ValueError(f"windows_days entries must be <= {MAX_WINDOW_DAYS}, got {item!r}")
        if item not in out:
            out.append(item)
    if not out:
        raise ValueError("windows_days must contain at least one window")
    return sorted(out)


# ───────────────────────────── layers ─────────────────────────────

def runtime_config_path() -> Path:
    explicit = os.environ.get("GENESYS_MCP_REPEAT_CONTACT_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return default_config_path().parent / "repeat-contact-report.json"


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _tenant_yaml_layer() -> dict[str, Any]:
    path = default_config_path()
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("could not read %s for %s: %s", path, SECTION, exc)
        return {}
    section = raw.get(SECTION) if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else {}


def _runtime_layer() -> dict[str, Any]:
    path = runtime_config_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise RepeatContactConfigError(f"runtime config at {path} is unreadable: {exc}") from exc
    section = raw.get(SECTION) if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else {}


def _env_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"expected a boolean, got {raw!r}")


def _env_list(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _env_int_list(raw: str) -> list[int]:
    try:
        return [int(part) for part in _env_list(raw)]
    except ValueError as exc:
        raise ValueError(f"expected comma-separated integers, got {raw!r}") from exc


# env var suffix → (dotted config path, parser)
_ENV_OVERRIDES: dict[str, tuple[str, Any]] = {
    "DIRECTION": ("direction", str.strip),
    "INCLUDE_ABANDONED": ("include_abandoned", _env_bool),
    "INCLUDE_PRE_QUEUE": ("include_pre_queue", _env_bool),
    "VOICE_MEDIA_TYPES": ("media_types.voice", _env_list),
    "MESSAGING_MEDIA_TYPES": ("media_types.messaging", _env_list),
    "QUEUE_IDS": ("queue_ids", _env_list),
    "WINDOWS_DAYS": ("windows_days", _env_int_list),
    "TIMEZONE": ("timezone", str.strip),
    "IDENTITY_MODE": ("identity.mode", str.strip),
    "IDENTITY_ENABLE_LOOKUP": ("identity.enable_lookup", _env_bool),
    "IDENTITY_RESOLVE_CANONICAL": ("identity.resolve_canonical", _env_bool),
    "IDENTITY_MAX_LOOKUPS": ("identity.max_lookups_per_run", int),
    "PHONE_DEFAULT_COUNTRY": ("identity.phone_normalisation.default_country", str.strip),
    "MESSAGING_KEYS": ("identity.messaging_keys", _env_list),
    "CACHE_ENABLED": ("cache.enabled", _env_bool),
    "CACHE_DIR": ("cache.dir", str.strip),
}
ENV_PREFIX = "GENESYS_MCP_REPEAT_"


def _env_layer() -> tuple[dict[str, Any], list[str]]:
    layer: dict[str, Any] = {}
    pinned: list[str] = []
    for suffix, (dotted, parse) in _ENV_OVERRIDES.items():
        raw = os.environ.get(ENV_PREFIX + suffix)
        if raw is None or raw == "":
            continue
        try:
            value = parse(raw)
        except (ValueError, TypeError) as exc:
            raise RepeatContactConfigError(f"{ENV_PREFIX}{suffix}: {exc}") from exc
        cursor = layer
        parts = dotted.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
        pinned.append(dotted)
    return layer, pinned


def _validate(data: dict[str, Any], *, source: str) -> RepeatContactReportConfig:
    try:
        return RepeatContactReportConfig(**data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or SECTION}: {err['msg']}"
            for err in exc.errors()
        )
        raise RepeatContactConfigError(f"Invalid {SECTION} config ({source}): {details}") from exc


def load_repeat_contact_config() -> tuple[RepeatContactReportConfig, dict[str, Any]]:
    """Return ``(effective_config, meta)``; meta names the layers in play."""
    merged = _deep_merge(_tenant_yaml_layer(), _runtime_layer())
    env_layer, pinned = _env_layer()
    merged = _deep_merge(merged, env_layer)
    config = _validate(merged, source="effective")
    path = runtime_config_path()
    return config, {
        "section": SECTION,
        "runtime_config_path": str(path),
        "runtime_config_exists": path.exists(),
        "env_overridden_keys": sorted(pinned),
        "env_prefix": ENV_PREFIX,
    }


def save_repeat_contact_config(updates: dict[str, Any] | None, *, reset: bool = False) -> None:
    """Deep-merge ``updates`` into the runtime store after validating the result.

    Validation runs on the merged (tenant.yaml + runtime) view *without* env
    overrides, so a stored value is always valid on its own even if an env
    pin is later removed. Nothing is written when validation fails.
    """
    if updates is not None and not isinstance(updates, dict):
        raise RepeatContactConfigError("updates must be an object of config keys to change")
    with _write_lock:
        current = {} if reset else _runtime_layer()
        candidate = _deep_merge(current, updates or {})
        _validate(_deep_merge(_tenant_yaml_layer(), candidate), source="update")

        path = runtime_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({SECTION: candidate}, indent=2, sort_keys=True) + "\n"
        # Write-then-rename so a crash mid-write can't leave a truncated file
        # that would break every later report run.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
