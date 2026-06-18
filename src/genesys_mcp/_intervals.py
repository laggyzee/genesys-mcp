"""Shared interval-computation helpers — v1.5.

Pre-v1.5 each tool module carried its own copy of ``_default_interval`` and
``_parse_iso``. v1.5 consolidates them here and adds
:func:`compute_period_interval`, which is the engine behind the new
``compute_interval`` MCP tool: it turns a fixed period keyword into a
timezone-aware ISO interval so foreign clients don't need to do tz math.

The canonical interval format across the entire codebase:

    "<startISO>/<endISO>"

where both ends are UTC, ISO-8601, with a ``.000Z`` suffix (e.g.
``2026-06-17T14:00:00.000Z``). Every existing tool already speaks this
format — this module just centralises the *production* of it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Single canonical docstring fragment for the ``interval:`` parameter on every
# interval-bearing tool. v1.5 docstring sweep interpolates this so the message
# can't drift between tools.
INTERVAL_HELP_STRING = (
    'ISO-8601 interval "startISO/endISO" in UTC. Accepts ANY window — '
    "calendar day, arbitrary range, multi-month. To get a tenant-timezone-"
    'aware ISO interval for a period like "today" or "last_week", call '
    "`compute_interval` first. Example for a calendar day in "
    'Australia/Sydney: "2026-06-17T14:00:00.000Z/2026-06-18T14:00:00.000Z". '
    "Defaults to the last 7 days UTC if omitted."
)


# Fixed enum of supported period keywords. Keep this list synchronised with
# the docstring on the ``compute_interval`` MCP tool wrapper.
SUPPORTED_PERIODS = (
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "last_7_days",
    "last_28_days",
)


def default_interval(days: int = 7) -> str:
    """ISO-8601 interval ``start/end`` covering the last N days up to now (UTC).

    Identical behaviour to the pre-v1.5 ``_default_interval`` copies that
    lived in 5 different modules.
    """
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    return f"{_fmt_utc(start)}/{_fmt_utc(end)}"


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string (handles trailing ``Z``).

    Identical behaviour to the pre-v1.5 ``_parse_iso`` copies in
    ``presence.py``, ``reports.py``, and ``wfm.py``.
    """
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _fmt_utc(dt: datetime) -> str:
    """Format a UTC datetime to the canonical ``...Z`` form with ``.000Z``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    iso = dt.astimezone(timezone.utc).isoformat()
    # Force the ``.000Z`` suffix for cross-tool consistency; Python's
    # ``isoformat()`` emits ``+00:00`` and may or may not include
    # microseconds.
    iso = iso.replace("+00:00", "")
    if "." not in iso:
        iso += ".000"
    return iso + "Z"


def _resolve_zoneinfo(tz_name: str) -> ZoneInfo:
    """Resolve an IANA timezone name to a ``ZoneInfo`` or raise ``ValueError``."""
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Unknown timezone {tz_name!r}. Expected an IANA name like "
            "'Australia/Sydney', 'America/New_York', or 'UTC'."
        ) from exc


def now_utc() -> datetime:
    """``datetime.now(timezone.utc)`` — lifted out so tests can monkey-patch it."""
    return datetime.now(timezone.utc)


def compute_period_interval(
    period: str,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Convert a period keyword to a tenant-timezone-aware ISO interval.

    Args:
        period: One of :data:`SUPPORTED_PERIODS`.
        timezone_name: IANA timezone name (e.g. ``"Australia/Sydney"``).
        now: Override "current time" — lets tests fix a reference clock.
            Defaults to :func:`now_utc`.

    Returns a dict with ``period``, ``timezone``, ``start_local``,
    ``end_local``, ``start_utc``, ``end_utc``, ``interval`` (the
    paste-ready string), and ``weekday_anchor`` (``"Mon"`` for week-based
    periods, else absent).

    Raises:
        ValueError: on unknown period keyword or unresolvable timezone.
    """
    if period not in SUPPORTED_PERIODS:
        raise ValueError(
            f"Unknown period {period!r}. Supported: {SUPPORTED_PERIODS}."
        )

    tz = _resolve_zoneinfo(timezone_name)
    ref_utc = (now or now_utc()).astimezone(timezone.utc)
    ref_local = ref_utc.astimezone(tz)
    today_local = ref_local.replace(hour=0, minute=0, second=0, microsecond=0)

    weekday_anchor: str | None = None

    if period == "today":
        start_local = today_local
        end_local = today_local + timedelta(days=1)
    elif period == "yesterday":
        start_local = today_local - timedelta(days=1)
        end_local = today_local
    elif period == "this_week":
        # Monday = 0 in Python's weekday()
        start_local = today_local - timedelta(days=ref_local.weekday())
        end_local = start_local + timedelta(days=7)
        weekday_anchor = "Mon"
    elif period == "last_week":
        this_week_start = today_local - timedelta(days=ref_local.weekday())
        start_local = this_week_start - timedelta(days=7)
        end_local = this_week_start
        weekday_anchor = "Mon"
    elif period == "this_month":
        start_local = today_local.replace(day=1)
        end_local = _add_month(start_local)
    elif period == "last_month":
        this_month_start = today_local.replace(day=1)
        # Step back one day from the 1st to land in the previous month,
        # then snap to its 1st.
        prev_month_any_day = this_month_start - timedelta(days=1)
        start_local = prev_month_any_day.replace(day=1)
        end_local = this_month_start
    elif period == "last_7_days":
        # Rolling: 7 × 24h ending now. Distinct from this_week which
        # anchors to Monday 00:00 local.
        end_local = ref_local
        start_local = end_local - timedelta(days=7)
    elif period == "last_28_days":
        end_local = ref_local
        start_local = end_local - timedelta(days=28)
    else:  # pragma: no cover — guarded by SUPPORTED_PERIODS check above
        raise ValueError(f"Unhandled period {period!r}")

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    interval_str = f"{_fmt_utc(start_utc)}/{_fmt_utc(end_utc)}"

    out: dict[str, Any] = {
        "period": period,
        "timezone": timezone_name,
        "start_local": start_local.isoformat(),
        "end_local": end_local.isoformat(),
        "start_utc": _fmt_utc(start_utc),
        "end_utc": _fmt_utc(end_utc),
        "interval": interval_str,
    }
    if weekday_anchor:
        out["weekday_anchor"] = weekday_anchor
    return out


def _add_month(dt: datetime) -> datetime:
    """Return ``dt`` advanced by one calendar month, snapped to the 1st.

    Helper for ``this_month`` end boundary. Doesn't try to preserve
    day-of-month — callers always pass the 1st in.
    """
    year, month = dt.year, dt.month + 1
    if month == 13:
        year += 1
        month = 1
    return dt.replace(year=year, month=month, day=1)
