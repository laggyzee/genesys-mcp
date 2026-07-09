"""Regression tests for the cc-coaching-prep build script.

Pre-v0.10 this file had **zero** tests despite the build script being
~600 LoC and consuming a complex tool output (``agent_coaching_pack``).
Each test pins one behaviour the brief depends on:

- Formatter functions for the headline KPI cards
- Class boundaries for the colour-coded vs-target / vs-peers pills
- Soft-degrade paths when sub-sections of the pack are missing or empty
- Narrative markdown → HTML parsing
- End-to-end render against a synthetic pack
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def mock_tenant_config(temp_tenant_config):
    """Return a loaded TenantConfig backed by the temp_tenant_config file."""
    from genesys_mcp.tenant import load_config
    return load_config()


# ── Formatters ──

class TestFormatters:
    def test_fmt_int_handles_none_and_thousands(self, build_report_coaching):
        assert build_report_coaching.fmt_int(None) == "—"
        assert build_report_coaching.fmt_int(0) == "0"
        assert build_report_coaching.fmt_int(1445) == "1,445"

    def test_fmt_secs_formats_minutes_and_seconds(self, build_report_coaching):
        assert build_report_coaching.fmt_secs(None) == "—"
        assert build_report_coaching.fmt_secs(45) == "45s"
        assert build_report_coaching.fmt_secs(330) == "5m 30s"
        assert build_report_coaching.fmt_secs(950) == "15m 50s"

    def test_fmt_pct_default_zero_digits(self, build_report_coaching):
        assert build_report_coaching.fmt_pct(None) == "—"
        assert build_report_coaching.fmt_pct(83.5) == "84%"
        assert build_report_coaching.fmt_pct(83.5, digits=1) == "83.5%"


# ── _aht_class / _acw_class boundaries ──

class TestAhtClass:
    def test_under_target_is_good(self, build_report_coaching):
        assert build_report_coaching._aht_class(0) == "good"
        assert build_report_coaching._aht_class(-5) == "good"

    def test_zero_to_twenty_pct_is_warn(self, build_report_coaching):
        assert build_report_coaching._aht_class(15) == "warn"
        assert build_report_coaching._aht_class(20) == "warn"

    def test_over_twenty_pct_is_bad(self, build_report_coaching):
        assert build_report_coaching._aht_class(21) == "bad"
        assert build_report_coaching._aht_class(330) == "bad"  # Person 007 case

    def test_none_returns_empty_class(self, build_report_coaching):
        assert build_report_coaching._aht_class(None) == ""


class TestAcwClass:
    def test_at_or_below_target_is_good(self, build_report_coaching):
        assert build_report_coaching._acw_class(15, 15) == "good"
        assert build_report_coaching._acw_class(10, 15) == "good"

    def test_up_to_double_target_is_warn(self, build_report_coaching):
        assert build_report_coaching._acw_class(25, 15) == "warn"
        assert build_report_coaching._acw_class(30, 15) == "warn"

    def test_over_double_target_is_bad(self, build_report_coaching):
        assert build_report_coaching._acw_class(78, 15) == "bad"

    def test_none_returns_empty_class(self, build_report_coaching):
        assert build_report_coaching._acw_class(None, 15) == ""
        assert build_report_coaching._acw_class(15, None) == ""


# ── _peer_delta pill colours ──

class TestPeerDelta:
    def test_lower_is_better_underperforming_target_returns_bad(self, build_report_coaching):
        # target 1000s, peer median 500s → +100% over peers, bad
        out = build_report_coaching._peer_delta(1000, 500, lower_is_better=True)
        assert "bad" in out and "+100%" in out

    def test_lower_is_better_outperforming_target_returns_good(self, build_report_coaching):
        # target 200s, peer median 300s → -33% vs peers, good
        out = build_report_coaching._peer_delta(200, 300, lower_is_better=True)
        assert "good" in out and "-33%" in out

    def test_higher_is_better_more_handle_is_good(self, build_report_coaching):
        # target 309h, peer median 100h → +209% (more is better), good
        out = build_report_coaching._peer_delta(309, 100, lower_is_better=False)
        assert "good" in out and "+209%" in out

    def test_returns_em_dash_when_values_missing(self, build_report_coaching):
        assert "—" in build_report_coaching._peer_delta(None, 100)
        assert "—" in build_report_coaching._peer_delta(100, None)


# ── render_performance_section soft-degrade ──

def _minimal_pack(**overrides) -> dict:
    """A coaching_pack with just enough structure to render."""
    pack = {
        "agent": {"id": "u1", "name": "Test Agent", "title": "Customer Service Specialist"},
        "interval": "2026-05-01/2026-05-24",
        "targets": {"voice_aht_s": 285, "msg_aht_s": 660, "acw_s": 15},
        "performance": {
            "target": {
                "voice_answered": 395, "message_answered": 1033, "callback_answered": 17,
                "voice_aht_s": 330, "message_aht_s": 950, "voice_acw_avg_s": 78,
                "voice_hold_ratio": 0.04, "total_handle_hours": 309.1,
                "voice_aht_vs_target_pct": 16, "message_aht_vs_target_pct": 44,
                "voice_excess_handle_hours": 5.0, "message_excess_handle_hours": 83.5,
            },
            "peer_count": 20,
            "peer_medians": {
                "voice_aht_s": 584, "message_aht_s": 839,
                "voice_acw_avg_s": 89, "voice_hold_ratio": 0.10,
                "total_handle_hours": 117.6,
            },
            "per_peer": {},
            "peer_resolution": {"strategy": "role", "reason": None},
        },
        "wrap_discipline": {
            "total_conversations": 1392, "with_wrapup_code": 1390,
            "with_own_notes": 1268, "note_rate": 0.91, "top_dispositions": [],
        },
        "queues_handled": [],
        "sentiment": {"avg": 0.16, "samples": 404},
        "quality": {"scope_available": True, "summary": None, "evaluations": []},
        "adherence": {"status": "available", "user": {"session_count": 0, "explained_overruns": 0, "unexplained_overruns": 0, "sessions": []}},
        "flagged_calls": {"limit": 10, "total_flagged": 0, "top": []},
        "recommended_focus": [],
    }
    pack.update(overrides)
    return pack


class TestRenderPerformanceSection:
    def test_renders_all_kpis(self, build_report_coaching, mock_tenant_config):
        html = build_report_coaching.render_performance_section(
            _minimal_pack(), mock_tenant_config,
        )
        assert "Voice answered" in html and "395" in html
        assert "Message answered" in html and "1,033" in html
        assert "Voice AHT" in html and "330s" in html
        assert "Message AHT" in html
        assert "Voice ACW" in html
        assert "309.1h" in html
        assert "—s" not in html

    def test_peer_table_appears_when_peers_present(
        self, build_report_coaching, mock_tenant_config,
    ):
        html = build_report_coaching.render_performance_section(
            _minimal_pack(), mock_tenant_config,
        )
        assert "Peer comparison" in html
        assert "n=20" in html

    def test_peer_table_omitted_when_no_peers(
        self, build_report_coaching, mock_tenant_config,
    ):
        pack = _minimal_pack()
        pack["performance"]["peer_medians"] = {}
        pack["performance"]["peer_count"] = 0
        html = build_report_coaching.render_performance_section(pack, mock_tenant_config)
        assert "Peer comparison" not in html
        # The KPI cards still render — only the table is omitted
        assert "Voice answered" in html

    def test_no_peers_renders_explicit_reason(
        self, build_report_coaching, mock_tenant_config,
    ):
        pack = _minimal_pack()
        pack["performance"]["peer_medians"] = {}
        pack["performance"]["peer_count"] = 0
        pack["performance"]["peer_resolution"] = {
            "strategy": "role",
            "reason": "No other active users matched the role.",
        }
        html = build_report_coaching.render_performance_section(pack, mock_tenant_config)
        assert "Peer comparison unavailable" in html
        assert "No other active users matched" in html


class TestRenderAdherence:
    def test_renders_adherence_summary(self, build_report_coaching):
        html = build_report_coaching.render_adherence_section(_minimal_pack())
        assert "Unexplained overruns" in html
        assert "Break / meal sessions" in html

    def test_unavailable_adherence_has_reason(self, build_report_coaching):
        pack = _minimal_pack(adherence={
            "status": "unavailable",
            "reason": "WFM permission unavailable.",
        })
        html = build_report_coaching.render_adherence_section(pack)
        assert "Break/meal adherence unavailable" in html
        assert "WFM permission unavailable" in html

    def test_exception_times_render_in_tenant_timezone(self, build_report_coaching, mock_tenant_config):
        mock_tenant_config.tenant.timezone = "Australia/Sydney"
        pack = _minimal_pack(adherence={
            "status": "available",
            "user": {
                "session_count": 1,
                "explained_overruns": 0,
                "unexplained_overruns": 1,
                "sessions": [{
                    "presence": "BREAK",
                    "start_utc": "2026-05-21T23:00:00Z",
                    "duration_min": 25,
                    "overrun_min": 10,
                    "over_target": True,
                    "matching_explanation": None,
                }],
            },
        })
        html = build_report_coaching.render_adherence_section(pack, mock_tenant_config)
        assert "22 May 2026, 09:00 AM" in html


class TestFlaggedCalls:
    def test_full_conversation_id_is_always_rendered(self, build_report_coaching, mock_tenant_config):
        pack = _minimal_pack()
        conversation_id = "11111111-2222-3333-4444-555555555555"
        pack["flagged_calls"] = {
            "total_flagged": 1,
            "top": [{
                "conversation_id": conversation_id,
                "started_at": "2026-05-01T00:00:00Z",
                "media": "voice",
                "handle_s": 600,
                "hold_s": 0,
                "sentiment_score": -0.2,
                "flag_reasons": ["AHT over target"],
                "transcript_excerpt": {"status": 404, "message": "not transcribed"},
            }],
        }
        html = build_report_coaching.render_flagged_section(pack, mock_tenant_config)
        assert conversation_id in html
        assert "not transcribed" in html


# ── render_focus_section soft-degrade ──

class TestRenderFocusSection:
    def test_empty_focus_shows_recognition_callout(self, build_report_coaching):
        html = build_report_coaching.render_focus_section(_minimal_pack())
        assert "No focus areas surfaced" in html
        assert "career-development" in html

    def test_focus_cards_render_with_rank_and_area(self, build_report_coaching):
        pack = _minimal_pack(recommended_focus=[
            {"rank": 1, "area": "Message AHT", "headline": "Message AHT 951s vs target 660s"},
            {"rank": 2, "area": "QA score", "headline": "QA avg 0% across 1 eval"},
        ])
        html = build_report_coaching.render_focus_section(pack)
        assert "No focus areas surfaced" not in html
        assert "Message AHT" in html
        assert "QA score" in html
        # rank pills should appear in order (rank 1 card before rank 2 card)
        assert html.index('<span class="rank">1</span>') < html.index('<span class="rank">2</span>')


# ── render_sentiment_quality soft-degrade on quality scope ──

class TestRenderSentimentQuality:
    def test_qa_scope_unavailable_renders_callout(self, build_report_coaching):
        pack = _minimal_pack()
        pack["quality"] = {"scope_available": False, "summary": None, "evaluations": []}
        html = build_report_coaching.render_sentiment_quality(pack)
        # Either an explicit "no QA scope" note or simply no QA card
        assert "QA" in html  # the section header still renders

    def test_no_sentiment_data_renders_em_dash(self, build_report_coaching):
        pack = _minimal_pack()
        pack["sentiment"] = {"avg": None, "samples": 0}
        html = build_report_coaching.render_sentiment_quality(pack)
        assert "no STA data" in html or "—" in html


# ── Narrative parsing ──

class TestParseNarrativeMd:
    def test_parses_three_named_sections(self, build_report_coaching, tmp_path: Path):
        md = tmp_path / "narrative.md"
        md.write_text(
            "## Strengths to acknowledge\n\n"
            "Solid wrap-up note rate at 91%.\n\n"
            "## Areas to coach\n\n"
            "Message AHT is +44% over target.\n\n"
            "## Suggested talking points\n\n"
            "- Acknowledge wrap-up discipline\n"
            "- Discuss message AHT root cause\n"
        )
        sections = build_report_coaching.parse_narrative_md(md)
        assert "strengths" in sections
        assert "areas" in sections
        assert "talking-points" in sections
        assert "91%" in sections["strengths"]
        assert "+44%" in sections["areas"]

    def test_empty_markdown_returns_empty_dict(self, build_report_coaching, tmp_path: Path):
        md = tmp_path / "narrative.md"
        md.write_text("")
        sections = build_report_coaching.parse_narrative_md(md)
        assert sections == {}

    def test_unknown_headings_silently_ignored(
        self, build_report_coaching, tmp_path: Path,
    ):
        md = tmp_path / "narrative.md"
        md.write_text(
            "## Random other heading\n\nSome content.\n\n"
            "## Strengths to acknowledge\n\nGood notes.\n"
        )
        sections = build_report_coaching.parse_narrative_md(md)
        assert "strengths" in sections
        # Unknown headings shouldn't make it into the output
        assert "random" not in sections


# ── End-to-end render_html sanity ──

class TestRenderHtmlEndToEnd:
    def test_full_pack_produces_all_required_sections(
        self, build_report_coaching, mock_tenant_config,
    ):
        pack = _minimal_pack(recommended_focus=[
            {"rank": 1, "area": "Message AHT", "headline": "Message AHT 951s vs target 660s"},
        ])
        html = build_report_coaching.render_html(
            pack, period="1-24 May 2026", cfg=mock_tenant_config,
        )
        for anchor in ("#performance", "#sentiment", "#adherence", "#wrap", "#flagged", "#focus"):
            assert anchor in html, f"missing section anchor {anchor}"
        # Tenant name from mock config bleeds through into the header
        assert mock_tenant_config.tenant.name in html


# ── v1.11: per-agent NPS + disposition mix ──

class TestAgentNpsRollup:
    """Pins the v1.11 per-agent NPS rollup (group org-wide search by agent_user_id)."""

    def test_none_inputs_return_none(self, build_report_coaching):
        assert build_report_coaching.aggregate_agent_nps(None, None) is None
        assert build_report_coaching.aggregate_agent_nps({}, "u1") is None
        assert build_report_coaching.aggregate_agent_nps({"conversations": []}, "u1") is None

    def test_groups_target_agent_and_picks_detractors(self, build_report_coaching):
        raw = {"totals": {"conversation_count": 5}, "conversations": [
            {"conversation_id": "c1", "conversation_start": "2026-06-01T10:00Z", "agent_user_id": "u1", "attribute_value": "3"},
            {"conversation_id": "c2", "conversation_start": "2026-06-02T10:00Z", "agent_user_id": "u1", "attribute_value": "8"},
            {"conversation_id": "c3", "conversation_start": "2026-06-03T10:00Z", "agent_user_id": "u1", "attribute_value": "9"},
            {"conversation_id": "c4", "conversation_start": "2026-06-04T10:00Z", "agent_user_id": "u1", "attribute_value": "10"},
            {"conversation_id": "c5", "conversation_start": "2026-06-05T10:00Z", "agent_user_id": "u2", "attribute_value": "0"},
        ]}
        nps = build_report_coaching.aggregate_agent_nps(raw, "u1")
        # promoters=2, passives=1, detractors=1, total=4 → score = (2-1)/4*100 = 25
        assert nps["total"] == 4
        assert nps["promoters"] == 2 and nps["passives"] == 1 and nps["detractors"] == 1
        assert nps["score"] == 25.0
        assert len(nps["detractor_calls"]) == 1
        assert nps["detractor_calls"][0]["conversation_id"] == "c1"

    def test_target_not_in_conversations_returns_none(self, build_report_coaching):
        raw = {"totals": {"conversation_count": 1}, "conversations": [
            {"conversation_id": "c1", "agent_user_id": "u-other", "attribute_value": "9"},
        ]}
        assert build_report_coaching.aggregate_agent_nps(raw, "u1") is None


class TestAgentNpsSectionRender:
    def test_none_renders_empty(self, build_report_coaching):
        assert build_report_coaching.render_agent_nps_section(None) == ""

    def test_with_detractor_calls_renders_table(self, build_report_coaching):
        nps = {
            "score": 25.0, "total": 4, "promoters": 2, "passives": 1, "detractors": 1,
            "detractor_calls": [
                {"conversation_id": "abc-123", "score": 3, "conversation_start": "2026-06-01T10:00Z"},
            ],
        }
        html = build_report_coaching.render_agent_nps_section(nps)
        assert "section" in html and "cx-nps" in html
        assert "abc-123" in html
        assert "listen back" in html

    def test_no_detractor_calls_renders_clean_callout(self, build_report_coaching):
        nps = {
            "score": 80.0, "total": 5, "promoters": 5, "passives": 0, "detractors": 0,
            "detractor_calls": [],
        }
        html = build_report_coaching.render_agent_nps_section(nps)
        assert "No detractor calls" in html


class TestDispositionMix:
    """Pins the v1.11 per-agent disposition mix vs team."""

    def test_either_input_none_returns_none(self, build_report_coaching):
        team = {"distribution": [{"name": "Resolved", "percentage": 70.0}]}
        assert build_report_coaching.aggregate_disposition_mix(None, team) is None
        assert build_report_coaching.aggregate_disposition_mix(
            {"distribution": [{"name": "X", "percentage": 50.0}]}, None
        ) is None

    def test_flags_codes_with_large_delta(self, build_report_coaching):
        agent = {"distribution": [
            {"name": "Resolved", "count": 60, "percentage": 60.0},
            {"name": "Transfer", "count": 40, "percentage": 40.0},
        ]}
        team = {"distribution": [
            {"name": "Resolved", "count": 600, "percentage": 75.0},
            {"name": "Transfer", "count": 200, "percentage": 25.0},
        ]}
        mix = build_report_coaching.aggregate_disposition_mix(agent, team, flag_pp_delta=10.0)
        # Resolved -15pp, Transfer +15pp → both flagged
        assert len(mix["flagged_codes"]) == 2
        delta_by_name = {r["name"]: r["delta_pp"] for r in mix["rows"]}
        assert delta_by_name["Resolved"] == -15.0
        assert delta_by_name["Transfer"] == 15.0

    def test_does_not_flag_inside_tolerance(self, build_report_coaching):
        agent = {"distribution": [{"name": "Resolved", "count": 100, "percentage": 80.0}]}
        team = {"distribution": [{"name": "Resolved", "count": 1000, "percentage": 75.0}]}
        mix = build_report_coaching.aggregate_disposition_mix(agent, team, flag_pp_delta=10.0)
        # |80 - 75| = 5pp < 10pp → not flagged
        assert len(mix["flagged_codes"]) == 0


class TestDispositionMixRender:
    def test_none_renders_empty(self, build_report_coaching):
        assert build_report_coaching.render_disposition_mix_section(None) == ""

    def test_flagged_codes_callout(self, build_report_coaching):
        mix = {
            "rows": [
                {"name": "Resolved", "count": 60, "agent_pct": 60.0, "team_pct": 75.0, "delta_pp": -15.0, "flagged": True},
                {"name": "Transfer", "count": 40, "agent_pct": 40.0, "team_pct": 25.0, "delta_pp": 15.0, "flagged": True},
            ],
            "flagged_codes": [
                {"name": "Resolved", "count": 60, "agent_pct": 60.0, "team_pct": 75.0, "delta_pp": -15.0, "flagged": True},
                {"name": "Transfer", "count": 40, "agent_pct": 40.0, "team_pct": 25.0, "delta_pp": 15.0, "flagged": True},
            ],
        }
        html = build_report_coaching.render_disposition_mix_section(mix)
        assert "section" in html and "disposition-mix" in html
        assert "2 code(s) flagged" in html
        # Negative delta → 'good' (under-use); positive → 'bad' (over-use)
        assert "vs-target good" in html and "vs-target bad" in html

    def test_no_flagged_codes_no_callout(self, build_report_coaching):
        mix = {
            "rows": [
                {"name": "Resolved", "count": 80, "agent_pct": 80.0, "team_pct": 78.0, "delta_pp": 2.0, "flagged": False},
            ],
            "flagged_codes": [],
        }
        html = build_report_coaching.render_disposition_mix_section(mix)
        assert "Resolved" in html
        assert "code(s) flagged" not in html
