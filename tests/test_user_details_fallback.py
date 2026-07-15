"""Recent user-detail fallback and archive-repair contract."""

from __future__ import annotations

import pytest

from genesys_mcp import _user_details as details


INTERVAL = "2026-07-14T00:00:00Z/2026-07-14T01:00:00Z"


def _detail(user_id: str = "u1") -> dict:
    return {
        "userId": user_id,
        "primaryPresence": [
            {
                "systemPresence": "AVAILABLE",
                "startTime": "2026-07-14T00:00:00Z",
                "endTime": "2026-07-14T00:30:00Z",
            },
            {
                "systemPresence": "ON_QUEUE",
                "startTime": "2026-07-14T00:30:00Z",
                "endTime": "2026-07-14T01:00:00Z",
            },
        ],
        "routingStatus": [],
    }


def _aggregates(user_id: str = "u1") -> dict:
    return {
        "results": [{
            "group": {"userId": user_id},
            "data": [{
                "metrics": [
                    {
                        "metric": "tSystemPresence",
                        "qualifier": "AVAILABLE",
                        "stats": {"sum": 1_800_000},
                    },
                    {
                        "metric": "tSystemPresence",
                        "qualifier": "ON_QUEUE",
                        "stats": {"sum": 1_800_000},
                    },
                    {
                        "metric": "tAgentRoutingStatus",
                        "qualifier": "INTERACTING",
                        "stats": {"sum": 600_000},
                    },
                ],
            }],
        }],
    }


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    details.clear_user_details_cache()
    monkeypatch.setattr(details, "get_api", lambda: object())
    monkeypatch.setattr(details, "to_dict", lambda value: value)
    monkeypatch.setattr(details, "with_retry", lambda fn: fn)


def test_settled_archive_remains_authoritative(monkeypatch):
    class Api:
        def __init__(self, *_args): pass

        def post_analytics_users_details_jobs(self, body):
            assert body["interval"] == INTERVAL
            return {"jobId": "job-1"}

        def get_analytics_users_details_job(self, job_id):
            assert job_id == "job-1"
            return {"state": "FULFILLED"}

        def get_analytics_users_details_job_results(self, **_kwargs):
            return {"userDetails": [_detail()]}

    monkeypatch.setattr(details.gc, "AnalyticsApi", Api)
    monkeypatch.setattr(details, "presence_data_availability", lambda *_args: {
        "complete": True,
        "data_available_until": "2026-07-14T02:00:00Z",
        "note": None,
    })

    result = details.fetch_user_details(["u1"], INTERVAL)

    assert result["data_complete"] is True
    assert result["archive_data_complete"] is True
    assert result["data_provisional"] is False
    assert result["data_source"] == "analytics_users_details_jobs"


def test_lagging_archive_uses_reconciled_sync_detail_and_cache(monkeypatch):
    calls = {"details": 0, "aggregates": 0}

    class Api:
        def __init__(self, *_args): pass

        def post_analytics_users_details_query(self, body):
            calls["details"] += 1
            assert body["paging"] == {"pageSize": 100, "pageNumber": 1}
            return {"totalHits": 1, "userDetails": [_detail()]}

        def post_analytics_users_aggregates_query(self, body):
            calls["aggregates"] += 1
            assert body["groupBy"] == ["userId"]
            assert set(body["metrics"]) == {
                "tSystemPresence", "tOrganizationPresence", "tAgentRoutingStatus",
            }
            return _aggregates()

    monkeypatch.setattr(details.gc, "AnalyticsApi", Api)
    monkeypatch.setattr(details, "presence_data_availability", lambda *_args: {
        "complete": False,
        "data_available_until": "2026-07-13T20:00:00Z",
        "note": "archive lagging",
    })

    first = details.fetch_user_details(["u1"], INTERVAL)
    second = details.fetch_user_details(["u1"], INTERVAL)

    assert first == second
    assert first["data_complete"] is True
    assert first["archive_data_complete"] is False
    assert first["data_provisional"] is True
    assert first["fallback_validation"]["reconciled"] is True
    assert calls == {"details": 1, "aggregates": 1}


def test_missing_engaged_user_keeps_fallback_incomplete(monkeypatch):
    class Api:
        def __init__(self, *_args): pass

        def post_analytics_users_details_query(self, body=None, **_kwargs):
            return {"totalHits": 0, "userDetails": []}

        def post_analytics_users_aggregates_query(self, body=None, **_kwargs):
            return _aggregates()

    monkeypatch.setattr(details.gc, "AnalyticsApi", Api)
    monkeypatch.setattr(details, "presence_data_availability", lambda *_args: {
        "complete": False,
        "data_available_until": "2026-07-13T20:00:00Z",
        "note": "archive lagging",
    })

    result = details.fetch_user_details(["u1"], INTERVAL)

    assert result["data_complete"] is False
    assert result["data_provisional"] is False
    assert result["fallback_validation"]["missing_user_count"] == 1


def test_sync_failure_falls_back_to_partial_archive(monkeypatch):
    class Api:
        def __init__(self, *_args): pass

        def post_analytics_users_details_query(self, **_kwargs):
            raise RuntimeError("sync unavailable")

        def post_analytics_users_details_jobs(self, **_kwargs):
            return {"jobId": "job-1"}

        def get_analytics_users_details_job(self, **_kwargs):
            return {"state": "FULFILLED"}

        def get_analytics_users_details_job_results(self, **_kwargs):
            return {"userDetails": [_detail()]}

    monkeypatch.setattr(details.gc, "AnalyticsApi", Api)
    monkeypatch.setattr(details, "presence_data_availability", lambda *_args: {
        "complete": False,
        "data_available_until": "2026-07-13T20:00:00Z",
        "note": "archive lagging",
    })

    result = details.fetch_user_details(["u1"], INTERVAL)

    assert result["data_complete"] is False
    assert result["archive_data_complete"] is False
    assert result["data_source"] == "analytics_users_details_jobs_partial"
    assert "sync unavailable" in result["fallback_validation"]["error"]
