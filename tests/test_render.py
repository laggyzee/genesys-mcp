"""Structural assertions on the HTML output of the render helpers.

Tests parse the generated HTML with BeautifulSoup and assert that specific
cells / sections / classes are present — not byte-identical snapshots
(too brittle on CSS tweaks).

The signals each test pins:

- Specific column counts on each table (refactors that drop/add columns
  will fail until the test is updated alongside)
- Colour classes (``vs-target good/warn/bad``, pill colours) match the
  threshold semantics so a refactor changing thresholds gets caught
- Critical fields ("Total handle hours", "AHT", "Service level") appear
  on the rendered tables
- The narrative-synthesis section (when provided) renders with the right
  class + heading
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


# ── render_workforce_table ──

class TestRenderWorkforceTable:
    def _sample_row(self) -> dict:
        return {
            "name": "Anthony Kha",
            "role": "Customer Service Specialist",
            "answered": 200,
            "voice_ans": 55,
            "msg_ans": 145,
            "voice_aht_s": 330.0,
            "msg_aht_s": 780.0,
            "voice_aht_vs_target_pct": 15.8,
            "msg_aht_vs_target_pct": 18.3,
            "avg_acw_s": 25.0,
            "acw_vs_target_pct": 66.7,
            "total_handle_h": 45.0,
            "overruns": 2,
            "overrun_min": 15,
            "break_sessions": 8,           # required by render — indicates session-count is known
            "away_count": 3,
            "away_min": 12,
            "pre_break_overrun_count": 5,
            "pre_break_overrun_min": 50,
            "voice_excess_min": 41,
            "msg_excess_min": 372,
            "total_excess_min": 413,
        }

    def test_renders_a_table(self, build_report_monthly):
        html = build_report_monthly.render_workforce_table([self._sample_row()])
        soup = BeautifulSoup(html, "html.parser")
        assert soup.find("table"), "render_workforce_table should produce a <table>"
        assert soup.find("thead"), "should have a header row"
        assert soup.find("tbody"), "should have a body"

    def test_column_count_matches_v0_2_1_refactor(self, build_report_monthly):
        # The v0.2.1 refactor collapsed 17 columns → 12. Pin that count so a
        # future "let me just add one more column" change is caught and gets
        # design review before it ships.
        html = build_report_monthly.render_workforce_table([self._sample_row()])
        soup = BeautifulSoup(html, "html.parser")
        headers = soup.find("thead").find_all("th")
        assert len(headers) == 12, (
            f"workforce table should have 12 columns (v0.2.1 refactor); got {len(headers)}"
        )

    def test_aht_vs_target_pill_colour_warn(self, build_report_monthly):
        # voice_aht_vs_target_pct = +15.8% → 'warn' band (0 < x <= 20).
        # Pinning the threshold ensures changes to the bands are deliberate.
        html = build_report_monthly.render_workforce_table([self._sample_row()])
        assert "vs-target warn" in html, "+16% should produce a 'warn' pill"
        assert "+16%" in html

    def test_aht_vs_target_pill_colour_bad_above_20pct(self, build_report_monthly):
        row = self._sample_row()
        row["voice_aht_vs_target_pct"] = 45.0  # well over 20%
        html = build_report_monthly.render_workforce_table([row])
        assert "vs-target bad" in html, "+45% should produce a 'bad' pill"

    def test_aht_vs_target_pill_colour_good_under_target(self, build_report_monthly):
        row = self._sample_row()
        row["voice_aht_vs_target_pct"] = -10.0  # under target
        html = build_report_monthly.render_workforce_table([row])
        assert "vs-target good" in html, "-10% should produce a 'good' pill"

    def test_agent_name_appears_verbatim(self, build_report_monthly):
        html = build_report_monthly.render_workforce_table([self._sample_row()])
        assert "Anthony Kha" in html


# ── render_brand_table ──

class TestRenderBrandTable:
    def test_renders_voice_and_message_separately(self, build_report_monthly):
        rows = [
            {"brand": "BrandA", "media": "voice", "offered": 1000, "answered": 900,
             "abandoned": 100, "ans_pct": 90.0, "sl_pct": 82.0,
             "avg_wait_s": 12.0, "avg_handle_s": 300.0},
            {"brand": "BrandA", "media": "message", "offered": 500, "answered": 450,
             "abandoned": 0, "ans_pct": 90.0, "sl_pct": None,
             "avg_wait_s": 30.0, "avg_handle_s": 700.0},
        ]
        html = build_report_monthly.render_brand_table(rows)
        soup = BeautifulSoup(html, "html.parser")
        body_rows = soup.find("tbody").find_all("tr")
        # Two input rows → two output rows
        assert len(body_rows) == 2

    def test_offered_displayed_with_thousands_separator(self, build_report_monthly):
        rows = [{
            "brand": "BrandA", "media": "voice", "offered": 12345, "answered": 11000,
            "abandoned": 1345, "ans_pct": 89.1, "sl_pct": 80.0,
            "avg_wait_s": 12.0, "avg_handle_s": 300.0,
        }]
        html = build_report_monthly.render_brand_table(rows)
        assert "12,345" in html


# ── render_unresolved_table ──

class TestRenderUnresolvedTable:
    def test_unresolved_share_renders_as_percentage(self, build_report_monthly):
        rows = [
            {"ani": "+61 4 9999 9999", "answered_with_outcome": 5,
             "unresolved_share": 0.8, "ai_outcomes": {"Unresolved": 4, "Resolved": 1}},
        ]
        html = build_report_monthly.render_unresolved_table(rows)
        assert "80%" in html

    def test_full_unresolved_gets_bad_pill(self, build_report_monthly):
        rows = [{
            "ani": "+61 4 9999 9999", "answered_with_outcome": 3,
            "unresolved_share": 1.0, "ai_outcomes": {"Unresolved": 3},
        }]
        html = build_report_monthly.render_unresolved_table(rows)
        assert "pill bad" in html

    def test_partial_unresolved_gets_warn_pill(self, build_report_monthly):
        rows = [{
            "ani": "+61 4 9999 9999", "answered_with_outcome": 5,
             "unresolved_share": 0.6, "ai_outcomes": {"Unresolved": 3, "Resolved": 2},
        }]
        html = build_report_monthly.render_unresolved_table(rows)
        assert "pill warn" in html
        assert "pill bad" not in html  # not fully unresolved → not bad


# ── render_daily_sl_chart ──

class TestRenderDailySLChart:
    def test_chart_renders_bars_for_each_day(self, build_report_monthly):
        daily = [
            {"date": "2026-05-18", "sl_pct": 82.0, "answered": 820, "offered": 1000},
            {"date": "2026-05-19", "sl_pct": 75.0, "answered": 712, "offered": 950},
            {"date": "2026-05-20", "sl_pct": 65.0, "answered": 585, "offered": 900},
        ]
        html = build_report_monthly.render_daily_sl_chart(daily)
        soup = BeautifulSoup(html, "html.parser")
        bars = soup.find_all(class_="bar")
        # The v0.5 chart fix ensured each day renders a bar element.
        assert len(bars) == 3, (
            f"daily SL chart should render one bar per day; got {len(bars)} for 3 days"
        )

    def test_target_line_present(self, build_report_monthly):
        daily = [{"date": "2026-05-18", "sl_pct": 82.0, "answered": 820, "offered": 1000}]
        html = build_report_monthly.render_daily_sl_chart(daily)
        soup = BeautifulSoup(html, "html.parser")
        # The 80% target reference line (the v0.5 chart polish).
        assert soup.find(class_="target-line"), "expected a target-line element on the chart"

    def test_target_line_labelled_80pct(self, build_report_monthly):
        daily = [{"date": "2026-05-18", "sl_pct": 82.0, "answered": 820, "offered": 1000}]
        html = build_report_monthly.render_daily_sl_chart(daily)
        # The "80% target" label on the line — sentinel for the threshold
        # being unchanged. Update only when target shifts deliberately.
        assert "80%" in html


# ── Narrative synthesis (v0.7) ──

class TestNarrativeSynthesis:
    def test_no_narrative_means_no_narrative_section(self, build_report_monthly):
        # When --with-narrative isn't passed, render_narrative_block returns
        # ("", "") so the report layout is identical to the v0.6 era.
        toc_html, sections_html = build_report_monthly.render_narrative_block(None)
        assert toc_html == ""
        assert sections_html == ""

    def test_narrative_renders_each_provided_section(self, build_report_monthly):
        narrative = {
            "coverage": "<p>April 2026 data, **bold** + plain.</p>",
            "what-worked": "<p>SL recovered to 82%.</p>",
            "what-wrong": "<p>Pre-break overruns ballooned.</p>",
            "recommended": "<p>Audit top 5 over-target agents.</p>",
        }
        toc_html, sections_html = build_report_monthly.render_narrative_block(narrative)
        soup = BeautifulSoup(sections_html, "html.parser")
        # All four narrative sections should render
        sections = soup.find_all("section", class_="narrative")
        assert len(sections) == 4, (
            f"expected 4 narrative sections; got {len(sections)}"
        )
        # Each section has an h2 with the right rank + title
        h2s = [s.find("h2").get_text() for s in sections]
        assert any("Coverage" in h for h in h2s)
        assert any("What worked" in h for h in h2s)
        assert any("What went wrong" in h for h in h2s)
        assert any("Recommended" in h for h in h2s)

    def test_narrative_toc_has_link_per_section(self, build_report_monthly):
        narrative = {
            "coverage": "<p>x</p>", "what-worked": "<p>y</p>",
            "what-wrong": "<p>z</p>", "recommended": "<p>w</p>",
        }
        toc_html, _ = build_report_monthly.render_narrative_block(narrative)
        # Four anchor tags, one per slug.
        assert toc_html.count('<a href="#coverage">') == 1
        assert toc_html.count('<a href="#what-worked">') == 1
        assert toc_html.count('<a href="#what-wrong">') == 1
        assert toc_html.count('<a href="#recommended">') == 1


class TestParseNarrativeMd:
    """parse_narrative_md handles markdown subset correctly."""

    def test_parses_four_section_md(self, build_report_monthly, tmp_path: Path):
        md_path = tmp_path / "narrative.md"
        md_path.write_text(
            "## Coverage & caveats\n\nApril data.\n\n"
            "## What worked\n\nSL up.\n\n"
            "## What went wrong\n\nAHT up.\n\n"
            "## Recommended actions\n\n- Audit AHT\n- Coach top 5\n"
        )
        sections = build_report_monthly.parse_narrative_md(md_path)
        assert set(sections.keys()) == {
            "coverage", "what-worked", "what-wrong", "recommended",
        }

    def test_partial_md_only_returns_provided_sections(
        self, build_report_monthly, tmp_path: Path,
    ):
        # Two sections provided, two omitted. Should NOT fail — partial is ok.
        md_path = tmp_path / "narrative.md"
        md_path.write_text(
            "## Coverage & caveats\n\nData.\n\n"
            "## Recommended actions\n\n- Action 1\n"
        )
        sections = build_report_monthly.parse_narrative_md(md_path)
        assert sections.keys() == {"coverage", "recommended"}

    def test_bullets_rendered_as_ul(self, build_report_monthly, tmp_path: Path):
        md_path = tmp_path / "narrative.md"
        md_path.write_text(
            "## What worked\n\n- First win\n- Second win\n- Third win\n"
        )
        sections = build_report_monthly.parse_narrative_md(md_path)
        html = sections["what-worked"]
        assert "<ul>" in html
        assert html.count("<li>") == 3

    def test_bold_and_italic_inline(self, build_report_monthly, tmp_path: Path):
        md_path = tmp_path / "narrative.md"
        md_path.write_text(
            "## Coverage & caveats\n\nThis is **bold** and *italic* text.\n"
        )
        sections = build_report_monthly.parse_narrative_md(md_path)
        html = sections["coverage"]
        assert "<strong>bold</strong>" in html
        assert "<em>italic</em>" in html

    def test_html_in_input_is_escaped(self, build_report_monthly, tmp_path: Path):
        # If the LLM emits something that looks like HTML in its markdown,
        # we escape it before rendering — security + correctness.
        md_path = tmp_path / "narrative.md"
        md_path.write_text(
            "## Coverage & caveats\n\nA <script>alert('xss')</script> attempt.\n"
        )
        sections = build_report_monthly.parse_narrative_md(md_path)
        html = sections["coverage"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
