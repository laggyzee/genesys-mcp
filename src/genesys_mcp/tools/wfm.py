"""Workforce Management tools — management units, agent adherence explanations,
and a composition tool that pairs presence sessions with adherence info.

Requires the OAuth client to have ``workforce-management:readonly``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry
from genesys_mcp.naming import resolver

logger = logging.getLogger(__name__)


def _default_interval(days: int = 7) -> str:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    return f"{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}/{end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_management_units(
        page_size: int = Field(default=100, ge=1, le=200),
        page_number: int = Field(default=1, ge=1),
    ) -> dict:
        """List WFM management units across all business units.

        Most ops questions need the management unit id, not the business unit id.
        Use this to find the MU for the contact-centre ops you care about.
        """
        api = gc.WorkforceManagementApi(get_api())
        resp = with_retry(api.get_workforcemanagement_managementunits)(
            page_size=page_size, page_number=page_number
        )
        rows = [
            {"id": mu.id, "name": mu.name,
             "business_unit_id": getattr(getattr(mu, "business_unit", None), "id", None)}
            for mu in (resp.entities or [])
        ]
        return {
            "total": resp.total,
            "page_number": resp.page_number,
            "page_size": resp.page_size,
            "management_units": rows,
        }

    @mcp.tool()
    def get_user_management_unit(
        user_id: str = Field(description="User id."),
    ) -> dict:
        """Look up which WFM management unit a user belongs to. Required input
        for adherence/schedule queries.
        """
        api = gc.WorkforceManagementApi(get_api())
        try:
            resp = with_retry(api.get_workforcemanagement_agent_managementunit)(agent_id=user_id)
            return to_dict(resp)
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                return {"status": 404, "user_id": user_id, "managementUnit": None}
            raise

    @mcp.tool()
    def query_agent_adherence_explanations(
        user_id: str = Field(description="User (agent) id."),
        interval: str | None = Field(
            default=None,
            description="ISO-8601 interval. Defaults to last 7 days UTC.",
        ),
    ) -> dict:
        """Adherence explanations for an agent over a date range.

        Adherence explanations are the entries supervisors/agents log to explain
        why someone was off-schedule (training, sick, system issue, approved
        unscheduled break, etc.). Returns one row per explanation with status,
        type, and time range.

        Pair with break_overrun_report or presence_sessions: an unexplained
        overrun is more concerning than one with a logged 'training' explanation.
        """
        interval = interval or _default_interval(7)
        try:
            start_str, end_str = interval.split("/", 1)
            start_iso = start_str
            end_iso = end_str
        except ValueError:
            raise ValueError(f"Invalid interval {interval!r}; expected 'start/end'")

        api = gc.WorkforceManagementApi(get_api())
        body = {"startDate": start_iso, "endDate": end_iso}
        resp = with_retry(api.post_workforcemanagement_agent_adherence_explanations_query)(
            agent_id=user_id, body=body
        )
        return to_dict(resp)

    @mcp.tool()
    def agent_adherence_review(
        user_ids: list[str] = Field(
            description="User ids to review. Returns one block per user.",
        ),
        interval: str | None = Field(
            default=None,
            description="ISO-8601 interval. Defaults to last 7 days UTC.",
        ),
        break_target_min: int = Field(
            default=15, ge=1, le=120,
            description="Target break duration in minutes (default 15).",
        ),
        meal_target_min: int = Field(
            default=30, ge=1, le=120,
            description="Target meal duration in minutes (default 30).",
        ),
        tolerance_min: int = Field(
            default=2, ge=0, le=10,
            description="Grace minutes before flagging an overrun.",
        ),
    ) -> dict:
        """Composition tool: presence break/meal overruns + WFM adherence explanations side by side.

        For each user, returns:
        - Break/meal sessions with overrun flags (same logic as break_overrun_report)
        - WFM-logged adherence explanations covering the same window
        - For each overrun, marks whether a matching adherence explanation exists

        An overrun WITH an explanation = expected variance (training, approved time off).
        An overrun WITHOUT an explanation = the kind of pattern that needs a TL conversation.

        Note: this tool does NOT compare actual vs scheduled (that requires the
        async historical-adherence flow + published schedule lookup). It surfaces
        the simpler "was this break overrun explained" signal which is usually
        what TLs need first.
        """
        if not user_ids:
            raise ValueError("user_ids must contain at least one id.")
        interval = interval or _default_interval(7)
        try:
            start_str, end_str = interval.split("/", 1)
            interval_start = _parse_iso(start_str)
            interval_end = _parse_iso(end_str)
        except Exception:
            raise ValueError(f"Invalid interval {interval!r}")

        # 1. Presence sessions (BREAK/MEAL only) for all users via one job
        analytics_api = gc.AnalyticsApi(get_api())
        body = {
            "interval": interval,
            "order": "asc",
            "userFilters": [{
                "type": "or",
                "predicates": [
                    {"type": "dimension", "dimension": "userId",
                     "operator": "matches", "value": uid}
                    for uid in user_ids
                ],
            }],
        }
        submit = with_retry(analytics_api.post_analytics_users_details_jobs)(body=body)
        job_id = submit.job_id if hasattr(submit, "job_id") else to_dict(submit).get("jobId")
        for _ in range(30):
            status = with_retry(analytics_api.get_analytics_users_details_job)(job_id=job_id)
            state = getattr(status, "state", None) or to_dict(status).get("state")
            if state == "FULFILLED":
                break
            if state in ("FAILED", "CANCELLED", "EXPIRED"):
                raise RuntimeError(f"job {job_id} terminated in state {state}")
            time.sleep(1)

        sessions_by_user: dict[str, list[dict]] = {uid: [] for uid in user_ids}
        cursor = None
        for _ in range(20):
            kwargs: dict[str, Any] = {"job_id": job_id, "page_size": 100}
            if cursor:
                kwargs["cursor"] = cursor
            page = with_retry(analytics_api.get_analytics_users_details_job_results)(**kwargs)
            page_dict = to_dict(page) or {}
            for ud in page_dict.get("userDetails") or []:
                uid = ud.get("userId")
                if uid not in sessions_by_user:
                    continue
                for sess in ud.get("primaryPresence") or []:
                    sp = (sess.get("systemPresence") or "").upper()
                    if sp not in ("BREAK", "MEAL"):
                        continue
                    if not sess.get("startTime") or not sess.get("endTime"):
                        continue
                    try:
                        st = _parse_iso(sess["startTime"])
                        en = _parse_iso(sess["endTime"])
                    except Exception:
                        continue
                    if en < interval_start or st > interval_end:
                        continue
                    st_clip = max(st, interval_start)
                    en_clip = min(en, interval_end)
                    dur_s = (en_clip - st_clip).total_seconds()
                    if dur_s <= 0:
                        continue
                    target_s = (break_target_min if sp == "BREAK" else meal_target_min) * 60
                    sessions_by_user[uid].append({
                        "presence": sp,
                        "start_utc": st_clip,
                        "end_utc": en_clip,
                        "duration_min": round(dur_s / 60, 1),
                        "target_min": target_s // 60,
                        "over_target": dur_s > (target_s + tolerance_min * 60),
                        "overrun_min": round((dur_s - target_s) / 60, 1) if dur_s > target_s else 0.0,
                    })

            cursor = page_dict.get("cursor")
            if not cursor:
                break

        # 2. WFM adherence explanations per user
        wfm_api = gc.WorkforceManagementApi(get_api())
        explanations_by_user: dict[str, list[dict]] = {}
        for uid in user_ids:
            try:
                resp = with_retry(wfm_api.post_workforcemanagement_agent_adherence_explanations_query)(
                    agent_id=uid, body={"startDate": interval.split("/")[0], "endDate": interval.split("/")[1]}
                )
                expls = to_dict(resp).get("entities") or []
                explanations_by_user[uid] = expls
            except Exception as exc:
                logger.warning("WFM adherence query failed for %s: %s", uid, exc)
                explanations_by_user[uid] = []

        # 3. For each overrun session, flag whether any explanation overlaps
        names = resolver.user_names(user_ids)
        out_users = []
        for uid in user_ids:
            expls = explanations_by_user.get(uid, [])
            expl_intervals = []
            for e in expls:
                try:
                    e_start = _parse_iso(e["startDate"])
                    e_end = _parse_iso(e["endDate"])
                    expl_intervals.append((e_start, e_end, e))
                except Exception:
                    continue

            sessions_out = []
            unexplained_overruns = 0
            explained_overruns = 0
            for s in sessions_by_user[uid]:
                matching_expl = None
                if s["over_target"]:
                    for e_start, e_end, e in expl_intervals:
                        if s["start_utc"] < e_end and s["end_utc"] > e_start:
                            matching_expl = {
                                "type": e.get("type"),
                                "status": e.get("status"),
                                "notes": e.get("notes"),
                            }
                            break
                    if matching_expl:
                        explained_overruns += 1
                    else:
                        unexplained_overruns += 1
                sessions_out.append({
                    "presence": s["presence"],
                    "start_utc": s["start_utc"].isoformat().replace("+00:00", "Z"),
                    "end_utc": s["end_utc"].isoformat().replace("+00:00", "Z"),
                    "duration_min": s["duration_min"],
                    "target_min": s["target_min"],
                    "over_target": s["over_target"],
                    "overrun_min": s["overrun_min"],
                    "matching_explanation": matching_expl,
                })

            out_users.append({
                "user_id": uid,
                "user_name": names.get(uid),
                "session_count": len(sessions_out),
                "explained_overruns": explained_overruns,
                "unexplained_overruns": unexplained_overruns,
                "explanations_logged": len(expls),
                "sessions": sessions_out,
                "explanations": expls,
            })

        # Sort by unexplained overruns descending — that's the actionable list
        out_users.sort(key=lambda r: -r["unexplained_overruns"])

        return {
            "interval": interval,
            "break_target_min": break_target_min,
            "meal_target_min": meal_target_min,
            "users": out_users,
        }
