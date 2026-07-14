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

from genesys_mcp._availability import presence_data_availability
from genesys_mcp._intervals import INTERVAL_HELP_STRING
from genesys_mcp._intervals import default_interval as _default_interval
from genesys_mcp._intervals import parse_iso as _parse_iso
from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def presence_sessions(
        user_ids: list[str] = Field(
            description="User ids to fetch presence sessions for. Use list_users / find_user to resolve.",
        ),
        interval: str | None = Field(
            default=None,
            description=INTERVAL_HELP_STRING,
        ),
        presence_filter: list[str] | None = Field(
            default=None,
            description="systemPresence values to keep, e.g. ['BREAK','MEAL','AWAY']. "
            "Defaults to ['BREAK','MEAL','AWAY']. Pass an empty list to return ALL presence sessions.",
        ),
        max_pages: int = Field(
            default=50,
            ge=1,
            le=200,
            description="Safety cap on result pagination (each page returns up to 1000 userDetails records).",
        ),
        pre_break_organization_presence_id: str | None = Field(
            default=None,
            description=(
                "Optional. Org-level 'Pre Break' presence UUID. When set, "
                "BUSY sessions carrying this organizationPresenceId are "
                "included (re-labelled as 'PRE_BREAK') even if BUSY isn't "
                "in `presence_filter`. Mirrors `break_overrun_report`'s "
                "behaviour. Pass `cfg.presence.pre_break_organisation_presence_id` "
                "from tenant.yaml. When None, pre-break sessions are not "
                "specifically surfaced (they fall under generic BUSY)."
            ),
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
        - Data availability: presence detail settles asynchronously. The response
          carries 'data_complete' (False when the interval extends past Genesys'
          settled watermark 'data_available_until'), plus a 'data_availability_note'.
          When data_complete is False, sessions after the watermark are MISSING —
          do not treat the last returned session as the agent's logout, and treat
          per-presence totals as lower bounds.
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
        # Presence detail data settles asynchronously; a window extending past
        # the availability watermark returns partial with no error. Check it up
        # front so the response can flag incompleteness rather than imply the
        # last recorded session was the agent's real logout.
        availability = presence_data_availability(api, interval_end)
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
            kwargs: dict[str, Any] = {"job_id": job_id, "page_size": 1000}
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
                    org_pres_id = sess.get("organizationPresenceId")
                    # v1.3: pre-break detection — BUSY presence with the
                    # configured org_id gets re-labelled to PRE_BREAK so
                    # filter logic and downstream consumers can treat it
                    # as a distinct category.
                    is_pre_break = (
                        pre_break_organization_presence_id is not None
                        and org_pres_id == pre_break_organization_presence_id
                    )
                    label = "PRE_BREAK" if is_pre_break else sp
                    # Standard filter: skip unless either (a) in the keep set
                    # or (b) it's a configured pre-break session.
                    if keep and label not in keep and not is_pre_break:
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
                        "system_presence": label,
                        "organization_presence_id": org_pres_id,
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
            # v1.17: data-availability watermark. `data_complete` is False when
            # the interval extends past what Genesys has settled — sessions after
            # `data_available_until` are omitted, so totals and any inferred
            # logout time are partial. Consumers must not treat the last session
            # as the agent's real end-of-day when this is False.
            "data_complete": availability["complete"],
            "data_available_until": availability["data_available_until"],
            "data_availability_note": availability["note"],
            "users": out_users,
        }
