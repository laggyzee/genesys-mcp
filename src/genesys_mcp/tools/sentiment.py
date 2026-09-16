"""Aggregate sentiment per queue / media / brand over an interval.

v1.23+. Closes the gap between the per-conversation
``get_conversation_sentiment`` tool (fine for coaching one call) and the
ops question *"what was each brand's sentiment last week, and did it
move?"* — which previously needed an N+1 walk over every conversation.

Backed by ``POST /api/v2/analytics/transcripts/aggregates/query`` (the
speech-and-text-analytics aggregate endpoint; permission
``analytics:speechAndTextAnalyticsAggregates:view``). One call returns,
per ``queueId × mediaType`` group:

- ``nSpeechTextAnalyzedConversations`` — conversations STA processed
- ``oSentimentScore`` — the per-communication sentiment score Genesys
  shows as "Sentiment score" (−100 … +100); ``sum / count`` is the mean.
  ``count`` can exceed the conversation count on voice because a
  conversation with several communications (transfer, callback leg)
  contributes one score per communication.
- ``oCustomerSentiment`` — customer phrase-level sentiment: each scored
  phrase is ±100, so ``countPositive`` / ``countNegative`` are phrase
  counts, not conversation counts.

Brand rollup uses the tenant's ``queues.name_pattern`` (via
``queue_parser.parse_queue_name``) and honours ``queues.skip_substrings``
so holding / internal / staging queues never dilute a brand's number.
Rows Genesys returns *without* a ``queueId`` (pre-queue bot / flow
messaging) are surfaced under ``unattributed`` and never folded into a
brand or the headline totals.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from typing import Any

import PureCloudPlatformClientV2 as gc
from PureCloudPlatformClientV2.rest import ApiException
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._aggregates import run_chunked_query
from genesys_mcp._envelopes import soft_fail_envelope
from genesys_mcp._intervals import INTERVAL_HELP_STRING
from genesys_mcp._intervals import default_interval as _default_interval
from genesys_mcp._intervals import now_utc as _now_utc
from genesys_mcp._intervals import parse_iso as _parse_iso
from genesys_mcp.client import get_api, to_dict, with_retry
from genesys_mcp.naming import resolver
from genesys_mcp.queue_parser import parse_queue_name
from genesys_mcp.tenant import TenantConfigError, load_config

logger = logging.getLogger(__name__)

_METRICS = [
    "nSpeechTextAnalyzedConversations",
    "oSentimentScore",
    "oCustomerSentiment",
]

_SCALE_NOTE = (
    "-100 (very negative) to +100 (very positive). mean_sentiment is the "
    "record-weighted mean of Genesys' per-communication sentiment score — "
    "the same figure the Genesys 'Sentiment score' column shows."
)

_NOTES = [
    "Messaging and email consistently score lower than voice on Genesys "
    "text sentiment; compare a channel with itself over time rather than "
    "across channels.",
    "sentiment_records can exceed analyzed_conversations (mostly on voice) "
    "because each communication in a conversation is scored separately.",
    "customer_phrases_* are phrase counts (each scored phrase is +100 or "
    "-100), not conversation counts.",
    "unattributed rows had no queueId in Genesys (pre-queue bot / flow "
    "traffic) and are excluded from totals and by_brand.",
]


def _prior_interval(interval: str) -> str:
    """Same length, immediately before ``interval``."""
    start_iso, end_iso = interval.split("/", 1)
    start = _parse_iso(start_iso).astimezone(timezone.utc)
    end = _parse_iso(end_iso).astimezone(timezone.utc)
    length = end - start
    prior_start = start - length
    return (
        prior_start.isoformat().replace("+00:00", "Z")
        + "/"
        + start.isoformat().replace("+00:00", "Z")
    )


def _build_body(
    *,
    interval: str,
    queue_ids: list[str] | None,
    media_types: list[str] | None,
    group_by_media: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "interval": interval,
        "groupBy": ["queueId", "mediaType"] if group_by_media else ["queueId"],
        "metrics": list(_METRICS),
    }
    clauses: list[dict[str, Any]] = []
    if queue_ids:
        clauses.append({"type": "or", "predicates": [
            {"dimension": "queueId", "value": qid} for qid in queue_ids
        ]})
    if media_types:
        clauses.append({"type": "or", "predicates": [
            {"dimension": "mediaType", "value": m} for m in media_types
        ]})
    if clauses:
        body["filter"] = {"type": "and", "clauses": clauses}
    return body


# ───────────────────────────── parsing ─────────────────────────────

def _empty_acc() -> dict[str, Any]:
    return {
        "n": 0, "sc": 0, "ssum": 0.0, "smin": None, "smax": None,
        "pos": None, "neg": None,
    }


def _add_stats(acc: dict[str, Any], metrics: dict[str, dict]) -> None:
    n = int((metrics.get("nSpeechTextAnalyzedConversations") or {}).get("count", 0) or 0)
    s = metrics.get("oSentimentScore") or {}
    c = metrics.get("oCustomerSentiment")
    acc["n"] += n
    acc["sc"] += int(s.get("count", 0) or 0)
    acc["ssum"] += float(s.get("sum", 0) or 0)
    for key, fn in (("smin", min), ("smax", max)):
        v = s.get("min" if key == "smin" else "max")
        if v is not None:
            acc[key] = v if acc[key] is None else fn(acc[key], v)
    if c is not None:
        pos = int(c.get("countPositive", 0) or 0)
        neg = int(c.get("countNegative", 0) or 0)
        acc["pos"] = (acc["pos"] or 0) + pos
        acc["neg"] = (acc["neg"] or 0) + neg


def _parse_groups(resp: dict) -> dict[tuple[str | None, str | None], dict[str, Any]]:
    """Collapse a transcript-aggregates response to {(queue_id, media): acc}.

    Chunked (multi-year) responses can carry several buckets per group;
    they're summed here.
    """
    out: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for grp in resp.get("results") or []:
        key_raw = grp.get("group") or {}
        key = (key_raw.get("queueId"), key_raw.get("mediaType"))
        acc = out.setdefault(key, _empty_acc())
        for bucket in grp.get("data") or []:
            metrics = {
                m["metric"]: (m.get("stats") or {})
                for m in (bucket.get("metrics") or [])
                if m.get("metric")
            }
            _add_stats(acc, metrics)
    return out


def _mean(acc: dict[str, Any]) -> float | None:
    return round(acc["ssum"] / acc["sc"], 1) if acc["sc"] else None


def _pos_pct(acc: dict[str, Any]) -> float | None:
    pos, neg = acc.get("pos"), acc.get("neg")
    if pos is None and neg is None:
        return None
    total = (pos or 0) + (neg or 0)
    return round((pos or 0) / total * 100, 1) if total else None


def _delta(cur: float | None, prior: float | None) -> float | None:
    if cur is None or prior is None:
        return None
    return round(cur - prior, 1)


def _measures(acc: dict[str, Any], prior: dict[str, Any] | None) -> dict[str, Any]:
    mean = _mean(acc)
    prior_mean = _mean(prior) if prior else None
    return {
        "analyzed_conversations": acc["n"],
        "sentiment_records": acc["sc"],
        "mean_sentiment": mean,
        "min_sentiment": acc["smin"],
        "max_sentiment": acc["smax"],
        "customer_phrases_positive": acc["pos"],
        "customer_phrases_negative": acc["neg"],
        "customer_phrase_positive_pct": _pos_pct(acc),
        "prior_mean_sentiment": prior_mean,
        "delta_sentiment": _delta(mean, prior_mean),
    }


def _merge_into(target: dict[str, Any], src: dict[str, Any]) -> None:
    target["n"] += src["n"]
    target["sc"] += src["sc"]
    target["ssum"] += src["ssum"]
    for key, fn in (("smin", min), ("smax", max)):
        if src[key] is not None:
            target[key] = src[key] if target[key] is None else fn(target[key], src[key])
    for key in ("pos", "neg"):
        if src[key] is not None:
            target[key] = (target[key] or 0) + src[key]


def _load_queue_naming() -> tuple[str | None, bool, list[str], str | None]:
    """Return (name_pattern, match_required, skip_substrings, unavailable_reason)."""
    try:
        cfg = load_config()
    except TenantConfigError as exc:
        return None, True, [], (
            "Brand rollup needs a tenant config (queues.name_pattern). "
            f"{exc} — run the genesys-tenant-setup skill or set "
            "$GENESYS_MCP_CONFIG."
        )
    q = cfg.queues
    if not q.name_pattern or "{brand}" not in q.name_pattern:
        return None, True, list(q.skip_substrings or []), (
            "tenant.yaml queues.name_pattern has no {brand} placeholder, so "
            "queues can't be attributed to brands."
        )
    return (
        q.name_pattern,
        bool(getattr(q, "name_pattern_match_required", True)),
        list(q.skip_substrings or []),
        None,
    )


# ───────────────────────────── tool ─────────────────────────────

def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def sentiment_summary(
        interval: str | None = Field(
            default=None,
            description=INTERVAL_HELP_STRING,
        ),
        queue_ids: list[str] | None = Field(
            default=None,
            description=(
                "Optional queue filter (OR'd together). Omit for every queue "
                "in the org — the usual choice for a brand-level weekly view."
            ),
        ),
        media_types: list[str] | None = Field(
            default=None,
            description=(
                "Optional media-type filter: one or more of 'voice', "
                "'message', 'email', 'chat', 'callback'. Omit for all media."
            ),
        ),
        group_by_media: bool = Field(
            default=True,
            description=(
                "If true (default), rows are split per media type so voice / "
                "message / email sentiment can be read separately. Set false "
                "for one row per queue across all media."
            ),
        ),
        include_trend: bool = Field(
            default=True,
            description=(
                "When true (default), also queries the immediately-prior "
                "interval of the same length and adds prior_mean_sentiment + "
                "delta_sentiment to every row. Set false for a one-shot read."
            ),
        ),
        mode: str = Field(
            default="summary",
            description=(
                "'summary' (default) returns the derived rows only. 'full' "
                "adds the raw Genesys aggregate responses under 'raw' — only "
                "needed to debug a number."
            ),
        ),
    ) -> dict:
        """Sentiment score per queue, per media type and per brand over an interval.

        Answers *"what was each brand's sentiment last reporting week, and
        did it move?"* in one call. Backed by the Genesys transcript
        aggregates endpoint (``POST /api/v2/analytics/transcripts/aggregates/
        query``), grouped by ``queueId`` (+ ``mediaType``).

        Response blocks:
          - ``totals``   — org-wide (or filtered) headline: analyzed
            conversations, sentiment records, mean_sentiment, phrase counts,
            prior mean + delta. Excludes unattributed rows.
          - ``by_brand`` — one row per brand (media_type = null) followed by
            one per brand × media, derived from tenant.yaml
            ``queues.name_pattern`` with ``queues.skip_substrings`` applied.
            ``null`` with ``by_brand_unavailable_reason`` when no tenant
            config is present.
          - ``by_queue`` — one row per queue × media, worst mean first, with
            queue_name / brand / function resolved and
            ``excluded_from_brand_rollup`` flagged.
          - ``unattributed`` — rows Genesys returned with no queueId
            (pre-queue bot / flow messaging). Never folded into brands.

        Scale is −100 … +100 (see ``scale``). ``sentiment_records`` can
        exceed ``analyzed_conversations`` on voice because each communication
        is scored separately; means are record-weighted. Messaging and email
        score structurally lower than voice — compare like with like.

        Needs ``analytics:speechAndTextAnalyticsAggregates:view`` (bundled
        into ``analytics:readonly`` on tenants verified so far). Soft-fails
        with a canonical envelope on 403.
        """
        if mode not in ("summary", "full"):
            raise ValueError(
                f"sentiment_summary.mode must be 'summary' or 'full', got {mode!r}"
            )

        resolved_interval = interval or _default_interval(7)
        prior_interval_str = _prior_interval(resolved_interval) if include_trend else None

        api = gc.AnalyticsApi(get_api())

        def _query(iv: str) -> dict:
            body = _build_body(
                interval=iv,
                queue_ids=queue_ids,
                media_types=media_types,
                group_by_media=group_by_media,
            )
            return to_dict(with_retry(api.post_analytics_transcripts_aggregates_query)(body)) or {}

        holder: dict[str, dict] = {}

        def _fetch_current() -> None:
            holder["current"] = run_chunked_query(_query, resolved_interval)

        def _fetch_prior() -> None:
            holder["prior"] = run_chunked_query(_query, prior_interval_str)  # type: ignore[arg-type]

        try:
            if include_trend:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    for fut in [pool.submit(_fetch_current), pool.submit(_fetch_prior)]:
                        fut.result()
            else:
                _fetch_current()
                holder["prior"] = {"results": []}
        except ApiException as exc:
            return soft_fail_envelope(
                status=int(getattr(exc, "status", 0) or 500),
                kind="sentiment_summary",
                message=(
                    "Transcript aggregates query failed: "
                    f"{getattr(exc, 'reason', None) or type(exc).__name__}. "
                    "If the status is 403, grant the OAuth client "
                    "'analytics:speechAndTextAnalyticsAggregates:view' "
                    "(bundled into 'analytics:readonly' on most tenants)."
                ),
                interval=resolved_interval,
                http_body=(getattr(exc, "body", None) or "")[:500] if getattr(exc, "body", None) else None,
            )

        current = _parse_groups(holder["current"])
        prior = _parse_groups(holder["prior"]) if include_trend else {}

        # Resolve names once for every queue seen in either period.
        seen_ids = sorted({q for (q, _m) in list(current) + list(prior) if q})
        names = resolver.queue_names(seen_ids) if seen_ids else {}

        pattern, match_required, skip_substrings, brand_reason = _load_queue_naming()

        def _parse(queue_name: str | None) -> tuple[str | None, str | None]:
            if not queue_name or pattern is None:
                return None, None
            parts = parse_queue_name(queue_name, pattern, match_required=match_required)
            if parts is None or not parts.matched:
                return None, None
            return parts.brand, parts.function

        def _skipped(queue_name: str | None) -> bool:
            if not queue_name:
                return True
            return any(s in queue_name for s in skip_substrings)

        by_queue: list[dict[str, Any]] = []
        unattributed: list[dict[str, Any]] = []
        totals_acc = _empty_acc()
        totals_prior = _empty_acc()
        unattributed_n = 0
        brand_acc: dict[tuple[str, str | None], dict[str, Any]] = {}
        brand_prior: dict[tuple[str, str | None], dict[str, Any]] = {}

        for (qid, media), acc in current.items():
            p = prior.get((qid, media))
            if not qid:
                row = {"media_type": media, **_measures(acc, p)}
                unattributed.append(row)
                unattributed_n += acc["n"]
                continue
            qname = names.get(qid)
            brand, function = _parse(qname)
            excluded = pattern is not None and (brand is None or _skipped(qname))
            by_queue.append({
                "queue_id": qid,
                "queue_name": qname,
                "brand": brand,
                "function": function,
                "media_type": media,
                "excluded_from_brand_rollup": excluded,
                **_measures(acc, p),
            })
            _merge_into(totals_acc, acc)
            if p:
                _merge_into(totals_prior, p)
            if brand is not None and not excluded:
                for bkey in ((brand, None), (brand, media)):
                    _merge_into(brand_acc.setdefault(bkey, _empty_acc()), acc)
                    if p:
                        _merge_into(brand_prior.setdefault(bkey, _empty_acc()), p)

        # Prior-only rows (a queue that had traffic last period, none now)
        # are ignored on purpose: nothing current to compare against.

        by_queue.sort(key=lambda r: (
            r["mean_sentiment"] if r["mean_sentiment"] is not None else float("inf"),
            r["queue_name"] or "",
            r["media_type"] or "",
        ))
        unattributed.sort(key=lambda r: r["media_type"] or "")

        by_brand: list[dict[str, Any]] | None
        if brand_reason is not None:
            by_brand = None
        else:
            by_brand = [
                {"brand": b, "media_type": m,
                 **_measures(acc, brand_prior.get((b, m)))}
                for (b, m), acc in brand_acc.items()
            ]
            by_brand.sort(key=lambda r: (r["brand"], r["media_type"] is not None, r["media_type"] or ""))

        totals = _measures(totals_acc, totals_prior if include_trend else None)
        totals["unattributed_conversations"] = unattributed_n

        out: dict[str, Any] = {
            "interval": resolved_interval,
            "as_of_utc": _now_utc().isoformat().replace("+00:00", "Z"),
            "prior_interval": prior_interval_str,
            "mode": mode,
            "filters": {
                "queue_ids": list(queue_ids) if queue_ids else None,
                "media_types": list(media_types) if media_types else None,
                "group_by_media": group_by_media,
            },
            "scale": _SCALE_NOTE,
            "totals": totals,
            "by_brand": by_brand,
            "by_brand_unavailable_reason": brand_reason,
            "by_queue": by_queue,
            "unattributed": unattributed,
            "notes": list(_NOTES),
        }
        if mode == "full":
            out["raw"] = {"current": holder["current"], "prior": holder["prior"]}
        return out
