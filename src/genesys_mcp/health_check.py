"""CLI entry point for the health check.

Invoke via ``python -m genesys_mcp.health_check``. Reads OAuth credentials
from ``GENESYS_CLIENT_*`` (or ``~/.config/genesys-mcp.env``, ``.env``,
``.env.write`` mirroring the provisioning script's loader), runs every
check, and prints a human-readable report to stdout. Exit code 0 when the
verdict is ``ready`` or ``ready_with_warnings``, 1 when ``blocked``.

Used by ``install.sh`` to verify the install succeeded.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Load env files the way scripts/provision_users.py does — works whether
# the user keeps creds in .env / .env.write / ~/.config/.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILES = (
    _REPO_ROOT / ".env",
    _REPO_ROOT / ".env.write",
    Path.home() / ".config" / "genesys-mcp.env",
)


def _load_env_files() -> list[Path]:
    loaded: list[Path] = []
    for path in _ENV_FILES:
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
        loaded.append(path)
    return loaded


_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"
_BOLD = "\033[1m"


def _glyph(status: str) -> str:
    if status == "ok":
        return f"{_GREEN}✓{_RESET}"
    if status == "missing":
        return f"{_RED}✗{_RESET}"
    return f"{_YELLOW}!{_RESET}"


def _print_report(report: dict, *, as_json: bool, no_colour: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2))
        return

    if no_colour:
        green = yellow = red = reset = bold = ""
    else:
        green, yellow, red, reset, bold = _GREEN, _YELLOW, _RED, _RESET, _BOLD

    verdict = report["verdict"]
    if verdict == "ready":
        verdict_str = f"{green}{bold}READY{reset}"
    elif verdict == "ready_with_warnings":
        verdict_str = f"{yellow}{bold}READY WITH WARNINGS{reset}"
    else:
        verdict_str = f"{red}{bold}BLOCKED{reset}"

    version = report.get("mcp_version", "unknown")
    print(f"\n{bold}genesys-mcp v{version} health check{reset}")
    print(f"Verdict: {verdict_str}\n")

    print(f"{bold}OAuth scopes{reset} (region: {report['oauth']['region']})")
    for s in report["oauth"]["scopes_tested"]:
        status = s["status"]
        glyph_str = _glyph(status) if not no_colour else (
            "OK" if status == "ok" else "MISSING" if status == "missing" else status.upper()
        )
        print(f"  {glyph_str} {s['scope']:38s} {s['description']}")
        if status != "ok" and s.get("remediation"):
            print(f"      → {s['remediation']}")
    print()

    print(f"{bold}Tenant config{reset}")
    tc = report["tenant_config"]
    print(f"  path: {tc['path']}")
    if not tc["exists"]:
        print(f"  {_glyph('missing') if not no_colour else 'MISSING'} file does not exist")
    elif not tc["loaded_ok"]:
        print(f"  {_glyph('missing') if not no_colour else 'INVALID'} present but failed validation")
    else:
        print(f"  {_glyph('ok') if not no_colour else 'OK'} loaded: tenant={tc['tenant_name']!r} "
              f"brands={tc['brand_count']} MUs={tc['mu_count']}")
    for err in tc.get("errors", []):
        print(f"      → {err}")
    for warn in tc.get("warnings", []):
        print(f"      ⚠ {warn}")
    print()

    print(f"{bold}Companion skills{reset}")
    for s in report["skills_linked"]:
        glyph_str = _glyph("ok" if s["ok"] else "missing") if not no_colour else (
            "OK" if s["ok"] else "MISSING"
        )
        loc = s["linked_at"] or "(not linked)"
        print(f"  {glyph_str} {s['skill']:24s} {loc}")
        if not s["ok"] and s.get("remediation"):
            print(f"      → {s['remediation']}")
    print()

    if report["blockers"]:
        print(f"{bold}{red}Blockers:{reset}")
        for b in report["blockers"]:
            print(f"  • {b}")
        print()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m genesys_mcp.health_check",
        description="Verify the MCP is wired up correctly end-to-end.",
    )
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of a human-readable report.")
    parser.add_argument("--no-colour", action="store_true",
                        help="Disable ANSI colour escapes in the human report.")
    parser.add_argument(
        "--strict", action="store_true",
        help=(
            "Exit non-zero on any warning, not just blockers. Use for CI / "
            "scripted pre-release validation — under --strict, an "
            "actionable misconfiguration (low queue-pattern match rate, "
            "specialist_roles don't resolve, etc.) fails the build."
        ),
    )
    args = parser.parse_args(argv)

    _load_env_files()

    # Late import so --help works without OAuth creds set.
    from genesys_mcp.client import init_api
    from genesys_mcp.tools.health import run_health_check

    try:
        init_api()
    except Exception as exc:
        report = {
            "verdict": "blocked",
            "blockers": [f"OAuth client init failed: {exc}"],
            "oauth": {"region": os.environ.get("GENESYS_REGION", "?"),
                      "scopes_tested": []},
            "tenant_config": {"path": "(not checked)", "exists": False,
                              "loaded_ok": False, "errors": [str(exc)]},
            "skills_linked": [],
        }
        _print_report(report, as_json=args.json, no_colour=args.no_colour)
        return 1

    report = run_health_check()
    _print_report(report, as_json=args.json, no_colour=args.no_colour)
    if report["verdict"] == "blocked":
        return 1
    if args.strict and report["verdict"] == "ready_with_warnings":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
