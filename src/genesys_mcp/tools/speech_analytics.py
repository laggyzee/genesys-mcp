"""Speech & text analytics tools — conversation summaries, sentiment,
transcript URLs, and full parsed transcripts for quality reviews + coaching.

Requires the OAuth client to have ``speech-and-text-analytics:readonly``.
The transcript tool also needs ``recording:recording:view`` (to resolve a
conversation id → recording session ids before fetching the transcript URL).

All tools soft-fail on 404 (returning ``{"status": 404, ...}``) so they can
be used safely in batch loops over conversation lists, where some calls
genuinely don't have analytics data (short calls, pre-STA conversations,
non-recorded interactions).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._envelopes import soft_fail_envelope
from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


def _soft_404(exc: Exception, conversation_id: str, kind: str) -> dict | None:
    """Return the canonical 404 envelope if the exception is HTTP 404, else None.

    v1.3+: uses ``soft_fail_envelope`` so the shape matches every other
    soft-fail across the codebase.
    """
    status = getattr(exc, "status", None)
    if status == 404:
        return soft_fail_envelope(
            kind=kind,
            message=f"{kind} not found",
            conversation_id=conversation_id,
        )
    return None


# ─────────────────────── transcript parsing helpers ───────────────────────
#
# The Genesys transcript JSON lives at a signed S3 URL — schema documented at
# https://developer.genesys.cloud/analyticsdatamanagement/speechtextanalytics/transcript-url
#
# Shape (relevant subset):
#   {
#     "conversationId": ..., "communicationId": ..., "mediaType": "voice",
#     "conversationStartTime": <epoch_ms>,
#     "transcripts": [{
#       "phrases": [{
#         "text": "I'm calling about...",
#         "decoratedText": "...",                 # punctuated/cased version
#         "startTimeMs": 4500,
#         "participantPurpose": "external"        # or "internal"
#       }],
#       "analytics": {
#         "sentiment": [{"phraseIndex": 0, "sentiment": -1, ...}],
#       }
#     }],
#     "participants": [{
#       "participantPurpose": "agent" | "customer" | "ivr" | "acd" | ...,
#       "userId": ..., "startTimeMs": ..., "endTimeMs": ...
#     }]
#   }

_SPEAKER_LABELS = {
    "external": "customer",
    "customer": "customer",
    "internal": "agent",
    "agent": "agent",
    "user": "agent",
    "acd": "acd",
    "ivr": "ivr",
    "voicemail": "voicemail",
    "fax": "fax",
}


def _normalise_speaker(participant_purpose: str | None) -> str:
    """Map a Genesys participantPurpose to a normalised speaker label."""
    if not participant_purpose:
        return "unknown"
    return _SPEAKER_LABELS.get(participant_purpose.lower(), participant_purpose.lower())


def _sentiment_label(score: float | None) -> str | None:
    """Translate Genesys sentiment (-1/0/+1) into a human label."""
    if score is None:
        return None
    if score >= 0.5:
        return "positive"
    if score <= -0.5:
        return "negative"
    return "neutral"


def _build_utterance_list(transcript_json: dict) -> list[dict]:
    """Flatten Genesys's transcript JSON into a list of utterance dicts.

    Each utterance: {speaker, start_s, text, sentiment?, sentiment_label?}.
    Per-phrase sentiment comes from analytics.sentiment[].phraseIndex match.
    """
    utterances: list[dict] = []
    for transcript in transcript_json.get("transcripts") or []:
        # Build a phraseIndex → sentiment lookup once per transcript
        sentiment_by_phrase: dict[int, float] = {}
        analytics = transcript.get("analytics") or {}
        for s in analytics.get("sentiment") or []:
            idx = s.get("phraseIndex")
            if idx is not None and "sentiment" in s:
                sentiment_by_phrase[idx] = s["sentiment"]

        phrases = transcript.get("phrases") or []
        for i, phrase in enumerate(phrases):
            text = phrase.get("decoratedText") or phrase.get("text")
            if not text:
                continue
            start_ms = phrase.get("startTimeMs")
            sentiment = sentiment_by_phrase.get(i)
            speaker = _normalise_speaker(phrase.get("participantPurpose"))
            row = {
                "speaker": speaker,
                "start_s": round(start_ms / 1000, 2) if start_ms is not None else None,
                "text": text.strip(),
            }
            if sentiment is not None:
                row["sentiment"] = sentiment
                row["sentiment_label"] = _sentiment_label(sentiment)
            utterances.append(row)
    return utterances


def _summarise_participants(transcript_json: dict) -> dict[str, str]:
    """Extract a friendly {role: identifier} map from the participants list."""
    out: dict[str, str] = {}
    for p in transcript_json.get("participants") or []:
        role = _normalise_speaker(p.get("participantPurpose"))
        identifier = p.get("userId") or p.get("ani") or p.get("dnis")
        if identifier and role not in out:
            out[role] = identifier
    return out


def _strip_utterance_sentiment(utterances: list[dict]) -> list[dict]:
    """Return utterances with per-phrase sentiment fields removed."""
    out: list[dict] = []
    for u in utterances:
        out.append({k: v for k, v in u.items() if k not in ("sentiment", "sentiment_label")})
    return out


def _fetch_transcript_json(url: str) -> dict:
    """GET the transcript JSON from a signed Genesys S3 URL."""
    resp = httpx.get(url, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def fetch_conversation_transcript(
    conversation_id: str,
    *,
    mode: str = "summary",
    max_utterances: int = 200,
) -> dict:
    """Module-level transcript fetcher — shared between the MCP tool and other tools.

    The MCP tool wrapper (``get_conversation_transcript``) is a thin shell
    over this helper; ``agent_coaching_pack`` also calls this to attach
    transcript excerpts to its flagged-call section in v1.2.

    Returns the same shape as the MCP tool. Doesn't raise on missing
    recordings or 404s — returns ``{status: 404, ...}`` instead so callers
    iterating over many conversations can `.get('utterances', [])` safely.
    """
    if mode not in ("summary", "full"):
        raise ValueError(
            f"mode must be 'summary' or 'full', got {mode!r}"
        )

    recording_api = gc.RecordingApi(get_api())
    try:
        recordings_resp = with_retry(recording_api.get_conversation_recordings)(
            conversation_id=conversation_id,
        )
    except Exception as exc:
        envelope = _soft_404(exc, conversation_id, "recordings")
        if envelope is not None:
            return envelope
        raise

    recordings = to_dict(recordings_resp) or []
    if isinstance(recordings, dict):
        recordings = recordings.get("entities") or [recordings]
    session_ids = [r.get("sessionId") for r in recordings if r.get("sessionId")]
    if not session_ids:
        return soft_fail_envelope(
            kind="recordings",
            message="no recording sessions found for conversation",
            conversation_id=conversation_id,
        )

    sta_api = gc.SpeechTextAnalyticsApi(get_api())
    all_utterances: list[dict] = []
    participants_seen: dict[str, str] = {}
    media_type: str | None = None
    total_duration_s: float | None = None
    sessions_processed = 0
    sessions_no_transcript = 0

    for session_id in session_ids:
        try:
            url_resp = with_retry(
                sta_api.get_speechandtextanalytics_conversation_communication_transcripturl
            )(
                conversation_id=conversation_id,
                communication_id=session_id,
            )
        except Exception as exc:
            if _soft_404(exc, conversation_id, "transcript url") is not None:
                sessions_no_transcript += 1
                continue
            raise
        url_dict = to_dict(url_resp) or {}
        transcript_url = url_dict.get("url") or url_dict.get("uri")
        if not transcript_url:
            sessions_no_transcript += 1
            continue

        try:
            tj = _fetch_transcript_json(transcript_url)
        except httpx.HTTPError as exc:
            logger.warning(
                "transcript fetch failed for conv=%s session=%s: %s",
                conversation_id, session_id, exc,
            )
            sessions_no_transcript += 1
            continue

        sessions_processed += 1
        media_type = media_type or tj.get("mediaType")
        duration_raw = tj.get("duration")
        if isinstance(duration_raw, (int, float)) and total_duration_s is None:
            total_duration_s = round(float(duration_raw) / 1000, 1)

        for role, ident in _summarise_participants(tj).items():
            participants_seen.setdefault(role, ident)

        all_utterances.extend(_build_utterance_list(tj))

    all_utterances.sort(key=lambda u: (u.get("start_s") is None, u.get("start_s") or 0))

    total = len(all_utterances)
    truncated_at: int | None = None
    dropped = 0
    if total > max_utterances:
        truncated_at = max_utterances
        dropped = total - max_utterances
        all_utterances = all_utterances[:max_utterances]

    if mode == "summary":
        all_utterances = _strip_utterance_sentiment(all_utterances)

    return {
        "conversation_id": conversation_id,
        "media_type": media_type,
        "duration_s": total_duration_s,
        "participants": participants_seen,
        "sessions_processed": sessions_processed,
        "sessions_no_transcript": sessions_no_transcript,
        "total_utterances": total,
        "truncated_at": truncated_at,
        "total_utterances_dropped": dropped,
        "utterances": all_utterances,
    }


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
    def get_conversation_transcript(
        conversation_id: str = Field(description="Conversation id."),
        mode: str = Field(
            default="summary",
            description=(
                "Response shape. 'summary' (default, v1.2+) returns utterances "
                "with {speaker, start_s, text} only — optimised for chat "
                "context. 'full' adds per-utterance sentiment ({-1, 0, +1}) "
                "and a friendly sentiment_label (negative/neutral/positive). "
                "Use 'full' when sentiment progression matters (coaching, "
                "escalation diagnosis); the default is enough for reading "
                "what was said."
            ),
        ),
        max_utterances: int = Field(
            default=200, ge=10, le=2000,
            description=(
                "Cap on returned utterances. A 30-minute voice call typically "
                "has ~150-200 utterances; messages average 10-40. When the "
                "raw transcript exceeds this cap, the response includes "
                "`truncated_at: N` and the dropped tail count under "
                "`total_utterances_dropped`. Raise for full-call deep dives, "
                "lower for chat-context-tight scans across many calls."
            ),
        ),
    ) -> dict:
        """Structured, time-aligned transcript for a conversation.

        Resolves the conversation id → recording session ids via
        ``recording:recording:view``, then for each session pulls the
        STA transcript URL and downloads the JSON. Returns a flat list of
        utterances attributed to ``customer`` / ``agent`` / ``ivr`` / ``acd``
        with start time and optional per-utterance sentiment.

        Useful for:

        - Coaching: read a flagged call without leaving chat context.
        - Daily brief: summarise a long abandoned call from the repeat-caller
          hotlist.
        - Routing diagnostic: see what the customer said at the IVR step
          before they abandoned.

        Soft-fails on missing recordings (returns ``{status: 404, ...}``).
        Hard-fails on auth issues so the caller knows scope is the problem.
        """
        return fetch_conversation_transcript(
            conversation_id, mode=mode, max_utterances=max_utterances,
        )

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
