"""Pytest configuration + shared fixtures for genesys-mcp tests.

Tests target the **aggregation layer** — pure functions in
``skills/*/build_report.py`` and ``src/genesys_mcp/tools/*.py``. The MCP tool
wrappers themselves are 1:1 SDK calls; testing them mostly tests the SDK, so
they're deliberately out of scope.

Conventions:

- Live-tenant fixtures live in ``tests/fixtures/`` and are produced by
  ``tests/_capture_fixtures.py`` against a known period. They're JSON dumps
  of each tool's response. Refresh them deliberately (not silently) when
  intentionally changing aggregator output shapes.
- The build_report.py modules live inside skill directories; the
  ``build_report_*`` fixtures below load each one via ``importlib`` so we
  can call its module-level aggregators without symlink/path gymnastics.
- Tenant config is mocked via a per-test ``temp_tenant_config`` fixture
  rather than relying on the user's real ``~/.config/genesys-mcp/tenant.yaml``.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_module_by_path(name: str, path: Path) -> ModuleType:
    """Import a Python file outside the package layout (skills/*/build_report.py)."""
    if str(_REPO_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT / "src"))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Module fixtures (importlib-loaded skill build scripts) ──

@pytest.fixture(scope="session")
def build_report_monthly() -> ModuleType:
    """The cc-monthly-report build_report.py module."""
    return _load_module_by_path(
        "br_monthly", _REPO_ROOT / "skills/cc-monthly-report/build_report.py",
    )


@pytest.fixture(scope="session")
def build_report_coaching() -> ModuleType:
    """The cc-coaching-prep build_report.py module."""
    return _load_module_by_path(
        "br_coaching", _REPO_ROOT / "skills/cc-coaching-prep/build_report.py",
    )


@pytest.fixture(scope="session")
def build_report_daily() -> ModuleType:
    """The cc-daily-brief build_report.py module."""
    return _load_module_by_path(
        "br_daily", _REPO_ROOT / "skills/cc-daily-brief/build_report.py",
    )


@pytest.fixture(scope="session")
def build_checklist_reconcile() -> ModuleType:
    """The mcp-reconcile build_checklist.py module."""
    return _load_module_by_path(
        "br_reconcile", _REPO_ROOT / "skills/mcp-reconcile/build_checklist.py",
    )


# ── Live-tenant fixture loaders ──

def _load_fixture(name: str) -> dict:
    path = _FIXTURES_DIR / name
    if not path.exists():
        pytest.skip(
            f"Fixture {name} not found at {path}. Run "
            f"`python tests/_capture_fixtures.py` against a live tenant to "
            f"generate fixtures, then re-run the test."
        )
    return json.loads(path.read_text())


@pytest.fixture(scope="session")
def fix_queue_performance() -> dict:
    return _load_fixture("queue_performance.json")


@pytest.fixture(scope="session")
def fix_queue_performance_daily() -> dict:
    return _load_fixture("queue_performance_daily.json")


@pytest.fixture(scope="session")
def fix_queue_performance_hourly() -> dict:
    return _load_fixture("queue_performance_hourly.json")


@pytest.fixture(scope="session")
def fix_agent_performance_daily() -> dict:
    return _load_fixture("agent_performance_daily.json")


@pytest.fixture(scope="session")
def fix_agent_performance() -> dict:
    return _load_fixture("agent_performance.json")


@pytest.fixture(scope="session")
def fix_break_overrun() -> dict:
    return _load_fixture("break_overrun_report.json")


@pytest.fixture(scope="session")
def fix_repeat_caller_deep_dive() -> dict:
    return _load_fixture("repeat_caller_deep_dive.json")


@pytest.fixture(scope="session")
def fix_wfm_schedule() -> dict:
    return _load_fixture("wfm_schedule.json")


@pytest.fixture(scope="session")
def fix_qmap() -> dict[str, list[str]]:
    """queueId → [brand, queue_name] map captured alongside the tool outputs."""
    return _load_fixture("qmap.json")


@pytest.fixture(scope="session")
def fix_user_roles() -> dict[str, list[str]]:
    """userId → [name, role] map captured alongside the tool outputs."""
    return _load_fixture("user_roles.json")


@pytest.fixture(scope="session")
def fix_expected_outputs() -> dict:
    """Captured aggregator outputs from a known-good run.

    Populated by ``tests/_capture_fixtures.py`` after running each aggregator
    against the live-tenant fixtures. Test assertions compare current
    aggregator output against these recorded values — any drift = either a
    bug or an intentional change that needs the fixture refreshed.
    """
    return _load_fixture("expected_outputs.json")


# ── Tenant-config mock ──

@pytest.fixture
def temp_tenant_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a minimal valid tenant.yaml and point the loader at it.

    Avoids depending on the user's real ~/.config/genesys-mcp/tenant.yaml
    during testing. Returns the resolved Path so tests can override the
    contents per-test if needed.
    """
    cfg_path = tmp_path / "tenant.yaml"
    cfg_path.write_text(
        """tenant:
  name: "Test Tenant"
  short_name: "test"
  timezone: "UTC"
brands:
  names: ["BrandA", "BrandB"]
queues:
  name_pattern: "{brand} - {function}"
  channels: ["Voice", "Chat"]
  functions: ["Sales", "Support"]
  skip_substrings: ["Holding"]
specialist_roles: ["Specialist"]
operating_model:
  has_pre_break_presence: false
"""
    )
    monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg_path))
    return cfg_path
