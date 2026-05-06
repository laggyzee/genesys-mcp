"""Analytics tools: queue & agent performance, real-time observation."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _agent_message_volume(user_ids: list[str], interval: str) -> dict[str, dict]:
    """Per-agent message-channel conversation count + handle time for the interval.

    The conversations-aggregates ``groupBy=userId`` path doesn't return ``nConversations``
    for the message media bucket — only time stats — so per-agent message attribution
    isn't possible from the aggregates endpoint. This helper walks the full conversation
    details for ``mediaType=message`` via the async job endpoint and counts per-agent
    participation directly from the participants array.

    Returns ``{user_id: {"conversations": int, "handle_ms": float}}`` for each requested
    user. Users with no message activity get zeros (not omitted) so the merge into
    agent_performance is safe.
    """
    # Local import to avoid circular dependency with reports module.
    from genesys_mcp.tools.reports import _run_conv_details_job

    user_set = set(user_ids)
    counts: dict[str, dict] = {uid: {"conversations": 0, "handle_ms": 0.0} for uid in user_ids}

    body = {
        "interval": interval,
        "order": "asc",
        "orderBy": "conversationStart",
        "segmentFilters": [{
            "type": "and",
            "predicates": [
                {"type": "dimension", "dimension": "mediaType",
                 "operator": "matches", "value": "message"},
            ],
        }],
    }
    try:
        convs = _run_conv_details_job(body, max_pages=200)
    except Exception as exc:
        logger.warning("agent_performance: message volume pull failed (%s); message bucket will be empty", exc)
        return counts

    for c in convs:
        # For each conversation, find which of OUR users handled it. Multiple handlers
        # on the same conversation each get +1; their handle time is from their own
        # session segments.
        users_in_conv: set[str] = set()
        per_user_handle_ms: dict[str, float] = defaultdict(float)
        for p in c.get("participants") or []:
            uid = p.get("userId")
            purpose = p.get("purpose")
            if uid not in user_set:
                continue
            if purpose not in ("agent", "user"):
                continue
            users_in_conv.add(uid)
            for s in p.get("sessions") or []:
                for seg in s.get("segments") or []:
                    if seg.get("segmentType") not in ("interact", "wrapup"):
                        continue
                    st_raw = seg.get("segmentStart")
                    en_raw = seg.get("segmentEnd")
                    if not st_raw or not en_raw:
                        continue
                    try:
                        per_user_handle_ms[uid] += (
                            _parse_iso(en_raw) - _parse_iso(st_raw)
                        ).total_seconds() * 1000
                    except Exception:
                        continue
        for uid in users_in_conv:
            counts[uid]["conversations"] += 1
            counts[uid]["handle_ms"] += per_user_handle_ms.get(uid, 0.0)

    total_attributed = sum(v["conversations"] for v in counts.values())
    logger.info(
        "agent_performance: %d message conversations attributed across %d users (from %d total)",
        total_attributed,
        sum(1 for v in counts.values() if v["conversations"] > 0),
        len(convs),
    )
    return counts


def _default_interval(days: int = 7) -> str:
    """ISO-8601 interval 'start/end' covering the last N days up to now (UTC)."""
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    return f"{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}/{end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"


def _attach_derived_metrics(resp: dict) -> None:
    """Walk a conversation-aggregates response and attach a 'derived' dict to each bucket.

    nConnected in Genesys is not the same as 'answered' — the UI's 'Answer' column equals
    tAnswered.count. This helper computes the derived fields that ops users actually want.
    """
    for result in resp.get("results") or []:
        for bucket in result.get("data") or []:
            stats = {m["metric"]: m.get("stats") or {} for m in bucket.get("metrics") or []}
            offered = stats.get("nOffered", {}).get("count", 0) or 0
            over = stats.get("nOverSla", {}).get("count", 0) or 0
            answered = stats.get("tAnswered", {}).get("count", 0) or 0
            abandoned = stats.get("tAbandon", {}).get("count", 0) or 0
            a_sum = stats.get("tAnswered", {}).get("sum", 0) or 0
            w_sum = stats.get("tWait", {}).get("sum", 0) or 0
            w_cnt = stats.get("tWait", {}).get("count", 0) or 0
            h_sum = stats.get("tHandle", {}).get("sum", 0) or 0
            h_cnt = stats.get("tHandle", {}).get("count", 0) or 0

            def pct(n: float, d: float) -> float | None:
                return round(n / d * 100, 1) if d else None

            def secs(s: float, c: float) -> float | None:
                return round(s / c / 1000, 1) if c else None

            bucket["derived"] = {
                "answered": answered,
                "abandoned": abandoned,
                "answered_pct": pct(answered, offered),
                "abandoned_pct": pct(abandoned, offered),
                "service_level_pct": pct(max(answered - over, 0), offered),
                "avg_wait_s": secs(w_sum, w_cnt),
                "avg_answer_s": secs(a_sum, answered),
                "avg_handle_s": secs(h_sum, h_cnt),
            }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def queue_observation(
        queue_ids: list[str] = Field(
            description="Queue ids to observe (at least one required — the Genesys observations API does not support empty filters).",
        ),
    ) -> dict:
        """Real-time snapshot: interactions waiting, agents on-queue, agents interacting, etc.

        Returns aggregate counts per queue and per media type. Use list_queues first to resolve ids.
        """
        if not queue_ids:
            raise ValueError("queue_ids must contain at least one id. Use list_queues first.")
        api = gc.AnalyticsApi(get_api())
        body = {
            "filter": {
                "type": "or",
                "predicates": [
                    {"dimension": "queueId", "value": qid} for qid in queue_ids
                ],
            },
            "metrics": [
                "oWaiting",
                "oInteracting",
                "oOnQueueUsers",
                "oActiveUsers",
            ],
        }
        resp = with_retry(api.post_analytics_queues_observations_query)(body)
        return to_dict(resp)

    @mcp.tool()
    def queue_performance(
        queue_ids: list[str] = Field(
            description="Queue ids to report on (required)."
        ),
        interval: str | None = Field(
            default=None,
            description="ISO-8601 interval 'start/end'. Defaults to last 7 days.",
        ),
        granularity: str = Field(
            default="P1D",
            description="Bucket size, ISO-8601 duration. 'P1D' = daily, 'PT1H' = hourly.",
        ),
        group_by_media: bool = Field(
            default=True,
            description="If true, also group by mediaType so voice/callback/message/email are split.",
        ),
    ) -> dict:
        """Aggregate queue performance per bucket, with derived answered/abandoned/SLA fields.

        Raw Genesys metrics requested:
          - nOffered      — offered interactions (matches UI 'Offer' column)
          - nConnected    — segment connects; NOT the same as 'answered', do not use alone
          - nTransferred  — interactions transferred
          - nOverSla      — answered interactions that breached the SLA threshold
          - tAnswered     — time-to-answer histogram; count = answered interactions
          - tAbandon      — time-to-abandon histogram; count = abandoned interactions
          - tWait, tHandle, tTalk — other time histograms

        Derived fields added to each bucket (under 'derived' key):
          - answered       = tAnswered.count            (matches UI 'Answer' column)
          - abandoned      = tAbandon.count             (matches UI 'Abandon' column)
          - answered_pct   = answered / nOffered
          - abandoned_pct  = abandoned / nOffered
          - service_level  = (answered - nOverSla) / nOffered   (matches UI 'Service Level')
          - avg_wait_s     = tWait.sum / tWait.count / 1000
          - avg_answer_s   = tAnswered.sum / tAnswered.count / 1000  (ASA)
          - avg_handle_s   = tHandle.sum / tHandle.count / 1000

        Callback media note: callbacks do not populate tAnswered/tAbandon. For callbacks,
        nOffered = callbacks scheduled, nConnected = callbacks where customer was reached
        and bridged to agent (customer-first flow). Derived answered/abandoned will be 0
        on callback rows — treat nConnected / nOffered as the connect rate instead.

        Defaults to daily granularity, last 7 days, grouped by queue + media type.
        Use list_queues first to resolve queue ids.
        """
        api = gc.AnalyticsApi(get_api())
        group_by = ["queueId", "mediaType"] if group_by_media else ["queueId"]
        body = {
            "interval": interval or _default_interval(7),
            "granularity": granularity,
            "groupBy": group_by,
            "filter": {
                "type": "or",
                "predicates": [
                    {"dimension": "queueId", "value": qid} for qid in queue_ids
                ],
            },
            "metrics": [
                "nOffered",
                "nConnected",
                "nTransferred",
                "nOverSla",
                "tHandle",
                "tTalk",
                "tAnswered",
                "tAbandon",
                "tWait",
            ],
        }
        resp = with_retry(api.post_analytics_conversations_aggregates_query)(body)
        out = to_dict(resp)
        _attach_derived_metrics(out)
        return out

    @mcp.tool()
    def queue_estimated_wait_time(
        queue_ids: list[str] = Field(
            description="Queue ids to fetch EWTs for. Use list_queues first to resolve names → ids.",
        ),
        media_type: str = Field(
            default="call",
            description="Media type: 'call' (voice), 'callback', 'message', 'email', 'chat'. Defaults to 'call'.",
        ),
    ) -> dict:
        """Genesys' own model-based estimated wait time per queue (AI-adjusted AHT formula).

        This is the value Genesys uses for IVR announcements and routing decisions. Prefer
        this over computing EWT from queue_performance aggregates — the model accounts for
        agent availability, AHT trends, and queue position dynamics.

        Returns one row per queue with `estimated_wait_time_seconds`. If a queue has no
        forecastable EWT (e.g. no agents skilled, queue inactive), Genesys returns an empty
        results list and we surface that as `estimated_wait_time_seconds: null`.

        Endpoint: GET /api/v2/routing/queues/{queueId}/mediatypes/{mediaType}/estimatedwaittime
        """
        api = get_api()
        out = []
        for qid in queue_ids:
            path = f"/api/v2/routing/queues/{qid}/mediatypes/{media_type}/estimatedwaittime"
            try:
                resp = with_retry(lambda: api.call_api(
                    resource_path=path,
                    method="GET",
                    query_params={},
                    header_params={"Accept": "application/json"},
                    auth_settings=["PureCloud OAuth"],
                    response_type="object",
                ))()
                results = (resp or {}).get("results") or []
                ewt = results[0].get("estimatedWaitTimeSeconds") if results else None
                formula = results[0].get("formula") if results else None
                out.append({
                    "queue_id": qid,
                    "media_type": media_type,
                    "estimated_wait_time_seconds": ewt,
                    "formula": formula,
                })
            except Exception as exc:
                out.append({
                    "queue_id": qid,
                    "media_type": media_type,
                    "estimated_wait_time_seconds": None,
                    "error": str(exc),
                })
        return {"results": out}

    @mcp.tool()
    def agent_performance(
        user_ids: list[str] = Field(description="User ids to report on (required)."),
        interval: str | None = Field(
            default=None, description="ISO-8601 interval 'start/end'. Defaults to last 7 days."
        ),
        granularity: str = Field(default="P1D"),
    ) -> dict:
        """Per-agent productivity for an interval: connected interactions, handle time,
        talk time, after-call work, and a derived avg AHT plus a flat per-user summary.

        Backed by /api/v2/analytics/conversations/aggregates/query with groupBy=userId.
        The user-aggregates endpoint (post_analytics_users_aggregates_query) only
        accepts presence-state metrics (tAgentRoutingStatus, tSystemPresence,
        tOrganizationPresence) and rejects tHandle/tTalk/etc with HTTP 400 — so this
        tool deliberately uses the conversations-aggregates path with the same
        ConversationAggregateMetric set that queue_performance uses.
        """
        api = gc.AnalyticsApi(get_api())
        body = {
            "interval": interval or _default_interval(7),
            "granularity": granularity,
            "groupBy": ["userId"],
            "filter": {
                "type": "or",
                "predicates": [{"dimension": "userId", "value": u} for u in user_ids],
            },
            "metrics": [
                "nConversations",
                "nOutbound",
                "nTransferred",
                "tHandle",
                "tTalk",
                "tAcw",
                "tHeld",
            ],
        }
        resp = with_retry(api.post_analytics_conversations_aggregates_query)(body)
        raw = to_dict(resp) or {}

        # Aggregates endpoint doesn't return nConversations for the message bucket
        # when grouped by userId — fall back to walking conversation details for messages.
        message_volume = _agent_message_volume(user_ids, body["interval"])

        # Genesys auto-splits each user's results by mediaType (voice / message / email
        # / callback / chat). Aggregate per-user *and* per-user-per-media for the summary.
        per_user: dict[str, dict] = {}
        for uid in user_ids:
            per_user[uid] = {
                "user_id": uid,
                "by_media": {},
                "conversations": 0, "outbound": 0, "transferred": 0,
                "h_sum": 0.0, "h_n": 0, "t_sum": 0.0, "t_n": 0,
                "acw_sum": 0.0, "acw_n": 0, "held_sum": 0.0, "held_n": 0,
            }

        for grp in raw.get("results") or []:
            grp_key = grp.get("group") or {}
            uid = grp_key.get("userId")
            media = grp_key.get("mediaType") or "unknown"
            if uid not in per_user:
                continue
            row = per_user[uid]
            mrow = row["by_media"].setdefault(media, {
                "conversations": 0, "outbound": 0, "transferred": 0,
                "h_sum": 0.0, "h_n": 0, "t_sum": 0.0, "t_n": 0,
                "acw_sum": 0.0, "acw_n": 0, "held_sum": 0.0, "held_n": 0,
            })
            for bucket in grp.get("data") or []:
                metrics = {m["metric"]: (m.get("stats") or {}) for m in (bucket.get("metrics") or [])}
                conv  = int(metrics.get("nConversations",{}).get("count", 0) or 0)
                outb  = int(metrics.get("nOutbound",     {}).get("count", 0) or 0)
                tran  = int(metrics.get("nTransferred",  {}).get("count", 0) or 0)
                row["conversations"] += conv;  mrow["conversations"] += conv
                row["outbound"]      += outb;  mrow["outbound"]      += outb
                row["transferred"]   += tran;  mrow["transferred"]   += tran
                for f, mname in [("h", "tHandle"), ("t", "tTalk"), ("acw", "tAcw"), ("held", "tHeld")]:
                    s = metrics.get(mname, {})
                    add_sum = float(s.get("sum", 0) or 0)
                    add_n   = int(s.get("count", 0) or 0)
                    row[f + "_sum"] += add_sum;  mrow[f + "_sum"] += add_sum
                    row[f + "_n"]   += add_n;    mrow[f + "_n"]   += add_n

        def _avg_s(sum_ms: float, n: int) -> int | None:
            return round(sum_ms / n / 1000) if n else None

        # Note: nOutbound counts outbound *interactions*, not conversations — a single
        # conversation can include multiple outbound legs (callbacks, transfers). So
        # nOutbound can exceed nConversations and inbound = conversations − outbound is
        # misleading. We surface conversations and outbound_interactions side by side
        # and let the consumer interpret.
        summary = []
        for uid, row in per_user.items():
            # Message volume from the details-job fallback. Override the message bucket
            # if the aggregates endpoint returned a stub (no count metric); also extend
            # the user's overall handle-time totals.
            mv = message_volume.get(uid) or {"conversations": 0, "handle_ms": 0.0}
            msg_conv = int(mv["conversations"])
            msg_handle_ms = float(mv["handle_ms"])
            msg_bucket = row["by_media"].get("message")
            if msg_conv > 0:
                if msg_bucket is None:
                    msg_bucket = row["by_media"]["message"] = {
                        "conversations": 0, "outbound": 0, "transferred": 0,
                        "h_sum": 0.0, "h_n": 0, "t_sum": 0.0, "t_n": 0,
                        "acw_sum": 0.0, "acw_n": 0, "held_sum": 0.0, "held_n": 0,
                    }
                # Overwrite count (aggregates returned 0/missing for messages); add
                # handle-time on top of any time the aggregates response did include.
                msg_bucket["conversations"] = msg_conv
                msg_bucket["h_sum"] += msg_handle_ms
                msg_bucket["h_n"]   += msg_conv  # one handle entry per conversation as proxy
                row["conversations"] += msg_conv
                row["h_sum"] += msg_handle_ms
                row["h_n"]   += msg_conv

            conv = row["conversations"]
            by_media_summary = {}
            for media, m in row["by_media"].items():
                by_media_summary[media] = {
                    "conversations": m["conversations"],
                    "outbound_interactions": m["outbound"],
                    "transferred": m["transferred"],
                    "avg_handle_s": _avg_s(m["h_sum"], m["h_n"]),
                    "total_handle_min": round(m["h_sum"] / 1000 / 60, 1) if m["h_sum"] else 0,
                }
            summary.append({
                "user_id": uid,
                "conversations": conv,
                "outbound_interactions": row["outbound"],
                "transferred": row["transferred"],
                "transfer_rate_pct": round(row["transferred"] / conv * 100, 1) if conv else None,
                "avg_handle_s": _avg_s(row["h_sum"], row["h_n"]),
                "avg_talk_s":   _avg_s(row["t_sum"], row["t_n"]),
                "avg_acw_s":    _avg_s(row["acw_sum"], row["acw_n"]),
                "avg_held_s":   _avg_s(row["held_sum"], row["held_n"]),
                "total_handle_min": round(row["h_sum"] / 1000 / 60, 1) if row["h_sum"] else 0,
                "by_media": by_media_summary,
            })
        summary.sort(key=lambda r: -(r["conversations"] or 0))

        return {
            "interval": body["interval"],
            "granularity": granularity,
            "summary": summary,
            "results": raw.get("results") or [],
        }
