"""Presence tools — agent break/meal/away/etc session-level data via the
analytics/users/details async-jobs API, but presented as a one-shot tool that
hides the submit-poll-paginate dance.
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

logger = logging.getLogger(__name__)


def _default_interval(days: int = 7) -> str:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    return f"{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}/{end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def presence_sessions(
        user_ids: list[str] = Field(
            description="User ids to fetch presence sessions for. Use list_users / find_user to resolve.",
        ),
        interval: str | None = Field(
            default=None,
            description="ISO-8601 interval 'start/end'. Defaults to last 7 days UTC.",
        ),
        presence_filter: list[str] | None = Field(
            default=None,
            description="systemPresence values to keep, e.g. ['BREAK','MEAL','AWAY']. "
            "Defaults to ['BREAK','MEAL','AWAY']. Pass an empty list to return ALL presence sessions.",
        ),
        max_pages: int = Field(
            default=20,
            ge=1,
            le=100,
            description="Safety cap on result pagination (each page ~100 sessions).",
        ),
    ) -> dict:
        """Per-user presence sessions (clipped to the interval) for break/meal/away analysis.

        Wraps /api/v2/analytics/users/details/jobs (submit → poll → paginate) into a
        single call. Returns a flat list per user with start_utc, end_utc, duration_s,
        and the systemPresence label. Each session is clipped to the requested interval.

        Common usage:
        - Break/lunch overrun checks: filter to ['BREAK','MEAL'] (the default), then look
          for sessions where duration_s > target.
        - Adherence cross-check: pair with WFM adherence (Wave 3 tool agent_adherence_review).

        Caveats:
        - Sessions still open at interval end are excluded (no end_time means we can't
          measure duration reliably).
        - Use the AnalyticsApi job results cursor under the hood; if the cap is hit,
          'truncated': true is set in the response.
        """
        if not user_ids:
            raise ValueError("user_ids must contain at least one id.")
        if presence_filter is None:
            presence_filter = ["BREAK", "MEAL", "AWAY"]
        keep = {p.upper() for p in presence_filter} if presence_filter else None

        interval = interval or _default_interval(7)
        try:
            start_str, end_str = interval.split("/", 1)
            interval_start = _parse_iso(start_str)
            interval_end = _parse_iso(end_str)
        except Exception as exc:
            raise ValueError(f"Invalid interval {interval!r}: {exc}") from exc

        api = gc.AnalyticsApi(get_api())
        body = {
            "interval": interval,
            "order": "asc",
            "userFilters": [
                {
                    "type": "or",
                    "predicates": [
                        {"type": "dimension", "dimension": "userId",
                         "operator": "matches", "value": uid}
                        for uid in user_ids
                    ],
                }
            ],
        }
        submit = with_retry(api.post_analytics_users_details_jobs)(body=body)
        job_id = submit.job_id if hasattr(submit, "job_id") else to_dict(submit).get("jobId")
        if not job_id:
            raise RuntimeError(f"users/details/jobs submit returned no jobId: {to_dict(submit)}")

        # Poll until FULFILLED (jobs typically finish within 5–15s)
        for _ in range(30):
            status_resp = with_retry(api.get_analytics_users_details_job)(job_id=job_id)
            state = getattr(status_resp, "state", None) or to_dict(status_resp).get("state")
            if state == "FULFILLED":
                break
            if state in ("FAILED", "CANCELLED", "EXPIRED"):
                raise RuntimeError(f"job {job_id} terminated in state {state}")
            time.sleep(1)
        else:
            raise RuntimeError(f"job {job_id} did not reach FULFILLED within 30s")

        # Paginate results, collect primaryPresence per user
        sessions_by_user: dict[str, list[dict]] = {uid: [] for uid in user_ids}
        cursor: str | None = None
        truncated = False

        for page_idx in range(max_pages):
            kwargs: dict[str, Any] = {"job_id": job_id, "page_size": 100}
            if cursor:
                kwargs["cursor"] = cursor
            page = with_retry(api.get_analytics_users_details_job_results)(**kwargs)
            page_dict = to_dict(page) or {}
            details = page_dict.get("userDetails") or []
            cursor = page_dict.get("cursor")

            for ud in details:
                uid = ud.get("userId")
                if uid not in sessions_by_user:
                    continue
                for sess in (ud.get("primaryPresence") or []):
                    sp = (sess.get("systemPresence") or "").upper()
                    if keep and sp not in keep:
                        continue
                    st_raw = sess.get("startTime")
                    en_raw = sess.get("endTime")
                    if not st_raw or not en_raw:
                        continue
                    try:
                        st = _parse_iso(st_raw)
                        en = _parse_iso(en_raw)
                    except Exception:
                        continue
                    # Clip to interval
                    if en < interval_start or st > interval_end:
                        continue
                    st_clip = max(st, interval_start)
                    en_clip = min(en, interval_end)
                    dur = (en_clip - st_clip).total_seconds()
                    if dur <= 0:
                        continue
                    sessions_by_user[uid].append({
                        "system_presence": sp,
                        "organization_presence_id": sess.get("organizationPresenceId"),
                        "start_utc": st_clip.isoformat().replace("+00:00", "Z"),
                        "end_utc": en_clip.isoformat().replace("+00:00", "Z"),
                        "duration_s": int(dur),
                        "duration_minutes": round(dur / 60, 1),
                    })

            if not cursor:
                break
        else:
            truncated = True

        # Build result with totals per user
        out_users = []
        for uid in user_ids:
            sessions = sessions_by_user[uid]
            total_s = sum(s["duration_s"] for s in sessions)
            counts: dict[str, int] = {}
            durations: dict[str, int] = {}
            for s in sessions:
                sp = s["system_presence"]
                counts[sp] = counts.get(sp, 0) + 1
                durations[sp] = durations.get(sp, 0) + s["duration_s"]
            out_users.append({
                "user_id": uid,
                "session_count": len(sessions),
                "total_duration_s": total_s,
                "by_presence_count": counts,
                "by_presence_duration_s": durations,
                "sessions": sessions,
            })

        return {
            "interval": interval,
            "presence_filter": list(keep) if keep else None,
            "truncated": truncated,
            "users": out_users,
        }
