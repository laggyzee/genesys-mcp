"""Pin v1.9 ``wrap_up_code_distribution``.

The gap this tool closes: pre-v1.9 nothing aggregated wrap-up codes via
Genesys analytics. The only rollup path was N+1 per-conversation
enrichment in ``repeat_caller_deep_dive``, covering only the repeat-
caller cohort.

These tests pin:

- Request body shape sent to ``/api/v2/analytics/conversations/aggregates/query``
  (groupBy = ["wrapUpCode"], metric = tHandle, outer-and-of-or filter)
- Prior-interval computation (same length, immediately before)
- Parallel firing of current + prior calls when include_trend=True
- Distribution + percentages + sort + top_n truncation + "Other" rollup
- Trend block (delta, delta_pct, movement, largest_movers, new/retired)
- v1.5 envelope contract (top-level interval + as_of_utc)
- Empty-result safety
- Catalogue cache reuse across calls
"""
from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture(autouse=True)
def _reset_wrapup_cache():
    from genesys_mcp.tools import wrapup
    wrapup._WRAPUP_CODE_CACHE.clear()
    wrapup._WRAPUP_CACHE_LOADED = False
    yield
    wrapup._WRAPUP_CODE_CACHE.clear()
    wrapup._WRAPUP_CACHE_LOADED = False


def _make_fake(*, current_resp, prior_resp=None, catalogue=None):
    captured: dict[str, list] = {"aggregate_bodies": [], "wrapupcode_calls": 0}

    class FakeAnalyticsApi:
        def __init__(self, *a, **k): pass

        def post_analytics_conversations_aggregates_query(self, body):
            captured["aggregate_bodies"].append(body)
            n = len(captured["aggregate_bodies"])
            if n == 1:
                return current_resp
            return prior_resp if prior_resp is not None else {"results": []}

    class _CodeEntity:
        def __init__(self, cid, name):
            self.id = cid
            self.name = name
            self.division = None
            self.description = None

    class _CodesPage:
        def __init__(self, entities):
            self.entities = entities

    class FakeRoutingApi:
        def __init__(self, *a, **k): pass

        def get_routing_wrapupcodes(self, page_size, page_number, **kwargs):
            captured["wrapupcode_calls"] += 1
            if page_number > 1:
                return _CodesPage([])
            cat = catalogue or {}
            return _CodesPage([_CodeEntity(cid, name) for cid, name in cat.items()])

    return FakeAnalyticsApi, FakeRoutingApi, captured


def _fake_to_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


def _call_tool(args, monkeypatch, *, current_resp, prior_resp=None, catalogue=None):
    import PureCloudPlatformClientV2 as gc
    from genesys_mcp import client as gen_client
    from genesys_mcp.tools import wrapup
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(wrapup, "to_dict", _fake_to_dict)
    FakeAnalyticsApi, FakeRoutingApi, captured = _make_fake(
        current_resp=current_resp,
        prior_resp=prior_resp,
        catalogue=catalogue,
    )
    monkeypatch.setattr(wrapup.gc, "AnalyticsApi", FakeAnalyticsApi)
    monkeypatch.setattr(wrapup.gc, "RoutingApi", FakeRoutingApi)
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

    app = FastMCP(name="t")
    wrapup.register(app)
    result = asyncio.run(app.call_tool("wrap_up_code_distribution", args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text), captured


_INTERVAL = "2026-06-23T14:00:00.000Z/2026-06-24T14:00:00.000Z"


def _resp(counts: dict[str, int]) -> dict:
    return {
        "results": [
            {
                "group": {"wrapUpCode": cid},
                "data": [{
                    "interval": _INTERVAL,
                    "metrics": [{"metric": "tHandle", "stats": {"count": n}}],
                }],
            }
            for cid, n in counts.items()
        ],
    }


_CATALOGUE = {
    "code-resolved": "Customer Resolved",
    "code-callback": "Callback Requested",
    "code-wrong":    "Wrong Number",
    "code-other":    "Other Issue",
    "code-new":      "New Promo Inquiry",
    "code-retired":  "Old Workflow",
}


# ─────────────────────── request body shape ───────────────────────


class TestRequestBodyShape:
    def test_group_by_wrapupcode(self, monkeypatch):
        _, captured = _call_tool(
            {"interval": _INTERVAL, "include_trend": False},
            monkeypatch,
            current_resp={"results": []},
            catalogue={},
        )
        body = captured["aggregate_bodies"][0]
        assert body["groupBy"] == ["wrapUpCode"]
        assert body["metrics"] == ["tHandle"]
        assert body["interval"] == _INTERVAL

    def test_filter_shape_with_queue_user_media(self, monkeypatch):
        _, captured = _call_tool(
            {
                "interval": _INTERVAL,
                "include_trend": False,
                "queue_ids": ["q1", "q2"],
                "user_ids": ["u1"],
                "media_types": ["voice", "message"],
            },
            monkeypatch,
            current_resp={"results": []},
            catalogue={},
        )
        body = captured["aggregate_bodies"][0]
        f = body["filter"]
        assert f["type"] == "and"
        clauses = f["clauses"]
        assert len(clauses) == 3
        assert clauses[0]["type"] == "or"
        assert {p["value"] for p in clauses[0]["predicates"]} == {"q1", "q2"}
        assert all(p["dimension"] == "queueId" for p in clauses[0]["predicates"])
        assert all(p["dimension"] == "userId"  for p in clauses[1]["predicates"])
        assert all(p["dimension"] == "mediaType" for p in clauses[2]["predicates"])

    def test_filter_absent_when_no_filters(self, monkeypatch):
        _, captured = _call_tool(
            {"interval": _INTERVAL, "include_trend": False},
            monkeypatch,
            current_resp={"results": []},
            catalogue={},
        )
        assert "filter" not in captured["aggregate_bodies"][0]


# ─────────────────────── prior-interval computation ───────────────────────


class TestPriorIntervalComputation:
    def test_prior_interval_is_same_length_immediately_before(self, monkeypatch):
        _, captured = _call_tool(
            {"interval": "2026-06-23T14:00:00.000Z/2026-06-24T14:00:00.000Z"},
            monkeypatch,
            current_resp={"results": []},
            prior_resp={"results": []},
            catalogue={},
        )
        prior_body = captured["aggregate_bodies"][1]
        assert prior_body["interval"] == (
            "2026-06-22T14:00:00Z/2026-06-23T14:00:00Z"
        )

    def test_only_one_call_when_include_trend_false(self, monkeypatch):
        _, captured = _call_tool(
            {"interval": _INTERVAL, "include_trend": False},
            monkeypatch,
            current_resp={"results": []},
            catalogue={},
        )
        assert len(captured["aggregate_bodies"]) == 1

    def test_two_calls_when_include_trend_true(self, monkeypatch):
        _, captured = _call_tool(
            {"interval": _INTERVAL, "include_trend": True},
            monkeypatch,
            current_resp={"results": []},
            prior_resp={"results": []},
            catalogue={},
        )
        assert len(captured["aggregate_bodies"]) == 2


# ─────────────────────── distribution ───────────────────────


class TestDistribution:
    def test_distribution_sort_and_percentages(self, monkeypatch):
        current = _resp({
            "code-resolved": 100,
            "code-callback": 60,
            "code-wrong":    40,
        })
        prior = _resp({
            "code-resolved": 90,
            "code-callback": 50,
            "code-wrong":    20,
        })
        out, _ = _call_tool(
            {"interval": _INTERVAL, "include_trend": True},
            monkeypatch,
            current_resp=current,
            prior_resp=prior,
            catalogue=_CATALOGUE,
        )
        dist = out["distribution"]
        assert [r["name"] for r in dist] == [
            "Customer Resolved", "Callback Requested", "Wrong Number",
        ]
        assert [r["percentage"] for r in dist] == [50.0, 30.0, 20.0]

    def test_name_resolved_from_catalogue(self, monkeypatch):
        out, _ = _call_tool(
            {"interval": _INTERVAL, "include_trend": False},
            monkeypatch,
            current_resp=_resp({"code-resolved": 10}),
            catalogue=_CATALOGUE,
        )
        assert out["distribution"][0]["name"] == "Customer Resolved"


# ─────────────────────── top_n truncation ───────────────────────


class TestTopNTruncation:
    def test_other_rollup_when_more_codes_than_top_n(self, monkeypatch):
        current = _resp({
            "code-resolved": 100,
            "code-callback":  60,
            "code-wrong":     40,
            "code-other":     20,
            "code-new":       10,
        })
        out, _ = _call_tool(
            {"interval": _INTERVAL, "include_trend": False, "top_n": 2},
            monkeypatch,
            current_resp=current,
            catalogue=_CATALOGUE,
        )
        dist = out["distribution"]
        assert len(dist) == 3
        assert dist[0]["name"] == "Customer Resolved"
        assert dist[1]["name"] == "Callback Requested"
        assert dist[-1]["name"] == "Other (truncated)"
        assert dist[-1]["count"] == 70
        assert out["totals"]["truncated"] is True

    def test_no_truncation_when_under_top_n(self, monkeypatch):
        current = _resp({"code-resolved": 10, "code-callback": 5})
        out, _ = _call_tool(
            {"interval": _INTERVAL, "include_trend": False, "top_n": 25},
            monkeypatch,
            current_resp=current,
            catalogue=_CATALOGUE,
        )
        assert out["totals"]["truncated"] is False
        assert all(r["name"] != "Other (truncated)" for r in out["distribution"])


# ─────────────────────── trend ───────────────────────


class TestTrend:
    def test_delta_pct_and_movement(self, monkeypatch):
        current = _resp({
            "code-resolved": 101,
            "code-callback": 60,
            "code-wrong":    40,
        })
        prior = _resp({
            "code-resolved": 100,
            "code-callback": 50,
            "code-wrong":    20,
        })
        out, _ = _call_tool(
            {"interval": _INTERVAL, "include_trend": True},
            monkeypatch,
            current_resp=current,
            prior_resp=prior,
            catalogue=_CATALOGUE,
        )
        by_name = {r["name"]: r for r in out["distribution"]}
        assert by_name["Customer Resolved"]["movement"] == "flat"
        assert by_name["Callback Requested"]["delta_pct"] == 20.0
        assert by_name["Callback Requested"]["movement"] == "up"
        assert by_name["Wrong Number"]["delta_pct"] == 100.0
        assert by_name["Wrong Number"]["movement"] == "up"

    def test_largest_movers_sorted_by_abs_delta_pct(self, monkeypatch):
        current = _resp({
            "code-callback": 60,
            "code-wrong":    40,
            "code-other":    10,
        })
        prior = _resp({
            "code-callback": 50,
            "code-wrong":    20,
            "code-other":    20,
        })
        out, _ = _call_tool(
            {"interval": _INTERVAL, "include_trend": True},
            monkeypatch,
            current_resp=current,
            prior_resp=prior,
            catalogue=_CATALOGUE,
        )
        movers = out["trend"]["largest_movers"]
        assert movers[0]["name"] == "Wrong Number"
        assert movers[0]["delta_pct"] == 100.0
        assert movers[1]["name"] == "Other Issue"
        assert movers[1]["delta_pct"] == -50.0
        assert movers[2]["name"] == "Callback Requested"

    def test_new_and_retired_codes_detected(self, monkeypatch):
        current = _resp({"code-resolved": 10, "code-new": 5})
        prior = _resp({"code-resolved": 10, "code-retired": 5})
        out, _ = _call_tool(
            {"interval": _INTERVAL, "include_trend": True},
            monkeypatch,
            current_resp=current,
            prior_resp=prior,
            catalogue=_CATALOGUE,
        )
        assert "New Promo Inquiry" in out["trend"]["new_codes_this_period"]
        assert "Old Workflow" in out["trend"]["retired_codes"]


# ─────────────────────── envelope + cache + edges ───────────────────────


class TestEnvelopeCacheAndEdges:
    def test_top_level_envelope(self, monkeypatch):
        out, _ = _call_tool(
            {"interval": _INTERVAL, "include_trend": False},
            monkeypatch,
            current_resp={"results": []},
            catalogue={},
        )
        assert out["interval"] == _INTERVAL
        assert "as_of_utc" in out
        assert out["as_of_utc"].endswith("Z")
        assert out["mode"] == "summary"
        assert out["filters"] == {
            "queue_ids": None, "user_ids": None, "media_types": None,
        }

    def test_empty_result_safe(self, monkeypatch):
        out, _ = _call_tool(
            {"interval": _INTERVAL, "include_trend": True},
            monkeypatch,
            current_resp={"results": []},
            prior_resp={"results": []},
            catalogue=_CATALOGUE,
        )
        assert out["totals"]["conversation_count"] == 0
        assert out["totals"]["distinct_code_count"] == 0
        assert out["distribution"] == []
        assert out["trend"]["largest_movers"] == []

    def test_invalid_mode_raises(self, monkeypatch):
        with pytest.raises(Exception, match="mode must be 'summary' or 'full'"):
            _call_tool(
                {"interval": _INTERVAL, "mode": "bogus"},
                monkeypatch,
                current_resp={"results": []},
                catalogue={},
            )


# ── v1.12.1: soft-fail envelope on 403 ──

class TestSoftFailEnvelope:
    """When the aggregates call 403s, the tool must return a canonical
    soft-fail envelope (status / kind / message) so the skill renders a
    visible 'missing scope' callout instead of inviting LLM-narrative."""

    def _call_with_aggregates_403(self, monkeypatch):
        import asyncio
        import json as _json
        import PureCloudPlatformClientV2 as gc
        from PureCloudPlatformClientV2.rest import ApiException
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import wrapup
        from mcp.server.fastmcp import FastMCP

        monkeypatch.setattr(wrapup, "to_dict", _fake_to_dict)

        class FakeAnalyticsApi:
            def __init__(self, *a, **k): pass
            def post_analytics_conversations_aggregates_query(self, body):
                raise ApiException(
                    status=403,
                    reason="Forbidden — missing analytics:conversationAggregate:view",
                )

        class FakeRoutingApi:
            def __init__(self, *a, **k): pass
            def get_routing_wrapupcodes(self, **k):
                class _Empty:
                    entities = []
                return _Empty()

        monkeypatch.setattr(wrapup.gc, "AnalyticsApi", FakeAnalyticsApi)
        monkeypatch.setattr(wrapup.gc, "RoutingApi", FakeRoutingApi)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        app = FastMCP(name="t")
        wrapup.register(app)
        result = asyncio.run(app.call_tool(
            "wrap_up_code_distribution",
            {"interval": _INTERVAL, "include_trend": True},
        ))
        text = getattr(result[0], "text", None) or result[0].get("text")
        return _json.loads(text)

    def test_403_returns_canonical_envelope(self, monkeypatch):
        out = self._call_with_aggregates_403(monkeypatch)
        # Must match the v1.3 canonical envelope shape exactly.
        assert out["status"] == 403
        assert out["kind"] == "wrap_up_code_distribution"
        assert "analytics:conversationAggregate:view" in out["message"]
        # No distribution field — soft-fail short-circuits the success path.
        assert "distribution" not in out
        # Interval echoed so callers know what query failed.
        assert out["interval"] == _INTERVAL
