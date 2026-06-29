"""Wrap-up code distribution + period-over-period trend.

v1.9+. Closes the gap that surfaced as *"Wrap-up code distribution not
available in this run"* in `cc-monthly-report` / `cc-daily-brief`. Pre-v1.9
no MCP tool aggregated wrap-up codes via the Genesys analytics endpoint
— the only rollup path (`repeat_caller_deep_dive.org_rollup.top_dispositions`)
walked one conversation at a time and only covered the repeat-caller cohort.

This tool uses `groupBy: ["wrapUpCode"]` on the conversations-aggregates
endpoint — confirmed via the platform-api schema as a valid groupBy
dimension. One API call returns per-code counts for the entire period.
A second parallel call against the prior interval powers the trend
block (largest movers, new / retired codes).

Wrap-up code UUIDs are resolved to human-readable names via the existing
``RoutingApi.get_routing_wrapupcodes`` catalogue. Cached process-lifetime
(same pattern as `directory._load_presence_label_cache` v1.3 and
`tools/timeoff._fetch_activity_codes` v1.7).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from typing import Any

import PureCloudPlatformClientV2 as gc
from PureCloudPlatformClientV2.rest import ApiException
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._envelopes import soft_fail_envelope
from genesys_mcp._intervals import INTERVAL_HELP_STRING
from genesys_mcp._intervals import default_interval as _default_interval
from genesys_mcp._intervals import now_utc as _now_utc
from genesys_mcp._intervals import parse_iso as _parse_iso
from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


# Process-lifetime cache of {wrapup_code_id: {id, name, division_id, description}}.
_WRAPUP_CODE_CACHE: dict[str, dict] = {}
_WRAPUP_CACHE_LOADED: bool = False


def _load_wrapup_code_cache() -> dict[str, dict]:
    """Fetch + cache the full wrap-up code catalogue."""
    global _WRAPUP_CACHE_LOADED
    if _WRAPUP_CACHE_LOADED:
        return _WRAPUP_CODE_CACHE
    api = gc.RoutingApi(get_api())
    page_number = 1
    while True:
        resp = with_retry(api.get_routing_wrapupcodes)(
            page_size=200, page_number=page_number,
        )
        entities = getattr(resp, "entities", None) or []
        for e in entities:
            cid = getattr(e, "id", None)
            if not cid:
                continue
            _WRAPUP_CODE_CACHE[cid] = {
                "id": cid,
                "name": getattr(e, "name", None),
                "division_id": getattr(getattr(e, "division", None), "id", None),
                "description": getattr(e, "description", None),
            }
        if not entities or len(entities) < 200:
            break
        page_number += 1
        if page_number > 25:
            break
    _WRAPUP_CACHE_LOADED = True
    logger.info(
        "wrap-up code cache loaded with %d entries", len(_WRAPUP_CODE_CACHE),
    )
    return _WRAPUP_CODE_CACHE


def _prior_interval(interval: str) -> str:
    """Compute the prior interval — same length, immediately before."""
    start_iso, end_iso = interval.split("/", 1)
    start = _parse_iso(start_iso).astimezone(timezone.utc)
    end = _parse_iso(end_iso).astimezone(timezone.utc)
    length = end - start
    prior_end = start
    prior_start = prior_end - length
    return (
        prior_start.isoformat().replace("+00:00", "Z")
        + "/"
        + prior_end.isoformat().replace("+00:00", "Z")
    )


def _build_body(
    *,
    interval: str,
    queue_ids: list[str] | None,
    user_ids: list[str] | None,
    media_types: list[str] | None,
) -> dict:
    """Build the aggregates body with the canonical outer-and-of-or filter shape."""
    clauses: list[dict] = []
    if queue_ids:
        clauses.append({
            "type": "or",
            "predicates": [
                {"dimension": "queueId", "value": qid} for qid in queue_ids
            ],
        })
    if user_ids:
        clauses.append({
            "type": "or",
            "predicates": [
                {"dimension": "userId", "value": uid} for uid in user_ids
            ],
        })
    if media_types:
        clauses.append({
            "type": "or",
            "predicates": [
                {"dimension": "mediaType", "value": m} for m in media_types
            ],
        })
    body: dict[str, Any] = {
        "interval": interval,
        "groupBy": ["wrapUpCode"],
        "metrics": ["tHandle"],
    }
    if clauses:
        body["filter"] = {"type": "and", "clauses": clauses}
    return body


def _parse_counts(resp: dict) -> dict[str, int]:
    """Reduce a conversations/aggregates response into ``{wrapup_code_id: count}``."""
    by_code: dict[str, int] = {}
    for grp in resp.get("results") or []:
        group_key = grp.get("group") or {}
        code_id = group_key.get("wrapUpCode")
        if not code_id:
            continue
        total = 0
        for bucket in grp.get("data") or []:
            for m in bucket.get("metrics") or []:
                if m.get("metric") == "tHandle":
                    stats = m.get("stats") or {}
                    total += int(stats.get("count", 0) or 0)
        by_code[code_id] = total
    return by_code


def _movement(delta_pct: float | None) -> str:
    """Categorise a delta_pct into up / down / flat (|·| < 2%)."""
    if delta_pct is None:
        return "flat"
    if delta_pct > 2.0:
        return "up"
    if delta_pct < -2.0:
        return "down"
    return "flat"


def _build_distribution(
    current_counts: dict[str, int],
    prior_counts: dict[str, int],
    codes: dict[str, dict],
    top_n: int,
    include_trend: bool,
) -> tuple[list[dict], bool, list[str], list[str]]:
    """Build the per-code distribution + truncation flag + new/retired lists."""
    current_total = sum(current_counts.values())
    all_ids = set(current_counts) | set(prior_counts)

    rows: list[dict] = []
    for cid in all_ids:
        cur = current_counts.get(cid, 0)
        prior = prior_counts.get(cid, 0)
        catalogue = codes.get(cid) or {}
        row: dict[str, Any] = {
            "wrapup_code_id": cid,
            "name": catalogue.get("name") or f"<unknown {cid[:8]}>",
            "count": cur,
            "percentage": round(cur / current_total * 100, 1) if current_total else 0.0,
        }
        if include_trend:
            delta = cur - prior
            if prior > 0:
                delta_pct = round(delta / prior * 100, 1)
            else:
                delta_pct = None
            row.update({
                "prior_count": prior,
                "delta": delta,
                "delta_pct": delta_pct,
                "movement": _movement(delta_pct),
            })
        rows.append(row)

    rows.sort(key=lambda r: (r["count"] == 0, -r["count"]))

    truncated = False
    new_codes: list[str] = []
    retired_codes: list[str] = []
    if include_trend:
        for r in rows:
            if r["prior_count"] == 0 and r["count"] > 0:
                new_codes.append(r["name"])
            elif r["prior_count"] > 0 and r["count"] == 0:
                retired_codes.append(r["name"])

    if len(rows) > top_n:
        truncated = True
        keep, rest = rows[:top_n], rows[top_n:]
        other_count = sum(r["count"] for r in rest)
        other_prior = sum(r.get("prior_count", 0) for r in rest)
        other_pct = round(other_count / current_total * 100, 1) if current_total else 0.0
        other_row: dict[str, Any] = {
            "wrapup_code_id": None,
            "name": "Other (truncated)",
            "count": other_count,
            "percentage": other_pct,
        }
        if include_trend:
            other_delta = other_count - other_prior
            if other_prior > 0:
                other_delta_pct = round(other_delta / other_prior * 100, 1)
            else:
                other_delta_pct = None
            other_row.update({
                "prior_count": other_prior,
                "delta": other_delta,
                "delta_pct": other_delta_pct,
                "movement": _movement(other_delta_pct),
            })
        rows = keep + [other_row]

    return rows, truncated, new_codes, retired_codes


def _largest_movers(rows: list[dict], top: int = 5) -> list[dict]:
    """Top N rows by |delta_pct|, excluding the Other rollup."""
    candidates = [
        r for r in rows
        if r.get("wrapup_code_id")
        and r.get("delta_pct") is not None
    ]
    candidates.sort(key=lambda r: abs(r["delta_pct"]), reverse=True)
    return [
        {
            "name": r["name"],
            "delta_pct": r["delta_pct"],
            "movement": r["movement"],
        }
        for r in candidates[:top]
    ]


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def wrap_up_code_distribution(
        interval: str | None = Field(
            default=None,
            description=INTERVAL_HELP_STRING,
        ),
        queue_ids: list[str] | None = Field(
            default=None,
            description=(
                "Optional queue filter. Pass a list to restrict to those "
                "queues (OR'd together). Omit for org-wide."
            ),
        ),
        user_ids: list[str] | None = Field(
            default=None,
            description=(
                "Optional user filter. Pass to scope per-agent (e.g. for "
                "coaching prep). Omit for org-wide."
            ),
        ),
        media_types: list[str] | None = Field(
            default=None,
            description=(
                "Optional media-type filter: one or more of 'voice', "
                "'message', 'callback', 'email'. Omit for all media."
            ),
        ),
        include_trend: bool = Field(
            default=True,
            description=(
                "When true (default), the tool also queries the immediately-"
                "prior interval of the same length and surfaces per-code "
                "deltas + a largest-movers block. Set false for a one-shot "
                "distribution if you don't need trend context."
            ),
        ),
        top_n: int = Field(
            default=25, ge=1, le=200,
            description=(
                "Cap on distinct rows in the distribution. Codes beyond "
                "the top N (by current-period count) are rolled up into a "
                "single 'Other (truncated)' row; ``totals.truncated`` flags "
                "when this happened."
            ),
        ),
        mode: str = Field(
            default="summary",
            description=(
                "Response shape. 'summary' (default) returns the rolled-up "
                "distribution + trend block. 'full' adds `_raw` with the "
                "underlying aggregates responses."
            ),
        ),
    ) -> dict:
        """Per-wrap-up-code conversation distribution + period-over-period trend.

        v1.9+. Answers *"what wrap-up codes did agents use this period, and
        which ones moved most vs the prior period?"* in **one API call** to
        Genesys (or two when ``include_trend=True``). Replaces the slow
        N+1 per-conversation walk that was the only previous path to a
        wrap-up rollup.

        Uses ``post_analytics_conversations_aggregates_query`` with
        ``groupBy: ["wrapUpCode"]`` and metric ``tHandle.count`` (count of
        handled conversations carrying each wrap-up code). The filter
        shape mirrors ``agent_performance`` / ``queue_performance`` (outer
        ``and`` of ``or`` clauses) so numbers match the Genesys UI exactly.

        Returns:

        - **`totals`** — ``conversation_count`` (sum across all codes),
          ``distinct_code_count``, ``truncated``.
        - **`distribution`** — one row per wrap-up code (capped at
          ``top_n`` with an ``Other (truncated)`` rollup). Each row carries
          ``count``, ``percentage`` of total, and (when
          ``include_trend=True``) ``prior_count``, ``delta``, ``delta_pct``,
          ``movement`` (``up`` / ``down`` / ``flat``).
        - **`trend`** (when ``include_trend=True``) — ``prior_interval``,
          ``largest_movers`` (top 5 by absolute ``delta_pct``),
          ``new_codes_this_period``, ``retired_codes``.

        Wrap-up code UUIDs are resolved to human-readable names via the
        cached ``RoutingApi.get_routing_wrapupcodes`` catalogue (process-
        lifetime cache; restart the MCP server to refresh after an admin
        rename).

        Top-level response carries the v1.5 contract: ``interval`` +
        ``as_of_utc`` at the top so persisted-file readers see the window
        immediately.

        Needs ``analytics:conversationAggregate:view`` (typically bundled
        into ``analytics:readonly``) and ``routing:wrapupCode:view``.
        """
        if mode not in ("summary", "full"):
            raise ValueError(
                f"wrap_up_code_distribution.mode must be 'summary' or 'full', got {mode!r}"
            )

        resolved_interval = interval or _default_interval(7)
        prior_interval_str = _prior_interval(resolved_interval) if include_trend else None

        api = gc.AnalyticsApi(get_api())

        body_current = _build_body(
            interval=resolved_interval,
            queue_ids=queue_ids,
            user_ids=user_ids,
            media_types=media_types,
        )
        result_holder: dict[str, dict] = {}

        def _fetch_current() -> None:
            resp = with_retry(api.post_analytics_conversations_aggregates_query)(body_current)
            result_holder["current"] = to_dict(resp) or {}

        def _fetch_prior() -> None:
            body_prior = _build_body(
                interval=prior_interval_str,
                queue_ids=queue_ids,
                user_ids=user_ids,
                media_types=media_types,
            )
            resp = with_retry(api.post_analytics_conversations_aggregates_query)(body_prior)
            result_holder["prior"] = to_dict(resp) or {}

        # v1.12.1: catch ApiException from the aggregates calls and return a
        # canonical soft-fail envelope so the skill can render a visible
        # missing-scope callout instead of leaving the wrap-up section blank
        # and inviting an LLM-narrative fallback.
        try:
            if include_trend:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(_fetch_current), pool.submit(_fetch_prior)]
                    for fut in futures:
                        fut.result()
            else:
                _fetch_current()
                result_holder["prior"] = {"results": []}
        except ApiException as exc:
            return soft_fail_envelope(
                status=int(getattr(exc, "status", 0) or 500),
                kind="wrap_up_code_distribution",
                message=(
                    "Wrap-up aggregates query failed: "
                    f"{getattr(exc, 'reason', None) or type(exc).__name__}. "
                    "If the status is 403, grant the OAuth client "
                    "'analytics:conversationAggregate:view' (typically bundled "
                    "into 'analytics:readonly')."
                ),
                interval=resolved_interval,
                http_body=(getattr(exc, "body", None) or "")[:500] if getattr(exc, "body", None) else None,
            )

        current_counts = _parse_counts(result_holder["current"])
        prior_counts = _parse_counts(result_holder["prior"]) if include_trend else {}
        # v1.12.1: catalogue load may itself 403 on routing:wrapupCode:view.
        # Treat that as a *partial* success — we still have the aggregates,
        # we just can't resolve UUIDs to names. The distribution rows fall
        # back to "<unknown {cid[:8]}>" labels (existing behaviour).
        try:
            codes = _load_wrapup_code_cache()
        except ApiException as exc:
            logger.warning(
                "wrap-up code catalogue 403'd (need 'routing:wrapupCode:view'): %s",
                getattr(exc, "reason", None) or type(exc).__name__,
            )
            codes = {}

        rows, truncated, new_codes, retired_codes = _build_distribution(
            current_counts=current_counts,
            prior_counts=prior_counts,
            codes=codes,
            top_n=top_n,
            include_trend=include_trend,
        )

        out: dict[str, Any] = {
            "interval": resolved_interval,
            "as_of_utc": _now_utc().isoformat().replace("+00:00", "Z"),
            "mode": mode,
            "filters": {
                "queue_ids": list(queue_ids) if queue_ids else None,
                "user_ids": list(user_ids) if user_ids else None,
                "media_types": list(media_types) if media_types else None,
            },
            "totals": {
                "conversation_count": sum(current_counts.values()),
                "distinct_code_count": len(current_counts),
                "truncated": truncated,
            },
            "distribution": rows,
            "trend": None,
        }
        if include_trend:
            out["trend"] = {
                "prior_interval": prior_interval_str,
                "largest_movers": _largest_movers(rows),
                "new_codes_this_period": new_codes,
                "retired_codes": retired_codes,
            }
        if mode == "full":
            out["_raw"] = result_holder
        return out
