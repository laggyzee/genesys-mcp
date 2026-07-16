"""Recent conversation-detail fallback for archive-lagged reporting days."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from genesys_mcp import _conversation_details as details
from genesys_mcp.tools.reports import _is_customer_participant


class FakeAnalyticsApi:
    def __init__(self, pages: dict[int, dict] | None = None, error: Exception | None = None):
        self.pages = pages or {}
        self.error = error
        self.bodies: list[dict] = []

    def post_analytics_conversations_details_query(self, body):
        self.bodies.append(body)
        if self.error:
            raise self.error
        return self.pages[body["paging"]["pageNumber"]]


BODY = {
    "interval": "2026-07-14T14:00:00Z/2026-07-15T14:00:00Z",
    "order": "asc",
    "orderBy": "conversationStart",
}


@pytest.fixture(autouse=True)
def clear_cache():
    details.clear_conversation_details_cache()
    yield
    details.clear_conversation_details_cache()


def install_api(monkeypatch, api):
    monkeypatch.setattr(details, "get_api", lambda: object())
    monkeypatch.setattr(details.gc, "AnalyticsApi", lambda _client: api)
    monkeypatch.setattr(details, "to_dict", lambda value: value)


def incomplete_availability(monkeypatch):
    monkeypatch.setattr(details, "conversation_data_availability", lambda _api, _end: {
        "complete": False,
        "data_available_until": "2026-07-15T07:13:14Z",
        "note": "archive behind",
    })


def test_recent_query_paginates_to_total_hits_and_marks_provisional(monkeypatch):
    api = FakeAnalyticsApi({
        1: {"totalHits": 3, "conversations": [{"conversationId": "c1"}, {"conversationId": "c2"}]},
        2: {"totalHits": 3, "conversations": [{"conversationId": "c3"}]},
    })
    install_api(monkeypatch, api)
    incomplete_availability(monkeypatch)

    result = details.fetch_conversation_details(BODY)

    assert [row["conversationId"] for row in result["conversations"]] == ["c1", "c2", "c3"]
    assert result["data_complete"] is True
    assert result["archive_data_complete"] is False
    assert result["data_provisional"] is True
    assert result["data_source"] == "analytics_conversations_details_query_recent"
    assert result["fallback_validation"] == {
        "reconciled": True,
        "total_hits": 3,
        "fetched_count": 3,
        "unique_conversation_count": 3,
        "duplicate_count": 0,
        "missing_id_count": 0,
        "inconsistent_total_hits": False,
        "truncated": False,
    }
    assert [body["paging"] for body in api.bodies] == [
        {"pageSize": 100, "pageNumber": 1},
        {"pageSize": 100, "pageNumber": 2},
    ]


def test_recent_query_stays_incomplete_when_page_budget_truncates(monkeypatch):
    api = FakeAnalyticsApi({1: {"totalHits": 2, "conversations": [{"conversationId": "c1"}]}})
    install_api(monkeypatch, api)
    incomplete_availability(monkeypatch)

    result = details.fetch_conversation_details(BODY, max_pages=1)

    assert result["data_complete"] is False
    assert result["data_provisional"] is False
    assert result["fallback_validation"]["truncated"] is True


def test_duplicate_ids_fail_reconciliation(monkeypatch):
    api = FakeAnalyticsApi({1: {"totalHits": 2, "conversations": [{"conversationId": "c1"}, {"conversationId": "c1"}]}})
    install_api(monkeypatch, api)
    incomplete_availability(monkeypatch)

    result = details.fetch_conversation_details(BODY)

    assert result["data_complete"] is False
    assert result["fallback_validation"]["duplicate_count"] == 1


def test_archive_complete_uses_authoritative_job(monkeypatch):
    install_api(monkeypatch, FakeAnalyticsApi())
    monkeypatch.setattr(details, "conversation_data_availability", lambda _api, _end: {
        "complete": True,
        "data_available_until": "2026-07-15T14:00:00Z",
        "note": None,
    })
    monkeypatch.setattr(details, "run_conversation_details_job", lambda _body, _pages: ([{"conversationId": "archive-1"}], False))

    result = details.fetch_conversation_details(BODY)

    assert result["data_source"] == "analytics_conversations_details_jobs"
    assert result["data_complete"] is True
    assert result["data_provisional"] is False


def test_sync_failure_falls_back_to_partial_archive(monkeypatch):
    install_api(monkeypatch, FakeAnalyticsApi(error=RuntimeError("query unavailable")))
    incomplete_availability(monkeypatch)
    monkeypatch.setattr(details, "run_conversation_details_job", lambda _body, _pages: ([{"conversationId": "archive-1"}], False))

    result = details.fetch_conversation_details(BODY)

    assert result["data_source"] == "analytics_conversations_details_jobs_partial"
    assert result["data_complete"] is False
    assert result["fallback_validation"]["reconciled"] is False


def test_recent_result_is_cached_for_identical_query(monkeypatch):
    api = FakeAnalyticsApi({1: {"totalHits": 1, "conversations": [{"conversationId": "c1"}]}})
    install_api(monkeypatch, api)
    incomplete_availability(monkeypatch)

    first = details.fetch_conversation_details(BODY)
    second = details.fetch_conversation_details(BODY)

    assert first == second
    assert len(api.bodies) == 1


def test_recent_external_and_archive_customer_purposes_both_identify_the_caller_leg():
    assert _is_customer_participant("external") is True
    assert _is_customer_participant("customer") is True
    assert _is_customer_participant("agent") is False
