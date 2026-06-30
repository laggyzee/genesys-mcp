"""Pin v1.14 P4 ``wfm_schedule`` fixes — timezone day bucketing + published/overlap.

Two real bugs this pins:

1. Shift hours were bucketed by the UTC ``.date()`` of each activity, so in a
   non-UTC tenant (Australia/Sydney = UTC+10) a Saturday-morning shift whose
   UTC timestamp is still Friday landed on the wrong calendar day. They must be
   bucketed in the schedule/tenant timezone.
2. The schedules/search endpoint is date-range scoped, not schedule-scoped, so
   iterating per schedule double-counted shifts when schedules overlapped, and
   drafts were summed alongside published schedules. Published-only + de-dup fix.
"""
from __future__ import annotations

import asyncio
import json


_BU = "bu-1"
_MU = "mu-1"
# Sat 20 Jun 2026 00:00 Sydney = Fri 19 Jun 14:00Z; covers Sat + Sun.
_INTERVAL = "2026-06-19T14:00:00.000Z/2026-06-21T14:00:00.000Z"

# A shift activity at 2026-06-19T23:00Z = 2026-06-20T09:00 Sydney (Saturday).
# Under UTC bucketing its date is Friday 2026-06-19; under Sydney it's Saturday.
_SAT_ACTIVITY_START = "2026-06-19T23:00:00.000Z"


def _schedules(*, with_draft=True):
    rows = [
        {"id": "sch-pub-1", "weekDate": "2026-06-15", "weekCount": 1, "published": True},
        {"id": "sch-pub-2", "weekDate": "2026-06-15", "weekCount": 1, "published": True},
    ]
    if with_draft:
        rows.append({"id": "sch-draft", "weekDate": "2026-06-15", "weekCount": 1, "published": False})
    return rows


def _search_response():
    return {
        "userSchedules": {
            "u1": {
                "shifts": [
                    {
                        "startDate": _SAT_ACTIVITY_START,
                        "activities": [
                            {"countsAsPaidTime": True,
                             "startDate": _SAT_ACTIVITY_START,
                             "lengthInMinutes": 480},  # 8h
                        ],
                    },
                ],
            },
        },
    }


def _make_fake_api(*, schedules, bu_timezone="Australia/Sydney"):
    captured = {"search_calls": 0}

    class FakeApi:
        def call_api(self, **kwargs):
            path = kwargs.get("resource_path", "")
            method = kwargs.get("method")
            if path.endswith("/schedules/search"):
                captured["search_calls"] += 1
                return _search_response()
            if "/headcountforecast" in path:
                return {"result": {"entities": []}}
            if path.endswith("/schedules") and method == "GET":
                return {"entities": schedules}
            if path.endswith(f"/businessunits/{_BU}"):
                return {"settings": {"timeZone": bu_timezone}} if bu_timezone else {}
            raise RuntimeError(f"unexpected path: {path}")

    return FakeApi, captured


def _call(args, monkeypatch, *, fake_api):
    import PureCloudPlatformClientV2 as gc
    from genesys_mcp import client as gen_client
    from genesys_mcp.tools import wfm
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(wfm, "get_api", lambda: fake_api)
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

    app = FastMCP(name="t")
    wfm.register(app)
    result = asyncio.run(app.call_tool("wfm_schedule", args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text)


def _base_args(**over):
    args = {"business_unit_id": _BU, "management_unit_ids": [_MU],
            "user_ids": ["u1"], "interval": _INTERVAL}
    args.update(over)
    return args


class TestTimezoneBucketing:
    def test_saturday_sydney_shift_lands_on_saturday_not_friday(self, monkeypatch):
        FakeApi, _ = _make_fake_api(schedules=_schedules())
        out = _call(_base_args(time_zone="Australia/Sydney"), monkeypatch, fake_api=FakeApi())
        days = {d["date"]: d for d in out["daily"]}
        assert "2026-06-20" in days, "Saturday-Sydney shift must bucket to Saturday"
        assert "2026-06-19" not in days, "must NOT land on Friday (the UTC-date bug)"
        assert days["2026-06-20"]["scheduled_hours"] == 8.0
        assert out["time_zone"] == "Australia/Sydney"
        assert out["time_zone_source"] == "caller"

    def test_utc_fallback_when_no_timezone_resolvable(self, monkeypatch):
        # BU returns no timezone and caller passes none → UTC fallback; the same
        # shift then (mis)attributes to Friday — documents why tz matters.
        FakeApi, _ = _make_fake_api(schedules=_schedules(), bu_timezone=None)
        out = _call(_base_args(), monkeypatch, fake_api=FakeApi())
        assert out["time_zone"] == "UTC"
        assert out["time_zone_source"] == "utc_fallback"
        days = {d["date"] for d in out["daily"]}
        assert "2026-06-19" in days  # UTC date of the Saturday-Sydney shift

    def test_business_unit_timezone_used_when_caller_omits(self, monkeypatch):
        FakeApi, _ = _make_fake_api(schedules=_schedules(), bu_timezone="Australia/Sydney")
        out = _call(_base_args(), monkeypatch, fake_api=FakeApi())
        assert out["time_zone"] == "Australia/Sydney"
        assert out["time_zone_source"] == "business_unit"
        assert {d["date"] for d in out["daily"]} == {"2026-06-20"}


class TestPublishedAndDedup:
    def test_overlapping_published_schedules_count_once(self, monkeypatch):
        # Two published schedules both cover the week → search fires twice and
        # returns the same activity; de-dup must keep it to 8h, not 16h.
        FakeApi, captured = _make_fake_api(schedules=_schedules(with_draft=False))
        out = _call(_base_args(time_zone="Australia/Sydney"), monkeypatch, fake_api=FakeApi())
        assert captured["search_calls"] == 2
        days = {d["date"]: d for d in out["daily"]}
        assert days["2026-06-20"]["scheduled_hours"] == 8.0  # de-duped, not 16.0

    def test_draft_schedule_excluded(self, monkeypatch):
        FakeApi, _ = _make_fake_api(schedules=_schedules(with_draft=True))
        out = _call(_base_args(time_zone="Australia/Sydney"), monkeypatch, fake_api=FakeApi())
        assert out["published_only"] is True
        returned_ids = {s["id"] for s in out["schedules"]}
        assert returned_ids == {"sch-pub-1", "sch-pub-2"}
        assert "sch-draft" not in returned_ids
