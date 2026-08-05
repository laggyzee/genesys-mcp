"""Shared user-detail collection with a reconciled recent-data fallback.

The async ``analytics/users/details/jobs`` archive is authoritative but can lag the
previous local day by many hours.  For an interval beyond that watermark we query the
recent synchronous endpoint, then reconcile its per-user presence durations against
``analytics/users/aggregates/query`` before allowing consumers to treat it as complete.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections import defaultdict
from typing import Any

import PureCloudPlatformClientV2 as gc

from genesys_mcp._availability import presence_data_availability
from genesys_mcp._intervals import parse_iso
from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)

_SYNC_PAGE_SIZE = 100
_CACHE_TTL_SECONDS = 300
_RECONCILIATION_MIN_TOLERANCE_SECONDS = 120
_RECONCILIATION_FRACTION = 0.02
_cache_lock = threading.Lock()
_cache: dict[tuple[str, tuple[str, ...], int], tuple[float, dict[str, Any]]] = {}


def clear_user_details_cache() -> None:
    """Clear the short-lived cache (primarily for tests and explicit refreshes)."""
    with _cache_lock:
        _cache.clear()


def _user_filter(user_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "or",
        "predicates": [
            {
                "type": "dimension",
                "dimension": "userId",
                "operator": "matches",
                "value": user_id,
            }
            for user_id in user_ids
        ],
    }


def _aggregate_user_filter(user_ids: list[str]) -> dict[str, Any]:
    """Return the predicate shape accepted by users/aggregates/query."""
    return {
        "type": "or",
        "predicates": [
            {"dimension": "userId", "value": user_id}
            for user_id in user_ids
        ],
    }


def _async_details(api: Any, user_ids: list[str], interval: str, max_pages: int) -> tuple[list[dict], bool]:
    body = {"interval": interval, "order": "asc", "userFilters": [_user_filter(user_ids)]}
    submit = with_retry(api.post_analytics_users_details_jobs)(body=body)
    job_id = getattr(submit, "job_id", None) or (to_dict(submit) or {}).get("jobId")
    if not job_id:
        raise RuntimeError(f"users/details/jobs submit returned no jobId: {to_dict(submit)}")

    for _ in range(30):
        status_response = with_retry(api.get_analytics_users_details_job)(job_id=job_id)
        state = getattr(status_response, "state", None) or (to_dict(status_response) or {}).get("state")
        if state == "FULFILLED":
            break
        if state in ("FAILED", "CANCELLED", "EXPIRED"):
            raise RuntimeError(f"job {job_id} terminated in state {state}")
        time.sleep(1)
    else:
        raise RuntimeError(f"job {job_id} did not reach FULFILLED within 30s")

    details: list[dict] = []
    cursor: str | None = None
    truncated = False
    for _ in range(max_pages):
        kwargs: dict[str, Any] = {"job_id": job_id, "page_size": 1000}
        if cursor:
            kwargs["cursor"] = cursor
        page = to_dict(with_retry(api.get_analytics_users_details_job_results)(**kwargs)) or {}
        details.extend(page.get("userDetails") or [])
        cursor = page.get("cursor")
        if not cursor:
            break
    else:
        truncated = cursor is not None
    return details, truncated


def _merge_user_detail_page(merged: dict[str, dict], rows: list[dict]) -> None:
    for row in rows:
        user_id = row.get("userId")
        if not user_id:
            continue
        target = merged.setdefault(user_id, {"userId": user_id, "primaryPresence": [], "routingStatus": []})
        target["primaryPresence"].extend(row.get("primaryPresence") or [])
        target["routingStatus"].extend(row.get("routingStatus") or [])


def _sync_details(api: Any, user_ids: list[str], interval: str, max_pages: int) -> tuple[list[dict], bool, int]:
    merged: dict[str, dict] = {}
    total_hits = 0
    truncated = False
    for page_number in range(1, max_pages + 1):
        body = {
            "interval": interval,
            "order": "asc",
            "userFilters": [_user_filter(user_ids)],
            "paging": {"pageSize": _SYNC_PAGE_SIZE, "pageNumber": page_number},
        }
        response = to_dict(with_retry(api.post_analytics_users_details_query)(body=body)) or {}
        rows = response.get("userDetails") or []
        total_hits = int(response.get("totalHits") or 0)
        _merge_user_detail_page(merged, rows)
        if page_number * _SYNC_PAGE_SIZE >= total_hits:
            break
        if not rows:
            truncated = True
            break
    else:
        truncated = max_pages * _SYNC_PAGE_SIZE < total_hits

    for row in merged.values():
        row["primaryPresence"].sort(key=lambda item: item.get("startTime") or "")
        row["routingStatus"].sort(key=lambda item: item.get("startTime") or "")
    return list(merged.values()), truncated, total_hits


def _qualified_aggregates(api: Any, user_ids: list[str], interval: str) -> dict[str, dict[str, dict[str, float]]]:
    body = {
        "interval": interval,
        "groupBy": ["userId"],
        "filter": {"type": "and", "clauses": [_aggregate_user_filter(user_ids)]},
        "metrics": ["tSystemPresence", "tOrganizationPresence", "tAgentRoutingStatus"],
    }
    response = to_dict(with_retry(api.post_analytics_users_aggregates_query)(body=body)) or {}
    by_user: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: {"system_presence": {}, "organization_presence": {}, "routing_status": {}}
    )
    metric_keys = {
        "tSystemPresence": "system_presence",
        "tOrganizationPresence": "organization_presence",
        "tAgentRoutingStatus": "routing_status",
    }
    for result in response.get("results") or []:
        user_id = (result.get("group") or {}).get("userId")
        if not user_id:
            continue
        for bucket in result.get("data") or []:
            for metric in bucket.get("metrics") or []:
                key = metric_keys.get(metric.get("metric"))
                qualifier = metric.get("qualifier")
                if not key or not qualifier:
                    continue
                seconds = float((metric.get("stats") or {}).get("sum") or 0) / 1000
                target = by_user[user_id][key]
                target[qualifier] = target.get(qualifier, 0.0) + seconds
    return dict(by_user)


def _reconcile(
    details: list[dict],
    aggregates: dict[str, dict[str, dict[str, float]]],
    interval_start: Any,
    interval_end: Any,
    truncated: bool,
) -> dict[str, Any]:
    detail_seconds: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for user in details:
        user_id = user.get("userId")
        if not user_id:
            continue
        for session in user.get("primaryPresence") or []:
            start_raw = session.get("startTime")
            if not start_raw:
                continue
            try:
                start = max(interval_start, parse_iso(start_raw))
                end = min(interval_end, parse_iso(session.get("endTime")) if session.get("endTime") else interval_end)
            except Exception:
                continue
            if end <= start:
                continue
            qualifier = (session.get("systemPresence") or "").upper()
            detail_seconds[user_id][qualifier] += (end - start).total_seconds()

    engaged_users = {
        user_id
        for user_id, metrics in aggregates.items()
        if sum(metrics.get("routing_status", {}).values()) > 0
    }
    missing_users: list[str] = []
    mismatches: list[dict[str, Any]] = []
    max_delta = 0.0
    for user_id in sorted(engaged_users):
        expected = aggregates[user_id].get("system_presence", {})
        actual = detail_seconds.get(user_id, {})
        if not actual:
            missing_users.append(user_id)
            continue
        for qualifier in set(expected) | set(actual):
            if qualifier == "OFFLINE":
                # A presence record that spans the whole query range can start before the
                # interval and is legitimately absent from details/query. It is irrelevant to
                # a handled-agent coaching timeline, so validate the active day instead.
                continue
            expected_seconds = expected.get(qualifier, 0.0)
            actual_seconds = actual.get(qualifier, 0.0)
            delta = abs(expected_seconds - actual_seconds)
            max_delta = max(max_delta, delta)
            tolerance = max(_RECONCILIATION_MIN_TOLERANCE_SECONDS, expected_seconds * _RECONCILIATION_FRACTION)
            if delta > tolerance:
                mismatches.append(
                    {
                        "user_id": user_id,
                        "system_presence": qualifier,
                        "expected_seconds": round(expected_seconds, 1),
                        "detail_seconds": round(actual_seconds, 1),
                        "delta_seconds": round(delta, 1),
                    }
                )

    return {
        "reconciled": not truncated and not missing_users and not mismatches,
        "engaged_user_count": len(engaged_users),
        "reconciled_user_count": len(engaged_users) - len(missing_users),
        "missing_user_count": len(missing_users),
        "mismatch_count": len(mismatches),
        "max_delta_seconds": round(max_delta, 1),
        "truncated": truncated,
        # Counts plus bounded examples provide an operational signal without bloating
        # every snapshot.
        "mismatch_examples": mismatches[:5],
    }


def fetch_user_details(user_ids: list[str], interval: str, max_pages: int = 50) -> dict[str, Any]:
    """Fetch user details, falling back to recent query data when the archive lags.

    ``data_complete`` means the returned detail is safe for report use.  The separate
    ``archive_data_complete`` retains the jobs watermark truth so consumers can keep their
    delayed repair scheduled and later replace provisional data with the archive.
    """
    key = (interval, tuple(sorted(user_ids)), max_pages)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])

    start_raw, end_raw = interval.split("/", 1)
    interval_start = parse_iso(start_raw)
    interval_end = parse_iso(end_raw)
    api = gc.AnalyticsApi(get_api())
    availability = presence_data_availability(api, interval_end)
    archive_complete = availability["complete"]

    if archive_complete is True:
        details, truncated = _async_details(api, user_ids, interval, max_pages)
        result = {
            "user_details": details,
            "truncated": truncated,
            "data_complete": not truncated,
            "archive_data_complete": True,
            "data_provisional": False,
            "data_source": "analytics_users_details_jobs",
            "data_available_until": availability["data_available_until"],
            "data_availability_note": availability["note"],
            "fallback_validation": None,
        }
    else:
        try:
            details, truncated, total_hits = _sync_details(api, user_ids, interval, max_pages)
            aggregates = _qualified_aggregates(api, user_ids, interval)
            validation = _reconcile(details, aggregates, interval_start, interval_end, truncated)
            usable_complete = validation["reconciled"]
            note = (
                "The archived users/details watermark has not reached the end of this interval. "
                "Used the recent synchronous user-detail query and reconciled active-user "
                "durations against user aggregates; the archive repair should replace it later."
                if usable_complete
                else "The recent synchronous user-detail fallback did not reconcile completely; treat presence totals as partial."
            )
            result = {
                "user_details": details,
                "truncated": truncated,
                "data_complete": usable_complete,
                "archive_data_complete": archive_complete,
                "data_provisional": usable_complete,
                "data_source": "analytics_users_details_query_reconciled" if usable_complete else "analytics_users_details_query_unreconciled",
                "data_available_until": availability["data_available_until"],
                "data_availability_note": note,
                "fallback_validation": {**validation, "total_hits": total_hits},
            }
        except Exception as exc:
            logger.warning("users/details synchronous fallback failed; using archive job response: %s", exc)
            details, truncated = _async_details(api, user_ids, interval, max_pages)
            result = {
                "user_details": details,
                "truncated": truncated,
                "data_complete": archive_complete is True and not truncated,
                "archive_data_complete": archive_complete,
                "data_provisional": False,
                "data_source": "analytics_users_details_jobs_partial",
                "data_available_until": availability["data_available_until"],
                "data_availability_note": availability["note"],
                "fallback_validation": {"reconciled": False, "error": str(exc)[:300]},
            }

    with _cache_lock:
        _cache[key] = (time.monotonic(), copy.deepcopy(result))
    return result
