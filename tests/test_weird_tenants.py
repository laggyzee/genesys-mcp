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


@pytest.mark.parametrize("shape", ["single_brand", "no_pre_break", "message_only", "no_survey"])
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


class TestNoSurveyShape:
    """v1.11 fixture: ``survey`` block absent, ``business_unit.id`` null.

    Pins the graceful-when-absent contract for every v1.11 wiring point —
    NPS card, wrap-up mini-card, Customer Experience section, Wrap-up Codes
    section, Leave summary, Occupancy column. Without these inputs the
    skills must render exactly the v1.10-era output, no crashes.
    """

    SHAPE = "no_survey"

    def test_survey_defaults_all_none(self, monkeypatch):
        from genesys_mcp.tenant import load_config
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(_WEIRD_DIR / self.SHAPE / "tenant.yaml"))
        cfg = load_config()
        # Pydantic _Survey defaults — all None when block absent.
        assert cfg.survey.nps_attribute_key is None
        assert cfg.survey.agent_score_attribute_key is None
        assert cfg.survey.experience_score_attribute_key is None
        # business_unit.id absent → None, won't trigger wfm_time_off_requests
        assert cfg.business_unit.id is None
        # management_units.ids absent → empty list
        assert cfg.management_units.ids == []

    def test_daily_brief_renders_without_v1_11_inputs(
        self, monkeypatch, build_report_daily,
    ):
        from genesys_mcp.tenant import load_config
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(_WEIRD_DIR / self.SHAPE / "tenant.yaml"))
        cfg = load_config()

        # Render with nps=None, wrap_mini=None — both default
        html = build_report_daily.render_html(
            cfg, "2026-06-23", "interval", "window",
            headline={
                "voice_sl_today": 80.0, "voice_sl_baseline": 80.0,
                "message_sl_today": 80.0, "message_sl_baseline": 80.0,
                "total_offered_today": 100,
            },
            worst=[], flagged=[], hotlist=[], adherence=[],
        )
        # No NPS card (gated on nps != None)
        assert "NPS (yesterday)" not in html
        # No wrap-up mini-section (gated on wrap_mini != None)
        assert "Top wrap-up codes" not in html
        # Existing cards still render
        assert "Voice SL" in html and "Message SL" in html

    def test_monthly_report_renders_without_v1_11_inputs(
        self, monkeypatch, build_report_monthly,
    ):
        from genesys_mcp.tenant import load_config
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(_WEIRD_DIR / self.SHAPE / "tenant.yaml"))
        cfg = load_config()

        # Minimal-but-valid args, no v1.11 kwargs passed → all default None.
        themes = {
            "scope": {"shortlisted": 0, "candidates_meeting_min_calls": 0},
            "top_dispositions": [],
            "top_ai_outcomes": {"Resolved": 1},
            "top_expected_fixes": [],
            "unresolved_repeaters": [],
        }
        html = build_report_monthly.render_html(
            period="June 2026",
            interval="2026-06-01T00:00:00.000Z/2026-07-01T00:00:00.000Z",
            brand_rows=[],
            per_queue=[],
            workforce=[],
            themes=themes,
            cfg=cfg,
        )
        # No Customer Experience section (cx is None by default)
        assert 'id="cx"' not in html
        # No Wrap-up Codes section (wrap_up is None by default)
        assert 'id="wrapup"' not in html
        # No Leave summary callout
        assert "Leave taken this period" not in html
        # No Occupancy column header
        assert "Occupancy" not in html
        # TOC also omits the new entries
        assert "#cx" not in html
        assert "#wrapup" not in html
        # Existing Workforce section still renders
        assert 'id="workforce"' in html

    def test_coaching_prep_renders_without_v1_11_inputs(
        self, monkeypatch, build_report_coaching,
    ):
        from genesys_mcp.tenant import load_config
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(_WEIRD_DIR / self.SHAPE / "tenant.yaml"))
        cfg = load_config()
        pack = {
            "agent": {"name": "Test", "user_id": "u1", "title": "Specialist"},
            "performance": {"voice": {"answered": 0, "aht_s": 0, "vs_target_pct": 0}},
            "wrap_discipline": {},
            "sentiment": {},
            "quality": {},
            "flagged_calls": {"calls": []},
            "recommended_focus": [],
        }
        # No agent_nps or disposition_mix kwargs — both default None.
        html = build_report_coaching.render_html(pack, period="June 2026", cfg=cfg)
        # New v1.11 sections silently absent
        assert 'id="cx-nps"' not in html
        assert 'id="disposition-mix"' not in html
        # Existing TOC anchors still present
        assert "#performance" in html and "#wrap" in html
