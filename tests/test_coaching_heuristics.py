"""Pins ``_recommend_focus`` against the new ``cfg.coaching.heuristics`` block.

Pre-v1.0 these cutoffs were hardcoded in coaching.py:
    - hold ratio 0.15
    - peer AHT multiplier 1.15
    - QA pass mark 80
    - wrap-up note rate 0.7
    - voice/message excess-hours 2.0

v1.0 moves them to tenant.yaml so message-only or sales-heavy tenants can
tune without forking the code. These tests pin the new behaviour and prove
the heuristics actually drive the focus selection — i.e. raising the QA
pass mark should make a 75%-QA agent no longer flagged.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


@pytest.fixture(scope="module")
def coaching_mod() -> ModuleType:
    """Direct import of coaching.py for unit-testing helpers (not the FastMCP tool)."""
    from genesys_mcp.tools import coaching
    return coaching


def _target_kpis(**overrides) -> dict:
    base = {
        "voice_aht_s": 330.0, "message_aht_s": 950.0, "voice_acw_avg_s": 78.0,
        "voice_hold_ratio": 0.04, "total_handle_hours": 309.1,
        "voice_aht_vs_target_pct": 16.0, "message_aht_vs_target_pct": 44.0,
        "voice_excess_handle_hours": 5.0, "message_excess_handle_hours": 83.5,
    }
    base.update(overrides)
    return base


def _wrap_stats(**overrides) -> dict:
    base = {
        "wrapup_note_rate": 0.91, "with_own_notes": 1268, "with_wrapup_code": 1390,
    }
    base.update(overrides)
    return base


_TARGETS = {"voice_aht_s": 285, "msg_aht_s": 660, "acw_s": 15, "fte_hours_per_month": 160}


class TestQaPassMarkConfigurable:
    def test_default_80_flags_agent_at_75(self, coaching_mod):
        """Agent at 75% QA flagged against default pass mark of 80."""
        focus = coaching_mod._recommend_focus(
            target_kpis=_target_kpis(),
            peer_medians={},
            wrap_stats=_wrap_stats(),
            qa_summary={"avg_score": 75, "n_evaluations": 4},
            targets=_TARGETS,
        )
        areas = {f["area"] for f in focus}
        assert "QA score" in areas

    def test_raised_pass_mark_90_flags_agent_at_85(self, coaching_mod):
        """Tenant with 90% pass mark sees 85%-QA agent flagged."""
        focus = coaching_mod._recommend_focus(
            target_kpis=_target_kpis(),
            peer_medians={},
            wrap_stats=_wrap_stats(),
            qa_summary={"avg_score": 85, "n_evaluations": 4},
            targets=_TARGETS,
            heuristics={"qa_pass_mark": 90},
        )
        headlines = " ".join(f["headline"] for f in focus)
        assert "below the 90% pass mark" in headlines

    def test_lowered_pass_mark_70_excuses_agent_at_75(self, coaching_mod):
        """Tenant with 70% pass mark no longer flags 75%-QA agent."""
        focus = coaching_mod._recommend_focus(
            target_kpis=_target_kpis(),
            peer_medians={},
            wrap_stats=_wrap_stats(),
            qa_summary={"avg_score": 75, "n_evaluations": 4},
            targets=_TARGETS,
            heuristics={"qa_pass_mark": 70},
        )
        areas = {f["area"] for f in focus}
        assert "QA score" not in areas


class TestHoldRatioConfigurable:
    def test_default_015_flags_agent_at_020(self, coaching_mod):
        focus = coaching_mod._recommend_focus(
            target_kpis=_target_kpis(voice_hold_ratio=0.20),
            peer_medians={}, wrap_stats=_wrap_stats(), qa_summary=None,
            targets=_TARGETS,
        )
        areas = {f["area"] for f in focus}
        assert "Hold time" in areas

    def test_retention_team_threshold_030_excuses_agent_at_020(self, coaching_mod):
        """A transfer-heavy retention team probably wants 30% hold threshold."""
        focus = coaching_mod._recommend_focus(
            target_kpis=_target_kpis(voice_hold_ratio=0.20),
            peer_medians={}, wrap_stats=_wrap_stats(), qa_summary=None,
            targets=_TARGETS,
            heuristics={"hold_ratio_threshold": 0.30},
        )
        areas = {f["area"] for f in focus}
        assert "Hold time" not in areas


class TestPeerAhtMultiplier:
    def test_default_115_flags_agent_at_120_of_peer(self, coaching_mod):
        # peer 285s, agent 350s → 22.8% over peer
        focus = coaching_mod._recommend_focus(
            target_kpis=_target_kpis(voice_aht_s=350),
            peer_medians={"voice_aht_s": 285},
            wrap_stats=_wrap_stats(), qa_summary=None, targets=_TARGETS,
        )
        areas = {f["area"] for f in focus}
        assert "vs Peers — voice handle" in areas

    def test_loose_125_multiplier_excuses_agent_at_120_of_peer(self, coaching_mod):
        focus = coaching_mod._recommend_focus(
            target_kpis=_target_kpis(voice_aht_s=350),
            peer_medians={"voice_aht_s": 285},
            wrap_stats=_wrap_stats(), qa_summary=None, targets=_TARGETS,
            heuristics={"peer_aht_multiplier": 1.25},
        )
        areas = {f["area"] for f in focus}
        assert "vs Peers — voice handle" not in areas


class TestWrapUpNoteRateConfigurable:
    def test_default_07_flags_agent_at_60_pct(self, coaching_mod):
        focus = coaching_mod._recommend_focus(
            target_kpis=_target_kpis(),
            peer_medians={}, wrap_stats=_wrap_stats(wrapup_note_rate=0.60),
            qa_summary=None, targets=_TARGETS,
        )
        areas = {f["area"] for f in focus}
        assert "Wrap-up discipline" in areas

    def test_strict_threshold_095_flags_agent_at_91_pct(self, coaching_mod):
        focus = coaching_mod._recommend_focus(
            target_kpis=_target_kpis(),
            peer_medians={}, wrap_stats=_wrap_stats(wrapup_note_rate=0.91),
            qa_summary=None, targets=_TARGETS,
            heuristics={"wrap_up_note_rate_threshold": 0.95},
        )
        areas = {f["area"] for f in focus}
        assert "Wrap-up discipline" in areas


class TestExcessHoursThresholdsConfigurable:
    def test_default_2h_flags_agent_with_5h_voice_excess(self, coaching_mod):
        focus = coaching_mod._recommend_focus(
            target_kpis=_target_kpis(voice_excess_handle_hours=5.0,
                                     message_excess_handle_hours=0.0),
            peer_medians={}, wrap_stats=_wrap_stats(), qa_summary=None,
            targets=_TARGETS,
        )
        areas = {f["area"] for f in focus}
        assert "Voice AHT" in areas

    def test_high_threshold_10h_excuses_agent_with_5h_excess(self, coaching_mod):
        focus = coaching_mod._recommend_focus(
            target_kpis=_target_kpis(voice_excess_handle_hours=5.0,
                                     message_excess_handle_hours=0.0),
            peer_medians={}, wrap_stats=_wrap_stats(), qa_summary=None,
            targets=_TARGETS,
            heuristics={"voice_excess_hours_threshold": 10.0},
        )
        areas = {f["area"] for f in focus}
        assert "Voice AHT" not in areas


class TestTenantConfigSchema:
    """Pins the new ``cfg.coaching.heuristics`` block on TenantConfig."""

    def test_heuristics_block_exposes_all_seven_knobs(self, temp_tenant_config):
        from genesys_mcp.tenant import load_config
        cfg = load_config()
        h = cfg.coaching.heuristics
        # All seven knobs must exist with sensible defaults
        assert h.hold_ratio_threshold == 0.15
        assert h.peer_aht_multiplier == 1.15
        assert h.negative_sentiment_call_threshold == -0.4
        assert h.hold_ratio_call_threshold == 0.3
        assert h.wrap_up_note_rate_threshold == 0.7
        assert h.qa_pass_mark == 80
        assert h.voice_excess_hours_threshold == 2.0


class TestResolveTargetsRequiresTenantConfig:
    """v1.0 hard-fail contract: no tenant.yaml = no Prvidr-shaped fallbacks."""

    def test_missing_cfg_raises_with_remediation_message(self, coaching_mod):
        from genesys_mcp.tenant import TenantConfigError
        with pytest.raises(TenantConfigError, match="genesys-tenant-setup"):
            coaching_mod._resolve_targets(None)


class TestSpecialistRolesRequired:
    """Pre-v1.0 had ``['Specialist', 'Customer Service Specialist']`` baked in."""

    def test_missing_specialist_roles_fails_validation(self, tmp_path: Path,
                                                       monkeypatch):
        from genesys_mcp.tenant import TenantConfigError, load_config
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(
            """tenant:
  name: "Test Tenant"
  short_name: "test"
brands:
  names: ["BrandA"]
"""
        )
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        with pytest.raises(TenantConfigError, match="specialist_roles"):
            load_config()
