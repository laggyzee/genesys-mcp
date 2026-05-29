"""Pin :func:`genesys_mcp._aggregates.accumulate_metric_stats`.

Two production sites (coaching.py and reports.py) consume this helper. A
regression in either site would historically appear as silently-truncated
metrics (last bucket only). Testing the shared helper once locks the
contract for every consumer.

History:
- v0.9.1 fixed the equivalent inline accumulator in coaching.py
- v0.10 extracted the logic to _aggregates.py and fixed a second instance
  of the same overwrite bug in reports.py:agent_quality_snapshot.
"""
from __future__ import annotations

import pytest

from genesys_mcp._aggregates import accumulate_metric_stats


class TestAccumulateMetricStats:
    def test_sums_count_and_sum_across_multiple_buckets(self):
        # 4 P7D buckets, mimicking a 24-day interval split into ~4 weeks.
        # tAnswered totals: 100 + 100 + 100 + 95 = 395.
        # tHandle.sum totals: 4 × 32_700_000 ms.
        buckets = [
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
        ]
        out = accumulate_metric_stats(buckets)
        assert out["tAnswered"]["count"] == 395
        assert out["tHandle"]["count"] == 395
        assert out["tHandle"]["sum"] == 4 * 32_700_000

    def test_min_and_max_combine_via_min_and_max(self):
        buckets = [
            {"metrics": [{"metric": "tHandle", "stats": {
                "count": 10, "sum": 100, "min": 5.0, "max": 50.0,
            }}]},
            {"metrics": [{"metric": "tHandle", "stats": {
                "count": 10, "sum": 100, "min": 2.0, "max": 75.0,
            }}]},
        ]
        slot = accumulate_metric_stats(buckets)["tHandle"]
        assert slot["min"] == 2.0
        assert slot["max"] == 75.0
        assert slot["count"] == 20
        assert slot["sum"] == 200

    def test_empty_input_returns_empty_dict(self):
        assert accumulate_metric_stats([]) == {}
        assert accumulate_metric_stats(None) == {}  # type: ignore[arg-type]

    def test_buckets_with_no_metrics_are_skipped(self):
        buckets = [{"metrics": []}, {"metrics": None}, {}]
        assert accumulate_metric_stats(buckets) == {}

    def test_none_stats_fields_do_not_coerce_to_zero(self):
        # Genesys sometimes omits sub-fields entirely or sends explicit nulls.
        # We must skip them, not treat them as 0 — that would corrupt min/max.
        buckets = [
            {"metrics": [{"metric": "tHandle", "stats": {
                "count": 10, "sum": None, "min": None, "max": 50.0,
            }}]},
            {"metrics": [{"metric": "tHandle", "stats": {
                "count": 5, "sum": 1000.0,
            }}]},
        ]
        slot = accumulate_metric_stats(buckets)["tHandle"]
        assert slot["count"] == 15
        assert slot["sum"] == 1000.0  # the None was skipped, not added as 0
        assert slot["max"] == 50.0
        assert "min" not in slot  # never seen a real value
