"""Speech & text analytics tools — conversation summaries, sentiment, and
transcript URLs for quality reviews.

Requires the OAuth client to have ``speech-and-text-analytics:readonly``.

All three tools soft-fail on 404 (returning ``{"status": 404, ...}``) so they
can be used safely in batch loops over conversation lists, where some calls
genuinely don't have analytics data (short calls, pre-STA conversations,
non-recorded interactions).
"""

from __future__ import annotations

import logging

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


def _soft_404(exc: Exception, conversation_id: str, kind: str) -> dict | None:
    """Return a 404 envelope if the exception is HTTP 404, else None (re-raise)."""
    status = getattr(exc, "status", None)
    if status == 404:
        return {"status": 404, "conversation_id": conversation_id, "message": f"{kind} not found"}
    return None


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_conversation_summary(
        conversation_id: str = Field(description="Conversation id."),
    ) -> dict:
        """AI-generated summary for a single conversation: topics, key issues, resolution.

        This is the same auto-summary that ends up in wrap-up notes — but as
        structured data with topic/issue breakdown rather than free-text. Useful
        for filtering on calls that mention a specific topic across an agent's
        history.

        Returns the listing as Genesys returns it (most conversations have one
        summary; multi-channel conversations may have one per communication).
        Soft-fails on 404 (no summary available — common for short or non-recorded calls).
        """
        api = gc.SpeechTextAnalyticsApi(get_api())
        try:
            resp = with_retry(api.get_speechandtextanalytics_conversation_summaries)(
                conversation_id=conversation_id
            )
            return to_dict(resp)
        except Exception as exc:
            envelope = _soft_404(exc, conversation_id, "summary")
            if envelope is not None:
                return envelope
            raise

    @mcp.tool()
    def get_conversation_sentiment(
        conversation_id: str = Field(description="Conversation id."),
    ) -> dict:
        """Per-conversation sentiment data: overall score and per-phrase timeline.

        Use for QA — flags calls where the customer's sentiment trended sharply
        negative (escalation risk) or positive (good customer outcomes worth
        learning from). Soft-fails on 404.
        """
        api = gc.SpeechTextAnalyticsApi(get_api())
        try:
            resp = with_retry(api.get_speechandtextanalytics_conversation_sentiments)(
                conversation_id=conversation_id
            )
            return to_dict(resp)
        except Exception as exc:
            envelope = _soft_404(exc, conversation_id, "sentiment")
            if envelope is not None:
                return envelope
            raise

    @mcp.tool()
    def get_transcript_url(
        conversation_id: str = Field(description="Conversation id."),
        communication_id: str = Field(
            description="Communication id (= sessionId of the recorded leg). "
            "If unknown, call get_conversation first to find communication ids.",
        ),
    ) -> dict:
        """Signed URL to a conversation transcript JSON.

        The transcript itself is hosted on Genesys S3; the URL is short-lived
        (~1h). Used to verify the silent-transcript flags from
        agent_quality_snapshot — pull the actual transcript and confirm whether
        the AI summary's 'undefined' was real silence or a transcription gap.
        Soft-fails on 404.
        """
        api = gc.SpeechTextAnalyticsApi(get_api())
        try:
            resp = with_retry(api.get_speechandtextanalytics_conversation_communication_transcripturl)(
                conversation_id=conversation_id,
                communication_id=communication_id,
            )
            return to_dict(resp)
        except Exception as exc:
            envelope = _soft_404(exc, conversation_id, "transcript url")
            if envelope is not None:
                return envelope
            raise
