"""Agent utilization — routing-status durations + answered counts + productivity ratios.

v1.6+. Closes a gap surfaced by a real user ask: *"give me details of all
agents, how much on-queue time they had, how many calls and messages they
took, and a ratio."*

Pre-v1.6 the MCP could answer "how many answered" (`agent_performance`,
via the conversations/aggregates endpoint) but not "how long were they
on queue" — nothing in the codebase queried the
``/api/v2/analytics/users/aggregates/query`` endpoint that exposes
`tAgentRoutingStatus` durations.

This tool fires both endpoints concurrently and joins the results into
one row per agent, with three pre-computed productivity ratios.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._aggregates import run_chunked_query
from genesys_mcp._intervals import INTERVAL_HELP_STRING
from genesys_mcp._intervals import default_interval as _default_interval
from genesys_mcp._intervals import now_utc as _now_utc
from genesys_mcp.client import get_api, to_dict, with_retry
from genesys_mcp.naming import resolver

logger = logging.getLogger(__name__)


# Stable output keys for downstream renderers. ON_QUEUE/OFF_QUEUE are derived
# from tSystemPresence; the remaining values are tAgentRoutingStatus qualifiers.
_ROUTING_STATUSES = (
    "ON_QUEUE",
    "INTERACTING",
    "IDLE",
    "NOT_RESPONDING",
    "COMMUNICATING",
    "OFF_QUEUE",
)


def _routing_status_body(user_ids: list[str], interval: str) -> dict:
    """Build the users/aggregates body for routing-status durations."""
    return {
        "interval": interval,
        "groupBy": ["userId"],
        "filter": {
            "type": "and",
            "clauses": [
                {"type": "or", "predicates": [
                    {"dimension": "userId", "value": uid} for uid in user_ids
                ]},
            ],
        },
        "metrics": ["tAgentRoutingStatus", "tSystemPresence"],
    }


def _conversation_aggregates_body(user_ids: list[str], interval: str) -> dict:
    """Build the conversations/aggregates body — mirrors agent_performance.

    The outer-and-of-or filter shape and the (userId, mediaType) groupBy
    are the UI-parity contract pinned by ``tests/test_analytics_filters.py``.
    Pre-v0.2 the flat-OR shape silently undercounted by up to 8x.
    """
    return {
        "interval": interval,
        "groupBy": ["userId", "mediaType"],
        "filter": {
            "type": "and",
            "clauses": [
                {"type": "or", "predicates": [
                    {"dimension": "userId", "value": uid} for uid in user_ids
                ]},
            ],
        },
        "metrics": ["tAnswered", "tHandle"],
    }


def _parse_routing_status(resp: dict, user_ids: list[str]) -> dict[str, dict[str, int]]:
    """Reduce a users/aggregates response into per-user × routing-status seconds.

    Returns ``{user_id: {ON_QUEUE: seconds, INTERACTING: seconds, ...}}``.
    Every user in ``user_ids`` is initialised to all-zero so a downstream
    table renderer never sees a missing key.
    """
    by_user: dict[str, dict[str, int]] = {
        uid: {status: 0 for status in _ROUTING_STATUSES}
        for uid in user_ids
    }
    for grp in resp.get("results") or []:
        group_key = grp.get("group") or {}
        uid = group_key.get("userId")
        if uid not in by_user:
            continue
        for bucket in grp.get("data") or []:
            for m in bucket.get("metrics") or []:
                metric = m.get("metric")
                # Current users/aggregates responses carry the category in the
                # metric qualifier. Keep the old group dimension as a compatibility
                # fallback for previously captured fixtures/responses.
                qualifier = (
                    m.get("qualifier") or group_key.get("routingStatus") or ""
                ).upper()
                sum_seconds = int(float((m.get("stats") or {}).get("sum", 0) or 0) / 1000)
                if metric == "tAgentRoutingStatus" and qualifier in {
                    "INTERACTING", "IDLE", "NOT_RESPONDING", "COMMUNICATING",
                }:
                    by_user[uid][qualifier] += sum_seconds
                elif metric == "tSystemPresence":
                    if qualifier == "ON_QUEUE":
                        by_user[uid]["ON_QUEUE"] += sum_seconds
                    else:
                        by_user[uid]["OFF_QUEUE"] += sum_seconds
    return by_user


def _parse_conversation_aggregates(
    resp: dict, user_ids: list[str],
) -> dict[str, dict[str, dict[str, float]]]:
    """Reduce conversations/aggregates into per-user × media answered + handle.

    Returns ``{user_id: {media: {"answered": int, "handle_seconds": float}}}``.
    """
    by_user: dict[str, dict[str, dict[str, float]]] = {
        uid: {} for uid in user_ids
    }
    for grp in resp.get("results") or []:
        group_key = grp.get("group") or {}
        uid = group_key.get("userId")
        media = (group_key.get("mediaType") or "unknown").lower()
        if uid not in by_user:
            continue
        media_row = by_user[uid].setdefault(
            media, {"answered": 0, "handle_seconds": 0.0},
        )
        for bucket in grp.get("data") or []:
            for m in bucket.get("metrics") or []:
                name = m.get("metric")
                stats = m.get("stats") or {}
                if name == "tAnswered":
                    media_row["answered"] += int(stats.get("count", 0) or 0)
                elif name == "tHandle":
                    media_row["handle_seconds"] += float(stats.get("sum", 0) or 0) / 1000.0
    return by_user


def _compute_ratios(
    on_queue_seconds: int, total_answered: int, total_handle_seconds: float,
    voice_answered: int, message_answered: int,
) -> dict[str, float | None]:
    """Compute the three productivity ratios. Guards every divide-by-zero."""
    if on_queue_seconds <= 0:
        ipoh = None
        occupancy = None
    else:
        ipoh = round(total_answered / (on_queue_seconds / 3600.0), 2)
        occupancy = round(total_handle_seconds / on_queue_seconds * 100.0, 1)
    voice_to_message = (
        round(voice_answered / message_answered, 2)
        if message_answered > 0
        else None
    )
    return {
        "interactions_per_on_queue_hour": ipoh,
        "occupancy_pct": occupancy,
        "voice_to_message_ratio": voice_to_message,
    }


def _build_user_row(
    uid: str,
    user_name: str | None,
    routing: dict[str, int],
    conv_by_media: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Assemble one per-agent row."""
    voice = conv_by_media.get("voice", {"answered": 0, "handle_seconds": 0.0})
    message = conv_by_media.get("message", {"answered": 0, "handle_seconds": 0.0})
    callback = conv_by_media.get("callback", {"answered": 0, "handle_seconds": 0.0})

    voice_answered = int(voice["answered"])
    message_answered = int(message["answered"])
    callback_answered = int(callback["answered"])
    total_answered = voice_answered + message_answered + callback_answered

    voice_handle = float(voice["handle_seconds"])
    message_handle = float(message["handle_seconds"])
    callback_handle = float(callback["handle_seconds"])
    total_handle = voice_handle + message_handle + callback_handle

    ratios = _compute_ratios(
        on_queue_seconds=routing["ON_QUEUE"],
        total_answered=total_answered,
        total_handle_seconds=total_handle,
        voice_answered=voice_answered,
        message_answered=message_answered,
    )

    return {
        "user_id": uid,
        "user_name": user_name,
        "on_queue_seconds": routing["ON_QUEUE"],
        "interacting_seconds": routing["INTERACTING"],
        "idle_seconds": routing["IDLE"],
        "not_responding_seconds": routing["NOT_RESPONDING"],
        "communicating_seconds": routing["COMMUNICATING"],
        "off_queue_seconds": routing["OFF_QUEUE"],
        "voice_answered": voice_answered,
        "message_answered": message_answered,
        "callback_answered": callback_answered,
        "total_answered": total_answered,
        "voice_handle_seconds": round(voice_handle, 1),
        "message_handle_seconds": round(message_handle, 1),
        "total_handle_seconds": round(total_handle, 1),
        **ratios,
    }


def _sort_users(rows: list[dict]) -> list[dict]:
    """Sort by interactions_per_on_queue_hour desc; nulls last."""
    return sorted(
        rows,
        key=lambda r: (
            r["interactions_per_on_queue_hour"] is None,
            -(r["interactions_per_on_queue_hour"] or 0),
        ),
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def agent_utilization(
        user_ids: list[str] = Field(
            description=(
                "User ids to report on. Resolve names via `list_users` or "
                "`find_user` first. Required — the routing-status query "
                "filters on userId."
            ),
        ),
        interval: str | None = Field(
            default=None,
            description=INTERVAL_HELP_STRING,
        ),
        mode: str = Field(
            default="summary",
            description=(
                "Response shape. 'summary' (default) returns just the per-"
                "agent rows + top-level interval/as_of_utc. 'full' adds "
                "`_raw` with both source aggregates responses for callers "
                "that want to inspect the underlying buckets."
            ),
        ),
    ) -> dict:
        """Per-agent productivity: routing-status durations + answered counts + ratios.

        v1.6+. Combines two Genesys analytics endpoints in one call:

        - ``/api/v2/analytics/users/aggregates/query`` → qualified system-
          presence and routing-status durations (ON_QUEUE / INTERACTING / IDLE /
          NOT_RESPONDING / COMMUNICATING / OFF_QUEUE seconds per agent)
        - ``/api/v2/analytics/conversations/aggregates/query`` → voice /
          message / callback answered counts + handle time per agent
          (same body as ``agent_performance``, matches the Genesys UI)

        Both queries fire concurrently, then join by ``userId`` into one
        row per agent. Three productivity ratios are computed:

        - ``interactions_per_on_queue_hour`` (HEADLINE) — total_answered /
          (on_queue_seconds / 3600). A throughput rate that controls for
          variable AHT between agents.
        - ``occupancy_pct`` — total_handle / on_queue × 100. The CC-industry
          standard "of the time you were available, what fraction were you
          actually working an interaction". 70-85% is typical.
        - ``voice_to_message_ratio`` — voice_answered / message_answered.
          A channel-mix ratio (``null`` when an agent took no messages).

        Sorted by ``interactions_per_on_queue_hour`` descending by default;
        agents with no on-queue time (``null`` rate) appear at the bottom.

        Top-level response carries the v1.5 contract: ``interval`` and
        ``as_of_utc`` at the top of the response so persisted-file readers
        see the window immediately.

        Soft-fails on 403 against the routing-status endpoint (some tenants
        restrict ``analytics:userAggregate:view``): the routing-status seconds
        all degrade to 0 with a top-level ``routing_status_scope_available:
        false`` flag, and the conversation-side answered counts still
        populate so the response is still partially useful.

        Callback media note: under customer-first callbacks ``callback_answered``
        is structurally ~0 for every agent — the bridged call is a voice session,
        so callback work already sits inside voice answered/handle. Do not read
        the callback column as "agents aren't doing callbacks".
        """
        if not user_ids:
            raise ValueError("user_ids must contain at least one id.")
        if mode not in ("summary", "full"):
            raise ValueError(
                f"agent_utilization.mode must be 'summary' or 'full', got {mode!r}"
            )

        resolved_interval = interval or _default_interval(7)
        api = gc.AnalyticsApi(get_api())

        # Capture exceptions per call so we can apply different policies:
        # routing-status 403 → soft-fail (degraded but useful response);
        # conversation hard-fail → propagate. Both queries chunk long intervals
        # (multi-year staffing trends) into ≤12-month sub-queries and merge.
        routing_result: dict[str, Any] = {"scope_available": True}
        conv_result: dict[str, Any] = {}

        def _fetch_routing() -> None:
            def q(iv: str) -> dict:
                resp = with_retry(api.post_analytics_users_aggregates_query)(
                    _routing_status_body(user_ids, iv)
                )
                return to_dict(resp) or {}
            try:
                routing_result["raw"] = run_chunked_query(q, resolved_interval)
            except Exception as exc:
                if getattr(exc, "status", None) == 403:
                    logger.info(
                        "agent_utilization: routing-status scope unavailable; "
                        "degrading to zeros (%s)",
                        exc,
                    )
                    routing_result["scope_available"] = False
                    routing_result["raw"] = {"results": []}
                    return
                raise

        def _fetch_conversations() -> None:
            def q(iv: str) -> dict:
                resp = with_retry(api.post_analytics_conversations_aggregates_query)(
                    _conversation_aggregates_body(user_ids, iv)
                )
                return to_dict(resp) or {}
            conv_result["raw"] = run_chunked_query(q, resolved_interval)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_fetch_routing), pool.submit(_fetch_conversations)]
            for fut in futures:
                fut.result()  # propagate any non-soft-fail exception

        routing_by_user = _parse_routing_status(routing_result["raw"], user_ids)
        conv_by_user = _parse_conversation_aggregates(conv_result["raw"], user_ids)
        names = resolver.user_names(user_ids)

        rows = [
            _build_user_row(
                uid=uid,
                user_name=names.get(uid),
                routing=routing_by_user[uid],
                conv_by_media=conv_by_user.get(uid, {}),
            )
            for uid in user_ids
        ]
        rows = _sort_users(rows)

        out: dict[str, Any] = {
            "interval": resolved_interval,
            "as_of_utc": _now_utc().isoformat().replace("+00:00", "Z"),
            "mode": mode,
            "sort_by": "interactions_per_on_queue_hour_desc",
            "routing_status_scope_available": routing_result["scope_available"],
            "user_count": len(user_ids),
            "users": rows,
        }
        if not routing_result["scope_available"]:
            out["routing_status_unavailable_note"] = (
                "On-queue/routing-status time is unavailable because the OAuth "
                "client lacks the analytics:userAggregate:view scope — this is a "
                "MISSING SCOPE, not a tenant block or a broken query. Ask the "
                "Genesys admin to grant analytics:userAggregate:view to the "
                "OAuth client; routing-status seconds and the derived "
                "ratios then populate. Answered counts already reflect actual data."
            )
            for r in rows:
                r["interactions_per_on_queue_hour"] = None
                r["occupancy_pct"] = None
        if mode == "full":
            out["_raw"] = {
                "routing_status": routing_result["raw"],
                "conversations": conv_result["raw"],
            }
        return out
