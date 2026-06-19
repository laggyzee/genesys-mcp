"""Tests for the conversation deep-link helper.

Small standalone module — pure functions of string + env / args. Tests cover
the resolution priority (explicit > region > env > None), the URL shape,
and the fallback rendering when no base URL is resolvable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from genesys_mcp.conversation_links import (  # noqa: E402
    REGION_TO_APP_HOST,
    conversation_url,
    render_conversation_cell,
    resolve_app_base_url,
)


class TestResolveAppBaseUrl:
    def test_explicit_tenant_url_wins(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GENESYS_REGION", "us-east-1")
        url = resolve_app_base_url(tenant_base_url="https://custom.example.com")
        assert url == "https://custom.example.com"

    def test_trailing_slash_stripped(self):
        url = resolve_app_base_url(tenant_base_url="https://custom.example.com/")
        assert url == "https://custom.example.com"

    def test_region_argument_overrides_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GENESYS_REGION", "us-east-1")
        # Pass an explicit region; should map to AU regardless of env
        url = resolve_app_base_url(region="ap-southeast-2")
        assert url == "https://apps.mypurecloud.com.au"

    def test_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GENESYS_REGION", "eu-west-1")
        assert resolve_app_base_url() == "https://apps.mypurecloud.ie"

    def test_returns_none_when_no_resolution(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("GENESYS_REGION", raising=False)
        assert resolve_app_base_url() is None

    def test_unknown_region_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GENESYS_REGION", "narnia-1")
        assert resolve_app_base_url() is None

    def test_known_regions_have_https_scheme(self):
        # Every region in the map should produce a valid https URL.
        for region in REGION_TO_APP_HOST:
            url = resolve_app_base_url(region=region)
            assert url is not None
            assert url.startswith("https://")
            assert not url.endswith("/")


class TestConversationUrl:
    def test_canonical_url_shape(self):
        # The /directory/#/analytics/interactions/{id}/admin path is
        # what Genesys's web app uses. If they rename the route, this test
        # fails — that's the signal to update the helper.
        url = conversation_url("abc-123", region="ap-southeast-2")
        assert url == (
            "https://apps.mypurecloud.com.au"
            "/directory/#/analytics/interactions/abc-123/admin"
        )

    def test_empty_id_returns_none(self):
        assert conversation_url("", region="ap-southeast-2") is None

    def test_no_region_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("GENESYS_REGION", raising=False)
        assert conversation_url("abc-123") is None


class TestRenderConversationCell:
    def test_clickable_link_when_resolvable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GENESYS_REGION", "ap-southeast-2")
        html = render_conversation_cell("abc-12345-very-long-id")
        assert "<a href=" in html
        assert 'target="_blank"' in html
        # HREF includes the full id (correct — clickable target); the
        # visible label is truncated to 8 chars + ellipsis.
        assert "interactions/abc-12345-very-long-id/admin" in html
        assert ">abc-1234…</a>" in html  # truncated label

    def test_falls_back_to_code_when_no_base_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("GENESYS_REGION", raising=False)
        html = render_conversation_cell("abc-12345")
        assert "<code" in html
        assert "<a href=" not in html

    def test_empty_id_returns_empty_string(self):
        assert render_conversation_cell(None) == ""
        assert render_conversation_cell("") == ""

    def test_explicit_base_url_overrides_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GENESYS_REGION", "us-east-1")
        html = render_conversation_cell(
            "abc", tenant_base_url="https://custom.example.com",
        )
        assert "custom.example.com" in html
        assert "mypurecloud" not in html
