"""End-to-end smoke tests for the v1.0 weird-tenant fixtures.

Each fixture in ``tests/fixtures/weird_tenants/<shape>/`` exercises a
non-default-shaped tenant config. The tests:

1. Confirm ``load_config()`` accepts each config — pinning the
   model-validator invariants don't false-positive on legitimate variation.
2. Drive a tiny synthetic data set through ``cc-daily-brief``'s
   ``build_report.py`` and assert the right "degradation" hooks fire:
   tracking-disabled callouts, omitted KPI cards, etc.

These exist as the tenant-agnostic regression net — a future change that
breaks one of the shape paths trips here before any real deployer hits it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_WEIRD_DIR = Path(__file__).resolve().parent / "fixtures" / "weird_tenants"


def _empty_qp() -> dict:
    """Minimal but shape-valid queue_performance payload — zero traffic."""
    return {"results": []}


def _empty_ap() -> dict:
    return {"results": []}


def _empty_deep() -> dict:
    return {"interval": "", "scope": {}, "org_rollup": {}, "repeaters": []}


def _empty_brk() -> dict:
    return {
        "interval": "", "break_target_min": 15, "meal_target_min": 30,
        "pre_break_target_min": 10, "tolerance_min": 2, "users": [],
    }


def _write_data_dir(tmp_path: Path) -> Path:
    """Write minimal synthetic data files for cc-daily-brief into tmp_path."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "queue_perf_day.json").write_text(json.dumps(_empty_qp()))
    (d / "queue_perf_window.json").write_text(json.dumps(_empty_qp()))
    (d / "agent_perf_day.json").write_text(json.dumps(_empty_ap()))
    (d / "repeat_callers.json").write_text(json.dumps(_empty_deep()))
    (d / "break_overrun.json").write_text(json.dumps(_empty_brk()))
    return d


@pytest.mark.parametrize("shape", ["single_brand", "no_pre_break", "message_only"])
class TestConfigLoads:
    """Every weird-tenant config must validate cleanly."""

    def test_config_loads_without_error(self, shape: str, monkeypatch):
        from genesys_mcp.tenant import load_config
        cfg_path = _WEIRD_DIR / shape / "tenant.yaml"
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        cfg = load_config()
        assert cfg.tenant.name  # smoke


class TestSingleBrandShape:
    SHAPE = "single_brand"

    def test_brand_structure_disabled(self, monkeypatch):
        from genesys_mcp.tenant import load_config
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(_WEIRD_DIR / self.SHAPE / "tenant.yaml"))
        cfg = load_config()
        assert cfg.operating_model.has_brand_structure is False
        assert len(cfg.brands.names) == 1


class TestNoPreBreakShape:
    SHAPE = "no_pre_break"

    def test_daily_brief_renders_tracking_disabled_callout(
        self, monkeypatch, tmp_path: Path, build_report_daily,
    ):
        from genesys_mcp.tenant import load_config
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(_WEIRD_DIR / self.SHAPE / "tenant.yaml"))
        cfg = load_config()

        html = build_report_daily.render_html(
            cfg, "2026-05-25", "interval", "window",
            headline={
                "voice_sl_today": 80.0, "voice_sl_baseline": 80.0,
                "message_sl_today": 80.0, "message_sl_baseline": 80.0,
                "total_offered_today": 0,
            },
            worst=[], flagged=[], hotlist=[], adherence=[],
            adherence_summary_data={
                "break_meal_min": 0, "pre_break_min": 0, "total_min": 0,
                "agents_with_any": 0, "overrun_sessions": 0,
            },
        )
        assert "Pre-break tracking disabled for this tenant" in html


class TestMessageOnlyShape:
    SHAPE = "message_only"

    def test_daily_brief_omits_voice_sl_card(
        self, monkeypatch, tmp_path: Path, build_report_daily,
    ):
        from genesys_mcp.tenant import load_config
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(_WEIRD_DIR / self.SHAPE / "tenant.yaml"))
        cfg = load_config()

        html = build_report_daily.render_html(
            cfg, "2026-05-25", "interval", "window",
            headline={
                "voice_sl_today": None, "voice_sl_baseline": None,
                "message_sl_today": 95.0, "message_sl_baseline": 92.0,
                "total_offered_today": 50,
            },
            worst=[], flagged=[], hotlist=[],
            adherence=[],
        )
        assert "Voice SL" not in html
        assert "Message SL" in html
        # Channel summary on the total-interactions card should read "message"
        assert "message offered" in html
