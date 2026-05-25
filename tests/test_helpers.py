"""Unit tests for the pure-function helpers in the skill build scripts +
``src/genesys_mcp/tools/reports.py``.

These cover the tiny utility functions (formatters, threshold classifiers,
sentiment/trend labellers) that appear in every report row. They're cheap
to test, easy to assert, and catch regressions on subtle thresholds (e.g.
the colour-band cutoffs on AHT-vs-target pills).

No live tenant data required.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


# ── Formatters (build_report.py module-level helpers) ──

class TestFmtSecs:
    """fmt_secs handles None, zero, sub-minute, and ≥1-minute durations."""

    @pytest.mark.parametrize("value,expected", [
        (None, "—"),
        (0, "—"),
        (1, "1s"),
        (45, "45s"),
        (59, "59s"),
        (60, "1m 00s"),
        (61, "1m 01s"),
        (125, "2m 05s"),
        (3600, "60m 00s"),  # We don't roll up to hours — verifies that
        (285, "4m 45s"),    # voice AHT target
        (660, "11m 00s"),   # message AHT target
    ])
    def test_formats_correctly(self, build_report_monthly, value, expected):
        assert build_report_monthly.fmt_secs(value) == expected

    def test_float_rounds(self, build_report_monthly):
        # 59.4 rounds to 59; 59.5 rounds to 60 (Python banker's rounding —
        # actually Python rounds 59.5 to 60 since 60 is even)
        assert build_report_monthly.fmt_secs(59.4) == "59s"
        assert build_report_monthly.fmt_secs(59.6) == "1m 00s"


class TestFmtInt:
    @pytest.mark.parametrize("value,expected", [
        (None, "—"),
        (0, "0"),
        (1, "1"),
        (1000, "1,000"),
        (123456, "123,456"),
        (-5, "-5"),
        (3.7, "3"),    # truncates floats
    ])
    def test_formats(self, build_report_monthly, value, expected):
        assert build_report_monthly.fmt_int(value) == expected


class TestFmtPct:
    @pytest.mark.parametrize("value,dp,expected", [
        (None, 1, "—"),
        (0.0, 1, "0.0%"),
        (75.5, 1, "75.5%"),
        (75.5, 0, "76%"),    # default dp=1 vs explicit dp=0
        (100.0, 1, "100.0%"),
        (75.55, 2, "75.55%"),
    ])
    def test_formats(self, build_report_monthly, value, dp, expected):
        assert build_report_monthly.fmt_pct(value, dp) == expected

    def test_default_dp_is_one(self, build_report_monthly):
        assert build_report_monthly.fmt_pct(75.5) == "75.5%"


# ── Threshold helpers ──

class TestBarClass:
    """bar_class returns 'good'/'warn'/'bad'/'neutral' on threshold bands."""

    @pytest.mark.parametrize("pct,expected", [
        (None, "neutral"),
        (0, "bad"),
        (49.9, "bad"),
        (50, "warn"),     # threshold inclusive on the lower end
        (50.0, "warn"),
        (79.9, "warn"),
        (80, "good"),     # SL target threshold
        (100, "good"),
    ])
    def test_default_bands(self, build_report_monthly, pct, expected):
        assert build_report_monthly.bar_class(pct) == expected

    def test_custom_bands(self, build_report_monthly):
        # AHT-style: lower is better, but bar_class is "higher is better"
        # so callers invert when needed.
        assert build_report_monthly.bar_class(95, good_at=90, warn_at=70) == "good"
        assert build_report_monthly.bar_class(75, good_at=90, warn_at=70) == "warn"
        assert build_report_monthly.bar_class(65, good_at=90, warn_at=70) == "bad"


class TestVsTargetPct:
    """_vs_target_pct computes (actual - target) / target * 100."""

    @pytest.mark.parametrize("actual,target,expected", [
        (None, 285, None),
        (0, 0, None),       # target=0 guard
        (285, 285, 0.0),    # exactly on target
        (330, 285, 15.8),   # the Anthony Kha v0.5 example: +15.8%
        (240, 285, -15.8),  # 240s = top-performer p25, ~16% under
        (660, 660, 0.0),
        (781, 660, 18.3),   # Anthony message AHT
    ])
    def test_computes_correctly(self, build_report_monthly, actual, target, expected):
        result = build_report_monthly._vs_target_pct(actual, target)
        if expected is None:
            assert result is None
        else:
            assert result == pytest.approx(expected, abs=0.1)


# ── Sentiment / trend labellers (reports.py) ──

class TestSentimentLabel:
    """_sentiment_label classifies a single sentiment score into a label.

    Boundaries pinned to the v0.2 implementation (lowercase, snake_case labels):
    ≥0.4 = positive, ≥0.1 = mildly_positive, |x|<0.1 = neutral,
    >-0.4 = mildly_negative, else negative.
    """

    @pytest.mark.parametrize("score,expected", [
        (None, "unknown"),
        (0.7, "positive"),
        (0.4, "positive"),           # boundary inclusive
        (0.3, "mildly_positive"),
        (0.1, "mildly_positive"),    # boundary inclusive
        (0.05, "neutral"),
        (0.0, "neutral"),
        (-0.05, "neutral"),
        (-0.1, "mildly_negative"),
        (-0.3, "mildly_negative"),
        (-0.4, "negative"),          # boundary on -0.4 = negative (>-0.4 is mildly)
        (-0.7, "negative"),
    ])
    def test_levels(self, score, expected):
        from genesys_mcp.tools.reports import _sentiment_label
        assert _sentiment_label(score) == expected


class TestTrendLabel:
    """_trend_label respects single-call trend class for 1-call ANIs.

    Empty scores → 'no_data'. Single call → derive from
    single_call_trend_class via the GENESYS_TREND_TO_LABEL map. 2+ calls →
    compute from first/last delta + avg, mapping to deteriorating /
    improving / stable / persistently_negative / stable_neutral.
    """

    def test_empty_returns_no_data(self):
        from genesys_mcp.tools.reports import _trend_label
        assert _trend_label([]) == "no_data"

    def test_single_call_with_no_class_returns_single_call(self):
        from genesys_mcp.tools.reports import _trend_label
        assert _trend_label([0.5]) == "single_call"

    def test_single_call_with_provided_class_maps(self):
        from genesys_mcp.tools.reports import _trend_label
        # Genesys's per-call sentimentTrendClass — pinned mapping from v0.2.
        assert _trend_label([0.5], single_call_trend_class="GreatlyImproving") == "improving"
        assert _trend_label([0.5], single_call_trend_class="GreatlyDeclining") == "deteriorating"
        assert _trend_label([0.5], single_call_trend_class="NoChange") == "stable"
        assert _trend_label([0.5], single_call_trend_class="NotCalculated") == "unknown"

    def test_multi_call_improving(self):
        from genesys_mcp.tools.reports import _trend_label
        # delta >= 0.3 over multiple calls = improving
        assert _trend_label([-0.5, 0.0, 0.5]) == "improving"

    def test_multi_call_deteriorating(self):
        from genesys_mcp.tools.reports import _trend_label
        assert _trend_label([0.5, 0.0, -0.5]) == "deteriorating"

    def test_persistently_negative(self):
        from genesys_mcp.tools.reports import _trend_label
        # avg <= -0.3 and |delta| < 0.2 = persistently_negative
        assert _trend_label([-0.5, -0.4, -0.45]) == "persistently_negative"

    def test_stable_neutral(self):
        from genesys_mcp.tools.reports import _trend_label
        # -0.1 <= avg <= 0.1 with small delta = stable_neutral
        assert _trend_label([0.0, 0.05, -0.05]) == "stable_neutral"


class TestRecommendAction:
    """_recommend_action heuristic — first-match-wins order is load-bearing.

    Required row keys: abandoned_in_queue_count, last_call,
    queues_offered, sentiment_trend, topics. Rule order:
    1. abandoned >= 3 AND last call NOT answered → callback_recommended
    2. trend == 'deteriorating' AND retention topic → escalate_to_retention
    3. distinct queues >= 3 → route_review
    4. otherwise → monitor
    """

    def _row(self, **overrides) -> dict:
        # Sensible defaults; tests override specific fields.
        base = {
            "abandoned_in_queue_count": 0,
            "last_call": {"status": "answered"},
            "queues_offered": ["Q1"],
            "sentiment_trend": "stable",
            "topics": [],
        }
        base.update(overrides)
        return base

    def test_callback_recommended_when_multiple_abandons_and_last_not_answered(self):
        from genesys_mcp.tools.reports import _recommend_action
        row = self._row(
            abandoned_in_queue_count=3,
            last_call={"status": "abandoned"},
        )
        assert _recommend_action(row) == "callback_recommended"

    def test_escalate_when_trend_deteriorating_and_retention_topic(self):
        from genesys_mcp.tools.reports import _recommend_action
        row = self._row(
            sentiment_trend="deteriorating",
            topics=[{"topic": "billing dispute"}],
        )
        assert _recommend_action(row) == "escalate_to_retention"

    def test_route_review_when_many_distinct_queues(self):
        from genesys_mcp.tools.reports import _recommend_action
        row = self._row(queues_offered=["Q1", "Q2", "Q3", "Q4"])
        assert _recommend_action(row) == "route_review"

    def test_monitor_default(self):
        from genesys_mcp.tools.reports import _recommend_action
        assert _recommend_action(self._row()) == "monitor"

    def test_first_match_wins_callback_over_escalate(self):
        # Both rules 1 and 2 trigger — rule 1 wins because it comes first.
        from genesys_mcp.tools.reports import _recommend_action
        row = self._row(
            abandoned_in_queue_count=4,
            last_call={"status": "abandoned"},
            sentiment_trend="deteriorating",
            topics=[{"topic": "billing"}],
        )
        assert _recommend_action(row) == "callback_recommended"


# ── HTML cell helpers (build_report.py module-level renderers) ──

class TestAhtWithTarget:
    """_aht_with_target picks colour class on the threshold bands."""

    def test_under_target_is_good(self, build_report_monthly):
        # vs_pct <= 0 means under target = good
        html = build_report_monthly._aht_with_target(240, -15.8)
        assert "good" in html
        assert "240s" in html
        assert "-16%" in html  # rounded

    def test_just_over_target_is_warn(self, build_report_monthly):
        # 0 < vs_pct <= 20 = warn
        html = build_report_monthly._aht_with_target(330, 15.8)
        assert "warn" in html
        assert "+16%" in html

    def test_far_over_target_is_bad(self, build_report_monthly):
        # vs_pct > 20 = bad
        html = build_report_monthly._aht_with_target(400, 40.0)
        assert "bad" in html
        assert "+40%" in html

    def test_none_aht_renders_dash(self, build_report_monthly):
        html = build_report_monthly._aht_with_target(None, None)
        assert "—" in html


class TestAcwWithTarget:
    """_acw_with_target — lower-is-better thresholds."""

    def test_under_target_is_good(self, build_report_monthly):
        html = build_report_monthly._acw_with_target(10, -33.3)  # ACW target 15s
        assert "good" in html
        assert "10s" in html

    def test_far_over_target_is_bad(self, build_report_monthly):
        html = build_report_monthly._acw_with_target(80, 433.3)  # ACW way over
        assert "bad" in html


class TestCountAndMinCell:
    """_count_and_min_cell — pill colour by count + min threshold."""

    def test_zero_count_when_sessions_known(self, build_report_monthly):
        html = build_report_monthly._count_and_min_cell(
            count=0, minutes=0, sessions_known=True,
        )
        assert "good" in html or "0" in html  # zero overruns is good

    def test_sessions_unknown_returns_dash(self, build_report_monthly):
        html = build_report_monthly._count_and_min_cell(
            count=0, minutes=0, sessions_known=False,
        )
        assert "—" in html

    def test_high_count_warns(self, build_report_monthly):
        html = build_report_monthly._count_and_min_cell(
            count=8, minutes=45, sessions_known=True, warn_at=5, bad_at=7,
        )
        assert "bad" in html
