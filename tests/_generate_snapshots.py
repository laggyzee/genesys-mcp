#!/usr/bin/env python3
"""Regenerate aggregator output snapshots from the captured live-tenant fixtures.

The :mod:`tests.test_snapshots` suite pins the *numeric output* of each
aggregator against a small JSON file under ``tests/fixtures/_snapshots/``.
A behaviour change that flips any number trips the snapshot diff — exactly
the kind of bug that the v0.9.1 bucket-overwrite landed without an error.

Regenerate snapshots only when the behaviour change is **intentional**:

    python tests/_generate_snapshots.py

Review the resulting git diff line-by-line before committing — every number
that moves should be expected and explained in the commit / release notes.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_SNAPSHOT_DIR = _FIXTURES / "_snapshots"

sys.path.insert(0, str(_REPO_ROOT / "src"))


def _load_module(name: str, rel: str) -> ModuleType:
    path = _REPO_ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_json(name: str) -> dict | list:
    return json.loads((_FIXTURES / name).read_text())


def _normalise(obj):
    """Recursively round floats so trivial drift (e.g. 0.81234 vs 0.81235)
    doesn't fail the snapshot. Snapshots pin meaningful precision only.
    """
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _normalise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalise(x) for x in obj]
    return obj


def main() -> int:
    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    br = _load_module("br_monthly", "skills/cc-monthly-report/build_report.py")

    qp = _load_json("queue_performance.json")
    qp_daily = _load_json("queue_performance_daily.json")
    ap = _load_json("agent_performance.json")
    brk = _load_json("break_overrun_report.json")
    deep = _load_json("repeat_caller_deep_dive.json")
    qmap = _load_json("qmap.json")
    user_roles = _load_json("user_roles.json")

    snapshots = {
        "aggregate_queue_performance": br.aggregate_queue_performance(qp, qmap),
        "aggregate_agents": br.aggregate_agents(ap, brk, user_roles, specialist_only=True),
        "aggregate_daily_voice_sl": br.aggregate_daily_voice_sl(qp_daily, qmap),
        "extract_themes": br.extract_themes(deep),
    }

    for name, value in snapshots.items():
        out = _SNAPSHOT_DIR / f"{name}.json"
        out.write_text(json.dumps(_normalise(value), indent=2, sort_keys=True, default=str))
        print(f"wrote {out.relative_to(_REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
