"""callback_outcomes classification + funnel math, and the callback-aware derived block.

Customer-first callbacks record their outcome on voice sessions (the callback ACD
session ends at dial-out), so classification must walk conversation details. These
tests pin the classifier against the session/segment shapes observed on a live
tenant (2026-07): callback ACD session + outbound customer voice dial + re-entry
agent voice session carrying tTalk.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from genesys_mcp.tools.analytics import _attach_derived_metrics
from genesys_mcp.tools.callbacks import classify_conversation, summarise_outcomes

QUEUE = "q-general"


def _seg(seg_type: str, start: str, queue: str | None = None) -> dict:
    seg = {"segmentType": seg_type, "segmentStart": start, "segmentEnd": start}
    if queue:
        seg["queueId"] = queue
    return seg


def _conv(
    conv_id: str = "c1",
    queue: str = QUEUE,
    dial_attempts: int = 1,
    customer_answered: bool = True,
    agent_after_dial: bool = True,
    agent_before_dial: bool = False,
    with_dial: bool = True,
) -> dict:
    """Synthetic customer-first callback conversation.

    Timeline mirrors live records: callback ACD at 08:45:41, dial at 08:5X:41,
    customer interact 08:55:49, agent interact 08:56:31. agent_before_dial adds
    an agent leg from the original inbound call (08:30) that must NOT count as
    a bridged callback.
    """
    participants: list[dict] = [
        {
            "purpose": "acd",
            "sessions": [{
                "mediaType": "callback",
                "direction": "outbound",
                "segments": [_seg("interact", "2026-07-10T08:45:41.212Z", queue)],
                "metrics": [{"name": "nOffered", "value": 1}],
            }],
        },
    ]
    if with_dial:
        dialing = [
            _seg("dialing", f"2026-07-10T08:5{i}:41.408Z", queue)
            for i in range(dial_attempts)
        ]
        segments = list(dialing)
        if customer_answered:
            segments.append(_seg("interact", "2026-07-10T08:55:49.728Z", queue))
        participants.append({
            "purpose": "customer",
            "sessions": [{
                "mediaType": "voice",
                "direction": "outbound",
                "segments": segments,
                "metrics": [],
            }],
        })
    if agent_before_dial:
        participants.append({
            "purpose": "agent",
            "sessions": [{
                "mediaType": "voice",
                "direction": "inbound",
                "segments": [_seg("interact", "2026-07-10T08:30:00.000Z", queue)],
                "metrics": [{"name": "tTalk", "value": 60000}],
            }],
        })
    if agent_after_dial:
        participants.append({
            "purpose": "agent",
            "sessions": [{
                "mediaType": "voice",
                "direction": "inbound",
                "segments": [_seg("interact", "2026-07-10T08:56:31.000Z", queue)],
                "metrics": [{"name": "tTalk", "value": 353135}],
            }],
        })
    return {"conversationId": conv_id, "participants": participants}


class TestClassifyConversation:
    def test_answered_and_bridged(self):
        row = classify_conversation(_conv())
        assert row["outcome"] == "answered_and_bridged"
        assert row["queue_id"] == QUEUE
        assert row["dial_attempts"] == 1
        assert row["wait_to_dial_s"] is not None

    def test_answered_not_bridged(self):
        row = classify_conversation(_conv(agent_after_dial=False))
        assert row["outcome"] == "answered_not_bridged"

    def test_dialed_not_answered(self):
        row = classify_conversation(
            _conv(customer_answered=False, agent_after_dial=False)
        )
        assert row["outcome"] == "dialed_not_answered"

    def test_never_dialed(self):
        row = classify_conversation(_conv(with_dial=False, agent_after_dial=False))
        assert row["outcome"] == "never_dialed"
        assert row["dial_attempts"] == 0

    def test_retries_counted(self):
        row = classify_conversation(_conv(dial_attempts=2))
        assert row["dial_attempts"] == 2

    def test_agent_leg_before_dial_does_not_count_as_bridged(self):
        # An agent who took the original inbound call (and scheduled the
        # callback) must not make an unbridged callback look bridged.
        row = classify_conversation(
            _conv(agent_after_dial=False, agent_before_dial=True)
        )
        assert row["outcome"] == "answered_not_bridged"

    def test_non_callback_conversation_returns_none(self):
        conv = {"conversationId": "x", "participants": [
            {"purpose": "acd", "sessions": [{
                "mediaType": "voice", "direction": "inbound",
                "segments": [_seg("interact", "2026-07-10T08:00:00.000Z", QUEUE)],
                "metrics": [],
            }]},
        ]}
        assert classify_conversation(conv) is None

    def test_queue_filter_excludes_other_queues(self):
        # Segment filters over-match at the API level; the classifier must
        # drop callbacks whose ACD segment sits on a non-requested queue.
        assert classify_conversation(_conv(queue="q-other"), {QUEUE}) is None
        assert classify_conversation(_conv(), {QUEUE}) is not None


class TestSummariseOutcomes:
    def test_funnel_math(self):
        rows = [
            classify_conversation(_conv(conv_id=f"b{i}")) for i in range(6)
        ] + [
            classify_conversation(_conv(conv_id="nb", agent_after_dial=False)),
            classify_conversation(_conv(conv_id="na", customer_answered=False, agent_after_dial=False)),
            classify_conversation(_conv(conv_id="nd", with_dial=False, agent_after_dial=False)),
            classify_conversation(_conv(conv_id="r2", dial_attempts=2)),
        ]
        out = summarise_outcomes(rows)
        totals = out["totals"]
        assert totals["callbacks_scheduled"] == 10
        assert totals["bridged_to_agent"] == 7            # 6 + the retry one
        assert totals["customer_reached"] == 8            # bridged + not-bridged
        assert totals["customer_reached_pct"] == 80.0
        assert totals["bridged_to_agent_pct"] == 70.0
        assert totals["outcomes"]["dialed_not_answered"] == 1
        assert totals["outcomes"]["never_dialed"] == 1
        assert totals["dial_attempts_histogram"]["2"] == 1
        assert out["queues"][QUEUE]["callbacks_scheduled"] == 10
        assert len(totals["example_conversation_ids"]["answered_and_bridged"]) == 3

    def test_empty_rows(self):
        out = summarise_outcomes([])
        assert out["totals"]["callbacks_scheduled"] == 0
        assert out["totals"]["customer_reached_pct"] is None
        assert out["queues"] == {}


class TestCallbackAwareDerivedBlock:
    def _resp(self) -> dict:
        return {"results": [
            {
                "group": {"queueId": "q1", "mediaType": "callback"},
                "data": [{"interval": "i", "metrics": [
                    {"metric": "nOffered", "stats": {"count": 143}},
                    {"metric": "nConnected", "stats": {"count": 7}},
                    {"metric": "tWait", "stats": {"count": 143, "sum": 55173891.0}},
                ]}],
            },
            {
                "group": {"queueId": "q1", "mediaType": "voice"},
                "data": [{"interval": "i", "metrics": [
                    {"metric": "nOffered", "stats": {"count": 100}},
                    {"metric": "tAnswered", "stats": {"count": 90, "sum": 900000.0}},
                    {"metric": "tAbandon", "stats": {"count": 5, "sum": 50000.0}},
                ]}],
            },
        ]}

    def test_callback_row_gets_nulls_not_zeros(self):
        resp = self._resp()
        _attach_derived_metrics(resp)
        derived = resp["results"][0]["data"][0]["derived"]
        assert derived["callbacks_scheduled"] == 143
        assert derived["answered"] is None
        assert derived["answered_pct"] is None
        assert derived["abandoned_pct"] is None
        assert derived["avg_wait_to_dial_s"] == 385.8
        assert "callback_outcomes" in derived["callback_note"]

    def test_voice_row_unchanged(self):
        resp = self._resp()
        _attach_derived_metrics(resp)
        derived = resp["results"][1]["data"][0]["derived"]
        assert derived["answered"] == 90
        assert derived["answered_pct"] == 90.0
        assert derived["abandoned_pct"] == 5.0
        assert "callback_note" not in derived

    def test_ungrouped_response_keeps_legacy_shape(self):
        # group_by_media=False → no mediaType in group; must not special-case.
        resp = {"results": [{
            "group": {"queueId": "q1"},
            "data": [{"interval": "i", "metrics": [
                {"metric": "nOffered", "stats": {"count": 10}},
                {"metric": "tAnswered", "stats": {"count": 8, "sum": 80000.0}},
            ]}],
        }]}
        _attach_derived_metrics(resp)
        assert resp["results"][0]["data"][0]["derived"]["answered"] == 8


class TestCallbackOutcomesTool:
    def test_paginates_and_summarises(self, monkeypatch):
        import asyncio
        import json

        import PureCloudPlatformClientV2 as gc
        from mcp.server.fastmcp import FastMCP

        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import callbacks as callbacks_mod

        # Page 1 full (100 conversations) → page 2 short → stop.
        pages = {
            1: {"totalHits": 105, "conversations": [
                _conv(conv_id=f"p1-{i}") for i in range(100)
            ]},
            2: {"totalHits": 105, "conversations": [
                _conv(conv_id=f"p2-{i}", agent_after_dial=False) for i in range(5)
            ]},
        }
        captured_bodies: list[dict] = []

        def fake_details(self, body, **kwargs):
            captured_bodies.append(body)
            return pages[body["paging"]["pageNumber"]]

        monkeypatch.setattr(
            gc.AnalyticsApi, "post_analytics_conversations_details_query", fake_details,
        )
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())
        # The fake returns plain dicts; production passes SDK models through to_dict.
        monkeypatch.setattr(callbacks_mod, "to_dict", lambda x: x)

        test_mcp = FastMCP(name="t")
        callbacks_mod.register(test_mcp)
        contents = asyncio.run(test_mcp.call_tool("callback_outcomes", {
            "queue_ids": [QUEUE],
            "interval": "2026-07-05T14:00:00.000Z/2026-07-12T14:00:00.000Z",
        }))
        # FastMCP returns a list of content blocks; the tool's dict comes back
        # as JSON text in the first block.
        payload = json.loads(contents[0].text)

        assert len(captured_bodies) == 2
        assert captured_bodies[0]["segmentFilters"][0]["predicates"][0]["value"] == "callback"
        totals = payload["totals"]
        assert totals["callbacks_scheduled"] == 105
        assert totals["bridged_to_agent"] == 100
        assert totals["outcomes"]["answered_not_bridged"] == 5
        assert payload["truncated"] is False
        assert payload["total_hits"] == 105
        assert payload["conversations_scanned"] == 105
