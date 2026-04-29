"""Conversation / interaction tools: search, detail, recordings metadata."""

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
        """Full detail on a single conversation: all participants, segments, attributes."""
        api = gc.ConversationsApi(get_api())
        resp = with_retry(api.get_conversation)(conversation_id)
        return to_dict(resp)

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
