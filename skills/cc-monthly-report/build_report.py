#!/usr/bin/env python3
"""Build the Prvidr Contact-Centre HTML report from raw genesys-mcp JSON dumps.

Usage:
    python build_report.py \\
        --period "April 2026" \\
        --interval "2026-03-31T14:00:00.000Z/2026-04-30T14:00:00.000Z" \\
        --data-dir /tmp/cc-report-april-2026 \\
        --qmap-json /tmp/cc-report-april-2026/qmap.json \\
        --user-roles-json /tmp/cc-report-april-2026/user_roles.json \\
        --output ~/Documents/Prvidr-CC-april-2026.html

The data directory must contain four JSON files — one per MCP tool result text payload:
    queue_performance.json
    agent_performance.json
    break_overrun_report.json
    repeat_caller_deep_dive.json

Each file is the parsed result.text (the inner JSON) from a `mcp__genesys__*` call. The
qmap.json maps queueId → [brand, queue_name]; user_roles.json maps userId → [name, role].
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


# ---------- helpers ----------

def fmt_secs(seconds: float | int | None) -> str:
    if seconds is None or seconds == 0:
        return "—"
    s = int(round(seconds))
    return f"{s//60}m {s%60:02d}s" if s >= 60 else f"{s}s"


def fmt_int(n: int | float | None) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}"


def fmt_pct(pct: float | None, dp: int = 1) -> str:
    if pct is None:
        return "—"
    return f"{pct:.{dp}f}%"


def bar_class(pct: float | None, good_at: float = 80, warn_at: float = 50) -> str:
    if pct is None:
        return "neutral"
    if pct >= good_at:
        return "good"
    if pct >= warn_at:
        return "warn"
    return "bad"


def bar(pct: float | None, label: str | None = None, good_at: float = 80, warn_at: float = 50) -> str:
    if pct is None:
        return f'<div class="bar-cell"><div class="bar neutral"><span style="width:0%"></span></div><div class="bar-label">—</div></div>'
    cls = bar_class(pct, good_at, warn_at)
    width = max(0, min(100, pct))
    text = label or f"{pct:.1f}%"
    return f'<div class="bar-cell"><div class="bar {cls}"><span style="width:{width:.1f}%"></span></div><div class="bar-label">{text}</div></div>'


# ---------- aggregation ----------

def aggregate_queue_performance(qp: dict, qmap: dict[str, list[str]]) -> dict:
    """Aggregate queue_performance response by brand × media using derived.answered."""
    brand_agg: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "offered": 0, "answered": 0, "abandoned": 0, "over_sla": 0,
        "h_sum": 0.0, "h_n": 0, "w_sum": 0.0, "w_n": 0,
    })
    per_queue: list[dict] = []

    for grp in qp.get("results") or []:
        gk = grp.get("group") or {}
        qid, media = gk.get("queueId"), gk.get("mediaType")
        if qid not in qmap or media not in ("voice", "message"):
            continue
        brand, qname = qmap[qid]
        for bucket in grp.get("data") or []:
            d = bucket.get("derived") or {}
            ms = {m["metric"]: (m.get("stats") or {}) for m in (bucket.get("metrics") or [])}
            offered = ms.get("nOffered", {}).get("count", 0) or 0
            answered = d.get("answered") or 0
            abandoned = d.get("abandoned") or 0
            over = ms.get("nOverSla", {}).get("count", 0) or 0
            ans_pct = (answered / offered * 100) if offered else None
            sl_pct = d.get("service_level_pct")
            avg_wait = d.get("avg_wait_s")
            avg_handle = d.get("avg_handle_s")
            per_queue.append({
                "brand": brand, "queue": qname, "media": media,
                "offered": offered, "answered": answered, "abandoned": abandoned,
                "over_sla": over, "ans_pct": ans_pct, "sl_pct": sl_pct,
                "avg_wait_s": avg_wait, "avg_handle_s": avg_handle,
            })
            agg = brand_agg[(brand, media)]
            agg["offered"] += offered
            agg["answered"] += answered
            agg["abandoned"] += abandoned
            agg["over_sla"] += over
            for f, mname in [("h", "tHandle"), ("w", "tWait")]:
                s = ms.get(mname, {})
                agg[f + "_sum"] += s.get("sum", 0) or 0
                agg[f + "_n"] += s.get("count", 0) or 0

    brand_rows = []
    for (brand, media), a in sorted(brand_agg.items()):
        offered = a["offered"]
        ans_pct = (a["answered"] / offered * 100) if offered else None
        sl_pct = (max(a["answered"] - a["over_sla"], 0) / offered * 100) if offered else None
        avg_wait = (a["w_sum"] / a["w_n"] / 1000) if a["w_n"] else None
        avg_handle = (a["h_sum"] / a["h_n"] / 1000) if a["h_n"] else None
        brand_rows.append({
            "brand": brand, "media": media,
            "offered": offered, "answered": a["answered"], "abandoned": a["abandoned"],
            "ans_pct": ans_pct, "sl_pct": sl_pct,
            "avg_wait_s": avg_wait, "avg_handle_s": avg_handle,
        })
    return {"brand_rows": brand_rows, "per_queue": per_queue}


SPECIALIST_ROLES = {"Specialist", "Customer Service Specialist"}

# Org targets — total Genesys tHandle per interaction (talk + hold + ACW combined).
# ACW target is included within the AHT figures; we surface it separately for
# coaching context.
VOICE_AHT_TARGET_S = 285
MSG_AHT_TARGET_S = 660
ACW_TARGET_S = 15
# 1 FTE = ~160 productive (handle-time) hours per month — typical contact-centre
# planning assumption (40h/wk × 4 weeks × 0.85 occupancy = ~136h, rounded up).
FTE_HOURS_PER_MONTH = 160


def _vs_target_pct(actual: float | None, target: float) -> float | None:
    if actual is None or target == 0:
        return None
    return round((actual - target) / target * 100, 1)


def aggregate_agents(ap: dict, brk: dict, user_roles: dict[str, list[str]],
                     specialist_only: bool = True) -> list[dict]:
    """Merge agent_performance + break_overrun_report into per-agent rows.

    Excludes email entirely from totals; splits AHT into voice and message.
    By default excludes Team Leaders / Managers / Senior TLs — leadership has
    different productivity expectations and including them in a peer ranking
    is misleading.
    """
    by_uid_perf = {s["user_id"]: s for s in ap.get("summary") or []}
    by_uid_brk = {u["user_id"]: u for u in brk.get("users") or []}
    rows = []
    for uid, (name, role) in user_roles.items():
        if specialist_only and role not in SPECIALIST_ROLES:
            continue
        ps = by_uid_perf.get(uid, {})
        bs = by_uid_brk.get(uid, {})
        m = ps.get("by_media", {}) or {}
        v = m.get("voice") or {}
        msg = m.get("message") or {}
        cb = m.get("callback") or {}
        ans_no_email = (v.get("answered", 0) or 0) + (msg.get("answered", 0) or 0) + (cb.get("answered", 0) or 0)
        h_min_no_email = (
            (v.get("total_handle_min", 0) or 0)
            + (msg.get("total_handle_min", 0) or 0)
            + (cb.get("total_handle_min", 0) or 0)
        )
        voice_aht = v.get("avg_handle_s")
        msg_aht = msg.get("avg_handle_s")
        v_ans = v.get("answered", 0) or 0
        m_ans = msg.get("answered", 0) or 0
        # Excess handle minutes vs target — only count agents over target (negative
        # excess from being faster than target is good but doesn't add capacity to
        # the team's coachable lever).
        v_excess_min = max((voice_aht - VOICE_AHT_TARGET_S), 0) * v_ans / 60 if voice_aht else 0
        m_excess_min = max((msg_aht - MSG_AHT_TARGET_S), 0) * m_ans / 60 if msg_aht else 0

        rows.append({
            "name": name, "role": role,
            "answered": ans_no_email,
            "voice_ans": v_ans,
            "msg_ans": m_ans,
            "voice_aht_s": voice_aht,
            "msg_aht_s": msg_aht,
            "voice_aht_vs_target_pct": _vs_target_pct(voice_aht, VOICE_AHT_TARGET_S),
            "msg_aht_vs_target_pct": _vs_target_pct(msg_aht, MSG_AHT_TARGET_S),
            "voice_excess_min": round(v_excess_min, 1),
            "msg_excess_min": round(m_excess_min, 1),
            "total_excess_min": round(v_excess_min + m_excess_min, 1),
            "avg_acw_s": ps.get("avg_acw_s"),
            "acw_vs_target_pct": _vs_target_pct(ps.get("avg_acw_s"), ACW_TARGET_S),
            "total_handle_h": round(h_min_no_email / 60, 1),
            "break_sessions": bs.get("total_sessions", 0),
            "overruns": bs.get("overrun_count", 0),
            "overrun_min": bs.get("total_overrun_min", 0),
            "away_count": bs.get("away_count", 0),
            "away_min": bs.get("away_total_min", 0),
            "pre_break_overrun_count": bs.get("pre_break_overrun_count", 0),
            "pre_break_overrun_min": bs.get("pre_break_overrun_total_min", 0),
        })
    rows.sort(key=lambda r: -(r["answered"] or 0))
    return rows


def aggregate_daily_voice_sl(qp_daily: dict, qmap: dict[str, list[str]]) -> list[dict]:
    """Aggregate daily queue_performance into one org-wide voice SL row per day.

    Returns ``[{date, offered, answered, over_sla, sl_pct, ans_pct}]`` sorted by date.
    """
    by_date: dict[str, dict] = defaultdict(lambda: {"offered": 0, "answered": 0, "over_sla": 0})
    for grp in qp_daily.get("results") or []:
        gk = grp.get("group") or {}
        if gk.get("queueId") not in qmap or gk.get("mediaType") != "voice":
            continue
        for bucket in grp.get("data") or []:
            interval = bucket.get("interval") or ""
            # interval like "2026-04-01T00:00:00.000Z/2026-04-02T00:00:00.000Z"
            day = interval.split("T", 1)[0] if interval else None
            if not day:
                continue
            d = bucket.get("derived") or {}
            ms = {m["metric"]: (m.get("stats") or {}) for m in (bucket.get("metrics") or [])}
            offered = ms.get("nOffered", {}).get("count", 0) or 0
            answered = d.get("answered") or 0
            over = ms.get("nOverSla", {}).get("count", 0) or 0
            row = by_date[day]
            row["offered"] += offered
            row["answered"] += answered
            row["over_sla"] += over

    out = []
    for day, r in sorted(by_date.items()):
        offered = r["offered"]
        sl_pct = (max(r["answered"] - r["over_sla"], 0) / offered * 100) if offered else None
        ans_pct = (r["answered"] / offered * 100) if offered else None
        out.append({
            "date": day,
            "offered": offered,
            "answered": r["answered"],
            "over_sla": r["over_sla"],
            "sl_pct": sl_pct,
            "ans_pct": ans_pct,
        })
    return out


def aggregate_staffing(wfm: dict | None) -> dict | None:
    """Roll up wfm_schedule's daily series into headline staffing-vs-demand stats.

    Returns ``None`` if no WFM data was supplied.
    """
    if not wfm or not wfm.get("daily"):
        return None
    daily = wfm["daily"]
    totals = wfm.get("totals") or {}
    over_days = [d for d in daily if (d["gap_hours"] or 0) <= 0]   # scheduled >= required
    under_days = [d for d in daily if (d["gap_hours"] or 0) > 0]   # scheduled < required
    peak_shortfall_h = sum(d["gap_hours"] for d in under_days)
    excess_h = -sum(d["gap_hours"] for d in over_days)             # positive = surplus
    worst_day = max(daily, key=lambda d: d["gap_hours"] or 0) if daily else None
    return {
        "total_scheduled_h": totals.get("scheduled_hours", 0),
        "total_required_h": totals.get("required_hours", 0),
        "total_net_gap_h": totals.get("gap_hours", 0),  # positive = under (need more)
        "under_days_count": len(under_days),
        "over_days_count": len(over_days),
        "peak_shortfall_h": round(peak_shortfall_h, 1),
        "aggregate_excess_h": round(excess_h, 1),
        "worst_day": worst_day,
        "daily": daily,
    }


def compute_performance_leverage(workforce: list[dict], deep: dict, brand_rows: list[dict]) -> dict:
    """Quantify how many handle hours could be freed up if (a) every agent hit AHT
    targets and (b) every repeat caller had been resolved on the first call.

    Returns the headline numbers plus the top contributors for each.
    """
    # AHT phantom capacity — sum of voice + message excess minutes across the team
    aht_excess_min = sum(r["total_excess_min"] for r in workforce)
    aht_excess_h = aht_excess_min / 60
    aht_excess_fte = aht_excess_h / FTE_HOURS_PER_MONTH

    top_aht_offenders = sorted(
        [r for r in workforce if r["total_excess_min"] > 0],
        key=lambda r: -r["total_excess_min"],
    )[:5]

    # Org-average voice AHT (used to value each "wasted" repeat call)
    voice_offered = sum(r["offered"] for r in brand_rows if r["media"] == "voice")
    voice_handle_total_s = 0.0
    voice_count = 0
    for r in brand_rows:
        if r["media"] == "voice" and r.get("avg_handle_s") and r["answered"]:
            voice_handle_total_s += r["avg_handle_s"] * r["answered"]
            voice_count += r["answered"]
    org_voice_aht_s = voice_handle_total_s / voice_count if voice_count else None

    # FCR drag — for each repeater in the deep-dive, the (answered_count - 1) is the
    # number of "extra" answered calls. Multiply by org_voice_aht to value as wasted
    # handle minutes. Cap at acd_offered - 1 to ignore abandons (which didn't
    # consume handle time).
    fcr_drag_min = 0.0
    fcr_top: list[dict] = []
    repeaters = deep.get("repeaters") or []
    for rep in repeaters:
        offered = rep.get("acd_offered_count", 0) or 0
        answered = rep.get("answered_count", 0) or 0
        unresolved = rep.get("unresolved_share")
        if answered <= 1 or unresolved is None or unresolved == 0:
            continue
        # Extra answered calls = answered - 1 (the first one was the legit attempt)
        extra = max(answered - 1, 0)
        # Each extra answered call consumed ~org_voice_aht seconds
        if org_voice_aht_s is None:
            continue
        drag_min = extra * org_voice_aht_s / 60 * unresolved  # weight by unresolved share
        fcr_drag_min += drag_min
        fcr_top.append({
            "ani": rep.get("ani"),
            "extra_calls": extra,
            "unresolved_share": unresolved,
            "drag_min": round(drag_min, 1),
        })
    fcr_top.sort(key=lambda x: -x["drag_min"])

    fcr_drag_h = fcr_drag_min / 60
    fcr_drag_fte = fcr_drag_h / FTE_HOURS_PER_MONTH

    total_h = aht_excess_h + fcr_drag_h
    total_fte = total_h / FTE_HOURS_PER_MONTH

    return {
        "aht_excess_h": round(aht_excess_h, 1),
        "aht_excess_fte": round(aht_excess_fte, 2),
        "fcr_drag_h": round(fcr_drag_h, 1),
        "fcr_drag_fte": round(fcr_drag_fte, 2),
        "total_recoverable_h": round(total_h, 1),
        "total_recoverable_fte": round(total_fte, 2),
        "org_voice_aht_s": round(org_voice_aht_s, 1) if org_voice_aht_s else None,
        "top_aht_offenders": top_aht_offenders,
        "top_fcr_repeaters": fcr_top[:5],
    }


def extract_themes(deep: dict) -> dict:
    org = deep.get("org_rollup") or {}
    return {
        "scope": deep.get("scope") or {},
        "top_dispositions": (org.get("top_dispositions") or [])[:10],
        "top_ai_outcomes": org.get("top_ai_outcomes") or {},
        "top_expected_fixes": (org.get("top_expected_fixes") or [])[:10],
        "unresolved_repeaters": (org.get("unresolved_repeaters") or [])[:15],
        "recommended_actions": org.get("recommended_actions") or {},
        "sentiment_trends": org.get("sentiment_trends") or {},
        "repeaters_top": (deep.get("repeaters") or [])[:15],
    }


# ---------- HTML rendering ----------

CSS = """
:root {
  --ink:#1a2332;--ink-soft:#4a5568;--muted:#718096;--line:#e2e8f0;
  --bg:#ffffff;--bg-soft:#f7fafc;--bg-card:#fafbfc;
  --accent:#2c5282;--accent-soft:#ebf4fc;
  --good:#2f855a;--good-soft:#f0fff4;
  --warn:#c05621;--warn-soft:#fffaf0;
  --bad:#c53030;--bad-soft:#fff5f5;
  --neutral-bar:#cbd5e0;
}
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  color:var(--ink); background:var(--bg-soft); margin:0; line-height:1.55; font-size:14px; }
.wrap { max-width:1100px; margin:0 auto; padding:28px 32px 80px; }
header.title-band { background:linear-gradient(135deg,#1a2332 0%,#2c5282 100%); color:white;
  padding:36px 32px 28px; margin:-28px -32px 28px; border-bottom:4px solid #d69e2e; }
header.title-band h1 { margin:0 0 6px; font-size:28px; font-weight:700; letter-spacing:-0.5px; }
header.title-band .meta { font-size:13px; opacity:0.85; }
header.title-band .meta strong { color:#f6ad55; font-weight:600; }
nav.toc { background:var(--bg-card); border:1px solid var(--line); border-radius:6px;
  padding:14px 22px; margin-bottom:28px; font-size:13px; }
nav.toc strong { color:var(--ink); }
nav.toc a { display:inline-block; color:var(--accent); text-decoration:none; margin-right:14px; padding:2px 0; }
nav.toc a:hover { text-decoration:underline; }
h2 { color:var(--ink); font-size:20px; margin:36px 0 14px; padding-bottom:8px; border-bottom:2px solid var(--line); font-weight:600; }
h3 { color:var(--ink); font-size:16px; margin:22px 0 10px; font-weight:600; }
section { background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:8px 26px 24px; margin-bottom:22px; }
p { margin:8px 0 14px; }
ul,ol { margin:8px 0 14px; padding-left:22px; }
li { margin-bottom:6px; }
table { width:100%; border-collapse:collapse; font-size:13px; margin:12px 0 18px; }
th { text-align:left; background:var(--bg-card); color:var(--ink); font-weight:600; padding:9px 10px; border-bottom:2px solid var(--line); }
td { padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:middle; }
tr:hover td { background:var(--bg-soft); }
th.num,td.num { text-align:right; font-variant-numeric:tabular-nums; }
td.muted { color:var(--muted); }
.pill { display:inline-block; padding:2px 9px; border-radius:11px; font-size:11px; font-weight:600; letter-spacing:0.2px; text-transform:uppercase; }
.pill.good { background:var(--good-soft); color:var(--good); }
.pill.warn { background:var(--warn-soft); color:var(--warn); }
.pill.bad  { background:var(--bad-soft); color:var(--bad); }
.bar-cell { position:relative; min-width:120px; }
.bar { position:relative; height:18px; background:#edf2f7; border-radius:3px; overflow:hidden; }
.bar > span { display:block; height:100%; }
.bar.good > span { background:linear-gradient(90deg,#38a169,#48bb78); }
.bar.warn > span { background:linear-gradient(90deg,#dd6b20,#ed8936); }
.bar.bad > span { background:linear-gradient(90deg,#c53030,#e53e3e); }
.bar.neutral > span { background:var(--neutral-bar); }
.bar-label { font-size:11px; color:var(--ink-soft); margin-top:2px; font-variant-numeric:tabular-nums; }
.kpi-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; margin:16px 0 22px; }
.kpi { background:var(--bg-card); border:1px solid var(--line); border-left:4px solid var(--accent); padding:14px 16px; border-radius:4px; }
.kpi.good { border-left-color:var(--good); } .kpi.warn { border-left-color:var(--warn); } .kpi.bad { border-left-color:var(--bad); }
.kpi .label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:4px; font-weight:600; }
.kpi .value { font-size:24px; font-weight:700; color:var(--ink); font-variant-numeric:tabular-nums; }
.kpi .sub { font-size:12px; color:var(--ink-soft); margin-top:2px; }
.callout { background:var(--accent-soft); border-left:4px solid var(--accent); padding:12px 16px; margin:14px 0; border-radius:4px; font-size:13px; }
.callout.warn { background:var(--warn-soft); border-left-color:var(--warn); }
.callout.bad { background:var(--bad-soft); border-left-color:var(--bad); }
.callout strong { color:var(--ink); }
.vs-target { font-weight:600; font-size:11px; margin-left:3px; }
.vs-target.good { color:var(--good); }
.vs-target.warn { color:var(--warn); }
.vs-target.bad  { color:var(--bad); }
.trend-chart { position:relative; height:240px; margin:18px 0 8px; padding:0 0 22px; border-left:2px solid var(--line); border-bottom:2px solid var(--line); }
.trend-chart .target-line { position:absolute; left:0; right:0; top:20%; border-top:1px dashed var(--good); pointer-events:none; }
.trend-chart .target-line::after { content:"80% target"; position:absolute; right:0; top:-7px; font-size:10px; color:var(--good); background:white; padding:0 4px; }
.trend-chart .y-axis { position:absolute; left:-30px; top:0; bottom:22px; width:28px; font-size:10px; color:var(--muted); }
.trend-chart .y-axis span { position:absolute; right:4px; transform:translateY(-50%); }
.trend-chart .y-axis .y-100 { top:0; } .trend-chart .y-axis .y-80 { top:20%; } .trend-chart .y-axis .y-50 { top:50%; } .trend-chart .y-axis .y-0 { top:100%; }
.trend-chart .bars { display:flex; gap:2px; height:100%; padding:6px 0 0; }
.trend-chart .bar { flex:1 1 0; position:relative; min-width:6px; height:100%; background:transparent; border-radius:2px 2px 0 0; }
.trend-chart .bar > .fill { position:absolute; bottom:0; left:0; right:0; border-radius:2px 2px 0 0; }
.trend-chart .bar.good > .fill { background:linear-gradient(180deg,#48bb78,#2f855a); }
.trend-chart .bar.warn > .fill { background:linear-gradient(180deg,#ed8936,#c05621); }
.trend-chart .bar.bad > .fill { background:linear-gradient(180deg,#e53e3e,#c53030); }
.trend-chart .bar:hover::after { content:attr(data-tip); position:absolute; bottom:100%; left:50%; transform:translateX(-50%); background:var(--ink); color:white; padding:4px 7px; border-radius:3px; font-size:11px; white-space:nowrap; z-index:1; }
.trend-chart .x-axis { position:absolute; left:0; right:0; bottom:0; height:18px; display:flex; gap:2px; font-size:10px; color:var(--muted); }
.trend-chart .x-axis span { flex:1 1 0; text-align:center; min-width:6px; }
footer { color:var(--muted); font-size:12px; padding-top:18px; margin-top:28px; border-top:1px solid var(--line); }
@media print { body{background:white;} .wrap{padding:0 12mm; max-width:none;} nav.toc{display:none;} section{page-break-inside:avoid;} h2{page-break-after:avoid;} a{color:inherit; text-decoration:none;} }
"""


def render_daily_sl_chart(daily: list[dict]) -> str:
    """Render a vertical bar chart of daily voice service-level %, with 80% target line."""
    if not daily:
        return '<p style="color:var(--muted);font-size:13px;">No daily data available.</p>'
    bars = []
    x_labels = []
    days_per_label = max(1, len(daily) // 10)  # show ~10 date labels along the x-axis
    for i, d in enumerate(daily):
        sl = d["sl_pct"] or 0
        if d["sl_pct"] is None:
            cls, height = "neutral", 0
        elif sl >= 80:
            cls, height = "good", sl
        elif sl >= 50:
            cls, height = "warn", sl
        else:
            cls, height = "bad", sl
        tip = f"{d['date']}: SL {sl:.0f}% — {d['answered']:,} of {d['offered']:,} answered"
        bars.append(f'<div class="bar {cls}" data-tip="{tip}"><div class="fill" style="height:{height:.1f}%"></div></div>')
        # Day-of-month label for every Nth bar
        day_num = d["date"].split("-")[-1] if d["date"] else ""
        x_labels.append(f'<span>{day_num if i % days_per_label == 0 else ""}</span>')

    # Stats
    valid_sl = [d["sl_pct"] for d in daily if d["sl_pct"] is not None]
    if valid_sl:
        avg = sum(valid_sl) / len(valid_sl)
        days_above = sum(1 for x in valid_sl if x >= 80)
        days_below_50 = sum(1 for x in valid_sl if x < 50)
        worst = min(daily, key=lambda d: d["sl_pct"] if d["sl_pct"] is not None else 999)
        best = max(daily, key=lambda d: d["sl_pct"] if d["sl_pct"] is not None else -1)
        stats_html = (
            f'<div style="display:flex;gap:18px;font-size:13px;color:var(--ink-soft);margin-top:12px;flex-wrap:wrap;">'
            f'<span><strong>Avg:</strong> {avg:.1f}%</span>'
            f'<span><strong>Days at/above 80%:</strong> {days_above} / {len(valid_sl)}</span>'
            f'<span><strong>Days under 50%:</strong> {days_below_50}</span>'
            f'<span><strong>Best:</strong> {best["date"]} ({best["sl_pct"]:.0f}%)</span>'
            f'<span><strong>Worst:</strong> {worst["date"]} ({worst["sl_pct"]:.0f}%)</span>'
            f'</div>'
        )
    else:
        stats_html = ""

    return f"""<div class="trend-chart">
  <div class="y-axis">
    <span class="y-100">100%</span><span class="y-80">80%</span>
    <span class="y-50">50%</span><span class="y-0">0%</span>
  </div>
  <div class="target-line"></div>
  <div class="bars">{''.join(bars)}</div>
  <div class="x-axis">{''.join(x_labels)}</div>
</div>
{stats_html}"""


def render_brand_table(brand_rows: list[dict]) -> str:
    rows_html = []
    for r in brand_rows:
        ans_pct = r["ans_pct"]
        sl_pct = r["sl_pct"]
        offered = r["offered"]
        answered = r["answered"]
        abandoned = r["abandoned"] if r["media"] != "message" else None
        wait = fmt_secs(r["avg_wait_s"]) if r["avg_wait_s"] else "—"
        rows_html.append(f"""<tr>
  <td><strong>{r['brand']}</strong></td><td>{r['media']}</td>
  <td class="num">{fmt_int(offered)}</td><td class="num">{fmt_int(answered)}</td>
  <td>{bar(ans_pct, good_at=85, warn_at=70)}</td>
  <td class="num">{fmt_int(abandoned) if abandoned is not None else '—'}</td>
  <td>{bar(sl_pct, label=f"{sl_pct:.1f}%" if sl_pct is not None else "—", good_at=80, warn_at=60)}</td>
  <td class="num">{wait}</td>
</tr>""")
    return f"""<table>
  <thead><tr><th>Brand</th><th>Channel</th><th class="num">Offered</th><th class="num">Answered</th>
    <th>Answer rate</th><th class="num">Abandoned</th><th>Service Level</th><th class="num">Avg wait</th></tr></thead>
  <tbody>{''.join(rows_html)}</tbody>
</table>"""


def render_queue_table(per_queue: list[dict], media: str, n: int = 10) -> str:
    items = sorted([q for q in per_queue if q["media"] == media], key=lambda x: -x["offered"])[:n]
    rows_html = []
    for q in items:
        rows_html.append(f"""<tr>
  <td>{q['brand']}</td><td>{q['queue']}</td>
  <td class="num">{fmt_int(q['offered'])}</td><td class="num">{fmt_int(q['answered'])}</td>
  <td>{bar(q['ans_pct'], good_at=85, warn_at=70)}</td>
  <td>{bar(q['sl_pct'], label=f"{q['sl_pct']:.1f}%" if q['sl_pct'] is not None else '—', good_at=80, warn_at=60)}</td>
  <td class="num">{fmt_secs(q['avg_wait_s']) if media=='voice' else '—'}</td>
</tr>""")
    return f"""<table>
  <thead><tr><th>Brand</th><th>Queue</th><th class="num">Offered</th><th class="num">Answered</th>
    <th>Answer rate</th><th>Service Level</th><th class="num">Avg wait</th></tr></thead>
  <tbody>{''.join(rows_html)}</tbody>
</table>"""


def _vs_target_pill(pct: float | None) -> str:
    """Render a vs-target % as a colour-coded pill. Negative or zero = good (under target),
    0–20% over = warn, >20% over = bad. None = em-dash."""
    if pct is None:
        return '<span class="muted">—</span>'
    sign = "+" if pct > 0 else ""
    if pct <= 0:
        cls = "good"
    elif pct <= 20:
        cls = "warn"
    else:
        cls = "bad"
    return f'<span class="pill {cls}">{sign}{pct:.0f}%</span>'


def _aht_with_target(aht_s: float | None, vs_pct: float | None) -> str:
    """Render '<aht>s (<+pct>)' with the % colour-coded inline. Compact for tables."""
    if aht_s is None:
        return '<span class="muted">—</span>'
    if vs_pct is None:
        return f'{int(aht_s)}s'
    sign = "+" if vs_pct > 0 else ""
    cls = "good" if vs_pct <= 0 else ("warn" if vs_pct <= 20 else "bad")
    return f'{int(aht_s)}s <span class="vs-target {cls}">{sign}{vs_pct:.0f}%</span>'


def _acw_with_target(acw_s: float | None, vs_pct: float | None) -> str:
    """ACW with vs-target inline. Different colour bands than AHT (ACW target is 15s
    so an over-pct is normal; we colour on absolute seconds rather than % over)."""
    if acw_s is None:
        return '<span class="muted">—</span>'
    cls = "good" if acw_s <= ACW_TARGET_S else ("warn" if acw_s <= 60 else "bad")
    if vs_pct is None or vs_pct <= 0:
        return f'<span class="vs-target {cls}">{int(acw_s)}s</span>'
    return f'{int(acw_s)}s <span class="vs-target {cls}">+{vs_pct:.0f}%</span>'


def _count_and_min_cell(count: int, minutes: float, sessions_known: bool = True,
                        warn_at: int = 5, bad_at: int = 7) -> str:
    """Compact 'NN / MM min' cell with the count colour-coded.

    sessions_known=False → render '—' instead of '0 / 0' for the no-data case
    (used when an agent has no break sessions tracked at all).
    """
    if not sessions_known:
        return '<span class="muted">—</span>'
    if count >= bad_at:
        count_html = f'<span class="pill bad">{count}</span>'
    elif count >= warn_at:
        count_html = f'<span class="pill warn">{count}</span>'
    elif count == 0:
        count_html = '<span class="pill good">0</span>'
    else:
        count_html = str(count)
    return f'{count_html} / {minutes} min'


def render_workforce_table(rows: list[dict]) -> str:
    body = []
    for r in rows:
        bg = ""
        if rows and r is rows[0] and r["answered"] > 0:
            bg = ' style="background:var(--good-soft)"'
        elif r["overruns"] >= 7 and r["answered"] < 600:
            bg = ' style="background:var(--bad-soft)"'

        sessions_known = r["break_sessions"] > 0
        v_aht_cell = _aht_with_target(r["voice_aht_s"], r.get("voice_aht_vs_target_pct"))
        m_aht_cell = _aht_with_target(r["msg_aht_s"], r.get("msg_aht_vs_target_pct"))
        acw_cell = _acw_with_target(r.get("avg_acw_s"), r.get("acw_vs_target_pct"))
        br_cell = _count_and_min_cell(r["overruns"], r["overrun_min"], sessions_known)
        away_cell = _count_and_min_cell(r["away_count"], r["away_min"], sessions_known,
                                         warn_at=20, bad_at=50)
        pb_cell = _count_and_min_cell(r["pre_break_overrun_count"], r["pre_break_overrun_min"],
                                       sessions_known, warn_at=15, bad_at=25)

        body.append(f"""<tr{bg}>
  <td>{r['name']}</td><td>{r['role']}</td>
  <td class="num"><strong>{fmt_int(r['answered'])}</strong></td>
  <td class="num">{fmt_int(r['voice_ans'])}</td>
  <td class="num">{fmt_int(r['msg_ans'])}</td>
  <td class="num">{v_aht_cell}</td>
  <td class="num">{m_aht_cell}</td>
  <td class="num">{acw_cell}</td>
  <td class="num">{r['total_handle_h']}</td>
  <td class="num">{br_cell}</td>
  <td class="num">{away_cell}</td>
  <td class="num">{pb_cell}</td>
</tr>""")
    return f"""<table>
  <thead><tr>
    <th>Agent</th><th>Role</th>
    <th class="num">Answered</th><th class="num">Voice</th><th class="num">Msg</th>
    <th class="num">Voice AHT</th>
    <th class="num">Msg AHT</th>
    <th class="num">ACW</th>
    <th class="num">Total handle (h)</th>
    <th class="num">Br overruns</th>
    <th class="num">Away</th>
    <th class="num">Pre-br overruns</th>
  </tr></thead>
  <tbody>{''.join(body)}</tbody>
</table>"""


def render_dispositions_table(dispositions: list[dict]) -> str:
    rows_html = []
    for i, d in enumerate(dispositions, 1):
        rows_html.append(f'<tr><td class="num">{i}</td><td>{d.get("disposition")}</td><td class="num">{d.get("count")}</td></tr>')
    return f'<table><thead><tr><th class="num">#</th><th>Disposition</th><th class="num">Calls</th></tr></thead><tbody>{"".join(rows_html)}</tbody></table>'


def render_outcomes_table(outcomes: dict) -> str:
    total = sum(outcomes.values()) or 1
    rows_html = []
    for k, v in sorted(outcomes.items(), key=lambda kv: -kv[1]):
        share_pct = v / total * 100
        cls = "good" if k == "Resolved" else ("bad" if k in ("Unresolved Chat",) else "warn")
        rows_html.append(f'<tr><td>{k}</td><td class="num">{v}</td><td>{bar(share_pct, label=f"{share_pct:.0f}%", good_at=50, warn_at=20)}</td></tr>')
    return f'<table><thead><tr><th>Outcome</th><th class="num">Calls</th><th>Share</th></tr></thead><tbody>{"".join(rows_html)}</tbody></table>'


def render_unresolved_table(unresolved: list[dict]) -> str:
    rows_html = []
    for u in unresolved:
        share = u.get("unresolved_share")
        share_str = f"{int(share*100)}%" if share is not None else "—"
        pill_cls = "bad" if share == 1.0 else "warn"
        outcomes_str = ", ".join(f"{k}: {v}" for k, v in (u.get("ai_outcomes") or {}).items())
        rows_html.append(f'<tr><td>{u.get("ani")}</td><td class="num">{u.get("answered_with_outcome")}</td><td class="num"><span class="pill {pill_cls}">{share_str}</span></td><td>{outcomes_str}</td></tr>')
    return f'<table><thead><tr><th>ANI</th><th class="num">Answered (with outcome)</th><th class="num">Unresolved %</th><th>Outcome breakdown</th></tr></thead><tbody>{"".join(rows_html)}</tbody></table>'


def render_performance_leverage(lev: dict) -> str:
    """Render the 'Performance leverage' section: phantom capacity + FCR drag."""
    aht_rows = "".join(
        f'<tr><td>{r["name"]}</td><td class="num">{int(r["voice_aht_s"]) if r["voice_aht_s"] else "—"}</td>'
        f'<td class="num">{int(r["msg_aht_s"]) if r["msg_aht_s"] else "—"}</td>'
        f'<td class="num">{r["voice_excess_min"]:.0f}</td>'
        f'<td class="num">{r["msg_excess_min"]:.0f}</td>'
        f'<td class="num"><strong>{r["total_excess_min"]:.0f}</strong></td>'
        f'<td class="num">{(r["total_excess_min"]/60):.1f}</td></tr>'
        for r in lev["top_aht_offenders"]
    )
    fcr_rows = "".join(
        f'<tr><td>{r["ani"]}</td><td class="num">{r["extra_calls"]}</td>'
        f'<td class="num">{int(r["unresolved_share"]*100)}%</td>'
        f'<td class="num"><strong>{r["drag_min"]:.0f}</strong></td>'
        f'<td class="num">{(r["drag_min"]/60):.1f}</td></tr>'
        for r in lev["top_fcr_repeaters"]
    )
    return f"""<h3>Performance leverage — what we'd save if we hit targets</h3>
<p style="color:var(--muted); font-size:13px;">Two sources of recoverable handle capacity that don't require hiring. Compare the totals to the WFM-derived staffing shortfall (next subsection) to decide between coaching and headcount.</p>

<div class="kpi-grid">
  <div class="kpi warn"><div class="label">AHT phantom capacity</div><div class="value">{lev['aht_excess_h']:.0f} h</div><div class="sub">{lev['aht_excess_fte']:.1f} FTE-equivalent if every agent hit voice 285s / msg 660s</div></div>
  <div class="kpi warn"><div class="label">Repeat-caller drag</div><div class="value">{lev['fcr_drag_h']:.0f} h</div><div class="sub">{lev['fcr_drag_fte']:.1f} FTE-equivalent if first call resolved</div></div>
  <div class="kpi"><div class="label">Total recoverable</div><div class="value">{lev['total_recoverable_h']:.0f} h</div><div class="sub">{lev['total_recoverable_fte']:.1f} FTE / month — coachable, no hire needed</div></div>
</div>

<h4 style="margin:18px 0 6px;font-size:14px;">Top 5 AHT offenders by total excess minutes (the highest-leverage coaching list)</h4>
<table>
  <thead><tr><th>Agent</th><th class="num">V-AHT (s)</th><th class="num">M-AHT (s)</th>
    <th class="num">Voice excess (min)</th><th class="num">Msg excess (min)</th>
    <th class="num">Total excess (min)</th><th class="num">Hours</th></tr></thead>
  <tbody>{aht_rows}</tbody>
</table>

<h4 style="margin:18px 0 6px;font-size:14px;">Top 5 repeat callers by FCR drag</h4>
<table>
  <thead><tr><th>ANI</th><th class="num">Extra calls</th><th class="num">Unresolved %</th>
    <th class="num">Drag (min)</th><th class="num">Hours</th></tr></thead>
  <tbody>{fcr_rows}</tbody>
</table>
<p style="color:var(--muted); font-size:12px;">Drag = (answered_count − 1) × org-avg voice AHT × unresolved_share. Approximation; treats each unresolved repeat as a wasted answered call worth the org-average handle time.</p>"""


def render_staffing_section(staffing: dict, leverage: dict | None) -> str:
    """Render the demand-vs-capacity table + the synthesised "more staff vs better staff"
    recommendation block. Both depend on WFM data being available; when it's not, render
    nothing (caller handles that).
    """
    daily_rows = "".join(
        f'<tr><td>{d["date"]}</td><td class="num">{d["scheduled_hours"]}</td>'
        f'<td class="num">{d["required_hours"]}</td>'
        f'<td class="num"><span class="pill {"bad" if (d["gap_hours"] or 0) > 0 else "good"}">'
        f'{("+" if (d["gap_hours"] or 0) > 0 else "")}{d["gap_hours"]}</span></td>'
        f'<td class="num">{d["scheduled_users"]}</td></tr>'
        for d in staffing["daily"]
    )

    rec_html = ""
    if leverage:
        coachable_h = leverage["total_recoverable_h"]
        peak_short = staffing["peak_shortfall_h"]
        if coachable_h >= peak_short:
            verdict = "good"
            verdict_text = (
                f"<strong>Coachable capacity ({coachable_h:.0f} h) exceeds peak shortfall "
                f"({peak_short:.0f} h). The team doesn't need more headcount — it needs "
                f"better AHT discipline, FCR resolution, and a re-shaped schedule.</strong>"
            )
        else:
            extra_fte = (peak_short - coachable_h) / FTE_HOURS_PER_MONTH
            verdict = "bad"
            verdict_text = (
                f"<strong>Coachable capacity ({coachable_h:.0f} h) doesn't cover the peak "
                f"shortfall ({peak_short:.0f} h). Even after coaching, the team is "
                f"~{extra_fte:.1f} FTE short for peak demand. Hire AND coach.</strong>"
            )
        rec_html = f"""<div class="callout {verdict}">{verdict_text}</div>
<table>
  <thead><tr><th>Lever</th><th class="num">Hours/month</th><th class="num">FTE-equivalent</th></tr></thead>
  <tbody>
    <tr><td>AHT phantom capacity (Layer A)</td><td class="num">{leverage['aht_excess_h']:.0f}</td><td class="num">{leverage['aht_excess_fte']:.1f}</td></tr>
    <tr><td>Repeat-caller drag (Layer B)</td><td class="num">{leverage['fcr_drag_h']:.0f}</td><td class="num">{leverage['fcr_drag_fte']:.1f}</td></tr>
    <tr><td><strong>Total recoverable through coaching</strong></td><td class="num"><strong>{leverage['total_recoverable_h']:.0f}</strong></td><td class="num"><strong>{leverage['total_recoverable_fte']:.1f}</strong></td></tr>
    <tr><td>Peak shortfall (sum of days where scheduled &lt; forecast required)</td><td class="num">{staffing['peak_shortfall_h']:.0f}</td><td class="num">{staffing['peak_shortfall_h']/FTE_HOURS_PER_MONTH:.2f}</td></tr>
    <tr><td>Aggregate excess (sum of days where scheduled &gt; required)</td><td class="num">{staffing['aggregate_excess_h']:.0f}</td><td class="num">{staffing['aggregate_excess_h']/FTE_HOURS_PER_MONTH:.2f}</td></tr>
  </tbody>
</table>"""

    worst = staffing.get("worst_day") or {}
    return f"""<h3>Demand vs scheduled capacity (WFM)</h3>
<p style="color:var(--muted); font-size:13px;">Daily comparison of WFM-scheduled hours vs WFM-forecast required hours, per the published schedule for the period. <strong>Negative gap</strong> = overstaffed (good or wasted depending on view); <strong>positive gap</strong> = understaffed for forecasted demand.</p>

<div class="kpi-grid">
  <div class="kpi"><div class="label">Scheduled (total)</div><div class="value">{staffing['total_scheduled_h']:.0f} h</div></div>
  <div class="kpi"><div class="label">Forecast required (total)</div><div class="value">{staffing['total_required_h']:.0f} h</div></div>
  <div class="kpi {'bad' if staffing['peak_shortfall_h'] > 50 else 'warn'}"><div class="label">Peak shortfall</div><div class="value">{staffing['peak_shortfall_h']:.0f} h</div><div class="sub">across {staffing['under_days_count']} understaffed days</div></div>
  <div class="kpi"><div class="label">Aggregate excess</div><div class="value">{staffing['aggregate_excess_h']:.0f} h</div><div class="sub">on {staffing['over_days_count']} overstaffed days</div></div>
</div>

<table>
  <thead><tr><th>Date</th><th class="num">Scheduled (h)</th><th class="num">Required (h)</th><th class="num">Gap (h)</th><th class="num">Users on shift</th></tr></thead>
  <tbody>{daily_rows}</tbody>
</table>

{f'<div class="callout warn"><strong>Worst day: {worst.get("date")}</strong> — required {worst.get("required_hours"):.0f} h vs scheduled {worst.get("scheduled_hours"):.0f} h ({"+" if worst.get("gap_hours",0)>0 else ""}{worst.get("gap_hours")} h gap, {worst.get("scheduled_users")} users on shift). This is a schedule-shape failure: WFM forecast knew about the demand peak; the schedule didn\'t cover it.</div>' if worst else ''}

<h3>Synthesis — more staff or better staff?</h3>
{rec_html}"""


def render_html(period: str, interval: str, brand_rows: list[dict], per_queue: list[dict],
                workforce: list[dict], themes: dict, daily_sl: list[dict] | None = None,
                leverage: dict | None = None, staffing: dict | None = None) -> str:
    # KPIs
    voice = next((r for r in brand_rows if r["brand"] == "Coles" and r["media"] == "voice"), None)
    cole_msg = next((r for r in brand_rows if r["brand"] == "Coles" and r["media"] == "message"), None)
    onepass_v = next((r for r in brand_rows if r["brand"] == "OnePass" and r["media"] == "voice"), None)
    onepass_msg = next((r for r in brand_rows if r["brand"] == "OnePass" and r["media"] == "message"), None)
    total_voice_off = sum(r["offered"] for r in brand_rows if r["media"] == "voice")
    total_voice_ans = sum(r["answered"] for r in brand_rows if r["media"] == "voice")
    total_msg_off = sum(r["offered"] for r in brand_rows if r["media"] == "message")
    total_msg_ans = sum(r["answered"] for r in brand_rows if r["media"] == "message")
    total_inter = total_voice_off + total_msg_off
    voice_ans_rate = (total_voice_ans / total_voice_off * 100) if total_voice_off else 0
    msg_ans_rate = (total_msg_ans / total_msg_off * 100) if total_msg_off else 0

    # Voice service level (weighted)
    voice_offered_sum = sum(r["offered"] for r in brand_rows if r["media"] == "voice")
    voice_sl_weighted = sum((r["sl_pct"] or 0) * r["offered"] for r in brand_rows if r["media"] == "voice") / (voice_offered_sum or 1)

    # Repeat caller resolution
    outcomes = themes.get("top_ai_outcomes") or {}
    total_outcomes = sum(outcomes.values()) or 1
    resolved = outcomes.get("Resolved", 0)
    res_pct = resolved / total_outcomes * 100

    # Workforce highlights
    top_performer = workforce[0] if workforce else None

    # Pre-break aggregate
    total_pb_min = sum(r["pre_break_overrun_min"] or 0 for r in workforce)
    top_pb = sorted(workforce, key=lambda r: -(r["pre_break_overrun_min"] or 0))[:3]

    # Away aggregate
    top_away = sorted(workforce, key=lambda r: -(r["away_min"] or 0))[:3]

    return f"""<!DOCTYPE html>
<html lang="en-AU">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Prvidr Contact Centre — {period} Report</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<header class="title-band">
  <h1>Prvidr Contact Centre — {period} Report</h1>
  <div class="meta">
    <strong>Period:</strong> {period} (AEST) &nbsp;|&nbsp;
    <strong>Brands:</strong> Coles, OnePass, Members, Spriggy &nbsp;|&nbsp;
    <strong>Channels:</strong> Voice + Message + Email
  </div>
</header>

<nav class="toc">
  <strong>Contents:</strong>
  <a href="#exec">Executive summary</a>
  <a href="#funnel">Volume &amp; funnel</a>
  <a href="#themes">Themes</a>
  <a href="#repeat">Repeat callers</a>
  <a href="#workforce">Workforce</a>
  <a href="#leverage">Performance leverage</a>
</nav>

<section id="exec">
<h2>1. Executive summary</h2>
<div class="kpi-grid">
  <div class="kpi"><div class="label">Total interactions</div><div class="value">{fmt_int(total_inter)}</div><div class="sub">voice + message, customer-facing queues</div></div>
  <div class="kpi {'good' if voice_ans_rate >= 80 else ('warn' if voice_ans_rate >= 60 else 'bad')}"><div class="label">Voice answer rate</div><div class="value">{voice_ans_rate:.0f}%</div><div class="sub">{fmt_int(total_voice_ans)} of {fmt_int(total_voice_off)}</div></div>
  <div class="kpi {'good' if msg_ans_rate >= 80 else ('warn' if msg_ans_rate >= 60 else 'bad')}"><div class="label">Message answer rate</div><div class="value">{msg_ans_rate:.0f}%</div><div class="sub">{fmt_int(total_msg_ans)} of {fmt_int(total_msg_off)}</div></div>
  <div class="kpi {'good' if voice_sl_weighted >= 80 else ('warn' if voice_sl_weighted >= 60 else 'bad')}"><div class="label">Voice service level</div><div class="value">{voice_sl_weighted:.0f}%</div><div class="sub">target 80%</div></div>
  <div class="kpi {'good' if res_pct >= 50 else 'bad'}"><div class="label">Repeat-caller resolution</div><div class="value">{res_pct:.0f}%</div><div class="sub">{resolved} of {total_outcomes} outcome-tagged</div></div>
</div>
</section>

<section id="funnel">
<h2>2. Volume &amp; funnel</h2>
<h3>Brand × channel totals</h3>
{render_brand_table(brand_rows)}
<div class="callout"><strong>Note on message abandons:</strong> Genesys doesn't classify in-queue abandons for messaging the same way it does for voice (chat sessions don't expire identically). Drop-offs in chat surface in the AI outcome data instead — see the Themes section.</div>
<h3>Voice queues by volume</h3>
{render_queue_table(per_queue, "voice", 10)}
<h3>Message queues by volume</h3>
{render_queue_table(per_queue, "message", 10)}
{('<h3>Voice service-level — daily trend</h3><p style="color:var(--muted);font-size:13px;">Org-wide voice SL by day. Target line at 80%. Hover a bar to see the date and counts.</p>' + render_daily_sl_chart(daily_sl or [])) if daily_sl else ''}
</section>

<section id="themes">
<h2>3. Themes (voice)</h2>
<p style="color:var(--muted); font-size:13px;">From the top {themes.get('scope', {}).get('shortlisted', '?')} repeat-caller ANIs deep-dived in {period}. {themes.get('scope', {}).get('candidates_meeting_min_calls', '?')} ANIs called ≥3 times.</p>

<h3>Top dispositions (wrap-up codes)</h3>
{render_dispositions_table(themes['top_dispositions'])}

<h3>AI outcome distribution</h3>
{render_outcomes_table(themes['top_ai_outcomes'])}

<h3>Top expected fixes</h3>
{render_dispositions_table([{'disposition': f['fix'], 'count': f['count']} for f in themes['top_expected_fixes']])}
</section>

<section id="repeat">
<h2>4. Repeat callers — priority list</h2>
<p>Customers whose answered calls are at least 50% <em>not</em>-Resolved (with ≥2 outcome-tagged calls). The actionable list for an outbound call-back team.</p>
{render_unresolved_table(themes['unresolved_repeaters'])}
</section>

<section id="workforce">
<h2>5. Workforce — productivity &amp; adherence</h2>
<p style="color:var(--muted); font-size:13px;"><strong>Email is excluded</strong> from this table (email handle times can span days, which inflates AHT and total handle hours unhelpfully). The figures below are voice + message + callback only. <strong>Voice AHT / Msg AHT</strong> in seconds — split out so neither inflates the other. <strong>Br over</strong> = break/meal overruns. <strong>Away n / min</strong> = count + total minutes on AWAY (raw negative). <strong>Pre-br over n / min</strong> = pre-break sessions running &gt;10 min.</p>

{render_workforce_table(workforce)}

{f'<div class="callout"><strong>Top performer:</strong> {top_performer["name"]} — {fmt_int(top_performer["answered"])} answered ({fmt_int(top_performer["voice_ans"])} voice + {fmt_int(top_performer["msg_ans"])} messages), {top_performer["overruns"]} break overruns.</div>' if top_performer and top_performer["answered"] > 0 else ''}

<div class="callout warn"><strong>Pre-break overrun total: {total_pb_min:.0f} min ({total_pb_min/60:.1f} hours).</strong> Pre-break is the auto-applied 10-minute drain window before scheduled breaks; going past 10 minutes turns wind-down into idle time. Top: {', '.join(f'{r["name"]} ({r["pre_break_overrun_min"]:.0f} min over {r["pre_break_overrun_count"]} instances)' for r in top_pb if r["pre_break_overrun_min"] > 0)}.</div>

<div class="callout warn"><strong>AWAY usage hot-spots:</strong> {', '.join(f'{r["name"]} ({r["away_count"]} instances / {r["away_min"]:.0f} min)' for r in top_away if r["away_min"] > 0)}.</div>
</section>

{f'<section id="leverage"><h2>6. Performance leverage</h2>{render_performance_leverage(leverage)}{render_staffing_section(staffing, leverage) if staffing else ""}</section>' if leverage else ''}

<footer>
<p><strong>Generated</strong> from the Prvidr Genesys Cloud MCP — <code>list_queues</code>, <code>queue_performance</code>, <code>repeat_caller_deep_dive</code>, <code>agent_performance</code>, <code>break_overrun_report</code>. AU region (<code>ap-southeast-2</code>), read-only OAuth. Source: <a href="https://github.com/laggyzee/genesys-mcp">github.com/laggyzee/genesys-mcp</a>. Report compiled {datetime.now().strftime('%-d %B %Y')}.</p>
<p>Period covered: {period} ({interval}).</p>
</footer>

</div>
</body>
</html>"""


# ---------- main ----------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--period", required=True)
    p.add_argument("--interval", required=True)
    p.add_argument("--data-dir", required=True)
    p.add_argument("--qmap-json", required=True, help="JSON dict of queueId → [brand, queue_name]")
    p.add_argument("--user-roles-json", required=True, help="JSON dict of userId → [name, role]")
    p.add_argument("--output", required=True)
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    out_path = Path(os.path.expanduser(args.output))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    qmap = json.loads(Path(args.qmap_json).read_text())
    user_roles = json.loads(Path(args.user_roles_json).read_text())

    qp = json.loads((data_dir / "queue_performance.json").read_text())
    ap = json.loads((data_dir / "agent_performance.json").read_text())
    brk = json.loads((data_dir / "break_overrun_report.json").read_text())
    deep = json.loads((data_dir / "repeat_caller_deep_dive.json").read_text())

    daily_path = data_dir / "queue_performance_daily.json"
    qp_daily = json.loads(daily_path.read_text()) if daily_path.exists() else None

    wfm_path = data_dir / "wfm_schedule.json"
    wfm_raw = json.loads(wfm_path.read_text()) if wfm_path.exists() else None

    qp_agg = aggregate_queue_performance(qp, qmap)
    workforce = aggregate_agents(ap, brk, user_roles, specialist_only=True)
    themes = extract_themes(deep)
    daily_sl = aggregate_daily_voice_sl(qp_daily, qmap) if qp_daily else None
    leverage = compute_performance_leverage(workforce, deep, qp_agg["brand_rows"])
    staffing = aggregate_staffing(wfm_raw)

    html = render_html(args.period, args.interval, qp_agg["brand_rows"], qp_agg["per_queue"],
                       workforce, themes, daily_sl=daily_sl, leverage=leverage, staffing=staffing)
    out_path.write_text(html)

    print(f"OK report written to {out_path}")
    daily_note = f", {len(daily_sl)} daily SL points" if daily_sl else ""
    print(f"   {len(qp_agg['brand_rows'])} brand×media rows, {len(qp_agg['per_queue'])} queues, "
          f"{len(workforce)} specialist agents (TL/Mgr excluded), "
          f"{len(themes['top_dispositions'])} top dispositions, "
          f"{len(themes['unresolved_repeaters'])} unresolved repeaters{daily_note}")
    print(f"   leverage: AHT excess {leverage['aht_excess_h']:.0f}h, FCR drag {leverage['fcr_drag_h']:.0f}h, "
          f"total {leverage['total_recoverable_h']:.0f}h ({leverage['total_recoverable_fte']:.1f} FTE)")
    if staffing:
        print(f"   staffing: scheduled {staffing['total_scheduled_h']:.0f}h, "
              f"required {staffing['total_required_h']:.0f}h, "
              f"peak shortfall {staffing['peak_shortfall_h']:.0f}h "
              f"({staffing['under_days_count']} under days, {staffing['over_days_count']} over days)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
