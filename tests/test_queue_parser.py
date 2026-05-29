"""Pins :func:`genesys_mcp.queue_parser.parse_queue_name` + match-rate helper.

Pre-v1.0 every skill assumed queue names matched
``cfg.queues.name_pattern`` (default ``{brand} - {channel} - {function}``).
Tenants whose queue names don't fit silently dropped queues from reports.

v1.0 introduces two tenant.yaml knobs (``name_pattern: null`` and
``name_pattern_match_required: false``). These tests pin the resulting
behaviour for the three main shapes: strict (default), permissive
(legacy-mixed tenants), and unstructured (no brand/channel at all).
"""
from __future__ import annotations

from genesys_mcp.queue_parser import (
    QueueParts,
    compute_pattern_match_rate,
    parse_queue_name,
)


class TestParseWithDefaultBrandChannelFunctionPattern:
    PATTERN = "{brand} - {channel} - {function}"

    def test_matching_queue_returns_all_three_parts(self):
        parts = parse_queue_name("Coles - Voice - Sales", self.PATTERN)
        assert parts == QueueParts(
            brand="Coles", channel="Voice", function="Sales", matched=True,
        )

    def test_non_matching_queue_returns_none_when_required(self):
        # Strict mode (default) — non-matching → None, caller skips it.
        assert parse_queue_name("Sales_Voice_AU", self.PATTERN) is None

    def test_non_matching_queue_falls_back_when_not_required(self):
        parts = parse_queue_name(
            "Sales_Voice_AU", self.PATTERN, match_required=False,
        )
        assert parts.matched is False
        assert parts.brand == ""
        assert parts.channel == ""
        assert parts.function == "Sales_Voice_AU"


class TestParseWithBrandOnlyPattern:
    """Some tenants only structure by brand — `BrandName - QueueFunction`."""
    PATTERN = "{brand} - {function}"

    def test_matching_two_part_name(self):
        parts = parse_queue_name("BrandA - Sales", self.PATTERN)
        assert parts.brand == "BrandA"
        assert parts.function == "Sales"
        assert parts.channel == ""
        assert parts.matched is True


class TestParseWithNullPattern:
    """No structured naming at all."""

    def test_every_queue_becomes_function_only(self):
        parts = parse_queue_name("Sales_Voice_AU", pattern=None)
        assert parts.brand == ""
        assert parts.channel == ""
        assert parts.function == "Sales_Voice_AU"
        # `matched` is False because no pattern → no clean match
        assert parts.matched is False

    def test_null_pattern_ignores_match_required(self):
        # match_required doesn't apply when there's no pattern to match.
        parts = parse_queue_name(
            "Anything Goes", pattern=None, match_required=True,
        )
        assert parts is not None
        assert parts.function == "Anything Goes"


class TestParseWithUnderscoreSeparator:
    """Validate that escapeable separators work — not just ' - '."""
    PATTERN = "{brand}_{channel}_{function}"

    def test_underscore_separator(self):
        parts = parse_queue_name("Sales_Voice_AU", self.PATTERN)
        assert parts.brand == "Sales"
        assert parts.channel == "Voice"
        assert parts.function == "AU"


class TestComputePatternMatchRate:
    def test_full_match_returns_one(self):
        names = ["A - B - C", "X - Y - Z"]
        assert compute_pattern_match_rate(names, "{brand} - {channel} - {function}") == 1.0

    def test_partial_match(self):
        names = ["A - B - C", "weird", "X - Y - Z", "also weird"]
        rate = compute_pattern_match_rate(names, "{brand} - {channel} - {function}")
        assert rate == 0.5

    def test_no_match(self):
        names = ["q1", "q2", "q3"]
        assert compute_pattern_match_rate(names, "{brand} - {channel}") == 0.0

    def test_empty_list_returns_one(self):
        # No queues → vacuously matched. (mcp_health_check doesn't warn.)
        assert compute_pattern_match_rate([], "{brand}") == 1.0

    def test_null_pattern_returns_one(self):
        # Caller opted out of structured parsing → vacuously matched.
        assert compute_pattern_match_rate(["a", "b"], None) == 1.0
