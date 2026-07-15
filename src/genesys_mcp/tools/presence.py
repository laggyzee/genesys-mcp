"""Presence tools — agent break/meal/away/etc session-level analytics data."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._intervals import INTERVAL_HELP_STRING
from genesys_mcp._intervals import default_interval as _default_interval
from genesys_mcp._intervals import parse_iso as _parse_iso
from genesys_mcp._user_details import fetch_user_details

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

        Uses the authoritative asynchronous archive when it has settled. While that
        archive is lagging, it can use the recent synchronous user-detail endpoint only
        after reconciling active-user presence durations against user aggregates.
        Returns a flat list per user with start_utc, end_utc, duration_s, and the
        systemPresence label. Each session is clipped to the requested interval.

        Common usage:
        - Break/lunch overrun checks: filter to ['BREAK','MEAL'] (the default), then look
          for sessions where duration_s > target.
        - Adherence cross-check: pair with WFM adherence (Wave 3 tool agent_adherence_review).

        Caveats:
        - Sessions still open at interval end are excluded (no end_time means we can't
          measure duration reliably).
        - Use the AnalyticsApi job results cursor under the hood; if the cap is hit,
          'truncated': true is set in the response.
        - ``data_complete`` means the returned rows are safe to use. When
          ``data_provisional`` is true, they came from a reconciled recent-data fallback;
          ``archive_data_complete`` remains false so consumers can repair from the
          authoritative archive later.
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

        detail_result = fetch_user_details(user_ids, interval, max_pages)
        sessions_by_user: dict[str, list[dict]] = {uid: [] for uid in user_ids}
        for ud in detail_result["user_details"]:
            uid = ud.get("userId")
            if uid not in sessions_by_user:
                continue
            for sess in (ud.get("primaryPresence") or []):
                sp = (sess.get("systemPresence") or "").upper()
                org_pres_id = sess.get("organizationPresenceId")
                is_pre_break = (
                    pre_break_organization_presence_id is not None
                    and org_pres_id == pre_break_organization_presence_id
                )
                label = "PRE_BREAK" if is_pre_break else sp
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
            "truncated": detail_result["truncated"],
            "data_complete": detail_result["data_complete"],
            "archive_data_complete": detail_result["archive_data_complete"],
            "data_provisional": detail_result["data_provisional"],
            "data_source": detail_result["data_source"],
            "data_available_until": detail_result["data_available_until"],
            "data_availability_note": detail_result["data_availability_note"],
            "fallback_validation": detail_result["fallback_validation"],
            "users": out_users,
        }
