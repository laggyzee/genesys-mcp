"""Pin v1.5 :mod:`genesys_mcp._intervals` — keyword → ISO interval helper.

These tests use a fixed reference time so every assertion is deterministic.

Reference clock: ``2026-06-18T14:00:00Z`` (= ``2026-06-19T00:00:00 AEST``,
a Friday). June is winter in Sydney so AEST is fixed at UTC+10 — no DST
transitions to second-guess.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from genesys_mcp._intervals import (
    INTERVAL_HELP_STRING,
    SUPPORTED_PERIODS,
    compute_period_interval,
    default_interval,
    parse_iso,
)


REF_UTC = datetime(2026, 6, 18, 14, 0, 0, tzinfo=timezone.utc)
SYD = "Australia/Sydney"


class TestSupportedPeriodsEnum:
    def test_eight_keywords(self):
        assert len(SUPPORTED_PERIODS) == 8

    def test_exact_set(self):
        assert set(SUPPORTED_PERIODS) == {
            "today", "yesterday",
            "this_week", "last_week",
            "this_month", "last_month",
            "last_7_days", "last_28_days",
        }


class TestIntervalHelpString:
    def test_mentions_compute_interval(self):
        assert "compute_interval" in INTERVAL_HELP_STRING

    def test_mentions_calendar_day(self):
        assert "calendar day" in INTERVAL_HELP_STRING

    def test_mentions_iso_format(self):
        assert "ISO-8601" in INTERVAL_HELP_STRING


class TestComputePeriodIntervalToday:
    def test_today_aest_boundary(self):
        out = compute_period_interval("today", SYD, now=REF_UTC)
        assert out["start_utc"] == "2026-06-18T14:00:00.000Z"
        assert out["end_utc"] == "2026-06-19T14:00:00.000Z"

    def test_interval_string_round_trips_through_parse_iso(self):
        out = compute_period_interval("today", SYD, now=REF_UTC)
        start_str, end_str = out["interval"].split("/")
        assert parse_iso(start_str) == datetime(2026, 6, 18, 14, tzinfo=timezone.utc)
        assert parse_iso(end_str) == datetime(2026, 6, 19, 14, tzinfo=timezone.utc)

    def test_period_and_timezone_echoed(self):
        out = compute_period_interval("today", SYD, now=REF_UTC)
        assert out["period"] == "today"
        assert out["timezone"] == SYD

    def test_no_weekday_anchor_for_non_week_periods(self):
        out = compute_period_interval("today", SYD, now=REF_UTC)
        assert "weekday_anchor" not in out


class TestComputePeriodIntervalYesterday:
    def test_yesterday_aest(self):
        out = compute_period_interval("yesterday", SYD, now=REF_UTC)
        assert out["start_utc"] == "2026-06-17T14:00:00.000Z"
        assert out["end_utc"] == "2026-06-18T14:00:00.000Z"


class TestComputePeriodIntervalWeek:
    def test_this_week_anchors_monday(self):
        # REF local = Fri 2026-06-19. this_week = Mon 2026-06-15 → Mon 2026-06-22
        out = compute_period_interval("this_week", SYD, now=REF_UTC)
        assert out["start_utc"] == "2026-06-14T14:00:00.000Z"
        assert out["end_utc"] == "2026-06-21T14:00:00.000Z"
        assert out["weekday_anchor"] == "Mon"

    def test_last_week_anchors_monday(self):
        # last_week = Mon 2026-06-08 → Mon 2026-06-15
        out = compute_period_interval("last_week", SYD, now=REF_UTC)
        assert out["start_utc"] == "2026-06-07T14:00:00.000Z"
        assert out["end_utc"] == "2026-06-14T14:00:00.000Z"
        assert out["weekday_anchor"] == "Mon"

    def test_this_week_starts_on_monday_when_today_is_monday(self):
        # Monday 2026-06-15 at 10:00 AEST = 2026-06-15T00:00Z
        mon_ref = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        out = compute_period_interval("this_week", SYD, now=mon_ref)
        # this_week start = Mon 2026-06-15T00:00 AEST = 2026-06-14T14:00Z
        assert out["start_utc"] == "2026-06-14T14:00:00.000Z"


class TestComputePeriodIntervalMonth:
    def test_this_month_snaps_to_first(self):
        # this_month = 2026-06-01T00:00 AEST → 2026-07-01T00:00 AEST
        out = compute_period_interval("this_month", SYD, now=REF_UTC)
        assert out["start_utc"] == "2026-05-31T14:00:00.000Z"
        assert out["end_utc"] == "2026-06-30T14:00:00.000Z"

    def test_last_month_snaps_to_first(self):
        out = compute_period_interval("last_month", SYD, now=REF_UTC)
        assert out["start_utc"] == "2026-04-30T14:00:00.000Z"
        assert out["end_utc"] == "2026-05-31T14:00:00.000Z"

    def test_month_rolls_over_december_to_january(self):
        # Mid-December: Sydney is in AEDT (UTC+11). 2026-12-15T05:00Z = 2026-12-15T16:00 AEDT
        dec_ref = datetime(2026, 12, 15, 5, 0, 0, tzinfo=timezone.utc)
        out = compute_period_interval("this_month", SYD, now=dec_ref)
        # Dec 1 AEDT = 2026-11-30T13:00Z → Jan 1 AEDT = 2026-12-31T13:00Z
        assert out["start_utc"] == "2026-11-30T13:00:00.000Z"
        assert out["end_utc"] == "2026-12-31T13:00:00.000Z"


class TestRollingPeriods:
    def test_last_7_days_is_rolling_not_anchored(self):
        # ref_local = 2026-06-19T00:00. last_7_days end = ref_local, start = -7d
        out = compute_period_interval("last_7_days", SYD, now=REF_UTC)
        assert out["start_utc"] == "2026-06-11T14:00:00.000Z"
        assert out["end_utc"] == "2026-06-18T14:00:00.000Z"

    def test_last_7_days_distinct_from_this_week(self):
        rolling = compute_period_interval("last_7_days", SYD, now=REF_UTC)
        anchored = compute_period_interval("this_week", SYD, now=REF_UTC)
        assert rolling["interval"] != anchored["interval"]

    def test_last_28_days_aligns_with_coaching_window(self):
        # ref = 2026-06-19T00:00 AEST. -28d = 2026-05-22T00:00 AEST.
        out = compute_period_interval("last_28_days", SYD, now=REF_UTC)
        assert out["start_utc"] == "2026-05-21T14:00:00.000Z"
        assert out["end_utc"] == "2026-06-18T14:00:00.000Z"


class TestErrors:
    def test_unknown_period_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown period"):
            compute_period_interval("yesteryear", SYD, now=REF_UTC)

    def test_unknown_timezone_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown timezone"):
            compute_period_interval("today", "Mars/Olympus_Mons", now=REF_UTC)

    def test_error_message_lists_supported_periods(self):
        with pytest.raises(ValueError) as exc_info:
            compute_period_interval("bogus", SYD, now=REF_UTC)
        assert "today" in str(exc_info.value)


class TestDefaultIntervalAndParseIso:
    def test_default_interval_canonical_format(self):
        s = default_interval(7)
        start, end = s.split("/")
        assert start.endswith(".000Z")
        assert end.endswith(".000Z")

    def test_default_window_is_n_days(self):
        s = default_interval(7)
        start, end = s.split("/")
        td = parse_iso(end) - parse_iso(start)
        assert td.days == 7

    def test_parse_iso_handles_z_suffix(self):
        dt = parse_iso("2026-06-17T14:00:00.000Z")
        assert dt == datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc)
