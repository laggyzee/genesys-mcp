"""Routing diagnostics — explain why a specific call routed (or didn't) as expected.

Supervisors spelunk through queue configs, skill assignments, and routing flow
audit logs to answer "why did this call go to that agent / why was it
abandoned / why did the wait blow out". This tool collapses that walk into a
single per-call payload:

- The conversation's path: IVR → queue → outcome, with time-in-each
- The queue's routing rules: skill requirements, routing method, ACD enabled
- Current eligible-agent count (members of the queue who have all required skills)
- Outcome classification: answered / abandoned (with abandon reason) / transferred
- Per-offer log: when was the call offered, to whom, accepted or declined

Conversation_id mode only in v0.5 — aggregate mode ("show me all the failed
routes this week") is planned for v0.5.x.
"""
from __future__ import annotations

import logging
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry
from genesys_mcp.tools.reports import _parse_iso, _run_conv_details_job, _seg_dur_s

logger = logging.getLogger(__name__)


def _fetch_analytics_conversation(conversation_id: str) -> dict | None:
    """Pull the analytics view of a conversation — structured segments + eligible-agent counts.

    The live ``get_conversation`` endpoint doesn't expose segments the same
    way; the analytics endpoint gives us session-level ``segments`` (with
    ``segmentType`` / start / end), ``eligibleAgentCounts``, ``requestedRoutings``,
    and ``activeSkillIds`` — everything routing diagnostics needs.

    Returns None on 404.
    """
    api = gc.AnalyticsApi(get_api())
    try:
        resp = with_retry(api.get_analytics_conversation_details)(
            conversation_id=conversation_id,
        )
        return to_dict(resp)
    except gc.rest.ApiException as exc:
        if exc.status == 404:
            return None
        raise


def _fetch_queue(queue_id: str) -> dict | None:
    api = gc.RoutingApi(get_api())
    try:
        resp = with_retry(api.get_routing_queue)(queue_id=queue_id)
        return to_dict(resp)
    except gc.rest.ApiException as exc:
        if exc.status == 404:
            return None
        raise


_QUEUE_MEMBERS_PAGE_SIZE = 100  # server max for GET /routing/queues/{id}/members


def _fetch_queue_members(queue_id: str, max_pages: int = 5) -> list[dict]:
    """Pull all members of a queue with their joined/active state.

    ``pageSize`` for this endpoint is capped at 100 server-side; requesting
    more silently clamps (or 400s), and looping on ``< requested_page_size``
    with an unclamped request size would then never terminate correctly.
    """
    api = gc.RoutingApi(get_api())
    out: list[dict] = []
    for pg in range(1, max_pages + 1):
        resp = to_dict(
            with_retry(api.get_routing_queue_members)(
                queue_id=queue_id, page_size=_QUEUE_MEMBERS_PAGE_SIZE,
                page_number=pg, joined=True,
                expand=["routingStatus", "skills"],
            )
        )
        entities = resp.get("entities") or []
        out.extend(entities)
        if len(entities) < _QUEUE_MEMBERS_PAGE_SIZE:
            break
    return out


# disconnectType values that indicate a transfer. segmentType has NO
# 'transfer'/'ininternaltransfer' value in the Genesys schema (see
# AnalyticsConversationSegment.segmentType) — transfers are indicated by
# AnalyticsConversationSegment.disconnectType instead. Every disconnectType
# enum member whose name denotes a transfer is included; 'dndEndpoint' is
# excluded (it's a DND decline, not a transfer).
_TRANSFER_DISCONNECT_TYPES = frozenset({
    "transfer",
    "conferenceTransfer",
    "consultTransfer",
    "forwardTransfer",
    "noAnswerTransfer",
    "notAvailableTransfer",
    "dndTransfer",
})


def _has_agent_interact_segment(conv: dict) -> bool:
    """True if any agent-purpose participant has an 'interact' segment.

    This is the "was the call actually answered by an agent" signal used
    by both the per-call and aggregate outcome classifiers — see
    ``routing_diagnostic_aggregate._matches_outcome`` below, which this
    helper was extracted from (it originally reused
    ``session.flaggedReason``, a field that doesn't exist on AnalyticsSession
    and made the 'abandoned' verdict unreachable).
    """
    for p in conv.get("participants") or []:
        if p.get("purpose") != "agent":
            continue
        for s in p.get("sessions") or []:
            for seg in s.get("segments") or []:
                if seg.get("segmentType") == "interact":
                    return True
    return False


def _count_transfer_segments(conv: dict) -> int:
    """Count segments whose ``disconnectType`` indicates a transfer.

    Shared by ``_classify_outcome`` (per-call) and
    ``routing_diagnostic_aggregate._matches_outcome`` (aggregate) so both
    tools agree on what counts as a transfer.
    """
    count = 0
    for p in conv.get("participants") or []:
        for s in p.get("sessions") or []:
            for seg in s.get("segments") or []:
                if seg.get("disconnectType") in _TRANSFER_DISCONNECT_TYPES:
                    count += 1
    return count


def _classify_outcome(conv: dict) -> dict[str, Any]:
    """Reduce the analytics conversation shape into a clear outcome verdict.

    Answered: any agent-purpose participant has an 'interact' segment
    (:func:`_has_agent_interact_segment`). Abandoned: no such segment exists
    — the customer disconnected before any agent picked up. This mirrors
    ``routing_diagnostic_aggregate._matches_outcome``'s abandon logic (both
    now call the same shared helper) rather than the pre-v1.14 code, which
    read a nonexistent ``session.flaggedReason`` field and could never
    actually reach the 'abandoned' verdict.

    Transfers are counted via ``disconnectType`` (:func:`_count_transfer_segments`)
    — ``segmentType`` has no transfer value in the Genesys schema.
    """
    answered = _has_agent_interact_segment(conv)
    transfers = _count_transfer_segments(conv)

    first_answer_at: str | None = None
    if answered:
        for p in conv.get("participants") or []:
            if p.get("purpose") != "agent":
                continue
            for s in p.get("sessions") or []:
                for seg in s.get("segments") or []:
                    if seg.get("segmentType") == "interact":
                        if not first_answer_at:
                            first_answer_at = seg.get("segmentStart")

    abandoned_at: str | None = None
    if not answered:
        for p in conv.get("participants") or []:
            if p.get("purpose") != "customer":
                continue
            for s in p.get("sessions") or []:
                segs = s.get("segments") or []
                if segs:
                    abandoned_at = segs[-1].get("segmentEnd")

    outcome: dict[str, Any] = {
        "verdict": "answered" if answered else "abandoned",
        "explanation": "",
        "first_answer_at": first_answer_at,
        "abandoned_at": abandoned_at if not answered else None,
        "transfer_count": transfers,
    }
    if answered:
        outcome["explanation"] = "Call connected to an agent and was answered."
    else:
        outcome["explanation"] = (
            "Customer disconnected before any agent picked up "
            "(no agent-purpose interact segment found)."
        )
    if transfers:
        outcome["explanation"] += f" Call was transferred {transfers} time(s)."
    return outcome


def _trace_path(conv: dict) -> list[dict]:
    """Chronological IVR/queue/agent path from the analytics segments.

    Surfaces eligible-agent counts per session (sourced from
    ``session.eligibleAgentCounts`` — Genesys-provided, this is the count at
    the time of routing, not the current-state proxy).
    """
    events: list[dict] = []

    for p in conv.get("participants") or []:
        purpose = p.get("purpose")
        for s in p.get("sessions") or []:
            session_eligible_counts = s.get("eligibleAgentCounts") or []
            session_active_skills = s.get("activeSkillIds") or []
            requested_routings = s.get("requestedRoutings") or []
            for seg in s.get("segments") or []:
                st = seg.get("segmentType")
                if st in ("ivr", "system"):
                    events.append({
                        "kind": "ivr_or_system",
                        "started_at": seg.get("segmentStart"),
                        "ended_at": seg.get("segmentEnd"),
                        "duration_s": round(_seg_dur_s(seg), 1),
                        "participant_purpose": purpose,
                    })
                elif st == "interact" and purpose in ("acd", "agent"):
                    qid = seg.get("queueId")
                    events.append({
                        "kind": ("queue_wait" if purpose == "acd" else "agent_handle"),
                        "started_at": seg.get("segmentStart"),
                        "ended_at": seg.get("segmentEnd"),
                        "duration_s": round(_seg_dur_s(seg), 1),
                        "queue_id": qid,
                        "agent_user_id": p.get("userId") if purpose == "agent" else None,
                        "eligible_agent_counts": session_eligible_counts,
                        "active_skill_ids": session_active_skills,
                        "requested_routings": requested_routings,
                    })
                elif st in ("hold",):
                    events.append({
                        "kind": "hold",
                        "started_at": seg.get("segmentStart"),
                        "ended_at": seg.get("segmentEnd"),
                        "duration_s": round(_seg_dur_s(seg), 1),
                    })
                # Transfer is a disconnectType, not a segmentType — it can
                # co-occur with any of the segments above, so it's checked
                # independently rather than as another segmentType branch.
                if seg.get("disconnectType") in _TRANSFER_DISCONNECT_TYPES:
                    events.append({
                        "kind": "transfer",
                        "started_at": seg.get("segmentStart"),
                        "ended_at": seg.get("segmentEnd"),
                        "duration_s": round(_seg_dur_s(seg), 1),
                        "queue_id": seg.get("queueId"),
                        "disconnect_type": seg.get("disconnectType"),
                    })
    events.sort(key=lambda e: e.get("started_at") or "")
    return events


def _eligible_member_count(
    members: list[dict], active_skill_ids: list[str] | None,
) -> dict[str, Any]:
    """How many current queue members are eligible for THIS call's skill set?

    Queue has no required-skills field in the Genesys schema (no
    ``memberGroups[].skills`` or top-level ``skills``) — there is no
    queue-level "required skills" to read. QueueMember itself carries no
    skills either; they live under ``member.user.skills`` (populated when
    the members call is expanded with ``expand=["skills"]``, as
    ``_fetch_queue_members`` does).

    When the analytics conversation exposes ``activeSkillIds`` for this
    queue-session, we treat that as the actual skill requirement for THIS
    call and compute eligibility against it. Without that signal there is
    no queue-level fallback to compute eligibility from, so we report
    membership honestly (total/idle counts) and leave eligibility unset
    rather than fabricate a requirement.

    'Current' state — not historical. We can't reconstruct which skills an
    agent held at the moment of the call, so this is a current-state proxy;
    most informative for recent failures.
    """
    total_members = len(members)
    idle_members = sum(
        1 for m in members
        if (m.get("routingStatus") or {}).get("status") == "IDLE"
    )

    if not active_skill_ids:
        return {
            "total_members": total_members,
            "idle_now": idle_members,
            "eligible_now": None,
            "idle_eligible_now": None,
            "sample_eligible_idle": [],
            "eligibility_note": (
                "No activeSkillIds on this conversation's queue session — "
                "eligibility isn't computed (Queue has no required-skills "
                "field to fall back on). total_members/idle_now are "
                "unfiltered membership counts."
            ),
        }

    required = set(active_skill_ids)
    eligible: list[dict] = []
    idle_eligible: list[dict] = []
    for m in members:
        user = m.get("user") or {}
        user_skill_ids = {
            s.get("id") for s in (user.get("skills") or []) if s.get("id")
        }
        if required.issubset(user_skill_ids):
            row = {
                "user_id": user.get("id"),
                "user_name": user.get("name"),
                "routing_status": (m.get("routingStatus") or {}).get("status"),
            }
            eligible.append(row)
            if row["routing_status"] == "IDLE":
                idle_eligible.append(row)
    return {
        "total_members": total_members,
        "idle_now": idle_members,
        "eligible_now": len(eligible),
        "idle_eligible_now": len(idle_eligible),
        "sample_eligible_idle": idle_eligible[:5],
        "required_skill_ids": sorted(required),
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def routing_diagnostic(
        conversation_id: str = Field(
            description=(
                "Conversation id to diagnose. The tool will pull the conversation, "
                "trace its IVR → queue → outcome path, and surface the queue's "
                "routing rules + current eligible-agent count."
            ),
        ),
    ) -> dict:
        """Per-call routing trace — explain why this conversation ended up where it did.

        Returns:

        - **outcome**: answered / abandoned, with explanation and transfer count
        - **path**: chronological IVR → queue → agent path with durations
        - **queues_visited**: each unique queue, plus current membership
          (total members / idle members). Skill-based eligibility is only
          computed when the conversation exposes ``activeSkillIds`` for
          that queue-session — Queue has no required-skills field to read,
          so eligibility is never fabricated when that signal is absent.
        - **timing**: total time-in-queue, time-to-first-offer, transfer count

        Limitations (v0.5):

        - "Eligible-agent count" is **current** state, not historical. For very
          old conversations this is approximate; recent failures (last few days)
          it answers "is this queue under-staffed for the skill set?"
        - Per-agent offer/decline log isn't easily reconstructable from the
          conversations API; surfaced as transfer-count for now.
        - Aggregate failure-mode analysis ("show me all this week's abandons by
          cause") is planned for v0.5.x.
        """
        conv = _fetch_analytics_conversation(conversation_id)
        if not conv:
            return {
                "conversation_id": conversation_id,
                "found": False,
                "message": "Conversation not found (404).",
            }

        outcome = _classify_outcome(conv)
        path = _trace_path(conv)

        # Unique queue ids touched
        queue_ids: list[str] = []
        for ev in path:
            qid = ev.get("queue_id")
            if qid and qid not in queue_ids:
                queue_ids.append(qid)

        queues_visited: list[dict] = []
        for qid in queue_ids:
            q = _fetch_queue(qid)
            if not q:
                queues_visited.append({"queue_id": qid, "found": False})
                continue
            members = _fetch_queue_members(qid)
            # activeSkillIds is a session-level field on THIS conversation's
            # queue-wait event — it's the closest thing to a real skill
            # requirement for this specific call (Queue itself has none).
            active_skill_ids: list[str] = []
            for ev in path:
                if ev.get("queue_id") == qid and ev.get("active_skill_ids"):
                    active_skill_ids = ev["active_skill_ids"]
                    break
            eligibility = _eligible_member_count(members, active_skill_ids)
            queues_visited.append({
                "queue_id": qid,
                "queue_name": q.get("name"),
                "found": True,
                "media_settings_count": len(q.get("mediaSettings") or {}),
                "skill_evaluation_method": q.get("skillEvaluationMethod"),
                "acw_settings": q.get("acwSettings"),
                "auto_answer_on_call_enabled": q.get("autoAnswerOnCall"),
                "eligibility_now": eligibility,
            })

        # Timing summary
        time_in_queue_s = 0.0
        time_to_first_answer_s: float | None = None
        first_acd_arrival: str | None = None
        for ev in path:
            if ev["kind"] == "queue_wait":
                time_in_queue_s += ev["duration_s"]
                if not first_acd_arrival:
                    first_acd_arrival = ev["started_at"]
        if first_acd_arrival and outcome["first_answer_at"]:
            try:
                time_to_first_answer_s = (
                    _parse_iso(outcome["first_answer_at"]) - _parse_iso(first_acd_arrival)
                ).total_seconds()
            except Exception:
                time_to_first_answer_s = None

        return {
            "conversation_id": conversation_id,
            "found": True,
            "started_at": conv.get("conversationStart"),
            "ended_at": conv.get("conversationEnd"),
            "outcome": outcome,
            "timing": {
                "time_in_acd_queue_s": round(time_in_queue_s, 1),
                "time_to_first_answer_s": (
                    round(time_to_first_answer_s, 1)
                    if time_to_first_answer_s is not None else None
                ),
                "transfer_count": outcome["transfer_count"],
            },
            "path": path,
            "queues_visited": queues_visited,
            "diagnostic_notes": [
                "Eligible-agent counts in the 'path' are session-level, "
                "Genesys-provided at routing time (accurate for the moment of "
                "the call). The 'eligibility_now' field on each queue is "
                "current-state for the queue today — useful for recent "
                "failures, less reliable for old investigations.",
                "Per-agent offer/decline log isn't reconstructable from this "
                "view in v0.5; transfer count is surfaced as a proxy.",
            ],
        }

    # ── Aggregate mode (v0.9) — closes the v0.5 backlog ──

    @mcp.tool()
    def routing_diagnostic_aggregate(
        queue_id: str = Field(
            description=(
                "Queue id to analyse. Use list_queues to resolve names → ids."
            ),
        ),
        interval: str = Field(
            description=(
                "ISO-8601 interval 'startISO/endISO' (UTC). Typically a recent "
                "week or day; longer windows make the per-bucket breakdown noisy."
            ),
        ),
        outcome_filter: str = Field(
            default="abandoned",
            description=(
                "Which failure mode to investigate. One of: 'abandoned' "
                "(customer hung up before answer), 'long_wait' (answered but "
                "wait > 30s), 'transferred' (call was transferred). Defaults "
                "to 'abandoned' — usually the highest-priority failure mode."
            ),
        ),
        bucket_size: str = Field(
            default="15min",
            description=(
                "Interval bucket size for the worst-windows breakdown. One of "
                "'15min', '30min', '1h'. Default 15min — matches Genesys's own "
                "intra-day reporting granularity."
            ),
        ),
    ) -> dict:
        """Aggregate routing failure-mode analysis for a queue over an interval.

        Pulls all conversations matching the queue + outcome filter, classifies
        each one's failure mode, and rolls them up to answer questions like:

        - *"Of last week's 500 abandons, how many were because every eligible
          agent was on another interaction?"*
        - *"Which 15-minute windows in the period had the highest abandon
          concentration?"*
        - *"Did the abandoned calls all need the same skill? Was that skill
          under-staffed in those windows?"*

        Closes the v0.5 promise of an aggregate mode for routing_diagnostic.
        Pairs with the v0.7 cc-daily-brief 'worst routes' section — daily
        brief surfaces *which* queues failed; this tool surfaces *why*.

        Returns:

        - **counts_by_failure_mode**: e.g. {"no_eligible_agents": 47,
          "all_eligible_busy": 312, "abandoned_in_ivr": 18, "outside_hours": 5}
        - **worst_buckets**: top 5 time windows by failure count
        - **affected_skills**: skill ids most often requested by failing calls
        - **sample_conversations**: top 10 conversation_ids (caller can drill
          down via routing_diagnostic per-call mode)

        Limitations: failure-mode classification is heuristic — uses the
        session-level eligibleAgentCounts to bucket. A "0 eligible" count at
        the moment of arrival could be either "nobody scheduled" or "every
        scheduled agent on a call". The v0.9 classification doesn't
        distinguish those without WFM joins; v0.10 could refine via
        cross-ref against wfm_schedule data.
        """
        if outcome_filter not in ("abandoned", "long_wait", "transferred"):
            return {
                "error": f"outcome_filter must be 'abandoned', 'long_wait', or "
                         f"'transferred'; got {outcome_filter!r}",
            }
        if bucket_size not in ("15min", "30min", "1h"):
            return {
                "error": f"bucket_size must be '15min', '30min', or '1h'; got "
                         f"{bucket_size!r}",
            }
        bucket_seconds = {"15min": 900, "30min": 1800, "1h": 3600}[bucket_size]

        # Pull every conversation that touched this queue in the interval,
        # then classify the outcome post-hoc in Python (see _matches_outcome
        # below) — the conv-details filter API has no outcome/abandon
        # dimension to filter on directly.
        body = {
            "interval": interval,
            "order": "desc",
            "orderBy": "conversationStart",
            "segmentFilters": [{
                "type": "and",
                "predicates": [
                    {"type": "dimension", "dimension": "queueId",
                     "operator": "matches", "value": queue_id},
                ],
            }],
        }
        convs = _run_conv_details_job(body, max_pages=20)

        # Pre-filter to the requested outcome class so the failure-mode
        # rollup is scoped to the right population. Abandon/transfer detection
        # is shared with _classify_outcome via _has_agent_interact_segment /
        # _count_transfer_segments — both tools now agree on what "answered"
        # and "transferred" mean. had_acd_interact_on_queue / queue_wait_s stay
        # local here since they're queue-scoped (this tool cares about time
        # spent on THIS queue specifically, not the whole conversation).
        def _matches_outcome(conv: dict) -> bool:
            had_acd_interact_on_queue = False
            queue_wait_s = 0.0
            for p in conv.get("participants") or []:
                if p.get("purpose") != "acd":
                    continue
                for s in p.get("sessions") or []:
                    for seg in s.get("segments") or []:
                        if (
                            seg.get("segmentType") == "interact"
                            and seg.get("queueId") == queue_id
                        ):
                            had_acd_interact_on_queue = True
                            queue_wait_s += _seg_dur_s(seg)
            if not had_acd_interact_on_queue:
                # Conversation matched the queue filter via a non-interact
                # segment (e.g. a quick re-queue) but never sat on this queue.
                return False
            had_agent_interact = _has_agent_interact_segment(conv)
            if outcome_filter == "abandoned":
                return not had_agent_interact
            if outcome_filter == "transferred":
                return _count_transfer_segments(conv) > 0
            if outcome_filter == "long_wait":
                return had_agent_interact and queue_wait_s > 30
            return False

        convs = [c for c in convs if _matches_outcome(c)]

        # Classify each conv into a failure mode based on eligibleAgentCounts
        # at arrival + whether it ever reached the queue (IVR-only marker).
        from collections import Counter
        failure_counts: Counter = Counter()
        bucket_counts: dict[str, int] = {}
        skill_counts: Counter = Counter()
        sample_convs: list[dict] = []

        for c in convs:
            conv_id = c.get("conversationId")
            conv_start = c.get("conversationStart") or ""

            # Find the queue session matching the queue_id
            queue_session = None
            for p in c.get("participants") or []:
                if p.get("purpose") != "acd":
                    continue
                for s in p.get("sessions") or []:
                    for seg in s.get("segments") or []:
                        if seg.get("queueId") == queue_id and seg.get("segmentType") == "interact":
                            queue_session = s
                            break
                    if queue_session:
                        break
                if queue_session:
                    break

            # Classify
            failure_mode = "unknown"
            if queue_session is None:
                # Customer never reached the queue — IVR-only path
                failure_mode = "abandoned_in_ivr"
            else:
                eligible_counts = queue_session.get("eligibleAgentCounts") or []
                requested_skills = queue_session.get("activeSkillIds") or []
                # Was anyone eligible at all?
                max_eligible = max(eligible_counts) if eligible_counts else 0
                if max_eligible == 0:
                    failure_mode = "no_eligible_agents"
                else:
                    # Eligible agents existed; presumably all were busy on
                    # other interactions (the conv-details API doesn't give us
                    # a direct "agent was offered and didn't pick up" signal).
                    failure_mode = "all_eligible_busy"
                for sk in requested_skills:
                    skill_counts[sk] += 1

            failure_counts[failure_mode] += 1

            # Bucket the conv start time
            if conv_start:
                from datetime import datetime, timezone as _tz
                try:
                    dt = datetime.fromisoformat(conv_start.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_tz.utc)
                    epoch = int(dt.timestamp())
                    floored = epoch - (epoch % bucket_seconds)
                    bucket_key = datetime.fromtimestamp(
                        floored, tz=_tz.utc
                    ).isoformat().replace("+00:00", "Z")
                    bucket_counts[bucket_key] = bucket_counts.get(bucket_key, 0) + 1
                except Exception:
                    pass

            if len(sample_convs) < 10:
                sample_convs.append({
                    "conversation_id": conv_id,
                    "started_at": conv_start,
                    "failure_mode": failure_mode,
                })

        # Top failing buckets
        worst_buckets = sorted(
            bucket_counts.items(), key=lambda kv: -kv[1]
        )[:5]

        return {
            "queue_id": queue_id,
            "interval": interval,
            "outcome_filter": outcome_filter,
            "bucket_size": bucket_size,
            "total_matching": len(convs),
            "counts_by_failure_mode": dict(failure_counts),
            "worst_buckets": [
                {"interval_start": bk, "count": cnt} for bk, cnt in worst_buckets
            ],
            "affected_skills": [
                {"skill_id": sk, "count": cnt}
                for sk, cnt in skill_counts.most_common(10)
            ],
            "sample_conversations": sample_convs,
            "diagnostic_notes": [
                "Failure-mode classification is heuristic. "
                "'no_eligible_agents' = no agents with the required skill set "
                "were on-queue at any moment of the wait; "
                "'all_eligible_busy' = at least one was on-queue but all were "
                "busy (the most common bucket for high-volume failures); "
                "'abandoned_in_ivr' = customer hung up before reaching the queue.",
                "The classifier doesn't yet cross-ref against wfm_schedule, so "
                "'no_eligible_agents' might mean 'nobody scheduled' OR 'everyone "
                "scheduled was logged out'. v0.10 candidate.",
            ],
        }
