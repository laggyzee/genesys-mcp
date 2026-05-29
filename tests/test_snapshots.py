"""Numeric-output snapshot tests for the cc-monthly-report aggregators.

These tests pin the *exact* numeric output of each aggregator against a
small JSON snapshot in ``tests/fixtures/_snapshots/``. They catch the
class of bug that the v0.9.1 P7D-bucket-overwrite landed without an
error: any one-character change in reduce logic that flips a number.

If a test fails because the behaviour change was intentional:

1. Re-run ``python tests/_generate_snapshots.py``
2. ``git diff tests/fixtures/_snapshots/`` — every line that moved should
   be expected and explainable in the commit / release notes.
3. Commit the updated snapshots alongside the behaviour change.

Floats are rounded to 4 decimal places in the snapshot (see
``tests/_generate_snapshots.py:_normalise``) so trivial precision drift
doesn't trip these tests — only meaningful drift does.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SNAPSHOT_DIR = Path(__file__).resolve().parent / "fixtures" / "_snapshots"
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _normalise(obj):
    """Mirror :func:`tests._generate_snapshots._normalise` so live aggregator
    output is compared on the same rounding rules as the saved snapshot.
    """
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _normalise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalise(x) for x in obj]
    return obj


def _load_snapshot(name: str) -> dict:
    path = _SNAPSHOT_DIR / f"{name}.json"
    if not path.exists():
        pytest.skip(
            f"snapshot {path.relative_to(_REPO_ROOT)} not present — run "
            "`python tests/_generate_snapshots.py` to seed"
        )
    return json.loads(path.read_text())


def _roundtrip(obj):
    """JSON round-trip so tuples → lists, datetime → strings, etc. — matches
    what _generate_snapshots writes via ``json.dumps(..., default=str)``.
    """
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


class TestQueuePerformanceSnapshot:
    """``aggregate_queue_performance`` — brand × media rollup numbers."""

    def test_matches_snapshot(self, build_report_monthly,
                              fix_queue_performance, fix_qmap):
        actual = build_report_monthly.aggregate_queue_performance(
            fix_queue_performance, fix_qmap,
        )
        expected = _load_snapshot("aggregate_queue_performance")
        assert _normalise(_roundtrip(actual)) == expected, (
            "queue_performance aggregator output drifted from snapshot. "
            "If intentional, regenerate via `python tests/_generate_snapshots.py` "
            "and explain the diff in the commit."
        )


class TestAggregateAgentsSnapshot:
    """``aggregate_agents`` — per-agent productivity numbers."""

    def test_matches_snapshot(self, build_report_monthly,
                              fix_agent_performance, fix_break_overrun,
                              fix_user_roles):
        actual = build_report_monthly.aggregate_agents(
            fix_agent_performance, fix_break_overrun, fix_user_roles,
            specialist_only=True,
        )
        expected = _load_snapshot("aggregate_agents")
        assert _normalise(_roundtrip(actual)) == expected


class TestDailyVoiceSLSnapshot:
    """``aggregate_daily_voice_sl`` — daily SL trend numbers."""

    def test_matches_snapshot(self, build_report_monthly,
                              fix_queue_performance_daily, fix_qmap):
        actual = build_report_monthly.aggregate_daily_voice_sl(
            fix_queue_performance_daily, fix_qmap,
        )
        expected = _load_snapshot("aggregate_daily_voice_sl")
        assert _normalise(_roundtrip(actual)) == expected


class TestExtractThemesSnapshot:
    """``extract_themes`` — dispositions / AI outcomes / expected fixes rollup."""

    def test_matches_snapshot(self, build_report_monthly,
                              fix_repeat_caller_deep_dive):
        actual = build_report_monthly.extract_themes(fix_repeat_caller_deep_dive)
        expected = _load_snapshot("extract_themes")
        assert _normalise(_roundtrip(actual)) == expected
