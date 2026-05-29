"""Pins the v1.0 ``schema_version`` field + ``load_config`` behaviour.

Three outcomes:

1. Missing version (pre-1.0 config) → warning + load with defaults.
2. Newer version (config from a future genesys-mcp) → hard fail with a
   clear "upgrade" message.
3. Same or older version → load cleanly.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest


def _write_config(path: Path, body: str) -> None:
    path.write_text(
        body + """
tenant:
  name: "X"
  short_name: "x"
brands:
  names: ["BrandA"]
specialist_roles: ["Spec"]
operating_model:
  has_pre_break_presence: false
"""
    )


class TestMissingSchemaVersion:
    def test_loads_with_deprecation_warning(self, tmp_path: Path, monkeypatch,
                                            caplog: pytest.LogCaptureFixture):
        from genesys_mcp.tenant import load_config
        cfg_path = tmp_path / "tenant.yaml"
        _write_config(cfg_path, "")  # no schema_version key
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        with caplog.at_level(logging.WARNING, logger="genesys_mcp.tenant"):
            cfg = load_config()
        assert cfg.tenant.name == "X"
        # Loader should have warned about the missing version
        assert any("schema_version" in r.message for r in caplog.records)


class TestNewerSchemaVersion:
    def test_future_major_version_hard_fails(self, tmp_path: Path, monkeypatch):
        from genesys_mcp.tenant import TenantConfigError, load_config
        cfg_path = tmp_path / "tenant.yaml"
        _write_config(cfg_path, 'schema_version: "2.0"\n')
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        with pytest.raises(TenantConfigError, match=r"(?i)upgrade with"):
            load_config()

    def test_future_minor_version_hard_fails(self, tmp_path: Path, monkeypatch):
        from genesys_mcp.tenant import TenantConfigError, load_config
        cfg_path = tmp_path / "tenant.yaml"
        _write_config(cfg_path, 'schema_version: "1.99"\n')
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        with pytest.raises(TenantConfigError, match=r"(?i)upgrade with"):
            load_config()


class TestCurrentSchemaVersion:
    def test_explicit_v1_0_loads_cleanly(self, tmp_path: Path, monkeypatch,
                                         caplog: pytest.LogCaptureFixture):
        from genesys_mcp.tenant import load_config
        cfg_path = tmp_path / "tenant.yaml"
        _write_config(cfg_path, 'schema_version: "1.0"\n')
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        with caplog.at_level(logging.WARNING, logger="genesys_mcp.tenant"):
            cfg = load_config()
        assert cfg.schema_version == "1.0"
        # No warning for an up-to-date config
        assert not any("schema_version" in r.message for r in caplog.records)


class TestMalformedSchemaVersion:
    def test_non_numeric_version_string_fails_with_clear_message(
        self, tmp_path: Path, monkeypatch,
    ):
        from genesys_mcp.tenant import TenantConfigError, load_config
        cfg_path = tmp_path / "tenant.yaml"
        _write_config(cfg_path, 'schema_version: "v1-alpha"\n')
        monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
        with pytest.raises(TenantConfigError, match="not a valid"):
            load_config()
