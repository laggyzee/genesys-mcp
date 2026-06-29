"""Pin the v1.3 soft-fail envelope contract + every tool's adoption.

Pre-v1.3 every tool that soft-failed invented its own envelope shape:

- speech_analytics:  {"status": 404, "conversation_id": cid, "message": ...}
- external_contacts: {"status": 404, "value": v, "type": t, "match": None}
- (queue_EWT had no envelope — just an `error` string)

v1.3 standardises on :func:`genesys_mcp._envelopes.soft_fail_envelope`.
These tests pin the helper itself + every tool's adoption so a future
refactor can't drift back to bespoke shapes.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ─────────────────────── helper itself ───────────────────────

class TestSoftFailEnvelopeHelper:
    def test_canonical_shape_has_status_kind_message(self):
        from genesys_mcp._envelopes import soft_fail_envelope
        env = soft_fail_envelope(
            kind="transcript url", message="transcript url not found",
            conversation_id="conv-1",
        )
        assert env["status"] == 404
        assert env["kind"] == "transcript url"
        assert env["message"] == "transcript url not found"
        assert env["conversation_id"] == "conv-1"

    def test_status_defaults_to_404(self):
        from genesys_mcp._envelopes import soft_fail_envelope
        env = soft_fail_envelope(kind="x", message="m")
        assert env["status"] == 404

    def test_custom_status_passed_through(self):
        from genesys_mcp._envelopes import soft_fail_envelope
        env = soft_fail_envelope(status=403, kind="auth", message="forbidden")
        assert env["status"] == 403

    def test_arbitrary_id_fields_preserved(self):
        from genesys_mcp._envelopes import soft_fail_envelope
        env = soft_fail_envelope(
            kind="ewt", message="no ewt",
            queue_id="q-1", media_type="call", estimated_wait_time_seconds=None,
        )
        assert env["queue_id"] == "q-1"
        assert env["media_type"] == "call"
        assert env["estimated_wait_time_seconds"] is None

    def test_field_order_status_kind_message_then_ids(self):
        """Canonical key order makes responses predictable to scan visually."""
        from genesys_mcp._envelopes import soft_fail_envelope
        env = soft_fail_envelope(
            kind="x", message="m", conversation_id="c-1", communication_id="s-1",
        )
        keys = list(env.keys())
        # status/kind/message come first; id fields follow in the order they were passed
        assert keys[:3] == ["status", "kind", "message"]
        # ids follow
        assert "conversation_id" in keys[3:]


class TestIsSoftFail:
    def test_identifies_canonical_envelope(self):
        from genesys_mcp._envelopes import is_soft_fail, soft_fail_envelope
        assert is_soft_fail(soft_fail_envelope(kind="x", message="m"))

    def test_rejects_success_dict(self):
        from genesys_mcp._envelopes import is_soft_fail
        assert not is_soft_fail({"id": "abc", "name": "thing"})

    def test_rejects_non_dict(self):
        from genesys_mcp._envelopes import is_soft_fail
        assert not is_soft_fail("just a string")
        assert not is_soft_fail([{"status": 404}])
        assert not is_soft_fail(None)

    def test_status_below_400_not_soft_fail(self):
        """A 200-OK-with-status-key dict is not a soft-fail."""
        from genesys_mcp._envelopes import is_soft_fail
        assert not is_soft_fail({"status": 200, "data": "ok"})


# ─────────────────────── per-tool soft-fail adoption ───────────────────────

class TestSpeechAnalyticsSoftFails:
    def test_soft_404_uses_canonical_envelope(self):
        from genesys_mcp.tools.speech_analytics import _soft_404

        exc = type("HttpExc", (), {"status": 404})()
        env = _soft_404(exc, "conv-1", "transcript url")
        assert env["status"] == 404
        assert env["kind"] == "transcript url"
        assert env["message"] == "transcript url not found"
        assert env["conversation_id"] == "conv-1"

    def test_soft_404_returns_none_on_non_404(self):
        from genesys_mcp.tools.speech_analytics import _soft_404
        exc = type("HttpExc", (), {"status": 500})()
        assert _soft_404(exc, "conv-1", "x") is None


class TestGetConversationSoftFail:
    """v1.3: get_conversation now soft-fails on 404 instead of raising."""

    def test_404_returns_canonical_envelope(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import conversations as conv_mod
        from mcp.server.fastmcp import FastMCP
        import asyncio

        class FakeExc(Exception):
            status = 404

        class FakeConvApi:
            def __init__(self, *args, **kwargs): pass
            def get_conversation(self, *args, **kwargs):
                raise FakeExc()

        monkeypatch.setattr(conv_mod.gc, "ConversationsApi", FakeConvApi)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        app = FastMCP(name="t")
        conv_mod.register(app)
        result = asyncio.run(
            app.call_tool("get_conversation", {"conversation_id": "deleted-conv"})
        )
        # FastMCP wraps the result; pull it out
        import json
        text = getattr(result[0], "text", None)
        if text is None and isinstance(result[0], dict):
            text = result[0].get("text")
        payload = json.loads(text)
        assert payload["status"] == 404
        assert payload["kind"] == "conversation"
        assert payload["conversation_id"] == "deleted-conv"
        assert "deleted" in payload["message"].lower()

    def test_non_404_propagates(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import conversations as conv_mod
        from mcp.server.fastmcp import FastMCP
        import asyncio

        class FakeExc(Exception):
            status = 500

        class FakeConvApi:
            def __init__(self, *args, **kwargs): pass
            def get_conversation(self, *args, **kwargs):
                raise FakeExc("server error")

        monkeypatch.setattr(conv_mod.gc, "ConversationsApi", FakeConvApi)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        app = FastMCP(name="t")
        conv_mod.register(app)
        with pytest.raises(Exception):
            asyncio.run(
                app.call_tool("get_conversation", {"conversation_id": "x"})
            )


class TestExternalContactSoftFail:
    """v1.3: lookup_external_contact migrated to canonical envelope."""

    def test_404_returns_canonical_envelope_with_match_none(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import external_contacts as ec
        from mcp.server.fastmcp import FastMCP
        import asyncio

        class FakeExc(Exception):
            status = 404

        class FakeEcApi:
            def __init__(self, *args, **kwargs): pass
            def post_externalcontacts_identifierlookup_contacts(self, **kwargs):
                raise FakeExc()

        monkeypatch.setattr(ec.gc, "ExternalContactsApi", FakeEcApi)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        app = FastMCP(name="t")
        ec.register(app)
        result = asyncio.run(
            app.call_tool("lookup_external_contact",
                          {"value": "+61400000000", "identifier_type": "Phone"})
        )
        import json
        text = getattr(result[0], "text", None) or result[0].get("text")
        payload = json.loads(text)
        assert payload["status"] == 404
        assert payload["kind"] == "external contact"
        assert payload["value"] == "+61400000000"
        assert payload["match"] is None  # back-compat field retained


class TestQueueEwtErrorHandling:
    """v1.3: queue_estimated_wait_time soft-fails on 404, hard-fails on 5xx."""

    def test_404_uses_canonical_envelope(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import analytics as an
        from mcp.server.fastmcp import FastMCP
        import asyncio

        class FakeExc(Exception):
            status = 404

        class FakeApi:
            def call_api(self, **kwargs):
                raise FakeExc()

        monkeypatch.setattr(an, "get_api", lambda: FakeApi())
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        app = FastMCP(name="t")
        an.register(app)
        result = asyncio.run(
            app.call_tool("queue_estimated_wait_time",
                          {"queue_ids": ["q-1"], "media_type": "call"})
        )
        import json
        text = getattr(result[0], "text", None) or result[0].get("text")
        payload = json.loads(text)
        assert len(payload["results"]) == 1
        row = payload["results"][0]
        assert row["status"] == 404
        assert row["kind"] == "estimated wait time"
        assert row["queue_id"] == "q-1"

    def test_non_404_propagates(self, monkeypatch):
        """Pre-v1.3 swallowed everything into an `error` string. v1.3 raises."""
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import analytics as an
        from mcp.server.fastmcp import FastMCP
        import asyncio

        class FakeExc(Exception):
            status = 500

        class FakeApi:
            def call_api(self, **kwargs):
                raise FakeExc("oops")

        monkeypatch.setattr(an, "get_api", lambda: FakeApi())
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        app = FastMCP(name="t")
        an.register(app)
        with pytest.raises(Exception):
            asyncio.run(
                app.call_tool("queue_estimated_wait_time",
                              {"queue_ids": ["q-1"]})
            )


# ─────────────────────── new tool ───────────────────────

class TestListOrgPresences:
    """v1.3: list_org_presences exposes the discovery path for pre-break setup."""

    def test_returns_id_label_system_presence(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import directory as dir_mod
        from mcp.server.fastmcp import FastMCP
        import asyncio

        class FakePresence:
            def __init__(self, id, system, labels, deactivated=False):
                self.id = id
                self.system_presence = system
                self.language_labels = labels
                self.deactivated = deactivated

        class FakeResp:
            def __init__(self, entities): self.entities = entities

        class FakePresenceApi:
            def __init__(self, *args, **kwargs): pass
            def get_presence_definitions(self, **kwargs):
                return FakeResp([
                    FakePresence("uuid-1", "BUSY", {"en": "Pre Break"}),
                    FakePresence("uuid-2", "AWAY", {"en": "Coaching"}),
                    FakePresence("uuid-3", "BUSY", {"en": "Training"}),
                ])

        monkeypatch.setattr(dir_mod.gc, "PresenceApi", FakePresenceApi)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        app = FastMCP(name="t")
        dir_mod.register(app)
        result = asyncio.run(app.call_tool("list_org_presences", {}))
        import json
        text = getattr(result[0], "text", None) or result[0].get("text")
        payload = json.loads(text)
        assert payload["count"] == 3
        ids = [p["id"] for p in payload["presences"]]
        assert ids == ["uuid-1", "uuid-2", "uuid-3"]
        labels = [p["label"] for p in payload["presences"]]
        assert labels == ["Pre Break", "Coaching", "Training"]

    def test_does_not_pass_pagination_kwargs_to_sdk(self, monkeypatch):
        """Regression for v1.13.1: ``get_presence_definitions`` (the underlying
        SDK method) accepts only ``deactivated``, ``division_id``,
        ``locale_code`` — passing ``page_size`` / ``page_number`` raises
        TypeError. Pre-fix list_org_presences passed both and crashed
        user-visibly. This test pins that those kwargs never leak through."""
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import directory as dir_mod
        from mcp.server.fastmcp import FastMCP
        import asyncio
        import json

        captured: dict = {"kwargs_seen": []}

        class FakeResp:
            entities: list = []

        class StrictFakePresenceApi:
            """Mimics the SDK's strict allowlist — raises if forbidden kwargs
            arrive, so a regression that re-introduces page_size hard-fails."""
            _ALLOWED = {"deactivated", "division_id", "locale_code"}
            def __init__(self, *args, **kwargs): pass
            def get_presence_definitions(self, **kwargs):
                captured["kwargs_seen"].append(dict(kwargs))
                forbidden = set(kwargs) - self._ALLOWED
                if forbidden:
                    raise TypeError(
                        "Got an unexpected keyword argument "
                        f"{sorted(forbidden)[0]!r} to method get_presence_definitions"
                    )
                return FakeResp()

        monkeypatch.setattr(dir_mod.gc, "PresenceApi", StrictFakePresenceApi)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        app = FastMCP(name="t")
        dir_mod.register(app)
        result = asyncio.run(app.call_tool("list_org_presences", {}))
        text = getattr(result[0], "text", None) or result[0].get("text")
        payload = json.loads(text)
        assert payload["count"] == 0  # empty entities → empty result, no crash
        # Critical: every call must have ONLY allowed kwargs.
        assert len(captured["kwargs_seen"]) >= 1
        for kw in captured["kwargs_seen"]:
            assert "page_size" not in kw, f"page_size leaked: {kw}"
            assert "page_number" not in kw, f"page_number leaked: {kw}"

    def test_name_contains_filter(self, monkeypatch):
        import PureCloudPlatformClientV2 as gc
        from genesys_mcp import client as gen_client
        from genesys_mcp.tools import directory as dir_mod
        from mcp.server.fastmcp import FastMCP
        import asyncio

        class FakePresence:
            def __init__(self, id, system, labels):
                self.id = id; self.system_presence = system
                self.language_labels = labels; self.deactivated = False

        class FakeResp:
            def __init__(self, entities): self.entities = entities

        class FakePresenceApi:
            def __init__(self, *args, **kwargs): pass
            def get_presence_definitions(self, **kwargs):
                return FakeResp([
                    FakePresence("u1", "BUSY", {"en": "Pre Break"}),
                    FakePresence("u2", "AWAY", {"en": "Coaching"}),
                ])

        monkeypatch.setattr(dir_mod.gc, "PresenceApi", FakePresenceApi)
        monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

        app = FastMCP(name="t")
        dir_mod.register(app)
        result = asyncio.run(
            app.call_tool("list_org_presences", {"name_contains": "pre break"})
        )
        import json
        text = getattr(result[0], "text", None) or result[0].get("text")
        payload = json.loads(text)
        assert payload["count"] == 1
        assert payload["presences"][0]["label"] == "Pre Break"


# ─────────────────────── presence_sessions pre-break ───────────────────────

class TestPresenceSessionsPreBreakLabel:
    """v1.3: presence_sessions re-labels BUSY+org_id matches to PRE_BREAK."""

    def test_helper_signature_includes_pre_break_param(self):
        """Just verify the new parameter is on the registered tool signature."""
        import inspect
        from genesys_mcp.tools import presence as p
        from mcp.server.fastmcp import FastMCP

        app = FastMCP(name="t")
        p.register(app)
        # The tool was registered inside a closure; we can't introspect it
        # directly. Verify via the registered tool list instead.
        tools = list(app._tool_manager._tools.values())
        ps_tool = next((t for t in tools if t.name == "presence_sessions"), None)
        assert ps_tool is not None
        # FastMCP exposes parameters via the json schema
        schema = ps_tool.parameters
        assert "pre_break_organization_presence_id" in schema["properties"]
