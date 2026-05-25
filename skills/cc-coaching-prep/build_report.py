#!/usr/bin/env python3
"""Render the cc-coaching-prep HTML brief from an agent_coaching_pack JSON payload.

The MCP tool ``agent_coaching_pack`` returns a single structured JSON with
every section's data already aggregated. This script's job is purely the
HTML rendering layer: load the JSON + tenant.yaml, lay out the brief using
the same CSS idiom as ``cc-monthly-report``, write to the configured
output path.

Usage:
    python build_report.py \\
        --coaching-pack /tmp/cc-coaching-anthony-april/coaching_pack.json \\
        --agent-slug anthony-kha \\
        --period "April 2026" \\
        --period-slug april-2026

The output path comes from ``cfg.coaching_output_path(agent_slug, period_slug)``.
Mirrors the pattern in ``skills/cc-monthly-report/build_report.py``.
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

from genesys_mcp.conversation_links import render_conversation_cell  # noqa: E402
from genesys_mcp.tenant import TenantConfig, load_config  # noqa: E402


# ── helpers (mirrored from cc-monthly-report) ──

def fmt_int(n):
    return "—" if n is None else f"{int(n):,}"


def fmt_secs(n):
    if n is None:
        return "—"
    s = int(round(n))
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


def fmt_pct(n, digits=0):
    if n is None:
        return "—"
    return f"{n:.{digits}f}%"


def _aht_with_target(aht_s, vs_pct):
    if aht_s is None:
        return '<span class="muted">—</span>'
    if vs_pct is None:
        return f"{int(aht_s)}s"
    sign = "+" if vs_pct > 0 else ""
    cls = "good" if vs_pct <= 0 else ("warn" if vs_pct <= 20 else "bad")
    return (
        f'{int(aht_s)}s <span class="vs-target {cls}">{sign}{vs_pct:.0f}%</span>'
    )


def _peer_delta(target_val, peer_val, lower_is_better=True):
    """Inline pill comparing target value to peer-median."""
    if target_val is None or peer_val is None:
        return '<span class="muted">—</span>'
    diff = target_val - peer_val
    pct = (diff / peer_val * 100) if peer_val else 0
    if lower_is_better:
        cls = "good" if pct <= -5 else ("bad" if pct >= 15 else "warn" if pct >= 5 else "good")
    else:
        cls = "good" if pct >= 5 else ("bad" if pct <= -15 else "warn" if pct <= -5 else "good")
    sign = "+" if pct > 0 else ""
    return (
        f'<span class="vs-target {cls}">{sign}{pct:.0f}% vs peers</span>'
    )


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
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  color:var(--ink); background:var(--bg-soft); margin:0; line-height:1.55; font-size:14px; }
.wrap { max-width:1000px; margin:0 auto; padding:28px 32px 80px; }
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
h2 { color:var(--ink); font-size:20px; margin:30px 0 14px; padding-bottom:8px; border-bottom:2px solid var(--line); font-weight:600; }
h3 { color:var(--ink); font-size:15px; margin:18px 0 8px; font-weight:600; }
section { background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:8px 26px 24px; margin-bottom:22px; }
p { margin:8px 0 14px; }
table { width:100%; border-collapse:collapse; font-size:13px; margin:10px 0 14px; }
th { text-align:left; background:var(--bg-card); color:var(--ink); font-weight:600; padding:9px 10px; border-bottom:2px solid var(--line); }
td { padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:middle; }
tr:hover td { background:var(--bg-soft); }
th.num,td.num { text-align:right; font-variant-numeric:tabular-nums; }
td.muted, span.muted { color:var(--muted); }
.kpi-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; margin:16px 0 22px; }
.kpi { background:var(--bg-card); border:1px solid var(--line); border-left:4px solid var(--accent); padding:14px 16px; border-radius:4px; }
.kpi.good { border-left-color:var(--good); } .kpi.warn { border-left-color:var(--warn); } .kpi.bad { border-left-color:var(--bad); }
.kpi .label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:4px; font-weight:600; }
.kpi .value { font-size:24px; font-weight:700; color:var(--ink); font-variant-numeric:tabular-nums; }
.kpi .sub { font-size:12px; color:var(--ink-soft); margin-top:2px; }
.callout { background:var(--accent-soft); border-left:4px solid var(--accent); padding:12px 16px; margin:14px 0; border-radius:4px; font-size:13px; }
.callout.good { background:var(--good-soft); border-left-color:var(--good); }
.callout.warn { background:var(--warn-soft); border-left-color:var(--warn); }
.callout.bad { background:var(--bad-soft); border-left-color:var(--bad); }
.vs-target { font-weight:600; font-size:11px; margin-left:3px; }
.vs-target.good { color:var(--good); }
.vs-target.warn { color:var(--warn); }
.vs-target.bad  { color:var(--bad); }
.focus-card { background:var(--bg-card); border:1px solid var(--line); border-left:4px solid var(--warn); border-radius:5px; padding:14px 18px; margin:10px 0; }
.focus-card .rank { display:inline-block; background:var(--warn); color:white; width:24px; height:24px; line-height:24px; text-align:center; border-radius:50%; font-weight:700; font-size:13px; margin-right:10px; }
.focus-card .area { font-weight:600; font-size:14px; }
.focus-card .headline { color:var(--ink-soft); font-size:13px; margin-top:6px; }
.flag-pill { display:inline-block; background:var(--warn-soft); color:var(--warn); padding:2px 7px; border-radius:3px; font-size:11px; font-weight:600; margin-right:4px; margin-bottom:2px; }
footer { color:var(--muted); font-size:12px; padding-top:18px; margin-top:28px; border-top:1px solid var(--line); }
@media print { body{background:white;} .wrap{padding:0 12mm; max-width:none;} nav.toc{display:none;} section{page-break-inside:avoid;} h2{page-break-after:avoid;} a{color:inherit; text-decoration:none;} }
"""


def render_header(pack: dict, period: str, cfg: TenantConfig) -> str:
    agent = pack.get("agent", {})
    peers = pack.get("performance", {}).get("peer_count", 0)
    return (
        f'<header class="title-band">'
        f'<h1>Coaching brief — {escape(agent.get("name") or "agent")}</h1>'
        f'<div class="meta">'
        f'<strong>{escape(period)}</strong> · '
        f'{escape(agent.get("title") or "")} · '
        f'Manager: {escape(agent.get("manager_name") or "—")} · '
        f'Peers: {peers} · '
        f'Tenant: {escape(cfg.tenant.name)}'
        f'</div></header>'
    )


def render_toc() -> str:
    return (
        '<nav class="toc">'
        '<strong>Sections:</strong> '
        '<a href="#performance">1. Performance vs targets</a>'
        '<a href="#sentiment">2. Sentiment &amp; quality</a>'
        '<a href="#wrap">3. Wrap-up &amp; handling</a>'
        '<a href="#flagged">4. Flagged calls</a>'
        '<a href="#focus">5. Recommended focus</a>'
        '</nav>'
    )


def render_performance_section(pack: dict, cfg: TenantConfig) -> str:
    p = pack.get("performance", {})
    t = p.get("target") or {}
    peer = p.get("peer_medians") or {}
    targets = pack.get("targets", {})

    kpi_html = (
        '<div class="kpi-grid">'
        + _kpi_card("Voice answered", fmt_int(t.get("voice_answered")), "calls")
        + _kpi_card("Message answered", fmt_int(t.get("message_answered")), "interactions")
        + _kpi_card("Total handle hours", fmt_int(t.get("total_handle_hours")), "across all media")
        + _kpi_card(
            "Voice AHT",
            _aht_with_target(t.get("voice_aht_s"), t.get("voice_aht_vs_target_pct")),
            f"target {targets.get('voice_aht_s')}s",
            cls=_aht_class(t.get("voice_aht_vs_target_pct")),
        )
        + _kpi_card(
            "Message AHT",
            _aht_with_target(t.get("message_aht_s"), t.get("message_aht_vs_target_pct")),
            f"target {targets.get('msg_aht_s')}s",
            cls=_aht_class(t.get("message_aht_vs_target_pct")),
        )
        + _kpi_card(
            "Voice ACW",
            (
                f"{int(t['voice_acw_avg_s'])}s" if t.get("voice_acw_avg_s") is not None else "—"
            ),
            f"target {targets.get('acw_s')}s",
            cls=_acw_class(t.get("voice_acw_avg_s"), targets.get("acw_s")),
        )
        + "</div>"
    )

    # Peer comparison table
    rows: list[str] = []
    if peer.get("voice_aht_s") is not None:
        rows.append(
            f'<tr><td>Voice AHT</td>'
            f'<td class="num">{int(t["voice_aht_s"]) if t.get("voice_aht_s") else "—"}s</td>'
            f'<td class="num">{int(peer["voice_aht_s"])}s</td>'
            f'<td>{_peer_delta(t.get("voice_aht_s"), peer.get("voice_aht_s"))}</td></tr>'
        )
    if peer.get("message_aht_s") is not None:
        rows.append(
            f'<tr><td>Message AHT</td>'
            f'<td class="num">{int(t["message_aht_s"]) if t.get("message_aht_s") else "—"}s</td>'
            f'<td class="num">{int(peer["message_aht_s"])}s</td>'
            f'<td>{_peer_delta(t.get("message_aht_s"), peer.get("message_aht_s"))}</td></tr>'
        )
    if peer.get("voice_acw_avg_s") is not None and t.get("voice_acw_avg_s") is not None:
        rows.append(
            f'<tr><td>Voice ACW</td>'
            f'<td class="num">{int(t["voice_acw_avg_s"])}s</td>'
            f'<td class="num">{int(peer["voice_acw_avg_s"])}s</td>'
            f'<td>{_peer_delta(t.get("voice_acw_avg_s"), peer.get("voice_acw_avg_s"))}</td></tr>'
        )
    if peer.get("voice_hold_ratio") is not None and t.get("voice_hold_ratio") is not None:
        rows.append(
            f'<tr><td>Voice hold ratio</td>'
            f'<td class="num">{t["voice_hold_ratio"]:.0%}</td>'
            f'<td class="num">{peer["voice_hold_ratio"]:.0%}</td>'
            f'<td>{_peer_delta(t.get("voice_hold_ratio"), peer.get("voice_hold_ratio"))}</td></tr>'
        )
    if peer.get("total_handle_hours") is not None and t.get("total_handle_hours") is not None:
        rows.append(
            f'<tr><td>Total handle hours</td>'
            f'<td class="num">{t["total_handle_hours"]:.1f}h</td>'
            f'<td class="num">{peer["total_handle_hours"]:.1f}h</td>'
            f'<td>{_peer_delta(t.get("total_handle_hours"), peer.get("total_handle_hours"), lower_is_better=False)}</td></tr>'
        )

    peer_table = ""
    if rows:
        peer_table = (
            "<h3>Peer comparison</h3>"
            f'<p class="muted">Comparing against {p.get("peer_count", 0)} peers in the same role / management unit.</p>'
            '<table><thead><tr><th>Metric</th><th class="num">This agent</th>'
            f'<th class="num">Peer median (n={p.get("peer_count", 0)})</th><th>Delta</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
        )

    return (
        f'<section id="performance"><h2>1. Performance vs targets</h2>{kpi_html}{peer_table}</section>'
    )


def _kpi_card(label, value, sub, cls=""):
    cls_str = f" {cls}" if cls else ""
    return (
        f'<div class="kpi{cls_str}">'
        f'<div class="label">{escape(label)}</div>'
        f'<div class="value">{value}</div>'
        f'<div class="sub">{escape(sub)}</div>'
        f"</div>"
    )


def _aht_class(vs_pct):
    if vs_pct is None:
        return ""
    if vs_pct <= 0:
        return "good"
    if vs_pct <= 20:
        return "warn"
    return "bad"


def _acw_class(acw_s, target_s):
    if acw_s is None or target_s is None:
        return ""
    if acw_s <= target_s:
        return "good"
    if acw_s <= target_s * 2:
        return "warn"
    return "bad"


def render_sentiment_quality(pack: dict) -> str:
    sent = pack.get("sentiment") or {}
    qa = pack.get("quality") or {}
    qa_summary = qa.get("summary") or {}
    scope = qa.get("scope_available", False)

    cards = ['<div class="kpi-grid">']
    s = sent.get("avg")
    if s is None:
        cards.append(_kpi_card("Avg sentiment", "—", "no STA data this period"))
    else:
        cls = "good" if s > 0.2 else ("bad" if s < -0.2 else "warn")
        cards.append(_kpi_card("Avg sentiment", f"{s:+.2f}",
                               f"{sent.get('samples', 0)} call samples", cls=cls))

    if scope and qa_summary.get("avg_score") is not None:
        avg = qa_summary["avg_score"]
        cls = "good" if avg >= 90 else ("warn" if avg >= 80 else "bad")
        cards.append(_kpi_card("QA avg score", f"{avg:.1f}%",
                               f"{qa_summary['n_evaluations']} evaluations", cls=cls))
        pass_rate = qa_summary.get("pass_rate")
        if pass_rate is not None:
            cards.append(_kpi_card("QA pass rate", f"{pass_rate*100:.0f}%",
                                   "evaluations ≥ 80%",
                                   cls=("good" if pass_rate >= 0.9 else
                                        ("warn" if pass_rate >= 0.75 else "bad"))))
        if qa_summary.get("last_evaluated_at"):
            cards.append(_kpi_card("Last evaluated",
                                   _date_short(qa_summary["last_evaluated_at"]),
                                   "most recent QA"))
    else:
        cards.append(
            '<div class="kpi"><div class="label">QA evaluations</div>'
            '<div class="value">—</div>'
            f'<div class="sub">{"no evals in period" if scope else "scope unavailable"}</div></div>'
        )
    cards.append("</div>")

    # QA detail table
    evals = qa.get("evaluations") or []
    eval_rows: list[str] = []
    for e in evals[:8]:
        score = e.get("total_score")
        cls = (
            "good" if score and score >= 90 else
            ("warn" if score and score >= 80 else "bad")
        )
        score_html = f'<span class="vs-target {cls}">{score:.1f}%</span>' if score else "—"
        critical = "✓" if e.get("critical_passed") else ("✗" if e.get("critical_passed") is False else "—")
        eval_rows.append(
            f'<tr><td>{_date_short(e.get("released_date"))}</td>'
            f'<td>{escape(e.get("form_name") or "—")}</td>'
            f'<td>{escape(e.get("evaluator_name") or "—")}</td>'
            f'<td class="num">{score_html}</td>'
            f'<td class="num">{critical}</td></tr>'
        )
    qa_table = ""
    if eval_rows:
        qa_table = (
            "<h3>Recent evaluations</h3>"
            '<table><thead><tr><th>Released</th><th>Form</th><th>Evaluator</th>'
            '<th class="num">Score</th><th class="num">Critical</th></tr></thead>'
            f'<tbody>{"".join(eval_rows)}</tbody></table>'
        )

    return (
        f'<section id="sentiment"><h2>2. Sentiment &amp; quality</h2>'
        f'{"".join(cards)}{qa_table}</section>'
    )


def render_wrap_section(pack: dict) -> str:
    w = pack.get("wrap_discipline") or {}
    total = w.get("total_conversations") or 0
    note_rate = w.get("note_rate")
    note_cls = (
        "good" if note_rate and note_rate >= 0.85 else
        ("warn" if note_rate and note_rate >= 0.6 else "bad")
    )

    cards = (
        '<div class="kpi-grid">'
        + _kpi_card("Conversations handled", fmt_int(total), "across all media")
        + _kpi_card(
            "Wrap-up note rate",
            f"{note_rate*100:.0f}%" if note_rate is not None else "—",
            f"{w.get('with_own_notes', 0)}/{w.get('with_wrapup_code', 0)} with notes",
            cls=note_cls,
        )
        + "</div>"
    )

    # Top dispositions
    disps = w.get("top_dispositions") or []
    rows = []
    for d, n in disps[:8]:
        rows.append(f'<tr><td>{escape(d)}</td><td class="num">{n}</td></tr>')
    disp_table = ""
    if rows:
        disp_table = (
            "<h3>Top wrap-up dispositions</h3>"
            '<table><thead><tr><th>Disposition</th><th class="num">Count</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
        )

    return f'<section id="wrap"><h2>3. Wrap-up &amp; handling</h2>{cards}{disp_table}</section>'


def render_flagged_section(pack: dict, cfg: TenantConfig | None = None) -> str:
    fc = pack.get("flagged_calls") or {}
    top = fc.get("top") or []
    total = fc.get("total_flagged") or 0
    intro = (
        f'<p>{total} call(s) flagged for review out of '
        f'{pack.get("wrap_discipline", {}).get("total_conversations") or 0} handled. '
        f"Showing top {len(top)} by composite flag score.</p>"
    )
    if not top:
        return (
            f'<section id="flagged"><h2>4. Flagged calls</h2>'
            '<div class="callout good"><strong>No calls flagged</strong> against the configured '
            "thresholds (sentiment, hold ratio, AHT excess, wrap-up notes). This is a "
            "legitimate finding — the agent is performing within thresholds on every "
            "conversation in the period.</div></section>"
        )
    rows: list[str] = []
    for c in top:
        reasons = "".join(
            f'<span class="flag-pill">{escape(r)}</span>' for r in c.get("flag_reasons") or []
        )
        sentiment = c.get("sentiment_score")
        sentiment_html = (
            f'{sentiment:+.2f}' if sentiment is not None else
            '<span class="muted">—</span>'
        )
        rows.append(
            f'<tr>'
            f'<td>{_date_short(c.get("started_at"))}</td>'
            f'<td>{escape((c.get("media") or "").upper())}</td>'
            f'<td class="num">{fmt_secs(c.get("handle_s"))}</td>'
            f'<td class="num">{fmt_secs(c.get("hold_s"))}</td>'
            f'<td class="num">{sentiment_html}</td>'
            f'<td>{reasons}</td>'
            f'<td>{render_conversation_cell(c.get("conversation_id"), tenant_base_url=cfg.tenant.genesys_app_base_url if cfg else None)}</td>'
            f'</tr>'
        )
    return (
        f'<section id="flagged"><h2>4. Flagged calls</h2>{intro}'
        '<table><thead><tr><th>Started</th><th>Media</th><th class="num">Handle</th>'
        '<th class="num">Hold</th><th class="num">Sentiment</th><th>Reasons</th><th>Conv id</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></section>'
    )


def render_focus_section(pack: dict) -> str:
    focus = pack.get("recommended_focus") or []
    if not focus:
        return (
            '<section id="focus"><h2>5. Recommended coaching focus</h2>'
            '<div class="callout good"><strong>No focus areas surfaced.</strong> The agent is '
            "broadly on target across AHT, ACW, wrap-up discipline, QA, sentiment, and hold time. "
            "Use this session to recognise that and explore career-development goals.</div></section>"
        )
    cards = []
    for f in focus:
        cards.append(
            f'<div class="focus-card">'
            f'<span class="rank">{f["rank"]}</span>'
            f'<span class="area">{escape(f["area"])}</span>'
            f'<div class="headline">{escape(f["headline"])}</div>'
            f"</div>"
        )
    return (
        f'<section id="focus"><h2>5. Recommended coaching focus</h2>'
        '<p>Heuristic priorities — highest-leverage areas for this period given the data. '
        "Use as a starting point, not a rigid script. Acknowledge what's going well first.</p>"
        f'{"".join(cards)}</section>'
    )


def _date_short(s):
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y")
    except Exception:
        return s[:10]


def render_html(pack: dict, period: str, cfg: TenantConfig) -> str:
    body = (
        render_header(pack, period, cfg)
        + render_toc()
        + render_performance_section(pack, cfg)
        + render_sentiment_quality(pack)
        + render_wrap_section(pack)
        + render_flagged_section(pack, cfg)
        + render_focus_section(pack)
        + (
            f'<footer>Generated {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")} · '
            f"genesys-mcp cc-coaching-prep · "
            f"<a href=\"https://github.com/laggyzee/genesys-mcp\">github.com/laggyzee/genesys-mcp</a></footer>"
        )
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<title>Coaching brief — {escape(pack.get("agent", {}).get("name") or "agent")} · {escape(period)}</title>'
        f"<style>{CSS}</style></head>"
        f'<body><div class="wrap">{body}</div></body></html>'
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--coaching-pack", required=True, help="Path to coaching_pack.json")
    p.add_argument("--agent-slug", required=True, help="Agent slug for filename, e.g. 'anthony-kha'")
    p.add_argument("--period", required=True, help="Human period label, e.g. 'April 2026'")
    p.add_argument("--period-slug", required=True, help="Period slug for filename, e.g. 'april-2026'")
    p.add_argument("--output", default=None,
                   help="Output HTML path (defaults to cfg.coaching_output_path).")
    args = p.parse_args()

    cfg = load_config()
    pack = json.loads(Path(args.coaching_pack).read_text())
    html = render_html(pack, args.period, cfg)

    out_path = (
        Path(args.output).expanduser() if args.output
        else cfg.coaching_output_path(args.agent_slug, args.period_slug)
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    print(f"Wrote {out_path} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
