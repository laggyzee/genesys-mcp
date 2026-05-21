#!/usr/bin/env python3
"""Build the cc-daily-brief HTML from raw genesys-mcp JSON dumps.

Tenant-aware via ``~/.config/genesys-mcp/tenant.yaml``. Reads:

    /tmp/cc-daily-brief-{date}/queue_perf_day.json
    /tmp/cc-daily-brief-{date}/queue_perf_window.json
    /tmp/cc-daily-brief-{date}/agent_perf_day.json
    /tmp/cc-daily-brief-{date}/repeat_callers.json
    /tmp/cc-daily-brief-{date}/break_overrun.json

Each is the parsed result.text payload from the corresponding MCP tool call.

Usage (driven by SKILL.md):

    python build_report.py \\
        --target-date 2026-05-20 \\
        --day-interval "2026-05-19T14:00:00.000Z/2026-05-20T14:00:00.000Z" \\
        --window-interval "2026-05-12T14:00:00.000Z/2026-05-19T14:00:00.000Z" \\
        --data-dir /tmp/cc-daily-brief-2026-05-20 \\
        --output ~/Documents/daily-brief-2026-05-20.html
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from statistics import median
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from genesys_mcp.tenant import TenantConfig, load_config  # noqa: E402


# ── helpers (subset of monthly skill's) ──

def fmt_int(n) -> str:
    return "—" if n is None else f"{int(n):,}"


def fmt_pct(n, digits=0) -> str:
    if n is None:
        return "—"
    return f"{n:.{digits}f}%"


def fmt_secs(s) -> str:
    if s is None or s == 0:
        return "—"
    s = int(round(s))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def _vs_pill(value: float | None, baseline: float | None,
             lower_is_better: bool = False, units: str = "") -> str:
    if value is None or baseline is None:
        return '<span class="muted">—</span>'
    diff = value - baseline
    if baseline == 0:
        pct = None
    else:
        pct = diff / baseline * 100
    # Colour rules
    if lower_is_better:
        cls = ("good" if diff < -0.05 * abs(baseline)
               else "bad" if diff > 0.05 * abs(baseline)
               else "warn")
    else:
        cls = ("good" if diff > 0.05 * abs(baseline)
               else "bad" if diff < -0.05 * abs(baseline)
               else "warn")
    sign = "+" if diff > 0 else ""
    pct_str = f" ({sign}{pct:.0f}%)" if pct is not None else ""
    return f'<span class="vs-target {cls}">{sign}{diff:.1f}{units}{pct_str}</span>'


# ── aggregation ──

def aggregate_queue_kpis(qp: dict, qmap: dict | None = None) -> dict[str, dict]:
    """Reduce queue_performance output to per-queue voice + message KPIs.

    Genesys analytics aggregates shape::
        {"results": [
            {"group": {"queueId": ..., "mediaType": "voice"},
             "data": [{"interval": ..., "metrics": [...], "derived": {...}}]},
            ...
        ]}

    Returns ``{queue_id: {name, voice_sl_pct, message_sl_pct, voice_offered,
    message_offered, voice_answered, message_answered}}``.
    """
    out: dict[str, dict] = defaultdict(lambda: {
        "name": None,
        "voice_sl_pct": None, "voice_offered": 0, "voice_answered": 0,
        "message_sl_pct": None, "message_offered": 0, "message_answered": 0,
    })
    qmap = qmap or {}
    for r in qp.get("results") or []:
        grp = r.get("group") or {}
        qid = grp.get("queueId")
        media = grp.get("mediaType")
        if not qid or media not in ("voice", "message"):
            continue
        derived = (r.get("data") or [{}])[0].get("derived") or {}
        metrics = (r.get("data") or [{}])[0].get("metrics") or []
        # Offered = nOffered.count (preferred) or nConnected.count (fallback).
        offered = 0
        for m in metrics:
            if m.get("metric") == "nOffered":
                offered = (m.get("stats") or {}).get("count") or 0
                break
        if not offered:
            for m in metrics:
                if m.get("metric") == "nConnected":
                    offered = (m.get("stats") or {}).get("count") or 0
                    break
        answered = derived.get("answered") or 0
        sl_pct = derived.get("service_level_pct")
        out[qid]["name"] = qmap.get(qid, [None, None])[1] if qmap else None
        if media == "voice":
            out[qid]["voice_sl_pct"] = sl_pct
            out[qid]["voice_offered"] = offered
            out[qid]["voice_answered"] = answered
        elif media == "message":
            out[qid]["message_sl_pct"] = sl_pct
            out[qid]["message_offered"] = offered
            out[qid]["message_answered"] = answered
    return dict(out)


def headline_kpis(day_kpis: dict[str, dict], window_kpis: dict[str, dict]) -> dict[str, Any]:
    """Compute org-wide voice/message SL for the day, vs rolling-window medians."""

    def _weighted_sl(kpis: dict[str, dict], media: str) -> float | None:
        total_off = 0
        weighted = 0.0
        for q in kpis.values():
            off = q[f"{media}_offered"]
            sl = q[f"{media}_sl_pct"]
            if off and sl is not None:
                total_off += off
                weighted += sl * off
        return (weighted / total_off) if total_off else None

    return {
        "voice_sl_today": _weighted_sl(day_kpis, "voice"),
        "voice_sl_baseline": _weighted_sl(window_kpis, "voice"),
        "message_sl_today": _weighted_sl(day_kpis, "message"),
        "message_sl_baseline": _weighted_sl(window_kpis, "message"),
        "total_offered_today": sum(q["voice_offered"] + q["message_offered"]
                                   for q in day_kpis.values()),
        "total_offered_baseline_per_day": (
            sum(q["voice_offered"] + q["message_offered"] for q in window_kpis.values())
            / max(1, len({q["name"] for q in window_kpis.values()}))
        ) if window_kpis else None,
    }


def worst_routes(day_kpis: dict[str, dict], window_kpis: dict[str, dict],
                 sl_drop_threshold_pp: float, top_n: int = 5) -> list[dict]:
    """Top N queues by voice SL drop vs the rolling window."""
    rows: list[dict] = []
    for qid, today in day_kpis.items():
        if not today.get("voice_offered"):
            continue
        baseline = window_kpis.get(qid, {})
        bsl = baseline.get("voice_sl_pct")
        tsl = today.get("voice_sl_pct")
        if bsl is None or tsl is None:
            continue
        drop = bsl - tsl
        if drop < sl_drop_threshold_pp:
            continue
        rows.append({
            "queue_id": qid,
            "queue_name": today.get("name") or qid,
            "today_sl_pct": tsl,
            "baseline_sl_pct": bsl,
            "drop_pp": round(drop, 1),
            "today_offered": today["voice_offered"],
        })
    rows.sort(key=lambda r: -r["drop_pp"])
    return rows[:top_n]


def flagged_agents(agent_perf: dict, voice_aht_target_s: int,
                   aht_excess_pct_threshold: float, user_names: dict[str, str] | None = None,
                   top_n: int = 5) -> list[dict]:
    """Top agents by AHT excess on yesterday's voice handle.

    Reads the analytics aggregates shape: ``results[*].group = {userId, mediaType}``,
    ``data[0].derived = {answered, aht_s, ...}``. For v0.7, the flag heuristic
    uses AHT-vs-target only — daily-brief shouldn't run the full coaching walk
    per agent (expensive). Sentiment + adherence flags would be a v0.7.x
    extension if signal warrants.
    """
    user_names = user_names or {}
    per_user: dict[str, dict] = {}
    for r in agent_perf.get("results") or []:
        grp = r.get("group") or {}
        uid = grp.get("userId")
        media = grp.get("mediaType")
        if not uid or media != "voice":
            continue
        derived = (r.get("data") or [{}])[0].get("derived") or {}
        answered = derived.get("answered") or 0
        aht_s = derived.get("avg_handle_s")
        if not answered or not aht_s or answered < 5:
            continue
        excess_pct = (aht_s - voice_aht_target_s) / voice_aht_target_s * 100.0
        if excess_pct < aht_excess_pct_threshold:
            continue
        per_user[uid] = {
            "user_id": uid,
            "name": user_names.get(uid) or uid[:8],
            "voice_answered": int(answered),
            "voice_aht_s": aht_s,
            "voice_aht_excess_pct": round(excess_pct, 1),
            "voice_excess_minutes": round((aht_s - voice_aht_target_s) * answered / 60.0, 1),
        }
    rows = sorted(per_user.values(), key=lambda r: -r["voice_excess_minutes"])
    return rows[:top_n]


def repeat_caller_hotlist(deep: dict, top_n: int = 8) -> list[dict]:
    """Filter the deep-dive's unresolved_repeaters to a top-N callback list."""
    rows = deep.get("unresolved_repeaters") or []
    out = []
    for r in rows[:top_n]:
        out.append({
            "ani_masked": r.get("ani_masked") or r.get("ani"),
            "answered_count": r.get("answered_count"),
            "unresolved_share": r.get("unresolved_share"),
            "last_summary": (r.get("last_summary") or "")[:160],
            "recommended_action": r.get("recommended_action"),
        })
    return out


def adherence_flags(brk: dict, user_names: dict[str, str] | None = None,
                    overrun_min_threshold: int = 30,
                    top_n: int = 5) -> list[dict]:
    """Agents whose total break/meal/away/pre-break overrun > threshold.

    Reads break_overrun_report's shape: ``users[*] = {user_id, total_overrun_min,
    pre_break_overrun_total_min, away_total_min, ...}``.
    """
    user_names = user_names or {}
    rows: list[dict] = []
    for r in brk.get("users") or []:
        # Per-source overruns
        pb_min = r.get("pre_break_overrun_total_min") or 0
        # break/meal overruns roll up into total_overrun_min (the report's
        # primary number). We split where possible; otherwise total carries it.
        total_min = r.get("total_overrun_min") or 0
        if total_min < overrun_min_threshold:
            continue
        uid = r.get("user_id")
        rows.append({
            "name": user_names.get(uid) or uid[:8],
            "user_id": uid,
            "pre_break_overrun_min": pb_min,
            "break_overrun_min": max(0, total_min - pb_min),  # remainder
            "meal_overrun_min": 0,  # not split in the source
            "away_total_min": r.get("away_total_min") or 0,
            "total_min": round(total_min, 1),
        })
    rows.sort(key=lambda r: -r["total_min"])
    return rows[:top_n]


# ── CSS — trimmed from cc-monthly-report's; same visual idiom ──

CSS = """
:root {
  --ink:#1a2332;--ink-soft:#4a5568;--muted:#718096;--line:#e2e8f0;
  --bg:#ffffff;--bg-soft:#f7fafc;--bg-card:#fafbfc;
  --accent:#2c5282;--accent-soft:#ebf4fc;
  --good:#2f855a;--good-soft:#f0fff4;
  --warn:#c05621;--warn-soft:#fffaf0;
  --bad:#c53030;--bad-soft:#fff5f5;
}
* { box-sizing: border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
  color:var(--ink); background:var(--bg-soft); margin:0; line-height:1.5; font-size:14px; }
.wrap { max-width:780px; margin:0 auto; padding:20px 24px 60px; }
header.title-band { background:linear-gradient(135deg,#1a2332 0%,#2c5282 100%); color:white;
  padding:24px 28px 20px; margin:-20px -24px 22px; border-bottom:3px solid #d69e2e; }
header.title-band h1 { margin:0 0 4px; font-size:22px; font-weight:700; }
header.title-band .meta { font-size:12px; opacity:0.85; }
h2 { color:var(--ink); font-size:17px; margin:22px 0 10px; padding-bottom:6px; border-bottom:2px solid var(--line); font-weight:600; }
section { background:var(--bg); border:1px solid var(--line); border-radius:5px; padding:6px 22px 18px; margin-bottom:14px; }
table { width:100%; border-collapse:collapse; font-size:13px; margin:6px 0 10px; }
th { text-align:left; background:var(--bg-card); color:var(--ink); font-weight:600; padding:7px 9px; border-bottom:2px solid var(--line); }
td { padding:7px 9px; border-bottom:1px solid var(--line); vertical-align:middle; }
tr:hover td { background:var(--bg-soft); }
th.num,td.num { text-align:right; font-variant-numeric:tabular-nums; }
.muted { color:var(--muted); }
.kpi-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin:10px 0 16px; }
.kpi { background:var(--bg-card); border:1px solid var(--line); border-left:4px solid var(--accent); padding:11px 13px; border-radius:4px; }
.kpi.good { border-left-color:var(--good); } .kpi.warn { border-left-color:var(--warn); } .kpi.bad { border-left-color:var(--bad); }
.kpi .label { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:0.4px; margin-bottom:3px; font-weight:600; }
.kpi .value { font-size:22px; font-weight:700; color:var(--ink); font-variant-numeric:tabular-nums; }
.kpi .sub { font-size:11px; color:var(--ink-soft); margin-top:2px; }
.callout { background:var(--good-soft); border-left:4px solid var(--good); padding:10px 14px; margin:10px 0; border-radius:4px; font-size:13px; }
.vs-target { font-weight:600; font-size:11px; }
.vs-target.good { color:var(--good); }
.vs-target.warn { color:var(--warn); }
.vs-target.bad  { color:var(--bad); }
footer { color:var(--muted); font-size:11px; padding-top:12px; margin-top:18px; border-top:1px solid var(--line); }
"""


# ── HTML render ──

def render_html(cfg: TenantConfig, target_date: str, day_interval: str,
                window_interval: str, headline: dict, worst: list[dict],
                flagged: list[dict], hotlist: list[dict],
                adherence: list[dict]) -> str:
    voice_sl_today = headline["voice_sl_today"]
    voice_sl_base = headline["voice_sl_baseline"]
    sl_cls = ("good" if voice_sl_today and voice_sl_today >= 80
              else "warn" if voice_sl_today and voice_sl_today >= 65
              else "bad")
    msg_sl_today = headline["message_sl_today"]
    msg_sl_base = headline["message_sl_baseline"]
    msg_cls = ("good" if msg_sl_today and msg_sl_today >= 80
               else "warn" if msg_sl_today and msg_sl_today >= 65
               else "bad")

    headline_html = (
        '<div class="kpi-grid">'
        f'<div class="kpi {sl_cls}"><div class="label">Voice SL</div>'
        f'<div class="value">{fmt_pct(voice_sl_today)}</div>'
        f'<div class="sub">vs rolling median {fmt_pct(voice_sl_base)} '
        f'{_vs_pill(voice_sl_today, voice_sl_base, units="pp")}</div></div>'
        f'<div class="kpi {msg_cls}"><div class="label">Message SL</div>'
        f'<div class="value">{fmt_pct(msg_sl_today)}</div>'
        f'<div class="sub">vs rolling median {fmt_pct(msg_sl_base)} '
        f'{_vs_pill(msg_sl_today, msg_sl_base, units="pp")}</div></div>'
        f'<div class="kpi"><div class="label">Total interactions</div>'
        f'<div class="value">{fmt_int(headline["total_offered_today"])}</div>'
        f'<div class="sub">voice + message offered</div></div>'
        '</div>'
    )

    # Worst routes
    if not worst:
        worst_html = (
            '<div class="callout"><strong>No queues with significant SL drop</strong> '
            f'(threshold {cfg.daily_brief.flag_thresholds.sl_drop_pp:.0f}pp vs rolling median). '
            "Routing looks consistent with the window baseline.</div>"
        )
    else:
        rows = "".join(
            f'<tr><td>{escape(r["queue_name"])}</td>'
            f'<td class="num">{fmt_pct(r["today_sl_pct"])}</td>'
            f'<td class="num">{fmt_pct(r["baseline_sl_pct"])}</td>'
            f'<td class="num"><span class="vs-target bad">-{r["drop_pp"]:.1f}pp</span></td>'
            f'<td class="num">{fmt_int(r["today_offered"])}</td></tr>'
            for r in worst
        )
        worst_html = (
            '<table><thead><tr><th>Queue</th>'
            '<th class="num">Today SL</th><th class="num">Rolling median</th>'
            '<th class="num">Drop</th><th class="num">Offered today</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    # Flagged agents
    if not flagged:
        flagged_html = (
            '<div class="callout"><strong>No agents flagged for AHT excess</strong> '
            f"(threshold +{cfg.daily_brief.flag_thresholds.aht_excess_pct:.0f}% over "
            f"target {cfg.targets.voice_aht_s}s, ≥5 voice calls).</div>"
        )
    else:
        rows = "".join(
            f'<tr><td>{escape(r["name"])}</td>'
            f'<td class="num">{fmt_int(r["voice_answered"])}</td>'
            f'<td class="num">{int(r["voice_aht_s"])}s</td>'
            f'<td class="num"><span class="vs-target bad">+{r["voice_aht_excess_pct"]:.0f}%</span></td>'
            f'<td class="num">{r["voice_excess_minutes"]:.0f} min</td></tr>'
            for r in flagged
        )
        flagged_html = (
            '<table><thead><tr><th>Agent</th>'
            '<th class="num">Voice ans.</th><th class="num">AHT</th>'
            '<th class="num">vs target</th><th class="num">Excess</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    # Hotlist
    if not hotlist:
        hotlist_html = (
            '<div class="callout"><strong>No unresolved repeaters from yesterday.</strong></div>'
        )
    else:
        rows = "".join(
            f'<tr><td><code style="font-size:11px">{escape(r["ani_masked"] or "")}</code></td>'
            f'<td class="num">{r["answered_count"]}</td>'
            f'<td class="num">{int((r["unresolved_share"] or 0) * 100)}%</td>'
            f'<td>{escape(r["recommended_action"] or "")}</td>'
            f'<td class="muted" style="font-size:12px">{escape(r["last_summary"])}</td></tr>'
            for r in hotlist
        )
        hotlist_html = (
            '<table><thead><tr><th>ANI</th><th class="num">Calls</th>'
            '<th class="num">Unresolved %</th><th>Action</th><th>Last summary</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    # Adherence
    if not adherence:
        adherence_html = (
            '<div class="callout"><strong>No adherence flags</strong> '
            "(no agents over 30 min combined break + pre-break + meal overrun).</div>"
        )
    else:
        rows = "".join(
            f'<tr><td>{escape(r["name"])}</td>'
            f'<td class="num">{r["pre_break_overrun_min"]:.0f}</td>'
            f'<td class="num">{r["break_overrun_min"]:.0f}</td>'
            f'<td class="num">{r["meal_overrun_min"]:.0f}</td>'
            f'<td class="num"><strong>{r["total_min"]:.0f}</strong></td></tr>'
            for r in adherence
        )
        adherence_html = (
            '<table><thead><tr><th>Agent</th>'
            '<th class="num">Pre-break</th><th class="num">Break</th>'
            '<th class="num">Meal</th><th class="num">Total min</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<title>{escape(cfg.tenant.name)} — Daily brief {target_date}</title>'
        f"<style>{CSS}</style></head><body><div class=\"wrap\">"
        f'<header class="title-band">'
        f'<h1>Daily brief — {target_date}</h1>'
        f'<div class="meta">{escape(cfg.tenant.name)} · '
        f'comparison window: {cfg.daily_brief.comparison_window_days} days · '
        f'thresholds: AHT +{cfg.daily_brief.flag_thresholds.aht_excess_pct:.0f}%, '
        f'SL drop {cfg.daily_brief.flag_thresholds.sl_drop_pp:.0f}pp</div>'
        f'</header>'
        f'<section><h2>1. Headline KPIs</h2>{headline_html}</section>'
        f'<section><h2>2. Worst routes</h2>{worst_html}</section>'
        f'<section><h2>3. Flagged agents (voice AHT)</h2>{flagged_html}</section>'
        f'<section><h2>4. Repeat-caller callback list</h2>{hotlist_html}</section>'
        f'<section><h2>5. Adherence flags</h2>{adherence_html}</section>'
        f'<footer>Generated {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")} · '
        f"genesys-mcp cc-daily-brief · "
        f"day: {day_interval} · window: {window_interval}</footer>"
        f'</div></body></html>'
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--target-date", required=True, help="YYYY-MM-DD of the brief's target day")
    p.add_argument("--day-interval", required=True, help="ISO interval for the target day")
    p.add_argument("--window-interval", required=True, help="ISO interval for the rolling-N-day baseline")
    p.add_argument("--data-dir", required=True)
    p.add_argument("--output", default=None,
                   help="Output HTML path (defaults to cfg.daily_brief_output_path)")
    args = p.parse_args()

    cfg = load_config()
    data_dir = Path(args.data_dir)

    qp_day = json.loads((data_dir / "queue_perf_day.json").read_text())
    qp_window = json.loads((data_dir / "queue_perf_window.json").read_text())
    ap_day = json.loads((data_dir / "agent_perf_day.json").read_text())
    deep = json.loads((data_dir / "repeat_callers.json").read_text())
    brk = json.loads((data_dir / "break_overrun.json").read_text())

    # Optional lookup maps for prettier output (queue/user names). The skill
    # workflow can save these alongside the data; if missing we fall back to
    # id-prefixes so the brief still renders.
    qmap_path = data_dir / "qmap.json"
    qmap = json.loads(qmap_path.read_text()) if qmap_path.exists() else {}
    user_names_path = data_dir / "user_names.json"
    user_names = json.loads(user_names_path.read_text()) if user_names_path.exists() else {}

    day_kpis = aggregate_queue_kpis(qp_day, qmap)
    window_kpis = aggregate_queue_kpis(qp_window, qmap)
    headline = headline_kpis(day_kpis, window_kpis)
    worst = worst_routes(day_kpis, window_kpis,
                         cfg.daily_brief.flag_thresholds.sl_drop_pp)
    flagged = flagged_agents(ap_day, cfg.targets.voice_aht_s,
                             cfg.daily_brief.flag_thresholds.aht_excess_pct,
                             user_names=user_names)
    hotlist = repeat_caller_hotlist(deep)
    adherence = adherence_flags(brk, user_names=user_names)

    html = render_html(cfg, args.target_date, args.day_interval,
                       args.window_interval, headline, worst, flagged,
                       hotlist, adherence)

    out_path = (Path(args.output).expanduser() if args.output
                else cfg.daily_brief_output_path(args.target_date))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    print(f"OK wrote {out_path} ({len(html):,} bytes)")
    print(f"   voice SL today: {fmt_pct(headline['voice_sl_today'])} "
          f"(vs rolling median {fmt_pct(headline['voice_sl_baseline'])})")
    print(f"   {len(worst)} flagged routes, {len(flagged)} flagged agents, "
          f"{len(hotlist)} callback candidates, {len(adherence)} adherence flags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
