"""Pin :mod:`genesys_mcp.shapes` — Genesys response envelope validators.

The validators exist to fail loud on the silent-empty bug class fixed in
v0.9.1 / v0.9.2 / v0.10. Each test pins one historical bug or one
known-dangerous shape mismatch.
"""
from __future__ import annotations

import pytest

from genesys_mcp.shapes import (
    ShapeError,
    assert_aggregates_envelope,
    assert_break_overrun_report,
    assert_conversation_detail_list,
    assert_repeat_caller_deep_dive,
    assert_users_aggregates_envelope,
)


class TestAggregatesEnvelope:
    def test_valid_aggregates_payload_passes(self):
        payload = {"results": [{
            "group": {"userId": "u1", "mediaType": "voice"},
            "data": [{"metrics": [{"metric": "tAnswered", "stats": {"count": 10}}]}],
        }]}
        assert_aggregates_envelope(payload)

    def test_empty_results_is_valid(self):
        assert_aggregates_envelope({"results": []})

    def test_non_dict_root_raises(self):
        with pytest.raises(ShapeError, match="expected dict at <root>"):
            assert_aggregates_envelope([1, 2, 3])

    def test_missing_results_raises(self):
        with pytest.raises(ShapeError, match=r"expected list at \.results"):
            assert_aggregates_envelope({"data": []})

    def test_source_appears_in_error_message(self):
        with pytest.raises(ShapeError, match="agent_performance:"):
            assert_aggregates_envelope({"results": [{"group": "wrong"}]},
                                       source="agent_performance")

    def test_expect_derived_passes_when_derived_present(self):
        payload = {"results": [{
            "group": {"queueId": "q1", "mediaType": "voice"},
            "data": [{
                "metrics": [],
                "derived": {"answered": 100, "service_level": 0.82},
            }],
        }]}
        assert_aggregates_envelope(payload, expect_derived=True, source="queue_performance")

    def test_expect_derived_raises_on_agent_performance_shape(self):
        # This is the bug class fixed in three files across v0.9.1 + v0.9.2:
        # caller asked for derived block on an agent_performance response.
        agent_perf = {"results": [{
            "group": {"userId": "u1", "mediaType": "voice"},
            "data": [{"metrics": [{"metric": "tAnswered", "stats": {"count": 19}}]}],
        }]}
        with pytest.raises(ShapeError, match="this looks like agent_performance shape"):
            assert_aggregates_envelope(agent_perf, expect_derived=True)

    def test_expect_derived_permissive_when_results_empty(self):
        # No results → can't fail "no derived found"; that's not a shape bug.
        assert_aggregates_envelope({"results": []}, expect_derived=True)


class TestConversationDetailList:
    def test_valid_list_passes(self):
        convs = [
            {"conversationId": "abc", "participants": [{"userId": "u1", "sessions": []}]},
            {"conversationId": "def", "participants": []},
        ]
        assert_conversation_detail_list(convs)

    def test_non_list_raises(self):
        with pytest.raises(ShapeError, match="expected list at <root>"):
            assert_conversation_detail_list({"conversationId": "abc"})

    def test_missing_conversation_id_raises(self):
        with pytest.raises(ShapeError, match=r"conversationId"):
            assert_conversation_detail_list([{"participants": []}])


class TestRepeatCallerDeepDive:
    def test_repeaters_key_passes(self):
        deep = {"repeaters": [{"ani": "+61400000001"}], "org_rollup": {}}
        assert_repeat_caller_deep_dive(deep)

    def test_legacy_unresolved_repeaters_key_passes(self):
        # Backwards-compat fallback that cc-daily-brief uses post-v0.9.2.
        deep = {"unresolved_repeaters": [{"ani": "+61400000002"}]}
        assert_repeat_caller_deep_dive(deep)

    def test_both_keys_missing_raises_v092_mis_key_bug(self):
        # The exact bug fixed in v0.9.2. The validator pins it so a future
        # refactor reading the wrong key gets a clear error message.
        with pytest.raises(ShapeError, match="v0.9.2 daily-brief mis-key bug class"):
            assert_repeat_caller_deep_dive({"org_rollup": {}, "scope": {}})

    def test_repeaters_must_be_list(self):
        with pytest.raises(ShapeError, match=r"expected list at \.repeaters"):
            assert_repeat_caller_deep_dive({"repeaters": "not a list"})


class TestBreakOverrunReport:
    def _user_row(self, **overrides) -> dict:
        row = {
            "user_id": "u1",
            "total_overrun_min": 0,
            "pre_break_overrun_total_min": 0,
            "away_total_min": 0,
            "overrun_count": 0,
            "pre_break_overrun_count": 0,
        }
        row.update(overrides)
        return row

    def test_valid_payload_passes(self):
        brk = {"users": [self._user_row(total_overrun_min=12.3)]}
        assert_break_overrun_report(brk)

    def test_missing_pre_break_total_raises(self):
        # Pins the v0.9.2 bug: code that read total_overrun_min but ignored
        # pre_break_overrun_total_min silently lost the dominant signal.
        # If the field is missing from the payload entirely, fail loud.
        row = self._user_row()
        del row["pre_break_overrun_total_min"]
        with pytest.raises(ShapeError, match="pre_break_overrun_total_min"):
            assert_break_overrun_report({"users": [row]})

    def test_missing_users_key_raises(self):
        with pytest.raises(ShapeError, match=r"expected list at \.users"):
            assert_break_overrun_report({"interval": "..."})


class TestUsersAggregatesEnvelope:
    """v1.6 — pin the new users/aggregates response envelope shape."""

    def test_valid_routing_status_payload_passes(self):
        payload = {"results": [{
            "group": {"userId": "u1", "routingStatus": "ON_QUEUE"},
            "data": [{
                "interval": "2026-06-15T14:00:00.000Z/2026-06-22T14:00:00.000Z",
                "metrics": [{
                    "metric": "tAgentRoutingStatus",
                    "stats": {"sum": 21600000, "count": 12},
                }],
            }],
        }]}
        assert_users_aggregates_envelope(payload)

    def test_empty_results_passes(self):
        # Empty is valid — represents a queried interval with no agent
        # activity (legitimate for off-hours intervals).
        assert_users_aggregates_envelope({"results": []})

    def test_missing_user_id_raises(self):
        # If Genesys ever changes the group dict shape (e.g. renames userId),
        # this fires loudly rather than silently emitting zeros.
        payload = {"results": [{
            "group": {"routingStatus": "ON_QUEUE"},
            "data": [],
        }]}
        with pytest.raises(ShapeError, match="missing 'userId'"):
            assert_users_aggregates_envelope(payload)

    def test_non_dict_root_raises(self):
        with pytest.raises(ShapeError, match="expected dict at <root>"):
            assert_users_aggregates_envelope([1, 2, 3])

    def test_source_appears_in_error_message(self):
        with pytest.raises(ShapeError, match="agent_utilization:"):
            assert_users_aggregates_envelope(
                {"results": [{"group": {"routingStatus": "ON_QUEUE"}, "data": []}]},
                source="agent_utilization",
            )
