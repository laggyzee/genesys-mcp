"""Repeat Contacts & FCR report + its runtime config tools (v1.24+).

``get_repeat_contact_report`` answers *"what share of contacts were repeat
contacts, per channel and per N-day window, and what is the derived FCR?"*

- Source: the conversation-detail archive, walked one local-timezone day at
  a time via ``fetch_conversation_details`` (async jobs once the archive
  watermark covers the day, validated synchronous paging before that). Every
  day is fully paged, de-duplicated by ``conversationId`` and reduced to a
  slim :class:`Contact` immediately — a 45-day lookback is ~50 day-slices
  and raw detail rows are ~15 KB each.
- Identity: External Contact link → canonical (merge) resolution →
  identifier lookup → normalised raw identifier → unidentified.
- Settled day-slices and External Contact resolutions are cached on disk so
  adjacent periods (and re-runs) don't refetch the lookback. The cache holds
  customer identifiers, so it is written 0600 under a 0700 directory and
  pruned after ``cache.retention_days``.

All calculation lives in ``genesys_mcp._repeat_contacts`` (pure, tested);
config layering lives in ``genesys_mcp._repeat_contact_config``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import PureCloudPlatformClientV2 as gc
from PureCloudPlatformClientV2.rest import ApiException
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._conversation_details import fetch_conversation_details
from genesys_mcp._envelopes import soft_fail_envelope
from genesys_mcp._intervals import now_utc as _now_utc
from genesys_mcp._repeat_contact_config import (
    RepeatContactConfigError,
    RepeatContactReportConfig,
    load_repeat_contact_config,
    save_repeat_contact_config,
    validate_windows,
)
from genesys_mcp._repeat_contacts import (
    CHANNELS,
    FCR_LABEL,
    METHODOLOGY,
    Contact,
    build_report,
    dedupe_contacts,
    in_scope,
    resolve_identities,
    slim_conversation,
)
from genesys_mcp.client import get_api, to_dict, with_retry
from genesys_mcp.conversation_links import resolve_app_base_url
from genesys_mcp.naming import resolver

logger = logging.getLogger(__name__)

_CACHE_SCHEMA = 1
_MAX_PERIOD_DAYS = 400
_DAY_MAX_PAGES = 500  # sync pages are 100 rows, job pages 1000
_FETCH_WORKERS = 3
_BULK_CONTACT_BATCH = 50
_MIN_CALL_INTERVAL_S = 0.22  # ~270 req/min, under the 300/min client limit

_IDENTIFIER_TYPES = {
    "phone": "Phone",
    "email": "Email",
    "social:whatsapp": "SocialWhatsapp",
    "social:facebook": "SocialFacebook",
    "social:twitter": "SocialTwitter",
    "social:line": "SocialLine",
    "social:instagram": "SocialInstagram",
}


class _Throttle:
    """Space External Contacts calls so a cold run can't trip the rate limit."""

    def __init__(self, min_interval: float) -> None:
        self._min = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._min
        if delay > 0:
            time.sleep(delay)


# ───────────────────────────── cache ─────────────────────────────

def _cache_root(config: RepeatContactReportConfig) -> Path | None:
    if not config.cache.enabled:
        return None
    base = (
        config.cache.dir
        or os.environ.get("GENESYS_MCP_CACHE_DIR")
        or os.path.join(os.environ.get("XDG_CACHE_HOME") or "~/.cache", "genesys-mcp")
    )
    # Namespace by OAuth client + region so two tenants sharing a cache dir
    # can never read each other's contacts.
    tenant = hashlib.sha256(
        f"{os.environ.get('GENESYS_CLIENT_ID', '')}|{os.environ.get('GENESYS_REGION', '')}".encode()
    ).hexdigest()[:16]
    root = Path(base).expanduser() / "repeat-contacts" / tenant
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        logger.warning("repeat-contact cache disabled (cannot create %s): %s", root, exc)
        return None
    return root


def _read_cache(path: Path) -> Any | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError) as exc:
        logger.warning("ignoring unreadable cache file %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or data.get("schema") != _CACHE_SCHEMA:
        return None
    return data


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as handle:
            json.dump({"schema": _CACHE_SCHEMA, **payload}, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("could not write cache file %s: %s", path, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _prune_cache(root: Path, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    for entry in root.glob("day-*.json.gz"):
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


# ───────────────────────────── conversations ─────────────────────────────

def _details_body(interval: str, config: RepeatContactReportConfig) -> dict[str, Any]:
    media = list(dict.fromkeys(config.media_types.voice + config.media_types.messaging))
    body: dict[str, Any] = {
        "interval": interval,
        "order": "asc",
        "orderBy": "conversationStart",
        "segmentFilters": [{
            "type": "or",
            "predicates": [
                {"type": "dimension", "dimension": "mediaType", "operator": "matches", "value": m}
                for m in media
            ],
        }],
    }
    if config.direction != "all":
        body["conversationFilters"] = [{
            "type": "and",
            "predicates": [{
                "type": "dimension", "dimension": "originatingDirection",
                "operator": "matches", "value": config.direction,
            }],
        }]
    return body


def _utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fetch_day(
    day: date, zone: ZoneInfo, config: RepeatContactReportConfig, cache_root: Path | None,
) -> tuple[list[Contact], dict[str, Any]]:
    """All in-direction voice + messaging contacts that *started* on ``day``.

    The slice is deliberately unfiltered by queue / abandoned so one cached
    day serves every scope; scope is applied in memory afterwards.
    """
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    scope_hash = hashlib.sha256(json.dumps({
        "tz": config.timezone, "direction": config.direction,
        "voice": sorted(config.media_types.voice),
        "messaging": sorted(config.media_types.messaging),
    }, sort_keys=True).encode()).hexdigest()[:12]
    cache_path = cache_root / f"day-{day.isoformat()}-{scope_hash}.json.gz" if cache_root else None

    if cache_path is not None:
        cached = _read_cache(cache_path)
        if cached is not None:
            contacts = [Contact.from_record(r) for r in cached.get("contacts") or []]
            return contacts, {"date": day.isoformat(), "source": "cache", "data_complete": True,
                              "contacts": len(contacts)}

    detail = fetch_conversation_details(
        _details_body(f"{_utc_z(start)}/{_utc_z(end)}", config),
        max_pages=_DAY_MAX_PAGES, use_cache=False,
    )
    contacts = []
    for conv in detail["conversations"]:
        contact = slim_conversation(
            conv,
            voice_media=config.media_types.voice,
            messaging_media=config.media_types.messaging,
        )
        # The interval filter matches any conversation *active* in the day;
        # a contact belongs to the day it started, so drop the overhang.
        if contact is not None and start <= contact.start < end:
            contacts.append(contact)
    contacts = dedupe_contacts(contacts)

    settled = end + timedelta(hours=config.cache.settled_after_hours) <= _now_utc()
    complete = bool(detail["data_complete"])
    if cache_path is not None and settled and complete and not detail["data_provisional"]:
        _write_cache(cache_path, {
            "date": day.isoformat(), "fetched_at": _utc_z(_now_utc()),
            "contacts": [c.to_record() for c in contacts],
        })
    return contacts, {
        "date": day.isoformat(), "source": detail["data_source"], "data_complete": complete,
        "data_provisional": bool(detail["data_provisional"]), "contacts": len(contacts),
    }


def _load_contacts(
    first_day: date, last_day: date, zone: ZoneInfo,
    config: RepeatContactReportConfig, cache_root: Path | None,
) -> tuple[list[Contact], list[dict[str, Any]]]:
    days = [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        fetched = list(pool.map(lambda d: _fetch_day(d, zone, config, cache_root), days))
    contacts = dedupe_contacts(c for day_contacts, _meta in fetched for c in day_contacts)
    return contacts, [meta for _contacts, meta in fetched]


# ───────────────────────────── External Contacts ─────────────────────────────

class _ExternalContacts:
    """Canonical (merge) resolution + identifier lookup, batched and cached."""

    def __init__(self, config: RepeatContactReportConfig, cache_root: Path | None) -> None:
        self._config = config
        self._path = cache_root / "identity.json.gz" if cache_root else None
        self._ttl = config.cache.identity_ttl_hours * 3600
        self._throttle = _Throttle(_MIN_CALL_INTERVAL_S)
        self._lock = threading.Lock()
        self._canonical: dict[str, list[Any]] = {}  # id → [canonical_id, epoch]
        self._lookups: dict[str, list[Any]] = {}  # "kind|value" → [contact_id|None, epoch]
        self._dirty = False
        self.unavailable_reason: str | None = None
        self.api_calls = 0
        if self._path is not None:
            cached = _read_cache(self._path) or {}
            self._canonical = cached.get("canonical") or {}
            self._lookups = cached.get("lookups") or {}

    def _fresh(self, entry: list[Any] | None) -> bool:
        return bool(entry) and (time.time() - float(entry[1])) < self._ttl

    def _fail(self, exc: ApiException, action: str) -> None:
        status = int(getattr(exc, "status", 0) or 0)
        if status in (401, 403):
            self.unavailable_reason = (
                f"External Contacts {action} was refused (HTTP {status}). Grant the OAuth "
                "client 'externalContacts:contact:view' (external-contacts:readonly) to "
                "enable merge resolution and identifier lookup."
            )
        logger.warning("external contacts %s failed: HTTP %s", action, status)

    @staticmethod
    def _canonical_of(entity: dict[str, Any], fallback: str) -> str:
        for field in ("canonicalContact", "mergedTo"):
            ref = entity.get(field) or {}
            if ref.get("id"):
                return ref["id"]
        return entity.get("id") or fallback

    def resolve_canonical(self, contact_ids: set[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        todo: list[str] = []
        for contact_id in contact_ids:
            entry = self._canonical.get(contact_id)
            if self._fresh(entry):
                out[contact_id] = entry[0]
            else:
                todo.append(contact_id)
        if not todo:
            return out
        logger.info("repeat-contact report: resolving %d external contacts", len(todo))
        api = gc.ExternalContactsApi(get_api())
        batches = [todo[i:i + _BULK_CONTACT_BATCH] for i in range(0, len(todo), _BULK_CONTACT_BATCH)]

        def _run(batch: list[str]) -> dict[str, str]:
            if self.unavailable_reason:
                return {}
            self._throttle.wait()
            try:
                resp = to_dict(with_retry(api.post_externalcontacts_bulk_contacts)(
                    body={"entities": [{"id": cid} for cid in batch]},
                )) or {}
            except ApiException as exc:
                self._fail(exc, "bulk contact fetch")
                return {}
            resolved: dict[str, str] = {}
            for row in resp.get("results") or []:
                entity = row.get("entity") or {}
                requested = row.get("id") or entity.get("id")
                if requested and entity:
                    resolved[requested] = self._canonical_of(entity, requested)
            return resolved

        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            for resolved in pool.map(_run, batches):
                now = time.time()
                with self._lock:
                    self.api_calls += 1
                    for contact_id, canonical_id in resolved.items():
                        self._canonical[contact_id] = [canonical_id, now]
                        out[contact_id] = canonical_id
                    self._dirty = self._dirty or bool(resolved)
        return out

    def lookup(self, kind: str, value: str) -> str | None:
        identifier_type = _IDENTIFIER_TYPES.get(kind)
        if identifier_type is None or self.unavailable_reason:
            return None
        cache_key = f"{kind}|{value}"
        entry = self._lookups.get(cache_key)
        if self._fresh(entry):
            return entry[0]

        api = gc.ExternalContactsApi(get_api())
        found: str | None = None
        self._throttle.wait()
        try:
            self.api_calls += 1
            entity = to_dict(with_retry(api.post_externalcontacts_identifierlookup_contacts)(
                identifier={"type": identifier_type, "value": value},
            )) or {}
            if entity.get("id"):
                found = self._canonical_of(entity, entity["id"])
        except ApiException as exc:
            if int(getattr(exc, "status", 0) or 0) != 404:
                self._fail(exc, "identifier lookup")
                return None
            if self._config.identity.lookup_search_fallback:
                found = self._search_exactly_one(api, value)
        with self._lock:
            self._lookups[cache_key] = [found, time.time()]
            self._dirty = True
        return found

    def _search_exactly_one(self, api: gc.ExternalContactsApi, value: str) -> str | None:
        self._throttle.wait()
        try:
            self.api_calls += 1
            page = to_dict(with_retry(api.get_externalcontacts_contacts)(q=value, page_size=2)) or {}
        except ApiException as exc:
            self._fail(exc, "contact search")
            return None
        canonical_ids = {
            self._canonical_of(entity, entity.get("id") or "")
            for entity in page.get("entities") or [] if entity.get("id")
        }
        # Several rows that all merge into one surviving contact are still
        # exactly one customer; anything else is ambiguous and rejected.
        return next(iter(canonical_ids)) if len(canonical_ids) == 1 else None

    def save(self) -> None:
        if self._path is None or not self._dirty:
            return
        cutoff = time.time() - max(self._ttl, 3600)
        _write_cache(self._path, {
            "canonical": {k: v for k, v in self._canonical.items() if float(v[1]) >= cutoff},
            "lookups": {k: v for k, v in self._lookups.items() if float(v[1]) >= cutoff},
        })


# ───────────────────────────── helpers ─────────────────────────────

def _parse_day(value: str, zone: ZoneInfo, name: str) -> date:
    text = (value or "").strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD) or datetime, got {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone).date()


def _flatten(data: dict[str, Any], prefix: str = "") -> list[str]:
    keys: list[str] = []
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            keys.extend(_flatten(value, dotted + "."))
        else:
            keys.append(dotted)
    return keys


def _config_payload(config: RepeatContactReportConfig, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "section": meta["section"],
        "config": config.model_dump(mode="json"),
        "env_overridden_keys": meta["env_overridden_keys"],
        "runtime_config_path": meta["runtime_config_path"],
        "runtime_config_exists": meta["runtime_config_exists"],
        "env_prefix": meta["env_prefix"],
        "fcr_label": FCR_LABEL,
        "as_of_utc": _now_utc().isoformat().replace("+00:00", "Z"),
    }


def _bad_request(kind: str, exc: Exception) -> dict[str, Any]:
    return soft_fail_envelope(status=400, kind=kind, message=str(exc))


# ───────────────────────────── tools ─────────────────────────────

def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_repeat_contact_config() -> dict:
        """Read the effective ``repeatContactReport`` config (defaults for get_repeat_contact_report).

        Returns ``config`` (direction, include_abandoned, include_pre_queue,
        media_types, queue_ids, windows_days, timezone, identity.*, cache.*),
        plus ``env_overridden_keys`` — dotted keys pinned by a
        ``GENESYS_MCP_REPEAT_*`` environment variable. A pinned key cannot be
        changed by ``set_repeat_contact_config``; show it as locked.
        """
        try:
            config, meta = load_repeat_contact_config()
        except RepeatContactConfigError as exc:
            return _bad_request("repeat_contact_config", exc)
        return _config_payload(config, meta)

    @mcp.tool()
    def set_repeat_contact_config(
        updates: dict | None = Field(
            default=None,
            description=(
                "Partial config to deep-merge into the stored defaults, e.g. "
                "{'windows_days': [7, 30, 45], 'include_abandoned': true, "
                "'identity': {'mode': 'raw_only', 'enable_lookup': false}}. "
                "Unknown keys are rejected. windows_days must be positive integers."
            ),
        ),
        reset: bool = Field(
            default=False,
            description="Discard every stored override first (back to defaults), then apply updates.",
        ),
    ) -> dict:
        """Change the stored ``repeatContactReport`` defaults without a redeploy.

        The whole merged config is validated before anything is written; on a
        validation error nothing changes and a ``status: 400`` envelope names
        the offending key. Returns the new effective config in the same shape
        as ``get_repeat_contact_config``. This writes a local config file
        only — it never writes to Genesys.
        """
        try:
            save_repeat_contact_config(updates, reset=reset)
            config, meta = load_repeat_contact_config()
        except RepeatContactConfigError as exc:
            return _bad_request("repeat_contact_config", exc)
        except OSError as exc:
            return soft_fail_envelope(
                status=500, kind="repeat_contact_config",
                message=f"Could not write the runtime config file: {exc}",
            )
        payload = _config_payload(config, meta)
        requested = set(_flatten(updates or {}))
        payload["ignored_env_pinned_keys"] = sorted(requested.intersection(meta["env_overridden_keys"]))
        return payload

    @mcp.tool()
    def get_repeat_contact_report(
        period_start: str = Field(
            description="First day of the reporting period, YYYY-MM-DD, in the configured timezone (inclusive).",
        ),
        period_end: str = Field(
            description="Last day of the reporting period, YYYY-MM-DD, in the configured timezone (inclusive).",
        ),
        windows_days: list[int] | None = Field(
            default=None,
            description="Repeat windows in days, e.g. [7, 45]. Positive integers. Default: config windows_days.",
        ),
        channels: list[str] | None = Field(
            default=None,
            description="Any of 'voice', 'messaging', 'combined'. Default: all three.",
        ),
        queue_ids: list[str] | None = Field(
            default=None,
            description=(
                "Queue scope. Omit to use config queue_ids; pass [] for all queues. "
                "Scope applies to prior contacts too."
            ),
        ),
        include_abandoned: bool | None = Field(
            default=None,
            description="Include contacts that queued but no agent handled. Default: config include_abandoned.",
        ),
        breakdown: list[str] | None = Field(
            default=None,
            description="Optional breakdowns: any of 'day', 'queue'.",
        ),
        drilldown: bool = Field(
            default=False,
            description=(
                "Include the list of repeat conversations with their prior contact. "
                "Large — leave false unless the rows are needed."
            ),
        ),
    ) -> dict:
        """Repeat Contact Rate and FCR (Amaysim methodology) per window × channel.

        A contact is one conversation. It is a REPEAT when the same customer
        had any prior in-scope contact within the preceding N days (any
        reason). ``repeat_rate = repeat_contacts / total_contacts`` and
        ``fcr = 1 - repeat_rate`` — FCR here is *derived*, not measured; label
        it exactly "FCR (Amaysim methodology)". Voice / Messaging only match a
        prior contact on the same channel; Combined matches either.

        ``results`` has one row per window × channel: window_days, channel,
        total_contacts, repeat_contacts, repeat_rate, fcr (0–1 fractions, plus
        ``*_pct``), unidentified_contacts, raw_key_contacts, coverage
        ({external_contact_linked, external_contact_lookup, raw_key,
        unidentified} as count + pct), period_start, period_end, config_used,
        warnings[]. A ``combined_rate_likely_understated`` warning appears
        when too few Combined contacts resolve to an External Contact.

        Contacts from the lookback (period_start − largest window) are fetched
        for matching only and never counted. The first run over a long window
        is slow (it walks every lookback day and resolves External Contacts);
        settled days are cached on disk, so adjacent periods and re-runs are
        fast. ``data_quality`` reports incomplete days and lookup volume.

        Needs ``analytics:conversationDetail:view``; External Contact
        resolution additionally needs ``externalContacts:contact:view`` and
        degrades to raw identifiers (with a warning) without it.
        """
        try:
            config, _meta = load_repeat_contact_config()
        except RepeatContactConfigError as exc:
            return _bad_request("repeat_contact_report", exc)

        try:
            zone = ZoneInfo(config.timezone)
            start_day = _parse_day(period_start, zone, "period_start")
            end_day = _parse_day(period_end, zone, "period_end")
            if end_day < start_day:
                raise ValueError("period_end is before period_start")
            if (end_day - start_day).days + 1 > _MAX_PERIOD_DAYS:
                raise ValueError(f"reporting period is limited to {_MAX_PERIOD_DAYS} days")
            windows = validate_windows(windows_days) if windows_days is not None else list(config.windows_days)
            wanted_channels = [c.strip().lower() for c in (channels or CHANNELS)]
            unknown = sorted(set(wanted_channels) - set(CHANNELS))
            if unknown:
                raise ValueError(f"unknown channels {unknown}; use any of {list(CHANNELS)}")
            wanted_breakdown = {b.strip().lower() for b in (breakdown or [])}
            if wanted_breakdown - {"day", "queue"}:
                raise ValueError("breakdown accepts only 'day' and 'queue'")
        except ValueError as exc:
            return _bad_request("repeat_contact_report", exc)

        queue_filter = frozenset(config.queue_ids if queue_ids is None else queue_ids)
        abandoned = config.include_abandoned if include_abandoned is None else bool(include_abandoned)
        lookback_start = start_day - timedelta(days=max(windows))
        cache_root = _cache_root(config)
        if cache_root is not None:
            _prune_cache(cache_root, config.cache.retention_days)

        logger.info(
            "repeat-contact report %s..%s windows=%s lookback_from=%s",
            start_day, end_day, windows, lookback_start,
        )
        try:
            all_contacts, day_meta = _load_contacts(lookback_start, end_day, zone, config, cache_root)
        except ApiException as exc:
            return soft_fail_envelope(
                status=int(getattr(exc, "status", 0) or 500),
                kind="repeat_contact_report",
                message=(
                    "Conversation detail query failed: "
                    f"{getattr(exc, 'reason', None) or type(exc).__name__}. "
                    "If the status is 403, grant the OAuth client "
                    "'analytics:conversationDetail:view'."
                ),
                period_start=start_day.isoformat(),
                period_end=end_day.isoformat(),
            )

        scoped = [
            c for c in all_contacts
            if in_scope(c, include_abandoned=abandoned,
                        include_pre_queue=config.include_pre_queue, queue_filter=queue_filter)
        ]
        period_ids = {
            c.conversation_id for c in scoped
            if start_day <= c.start.astimezone(zone).date() <= end_day
        }

        external = _ExternalContacts(config, cache_root)
        use_api = config.identity.mode == "external_contact_first"
        identities, identity_stats = resolve_identities(
            scoped,
            mode=config.identity.mode,
            enable_lookup=config.identity.enable_lookup,
            resolve_canonical=config.identity.resolve_canonical,
            max_lookups=config.identity.max_lookups_per_run,
            default_country=config.identity.phone_normalisation.default_country,
            messaging_keys=config.identity.messaging_keys,
            canonical_resolver=external.resolve_canonical if use_api else None,
            lookup=external.lookup if use_api else None,
            priority_ids=period_ids,
        )
        external.save()

        queue_names: dict[str, str] = {}
        if wanted_breakdown or drilldown:
            seen_queues = {q for c in scoped for q in c.queue_ids}
            queue_names = resolver.queue_names(seen_queues) if seen_queues else {}

        report = build_report(
            scoped, identities,
            period_start=start_day, period_end=end_day, windows_days=windows,
            channels=wanted_channels, tz=zone, queue_filter=queue_filter,
            breakdown=wanted_breakdown, drilldown=drilldown,
            drilldown_max_rows=config.drilldown_max_rows,
            low_coverage_warning_pct=config.identity.low_coverage_warning_pct,
            queue_name=queue_names.get,
        )

        config_used = {
            **config.model_dump(mode="json", exclude={"cache"}),
            "windows_days": windows,
            "queue_ids": sorted(queue_filter),
            "include_abandoned": abandoned,
            "channels": [c for c in CHANNELS if c in wanted_channels],
        }
        run_warnings: list[dict[str, str]] = []
        incomplete = [m["date"] for m in day_meta if not m["data_complete"]]
        if incomplete:
            run_warnings.append({
                "code": "incomplete_conversation_data",
                "message": (
                    f"{len(incomplete)} day(s) did not return complete conversation detail "
                    f"({', '.join(incomplete[:5])}{'…' if len(incomplete) > 5 else ''}); "
                    "totals and repeat matching for those days may be understated."
                ),
            })
        if external.unavailable_reason:
            run_warnings.append({"code": "external_contacts_unavailable", "message": external.unavailable_reason})
        if identity_stats["lookups_skipped_over_cap"]:
            run_warnings.append({
                "code": "identity_lookup_cap_reached",
                "message": (
                    f"{identity_stats['lookups_skipped_over_cap']} identifier(s) were not looked up "
                    f"(identity.max_lookups_per_run = {config.identity.max_lookups_per_run}); "
                    "they fell back to raw keys."
                ),
            })
        if (report.get("drilldown") or {}).get("truncated"):
            run_warnings.append({
                "code": "drilldown_truncated",
                "message": f"Drill-down capped at {config.drilldown_max_rows} rows per window × channel.",
            })

        # Top level carries every distinct warning; each row carries its own
        # channel warnings plus the run-wide ones, per the per-row contract.
        top_warnings = list(run_warnings)
        for row in report["results"]:
            for warning in row["warnings"]:
                if warning not in top_warnings:
                    top_warnings.append(warning)
            row["config_used"] = config_used
            row["warnings"] = row["warnings"] + run_warnings

        app_base = resolve_app_base_url()
        return {
            "report": "repeat_contacts_fcr",
            "fcr_label": FCR_LABEL,
            "methodology": METHODOLOGY,
            "as_of_utc": _now_utc().isoformat().replace("+00:00", "Z"),
            "period_start": start_day.isoformat(),
            "period_end": end_day.isoformat(),
            "lookback_start": lookback_start.isoformat(),
            "timezone": config.timezone,
            "config_used": config_used,
            **report,
            "warnings": top_warnings,
            "conversation_url_template": (
                f"{app_base}/directory/#/analytics/interactions/{{conversation_id}}/admin" if app_base else None
            ),
            "data_quality": {
                "days_fetched": len(day_meta),
                "days_from_cache": sum(1 for m in day_meta if m["source"] == "cache"),
                "incomplete_days": incomplete,
                "provisional_days": [m["date"] for m in day_meta if m.get("data_provisional")],
                "conversations_fetched": len(all_contacts),
                "conversations_in_scope": len(scoped),
                "period_contacts_in_scope": len(period_ids),
                "identity": {**identity_stats, "external_contact_api_calls": external.api_calls},
            },
        }
