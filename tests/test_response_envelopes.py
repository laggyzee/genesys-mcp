"""Pin v1.5 top-level ``interval`` + ``as_of_utc`` echo on the 4 analytical tools.

The persisted-output bug that triggered v1.5: a foreign LLM read a saved
``queue_performance`` response file and couldn't see what interval it
covered (the field was buried 4 levels deep). It then hallucinated a
non-existent constraint ("Genesys can't slice to a calendar day"). The
fix is to surface ``interval`` and ``as_of_utc`` at the **top** of every
analytical response so a reader of the first 10 lines sees the window.

These tests pin that fix on all four analytical tools so a future refactor
can't silently strip the echo and re-introduce the cross-app failure mode.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re

import pytest


def _fake_to_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, list):
        return obj
    return obj


def _call_tool(register_fn, tool_name, args, monkeypatch, *, sdk_patches):
    """Boilerplate: register tool, monkeypatch SDK pieces, invoke, return parsed JSON."""
    import PureCloudPlatformClientV2 as gc
    from genesys_mcp import client as gen_client
    from genesys_mcp.tools import (
        analytics, conversations, coaching, directory,
        external_contacts, presence, reports, routing,
        speech_analytics, wfm,
    )
    from mcp.server.fastmcp import FastMCP

    for mod in (analytics, conversations, coaching, directory,
                external_contacts, presence, reports, routing,
                speech_analytics, wfm):
        if hasattr(mod, "to_dict"):
            monkeypatch.setattr(mod, "to_dict", _fake_to_dict)

    for module, attr, value in sdk_patches:
        monkeypatch.setattr(module, attr, value)
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

    app = FastMCP(name="t")
    register_fn(app)
    result = asyncio.run(app.call_tool(tool_name, args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text)


_TEST_INTERVAL = "2026-05-19T14:00:00.000Z/2026-05-20T14:00:00.000Z"


# ─────────────────────── queue_performance ───────────────────────


class TestQueuePerformanceEchoes:
    """queue_performance had NEITHER interval nor as_of_utc at top level pre-v1.5."""

    def test_top_level_interval_echoed(self, monkeypatch):
        from genesys_mcp.tools import analytics as a

        class FakeAnalyticsApi:
            def __init__(self, *a, **k): pass
            def post_analytics_conversations_aggregates_query(self, body):
                return {"results": []}

        out = _call_tool(
            a.register, "queue_performance",
            {"queue_ids": ["q1"], "interval": _TEST_INTERVAL},
            monkeypatch, sdk_patches=[(a.gc, "AnalyticsApi", FakeAnalyticsApi)],
        )
        assert out["interval"] == _TEST_INTERVAL, (
            "v1.5 contract: queue_performance must echo `interval` at top "
            "level. A reader of the persisted file must see the window in "
            "the first lines, not buried under results[].data[]."
        )

    def test_top_level_as_of_utc_echoed(self, monkeypatch):
        from genesys_mcp.tools import analytics as a

        class FakeAnalyticsApi:
            def __init__(self, *a, **k): pass
            def post_analytics_conversations_aggregates_query(self, body):
                return {"results": []}

        out = _call_tool(
            a.register, "queue_performance",
            {"queue_ids": ["q1"], "interval": _TEST_INTERVAL},
            monkeypatch, sdk_patches=[(a.gc, "AnalyticsApi", FakeAnalyticsApi)],
        )
        assert "as_of_utc" in out
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", out["as_of_utc"])
        assert out["as_of_utc"].endswith("Z")

    def test_granularity_echoed_too(self, monkeypatch):
        from genesys_mcp.tools import analytics as a

        class FakeAnalyticsApi:
            def __init__(self, *a, **k): pass
            def post_analytics_conversations_aggregates_query(self, body):
                return {"results": []}

        out = _call_tool(
            a.register, "queue_performance",
            {"queue_ids": ["q1"], "interval": _TEST_INTERVAL, "granularity": "P1D"},
            monkeypatch, sdk_patches=[(a.gc, "AnalyticsApi", FakeAnalyticsApi)],
        )
        assert out["granularity"] == "P1D"


# ─────────────────────── agent_performance ───────────────────────


class TestAgentPerformanceEchoes:
    """agent_performance had interval at top but NOT as_of_utc pre-v1.5."""

    def test_top_level_interval_present(self, monkeypatch):
        from genesys_mcp.tools import analytics as a

        class FakeAnalyticsApi:
            def __init__(self, *a, **k): pass
            def post_analytics_conversations_aggregates_query(self, body):
                return {"results": []}

        out = _call_tool(
            a.register, "agent_performance",
            {"user_ids": ["u1"], "interval": _TEST_INTERVAL},
            monkeypatch, sdk_patches=[(a.gc, "AnalyticsApi", FakeAnalyticsApi)],
        )
        assert out["interval"] == _TEST_INTERVAL

    def test_top_level_as_of_utc_added(self, monkeypatch):
        from genesys_mcp.tools import analytics as a

        class FakeAnalyticsApi:
            def __init__(self, *a, **k): pass
            def post_analytics_conversations_aggregates_query(self, body):
                return {"results": []}

        out = _call_tool(
            a.register, "agent_performance",
            {"user_ids": ["u1"], "interval": _TEST_INTERVAL},
            monkeypatch, sdk_patches=[(a.gc, "AnalyticsApi", FakeAnalyticsApi)],
        )
        assert "as_of_utc" in out
        assert out["as_of_utc"].endswith("Z")


# ─────────────────────── deep_dive + break_overrun source sentinels ─────


class TestReportToolEchoSentinels:
    """Source-level pins for tools whose end-to-end mocking is invasive.

    ``repeat_caller_deep_dive`` and ``break_overrun_report`` both run the
    async jobs flow (post → poll → paginate), which would need a 5+ class
    mock chain to drive to a return. The regression we actually care about
    is "did someone delete the as_of_utc emission line" — a source check
    catches that with no mocking fragility.
    """

    def test_repeat_caller_deep_dive_emits_as_of_utc(self):
        from genesys_mcp.tools import reports
        src = inspect.getsource(reports)
        assert '"as_of_utc": _now_utc()' in src, (
            "repeat_caller_deep_dive must echo as_of_utc at top level "
            "(v1.5 contract)."
        )

    def test_break_overrun_report_emits_as_of_utc(self):
        from genesys_mcp.tools import reports
        src = inspect.getsource(reports)
        # Both repeat_caller_deep_dive and break_overrun_report use the same
        # emission pattern — assert two occurrences (one per function).
        emission_count = src.count('"as_of_utc": _now_utc()')
        assert emission_count >= 2, (
            f"Expected as_of_utc emission in both repeat_caller_deep_dive "
            f"AND break_overrun_report (v1.5 contract). Found "
            f"{emission_count} occurrence(s) in reports.py source."
        )


# ─────────────────────── compute_interval registered ───────────────────────


class TestComputeIntervalRegistered:
    """v1.5: ``compute_interval`` is the new tool that gives clients an
    obvious path from a period keyword to an ISO interval.
    """

    def test_returns_paste_ready_interval(self, monkeypatch):
        from genesys_mcp.tools import directory as d
        out = _call_tool(
            d.register, "compute_interval",
            {"period": "today", "timezone": "Australia/Sydney"},
            monkeypatch, sdk_patches=[],
        )
        assert "interval" in out
        assert "/" in out["interval"]
        assert out["interval"].split("/")[0].endswith("Z")
        assert out["period"] == "today"
        assert out["timezone"] == "Australia/Sydney"

    def test_unknown_period_returns_error_envelope(self, monkeypatch):
        from genesys_mcp.tools import directory as d
        out = _call_tool(
            d.register, "compute_interval",
            {"period": "yesteryear", "timezone": "Australia/Sydney"},
            monkeypatch, sdk_patches=[],
        )
        assert out["status"] == "error"
        assert out["kind"] == "invalid_argument"
        assert "supported_periods" in out
        assert "today" in out["supported_periods"]
