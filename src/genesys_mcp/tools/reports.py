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
from datetime import datetime, timedelta, timezone
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry
from genesys_mcp.naming import resolver

logger = logging.getLogger(__name__)


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
        """Find customers calling repeatedly within an interval, with the queues they hit.

        Returns one row per repeater customer, sorted by call count descending. Includes
        connect rate (calls where an agent was reached) and the queue mix touched —
        useful for FCR analysis and routing diagnostics.

        Backed by /api/v2/analytics/conversations/details/jobs (async). Replaces the
        pull-pages-and-aggregate-in-Python pattern we were doing manually.
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

        by_ani: dict[str, list[dict]] = defaultdict(list)
        for c in convs:
            ani = None
            connected = False
            queue_id = None
            for p in c.get("participants") or []:
                if p.get("purpose") == "customer":
                    for s in p.get("sessions") or []:
                        if s.get("ani") and not ani:
                            ani = (s["ani"] or "").replace("tel:", "")
                if p.get("purpose") == "agent" and p.get("userId"):
                    connected = True
                for s in p.get("sessions") or []:
                    for seg in s.get("segments") or []:
                        if seg.get("queueId") and not queue_id:
                            queue_id = seg["queueId"]
            if not ani or ani.startswith("sip:") or ani in excluded:
                continue
            by_ani[ani].append({
                "conversation_id": c.get("conversationId"),
                "start": c.get("conversationStart"),
                "queue_id": queue_id,
                "queue_name": resolver.queue_name(queue_id) if queue_id else None,
                "connected": connected,
            })

        # Build repeater rows
        all_qids = {r["queue_id"] for rows in by_ani.values() for r in rows if r["queue_id"]}
        resolver.queue_names(all_qids)  # pre-warm cache

        rows = []
        for ani, calls in by_ani.items():
            n = len(calls)
            if n < min_calls:
                continue
            connected_n = sum(1 for r in calls if r["connected"])
            queue_counter = Counter(r["queue_name"] or r["queue_id"] for r in calls if r["queue_id"])
            rows.append({
                "ani": ani,
                "call_count": n,
                "connected_count": connected_n,
                "connect_rate_pct": round(connected_n / n * 100, 1) if n else 0,
                "queues_touched": dict(queue_counter),
                "first_call": calls[0]["start"],
                "last_call": calls[-1]["start"],
                "conversation_ids": [r["conversation_id"] for r in calls],
            })
        rows.sort(key=lambda r: -r["call_count"])

        total_unique = len({c.get("participants") and (c.get("participants")[0].get("sessions") or [{}])[0].get("ani") for c in convs}) if convs else 0

        return {
            "interval": interval,
            "media_type": media_type,
            "total_conversations": len(convs),
            "unique_callers": len(by_ani),
            "repeater_count": len(rows),
            "repeater_calls": sum(r["call_count"] for r in rows),
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
