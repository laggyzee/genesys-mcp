"""Tests for the v1.13.0 ``cc-workforce-history`` skill build script.

Exercises the pure-Python renderers in
``skills/cc-workforce-history/build_report.py`` against synthetic
``user_activity_history`` payloads. SDK-shaped tool behaviour is
covered in ``tests/test_workforce_history.py``; this file is the
skill-side HTML render contract.
"""
from __future__ import annotations

import pytest
from bs4 import BeautifulSoup


# ── Section renderers ──

class TestHeadcountSection:
    def test_empty_input_renders_empty(self, build_report_workforce_history):
        assert build_report_workforce_history.render_headcount_section([]) == ""

    def test_active_count_joiners_leavers_rendered(self, build_report_workforce_history):
        headcount = [
            {"bucket": "2023-Q3", "active_agents": 10, "joiners": 10, "leavers": 0},
            {"bucket": "2023-Q4", "active_agents": 12, "joiners": 3, "leavers": 1},
            {"bucket": "2024-Q1", "active_agents": 14, "joiners": 4, "leavers": 2},
        ]
        html = build_report_workforce_history.render_headcount_section(headcount)
        soup = BeautifulSoup(html, "html.parser")
        assert soup.find("section", id="headcount") is not None
        ths = soup.find("thead").find_all("th")
        assert len(ths) == 5
        for q in ("2023-Q3", "2023-Q4", "2024-Q1"):
            assert q in html
        assert "+10" in html and "+3" in html
        assert "−1" in html and "−2" in html


class TestTenureSection:
    def test_empty_input_renders_empty(self, build_report_workforce_history):
        assert build_report_workforce_history.render_tenure_section([]) == ""

    def test_mean_median_n_rendered(self, build_report_workforce_history):
        tenure = [
            {"bucket": "2024-Q1", "mean_tenure_months": 8.2, "median_tenure_months": 7.0, "n": 12},
            {"bucket": "2024-Q2", "mean_tenure_months": 11.1, "median_tenure_months": 10.5, "n": 14},
        ]
        html = build_report_workforce_history.render_tenure_section(tenure)
        soup = BeautifulSoup(html, "html.parser")
        assert soup.find("section", id="tenure") is not None
        assert len(soup.find("thead").find_all("th")) == 4
        assert "8.2" in html and "11.1" in html
        assert "12" in html and "14" in html


class TestPerUserSection:
    def test_empty_input_renders_empty(self, build_report_workforce_history):
        assert build_report_workforce_history.render_per_user_section([]) == ""

    def test_joiner_and_leaver_flags_render_pills(self, build_report_workforce_history):
        users = [
            {"user_id": "u1", "name": "Alice", "state": "active",
             "first_active_date": "2024-04-01", "last_active_date": "2026-06-01",
             "total_handled": 1500, "is_joiner_in_window": True,
             "is_leaver_in_window": False},
            {"user_id": "u2", "name": "Bob", "state": "deleted",
             "first_active_date": "2023-08-01", "last_active_date": "2024-12-01",
             "total_handled": 800, "is_joiner_in_window": True,
             "is_leaver_in_window": True},
        ]
        html = build_report_workforce_history.render_per_user_section(users)
        assert html.count('class="pill joiner">joiner') == 2
        assert html.count('class="pill leaver">leaver') == 1
        assert 'class="pill active"' in html
        assert 'class="pill deleted"' in html
        assert "Alice" in html and "Bob" in html


class TestDataCoverageCallout:
    def test_data_starts_after_window_start_renders_warn_callout(
        self, build_report_workforce_history,
    ):
        data = {"data_starts_at": "2025-06"}
        interval = "2023-07-01T00:00:00.000Z/2026-07-01T00:00:00.000Z"
        html = build_report_workforce_history.render_data_coverage_callout(data, interval)
        assert "Data starts at 2025-06" in html
        assert "callout warn" in html
        assert "past Genesys analytics retention" in html

    def test_data_starts_at_window_start_renders_nothing(
        self, build_report_workforce_history,
    ):
        data = {"data_starts_at": "2023-07"}
        interval = "2023-07-01T00:00:00.000Z/2026-07-01T00:00:00.000Z"
        html = build_report_workforce_history.render_data_coverage_callout(data, interval)
        assert html == ""

    def test_no_data_starts_at_renders_bad_callout(self, build_report_workforce_history):
        data = {"data_starts_at": None}
        interval = "2023-07-01T00:00:00.000Z/2026-07-01T00:00:00.000Z"
        html = build_report_workforce_history.render_data_coverage_callout(data, interval)
        assert "No activity data found" in html
        assert "callout bad" in html


# ── End-to-end render ──

class TestRenderHtmlEndToEnd:
    def _build_cfg(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(
            """tenant:
  name: "Test Tenant"
  short_name: "test"
  timezone: "Australia/Sydney"
brands:
  names: ["BrandA"]
specialist_roles: ["Specialist"]
operating_model:
  has_pre_break_presence: false
  has_brand_structure: false
"""
        )
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        from genesys_mcp.tenant import load_config
        return load_config()

    def test_success_path_renders_all_three_sections(
        self, build_report_workforce_history, tmp_path, monkeypatch,
    ):
        cfg = self._build_cfg(tmp_path, monkeypatch)
        data = {
            "interval": "2023-07-01T00:00:00.000Z/2026-07-01T00:00:00.000Z",
            "as_of_utc": "2026-06-24T00:00:00.000Z",
            "tz": "Australia/Sydney",
            "bucket": "quarter",
            "user_count": 2,
            "data_starts_at": "2024-04",
            "headcount_by_bucket": [
                {"bucket": "2024-Q2", "active_agents": 2, "joiners": 2, "leavers": 0},
            ],
            "tenure_trend": [
                {"bucket": "2024-Q2", "mean_tenure_months": 0.0,
                 "median_tenure_months": 0.0, "n": 2},
            ],
            "per_user": [
                {"user_id": "u1", "name": "Alice", "state": "active",
                 "first_active_date": "2024-04-01", "last_active_date": "2026-06-01",
                 "total_handled": 1000, "is_joiner_in_window": True,
                 "is_leaver_in_window": False},
            ],
        }
        html = build_report_workforce_history.render_html(data, "Jul 2023 - Jun 2026", cfg)
        for anchor in ('id="headcount"', 'id="tenure"', 'id="per-user"'):
            assert anchor in html, f"missing section anchor {anchor}"
        assert "Test Tenant" in html
        assert "Data starts at 2024-04" in html
        assert "Alice" in html

    def test_soft_fail_envelope_renders_callout_only_page(
        self, build_report_workforce_history, tmp_path, monkeypatch,
    ):
        cfg = self._build_cfg(tmp_path, monkeypatch)
        envelope = {
            "status": 403,
            "kind": "user_activity_history",
            "message": "Grant analytics:conversationAggregate:view.",
        }
        html = build_report_workforce_history.render_soft_fail_page(
            envelope, cfg, "Jul 2023 - Jun 2026",
        )
        assert "Workforce data not retrieved" in html
        assert "403" in html
        assert "analytics:conversationAggregate:view" in html
        for anchor in ('id="headcount"', 'id="tenure"', 'id="per-user"'):
            assert anchor not in html
