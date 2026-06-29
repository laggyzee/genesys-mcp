#!/usr/bin/env python3
"""Build the cc-workforce-history HTML from a single user_activity_history JSON dump.

Tenant-aware via ``~/.config/genesys-mcp/tenant.yaml``. Reads:

    /tmp/cc-workforce-history-{period-slug}/result.json

The JSON is the parsed ``user_activity_history`` (v1.12) tool response.

Usage (driven by SKILL.md):

    python build_report.py \\
        --data /tmp/cc-workforce-history-2023-07-to-2026-06/result.json \\
        --period "Jul 2023 - Jun 2026" \\
        --period-slug "2023-07-to-2026-06" \\
        --output ~/Documents/tenant-workforce-history-2023-07-to-2026-06.html

The script accepts a v1.12.1 soft-fail envelope (``{status, kind, message}``)
in the input file and renders a visible callout instead of crashing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from html import escape
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from genesys_mcp.tenant import load_config  # noqa: E402


CSS = """
:root {
  --good: #2c7a3e; --good-soft: #e2f1e7;
  --warn: #b06600; --warn-soft: #fbeed3;
  --bad:  #b3261e; --bad-soft:  #fbe1dd;
  --muted: #6b7280;
  --line: #e5e7eb;
  --bg: #fff;
  --text: #111;
}
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       color: var(--text); margin: 0; line-height: 1.5; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 24px 28px 48px; }
header.title-band { padding-bottom: 12px; border-bottom: 2px solid var(--line); margin-bottom: 18px; }
h1 { margin: 0 0 4px 0; font-size: 24px; }
h2 { margin: 24px 0 10px 0; font-size: 18px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
h3 { margin: 16px 0 6px 0; font-size: 15px; color: var(--muted); }
.meta { color: var(--muted); font-size: 13px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0 18px; }
thead th { text-align: left; background: #f6f7f9; padding: 6px 8px;
           border-bottom: 1px solid var(--line); font-weight: 600; }
tbody td { padding: 5px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.muted { color: var(--muted); }
.pill { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
        font-weight: 600; }
.pill.joiner  { background: var(--good-soft); color: var(--good); }
.pill.leaver  { background: var(--bad-soft);  color: var(--bad); }
.pill.active  { background: #e0e7ff; color: #3730a3; }
.pill.inactive { background: #fff7ed; color: #c2410c; }
.pill.deleted { background: #fee2e2; color: #991b1b; }
.callout { background: var(--good-soft); border-left: 4px solid var(--good);
           padding: 10px 14px; margin: 10px 0; border-radius: 4px; font-size: 13px; }
.callout.warn { background: var(--warn-soft); border-left-color: var(--warn); }
.callout.bad  { background: var(--bad-soft);  border-left-color: var(--bad); }
.bar-track { height: 14px; background: #f3f4f6; border-radius: 3px; overflow: hidden; }
.bar-fill { height: 100%; background: #4f46e5; }
footer { color: var(--muted); font-size: 11px; padding-top: 12px; margin-top: 24px;
         border-top: 1px solid var(--line); }
"""


def _fmt_int(n):
    return "—" if n is None else f"{int(n):,}"


def _fmt_float(n, dp=1):
    return "—" if n is None else f"{n:.{dp}f}"


def _state_pill(state):
    if not state:
        return '<span class="muted">—</span>'
    cls = state if state in ("active", "inactive", "deleted") else "active"
    return f'<span class="pill {cls}">{escape(state)}</span>'


def _bar(value, max_value):
    if max_value <= 0:
        return ""
    pct = min(100, round(value / max_value * 100))
    return (
        '<div class="bar-track">'
        f'<div class="bar-fill" style="width:{pct}%"></div>'
        '</div>'
    )


def render_data_coverage_callout(data: dict, interval: str) -> str:
    """Surface ``data_starts_at`` so the user can interpret pre-retention buckets."""
    starts = data.get("data_starts_at")
    if not starts:
        return (
            '<div class="callout bad"><strong>No activity data found in this window.</strong> '
            'Either no agents handled interactions during this period, or the window '
            'predates Genesys analytics retention (typically ~13 months).</div>'
        )
    if not interval or "/" not in interval:
        return ""
    interval_start_date = interval.split("/")[0][:10]
    interval_start_ym = interval_start_date[:7]
    if starts > interval_start_ym:
        return (
            f'<div class="callout warn"><strong>Data starts at {escape(starts)}</strong> — '
            f'earlier quarters in this window are past Genesys analytics retention '
            f'(requested from {escape(interval_start_ym)}). Treat zero-headcount buckets '
            f'before {escape(starts)} as <em>unknown</em>, not zero.</div>'
        )
    return ""


def render_headcount_section(headcount: list[dict]) -> str:
    if not headcount:
        return ""
    max_active = max((h.get("active_agents") or 0) for h in headcount)
    rows = []
    for h in headcount:
        active = h.get("active_agents") or 0
        joiners = h.get("joiners") or 0
        leavers = h.get("leavers") or 0
        joiner_cell = (
            f'<span class="pill joiner">+{joiners}</span>'
            if joiners else '<span class="muted">—</span>'
        )
        leaver_cell = (
            f'<span class="pill leaver">−{leavers}</span>'
            if leavers else '<span class="muted">—</span>'
        )
        rows.append(
            f'<tr><td>{escape(h.get("bucket") or "")}</td>'
            f'<td class="num">{_fmt_int(active)}</td>'
            f'<td>{_bar(active, max_active)}</td>'
            f'<td class="num">{joiner_cell}</td>'
            f'<td class="num">{leaver_cell}</td></tr>'
        )
    return (
        '<section id="headcount">'
        '<h2>1. Active agents per quarter</h2>'
        '<p class="meta">Active = ≥1 handled interaction (voice + message + callback) '
        'in the quarter. Joiners = first-active month falls in the quarter. Leavers = '
        'last-active month falls in the quarter <em>and</em> it is not the most recent '
        'quarter in the window.</p>'
        '<table><thead><tr><th>Quarter</th><th class="num">Active</th>'
        '<th>Trend</th><th class="num">Joiners</th><th class="num">Leavers</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        '</section>'
    )


def render_tenure_section(tenure: list[dict]) -> str:
    if not tenure:
        return ""
    rows = []
    for t in tenure:
        rows.append(
            f'<tr><td>{escape(t.get("bucket") or "")}</td>'
            f'<td class="num">{_fmt_float(t.get("mean_tenure_months"))}</td>'
            f'<td class="num">{_fmt_float(t.get("median_tenure_months"))}</td>'
            f'<td class="num">{_fmt_int(t.get("n"))}</td></tr>'
        )
    return (
        '<section id="tenure">'
        '<h2>2. Tenure trend</h2>'
        '<p class="meta">Tenure measured from each agent\'s first-active month to '
        'the start of the bucket (months). <code>n</code> = active-agent count that '
        'quarter (matches section 1).</p>'
        '<table><thead><tr><th>Quarter</th>'
        '<th class="num">Mean tenure (months)</th>'
        '<th class="num">Median tenure (months)</th>'
        '<th class="num">n</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        '</section>'
    )


def render_per_user_section(per_user: list[dict]) -> str:
    if not per_user:
        return ""
    rows = []
    for u in per_user:
        flags = []
        if u.get("is_joiner_in_window"):
            flags.append('<span class="pill joiner">joiner</span>')
        if u.get("is_leaver_in_window"):
            flags.append('<span class="pill leaver">leaver</span>')
        flags_html = " ".join(flags) or '<span class="muted">—</span>'
        rows.append(
            f'<tr><td>{escape(u.get("name") or u.get("user_id") or "—")}</td>'
            f'<td>{_state_pill(u.get("state"))}</td>'
            f'<td>{escape(u.get("first_active_date") or "—")}</td>'
            f'<td>{escape(u.get("last_active_date") or "—")}</td>'
            f'<td class="num">{_fmt_int(u.get("total_handled"))}</td>'
            f'<td>{flags_html}</td></tr>'
        )
    return (
        '<section id="per-user">'
        f'<h2>3. Per-person first / last active ({len(per_user)} users)</h2>'
        '<p class="meta">Sorted by total handled interactions desc. Users with zero '
        'activity in the window appear at the bottom with no first/last date.</p>'
        '<table><thead><tr><th>Name</th><th>State</th>'
        '<th>First active</th><th>Last active</th>'
        '<th class="num">Total handled</th><th>Flags</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        '</section>'
    )


def render_soft_fail_page(envelope: dict, cfg, period: str) -> str:
    """If the tool returned a soft-fail envelope, render a single-callout
    page explaining the gap with the missing-scope remediation."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<title>{escape(cfg.tenant.name)} — Workforce history {escape(period)}</title>'
        f"<style>{CSS}</style></head><body><div class=\"wrap\">"
        '<header class="title-band">'
        f'<h1>Workforce history — {escape(period)}</h1>'
        f'<div class="meta">{escape(cfg.tenant.name)}</div>'
        '</header>'
        '<div class="callout bad">'
        '<strong>⚠️ Workforce data not retrieved</strong> '
        f'(status {envelope.get("status")}). '
        f'{escape(str(envelope.get("message") or ""))}'
        '</div>'
        f'<footer>Generated {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")} · '
        'genesys-mcp cc-workforce-history</footer>'
        '</div></body></html>'
    )


def render_html(data: dict, period: str, cfg) -> str:
    interval = data.get("interval") or ""
    bucket = data.get("bucket") or "quarter"
    user_count = data.get("user_count") or 0
    coverage = render_data_coverage_callout(data, interval)
    headcount = render_headcount_section(data.get("headcount_by_bucket") or [])
    tenure = render_tenure_section(data.get("tenure_trend") or [])
    per_user = render_per_user_section(data.get("per_user") or [])

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<title>{escape(cfg.tenant.name)} — Workforce history {escape(period)}</title>'
        f"<style>{CSS}</style></head><body><div class=\"wrap\">"
        '<header class="title-band">'
        f'<h1>Workforce history — {escape(period)}</h1>'
        f'<div class="meta">{escape(cfg.tenant.name)} · '
        f'{user_count} users in scope (active + inactive + deleted) · '
        f'bucket: {escape(bucket)} · tz: {escape(data.get("tz") or "UTC")}</div>'
        '</header>'
        f'{coverage}'
        f'{headcount}'
        f'{tenure}'
        f'{per_user}'
        f'<footer>Generated {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")} · '
        f'genesys-mcp cc-workforce-history · '
        f'interval: {escape(interval)} · data_starts_at: '
        f'{escape(str(data.get("data_starts_at") or "—"))}</footer>'
        '</div></body></html>'
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True,
                   help="Path to the user_activity_history JSON dump.")
    p.add_argument("--period", required=True,
                   help="Human label, e.g. 'Jul 2023 - Jun 2026'.")
    p.add_argument("--period-slug", required=True,
                   help="Slug for output filename, e.g. '2023-07-to-2026-06'.")
    p.add_argument("--output", default=None,
                   help="Output HTML path (defaults to "
                        "cfg.report_output_path('workforce-history-<slug>')).")
    args = p.parse_args()

    cfg = load_config()
    data = json.loads(Path(args.data).expanduser().read_text())

    status = data.get("status")
    if isinstance(status, int) and status >= 400:
        html = render_soft_fail_page(data, cfg, args.period)
    else:
        html = render_html(data, args.period, cfg)

    out_path = (
        Path(args.output).expanduser() if args.output
        else cfg.report_output_path(f"workforce-history-{args.period_slug}")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    print(f"OK wrote {out_path} ({len(html):,} bytes)")
    if isinstance(status, int) and status >= 400:
        print(f"   soft-fail rendered: status {status}, message starts: "
              f"{str(data.get('message') or '')[:80]}")
    else:
        print(f"   {data.get('user_count')} users · "
              f"{len(data.get('headcount_by_bucket') or [])} buckets · "
              f"data_starts_at={data.get('data_starts_at')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
