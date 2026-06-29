"""Regression tests for cc-daily-brief aggregators — pins the v0.9.2 fixes.

Four bugs surfaced when running the brief against a live tenant for Monday
25 May 2026; every Section 3/4/5 row was silently filtered out. Each fix is
pinned here so a future refactor can't reintroduce the same shape mismatch.
"""
from __future__ import annotations

import pytest


class TestFlaggedAgentsReadsRawMetrics:
    """`agent_performance` results don't carry a `derived` block.

    Pre-v0.9.2 the function read ``data[0].derived = {answered, avg_handle_s}``
    and silently filtered every agent (the key didn't exist). The fix reads
    raw ``tAnswered.count`` + ``tHandle.sum/count`` directly. This test pins
    that shape so a future refactor can't reintroduce the bug.
    """

    def _agent_perf_payload(self, uid: str, answered: int, handle_count: int, handle_sum_ms: int) -> dict:
        return {
            "results": [{
                "group": {"userId": uid, "mediaType": "voice"},
                "data": [{
                    "metrics": [
                        {"metric": "tAnswered", "stats": {"count": answered}},
                        {"metric": "tHandle", "stats": {"count": handle_count, "sum": handle_sum_ms}},
                    ],
                }],
            }],
        }

    def test_flags_agent_when_aht_exceeds_threshold(self, build_report_daily):
        # 19 voice answered, 23 min average handle = 1380s vs 285s target = +384%
        payload = self._agent_perf_payload("u1", answered=19, handle_count=19, handle_sum_ms=26_220_000)
        rows = build_report_daily.flagged_agents(
            payload, voice_aht_target_s=285, aht_excess_pct_threshold=15.0,
            user_names={"u1": "Test Agent"},
        )
        assert len(rows) == 1
        assert rows[0]["voice_answered"] == 19
        assert rows[0]["voice_aht_s"] == pytest.approx(1380, abs=1)
        # Excess minutes = (1380 - 285) * 19 / 60 ≈ 346.75 min
        assert rows[0]["voice_excess_minutes"] == pytest.approx(346.75, abs=0.5)

    def test_skips_agents_under_five_voice_calls(self, build_report_daily):
        payload = self._agent_perf_payload("u1", answered=4, handle_count=4, handle_sum_ms=10_000_000)
        rows = build_report_daily.flagged_agents(
            payload, voice_aht_target_s=285, aht_excess_pct_threshold=15.0,
        )
        assert rows == []

    def test_returns_empty_on_pre_v092_derived_only_payload(self, build_report_daily):
        # The buggy payload shape: nothing in metrics, only a `derived` block.
        # Pre-fix: silently picked up `derived.answered`. Post-fix: returns 0
        # answered because metrics are empty. This guards against accidental
        # re-introduction of derived-block reads.
        payload = {
            "results": [{
                "group": {"userId": "u1", "mediaType": "voice"},
                "data": [{"derived": {"answered": 19, "avg_handle_s": 1380}, "metrics": []}],
            }],
        }
        rows = build_report_daily.flagged_agents(
            payload, voice_aht_target_s=285, aht_excess_pct_threshold=15.0,
        )
        assert rows == [], "must read raw metrics, never a derived block"


class TestAdherenceFlagsIncludesPreBreak:
    """Pre-v0.9.2 the threshold gated on ``total_overrun_min`` only.

    Agents with a 40-minute pre-break parking session but zero break/meal
    overrun (a recurring real-tenant pattern) were silently dropped.
    """

    def test_pre_break_only_agent_flagged_when_combined_over_threshold(self, build_report_daily):
        brk = {"users": [
            {"user_id": "u1", "user_name": "PB Only",
             "total_overrun_min": 0, "pre_break_overrun_total_min": 42.5,
             "away_total_min": 0, "overrun_count": 0, "pre_break_overrun_count": 1},
        ]}
        rows = build_report_daily.adherence_flags(brk, overrun_min_threshold=30)
        assert len(rows) == 1
        assert rows[0]["pre_break_overrun_min"] == 42.5
        assert rows[0]["total_min"] == 42.5

    def test_combined_break_meal_plus_pre_break_crosses_threshold(self, build_report_daily):
        # 18 min break overrun + 15 min pre-break = 33 min combined → flagged
        brk = {"users": [
            {"user_id": "u1", "user_name": "Combined",
             "total_overrun_min": 18.0, "pre_break_overrun_total_min": 15.0,
             "away_total_min": 0, "overrun_count": 1, "pre_break_overrun_count": 1},
        ]}
        rows = build_report_daily.adherence_flags(brk, overrun_min_threshold=30)
        assert len(rows) == 1
        assert rows[0]["total_min"] == 33.0

    def test_small_break_overrun_still_filtered_below_threshold(self, build_report_daily):
        brk = {"users": [
            {"user_id": "u1", "user_name": "Small overrun",
             "total_overrun_min": 5.4, "pre_break_overrun_total_min": 0,
             "away_total_min": 0, "overrun_count": 1, "pre_break_overrun_count": 0},
        ]}
        rows = build_report_daily.adherence_flags(brk, overrun_min_threshold=30)
        assert rows == []


class TestRepeatCallerHotlistKey:
    """Pre-v0.9.2 read ``unresolved_repeaters`` — the deep-dive returns ``repeaters``."""

    def test_reads_repeaters_key(self, build_report_daily):
        deep = {"repeaters": [
            {"ani": "+61400000001", "answered_count": 23,
             "unresolved_share": 0.78, "recommended_action": "route_review",
             "topics": [{"topic": "Billing (answered)"}]},
        ]}
        rows = build_report_daily.repeat_caller_hotlist(deep)
        assert len(rows) == 1
        assert rows[0]["ani_masked"] == "+61400000001"
        assert rows[0]["unresolved_share"] == 0.78

    def test_keeps_callback_and_retention_actions_regardless_of_share(self, build_report_daily):
        deep = {"repeaters": [
            {"ani": "+61400000002", "answered_count": 3,
             "unresolved_share": 0.0, "recommended_action": "escalate_to_retention",
             "topics": []},
            {"ani": "+61400000003", "answered_count": 1,
             "unresolved_share": 0.0, "recommended_action": "monitor",
             "topics": []},
        ]}
        rows = build_report_daily.repeat_caller_hotlist(deep)
        assert len(rows) == 1
        assert rows[0]["recommended_action"] == "escalate_to_retention"

    def test_falls_back_to_legacy_unresolved_repeaters_key(self, build_report_daily):
        deep = {"unresolved_repeaters": [
            {"ani": "+61400000004", "answered_count": 5,
             "unresolved_share": 0.9, "recommended_action": "monitor"},
        ]}
        rows = build_report_daily.repeat_caller_hotlist(deep)
        assert len(rows) == 1


class TestAdherenceSummary:
    """The summary line must always reflect org-level overrun, independent of
    the per-agent threshold. Without it the brief silently swallowed ~200 min
    of accumulated overrun on a typical day.
    """

    def test_sums_break_meal_and_pre_break_across_users(self, build_report_daily):
        brk = {"users": [
            {"user_id": "u1", "total_overrun_min": 12.3, "pre_break_overrun_total_min": 0,
             "overrun_count": 1, "pre_break_overrun_count": 0},
            {"user_id": "u2", "total_overrun_min": 0, "pre_break_overrun_total_min": 42.5,
             "overrun_count": 0, "pre_break_overrun_count": 1},
            {"user_id": "u3", "total_overrun_min": 5.4, "pre_break_overrun_total_min": 23.8,
             "overrun_count": 1, "pre_break_overrun_count": 3},
            {"user_id": "u4", "total_overrun_min": 0, "pre_break_overrun_total_min": 0,
             "overrun_count": 0, "pre_break_overrun_count": 0},
        ]}
        s = build_report_daily.adherence_summary(brk)
        assert s["break_meal_min"] == pytest.approx(17.7)
        assert s["pre_break_min"] == pytest.approx(66.3)
        assert s["total_min"] == pytest.approx(84.0)
        assert s["agents_with_any"] == 3
        assert s["overrun_sessions"] == 6  # 1+1+(1+3)

    def test_empty_users_returns_zero_totals(self, build_report_daily):
        s = build_report_daily.adherence_summary({"users": []})
        assert s["total_min"] == 0
        assert s["agents_with_any"] == 0
        assert s["overrun_sessions"] == 0


# ── v1.11: NPS card + wrap-up mini-card ──

class TestNpsAggregator:
    """Pins the v1.11 NPS aggregator for the daily brief."""

    def test_none_input(self, build_report_daily):
        assert build_report_daily.aggregate_nps(None) is None

    def test_zero_conversations(self, build_report_daily):
        payload = {"totals": {"conversation_count": 0}, "numeric_summary": None}
        assert build_report_daily.aggregate_nps(payload) is None

    def test_non_nps_numeric_returns_none(self, build_report_daily):
        # Numeric values that aren't 0-10 integers → numeric_summary.nps is None.
        payload = {
            "totals": {"conversation_count": 25},
            "numeric_summary": {"mean": 4.2, "median": 4.0, "nps": None},
        }
        assert build_report_daily.aggregate_nps(payload) is None

    def test_well_formed_nps_payload(self, build_report_daily):
        payload = {
            "totals": {"conversation_count": 40},
            "numeric_summary": {
                "mean": 8.2,
                "nps": {"score": 35.0, "promoters_9_10": 22, "passives_7_8": 14, "detractors_0_6": 4},
            },
        }
        nps = build_report_daily.aggregate_nps(payload)
        assert nps == {"score": 35.0, "total": 40, "promoters": 22, "passives": 14, "detractors": 4}


class TestNpsCardRender:
    """Pins the v1.11 NPS card render gating + colour bands."""

    def test_none_renders_empty(self, build_report_daily):
        assert build_report_daily.render_nps_card(None) == ""

    def test_strong_score_renders_good_band(self, build_report_daily):
        nps = {"score": 45.0, "total": 100, "promoters": 60, "passives": 25, "detractors": 15}
        html = build_report_daily.render_nps_card(nps)
        assert "good" in html and "+45" in html
        assert "60 / 25 / 15" in html
        assert "n=100" in html

    def test_negative_score_renders_bad_band(self, build_report_daily):
        nps = {"score": -10.0, "total": 50, "promoters": 10, "passives": 20, "detractors": 20}
        html = build_report_daily.render_nps_card(nps)
        assert "bad" in html and "-10" in html


class TestWrapUpMiniAggregator:
    """Pins the v1.11 wrap-up mini-card aggregator."""

    def test_none_input(self, build_report_daily):
        assert build_report_daily.aggregate_wrap_up_mini(None) is None

    def test_zero_conversations(self, build_report_daily):
        payload = {"totals": {"conversation_count": 0}, "distribution": []}
        assert build_report_daily.aggregate_wrap_up_mini(payload) is None

    def test_top_n_cap_and_largest_mover(self, build_report_daily):
        payload = {
            "totals": {"conversation_count": 200, "distinct_code_count": 8},
            "distribution": [
                {"name": f"Code{i}", "count": 50 - i * 5, "percentage": 25.0 - i * 2.5}
                for i in range(8)
            ],
            "trend": {
                "largest_movers": [
                    {"name": "Code0", "delta_pct": 15.0, "movement": "up"},
                    {"name": "Code3", "delta_pct": -10.0, "movement": "down"},
                ],
            },
        }
        agg = build_report_daily.aggregate_wrap_up_mini(payload, top_n=5)
        assert len(agg["top_codes"]) == 5
        assert agg["largest_mover"]["name"] == "Code0"
        assert agg["total_conversations"] == 200


class TestWrapUpMiniCardRender:
    """Pins the v1.11 wrap-up mini-card render."""

    def test_none_renders_empty(self, build_report_daily):
        assert build_report_daily.render_wrap_up_mini_card(None) == ""

    def test_soft_fail_envelope_renders_visible_callout(self, build_report_daily):
        # v1.12.1: soft-fail envelope from wrap-up tool → visible callout
        envelope = {
            "status": 403,
            "kind": "wrap_up_code_distribution",
            "message": "Grant analytics:conversationAggregate:view.",
        }
        agg = build_report_daily.aggregate_wrap_up_mini(envelope)
        assert agg is not None
        assert agg["_soft_fail"] is True
        html = build_report_daily.render_wrap_up_mini_card(agg)
        assert "data not retrieved" in html
        assert "403" in html
        assert "analytics:conversationAggregate:view" in html

    def test_renders_table_and_mover_callout(self, build_report_daily):
        from bs4 import BeautifulSoup
        wrap = {
            "total_conversations": 100,
            "top_codes": [
                {"name": "Resolved", "count": 60, "percentage": 60.0},
                {"name": "Transfer", "count": 20, "percentage": 20.0},
            ],
            "largest_mover": {"name": "Transfer", "delta_pct": 12.3, "movement": "up"},
        }
        html = build_report_daily.render_wrap_up_mini_card(wrap)
        assert "Resolved" in html and "Transfer" in html
        assert "Largest mover" in html
        assert "↑" in html and "+12.3%" in html
        soup = BeautifulSoup(html, "html.parser")
        assert len(soup.find("thead").find_all("th")) == 3
