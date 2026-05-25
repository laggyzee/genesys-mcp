"""Pin the canonical Genesys-UI filter shapes used by analytics-backed tools.

The v0.2 UI-parity work was load-bearing: `queue_performance` and
`agent_performance` numbers only match the Genesys "Performance > Queues" /
"Performance > Agents" UI when the filter body is shaped as an **outer `and`
of `or` clauses** (one clause per dimension), not a flat top-level OR. The
pre-v0.2 flat-OR shape silently undercounted by up to 8x.

These tests **pin that shape so it can't silently regress.** They don't hit
the live API — we monkey-patch the SDK call to capture the body argument
and assert its structure.

If a future refactor restructures the filter body and these tests fail,
that's the signal to verify against the Genesys UI before merging — the
shape change might be intentional but it needs a deliberate fixture
refresh and live spot-check.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


@pytest.fixture
def captured_aggregate_body(monkeypatch: pytest.MonkeyPatch):
    """Monkey-patch the Genesys analytics-aggregates SDK call; capture the body.

    Yields a list-of-bodies — tests can run multiple tool calls and inspect
    each captured body independently.
    """
    import PureCloudPlatformClientV2 as gc

    captured: list[Any] = []

    def fake_aggregates(self, body, **kwargs):
        captured.append(body)
        # Tests only care about the body that was about to be sent.
        # Raise immediately so downstream parsing doesn't matter — each
        # test wraps its tool call in try/except.
        raise RuntimeError("captured-and-stopped (test fixture)")

    monkeypatch.setattr(
        gc.AnalyticsApi,
        "post_analytics_conversations_aggregates_query",
        fake_aggregates,
    )

    # Also mock get_api so we don't need real OAuth creds for these tests.
    from genesys_mcp import client as gen_client
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

    return captured


def _call_tool_capturing(register_fn, tool_name: str, args: dict) -> None:
    """Helper: register, invoke a tool, swallow the fixture-raised exception.

    The fake_aggregates fixture raises after capturing — that's fine; we just
    need the body in ``captured``. Any other tools the body-capture path
    traverses may also fail (e.g. coaching_pack calls users_api after the
    aggregates query); we accept that and rely on the captured body.
    """
    import asyncio
    test_mcp = FastMCP(name="t")
    register_fn(test_mcp)
    try:
        asyncio.run(test_mcp.call_tool(tool_name, args))
    except Exception:
        # Expected — the fixture's fake_aggregates raises after capture.
        pass


def _filter_has_canonical_shape(body: dict) -> bool:
    """Verify the `filter` is the canonical outer-AND of OR clauses.

    Shape:
        {"type": "and", "clauses": [{"type": "or", "predicates": [...]}, ...]}

    NOT:
        {"type": "or", "predicates": [...]}    # the broken v0.1 shape
        {"type": "or", "clauses": [...]}       # any other flat shape
    """
    f = body.get("filter") or {}
    if f.get("type") != "and":
        return False
    clauses = f.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        return False
    return all(
        c.get("type") == "or" and isinstance(c.get("predicates"), list)
        for c in clauses
    )


# ── Filter-shape tests ──

class TestQueuePerformanceFilter:
    """queue_performance filter shape pins the v0.2 fix."""

    def test_filter_is_outer_and_of_or(self, captured_aggregate_body):
        from genesys_mcp.tools import analytics
        _call_tool_capturing(analytics.register, "queue_performance", {
            "queue_ids": ["q1", "q2", "q3"],
            "interval": "2026-05-19T14:00:00.000Z/2026-05-20T14:00:00.000Z",
        })
        assert captured_aggregate_body, "tool didn't call the analytics API"
        body = captured_aggregate_body[0]
        assert _filter_has_canonical_shape(body), (
            f"queue_performance filter is NOT in canonical and+or shape — "
            f"this is the v0.2 UI-parity regression sentinel. Got: {body.get('filter')}"
        )

    def test_predicate_dimension_is_queueid(self, captured_aggregate_body):
        from genesys_mcp.tools import analytics
        _call_tool_capturing(analytics.register, "queue_performance", {
            "queue_ids": ["q1", "q2"],
            "interval": "2026-05-19T14:00:00.000Z/2026-05-20T14:00:00.000Z",
        })
        body = captured_aggregate_body[0]
        first_clause = body["filter"]["clauses"][0]
        for pred in first_clause["predicates"]:
            assert pred["dimension"] == "queueId"
        values = {p["value"] for p in first_clause["predicates"]}
        assert values == {"q1", "q2"}

    def test_groupby_includes_queueid(self, captured_aggregate_body):
        from genesys_mcp.tools import analytics
        _call_tool_capturing(analytics.register, "queue_performance", {
            "queue_ids": ["q1"],
            "interval": "2026-05-19T14:00:00.000Z/2026-05-20T14:00:00.000Z",
        })
        body = captured_aggregate_body[0]
        assert "queueId" in body["groupBy"]

    def test_canonical_metrics_present(self, captured_aggregate_body):
        """tAnswered.count is the canonical 'Answer' column — must be in metrics."""
        from genesys_mcp.tools import analytics
        _call_tool_capturing(analytics.register, "queue_performance", {
            "queue_ids": ["q1"],
            "interval": "2026-05-19T14:00:00.000Z/2026-05-20T14:00:00.000Z",
        })
        body = captured_aggregate_body[0]
        metrics = set(body["metrics"])
        # These four are the load-bearing metrics for UI parity. Removing
        # any of them silently breaks the matching aggregator.
        assert "tAnswered" in metrics
        assert "tHandle" in metrics
        assert "nOffered" in metrics
        assert "nOverSla" in metrics


class TestAgentPerformanceFilter:
    """agent_performance filter shape pins the v0.2 fix.

    For agent_performance the filter clause carries userId predicates;
    mediaType becomes part of groupBy. Same outer and+or shape applies.
    """

    def test_filter_is_outer_and_of_or(self, captured_aggregate_body):
        from genesys_mcp.tools import analytics
        _call_tool_capturing(analytics.register, "agent_performance", {
            "user_ids": ["u1", "u2", "u3"],
            "interval": "2026-05-19T14:00:00.000Z/2026-05-20T14:00:00.000Z",
        })
        assert captured_aggregate_body, "tool didn't call the analytics API"
        body = captured_aggregate_body[0]
        assert _filter_has_canonical_shape(body), (
            f"agent_performance filter is NOT in canonical and+or shape — "
            f"this is the v0.2 UI-parity regression sentinel. Got: {body.get('filter')}"
        )

    def test_predicate_dimension_is_userid(self, captured_aggregate_body):
        from genesys_mcp.tools import analytics
        _call_tool_capturing(analytics.register, "agent_performance", {
            "user_ids": ["u1", "u2"],
            "interval": "2026-05-19T14:00:00.000Z/2026-05-20T14:00:00.000Z",
        })
        body = captured_aggregate_body[0]
        first_clause = body["filter"]["clauses"][0]
        for pred in first_clause["predicates"]:
            assert pred["dimension"] == "userId"

    def test_groupby_includes_userid_and_media(self, captured_aggregate_body):
        from genesys_mcp.tools import analytics
        _call_tool_capturing(analytics.register, "agent_performance", {
            "user_ids": ["u1"],
            "interval": "2026-05-19T14:00:00.000Z/2026-05-20T14:00:00.000Z",
        })
        body = captured_aggregate_body[0]
        # v0.2 split-out: per-(userId, mediaType) auto-grouping in the
        # response. If this changes, the by_media block in the response
        # falls apart and downstream aggregators break.
        assert body["groupBy"] == ["userId", "mediaType"]

    def test_canonical_metrics_include_tanswered_and_thandle(self, captured_aggregate_body):
        from genesys_mcp.tools import analytics
        _call_tool_capturing(analytics.register, "agent_performance", {
            "user_ids": ["u1"],
            "interval": "2026-05-19T14:00:00.000Z/2026-05-20T14:00:00.000Z",
        })
        body = captured_aggregate_body[0]
        metrics = set(body["metrics"])
        # tAnswered.count = UI "Answer" column. tHandle.count = "Handle".
        # Lose these and we lose UI parity.
        assert "tAnswered" in metrics
        assert "tHandle" in metrics


class TestAgentCoachingPackFilter:
    """agent_coaching_pack uses the same and+or+or shape for target + peers."""

    def test_filter_carries_both_user_and_media_clauses(self, captured_aggregate_body):
        from genesys_mcp.tools import coaching
        _call_tool_capturing(coaching.register, "agent_coaching_pack", {
            "user_id": "u1",
            "interval": "2026-05-19T14:00:00.000Z/2026-05-20T14:00:00.000Z",
            "peer_user_ids": ["u2", "u3"],
            "flagged_calls_limit": 5,
        })
        assert captured_aggregate_body, "coaching pack didn't reach the analytics query"
        body = captured_aggregate_body[0]
        assert _filter_has_canonical_shape(body)
        # Two clauses: userId list AND mediaType list — coaching_pack
        # specifically filters by both
        clauses = body["filter"]["clauses"]
        assert len(clauses) >= 2, (
            "coaching_pack filter should have both userId AND mediaType "
            f"clauses; got {len(clauses)}"
        )
        dims = {c["predicates"][0]["dimension"] for c in clauses
                if c.get("predicates")}
        assert "userId" in dims
        assert "mediaType" in dims


class TestAggregatesForUsersAccumulation:
    """Pin the multi-bucket accumulation in _aggregates_for_users.

    The coaching pack queries with granularity=P7D, so a 24-day interval
    returns ~4 buckets per (userId, mediaType). A pre-v0.9.1 bug overwrote
    instead of accumulating — Section 1 KPIs read ~1/N of the true volume.
    This test pins the fix: per-bucket counts and sums must accumulate.
    """

    def test_multi_bucket_counts_and_sums_accumulate(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools.coaching import _aggregates_for_users

        # Synthetic response: 4 P7D buckets for one user × voice media.
        # tAnswered.count totals: 100 + 100 + 100 + 95 = 395 (Jasmine's
        # real voice answered for the 1-24 May 2026 interval).
        # tHandle.sum totals: 4 × 32_700_000 ms ≈ 36.3 hours.
        fake_response = {
            "results": [{
                "group": {"userId": "u1", "mediaType": "voice"},
                "data": [
                    {"metrics": [
                        {"metric": "tAnswered", "stats": {"count": 100}},
                        {"metric": "tHandle", "stats": {"count": 100, "sum": 32_700_000}},
                    ]},
                    {"metrics": [
                        {"metric": "tAnswered", "stats": {"count": 100}},
                        {"metric": "tHandle", "stats": {"count": 100, "sum": 32_700_000}},
                    ]},
                    {"metrics": [
                        {"metric": "tAnswered", "stats": {"count": 100}},
                        {"metric": "tHandle", "stats": {"count": 100, "sum": 32_700_000}},
                    ]},
                    {"metrics": [
                        {"metric": "tAnswered", "stats": {"count": 95}},
                        {"metric": "tHandle", "stats": {"count": 95, "sum": 32_700_000}},
                    ]},
                ],
            }],
        }

        def fake_aggregates(self, body, **kwargs):
            return fake_response

        monkeypatch.setattr(
            gc.AnalyticsApi,
            "post_analytics_conversations_aggregates_query",
            fake_aggregates,
        )
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        stats = _aggregates_for_users(["u1"], "2026-04-30T14:00:00.000Z/2026-05-24T14:00:00.000Z")

        # Critical assertion: counts SUM across buckets, not overwrite.
        # Pre-fix this returned 95 (last bucket only).
        assert stats["u1"]["voice"]["tAnswered"]["count"] == 395
        assert stats["u1"]["voice"]["tHandle"]["count"] == 395
        assert stats["u1"]["voice"]["tHandle"]["sum"] == 4 * 32_700_000

    def test_min_max_combine_across_buckets(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools.coaching import _aggregates_for_users

        fake_response = {
            "results": [{
                "group": {"userId": "u1", "mediaType": "voice"},
                "data": [
                    {"metrics": [{"metric": "tHandle", "stats": {
                        "count": 10, "sum": 100, "min": 5.0, "max": 50.0,
                    }}]},
                    {"metrics": [{"metric": "tHandle", "stats": {
                        "count": 10, "sum": 100, "min": 2.0, "max": 75.0,
                    }}]},
                ],
            }],
        }
        monkeypatch.setattr(
            gc.AnalyticsApi,
            "post_analytics_conversations_aggregates_query",
            lambda self, body, **kw: fake_response,
        )
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        stats = _aggregates_for_users(["u1"], "2026-05-01/2026-05-24")
        slot = stats["u1"]["voice"]["tHandle"]
        assert slot["min"] == 2.0
        assert slot["max"] == 75.0
        assert slot["count"] == 20
        assert slot["sum"] == 200
