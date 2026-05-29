"""Pins the v1.0 ``operating_model`` toggles + their graceful-degradation paths.

Three toggles, three behaviour contracts:

- ``has_pre_break_presence`` — false → daily-brief renders a "tracking
  disabled" callout instead of pre-break overrun summary; the validator
  refuses true-without-presence-id configs.
- ``has_brand_structure`` — false → must have ≤1 brand in ``brands.names``
  (the validator catches the inconsistency).
- ``expected_channels`` — controls which headline KPI cards render in the
  daily brief (a message-only tenant doesn't get a misleading 'voice SL 0%').
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Schema-level: operating_model block exists with sensible defaults ──

class TestOperatingModelSchema:
    def test_defaults_assume_prvidr_shape(self, temp_tenant_config):
        from genesys_mcp.tenant import load_config
        cfg = load_config()
        om = cfg.operating_model
        # The test fixture sets has_pre_break_presence: false explicitly
        assert om.has_pre_break_presence is False
        # Other defaults are unset in the fixture → defaults apply
        assert om.has_brand_structure is True
        assert om.expected_channels == ["voice", "message"]


# ── Schema-level: invariants enforced by the model validator ──

class TestPreBreakInvariant:
    def test_has_pre_break_true_without_id_fails(self, tmp_path: Path, monkeypatch):
        """``has_pre_break_presence: true`` requires the presence id to be set."""
        from genesys_mcp.tenant import TenantConfigError, load_config
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(
            """tenant:
  name: "X"
  short_name: "x"
brands:
  names: ["BrandA"]
specialist_roles: ["Spec"]
operating_model:
  has_pre_break_presence: true
"""
        )
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        with pytest.raises(TenantConfigError, match="pre_break_organisation_presence_id is unset"):
            load_config()

    def test_has_pre_break_false_does_not_need_id(self, tmp_path: Path, monkeypatch):
        from genesys_mcp.tenant import load_config
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(
            """tenant:
  name: "X"
  short_name: "x"
brands:
  names: ["BrandA"]
specialist_roles: ["Spec"]
operating_model:
  has_pre_break_presence: false
"""
        )
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        cfg = load_config()
        assert cfg.operating_model.has_pre_break_presence is False


class TestBrandStructureInvariant:
    def test_has_brand_false_with_multiple_brands_fails(self, tmp_path: Path, monkeypatch):
        from genesys_mcp.tenant import TenantConfigError, load_config
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(
            """tenant:
  name: "X"
  short_name: "x"
brands:
  names: ["BrandA", "BrandB"]
specialist_roles: ["Spec"]
operating_model:
  has_pre_break_presence: false
  has_brand_structure: false
"""
        )
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        with pytest.raises(TenantConfigError, match="has_brand_structure is False but"):
            load_config()

    def test_has_brand_false_with_single_brand_passes(self, tmp_path: Path, monkeypatch):
        from genesys_mcp.tenant import load_config
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(
            """tenant:
  name: "X"
  short_name: "x"
brands:
  names: ["Only Brand"]
specialist_roles: ["Spec"]
operating_model:
  has_pre_break_presence: false
  has_brand_structure: false
"""
        )
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        cfg = load_config()
        assert cfg.operating_model.has_brand_structure is False


class TestExpectedChannelsValidation:
    def test_normalises_case(self, tmp_path: Path, monkeypatch):
        from genesys_mcp.tenant import load_config
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(
            """tenant:
  name: "X"
  short_name: "x"
brands:
  names: ["BrandA"]
specialist_roles: ["Spec"]
operating_model:
  has_pre_break_presence: false
  expected_channels: ["VOICE", "Message"]
"""
        )
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        cfg = load_config()
        assert cfg.operating_model.expected_channels == ["voice", "message"]

    def test_rejects_unknown_channel(self, tmp_path: Path, monkeypatch):
        from genesys_mcp.tenant import TenantConfigError, load_config
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(
            """tenant:
  name: "X"
  short_name: "x"
brands:
  names: ["BrandA"]
specialist_roles: ["Spec"]
operating_model:
  has_pre_break_presence: false
  expected_channels: ["voice", "telegram"]
"""
        )
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        with pytest.raises(TenantConfigError, match="unknown channel"):
            load_config()

    def test_empty_list_rejected(self, tmp_path: Path, monkeypatch):
        from genesys_mcp.tenant import TenantConfigError, load_config
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(
            """tenant:
  name: "X"
  short_name: "x"
brands:
  names: ["BrandA"]
specialist_roles: ["Spec"]
operating_model:
  has_pre_break_presence: false
  expected_channels: []
"""
        )
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        with pytest.raises(TenantConfigError, match="at least one channel"):
            load_config()


# ── Render-level: daily-brief respects the toggles ──

class TestDailyBriefChannelAwareness:
    def _headline_payload(self) -> dict:
        return {
            "voice_sl_today": 55.0, "voice_sl_baseline": 53.0,
            "message_sl_today": 59.0, "message_sl_baseline": 45.0,
            "total_offered_today": 1245,
        }

    def test_voice_only_omits_message_card(self, tmp_path: Path, monkeypatch,
                                           build_report_daily):
        from genesys_mcp.tenant import load_config
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(
            """tenant:
  name: "Voice Only"
  short_name: "vo"
brands:
  names: ["BrandA"]
specialist_roles: ["Spec"]
operating_model:
  has_pre_break_presence: false
  expected_channels: ["voice"]
"""
        )
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        cfg = load_config()
        html = build_report_daily.render_html(
            cfg, "2026-05-25", "interval", "window",
            self._headline_payload(), worst=[], flagged=[], hotlist=[],
            adherence=[],
        )
        assert "Voice SL" in html
        assert "Message SL" not in html

    def test_message_only_omits_voice_card(self, tmp_path: Path, monkeypatch,
                                           build_report_daily):
        from genesys_mcp.tenant import load_config
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(
            """tenant:
  name: "Message Only"
  short_name: "mo"
brands:
  names: ["BrandA"]
specialist_roles: ["Spec"]
operating_model:
  has_pre_break_presence: false
  expected_channels: ["message"]
"""
        )
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        cfg = load_config()
        html = build_report_daily.render_html(
            cfg, "2026-05-25", "interval", "window",
            self._headline_payload(), worst=[], flagged=[], hotlist=[],
            adherence=[],
        )
        assert "Voice SL" not in html
        assert "Message SL" in html


class TestDailyBriefPreBreakAwareness:
    def _headline(self) -> dict:
        return {"voice_sl_today": 80.0, "voice_sl_baseline": 80.0,
                "message_sl_today": 80.0, "message_sl_baseline": 80.0,
                "total_offered_today": 100}

    def test_no_pre_break_renders_tracking_disabled_callout(
        self, temp_tenant_config, build_report_daily,
    ):
        from genesys_mcp.tenant import load_config
        cfg = load_config()  # fixture has has_pre_break_presence: false
        html = build_report_daily.render_html(
            cfg, "2026-05-25", "interval", "window",
            self._headline(), worst=[], flagged=[], hotlist=[],
            adherence=[], adherence_summary_data={
                "break_meal_min": 0, "pre_break_min": 0, "total_min": 0,
                "agents_with_any": 0, "overrun_sessions": 0,
            },
        )
        assert "Pre-break tracking disabled for this tenant" in html
