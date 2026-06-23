"""Conversation participant attribute search.

v1.8+. Wraps ``POST /api/v2/conversations/participants/attributes/search``
so callers can ask "how many conversations had attribute X = Y?" or
"what's the NPS today?" — neither was reachable before.

Pre-v1.8 nothing in the codebase queried by participant attribute:

- ``search_conversations`` only filters by ANI / queue / agent / direction.
- The analytics async-jobs endpoint accepts a fixed enum of predicate
  dimensions; none target ``participants[].attributes``.
- ``get_conversation`` returns attributes for ONE conversation but
  doesn't search across many.

The dedicated Genesys "Conversations: Search by Participant Attributes"
endpoint is the right path (the user's link:
https://developer.genesys.cloud/organization/search/conversation-participant-attribute-search).
It supports EXACT-match and DATE_RANGE criteria, combined via AND.

This tool also auto-detects NPS shape (all integer values 0-10) and
computes the standard %promoters − %detractors score so the daily-brief
/ monthly-report skills can surface NPS without a second round-trip.
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._intervals import INTERVAL_HELP_STRING
from genesys_mcp._intervals import default_interval as _default_interval
from genesys_mcp._intervals import now_utc as _now_utc
from genesys_mcp._intervals import parse_iso as _parse_iso
from genesys_mcp.client import get_api, with_retry

logger = logging.getLogger(__name__)


# Field path conventions for the search endpoint. Both confirmed against
# the schema's free-form ``fields`` parameter; values verified during
# live tenant smoke. If a future schema rename breaks these, swap them
# here in one place rather than chasing every call site.
_ATTRIBUTE_FIELD_PREFIX = "participantData"
_DATE_RANGE_FIELD = "segments.start"

# Default value enumeration when caller doesn't pass ``attribute_value``.
# Covers the dominant "any value" case (NPS 0-10). For non-NPS attributes
# the caller must pass an explicit value — there's no exists-operator on
# this endpoint, so unbounded scans aren't supported.
_NPS_VALUES = [str(i) for i in range(0, 11)]


def _interval_to_search_range(interval: str) -> tuple[str, str]:
    """Convert canonical ISO-8601 interval to (startISO, endISO) for DATE_RANGE."""
    start_iso, end_iso = interval.split("/", 1)
    return (
        _parse_iso(start_iso).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        _parse_iso(end_iso).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def _build_body(
    *, attribute_key: str, attribute_value: str | None, interval: str,
    page_size: int, page_number: int,
) -> dict:
    """Build the search request body."""
    start_iso, end_iso = _interval_to_search_range(interval)

    attr_criterion: dict[str, Any] = {
        "type": "EXACT",
        "fields": [f"{_ATTRIBUTE_FIELD_PREFIX}.{attribute_key}"],
    }
    if attribute_value is not None:
        attr_criterion["value"] = attribute_value
    else:
        # Default to NPS enumeration when no specific value given.
        attr_criterion["values"] = list(_NPS_VALUES)

    return {
        "query": [
            {
                "type": "DATE_RANGE",
                "fields": [_DATE_RANGE_FIELD],
                "startValue": start_iso,
                "endValue": end_iso,
            },
            attr_criterion,
        ],
        "sortOrder": "DESC",
        "sortBy": "conversationStart",
        "pageSize": page_size,
        "pageNumber": page_number,
    }


def _extract_attribute_value(
    conv: dict, attribute_key: str,
) -> tuple[str | None, str | None]:
    """Extract (attribute_value, agent_user_id) from a conversation result.

    Returns the first matching participant's attribute value and the
    handling agent's userId (most recent agent participant). Either may
    be None.
    """
    found_value: str | None = None
    agent_user_id: str | None = None
    for p in conv.get("participants") or []:
        attrs = p.get("attributes") or {}
        if attribute_key in attrs and found_value is None:
            v = attrs[attribute_key]
            found_value = str(v) if v is not None else None
        if (p.get("purpose") or "").lower() == "agent":
            uid = p.get("userId")
            if uid:
                agent_user_id = uid  # last agent participant wins
    return found_value, agent_user_id


def _parse_numeric(value: str) -> float | None:
    """Try to parse a string as a number; return None if it doesn't look numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _numeric_summary(values: list[str]) -> dict | None:
    """Build numeric_summary block when all values parse as numbers.

    Adds the NPS sub-block when all values are integers in [0, 10].
    Returns None when values aren't fully numeric.
    """
    if not values:
        return None
    parsed: list[float] = []
    for v in values:
        n = _parse_numeric(v)
        if n is None:
            return None  # non-numeric value → bail
        parsed.append(n)

    summary: dict[str, Any] = {
        "count": len(parsed),
        "mean": round(statistics.mean(parsed), 2),
        "median": round(statistics.median(parsed), 2),
        "min": round(min(parsed), 2),
        "max": round(max(parsed), 2),
        "nps": None,
    }

    # NPS detection: all values must be integers in [0, 10]
    if all(n == int(n) and 0 <= n <= 10 for n in parsed):
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
    """Count distinct values, sort by count desc, include percentage."""
    if not values:
        return []
    counter = Counter(values)
    total = sum(counter.values())
    return [
        {
            "value": v,
            "count": c,
            "percentage": round(c / total * 100, 1),
        }
        for v, c in counter.most_common()
    ]


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def search_conversations_by_attribute(
        attribute_key: str = Field(
            description=(
                "Participant attribute key, exactly as Genesys stores it "
                "(spaces and case preserved). Examples seen in real tenants: "
                "'NPS Score', 'Agent Score', 'Experience Score', 'outcome', "
                "'csat'. Inspect `get_conversation` on a recent conversation "
                "to discover what keys YOUR tenant sets — Genesys has no "
                "org-wide enumeration endpoint."
            ),
        ),
        attribute_value: str | None = Field(
            default=None,
            description=(
                "Exact value to match (e.g. 'Resolved'). Omit to default "
                "to the standard NPS enumeration ['0','1',…,'10'] — the "
                "dominant 'any value' case. For non-NPS attributes pass "
                "the specific value; unbounded attribute scans aren't "
                "supported by the underlying Genesys endpoint."
            ),
        ),
        interval: str | None = Field(
            default=None,
            description=INTERVAL_HELP_STRING,
        ),
        max_results: int = Field(
            default=1000, ge=1, le=10000,
            description=(
                "Cap on returned per-conversation rows. The endpoint paginates "
                "in pages of 100; we stop fetching once max_results is reached "
                "and set `totals.truncated: true` if more matches existed."
            ),
        ),
        mode: str = Field(
            default="summary",
            description=(
                "Response shape. 'summary' (default) returns rollups + "
                "per-conversation rows. 'full' adds `_raw` with the underlying "
                "search results for callers that want to inspect them."
            ),
        ),
    ) -> dict:
        """Search conversations by customer/agent participant attribute.

        v1.8+. Wraps the dedicated Genesys endpoint:
        ``POST /api/v2/conversations/participants/attributes/search``.
        Returns matching conversations plus three pre-computed views so
        a single call answers the most common questions:

        - **`totals`** — `conversation_count`, `truncated` flag.
        - **`value_distribution`** — count + percentage for each distinct
          attribute value seen. Sorted by count desc.
        - **`numeric_summary`** — when all values parse as numbers, surfaces
          count / mean / median / min / max. When values are integers in
          `[0, 10]`, also computes the **NPS** rollup (`score`,
          detractor/passive/promoter buckets) so "what's the NPS today?"
          answers itself without a follow-up call.
        - **`conversations`** — one row per matching conversation, with
          `attribute_value` (the matched value) and `agent_user_id`
          (handling agent, when present) — useful for per-agent NPS
          rollups in coaching workflows.

        Top-level response carries the v1.5 contract: ``interval`` +
        ``as_of_utc`` at the top so persisted-file readers see the window
        immediately.

        Needs ``conversation:participant:attributesview`` (typically bundled
        into ``conversation:readonly``).
        """
        if mode not in ("summary", "full"):
            raise ValueError(
                f"search_conversations_by_attribute.mode must be 'summary' or "
                f"'full', got {mode!r}"
            )

        resolved_interval = interval or _default_interval(7)

        api_client = get_api()
        page_size = 100
        all_results: list[dict] = []
        truncated = False
        page_number = 1
        max_pages = 100

        while page_number <= max_pages:
            body = _build_body(
                attribute_key=attribute_key,
                attribute_value=attribute_value,
                interval=resolved_interval,
                page_size=page_size,
                page_number=page_number,
            )
            resp = with_retry(api_client.call_api)(
                resource_path="/api/v2/conversations/participants/attributes/search",
                method="POST",
                body=body,
                auth_settings=["PureCloud OAuth"],
                response_type="object",
            ) or {}
            page = resp.get("results") or []
            all_results.extend(page)
            if len(all_results) >= max_results:
                if len(all_results) > max_results or (resp.get("pageCount") or 1) > page_number:
                    truncated = True
                all_results = all_results[:max_results]
                break
            if len(page) < page_size:
                break  # last page
            if page_number >= (resp.get("pageCount") or page_number):
                break
            page_number += 1
        else:
            truncated = True

        conversations: list[dict] = []
        matched_values: list[str] = []
        for conv in all_results:
            attr_val, agent_uid = _extract_attribute_value(conv, attribute_key)
            if attr_val is None:
                continue
            matched_values.append(attr_val)
            conv_id = conv.get("conversationId") or conv.get("id")
            conv_start = conv.get("conversationStart")
            queue_id = None
            for p in conv.get("participants") or []:
                for s in p.get("segments") or []:
                    qid = s.get("queueId")
                    if qid and queue_id is None:
                        queue_id = qid
                        break
                if queue_id:
                    break
            conversations.append({
                "conversation_id": conv_id,
                "conversation_start": conv_start,
                "queue_id": queue_id,
                "agent_user_id": agent_uid,
                "attribute_value": attr_val,
            })

        distribution = _value_distribution(matched_values)
        numeric_summary = _numeric_summary(matched_values)

        out: dict[str, Any] = {
            "interval": resolved_interval,
            "as_of_utc": _now_utc().isoformat().replace("+00:00", "Z"),
            "attribute_key": attribute_key,
            "attribute_value": attribute_value,
            "mode": mode,
            "totals": {
                "conversation_count": len(conversations),
                "truncated": truncated,
            },
            "value_distribution": distribution,
            "numeric_summary": numeric_summary,
            "conversations": conversations,
        }
        if mode == "full":
            out["_raw"] = {"results": all_results}
        return out
