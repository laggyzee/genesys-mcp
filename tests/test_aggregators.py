"""Golden-fixture tests for the cc-monthly-report aggregators.

The aggregators in ``skills/cc-monthly-report/build_report.py`` are pure
functions of dict → dict. We load fixtures captured from a live tenant
(``tests/_capture_fixtures.py``), run each aggregator, and assert
**structural properties** of the output rather than full snapshots.

Why structural over snapshots: snapshots are brittle (a single new field
breaks every snapshot test); structural assertions catch the real
regressions (counts disagree, totals don't reconcile, fields go missing)
without the maintenance burden.

If a fixture is missing the tests skip (with a clear pointer to the
capture script) — they don't fail. That keeps the suite runnable on
a fresh clone without OAuth creds.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


# ── aggregate_queue_performance ──

class TestAggregateQueuePerformance:
    """Brand × media rollup of queue_performance output."""

    def test_returns_brand_rows_and_per_queue(
        self, build_report_monthly, fix_queue_performance, fix_qmap,
    ):
        result = build_report_monthly.aggregate_queue_performance(
            fix_queue_performance, fix_qmap,
        )
        assert "brand_rows" in result
        assert "per_queue" in result

    def test_brand_rows_have_canonical_fields(
        self, build_report_monthly, fix_queue_performance, fix_qmap,
    ):
        result = build_report_monthly.aggregate_queue_performance(
            fix_queue_performance, fix_qmap,
        )
        assert result["brand_rows"], "expected at least one brand row from fixture"
        row = result["brand_rows"][0]
        for field in ("brand", "media", "offered", "answered", "abandoned",
                      "ans_pct", "sl_pct", "avg_wait_s", "avg_handle_s"):
            assert field in row, f"brand_row missing canonical field {field!r}"

    def test_media_types_are_voice_or_message(
        self, build_report_monthly, fix_queue_performance, fix_qmap,
    ):
        # The aggregator filters out other media types; only voice + message
        # roll into brand_rows. Catches regressions where someone widens the
        # filter and includes callback/email noise.
        result = build_report_monthly.aggregate_queue_performance(
            fix_queue_performance, fix_qmap,
        )
        for row in result["brand_rows"]:
            assert row["media"] in ("voice", "message"), (
                f"unexpected media {row['media']!r} in brand_rows"
            )

    def test_brand_totals_reconcile_against_per_queue(
        self, build_report_monthly, fix_queue_performance, fix_qmap,
    ):
        # For each (brand, media) bucket, the brand_rows offered count
        # should equal the sum of per_queue offered for that same bucket.
        # If this drifts, double-counting or filter-shape bugs have
        # silently broken aggregation.
        result = build_report_monthly.aggregate_queue_performance(
            fix_queue_performance, fix_qmap,
        )
        from collections import defaultdict
        per_q_totals = defaultdict(int)
        for r in result["per_queue"]:
            per_q_totals[(r["brand"], r["media"])] += r["offered"]
        for r in result["brand_rows"]:
            key = (r["brand"], r["media"])
            assert r["offered"] == per_q_totals[key], (
                f"brand_row offered={r['offered']} doesn't match sum of "
                f"per_queue offered={per_q_totals[key]} for {key}"
            )

    def test_unknown_queue_ids_are_dropped(
        self, build_report_monthly, fix_queue_performance,
    ):
        # An empty qmap should drop every row — the aggregator filters
        # against qmap keys to scope to customer-facing queues.
        result = build_report_monthly.aggregate_queue_performance(
            fix_queue_performance, qmap={},
        )
        assert result["brand_rows"] == []
        assert result["per_queue"] == []


# ── aggregate_agents ──

class TestAggregateAgents:
    """Per-agent rollup combining agent_performance + break_overrun_report."""

    def test_returns_list_of_rows(
        self, build_report_monthly, fix_agent_performance, fix_break_overrun,
        fix_user_roles,
    ):
        rows = build_report_monthly.aggregate_agents(
            fix_agent_performance, fix_break_overrun, fix_user_roles,
            specialist_only=True,
        )
        assert isinstance(rows, list)
        assert rows, "fixture should produce at least one specialist row"

    def test_each_row_has_required_fields(
        self, build_report_monthly, fix_agent_performance, fix_break_overrun,
        fix_user_roles,
    ):
        rows = build_report_monthly.aggregate_agents(
            fix_agent_performance, fix_break_overrun, fix_user_roles,
            specialist_only=True,
        )
        row = rows[0]
        required = (
            "name", "role", "answered",
            "voice_ans", "msg_ans",
            "voice_aht_s", "msg_aht_s",
            "avg_acw_s",
            "overruns", "overrun_min",
            "away_count", "away_min",
            "pre_break_overrun_count", "pre_break_overrun_min",
        )
        for field in required:
            assert field in row, f"agent row missing required field {field!r}"

    def test_specialist_only_filters_non_specialists(
        self, build_report_monthly, fix_agent_performance, fix_break_overrun,
        fix_user_roles,
    ):
        # Reassign one user's role to "Team Leader"; specialist_only=True
        # should exclude them. Catches regressions in the role filter.
        roles_with_tl = dict(fix_user_roles)
        first_uid = next(iter(roles_with_tl))
        original_name = roles_with_tl[first_uid][0]
        roles_with_tl[first_uid] = [original_name, "Team Leader"]
        rows = build_report_monthly.aggregate_agents(
            fix_agent_performance, fix_break_overrun, roles_with_tl,
            specialist_only=True,
        )
        # The reassigned user (by name) should NOT appear in specialist rows
        assert not any(r["name"] == original_name for r in rows), (
            "specialist_only=True should exclude users with non-specialist role"
        )
        # And the included rows should all carry a specialist role
        for r in rows:
            assert "Specialist" in (r["role"] or ""), (
                f"specialist_only=True row has non-specialist role {r['role']!r}"
            )

    def test_aht_targets_are_positive_when_present(
        self, build_report_monthly, fix_agent_performance, fix_break_overrun,
        fix_user_roles,
    ):
        # AHT shouldn't ever come back negative. Catches arithmetic bugs.
        rows = build_report_monthly.aggregate_agents(
            fix_agent_performance, fix_break_overrun, fix_user_roles,
        )
        for r in rows:
            if r["voice_aht_s"] is not None:
                assert r["voice_aht_s"] >= 0, f"negative voice AHT for {r['name']}"
            if r["msg_aht_s"] is not None:
                assert r["msg_aht_s"] >= 0, f"negative message AHT for {r['name']}"


# ── extract_themes ──

class TestExtractThemes:
    """Theme extraction from repeat_caller_deep_dive output."""

    def test_returns_canonical_sections(
        self, build_report_monthly, fix_repeat_caller_deep_dive,
    ):
        themes = build_report_monthly.extract_themes(fix_repeat_caller_deep_dive)
        for key in ("top_dispositions", "top_ai_outcomes",
                    "top_expected_fixes", "unresolved_repeaters", "scope"):
            assert key in themes, f"themes output missing canonical section {key!r}"

    def test_unresolved_repeaters_have_required_fields(
        self, build_report_monthly, fix_repeat_caller_deep_dive,
    ):
        themes = build_report_monthly.extract_themes(fix_repeat_caller_deep_dive)
        for u in themes["unresolved_repeaters"]:
            assert "ani" in u or "ani_masked" in u  # one of the two
            assert "unresolved_share" in u
            assert "ai_outcomes" in u

    def test_top_lists_are_truncated_reasonably(
        self, build_report_monthly, fix_repeat_caller_deep_dive,
    ):
        # Each "top N" list should be bounded — the aggregator truncates
        # to keep HTML rendering tidy. Catches regressions where someone
        # removes the truncation and the report explodes.
        themes = build_report_monthly.extract_themes(fix_repeat_caller_deep_dive)
        assert len(themes["unresolved_repeaters"]) <= 15
        assert len(themes["top_dispositions"]) <= 15
        assert len(themes["top_ai_outcomes"]) <= 15


# ── aggregate_daily_voice_sl ──

class TestAggregateDailyVoiceSL:
    """Daily SL trend extraction for the line chart."""

    def test_returns_list_of_daily_points(
        self, build_report_monthly, fix_queue_performance_daily, fix_qmap,
    ):
        points = build_report_monthly.aggregate_daily_voice_sl(
            fix_queue_performance_daily, fix_qmap,
        )
        assert isinstance(points, list)
        # Fixture is a week — expect 7 days
        assert len(points) >= 6  # tolerance for partial day at the boundary

    def test_each_point_has_date_and_sl(
        self, build_report_monthly, fix_queue_performance_daily, fix_qmap,
    ):
        points = build_report_monthly.aggregate_daily_voice_sl(
            fix_queue_performance_daily, fix_qmap,
        )
        for p in points:
            assert "date" in p
            assert "sl_pct" in p
            assert "offered" in p
            if p["sl_pct"] is not None:
                assert 0 <= p["sl_pct"] <= 100, (
                    f"SL out of range on {p['date']}: {p['sl_pct']}"
                )


# ── compute_performance_leverage ──

class TestComputePerformanceLeverage:
    """Phantom-capacity + FCR-drag calc for the leverage section."""

    def test_returns_structured_payload(
        self, build_report_monthly, fix_agent_performance, fix_break_overrun,
        fix_user_roles, fix_queue_performance, fix_qmap, fix_repeat_caller_deep_dive,
    ):
        # Need workforce + deep + brand_rows as inputs.
        qp = build_report_monthly.aggregate_queue_performance(
            fix_queue_performance, fix_qmap,
        )
        workforce = build_report_monthly.aggregate_agents(
            fix_agent_performance, fix_break_overrun, fix_user_roles,
            specialist_only=True,
        )
        lev = build_report_monthly.compute_performance_leverage(
            workforce, fix_repeat_caller_deep_dive, qp["brand_rows"],
        )
        # Leverage output: phantom_capacity_hours, fcr_drag_hours, totals
        assert isinstance(lev, dict)
        # Hours and FTE values should be non-negative if present.
        for key in lev:
            val = lev[key]
            if isinstance(val, (int, float)) and "hours" in key:
                assert val >= 0, f"{key} should be non-negative; got {val}"


# ── aggregate_hourly_heatmap (v0.9) ──

class TestAggregateHourlyHeatmap:
    """Hour-of-day × day-of-week heatmap from PT1H queue_performance output."""

    def test_returns_canonical_shape(
        self, build_report_monthly, fix_queue_performance_hourly, fix_qmap,
    ):
        result = build_report_monthly.aggregate_hourly_heatmap(
            fix_queue_performance_hourly, fix_qmap, tz_offset_hours=10,
        )
        assert "cells" in result
        assert "days_used" in result
        assert isinstance(result["cells"], list)
        assert result["days_used"] >= 1

    def test_cells_have_dow_hour_offered_sl_pct(
        self, build_report_monthly, fix_queue_performance_hourly, fix_qmap,
    ):
        result = build_report_monthly.aggregate_hourly_heatmap(
            fix_queue_performance_hourly, fix_qmap, tz_offset_hours=10,
        )
        assert result["cells"], "fixture should produce cells with traffic"
        for cell in result["cells"]:
            assert 0 <= cell["dow"] <= 6
            assert 0 <= cell["hour"] <= 23
            assert "offered" in cell
            assert "sl_pct" in cell
            if cell["sl_pct"] is not None:
                # SL can technically exceed 100% on hourly buckets when
                # calls offered in one hour answer in the next (or v.v.) —
                # this is a quirk of Genesys's hour-bucketed aggregates, not
                # a bug in our aggregator. Allow ≤120% before flagging.
                assert 0 <= cell["sl_pct"] <= 120, (
                    f"SL out of plausible range at dow={cell['dow']} "
                    f"hour={cell['hour']}: {cell['sl_pct']}%"
                )

    def test_tz_offset_shifts_bucket_assignment(
        self, build_report_monthly, fix_queue_performance_hourly, fix_qmap,
    ):
        # Same fixture, different tz_offset_hours should distribute volume
        # differently across (dow, hour) buckets — the bucket key depends
        # on local hour, not UTC hour.
        result_utc = build_report_monthly.aggregate_hourly_heatmap(
            fix_queue_performance_hourly, fix_qmap, tz_offset_hours=0,
        )
        result_aest = build_report_monthly.aggregate_hourly_heatmap(
            fix_queue_performance_hourly, fix_qmap, tz_offset_hours=10,
        )
        utc_keys = {(c["dow"], c["hour"]) for c in result_utc["cells"]}
        aest_keys = {(c["dow"], c["hour"]) for c in result_aest["cells"]}
        # The key sets should differ if any UTC hour crosses a +10 boundary
        # (always true for a full week of data spanning the day boundary).
        assert utc_keys != aest_keys, (
            "tz_offset_hours should change bucket distribution"
        )


# ── aggregate_agent_voice_sparklines (v0.9) ──

class TestAggregateAgentVoiceSparklines:
    """Per-agent daily voice AHT trajectory from P1D agent_performance output."""

    def test_returns_dict_keyed_by_user_id(
        self, build_report_monthly, fix_agent_performance_daily,
    ):
        result = build_report_monthly.aggregate_agent_voice_sparklines(
            fix_agent_performance_daily,
        )
        assert isinstance(result, dict)
        # Some agents will be in the fixture; not all need daily voice data
        if result:
            for uid, series in result.items():
                assert isinstance(series, list)
                for day in series:
                    assert "date" in day
                    assert "voice_aht_s" in day
                    assert "voice_answered" in day

    def test_series_sorted_by_date(
        self, build_report_monthly, fix_agent_performance_daily,
    ):
        result = build_report_monthly.aggregate_agent_voice_sparklines(
            fix_agent_performance_daily,
        )
        for uid, series in result.items():
            dates = [d["date"] for d in series]
            assert dates == sorted(dates), (
                f"sparkline data for {uid} not sorted by date"
            )

    def test_aht_only_populated_when_answered(
        self, build_report_monthly, fix_agent_performance_daily,
    ):
        # Days with 0 answered should have voice_aht_s = None (gap),
        # not a divide-by-zero crash.
        result = build_report_monthly.aggregate_agent_voice_sparklines(
            fix_agent_performance_daily,
        )
        for uid, series in result.items():
            for day in series:
                if day["voice_answered"] == 0:
                    assert day["voice_aht_s"] is None, (
                        f"zero-answered day for {uid} on {day['date']} should "
                        f"have voice_aht_s=None"
                    )


# ── aggregate_staffing ──

class TestAggregateStaffing:
    """Daily WFM scheduled-vs-required from wfm_schedule output."""

    def test_handles_none_input(self, build_report_monthly):
        # Gracefully returns None when wfm_schedule wasn't run.
        assert build_report_monthly.aggregate_staffing(None) is None

    def test_returns_daily_series_from_fixture(
        self, build_report_monthly, fix_wfm_schedule,
    ):
        result = build_report_monthly.aggregate_staffing(fix_wfm_schedule)
        if result is None:
            pytest.skip("wfm_schedule fixture has no usable data for this tenant")
        assert "daily" in result
        for day in result["daily"]:
            for field in ("date", "scheduled_hours", "required_hours"):
                assert field in day
