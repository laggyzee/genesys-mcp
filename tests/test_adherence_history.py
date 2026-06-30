"""Pin v1.14 P5 ``agent_adherence_history`` — synchronous historical adherence.

The gap this closes: pre-v1.14 nothing returned actual-vs-scheduled adherence %
to the caller. ``agent_adherence_review`` explicitly disclaims it; the real
Genesys endpoint (``POST .../managementunits/{mu}/historicaladherencequery``) is
async — submit, then poll a presigned download URL until it serves the result.

These tests pin:
- the request hits the MU-scoped historicaladherencequery path with the right body
- inline ``downloadResult`` is parsed into per-user adherence/conformance rows
- a presigned ``downloadUrl`` is polled (httpx) and parsed when not inline
- still-processing (URL never serves) → soft-fail (never hangs/returns wrong data)
- span cap (31 days; 7 with include_exceptions) raises with remediation
- 403 → soft-fail envelope naming wfm:historicalAdherence:view
"""
from __future__ import annotations

import asyncio
import json

import pytest


_MU = "mu-1"
_INTERVAL = "2026-06-15T00:00:00.000Z/2026-06-22T00:00:00.000Z"  # 7 days


class _FakeHttpResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _result_wrapper(rows):
    return {"entityId": "e1", "data": rows, "lookupIdToSecondaryPresenceId": {}}


def _call(args, monkeypatch, *, submit_response, http_responses=None):
    """Invoke agent_adherence_history with a fake call_api + fake httpx.get."""
    import PureCloudPlatformClientV2 as gc
    from genesys_mcp import client as gen_client
    from genesys_mcp.tools import wfm
    from mcp.server.fastmcp import FastMCP

    captured: dict[str, object] = {}

    class FakeApi:
        def call_api(self, **kwargs):
            path = kwargs.get("resource_path", "")
            if path.endswith("/historicaladherencequery"):
                captured["path"] = path
                captured["body"] = kwargs.get("body")
                if isinstance(submit_response, Exception):
                    raise submit_response
                return submit_response
            raise RuntimeError(f"unexpected path: {path}")

    monkeypatch.setattr(wfm, "get_api", lambda: FakeApi())
    monkeypatch.setattr(wfm, "to_dict", lambda o: o if isinstance(o, dict) else o)
    monkeypatch.setattr(
        wfm.resolver, "user_names", lambda uids: {u: f"User {u}" for u in uids}
    )
    monkeypatch.setattr(wfm.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

    responses = list(http_responses or [])

    def fake_get(url, timeout=None):
        return responses.pop(0) if responses else _FakeHttpResp(404)

    monkeypatch.setattr(wfm.httpx, "get", fake_get)

    app = FastMCP(name="t")
    wfm.register(app)
    result = asyncio.run(app.call_tool("agent_adherence_history", args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text), captured


class FakeHttpExc(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


class TestRequestPathAndInlineResult:
    def test_mu_scoped_path_and_body(self, monkeypatch):
        out, captured = _call(
            {"management_unit_id": _MU, "interval": _INTERVAL,
             "time_zone": "Australia/Sydney", "user_ids": ["u1"]},
            monkeypatch,
            submit_response={"id": "q1", "queryState": "Complete",
                             "downloadResult": _result_wrapper([])},
        )
        assert f"/managementunits/{_MU}/historicaladherencequery" in captured["path"]
        assert "/businessunits/" not in captured["path"]
        body = captured["body"]
        assert body["startDate"] == "2026-06-15T00:00:00.000Z"
        assert body["endDate"] == "2026-06-22T00:00:00.000Z"
        assert body["timeZone"] == "Australia/Sydney"
        assert body["userIds"] == ["u1"]
        assert body["includeExceptions"] is False

    def test_inline_result_parsed_sorted_and_averaged(self, monkeypatch):
        rows = [
            {"userId": "u1", "adherencePercentage": 95.0, "conformancePercentage": 98.0,
             "impact": "Positive", "exceptionInfo": []},
            {"userId": "u2", "adherencePercentage": 80.0, "conformancePercentage": 88.0,
             "impact": "Negative", "exceptionInfo": [{"x": 1}, {"x": 2}]},
        ]
        out, _ = _call(
            {"management_unit_id": _MU, "interval": _INTERVAL},
            monkeypatch,
            submit_response={"id": "q1", "queryState": "Complete",
                             "downloadResult": _result_wrapper(rows)},
        )
        assert out["user_count"] == 2
        # worst adherence first
        assert [u["user_id"] for u in out["users"]] == ["u2", "u1"]
        assert out["users"][0]["adherence_pct"] == 80.0
        assert out["users"][0]["exception_count"] == 2
        assert out["users"][0]["user_name"] == "User u2"
        assert out["mean_adherence_pct"] == 87.5  # (95+80)/2
        assert out["mean_conformance_pct"] == 93.0
        assert out["query_id"] == "q1"
        assert out["interval"] == _INTERVAL
        assert out["as_of_utc"].endswith("Z")

    def test_inline_empty_data_is_a_valid_zero_result_not_processing(self, monkeypatch):
        # downloadResult present with data: [] = "zero agents matched", a valid
        # complete result — must NOT be treated as still-processing (202).
        out, _ = _call(
            {"management_unit_id": _MU, "interval": _INTERVAL},
            monkeypatch,
            submit_response={"id": "q1", "queryState": "Complete",
                             "downloadResult": _result_wrapper([])},
        )
        assert out.get("status") != 202
        assert out["user_count"] == 0
        assert out["mean_adherence_pct"] is None

    def test_include_exceptions_passed_and_detail_returned(self, monkeypatch):
        rows = [{"userId": "u1", "adherencePercentage": 90.0, "conformancePercentage": 92.0,
                 "impact": "Positive", "exceptionInfo": [{"reason": "training"}]}]
        out, captured = _call(
            {"management_unit_id": _MU,
             "interval": "2026-06-18T00:00:00.000Z/2026-06-22T00:00:00.000Z",  # 4 days < 7
             "include_exceptions": True},
            monkeypatch,
            submit_response={"id": "q1", "queryState": "Complete",
                             "downloadResult": _result_wrapper(rows)},
        )
        assert captured["body"]["includeExceptions"] is True
        assert out["users"][0]["exceptions"] == [{"reason": "training"}]


class TestDownloadPoll:
    def test_polls_download_url_when_not_inline(self, monkeypatch):
        rows = [{"userId": "u1", "adherencePercentage": 99.0, "conformancePercentage": 99.0,
                 "impact": "Positive", "exceptionInfo": []}]
        out, _ = _call(
            {"management_unit_id": _MU, "interval": _INTERVAL},
            monkeypatch,
            submit_response={"id": "q2", "queryState": "Processing",
                             "downloadUrls": ["https://dl.example/result.json"]},
            http_responses=[_FakeHttpResp(404), _FakeHttpResp(200, _result_wrapper(rows))],
        )
        assert out["user_count"] == 1
        assert out["users"][0]["adherence_pct"] == 99.0

    def test_never_ready_soft_fails(self, monkeypatch):
        out, _ = _call(
            {"management_unit_id": _MU, "interval": _INTERVAL},
            monkeypatch,
            submit_response={"id": "q3", "queryState": "Processing",
                             "downloadUrl": "https://dl.example/never.json"},
            http_responses=[],  # always 404
        )
        assert out["status"] == 202
        assert out["query_id"] == "q3"
        assert "not ready" in out["message"].lower()


class TestGuards:
    def test_span_cap_31_days_raises(self, monkeypatch):
        with pytest.raises(Exception, match="caps it at 31 days"):
            _call(
                {"management_unit_id": _MU,
                 "interval": "2026-04-23T00:00:00.000Z/2026-06-22T00:00:00.000Z"},  # 60 days
                monkeypatch,
                submit_response={"id": "q", "downloadResult": _result_wrapper([])},
            )

    def test_span_cap_7_days_with_exceptions_raises(self, monkeypatch):
        with pytest.raises(Exception, match="caps it at 7 days"):
            _call(
                {"management_unit_id": _MU,
                 "interval": "2026-06-08T00:00:00.000Z/2026-06-22T00:00:00.000Z",  # 14 days
                 "include_exceptions": True},
                monkeypatch,
                submit_response={"id": "q", "downloadResult": _result_wrapper([])},
            )

    def test_403_soft_fails_naming_scope(self, monkeypatch):
        out, _ = _call(
            {"management_unit_id": _MU, "interval": _INTERVAL},
            monkeypatch,
            submit_response=FakeHttpExc(403),
        )
        assert out["status"] == 403
        assert out["kind"] == "historical adherence"
        assert "wfm:historicalAdherence:view" in out["message"]
        assert out["management_unit_id"] == _MU
