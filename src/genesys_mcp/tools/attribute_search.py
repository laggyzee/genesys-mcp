"""Conversation participant attribute search.

v1.20 rewrite. ``POST /api/v2/conversations/participants/attributes/search``
does NOT support searching by attribute key/value — its only searchable
fields are ``conversationId``, ``startTime``, ``endTime`` and ``divisionId``
(Genesys "Conversation participant attributes search" docs). The v1.8–v1.19
implementation sent an EXACT criterion on ``participantData.<key>`` plus a
DATE_RANGE on ``segments.start``; both are unsupported field paths and the
endpoint rejects the whole request with a generic 400 "Search not supported."
(verified live 2026-07-28).

The working shape (also verified live): DATE_RANGE on ``startTime`` only.
Each result row carries the full attribute payload::

    {
      "conversationId": "...", "startTime": "...", "endTime": "...",
      "divisionIds": ["..."],
      "participantData": [
        {"participantPurpose": "external",
         "participantAttributes": {"NPS Score": "9", ...},
         "participantId": "...", "sessionIds": ["..."]}
      ],
      "truncatedData": false
    }

so attribute filtering happens client-side. That flips the old limitation on
its head: "key exists with ANY value" is now the natural default (no more
0–10 value enumeration), and value-format drift ("9.0", "N/A", 0–100 scales)
can't silently drop records.

Endpoint constraints honoured here (from the Genesys search-limits docs):

- 30-day retention — windows older than that return nothing; we clamp and say so.
- Genesys recommends ≤4h pulls (24h is the current hard max, shrinking) —
  we chunk the requested interval into windows, newest first: 4h windows for
  spans up to a week, 12h for longer spans (a month at 4h would be 180+
  sequential round-trips inside one tool call).
- Cursor pagination only (no pageSize/pageNumber), ~1MB / ~50 rows per page.
- Only conversations WITH attributes (<20KB of them) are stored at all;
  rows whose payload was cut carry ``truncatedData: true``.

The endpoint's rows carry no queue/agent identifiers, but downstream
consumers (cc-coaching-prep per-agent NPS, brand/queue rollups) rely on the
``queue_id`` / ``agent_user_id`` row fields, so matched conversations are
enriched via one batched ``POST /api/v2/analytics/conversations/details/query``
per 50 ids. Enrichment is best-effort: a failure leaves the fields null and
adds a note rather than failing the search.
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._intervals import INTERVAL_HELP_STRING
from genesys_mcp._intervals import default_interval as _default_interval
from genesys_mcp._intervals import now_utc as _now_utc
from genesys_mcp._intervals import parse_iso as _parse_iso
from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)

# The only date field the endpoint can range-search (with endTime).
_DATE_RANGE_FIELD = "startTime"

# Genesys recommends ≤4h pulls (their stability guidance, likely to become
# enforced); 24h is the current hard max. Short spans use the recommended 4h.
# Long spans (a monthly report) would need 180+ sequential windows at 4h, so
# they use 12h — still comfortably under the current 24h max. If Genesys
# starts enforcing 4h, requests will fail loudly with a 400 and this constant
# is the one-line fix.
_WINDOW = timedelta(hours=4)
_WINDOW_LONG = timedelta(hours=12)
_LONG_SPAN_THRESHOLD = timedelta(days=7)

# Endpoint stores conversations for 30 days, then drops them.
_RETENTION_DAYS = 30

# Global page budget across all windows. Pages are ~50 rows / ≤1MB. Every
# window costs at least one page even when empty (a full 30-day scan is 60
# twelve-hour windows), so 300 covers a full-retention scan of a quiet org
# while still hard-bounding a busy one (~15,000 scanned rows). Exhausting it
# sets `truncated: true` plus an explicit note — never a silent cap.
_MAX_PAGES_TOTAL = 300

# Guard against a server that keeps echoing a cursor forever within one window.
_MAX_PAGES_PER_WINDOW = 60

# Enrichment: batched analytics details query, 50 conversation ids per POST.
# The cap matches the tool's max_results default (20 batched calls worst case)
# so a default pull never returns partially-enriched rows.
_ENRICH_BATCH = 50
_ENRICH_MAX = 1000

# available_keys is capped to the most common keys; the cap is reported.
_KEYS_TOP_N = 50

# mode="full" raw-results cap (a full day can be thousands of rows).
_RAW_CAP = 200

# Values treated as an explicit "customer did not respond" sentinel: they are
# excluded from the numeric summary (counted separately) so a survey that
# writes "N/A" for non-responses can't disable the NPS rollup.
_NO_RESPONSE_SENTINELS = {"", "n/a", "na", "none", "null", "no response", "not answered"}


def _interval_bounds(interval: str) -> tuple[datetime, datetime]:
    start_iso, end_iso = interval.split("/", 1)
    start = _parse_iso(start_iso).astimezone(timezone.utc)
    end = _parse_iso(end_iso).astimezone(timezone.utc)
    if start >= end:
        raise ValueError(f"interval start must precede end, got {interval!r}")
    return start, end


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _windows(start: datetime, end: datetime) -> list[tuple[str, str]]:
    """Chunk [start, end) into windows, newest first.

    Newest-first means the max_results / page-budget caps keep the most
    recent conversations, matching the old sortOrder=DESC behaviour.
    Adjacent windows share a boundary instant; the scan loop dedupes by
    conversationId in case DATE_RANGE is inclusive at both ends.
    """
    window = _WINDOW if (end - start) <= _LONG_SPAN_THRESHOLD else _WINDOW_LONG
    out: list[tuple[str, str]] = []
    hi = end
    while hi > start:
        lo = max(start, hi - window)
        out.append((_iso_z(lo), _iso_z(hi)))
        hi = lo
    return out


def _build_body(window_start: str, window_end: str, cursor: str | None) -> dict:
    """DATE_RANGE on startTime is the ONLY criterion the endpoint accepts for
    interval pulls — adding attribute criteria (or sort fields the schema
    doesn't index) draws the 400 "Search not supported." rejection."""
    body: dict[str, Any] = {
        "query": [
            {
                "type": "DATE_RANGE",
                "fields": [_DATE_RANGE_FIELD],
                "startValue": window_start,
                "endValue": window_end,
            },
        ],
    }
    if cursor:
        body["cursor"] = cursor
    return body


def _extract_attribute_value(row: dict, attribute_key: str) -> str | None:
    """First participant carrying the key wins; values are stringified."""
    for pd in row.get("participantData") or []:
        attrs = pd.get("participantAttributes") or {}
        if attribute_key in attrs:
            v = attrs[attribute_key]
            return str(v) if v is not None else None
    return None


def _conversation_keys(row: dict) -> set[str]:
    keys: set[str] = set()
    for pd in row.get("participantData") or []:
        keys.update((pd.get("participantAttributes") or {}).keys())
    return keys


def _parse_numeric(value: str) -> float | None:
    """Parse a finite number; NaN/inf strings count as non-numeric (they'd
    blow up int() in the NPS check and aren't valid survey scores anyway)."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _is_no_response(value: str) -> bool:
    return value.strip().lower() in _NO_RESPONSE_SENTINELS


def _numeric_summary(values: list[str]) -> dict | None:
    """Numeric rollup over the numeric subset of matched values.

    No-response sentinels ("N/A", "", …) are excluded and counted in
    ``no_response_count``. Genuinely non-numeric values (e.g. "Resolved")
    are counted in ``non_numeric_count`` AND disable the NPS block — mixed
    text/number data isn't a survey scale. NPS computes when every remaining
    value is an integer 0–10 (accepts "9.0"-style decimals that are whole).
    """
    if not values:
        return None
    parsed: list[float] = []
    no_response = 0
    non_numeric = 0
    for v in values:
        if _is_no_response(v):
            no_response += 1
            continue
        n = _parse_numeric(v)
        if n is None:
            non_numeric += 1
            continue
        parsed.append(n)
    if not parsed:
        return None

    summary: dict[str, Any] = {
        "count": len(parsed),
        "no_response_count": no_response,
        "non_numeric_count": non_numeric,
        "mean": round(statistics.mean(parsed), 2),
        "median": round(statistics.median(parsed), 2),
        "min": round(min(parsed), 2),
        "max": round(max(parsed), 2),
        "nps": None,
    }

    if non_numeric == 0 and all(n == int(n) and 0 <= n <= 10 for n in parsed):
        ints = [int(n) for n in parsed]
        detractors = sum(1 for n in ints if 0 <= n <= 6)
        passives = sum(1 for n in ints if 7 <= n <= 8)
        promoters = sum(1 for n in ints if 9 <= n <= 10)
        total = len(ints)
        score = round((promoters - detractors) / total * 100, 1) if total else None
        summary["nps"] = {
            "score": score,
            "detractors_0_6": detractors,
            "passives_7_8": passives,
            "promoters_9_10": promoters,
        }

    return summary


def _value_distribution(values: list[str]) -> list[dict]:
    if not values:
        return []
    counter = Counter(values)
    total = sum(counter.values())
    return [
        {"value": v, "count": c, "percentage": round(c / total * 100, 1)}
        for v, c in counter.most_common()
    ]


def _enrich_rows(rows: list[dict], scan_start: datetime, scan_end: datetime) -> None:
    """Fill queue_id / agent_user_id in-place from the analytics details query.

    The attribute-search rows carry no queue or agent identifiers, but the
    coaching / brand rollups downstream need them. Matched conversations are
    sparse (survey responders), so one batched details query per 50 ids is
    cheap. Padding the interval ±1h absorbs any boundary-inclusivity skew
    between the two endpoints.
    """
    api = gc.AnalyticsApi(get_api())
    interval = f"{_iso_z(scan_start - timedelta(hours=1))}/{_iso_z(scan_end + timedelta(hours=1))}"
    by_id = {r["conversation_id"]: r for r in rows if r.get("conversation_id")}
    ids = list(by_id.keys())[:_ENRICH_MAX]
    for i in range(0, len(ids), _ENRICH_BATCH):
        batch = ids[i:i + _ENRICH_BATCH]
        body = {
            "interval": interval,
            "order": "desc",
            "orderBy": "conversationStart",
            "paging": {"pageSize": 100, "pageNumber": 1},
            "conversationFilters": [{
                "type": "or",
                "predicates": [
                    {"type": "dimension", "dimension": "conversationId",
                     "operator": "matches", "value": cid}
                    for cid in batch
                ],
            }],
        }
        resp = to_dict(with_retry(api.post_analytics_conversations_details_query)(body)) or {}
        for conv in resp.get("conversations") or []:
            cid = conv.get("conversation_id") or conv.get("conversationId")
            row = by_id.get(cid)
            if not row:
                continue
            queue_id: str | None = None
            agent_user_id: str | None = None
            for p in conv.get("participants") or []:
                if (p.get("purpose") or "").lower() == "agent":
                    uid = p.get("user_id") or p.get("userId")
                    if uid:
                        agent_user_id = uid  # last agent participant wins
                for s in p.get("sessions") or []:
                    for seg in s.get("segments") or []:
                        qid = seg.get("queue_id") or seg.get("queueId")
                        if qid and queue_id is None:
                            queue_id = qid
            row["queue_id"] = queue_id
            row["agent_user_id"] = agent_user_id


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def search_conversations_by_attribute(
        attribute_key: str = Field(
            description=(
                "Participant attribute key, exactly as Genesys stores it "
                "(spaces and case preserved), e.g. 'NPS Score', 'outcome', "
                "'csat'. If unsure of the spelling, run once with your best "
                "guess — the response's `available_keys` lists the keys "
                "actually seen in the window (top 50 by frequency)."
            ),
        ),
        attribute_value: str | None = Field(
            default=None,
            description=(
                "Exact value to match (e.g. 'Resolved'). Omit to match ANY "
                "value — filtering is client-side, so key-exists queries are "
                "fully supported and no value format ('9', '9.0', 'N/A', "
                "0-100 scales) is ever silently dropped."
            ),
        ),
        interval: str | None = Field(
            default=None,
            description=INTERVAL_HELP_STRING + (
                " NOTE: this endpoint only retains conversations for 30 "
                "days; older windows are clamped (a note says so)."
            ),
        ),
        max_results: int = Field(
            default=1000, ge=1, le=10000,
            description=(
                "Cap on returned per-conversation rows (newest kept). "
                "`totals.truncated: true` when more matches likely existed."
            ),
        ),
        mode: str = Field(
            default="summary",
            description=(
                "Response shape. 'summary' (default) returns rollups + "
                "per-conversation rows. 'full' adds `_raw` with up to "
                f"{_RAW_CAP} of the underlying scanned rows."
            ),
        ),
    ) -> dict:
        """Search conversations by customer/agent participant attribute.

        USE THIS for NPS and other per-conversation survey/participant
        attributes (e.g. "NPS for Acme last week"). Omit ``attribute_value``
        to match any value; when the matched values look like an NPS scale
        (integers 0-10, decimals like "9.0" included, "N/A"-style
        no-response sentinels excluded) the standard NPS rollup (score +
        promoter/passive/detractor split) is computed automatically.

        v1.20+: the Genesys endpoint
        (``POST /api/v2/conversations/participants/attributes/search``)
        only supports searching by conversationId/startTime/endTime/divisionId
        — NOT by attribute — so this tool pulls the window in ≤4h chunks
        (12h for spans over a week; newest first, cursor-paginated) and
        filters attributes client-side. Consequences worth knowing:

        - **`available_keys`** lists the most common attribute keys seen
          while scanning (top 50), with per-conversation counts — if your
          key returned nothing, check it for the real spelling instead of
          guessing.
        - The endpoint retains only **30 days** of conversations; older
          windows are clamped with a note (`scanned_interval` reports the
          window actually scanned when it differs from `interval`).
        - Scan cost is proportional to how many attribute-carrying
          conversations the org had in the window, not to matches. A page
          budget bounds the walk; hitting any cap sets ``totals.truncated``
          and an explicit note.

        Returns (v1.5 envelope: top-level ``interval`` + ``as_of_utc``):

        - **`totals`** — `conversation_count` (matched), `conversations_scanned`,
          `truncated`.
        - **`value_distribution`** — count + percentage per distinct matched
          value, sorted by count desc.
        - **`numeric_summary`** — count / no_response_count /
          non_numeric_count / mean / median / min / max over the numeric
          subset, plus the **NPS** block when values are NPS-shaped.
        - **`conversations`** — one row per match: `conversation_id`,
          `conversation_start`, `queue_id`, `agent_user_id`,
          `attribute_value`. Queue/agent come from a batched analytics
          details lookup (best-effort; null + note on failure).
        - **`available_keys`** + **`notes`** — see above.

        Needs ``conversation:participant:attributesview`` (typically bundled
        into ``conversation:readonly``); queue/agent enrichment additionally
        uses ``analytics:conversationDetail:view`` (soft — rows stay null
        without it).
        """
        if mode not in ("summary", "full"):
            raise ValueError(
                f"search_conversations_by_attribute.mode must be 'summary' or "
                f"'full', got {mode!r}"
            )

        resolved_interval = interval or _default_interval(7)
        req_start, req_end = _interval_bounds(resolved_interval)
        notes: list[str] = []

        now = _now_utc()
        retention_floor = now - timedelta(days=_RETENTION_DAYS)
        scan_start, scan_end = req_start, req_end
        if scan_end <= retention_floor:
            notes.append(
                f"Requested window ends before the endpoint's {_RETENTION_DAYS}-day "
                "retention horizon — participant-attribute data for it no longer "
                "exists. Returning empty results without querying."
            )
            scan_windows: list[tuple[str, str]] = []
        else:
            if scan_start < retention_floor:
                scan_start = retention_floor
                notes.append(
                    f"Window start clamped to {_iso_z(retention_floor)}: the endpoint "
                    f"only retains {_RETENTION_DAYS} days of conversations."
                )
            scan_windows = _windows(scan_start, scan_end)

        api_client = get_api()
        matched: list[dict] = []
        matched_values: list[str] = []
        keys_counter: Counter[str] = Counter()
        seen_ids: set[str] = set()
        raw_sample: list[dict] = []
        scanned = 0
        pages_used = 0
        truncated = False
        overflow_matches = 0  # matches discarded because max_results was full
        truncated_rows = 0    # rows the endpoint flagged truncatedData: true

        def _cap_hit() -> str | None:
            if len(matched) >= max_results:
                return "max_results"
            if pages_used >= _MAX_PAGES_TOTAL:
                return "page_budget"
            return None

        stop_reason: str | None = None
        for w_idx, (w_start, w_end) in enumerate(scan_windows):
            cursor: str | None = None
            for _ in range(_MAX_PAGES_PER_WINDOW):
                resp = with_retry(api_client.call_api)(
                    resource_path="/api/v2/conversations/participants/attributes/search",
                    method="POST",
                    body=_build_body(w_start, w_end, cursor),
                    auth_settings=["PureCloud OAuth"],
                    response_type="object",
                ) or {}
                pages_used += 1
                page = resp.get("results") or []
                cursor = resp.get("cursor") or None

                for row in page:
                    cid = row.get("conversationId")
                    if cid:
                        # Adjacent windows share a boundary instant and cursor
                        # replays are possible — count each conversation once.
                        if cid in seen_ids:
                            continue
                        seen_ids.add(cid)
                    scanned += 1
                    if row.get("truncatedData"):
                        truncated_rows += 1
                    if len(raw_sample) < _RAW_CAP:
                        raw_sample.append(row)
                    for k in _conversation_keys(row):
                        keys_counter[k] += 1
                    value = _extract_attribute_value(row, attribute_key)
                    if value is None:
                        continue
                    if attribute_value is not None and value != attribute_value:
                        continue
                    if len(matched) >= max_results:
                        overflow_matches += 1
                        continue  # keep scanning stats honest within the page
                    matched.append({
                        "conversation_id": cid,
                        "conversation_start": row.get("startTime"),
                        "queue_id": None,
                        "agent_user_id": None,
                        "attribute_value": value,
                    })
                    matched_values.append(value)

                stop_reason = _cap_hit()
                if stop_reason or not cursor:
                    break

            windows_remaining = w_idx < len(scan_windows) - 1
            if stop_reason:
                more_unscanned = bool(cursor) or windows_remaining
                truncated = truncated or more_unscanned or overflow_matches > 0
                if more_unscanned:
                    if stop_reason == "max_results":
                        notes.append(
                            f"Stopped after reaching max_results={max_results}; older "
                            "conversations in the window were not scanned."
                        )
                    else:
                        notes.append(
                            f"Stopped after the {_MAX_PAGES_TOTAL}-page scan budget "
                            f"(~{scanned} conversations scanned); older conversations "
                            "in the window were not scanned. Narrow the interval for "
                            "complete coverage."
                        )
                break
            if cursor:
                # Per-window page guard tripped with data still pending.
                truncated = True
                notes.append(
                    f"Window {w_start}/{w_end} exceeded {_MAX_PAGES_PER_WINDOW} pages; "
                    "its remaining conversations were not scanned."
                )

        if overflow_matches:
            truncated = True
            notes.append(
                f"{overflow_matches} additional matching conversation(s) in the "
                f"scanned pages were discarded by max_results={max_results} — "
                "rollups cover only the returned rows."
            )

        if truncated_rows:
            notes.append(
                f"{truncated_rows} scanned conversation(s) carried truncatedData: "
                "true (attribute payload cut at the endpoint's per-conversation "
                "limit) — the searched key may be missing from those rows."
            )

        if matched:
            try:
                _enrich_rows(matched, scan_start, scan_end)
            except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
                logger.warning("attribute-search enrichment failed: %s", exc)
                notes.append(
                    "queue_id/agent_user_id enrichment failed "
                    f"({type(exc).__name__}); rows returned with null queue/agent. "
                    "Enrichment needs analytics:conversationDetail:view."
                )
            if len(matched) > _ENRICH_MAX:
                notes.append(
                    f"Only the first {_ENRICH_MAX} matched conversations were "
                    "enriched with queue/agent ids."
                )

        matched.sort(key=lambda r: r.get("conversation_start") or "", reverse=True)

        if not matched and scanned and attribute_key not in keys_counter:
            near = [k for k in keys_counter if k.lower() == attribute_key.lower()]
            hint = f" Did you mean {near[0]!r}?" if near else ""
            notes.append(
                f"Attribute key {attribute_key!r} was not present on any of the "
                f"{scanned} attribute-carrying conversations scanned — it may be "
                "misspelled, or the org may not write it. Check `available_keys`."
                + hint
            )

        available_keys = {
            "total_distinct": len(keys_counter),
            "top": [
                {"key": k, "conversations": c}
                for k, c in keys_counter.most_common(_KEYS_TOP_N)
            ],
        }

        out: dict[str, Any] = {
            "interval": resolved_interval,
            "as_of_utc": _iso_z(_now_utc()),
            "attribute_key": attribute_key,
            "attribute_value": attribute_value,
            "mode": mode,
            "totals": {
                "conversation_count": len(matched),
                "conversations_scanned": scanned,
                "truncated": truncated,
            },
            "value_distribution": _value_distribution(matched_values),
            "numeric_summary": _numeric_summary(matched_values),
            "conversations": matched,
            "available_keys": available_keys,
            "notes": notes,
        }
        if (scan_start, scan_end) != (req_start, req_end) and scan_windows:
            out["scanned_interval"] = f"{_iso_z(scan_start)}/{_iso_z(scan_end)}"
        if mode == "full":
            if scanned > _RAW_CAP:
                notes.append(
                    f"_raw capped at {_RAW_CAP} of {scanned} scanned rows."
                )
            out["_raw"] = {"results": raw_sample}
        return out
