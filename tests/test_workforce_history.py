"""Tests for the v1.12 ``user_activity_history`` tool.

Exercises the pure-Python aggregation helpers in
``src/genesys_mcp/tools/workforce_history.py``. The tool's API surface
itself is exercised at the helper level; SDK-shaped network calls are
not mocked here (they're SDK-1:1 and tested in upstream code).
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "wh", _REPO_ROOT / "src/genesys_mcp/tools/workforce_history.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wh():
    return _load_module()


# ── Helper-level tests ──

class TestBucketKeys:
    def test_quarterly_buckets_for_three_year_window(self, wh):
        start = datetime(2023, 7, 1, tzinfo=ZoneInfo("UTC"))
        end = datetime(2026, 7, 1, tzinfo=ZoneInfo("UTC"))
        keys = wh._bucket_keys(start, end, "quarter", ZoneInfo("Australia/Sydney"))
        # 13 buckets — Q3 2023 through Q3 2026 inclusive.
        assert len(keys) == 13
        assert keys[0] == "2023-Q3"
        assert keys[-1] == "2026-Q3"

    def test_monthly_buckets_for_a_year(self, wh):
        start = datetime(2024, 1, 1, tzinfo=ZoneInfo("UTC"))
        end = datetime(2025, 1, 1, tzinfo=ZoneInfo("UTC"))
        keys = wh._bucket_keys(start, end, "month", ZoneInfo("UTC"))
        assert len(keys) == 13
        assert keys[0] == "2024-01"
        assert keys[-1] == "2025-01"


class TestMonthToQuarter:
    @pytest.mark.parametrize("month,quarter", [
        ("2024-01", "2024-Q1"),
        ("2024-03", "2024-Q1"),
        ("2024-04", "2024-Q2"),
        ("2024-06", "2024-Q2"),
        ("2024-07", "2024-Q3"),
        ("2024-09", "2024-Q3"),
        ("2024-10", "2024-Q4"),
        ("2024-12", "2024-Q4"),
    ])
    def test_quarter_boundaries(self, wh, month, quarter):
        assert wh._month_to_quarter(month) == quarter


class TestMonthsBetween:
    def test_same_month_is_zero(self, wh):
        assert wh._months_between("2024-04", "2024-04") == 0

    def test_forward_only(self, wh):
        assert wh._months_between("2024-01", "2025-04") == 15

    def test_reverse_clamps_to_zero(self, wh):
        # Tenure can't be negative; the helper is used for first→bucket-start.
        assert wh._months_between("2024-04", "2024-01") == 0


class TestSplitIntervalByYear:
    def test_three_year_window_produces_three_chunks(self, wh):
        start = datetime(2023, 7, 1, tzinfo=ZoneInfo("UTC"))
        end = datetime(2026, 7, 1, tzinfo=ZoneInfo("UTC"))
        chunks = wh._split_interval_by_year(start, end)
        assert len(chunks) == 3
        assert "2023-07-01" in chunks[0] and "2024-07-01" in chunks[0]
        assert "2024-07-01" in chunks[1] and "2025-07-01" in chunks[1]
        assert "2025-07-01" in chunks[2] and "2026-07-01" in chunks[2]

    def test_short_window_single_chunk(self, wh):
        start = datetime(2025, 1, 1, tzinfo=ZoneInfo("UTC"))
        end = datetime(2025, 6, 1, tzinfo=ZoneInfo("UTC"))
        chunks = wh._split_interval_by_year(start, end)
        assert len(chunks) == 1


class TestBucketStartMonth:
    @pytest.mark.parametrize("bucket,expected", [
        ("2024-Q1", "2024-01"),
        ("2024-Q2", "2024-04"),
        ("2024-Q3", "2024-07"),
        ("2024-Q4", "2024-10"),
        ("2024-04", "2024-04"),  # monthly pass-through
    ])
    def test_bucket_start(self, wh, bucket, expected):
        assert wh._bucket_start_month(bucket) == expected


# ── End-to-end aggregator (synthetic per_user_months, no live API) ──

class TestFullRollup:
    """Wires the helpers together with deterministic synthetic input to
    verify joiner/leaver/tenure math end-to-end without going through the
    network."""

    def _build_rollup(self, wh, users, per_user_months, bucket="quarter"):
        start = datetime(2024, 1, 1, tzinfo=ZoneInfo("UTC"))
        end = datetime(2025, 1, 1, tzinfo=ZoneInfo("UTC"))
        all_buckets = wh._bucket_keys(start, end, bucket, ZoneInfo("UTC"))
        joiners = {b: 0 for b in all_buckets}
        leavers = {b: 0 for b in all_buckets}
        active = {b: set() for b in all_buckets}
        tenure: dict[str, list[int]] = {b: [] for b in all_buckets}
        per_user_out = []

        for u in users:
            uid = u["user_id"]
            months_sorted = sorted((per_user_months.get(uid) or {}).keys())
            if not months_sorted:
                continue
            first_m, last_m = months_sorted[0], months_sorted[-1]
            for mk in months_sorted:
                bk = wh._bucket_for_month(mk, bucket)
                if bk in active:
                    active[bk].add(uid)
            first_bk = wh._bucket_for_month(first_m, bucket)
            last_bk = wh._bucket_for_month(last_m, bucket)
            if first_bk in joiners:
                joiners[first_bk] += 1
            if last_bk in leavers and last_bk != all_buckets[-1]:
                leavers[last_bk] += 1
            for bk in {wh._bucket_for_month(m, bucket) for m in months_sorted}:
                if bk in tenure:
                    tenure[bk].append(
                        wh._months_between(first_m, wh._bucket_start_month(bk))
                    )
            per_user_out.append({**u, "first": first_m, "last": last_m})
        return all_buckets, joiners, leavers, active, tenure, per_user_out

    def test_joiner_and_leaver_math(self, wh):
        users = [
            {"user_id": "u1", "name": "Alice"},   # active whole year
            {"user_id": "u2", "name": "Bob"},     # first active Q2 → joiner Q2
            {"user_id": "u3", "name": "Carol"},   # last active Q3 → leaver Q3
        ]
        per_user_months = {
            "u1": {"2024-01": 5, "2024-04": 5, "2024-07": 5, "2024-10": 5},
            "u2": {"2024-04": 3, "2024-07": 3, "2024-10": 3},
            "u3": {"2024-01": 2, "2024-04": 2, "2024-07": 2},
        }
        _b, joiners, leavers, active, _t, _p = self._build_rollup(
            wh, users, per_user_months,
        )
        # Joiners: u1+u3 in Q1, u2 in Q2.
        assert joiners["2024-Q1"] == 2
        assert joiners["2024-Q2"] == 1
        assert joiners["2024-Q3"] == 0
        # Window has 5 buckets (Q1..Q4 2024 + Q1 2025); final = 2025-Q1.
        # u1 last_bk = Q4 != final → leaver in Q4.
        # u2 last_bk = Q4 != final → leaver in Q4.
        # u3 last_bk = Q3 != final → leaver in Q3.
        assert leavers["2024-Q3"] == 1
        assert leavers["2024-Q4"] == 2
        assert leavers["2025-Q1"] == 0
        # Active sets per quarter:
        assert active["2024-Q1"] == {"u1", "u3"}
        assert active["2024-Q2"] == {"u1", "u2", "u3"}
        assert active["2024-Q3"] == {"u1", "u2", "u3"}
        assert active["2024-Q4"] == {"u1", "u2"}
        assert active["2025-Q1"] == set()  # no activity

    def test_tenure_grows_with_each_active_bucket(self, wh):
        users = [{"user_id": "u1", "name": "Alice"}]
        per_user_months = {
            "u1": {"2024-01": 1, "2024-04": 1, "2024-07": 1, "2024-10": 1},
        }
        _b, _j, _l, _a, tenure, _p = self._build_rollup(wh, users, per_user_months)
        assert tenure["2024-Q1"] == [0]
        assert tenure["2024-Q2"] == [3]
        assert tenure["2024-Q3"] == [6]
        assert tenure["2024-Q4"] == [9]

    def test_user_with_no_activity_omitted_from_rollups(self, wh):
        users = [
            {"user_id": "u1", "name": "Alice"},
            {"user_id": "u2", "name": "Ghost"},
        ]
        per_user_months = {"u1": {"2024-01": 5}}
        _b, joiners, _l, active, _t, _p = self._build_rollup(
            wh, users, per_user_months,
        )
        # u2 is in no active set.
        for b, s in active.items():
            assert "u2" not in s
        # u2 contributes no joiner.
        assert sum(joiners.values()) == 1


# ── Tool-level shape ──

class TestToolShape:
    def test_module_exports_register(self, wh):
        assert callable(wh.register)

    def test_default_interval_resolves_to_three_years(self, wh):
        interval = wh._resolve_default_interval(ZoneInfo("Australia/Sydney"), 3)
        assert "/" in interval
        start_s, end_s = interval.split("/")
        assert start_s.endswith(".000Z") and end_s.endswith(".000Z")
        start_dt = datetime.fromisoformat(start_s.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_s.replace("Z", "+00:00"))
        delta_days = (end_dt - start_dt).days
        assert 1090 < delta_days < 1100, f"~3 years expected, got {delta_days} days"
