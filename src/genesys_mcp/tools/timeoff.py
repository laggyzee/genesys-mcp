"""WFM leave / time-off — pulls time-off requests + activity codes.

v1.7+. Closes the gap surfaced by *"give me a report on any leave/time off
over the last 4 weeks"*. Pre-v1.7 the MCP had no access to the
``/api/v2/workforcemanagement/.../timeoffrequests`` endpoint family at
all; the closest signal was `query_agent_adherence_explanations` which
captures post-hoc commentary on off-schedule events, not the approved
leave record.

Two tools:

- :func:`wfm_activity_codes` — lists the WFM activity-code catalogue
  for a business unit (the leave-type definitions). Process-lifetime
  cached.
- :func:`wfm_time_off_requests` — queries time-off requests over an
  interval, resolves each request's activity code to a human-readable
  name (Annual Leave / Sick Leave / etc.), and rolls up totals per
  user and per activity.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._intervals import INTERVAL_HELP_STRING
from genesys_mcp._intervals import default_interval as _default_interval
from genesys_mcp._intervals import now_utc as _now_utc
from genesys_mcp._intervals import parse_iso as _parse_iso
from genesys_mcp.client import get_api, with_retry
from genesys_mcp.naming import resolver

logger = logging.getLogger(__name__)


# Process-lifetime cache of {business_unit_id: {activity_code_id: row_dict}}.
# Activity codes change rarely (admin adds/edits a code), so a never-expire
# cache is the right trade-off. Restart the MCP to refresh. Same pattern as
# directory._load_presence_label_cache (v1.3).
_ACTIVITY_CODE_CACHE: dict[str, dict[str, dict]] = {}


def _fetch_activity_codes(business_unit_id: str) -> dict[str, dict]:
    """Fetch + cache the activity-code catalogue for a BU.

    Returns ``{activity_code_id: row_dict}``. Errors propagate so the
    caller knows when the WFM scope is missing.
    """
    if business_unit_id in _ACTIVITY_CODE_CACHE:
        return _ACTIVITY_CODE_CACHE[business_unit_id]
    api_client = get_api()
    resp = with_retry(api_client.call_api)(
        resource_path=(
            f"/api/v2/workforcemanagement/businessunits/"
            f"{business_unit_id}/activitycodes"
        ),
        method="GET",
        auth_settings=["PureCloud OAuth"],
        response_type="object",
    ) or {}
    # Genesys may emit the catalogue as `entities: [...]` OR as
    # `activityCodes: {<id>: {...}}` (dict keyed by id). Handle both.
    entities = resp.get("entities")
    if entities is None:
        ac = resp.get("activityCodes")
        if isinstance(ac, dict):
            entities = list(ac.values())
        elif isinstance(ac, list):
            entities = ac
        else:
            entities = []
    by_id: dict[str, dict] = {}
    for e in entities:
        cid = e.get("id")
        if not cid:
            continue
        by_id[cid] = {
            "id": cid,
            "name": e.get("name"),
            "category": e.get("category"),
            "paid": e.get("countsAsPaidTime"),
            "length_minutes": e.get("lengthInMinutes"),
            "active": e.get("active", True),
        }
    _ACTIVITY_CODE_CACHE[business_unit_id] = by_id
    logger.info(
        "wfm activity-code cache loaded for BU %s: %d entries",
        business_unit_id, len(by_id),
    )
    return by_id


def _interval_to_date_range(interval: str) -> tuple[str, str]:
    """Convert canonical ``startISO/endISO`` to ``(YYYY-MM-DD, YYYY-MM-DD)``.

    The Genesys time-off-requests endpoint takes a ``dateRange`` with
    bare dates, not ISO-8601 timestamps. We strip the time portion.
    """
    start_iso, end_iso = interval.split("/", 1)
    start = _parse_iso(start_iso).astimezone(timezone.utc).date()
    end = _parse_iso(end_iso).astimezone(timezone.utc).date()
    return start.isoformat(), end.isoformat()


def _normalise_request(
    raw: dict, activity_codes: dict[str, dict], names: dict[str, str | None],
) -> dict:
    """Reduce one Genesys time-off-request entity to a flat row.

    Handles both full-day and partial-day shapes:

    - Full-day: ``fullDayManagementUnitDates: ["2026-06-08", ...]`` —
      hours = ``len(dates) * (dailyDurationMinutes / 60 or 8)``.
    - Partial-day: ``partialDayStartDateTimes: ["...T09:00:00Z", ...]``
      + ``dailyDurationMinutes`` — each entry is one partial day at
      that start time; hours = ``count * (dailyDurationMinutes / 60)``.
    """
    is_full_day = bool(raw.get("isFullDayRequest"))
    daily_min = raw.get("dailyDurationMinutes")
    hours_per_day = (daily_min / 60.0) if daily_min else (8.0 if is_full_day else 0.0)

    if is_full_day:
        dates = list(raw.get("fullDayManagementUnitDates") or [])
        partial_starts: list[str] = []
    else:
        partial_starts = list(raw.get("partialDayStartDateTimes") or [])
        # Each entry is an ISO datetime — extract date portion only,
        # preserving order and deduplicating.
        dates = []
        for dt_str in partial_starts:
            try:
                d = _parse_iso(dt_str).astimezone(timezone.utc).date().isoformat()
            except Exception:
                continue
            if d not in dates:
                dates.append(d)

    days = len(dates)
    start_date = dates[0] if dates else None
    end_date = dates[-1] if dates else None
    hours = round(days * hours_per_day, 2) if days and hours_per_day else 0.0

    activity_code_id = raw.get("activityCodeId")
    activity_row = activity_codes.get(activity_code_id) or {}
    activity_name = activity_row.get("name") or "Unknown activity"

    user = raw.get("user") or {}
    user_id = user.get("id")
    modified_by = raw.get("modifiedBy") or {}

    return {
        "id": raw.get("id"),
        "user_id": user_id,
        "user_name": names.get(user_id) if user_id else None,
        "activity_code_id": activity_code_id,
        "activity_name": activity_name,
        "activity_category": activity_row.get("category"),
        "status": raw.get("status"),
        "is_full_day": is_full_day,
        "start_date": start_date,
        "end_date": end_date,
        "dates": dates,
        "days": days,
        "hours": hours,
        "daily_duration_minutes": daily_min,
        "partial_day_start_times": partial_starts if partial_starts else None,
        "notes": raw.get("notes"),
        "modified_by_id": modified_by.get("id"),
        "modified_by_name": modified_by.get("name"),
        "modified_at": raw.get("modifiedDate"),
        "submitted_at": raw.get("submittedDate"),
    }


def _build_rollups(rows: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    """Build ``totals``, ``by_activity``, ``by_user`` from normalised rows."""
    approved_count = sum(1 for r in rows if r["status"] == "APPROVED")
    pending_count = sum(1 for r in rows if r["status"] == "PENDING")
    total_hours = round(sum(r["hours"] for r in rows), 2)
    total_days = sum(r["days"] for r in rows)

    totals = {
        "request_count": len(rows),
        "approved_count": approved_count,
        "pending_count": pending_count,
        "total_hours": total_hours,
        "total_days": total_days,
    }

    by_activity_map: dict[str, dict] = {}
    for r in rows:
        name = r["activity_name"]
        agg = by_activity_map.setdefault(name, {
            "activity_name": name,
            "request_count": 0,
            "total_hours": 0.0,
            "total_days": 0,
        })
        agg["request_count"] += 1
        agg["total_hours"] = round(agg["total_hours"] + r["hours"], 2)
        agg["total_days"] += r["days"]
    by_activity = sorted(
        by_activity_map.values(), key=lambda a: a["total_hours"], reverse=True,
    )

    by_user_map: dict[str, dict] = {}
    for r in rows:
        uid = r["user_id"]
        if not uid:
            continue
        agg = by_user_map.setdefault(uid, {
            "user_id": uid,
            "user_name": r["user_name"],
            "request_count": 0,
            "total_hours": 0.0,
            "total_days": 0,
            "activities": set(),
        })
        agg["request_count"] += 1
        agg["total_hours"] = round(agg["total_hours"] + r["hours"], 2)
        agg["total_days"] += r["days"]
        agg["activities"].add(r["activity_name"])
    by_user = sorted(by_user_map.values(), key=lambda a: a["total_hours"], reverse=True)
    for entry in by_user:
        entry["activities"] = sorted(entry["activities"])

    return totals, by_activity, by_user


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def wfm_activity_codes(
        business_unit_id: str = Field(
            description=(
                "Business unit id. Use `list_management_units` to find the BU id "
                "(look at the `business_unit_id` field on any management unit)."
            ),
        ),
    ) -> dict:
        """WFM activity-code catalogue for a business unit.

        v1.7+. Returns every activity code defined in WFM admin —
        e.g. *Annual Leave*, *Sick Leave*, *Personal Leave*,
        *Training*, *On Queue Time*, *Break*, *Meal*. Each row carries:

        - ``id`` — UUID used as the ``activityCodeId`` on time-off
          requests and schedule activities
        - ``name`` — human-readable label as configured in Genesys Admin
        - ``category`` — one of ``OnQueueTime``, ``OffQueueTime``,
          ``TimeOff``, ``Meeting``, ``Break``, ``Meal``, ``Training``,
          ``Unavailable``
        - ``paid`` — whether the activity counts as paid time
        - ``length_minutes`` — default duration (for fixed-length codes)

        Use this to:

        - Answer *"what leave types does this org track?"*
        - Pass `activityCodeId` filters to `wfm_time_off_requests`
        - Inspect what a particular code id resolves to when reading
          schedule activities

        Cached process-lifetime — first call hits Genesys, subsequent
        calls hit the in-process cache. Restart the MCP server to
        refresh after an admin change.

        Endpoint: ``GET /api/v2/workforcemanagement/businessunits/{businessUnitId}/activitycodes``.
        Needs ``workforce-management:readonly``.
        """
        codes = _fetch_activity_codes(business_unit_id)
        rows = sorted(
            codes.values(),
            key=lambda r: (r.get("category") or "", r.get("name") or ""),
        )
        return {
            "business_unit_id": business_unit_id,
            "count": len(rows),
            "activity_codes": rows,
        }

    @mcp.tool()
    def wfm_time_off_requests(
        business_unit_id: str = Field(
            description=(
                "Business unit id. Required for resolving activity-code "
                "names (the catalogue lives under "
                "`/businessunits/{id}/activitycodes`). Use "
                "`list_management_units` to find the BU id."
            ),
        ),
        management_unit_ids: list[str] = Field(
            description=(
                "Management unit ids to query. Time-off requests in "
                "Genesys are stored per-MU — the API has no BU-wide "
                "query endpoint. Pass every MU you want covered; the "
                "tool fans out concurrently and merges the results. "
                "Use `list_management_units` to discover them."
            ),
        ),
        interval: str | None = Field(
            default=None,
            description=INTERVAL_HELP_STRING,
        ),
        user_ids: list[str] | None = Field(
            default=None,
            description=(
                "Filter to specific agents. Omit (or pass an empty list) for "
                "org-wide. Use `list_users` / `find_user` to resolve names."
            ),
        ),
        statuses: list[str] | None = Field(
            default=None,
            description=(
                "Approval statuses to include. One or more of: "
                "'APPROVED', 'PENDING', 'DENIED', 'CANCELED'. Defaults to "
                "['APPROVED', 'PENDING'] — covers actual leave taken plus "
                "leave that's been requested but not yet signed off. Pass "
                "['APPROVED'] for confirmed-leave-only reports, or all four "
                "statuses to audit the approval workflow itself."
            ),
        ),
        mode: str = Field(
            default="summary",
            description=(
                "Response shape. 'summary' (default) returns headline "
                "rollups + per-request rows. 'full' adds `_raw` with the "
                "Genesys response for callers that want to inspect the "
                "underlying entities."
            ),
        ),
    ) -> dict:
        """Per-agent leave / time-off requests over an interval.

        v1.7+. Closes the *"give me a report on any leave/time off over
        the last 4 weeks"* reporting gap. Pre-v1.7 the MCP had no access
        to the time-off-request endpoint family — the closest signal was
        `query_agent_adherence_explanations` (post-hoc commentary on
        off-schedule events, not the approved leave record).

        Four layers in the response:

        - ``totals`` — request_count, approved_count, pending_count,
          total_hours, total_days. Headline numbers for one-line answers.
        - ``by_activity`` — *"Annual Leave: 28 days / 224 h across 14
          requests"* view. Sorted by total_hours desc.
        - ``by_user`` — *"Jane took 7 days across 3 requests (Annual +
          Sick)"* view. Sorted by total_hours desc.
        - ``requests`` — one row per leave request, with normalised
          ``start_date`` / ``end_date`` / ``days`` / ``hours`` regardless
          of whether the original Genesys shape was full-day or partial-day,
          plus a ``dates: [...]`` array for day-by-day analysis.

        Activity codes are resolved to human-readable names (e.g.
        ``"Annual Leave"``) via :func:`wfm_activity_codes`'s process-
        lifetime cache — one extra Genesys call on the first invocation
        per BU, then cached.

        Top-level response carries the v1.5 contract: ``interval`` and
        ``as_of_utc`` at the top so persisted-file readers see the
        window immediately.

        Default window: last 28 days UTC. For tenant-timezone-aware
        windows, call ``compute_interval(period="last_28_days")`` first
        and pass its ``interval`` field.

        Endpoint: ``POST /api/v2/workforcemanagement/managementunits/{managementUnitId}/timeoffrequests/query``
        — invoked once per MU in ``management_unit_ids`` (concurrent fan-out).
        Needs ``wfm:timeOffRequest:view``. The BU-scoped path used pre-v1.13.2
        does not exist in the Genesys schema and crashed 404 on every call.
        """
        if mode not in ("summary", "full"):
            raise ValueError(
                f"wfm_time_off_requests.mode must be 'summary' or 'full', got {mode!r}"
            )
        if not management_unit_ids:
            raise ValueError(
                "wfm_time_off_requests.management_unit_ids must contain at "
                "least one MU id. The Genesys time-off-request endpoint is "
                "MU-scoped — there is no BU-wide query. Discover MUs via "
                "list_management_units(business_unit_id=...)."
            )

        resolved_interval = interval or _default_interval(28)
        try:
            start_date, end_date = _interval_to_date_range(resolved_interval)
        except Exception as exc:
            raise ValueError(
                f"Invalid interval {resolved_interval!r}: {exc}"
            ) from exc

        resolved_statuses = list(statuses) if statuses else ["APPROVED", "PENDING"]

        def _base_body() -> dict[str, Any]:
            body: dict[str, Any] = {
                "dateRange": {"startDate": start_date, "endDate": end_date},
                "statuses": resolved_statuses,
                "pageSize": 100,
                "pageNumber": 1,
            }
            if user_ids:
                body["userIds"] = list(user_ids)
            return body

        api_client = get_api()

        def _fetch_one_mu(mu_id: str) -> list[dict]:
            """Paginate one MU's time-off queue (cap 10 pages = 1000 rows)."""
            mu_entities: list[dict] = []
            body = _base_body()
            for page_number in range(1, 11):
                body["pageNumber"] = page_number
                resp = with_retry(api_client.call_api)(
                    resource_path=(
                        f"/api/v2/workforcemanagement/managementunits/"
                        f"{mu_id}/timeoffrequests/query"
                    ),
                    method="POST",
                    body=body,
                    auth_settings=["PureCloud OAuth"],
                    response_type="object",
                ) or {}
                entities = resp.get("entities") or []
                mu_entities.extend(entities)
                if len(entities) < body["pageSize"]:
                    break
            return mu_entities

        # Fan out per MU. Cap concurrency at 4 — each call may itself
        # paginate (10 page-fetches max). At ~6 MUs × 4 workers wall-clock
        # stays under 5s for realistic orgs.
        all_entities: list[dict] = []
        max_workers = max(1, min(4, len(management_unit_ids)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_one_mu, mu) for mu in management_unit_ids]
            for fut in futures:
                all_entities.extend(fut.result())

        activity_codes = _fetch_activity_codes(business_unit_id)
        unique_user_ids = sorted({
            (e.get("user") or {}).get("id")
            for e in all_entities
            if (e.get("user") or {}).get("id")
        })
        names = resolver.user_names(unique_user_ids) if unique_user_ids else {}

        rows = [_normalise_request(e, activity_codes, names) for e in all_entities]
        # Most recent leave first.
        rows.sort(key=lambda r: (r.get("start_date") or ""), reverse=True)
        totals, by_activity, by_user = _build_rollups(rows)

        out: dict[str, Any] = {
            "interval": resolved_interval,
            "as_of_utc": _now_utc().isoformat().replace("+00:00", "Z"),
            "business_unit_id": business_unit_id,
            "management_unit_ids": list(management_unit_ids),
            "statuses_queried": resolved_statuses,
            "mode": mode,
            "totals": totals,
            "by_activity": by_activity,
            "by_user": by_user,
            "requests": rows,
        }
        if mode == "full":
            out["_raw"] = {"entities": all_entities}
        return out
