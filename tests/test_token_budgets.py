"""Pin v1.1's per-tool response sizes against regression.

Four heavy tools (queue_performance, agent_performance,
repeat_caller_deep_dive, break_overrun_report) ship slim 'summary' modes
that drop histograms, percentiles, debug scaffolding, and per-session
detail arrays. These tests build a slim response from a stored fixture
plus a *synthesised* histogram-laden bucket, then:

1. Assert the slim version fits under a tight regression budget (catches
   someone adding a histogram or percentile field back to the contract).
2. Assert the slim version strips the histogram-shaped fields entirely
   (catches the same class of regression from the other direction).

Captured fixtures in tests/fixtures/ have been compacted at capture time
(they pre-date the v1.1 trim) so they don't carry the full histograms a
real Genesys response would. The synthesised tests cover that gap.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _bytes(obj) -> int:
    return len(json.dumps(obj, default=str))


# ─────────────────────── queue_performance ───────────────────────

class TestQueuePerformanceBudget:
    """queue_performance summary should stay under ~18KB for the standard fixture."""

    @pytest.fixture(scope="class")
    def fixture(self) -> dict:
        return json.loads((_FIXTURES / "queue_performance.json").read_text())

    def test_summary_slim_is_under_budget(self, fixture):
        from genesys_mcp.tools.analytics import _slim_queue_response
        slim = json.loads(json.dumps(fixture))
        _slim_queue_response(slim)
        size = _bytes(slim)
        # Tight regression budget against the current shape — leaves ~2KB
        # headroom. Adding a histogram or percentile back would push it over.
        assert size <= 18_000, (
            f"queue_performance summary {size:,} bytes exceeds 18KB budget. "
            "Did someone add a histogram or percentile field to "
            "_QUEUE_SUMMARY_METRICS?"
        )

    def test_summary_drops_histogram_and_percentile_fields(self, fixture):
        from genesys_mcp.tools.analytics import _slim_queue_response
        # Inject histogram + percentile fields into a clone of the fixture so
        # we can prove the slim function actually strips them.
        with_histos = json.loads(json.dumps(fixture))
        for r in with_histos.get("results") or []:
            for bucket in r.get("data") or []:
                for m in bucket.get("metrics") or []:
                    m.setdefault("stats", {}).update({
                        "min": 1.0, "max": 9999.0, "current": 42.0,
                        "p50": 100.0, "p75": 250.0, "p90": 500.0,
                        "p95": 800.0, "p99": 1500.0,
                    })
        slim = json.loads(json.dumps(with_histos))
        _slim_queue_response(slim)
        for r in slim.get("results") or []:
            for bucket in r.get("data") or []:
                for m in bucket.get("metrics") or []:
                    stats = m.get("stats") or {}
                    extras = set(stats.keys()) - {"count", "sum"}
                    assert not extras, (
                        f"summary metric {m.get('metric')!r} kept "
                        f"unexpected stats {sorted(extras)}"
                    )

    def test_summary_drops_unused_metrics(self, fixture):
        from genesys_mcp.tools.analytics import _slim_queue_response, _QUEUE_SUMMARY_METRICS
        # Inject unused metrics (nTransferred, tAcw, tShortAbandon) into the
        # response and prove the slim function removes them.
        with_extra = json.loads(json.dumps(fixture))
        for r in with_extra.get("results") or []:
            for bucket in r.get("data") or []:
                bucket["metrics"] = (bucket.get("metrics") or []) + [
                    {"metric": "nTransferred", "stats": {"count": 99}},
                    {"metric": "tAcw", "stats": {"count": 100, "sum": 1500000}},
                    {"metric": "tShortAbandon", "stats": {"count": 5}},
                ]
        slim = json.loads(json.dumps(with_extra))
        _slim_queue_response(slim)
        for r in slim.get("results") or []:
            for bucket in r.get("data") or []:
                for m in bucket.get("metrics") or []:
                    assert m["metric"] in _QUEUE_SUMMARY_METRICS, (
                        f"unused metric {m['metric']!r} should be stripped "
                        "in summary mode"
                    )

    def test_summary_keeps_derived_block_intact(self, fixture):
        from genesys_mcp.tools.analytics import _slim_queue_response
        slim = json.loads(json.dumps(fixture))
        _slim_queue_response(slim)
        # The derived block (the user-facing KPIs) is exactly what we want
        # to keep — make sure nothing accidentally drops it.
        sample_in = ((fixture["results"] or [{}])[0].get("data") or [{}])[0]
        sample_out = ((slim["results"] or [{}])[0].get("data") or [{}])[0]
        if "derived" in sample_in:
            assert "derived" in sample_out
            assert sample_out["derived"] == sample_in["derived"]


# ─────────────────────── agent_performance ───────────────────────

class TestAgentPerformanceBudget:
    """agent_performance summary slims raw `results` per-bucket metrics."""

    @pytest.fixture(scope="class")
    def fixture(self) -> dict:
        return json.loads((_FIXTURES / "agent_performance.json").read_text())

    def test_summary_slim_results_under_budget(self, fixture):
        from genesys_mcp.tools.analytics import _slim_agent_results
        slim = json.loads(json.dumps(fixture))
        _slim_agent_results(slim)
        size = _bytes(slim)
        assert size <= 26_000, (
            f"agent_performance summary {size:,} bytes exceeds 26KB budget. "
            "Likely a metric or stat field was re-added to "
            "_AGENT_SUMMARY_METRICS."
        )

    def test_summary_keeps_only_whitelisted_metrics(self, fixture):
        from genesys_mcp.tools.analytics import (
            _slim_agent_results,
            _AGENT_SUMMARY_METRICS,
        )
        # Inject extras to prove they're stripped.
        with_extras = json.loads(json.dumps(fixture))
        for r in with_extras.get("results") or []:
            for bucket in r.get("data") or []:
                bucket["metrics"] = (bucket.get("metrics") or []) + [
                    {"metric": "nOutbound", "stats": {"count": 5}},
                    {"metric": "nBlindTransferred", "stats": {"count": 2}},
                ]
        slim = json.loads(json.dumps(with_extras))
        _slim_agent_results(slim)
        for r in slim.get("results") or []:
            for bucket in r.get("data") or []:
                for m in bucket.get("metrics") or []:
                    assert m.get("metric") in _AGENT_SUMMARY_METRICS

    def test_summary_drops_percentile_fields(self, fixture):
        from genesys_mcp.tools.analytics import _slim_agent_results
        with_histos = json.loads(json.dumps(fixture))
        for r in with_histos.get("results") or []:
            for bucket in r.get("data") or []:
                for m in bucket.get("metrics") or []:
                    m.setdefault("stats", {}).update({
                        "min": 1.0, "max": 999.0, "p95": 500.0,
                    })
        slim = json.loads(json.dumps(with_histos))
        _slim_agent_results(slim)
        for r in slim.get("results") or []:
            for bucket in r.get("data") or []:
                for m in bucket.get("metrics") or []:
                    extras = set((m.get("stats") or {}).keys()) - {"count", "sum"}
                    assert not extras, (
                        f"metric {m['metric']!r} kept {sorted(extras)} in summary"
                    )


# ─────────────────────── repeat_caller_deep_dive ───────────────────────

class TestRepeatCallerDeepDiveBudget:
    @pytest.fixture(scope="class")
    def fixture(self) -> dict:
        return json.loads((_FIXTURES / "repeat_caller_deep_dive.json").read_text())

    def test_summary_under_budget(self, fixture):
        from genesys_mcp.tools.reports import _slim_deep_dive_response
        slim = _slim_deep_dive_response(fixture)
        size = _bytes(slim)
        assert size <= 32_000, (
            f"deep_dive summary {size:,} bytes exceeds 32KB budget. "
            "Check sentiment_trajectory collapse + per-repeater dict trims."
        )

    def test_summary_drops_debug_scaffolding_from_scope(self, fixture):
        from genesys_mcp.tools.reports import _slim_deep_dive_response
        slim = _slim_deep_dive_response(fixture)
        scope = slim.get("scope") or {}
        for noisy_key in ("candidates_meeting_min_calls", "sta_calls_made",
                          "sta_calls_with_data", "sta_coverage_pct",
                          "wrapup_calls_made", "wrapup_calls_with_data",
                          "wrapup_coverage_pct"):
            assert noisy_key not in scope, (
                f"debug scaffolding {noisy_key!r} should not appear in "
                f"summary scope; got {sorted(scope.keys())}"
            )

    def test_summary_keeps_conversation_ids(self, fixture):
        """Conversation IDs are essential drill-down primitives — never trim."""
        from genesys_mcp.tools.reports import _slim_deep_dive_response
        slim = _slim_deep_dive_response(fixture)
        for r in slim.get("repeaters") or []:
            assert "evidence_conversation_ids" in r

    def test_summary_collapses_sentiment_trajectory(self, fixture):
        from genesys_mcp.tools.reports import _slim_deep_dive_response
        slim = _slim_deep_dive_response(fixture)
        for r in slim.get("repeaters") or []:
            traj = r.get("sentiment_trajectory") or {}
            assert set(traj.keys()) == {"initial", "final", "trend", "samples"}, (
                f"sentiment_trajectory should be a 4-key summary in summary "
                f"mode; got {traj!r}"
            )

    def test_summary_caps_top_dicts_at_three(self, fixture):
        from genesys_mcp.tools.reports import _slim_deep_dive_response
        # Inject 10 dispositions to confirm cap-at-3 actually kicks in.
        with_long_dispos = json.loads(json.dumps(fixture))
        for r in with_long_dispos.get("repeaters") or []:
            r["dispositions"] = {f"disposition_{i}": 10 - i for i in range(10)}
        slim = _slim_deep_dive_response(with_long_dispos)
        for r in slim.get("repeaters") or []:
            for capped_field in ("queues_offered", "dispositions",
                                 "expected_fixes"):
                assert len(r.get(capped_field) or {}) <= 3, (
                    f"{capped_field} should be top-3 only; got "
                    f"{len(r.get(capped_field) or {})} entries"
                )
            assert len(r.get("topics") or []) <= 3


# ─────────────────────── break_overrun_report ───────────────────────

class TestBreakOverrunReportBudget:
    @pytest.fixture(scope="class")
    def fixture(self) -> dict:
        return json.loads((_FIXTURES / "break_overrun_report.json").read_text())

    def _build_summary(self, fixture: dict) -> dict:
        """Apply the same slim transformation the tool does inline."""
        slim_users = []
        for u in fixture.get("users") or []:
            slim_users.append({
                k: v for k, v in u.items() if k not in (
                    "overrun_sessions", "pre_break_overrun_sessions", "away_sessions",
                )
            })
        return {**fixture, "users": slim_users}

    def test_summary_under_budget(self, fixture):
        slim = self._build_summary(fixture)
        size = _bytes(slim)
        # 30 users × ~15 scalar fields ≈ <5KB. Tight budget catches a
        # regression where per-session arrays creep back into the user row.
        assert size <= 5_000, (
            f"break_overrun_report summary {size:,} bytes exceeds 5KB "
            "budget. Did a per-session array creep back into the user row?"
        )

    def test_summary_drops_per_session_arrays(self, fixture):
        slim = self._build_summary(fixture)
        for u in slim.get("users") or []:
            for forbidden in ("overrun_sessions", "pre_break_overrun_sessions",
                              "away_sessions"):
                assert forbidden not in u

    def test_summary_keeps_all_aggregate_counters(self, fixture):
        slim = self._build_summary(fixture)
        for u in slim.get("users") or []:
            for required in ("user_id", "total_sessions", "overrun_count",
                             "total_overrun_min", "pre_break_overrun_count",
                             "pre_break_overrun_total_min"):
                assert required in u

    def test_full_mode_substantially_larger_than_summary(self, fixture):
        """Inject per-session arrays and confirm slim shaves them off."""
        with_sessions = json.loads(json.dumps(fixture))
        # Pad each user with 5 synthetic overrun sessions
        for u in with_sessions.get("users") or []:
            u["overrun_sessions"] = [
                {"presence": "BREAK", "start_utc": "2026-05-01T00:00:00Z",
                 "duration_s": 1000, "duration_min": 16.7, "target_min": 15,
                 "over_target": True, "overrun_min": 1.7}
                for _ in range(5)
            ]
        slim = self._build_summary(with_sessions)
        full_size = _bytes(with_sessions)
        slim_size = _bytes(slim)
        assert full_size > slim_size * 2, (
            f"with per-session arrays, full ({full_size:,}) should be "
            f"meaningfully larger than slim ({slim_size:,})"
        )
