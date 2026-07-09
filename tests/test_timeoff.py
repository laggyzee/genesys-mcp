"""Pin v1.7 ``wfm_time_off_requests`` + ``wfm_activity_codes``.

The gap these tools close: pre-v1.7 nothing in the codebase queried the
``/api/v2/workforcemanagement/.../timeoffrequests`` endpoint family.
Reports like *"who took leave last 4 weeks?"* were unreachable.

These tests pin:

- Activity-code catalogue fetch + process-lifetime cache reuse
- Request-body shape sent to Genesys (YYYY-MM-DD dateRange, status default,
  user filter propagation)
- Full-day vs partial-day request normalisation (days, hours, dates array)
- Rollups (totals, by_activity, by_user with sort + activity-set union)
- v1.5 envelope contract (top-level ``interval`` + ``as_of_utc``)
- Edge case: empty result
"""
from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture(autouse=True)
def _reset_activity_code_cache():
    """Wipe the process-lifetime cache between tests so they don't leak."""
    from genesys_mcp.tools import timeoff
    timeoff._ACTIVITY_CODE_CACHE.clear()
    yield
    timeoff._ACTIVITY_CODE_CACHE.clear()


def _make_fake_api(*, activity_codes_resp=None, timeoff_pages=None):
    """Build a fake api_client.

    ``activity_codes_resp`` is returned for any GET to /activitycodes.
    ``timeoff_pages`` is a dict {pageNumber: response}; defaults to one
    empty page.
    """
    captured: dict[str, list[dict]] = {
        "activitycodes_calls": [],
        "timeoff_calls": [],
    }
    pages = timeoff_pages or {1: {"entities": []}}

    class FakeApi:
        def call_api(self, **kwargs):
            path = kwargs.get("resource_path", "")
            if path.endswith("/activitycodes"):
                captured["activitycodes_calls"].append(kwargs)
                return activity_codes_resp or {"entities": []}
            if path.endswith("/timeoffrequests/query"):
                body = kwargs.get("body") or {}
                page = body.get("pageNumber", 1)
                captured["timeoff_calls"].append(body)
                return pages.get(page, {"entities": []})
            raise RuntimeError(f"unexpected path: {path}")

    return FakeApi, captured


def _call_tool(tool_name, args, monkeypatch, *, fake_api):
    import PureCloudPlatformClientV2 as gc
    from genesys_mcp import client as gen_client
    from genesys_mcp.tools import timeoff
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(timeoff, "get_api", lambda: fake_api)
    monkeypatch.setattr(
        timeoff.resolver, "user_names",
        lambda uids: {uid: f"User {uid}" for uid in uids},
    )
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

    app = FastMCP(name="t")
    timeoff.register(app)
    result = asyncio.run(app.call_tool(tool_name, args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text)


_BU = "bu-1"
_MU_IDS = ["mu-1"]
_INTERVAL = "2026-05-25T14:00:00.000Z/2026-06-22T14:00:00.000Z"


def _activity_codes_response():
    return {"entities": [
        {"id": "code-annual", "name": "Annual Leave",
         "category": "TimeOff", "countsAsPaidTime": True, "lengthInMinutes": 480, "active": True},
        {"id": "code-sick", "name": "Sick Leave",
         "category": "TimeOff", "countsAsPaidTime": True, "lengthInMinutes": 480, "active": True},
        {"id": "code-personal", "name": "Personal Leave",
         "category": "TimeOff", "countsAsPaidTime": False, "lengthInMinutes": 480, "active": True},
    ]}


def _full_day_request(*, request_id, user_id, activity_id, dates,
                       status="APPROVED", notes=None):
    return {
        "id": request_id,
        "user": {"id": user_id},
        "activityCodeId": activity_id,
        "status": status,
        "isFullDayRequest": True,
        "fullDayManagementUnitDates": dates,
        "notes": notes,
        "reviewedBy": {"id": "sup-1", "name": "Supervisor X"},
        "reviewedDate": "2026-06-01T12:00:00.000Z",
        "submittedDate": "2026-05-20T09:00:00.000Z",
    }


def _partial_day_request(*, request_id, user_id, activity_id,
                          partial_starts, daily_minutes, status="APPROVED"):
    return {
        "id": request_id,
        "user": {"id": user_id},
        "activityCodeId": activity_id,
        "status": status,
        "isFullDayRequest": False,
        "partialDayStartDateTimes": partial_starts,
        "dailyDurationMinutes": daily_minutes,
        "reviewedBy": {"id": "sup-1", "name": "Supervisor X"},
        "reviewedDate": "2026-06-01T12:00:00.000Z",
    }


# ─────────────────────── wfm_activity_codes ───────────────────────


class TestActivityCodes:
    def test_returns_mapped_rows(self, monkeypatch):
        FakeApi, _ = _make_fake_api(activity_codes_resp=_activity_codes_response())
        out = _call_tool("wfm_activity_codes", {"business_unit_id": _BU},
                         monkeypatch, fake_api=FakeApi())
        assert out["business_unit_id"] == _BU
        assert out["count"] == 3
        names = {r["name"] for r in out["activity_codes"]}
        assert names == {"Annual Leave", "Sick Leave", "Personal Leave"}
        annual = next(r for r in out["activity_codes"] if r["name"] == "Annual Leave")
        assert annual["paid"] is True
        assert annual["category"] == "TimeOff"

    def test_second_call_uses_cache(self, monkeypatch):
        FakeApi, captured = _make_fake_api(activity_codes_resp=_activity_codes_response())
        api = FakeApi()
        _call_tool("wfm_activity_codes", {"business_unit_id": _BU},
                   monkeypatch, fake_api=api)
        _call_tool("wfm_activity_codes", {"business_unit_id": _BU},
                   monkeypatch, fake_api=api)
        assert len(captured["activitycodes_calls"]) == 1, (
            "wfm_activity_codes must cache the catalogue process-lifetime — "
            "second invocation should not re-hit /activitycodes"
        )


# ─────────────────────── request body shape ───────────────────────


class TestRequestBodyShape:
    def test_date_range_is_yyyy_mm_dd(self, monkeypatch):
        FakeApi, captured = _make_fake_api(activity_codes_resp=_activity_codes_response())
        _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        body = captured["timeoff_calls"][0]
        assert body["dateRange"] == {
            "startDate": "2026-05-25",
            "endDate": "2026-06-22",
        }

    def test_default_statuses_are_approved_and_pending(self, monkeypatch):
        FakeApi, captured = _make_fake_api(activity_codes_resp=_activity_codes_response())
        _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        body = captured["timeoff_calls"][0]
        assert body["statuses"] == ["APPROVED", "PENDING"]

    def test_user_ids_filter_propagates(self, monkeypatch):
        FakeApi, captured = _make_fake_api(activity_codes_resp=_activity_codes_response())
        _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL,
             "user_ids": ["u1", "u2"]},
            monkeypatch, fake_api=FakeApi(),
        )
        body = captured["timeoff_calls"][0]
        assert body["userIds"] == ["u1", "u2"]

    def test_user_ids_absent_when_not_passed(self, monkeypatch):
        FakeApi, captured = _make_fake_api(activity_codes_resp=_activity_codes_response())
        _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        body = captured["timeoff_calls"][0]
        assert "userIds" not in body

    def test_explicit_statuses_override_default(self, monkeypatch):
        FakeApi, captured = _make_fake_api(activity_codes_resp=_activity_codes_response())
        _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL,
             "statuses": ["APPROVED"]},
            monkeypatch, fake_api=FakeApi(),
        )
        body = captured["timeoff_calls"][0]
        assert body["statuses"] == ["APPROVED"]


# ─────────────────────── request normalisation ───────────────────────


class TestNormalisation:
    def test_full_day_request_days_and_hours(self, monkeypatch):
        FakeApi, _ = _make_fake_api(
            activity_codes_resp=_activity_codes_response(),
            timeoff_pages={1: {"entities": [
                _full_day_request(
                    request_id="r1", user_id="u1",
                    activity_id="code-annual",
                    dates=["2026-06-08", "2026-06-09", "2026-06-10",
                           "2026-06-11", "2026-06-12"],
                ),
            ]}},
        )
        out = _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        row = out["requests"][0]
        assert row["days"] == 5
        assert row["hours"] == 40.0
        assert row["start_date"] == "2026-06-08"
        assert row["end_date"] == "2026-06-12"
        assert row["is_full_day"] is True

    def test_partial_day_request_days_and_hours(self, monkeypatch):
        FakeApi, _ = _make_fake_api(
            activity_codes_resp=_activity_codes_response(),
            timeoff_pages={1: {"entities": [
                _partial_day_request(
                    request_id="r2", user_id="u1",
                    activity_id="code-sick",
                    partial_starts=["2026-06-15T09:00:00.000Z",
                                    "2026-06-16T09:00:00.000Z"],
                    daily_minutes=240,
                ),
            ]}},
        )
        out = _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        row = out["requests"][0]
        assert row["days"] == 2
        assert row["hours"] == 8.0
        assert row["is_full_day"] is False
        assert row["dates"] == ["2026-06-15", "2026-06-16"]

    def test_activity_name_resolved_from_cache(self, monkeypatch):
        FakeApi, _ = _make_fake_api(
            activity_codes_resp=_activity_codes_response(),
            timeoff_pages={1: {"entities": [
                _full_day_request(
                    request_id="r1", user_id="u1",
                    activity_id="code-annual",
                    dates=["2026-06-08"],
                ),
            ]}},
        )
        out = _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        row = out["requests"][0]
        assert row["activity_name"] == "Annual Leave"
        assert row["activity_category"] == "TimeOff"
        assert row["user_name"] == "User u1"


# ─────────────────────── rollups ───────────────────────


class TestRollups:
    def _multi_request_payload(self):
        return {1: {"entities": [
            _full_day_request(request_id="r1", user_id="u1",
                               activity_id="code-annual",
                               dates=["2026-06-08", "2026-06-09", "2026-06-10",
                                      "2026-06-11", "2026-06-12"]),
            _full_day_request(request_id="r2", user_id="u1",
                               activity_id="code-sick",
                               dates=["2026-06-15"]),
            _full_day_request(request_id="r3", user_id="u2",
                               activity_id="code-annual",
                               dates=["2026-06-01", "2026-06-02"]),
            _full_day_request(request_id="r4", user_id="u3",
                               activity_id="code-annual",
                               dates=["2026-06-20"],
                               status="PENDING"),
        ]}}

    def test_totals(self, monkeypatch):
        FakeApi, _ = _make_fake_api(
            activity_codes_resp=_activity_codes_response(),
            timeoff_pages=self._multi_request_payload(),
        )
        out = _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        t = out["totals"]
        assert t["request_count"] == 4
        assert t["approved_count"] == 3
        assert t["pending_count"] == 1
        assert t["total_hours"] == 72.0
        assert t["total_days"] == 9

    def test_by_activity_sorted_by_hours_desc(self, monkeypatch):
        FakeApi, _ = _make_fake_api(
            activity_codes_resp=_activity_codes_response(),
            timeoff_pages=self._multi_request_payload(),
        )
        out = _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        by_act = out["by_activity"]
        assert [a["activity_name"] for a in by_act] == ["Annual Leave", "Sick Leave"]
        assert by_act[0]["total_hours"] == 64.0
        assert by_act[0]["total_days"] == 8
        assert by_act[0]["request_count"] == 3
        assert by_act[1]["total_hours"] == 8.0

    def test_by_user_sorted_and_activities_union(self, monkeypatch):
        FakeApi, _ = _make_fake_api(
            activity_codes_resp=_activity_codes_response(),
            timeoff_pages=self._multi_request_payload(),
        )
        out = _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        users = out["by_user"]
        assert [u["user_id"] for u in users] == ["u1", "u2", "u3"]
        u1 = users[0]
        assert u1["total_hours"] == 48.0
        assert u1["activities"] == ["Annual Leave", "Sick Leave"]


# ─────────────────────── envelope + edges ───────────────────────


class TestEnvelopeAndEdges:
    def test_top_level_interval_and_as_of_utc(self, monkeypatch):
        FakeApi, _ = _make_fake_api(activity_codes_resp=_activity_codes_response())
        out = _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["interval"] == _INTERVAL
        assert "as_of_utc" in out
        assert out["as_of_utc"].endswith("Z")
        assert out["business_unit_id"] == _BU
        assert out["statuses_queried"] == ["APPROVED", "PENDING"]

    def test_empty_result_is_safe(self, monkeypatch):
        FakeApi, _ = _make_fake_api(
            activity_codes_resp=_activity_codes_response(),
            timeoff_pages={1: {"entities": []}},
        )
        out = _call_tool(
            "wfm_time_off_requests",
            {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["totals"]["request_count"] == 0
        assert out["totals"]["total_hours"] == 0.0
        assert out["totals"]["total_days"] == 0
        assert out["by_activity"] == []
        assert out["by_user"] == []
        assert out["requests"] == []

    def test_activity_code_cache_reused_across_timeoff_calls(self, monkeypatch):
        FakeApi, captured = _make_fake_api(
            activity_codes_resp=_activity_codes_response(),
            timeoff_pages={1: {"entities": []}},
        )
        api = FakeApi()
        _call_tool("wfm_time_off_requests",
                   {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
                   monkeypatch, fake_api=api)
        _call_tool("wfm_time_off_requests",
                   {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL},
                   monkeypatch, fake_api=api)
        assert len(captured["activitycodes_calls"]) == 1
        assert len(captured["timeoff_calls"]) == 2

    def test_invalid_mode_raises(self, monkeypatch):
        FakeApi, _ = _make_fake_api(activity_codes_resp=_activity_codes_response())
        with pytest.raises(Exception, match="mode must be 'summary' or 'full'"):
            _call_tool(
                "wfm_time_off_requests",
                {"business_unit_id": _BU, "management_unit_ids": _MU_IDS, "interval": _INTERVAL, "mode": "bogus"},
                monkeypatch, fake_api=FakeApi(),
            )


# ── v1.13.2 regression: MU-scoped endpoint path + per-MU fan-out ──

class TestMuScopedEndpoint:
    """Pins the v1.13.2 fix.

    Pre-v1.13.2 the tool called ``POST /businessunits/{id}/timeoffrequests/query``,
    which does not exist in the Genesys Platform API schema (verified via the
    platform-api skill). Every call 404'd. The real endpoint is
    ``POST /managementunits/{muId}/timeoffrequests/query`` and is invoked
    once per MU in ``management_unit_ids``.
    """

    def _capture_paths(self, monkeypatch, mu_ids):
        captured: dict[str, list[str]] = {"timeoff_paths": []}

        class FakeApi:
            def call_api(self, **kwargs):
                path = kwargs.get("resource_path", "")
                if path.endswith("/activitycodes"):
                    return _activity_codes_response()
                if path.endswith("/timeoffrequests/query"):
                    captured["timeoff_paths"].append(path)
                    return {"entities": []}
                raise RuntimeError(f"unexpected path: {path}")

        _call_tool(
            "wfm_time_off_requests",
            {
                "business_unit_id": _BU,
                "management_unit_ids": mu_ids,
                "interval": _INTERVAL,
            },
            monkeypatch,
            fake_api=FakeApi(),
        )
        return captured["timeoff_paths"]

    def test_calls_mu_scoped_path_not_bu_scoped(self, monkeypatch):
        paths = self._capture_paths(monkeypatch, ["mu-aaa"])
        assert len(paths) == 1
        # Regression: must be MU-scoped, never BU-scoped.
        assert "/managementunits/mu-aaa/timeoffrequests/query" in paths[0]
        assert "/businessunits/" not in paths[0]

    def test_fans_out_one_call_per_mu(self, monkeypatch):
        paths = self._capture_paths(monkeypatch, ["mu-aaa", "mu-bbb", "mu-ccc"])
        # One path per MU; each must be MU-scoped.
        assert len(paths) == 3
        assert all("/managementunits/" in p for p in paths)
        # Every requested MU id appears exactly once.
        for mu in ("mu-aaa", "mu-bbb", "mu-ccc"):
            assert sum(1 for p in paths if f"/{mu}/" in p) == 1

    def test_empty_mu_list_raises_with_remediation(self, monkeypatch):
        FakeApi, _ = _make_fake_api(activity_codes_resp=_activity_codes_response())
        with pytest.raises(Exception, match="management_unit_ids must contain at least one"):
            _call_tool(
                "wfm_time_off_requests",
                {
                    "business_unit_id": _BU,
                    "management_unit_ids": [],
                    "interval": _INTERVAL,
                },
                monkeypatch,
                fake_api=FakeApi(),
            )

    def test_response_echoes_management_unit_ids(self, monkeypatch):
        FakeApi, _ = _make_fake_api(
            activity_codes_resp=_activity_codes_response(),
            timeoff_pages={1: {"entities": []}},
        )
        out = _call_tool(
            "wfm_time_off_requests",
            {
                "business_unit_id": _BU,
                "management_unit_ids": ["mu-x", "mu-y"],
                "interval": _INTERVAL,
            },
            monkeypatch,
            fake_api=FakeApi(),
        )
        assert out["management_unit_ids"] == ["mu-x", "mu-y"]
        # business_unit_id still echoed for activity-code-resolution provenance.
        assert out["business_unit_id"] == _BU
