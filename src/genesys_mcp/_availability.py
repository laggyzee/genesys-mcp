"""Data-availability watermark for the users/details analytics jobs API.

Genesys settles presence/routing detail data asynchronously. The
``/api/v2/analytics/users/details/jobs/availability`` endpoint returns a
``dataAvailabilityDate`` watermark: detail queries for any window extending
past it come back **partial, with no error and no flag** — the job succeeds
and simply omits the not-yet-settled tail.

This bit us in production: a coaching brief collected at 06:00 for the prior
day read the last recorded presence session as the agent's "logout", but the
watermark was hours behind, so an agent who worked into the evening looked
like she left mid-afternoon. Every presence-derived figure (login/logout,
online/on-queue totals, the day timeline) was silently truncated while her
conversation stats — a separate, near-real-time pipeline — were complete.

:func:`presence_data_availability` fetches the watermark once and reports
whether a requested interval is fully settled, so tools can surface
``complete: false`` + ``data_available_until`` instead of lying by omission.
"""
from __future__ import annotations

import logging
from typing import Any

import PureCloudPlatformClientV2 as gc

from genesys_mcp._intervals import parse_iso as _parse_iso
from genesys_mcp.client import to_dict, with_retry

logger = logging.getLogger(__name__)


def presence_data_availability(api: gc.AnalyticsApi, interval_end: Any) -> dict[str, Any]:
    """Assess whether ``interval_end`` is fully settled in users/details data.

    ``interval_end`` is a timezone-aware ``datetime`` (the end of the requested
    window). Returns a dict always safe to merge into a tool response:

      - ``complete``            — bool | None. True when the watermark is at or
                                  past ``interval_end``; False when it is
                                  behind (data is partial); None when the
                                  watermark could not be read (fail-open: we
                                  don't block the answer, but we don't claim
                                  completeness either).
      - ``data_available_until``— ISO-Z watermark string, or None if unknown.
      - ``lag_seconds``         — how far the window end is beyond the watermark
                                  (0 when complete; None when unknown).
      - ``note``                — human/LLM-readable explanation when not complete.

    Never raises: a watermark lookup failure degrades to ``complete: None`` so
    the underlying data query still returns.
    """
    watermark = None
    try:
        resp = with_retry(api.get_analytics_users_details_jobs_availability)()
        data = to_dict(resp) or {}
        raw = data.get("dataAvailabilityDate") or data.get("data_availability_date")
        if raw:
            watermark = _parse_iso(raw)
    except Exception as exc:  # noqa: BLE001 — fail open, never block the data query
        logger.warning("users/details availability watermark lookup failed: %s", exc)

    if watermark is None:
        return {
            "complete": None,
            "data_available_until": None,
            "lag_seconds": None,
            "note": (
                "Could not read the Genesys data-availability watermark; "
                "presence data may be incomplete for very recent windows."
            ),
        }

    watermark_z = watermark.isoformat().replace("+00:00", "Z")
    if watermark >= interval_end:
        return {
            "complete": True,
            "data_available_until": watermark_z,
            "lag_seconds": 0,
            "note": None,
        }

    lag = (interval_end - watermark).total_seconds()
    return {
        "complete": False,
        "data_available_until": watermark_z,
        "lag_seconds": int(lag),
        "note": (
            f"Presence data is only settled up to {watermark_z}, which is before "
            f"the end of the requested window. Sessions after that time are not "
            f"yet available and are omitted — totals, last-recorded presence, and "
            f"any derived logout time are incomplete for this interval."
        ),
    }
