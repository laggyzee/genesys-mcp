"""Unit tests for the pure helpers added to scripts/provision_users.py.

The script lives outside the package layout (scripts/), so we load it by path
via importlib — the same approach conftest.py uses for skills/*/build_report.py.
Only the pure/extracted helpers are tested; the live API orchestration
(snapshot_template, execute_user) is exercised via --self-test against a real
tenant, consistent with this repo's "don't unit-test 1:1 SDK calls" convention.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _load_provision():
    path = _REPO_ROOT / "scripts" / "provision_users.py"
    spec = importlib.util.spec_from_file_location("provision_users", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["provision_users"] = mod
    spec.loader.exec_module(mod)
    return mod


pu = _load_provision()


class TestDerivePhoneName:
    @pytest.mark.parametrize("email,expected", [
        ("jane.doe@example.com", "Jane.Doe"),
        ("john_smith@example.com", "John.Smith"),
        ("mary-jane.watson@example.com", "Mary.Jane.Watson"),
        ("madonna@example.com", "Madonna"),
        ("a.b.c@x.io", "A.B.C"),
    ])
    def test_dotted_titlecase(self, email, expected):
        assert pu.derive_phone_name(email) == expected


class TestBuildPhoneBody:
    def test_shape(self):
        cfg = {
            "site_id": "site-1",
            "phone_base_settings_id": "pbs-1",
            "line_base_settings_id": "lbs-1",
        }
        body = pu.build_phone_body("Jane.Doe", "user-99", cfg)
        assert body == {
            "name": "Jane.Doe",
            "site": {"id": "site-1"},
            "phoneBaseSettings": {"id": "pbs-1"},
            "lines": [{"name": "Jane.Doe", "lineBaseSettings": {"id": "lbs-1"}}],
            "webRtcUser": {"id": "user-99"},
        }


class TestResolvePhoneConfig:
    def _fake_api_factory(self, sites, pbs, lbs):
        """Return a fake call_api dispatching on path. Mirrors {entities:[...]}."""
        def fake_call_api(api, method, path, *, body=None, query=None):
            if path.endswith("/sites"):
                return {"entities": sites}
            if path.endswith("/phonebasesettings"):
                return {"entities": pbs}
            if path.endswith("/linebasesettings"):
                return {"entities": lbs}
            raise AssertionError(f"unexpected path {path}")
        return fake_call_api

    def test_discovers_by_metabase(self, monkeypatch):
        sites = [{"id": "site-syd", "name": "Prvidr Sydney"},
                 {"id": "other", "name": "Somewhere Else"}]
        pbs = [{"id": "pbs-webrtc", "name": "WebRTC Phone",
                "phoneMetaBase": {"id": "developer_webrtc.json"}},
               {"id": "pbs-sip", "name": "Generic SIP",
                "phoneMetaBase": {"id": "generic_sip.json"}}]
        lbs = [{"id": "lbs-webrtc", "name": "WebRTC Line",
                "lineMetaBase": {"id": "developer_webrtc.json"}}]
        monkeypatch.setattr(pu, "call_api", self._fake_api_factory(sites, pbs, lbs))
        cfg = pu.resolve_phone_config(object())
        assert cfg == {"site_id": "site-syd",
                       "phone_base_settings_id": "pbs-webrtc",
                       "line_base_settings_id": "lbs-webrtc"}

    def test_falls_back_to_name_contains_webrtc(self, monkeypatch):
        sites = [{"id": "site-syd", "name": "Prvidr Sydney"}]
        pbs = [{"id": "pbs-webrtc", "name": "Acme WebRTC base"}]  # no metaBase field
        lbs = [{"id": "lbs-webrtc", "name": "Acme WebRTC line"}]
        monkeypatch.setattr(pu, "call_api", self._fake_api_factory(sites, pbs, lbs))
        cfg = pu.resolve_phone_config(object())
        assert cfg["phone_base_settings_id"] == "pbs-webrtc"
        assert cfg["line_base_settings_id"] == "lbs-webrtc"

    def test_site_not_found_raises(self, monkeypatch):
        monkeypatch.setattr(pu, "call_api",
                            self._fake_api_factory([{"id": "x", "name": "Nope"}], [], []))
        with pytest.raises(RuntimeError, match="site"):
            pu.resolve_phone_config(object())

    def test_ambiguous_webrtc_raises(self, monkeypatch):
        sites = [{"id": "site-syd", "name": "Prvidr Sydney"}]
        pbs = [{"id": "a", "name": "WebRTC one"}, {"id": "b", "name": "WebRTC two"}]
        monkeypatch.setattr(pu, "call_api", self._fake_api_factory(sites, pbs, []))
        with pytest.raises(RuntimeError, match="phone base settings"):
            pu.resolve_phone_config(object())

    def test_env_overrides_short_circuit(self, monkeypatch):
        monkeypatch.setattr(pu, "PHONE_SITE_ID_OVERRIDE", "env-site")
        monkeypatch.setattr(pu, "PHONE_BASE_SETTINGS_ID_OVERRIDE", "env-pbs")
        monkeypatch.setattr(pu, "PHONE_LINE_BASE_SETTINGS_ID_OVERRIDE", "env-lbs")
        def boom(*a, **k):
            raise AssertionError("should not call API when all overrides set")
        monkeypatch.setattr(pu, "call_api", boom)
        cfg = pu.resolve_phone_config(object())
        assert cfg == {"site_id": "env-site",
                       "phone_base_settings_id": "env-pbs",
                       "line_base_settings_id": "env-lbs"}


class TestCreatePhoneForUser:
    CFG = {"site_id": "s", "phone_base_settings_id": "p", "line_base_settings_id": "l"}

    def test_skips_when_phone_name_exists(self, monkeypatch):
        calls = []
        def fake_call_api(api, method, path, *, body=None, query=None):
            calls.append((method, path, query, body))
            if method == "GET":
                return {"entities": [{"id": "existing-phone", "name": "Jane.Doe"}]}
            raise AssertionError("POST must not happen when phone exists")
        monkeypatch.setattr(pu, "call_api", fake_call_api)
        status, pid = pu.create_phone_for_user(object(), "Jane.Doe", "u1", self.CFG)
        assert status == "skipped"
        assert pid == "existing-phone"
        assert all(m == "GET" for m, *_ in calls)

    def test_creates_when_absent(self, monkeypatch):
        seen = {}
        def fake_call_api(api, method, path, *, body=None, query=None):
            if method == "GET":
                return {"entities": []}
            seen["body"] = body
            return {"id": "new-phone"}
        monkeypatch.setattr(pu, "call_api", fake_call_api)
        status, pid = pu.create_phone_for_user(object(), "Jane.Doe", "u1", self.CFG)
        assert status == "created"
        assert pid == "new-phone"
        assert seen["body"]["webRtcUser"] == {"id": "u1"}
        assert seen["body"]["name"] == "Jane.Doe"
        assert seen["body"]["lines"][0]["lineBaseSettings"] == {"id": "l"}

    def test_propagates_post_failure(self, monkeypatch):
        from PureCloudPlatformClientV2.rest import ApiException
        def fake_call_api(api, method, path, *, body=None, query=None):
            if method == "GET":
                return {"entities": []}
            raise ApiException(status=400, reason="bad")
        monkeypatch.setattr(pu, "call_api", fake_call_api)
        with pytest.raises(ApiException):
            pu.create_phone_for_user(object(), "Jane.Doe", "u1", self.CFG)
