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


def _agent_volume_for_media(user_ids: list[str], interval: str, media_type: str) -> dict[str, dict]:
    """Per-agent conversation count + handle time for one media type, via conversation details walk.

    Why a details-walk instead of conversations-aggregates: the aggregates endpoint
    with ``groupBy=userId`` and a top-level ``filter`` on userId only matches
    conversations where Genesys treats the user as a primary dimension — that
    misses the bulk of inbound traffic where the customer originated and the
    agent merely picked up. Walking conversation details and inspecting the
    participants array gives us the right per-agent count directly, matching
    the figures in the Genesys 'Performance > Agents' UI.

    Counting rule (matches Genesys 'Handle' column):
      - participant.purpose == 'agent'
      - participant.userId is in the requested set
      - participant has at least one 'interact' segment (i.e. actually handled,
        not just alert+drop or quick-transfer-away)

    Multi-handler conversations (transfer / co-handle) count +1 for each agent
    who satisfied those rules. Handle time per user is summed only over their
    own 'interact' segments — does not include the other agent's time.

    Returns ``{user_id: {"conversations": int, "handle_ms": float}}`` for every
    requested user (zero-filled).
    """
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
                 "operator": "matches", "value": media_type},
            ],
        }],
    }
    try:
        convs = _run_conv_details_job(body, max_pages=300)
    except Exception as exc:
        logger.warning("agent_performance %s: details pull failed (%s); bucket will be empty",
                       media_type, exc)
        return counts

    for c in convs:
        users_in_conv: set[str] = set()
        per_user_handle_ms: dict[str, float] = defaultdict(float)
        for p in c.get("participants") or []:
            if p.get("purpose") != "agent":
                continue
            uid = p.get("userId")
            if uid not in user_set:
                continue
            handle_ms = 0.0
            had_interact = False
            for s in p.get("sessions") or []:
                for seg in s.get("segments") or []:
                    seg_type = seg.get("segmentType")
                    if seg_type != "interact":
                        continue
                    had_interact = True
                    st_raw = seg.get("segmentStart")
                    en_raw = seg.get("segmentEnd")
                    if not st_raw or not en_raw:
                        continue
                    try:
                        handle_ms += (
                            _parse_iso(en_raw) - _parse_iso(st_raw)
                        ).total_seconds() * 1000
                    except Exception:
                        continue
            if had_interact:
                users_in_conv.add(uid)
                per_user_handle_ms[uid] += handle_ms
        for uid in users_in_conv:
            counts[uid]["conversations"] += 1
            counts[uid]["handle_ms"] += per_user_handle_ms.get(uid, 0.0)

    total_attributed = sum(v["conversations"] for v in counts.values())
    logger.info(
        "agent_performance %s: %d conversations attributed across %d users (from %d total)",
        media_type, total_attributed,
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

        # The aggregates response is unreliable for per-agent COUNTS — its top-level
        # userId filter only catches a subset of conversations (mostly outbound or
        # primary-dimension matches), missing most inbound traffic. We pull conversation
        # counts directly from conversation details for each media type. The aggregates
        # response is still used for time stats (talk / hold / ACW) where the time
        # numbers are reliable per user across whatever conversations Genesys did match.
        media_counts = {
            "voice":   _agent_volume_for_media(user_ids, body["interval"], "voice"),
            "message": _agent_volume_for_media(user_ids, body["interval"], "message"),
            "email":   _agent_volume_for_media(user_ids, body["interval"], "email"),
        }

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
            # Replace per-media counts and handle time with the details-walk numbers
            # (reliable). Time stats other than handle (talk / hold / ACW) come from
            # the aggregates response and are kept as-is — those are time totals
            # across whatever conversations Genesys matched and are still useful as
            # secondary signals.
            details_total_conv = 0
            details_total_handle_ms = 0.0
            for media_type, vol in media_counts.items():
                v = vol.get(uid) or {"conversations": 0, "handle_ms": 0.0}
                conv_count = int(v["conversations"])
                handle_ms = float(v["handle_ms"])
                if conv_count == 0 and media_type not in row["by_media"]:
                    continue
                bucket = row["by_media"].get(media_type)
                if bucket is None:
                    bucket = row["by_media"][media_type] = {
                        "conversations": 0, "outbound": 0, "transferred": 0,
                        "h_sum": 0.0, "h_n": 0, "t_sum": 0.0, "t_n": 0,
                        "acw_sum": 0.0, "acw_n": 0, "held_sum": 0.0, "held_n": 0,
                    }
                bucket["conversations"] = conv_count
                bucket["h_sum"] = handle_ms
                bucket["h_n"]   = conv_count
                details_total_conv += conv_count
                details_total_handle_ms += handle_ms
            row["conversations"] = details_total_conv
            row["h_sum"] = details_total_handle_ms
            row["h_n"]   = details_total_conv

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
