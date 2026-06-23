"""Pin v1.8 ``search_conversations_by_attribute``.

The gap this tool closes: pre-v1.8 there was no path to question
conversations by participant attribute. NPS / CSAT / outcome questions
all hit dead ends.

These tests pin:

- Request-body shape sent to ``/api/v2/conversations/participants/attributes/search``
  (DATE_RANGE + EXACT criteria, value vs values, field-path conventions)
- NPS auto-detection (positive + negative cases)
- Numeric summary when values aren't NPS-shaped
- Value distribution sort + percentage math
- v1.5 envelope contract (top-level interval + as_of_utc)
- Empty-result safety
- agent_user_id extraction from participants[purpose=agent]
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _make_fake_api(*, results_page=None, page_count=1):
    """Build a fake api_client.

    ``results_page`` is the single canned response body (used for every
    page request).
    """
    captured: dict[str, list[dict]] = {"calls": []}
    canned = results_page if results_page is not None else {"results": [], "pageCount": 1}

    class FakeApi:
        def call_api(self, **kwargs):
            captured["calls"].append(kwargs)
            return canned

    return FakeApi, captured


def _call_tool(args, monkeypatch, *, fake_api):
    import PureCloudPlatformClientV2 as gc
    from genesys_mcp import client as gen_client
    from genesys_mcp.tools import attribute_search
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(attribute_search, "get_api", lambda: fake_api)
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

    app = FastMCP(name="t")
    attribute_search.register(app)
    result = asyncio.run(app.call_tool("search_conversations_by_attribute", args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text)


_INTERVAL = "2026-06-23T14:00:00.000Z/2026-06-24T14:00:00.000Z"


def _conv(*, conv_id, attribute_key, attribute_value, agent_user_id=None, queue_id=None):
    """Build one fake conversation entity matching what the search returns."""
    participants = [{
        "purpose": "customer",
        "attributes": {attribute_key: attribute_value},
        "segments": ([{"queueId": queue_id, "start": "2026-06-23T14:00:00.000Z"}] if queue_id else []),
    }]
    if agent_user_id:
        participants.append({
            "purpose": "agent",
            "userId": agent_user_id,
            "segments": [],
        })
    return {
        "conversationId": conv_id,
        "conversationStart": "2026-06-23T14:30:00.000Z",
        "participants": participants,
    }


# ─────────────────────── request body shape ───────────────────────


class TestRequestBodyShape:
    def test_default_value_uses_nps_enumeration(self, monkeypatch):
        FakeApi, captured = _make_fake_api()
        _call_tool(
            {"attribute_key": "NPS Score", "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        body = captured["calls"][0]["body"]
        attr_criterion = body["query"][1]
        assert attr_criterion["type"] == "EXACT"
        assert attr_criterion["fields"] == ["participantData.NPS Score"]
        assert attr_criterion["values"] == [str(i) for i in range(0, 11)]
        assert "value" not in attr_criterion

    def test_explicit_value_uses_single_value(self, monkeypatch):
        FakeApi, captured = _make_fake_api()
        _call_tool(
            {"attribute_key": "outcome", "attribute_value": "Resolved",
             "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        body = captured["calls"][0]["body"]
        attr_criterion = body["query"][1]
        assert attr_criterion["type"] == "EXACT"
        assert attr_criterion["fields"] == ["participantData.outcome"]
        assert attr_criterion["value"] == "Resolved"
        assert "values" not in attr_criterion

    def test_date_range_criterion_present(self, monkeypatch):
        FakeApi, captured = _make_fake_api()
        _call_tool(
            {"attribute_key": "NPS Score", "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        body = captured["calls"][0]["body"]
        dr = body["query"][0]
        assert dr["type"] == "DATE_RANGE"
        assert dr["fields"] == ["segments.start"]
        assert dr["startValue"].endswith("Z")
        assert dr["endValue"].endswith("Z")

    def test_endpoint_path(self, monkeypatch):
        FakeApi, captured = _make_fake_api()
        _call_tool(
            {"attribute_key": "NPS Score", "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        assert (
            captured["calls"][0]["resource_path"]
            == "/api/v2/conversations/participants/attributes/search"
        )
        assert captured["calls"][0]["method"] == "POST"


# ─────────────────────── NPS detection ───────────────────────


class TestNpsDetection:
    def test_nps_positive_case_all_0_to_10(self, monkeypatch):
        # 12 detractors (0-6), 45 passives (7-8), 85 promoters (9-10) = 142 total
        # NPS = (85 - 12) / 142 * 100 = 51.4
        nps_values: list[str] = (
            ["0"] * 3 + ["3"] * 4 + ["6"] * 5
            + ["7"] * 20 + ["8"] * 25
            + ["9"] * 40 + ["10"] * 45
        )
        results = [
            _conv(conv_id=f"c{i}", attribute_key="NPS Score",
                   attribute_value=v, agent_user_id=f"u{i % 5}")
            for i, v in enumerate(nps_values)
        ]
        FakeApi, _ = _make_fake_api(results_page={"results": results, "pageCount": 1})

        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        ns = out["numeric_summary"]
        assert ns is not None
        assert ns["count"] == 142
        nps = ns["nps"]
        assert nps is not None
        assert nps["detractors_0_6"] == 12
        assert nps["passives_7_8"] == 45
        assert nps["promoters_9_10"] == 85
        assert nps["score"] == 51.4

    def test_nps_negative_out_of_range_value(self, monkeypatch):
        nps_values = ["5", "7", "11"]
        results = [
            _conv(conv_id=f"c{i}", attribute_key="NPS Score", attribute_value=v)
            for i, v in enumerate(nps_values)
        ]
        FakeApi, _ = _make_fake_api(results_page={"results": results, "pageCount": 1})

        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        ns = out["numeric_summary"]
        assert ns is not None
        assert ns["count"] == 3
        assert ns["nps"] is None

    def test_nps_negative_non_integer(self, monkeypatch):
        results = [
            _conv(conv_id="c1", attribute_key="Agent Score", attribute_value="4.5"),
            _conv(conv_id="c2", attribute_key="Agent Score", attribute_value="3.2"),
        ]
        FakeApi, _ = _make_fake_api(results_page={"results": results, "pageCount": 1})

        out = _call_tool(
            {"attribute_key": "Agent Score", "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        ns = out["numeric_summary"]
        assert ns is not None
        assert ns["count"] == 2
        assert ns["nps"] is None

    def test_non_numeric_values_yield_null_numeric_summary(self, monkeypatch):
        results = [
            _conv(conv_id="c1", attribute_key="outcome", attribute_value="Resolved"),
            _conv(conv_id="c2", attribute_key="outcome", attribute_value="Unresolved"),
            _conv(conv_id="c3", attribute_key="outcome", attribute_value="Resolved"),
        ]
        FakeApi, _ = _make_fake_api(results_page={"results": results, "pageCount": 1})

        out = _call_tool(
            {"attribute_key": "outcome", "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["numeric_summary"] is None


# ─────────────────────── value distribution ───────────────────────


class TestValueDistribution:
    def test_distribution_sorted_by_count_desc(self, monkeypatch):
        results = [
            _conv(conv_id="c1", attribute_key="outcome", attribute_value="Resolved"),
            _conv(conv_id="c2", attribute_key="outcome", attribute_value="Unresolved"),
            _conv(conv_id="c3", attribute_key="outcome", attribute_value="Resolved"),
            _conv(conv_id="c4", attribute_key="outcome", attribute_value="Escalated"),
            _conv(conv_id="c5", attribute_key="outcome", attribute_value="Unresolved"),
            _conv(conv_id="c6", attribute_key="outcome", attribute_value="Resolved"),
        ]
        FakeApi, _ = _make_fake_api(results_page={"results": results, "pageCount": 1})

        out = _call_tool(
            {"attribute_key": "outcome", "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        dist = out["value_distribution"]
        assert [r["value"] for r in dist] == ["Resolved", "Unresolved", "Escalated"]
        assert [r["count"] for r in dist] == [3, 2, 1]
        assert sum(r["percentage"] for r in dist) == pytest.approx(100.0, abs=0.5)


# ─────────────────────── envelope + edges ───────────────────────


class TestEnvelopeAndEdges:
    def test_top_level_interval_and_as_of_utc(self, monkeypatch):
        FakeApi, _ = _make_fake_api()
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["interval"] == _INTERVAL
        assert "as_of_utc" in out
        assert out["as_of_utc"].endswith("Z")
        assert out["attribute_key"] == "NPS Score"
        assert out["attribute_value"] is None

    def test_empty_result_is_safe(self, monkeypatch):
        FakeApi, _ = _make_fake_api(results_page={"results": [], "pageCount": 1})
        out = _call_tool(
            {"attribute_key": "outcome", "attribute_value": "Resolved",
             "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["totals"]["conversation_count"] == 0
        assert out["value_distribution"] == []
        assert out["numeric_summary"] is None
        assert out["conversations"] == []

    def test_agent_user_id_extracted(self, monkeypatch):
        results = [
            _conv(conv_id="c1", attribute_key="NPS Score",
                   attribute_value="9", agent_user_id="agent-42",
                   queue_id="q-1"),
        ]
        FakeApi, _ = _make_fake_api(results_page={"results": results, "pageCount": 1})

        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        row = out["conversations"][0]
        assert row["conversation_id"] == "c1"
        assert row["agent_user_id"] == "agent-42"
        assert row["queue_id"] == "q-1"
        assert row["attribute_value"] == "9"

    def test_invalid_mode_raises(self, monkeypatch):
        FakeApi, _ = _make_fake_api()
        with pytest.raises(Exception, match="mode must be 'summary' or 'full'"):
            _call_tool(
                {"attribute_key": "NPS Score", "interval": _INTERVAL, "mode": "bogus"},
                monkeypatch, fake_api=FakeApi(),
            )
