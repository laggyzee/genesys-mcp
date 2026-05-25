#!/usr/bin/env python3
"""Generate a Markdown reconciliation checklist from raw MCP tool dumps.

Pairs every key MCP number with a concrete Genesys UI navigation path so a
human can click through and verify exact-match parity. Run before each
release to catch silent drift.

Inputs (saved by the mcp-reconcile skill's Step 2):
    /tmp/cc-reconcile-{period}/queue_performance.json
    /tmp/cc-reconcile-{period}/agent_performance.json
    /tmp/cc-reconcile-{period}/break_overrun_report.json
    /tmp/cc-reconcile-{period}/qa_evaluations.json
    /tmp/cc-reconcile-{period}/qmap.json          # queueId → [brand, queue_name]
    /tmp/cc-reconcile-{period}/user_roles.json    # userId → [name, role]

Output: a single Markdown file at ``cfg.reports.output_dir/reconcile-{period}.md``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from genesys_mcp.tenant import load_config  # noqa: E402


def _fmt_int(n) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}"


def _fmt_secs(n) -> str:
    if n is None:
        return "—"
    s = int(round(n))
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


def _queue_rows(qp: dict, qmap: dict) -> list[dict]:
    """Per-(queue, media) answered counts. Sourced from derived.answered (tAnswered.count)."""
    out: list[dict] = []
    for r in qp.get("results") or []:
        grp = r.get("group") or {}
        qid = grp.get("queueId")
        media = grp.get("mediaType")
        if not qid or media not in ("voice", "message"):
            continue
        brand, qname = qmap.get(qid, ["?", qid])
        for bucket in r.get("data") or []:
            derived = bucket.get("derived") or {}
            answered = derived.get("answered") or 0
            sl_pct = derived.get("service_level_pct")
            avg_handle = derived.get("avg_handle_s")
            out.append({
                "queue_name": qname,
                "brand": brand,
                "media": media,
                "answered": answered,
                "sl_pct": sl_pct,
                "avg_handle_s": avg_handle,
            })
    return sorted(out, key=lambda r: (r["brand"], r["queue_name"], r["media"]))


def _agent_voice_rows(ap: dict, user_roles: dict) -> list[dict]:
    """Per-user voice AHT + answered. Filter to specialists (the reconciliation focus).

    agent_performance returns raw metrics (no derived block). Read tAnswered.count
    for the answered total and compute AHT from tHandle.sum / tHandle.count (ms).
    """
    out: list[dict] = []
    per_user: dict[str, dict] = {}
    for r in ap.get("results") or []:
        grp = r.get("group") or {}
        uid = grp.get("userId")
        media = grp.get("mediaType")
        if not uid or media != "voice":
            continue
        bucket = (r.get("data") or [{}])[0]
        metrics = {m["metric"]: (m.get("stats") or {}) for m in (bucket.get("metrics") or [])}
        answered = int(metrics.get("tAnswered", {}).get("count", 0) or 0)
        handle_count = int(metrics.get("tHandle", {}).get("count", 0) or 0)
        handle_sum_ms = float(metrics.get("tHandle", {}).get("sum", 0) or 0)
        voice_aht_s = (handle_sum_ms / 1000.0 / handle_count) if handle_count else None
        per_user[uid] = {
            "user_id": uid,
            "name": user_roles.get(uid, ["?", "?"])[0],
            "role": user_roles.get(uid, ["?", "?"])[1],
            "voice_answered": answered,
            "voice_aht_s": voice_aht_s,
        }
    return sorted(
        [r for r in per_user.values() if r["voice_answered"] >= 5],
        key=lambda r: -r["voice_answered"],
    )


def _qa_rows(qa: dict, user_roles: dict) -> list[dict]:
    if not qa.get("scope_available"):
        return []
    out: list[dict] = []
    for uid, payload in (qa.get("per_user") or {}).items():
        summary = payload.get("summary") or {}
        n = summary.get("n_evaluations") or 0
        if n == 0:
            continue
        out.append({
            "user_id": uid,
            "name": user_roles.get(uid, ["?", "?"])[0],
            "n_evaluations": n,
            "avg_score": summary.get("avg_score"),
            "pass_rate": summary.get("pass_rate"),
        })
    return sorted(out, key=lambda r: -r["n_evaluations"])


def _adherence_rows(brk: dict, user_roles: dict) -> list[dict]:
    out: list[dict] = []
    for r in brk.get("users") or []:
        uid = r.get("user_id")
        pb = r.get("pre_break_overrun_total_min") or 0
        if pb <= 0:
            continue
        out.append({
            "user_id": uid,
            "name": user_roles.get(uid, ["?", "?"])[0],
            "pre_break_overrun_min": pb,
            "pre_break_overrun_count": r.get("pre_break_overrun_count") or 0,
        })
    return sorted(out, key=lambda r: -r["pre_break_overrun_min"])[:15]  # cap at top 15 — checklist must be doable


# ── Markdown render ──

def _render_md(period: str, interval: str, q_rows: list[dict],
               agent_rows: list[dict], qa_rows: list[dict],
               adherence_rows: list[dict]) -> str:
    parts: list[str] = []
    parts.append(f"# MCP Reconciliation — {period}\n")
    parts.append(f"_Interval: `{interval}` (UTC)._\n")
    parts.append(
        "This document lists every key number the MCP reports for the period "
        "above, paired with the **exact Genesys UI path** to verify each one. "
        "Tick each row off in the live UI; any mismatch is the signal to "
        "investigate before merging the next release.\n"
    )
    parts.append(
        "**How to use:** open Genesys Cloud → Performance / Quality / Workforce "
        "as referenced in each section. Set the period filter to match the "
        "interval above. Compare the UI value against the MCP value — they "
        "should be **exact matches** (the MCP queries the same endpoints the "
        "UI uses). Rounding differences of ≤1 are acceptable on time-based "
        "metrics; volume counts should be identical.\n"
    )

    # ── Section 1: Queues ──
    parts.append("## 1. Voice + message answered per queue\n")
    parts.append(
        "Source: `queue_performance` → `derived.answered` (sourced from "
        "`tAnswered.count`, the canonical UI 'Answered' column).\n\n"
        "**Verify in Genesys UI:** Performance → Queues → set the date "
        "filter to the interval above → switch between Voice and Message "
        "tabs → look for the 'Answered' column on each row.\n"
    )
    parts.append(
        "| ✓ | Queue | Media | MCP answered | MCP SL% | MCP avg handle | Notes |\n"
        "|---|---|---|---:|---:|---:|---|\n"
    )
    for r in q_rows:
        sl = f"{r['sl_pct']:.1f}%" if r["sl_pct"] is not None else "—"
        ah = _fmt_secs(r["avg_handle_s"])
        parts.append(
            f"| ☐ | {r['queue_name']} | {r['media']} | {_fmt_int(r['answered'])} | {sl} | {ah} | |\n"
        )
    parts.append("\n")

    # ── Section 2: Agents (voice AHT) ──
    parts.append("## 2. Agent voice AHT + answered\n")
    parts.append(
        "Source: `agent_performance` → per-user voice `avg_handle_s` "
        "(sourced from `tHandle.sum / tHandle.count`).\n\n"
        "**Verify in Genesys UI:** Performance → Agents → set the date filter → "
        "Voice tab → 'Avg Handle' column. Only agents with ≥5 voice answered "
        "in the period are listed; thinner samples won't reconcile cleanly.\n"
    )
    parts.append(
        "| ✓ | Agent | Role | Voice answered | Voice AHT | Notes |\n"
        "|---|---|---|---:|---:|---|\n"
    )
    for r in agent_rows[:20]:  # cap: 20 rows is enough to spot-check parity
        parts.append(
            f"| ☐ | {r['name']} | {r['role']} | "
            f"{_fmt_int(r['voice_answered'])} | {_fmt_secs(r['voice_aht_s'])} | |\n"
        )
    if len(agent_rows) > 20:
        parts.append(f"\n_({len(agent_rows) - 20} more agents not shown — top-volume spot-check is sufficient.)_\n\n")
    else:
        parts.append("\n")

    # ── Section 3: Quality scores ──
    if qa_rows:
        parts.append("## 3. Quality evaluation scores per agent\n")
        parts.append(
            "Source: `qa_evaluations` → per-user `summary.avg_score` + "
            "`summary.n_evaluations`. Only agents with ≥1 released evaluation "
            "in the period are listed.\n\n"
            "**Verify in Genesys UI:** Quality → Reporting → set the date "
            "filter → filter by agent → average score across released "
            "evaluations.\n"
        )
        parts.append(
            "| ✓ | Agent | # evals | Avg score | Pass rate | Notes |\n"
            "|---|---|---:|---:|---:|---|\n"
        )
        for r in qa_rows:
            score = f"{r['avg_score']:.1f}%" if r["avg_score"] is not None else "—"
            passr = f"{r['pass_rate']*100:.0f}%" if r["pass_rate"] is not None else "—"
            parts.append(
                f"| ☐ | {r['name']} | {r['n_evaluations']} | {score} | {passr} | |\n"
            )
        parts.append("\n")
    else:
        parts.append("## 3. Quality evaluation scores per agent\n")
        parts.append(
            "_`qa_evaluations` returned no data — either `quality:readonly` "
            "scope is missing on the OAuth client, or no evaluations were "
            "released in this period. Skipping this section._\n\n"
        )

    # ── Section 4: Adherence (pre-break overrun) ──
    if adherence_rows:
        parts.append("## 4. Pre-break overrun per agent\n")
        parts.append(
            "Source: `break_overrun_report` → `pre_break_overrun_total_min`. "
            "Top 15 agents by total pre-break overrun minutes; agents with 0 "
            "overrun are omitted.\n\n"
            "**Verify in Genesys UI:** Workforce Management → Adherence → "
            "per-agent presence sessions → filter to the pre-break presence. "
            "Sum minutes where session duration > `pre_break_target_min` "
            "(default 10 min).\n\n"
            "_Rounding tolerance: ≤2 minutes per agent is acceptable._\n"
        )
        parts.append(
            "| ✓ | Agent | Pre-break overrun (min) | Overrun count | Notes |\n"
            "|---|---|---:|---:|---|\n"
        )
        for r in adherence_rows:
            parts.append(
                f"| ☐ | {r['name']} | {r['pre_break_overrun_min']:.0f} | "
                f"{r['pre_break_overrun_count']} | |\n"
            )
        parts.append("\n")

    parts.append("---\n")
    parts.append(
        "## After reconciling\n\n"
        "- **All ticks ✓:** numbers match the UI. Safe to tag the release / "
        "merge the refactor / ship the report.\n"
        "- **Any ✗:** flag the row in the Notes column with the discrepancy "
        "(UI says X, MCP says Y). Investigate the source endpoint and "
        "filter shape before doing anything else.\n"
        "- **Patterns:** if multiple rows in the same section are off, the "
        "bug is likely in that tool's filter shape (the v0.2 fix territory). "
        "Run `pytest tests/test_analytics_filters.py` and check the canonical-shape "
        "assertions.\n"
    )
    return "".join(parts)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--period", required=True, help="Human period label, e.g. '12-18 May 2026'")
    p.add_argument("--interval", required=True, help="ISO interval the MCP tools were called with")
    p.add_argument("--data-dir", required=True)
    p.add_argument("--qmap-json", required=True)
    p.add_argument("--user-roles-json", required=True)
    p.add_argument("--output", default=None,
                   help="Output Markdown path. Defaults to "
                        "{cfg.reports.output_dir}/reconcile-{slug}.md")
    args = p.parse_args()

    cfg = load_config()
    data_dir = Path(args.data_dir)
    qmap = json.loads(Path(args.qmap_json).read_text())
    user_roles = json.loads(Path(args.user_roles_json).read_text())

    qp = json.loads((data_dir / "queue_performance.json").read_text())
    ap = json.loads((data_dir / "agent_performance.json").read_text())
    brk = json.loads((data_dir / "break_overrun_report.json").read_text())
    qa_path = data_dir / "qa_evaluations.json"
    qa = json.loads(qa_path.read_text()) if qa_path.exists() else {"scope_available": False}

    q_rows = _queue_rows(qp, qmap)
    agent_rows = _agent_voice_rows(ap, user_roles)
    qa_rows = _qa_rows(qa, user_roles)
    adherence_rows = _adherence_rows(brk, user_roles)

    md = _render_md(args.period, args.interval, q_rows, agent_rows,
                    qa_rows, adherence_rows)

    if args.output:
        out_path = Path(args.output).expanduser()
    else:
        slug = args.period.lower().replace(" ", "-").replace(",", "")
        out_path = Path(cfg.reports.output_dir).expanduser() / f"reconcile-{slug}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    total_rows = len(q_rows) + len(agent_rows) + len(qa_rows) + len(adherence_rows)
    print(f"OK wrote {out_path} ({len(md):,} bytes, {total_rows} reconciliation rows)")
    print(f"   {len(q_rows)} queue rows, {len(agent_rows)} agent rows, "
          f"{len(qa_rows)} QA rows, {len(adherence_rows)} adherence rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
