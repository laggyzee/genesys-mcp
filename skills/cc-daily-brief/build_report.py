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

from genesys_mcp.shapes import (  # noqa: E402
    assert_aggregates_envelope,
    assert_break_overrun_report,
    assert_repeat_caller_deep_dive,
)
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
        # agent_performance results don't carry a `derived` block (only
        # queue_performance does); read raw metric stats and compute AHT.
        bucket = (r.get("data") or [{}])[0]
        metrics = {m["metric"]: (m.get("stats") or {}) for m in (bucket.get("metrics") or [])}
        answered = int(metrics.get("tAnswered", {}).get("count", 0) or 0)
        handle_count = int(metrics.get("tHandle", {}).get("count", 0) or 0)
        handle_sum_ms = float(metrics.get("tHandle", {}).get("sum", 0) or 0)
        aht_s = (handle_sum_ms / 1000.0 / handle_count) if handle_count else None
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


def repeat_caller_hotlist(deep: dict, top_n: int = 8,
                          unresolved_share_threshold: float = 0.5) -> list[dict]:
    """Filter the deep-dive's repeaters to a top-N callback list.

    repeat_caller_deep_dive returns rows under the ``repeaters`` key. We keep
    rows where either (a) the heuristic ``recommended_action`` is one of the
    explicit follow-up signals (``callback_recommended`` / ``escalate_to_retention``)
    or (b) ``unresolved_share`` is above the threshold (default 50% — the
    caller is still cycling back). Plain ``route_review`` / ``monitor`` rows
    aren't actionable from the daily-brief and don't make the cut.
    """
    rows = deep.get("repeaters") or deep.get("unresolved_repeaters") or []
    actionable_keep = {"callback_recommended", "escalate_to_retention"}
    out = []
    for r in rows:
        action = r.get("recommended_action")
        unresolved = r.get("unresolved_share") or 0
        if action not in actionable_keep and unresolved < unresolved_share_threshold:
            continue
        # Last-call summary lives inside topics or sentiment_trajectory in
        # the deep-dive output; fall back to the topics list for context.
        topics = r.get("topics") or []
        topic_str = ", ".join(t.get("topic", "") for t in topics[:2])
        out.append({
            "ani_masked": r.get("ani_masked") or r.get("ani") or "—",
            "answered_count": r.get("answered_count"),
            "unresolved_share": unresolved,
            "last_summary": topic_str[:160] or (r.get("last_summary") or "")[:160],
            "recommended_action": action,
        })
    out.sort(key=lambda r: -(r.get("unresolved_share") or 0))
    return out[:top_n]


def adherence_flags(brk: dict, user_names: dict[str, str] | None = None,
                    overrun_min_threshold: int = 30,
                    top_n: int = 5) -> list[dict]:
    """Agents whose combined break+meal+pre-break overrun > threshold.

    Reads break_overrun_report's shape. ``total_overrun_min`` rolls up break
    + meal overruns only — pre-break is tracked separately under
    ``pre_break_overrun_total_min``. The threshold gates on the *combined*
    minute count so a 40-minute pre-break parking session surfaces even when
    break/meal are clean.
    """
    user_names = user_names or {}
    rows: list[dict] = []
    for r in brk.get("users") or []:
        pb_min = r.get("pre_break_overrun_total_min") or 0
        # break/meal overruns roll up into total_overrun_min (the report's
        # primary number). We split where possible; otherwise total carries it.
        bm_min = r.get("total_overrun_min") or 0
        combined_min = bm_min + pb_min
        if combined_min < overrun_min_threshold:
            continue
        uid = r.get("user_id")
        rows.append({
            "name": user_names.get(uid) or uid[:8],
            "user_id": uid,
            "pre_break_overrun_min": pb_min,
            "break_overrun_min": bm_min,
            "meal_overrun_min": 0,  # not split in the source
            "away_total_min": r.get("away_total_min") or 0,
            "total_min": round(combined_min, 1),
        })
    rows.sort(key=lambda r: -r["total_min"])
    return rows[:top_n]


def adherence_summary(brk: dict) -> dict:
    """Org-level overrun totals, computed independently of the per-agent threshold.

    The threshold in :func:`adherence_flags` is a "surface the worst offenders"
    filter — agents with small overruns drop off the table even when there's
    real org-level drain. This summary always shows the total so the brief
    can't silently swallow 200 min of accumulated overrun.
    """
    break_meal_min = 0.0
    pre_break_min = 0.0
    agents_with_any = 0
    sessions = 0
    for r in brk.get("users") or []:
        bm = r.get("total_overrun_min") or 0
        pb = r.get("pre_break_overrun_total_min") or 0
        if bm or pb:
            agents_with_any += 1
            sessions += (r.get("overrun_count") or 0) + (r.get("pre_break_overrun_count") or 0)
        break_meal_min += bm
        pre_break_min += pb
    return {
        "break_meal_min": round(break_meal_min, 1),
        "pre_break_min": round(pre_break_min, 1),
        "total_min": round(break_meal_min + pre_break_min, 1),
        "agents_with_any": agents_with_any,
        "overrun_sessions": sessions,
    }


# ── Narrative synthesis (v0.9) ──
# Mirrors the v0.7 cc-monthly-report pattern with daily-brief-specific
# section shape. Two sections instead of four: a short Headline paragraph
# + a top-3 Priorities list. Daily briefs are meant to be glanced at; they
# don't need the long-form caveats/wins/wrongs/recommendations structure.

_NARRATIVE_SECTIONS = (
    ("Headline", "headline",
     "1 paragraph: how did we go vs yesterday + the rolling median, what stands out."),
    ("Today's priorities", "priorities",
     "Top 3 actions for the morning standup, sorted by impact."),
)


def _md_inline(text: str) -> str:
    """Minimal inline markdown: **bold**, *italic*, `code`, [link](url).

    Existing HTML is escaped first so untrusted-ish LLM output can't inject
    markup beyond what we explicitly support. Identical to the v0.7
    cc-monthly-report implementation — duplicated rather than shared because
    a 50-line abstraction across three skills isn't worth a shared module yet.
    """
    import re
    out = escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\*(.+?)\*", r"<em>\1</em>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{escape(m.group(2))}">{m.group(1)}</a>',
        out,
    )
    return out


def _md_section_body_to_html(body: str) -> str:
    """Block-level subset: paragraphs and `- ` bullet lists."""
    lines = body.strip().split("\n")
    out_blocks: list[str] = []
    para: list[str] = []
    bullets: list[str] = []

    def _flush_para():
        if para:
            text = " ".join(para).strip()
            if text:
                out_blocks.append(f"<p>{_md_inline(text)}</p>")
            para.clear()

    def _flush_bullets():
        if bullets:
            items = "".join(f"<li>{_md_inline(b)}</li>" for b in bullets)
            out_blocks.append(f"<ul>{items}</ul>")
            bullets.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            _flush_para()
            _flush_bullets()
            continue
        if stripped.startswith("- "):
            _flush_para()
            bullets.append(stripped[2:])
        else:
            _flush_bullets()
            para.append(stripped)
    _flush_para()
    _flush_bullets()
    return "\n".join(out_blocks)


def parse_narrative_md(path: Path) -> dict[str, str]:
    """Parse a narrative markdown file by `## Heading` boundaries.

    Returns ``{heading_slug: html_body}`` for whichever expected headings
    appear; missing sections are simply omitted (the renderer skips them).
    """
    text = path.read_text()
    expected = {title.lower(): slug for title, slug, _desc in _NARRATIVE_SECTIONS}
    sections: dict[str, str] = {}
    current_slug: str | None = None
    current_lines: list[str] = []

    def _close():
        if current_slug and current_lines:
            sections[current_slug] = _md_section_body_to_html("\n".join(current_lines))

    for line in text.splitlines():
        if line.startswith("## "):
            _close()
            heading = line[3:].strip()
            current_slug = expected.get(heading.lower())
            current_lines = []
        else:
            if current_slug is not None:
                current_lines.append(line)
    _close()
    return sections


def render_narrative_block(narrative: dict[str, str] | None) -> str:
    """Render the narrative block (single combined HTML) for daily-brief.

    Unlike cc-monthly-report (which has 4 separate sections), daily-brief's
    2 narrative sections render as a single combined `<section>` at the top
    of the report — short enough to read inline without TOC anchors.
    """
    if not narrative:
        return ""
    parts: list[str] = []
    for title, slug, _desc in _NARRATIVE_SECTIONS:
        body_html = narrative.get(slug)
        if not body_html:
            continue
        parts.append(f'<h3 style="margin-top:14px;">{escape(title)}</h3>{body_html}')
    if not parts:
        return ""
    return (
        '<section class="narrative" '
        'style="border-left:3px solid var(--accent);'
        'background:linear-gradient(to right, var(--accent-soft) 0%, var(--bg) 6%);">'
        '<h2>Daily summary</h2>'
        f'{"".join(parts)}'
        '</section>'
    )


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


# ── v1.11: NPS + wrap-up mini-card aggregators ──

def aggregate_nps(attr_search: dict | None) -> dict | None:
    """Reduce v1.8 ``search_conversations_by_attribute`` output to the NPS
    rollup the daily-brief card needs. Returns ``None`` when the tool wasn't
    called, returned zero conversations, or the attribute wasn't NPS-shaped
    (integer 0-10 values). Graceful-when-absent — caller skips the card.
    """
    if not attr_search:
        return None
    totals = attr_search.get("totals") or {}
    total = totals.get("conversation_count") or 0
    if total <= 0:
        return None
    numeric = attr_search.get("numeric_summary") or {}
    nps = numeric.get("nps") if numeric else None
    if not nps:
        return None
    return {
        "score": nps.get("score"),
        "total": total,
        "promoters": nps.get("promoters_9_10") or 0,
        "passives": nps.get("passives_7_8") or 0,
        "detractors": nps.get("detractors_0_6") or 0,
    }


def aggregate_wrap_up_mini(wrap_up: dict | None, top_n: int = 5) -> dict | None:
    """Reduce v1.10 ``wrap_up_code_distribution`` output to the top-N codes
    plus the single largest mover. Returns ``None`` when the tool wasn't
    called or returned zero conversations.
    """
    if not wrap_up:
        return None
    totals = wrap_up.get("totals") or {}
    total_convs = totals.get("conversation_count") or 0
    if total_convs <= 0:
        return None
    distribution = wrap_up.get("distribution") or []
    top_codes = [
        {
            "name": d.get("name") or "<unknown>",
            "count": d.get("count") or 0,
            "percentage": d.get("percentage") or 0.0,
        }
        for d in distribution[:top_n]
    ]
    trend = wrap_up.get("trend") or {}
    movers = trend.get("largest_movers") or []
    largest_mover = movers[0] if movers else None
    return {
        "total_conversations": total_convs,
        "top_codes": top_codes,
        "largest_mover": largest_mover,
    }


def render_nps_card(nps: dict | None) -> str:
    """Render the NPS KPI card. Returns empty string when ``nps`` is None."""
    if nps is None:
        return ""
    score = nps["score"]
    if score is None:
        score_str = "—"
        cls = ""
    else:
        cls = "good" if score >= 30 else "warn" if score >= 0 else "bad"
        sign = "+" if score > 0 else ""
        score_str = f"{sign}{score:.0f}"
    return (
        f'<div class="kpi {cls}"><div class="label">NPS (yesterday)</div>'
        f'<div class="value">{score_str}</div>'
        f'<div class="sub">{nps["promoters"]} / {nps["passives"]} / {nps["detractors"]} '
        f'prom / pass / det · n={nps["total"]}</div></div>'
    )


def render_wrap_up_mini_card(wrap_mini: dict | None) -> str:
    """Render the wrap-up mini-card. Returns empty string when ``wrap_mini`` is None."""
    if wrap_mini is None:
        return ""
    rows = "".join(
        f'<tr><td>{escape(c["name"])}</td>'
        f'<td class="num">{fmt_int(c["count"])}</td>'
        f'<td class="num">{fmt_pct(c["percentage"])}</td></tr>'
        for c in wrap_mini["top_codes"]
    )
    mover = wrap_mini.get("largest_mover")
    if mover and mover.get("delta_pct") is not None:
        dp = mover["delta_pct"]
        arrow = "↑" if dp > 0 else ("↓" if dp < 0 else "→")
        cls = "good" if dp > 0 else "bad" if dp < 0 else "warn"
        sign = "+" if dp > 0 else ""
        mover_html = (
            f'<div class="callout" style="margin-top:6px;font-size:12px">'
            f'<strong>Largest mover:</strong> {escape(mover.get("name") or "")} '
            f'<span class="vs-target {cls}">{arrow} {sign}{dp:.1f}%</span></div>'
        )
    else:
        mover_html = ""
    return (
        '<div>'
        '<table><thead><tr><th>Wrap-up code</th>'
        '<th class="num">Calls</th><th class="num">Share</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'{mover_html}'
        '</div>'
    )


# ── HTML render ──

def render_html(cfg: TenantConfig, target_date: str, day_interval: str,
                window_interval: str, headline: dict, worst: list[dict],
                flagged: list[dict], hotlist: list[dict],
                adherence: list[dict],
                adherence_summary_data: dict | None = None,
                narrative: dict[str, str] | None = None,
                nps: dict | None = None,
                wrap_mini: dict | None = None) -> str:
    # v1.0: respect operating_model.expected_channels. Tenants without
    # voice or without message skip the corresponding KPI card rather than
    # rendering a misleading "0%" or "—".
    channels = cfg.operating_model.expected_channels
    show_voice = "voice" in channels
    show_message = "message" in channels

    cards = ['<div class="kpi-grid">']
    if show_voice:
        voice_sl_today = headline["voice_sl_today"]
        voice_sl_base = headline["voice_sl_baseline"]
        sl_cls = ("good" if voice_sl_today and voice_sl_today >= 80
                  else "warn" if voice_sl_today and voice_sl_today >= 65
                  else "bad")
        cards.append(
            f'<div class="kpi {sl_cls}"><div class="label">Voice SL</div>'
            f'<div class="value">{fmt_pct(voice_sl_today)}</div>'
            f'<div class="sub">vs rolling median {fmt_pct(voice_sl_base)} '
            f'{_vs_pill(voice_sl_today, voice_sl_base, units="pp")}</div></div>'
        )
    if show_message:
        msg_sl_today = headline["message_sl_today"]
        msg_sl_base = headline["message_sl_baseline"]
        msg_cls = ("good" if msg_sl_today and msg_sl_today >= 80
                   else "warn" if msg_sl_today and msg_sl_today >= 65
                   else "bad")
        cards.append(
            f'<div class="kpi {msg_cls}"><div class="label">Message SL</div>'
            f'<div class="value">{fmt_pct(msg_sl_today)}</div>'
            f'<div class="sub">vs rolling median {fmt_pct(msg_sl_base)} '
            f'{_vs_pill(msg_sl_today, msg_sl_base, units="pp")}</div></div>'
        )
    channel_label = " + ".join(c for c in ("voice", "message") if c in channels)
    cards.append(
        f'<div class="kpi"><div class="label">Total interactions</div>'
        f'<div class="value">{fmt_int(headline["total_offered_today"])}</div>'
        f'<div class="sub">{channel_label} offered</div></div>'
    )
    # v1.11: NPS card (gated on cfg.survey.nps_attribute_key + tool output).
    # render_nps_card returns "" when nps is None, so this is a no-op for
    # tenants that haven't opted in.
    cards.append(render_nps_card(nps))
    cards.append('</div>')
    headline_html = "".join(cards)

    # v1.11: wrap-up mini-card section — sits between Headline and Worst
    # routes when wrap_up_code_distribution returned data; omitted entirely
    # otherwise so tenants who don't fetch it see no empty placeholder.
    wrap_mini_html = render_wrap_up_mini_card(wrap_mini)

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
    # Org-level summary line — always shown, regardless of whether the per-agent
    # 30-min threshold caught anyone. Stops the brief from silently swallowing
    # accumulated overrun (e.g. 4 small break overruns + 14 small pre-breaks).
    summary = adherence_summary_data or {
        "break_meal_min": 0, "pre_break_min": 0, "total_min": 0,
        "agents_with_any": 0, "overrun_sessions": 0,
    }
    # v1.0: respect operating_model.has_pre_break_presence — tenants without
    # a pre-break drain state get the section but with explicit "tracking
    # disabled" framing rather than misleading zeros.
    has_pre_break = cfg.operating_model.has_pre_break_presence
    pre_break_label = (
        f', pre-break {summary["pre_break_min"]:.0f} min'
        if has_pre_break else ''
    )
    if summary["total_min"] > 0:
        summary_html = (
            '<div class="callout" style="margin-bottom:8px">'
            f'<strong>Total overrun yesterday: {summary["total_min"]:.0f} min</strong> '
            f'across {summary["overrun_sessions"]} sessions / {summary["agents_with_any"]} agents '
            f'(break+meal {summary["break_meal_min"]:.0f} min{pre_break_label}). '
            f'Per-agent threshold for the table below: ≥30 min combined.</div>'
        )
    elif not has_pre_break:
        summary_html = (
            '<div class="callout" style="margin-bottom:8px">'
            '<strong>Pre-break tracking disabled for this tenant</strong> '
            '(set <code>operating_model.has_pre_break_presence: true</code> '
            'in tenant.yaml to enable). Break + meal overrun summary: clean today.</div>'
        )
    else:
        summary_html = ""

    if not adherence:
        adherence_html = summary_html + (
            '<div class="callout"><strong>No agents over the 30-min combined threshold.</strong>'
            + (f' Smaller overruns are rolled into the summary above.' if summary["total_min"] > 0 else "")
            + '</div>'
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
        adherence_html = summary_html + (
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
        f'{render_narrative_block(narrative)}'
        f'<section><h2>1. Headline KPIs</h2>{headline_html}</section>'
        + (f'<section><h2>1a. Top wrap-up codes</h2>{wrap_mini_html}</section>'
           if wrap_mini_html else '')
        + f'<section><h2>2. Worst routes</h2>{worst_html}</section>'
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
    p.add_argument("--with-narrative",
                   help="(v0.9) Path to a markdown file with 2 narrative sections "
                        "('## Headline', '## Today\\'s priorities'). Each section's "
                        "body renders via a minimal markdown subset (paragraphs, "
                        "**bold**, *italic*, `code`, [links], - bullets) and slots "
                        "in at the top of the brief. Optional; omitting it produces "
                        "the v0.7-era data-only brief.")
    args = p.parse_args()

    cfg = load_config()
    data_dir = Path(args.data_dir)

    qp_day = json.loads((data_dir / "queue_perf_day.json").read_text())
    qp_window = json.loads((data_dir / "queue_perf_window.json").read_text())
    ap_day = json.loads((data_dir / "agent_perf_day.json").read_text())
    deep = json.loads((data_dir / "repeat_callers.json").read_text())
    brk = json.loads((data_dir / "break_overrun.json").read_text())

    # Fail loud if any input has the wrong shape — catches the silent-empty
    # bug class the v0.9.x patches addressed (e.g. agent_perf accidentally
    # written into queue_perf_day.json, or the deep-dive output written
    # under the wrong filename). Validators are cheap envelope checks.
    assert_aggregates_envelope(qp_day, source="queue_perf_day", expect_derived=True)
    assert_aggregates_envelope(qp_window, source="queue_perf_window", expect_derived=True)
    assert_aggregates_envelope(ap_day, source="agent_perf_day", expect_derived=False)
    assert_repeat_caller_deep_dive(deep, source="repeat_callers")
    assert_break_overrun_report(brk, source="break_overrun")

    # Optional lookup maps for prettier output (queue/user names). The skill
    # workflow can save these alongside the data; if missing we fall back to
    # id-prefixes so the brief still renders.
    qmap_path = data_dir / "qmap.json"
    qmap = json.loads(qmap_path.read_text()) if qmap_path.exists() else {}
    user_names_path = data_dir / "user_names.json"
    user_names = json.loads(user_names_path.read_text()) if user_names_path.exists() else {}

    # v1.11: optional NPS + wrap-up inputs. Both gated entirely on the file
    # being present in data_dir — the skill workflow only saves them when
    # tenant.yaml has cfg.survey.nps_attribute_key set (NPS) or unconditionally
    # for wrap-up. Missing file → card/section silently omitted.
    nps_path = data_dir / "nps.json"
    nps_raw = json.loads(nps_path.read_text()) if nps_path.exists() else None
    wrap_path = data_dir / "wrap_up_distribution.json"
    wrap_raw = json.loads(wrap_path.read_text()) if wrap_path.exists() else None

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
    adherence_summary_data = adherence_summary(brk)

    narrative = (
        parse_narrative_md(Path(args.with_narrative).expanduser())
        if args.with_narrative else None
    )
    nps = aggregate_nps(nps_raw)
    wrap_mini = aggregate_wrap_up_mini(wrap_raw)
    html = render_html(cfg, args.target_date, args.day_interval,
                       args.window_interval, headline, worst, flagged,
                       hotlist, adherence,
                       adherence_summary_data=adherence_summary_data,
                       narrative=narrative,
                       nps=nps, wrap_mini=wrap_mini)

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
