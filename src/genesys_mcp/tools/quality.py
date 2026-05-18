"""Quality Management tools — agent evaluation scorecards.

Wraps Genesys Quality Management endpoints to surface per-agent QA scores,
pass rates, and evaluator workload over an interval. Pairs with
``agent_coaching_pack`` (in reports.py) to make the coaching brief complete.

Requires the OAuth client to have ``quality:readonly``. The tool soft-fails
with ``scope_available: false`` when the scope isn't granted — same graceful
degrade pattern as the speech & text analytics tools — so callers that batch
across many users don't crash on a single tenant without QM enabled.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


def _split_interval(interval: str) -> tuple[str, str]:
    """Split a Genesys-style 'startISO/endISO' interval into its halves."""
    if "/" not in interval:
        raise ValueError(
            f"interval must be ISO-8601 'start/end' format, got {interval!r}"
        )
    start, end = interval.split("/", 1)
    return start, end


def _to_iso_z(s: str) -> str:
    """Normalise an ISO timestamp to the Z-suffix form Genesys query params expect."""
    s = s.strip()
    if s.endswith("Z"):
        return s
    # Strip explicit +00:00 -> Z
    if s.endswith("+00:00"):
        return s[:-6] + "Z"
    return s


def _safe_num(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _pull_evaluations_for_user(
    api: gc.QualityApi,
    user_id: str,
    start_iso: str,
    end_iso: str,
    page_size: int = 100,
    max_pages: int = 20,
) -> list[dict]:
    """Page through evaluations for a single agent in the interval.

    Always requests ``expand_answer_total_scores=True``; without it Genesys
    returns evaluations with an empty ``answers`` block — no scores at all.
    Per-question detail and evaluator comments are filtered later in
    ``_summarise_evaluation`` based on the caller's flag.
    """
    out: list[dict] = []
    for page_number in range(1, max_pages + 1):
        resp = with_retry(api.get_quality_evaluations_query)(
            agent_user_id=user_id,
            start_time=start_iso,
            end_time=end_iso,
            page_size=page_size,
            page_number=page_number,
            expand=["agent", "evaluator"],
            expand_answer_total_scores=True,
        )
        d = to_dict(resp)
        entities = d.get("entities") or []
        out.extend(entities)
        page_count = d.get("pageCount") or 0
        if page_number >= page_count:
            break
        if not entities:
            break
    return out


def _summarise_evaluation(evaluation: dict, include_question_detail: bool) -> dict:
    """Reduce a Genesys evaluation entity to the fields callers actually use.

    Evaluator comments + per-question detail are PII-sensitive (free-text from
    the evaluator referencing specific customer interactions). Only attached
    when ``include_question_detail=True``.
    """
    form = evaluation.get("evaluationForm") or {}
    agent = evaluation.get("agent") or {}
    evaluator = evaluation.get("evaluator") or {}
    score = evaluation.get("answers") or {}
    conversation = evaluation.get("conversation") or {}

    row: dict[str, Any] = {
        "id": evaluation.get("id"),
        "form_id": form.get("id"),
        "form_name": form.get("name"),
        "agent_id": agent.get("id"),
        "evaluator_id": evaluator.get("id"),
        "evaluator_name": evaluator.get("name"),
        "media_type": evaluation.get("mediaType") or [],
        "total_score": _safe_num(score.get("totalScore")),
        "critical_score": _safe_num(score.get("totalCriticalScore")),
        "critical_passed": (
            None if score.get("anyFailedKillQuestions") is None
            else not score.get("anyFailedKillQuestions")
        ),
        "agent_has_read": evaluation.get("agentHasRead"),
        "status": evaluation.get("status"),
        "released_date": evaluation.get("releaseDate") or evaluation.get("assignedDate"),
        "conversation_id": conversation.get("id"),
    }
    if include_question_detail:
        row["evaluator_summary_comments"] = score.get("comments")
        row["question_group_scores"] = score.get("questionGroupScores")
    return row


def _aggregate_user(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {
            "n_evaluations": 0,
            "avg_score": None,
            "pass_rate": None,
            "critical_pass_rate": None,
            "last_evaluated_at": None,
        }
    scores = [r["total_score"] for r in rows if r["total_score"] is not None]
    crit = [r["critical_passed"] for r in rows if r["critical_passed"] is not None]
    last_dates = [r["released_date"] for r in rows if r["released_date"]]
    # Pass rate uses 80% as the implicit pass mark (tenant-config could override later
    # but Genesys evaluations are typically interpreted on that scale).
    pass_rate = (
        sum(1 for s in scores if s >= 80) / len(scores) if scores else None
    )
    return {
        "n_evaluations": len(rows),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "pass_rate": round(pass_rate, 3) if pass_rate is not None else None,
        "critical_pass_rate": (
            round(sum(1 for c in crit if c) / len(crit), 3) if crit else None
        ),
        "last_evaluated_at": max(last_dates) if last_dates else None,
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def qa_evaluations(
        user_ids: list[str] = Field(
            description="Genesys user ids (agents) to pull QA scores for.",
        ),
        interval: str = Field(
            description=(
                "ISO-8601 interval 'startISO/endISO' (UTC). Filters by evaluation "
                "release date for the agent. Example: "
                "'2026-04-01T00:00:00Z/2026-05-01T00:00:00Z'."
            ),
        ),
        include_question_detail: bool = Field(
            default=False,
            description=(
                "Include per-question scores and evaluator free-text comments. "
                "Off by default because comments can be PII-sensitive."
            ),
        ),
    ) -> dict:
        """Per-agent Quality Management evaluation summary over an interval.

        Returns one row per user with averages and pass rates, plus the raw
        per-evaluation list (form name, evaluator, score, critical pass,
        release date, conversation id). When ``include_question_detail`` is
        true, evaluator comments and per-question-group breakdowns are
        attached — opt-in because comments can contain PII.

        Soft-fails on 403 with ``scope_available: false`` so callers can batch
        across tenants where ``quality:readonly`` isn't granted without
        crashing — same pattern as the speech & text analytics tools.

        Pairs with ``agent_coaching_pack`` (which calls this internally to
        layer QA scores onto the coaching brief) and could be added to the
        workforce section of ``cc-monthly-report`` for team-wide QA stats.
        """
        start, end = _split_interval(interval)
        start_iso = _to_iso_z(start)
        end_iso = _to_iso_z(end)
        api = gc.QualityApi(get_api())

        per_user: dict[str, dict] = {}
        per_evaluator: dict[str, dict] = defaultdict(
            lambda: {"name": None, "n_evaluations": 0}
        )

        for user_id in user_ids:
            try:
                entities = _pull_evaluations_for_user(
                    api, user_id, start_iso, end_iso,
                )
            except gc.rest.ApiException as exc:
                if exc.status == 403:
                    return {
                        "scope_available": False,
                        "message": (
                            "OAuth client lacks 'quality:readonly' scope; cannot "
                            "fetch QA evaluations. Grant the scope and retry."
                        ),
                    }
                raise

            rows = [
                _summarise_evaluation(e, include_question_detail) for e in entities
            ]
            agent_name = (
                entities[0].get("agent", {}).get("name") if entities else None
            )
            per_user[user_id] = {
                "user_id": user_id,
                "user_name": agent_name,
                "summary": _aggregate_user(rows),
                "evaluations": rows,
            }
            for r in rows:
                eid = r["evaluator_id"]
                if not eid:
                    continue
                per_evaluator[eid]["name"] = r["evaluator_name"]
                per_evaluator[eid]["n_evaluations"] += 1

        return {
            "scope_available": True,
            "interval": f"{start_iso}/{end_iso}",
            "user_count": len(user_ids),
            "per_user": per_user,
            "per_evaluator": dict(per_evaluator),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
