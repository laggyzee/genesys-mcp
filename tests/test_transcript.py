"""Pin v1.2's transcript parsing + the helpers shared with coaching pack.

The transcript JSON shape (documented at
https://developer.genesys.cloud/analyticsdatamanagement/speechtextanalytics/transcript-url)
has phrases nested under transcripts[], with sentiment in a sibling
analytics.sentiment[] keyed by phraseIndex. These tests use synthesised
JSON fixtures so we don't depend on a live tenant.
"""
from __future__ import annotations

import json

import pytest


# ─────────────────────── speaker normalisation ───────────────────────

class TestSpeakerNormalisation:
    def test_external_becomes_customer(self):
        from genesys_mcp.tools.speech_analytics import _normalise_speaker
        assert _normalise_speaker("external") == "customer"
        assert _normalise_speaker("EXTERNAL") == "customer"

    def test_internal_becomes_agent(self):
        from genesys_mcp.tools.speech_analytics import _normalise_speaker
        assert _normalise_speaker("internal") == "agent"
        assert _normalise_speaker("user") == "agent"
        assert _normalise_speaker("agent") == "agent"

    def test_non_human_purposes_pass_through(self):
        from genesys_mcp.tools.speech_analytics import _normalise_speaker
        assert _normalise_speaker("ivr") == "ivr"
        assert _normalise_speaker("acd") == "acd"
        assert _normalise_speaker("voicemail") == "voicemail"

    def test_unknown_purpose_is_lowercased(self):
        from genesys_mcp.tools.speech_analytics import _normalise_speaker
        assert _normalise_speaker("Something Else") == "something else"

    def test_none_becomes_unknown(self):
        from genesys_mcp.tools.speech_analytics import _normalise_speaker
        assert _normalise_speaker(None) == "unknown"
        assert _normalise_speaker("") == "unknown"


# ─────────────────────── sentiment label translation ───────────────────────

class TestSentimentLabel:
    def test_positive_threshold(self):
        from genesys_mcp.tools.speech_analytics import _sentiment_label
        assert _sentiment_label(1) == "positive"
        assert _sentiment_label(0.5) == "positive"

    def test_negative_threshold(self):
        from genesys_mcp.tools.speech_analytics import _sentiment_label
        assert _sentiment_label(-1) == "negative"
        assert _sentiment_label(-0.5) == "negative"

    def test_neutral_range(self):
        from genesys_mcp.tools.speech_analytics import _sentiment_label
        assert _sentiment_label(0) == "neutral"
        assert _sentiment_label(0.3) == "neutral"
        assert _sentiment_label(-0.3) == "neutral"

    def test_none_returns_none(self):
        from genesys_mcp.tools.speech_analytics import _sentiment_label
        assert _sentiment_label(None) is None


# ─────────────────────── utterance flattening ───────────────────────

def _sample_transcript_json() -> dict:
    """Synthesised transcript JSON in Genesys's documented shape."""
    return {
        "conversationId": "conv-1",
        "communicationId": "session-1",
        "mediaType": "voice",
        "transcripts": [{
            "transcriptId": "t1",
            "phrases": [
                {"text": "Hi I'm calling about my bill",
                 "decoratedText": "Hi, I'm calling about my bill.",
                 "startTimeMs": 500, "participantPurpose": "external"},
                {"text": "Sure no problem let me pull that up",
                 "decoratedText": "Sure, no problem. Let me pull that up.",
                 "startTimeMs": 4200, "participantPurpose": "internal"},
                {"text": "this is taking forever",
                 "startTimeMs": 90000, "participantPurpose": "external"},
            ],
            "analytics": {
                "sentiment": [
                    {"phraseIndex": 0, "sentiment": 0,
                     "participant": "external", "startTimeMs": 500},
                    {"phraseIndex": 2, "sentiment": -1,
                     "participant": "external", "startTimeMs": 90000},
                ],
            },
        }],
        "participants": [
            {"participantPurpose": "external", "ani": "+61400000000",
             "startTimeMs": 0, "endTimeMs": 120000},
            {"participantPurpose": "internal", "userId": "agent-1",
             "startTimeMs": 0, "endTimeMs": 120000},
        ],
    }


class TestBuildUtteranceList:
    def test_flattens_phrases_into_utterances(self):
        from genesys_mcp.tools.speech_analytics import _build_utterance_list
        utts = _build_utterance_list(_sample_transcript_json())
        assert len(utts) == 3
        assert utts[0]["speaker"] == "customer"
        assert utts[0]["start_s"] == 0.5
        assert utts[0]["text"].startswith("Hi, I'm calling")

    def test_prefers_decorated_text_over_raw(self):
        from genesys_mcp.tools.speech_analytics import _build_utterance_list
        utts = _build_utterance_list(_sample_transcript_json())
        # The first utterance has both `text` and `decoratedText` — punctuated
        # version should win.
        assert "," in utts[0]["text"]

    def test_attaches_sentiment_by_phrase_index(self):
        from genesys_mcp.tools.speech_analytics import _build_utterance_list
        utts = _build_utterance_list(_sample_transcript_json())
        # Sentiment for phraseIndex 0 is 0 (neutral); 2 is -1 (negative);
        # phraseIndex 1 has no sentiment attached.
        assert utts[0]["sentiment"] == 0
        assert utts[0]["sentiment_label"] == "neutral"
        assert "sentiment" not in utts[1]  # no sentiment record for index 1
        assert utts[2]["sentiment"] == -1
        assert utts[2]["sentiment_label"] == "negative"

    def test_skips_empty_text_phrases(self):
        from genesys_mcp.tools.speech_analytics import _build_utterance_list
        tj = _sample_transcript_json()
        tj["transcripts"][0]["phrases"].append({
            "startTimeMs": 100000, "participantPurpose": "external"
            # no text, no decoratedText — should be skipped
        })
        utts = _build_utterance_list(tj)
        assert len(utts) == 3  # the empty phrase didn't add a row


class TestSummariseParticipants:
    def test_returns_role_to_identifier_map(self):
        from genesys_mcp.tools.speech_analytics import _summarise_participants
        out = _summarise_participants(_sample_transcript_json())
        assert out == {"customer": "+61400000000", "agent": "agent-1"}

    def test_first_identifier_per_role_wins(self):
        from genesys_mcp.tools.speech_analytics import _summarise_participants
        tj = _sample_transcript_json()
        tj["participants"].append({
            "participantPurpose": "external", "ani": "+61499999999",
        })
        out = _summarise_participants(tj)
        assert out["customer"] == "+61400000000"


# ─────────────────────── summary-mode trim ───────────────────────

class TestStripUtteranceSentiment:
    def test_removes_sentiment_fields_only(self):
        from genesys_mcp.tools.speech_analytics import _strip_utterance_sentiment
        utts = [
            {"speaker": "customer", "start_s": 1.0, "text": "hi",
             "sentiment": -1, "sentiment_label": "negative"},
        ]
        slim = _strip_utterance_sentiment(utts)
        assert slim == [{"speaker": "customer", "start_s": 1.0, "text": "hi"}]

    def test_preserves_other_fields(self):
        from genesys_mcp.tools.speech_analytics import _strip_utterance_sentiment
        utts = [{"speaker": "agent", "start_s": 0.0, "text": "hi"}]
        assert _strip_utterance_sentiment(utts) == utts


# ─────────────────────── fetch_conversation_transcript (end-to-end with mocks) ───────────────────────

class TestFetchConversationTranscriptEndToEnd:
    """Mock the SDK + HTTP layer; assert the full pipeline composes correctly."""

    def _patch_chain(self, monkeypatch, recordings, transcript_json,
                     transcript_url="https://example.invalid/t.json"):
        """Patch RecordingApi + SpeechTextAnalyticsApi + httpx in one shot."""
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import speech_analytics as sa

        # Mock recording API to return our recordings list
        class FakeRecordingApi:
            def __init__(self, *args, **kwargs): pass
            def get_conversation_recordings(self, conversation_id):
                return recordings

        class FakeTranscriptUrlResp:
            def __init__(self, url): self.url = url
            def to_dict(self): return {"url": self.url}

        class FakeStaApi:
            def __init__(self, *args, **kwargs): pass
            def get_speechandtextanalytics_conversation_communication_transcripturl(
                self, conversation_id, communication_id,
            ):
                return FakeTranscriptUrlResp(transcript_url)

        # Replace the SDK shape converter with an identity-ish that handles
        # both plain dicts and objects with .to_dict().
        def fake_to_dict(obj):
            if isinstance(obj, dict):
                return obj
            if hasattr(obj, "to_dict"):
                return obj.to_dict()
            if isinstance(obj, list):
                return obj
            return obj

        monkeypatch.setattr(sa, "to_dict", fake_to_dict)
        monkeypatch.setattr(sa.gc, "RecordingApi", FakeRecordingApi)
        monkeypatch.setattr(sa.gc, "SpeechTextAnalyticsApi", FakeStaApi)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        class FakeResp:
            def __init__(self, data): self._data = data
            def raise_for_status(self): pass
            def json(self): return self._data

        monkeypatch.setattr(sa.httpx, "get",
                            lambda url, timeout=30.0: FakeResp(transcript_json))

    def test_happy_path_returns_structured_utterances(self, monkeypatch):
        from genesys_mcp.tools.speech_analytics import fetch_conversation_transcript
        self._patch_chain(monkeypatch,
                          recordings=[{"sessionId": "session-1"}],
                          transcript_json=_sample_transcript_json())
        out = fetch_conversation_transcript("conv-1")
        assert out["conversation_id"] == "conv-1"
        assert out["media_type"] == "voice"
        assert out["participants"] == {"customer": "+61400000000", "agent": "agent-1"}
        assert out["total_utterances"] == 3
        assert out["truncated_at"] is None
        assert out["sessions_processed"] == 1
        assert out["utterances"][0]["speaker"] == "customer"

    def test_no_recordings_returns_404_envelope(self, monkeypatch):
        from genesys_mcp.tools.speech_analytics import fetch_conversation_transcript
        self._patch_chain(monkeypatch, recordings=[],
                          transcript_json=_sample_transcript_json())
        out = fetch_conversation_transcript("conv-empty")
        assert out["status"] == 404
        assert "no recording sessions" in out["message"]

    def test_summary_mode_strips_sentiment(self, monkeypatch):
        from genesys_mcp.tools.speech_analytics import fetch_conversation_transcript
        self._patch_chain(monkeypatch,
                          recordings=[{"sessionId": "session-1"}],
                          transcript_json=_sample_transcript_json())
        out = fetch_conversation_transcript("conv-1", mode="summary")
        for u in out["utterances"]:
            assert "sentiment" not in u
            assert "sentiment_label" not in u

    def test_full_mode_keeps_sentiment(self, monkeypatch):
        from genesys_mcp.tools.speech_analytics import fetch_conversation_transcript
        self._patch_chain(monkeypatch,
                          recordings=[{"sessionId": "session-1"}],
                          transcript_json=_sample_transcript_json())
        out = fetch_conversation_transcript("conv-1", mode="full")
        # At least one utterance had sentiment in the source — should be kept
        assert any("sentiment" in u for u in out["utterances"])

    def test_max_utterances_truncates_and_reports_dropped_count(self, monkeypatch):
        from genesys_mcp.tools.speech_analytics import fetch_conversation_transcript
        # Synthesise a long transcript: 50 phrases
        big = _sample_transcript_json()
        big["transcripts"][0]["phrases"] = [
            {"text": f"utterance {i}", "startTimeMs": i * 1000,
             "participantPurpose": "external" if i % 2 else "internal"}
            for i in range(50)
        ]
        big["transcripts"][0]["analytics"] = {"sentiment": []}
        self._patch_chain(monkeypatch,
                          recordings=[{"sessionId": "session-1"}],
                          transcript_json=big)
        out = fetch_conversation_transcript("conv-1", max_utterances=10)
        # total_utterances is the *full* count so callers know how much was
        # dropped; truncated_at is the cap; the list itself is the cap-long.
        assert out["total_utterances"] == 50
        assert out["truncated_at"] == 10
        assert out["total_utterances_dropped"] == 40
        assert len(out["utterances"]) == 10

    def test_invalid_mode_raises(self, monkeypatch):
        from genesys_mcp.tools.speech_analytics import fetch_conversation_transcript
        self._patch_chain(monkeypatch,
                          recordings=[{"sessionId": "session-1"}],
                          transcript_json=_sample_transcript_json())
        with pytest.raises(ValueError, match="must be 'summary' or 'full'"):
            fetch_conversation_transcript("conv-1", mode="abridged")


# ─────────────────────── token-budget for typical excerpt ───────────────────────

class TestTranscriptExcerptBudget:
    """A 40-utterance excerpt should fit in ~5KB — keeps coaching pack chunky but not absurd."""

    def test_forty_utterance_summary_under_8kb(self, monkeypatch):
        from genesys_mcp.tools.speech_analytics import fetch_conversation_transcript

        # Forty average-length utterances ~80 chars each
        long_transcript = _sample_transcript_json()
        long_transcript["transcripts"][0]["phrases"] = [
            {"text": "This is a moderate-length utterance with enough words to be representative " + str(i),
             "startTimeMs": i * 2000,
             "participantPurpose": "external" if i % 2 else "internal"}
            for i in range(40)
        ]
        long_transcript["transcripts"][0]["analytics"] = {"sentiment": []}

        # Patch as in the previous class
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import speech_analytics as sa

        class FakeRecordingApi:
            def __init__(self, *args, **kwargs): pass
            def get_conversation_recordings(self, conversation_id):
                return [{"sessionId": "session-1"}]

        class FakeTranscriptUrlResp:
            url = "https://example.invalid/t.json"
            def to_dict(self): return {"url": self.url}

        class FakeStaApi:
            def __init__(self, *args, **kwargs): pass
            def get_speechandtextanalytics_conversation_communication_transcripturl(
                self, conversation_id, communication_id,
            ):
                return FakeTranscriptUrlResp()

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return long_transcript

        def fake_to_dict(obj):
            if isinstance(obj, dict):
                return obj
            if hasattr(obj, "to_dict"):
                return obj.to_dict()
            if isinstance(obj, list):
                return obj
            return obj

        monkeypatch.setattr(sa, "to_dict", fake_to_dict)
        monkeypatch.setattr(sa.gc, "RecordingApi", FakeRecordingApi)
        monkeypatch.setattr(sa.gc, "SpeechTextAnalyticsApi", FakeStaApi)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())
        monkeypatch.setattr(sa.httpx, "get", lambda url, timeout=30.0: FakeResp())

        out = fetch_conversation_transcript("conv-1", mode="summary", max_utterances=40)
        size = len(json.dumps(out))
        assert size <= 8_000, (
            f"40-utterance summary excerpt is {size:,} bytes, expected ≤ 8KB. "
            "Excerpt is chunky enough to dominate coaching pack token use."
        )
