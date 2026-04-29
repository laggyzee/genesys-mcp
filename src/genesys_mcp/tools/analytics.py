"""Analytics tools: queue & agent performance, real-time observation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


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
        """Per-agent activity: handle time, talk time, on-queue time, interactions handled."""
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
                "tHandle",
                "tTalk",
                "tAcw",
                "tAgentRoutingStatus",
                "nHandle",
                "nOutbound",
            ],
        }
        resp = with_retry(api.post_analytics_users_aggregates_query)(body)
        return to_dict(resp)
