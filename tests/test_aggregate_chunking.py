"""Pin v1.14 P6 interval-chunking helpers in ``_aggregates``.

Multi-year analytics-aggregate spans are rejected/capped by Genesys, so long
intervals must be split into ≤12-month sub-queries and merged. These tests pin:

- short intervals pass through unchanged (single element / single call)
- long intervals split into ≤12-month chunks that tile the whole span with no
  gaps or overlaps
- merged responses concatenate ``data`` buckets per group (so the existing
  bucket-summing parsers still get correct totals)
- ``run_chunked_query`` fires one call per chunk and merges
"""
from __future__ import annotations

from genesys_mcp._aggregates import (
    merge_aggregate_results,
    run_chunked_query,
    split_interval_by_months,
)


class TestSplitInterval:
    def test_short_interval_passes_through_unchanged(self):
        iv = "2026-06-01T00:00:00.000Z/2026-06-22T00:00:00.000Z"
        assert split_interval_by_months(iv, max_months=12) == [iv]

    def test_exactly_twelve_months_is_one_chunk(self):
        iv = "2025-06-01T00:00:00.000Z/2026-06-01T00:00:00.000Z"
        assert split_interval_by_months(iv, max_months=12) == [iv]

    def test_three_years_splits_into_three_chunks_that_tile(self):
        iv = "2023-06-01T00:00:00.000Z/2026-06-01T00:00:00.000Z"
        chunks = split_interval_by_months(iv, max_months=12)
        assert len(chunks) == 3
        # Each chunk's end is the next chunk's start — no gaps, no overlaps.
        starts_ends = [c.split("/") for c in chunks]
        for (_, end), (nstart, _) in zip(starts_ends, starts_ends[1:]):
            assert end == nstart
        # Full span preserved end-to-end.
        assert starts_ends[0][0] == "2023-06-01T00:00:00.000Z"
        assert starts_ends[-1][1] == "2026-06-01T00:00:00.000Z"

    def test_chunk_size_respects_max_months(self):
        iv = "2024-01-01T00:00:00.000Z/2026-01-01T00:00:00.000Z"
        chunks = split_interval_by_months(iv, max_months=6)
        assert len(chunks) == 4  # 24 months / 6

    def test_jan_31_start_does_not_crash_on_short_month(self):
        # Stepping +1 month from Jan 31 would land on a non-existent Feb 31.
        iv = "2025-01-31T00:00:00.000Z/2026-07-31T00:00:00.000Z"
        chunks = split_interval_by_months(iv, max_months=1)
        assert len(chunks) >= 12
        # Still tiles to the end.
        assert chunks[-1].split("/")[1] == "2026-07-31T00:00:00.000Z"


class TestMergeResults:
    def test_concatenates_data_buckets_per_group(self):
        r1 = {"results": [{"group": {"userId": "u1"}, "data": [{"interval": "a"}]}]}
        r2 = {"results": [{"group": {"userId": "u1"}, "data": [{"interval": "b"}]}]}
        merged = merge_aggregate_results([r1, r2])
        assert len(merged["results"]) == 1
        assert merged["results"][0]["group"] == {"userId": "u1"}
        assert [d["interval"] for d in merged["results"][0]["data"]] == ["a", "b"]

    def test_distinct_groups_stay_separate(self):
        r1 = {"results": [{"group": {"userId": "u1"}, "data": [1]}]}
        r2 = {"results": [{"group": {"userId": "u2"}, "data": [2]}]}
        merged = merge_aggregate_results([r1, r2])
        groups = [g["group"]["userId"] for g in merged["results"]]
        assert sorted(groups) == ["u1", "u2"]

    def test_empty_and_missing_results_are_safe(self):
        assert merge_aggregate_results([{}, {"results": []}, {"results": None}]) == {"results": []}


class TestRunChunkedQuery:
    def test_single_chunk_calls_query_fn_once_with_original_interval(self):
        iv = "2026-06-01T00:00:00.000Z/2026-06-08T00:00:00.000Z"
        seen: list[str] = []

        def q(interval: str) -> dict:
            seen.append(interval)
            return {"results": []}

        run_chunked_query(q, iv)
        assert seen == [iv]

    def test_long_interval_fires_one_call_per_chunk_and_merges(self):
        iv = "2023-06-01T00:00:00.000Z/2026-06-01T00:00:00.000Z"
        seen: list[str] = []

        def q(interval: str) -> dict:
            seen.append(interval)
            # one bucket per chunk, same group → must concatenate to 3
            return {"results": [{"group": {"userId": "u1"}, "data": [{"iv": interval}]}]}

        out = run_chunked_query(q, iv, max_months=12)
        assert len(seen) == 3
        assert len(out["results"]) == 1
        assert len(out["results"][0]["data"]) == 3
