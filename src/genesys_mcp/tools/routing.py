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
from genesys_mcp.tools.reports import _parse_iso, _seg_dur_s

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


def _fetch_queue_members(queue_id: str, max_pages: int = 5) -> list[dict]:
    """Pull all members of a queue with their joined/active state."""
    api = gc.RoutingApi(get_api())
    out: list[dict] = []
    for pg in range(1, max_pages + 1):
        resp = to_dict(
            with_retry(api.get_routing_queue_members)(
                queue_id=queue_id, page_size=200, page_number=pg, joined=True,
                expand=["routingStatus", "skills"],
            )
        )
        entities = resp.get("entities") or []
        out.extend(entities)
        if len(entities) < 200:
            break
    return out


def _classify_outcome(conv: dict) -> dict[str, Any]:
    """Reduce the analytics conversation shape into a clear outcome verdict.

    Looks at participants → sessions → segments. ``segmentType=='interact'``
    on an ``agent``-purpose participant means the call was answered;
    ``flaggedReason`` or ``disconnectType=='client'`` on a customer session
    pre-answer means abandoned.
    """
    outcome = {
        "verdict": "unknown",
        "explanation": "",
        "first_answer_at": None,
        "abandoned_at": None,
        "abandon_reason": None,
        "transfer_count": 0,
    }
    answered = False
    abandoned = False
    transfers = 0
    first_answer_at: str | None = None
    abandon_at: str | None = None
    abandon_reason: str | None = None

    for p in conv.get("participants") or []:
        purpose = p.get("purpose")
        for s in p.get("sessions") or []:
            flagged = s.get("flaggedReason")
            for seg in s.get("segments") or []:
                st = seg.get("segmentType")
                # Answered: agent-purpose participant has an 'interact' segment.
                if st == "interact" and purpose == "agent":
                    if not first_answer_at:
                        first_answer_at = seg.get("segmentStart")
                        answered = True
                # Transfer: explicit transfer-segments on any session.
                if st in ("transfer", "ininternaltransfer"):
                    transfers += 1
            # Abandon: customer session disconnect-without-answer.
            if (
                purpose == "customer"
                and flagged in ("abandon", "short", "transferred")
            ):
                abandon_at = s.get("segmentEnd")
                abandon_reason = flagged
                if flagged == "abandon":
                    abandoned = True

    outcome["first_answer_at"] = first_answer_at
    outcome["abandoned_at"] = abandon_at
    outcome["abandon_reason"] = abandon_reason
    outcome["transfer_count"] = transfers

    if answered:
        outcome["verdict"] = "answered"
        outcome["explanation"] = "Call connected to an agent and was answered."
    elif abandoned:
        outcome["verdict"] = "abandoned"
        outcome["explanation"] = (
            f"Customer disconnected before answer (flag: {abandon_reason})."
        )
    else:
        outcome["verdict"] = "other"
        outcome["explanation"] = (
            "No agent answer and no customer-abandon flag detected — check "
            "transfer/timeout/voicemail segments."
        )
    if transfers > 1:
        outcome["explanation"] += f" Call was transferred {transfers} times."
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
                elif st in ("transfer", "ininternaltransfer"):
                    events.append({
                        "kind": "transfer",
                        "started_at": seg.get("segmentStart"),
                        "ended_at": seg.get("segmentEnd"),
                        "duration_s": round(_seg_dur_s(seg), 1),
                        "queue_id": seg.get("queueId"),
                    })
                elif st in ("hold",):
                    events.append({
                        "kind": "hold",
                        "started_at": seg.get("segmentStart"),
                        "ended_at": seg.get("segmentEnd"),
                        "duration_s": round(_seg_dur_s(seg), 1),
                    })
    events.sort(key=lambda e: e.get("started_at") or "")
    return events


def _eligible_member_count(queue: dict, members: list[dict]) -> dict[str, Any]:
    """How many current queue members have all the queue's required skills?

    'Current' state — not historical. We can't easily reconstruct agent skills
    at the moment of the call, so we report the current eligibility as a proxy.
    For very old calls this is less informative; for recent failures it answers
    "is the queue under-staffed for the skill set we require?"
    """
    required_skill_ids: set[str] = set()
    for sgrp in queue.get("memberGroups") or []:
        # memberGroups is rarely populated in the way we want; use skillGroups.
        for sk in sgrp.get("skills") or []:
            if sk.get("id"):
                required_skill_ids.add(sk["id"])
    # Some queues encode skill requirements via skillEvaluationMethod + skill list
    # on conditional routing — pull at top level if present.
    for sk in queue.get("skills") or []:
        if isinstance(sk, dict) and sk.get("id"):
            required_skill_ids.add(sk["id"])

    eligible: list[dict] = []
    on_queue_eligible: list[dict] = []
    for m in members:
        user = m.get("user") or {}
        m_skills = {s.get("id") for s in (m.get("skills") or []) if s.get("id")}
        if not required_skill_ids or required_skill_ids.issubset(m_skills):
            eligible.append({
                "user_id": user.get("id"),
                "user_name": user.get("name"),
                "routing_status": (m.get("routingStatus") or {}).get("status"),
            })
            if (m.get("routingStatus") or {}).get("status") == "IDLE":
                on_queue_eligible.append(eligible[-1])
    return {
        "required_skill_ids": sorted(required_skill_ids),
        "total_members": len(members),
        "eligible_now": len(eligible),
        "idle_now": len(on_queue_eligible),
        "sample_eligible_idle": on_queue_eligible[:5],
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

        - **outcome**: answered / abandoned (+ reason) / other, with explanation
        - **path**: chronological IVR → queue → agent path with durations
        - **queues_visited**: each unique queue with its routing config (skills
          required, routing method, ACW timeout), plus current eligible-agent
          counts (members with all required skills, broken down by IDLE state)
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
            eligibility = _eligible_member_count(q, members)
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
