"""Composition tools — chain multiple Genesys endpoints into ops-ready reports.

These are deliberately opinionated. Each tool answers one specific operational
question that we've answered manually multiple times in real sessions:
    - Who's calling us repeatedly and why? (repeat_caller_report)
    - Who consistently overruns breaks/lunches? (break_overrun_report)
    - Is this agent doing their job well? (agent_quality_snapshot)
    - What's happening across these queues right now? (live_wallboard)
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry
from genesys_mcp.naming import resolver

logger = logging.getLogger(__name__)


def _sta_details(conversation_id: str) -> dict | None:
    """Fetch the STA per-conversation snapshot.

    Calls GET /api/v2/speechandtextanalytics/conversations/{id} which is the
    endpoint that actually returns data in production tenants — the
    /summaries and /sentiments endpoints exposed by the SDK helpers
    consistently 404 even when STA is enabled.

    Returns a normalised dict (score, trend, trend_class, participant_metrics)
    or None on 404. Raises on other errors.
    """
    try:
        data = with_retry(get_api().call_api)(
            resource_path=f"/api/v2/speechandtextanalytics/conversations/{conversation_id}",
            method="GET",
            auth_settings=["PureCloud OAuth"],
            response_type="object",
        ) or {}
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            return None
        raise

    score = data.get("sentimentScore")
    if score is None:
        return None
    return {
        "score": float(score),
        "trend": float(data.get("sentimentTrend") or 0.0),
        "trend_class": data.get("sentimentTrendClass"),
        "participant_metrics": data.get("participantMetrics") or {},
        "empathy_scores": data.get("empathyScores") or [],
    }


def _fetch_wrapup(conversation_id: str) -> dict | None:
    """Pull wrap-up disposition + notes + AI-written attributes from the live conversation endpoint.

    The analytics endpoints (`get_analytics_conversation_details`, conversation details
    jobs) do *not* surface wrap-up data — that only appears on the live
    `/api/v2/conversations/{id}` endpoint, even for completed calls. In this tenant the
    notes field carries summaries written by Lawrence's external AI (native Genesys
    AI summarisation is off for cost). Custom attributes ``aiOutcome`` ("Resolved" /
    "Mid Flight") and ``expectedFix`` ("Simpack Recharge", "CHOWN", ...) are also
    written by that AI and are richer signals than the notes for clustering.

    Returns ``{disposition, code_id, notes, ai_outcome, expected_fix}`` or None on 404 /
    no agent participant. Picks the first agent participant with a userId — re-queue
    transfers may produce multiple agent legs but the first one is what the wrap-up
    landed against.
    """
    try:
        data = with_retry(get_api().call_api)(
            resource_path=f"/api/v2/conversations/{conversation_id}",
            method="GET",
            auth_settings=["PureCloud OAuth"],
            response_type="object",
        ) or {}
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            return None
        raise

    for p in data.get("participants") or []:
        if p.get("purpose") != "agent" or not p.get("userId"):
            continue
        wrap = p.get("wrapup") or {}
        if not wrap:
            continue
        attrs = p.get("attributes") or {}
        return {
            "disposition": wrap.get("name"),
            "code_id": wrap.get("code"),
            "notes": wrap.get("notes") or None,
            "ai_outcome": attrs.get("aiOutcome"),
            "expected_fix": attrs.get("expectedFix"),
        }
    return None


def _sentiment_label(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 0.4:
        return "positive"
    if score >= 0.1:
        return "mildly_positive"
    if score > -0.1:
        return "neutral"
    if score > -0.4:
        return "mildly_negative"
    return "negative"


_TREND_TO_DELTA = {
    "GreatlyDeclining": -0.5,
    "Declining": -0.3,
    "SlightlyDeclining": -0.15,
    "NoChange": 0.0,
    "SlightlyImproving": 0.15,
    "Improving": 0.3,
    "GreatlyImproving": 0.5,
}


_GENESYS_TREND_TO_LABEL = {
    "GreatlyDeclining": "deteriorating",
    "Declining": "deteriorating",
    "SlightlyDeclining": "stable",
    "NoChange": "stable",
    "SlightlyImproving": "stable",
    "Improving": "improving",
    "GreatlyImproving": "improving",
    "NotCalculated": "unknown",
}


def _trend_label(scores: list[float], single_call_trend_class: str | None = None) -> str:
    """Categorise a sentiment trajectory into a single label.

    For zero calls: 'no_data'.
    For one call: derive from Genesys' own trend_class on that call (it already reflects
      *intra-call* trajectory; that's a real signal even with only one data point).
    For 2+ calls: compute from the score sequence — first / last delta plus average.
    """
    if not scores:
        return "no_data"
    if len(scores) == 1:
        if single_call_trend_class:
            return _GENESYS_TREND_TO_LABEL.get(single_call_trend_class, "unknown")
        return "single_call"
    first, last = scores[0], scores[-1]
    avg = sum(scores) / len(scores)
    delta = last - first
    if avg <= -0.3 and abs(delta) < 0.2:
        return "persistently_negative"
    if delta <= -0.3:
        return "deteriorating"
    if delta >= 0.3:
        return "improving"
    if -0.1 <= avg <= 0.1:
        return "stable_neutral"
    return "stable"


_RETENTION_KEYWORDS = ("billing", "bill", "charge", "refund", "credit", "port", "complaint", "cancel", "dispute", "escalat")


def _recommend_action(row: dict) -> str:
    """Documented heuristic. Order matters — first match wins."""
    abandoned = row["abandoned_in_queue_count"]
    last = row["last_call"] or {}
    last_answered = (last.get("status") == "answered")
    distinct_queues = len(row["queues_offered"])
    trend = row["sentiment_trend"]
    topic_blob = " ".join(t["topic"] for t in row["topics"]).lower()

    if abandoned >= 3 and not last_answered:
        return "callback_recommended"
    if trend == "deteriorating" and any(k in topic_blob for k in _RETENTION_KEYWORDS):
        return "escalate_to_retention"
    if distinct_queues >= 3:
        return "route_review"
    return "monitor"


def _default_interval(days: int = 7) -> str:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    return f"{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}/{end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _seg_dur_s(seg: dict) -> float:
    st_raw = seg.get("segmentStart")
    en_raw = seg.get("segmentEnd")
    if not st_raw or not en_raw:
        return 0.0
    try:
        return (_parse_iso(en_raw) - _parse_iso(st_raw)).total_seconds()
    except Exception:
        return 0.0


def _run_conv_details_job(filters_body: dict[str, Any], max_pages: int = 20) -> list[dict]:
    """Submit a conversation details job, poll, paginate, return all conversations."""
    api = gc.AnalyticsApi(get_api())
    submit = with_retry(api.post_analytics_conversations_details_jobs)(body=filters_body)
    job_id = submit.job_id if hasattr(submit, "job_id") else to_dict(submit).get("jobId")
    if not job_id:
        raise RuntimeError(f"conversations/details/jobs submit returned no jobId")

    for _ in range(60):
        status = with_retry(api.get_analytics_conversations_details_job)(job_id=job_id)
        state = getattr(status, "state", None) or to_dict(status).get("state")
        if state == "FULFILLED":
            break
        if state in ("FAILED", "CANCELLED", "EXPIRED"):
            raise RuntimeError(f"conv details job {job_id} terminated in state {state}")
        time.sleep(1)
    else:
        raise RuntimeError(f"conv details job {job_id} did not reach FULFILLED")

    out: list[dict] = []
    cursor: str | None = None
    for _ in range(max_pages):
        kwargs: dict[str, Any] = {"job_id": job_id, "page_size": 1000}
        if cursor:
            kwargs["cursor"] = cursor
        page = with_retry(api.get_analytics_conversations_details_job_results)(**kwargs)
        page_dict = to_dict(page) or {}
        out.extend(page_dict.get("conversations") or [])
        cursor = page_dict.get("cursor")
        if not cursor:
            break
    return out


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def repeat_caller_report(
        queue_ids: list[str] = Field(
            description="Queue ids to scope the report. Pass an empty list for org-wide.",
        ),
        interval: str | None = Field(
            default=None,
            description="ISO-8601 interval. Defaults to last 7 days UTC.",
        ),
        min_calls: int = Field(
            default=2, ge=2, le=20,
            description="Minimum calls per ANI to be flagged as a repeater (default 2).",
        ),
        media_type: str = Field(
            default="voice",
            description="Media type to scope: 'voice', 'message', or 'callback'.",
        ),
        exclude_anis: list[str] | None = Field(
            default=None,
            description="ANIs to exclude (e.g., test/internal numbers).",
        ),
    ) -> dict:
        """Find customers calling repeatedly within an interval, with a full IVR→ACD→answer funnel.

        Per repeater row:
          - call_count                — every conversation we saw from this ANI
          - acd_offered_count         — conversations that entered a queue (acd participant or queue segment)
          - answered_count            — conversations an agent picked up
          - abandoned_in_queue_count  — offered minus answered (queued, didn't reach an agent)
          - ivr_only_count            — total minus offered (never made it past IVR/flow)
          - answer_rate_of_offered_pct  — answered / offered (the operationally meaningful rate)
          - answer_rate_of_total_pct    — answered / total (includes IVR-only abandons)
          - queues_offered            — counter of queues the offered calls hit

        Sorted by acd_offered_count desc, then total. Backed by
        /api/v2/analytics/conversations/details/jobs (async, paginated).
        """
        interval = interval or _default_interval(7)
        excluded = set(exclude_anis or [])

        # Build segmentFilters: queueId AND mediaType. Multi-queue = OR clauses.
        if queue_ids:
            queue_clauses = [
                {
                    "type": "and",
                    "predicates": [
                        {"type": "dimension", "dimension": "queueId",
                         "operator": "matches", "value": qid},
                        {"type": "dimension", "dimension": "mediaType",
                         "operator": "matches", "value": media_type},
                    ],
                }
                for qid in queue_ids
            ]
            segment_filter = {"type": "or", "clauses": queue_clauses}
        else:
            segment_filter = {
                "type": "and",
                "predicates": [
                    {"type": "dimension", "dimension": "mediaType",
                     "operator": "matches", "value": media_type},
                ],
            }

        body = {
            "interval": interval,
            "order": "asc",
            "orderBy": "conversationStart",
            "conversationFilters": [
                {
                    "type": "and",
                    "predicates": [
                        {"type": "dimension", "dimension": "originatingDirection",
                         "operator": "matches", "value": "inbound"},
                    ],
                }
            ],
            "segmentFilters": [segment_filter],
        }

        convs = _run_conv_details_job(body)

        # Org-wide funnel counters (across every inbound conv we pulled, not just repeaters)
        org_total = len(convs)
        org_offered = 0
        org_answered = 0

        by_ani: dict[str, list[dict]] = defaultdict(list)
        for c in convs:
            ani = None
            answered = False
            acd_offered = False
            queue_id = None
            for p in c.get("participants") or []:
                purpose = p.get("purpose")
                if purpose == "customer":
                    for s in p.get("sessions") or []:
                        if s.get("ani") and not ani:
                            ani = (s["ani"] or "").replace("tel:", "")
                elif purpose == "acd":
                    # Canonical signal: an acd participant means the call entered a queue.
                    # Source queue_id from the acd participant's segments (not the customer leg,
                    # whose interact segment can carry a queueId association even when the call
                    # hung up in IVR before any real ACD offer).
                    acd_offered = True
                    for s in p.get("sessions") or []:
                        for seg in s.get("segments") or []:
                            if seg.get("queueId") and not queue_id:
                                queue_id = seg["queueId"]
                elif purpose == "agent" and p.get("userId"):
                    answered = True

            if acd_offered:
                org_offered += 1
            if answered:
                org_answered += 1

            if not ani or ani.startswith("sip:") or ani in excluded:
                continue
            by_ani[ani].append({
                "conversation_id": c.get("conversationId"),
                "start": c.get("conversationStart"),
                "queue_id": queue_id,
                "queue_name": resolver.queue_name(queue_id) if queue_id else None,
                "acd_offered": acd_offered,
                "answered": answered,
            })

        # Build repeater rows
        all_qids = {r["queue_id"] for rows in by_ani.values() for r in rows if r["queue_id"]}
        resolver.queue_names(all_qids)  # pre-warm cache

        rows = []
        for ani, calls in by_ani.items():
            n = len(calls)
            if n < min_calls:
                continue
            offered_n = sum(1 for r in calls if r["acd_offered"])
            answered_n = sum(1 for r in calls if r["answered"])
            abandoned_in_queue_n = offered_n - answered_n
            ivr_only_n = n - offered_n
            queue_counter = Counter(
                r["queue_name"] or r["queue_id"]
                for r in calls
                if r["acd_offered"] and r["queue_id"]
            )
            rows.append({
                "ani": ani,
                "call_count": n,
                "acd_offered_count": offered_n,
                "answered_count": answered_n,
                "abandoned_in_queue_count": abandoned_in_queue_n,
                "ivr_only_count": ivr_only_n,
                "answer_rate_of_offered_pct": round(answered_n / offered_n * 100, 1) if offered_n else 0,
                "answer_rate_of_total_pct": round(answered_n / n * 100, 1) if n else 0,
                "queues_offered": dict(queue_counter),
                "first_call": calls[0]["start"],
                "last_call": calls[-1]["start"],
                "conversation_ids": [r["conversation_id"] for r in calls],
            })
        rows.sort(key=lambda r: (-r["acd_offered_count"], -r["call_count"]))

        return {
            "interval": interval,
            "media_type": media_type,
            "total_conversations": org_total,
            "unique_callers": len(by_ani),
            "repeater_count": len(rows),
            "repeater_calls": sum(r["call_count"] for r in rows),
            "org_funnel": {
                "total": org_total,
                "acd_offered": org_offered,
                "answered": org_answered,
                "abandoned_in_queue": org_offered - org_answered,
                "ivr_only": org_total - org_offered,
                "offered_rate_pct": round(org_offered / org_total * 100, 1) if org_total else 0,
                "answer_rate_of_offered_pct": round(org_answered / org_offered * 100, 1) if org_offered else 0,
                "answer_rate_of_total_pct": round(org_answered / org_total * 100, 1) if org_total else 0,
            },
            "repeaters": rows,
        }

    @mcp.tool()
    def repeat_caller_deep_dive(
        queue_ids: list[str] = Field(
            description="Queue ids to scope the report. Pass an empty list for org-wide.",
        ),
        interval: str | None = Field(
            default=None,
            description="ISO-8601 interval. Defaults to last 7 days UTC.",
        ),
        min_calls: int = Field(
            default=3, ge=2, le=20,
            description="Minimum calls per ANI to be eligible (default 3 — slightly higher than the funnel report).",
        ),
        media_type: str = Field(
            default="voice",
            description="Media type to scope: 'voice', 'message', or 'callback'.",
        ),
        exclude_anis: list[str] | None = Field(
            default=None,
            description="ANIs to exclude (e.g., test/internal numbers).",
        ),
        max_anis: int = Field(
            default=20, ge=1, le=100,
            description="Cap fan-out to this many top repeaters (ranked by acd_offered_count desc). Speech analytics calls fan out per conversation.",
        ),
        include_summaries: bool = Field(
            default=True,
            description="Pull AI summaries (topics) for answered calls. Disable for cheaper runs.",
        ),
        include_sentiment: bool = Field(
            default=True,
            description="Pull sentiment for answered calls. Disable for cheaper runs.",
        ),
    ) -> dict:
        """Repeat-caller funnel + WHY: topics, sentiment trajectory, last-call status, recommended action.

        Builds on repeat_caller_report by enriching the top repeaters with conversation
        summaries and sentiment, then clusters topics per ANI and across the org.

        For abandoned and IVR-only calls there's no transcript, so topics fall back to
        the queue name (e.g. 'Coles - Billing (abandoned)') — flagged with source='queue_fallback'
        so callers can tell real AI topics from inferred ones.

        Heuristic recommended_action labels (order matters, first match wins):
          - callback_recommended: abandoned_count >= 3 AND last call was not answered
          - escalate_to_retention: sentiment trend deteriorating AND topics mention billing/port/complaint/cancel
          - route_review: customer hit >= 3 distinct queues (likely a routing issue, not a customer issue)
          - monitor: none of the above

        Honest about data gaps: in tenants with sparse STA coverage (short calls, non-recorded
        queues), most rows will show topics from queue_fallback only and sentiment_trend
        'insufficient_data'. The funnel data is still valid.
        """
        interval = interval or _default_interval(7)
        excluded = set(exclude_anis or [])

        # ---- 1. Pull conversations (same shape as repeat_caller_report) ----
        if queue_ids:
            queue_clauses = [
                {
                    "type": "and",
                    "predicates": [
                        {"type": "dimension", "dimension": "queueId",
                         "operator": "matches", "value": qid},
                        {"type": "dimension", "dimension": "mediaType",
                         "operator": "matches", "value": media_type},
                    ],
                }
                for qid in queue_ids
            ]
            segment_filter = {"type": "or", "clauses": queue_clauses}
        else:
            segment_filter = {
                "type": "and",
                "predicates": [
                    {"type": "dimension", "dimension": "mediaType",
                     "operator": "matches", "value": media_type},
                ],
            }
        body = {
            "interval": interval,
            "order": "asc",
            "orderBy": "conversationStart",
            "conversationFilters": [
                {
                    "type": "and",
                    "predicates": [
                        {"type": "dimension", "dimension": "originatingDirection",
                         "operator": "matches", "value": "inbound"},
                    ],
                }
            ],
            "segmentFilters": [segment_filter],
        }
        convs = _run_conv_details_job(body)

        # ---- 2. Group by ANI with the canonical IVR/ACD/answered classification ----
        by_ani: dict[str, list[dict]] = defaultdict(list)
        for c in convs:
            ani = None
            answered = False
            acd_offered = False
            queue_id = None
            for p in c.get("participants") or []:
                purpose = p.get("purpose")
                if purpose == "customer":
                    for s in p.get("sessions") or []:
                        if s.get("ani") and not ani:
                            ani = (s["ani"] or "").replace("tel:", "")
                elif purpose == "acd":
                    acd_offered = True
                    for s in p.get("sessions") or []:
                        for seg in s.get("segments") or []:
                            if seg.get("queueId") and not queue_id:
                                queue_id = seg["queueId"]
                elif purpose == "agent" and p.get("userId"):
                    answered = True
            if not ani or ani.startswith("sip:") or ani in excluded:
                continue
            by_ani[ani].append({
                "conversation_id": c.get("conversationId"),
                "start": c.get("conversationStart"),
                "queue_id": queue_id,
                "queue_name": resolver.queue_name(queue_id) if queue_id else None,
                "acd_offered": acd_offered,
                "answered": answered,
            })

        # ---- 3. Rank and cap fan-out ----
        ranked = sorted(
            ((ani, calls) for ani, calls in by_ani.items()
             if len(calls) >= min_calls),
            key=lambda kv: -sum(1 for c in kv[1] if c["acd_offered"]),
        )
        shortlist = ranked[:max_anis]

        # ---- 4. Enrich answered conversations with STA (bounded concurrency) ----
        # We only enrich answered calls — abandoned / IVR-only have no recording so no STA data.
        # The /speechandtextanalytics/conversations/{id} endpoint returns sentiment + participant
        # metrics. Topic / summary text is not surfaced by this endpoint family in the AU tenant
        # (probed paths all 404'd) — likely needs Topic Spotting or AI Auto-Summarization
        # configured separately. Code is structured so summaries can plug in later without rewrite.
        enrich_needed = include_sentiment or include_summaries
        enrich_targets: list[tuple[str, str]] = []
        if enrich_needed:
            for ani, calls in shortlist:
                for call in calls:
                    if call["answered"] and call["conversation_id"]:
                        enrich_targets.append((ani, call["conversation_id"]))

        sta_calls = 0
        sta_with_data = 0
        wrapup_calls = 0
        wrapup_with_data = 0
        details: dict[str, dict | None] = {}
        wrapups: dict[str, dict | None] = {}

        def _enrich(cid: str) -> tuple[str, dict | None, dict | None]:
            return cid, _sta_details(cid), _fetch_wrapup(cid)

        if enrich_targets:
            with ThreadPoolExecutor(max_workers=8) as ex:
                cids = [cid for _, cid in enrich_targets]
                for cid, sta, wrap in ex.map(_enrich, cids):
                    details[cid] = sta
                    wrapups[cid] = wrap
                    sta_calls += 1
                    wrapup_calls += 1
                    if sta is not None:
                        sta_with_data += 1
                    if wrap is not None:
                        wrapup_with_data += 1

        logger.info("repeat_caller_deep_dive: enriched %d conversations across %d ANIs "
                    "(STA %d/%d, wrap-up %d/%d)",
                    len(enrich_targets), len(shortlist),
                    sta_with_data, sta_calls, wrapup_with_data, wrapup_calls)

        # Pre-warm queue name cache
        all_qids = {c["queue_id"] for _, calls in shortlist for c in calls if c["queue_id"]}
        resolver.queue_names(all_qids)

        # ---- 5. Build per-ANI rows ----
        rows: list[dict] = []
        org_topic_counter: Counter[str] = Counter()
        org_topic_anis: dict[str, set[str]] = defaultdict(set)
        for ani, calls in shortlist:
            n = len(calls)
            offered_n = sum(1 for c in calls if c["acd_offered"])
            answered_n = sum(1 for c in calls if c["answered"])
            abandoned_n = offered_n - answered_n
            ivr_only_n = n - offered_n
            queues_offered = Counter(
                c["queue_name"] or c["queue_id"]
                for c in calls
                if c["acd_offered"] and c["queue_id"]
            )

            # Per-call breakdown: queue-status counts (always available) + sentiment per call
            queue_status_counter: Counter[str] = Counter()
            disposition_counter: Counter[str] = Counter()
            ai_outcome_counter: Counter[str] = Counter()
            expected_fix_counter: Counter[str] = Counter()
            trajectory: list[dict] = []

            for call in calls:
                cid = call["conversation_id"]
                d = details.get(cid)
                w = wrapups.get(cid)

                if call["answered"]:
                    label = f"{call['queue_name'] or 'unknown queue'} (answered)"
                else:
                    label = (
                        f"{call['queue_name']} (abandoned)" if call["queue_name"]
                        else "IVR-only (no queue reached)"
                    )
                queue_status_counter[label] += 1

                if w is not None:
                    if w.get("disposition"):
                        disposition_counter[w["disposition"]] += 1
                    if w.get("ai_outcome"):
                        ai_outcome_counter[w["ai_outcome"]] += 1
                    if w.get("expected_fix"):
                        expected_fix_counter[w["expected_fix"]] += 1

                if d is not None:
                    pm = d.get("participant_metrics") or {}
                    raw_class = d.get("trend_class")
                    trend_class = "unknown" if raw_class == "NotCalculated" else raw_class
                    trajectory.append({
                        "conversation_id": cid,
                        "time": call["start"],
                        "queue_name": call["queue_name"],
                        "score": round(d["score"], 3),
                        "label": _sentiment_label(d["score"]),
                        "trend_class": trend_class,
                        "agent_pct": pm.get("agentDurationPercentage"),
                        "customer_pct": pm.get("customerDurationPercentage"),
                        "silence_pct": pm.get("silenceDurationPercentage"),
                    })

            # Top 5 queue-status labels for the ANI
            top_topics = [
                {"topic": label, "count": count, "source": "queue_status"}
                for label, count in queue_status_counter.most_common(5)
            ]
            # Org-rollup tracks queue-status labels too, so the response is useful even with
            # zero real STA topic data. ANIs the label affected are tracked for top_topics output.
            for label, count in queue_status_counter.items():
                org_topic_counter[label] += count
                org_topic_anis[label].add(ani)

            # Sentiment trajectory + trend
            trajectory.sort(key=lambda x: x["time"] or "")
            scores = [t["score"] for t in trajectory]
            single_class = trajectory[0]["trend_class"] if len(trajectory) == 1 else None
            trend = _trend_label(scores, single_call_trend_class=single_class)

            # Last call (by start time)
            calls_sorted = sorted(calls, key=lambda c: c["start"] or "")
            last = calls_sorted[-1]
            last_details = details.get(last["conversation_id"])
            last_wrap = wrapups.get(last["conversation_id"]) or {}
            last_notes = last_wrap.get("notes")
            last_call = {
                "conversation_id": last["conversation_id"],
                "time": last["start"],
                "queue_name": last["queue_name"],
                "status": "answered" if last["answered"] else (
                    "abandoned_in_queue" if last["acd_offered"] else "ivr_only"
                ),
                "disposition": last_wrap.get("disposition"),
                "ai_outcome": last_wrap.get("ai_outcome"),
                "expected_fix": last_wrap.get("expected_fix"),
                "summary": last_notes[:600] if last_notes else None,
                "sentiment": _sentiment_label(last_details["score"]) if last_details else None,
                "sentiment_trend_class": (
                    "unknown" if (last_details or {}).get("trend_class") == "NotCalculated"
                    else (last_details or {}).get("trend_class")
                ),
            }

            # Resolved share computed against answered calls that have an aiOutcome we can read.
            answered_with_outcome = sum(ai_outcome_counter.values())
            resolved_n = ai_outcome_counter.get("Resolved", 0)
            unresolved_share = (
                round((answered_with_outcome - resolved_n) / answered_with_outcome, 3)
                if answered_with_outcome else None
            )

            row = {
                "ani": ani,
                "call_count": n,
                "acd_offered_count": offered_n,
                "answered_count": answered_n,
                "abandoned_in_queue_count": abandoned_n,
                "ivr_only_count": ivr_only_n,
                "answer_rate_of_offered_pct": round(answered_n / offered_n * 100, 1) if offered_n else 0,
                "queues_offered": dict(queues_offered),
                "dispositions": dict(disposition_counter),
                "ai_outcomes": dict(ai_outcome_counter),
                "expected_fixes": dict(expected_fix_counter),
                "answered_with_outcome": answered_with_outcome,
                "unresolved_share": unresolved_share,
                "topics": top_topics,
                "sentiment_trajectory": trajectory,
                "sentiment_trend": trend,
                "last_call": last_call,
                "evidence_conversation_ids": [c["conversation_id"] for c in calls_sorted],
            }
            row["recommended_action"] = _recommend_action(row)
            rows.append(row)

        # ---- 6. Org rollup ----
        action_counts = Counter(r["recommended_action"] for r in rows)
        trend_counts = Counter(r["sentiment_trend"] for r in rows)
        single_queue = sum(1 for r in rows if len(r["queues_offered"]) == 1)
        multi_queue = sum(1 for r in rows if len(r["queues_offered"]) >= 3)
        top_topics_org = [
            {"topic": t, "count": c, "anis": len(org_topic_anis[t])}
            for t, c in org_topic_counter.most_common(10)
        ]

        # Aggregate wrap-up signals across all repeaters
        org_dispositions: Counter[str] = Counter()
        org_ai_outcomes: Counter[str] = Counter()
        org_expected_fixes: Counter[str] = Counter()
        unresolved_repeaters: list[dict] = []
        for r in rows:
            for k, v in r["dispositions"].items():
                org_dispositions[k] += v
            for k, v in r["ai_outcomes"].items():
                org_ai_outcomes[k] += v
            for k, v in r["expected_fixes"].items():
                org_expected_fixes[k] += v
            if r["unresolved_share"] is not None and r["unresolved_share"] >= 0.5 and r["answered_with_outcome"] >= 2:
                unresolved_repeaters.append({
                    "ani": r["ani"],
                    "answered_with_outcome": r["answered_with_outcome"],
                    "unresolved_share": r["unresolved_share"],
                    "ai_outcomes": r["ai_outcomes"],
                })
        unresolved_repeaters.sort(key=lambda x: (-x["unresolved_share"], -x["answered_with_outcome"]))

        return {
            "interval": interval,
            "media_type": media_type,
            "scope": {
                "max_anis": max_anis,
                "ranked_by": "acd_offered_count",
                "shortlisted": len(rows),
                "candidates_meeting_min_calls": len(ranked),
                "include_summaries": include_summaries,
                "include_sentiment": include_sentiment,
                "sta_calls_made": sta_calls,
                "sta_calls_with_data": sta_with_data,
                "sta_coverage_pct": round(sta_with_data / sta_calls * 100, 1) if sta_calls else 0,
                "wrapup_calls_made": wrapup_calls,
                "wrapup_calls_with_data": wrapup_with_data,
                "wrapup_coverage_pct": round(wrapup_with_data / wrapup_calls * 100, 1) if wrapup_calls else 0,
            },
            "org_rollup": {
                "top_topics": top_topics_org,
                "top_dispositions": [{"disposition": d, "count": c} for d, c in org_dispositions.most_common(10)],
                "top_ai_outcomes": dict(org_ai_outcomes),
                "top_expected_fixes": [{"fix": f, "count": c} for f, c in org_expected_fixes.most_common(10)],
                "recommended_actions": dict(action_counts),
                "sentiment_trends": dict(trend_counts),
                "single_queue_repeaters": single_queue,
                "multi_queue_repeaters": multi_queue,
                "unresolved_repeaters": unresolved_repeaters,
            },
            "repeaters": rows,
        }

    @mcp.tool()
    def break_overrun_report(
        user_ids: list[str] = Field(
            description="User ids to check. Use list_users / find_user to resolve names.",
        ),
        interval: str | None = Field(
            default=None,
            description="ISO-8601 interval. Defaults to last 7 days UTC.",
        ),
        break_target_min: int = Field(
            default=15,
            ge=1, le=120,
            description="Target break duration in minutes (default 15).",
        ),
        meal_target_min: int = Field(
            default=30,
            ge=1, le=120,
            description="Target meal duration in minutes (default 30).",
        ),
        tolerance_min: int = Field(
            default=2,
            ge=0, le=10,
            description="Grace period in minutes before flagging an overrun (default 2).",
        ),
    ) -> dict:
        """Per-agent break/meal overrun summary for an interval.

        Pulls presence sessions, classifies each as on-target / over / under, and
        ranks users by overrun frequency. Includes a per-instance list so a TL can
        see which specific breaks were over.
        """
        # Reuse the presence_sessions tool's data fetching logic by calling the
        # same job pipeline directly here — keeps this tool self-contained.
        if not user_ids:
            raise ValueError("user_ids must contain at least one id.")
        interval = interval or _default_interval(7)
        try:
            start_str, end_str = interval.split("/", 1)
            interval_start = _parse_iso(start_str)
            interval_end = _parse_iso(end_str)
        except Exception as exc:
            raise ValueError(f"Invalid interval {interval!r}") from exc

        api = gc.AnalyticsApi(get_api())
        body = {
            "interval": interval,
            "order": "asc",
            "userFilters": [{
                "type": "or",
                "predicates": [
                    {"type": "dimension", "dimension": "userId",
                     "operator": "matches", "value": uid}
                    for uid in user_ids
                ],
            }],
        }
        submit = with_retry(api.post_analytics_users_details_jobs)(body=body)
        job_id = submit.job_id if hasattr(submit, "job_id") else to_dict(submit).get("jobId")

        for _ in range(30):
            status = with_retry(api.get_analytics_users_details_job)(job_id=job_id)
            state = getattr(status, "state", None) or to_dict(status).get("state")
            if state == "FULFILLED":
                break
            if state in ("FAILED", "CANCELLED", "EXPIRED"):
                raise RuntimeError(f"job {job_id} terminated in state {state}")
            time.sleep(1)

        sessions_by_user: dict[str, list[dict]] = {uid: [] for uid in user_ids}
        cursor = None
        for _ in range(20):
            kwargs: dict[str, Any] = {"job_id": job_id, "page_size": 100}
            if cursor:
                kwargs["cursor"] = cursor
            page = with_retry(api.get_analytics_users_details_job_results)(**kwargs)
            page_dict = to_dict(page) or {}
            for ud in page_dict.get("userDetails") or []:
                uid = ud.get("userId")
                if uid not in sessions_by_user:
                    continue
                for sess in ud.get("primaryPresence") or []:
                    sp = (sess.get("systemPresence") or "").upper()
                    if sp not in ("BREAK", "MEAL"):
                        continue
                    if not sess.get("startTime") or not sess.get("endTime"):
                        continue
                    try:
                        st = _parse_iso(sess["startTime"])
                        en = _parse_iso(sess["endTime"])
                    except Exception:
                        continue
                    if en < interval_start or st > interval_end:
                        continue
                    st_clip = max(st, interval_start)
                    en_clip = min(en, interval_end)
                    dur_s = (en_clip - st_clip).total_seconds()
                    if dur_s <= 0:
                        continue
                    target_s = (break_target_min if sp == "BREAK" else meal_target_min) * 60
                    sessions_by_user[uid].append({
                        "presence": sp,
                        "start_utc": st_clip.isoformat().replace("+00:00", "Z"),
                        "duration_s": int(dur_s),
                        "duration_min": round(dur_s / 60, 1),
                        "target_min": target_s // 60,
                        "over_target": dur_s > (target_s + tolerance_min * 60),
                        "overrun_min": round((dur_s - target_s) / 60, 1) if dur_s > target_s else 0.0,
                    })
            cursor = page_dict.get("cursor")
            if not cursor:
                break

        # Resolve user names
        names = resolver.user_names(user_ids)

        # Rank users by overrun count
        ranked = []
        for uid, sessions in sessions_by_user.items():
            over = [s for s in sessions if s["over_target"]]
            break_ct = sum(1 for s in sessions if s["presence"] == "BREAK")
            meal_ct = sum(1 for s in sessions if s["presence"] == "MEAL")
            avg_break = (
                sum(s["duration_s"] for s in sessions if s["presence"] == "BREAK") / break_ct / 60
                if break_ct else 0
            )
            avg_meal = (
                sum(s["duration_s"] for s in sessions if s["presence"] == "MEAL") / meal_ct / 60
                if meal_ct else 0
            )
            total_overrun_min = sum(s["overrun_min"] for s in over)
            ranked.append({
                "user_id": uid,
                "user_name": names.get(uid),
                "total_sessions": len(sessions),
                "overrun_count": len(over),
                "total_overrun_min": round(total_overrun_min, 1),
                "break_count": break_ct,
                "avg_break_min": round(avg_break, 1),
                "meal_count": meal_ct,
                "avg_meal_min": round(avg_meal, 1),
                "overrun_sessions": over,
            })
        ranked.sort(key=lambda r: (-r["overrun_count"], -r["total_overrun_min"]))

        return {
            "interval": interval,
            "break_target_min": break_target_min,
            "meal_target_min": meal_target_min,
            "tolerance_min": tolerance_min,
            "users": ranked,
        }

    @mcp.tool()
    def agent_quality_snapshot(
        user_id: str = Field(description="User id to review."),
        interval: str | None = Field(
            default=None,
            description="ISO-8601 interval. Defaults to last 7 days UTC.",
        ),
        peer_user_ids: list[str] | None = Field(
            default=None,
            description="Optional peer user ids to include for benchmark comparison.",
        ),
        flag_silent_threshold_pct: float = Field(
            default=10.0,
            ge=0.0, le=100.0,
            description="If %% of voice calls have 'undefined' transcript, flag as concern.",
        ),
    ) -> dict:
        """One-shot agent review combining handle stats, conversation patterns, and quality signals.

        Replaces the multi-step Amelia-style review. Returns:
        - Volume + AHT + ACW stats per media (voice/message)
        - Hold-ratio distribution flagging excessive holds
        - Silent-transcript detection (auto-summary indicates no dialogue)
        - Wrap-up note coverage (% calls with own agent note vs auto-summary only)
        - Cross-queue spread
        - Peer comparison columns when peer_user_ids supplied
        """
        interval = interval or _default_interval(7)
        ids_to_pull = [user_id] + (peer_user_ids or [])

        # 1. Aggregates per user x media
        aggr_api = gc.AnalyticsApi(get_api())
        aggr_body = {
            "interval": interval,
            "granularity": "P7D",
            "groupBy": ["userId", "mediaType"],
            "filter": {
                "type": "or",
                "clauses": [
                    {"type": "and", "predicates": [
                        {"dimension": "userId", "value": uid}
                    ]} for uid in ids_to_pull
                ],
            },
            "metrics": ["nConnected", "tHandle", "tTalk", "tAcw", "tHeld", "tAnswered"],
        }
        aggr_resp = to_dict(with_retry(aggr_api.post_analytics_conversations_aggregates_query)(aggr_body))

        agg_by_user_media: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
        for r in aggr_resp.get("results") or []:
            uid = r["group"].get("userId")
            media = r["group"].get("mediaType", "?")
            for bucket in r["data"]:
                stats_by_metric = {m["metric"]: m.get("stats", {}) for m in bucket["metrics"]}
                agg_by_user_media[uid][media] = stats_by_metric

        # 2. Conversation details for the target user only (peers don't need the deep-dive)
        conv_body = {
            "interval": interval,
            "order": "desc",
            "orderBy": "conversationStart",
            "segmentFilters": [{
                "type": "and",
                "predicates": [
                    {"type": "dimension", "dimension": "userId",
                     "operator": "matches", "value": user_id},
                ],
            }],
        }
        convs = _run_conv_details_job(conv_body)

        # 3. Analyse target user's calls
        voice_calls = []
        message_calls = []
        wrapup_codes_seen: list[str] = []
        silent_count = 0
        own_note_count = 0
        for c in convs:
            media = None
            queue_id = None
            talk_s = 0.0
            hold_s = 0.0
            acw_s = 0.0
            wrapup_codes = []
            wrapup_notes = []
            for p in c.get("participants") or []:
                if p.get("userId") != user_id:
                    continue
                for s in p.get("sessions") or []:
                    if s.get("mediaType") in ("voice", "message", "callback"):
                        media = s["mediaType"]
                    for seg in s.get("segments") or []:
                        d = _seg_dur_s(seg)
                        st = seg.get("segmentType")
                        if st == "interact":
                            talk_s += d
                        elif st == "hold":
                            hold_s += d
                        elif st == "wrapup":
                            acw_s += d
                            if seg.get("wrapUpCode"):
                                wrapup_codes.append(seg["wrapUpCode"])
                            if seg.get("wrapUpNote"):
                                wrapup_notes.append(seg["wrapUpNote"])
                        if seg.get("queueId") and not queue_id:
                            queue_id = seg["queueId"]
            wrapup_codes_seen.extend(wrapup_codes)
            # Silent-transcript detection: AI auto-summary often contains "undefined"
            note_text = " ".join(wrapup_notes).lower()
            silent = "undefined" in note_text and ("no customer" in note_text or "no substantive" in note_text or "placeholder" in note_text)
            if silent:
                silent_count += 1
            # Own note = wrapup_note that has agent text BEFORE "Auto Summary"
            has_own = any(
                "agent note:" in n.lower()
                and (n.lower().split("auto summary")[0]
                     .replace("agent note:", "").strip())
                for n in wrapup_notes
            )
            if has_own:
                own_note_count += 1

            row = {
                "conversation_id": c.get("conversationId"),
                "start": c.get("conversationStart"),
                "queue_id": queue_id,
                "queue_name": resolver.queue_name(queue_id) if queue_id else None,
                "talk_s": int(talk_s),
                "hold_s": int(hold_s),
                "acw_s": int(acw_s),
                "hold_pct_of_talk": round(hold_s / talk_s * 100, 1) if talk_s else 0,
                "wrapup_codes": [{"id": wc, "name": resolver.wrapup_name(wc)} for wc in wrapup_codes],
                "silent_transcript": silent,
                "own_agent_note": has_own,
            }
            if media == "voice":
                voice_calls.append(row)
            elif media == "message":
                message_calls.append(row)

        def _summarise(calls: list[dict]) -> dict:
            if not calls:
                return {"count": 0}
            n = len(calls)
            total_talk = sum(c["talk_s"] for c in calls)
            total_hold = sum(c["hold_s"] for c in calls)
            total_acw = sum(c["acw_s"] for c in calls)
            high_hold = [c for c in calls if c["hold_pct_of_talk"] > 100]
            return {
                "count": n,
                "total_talk_min": round(total_talk / 60, 1),
                "total_hold_min": round(total_hold / 60, 1),
                "total_acw_min": round(total_acw / 60, 1),
                "avg_handle_s": round((total_talk + total_acw) / n, 0) if n else 0,
                "hold_ratio_pct": round(total_hold / total_talk * 100, 1) if total_talk else 0,
                "calls_with_hold_over_talk": len(high_hold),
            }

        voice_summary = _summarise(voice_calls)
        message_summary = _summarise(message_calls)

        # Wrapup code distribution
        code_ids = wrapup_codes_seen
        code_counter = Counter(code_ids)
        wrapup_distribution = [
            {"code_id": cid, "code_name": resolver.wrapup_name(cid), "count": n}
            for cid, n in code_counter.most_common(20)
        ]

        # Quality flags
        voice_n = len(voice_calls)
        silent_pct = (silent_count / voice_n * 100) if voice_n else 0
        own_note_pct = (own_note_count / voice_n * 100) if voice_n else 0
        flags = []
        if silent_pct > flag_silent_threshold_pct:
            flags.append({
                "kind": "silent_transcripts",
                "severity": "high",
                "message": f"{silent_count} of {voice_n} voice calls ({silent_pct:.0f}%) had no recorded dialogue per auto-summary.",
            })
        if voice_n >= 5 and own_note_pct < 25:
            flags.append({
                "kind": "low_note_discipline",
                "severity": "medium",
                "message": f"Agent added own note on only {own_note_count}/{voice_n} voice calls ({own_note_pct:.0f}%).",
            })
        if voice_summary.get("calls_with_hold_over_talk", 0) >= 3:
            flags.append({
                "kind": "excessive_holds",
                "severity": "medium",
                "message": f"{voice_summary['calls_with_hold_over_talk']} voice calls had hold time exceeding talk time.",
            })

        # Peer comparison
        peer_rows = []
        if peer_user_ids:
            peer_names = resolver.user_names(peer_user_ids)
            for pid in peer_user_ids:
                voice_stats = agg_by_user_media.get(pid, {}).get("voice", {})
                handle_count = (voice_stats.get("tHandle") or {}).get("count", 0)
                handle_sum = (voice_stats.get("tHandle") or {}).get("sum", 0)
                peer_rows.append({
                    "user_id": pid,
                    "user_name": peer_names.get(pid),
                    "voice_handled": handle_count,
                    "voice_avg_handle_s": round(handle_sum / handle_count / 1000) if handle_count else 0,
                })

        return {
            "interval": interval,
            "user_id": user_id,
            "user_name": resolver.user_name(user_id),
            "voice": voice_summary,
            "message": message_summary,
            "quality_flags": flags,
            "silent_transcript_count": silent_count,
            "silent_transcript_pct": round(silent_pct, 1),
            "own_agent_note_count": own_note_count,
            "own_agent_note_pct": round(own_note_pct, 1),
            "wrapup_code_distribution": wrapup_distribution,
            "voice_calls": voice_calls,
            "message_calls": message_calls,
            "peer_comparison": peer_rows,
        }

    @mcp.tool()
    def live_wallboard(
        queue_ids: list[str] = Field(description="Queue ids to display."),
        media_types: list[str] | None = Field(
            default=None,
            description="Media types to include. Defaults to ['call','message'].",
        ),
    ) -> dict:
        """Combined real-time view for a list of queues — replaces 3+ separate API calls.

        Per queue per media: currently waiting, currently interacting, oldest waiter age,
        Genesys-modeled EWT (where supported), and queue name.
        """
        if not queue_ids:
            raise ValueError("queue_ids must contain at least one id.")
        media_types = media_types or ["call", "message"]

        # 1. queue observations (single bulk call)
        api = gc.AnalyticsApi(get_api())
        obs_body = {
            "filter": {
                "type": "or",
                "predicates": [{"dimension": "queueId", "value": qid} for qid in queue_ids],
            },
            "metrics": ["oWaiting", "oInteracting", "oLongestWaiting", "oOnQueueUsers"],
            "groupBy": ["queueId", "mediaType"],
        }
        obs_resp = to_dict(with_retry(api.post_analytics_queues_observations_query)(obs_body))

        observation_by_qm: dict[tuple[str, str], dict] = {}
        agents_per_queue: dict[str, int] = {}
        for r in obs_resp.get("results") or []:
            qid = r["group"].get("queueId")
            media = r["group"].get("mediaType")
            metrics = {m["metric"]: m for m in r.get("data") or []}
            if media:
                rec = {
                    "waiting": (metrics.get("oWaiting") or {}).get("stats", {}).get("count", 0),
                    "interacting": (metrics.get("oInteracting") or {}).get("stats", {}).get("count", 0),
                }
                lw = metrics.get("oLongestWaiting") or {}
                lw_val = (lw.get("stats") or {}).get("calculatedMetricValue")
                rec["oldest_waiter_started_ms"] = lw_val
                observation_by_qm[(qid, media)] = rec
            else:
                # queue-level (no media) → has oOnQueueUsers
                onq = metrics.get("oOnQueueUsers") or {}
                count = (onq.get("stats") or {}).get("count", 0)
                agents_per_queue[qid] = count

        # 2. EWT per queue × media (one call each)
        client = get_api()
        ewt_by_qm: dict[tuple[str, str], int | None] = {}
        for qid in queue_ids:
            for media in media_types:
                path = f"/api/v2/routing/queues/{qid}/mediatypes/{media}/estimatedwaittime"
                try:
                    resp = with_retry(lambda: client.call_api(
                        resource_path=path, method="GET",
                        query_params={}, header_params={"Accept": "application/json"},
                        auth_settings=["PureCloud OAuth"], response_type="object",
                    ))()
                    results = (resp or {}).get("results") or []
                    ewt = results[0].get("estimatedWaitTimeSeconds") if results else None
                    ewt_by_qm[(qid, media)] = ewt
                except Exception:
                    ewt_by_qm[(qid, media)] = None

        # 3. Pre-warm queue names
        names = resolver.queue_names(queue_ids)

        # 4. Build response, computing oldest_waiter_age_s from now
        now_ms = int(time.time() * 1000)
        rows = []
        for qid in queue_ids:
            for media in media_types:
                obs = observation_by_qm.get((qid, media), {})
                started = obs.get("oldest_waiter_started_ms")
                oldest_age_s = (now_ms - started) / 1000 if started else None
                rows.append({
                    "queue_id": qid,
                    "queue_name": names.get(qid),
                    "media_type": media,
                    "waiting": obs.get("waiting", 0),
                    "interacting": obs.get("interacting", 0),
                    "oldest_waiter_age_s": int(oldest_age_s) if oldest_age_s else None,
                    "estimated_wait_time_s": ewt_by_qm.get((qid, media)),
                    "agents_on_queue": agents_per_queue.get(qid),
                })
        return {"as_of_utc": datetime.now(timezone.utc).isoformat(), "rows": rows}
