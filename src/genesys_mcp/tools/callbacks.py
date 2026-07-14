"""Callback outcome analysis: the customer-first callback funnel.

Why this tool exists: with customer-first callbacks (the system dials the
customer, detects a live answer, then bridges an agent) the callback media
row in conversation *aggregates* is structurally empty — the callback ACD
session ends the instant dial-out starts, so tAnswered/tAbandon never
populate and nConnected is meaningless. The actual outcome (customer
reached, agent bridged, talk time) is recorded on outbound voice sessions
and a re-entry voice ACD leg. The only reliable way to measure callbacks is
to classify each conversation's detail record, which is what this tool does.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._intervals import INTERVAL_HELP_STRING
from genesys_mcp._intervals import default_interval as _default_interval
from genesys_mcp._intervals import now_utc as _now_utc
from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
# Safety cap: 20 pages = 2,000 callback conversations per call. A day/week
# window on a normal tenant is a few hundred at most; if the cap is hit the
# response says so via `truncated` rather than silently under-reporting.
_MAX_PAGES = 20

OUTCOMES = (
    "answered_and_bridged",
    "answered_not_bridged",
    "dialed_not_answered",
    "never_dialed",
)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_conversation(conv: dict, queue_ids: set[str] | None = None) -> dict | None:
    """Classify one conversation's customer-first callback outcome.

    Returns None when the conversation has no callback ACD segment on one of
    the requested queues (the details-query segment filters can over-match:
    each filter only needs to match *some* segment, not the same one).

    Outcomes:
      answered_and_bridged  — customer answered the dial-out and an agent
                              interact (a session carrying tTalk) followed it
      answered_not_bridged  — customer answered but hung up / dropped before
                              any agent talk
      dialed_not_answered   — dial-out placed but the customer never reached
                              an interact segment (no answer / failed detect)
      never_dialed          — callback created but no outbound dial found
                              (expired, cancelled, or still pending)
    """
    callback_acd_starts: list[datetime] = []
    callback_queue: str | None = None
    dial_sessions: list[dict[str, list[dict]]] = []
    agent_talk_starts: list[datetime] = []

    for part in conv.get("participants") or []:
        purpose = part.get("purpose")
        for sess in part.get("sessions") or []:
            media = sess.get("mediaType")
            direction = sess.get("direction")
            segments = sess.get("segments") or []
            metric_names = {m.get("name") for m in (sess.get("metrics") or [])}

            if purpose == "acd" and media == "callback":
                for seg in segments:
                    qid = seg.get("queueId")
                    if queue_ids is not None and qid not in queue_ids:
                        continue
                    start = _parse_ts(seg.get("segmentStart"))
                    if start:
                        callback_acd_starts.append(start)
                    if callback_queue is None:
                        callback_queue = qid
            elif purpose == "customer" and media == "voice" and direction == "outbound":
                dial_sessions.append({
                    "dialing": [s for s in segments if s.get("segmentType") == "dialing"],
                    "interact": [s for s in segments if s.get("segmentType") == "interact"],
                })
            elif purpose == "agent" and media == "voice" and "tTalk" in metric_names:
                for seg in segments:
                    if seg.get("segmentType") == "interact":
                        start = _parse_ts(seg.get("segmentStart"))
                        if start:
                            agent_talk_starts.append(start)

    if not callback_acd_starts:
        return None

    callback_start = min(callback_acd_starts)
    dial_attempts = sum(len(d["dialing"]) for d in dial_sessions)
    dial_starts = [
        ts for d in dial_sessions for s in d["dialing"]
        if (ts := _parse_ts(s.get("segmentStart"))) is not None
    ]
    first_dial = min(dial_starts) if dial_starts else None
    customer_answered = any(d["interact"] for d in dial_sessions)
    # Only agent talk that begins at/after the dial-out counts as a bridged
    # callback — an agent leg from the original inbound call (e.g. an agent
    # who scheduled the callback mid-conversation) must not count.
    bridged = (
        any(ts >= first_dial for ts in agent_talk_starts) if first_dial else False
    )

    if not dial_sessions:
        outcome = "never_dialed"
    elif not customer_answered:
        outcome = "dialed_not_answered"
    elif bridged:
        outcome = "answered_and_bridged"
    else:
        outcome = "answered_not_bridged"

    wait_to_dial_s = (
        round((first_dial - callback_start).total_seconds(), 1)
        if first_dial and first_dial >= callback_start
        else None
    )
    return {
        "conversation_id": conv.get("conversationId"),
        "queue_id": callback_queue,
        "outcome": outcome,
        "dial_attempts": dial_attempts,
        "wait_to_dial_s": wait_to_dial_s,
    }


def summarise_outcomes(rows: list[dict]) -> dict[str, Any]:
    """Reduce classified rows into per-queue funnels plus a total funnel."""

    def _funnel(subset: list[dict]) -> dict[str, Any]:
        counts = Counter(row["outcome"] for row in subset)
        scheduled = len(subset)
        reached = counts["answered_and_bridged"] + counts["answered_not_bridged"]
        bridged = counts["answered_and_bridged"]
        waits = [r["wait_to_dial_s"] for r in subset if r["wait_to_dial_s"] is not None]
        attempts = Counter(r["dial_attempts"] for r in subset)

        def pct(n: int) -> float | None:
            return round(n / scheduled * 100, 1) if scheduled else None

        examples: dict[str, list[str]] = {}
        for row in subset:
            ids = examples.setdefault(row["outcome"], [])
            if len(ids) < 3 and row["conversation_id"]:
                ids.append(row["conversation_id"])

        return {
            "callbacks_scheduled": scheduled,
            "customer_reached": reached,
            "customer_reached_pct": pct(reached),
            "bridged_to_agent": bridged,
            "bridged_to_agent_pct": pct(bridged),
            "outcomes": {o: counts.get(o, 0) for o in OUTCOMES},
            "avg_wait_to_dial_s": round(sum(waits) / len(waits), 1) if waits else None,
            "dial_attempts_histogram": {str(k): v for k, v in sorted(attempts.items())},
            "example_conversation_ids": examples,
        }

    by_queue: dict[str, list[dict]] = {}
    for row in rows:
        by_queue.setdefault(row["queue_id"] or "unknown", []).append(row)

    return {
        "totals": _funnel(rows),
        "queues": {qid: _funnel(subset) for qid, subset in sorted(by_queue.items())},
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def callback_outcomes(
        queue_ids: list[str] = Field(
            description="Queue ids to analyse (required). Use list_queues to resolve names.",
        ),
        interval: str | None = Field(
            default=None,
            description=INTERVAL_HELP_STRING,
        ),
    ) -> dict:
        """True outcome funnel for customer-first callbacks, per queue.

        Aggregate metrics CANNOT measure customer-first callbacks: the callback
        media row only ever gets nOffered (scheduled) and tWait (time until
        dial-out) — the dial result and agent talk are booked on voice sessions.
        This tool classifies each callback conversation's detail record instead
        and returns, per queue and in total:

          - callbacks_scheduled     — callback ACD sessions created
          - customer_reached (+pct) — dial-out answered by the customer
          - bridged_to_agent (+pct) — customer answered AND an agent talked
          - outcomes                — full split: answered_and_bridged /
                                      answered_not_bridged (customer answered,
                                      dropped before an agent joined) /
                                      dialed_not_answered / never_dialed
          - avg_wait_to_dial_s      — callback creation to first dial attempt
          - dial_attempts_histogram — retries needed per callback
          - example_conversation_ids — up to 3 per outcome, for get_conversation

        Caveats: "customer answered" relies on Genesys answer detection, so
        voicemail pickups can count as answered. Bridged callbacks also appear
        in the queue's VOICE aggregates (an extra offered+answered interaction
        with near-zero speed of answer) — do not double-count callbacks when
        reading voice rows alongside this tool. never_dialed includes callbacks
        still waiting to be processed at query time.

        Scans up to 2,000 callback conversations per call; `truncated: true`
        signals the interval had more (narrow the window and re-query).
        """
        api = gc.AnalyticsApi(get_api())
        window = interval or _default_interval(7)
        wanted = set(queue_ids)

        rows: list[dict] = []
        total_hits: int | None = None
        scanned = 0
        truncated = False
        for page in range(1, _MAX_PAGES + 1):
            body: dict[str, Any] = {
                "interval": window,
                "order": "asc",
                "orderBy": "conversationStart",
                "paging": {"pageSize": _PAGE_SIZE, "pageNumber": page},
                # Two segment filters: each must match SOME segment of the
                # conversation (not necessarily the same one) — the queueId
                # over-match this allows is corrected in classify_conversation,
                # which only accepts callback ACD segments on requested queues.
                "segmentFilters": [
                    {"type": "and", "predicates": [
                        {"type": "dimension", "dimension": "mediaType", "operator": "matches", "value": "callback"},
                    ]},
                    {"type": "or", "predicates": [
                        {"type": "dimension", "dimension": "queueId", "operator": "matches", "value": qid}
                        for qid in queue_ids
                    ]},
                ],
            }
            resp = to_dict(with_retry(api.post_analytics_conversations_details_query)(body)) or {}
            if total_hits is None:
                total_hits = resp.get("totalHits")
            conversations = resp.get("conversations") or []
            scanned += len(conversations)
            for conv in conversations:
                row = classify_conversation(conv, wanted)
                if row is not None:
                    rows.append(row)
            if len(conversations) < _PAGE_SIZE:
                break
        else:
            truncated = True

        out = summarise_outcomes(rows)
        out["interval"] = window
        out["conversations_scanned"] = scanned
        out["total_hits"] = total_hits
        out["truncated"] = truncated
        out["caveats"] = [
            "customer_reached relies on live-answer detection; voicemail pickups can count as answered.",
            "Bridged callbacks also appear in the queue's voice aggregates (extra offered+answered, ~0s ASA).",
            "never_dialed includes callbacks still queued for dial-out at query time.",
        ]
        out["as_of_utc"] = _now_utc().isoformat().replace("+00:00", "Z")
        return out
