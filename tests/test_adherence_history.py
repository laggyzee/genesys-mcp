"""Pin v1.14.1 ``agent_adherence_history`` — synchronous historical adherence
via the Genesys BULK jobs API.

v1.14.0 wrongly used the MU-level ``historicaladherencequery`` endpoint, which
is notification-only (returns ``{queryState: Processing, downloadUrls: []}`` and
never a pollable result) — so it always soft-failed "still processing". v1.14.1
uses ``POST /adherence/historical/bulk`` → poll ``GET …/bulk/jobs/{id}`` until
``job.status == Complete`` → fetch the presigned download(s) → ``userResults``.

These tests pin:
- the request hits the top-level bulk path with one item per MU + timeZone
- a Processing→Complete poll fetches the presigned result and parses userResults
- an immediately-Complete submit skips the poll GET
- multiple MUs are submitted as multiple items and tagged in the output
- a failed job and a never-Complete job soft-fail (never hang / never wrong data)
- span cap (31 days; 7 with include_exceptions) raises; 403 soft-fails by scope
"""
from __future__ import annotations

import asyncio
import json

import pytest


_MU = "mu-1"
_MU2 = "mu-2"
_INTERVAL = "2026-06-15T00:00:00.000Z/2026-06-22T00:00:00.000Z"  # 7 days


class _FakeHttpResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _result_file(mu, rows):
    return {"managementUnitId": mu, "startDate": "x", "endDate": "y",
            "userResults": rows, "lookupIdToSecondaryPresenceId": {}}


class FakeHttpExc(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


def _call(args, monkeypatch, *, post_response, job_polls=None, files=None):
    import PureCloudPlatformClientV2 as gc
    from genesys_mcp import client as gen_client
    from genesys_mcp.tools import wfm
    from mcp.server.fastmcp import FastMCP

    captured: dict[str, object] = {"job_paths": []}
    polls = list(job_polls or [])

    class FakeApi:
        def call_api(self, **kwargs):
            path = kwargs.get("resource_path", "")
            method = kwargs.get("method")
            if path.endswith("/adherence/historical/bulk") and method == "POST":
                captured["post_path"] = path
                captured["body"] = kwargs.get("body")
                if isinstance(post_response, Exception):
                    raise post_response
                return post_response
            if "/adherence/historical/bulk/jobs/" in path and method == "GET":
                captured["job_paths"].append(path)
                if polls:
                    return polls.pop(0)
                # default: still processing (drives the never-complete path)
                return {"job": {"id": "j1", "status": "Processing"}, "downloadUrls": []}
            raise RuntimeError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(wfm, "get_api", lambda: FakeApi())
    monkeypatch.setattr(wfm, "to_dict", lambda o: o if isinstance(o, dict) else o)
    monkeypatch.setattr(wfm.resolver, "user_names", lambda uids: {u: f"User {u}" for u in uids})
    monkeypatch.setattr(wfm.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

    file_map = dict(files or {})
    monkeypatch.setattr(wfm.httpx, "get", lambda url, timeout=None: _FakeHttpResp(200, file_map[url]))

    app = FastMCP(name="t")
    wfm.register(app)
    result = asyncio.run(app.call_tool("agent_adherence_history", args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text), captured


_ROWS = [
    {"userId": "u1", "adherencePercentage": 95.0, "conformancePercentage": 98.0,
     "impact": "Positive", "exceptionInfo": []},
    {"userId": "u2", "adherencePercentage": 80.0, "conformancePercentage": 88.0,
     "impact": "Negative", "exceptionInfo": [{"x": 1}, {"x": 2}]},
]


class TestRequestShapeAndPoll:
    def test_bulk_path_body_and_processing_then_complete(self, monkeypatch):
        out, captured = _call(
            {"management_unit_ids": [_MU], "interval": _INTERVAL,
             "time_zone": "Australia/Sydney", "user_ids": ["u1", "u2"]},
            monkeypatch,
            post_response={"job": {"id": "j1", "status": "Processing"}, "downloadUrls": []},
            job_polls=[{"job": {"id": "j1", "status": "Complete"},
                        "downloadUrls": ["https://dl/coles.json"]}],
            files={"https://dl/coles.json": _result_file(_MU, _ROWS)},
        )
        assert captured["post_path"].endswith("/adherence/historical/bulk")
        body = captured["body"]
        assert body["timeZone"] == "Australia/Sydney"
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["managementUnitId"] == _MU
        assert item["startDate"] == "2026-06-15T00:00:00.000Z"
        assert item["includeActuals"] is False
        assert item["userIds"] == ["u1", "u2"]
        # polled once, then parsed
        assert len(captured["job_paths"]) == 1
        assert out["user_count"] == 2
        assert [u["user_id"] for u in out["users"]] == ["u2", "u1"]  # worst first
        assert out["users"][0]["adherence_pct"] == 80.0
        assert out["users"][0]["management_unit_id"] == _MU
        assert out["users"][0]["user_name"] == "User u2"
        assert out["mean_adherence_pct"] == 87.5
        assert out["mean_conformance_pct"] == 93.0
        assert out["job_id"] == "j1"
        assert out["management_unit_ids"] == [_MU]

    def test_immediately_complete_skips_poll(self, monkeypatch):
        out, captured = _call(
            {"management_unit_ids": [_MU], "interval": _INTERVAL},
            monkeypatch,
            post_response={"job": {"id": "j9", "status": "Complete"},
                           "downloadUrls": ["https://dl/x.json"]},
            files={"https://dl/x.json": _result_file(_MU, _ROWS[:1])},
        )
        assert captured["job_paths"] == []  # no GET needed
        assert out["user_count"] == 1

    def test_multiple_mus_tagged(self, monkeypatch):
        out, _ = _call(
            {"management_unit_ids": [_MU, _MU2], "interval": _INTERVAL},
            monkeypatch,
            post_response={"job": {"id": "j1", "status": "Complete"},
                           "downloadUrls": ["https://dl/a.json", "https://dl/b.json"]},
            files={
                "https://dl/a.json": _result_file(_MU, [_ROWS[0]]),
                "https://dl/b.json": _result_file(_MU2, [_ROWS[1]]),
            },
        )
        assert out["user_count"] == 2
        by_user = {u["user_id"]: u["management_unit_id"] for u in out["users"]}
        assert by_user == {"u1": _MU, "u2": _MU2}


class TestSoftFails:
    def test_job_failed_soft_fails(self, monkeypatch):
        out, _ = _call(
            {"management_unit_ids": [_MU], "interval": _INTERVAL},
            monkeypatch,
            post_response={"job": {"id": "j1", "status": "Processing"}, "downloadUrls": []},
            job_polls=[{"job": {"id": "j1", "status": "Error"}, "downloadUrls": []}],
        )
        assert out["status"] == 502
        assert out["job_id"] == "j1"

    def test_never_complete_soft_fails_with_job_id(self, monkeypatch):
        out, _ = _call(
            {"management_unit_ids": [_MU], "interval": _INTERVAL},
            monkeypatch,
            post_response={"job": {"id": "j1", "status": "Processing"}, "downloadUrls": []},
            job_polls=[],  # every poll returns Processing
        )
        assert out["status"] == 202
        assert out["job_id"] == "j1"
        assert "Complete" in out["message"]

    def test_403_soft_fails_naming_scope(self, monkeypatch):
        out, _ = _call(
            {"management_unit_ids": [_MU], "interval": _INTERVAL},
            monkeypatch,
            post_response=FakeHttpExc(403),
        )
        assert out["status"] == 403
        assert out["kind"] == "historical adherence"
        assert "workforce-management" in out["message"]
        assert out["management_unit_ids"] == [_MU]


class TestGuards:
    def test_empty_mu_list_raises(self, monkeypatch):
        with pytest.raises(Exception, match="management_unit_ids must contain at least one"):
            _call({"management_unit_ids": [], "interval": _INTERVAL}, monkeypatch,
                  post_response={"job": {"id": "j", "status": "Complete"}, "downloadUrls": []})

    def test_span_cap_31_days_raises(self, monkeypatch):
        with pytest.raises(Exception, match="caps it at 31 days"):
            _call(
                {"management_unit_ids": [_MU],
                 "interval": "2026-04-23T00:00:00.000Z/2026-06-22T00:00:00.000Z"},  # 60 days
                monkeypatch,
                post_response={"job": {"id": "j", "status": "Complete"}, "downloadUrls": []},
            )

    def test_span_cap_7_days_with_exceptions_raises(self, monkeypatch):
        with pytest.raises(Exception, match="caps it at 7 days"):
            _call(
                {"management_unit_ids": [_MU],
                 "interval": "2026-06-08T00:00:00.000Z/2026-06-22T00:00:00.000Z",  # 14 days
                 "include_exceptions": True},
                monkeypatch,
                post_response={"job": {"id": "j", "status": "Complete"}, "downloadUrls": []},
            )
