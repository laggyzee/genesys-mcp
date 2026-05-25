#!/usr/bin/env python3
"""One-off: capture MCP tool outputs as golden test fixtures.

Run this against a live tenant when:
- First setting up the test suite
- Intentionally changing a tool's response shape (refresh the fixtures so
  tests pin the new shape, not the old)
- Verifying numbers against a different tenant

Usage:

    .venv/bin/python tests/_capture_fixtures.py \\
        --interval "2026-05-18T14:00:00.000Z/2026-05-25T14:00:00.000Z" \\
        --queue-name-substring "Acme"   # filter scope; keeps fixtures small

Captures:
    tests/fixtures/queue_performance.json       (P1M aggregates)
    tests/fixtures/queue_performance_daily.json (P1D aggregates — daily SL chart)
    tests/fixtures/agent_performance.json
    tests/fixtures/break_overrun_report.json
    tests/fixtures/repeat_caller_deep_dive.json
    tests/fixtures/wfm_schedule.json
    tests/fixtures/qmap.json
    tests/fixtures/user_roles.json

The aggregator tests in tests/test_aggregators.py then load these and
assert structural properties (counts, totals, expected fields populated)
rather than byte-identical snapshots — fixtures don't break on CSS tweaks
or minor field reorderings.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _load_env() -> None:
    """Same env-loading pattern as the wizard / health check."""
    for path in [
        _REPO_ROOT / ".env",
        Path.home() / ".config" / "genesys-mcp.env",
    ]:
        if not path.exists():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--interval", required=True,
                   help="ISO interval — typically a recent completed week.")
    p.add_argument("--queue-name-substring",
                   help="Optional: only include queues whose name contains this "
                        "substring (keeps the fixture small + tenant-anonymous).")
    p.add_argument("--user-title-filter", default="Customer Service Specialist",
                   help="Only include users with this title (default: 'Customer "
                        "Service Specialist'). Pass empty to include all.")
    p.add_argument("--max-users", type=int, default=10,
                   help="Cap the number of users to keep fixtures small.")
    args = p.parse_args()

    _load_env()
    from genesys_mcp.client import init_api, get_api, to_dict
    init_api()

    import PureCloudPlatformClientV2 as gc
    from mcp.server.fastmcp import FastMCP
    from genesys_mcp.tools import analytics, reports, wfm
    from genesys_mcp.tenant import load_config

    cfg = load_config()
    fixtures_dir = _REPO_ROOT / "tests" / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    # ── Discover queues + users ──
    routing_api = gc.RoutingApi(get_api())
    users_api = gc.UsersApi(get_api())

    queues_resp = to_dict(routing_api.get_routing_queues(page_size=200))
    qmap: dict[str, list[str]] = {}
    for q in queues_resp.get("entities") or []:
        if args.queue_name_substring and args.queue_name_substring not in q["name"]:
            continue
        # brand inferred from " - " split (cc-monthly-report convention).
        parts = [s.strip() for s in q["name"].split(" - ")]
        brand = parts[0] if parts else "?"
        qmap[q["id"]] = [brand, q["name"]]
    print(f"  ✓ qmap: {len(qmap)} queues")
    (fixtures_dir / "qmap.json").write_text(json.dumps(qmap, indent=2))

    # Users: filter by title; cap at max-users to keep fixtures lean
    user_ids: list[str] = []
    user_roles: dict[str, list[str]] = {}
    for page in range(1, 10):
        r = to_dict(users_api.get_users(page_size=200, page_number=page, state="active"))
        for u in r.get("entities") or []:
            if args.user_title_filter and u.get("title") != args.user_title_filter:
                continue
            user_ids.append(u["id"])
            user_roles[u["id"]] = [u.get("name", "?"), u.get("title", "?")]
            if len(user_ids) >= args.max_users:
                break
        if len(user_ids) >= args.max_users or len(r.get("entities") or []) < 200:
            break
    print(f"  ✓ user_roles: {len(user_roles)} users (title={args.user_title_filter!r}, capped at {args.max_users})")
    (fixtures_dir / "user_roles.json").write_text(json.dumps(user_roles, indent=2))

    # ── Pull each tool's output via the FastMCP harness ──
    test_mcp = FastMCP(name="capture")
    analytics.register(test_mcp)
    reports.register(test_mcp)
    wfm.register(test_mcp)

    queue_ids = list(qmap.keys())

    async def _capture(name: str, tool: str, payload: dict) -> None:
        print(f"  → {name}...")
        result = await test_mcp.call_tool(tool, payload)
        text = result[0].text if isinstance(result, list) else result
        (fixtures_dir / name).write_text(text)
        print(f"    ✓ {(fixtures_dir / name).stat().st_size:,} bytes")

    async def _go():
        await _capture("queue_performance.json", "queue_performance", {
            "queue_ids": queue_ids,
            "interval": args.interval,
            "granularity": "P1M",
        })
        await _capture("queue_performance_daily.json", "queue_performance", {
            "queue_ids": queue_ids,
            "interval": args.interval,
            "granularity": "P1D",
        })
        await _capture("agent_performance.json", "agent_performance", {
            "user_ids": user_ids,
            "interval": args.interval,
            "granularity": "P1M",
        })
        await _capture("break_overrun_report.json", "break_overrun_report", {
            "user_ids": user_ids,
            "interval": args.interval,
        })
        await _capture("repeat_caller_deep_dive.json", "repeat_caller_deep_dive", {
            "queue_ids": [],
            "interval": args.interval,
            "media_type": "voice",
            "min_calls": 3,
            "max_anis": 10,
        })
        # wfm_schedule — best-effort; soft-fail if it errors (BU/MU may not be set)
        if cfg.business_unit.id and cfg.management_units.ids:
            try:
                await _capture("wfm_schedule.json", "wfm_schedule", {
                    "business_unit_id": cfg.business_unit.id,
                    "management_unit_ids": cfg.management_units.ids,
                    "user_ids": user_ids,
                    "interval": args.interval,
                })
            except Exception as exc:
                print(f"    ! wfm_schedule failed: {exc} — skipping")
        else:
            print("    ! wfm_schedule skipped: no business_unit / management_units in tenant config")

    asyncio.run(_go())

    print(f"\nOK fixtures captured under {fixtures_dir}/. Run `make test` to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
