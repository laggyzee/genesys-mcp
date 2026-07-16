"""Pins the v1.0 mcp_health_check upgrades.

Three new checks land in v1.0:

1. Queue-name pattern match rate against a sampled page of /routing/queues.
   <80% → actionable warning naming the remediation.
2. Specialist-role resolution against active users — at least one configured
   role must appear as a title on an active user.
3. Schema-version visibility in the tenant_config block.

Plus a CLI ``--strict`` flag that exits non-zero on warnings (for CI).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from genesys_mcp.tools import health as health_mod


def test_health_report_exposes_installed_mcp_version(monkeypatch):
    monkeypatch.setattr(health_mod, "_SCOPES", ())
    monkeypatch.setattr(health_mod, "_check_tenant_config", lambda api=None: {
        "exists": True,
        "loaded_ok": True,
        "warnings": [],
        "errors": [],
    })
    monkeypatch.setattr(health_mod, "_check_skills_linked", lambda: [])
    monkeypatch.setattr(health_mod, "get_api", lambda: object())

    report = health_mod.run_health_check()

    assert report["mcp_version"] == "1.18.0"


class TestQueuePatternMatchRate:
    def test_warns_when_match_rate_under_80_pct(self, temp_tenant_config):
        from genesys_mcp.tenant import load_config
        cfg = load_config()

        # Mock the api + routing endpoint to return mostly non-matching queues.
        api = MagicMock()
        routing_resp = {
            "entities": [
                {"name": "BrandA - Sales"},        # matches default pattern
                {"name": "weird_queue_one"},       # no match
                {"name": "weird_queue_two"},       # no match
                {"name": "weird_queue_three"},     # no match
                {"name": "weird_queue_four"},      # no match
            ],
        }
        with pytest.MonkeyPatch.context() as m:
            mock_routing_api = MagicMock()
            mock_routing_api.get_routing_queues.return_value = routing_resp
            m.setattr(health_mod.gc, "RoutingApi", lambda _api: mock_routing_api)
            m.setattr(health_mod, "to_dict", lambda x: x)

            out = {"warnings": [], "errors": []}
            health_mod._check_queue_pattern_match_rate(api, cfg, out)

        assert out["queue_pattern_match_rate"] == 0.2
        assert any("matches only 20%" in w for w in out["warnings"])
        # Remediation must name both the config knob and the alternative
        assert any("name_pattern_match_required" in w for w in out["warnings"])

    def test_silent_when_match_rate_at_or_above_80_pct(self, temp_tenant_config):
        from genesys_mcp.tenant import load_config
        cfg = load_config()
        api = MagicMock()
        routing_resp = {
            "entities": [{"name": f"BrandA - Sales {i}"} for i in range(10)],
        }
        with pytest.MonkeyPatch.context() as m:
            mock_routing_api = MagicMock()
            mock_routing_api.get_routing_queues.return_value = routing_resp
            m.setattr(health_mod.gc, "RoutingApi", lambda _api: mock_routing_api)
            m.setattr(health_mod, "to_dict", lambda x: x)

            out = {"warnings": [], "errors": []}
            health_mod._check_queue_pattern_match_rate(api, cfg, out)

        assert out["queue_pattern_match_rate"] == 1.0
        assert out["warnings"] == []


class TestSpecialistRolesResolution:
    def test_warns_when_no_role_matches_any_active_user(
        self, temp_tenant_config,
    ):
        from genesys_mcp.tenant import load_config
        cfg = load_config()
        # temp_tenant_config has specialist_roles=["Specialist"]
        api = MagicMock()
        users_resp = {
            "entities": [
                {"title": "Agent Level 1"},
                {"title": "Senior Rep"},
                {"title": None},
            ],
        }
        with pytest.MonkeyPatch.context() as m:
            mock_users_api = MagicMock()
            mock_users_api.get_users.return_value = users_resp
            m.setattr(health_mod.gc, "UsersApi", lambda _api: mock_users_api)
            m.setattr(health_mod, "to_dict", lambda x: x)

            out = {"warnings": [], "errors": []}
            health_mod._check_specialist_roles_resolve(api, cfg, out)

        assert out["specialist_roles_matched"] == []
        assert any("match no active user titles" in w for w in out["warnings"])
        # Warning lists actual titles to help diagnosis
        assert any("Agent Level 1" in w for w in out["warnings"])

    def test_silent_when_at_least_one_role_resolves(self, temp_tenant_config):
        from genesys_mcp.tenant import load_config
        cfg = load_config()
        api = MagicMock()
        users_resp = {
            "entities": [
                {"title": "Specialist"},  # matches
                {"title": "Agent Level 1"},
            ],
        }
        with pytest.MonkeyPatch.context() as m:
            mock_users_api = MagicMock()
            mock_users_api.get_users.return_value = users_resp
            m.setattr(health_mod.gc, "UsersApi", lambda _api: mock_users_api)
            m.setattr(health_mod, "to_dict", lambda x: x)

            out = {"warnings": [], "errors": []}
            health_mod._check_specialist_roles_resolve(api, cfg, out)

        assert out["specialist_roles_matched"] == ["Specialist"]
        assert out["warnings"] == []


class TestStrictExitCode:
    def test_main_exits_0_on_ready(self, monkeypatch):
        from genesys_mcp import health_check as cli
        monkeypatch.setattr(cli, "_load_env_files", lambda: [])
        monkeypatch.setattr("genesys_mcp.client.init_api", lambda: None)
        monkeypatch.setattr(
            "genesys_mcp.tools.health.run_health_check",
            lambda: {"verdict": "ready", "blockers": [],
                     "oauth": {"region": "ap-southeast-2", "scopes_tested": []},
                     "tenant_config": {"path": "(test)", "exists": False,
                                       "loaded_ok": False, "warnings": [], "errors": []},
                     "skills_linked": []},
        )
        assert cli.main([]) == 0

    def test_main_exits_2_under_strict_on_warnings(self, monkeypatch):
        from genesys_mcp import health_check as cli
        monkeypatch.setattr(cli, "_load_env_files", lambda: [])
        monkeypatch.setattr("genesys_mcp.client.init_api", lambda: None)
        monkeypatch.setattr(
            "genesys_mcp.tools.health.run_health_check",
            lambda: {"verdict": "ready_with_warnings", "blockers": [],
                     "oauth": {"region": "ap-southeast-2", "scopes_tested": []},
                     "tenant_config": {"path": "(test)", "exists": False,
                                       "loaded_ok": False, "warnings": [], "errors": []},
                     "skills_linked": []},
        )
        assert cli.main([]) == 0
        assert cli.main(["--strict"]) == 2

    def test_main_exits_1_on_blocked_regardless_of_strict(self, monkeypatch):
        from genesys_mcp import health_check as cli
        monkeypatch.setattr(cli, "_load_env_files", lambda: [])
        monkeypatch.setattr("genesys_mcp.client.init_api", lambda: None)
        monkeypatch.setattr(
            "genesys_mcp.tools.health.run_health_check",
            lambda: {"verdict": "blocked", "blockers": ["...broken..."],
                     "oauth": {"region": "ap-southeast-2", "scopes_tested": []},
                     "tenant_config": {"path": "(test)", "exists": False,
                                       "loaded_ok": False, "warnings": [], "errors": []},
                     "skills_linked": []},
        )
        assert cli.main([]) == 1
        assert cli.main(["--strict"]) == 1
