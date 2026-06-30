"""Pin v1.6 ``agent_utilization`` tool — routing-status durations + answered counts.

The gap this tool closes: pre-v1.6 nothing in the codebase queried
``/api/v2/analytics/users/aggregates/query``, so on-queue time per agent
was unreachable. With on-queue time absent, occupancy and interactions-
per-hour couldn't be computed at all.

These tests pin:

- the two API bodies are shaped correctly (request-shape sentinels)
- both calls fire concurrently (one SDK stub per endpoint)
- response composition joins routing + conversation data per user
- ratio math (occupancy, interactions/hour, voice:message)
- divide-by-zero guards (no on-queue time, no messages)
- sort order (interactions/hour desc, nulls last)
- v1.5 contract (top-level interval + as_of_utc)
- 403 soft-fail on the routing-status endpoint
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _fake_to_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, list):
        return obj
    return obj


def _make_fake_analytics(routing_resp, conv_resp, *, routing_status=None):
    """Factory for a FakeAnalyticsApi that returns the given responses."""
    captured: dict[str, dict] = {}

    class FakeHttpExc(Exception):
        def __init__(self, status):
            self.status = status
            super().__init__(f"HTTP {status}")

    class FakeAnalyticsApi:
        def __init__(self, *args, **kwargs): pass

        def post_analytics_users_aggregates_query(self, body):
            captured["routing"] = body
            if routing_status is not None:
                raise FakeHttpExc(routing_status)
            return routing_resp

        def post_analytics_conversations_aggregates_query(self, body):
            captured["conversations"] = body
            return conv_resp

    return FakeAnalyticsApi, captured


def _call_utilization(args, monkeypatch, *, routing_resp, conv_resp,
                       routing_status=None):
    """Register, mock, invoke ``agent_utilization``, return (parsed_json, captured)."""
    import PureCloudPlatformClientV2 as gc
    from genesys_mcp import client as gen_client
    from genesys_mcp.tools import utilization
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(utilization, "to_dict", _fake_to_dict)

    def fake_user_names(user_ids):
        return {uid: f"User {uid}" for uid in user_ids}

    monkeypatch.setattr(utilization.resolver, "user_names", fake_user_names)

    FakeApi, captured = _make_fake_analytics(
        routing_resp, conv_resp, routing_status=routing_status,
    )
    monkeypatch.setattr(utilization.gc, "AnalyticsApi", FakeApi)
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

    app = FastMCP(name="t")
    utilization.register(app)
    result = asyncio.run(app.call_tool("agent_utilization", args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text), captured


_INTERVAL = "2026-06-15T14:00:00.000Z/2026-06-22T14:00:00.000Z"


def _routing_response(per_user_seconds):
    """Build a fake users/aggregates response.

    ``per_user_seconds`` is ``{uid: {status: seconds}}``.
    """
    results = []
    for uid, by_status in per_user_seconds.items():
        for status, seconds in by_status.items():
            results.append({
                "group": {"userId": uid, "routingStatus": status},
                "data": [{
                    "interval": _INTERVAL,
                    "metrics": [{
                        "metric": "tAgentRoutingStatus",
                        "stats": {"sum": seconds * 1000, "count": 1},
                    }],
                }],
            })
    return {"results": results}


def _conv_response(per_user_media):
    """``{uid: {media: (answered_count, handle_seconds)}}`` → conv/aggregates shape."""
    results = []
    for uid, by_media in per_user_media.items():
        for media, (answered, handle_s) in by_media.items():
            results.append({
                "group": {"userId": uid, "mediaType": media},
                "data": [{
                    "interval": _INTERVAL,
                    "metrics": [
                        {"metric": "tAnswered", "stats": {"count": answered}},
                        {"metric": "tHandle",   "stats": {"count": answered, "sum": handle_s * 1000}},
                    ],
                }],
            })
    return {"results": results}


# ─────────────────────── guards ───────────────────────


class TestInputGuards:
    def test_empty_user_ids_raises(self, monkeypatch):
        with pytest.raises(Exception, match="user_ids must contain at least one"):
            _call_utilization(
                {"user_ids": []},
                monkeypatch,
                routing_resp={"results": []},
                conv_resp={"results": []},
            )

    def test_invalid_mode_raises(self, monkeypatch):
        with pytest.raises(Exception, match="mode must be 'summary' or 'full'"):
            _call_utilization(
                {"user_ids": ["u1"], "mode": "bogus"},
                monkeypatch,
                routing_resp={"results": []},
                conv_resp={"results": []},
            )


# ─────────────────────── request shape ───────────────────────


class TestRequestShape:
    def test_routing_body_shape(self, monkeypatch):
        _, captured = _call_utilization(
            {"user_ids": ["u1", "u2"], "interval": _INTERVAL},
            monkeypatch,
            routing_resp={"results": []},
            conv_resp={"results": []},
        )
        body = captured["routing"]
        assert body["groupBy"] == ["userId", "routingStatus"]
        assert "tAgentRoutingStatus" in body["metrics"]
        assert body["interval"] == _INTERVAL
        f = body["filter"]
        assert f["type"] == "and"
        assert f["clauses"][0]["type"] == "or"
        user_values = {p["value"] for p in f["clauses"][0]["predicates"]}
        assert user_values == {"u1", "u2"}

    def test_conversations_body_shape(self, monkeypatch):
        _, captured = _call_utilization(
            {"user_ids": ["u1"], "interval": _INTERVAL},
            monkeypatch,
            routing_resp={"results": []},
            conv_resp={"results": []},
        )
        body = captured["conversations"]
        assert body["groupBy"] == ["userId", "mediaType"]
        metrics = set(body["metrics"])
        assert "tAnswered" in metrics
        assert "tHandle" in metrics

    def test_both_endpoints_called(self, monkeypatch):
        _, captured = _call_utilization(
            {"user_ids": ["u1"]},
            monkeypatch,
            routing_resp={"results": []},
            conv_resp={"results": []},
        )
        assert "routing" in captured
        assert "conversations" in captured


# ─────────────────────── response composition ───────────────────────


class TestResponseComposition:
    def test_per_agent_block_populated(self, monkeypatch):
        out, _ = _call_utilization(
            {"user_ids": ["u1"], "interval": _INTERVAL},
            monkeypatch,
            routing_resp=_routing_response({
                "u1": {
                    "ON_QUEUE": 21600,
                    "INTERACTING": 14400,
                    "IDLE": 7200,
                    "OFF_QUEUE": 3600,
                },
            }),
            conv_resp=_conv_response({
                "u1": {"voice": (22, 7200), "message": (14, 4200)},
            }),
        )
        row = out["users"][0]
        assert row["user_id"] == "u1"
        assert row["user_name"] == "User u1"
        assert row["on_queue_seconds"] == 21600
        assert row["interacting_seconds"] == 14400
        assert row["idle_seconds"] == 7200
        assert row["off_queue_seconds"] == 3600
        assert row["not_responding_seconds"] == 0
        assert row["voice_answered"] == 22
        assert row["message_answered"] == 14
        assert row["total_answered"] == 36


# ─────────────────────── ratio math ───────────────────────


class TestRatioMath:
    def test_interactions_per_on_queue_hour(self, monkeypatch):
        out, _ = _call_utilization(
            {"user_ids": ["u1"]},
            monkeypatch,
            routing_resp=_routing_response({"u1": {"ON_QUEUE": 21600}}),
            conv_resp=_conv_response({
                "u1": {"voice": (22, 7200), "message": (14, 4200)},
            }),
        )
        assert out["users"][0]["interactions_per_on_queue_hour"] == 6.0

    def test_occupancy_pct(self, monkeypatch):
        out, _ = _call_utilization(
            {"user_ids": ["u1"]},
            monkeypatch,
            routing_resp=_routing_response({"u1": {"ON_QUEUE": 21600}}),
            conv_resp=_conv_response({
                "u1": {"voice": (22, 7200), "message": (14, 4200)},
            }),
        )
        assert out["users"][0]["occupancy_pct"] == 52.8

    def test_voice_to_message_ratio(self, monkeypatch):
        out, _ = _call_utilization(
            {"user_ids": ["u1"]},
            monkeypatch,
            routing_resp=_routing_response({"u1": {"ON_QUEUE": 21600}}),
            conv_resp=_conv_response({
                "u1": {"voice": (22, 7200), "message": (14, 4200)},
            }),
        )
        assert out["users"][0]["voice_to_message_ratio"] == 1.57


# ─────────────────────── divide-by-zero guards ───────────────────────


class TestDivideByZeroGuards:
    def test_zero_on_queue_yields_null_ratios(self, monkeypatch):
        out, _ = _call_utilization(
            {"user_ids": ["u1"]},
            monkeypatch,
            routing_resp=_routing_response({"u1": {"ON_QUEUE": 0}}),
            conv_resp=_conv_response({"u1": {"voice": (5, 1200)}}),
        )
        row = out["users"][0]
        assert row["interactions_per_on_queue_hour"] is None
        assert row["occupancy_pct"] is None

    def test_no_messages_yields_null_voice_to_message(self, monkeypatch):
        out, _ = _call_utilization(
            {"user_ids": ["u1"]},
            monkeypatch,
            routing_resp=_routing_response({"u1": {"ON_QUEUE": 21600}}),
            conv_resp=_conv_response({"u1": {"voice": (22, 7200)}}),
        )
        assert out["users"][0]["voice_to_message_ratio"] is None


# ─────────────────────── sort order ───────────────────────


class TestSortOrder:
    def test_sorted_by_interactions_per_hour_desc(self, monkeypatch):
        # u1: 10/hr, u2: 5/hr, u3: null (no on-queue) → expected order [u1, u2, u3]
        out, _ = _call_utilization(
            {"user_ids": ["u3", "u1", "u2"]},
            monkeypatch,
            routing_resp=_routing_response({
                "u1": {"ON_QUEUE": 3600},
                "u2": {"ON_QUEUE": 7200},
                "u3": {"ON_QUEUE": 0},
            }),
            conv_resp=_conv_response({
                "u1": {"voice": (10, 600)},
                "u2": {"voice": (10, 600)},
                "u3": {"voice": (1, 60)},
            }),
        )
        ordered_uids = [r["user_id"] for r in out["users"]]
        assert ordered_uids == ["u1", "u2", "u3"]
        assert out["users"][-1]["interactions_per_on_queue_hour"] is None


# ─────────────────────── v1.5 envelope contract ───────────────────────


class TestTopLevelEnvelope:
    def test_interval_and_as_of_utc_at_top(self, monkeypatch):
        out, _ = _call_utilization(
            {"user_ids": ["u1"], "interval": _INTERVAL},
            monkeypatch,
            routing_resp={"results": []},
            conv_resp={"results": []},
        )
        assert out["interval"] == _INTERVAL
        assert "as_of_utc" in out
        assert out["as_of_utc"].endswith("Z")
        assert out["mode"] == "summary"
        assert out["sort_by"] == "interactions_per_on_queue_hour_desc"


# ─────────────────────── soft-fail on routing 403 ───────────────────────


class TestRoutingStatusSoftFail:
    def test_403_degrades_routing_to_zero_but_keeps_answered(self, monkeypatch):
        out, _ = _call_utilization(
            {"user_ids": ["u1"]},
            monkeypatch,
            routing_resp=None,
            routing_status=403,
            conv_resp=_conv_response({"u1": {"voice": (10, 1200)}}),
        )
        assert out["routing_status_scope_available"] is False
        assert "routing_status_unavailable_note" in out
        row = out["users"][0]
        assert row["on_queue_seconds"] == 0
        assert row["interactions_per_on_queue_hour"] is None
        assert row["occupancy_pct"] is None
        assert row["voice_answered"] == 10
        assert row["total_answered"] == 10

    def test_403_note_names_scope_and_denies_tenant_block_misread(self, monkeypatch):
        # v1.14 P3: the note must make clear this is a MISSING SCOPE, not a
        # tenant block / broken query, so the agent stops saying "blocked".
        out, _ = _call_utilization(
            {"user_ids": ["u1"]},
            monkeypatch,
            routing_resp=None,
            routing_status=403,
            conv_resp=_conv_response({"u1": {"voice": (10, 1200)}}),
        )
        note = out["routing_status_unavailable_note"]
        assert "analytics:agentRouting:view" in note
        assert "MISSING SCOPE" in note
        assert "not a tenant block" in note
