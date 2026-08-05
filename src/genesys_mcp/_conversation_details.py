"""Conversation-detail collection with a recent-query fallback.

The async ``analytics/conversations/details/jobs`` archive can lag a completed
reporting day by hours.  When its watermark is behind, the synchronous details
query can already return the complete recent result set.  This module uses that
query, validates pagination against ``totalHits``, and keeps the archive state
separate so consumers can replace the provisional snapshot after settlement.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from typing import Any

import PureCloudPlatformClientV2 as gc

from genesys_mcp._availability import conversation_data_availability
from genesys_mcp._intervals import parse_iso
from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)

_SYNC_PAGE_SIZE = 100
_CACHE_TTL_SECONDS = 300
_cache_lock = threading.Lock()
_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}


def clear_conversation_details_cache() -> None:
    with _cache_lock:
        _cache.clear()


def run_conversation_details_job(filters_body: dict[str, Any], max_pages: int = 20) -> tuple[list[dict], bool]:
    """Submit, poll, and paginate the authoritative archive job."""
    api = gc.AnalyticsApi(get_api())
    submit = with_retry(api.post_analytics_conversations_details_jobs)(body=filters_body)
    job_id = getattr(submit, "job_id", None) or (to_dict(submit) or {}).get("jobId")
    if not job_id:
        raise RuntimeError("conversations/details/jobs submit returned no jobId")

    for _ in range(60):
        status = with_retry(api.get_analytics_conversations_details_job)(job_id=job_id)
        state = getattr(status, "state", None) or (to_dict(status) or {}).get("state")
        if state == "FULFILLED":
            break
        if state in ("FAILED", "CANCELLED", "EXPIRED"):
            raise RuntimeError(f"conv details job {job_id} terminated in state {state}")
        time.sleep(1)
    else:
        raise RuntimeError(f"conv details job {job_id} did not reach FULFILLED")

    conversations: list[dict] = []
    cursor: str | None = None
    truncated = False
    for _ in range(max_pages):
        kwargs: dict[str, Any] = {"job_id": job_id, "page_size": 1000}
        if cursor:
            kwargs["cursor"] = cursor
        page = to_dict(with_retry(api.get_analytics_conversations_details_job_results)(**kwargs)) or {}
        conversations.extend(page.get("conversations") or [])
        cursor = page.get("cursor")
        if not cursor:
            break
    else:
        truncated = cursor is not None
    return conversations, truncated


def _sync_details(api: Any, filters_body: dict[str, Any], max_pages: int) -> tuple[list[dict], dict[str, Any]]:
    conversations: list[dict] = []
    total_hits: int | None = None
    inconsistent_total_hits = False
    truncated = False

    for page_number in range(1, max_pages + 1):
        body = copy.deepcopy(filters_body)
        body["paging"] = {"pageSize": _SYNC_PAGE_SIZE, "pageNumber": page_number}
        response = to_dict(with_retry(api.post_analytics_conversations_details_query)(body=body)) or {}
        rows = response.get("conversations") or []
        reported_total = int(response.get("totalHits") or 0)
        if total_hits is None:
            total_hits = reported_total
        elif reported_total != total_hits:
            inconsistent_total_hits = True
            total_hits = max(total_hits, reported_total)
        conversations.extend(rows)
        if len(conversations) >= (total_hits or 0):
            break
        if not rows:
            truncated = True
            break
    else:
        truncated = len(conversations) < (total_hits or 0)

    ids = [row.get("conversationId") for row in conversations]
    valid_ids = [value for value in ids if isinstance(value, str) and value]
    unique_ids = set(valid_ids)
    missing_id_count = len(conversations) - len(valid_ids)
    duplicate_count = len(valid_ids) - len(unique_ids)
    expected = total_hits or 0
    reconciled = (
        not truncated
        and not inconsistent_total_hits
        and missing_id_count == 0
        and duplicate_count == 0
        and len(conversations) == expected
    )
    return conversations, {
        "reconciled": reconciled,
        "total_hits": expected,
        "fetched_count": len(conversations),
        "unique_conversation_count": len(unique_ids),
        "duplicate_count": duplicate_count,
        "missing_id_count": missing_id_count,
        "inconsistent_total_hits": inconsistent_total_hits,
        "truncated": truncated,
    }


def fetch_conversation_details(filters_body: dict[str, Any], max_pages: int = 20) -> dict[str, Any]:
    """Return complete archive detail or validated recent synchronous detail."""
    cache_key = (json.dumps(filters_body, sort_keys=True, separators=(",", ":")), max_pages)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])

    try:
        interval_end = parse_iso(str(filters_body["interval"]).split("/", 1)[1])
    except Exception as exc:
        raise ValueError(f"Invalid interval {filters_body.get('interval')!r}") from exc

    api = gc.AnalyticsApi(get_api())
    availability = conversation_data_availability(api, interval_end)
    archive_complete = availability["complete"]

    if archive_complete is True:
        conversations, truncated = run_conversation_details_job(filters_body, max_pages)
        result = {
            "conversations": conversations,
            "data_complete": not truncated,
            "archive_data_complete": True,
            "data_provisional": False,
            "data_source": "analytics_conversations_details_jobs",
            "data_available_until": availability["data_available_until"],
            "data_availability_note": availability["note"],
            "fallback_validation": None,
        }
    else:
        try:
            conversations, validation = _sync_details(api, filters_body, max_pages)
            usable_complete = validation["reconciled"]
            note = (
                "The archived conversations/details watermark has not reached the end of this interval. "
                "Used the recent synchronous conversation-detail query and retrieved every "
                "reported result page; the archive repair should replace it later."
                if usable_complete
                else "The recent synchronous conversation-detail query did not paginate completely; treat repeat-caller totals as partial."
            )
            result = {
                "conversations": conversations,
                "data_complete": usable_complete,
                "archive_data_complete": archive_complete,
                "data_provisional": usable_complete,
                "data_source": "analytics_conversations_details_query_recent" if usable_complete else "analytics_conversations_details_query_incomplete",
                "data_available_until": availability["data_available_until"],
                "data_availability_note": note,
                "fallback_validation": validation,
            }
        except Exception as exc:
            logger.warning("recent conversation-details fallback failed; using archive job response: %s", exc)
            conversations, truncated = run_conversation_details_job(filters_body, max_pages)
            result = {
                "conversations": conversations,
                "data_complete": archive_complete is True and not truncated,
                "archive_data_complete": archive_complete,
                "data_provisional": False,
                "data_source": "analytics_conversations_details_jobs_partial",
                "data_available_until": availability["data_available_until"],
                "data_availability_note": availability["note"],
                "fallback_validation": {"reconciled": False, "error": str(exc)[:300]},
            }

    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), copy.deepcopy(result))
    return result
