"""User activity history + workforce trend.

v1.12. For each user (active + inactive + deleted), reconstructs first/last
handled-interaction date from Genesys analytics and rolls up quarterly:

- ``active_agents`` — distinct users with ≥1 handled interaction in the quarter
- ``joiners`` — users whose first_active_month falls in the quarter
- ``leavers`` — users whose last_active_month falls in the quarter AND
  ``last_active`` is not the final bucket in the window
- ``mean_tenure_months`` / ``median_tenure_months`` — months between each
  active agent's ``first_active_month`` and the bucket start

Backed by:

- ``GET /api/v2/users?state=any&pageSize=200`` (paginated — gets every user
  in the org regardless of active/inactive/deleted state).
- ``POST /api/v2/analytics/conversations/aggregates/query`` with
  ``groupBy=["userId"]``, ``granularity="P1M"``, metric ``tAnswered``. The
  long interval is chunked into ~yearly slices to dodge per-query caps and
  fired concurrently via ``ThreadPoolExecutor``.

Permissions: ``users:user:view`` + ``analytics:conversationAggregate:view``.

Retention caveat: Genesys conversations/aggregates retention is typically
~13 months for most regions. Older months return zero results without
erroring. The tool surfaces ``data_starts_at`` so the caller can tell
whether a quarter shows zero headcount because it's pre-retention or
because the tenant genuinely had no agents handling interactions then.

"Handled" follows the cc-monthly-report convention: voice + message +
callback are counted; email is excluded because email handle times can
span days and would inflate per-month activity.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from statistics import mean, median
from zoneinfo import ZoneInfo

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


# ── helpers ──

def _resolve_default_interval(tz: ZoneInfo, years_back: int = 3) -> str:
    """Default window: ``years_back`` years ending now, local-midnight aligned."""
    now_local = datetime.now(tz)
    end_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = end_local.replace(year=end_local.year - years_back)
    start_utc = start_local.astimezone(ZoneInfo("UTC"))
    end_utc = end_local.astimezone(ZoneInfo("UTC"))
    return (
        f"{start_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z')}/"
        f"{end_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
    )


def _parse_interval(interval: str) -> tuple[datetime, datetime]:
    start_s, end_s = interval.split("/")
    return (
        datetime.fromisoformat(start_s.replace("Z", "+00:00")),
        datetime.fromisoformat(end_s.replace("Z", "+00:00")),
    )


def _split_interval_by_year(start: datetime, end: datetime) -> list[str]:
    """Split a long interval into ≤12-month chunks."""
    chunks: list[str] = []
    cur = start
    while cur < end:
        try:
            nxt = cur.replace(year=cur.year + 1)
        except ValueError:
            # Feb-29 → step back one day on the leap-year boundary.
            nxt = cur.replace(year=cur.year + 1, day=28)
        if nxt > end:
            nxt = end
        chunks.append(
            f"{cur.strftime('%Y-%m-%dT%H:%M:%S.000Z')}/"
            f"{nxt.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
        )
        cur = nxt
    return chunks


def _list_all_users_any_state(api: gc.UsersApi, page_size: int = 200) -> list[dict]:
    """Page through ``/api/v2/users?state=any`` and return every user record."""
    out: list[dict] = []
    page = 1
    while True:
        resp = with_retry(api.get_users)(
            page_size=page_size, page_number=page, state="any",
        )
        data = to_dict(resp) or {}
        entities = data.get("entities") or []
        if not entities:
            break
        for u in entities:
            out.append({
                "user_id": u.get("id"),
                "name": u.get("name"),
                "email": u.get("email"),
                "state": u.get("state"),
                "title": u.get("title"),
                "department": u.get("department"),
                "date_created": u.get("dateHired") or None,
            })
        if len(entities) < page_size:
            break
        page += 1
    return out


def _fetch_monthly_activity(
    api: gc.AnalyticsApi,
    interval_chunk: str,
    user_ids: list[str],
    media_types: tuple[str, ...] = ("voice", "message", "callback"),
) -> dict[str, dict[str, int]]:
    """Returns ``{user_id: {YYYY-MM: answered_count}}`` for one chunk.

    Only buckets with ``tAnswered.count > 0`` are kept — the caller cares
    about *activity*, not zero-rows from users who existed but didn't
    answer that month.
    """
    body = {
        "interval": interval_chunk,
        "granularity": "P1M",
        "groupBy": ["userId"],
        "filter": {
            "type": "and",
            "clauses": [
                {"type": "or", "predicates": [
                    {"dimension": "userId", "value": uid} for uid in user_ids
                ]},
                {"type": "or", "predicates": [
                    {"dimension": "mediaType", "value": m} for m in media_types
                ]},
            ],
        },
        "metrics": ["tAnswered"],
    }
    resp = with_retry(api.post_analytics_conversations_aggregates_query)(body)
    raw = to_dict(resp) or {}
    out: dict[str, dict[str, int]] = {}
    for grp in raw.get("results") or []:
        uid = (grp.get("group") or {}).get("userId")
        if not uid:
            continue
        for bucket in (grp.get("data") or []):
            iv = bucket.get("interval") or ""
            if not iv or "T" not in iv:
                continue
            month_key = iv.split("T")[0][:7]  # "2024-04"
            answered = 0
            for m in (bucket.get("metrics") or []):
                if m.get("metric") == "tAnswered":
                    answered = int((m.get("stats") or {}).get("count") or 0)
                    break
            if answered > 0:
                bucket_for_uid = out.setdefault(uid, {})
                bucket_for_uid[month_key] = bucket_for_uid.get(month_key, 0) + answered
    return out


def _month_to_quarter(month_key: str) -> str:
    """``"2024-04"`` → ``"2024-Q2"``."""
    y, m = month_key.split("-")
    q = (int(m) - 1) // 3 + 1
    return f"{y}-Q{q}"


def _bucket_keys(start: datetime, end: datetime, bucket: str, tz: ZoneInfo) -> list[str]:
    """Enumerate every YYYY-Qn (or YYYY-MM) bucket between start and end in ``tz``."""
    start_l = start.astimezone(tz)
    end_l = end.astimezone(tz)
    keys: list[str] = []
    if bucket == "month":
        y, m = start_l.year, start_l.month
        while (y, m) <= (end_l.year, end_l.month):
            keys.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                m = 1
                y += 1
    else:  # quarter
        y = start_l.year
        q = (start_l.month - 1) // 3 + 1
        end_q = (end_l.month - 1) // 3 + 1
        while (y, q) <= (end_l.year, end_q):
            keys.append(f"{y:04d}-Q{q}")
            q += 1
            if q > 4:
                q = 1
                y += 1
    return keys


def _months_between(month_key_a: str, month_key_b: str) -> int:
    ya, ma = [int(x) for x in month_key_a.split("-")]
    yb, mb = [int(x) for x in month_key_b.split("-")]
    return max(0, (yb - ya) * 12 + (mb - ma))


def _bucket_start_month(bucket_key: str) -> str:
    """``"2024-Q2"`` → ``"2024-04"``; ``"2024-04"`` → ``"2024-04"``."""
    if "Q" in bucket_key:
        y, q = bucket_key.split("-Q")
        month = (int(q) - 1) * 3 + 1
        return f"{y}-{month:02d}"
    return bucket_key


def _bucket_for_month(month_key: str, bucket: str) -> str:
    return _month_to_quarter(month_key) if bucket == "quarter" else month_key


# ── tool ──

def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def user_activity_history(
        user_ids: list[str] | None = Field(
            default=None,
            description=(
                "Specific user ids to analyse. Default ``None`` means "
                "every user in the tenant — active + inactive + deleted — "
                "fetched via ``GET /api/v2/users?state=any`` and paginated."
            ),
        ),
        interval: str | None = Field(
            default=None,
            description=(
                "ISO-8601 interval like ``2023-07-01T00:00:00.000Z/"
                "2026-07-01T00:00:00.000Z``. Default ``None`` resolves to "
                "the last 3 years ending at local-midnight today. Long "
                "intervals are chunked into ~yearly slices internally."
            ),
        ),
        bucket: str = Field(
            default="quarter",
            description="Rollup bucket size: 'quarter' (default) or 'month'.",
        ),
        tz_name: str = Field(
            default="Australia/Sydney",
            description=(
                "IANA timezone for bucket boundaries. Defaults to "
                "Australia/Sydney. Use 'UTC' for tenant-agnostic output."
            ),
        ),
        include_inactive: bool = Field(
            default=True,
            description="Include users whose Genesys state is 'inactive'.",
        ),
        include_deleted: bool = Field(
            default=True,
            description="Include users whose Genesys state is 'deleted'.",
        ),
        max_workers: int = Field(
            default=4,
            description="Concurrent yearly chunks to fetch (each is one API call).",
        ),
    ) -> dict:
        """Reconstruct first/last handled-interaction date per user and roll
        up to quarterly headcount + tenure trend + joiner/leaver flags.

        USE THIS for multi-year / long-range staffing, headcount and agent-trend
        questions (e.g. "staffing trend since 2023"). It chunks the long span
        into yearly slices internally, so it's the right tool when the window
        exceeds a year — do not try to force a multi-year span through the
        per-interval analytics tools.

        Three surfaces in one call:

        - ``per_user`` — one row per user with state, first_active_date,
          last_active_date, total_handled, active_buckets, is_joiner /
          is_leaver booleans.
        - ``headcount_by_bucket`` — for each quarter (or month) in the
          window: ``{bucket, active_agents, joiners, leavers}``.
        - ``tenure_trend`` — for each bucket: ``{bucket,
          mean_tenure_months, median_tenure_months, n}``.

        "Handled" counts voice + message + callback (email excluded,
        consistent with cc-monthly-report). Retention caveat: Genesys
        conversations/aggregates typically retain ~13 months. Older
        buckets surface as zero — check ``data_starts_at`` in the
        response to distinguish pre-retention from no-activity.
        """
        if bucket not in ("quarter", "month"):
            raise ValueError(f"bucket must be 'quarter' or 'month', got {bucket!r}")

        tz = ZoneInfo(tz_name)
        resolved_interval = interval or _resolve_default_interval(tz)
        start_utc, end_utc = _parse_interval(resolved_interval)

        users_api = gc.UsersApi(get_api())
        analytics_api = gc.AnalyticsApi(get_api())

        # 1. Pull users in scope.
        if user_ids is None or len(user_ids) == 0:
            all_users = _list_all_users_any_state(users_api)
        else:
            all_users = [{"user_id": uid, "name": None, "state": None}
                         for uid in user_ids]

        users = [
            u for u in all_users
            if u.get("user_id") and (
                u.get("state") == "active"
                or (include_inactive and u.get("state") == "inactive")
                or (include_deleted and u.get("state") == "deleted")
                or u.get("state") is None  # caller-supplied: trust them
            )
        ]
        if not users:
            return {
                "interval": resolved_interval,
                "as_of_utc": datetime.now(timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "tz": tz_name,
                "bucket": bucket,
                "per_user": [],
                "headcount_by_bucket": [],
                "tenure_trend": [],
                "data_starts_at": None,
                "user_count": 0,
            }
        scoped_user_ids = [u["user_id"] for u in users]

        # 2. Chunk the interval into ~yearly slices and the user list into
        # ≤50-predicate batches (the Genesys OR-clause safe bound).
        chunks = _split_interval_by_year(start_utc, end_utc)
        USER_BATCH = 50
        user_batches = [
            scoped_user_ids[i:i + USER_BATCH]
            for i in range(0, len(scoped_user_ids), USER_BATCH)
        ]

        per_user_months: dict[str, dict[str, int]] = {}

        def _fetch_one(chunk: str, uids: list[str]) -> dict[str, dict[str, int]]:
            try:
                return _fetch_monthly_activity(analytics_api, chunk, uids)
            except Exception as e:
                logger.warning(
                    "user_activity_history: chunk %s (%d users) failed: %s",
                    chunk, len(uids), e,
                )
                return {}

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(_fetch_one, chunk, batch)
                for chunk in chunks
                for batch in user_batches
            ]
            for f in futures:
                result = f.result()
                for uid, month_counts in result.items():
                    cur = per_user_months.setdefault(uid, {})
                    for mk, cnt in month_counts.items():
                        cur[mk] = cur.get(mk, 0) + cnt

        # 3. Per-user rollup + bucket accumulators.
        all_buckets = _bucket_keys(start_utc, end_utc, bucket, tz)
        per_user_out: list[dict] = []
        joiners_by_bucket: dict[str, int] = {b: 0 for b in all_buckets}
        leavers_by_bucket: dict[str, int] = {b: 0 for b in all_buckets}
        active_set_by_bucket: dict[str, set[str]] = {b: set() for b in all_buckets}
        tenure_samples: dict[str, list[int]] = {b: [] for b in all_buckets}

        data_starts_at: str | None = None

        for u in users:
            uid = u["user_id"]
            month_counts = per_user_months.get(uid) or {}
            months_sorted = sorted(month_counts.keys())
            total_handled = sum(month_counts.values())
            if not months_sorted:
                per_user_out.append({
                    **u,
                    "first_active_month": None,
                    "last_active_month": None,
                    "first_active_date": None,
                    "last_active_date": None,
                    "total_handled": 0,
                    "active_buckets": [],
                    "is_joiner_in_window": False,
                    "is_leaver_in_window": False,
                })
                continue

            first_m, last_m = months_sorted[0], months_sorted[-1]
            if data_starts_at is None or first_m < data_starts_at:
                data_starts_at = first_m

            active_buckets_for_user: set[str] = set()
            for mk in months_sorted:
                bk = _bucket_for_month(mk, bucket)
                if bk in active_set_by_bucket:
                    active_set_by_bucket[bk].add(uid)
                    active_buckets_for_user.add(bk)

            first_bk = _bucket_for_month(first_m, bucket)
            last_bk = _bucket_for_month(last_m, bucket)
            is_joiner = first_bk in joiners_by_bucket
            if is_joiner:
                joiners_by_bucket[first_bk] += 1

            # Leaver = last_active_bucket != final bucket in window.
            is_leaver = (
                last_bk in leavers_by_bucket
                and last_bk != all_buckets[-1]
            )
            if is_leaver:
                leavers_by_bucket[last_bk] += 1

            # Tenure samples per bucket the user was active in.
            for bk in active_buckets_for_user:
                bk_start_month = _bucket_start_month(bk)
                tenure_samples[bk].append(_months_between(first_m, bk_start_month))

            per_user_out.append({
                **u,
                "first_active_month": first_m,
                "last_active_month": last_m,
                "first_active_date": f"{first_m}-01",
                "last_active_date": f"{last_m}-01",
                "total_handled": total_handled,
                "active_buckets": sorted(active_buckets_for_user),
                "is_joiner_in_window": is_joiner,
                "is_leaver_in_window": is_leaver,
            })

        per_user_out.sort(key=lambda r: -(r["total_handled"] or 0))

        headcount_by_bucket = [
            {
                "bucket": b,
                "active_agents": len(active_set_by_bucket[b]),
                "joiners": joiners_by_bucket[b],
                "leavers": leavers_by_bucket[b],
            }
            for b in all_buckets
        ]
        tenure_trend = []
        for b in all_buckets:
            samples = tenure_samples[b]
            tenure_trend.append({
                "bucket": b,
                "mean_tenure_months": round(mean(samples), 1) if samples else None,
                "median_tenure_months": round(median(samples), 1) if samples else None,
                "n": len(samples),
            })

        return {
            "interval": resolved_interval,
            "as_of_utc": datetime.now(timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "tz": tz_name,
            "bucket": bucket,
            "user_count": len(users),
            "data_starts_at": data_starts_at,
            "per_user": per_user_out,
            "headcount_by_bucket": headcount_by_bucket,
            "tenure_trend": tenure_trend,
        }
