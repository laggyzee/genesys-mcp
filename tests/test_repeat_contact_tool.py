"""Pin the v1.24 repeat-contact config layering and tool wiring.

The methodology itself is pinned in ``test_repeat_contacts.py``. This file
covers what sits around it:

- ``repeatContactReport`` layering: defaults < tenant.yaml section < runtime
  JSON store < ``GENESYS_MCP_REPEAT_*`` env pins
- ``set_repeat_contact_config`` validates the *whole* merged config before
  writing anything; windows must be positive integers; unknown keys fail
- ``get_repeat_contact_report`` falls back to config for unspecified params,
  fetches the lookback (period_start − max window) one local day at a time,
  de-duplicates by conversationId, and reuses cached settled days
- External Contact merge resolution + identifier lookup are batched, cached,
  and degrade to raw keys with a warning on 403
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from mcp.server.fastmcp import FastMCP
from PureCloudPlatformClientV2.rest import ApiException

from genesys_mcp import _repeat_contact_config as rc_config
from genesys_mcp.tools import repeat_contacts as tool


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Point every config / cache path at tmp and clear env pins."""
    monkeypatch.setenv("GENESYS_MCP_CONFIG", str(tmp_path / "tenant.yaml"))
    monkeypatch.setenv("GENESYS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("GENESYS_CLIENT_ID", "client-a")
    monkeypatch.setenv("GENESYS_REGION", "ap-southeast-2")
    monkeypatch.delenv("GENESYS_MCP_REPEAT_CONTACT_CONFIG", raising=False)
    for suffix in rc_config._ENV_OVERRIDES:
        monkeypatch.delenv(rc_config.ENV_PREFIX + suffix, raising=False)
    return tmp_path


def _call(name, args=None):
    app = FastMCP(name="t")
    tool.register(app)
    result = asyncio.run(app.call_tool(name, args or {}))
    content = result[0] if isinstance(result, tuple) else result
    first = content[0] if isinstance(content, list) else content
    text = getattr(first, "text", None) or first.get("text")
    return json.loads(text)


# ───────────────────────────── config ─────────────────────────────

def test_defaults_match_the_documented_scope():
    out = _call("get_repeat_contact_config")
    cfg = out["config"]
    assert out["section"] == "repeatContactReport"
    assert cfg["direction"] == "inbound"
    assert cfg["include_abandoned"] is False
    assert cfg["media_types"] == {"voice": ["voice"], "messaging": ["message"]}
    assert cfg["queue_ids"] == []
    assert cfg["windows_days"] == [7, 45]
    assert cfg["timezone"] == "Australia/Sydney"
    assert cfg["identity"]["mode"] == "external_contact_first"
    assert cfg["identity"]["enable_lookup"] is True
    assert cfg["identity"]["phone_normalisation"] == {"format": "E.164", "default_country": "AU"}
    assert cfg["identity"]["messaging_keys"] == ["email", "sms", "web_messaging_user", "social"]
    assert out["env_overridden_keys"] == []
    assert out["fcr_label"] == "FCR (Amaysim methodology)"


def test_set_deep_merges_persists_and_round_trips(_isolated_config):
    out = _call("set_repeat_contact_config", {"updates": {
        "windows_days": [45, 7, 30, 7],
        "identity": {"enable_lookup": False},
    }})
    assert out["config"]["windows_days"] == [7, 30, 45]  # sorted, de-duplicated
    assert out["config"]["identity"]["enable_lookup"] is False
    assert out["config"]["identity"]["mode"] == "external_contact_first"  # siblings untouched

    stored = json.loads((_isolated_config / "repeat-contact-report.json").read_text())
    assert stored == {"repeatContactReport": {
        "identity": {"enable_lookup": False}, "windows_days": [45, 7, 30, 7],
    }}
    assert _call("get_repeat_contact_config")["config"]["windows_days"] == [7, 30, 45]

    reset = _call("set_repeat_contact_config", {"reset": True})
    assert reset["config"]["windows_days"] == [7, 45]


@pytest.mark.parametrize("bad", [[0], [-7], [7.5], ["7"], [True], [], "7,45", [7, 9999]])
def test_windows_must_be_positive_integers(bad, _isolated_config):
    out = _call("set_repeat_contact_config", {"updates": {"windows_days": bad}})
    assert out["status"] == 400
    assert "windows_days" in out["message"]
    assert not (_isolated_config / "repeat-contact-report.json").exists()  # nothing written


@pytest.mark.parametrize("updates,needle", [
    ({"windows_day": [7]}, "windows_day"),
    ({"timezone": "Australia/Atlantis"}, "timezone"),
    ({"identity": {"mode": "guess"}}, "identity.mode"),
    ({"identity": {"messaging_keys": ["fax"]}}, "messaging_keys"),
    ({"identity": {"phone_normalisation": {"default_country": "AUS"}}}, "default_country"),
    ({"direction": "sideways"}, "direction"),
    ({"media_types": {"voice": []}}, "media_types.voice"),
])
def test_invalid_updates_are_rejected_with_the_offending_key(updates, needle):
    out = _call("set_repeat_contact_config", {"updates": updates})
    assert out["status"] == 400 and needle in out["message"]


def test_layer_precedence_tenant_yaml_then_runtime_then_env(_isolated_config, monkeypatch):
    (_isolated_config / "tenant.yaml").write_text(
        "tenant: {name: T, short_name: t}\n"
        "repeatContactReport:\n  windows_days: [14]\n  include_abandoned: true\n"
    )
    assert _call("get_repeat_contact_config")["config"]["windows_days"] == [14]

    _call("set_repeat_contact_config", {"updates": {"windows_days": [10]}})
    cfg = _call("get_repeat_contact_config")["config"]
    assert cfg["windows_days"] == [10] and cfg["include_abandoned"] is True

    monkeypatch.setenv("GENESYS_MCP_REPEAT_WINDOWS_DAYS", "3, 60")
    monkeypatch.setenv("GENESYS_MCP_REPEAT_IDENTITY_ENABLE_LOOKUP", "false")
    out = _call("set_repeat_contact_config", {"updates": {"windows_days": [21]}})
    assert out["config"]["windows_days"] == [3, 60]  # the env pin still wins
    assert out["config"]["identity"]["enable_lookup"] is False
    assert out["env_overridden_keys"] == ["identity.enable_lookup", "windows_days"]
    assert out["ignored_env_pinned_keys"] == ["windows_days"]


def test_bad_env_override_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setenv("GENESYS_MCP_REPEAT_WINDOWS_DAYS", "seven")
    out = _call("get_repeat_contact_config")
    assert out["status"] == 400 and "GENESYS_MCP_REPEAT_WINDOWS_DAYS" in out["message"]


def test_explicit_runtime_config_path(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere" / "rc.json"
    monkeypatch.setenv("GENESYS_MCP_REPEAT_CONTACT_CONFIG", str(target))
    out = _call("set_repeat_contact_config", {"updates": {"include_abandoned": True}})
    assert out["runtime_config_path"] == str(target) and target.exists()


# ───────────────────────────── report wiring ─────────────────────────────

def _voice(cid, start, *, ec=None, ani="tel:+61400000001", queue="q1"):
    return {
        "conversationId": cid, "conversationStart": start,
        "participants": [
            {"purpose": "customer", "externalContactId": ec, "sessions": [{
                "mediaType": "voice", "direction": "inbound", "ani": ani,
                "segments": [{"segmentStart": start, "segmentType": "interact"}],
            }]},
            {"purpose": "acd", "sessions": [{"mediaType": "voice", "segments": [
                {"segmentStart": start, "segmentType": "interact", "queueId": queue}]}]},
            {"purpose": "agent", "userId": "u", "sessions": [{"mediaType": "voice", "segments": [
                {"segmentStart": start, "segmentType": "interact", "queueId": queue}]}]},
        ],
    }


class _FakeDetails:
    def __init__(self, conversations, *, complete=True):
        self.conversations = conversations
        self.complete = complete
        self.bodies = []

    def __call__(self, body, max_pages=20, *, use_cache=True):
        self.bodies.append(body)
        start, end = (datetime.fromisoformat(p.replace("Z", "+00:00")) for p in body["interval"].split("/"))
        rows = [c for c in self.conversations
                if start <= datetime.fromisoformat(c["conversationStart"].replace("Z", "+00:00")) < end]
        return {"conversations": rows + rows[:1],  # a duplicate row, to prove de-duplication
                "data_complete": self.complete, "data_provisional": False,
                "data_source": "analytics_conversations_details_jobs"}


class _Resolver:
    def queue_names(self, ids):
        return {i: f"Queue {i}" for i in ids}


@pytest.fixture
def wired(monkeypatch):
    def _wire(conversations, *, now, complete=True, external=None):
        fake = _FakeDetails(conversations, complete=complete)
        monkeypatch.setattr(tool, "fetch_conversation_details", fake)
        monkeypatch.setattr(tool, "resolver", _Resolver())
        monkeypatch.setattr(tool, "_now_utc", lambda: now)
        monkeypatch.setattr(tool, "get_api", lambda: object())
        monkeypatch.setattr(tool, "to_dict", lambda obj: obj)
        monkeypatch.setattr(tool, "with_retry", lambda fn: fn)
        monkeypatch.setattr(tool, "_MIN_CALL_INTERVAL_S", 0)
        monkeypatch.setattr(tool.gc, "ExternalContactsApi", lambda _api: external or _ExternalApi())
        return fake
    return _wire


class _ExternalApi:
    def __init__(self, *, merges=None, lookups=None, status=None):
        self.merges = merges or {}
        self.lookups = lookups or {}
        self.status = status
        self.bulk_calls, self.lookup_calls = [], []

    def post_externalcontacts_bulk_contacts(self, body):
        if self.status:
            raise ApiException(status=self.status, reason="Forbidden")
        ids = [e["id"] for e in body["entities"]]
        self.bulk_calls.append(ids)
        return {"results": [
            {"id": i, "entity": {"id": i, **({"canonicalContact": {"id": self.merges[i]}} if i in self.merges else {})}}
            for i in ids
        ]}

    def post_externalcontacts_identifierlookup_contacts(self, identifier):
        self.lookup_calls.append(identifier)
        found = self.lookups.get(identifier["value"])
        if not found:
            raise ApiException(status=404, reason="Not Found")
        return {"id": found}

    def get_externalcontacts_contacts(self, **_kwargs):
        return {"entities": []}


_NOW = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)


def test_report_uses_config_defaults_fetches_lookback_by_day_and_dedupes(wired):
    fake = wired([
        _voice("prior", "2026-08-20T02:00:00.000Z", ec="A"),   # lookback, 45-day only
        _voice("cur", "2026-09-02T02:00:00.000Z", ec="A"),
        _voice("other", "2026-09-03T02:00:00.000Z", ec="B"),
    ], now=_NOW)
    out = _call("get_repeat_contact_report", {"period_start": "2026-09-01", "period_end": "2026-09-07"})

    assert out["lookback_start"] == "2026-07-18"  # 1 Sep − 45 days
    assert len(fake.bodies) == 52  # 45 lookback days + 7 period days, one slice each
    assert fake.bodies[0]["interval"].startswith("2026-07-17T14:00:00.000Z/")  # Sydney midnight
    assert fake.bodies[0]["conversationFilters"][0]["predicates"][0]["value"] == "inbound"
    assert {p["value"] for p in fake.bodies[0]["segmentFilters"][0]["predicates"]} == {"voice", "message"}

    rows = {(r["window_days"], r["channel"]): r for r in out["results"]}
    assert set(rows) == {(w, c) for w in (7, 45) for c in ("voice", "messaging", "combined")}
    assert (rows[(7, "voice")]["total_contacts"], rows[(7, "voice")]["repeat_contacts"]) == (2, 0)
    assert rows[(45, "voice")]["repeat_contacts"] == 1
    assert rows[(45, "voice")]["fcr"] == 0.5
    assert rows[(45, "voice")]["config_used"]["windows_days"] == [7, 45]
    assert out["fcr_label"] == "FCR (Amaysim methodology)"
    assert "1 - Repeat Contact Rate" in out["methodology"]
    assert out["data_quality"]["conversations_fetched"] == 3  # duplicates collapsed


def test_params_override_config_and_breakdown_drilldown_are_optional(wired):
    wired([
        _voice("a", "2026-09-01T02:00:00.000Z", ec="A", queue="q1"),
        _voice("b", "2026-09-02T02:00:00.000Z", ec="A", queue="q2"),
    ], now=_NOW)
    base = _call("get_repeat_contact_report", {"period_start": "2026-09-01", "period_end": "2026-09-02"})
    assert "breakdown" not in base and "drilldown" not in base

    out = _call("get_repeat_contact_report", {
        "period_start": "2026-09-01", "period_end": "2026-09-02", "windows_days": [3],
        "channels": ["voice"], "queue_ids": ["q2"], "breakdown": ["day", "queue"], "drilldown": True,
    })
    assert [(r["window_days"], r["channel"]) for r in out["results"]] == [(3, "voice")]
    # queue scope applies to the prior as well: 'a' (q1) is out of scope, so 'b' is not a repeat
    assert (out["results"][0]["total_contacts"], out["results"][0]["repeat_contacts"]) == (1, 0)
    assert out["config_used"]["queue_ids"] == ["q2"]
    assert out["breakdown"]["by_queue"][0]["queue"] == "Queue q2"
    assert out["drilldown"]["rows"] == []


@pytest.mark.parametrize("args,needle", [
    ({"period_start": "2026-09-07", "period_end": "2026-09-01"}, "before period_start"),
    ({"period_start": "last week", "period_end": "2026-09-01"}, "period_start"),
    ({"period_start": "2026-09-01", "period_end": "2026-09-02", "windows_days": [0]}, "windows_days"),
    ({"period_start": "2026-09-01", "period_end": "2026-09-02", "channels": ["email"]}, "unknown channels"),
    ({"period_start": "2026-09-01", "period_end": "2026-09-02", "breakdown": ["agent"]}, "breakdown"),
])
def test_bad_report_params_return_a_400_envelope(wired, args, needle):
    wired([], now=_NOW)
    out = _call("get_repeat_contact_report", args)
    assert out["status"] == 400 and needle in out["message"]


def test_settled_days_are_cached_so_adjacent_periods_do_not_refetch(wired):
    convs = [_voice("a", "2026-09-01T02:00:00.000Z", ec="A")]
    first = wired(convs, now=_NOW)
    _call("get_repeat_contact_report",
          {"period_start": "2026-09-01", "period_end": "2026-09-07", "windows_days": [7]})
    assert len(first.bodies) == 14

    second = wired(convs, now=_NOW)
    out = _call("get_repeat_contact_report",
                {"period_start": "2026-09-08", "period_end": "2026-09-14", "windows_days": [7]})
    assert len(second.bodies) == 7  # only the 7 new days; the lookback came from cache
    assert out["data_quality"]["days_from_cache"] == 7


def test_unsettled_or_incomplete_days_are_not_cached_and_are_flagged(wired):
    convs = [_voice("a", "2026-09-19T02:00:00.000Z", ec="A")]
    args = {"period_start": "2026-09-19", "period_end": "2026-09-19", "windows_days": [1]}
    wired(convs, now=_NOW)  # 19 Sep (Sydney) ended 14:00Z on the 19th — under 24h before 'now'
    _call("get_repeat_contact_report", args)
    again = wired(convs, now=_NOW)
    _call("get_repeat_contact_report", args)
    assert len(again.bodies) == 1  # 18 Sep came from cache, 19 Sep was refetched

    wired(convs, now=_NOW + timedelta(days=30), complete=False)
    out = _call("get_repeat_contact_report",
                {"period_start": "2026-10-01", "period_end": "2026-10-01", "windows_days": [1]})
    assert out["data_quality"]["incomplete_days"] == ["2026-09-30", "2026-10-01"]
    assert out["warnings"][0]["code"] == "incomplete_conversation_data"
    assert out["results"][0]["warnings"][0]["code"] == "incomplete_conversation_data"


def test_merge_resolution_and_lookup_are_batched_and_cached(wired):
    external = _ExternalApi(merges={"old": "survivor"}, lookups={"+61400000009": "survivor"})
    convs = [
        _voice("a", "2026-09-01T02:00:00.000Z", ec="old"),
        _voice("b", "2026-09-02T02:00:00.000Z", ec="survivor"),
        _voice("c", "2026-09-03T02:00:00.000Z", ani="tel:+61400000009"),
    ]
    wired(convs, now=_NOW, external=external)
    args = {"period_start": "2026-09-01", "period_end": "2026-09-07", "windows_days": [7], "channels": ["voice"]}
    out = _call("get_repeat_contact_report", args)
    row = out["results"][0]
    assert (row["total_contacts"], row["repeat_contacts"]) == (3, 2)
    assert row["coverage"]["external_contact_linked"]["count"] == 2
    assert row["coverage"]["external_contact_lookup"]["count"] == 1
    assert len(external.bulk_calls) == 1 and sorted(external.bulk_calls[0]) == ["old", "survivor"]
    assert external.lookup_calls == [{"type": "Phone", "value": "+61400000009"}]
    assert out["data_quality"]["identity"]["external_contacts_merged"] == 1

    rerun = _ExternalApi()
    wired(convs, now=_NOW, external=rerun)
    assert _call("get_repeat_contact_report", args)["results"][0]["repeat_contacts"] == 2
    assert rerun.bulk_calls == [] and rerun.lookup_calls == []  # served from the identity cache


def test_external_contacts_403_degrades_to_links_and_raw_keys_with_a_warning(wired):
    wired([
        _voice("a", "2026-09-01T02:00:00.000Z", ec="A"),
        _voice("b", "2026-09-02T02:00:00.000Z", ec="A"),
    ], now=_NOW, external=_ExternalApi(status=403))
    out = _call("get_repeat_contact_report",
                {"period_start": "2026-09-01", "period_end": "2026-09-02", "windows_days": [7], "channels": ["voice"]})
    assert out["results"][0]["repeat_contacts"] == 1  # the unresolved link still matches itself
    assert "external_contacts_unavailable" in [w["code"] for w in out["warnings"]]


def test_conversation_detail_403_returns_the_canonical_envelope(wired, monkeypatch):
    wired([], now=_NOW)

    def _boom(*_a, **_k):
        raise ApiException(status=403, reason="Forbidden")

    monkeypatch.setattr(tool, "fetch_conversation_details", _boom)
    out = _call("get_repeat_contact_report", {"period_start": "2026-09-01", "period_end": "2026-09-01"})
    assert out["status"] == 403 and out["kind"] == "repeat_contact_report"
    assert "analytics:conversationDetail:view" in out["message"]
