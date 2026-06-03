"""Conversation / interaction tools: search, detail, recordings metadata."""

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
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    return f"{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}/{end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def search_conversations(
        interval: str | None = Field(
            default=None,
            description="ISO-8601 interval 'start/end' (UTC). Defaults to last 7 days.",
        ),
        ani: str | None = Field(
            default=None,
            description="Caller ANI (phone number). Accepts '+61412345678' or '0412345678'.",
        ),
        queue_id: str | None = Field(default=None, description="Restrict to a single queue."),
        user_id: str | None = Field(default=None, description="Restrict to an agent."),
        direction: str | None = Field(
            default=None, description="'inbound' or 'outbound'."
        ),
        page_size: int = Field(default=25, ge=1, le=100),
        page_number: int = Field(default=1, ge=1),
    ) -> dict:
        """Search conversations by phone number, queue, agent, direction, and/or time window.

        Returns conversation summaries (id, start/end, participants, queue). Use get_conversation
        on an id for full detail.
        """
        predicates: list[dict[str, Any]] = []
        if ani:
            predicates.append({"dimension": "ani", "value": ani})
        if queue_id:
            predicates.append({"dimension": "queueId", "value": queue_id})
        if user_id:
            predicates.append({"dimension": "userId", "value": user_id})
        if direction:
            predicates.append({"dimension": "direction", "value": direction})

        body: dict[str, Any] = {
            "interval": interval or _default_interval(7),
            "order": "desc",
            "orderBy": "conversationStart",
            "paging": {"pageSize": page_size, "pageNumber": page_number},
        }
        if predicates:
            body["conversationFilters"] = [{"type": "and", "predicates": predicates}]

        api = gc.AnalyticsApi(get_api())
        resp = with_retry(api.post_analytics_conversations_details_query)(body)
        return to_dict(resp)

    @mcp.tool()
    def get_conversation(
        conversation_id: str = Field(description="Conversation id from search_conversations."),
    ) -> dict:
        """Full detail on a single conversation: all participants, segments, attributes.

        v1.3+: soft-fails on 404 with the canonical envelope. Callers iterating
        over conversation lists (deleted convs, privacy-filtered convs, retention-
        expired records) can ``if r.get('status') == 404: continue`` rather than
        wrapping each call in a try/except.
        """
        api = gc.ConversationsApi(get_api())
        try:
            resp = with_retry(api.get_conversation)(conversation_id)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return soft_fail_envelope(
                    kind="conversation",
                    message="conversation not found (deleted, privacy-filtered, or retention-expired)",
                    conversation_id=conversation_id,
                )
            raise
        return to_dict(resp)

    @mcp.tool()
    def voice_call_quality(
        conversation_ids: list[str] = Field(
            description=(
                "Up to 100 conversation ids to score. Voice-only — non-voice "
                "convs return ``no_voice_segments: true`` rather than an error."
            ),
        ),
        low_mos_threshold: float = Field(
            default=3.5, ge=0.0, le=5.0,
            description=(
                "MOS value below which a segment is counted as 'poor'. Default "
                "3.5 matches industry convention (below 3.5 = noticeably bad)."
            ),
        ),
    ) -> dict:
        """Per-conversation MOS (Mean Opinion Score) for voice-call quality triage.

        v1.4+. Was a gap surfaced by the MakingChatbots-MCP comparison —
        MOS is the *"was it the network or the agent?"* signal. Calls with
        a min MOS < 3 are nearly always network-impacted (jitter, packet
        loss, codec issues) rather than agent-skill issues, so a coaching
        brief that includes a poor-MOS call should flag the network angle
        before suggesting agent coaching.

        Reads ``mediaEndpointStats[].min_mos`` from the analytics
        conversation-detail endpoint. Each session's media stream emits
        a stat row per polling interval (typically every ~30s during the
        call); the per-session minimum is the worst window observed.

        Returns per-conversation:

        - ``min_mos`` — worst MOS across all sessions' media stats
        - ``avg_mos`` — mean of all per-stat minimums
        - ``segments_with_low_mos`` — count of stat rows below the threshold
        - ``quality_label`` — ``good`` (≥4.0), ``fair`` (3.0-4.0), ``poor`` (<3.0)

        Soft-fails on 404 (deleted / privacy-filtered / retention-expired
        conversations) using the canonical envelope. Non-voice conversations
        return ``{conversation_id, no_voice_segments: true}``.

        Endpoint: ``GET /api/v2/analytics/conversations/{id}/details``.
        Needs ``analytics:conversationDetail:view``.
        """
        if not conversation_ids:
            raise ValueError("conversation_ids must contain at least one id.")
        if len(conversation_ids) > 100:
            raise ValueError(
                "voice_call_quality accepts up to 100 ids per call; got "
                f"{len(conversation_ids)}. Split the batch."
            )

        api = gc.AnalyticsApi(get_api())
        out: list[dict] = []
        for cid in conversation_ids:
            try:
                resp = with_retry(api.get_analytics_conversation_details)(
                    conversation_id=cid,
                )
            except Exception as exc:
                if getattr(exc, "status", None) == 404:
                    out.append(soft_fail_envelope(
                        kind="conversation",
                        message="conversation not found (deleted, privacy-filtered, or retention-expired)",
                        conversation_id=cid,
                    ))
                    continue
                raise

            conv = to_dict(resp) or {}
            mos_values: list[float] = []
            low_segments = 0
            # mediaEndpointStats lives at participants[].sessions[].mediaEndpointStats[]
            for p in (conv.get("participants") or []):
                for s in (p.get("sessions") or []):
                    if (s.get("mediaType") or "").lower() != "voice":
                        continue
                    for stat in (s.get("mediaEndpointStats") or []):
                        mos = stat.get("minMos") or stat.get("min_mos")
                        if mos is None:
                            continue
                        mos_values.append(float(mos))
                        if mos < low_mos_threshold:
                            low_segments += 1

            if not mos_values:
                out.append({
                    "conversation_id": cid,
                    "no_voice_segments": True,
                })
                continue

            min_mos = min(mos_values)
            avg_mos = sum(mos_values) / len(mos_values)
            label = ("good" if min_mos >= 4.0
                     else "fair" if min_mos >= 3.0
                     else "poor")
            out.append({
                "conversation_id": cid,
                "min_mos": round(min_mos, 2),
                "avg_mos": round(avg_mos, 2),
                "segments_evaluated": len(mos_values),
                "segments_with_low_mos": low_segments,
                "quality_label": label,
            })

        return {
            "low_mos_threshold": low_mos_threshold,
            "results": out,
        }

    @mcp.tool()
    def list_recordings(
        conversation_id: str = Field(description="Conversation id to list recordings for."),
    ) -> dict:
        """Recording *metadata* only (no media). The 'region' field confirms data residency.

        Returns an array of recording records; each has ``region`` which should match
        your tenant's home region (e.g. 'ap-southeast-2' if your tenant is in Sydney).
        A different region means the recording was stored outside the expected jurisdiction
        and may warrant a compliance check.
        """
        api = gc.RecordingApi(get_api())
        resp = with_retry(api.get_conversation_recordings)(conversation_id=conversation_id)
        return {"recordings": to_dict(resp)}

    @mcp.tool()
    def get_recording_url(
        conversation_id: str = Field(description="Conversation id."),
        recording_id: str = Field(
            description="Recording id from list_recordings.",
        ),
        format_id: str = Field(
            default="WAV",
            description="Audio container: WAV (default), MP3, OGG_VORBIS, OGG_OPUS, NONE.",
        ),
    ) -> dict:
        """Signed URL to download a single recording's media.

        Returns ``media_uri`` (a signed S3 URL valid for ~1h), ``region``, and the
        recording metadata. Use this when the user wants to actually listen to a
        flagged call (e.g., the 'silent transcript' calls in an agent quality review).

        Caveats:
        - The URI may not be ready immediately for very recent calls — Genesys
          processes the recording asynchronously. If ``media_uri`` is null, retry
          in a few seconds.
        - The ``region`` field reports where the media is stored. If it doesn't match
          your tenant's home region, the recording was stored outside the expected
          jurisdiction and may warrant a compliance check.
        """
        api = gc.RecordingApi(get_api())
        resp = with_retry(api.get_conversation_recording)(
            conversation_id=conversation_id,
            recording_id=recording_id,
            format_id=format_id,
        )
        data = to_dict(resp) or {}
        media_uris = data.get("mediaUris") or {}
        # Pick the first available signed URL across the format keys
        primary_uri = None
        primary_format = None
        for fmt, info in media_uris.items():
            if isinstance(info, dict) and info.get("mediaUri"):
                primary_uri = info["mediaUri"]
                primary_format = fmt
                break
        return {
            "recording_id": recording_id,
            "conversation_id": conversation_id,
            "region": data.get("region"),
            "format": primary_format or format_id,
            "media_uri": primary_uri,
            "media_uris_by_format": media_uris,
            "duration_ms": data.get("duration"),
            "start_time": data.get("startTime"),
            "end_time": data.get("endTime"),
        }
