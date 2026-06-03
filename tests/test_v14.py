"""Pin v1.4: call quality + batch ergonomics.

Covers all four deliverables:

1. ``get_user_presence_now`` label resolution + cache
2. ``agent_adherence_review`` concurrent fan-out
3. ``find_user`` batch variant (`name_contains_list`)
4. ``voice_call_quality`` (new tool, MOS scores)
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _fake_to_dict(obj):
    """Identity-ish to_dict for fakes that already use plain dicts or .to_dict()."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, list):
        return obj
    return obj


def _call_tool(register_fn, tool_name, args, monkeypatch, *, sdk_patches):
    """Boilerplate: register tool, monkeypatch SDK pieces, invoke, return parsed JSON.

    Auto-patches ``to_dict`` in every tools module so fakes don't need to
    expose ``swagger_types``.
    """
    import PureCloudPlatformClientV2 as gc
    from genesys_mcp import client as gen_client
    from genesys_mcp.tools import (
        analytics, conversations, coaching, directory,
        external_contacts, presence, reports, routing,
        speech_analytics, wfm,
    )
    from mcp.server.fastmcp import FastMCP

    # Patch to_dict in every tools module — saves having to thread it
    # through individual SDK patches.
    for mod in (analytics, conversations, coaching, directory,
                external_contacts, presence, reports, routing,
                speech_analytics, wfm):
        if hasattr(mod, "to_dict"):
            monkeypatch.setattr(mod, "to_dict", _fake_to_dict)

    for module, attr, value in sdk_patches:
        monkeypatch.setattr(module, attr, value)
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

    app = FastMCP(name="t")
    register_fn(app)
    result = asyncio.run(app.call_tool(tool_name, args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text)


# ─────────────────────── #3 find_user batch ───────────────────────

class TestFindUserBatch:
    def test_single_mode_unchanged(self, monkeypatch):
        from genesys_mcp.tools import directory as d

        class FakeSearchApi:
            def __init__(self, *a, **k): pass
            def post_users_search(self, body):
                class R:
                    def to_dict(self): return {"results": [{"id": "u1", "name": "Jane"}]}
                return R()

        out = _call_tool(d.register, "find_user", {"query": "Jane"}, monkeypatch,
                         sdk_patches=[(d.gc, "SearchApi", FakeSearchApi)])
        assert out["match_count"] == 1
        assert out["users"][0]["id"] == "u1"

    def test_batch_mode_groups_results_per_query(self, monkeypatch):
        from genesys_mcp.tools import directory as d

        # Return different results for different queries
        calls = []

        class FakeSearchApi:
            def __init__(self, *a, **k): pass
            def post_users_search(self, body):
                q = body["query"][0]["value"]
                calls.append(q)

                class R:
                    def to_dict(self):
                        if q == "Jane":
                            return {"results": [{"id": "u1", "name": "Jane Smith"}]}
                        if q == "Bob":
                            return {"results": [{"id": "u2", "name": "Bob Jones"}]}
                        return {"results": []}
                return R()

        out = _call_tool(d.register, "find_user",
                         {"name_contains_list": ["Jane", "Bob", "NotReal"]},
                         monkeypatch,
                         sdk_patches=[(d.gc, "SearchApi", FakeSearchApi)])
        assert out["mode"] == "batch"
        assert out["total_queries"] == 3
        assert out["matched_queries"] == 2
        assert "NotReal" in out["unmatched"]
        # Both query strings should appear among matches
        matched_queries = {m["name_query"] for m in out["matches"]}
        assert matched_queries == {"Jane", "Bob"}

    def test_mutex_errors_when_neither_provided(self, monkeypatch):
        from genesys_mcp.tools import directory as d

        class FakeSearchApi:
            def __init__(self, *a, **k): pass

        with pytest.raises(Exception):
            _call_tool(d.register, "find_user", {}, monkeypatch,
                       sdk_patches=[(d.gc, "SearchApi", FakeSearchApi)])

    def test_mutex_errors_when_both_provided(self, monkeypatch):
        from genesys_mcp.tools import directory as d

        class FakeSearchApi:
            def __init__(self, *a, **k): pass

        with pytest.raises(Exception):
            _call_tool(d.register, "find_user",
                       {"query": "x", "name_contains_list": ["y"]},
                       monkeypatch,
                       sdk_patches=[(d.gc, "SearchApi", FakeSearchApi)])


# ─────────────────────── #1 get_user_presence_now label ───────────────────────

class TestGetUserPresenceNowLabel:
    def _patch_users_api(self, monkeypatch, presence_def_id="def-1"):
        from genesys_mcp.tools import directory as d

        class FakeUserResp:
            def to_dict(self):
                return {
                    "name": "Jane",
                    "presence": {
                        "systemPresence": "BUSY",
                        "presenceDefinition": {"id": presence_def_id, "systemPresence": "BUSY"},
                        "modifiedDate": "2026-06-01T00:00:00Z",
                    },
                    "routingStatus": {"status": "IDLE", "startTime": "2026-06-01T00:00:00Z"},
                }

        class FakeUsersApi:
            def __init__(self, *a, **k): pass
            def get_user(self, **kwargs): return FakeUserResp()

        monkeypatch.setattr(d.gc, "UsersApi", FakeUsersApi)

    def _patch_presence_api(self, monkeypatch, presences):
        from genesys_mcp.tools import directory as d

        class FakeDef:
            def __init__(self, id, sp, labels):
                self.id = id
                self.system_presence = sp
                self.language_labels = labels

        class FakeResp:
            def __init__(self, entities): self.entities = entities

        class FakePresenceApi:
            def __init__(self, *a, **k): pass
            def get_presence_definitions(self, **kwargs):
                return FakeResp([FakeDef(p[0], p[1], p[2]) for p in presences])

        monkeypatch.setattr(d.gc, "PresenceApi", FakePresenceApi)

    def test_label_resolved_inline(self, monkeypatch):
        from genesys_mcp.tools import directory as d
        # Reset the module-level cache so this test sees a clean slate.
        monkeypatch.setattr(d, "_PRESENCE_LABEL_CACHE", {})
        monkeypatch.setattr(d, "_PRESENCE_CACHE_LOADED", False)
        self._patch_users_api(monkeypatch, presence_def_id="def-pre-break")
        self._patch_presence_api(monkeypatch, [
            ("def-pre-break", "BUSY", {"en": "Pre Break"}),
            ("def-coaching", "AWAY", {"en": "Coaching"}),
        ])

        out = _call_tool(d.register, "get_user_presence_now",
                         {"user_ids": ["u1"]},
                         monkeypatch,
                         sdk_patches=[])
        assert out["results"][0]["presence_label"] == "Pre Break"
        assert out["results"][0]["presence_definition_id"] == "def-pre-break"

    def test_include_label_false_skips_lookup(self, monkeypatch):
        from genesys_mcp.tools import directory as d
        monkeypatch.setattr(d, "_PRESENCE_LABEL_CACHE", {})
        monkeypatch.setattr(d, "_PRESENCE_CACHE_LOADED", False)
        self._patch_users_api(monkeypatch)

        # Note: NO presence-api patch — would fail if include_label tried to load
        class FailingPresenceApi:
            def __init__(self, *a, **k):
                raise AssertionError("presence api should not be called when include_label=False")

        monkeypatch.setattr(d.gc, "PresenceApi", FailingPresenceApi)

        out = _call_tool(d.register, "get_user_presence_now",
                         {"user_ids": ["u1"], "include_label": False},
                         monkeypatch, sdk_patches=[])
        assert "presence_label" not in out["results"][0]

    def test_unknown_id_returns_none_label(self, monkeypatch):
        from genesys_mcp.tools import directory as d
        monkeypatch.setattr(d, "_PRESENCE_LABEL_CACHE", {})
        monkeypatch.setattr(d, "_PRESENCE_CACHE_LOADED", False)
        self._patch_users_api(monkeypatch, presence_def_id="def-not-in-cache")
        self._patch_presence_api(monkeypatch, [("def-other", "BUSY", {"en": "Other"})])

        out = _call_tool(d.register, "get_user_presence_now",
                         {"user_ids": ["u1"]}, monkeypatch, sdk_patches=[])
        # Cache loaded but UUID not in it → label is None
        assert out["results"][0]["presence_label"] is None

    def test_cache_hit_no_second_load(self, monkeypatch):
        from genesys_mcp.tools import directory as d
        monkeypatch.setattr(d, "_PRESENCE_LABEL_CACHE", {})
        monkeypatch.setattr(d, "_PRESENCE_CACHE_LOADED", False)
        self._patch_users_api(monkeypatch, presence_def_id="def-pre-break")

        # Count load calls
        load_count = {"n": 0}

        class CountingPresenceApi:
            def __init__(self, *a, **k): pass
            def get_presence_definitions(self, **kwargs):
                load_count["n"] += 1

                class Def:
                    id = "def-pre-break"
                    system_presence = "BUSY"
                    language_labels = {"en": "Pre Break"}

                class R:
                    entities = [Def()]

                return R()

        monkeypatch.setattr(d.gc, "PresenceApi", CountingPresenceApi)

        # First call — triggers load
        _call_tool(d.register, "get_user_presence_now",
                   {"user_ids": ["u1"]}, monkeypatch, sdk_patches=[])
        assert load_count["n"] == 1

        # Second call — cache hit, no extra load
        _call_tool(d.register, "get_user_presence_now",
                   {"user_ids": ["u1"]}, monkeypatch, sdk_patches=[])
        assert load_count["n"] == 1, (
            "presence definitions should be cached after first load"
        )


# ─────────────────────── #4 voice_call_quality ───────────────────────

class TestVoiceCallQuality:
    def _fake_conv(self, mos_values, media_type="voice"):
        """Build a fake analytics conversation detail with given per-stat MOS values."""
        return {
            "conversationId": "conv-1",
            "participants": [{
                "sessions": [{
                    "mediaType": media_type,
                    "mediaEndpointStats": [{"minMos": v} for v in mos_values],
                }],
            }],
        }

    def _patch(self, monkeypatch, mos_values=None, media_type="voice",
               raise_404=False, raise_other=False):
        from genesys_mcp.tools import conversations as c

        class FakeExc(Exception):
            status = 404 if raise_404 else 500

        class FakeResp:
            def __init__(self, data): self._d = data
            def to_dict(self): return self._d

        class FakeAnalyticsApi:
            def __init__(self, *a, **k): pass
            def get_analytics_conversation_details(self, **kwargs):
                if raise_404 or raise_other:
                    raise FakeExc()
                return FakeResp(self_outer._fake_conv(mos_values or [], media_type))

        self_outer = self
        monkeypatch.setattr(c.gc, "AnalyticsApi", FakeAnalyticsApi)

    def test_good_quality_call(self, monkeypatch):
        from genesys_mcp.tools import conversations as c
        self._patch(monkeypatch, mos_values=[4.5, 4.3, 4.7])
        out = _call_tool(c.register, "voice_call_quality",
                         {"conversation_ids": ["conv-1"]}, monkeypatch,
                         sdk_patches=[])
        r = out["results"][0]
        assert r["quality_label"] == "good"
        assert r["min_mos"] == 4.3
        assert r["segments_evaluated"] == 3
        assert r["segments_with_low_mos"] == 0

    def test_fair_quality_call(self, monkeypatch):
        from genesys_mcp.tools import conversations as c
        self._patch(monkeypatch, mos_values=[3.2, 3.5, 3.8])
        out = _call_tool(c.register, "voice_call_quality",
                         {"conversation_ids": ["conv-1"]}, monkeypatch,
                         sdk_patches=[])
        r = out["results"][0]
        assert r["quality_label"] == "fair"
        # 3.2 + 3.5 default threshold of 3.5 → 1 low segment (3.2 only)
        assert r["segments_with_low_mos"] == 1

    def test_poor_quality_call(self, monkeypatch):
        from genesys_mcp.tools import conversations as c
        self._patch(monkeypatch, mos_values=[2.1, 2.8, 3.5])
        out = _call_tool(c.register, "voice_call_quality",
                         {"conversation_ids": ["conv-1"]}, monkeypatch,
                         sdk_patches=[])
        r = out["results"][0]
        assert r["quality_label"] == "poor"
        assert r["min_mos"] == 2.1

    def test_non_voice_conv_returns_no_voice_segments(self, monkeypatch):
        from genesys_mcp.tools import conversations as c
        self._patch(monkeypatch, mos_values=[4.5], media_type="message")
        out = _call_tool(c.register, "voice_call_quality",
                         {"conversation_ids": ["conv-1"]}, monkeypatch,
                         sdk_patches=[])
        assert out["results"][0]["no_voice_segments"] is True

    def test_404_returns_canonical_envelope(self, monkeypatch):
        from genesys_mcp.tools import conversations as c
        self._patch(monkeypatch, raise_404=True)
        out = _call_tool(c.register, "voice_call_quality",
                         {"conversation_ids": ["dead-conv"]}, monkeypatch,
                         sdk_patches=[])
        r = out["results"][0]
        assert r["status"] == 404
        assert r["kind"] == "conversation"
        assert r["conversation_id"] == "dead-conv"

    def test_custom_low_mos_threshold(self, monkeypatch):
        from genesys_mcp.tools import conversations as c
        self._patch(monkeypatch, mos_values=[3.7, 3.9, 4.0])
        out = _call_tool(c.register, "voice_call_quality",
                         {"conversation_ids": ["conv-1"], "low_mos_threshold": 4.0},
                         monkeypatch, sdk_patches=[])
        # 3.7, 3.9 are below 4.0 → 2 low segments
        assert out["results"][0]["segments_with_low_mos"] == 2


# ─────────────────────── #2 agent_adherence_review batch ───────────────────────

class TestAgentAdherenceReviewBatch:
    """Concurrent fan-out: N adherence queries run in a thread pool."""

    def test_thread_pool_fan_out_calls_one_per_user(self, monkeypatch):
        """Sanity: each user gets one query call regardless of pool size."""
        from genesys_mcp.tools import wfm as w

        # Patch the AnalyticsApi-side machinery to return zero presence sessions
        # so we focus on the WFM adherence query.
        class FakeAnalyticsApi:
            def __init__(self, *a, **k): pass
            def post_analytics_users_details_jobs(self, body):
                class R:
                    job_id = "job-1"
                return R()
            def get_analytics_users_details_job(self, job_id):
                class R:
                    state = "FULFILLED"
                return R()
            def get_analytics_users_details_job_results(self, **kwargs):
                class R:
                    def to_dict(self): return {"userDetails": []}
                return R()

        adherence_calls = []

        class FakeWfmApi:
            def __init__(self, *a, **k): pass
            def post_workforcemanagement_agent_adherence_explanations_query(
                self, agent_id, body,
            ):
                adherence_calls.append(agent_id)

                class R:
                    def to_dict(self): return {"entities": []}
                return R()

        monkeypatch.setattr(w.gc, "AnalyticsApi", FakeAnalyticsApi)
        monkeypatch.setattr(w.gc, "WorkforceManagementApi", FakeWfmApi)

        # Patch the name resolver since we don't want to make user-name fetches
        from genesys_mcp.naming import resolver as nresolver
        monkeypatch.setattr(nresolver, "user_names",
                            lambda uids: {u: f"name-{u}" for u in uids})

        out = _call_tool(w.register, "agent_adherence_review",
                         {"user_ids": ["u1", "u2", "u3"]},
                         monkeypatch, sdk_patches=[])

        # Exactly one adherence call per user, regardless of pool concurrency
        assert sorted(adherence_calls) == ["u1", "u2", "u3"]
        # Response shape backward-compatible
        assert len(out["users"]) == 3
        assert {u["user_id"] for u in out["users"]} == {"u1", "u2", "u3"}

    def test_per_user_failure_does_not_break_overall(self, monkeypatch):
        """A failing per-user adherence call surfaces as empty explanations,
        not a tool-level exception."""
        from genesys_mcp.tools import wfm as w

        class FakeAnalyticsApi:
            def __init__(self, *a, **k): pass
            def post_analytics_users_details_jobs(self, body):
                class R: job_id = "job-1"
                return R()
            def get_analytics_users_details_job(self, job_id):
                class R: state = "FULFILLED"
                return R()
            def get_analytics_users_details_job_results(self, **kwargs):
                class R:
                    def to_dict(self): return {"userDetails": []}
                return R()

        class FakeWfmApi:
            def __init__(self, *a, **k): pass
            def post_workforcemanagement_agent_adherence_explanations_query(
                self, agent_id, body,
            ):
                if agent_id == "broken-u":
                    raise RuntimeError("boom")
                class R:
                    def to_dict(self): return {"entities": []}
                return R()

        monkeypatch.setattr(w.gc, "AnalyticsApi", FakeAnalyticsApi)
        monkeypatch.setattr(w.gc, "WorkforceManagementApi", FakeWfmApi)
        from genesys_mcp.naming import resolver as nresolver
        monkeypatch.setattr(nresolver, "user_names",
                            lambda uids: {u: f"name-{u}" for u in uids})

        out = _call_tool(w.register, "agent_adherence_review",
                         {"user_ids": ["good-u", "broken-u"]},
                         monkeypatch, sdk_patches=[])
        # Both users present in output; broken one has 0 explanations
        by_uid = {u["user_id"]: u for u in out["users"]}
        assert by_uid["broken-u"]["explanations_logged"] == 0
        assert by_uid["good-u"]["explanations_logged"] == 0  # both empty in this fixture
