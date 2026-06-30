"""Shared helpers for accumulating Genesys analytics-aggregates responses.

Genesys's ``post_analytics_conversations_aggregates_query`` endpoint groups
results by the requested dimensions (e.g. ``userId``, ``mediaType``) and then
buckets the data by ``granularity``. When ``granularity`` is finer than the
interval — e.g. ``"P7D"`` over a 24-day interval — each group has multiple
buckets in ``r["data"]``. The pre-v0.9.1 pattern of writing
``out[uid][media] = stats_by_metric`` silently truncated to the last bucket,
undercounting answered/handled by ~Nx.

This module consolidates the accumulation logic so the same fix can be
trusted at every site (coaching.py, reports.py, future tools).
"""
from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable


def _add_months(base: datetime, months: int) -> datetime:
    """``base`` advanced by ``months`` whole calendar months, day-clamped.

    Anchored on ``base`` (not on the previous result) so repeated stepping
    doesn't drift the day-of-month downward — e.g. +1 month from Jan-31 is
    Feb-28, but +2 months is Mar-31, not Mar-28.
    """
    total = (base.year * 12 + (base.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return base.replace(year=year, month=month, day=day)


def split_interval_by_months(interval: str, max_months: int = 12) -> list[str]:
    """Split an ISO ``start/end`` interval into ≤``max_months``-month chunks.

    Genesys analytics-aggregate queries reject or silently cap very long
    intervals (multi-year), so a long span must be fired as several shorter
    sub-queries and stitched back together. Intervals already within the cap
    are returned unchanged as a single-element list, so callers can wrap every
    query unconditionally with zero behaviour change for normal windows.

    Boundaries are computed from ``start`` (``start + k·max_months`` months,
    day-clamped) so chunk sizes don't drift; the final chunk is clamped to
    ``end``.
    """
    start_s, end_s = interval.split("/", 1)
    start = datetime.fromisoformat(start_s.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_s.replace("Z", "+00:00"))
    if end <= start:
        return [interval]

    def _fmt(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    chunks: list[str] = []
    cur = start
    k = 1
    while cur < end:
        nxt = _add_months(start, k * max_months)
        if nxt >= end:
            nxt = end
        chunks.append(f"{_fmt(cur)}/{_fmt(nxt)}")
        cur = nxt
        k += 1
    return chunks or [interval]


def merge_aggregate_results(responses: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge several analytics-aggregate responses into one.

    Concatenates each response's ``results[].data[]`` buckets, grouped by the
    response group key (``userId``/``mediaType``/``queueId``/``routingStatus``
    etc.). Every downstream parser in this codebase already sums across the
    ``data`` buckets per group, so concatenation yields correct totals — the
    same result the (uncapped) single query would have produced.
    """
    by_group: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for resp in responses:
        for grp in (resp or {}).get("results") or []:
            key = repr(sorted((grp.get("group") or {}).items()))
            slot = by_group.get(key)
            if slot is None:
                slot = {"group": grp.get("group") or {}, "data": []}
                by_group[key] = slot
                order.append(key)
            slot["data"].extend(grp.get("data") or [])
    return {"results": [by_group[k] for k in order]}


def run_chunked_query(
    query_fn: Callable[[str], dict[str, Any]],
    interval: str,
    *,
    max_months: int = 12,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Run ``query_fn(interval)`` once, or chunked + merged for long intervals.

    ``query_fn`` takes an interval string and returns the parsed (``to_dict``)
    aggregates response. For intervals within ``max_months`` this is a single
    pass-through call (no behaviour change). For longer intervals the query is
    split, the chunks fire concurrently, and the responses are merged via
    :func:`merge_aggregate_results`.
    """
    chunks = split_interval_by_months(interval, max_months)
    if len(chunks) <= 1:
        return query_fn(interval)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(chunks))) as pool:
        responses = list(pool.map(query_fn, chunks))
    return merge_aggregate_results(responses)


def accumulate_metric_stats(buckets: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Sum ``count`` and ``sum`` across all buckets per metric; combine ``min``/``max``.

    Each bucket in the input has the Genesys shape::

        {"metrics": [{"metric": "tHandle", "stats": {"count": 100, "sum": 32_700_000, ...}}, ...]}

    Returns ``{metric_name: {"count": int, "sum": float, "min": float, "max": float}}``
    with ``count``/``sum`` summed across buckets and ``min``/``max`` combined
    via :func:`min` / :func:`max`. Stats fields that are ``None`` or absent
    are skipped (never coerced to zero, so missing data stays distinguishable
    from zero data downstream).

    Reuse the same helper at every call site so future fixes (e.g. handling
    a new stats field) happen in one place. Two known consumers as of v0.10:

    - ``coaching.py:_aggregates_for_users`` (fixed in v0.9.1)
    - ``reports.py:agent_quality_snapshot`` (fixed in v0.10)
    """
    accum: dict[str, dict[str, float]] = {}
    for bucket in buckets or []:
        for m in bucket.get("metrics") or []:
            metric = m["metric"]
            stats = m.get("stats") or {}
            slot = accum.setdefault(metric, {})
            for k in ("count", "sum"):
                if k in stats and stats[k] is not None:
                    slot[k] = slot.get(k, 0) + stats[k]
            if stats.get("min") is not None:
                slot["min"] = (
                    min(slot["min"], stats["min"]) if "min" in slot else stats["min"]
                )
            if stats.get("max") is not None:
                slot["max"] = (
                    max(slot["max"], stats["max"]) if "max" in slot else stats["max"]
                )
    return accum
