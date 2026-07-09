"""Workforce Management tools — management units, agent adherence explanations,
and a composition tool that pairs presence sessions with adherence info.

Requires the OAuth client to have ``workforce-management:readonly``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._aggregates import run_chunked_query
from genesys_mcp._envelopes import soft_fail_envelope
from genesys_mcp._intervals import INTERVAL_HELP_STRING
from genesys_mcp._intervals import default_interval as _default_interval
from genesys_mcp._intervals import parse_iso as _parse_iso
from genesys_mcp.client import get_api, to_dict, with_retry
from genesys_mcp.naming import resolver

logger = logging.getLogger(__name__)


def _bucket_key(dt: datetime, bucket_seconds: int) -> str:
    """Floor a datetime to a bucket boundary and return its ISO-8601 string.

    Used by ``volume_vs_forecast`` to join forecast quarter-hours with actual
    analytics aggregates into the same coarser bucket (e.g. 1-hour).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = int(dt.timestamp())
    floored = epoch - (epoch % bucket_seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_business_unit_tz(api_client: Any, business_unit_id: str) -> str | None:
    """Best-effort fetch of a business unit's WFM timezone (IANA name).

    WFM schedules and the headcount-forecast ``requiredPerInterval`` series are
    expressed in the BU's own timezone, so day boundaries must be computed in
    that zone — not UTC. Returns the IANA name (e.g. ``"Australia/Sydney"``) or
    ``None`` if it can't be resolved (the caller then falls back to UTC).
    """
    try:
        resp = with_retry(api_client.call_api)(
            resource_path=f"/api/v2/workforcemanagement/businessunits/{business_unit_id}",
            method="GET",
            query_params={"expand": "settings.timeZone"},
            auth_settings=["PureCloud OAuth"],
            response_type="object",
        ) or {}
    except Exception as exc:  # pragma: no cover - network/permission edge
        logger.warning("could not resolve BU %s timezone: %s", business_unit_id, exc)
        return None
    return ((resp.get("settings") or {}).get("timeZone")) or resp.get("timeZone")


def _safe_zoneinfo(tz_name: str | None) -> tuple[ZoneInfo, str]:
    """Resolve ``tz_name`` to a ``ZoneInfo``; fall back to UTC on miss.

    Returns ``(zoneinfo, resolved_name)`` so the response can report which zone
    was actually used for day bucketing.
    """
    if tz_name:
        try:
            return ZoneInfo(tz_name), tz_name
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("unknown timezone %r; bucketing schedule by UTC", tz_name)
    return ZoneInfo("UTC"), "UTC"


# Poll budget for the async historical-adherence download (linear backoff 1→5s).
_ADHERENCE_POLL_ATTEMPTS = 15


def _fetch_adherence_download(urls: list[str]) -> list[dict]:
    """Fetch + concatenate per-user adherence rows from the presigned result URL(s).

    Each bulk-job result file has the shape ``{managementUnitId, startDate,
    endDate, userResults: [...], lookupIdToSecondaryPresenceId}``. Returns the
    concatenated ``userResults``, each tagged with its file's
    ``managementUnitId`` so multi-MU results stay attributable. The URLs are
    presigned (the signature is the auth — no Bearer header needed) and are only
    handed back once the job is ``Complete``.
    """
    rows: list[dict] = []
    for url in urls:
        if not url:
            continue
        try:
            resp = httpx.get(url, timeout=30.0)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # pragma: no cover - network edge
            logger.warning("adherence download failed: %s", exc)
            continue
        if isinstance(payload, dict):
            mu = payload.get("managementUnitId")
            results = payload.get("userResults") or payload.get("data") or []
        else:
            mu, results = None, (payload or [])
        for row in results:
            if mu and isinstance(row, dict) and "managementUnitId" not in row:
                row = {**row, "managementUnitId": mu}
            rows.append(row)
    return rows


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
            description=INTERVAL_HELP_STRING,
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
            description=INTERVAL_HELP_STRING,
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

        Each user block carries ``explanations_available`` (bool): True when the
        adherence-explanations fetch actually returned a result for that user,
        False when it errored or came back async without a result yet. When
        False, overrun sessions are still returned (see ``overruns_unknown``)
        but are NOT counted toward ``unexplained_overruns`` — "couldn't fetch
        an explanation" is not the same claim as "no explanation exists", and
        conflating the two would falsely flag an explained overrun as a
        coaching issue.

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
        for _ in range(50):
            kwargs: dict[str, Any] = {"job_id": job_id, "page_size": 1000}
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

        # 2. WFM adherence explanations per user — v1.4: concurrent fan-out
        # instead of N sequential calls. On a 30-agent tenant this drops from
        # ~5 min (30 × ~10s) to ~10s (8 parallel workers, ~4 batches).
        from concurrent.futures import ThreadPoolExecutor

        wfm_api = gc.WorkforceManagementApi(get_api())
        start_iso, end_iso = interval.split("/")
        body = {"startDate": start_iso, "endDate": end_iso}

        def _fetch_expls(uid: str) -> tuple[str, list[dict], bool]:
            """Returns (uid, entities, explanations_available).

            The endpoint's response shape is ``{job, result, downloadUrl}``
            — explanation entities live under ``result.entities``, not at
            the top level. It can also complete async (202): ``result`` is
            absent and only ``job``/``downloadUrl`` are populated. In both
            the async-pending and error cases we return
            ``explanations_available=False`` so the caller doesn't treat
            "we couldn't fetch explanations" the same as "no explanation
            exists" (which would falsely flag a real, explained overrun as
            unexplained).
            """
            try:
                resp = with_retry(
                    wfm_api.post_workforcemanagement_agent_adherence_explanations_query
                )(agent_id=uid, body=body)
                resp_dict = to_dict(resp) or {}
                result = resp_dict.get("result")
                if result is None:
                    logger.info(
                        "WFM adherence query for %s returned async without a "
                        "result (job=%s); explanations unavailable this call",
                        uid, resp_dict.get("job"),
                    )
                    return uid, [], False
                return uid, (result.get("entities") or []), True
            except Exception as exc:
                logger.warning("WFM adherence query failed for %s: %s", uid, exc)
                return uid, [], False

        explanations_by_user: dict[str, list[dict]] = {}
        explanations_available_by_user: dict[str, bool] = {}
        max_workers = min(8, len(user_ids))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_expls, uid) for uid in user_ids]
            for fut in futures:
                uid, expls, available = fut.result()
                explanations_by_user[uid] = expls
                explanations_available_by_user[uid] = available

        # 3. For each overrun session, flag whether any explanation overlaps
        names = resolver.user_names(user_ids)
        out_users = []
        for uid in user_ids:
            expls = explanations_by_user.get(uid, [])
            explanations_available = explanations_available_by_user.get(uid, False)
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
            overruns_unknown = 0
            for s in sessions_by_user[uid]:
                matching_expl = None
                if s["over_target"]:
                    if not explanations_available:
                        # Couldn't fetch/parse explanations this call — don't
                        # claim "unexplained"; the overrun is still surfaced
                        # but its explanation status is unknown, not absent.
                        overruns_unknown += 1
                    else:
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
                    "explanation_status_known": explanations_available if s["over_target"] else None,
                })

            out_users.append({
                "user_id": uid,
                "user_name": names.get(uid),
                "session_count": len(sessions_out),
                "explanations_available": explanations_available,
                "explained_overruns": explained_overruns,
                "unexplained_overruns": unexplained_overruns,
                "overruns_unknown": overruns_unknown,
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

    @mcp.tool()
    def agent_adherence_history(
        management_unit_ids: list[str] = Field(
            description=(
                "Management unit ids to query (use list_management_units). One "
                "bulk item is submitted per MU, so pass every agent MU you want "
                "(e.g. both Agents_* units). Required."
            ),
        ),
        interval: str | None = Field(
            default=None,
            description=INTERVAL_HELP_STRING,
        ),
        user_ids: list[str] | None = Field(
            default=None,
            description=(
                "Optional user ids to limit the query. Omit for ALL agents in "
                "each management unit."
            ),
        ),
        time_zone: str = Field(
            default="UTC",
            description=(
                "IANA timezone the adherence day boundaries are computed in "
                "(e.g. 'Australia/Sydney'). Pass the tenant reporting timezone."
            ),
        ),
        include_exceptions: bool = Field(
            default=False,
            description=(
                "Include per-exception detail (off-schedule segments). When "
                "true the Genesys span cap drops from 31 days to 7 days."
            ),
        ),
    ) -> dict:
        """Actual-vs-scheduled **historical adherence %** + conformance %, returned in-session.

        THE tool for *"what was each agent's adherence / conformance last week?"* —
        the scheduled-vs-actual percentages that ``agent_adherence_review`` and
        ``query_agent_adherence_explanations`` deliberately do NOT compute.

        Uses the Genesys **bulk historical-adherence jobs** API: submits one job
        (one item per management unit), polls the job by id until it's
        ``Complete``, then fetches the presigned result download(s) — so the
        finished numbers come back here rather than only into the WFM
        Adherence/Reports area. (The per-MU ``historicaladherencequery`` endpoint
        is notification-only and never returns a pollable result; the bulk jobs
        endpoint is the synchronous-pollable one.)

        Returns one row per user with ``adherence_pct``, ``conformance_pct``,
        ``impact``, ``exception_count`` and (when ``include_exceptions``) the
        exception detail, plus a roll-up mean adherence/conformance.

        Span caps (Genesys): max 31 days, or 7 days when ``include_exceptions``
        is true. Soft-fails (never throws) if the OAuth client lacks WFM read
        access, or if the job isn't ``Complete`` within the poll budget (it
        returns the job id + a note so you can retry or narrow the range).
        """
        if not management_unit_ids:
            raise ValueError("management_unit_ids must contain at least one id (use list_management_units).")
        interval = interval or _default_interval(7)
        try:
            start_iso, end_iso = interval.split("/", 1)
            i_start = _parse_iso(start_iso)
            i_end = _parse_iso(end_iso)
        except Exception as exc:
            raise ValueError(f"Invalid interval {interval!r}") from exc

        span_days = (i_end - i_start).total_seconds() / 86400.0
        max_days = 7 if include_exceptions else 31
        if span_days > max_days + 1e-9:
            raise ValueError(
                f"Historical adherence span is {span_days:.1f} days; Genesys caps it "
                f"at {max_days} days{' when include_exceptions=true' if include_exceptions else ''}. "
                "Narrow the interval (or set include_exceptions=false for up to 31 days)."
            )

        # Bulk historical-adherence job: one item per MU. Submit → poll the job
        # by id until Complete → fetch the presigned result download(s).
        item_base: dict[str, Any] = {
            "startDate": start_iso,
            "endDate": end_iso,
            "includeExceptions": include_exceptions,
            "includeActuals": False,
        }
        if user_ids:
            item_base["userIds"] = user_ids
        body = {
            "items": [{**item_base, "managementUnitId": mu} for mu in management_unit_ids],
            "timeZone": time_zone,
        }

        api_client = get_api()
        try:
            submit = with_retry(api_client.call_api)(
                resource_path="/api/v2/workforcemanagement/adherence/historical/bulk",
                method="POST", body=body,
                auth_settings=["PureCloud OAuth"], response_type="object",
            ) or {}
        except Exception as exc:
            if getattr(exc, "status", None) in (401, 403):
                return soft_fail_envelope(
                    status=getattr(exc, "status", 403),
                    kind="historical adherence",
                    message=(
                        "OAuth client lacks workforce-management read access for "
                        "historical adherence — grant the QueueIQ client "
                        "workforce-management:readonly (or wfm:historicalAdherence:view). "
                        "This is a missing scope, not a tenant restriction."
                    ),
                    management_unit_ids=management_unit_ids,
                    interval=interval,
                )
            raise

        def _job_fields(resp: dict) -> tuple[str | None, str | None, list[str]]:
            job = resp.get("job") or {}
            return job.get("id"), job.get("status"), list(resp.get("downloadUrls") or [])

        job_id, status, download_urls = _job_fields(submit)

        # Poll the job until terminal. The bulk calc usually finishes in a few
        # seconds; linear backoff (1→5s) over ~15 attempts is a ~60s budget. If
        # it isn't Complete by then we soft-fail with the job id so the caller
        # can retry (Genesys keeps computing it) or narrow the range.
        delay = 1.0
        for _ in range(_ADHERENCE_POLL_ATTEMPTS):
            if status == "Complete":
                break
            if status and any(bad in status for bad in ("Error", "Failed", "Cancel")):
                return soft_fail_envelope(
                    status=502,
                    kind="historical adherence (job failed)",
                    message=f"Genesys historical-adherence job ended in state {status!r}. job_id={job_id}.",
                    management_unit_ids=management_unit_ids,
                    interval=interval,
                    job_id=job_id,
                )
            if not job_id:
                break
            time.sleep(delay)
            delay = min(delay + 1.0, 5.0)
            poll = with_retry(api_client.call_api)(
                resource_path=f"/api/v2/workforcemanagement/adherence/historical/bulk/jobs/{job_id}",
                method="GET",
                auth_settings=["PureCloud OAuth"], response_type="object",
            ) or {}
            job_id, status, urls = _job_fields(poll)
            if urls:
                download_urls = urls

        if status != "Complete":
            return soft_fail_envelope(
                status=202,
                kind="historical adherence (processing)",
                message=(
                    "Historical-adherence job submitted but wasn't Complete within the "
                    "poll budget. Retry shortly (Genesys keeps computing it), or narrow "
                    f"the interval. job_id={job_id}."
                ),
                management_unit_ids=management_unit_ids,
                interval=interval,
                job_id=job_id,
            )

        rows = _fetch_adherence_download(download_urls)

        users_out: list[dict] = []
        adh_vals: list[float] = []
        conf_vals: list[float] = []
        for row in rows:
            adh = row.get("adherencePercentage")
            conf = row.get("conformancePercentage")
            if isinstance(adh, (int, float)):
                adh_vals.append(float(adh))
            if isinstance(conf, (int, float)):
                conf_vals.append(float(conf))
            entry = {
                "user_id": row.get("userId"),
                "management_unit_id": row.get("managementUnitId"),
                "adherence_pct": adh,
                "conformance_pct": conf,
                "impact": row.get("impact"),
                "exception_count": len(row.get("exceptionInfo") or []),
            }
            if include_exceptions:
                entry["exceptions"] = row.get("exceptionInfo") or []
            users_out.append(entry)

        # Resolve names + sort worst-adherence first (the actionable order).
        ids = [u["user_id"] for u in users_out if u["user_id"]]
        names = resolver.user_names(ids) if ids else {}
        for u in users_out:
            u["user_name"] = names.get(u["user_id"])
        users_out.sort(key=lambda r: (r["adherence_pct"] is None, r["adherence_pct"] or 0.0))

        return {
            "interval": interval,
            "as_of_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "management_unit_ids": management_unit_ids,
            "time_zone": time_zone,
            "include_exceptions": include_exceptions,
            "job_id": job_id,
            "user_count": len(users_out),
            "mean_adherence_pct": round(sum(adh_vals) / len(adh_vals), 1) if adh_vals else None,
            "mean_conformance_pct": round(sum(conf_vals) / len(conf_vals), 1) if conf_vals else None,
            "users": users_out,
        }

    @mcp.tool()
    def wfm_schedule(
        business_unit_id: str = Field(
            description="Business unit id (use list_management_units → look at the businessUnit field)."
        ),
        management_unit_ids: list[str] = Field(
            description="Management unit ids to roll up. Pass an empty list for ALL MUs in the BU.",
        ),
        user_ids: list[str] = Field(
            description="User ids whose shifts to fetch. Required — schedules/search returns nothing without it.",
        ),
        interval: str | None = Field(
            default=None,
            description=INTERVAL_HELP_STRING,
        ),
        time_zone: str | None = Field(
            default=None,
            description=(
                "IANA timezone for day boundaries (e.g. 'Australia/Sydney'). "
                "Scheduled shift hours are bucketed into calendar days IN THIS "
                "ZONE so they line up with the locally-bucketed required hours. "
                "Pass the tenant reporting timezone. If omitted, the tool reads "
                "the business unit's WFM timezone; if that can't be resolved it "
                "falls back to UTC (which will mis-attribute evening shifts in "
                "non-UTC tenants), and ``time_zone_source`` reports which was used."
            ),
        ),
    ) -> dict:
        """Per-day WFM scheduled hours + headcount-forecast required hours.

        Identifies the **published** schedule(s) covering the requested interval
        (drafts are ignored), then for each schedule:

          * fetches the headcount forecast (per-MU, 15-minute granularity 'requiredPerInterval')
            and rolls it up to required FTE-hours per day
          * fetches per-user shifts via /managementunits/{muId}/schedules/search and rolls
            them up to scheduled FTE-hours per day

        Both the required and scheduled series are bucketed into calendar days in
        the business-unit/tenant timezone (see ``time_zone``), so a Friday-evening
        shift in Australia/Sydney lands on Friday — not on Saturday UTC. Shift
        activities are de-duplicated by (user, start, length) so overlapping
        published schedules can't double-count a day's hours.

        The result is a daily series suitable for capacity-vs-demand analysis. Compare
        ``scheduled_hours`` against ``required_hours`` to spot understaffed days.
        """
        if not user_ids:
            raise ValueError("user_ids must contain at least one id (schedules/search needs it).")
        interval = interval or _default_interval(7)
        try:
            int_start, int_end = interval.split("/", 1)
            i_start = _parse_iso(int_start)
            i_end = _parse_iso(int_end)
        except Exception as exc:
            raise ValueError(f"Invalid interval {interval!r}") from exc

        api_client = get_api()

        # Day boundaries must be computed in the schedule's own timezone, not UTC.
        # Prefer the caller-supplied tenant tz; else read the BU's WFM timezone.
        resolved_tz_name = time_zone or _resolve_business_unit_tz(api_client, business_unit_id)
        sched_tz, tz_used = _safe_zoneinfo(resolved_tz_name)

        def _local_day(dt: datetime) -> "date":
            """Calendar date of ``dt`` in the schedule timezone (UTC-safe input)."""
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(sched_tz).date()

        # Local-day window bounds, shared by BOTH the required-hours filter and
        # the scheduled-hours filter so the two series use the same day-boundary
        # convention (the forecast requiredPerInterval is already indexed in the
        # schedule's local time off weekDate, so local bounds align it with the
        # locally-bucketed shift hours).
        win_start = _local_day(i_start)
        win_end = _local_day(i_end)

        # 1. List published schedules in the BU; keep ones that overlap the interval.
        sched_paths = []
        # Genesys publishes schedules per "weekDate" (the Monday of week 1). A schedule
        # has weekCount weeks. We probe the weekDate Monday <= interval_start; the
        # schedules endpoint returns the schedule covering that date.
        # Simpler: iterate Mondays within the interval and call the per-week schedules endpoint.
        from datetime import date, timedelta as td
        d = i_start.date()
        # Move to the Monday on or before the interval start
        d -= td(days=d.weekday())
        seen_sched_ids: set[str] = set()
        schedules = []
        while d <= i_end.date():
            try:
                resp = with_retry(api_client.call_api)(
                    resource_path=(f"/api/v2/workforcemanagement/businessunits/"
                                   f"{business_unit_id}/weeks/{d.isoformat()}/schedules"),
                    method="GET",
                    auth_settings=["PureCloud OAuth"],
                    response_type="object",
                ) or {}
                for sch in resp.get("entities") or []:
                    sid = sch.get("id")
                    if not sid or sid in seen_sched_ids:
                        continue
                    seen_sched_ids.add(sid)
                    schedules.append({
                        "id": sid,
                        "weekDate": sch.get("weekDate"),
                        "weekCount": sch.get("weekCount", 1),
                        "published": sch.get("published"),
                    })
            except Exception as exc:
                if getattr(exc, "status", None) != 404:
                    raise
            d += td(days=7)

        if not schedules:
            return {"interval": interval, "schedules": [], "daily": [], "note": "no schedules found"}

        # 1b. Prefer PUBLISHED schedules. The list endpoint returns drafts and
        # published schedules alike; counting both double-counts hours (and a
        # draft can carry stale/wrong shifts). If any schedule is published,
        # drop the unpublished ones. If none are published (BU only has drafts),
        # keep them all but flag it so the caller knows the numbers are provisional.
        published = [s for s in schedules if s.get("published")]
        published_only = bool(published)
        if published_only:
            schedules = published

        # 2. Headcount forecast per schedule per MU — gives required FTE per 15-min interval.
        # If management_unit_ids is empty, we'll discover the MUs covered by each schedule.
        per_day_required_fte_15min: dict[str, float] = {}
        for sch in schedules:
            try:
                hc = with_retry(api_client.call_api)(
                    resource_path=(f"/api/v2/workforcemanagement/businessunits/{business_unit_id}/"
                                   f"weeks/{sch['weekDate']}/schedules/{sch['id']}/headcountforecast"),
                    method="GET",
                    auth_settings=["PureCloud OAuth"],
                    response_type="object",
                ) or {}
            except Exception as exc:
                logger.warning("headcountforecast %s failed: %s", sch["id"], exc)
                continue

            # Response shape: {result: {entities: [{ planningGroup, requiredPerInterval[], ... }]}}
            entities = ((hc.get("result") or {}).get("entities") or [])
            # The schedule starts at sch['weekDate'] (Monday) at 00:00 in MU timezone.
            sch_start = datetime.fromisoformat(sch["weekDate"]).date()
            for ent in entities:
                series = ent.get("requiredPerInterval") or []
                for idx, val in enumerate(series):
                    # Each interval is 15 minutes. idx 0 = sch_start 00:00.
                    minutes_in = idx * 15
                    day_offset = minutes_in // (24 * 60)
                    day = sch_start + td(days=day_offset)
                    day_iso = day.isoformat()
                    # Only count days within the requested interval. Use the
                    # local-day window so this aligns with the scheduled-hours
                    # side (both local) — avoids phantom boundary rows where one
                    # series has data and the other doesn't.
                    if day < win_start or day > win_end:
                        continue
                    per_day_required_fte_15min[day_iso] = (
                        per_day_required_fte_15min.get(day_iso, 0.0) + float(val or 0)
                    )

        # 3. User shifts per MU. If MU list is empty, fetch each user's MU once.
        target_mus = list(management_unit_ids) if management_unit_ids else []
        if not target_mus:
            # Fall back: fetch each user's MU. The raw path
            # GET /api/v2/workforcemanagement/users/{userId} does not exist
            # in the Genesys API (404s on every call) — use the SDK method
            # backing get_user_management_unit instead.
            wfm_api = gc.WorkforceManagementApi(api_client)
            seen = set()
            for uid in user_ids:
                try:
                    r = to_dict(
                        with_retry(wfm_api.get_workforcemanagement_agent_managementunit)(
                            agent_id=uid
                        )
                    ) or {}
                    mu = (r.get("managementUnit") or {}).get("id")
                    if mu and mu not in seen:
                        seen.add(mu)
                        target_mus.append(mu)
                except Exception:
                    continue

        per_day_scheduled_seconds: dict[str, float] = {}
        per_day_users: dict[str, set[str]] = {}
        # De-dup paid-time activities across (overlapping) schedules and MUs.
        # The schedules/search endpoint is date-range scoped, not schedule-id
        # scoped, so iterating per schedule can return the SAME shift twice when
        # two schedules overlap a week. Key each activity by (user, start, length)
        # so it counts exactly once. Reuses the shared local-day window bounds
        # (win_start/win_end) so a shift on the local boundary day isn't dropped
        # by a UTC-vs-local mismatch.
        seen_activities: set[tuple[str, str, int]] = set()
        for sch in schedules:
            sch_start = datetime.fromisoformat(sch["weekDate"]).date()
            sch_end = sch_start + td(days=7 * sch.get("weekCount", 1))
            for mu_id in target_mus:
                body = {
                    "startDate": sch_start.isoformat() + "T00:00:00.000Z",
                    "endDate": sch_end.isoformat() + "T00:00:00.000Z",
                    "userIds": user_ids,
                }
                try:
                    resp = with_retry(api_client.call_api)(
                        resource_path=(f"/api/v2/workforcemanagement/managementunits/"
                                       f"{mu_id}/schedules/search"),
                        method="POST", body=body,
                        auth_settings=["PureCloud OAuth"], response_type="object",
                    ) or {}
                except Exception as exc:
                    logger.warning("schedules/search MU %s failed: %s", mu_id, exc)
                    continue
                for uid, sched in (resp.get("userSchedules") or {}).items():
                    for shift in sched.get("shifts") or []:
                        st_raw = shift.get("startDate")
                        if not st_raw:
                            continue
                        # Sum paid-time activities (excludes unpaid lunch). Each
                        # activity is attributed to its LOCAL calendar day (in the
                        # schedule timezone) so evening shifts in a non-UTC tenant
                        # don't spill onto the next UTC day.
                        for act in shift.get("activities") or []:
                            if not act.get("countsAsPaidTime"):
                                continue
                            act_start_raw = act.get("startDate")
                            try:
                                act_st = _parse_iso(act_start_raw)
                            except Exception:
                                continue
                            mins = int(act.get("lengthInMinutes", 0) or 0)
                            if mins <= 0:
                                continue
                            dedup_key = (uid, str(act_start_raw), mins)
                            if dedup_key in seen_activities:
                                continue
                            seen_activities.add(dedup_key)
                            local_day = _local_day(act_st)
                            if local_day < win_start or local_day > win_end:
                                continue
                            day_iso = local_day.isoformat()
                            per_day_scheduled_seconds[day_iso] = (
                                per_day_scheduled_seconds.get(day_iso, 0.0) + mins * 60
                            )
                            per_day_users.setdefault(day_iso, set()).add(uid)

        # 4. Build daily output
        all_days = sorted(set(per_day_required_fte_15min) | set(per_day_scheduled_seconds))
        daily = []
        for d_iso in all_days:
            req_15min = per_day_required_fte_15min.get(d_iso, 0.0)
            req_h = req_15min * 0.25  # each "FTE-15min" = 0.25 FTE-hours
            sch_h = per_day_scheduled_seconds.get(d_iso, 0.0) / 3600
            users_n = len(per_day_users.get(d_iso, set()))
            daily.append({
                "date": d_iso,
                "scheduled_hours": round(sch_h, 1),
                "required_hours": round(req_h, 1),
                "gap_hours": round(req_h - sch_h, 1),
                "scheduled_users": users_n,
            })

        return {
            "interval": interval,
            "business_unit_id": business_unit_id,
            "management_unit_ids": target_mus,
            "time_zone": tz_used,
            "time_zone_source": (
                "caller" if time_zone else
                ("business_unit" if resolved_tz_name else "utc_fallback")
            ),
            "published_only": published_only,
            "schedules": schedules,
            "user_count_queried": len(user_ids),
            "daily": daily,
            "totals": {
                "scheduled_hours": round(sum(d["scheduled_hours"] for d in daily), 1),
                "required_hours": round(sum(d["required_hours"] for d in daily), 1),
                "gap_hours": round(sum(d["gap_hours"] for d in daily), 1),
            },
        }

    @mcp.tool()
    def volume_vs_forecast(
        business_unit_id: str = Field(
            description=(
                "Business unit id (use list_management_units → look at the "
                "businessUnit field). The short-term forecast lives at the BU level."
            ),
        ),
        interval: str | None = Field(
            default=None,
            description=INTERVAL_HELP_STRING,
        ),
        granularity: str = Field(
            default="1h",
            description=(
                "Time bucket size for the comparison. One of '15min', '30min', "
                "'1h', '1d'. WFM short-term forecast resolution is 15-min; "
                "coarser buckets aggregate. 1-hour is the readable default."
            ),
        ),
    ) -> dict:
        """Per-interval **forecast vs actual** comparison — closes the WFM loop.

        ``wfm_schedule`` answers *"how does scheduled capacity compare to the
        forecast?"* (forecast vs scheduled). This tool answers the missing
        third side: *"how accurate was the forecast?"* (forecast vs actual).
        Pairs well with wfm_schedule for the full demand/capacity triangle.

        Pulls the published short-term forecast for the interval, pulls actual
        conversation volume + handle time via the analytics aggregates API for
        the same period, and computes per-bucket variance plus a roll-up
        forecast-accuracy figure (MAPE — mean absolute percentage error).

        Returns:
        - ``buckets``: per-interval series ``{interval_start, forecast_offered,
          actual_offered, volume_variance_pct, forecast_aht_s, actual_aht_s,
          aht_variance_pct}``
        - ``totals``: rolled-up forecast vs actual offered + handle hours
        - ``accuracy``: ``{volume_mape_pct, aht_mape_pct, worst_buckets}`` —
          where ``volume_mape_pct`` is the mean absolute percentage error
          across buckets; lower is a more accurate forecast
        - ``worst_buckets``: top 5 buckets by absolute volume variance — the
          intervals the forecast missed by the most
        """
        interval = interval or _default_interval(7)
        try:
            int_start, int_end = interval.split("/", 1)
            i_start = _parse_iso(int_start)
            i_end = _parse_iso(int_end)
        except Exception as exc:
            raise ValueError(f"Invalid interval {interval!r}") from exc

        if granularity not in ("15min", "30min", "1h", "1d"):
            raise ValueError(
                f"granularity must be one of 15min/30min/1h/1d, got {granularity!r}"
            )
        bucket_seconds = {"15min": 900, "30min": 1800, "1h": 3600, "1d": 86400}[granularity]
        genesys_granularity = {
            "15min": "PT15M", "30min": "PT30M", "1h": "PT1H", "1d": "P1D",
        }[granularity]

        api_client = get_api()

        # 1. Discover forecasts for the interval. Genesys publishes weekly
        # forecasts keyed by the Monday of each week. Iterate Mondays in scope.
        from datetime import date, timedelta as td
        d = i_start.date()
        d -= td(days=d.weekday())
        seen_fc: set[str] = set()
        forecasts: list[dict] = []
        while d <= i_end.date():
            try:
                resp = with_retry(api_client.call_api)(
                    resource_path=(
                        f"/api/v2/workforcemanagement/businessunits/"
                        f"{business_unit_id}/weeks/{d.isoformat()}/shorttermforecasts"
                    ),
                    method="GET",
                    auth_settings=["PureCloud OAuth"],
                    response_type="object",
                ) or {}
                for fc in resp.get("entities") or []:
                    fid = fc.get("id")
                    if not fid or fid in seen_fc:
                        continue
                    seen_fc.add(fid)
                    # weekDate is the forecast's own published-at Monday — may
                    # be earlier than the listing Monday for multi-week forecasts.
                    # The /data endpoint lives at that week_date, not the listing's.
                    forecasts.append({
                        "id": fid,
                        "week_date": fc.get("weekDate") or d.isoformat(),
                        "week_count": fc.get("weekCount") or 1,
                        "description": fc.get("description"),
                        "state": fc.get("state"),
                    })
            except gc.rest.ApiException as exc:
                if exc.status not in (404, 403):
                    raise
            d += td(days=7)

        # 2. Pull each forecast's data and aggregate into a single timeline.
        # The /data endpoint returns ONE week per call, indexed by ?weekNumber=N
        # (1-indexed). For a multi-week forecast we iterate weekCount calls.
        # Each per-interval array is 7 days × 96 quarter-hours = 672 (or 676 if
        # a DST transition adds extra quarter-hours).
        forecast_buckets: dict[str, dict[str, float]] = {}
        for fc in forecasts:
            week_count = int(fc.get("week_count") or 1)
            for week_n in range(1, week_count + 1):
                try:
                    data_resp = with_retry(api_client.call_api)(
                        resource_path=(
                            f"/api/v2/workforcemanagement/businessunits/"
                            f"{business_unit_id}/weeks/{fc['week_date']}/"
                            f"shorttermforecasts/{fc['id']}/data"
                        ),
                        method="GET",
                        auth_settings=["PureCloud OAuth"],
                        response_type="object",
                        query_params={"weekNumber": week_n},
                    ) or {}
                except gc.rest.ApiException as exc:
                    if exc.status in (404, 403):
                        continue
                    raise

                result = data_resp.get("result") or {}
                ref_start = result.get("referenceStartDate") or (
                    fc["week_date"] + "T00:00:00.000Z"
                )
                origin = _parse_iso(ref_start)
                if origin.tzinfo is None:
                    origin = origin.replace(tzinfo=timezone.utc)
                # This call's data starts (week_n - 1) weeks after the origin.
                this_week_start = origin + timedelta(days=(week_n - 1) * 7)

                # Cheap window filter: if this week is fully outside the
                # requested interval, skip parsing the arrays.
                if this_week_start + timedelta(days=8) < i_start:
                    continue
                if this_week_start > i_end:
                    continue

                for pg in result.get("planningGroups") or []:
                    offered = pg.get("offeredPerInterval") or []
                    aht = pg.get("averageHandleTimeSecondsPerInterval") or []
                    for idx, off_val in enumerate(offered):
                        bucket_start = this_week_start + timedelta(minutes=15 * idx)
                        if bucket_start < i_start or bucket_start >= i_end:
                            continue
                        bucket_key = _bucket_key(bucket_start, bucket_seconds)
                        b = forecast_buckets.setdefault(
                            bucket_key,
                            {"offered": 0.0, "aht_weighted": 0.0, "aht_n": 0.0},
                        )
                        b["offered"] += float(off_val or 0)
                        aht_val = float((aht[idx] if idx < len(aht) else 0) or 0)
                        if aht_val > 0 and off_val:
                            b["aht_weighted"] += aht_val * float(off_val)
                            b["aht_n"] += float(off_val)

        # 3. Pull actual analytics conversations aggregates for the interval.
        # Chunk multi-year spans into ≤12-month sub-queries and merge the
        # granularity buckets; normal windows pass through as a single call.
        aapi = gc.AnalyticsApi(api_client)

        def _actual_q(iv: str) -> dict:
            return to_dict(
                with_retry(aapi.post_analytics_conversations_aggregates_query)({
                    "interval": iv,
                    "granularity": genesys_granularity,
                    "metrics": ["tAnswered", "tHandle"],
                })
            ) or {}
        actual_resp = run_chunked_query(_actual_q, interval)
        actual_buckets: dict[str, dict[str, float]] = {}
        for r in actual_resp.get("results") or []:
            for bucket in r.get("data") or []:
                bucket_dt = _parse_iso(bucket["interval"].split("/")[0])
                bucket_key = _bucket_key(bucket_dt, bucket_seconds)
                ab = actual_buckets.setdefault(
                    bucket_key, {"answered": 0.0, "handle_ms": 0.0},
                )
                for m in bucket.get("metrics") or []:
                    if m["metric"] == "tAnswered":
                        ab["answered"] += float(m.get("stats", {}).get("count", 0) or 0)
                    elif m["metric"] == "tHandle":
                        ab["handle_ms"] += float(m.get("stats", {}).get("sum", 0) or 0)

        # 4. Build the comparison series across all bucket keys.
        all_keys = sorted(set(forecast_buckets) | set(actual_buckets))
        buckets_out: list[dict] = []
        for key in all_keys:
            f = forecast_buckets.get(key) or {"offered": 0.0, "aht_weighted": 0.0, "aht_n": 0.0}
            a = actual_buckets.get(key) or {"answered": 0.0, "handle_ms": 0.0}
            fc_aht = f["aht_weighted"] / f["aht_n"] if f["aht_n"] else None
            actual_aht = a["handle_ms"] / 1000.0 / a["answered"] if a["answered"] else None
            vol_var_pct = (
                (a["answered"] - f["offered"]) / f["offered"] * 100.0
                if f["offered"] else None
            )
            aht_var_pct = (
                (actual_aht - fc_aht) / fc_aht * 100.0
                if (fc_aht and actual_aht) else None
            )
            buckets_out.append({
                "interval_start": key,
                "forecast_offered": round(f["offered"], 1),
                "actual_offered": int(a["answered"]),
                "volume_variance_pct": round(vol_var_pct, 1) if vol_var_pct is not None else None,
                "forecast_aht_s": round(fc_aht, 1) if fc_aht is not None else None,
                "actual_aht_s": round(actual_aht, 1) if actual_aht is not None else None,
                "aht_variance_pct": round(aht_var_pct, 1) if aht_var_pct is not None else None,
            })

        # 5. Roll-ups.
        total_fc_offered = sum(b["forecast_offered"] or 0 for b in buckets_out)
        total_actual_offered = sum(b["actual_offered"] or 0 for b in buckets_out)
        vol_errs = [abs(b["volume_variance_pct"]) for b in buckets_out
                    if b["volume_variance_pct"] is not None]
        aht_errs = [abs(b["aht_variance_pct"]) for b in buckets_out
                    if b["aht_variance_pct"] is not None]
        worst_volume = sorted(
            [b for b in buckets_out if b["volume_variance_pct"] is not None],
            key=lambda r: abs(r["volume_variance_pct"]),
            reverse=True,
        )[:5]

        return {
            "interval": interval,
            "granularity": granularity,
            "business_unit_id": business_unit_id,
            "forecasts_used": forecasts,
            "buckets": buckets_out,
            "totals": {
                "forecast_offered": round(total_fc_offered, 1),
                "actual_offered": total_actual_offered,
                "total_variance_pct": (
                    round((total_actual_offered - total_fc_offered) / total_fc_offered * 100.0, 1)
                    if total_fc_offered else None
                ),
            },
            "accuracy": {
                "volume_mape_pct": round(sum(vol_errs) / len(vol_errs), 1) if vol_errs else None,
                "aht_mape_pct": round(sum(aht_errs) / len(aht_errs), 1) if aht_errs else None,
                "bucket_count": len(buckets_out),
                "worst_buckets": worst_volume,
            },
        }
