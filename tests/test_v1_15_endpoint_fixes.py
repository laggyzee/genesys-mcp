"""Regression/contract tests for the v1.14 endpoint-correctness fixes.

Each class below pins one bug fixed in v1.14 — the shape/behaviour that,
pre-fix, was silently wrong (wrong request shape, wrong response field,
wrong endpoint, or a fabricated field). These are the tests that would
have caught the original bug.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _call_tool(register_fn, tool_name: str, args: dict):
    """Register a tool module against a fresh FastMCP app and invoke it.

    Mirrors the pattern used across the existing test suite (see
    tests/test_timeoff.py, tests/test_analytics_filters.py).
    """
    from mcp.server.fastmcp import FastMCP

    app = FastMCP(name="t")
    register_fn(app)
    result = asyncio.run(app.call_tool(tool_name, args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text)


def _call_tool_capturing(register_fn, tool_name: str, args: dict) -> None:
    """Invoke a tool, swallowing an expected fixture-raised exception."""
    from mcp.server.fastmcp import FastMCP

    app = FastMCP(name="t")
    register_fn(app)
    try:
        asyncio.run(app.call_tool(tool_name, args))
    except Exception:
        pass


# ── 1. search_conversations: predicates belong in segmentFilters ──


class TestSearchConversationsFilterRouting:
    def test_ani_queue_user_direction_go_in_segment_filters(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import conversations

        captured: list[dict] = []

        def fake_query(self, body, **kwargs):
            captured.append(body)
            raise RuntimeError("captured-and-stopped (test fixture)")

        monkeypatch.setattr(
            gc.AnalyticsApi, "post_analytics_conversations_details_query", fake_query,
        )
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        _call_tool_capturing(conversations.register, "search_conversations", {
            "ani": "+61412345678",
            "queue_id": "q1",
            "user_id": "u1",
            "direction": "inbound",
        })

        assert captured, "tool didn't reach the analytics query"
        body = captured[0]
        assert "conversationFilters" not in body, (
            "ani/queueId/userId/direction are segment-level dimensions — "
            "conversationFilters silently ignores them"
        )
        assert "segmentFilters" in body
        predicates = body["segmentFilters"][0]["predicates"]
        dims = {p["dimension"] for p in predicates}
        assert dims == {"ani", "queueId", "userId", "direction"}
        values = {p["dimension"]: p["value"] for p in predicates}
        assert values["ani"] == "+61412345678"
        assert values["queueId"] == "q1"
        assert values["userId"] == "u1"
        assert values["direction"] == "inbound"

    def test_no_predicates_when_no_filters_given(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import conversations

        captured: list[dict] = []

        def fake_query(self, body, **kwargs):
            captured.append(body)
            raise RuntimeError("captured-and-stopped (test fixture)")

        monkeypatch.setattr(
            gc.AnalyticsApi, "post_analytics_conversations_details_query", fake_query,
        )
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        _call_tool_capturing(conversations.register, "search_conversations", {})
        body = captured[0]
        assert "segmentFilters" not in body
        assert "conversationFilters" not in body


# ── 2. routing._fetch_queue_members: pageSize capped at server max (100) ──


class TestFetchQueueMembersPaging:
    def test_page_size_is_100_not_200(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import routing

        captured_kwargs: list[dict] = []

        def fake_get_members(self, **kwargs):
            captured_kwargs.append(kwargs)
            return {"entities": [{"id": "m1"}]}

        monkeypatch.setattr(gc.RoutingApi, "get_routing_queue_members", fake_get_members)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        routing._fetch_queue_members("q1")
        assert captured_kwargs
        for kw in captured_kwargs:
            assert kw["page_size"] == 100, (
                "page_size must be <= the Genesys server max of 100; "
                f"got {kw['page_size']}"
            )

    def test_terminates_on_short_page_not_full_200(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import routing

        pages = [
            {"entities": [{"id": f"m{i}"} for i in range(100)]},
            {"entities": [{"id": "m100"}]},  # short page — should stop here
        ]
        calls = {"n": 0}

        def fake_get_members(self, **kwargs):
            page = pages[calls["n"]]
            calls["n"] += 1
            return page

        monkeypatch.setattr(gc.RoutingApi, "get_routing_queue_members", fake_get_members)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        members = routing._fetch_queue_members("q1", max_pages=5)
        assert len(members) == 101
        assert calls["n"] == 2, "should stop after the first page shorter than 100"


# ── 3. routing outcome classification: shared helpers, no fabricated fields ──


class TestClassifyOutcome:
    def test_answered_when_agent_interact_segment_present(self):
        from genesys_mcp.tools.routing import _classify_outcome

        conv = {"participants": [
            {"purpose": "agent", "sessions": [{"segments": [
                {"segmentType": "interact", "segmentStart": "t1", "segmentEnd": "t2"},
            ]}]},
        ]}
        out = _classify_outcome(conv)
        assert out["verdict"] == "answered"
        assert out["first_answer_at"] == "t1"

    def test_abandoned_when_no_agent_interact_segment_anywhere(self):
        from genesys_mcp.tools.routing import _classify_outcome

        conv = {"participants": [
            {"purpose": "customer", "sessions": [{"segments": [
                {"segmentType": "ivr", "segmentStart": "t1", "segmentEnd": "t2"},
            ]}]},
        ]}
        out = _classify_outcome(conv)
        assert out["verdict"] == "abandoned"

    def test_transfer_counted_via_disconnect_type(self):
        from genesys_mcp.tools.routing import _classify_outcome

        conv = {"participants": [
            {"purpose": "agent", "sessions": [{"segments": [
                {"segmentType": "interact", "disconnectType": "transfer",
                 "segmentStart": "t1", "segmentEnd": "t2"},
            ]}]},
        ]}
        out = _classify_outcome(conv)
        assert out["transfer_count"] == 1

    def test_invalid_segment_type_transfer_value_is_ignored(self):
        # "transfer" is not a valid segmentType in the Genesys schema — a
        # segment claiming it must NOT be counted as a transfer. Only
        # disconnectType drives transfer detection post-fix.
        from genesys_mcp.tools.routing import _classify_outcome

        conv = {"participants": [
            {"purpose": "agent", "sessions": [{"segments": [
                {"segmentType": "transfer", "segmentStart": "t1", "segmentEnd": "t2"},
            ]}]},
        ]}
        out = _classify_outcome(conv)
        assert out["transfer_count"] == 0
        # Still answered, since segmentType 'interact' isn't present but
        # this segment isn't an agent-interact either — verdict here is
        # abandoned (no real interact segment exists).
        assert out["verdict"] == "abandoned"

    def test_no_flagged_reason_field_in_output(self):
        # abandon_reason/flaggedReason don't exist in the Genesys schema —
        # the pre-fix output carried a fabricated abandon_reason key.
        from genesys_mcp.tools.routing import _classify_outcome

        conv = {"participants": []}
        out = _classify_outcome(conv)
        assert "abandon_reason" not in out


class TestSharedAbandonTransferHelpers:
    """_classify_outcome and routing_diagnostic_aggregate._matches_outcome
    now both call these — pin their standalone behaviour directly.
    """

    def test_has_agent_interact_segment(self):
        from genesys_mcp.tools.routing import _has_agent_interact_segment

        assert _has_agent_interact_segment({"participants": [
            {"purpose": "agent", "sessions": [{"segments": [
                {"segmentType": "interact"},
            ]}]},
        ]}) is True
        assert _has_agent_interact_segment({"participants": [
            {"purpose": "customer", "sessions": [{"segments": [
                {"segmentType": "interact"},
            ]}]},
        ]}) is False

    def test_count_transfer_segments_uses_disconnect_type(self):
        from genesys_mcp.tools.routing import _count_transfer_segments

        conv = {"participants": [
            {"purpose": "customer", "sessions": [{"segments": [
                {"segmentType": "interact", "disconnectType": "consultTransfer"},
                {"segmentType": "hold", "disconnectType": "client"},
            ]}]},
        ]}
        assert _count_transfer_segments(conv) == 1


# ── 4. routing eligibility: no fabricated required_skill_ids ──


class TestEligibleMemberCount:
    def test_no_active_skill_ids_does_not_fabricate_eligibility(self):
        from genesys_mcp.tools.routing import _eligible_member_count

        members = [
            {"user": {"id": "u1", "name": "A", "skills": []},
             "routingStatus": {"status": "IDLE"}},
            {"user": {"id": "u2", "name": "B", "skills": []},
             "routingStatus": {"status": "INTERACTING"}},
        ]
        result = _eligible_member_count(members, active_skill_ids=None)
        assert result["total_members"] == 2
        assert result["idle_now"] == 1
        assert result["eligible_now"] is None
        assert "required_skill_ids" not in result

    def test_active_skill_ids_filters_by_user_skills_not_queue(self):
        from genesys_mcp.tools.routing import _eligible_member_count

        members = [
            {"user": {"id": "u1", "name": "A", "skills": [{"id": "sk1"}]},
             "routingStatus": {"status": "IDLE"}},
            {"user": {"id": "u2", "name": "B", "skills": []},
             "routingStatus": {"status": "IDLE"}},
        ]
        result = _eligible_member_count(members, active_skill_ids=["sk1"])
        assert result["total_members"] == 2
        assert result["eligible_now"] == 1
        assert result["idle_eligible_now"] == 1
        assert result["required_skill_ids"] == ["sk1"]


# ── 5. attribute_search: cursor pagination, not pageSize/pageNumber ──


class TestAttributeSearchCursorPagination:
    def test_request_has_no_page_size_or_page_number(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import attribute_search

        calls: list[dict] = []

        def fake_call_api(self, **kwargs):
            calls.append(kwargs)
            return {"results": [], "cursor": None}

        monkeypatch.setattr(gc.ApiClient, "call_api", fake_call_api)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        _call_tool(attribute_search.register, "search_conversations_by_attribute", {
            "attribute_key": "NPS Score",
        })
        assert calls
        body = calls[0]["body"]
        assert "pageSize" not in body
        assert "pageNumber" not in body
        assert "cursor" not in body  # first request has no cursor yet

    def test_advances_via_returned_cursor_and_stops_when_absent(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import attribute_search

        responses = [
            {"results": [{"conversationId": "c1",
                          "participants": [{"attributes": {"NPS Score": "9"}}]}],
             "cursor": "cursor-abc"},
            {"results": [{"conversationId": "c2",
                          "participants": [{"attributes": {"NPS Score": "10"}}]}],
             "cursor": None},
        ]
        calls: list[dict] = []

        def fake_call_api(self, **kwargs):
            calls.append(kwargs)
            return responses[len(calls) - 1]

        monkeypatch.setattr(gc.ApiClient, "call_api", fake_call_api)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        out = _call_tool(attribute_search.register, "search_conversations_by_attribute", {
            "attribute_key": "NPS Score",
        })

        assert len(calls) == 2, "should stop once the response has no cursor"
        assert "cursor" not in calls[0]["body"]
        assert calls[1]["body"]["cursor"] == "cursor-abc"
        assert out["totals"]["conversation_count"] == 2
        assert out["totals"]["truncated"] is False

    def test_truncated_only_when_cap_hit_with_cursor_still_present(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import attribute_search

        def fake_call_api(self, **kwargs):
            # Always returns one result and a cursor — never terminates on
            # its own, forcing the iteration cap to kick in.
            return {
                "results": [{"conversationId": "c",
                              "participants": [{"attributes": {"NPS Score": "5"}}]}],
                "cursor": "keep-going",
            }

        monkeypatch.setattr(gc.ApiClient, "call_api", fake_call_api)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        out = _call_tool(attribute_search.register, "search_conversations_by_attribute", {
            "attribute_key": "NPS Score",
            "max_results": 10000,  # higher than what 20 iterations will produce
        })
        assert out["totals"]["truncated"] is True


# ── 6. wfm.wfm_schedule: MU auto-discovery via the real SDK method ──


class TestWfmScheduleManagementUnitDiscovery:
    def test_discovers_mu_via_agent_managementunit_not_raw_users_path(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import wfm

        call_api_paths: list[str] = []

        def fake_call_api(self, **kwargs):
            path = kwargs.get("resource_path", "")
            call_api_paths.append(path)
            if "/weeks/" in path and path.endswith("/schedules"):
                return {"entities": [
                    {"id": "sched-1", "weekDate": "2026-06-01",
                     "weekCount": 1, "published": True},
                ]}
            if path.endswith("/headcountforecast"):
                return {"result": {"entities": []}}
            if path.endswith("/schedules/search"):
                return {"userSchedules": {}}
            raise AssertionError(f"unexpected call_api path: {path}")

        mu_lookup_calls: list[str] = []

        def fake_get_mu(self, agent_id, **kwargs):
            mu_lookup_calls.append(agent_id)
            return {"managementUnit": {"id": "mu-99"}}

        monkeypatch.setattr(gc.ApiClient, "call_api", fake_call_api)
        monkeypatch.setattr(
            gc.WorkforceManagementApi,
            "get_workforcemanagement_agent_managementunit",
            fake_get_mu,
        )
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        out = _call_tool(wfm.register, "wfm_schedule", {
            "business_unit_id": "bu-1",
            "management_unit_ids": [],
            "user_ids": ["u1"],
            "interval": "2026-06-01T00:00:00.000Z/2026-06-07T00:00:00.000Z",
        })

        assert mu_lookup_calls == ["u1"], (
            "MU discovery must call get_workforcemanagement_agent_managementunit "
            "per user"
        )
        assert not any(
            "/workforcemanagement/users/" in p for p in call_api_paths
        ), "the nonexistent raw GET /workforcemanagement/users/{userId} path must not be called"
        assert out["management_unit_ids"] == ["mu-99"]


# ── 7. wfm.agent_adherence_review: result.entities + explanations_available ──


class TestAgentAdherenceReviewExplanationsShape:
    def _install_common_fakes(self, monkeypatch, adherence_responses: dict):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client

        def fake_submit(self, body, **kwargs):
            return {"jobId": "job-1"}

        def fake_status(self, job_id, **kwargs):
            return {"state": "FULFILLED"}

        def fake_results(self, job_id, **kwargs):
            if kwargs.get("cursor"):
                return {"userDetails": [], "cursor": None}
            return {
                "userDetails": [
                    {
                        "userId": uid,
                        "primaryPresence": [{
                            "systemPresence": "BREAK",
                            "startTime": "2026-06-01T09:00:00.000Z",
                            "endTime": "2026-06-01T09:30:00.000Z",
                        }],
                    }
                    for uid in adherence_responses
                ],
                "cursor": None,
            }

        def fake_adherence(self, agent_id, body, **kwargs):
            return adherence_responses[agent_id]

        monkeypatch.setattr(
            gc.AnalyticsApi, "post_analytics_users_details_jobs", fake_submit,
        )
        monkeypatch.setattr(
            gc.AnalyticsApi, "get_analytics_users_details_job", fake_status,
        )
        monkeypatch.setattr(
            gc.AnalyticsApi, "get_analytics_users_details_job_results", fake_results,
        )
        monkeypatch.setattr(
            gc.WorkforceManagementApi,
            "post_workforcemanagement_agent_adherence_explanations_query",
            fake_adherence,
        )
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())
        monkeypatch.setattr(
            "genesys_mcp.naming.resolver.user_names",
            lambda uids: {uid: f"User {uid}" for uid in uids},
        )

    def test_explanations_available_true_reads_result_entities(self, monkeypatch):
        from genesys_mcp.tools import wfm

        self._install_common_fakes(monkeypatch, {
            "u1": {
                "result": {"entities": [{
                    "startDate": "2026-06-01T08:50:00.000Z",
                    "endDate": "2026-06-01T09:35:00.000Z",
                    "type": "Training",
                    "status": "Approved",
                }]},
            },
        })

        out = _call_tool(wfm.register, "agent_adherence_review", {
            "user_ids": ["u1"],
            "interval": "2026-06-01T00:00:00.000Z/2026-06-02T00:00:00.000Z",
        })
        row = out["users"][0]
        assert row["explanations_available"] is True
        assert row["explained_overruns"] == 1
        assert row["unexplained_overruns"] == 0
        assert row["overruns_unknown"] == 0

    def test_explanations_available_false_on_async_result_does_not_mark_unexplained(
        self, monkeypatch,
    ):
        from genesys_mcp.tools import wfm

        self._install_common_fakes(monkeypatch, {
            # No "result" key — mirrors the 202-async response shape
            # (job/downloadUrl present, result not yet ready).
            "u1": {"job": {"id": "job-async"}, "downloadUrl": None},
        })

        out = _call_tool(wfm.register, "agent_adherence_review", {
            "user_ids": ["u1"],
            "interval": "2026-06-01T00:00:00.000Z/2026-06-02T00:00:00.000Z",
        })
        row = out["users"][0]
        assert row["explanations_available"] is False
        assert row["unexplained_overruns"] == 0, (
            "a fetch failure/async-pending must NOT be counted as "
            "'unexplained' — that would falsely flag a real overrun"
        )
        assert row["overruns_unknown"] == 1

    def test_explanations_available_false_on_fetch_error(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp.tools import wfm

        self._install_common_fakes(monkeypatch, {"u1": {}})

        def raising_adherence(self, agent_id, body, **kwargs):
            raise gc.rest.ApiException(status=500, reason="boom")

        monkeypatch.setattr(
            gc.WorkforceManagementApi,
            "post_workforcemanagement_agent_adherence_explanations_query",
            raising_adherence,
        )

        out = _call_tool(wfm.register, "agent_adherence_review", {
            "user_ids": ["u1"],
            "interval": "2026-06-01T00:00:00.000Z/2026-06-02T00:00:00.000Z",
        })
        row = out["users"][0]
        assert row["explanations_available"] is False
        assert row["unexplained_overruns"] == 0


# ── 8. client.init_api: 401-refresh re-authenticates in place ──


class TestInitApiReauthInPlace:
    def test_refresh_reuses_same_client_object_and_refreshes_token(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client

        monkeypatch.setenv("GENESYS_CLIENT_ID", "id-1")
        monkeypatch.setenv("GENESYS_CLIENT_SECRET", "secret-1")
        monkeypatch.setenv("GENESYS_REGION", "ap-southeast-2")
        monkeypatch.setattr(gen_client, "_api_client", None)

        token_calls: list[tuple] = []

        def fake_get_token(self, client_id, client_secret):
            token_calls.append((client_id, client_secret))
            self.access_token = f"token-{len(token_calls)}"

        monkeypatch.setattr(gc.ApiClient, "get_client_credentials_token", fake_get_token)

        client1 = gen_client.init_api()
        assert client1.access_token == "token-1"

        client2 = gen_client.init_api()
        assert client2 is client1, (
            "init_api must re-authenticate the SAME ApiClient object on "
            "refresh, not build a new one that stale references can't see"
        )
        assert client2.access_token == "token-2"
        assert len(token_calls) == 2


# ── 9. client.with_retry_for: non-numeric Retry-After doesn't crash ──


class TestRetryAfterHeaderParsing:
    def test_http_date_retry_after_falls_back_to_default(self, monkeypatch):
        from PureCloudPlatformClientV2.rest import ApiException

        from genesys_mcp import client as gen_client

        sleeps: list[float] = []
        monkeypatch.setattr(gen_client.time, "sleep", lambda s: sleeps.append(s))

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                exc = ApiException(status=429, reason="rate limited")
                exc.headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
                raise exc
            return "ok"

        wrapped = gen_client.with_retry_for(None)(flaky)
        result = wrapped()

        assert result == "ok"
        assert sleeps == [2.0], (
            "a non-numeric (HTTP-date) Retry-After header must fall back "
            "to the 2.0s default instead of raising ValueError"
        )

    def test_numeric_retry_after_still_respected(self, monkeypatch):
        from PureCloudPlatformClientV2.rest import ApiException

        from genesys_mcp import client as gen_client

        sleeps: list[float] = []
        monkeypatch.setattr(gen_client.time, "sleep", lambda s: sleeps.append(s))

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                exc = ApiException(status=429, reason="rate limited")
                exc.headers = {"Retry-After": "5"}
                raise exc
            return "ok"

        wrapped = gen_client.with_retry_for(None)(flaky)
        wrapped()
        assert sleeps == [5.0]
