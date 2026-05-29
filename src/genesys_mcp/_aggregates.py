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

from typing import Any


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
