"""Unit tests for the pure helpers added to scripts/provision_users.py.

The script lives outside the package layout (scripts/), so we load it by path
via importlib — the same approach conftest.py uses for skills/*/build_report.py.
Only the pure/extracted helpers are tested; the live API orchestration
(snapshot_template, execute_user) is exercised via --self-test against a real
tenant, consistent with this repo's "don't unit-test 1:1 SDK calls" convention.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _load_provision():
    path = _REPO_ROOT / "scripts" / "provision_users.py"
    spec = importlib.util.spec_from_file_location("provision_users", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["provision_users"] = mod
    spec.loader.exec_module(mod)
    return mod


pu = _load_provision()


class TestDerivePhoneName:
    @pytest.mark.parametrize("email,expected", [
        ("jane.doe@example.com", "Jane.Doe"),
        ("john_smith@example.com", "John.Smith"),
        ("mary-jane.watson@example.com", "Mary.Jane.Watson"),
        ("madonna@example.com", "Madonna"),
        ("a.b.c@x.io", "A.B.C"),
    ])
    def test_dotted_titlecase(self, email, expected):
        assert pu.derive_phone_name(email) == expected
