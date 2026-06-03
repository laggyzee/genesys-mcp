"""Analytics tools: queue & agent performance, real-time observation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._envelopes import soft_fail_envelope
from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


def _default_interval(days: int = 7) -> str:
    """ISO-8601 interval 'start/end' covering the last N days up to now (UTC)."""
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    return f"{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}/{end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"


# Fields we keep in `mode: "summary"` for queue_performance. Everything else
# (histograms, percentiles, min/max/current, plus the unused metrics) gets
# stripped per v1.1's token-budget pass.
_QUEUE_SUMMARY_METRICS: dict[str, list[str]] = {
    "tAnswered":  ["count", "sum"],
    "tHandle":    ["count", "sum"],
    "tWait":      ["count", "sum"],
    "tAbandon":   ["count", "sum"],
    "nOffered":   ["count"],
    "nOverSla":   ["count"],
    "nConnected": ["count"],   # daily-brief's offered fallback when nOffered is empty
}


# Same shape as _QUEUE_SUMMARY_METRICS but for agent_performance. Drops the
# rarely-consumed transfer-type metrics (nOutbound, nBlindTransferred,
# nConsultTransferred) and the per-metric histograms / percentiles.
_AGENT_SUMMARY_METRICS: dict[str, list[str]] = {
    "tAnswered":     ["count", "sum"],
    "tHandle":       ["count", "sum"],
    "tTalkComplete": ["count", "sum"],
    "tAcw":          ["count", "sum"],
    "tHeldComplete": ["count", "sum"],
    "nTransferred":  ["count"],
}


def _slim_agent_results(resp: dict) -> None:
    """Trim a raw agent_performance ``results`` block in-place to summary mode.

    Drops the metrics nothing consumes plus all histogram / percentile fields.
    Keeps tAnswered + tHandle (count + sum) per bucket so the cc-monthly-report
    daily-sparkline aggregator continues to work without needing mode='full'.
    """
    for result in resp.get("results") or []:
        for bucket in result.get("data") or []:
            slim_metrics: list[dict] = []
            for m in bucket.get("metrics") or []:
                metric_name = m.get("metric")
                keep_stats = _AGENT_SUMMARY_METRICS.get(metric_name)
                if keep_stats is None:
                    continue
                stats = m.get("stats") or {}
                slim_metrics.append({
                    "metric": metric_name,
                    "stats": {k: stats[k] for k in keep_stats if k in stats},
                })
            bucket["metrics"] = slim_metrics


def _slim_queue_response(resp: dict) -> None:
    """Trim a queue_performance response in-place to the summary contract.

    Rewrites each bucket's ``metrics`` list to include only the metrics in
    ``_QUEUE_SUMMARY_METRICS`` and only the stat fields each metric uses.
    Cuts ~75% of payload bytes on typical responses while preserving every
    field any current skill or derived-block computation consumes.
    """
    for result in resp.get("results") or []:
        for bucket in result.get("data") or []:
            slim_metrics: list[dict] = []
            for m in bucket.get("metrics") or []:
                metric_name = m.get("metric")
                keep_stats = _QUEUE_SUMMARY_METRICS.get(metric_name)
                if keep_stats is None:
                    continue
                stats = m.get("stats") or {}
                slim_metrics.append({
                    "metric": metric_name,
                    "stats": {k: stats[k] for k in keep_stats if k in stats},
                })
            bucket["metrics"] = slim_metrics


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
        mode: str = Field(
            default="summary",
            description=(
                "Response shape. 'summary' (default, v1.1+) returns the derived "
                "block plus a slim set of raw metrics (count + sum on tAnswered, "
                "tHandle, tWait, tAbandon; count-only on nOffered, nOverSla, "
                "nConnected). 'full' returns the v1.0 shape with every metric "
                "and every stat field (histograms, percentiles, min/max). Most "
                "interactive use should stay on 'summary' — ~75% smaller payload "
                "and every current skill consumes only the summary fields. "
                "Switch to 'full' only when you genuinely need percentiles or "
                "histograms (e.g. 'what's the p95 wait time?')."
            ),
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
        # Filter shape mirrors what the Genesys 'Performance > Queues' UI sends: an
        # outer `and` of `or` clauses (queueId list, optional mediaType list). Equivalent
        # to a flat OR over queueIds for our purposes, but matches the canonical shape so
        # behaviour is consistent if a mediaType filter clause is added later.
        body = {
            "interval": interval or _default_interval(7),
            "granularity": granularity,
            "groupBy": group_by,
            "filter": {
                "type": "and",
                "clauses": [
                    {"type": "or", "predicates": [
                        {"dimension": "queueId", "value": qid} for qid in queue_ids
                    ]},
                ],
            },
            "metrics": [
                "nOffered",
                "tAnswered",
                "tAbandon",
                "nConnected",
                "nTransferred",
                "nOverSla",
                "tHandle",
                "tTalkComplete",
                "tHeldComplete",
                "tAcw",
                "tWait",
                "tShortAbandon",
            ],
        }
        resp = with_retry(api.post_analytics_conversations_aggregates_query)(body)
        out = to_dict(resp)
        _attach_derived_metrics(out)
        if mode == "summary":
            _slim_queue_response(out)
        elif mode != "full":
            raise ValueError(
                f"queue_performance.mode must be 'summary' or 'full', got {mode!r}"
            )
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
                # v1.3: distinguish soft-fail (no EWT available — typically
                # 404 because no agents are skilled or the queue is inactive)
                # from hard-fail (auth, 5xx, network). Soft-fails get null +
                # a tag; hard-fails propagate so the caller knows.
                status = getattr(exc, "status", None)
                if status == 404:
                    out.append(soft_fail_envelope(
                        kind="estimated wait time",
                        message="no EWT available (queue inactive or no agents skilled)",
                        queue_id=qid,
                        media_type=media_type,
                        estimated_wait_time_seconds=None,
                    ))
                    continue
                # Re-raise on hard failures — silent error-string burial
                # makes debugging auth/network issues painful.
                logger.warning(
                    "queue_estimated_wait_time failed for queue=%s media=%s: %s",
                    qid, media_type, exc,
                )
                raise
        return {"results": out}

    @mcp.tool()
    def agent_performance(
        user_ids: list[str] = Field(description="User ids to report on (required)."),
        interval: str | None = Field(
            default=None, description="ISO-8601 interval 'start/end'. Defaults to last 7 days."
        ),
        granularity: str = Field(default="P1D"),
        mode: str = Field(
            default="summary",
            description=(
                "Response shape. 'summary' (default, v1.1+) returns only the "
                "computed per-agent + per-media KPIs (answered, handled, AHT, "
                "ACW, transfer rate, total handle minutes). 'full' adds the "
                "raw Genesys aggregates response under 'results' with every "
                "metric and stat field — needed only for histograms or "
                "percentiles. The summary already drives every workforce table, "
                "coaching brief and reconciliation row across the four skills."
            ),
        ),
    ) -> dict:
        """Per-agent productivity matching Genesys 'Performance > Agents' UI:
        answered count, handle time, talk time, ACW, transferred — broken down per
        agent and per media type (voice / message / email / callback / chat).

        Backed by /api/v2/analytics/conversations/aggregates/query — the same endpoint
        the Genesys web UI uses. Filter shape mirrors the UI exactly: an outer ``and``
        with two ``or`` clauses, one for the userId list and one for the mediaType,
        and groupBy=[userId, mediaType] for the auto-split. tAnswered.count is the
        canonical answered-conversation count (matches the UI's "Answer" column);
        tHandle.count matches the "Handle" column.
        """
        api = gc.AnalyticsApi(get_api())
        resolved_interval = interval or _default_interval(7)
        body = {
            "interval": resolved_interval,
            "granularity": granularity,
            "groupBy": ["userId", "mediaType"],
            "filter": {
                "type": "and",
                "clauses": [
                    {"type": "or", "predicates": [
                        {"dimension": "userId", "value": uid} for uid in user_ids
                    ]},
                ],
            },
            "metrics": [
                "tAnswered",       # tAnswered.count = "Answer" column in UI
                "tHandle",         # tHandle.count = "Handle" column in UI
                "tTalkComplete",
                "tHeldComplete",
                "tAcw",
                "nTransferred",
                "nOutbound",
                "nBlindTransferred",
                "nConsultTransferred",
            ],
        }
        resp = with_retry(api.post_analytics_conversations_aggregates_query)(body)
        raw = to_dict(resp) or {}

        # Genesys returns one result group per (userId, mediaType) pairing.
        # tAnswered.count is the canonical "Answer" count (matches UI exactly);
        # tHandle.count is the canonical "Handle" count.
        per_user: dict[str, dict] = {}
        for uid in user_ids:
            per_user[uid] = {
                "user_id": uid,
                "by_media": {},
                "answered": 0, "handled": 0, "outbound": 0, "transferred": 0,
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
                "answered": 0, "handled": 0, "outbound": 0, "transferred": 0,
                "h_sum": 0.0, "h_n": 0, "t_sum": 0.0, "t_n": 0,
                "acw_sum": 0.0, "acw_n": 0, "held_sum": 0.0, "held_n": 0,
            })
            for bucket in grp.get("data") or []:
                metrics = {m["metric"]: (m.get("stats") or {}) for m in (bucket.get("metrics") or [])}
                ans  = int(metrics.get("tAnswered",   {}).get("count", 0) or 0)
                hand = int(metrics.get("tHandle",     {}).get("count", 0) or 0)
                outb = int(metrics.get("nOutbound",   {}).get("count", 0) or 0)
                tran = int(metrics.get("nTransferred",{}).get("count", 0) or 0)
                row["answered"]    += ans;  mrow["answered"]    += ans
                row["handled"]     += hand; mrow["handled"]     += hand
                row["outbound"]    += outb; mrow["outbound"]    += outb
                row["transferred"] += tran; mrow["transferred"] += tran
                for f, mname in [("h", "tHandle"), ("t", "tTalkComplete"),
                                 ("acw", "tAcw"), ("held", "tHeldComplete")]:
                    s = metrics.get(mname, {})
                    add_sum = float(s.get("sum", 0) or 0)
                    add_n   = int(s.get("count", 0) or 0)
                    row[f + "_sum"] += add_sum;  mrow[f + "_sum"] += add_sum
                    row[f + "_n"]   += add_n;    mrow[f + "_n"]   += add_n

        def _avg_s(sum_ms: float, n: int) -> int | None:
            return round(sum_ms / n / 1000) if n else None

        # Build per-user summary plus per-media breakdown.
        # nOutbound counts outbound *interactions*, not conversations, so it can exceed
        # the answered count for a user — surface it as outbound_interactions and let
        # the consumer interpret.
        summary = []
        for uid, row in per_user.items():
            answered = row["answered"]
            handled  = row["handled"]
            by_media_summary = {}
            for media, m in row["by_media"].items():
                by_media_summary[media] = {
                    "answered": m["answered"],
                    "handled": m["handled"],
                    "outbound_interactions": m["outbound"],
                    "transferred": m["transferred"],
                    "avg_handle_s": _avg_s(m["h_sum"], m["h_n"]),
                    "avg_talk_s":   _avg_s(m["t_sum"], m["t_n"]),
                    "avg_acw_s":    _avg_s(m["acw_sum"], m["acw_n"]),
                    "avg_held_s":   _avg_s(m["held_sum"], m["held_n"]),
                    "total_handle_min": round(m["h_sum"] / 1000 / 60, 1) if m["h_sum"] else 0,
                }
            summary.append({
                "user_id": uid,
                "answered": answered,
                "handled": handled,
                "outbound_interactions": row["outbound"],
                "transferred": row["transferred"],
                "transfer_rate_pct": round(row["transferred"] / handled * 100, 1) if handled else None,
                "avg_handle_s": _avg_s(row["h_sum"], row["h_n"]),
                "avg_talk_s":   _avg_s(row["t_sum"], row["t_n"]),
                "avg_acw_s":    _avg_s(row["acw_sum"], row["acw_n"]),
                "avg_held_s":   _avg_s(row["held_sum"], row["held_n"]),
                "total_handle_min": round(row["h_sum"] / 1000 / 60, 1) if row["h_sum"] else 0,
                "by_media": by_media_summary,
            })
        summary.sort(key=lambda r: -(r["answered"] or 0))

        out_results = raw.get("results") or []
        if mode == "summary":
            # Trim each bucket's metrics list to the slim contract (count + sum
            # on the metrics actually consumed). Keeps `results` for sparkline
            # and reconciliation aggregators that iterate per-bucket data.
            slimmed = {"results": out_results}
            _slim_agent_results(slimmed)
            out_results = slimmed["results"]
        elif mode != "full":
            raise ValueError(
                f"agent_performance.mode must be 'summary' or 'full', got {mode!r}"
            )
        return {
            "interval": body["interval"],
            "granularity": granularity,
            "summary": summary,
            "results": out_results,
        }
