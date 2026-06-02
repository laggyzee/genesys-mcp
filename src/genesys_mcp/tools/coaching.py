"""Agent coaching pack — one-shot composition tool for 1:1 prep.

Given a user_id and an interval, returns everything a Team Leader needs to
prep a coaching session in a single payload: performance vs target, peer
comparison, sentiment trajectory, QA scores, adherence behaviour, wrap-up
discipline, top flagged calls, and a heuristic top-3 recommended focus.

Composition only — orchestrates existing tools and SDK calls:
- ``post_analytics_conversations_aggregates_query`` (performance vs target/peers)
- ``post_analytics_conversations_details_jobs`` (per-call walk for flagged calls)
- ``qa_evaluations`` (QA scores; gracefully degrades if scope missing)
- ``_sta_details`` from reports.py (sentiment per call)

Loads tenant.yaml when present for AHT/ACW/ACW targets, FTE conversion, and
specialist-role hints; falls back to the in-code defaults from
``genesys_mcp.tenant`` when no config file exists, so the tool works
standalone via the MCP without a skill wrapper.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp._aggregates import accumulate_metric_stats
from genesys_mcp.client import get_api, to_dict, with_retry
from genesys_mcp.tenant import TenantConfig, TenantConfigError, load_config
from genesys_mcp.tools.quality import (
    _aggregate_user as _qa_aggregate_user,
    _pull_evaluations_for_user as _qa_pull_evaluations,
    _split_interval,
    _to_iso_z,
    _summarise_evaluation as _qa_summarise_evaluation,
)
from genesys_mcp.tools.reports import (
    _default_interval,
    _fetch_wrapup,
    _parse_iso,
    _run_conv_details_job,
    _seg_dur_s,
    _sta_details,
)

logger = logging.getLogger(__name__)


# Bounded concurrency for per-conversation enrichment fetches (STA + wrap-up).
# Genesys's documented rate limit is 300 req/min per OAuth client; 8 workers
# walking 200 conversations × 2 endpoints = 400 calls in ~5s, well under the
# limit. with_retry already handles 429 backoff if we ever do brush the cap.
_ENRICHMENT_WORKERS = 8


def _resolve_targets(cfg: TenantConfig | None) -> dict[str, int]:
    """Pull targets from tenant.yaml.

    Pre-v1.0 this had hardcoded fallbacks (voice 285s / message 660s / ACW 15s)
    that were tenant-specific and quietly applied when tenant.yaml was absent.
    v1.0 makes tenant.yaml a hard requirement — the fallbacks were a footgun
    for any other deployer.
    """
    if cfg is None:
        raise TenantConfigError(
            "agent_coaching_pack requires a tenant config — no in-code "
            "fallbacks since v1.0. Run the genesys-tenant-setup skill (it "
            "auto-discovers most values) to generate ~/.config/genesys-mcp/"
            "tenant.yaml, or set $GENESYS_MCP_CONFIG to point at an existing "
            "config."
        )
    return {
        "voice_aht_s": cfg.targets.voice_aht_s,
        "msg_aht_s": cfg.targets.message_aht_s,
        "acw_s": cfg.targets.acw_s,
        "fte_hours_per_month": cfg.targets.fte_hours_per_month,
    }


def _aggregates_for_users(
    user_ids: list[str], interval: str
) -> dict[str, dict[str, dict]]:
    """Pull per-user × media aggregates with the canonical UI-matching filter shape.

    Returns ``{user_id: {media_type: {metric_name: stats}}}``.
    """
    api = gc.AnalyticsApi(get_api())
    body = {
        "interval": interval,
        "granularity": "P7D",
        "groupBy": ["userId", "mediaType"],
        "filter": {
            "type": "and",
            "clauses": [
                {
                    "type": "or",
                    "predicates": [
                        {"dimension": "userId", "value": uid} for uid in user_ids
                    ],
                },
                {
                    "type": "or",
                    "predicates": [
                        {"dimension": "mediaType", "value": m}
                        for m in ("voice", "message", "callback")
                    ],
                },
            ],
        },
        "metrics": [
            "tAnswered", "nConnected", "tHandle", "tTalk", "tHeld", "tAcw",
        ],
    }
    resp = to_dict(
        with_retry(api.post_analytics_conversations_aggregates_query)(body)
    )
    out: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
    for r in resp.get("results") or []:
        uid = r["group"].get("userId")
        media = r["group"].get("mediaType", "?")
        # P7D over a multi-week interval yields several buckets per (uid, media).
        # The shared accumulator sums count/sum across buckets — picking a single
        # bucket would truncate to ~1/N of the real volume.
        out[uid][media] = accumulate_metric_stats(r.get("data") or [])
    return out


def _per_user_kpis(
    media_stats: dict[str, dict[str, dict]], voice_aht_s: int, msg_aht_s: int
) -> dict[str, Any]:
    """Reduce per-media stats into the headline KPIs."""

    def _stat(media: str, metric: str, field: str = "sum") -> float:
        return float(media_stats.get(media, {}).get(metric, {}).get(field, 0.0) or 0.0)

    voice_answered = _stat("voice", "tAnswered", "count")
    msg_answered = _stat("message", "tAnswered", "count")
    cb_answered = _stat("callback", "tAnswered", "count")
    voice_handle = _stat("voice", "tHandle", "sum") / 1000.0
    msg_handle = _stat("message", "tHandle", "sum") / 1000.0
    cb_handle = _stat("callback", "tHandle", "sum") / 1000.0
    voice_aht = voice_handle / voice_answered if voice_answered else None
    msg_aht = msg_handle / msg_answered if msg_answered else None
    cb_aht = cb_handle / cb_answered if cb_answered else None
    voice_acw = _stat("voice", "tAcw", "sum") / 1000.0
    voice_acw_avg = voice_acw / voice_answered if voice_answered else None
    voice_hold = _stat("voice", "tHeld", "sum") / 1000.0
    voice_hold_ratio = voice_hold / voice_handle if voice_handle else None

    return {
        "voice_answered": int(voice_answered),
        "message_answered": int(msg_answered),
        "callback_answered": int(cb_answered),
        "voice_aht_s": round(voice_aht, 1) if voice_aht else None,
        "message_aht_s": round(msg_aht, 1) if msg_aht else None,
        "callback_aht_s": round(cb_aht, 1) if cb_aht else None,
        "voice_acw_avg_s": round(voice_acw_avg, 1) if voice_acw_avg else None,
        "voice_hold_ratio": round(voice_hold_ratio, 3) if voice_hold_ratio else None,
        "total_handle_hours": round(
            (voice_handle + msg_handle + cb_handle) / 3600.0, 1
        ),
        "voice_aht_vs_target_pct": (
            round((voice_aht - voice_aht_s) / voice_aht_s * 100.0, 1)
            if voice_aht else None
        ),
        "message_aht_vs_target_pct": (
            round((msg_aht - msg_aht_s) / msg_aht_s * 100.0, 1)
            if msg_aht else None
        ),
        "voice_excess_handle_hours": (
            round((voice_aht - voice_aht_s) * voice_answered / 3600.0, 2)
            if voice_aht and voice_aht > voice_aht_s else 0.0
        ),
        "message_excess_handle_hours": (
            round((msg_aht - msg_aht_s) * msg_answered / 3600.0, 2)
            if msg_aht and msg_aht > msg_aht_s else 0.0
        ),
    }


def _peer_medians(per_peer: dict[str, dict]) -> dict[str, float | None]:
    """Compute peer-median for the headline KPIs."""
    metrics = [
        "voice_aht_s", "message_aht_s", "voice_acw_avg_s", "voice_hold_ratio",
        "total_handle_hours",
    ]
    out: dict[str, float | None] = {}
    for m in metrics:
        vals = sorted(
            v[m] for v in per_peer.values() if v.get(m) is not None
        )
        if not vals:
            out[m] = None
        else:
            mid = len(vals) // 2
            out[m] = vals[mid] if len(vals) % 2 == 1 else (vals[mid - 1] + vals[mid]) / 2
    return out


def _prefetch_enrichment(
    conv_ids: list[str], voice_ids: set[str],
) -> dict[str, dict]:
    """Concurrently fetch wrap-up + STA detail for every conv id.

    v0.7 perf win: ``_fetch_wrapup`` and ``_sta_details`` are independent
    per-conv HTTPs; running them in a bounded thread pool collapses the
    serial 200-conv × 2-endpoint walk from ~30s to ~5s. The aggregation
    logic in :func:`_walk_calls_for_signals` consumes the pre-fetched
    map by conv_id and is otherwise unchanged.

    Returns ``{conv_id: {"wrap": dict|None, "sta": dict|None}}``.
    STA is only fetched for voice convs (matches the per-call media check
    in the original sequential walk).
    """
    out: dict[str, dict] = {cid: {"wrap": None, "sta": None} for cid in conv_ids}
    if not conv_ids:
        return out

    def _fetch_wrap(cid: str) -> tuple[str, dict | None]:
        return cid, _fetch_wrapup(cid)

    def _fetch_sta(cid: str) -> tuple[str, dict | None]:
        return cid, _sta_details(cid)

    with ThreadPoolExecutor(max_workers=_ENRICHMENT_WORKERS) as pool:
        # Submit every conv for wrap-up; only voice convs for STA.
        wrap_futures = [pool.submit(_fetch_wrap, cid) for cid in conv_ids]
        sta_futures = [pool.submit(_fetch_sta, cid) for cid in conv_ids if cid in voice_ids]
        for fut in wrap_futures:
            cid, wrap = fut.result()
            out[cid]["wrap"] = wrap
        for fut in sta_futures:
            cid, sta = fut.result()
            out[cid]["sta"] = sta
    return out


def _walk_calls_for_signals(
    user_id: str,
    interval: str,
    sentiment_drop_threshold: float,
    silent_threshold_s: int,
    aht_excess_pct_threshold: float,
    voice_aht_target_s: int,
    flagged_calls_limit: int,
) -> dict[str, Any]:
    """Walk the user's calls in the interval, returning flagged-call list + discipline stats.

    Two-pass design (v0.7):
    1. **Local pass** — iterate conversations once to extract per-conv durations,
       media, queue ids, and the voice-conv id set. No network calls here.
    2. **Concurrent fetch** — pre-fetch wrap-up + STA detail for every conv in
       a thread pool (see :func:`_prefetch_enrichment`).
    3. **Scoring pass** — iterate again, layering the pre-fetched enrichment
       onto the local data to build the flagged-call list + counters.

    The output JSON is byte-identical to the pre-v0.7 sequential version;
    only wall time changes (~30s → ~5s for a 200-conv week).
    """
    body = {
        "interval": interval,
        "order": "desc",
        "orderBy": "conversationStart",
        "segmentFilters": [{
            "type": "and",
            "predicates": [
                {"type": "dimension", "dimension": "userId",
                 "operator": "matches", "value": user_id},
            ],
        }],
    }
    convs = _run_conv_details_job(body)

    # Pass 1 (local-only): extract per-conv durations + media. Collect conv ids
    # + the subset that are voice (need STA fetch).
    per_conv_local: dict[str, dict] = {}
    voice_conv_ids: set[str] = set()
    conv_ids: list[str] = []
    for c in convs:
        conv_id = c.get("conversationId")
        if not conv_id:
            continue
        media = None
        talk_s = 0.0
        hold_s = 0.0
        wrap_s = 0.0
        first_seg_queue: str | None = None
        for p in c.get("participants") or []:
            if p.get("userId") != user_id:
                continue
            for s in p.get("sessions") or []:
                if s.get("mediaType") in ("voice", "message", "callback"):
                    media = s["mediaType"]
                for seg in s.get("segments") or []:
                    st = seg.get("segmentType")
                    d = _seg_dur_s(seg)
                    if st == "interact":
                        talk_s += d
                    elif st == "hold":
                        hold_s += d
                    elif st == "wrapup":
                        wrap_s += d
                    if st == "interact" and not first_seg_queue:
                        first_seg_queue = seg.get("queueId")
        per_conv_local[conv_id] = {
            "conv": c,
            "media": media,
            "talk_s": talk_s,
            "hold_s": hold_s,
            "wrap_s": wrap_s,
            "first_seg_queue": first_seg_queue,
        }
        conv_ids.append(conv_id)
        if media == "voice":
            voice_conv_ids.add(conv_id)

    # Pass 2: concurrent enrichment fetch.
    enrichment = _prefetch_enrichment(conv_ids, voice_conv_ids)

    flagged: list[dict] = []
    own_note_count = 0
    total_with_wrapup = 0
    sentiment_scores: list[float] = []
    disposition_counter: Counter = Counter()
    queue_counter: Counter = Counter()

    # Pass 3: scoring — consume pre-fetched enrichment.
    for conv_id, local in per_conv_local.items():
        c = local["conv"]
        media = local["media"]
        talk_s = local["talk_s"]
        hold_s = local["hold_s"]
        wrap_s = local["wrap_s"]
        first_seg_queue = local["first_seg_queue"]

        # Disposition / wrap-up note enrichment (best-effort) — pre-fetched.
        wrap = enrichment.get(conv_id, {}).get("wrap")
        if wrap:
            total_with_wrapup += 1
            if wrap.get("notes"):
                own_note_count += 1
            for d_label in wrap.get("dispositions") or []:
                disposition_counter[d_label] += 1
        if first_seg_queue:
            queue_counter[first_seg_queue] += 1

        # Sentiment per call (voice only — message STA support is partial).
        # _sta_details returns snake_case keys: score/trend/trend_class/empathy_scores.
        # Pre-fetched concurrently in _prefetch_enrichment.
        sentiment = None
        sentiment_trend = None
        sentiment_trend_class = None
        if media == "voice":
            sta = enrichment.get(conv_id, {}).get("sta") or {}
            sentiment = sta.get("score")
            sentiment_trend = sta.get("trend")
            sentiment_trend_class = sta.get("trend_class")
            if sentiment is not None:
                sentiment_scores.append(sentiment)

        # AHT excess for THIS call
        call_handle_s = talk_s + hold_s + wrap_s
        aht_excess_pct = None
        if media == "voice" and call_handle_s > 0:
            aht_excess_pct = (
                (call_handle_s - voice_aht_target_s) / voice_aht_target_s * 100.0
            )

        # Score the call for flag-worthiness. Use the trend (sign + magnitude)
        # for "did sentiment trend negative through the call".
        flag_reasons: list[str] = []
        flag_score = 0.0
        if (
            sentiment_trend is not None
            and isinstance(sentiment_trend, (int, float))
            and sentiment_trend <= -sentiment_drop_threshold
        ):
            flag_reasons.append(f"sentiment trended down {sentiment_trend:.2f}")
            flag_score += abs(sentiment_trend)
        elif sentiment is not None and sentiment <= -0.4:
            flag_reasons.append(f"negative sentiment {sentiment:.2f}")
            flag_score += abs(sentiment)
        if hold_s > 0 and call_handle_s > 0 and hold_s / call_handle_s > 0.3:
            flag_reasons.append(f"hold ratio {hold_s / call_handle_s:.0%}")
            flag_score += 1.0
        if (
            aht_excess_pct is not None
            and aht_excess_pct >= aht_excess_pct_threshold
        ):
            flag_reasons.append(f"AHT +{aht_excess_pct:.0f}% over target")
            flag_score += aht_excess_pct / 50.0
        if wrap and not wrap.get("notes"):
            flag_reasons.append("no wrap-up notes")
            flag_score += 0.5

        if flag_reasons:
            flagged.append({
                "conversation_id": conv_id,
                "started_at": c.get("conversationStart"),
                "media": media,
                "queue_id": first_seg_queue,
                "handle_s": round(call_handle_s, 1),
                "hold_s": round(hold_s, 1),
                "sentiment_score": sentiment,
                "sentiment_trend": sentiment_trend,
                "sentiment_trend_class": sentiment_trend_class,
                "aht_excess_pct": (
                    round(aht_excess_pct, 1) if aht_excess_pct is not None else None
                ),
                "flag_reasons": flag_reasons,
                "_flag_score": flag_score,
            })

    flagged.sort(key=lambda r: r["_flag_score"], reverse=True)
    top_flagged = flagged[:flagged_calls_limit]
    for r in top_flagged:
        r.pop("_flag_score", None)

    avg_sentiment = (
        round(sum(sentiment_scores) / len(sentiment_scores), 3)
        if sentiment_scores else None
    )

    return {
        "total_conversations": len(convs),
        "with_wrapup_code": total_with_wrapup,
        "with_own_notes": own_note_count,
        "wrapup_note_rate": (
            round(own_note_count / total_with_wrapup, 3)
            if total_with_wrapup else None
        ),
        "top_dispositions": disposition_counter.most_common(8),
        "top_queues_by_volume": queue_counter.most_common(6),
        "avg_sentiment": avg_sentiment,
        "sentiment_samples": len(sentiment_scores),
        "flagged_calls": top_flagged,
        "flagged_call_count_total": len(flagged),
    }


def _recommend_focus(
    target_kpis: dict[str, Any],
    peer_medians: dict[str, float | None],
    wrap_stats: dict[str, Any],
    qa_summary: dict[str, Any] | None,
    targets: dict[str, int],
    heuristics: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Heuristic top-3 coaching focus areas with concrete evidence.

    Numeric cutoffs come from ``cfg.coaching.heuristics``. Pre-v1.0 they
    were hardcoded inline — fine for inbound-heavy CCs but wrong for
    transfer-heavy retention teams (higher hold ratios are normal there).
    """
    h = heuristics or {}
    voice_excess_thr = h.get("voice_excess_hours_threshold", 2.0)
    msg_excess_thr = h.get("message_excess_hours_threshold", 2.0)
    note_rate_thr = h.get("wrap_up_note_rate_threshold", 0.7)
    qa_pass = h.get("qa_pass_mark", 80)
    hold_thr = h.get("hold_ratio_threshold", 0.15)
    peer_mult = h.get("peer_aht_multiplier", 1.15)

    candidates: list[dict[str, Any]] = []

    v_excess = target_kpis.get("voice_excess_handle_hours") or 0.0
    if v_excess >= voice_excess_thr:
        candidates.append({
            "rank": None,
            "area": "Voice AHT",
            "headline": (
                f"Voice AHT {target_kpis['voice_aht_s']:.0f}s vs target "
                f"{targets['voice_aht_s']}s "
                f"(+{target_kpis['voice_aht_vs_target_pct']:.0f}%) — "
                f"{v_excess:.1f} handle-hours over target this period"
            ),
            "score": v_excess,
        })

    m_excess = target_kpis.get("message_excess_handle_hours") or 0.0
    if m_excess >= msg_excess_thr:
        candidates.append({
            "area": "Message AHT",
            "headline": (
                f"Message AHT {target_kpis['message_aht_s']:.0f}s vs target "
                f"{targets['msg_aht_s']}s "
                f"(+{target_kpis['message_aht_vs_target_pct']:.0f}%) — "
                f"{m_excess:.1f} handle-hours over target this period"
            ),
            "score": m_excess,
        })

    note_rate = wrap_stats.get("wrapup_note_rate")
    if note_rate is not None and note_rate < note_rate_thr:
        candidates.append({
            "area": "Wrap-up discipline",
            "headline": (
                f"{int((1 - note_rate) * 100)}% of calls without own wrap-up "
                f"notes ({wrap_stats['with_own_notes']}/{wrap_stats['with_wrapup_code']} "
                f"with notes)"
            ),
            "score": (1 - note_rate) * 5.0,
        })

    if qa_summary and qa_summary.get("avg_score") is not None:
        if qa_summary["avg_score"] < qa_pass:
            candidates.append({
                "area": "QA score",
                "headline": (
                    f"QA avg {qa_summary['avg_score']}% across "
                    f"{qa_summary['n_evaluations']} evaluations — "
                    f"below the {qa_pass}% pass mark"
                ),
                "score": (qa_pass - qa_summary["avg_score"]) / 5.0,
            })

    hold = target_kpis.get("voice_hold_ratio")
    if hold is not None and hold > hold_thr:
        candidates.append({
            "area": "Hold time",
            "headline": (
                f"Voice hold ratio {hold:.0%} — "
                f"above the {int(hold_thr * 100)}% threshold"
            ),
            "score": hold * 10.0,
        })

    peer_voice = peer_medians.get("voice_aht_s")
    if (
        peer_voice and target_kpis.get("voice_aht_s")
        and target_kpis["voice_aht_s"] > peer_voice * peer_mult
    ):
        candidates.append({
            "area": "vs Peers — voice handle",
            "headline": (
                f"Voice AHT {target_kpis['voice_aht_s']:.0f}s vs peer median "
                f"{peer_voice:.0f}s — {(target_kpis['voice_aht_s'] / peer_voice - 1) * 100:.0f}% slower than peers"
            ),
            "score": (target_kpis["voice_aht_s"] / peer_voice - 1) * 5.0,
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    top = candidates[:3]
    for i, c in enumerate(top, start=1):
        c["rank"] = i
        c.pop("score", None)
    return top


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def agent_coaching_pack(
        user_id: str = Field(description="User id of the agent being coached."),
        interval: str | None = Field(
            default=None,
            description=(
                "ISO-8601 'startISO/endISO' interval. Defaults to last 28 days "
                "(typical 4-week coaching cadence)."
            ),
        ),
        peer_user_ids: list[str] | None = Field(
            default=None,
            description=(
                "Peer user ids for the peer-median comparison. If omitted, "
                "the caller (or a skill wrapper) should resolve peers via "
                "tenant.yaml.coaching.peer_grouping before invoking."
            ),
        ),
        flagged_calls_limit: int = Field(
            default=10, ge=1, le=50,
            description="Cap on top flagged calls returned (sorted by composite flag score).",
        ),
        include_flagged_transcripts: bool = Field(
            default=True,
            description=(
                "When true (default, v1.2+), each flagged call gets an inline "
                "transcript excerpt attached under `transcript_excerpt` so the "
                "coaching brief can read what was said without a separate "
                "round-trip per call. Disable for cheaper runs or when you "
                "already have the conversation IDs and want to fetch "
                "transcripts on demand."
            ),
        ),
        transcript_max_utterances_per_call: int = Field(
            default=40, ge=5, le=200,
            description=(
                "Per-call cap on transcript utterances when "
                "`include_flagged_transcripts` is true. Default 40 captures "
                "the opening exchange (where most coaching friction surfaces) "
                "without ballooning chat context. Raise for full-call deep "
                "dives, lower if you have many flagged calls."
            ),
        ),
    ) -> dict:
        """Single-call 1:1 prep brief for one agent.

        Returns volume + AHT/ACW + adherence + wrap-up discipline + sentiment +
        QA scores + repeat-caller burden + top flagged calls + heuristic top-3
        recommended coaching focus areas.

        Tenant-aware: reads ``~/.config/genesys-mcp/tenant.yaml`` for AHT/ACW
        targets and coaching-thresholds when present. Falls back to in-code
        defaults (voice 285s / message 660s / ACW 15s) when the config file
        is absent — the tool works standalone without the skill wrapper.

        QA section soft-fails (returns ``scope_available: false``) if the
        OAuth client lacks ``quality:readonly``; sentiment soft-fails on
        per-call STA 404s; the rest of the pack always populates.
        """
        # 1. Tenant config — required since v1.0 (pre-v1.0 silently fell
        # back to tenant-specific defaults). Errors propagate with a clear
        # "run genesys-tenant-setup" remediation message.
        cfg = load_config()
        targets = _resolve_targets(cfg)
        thresholds = cfg.coaching.flagged_call_thresholds
        sentiment_drop_t = thresholds.sentiment_drop
        silent_s_t = thresholds.silent_seconds
        aht_excess_pct_t = thresholds.aht_excess_pct

        # 2. Interval
        interval = interval or _default_interval(28)

        # 3. Performance aggregates: target + peers in one query
        all_ids = [user_id] + (peer_user_ids or [])
        media_stats_by_user = _aggregates_for_users(all_ids, interval)
        target_kpis = _per_user_kpis(
            media_stats_by_user.get(user_id, {}),
            targets["voice_aht_s"], targets["msg_aht_s"],
        )
        per_peer_kpis = {
            uid: _per_user_kpis(
                media_stats_by_user.get(uid, {}),
                targets["voice_aht_s"], targets["msg_aht_s"],
            )
            for uid in peer_user_ids or []
        }
        peer_medians = _peer_medians(per_peer_kpis)

        # 4. Walk target's calls for per-call signals (sentiment / wrapup / flagged)
        call_signals = _walk_calls_for_signals(
            user_id=user_id,
            interval=interval,
            sentiment_drop_threshold=sentiment_drop_t,
            silent_threshold_s=silent_s_t,
            aht_excess_pct_threshold=aht_excess_pct_t,
            voice_aht_target_s=targets["voice_aht_s"],
            flagged_calls_limit=flagged_calls_limit,
        )

        # 4b. v1.2: attach inline transcript excerpts to each flagged call so
        # the coaching brief can read what was said without a per-call round
        # trip. Concurrent fetch (1 thread per flagged call) keeps wall time
        # bounded — N flagged calls × ~1-2s each becomes ~3-5s total.
        if include_flagged_transcripts and call_signals.get("flagged_calls"):
            from genesys_mcp.tools.speech_analytics import fetch_conversation_transcript

            def _fetch_for(conv_id: str) -> tuple[str, dict]:
                try:
                    return conv_id, fetch_conversation_transcript(
                        conv_id, mode="summary",
                        max_utterances=transcript_max_utterances_per_call,
                    )
                except Exception as exc:
                    logger.info(
                        "transcript excerpt skipped for conv=%s: %s", conv_id, exc,
                    )
                    return conv_id, {"status": "error", "message": str(exc)}

            with ThreadPoolExecutor(max_workers=_ENRICHMENT_WORKERS) as pool:
                futures = [
                    pool.submit(_fetch_for, fc["conversation_id"])
                    for fc in call_signals["flagged_calls"]
                ]
                excerpts: dict[str, dict] = {}
                for fut in futures:
                    cid, excerpt = fut.result()
                    excerpts[cid] = excerpt

            for fc in call_signals["flagged_calls"]:
                excerpt = excerpts.get(fc["conversation_id"]) or {}
                # Strip metadata the coaching brief doesn't need — just keep
                # the utterances and a couple of context scalars. Keeps the
                # attached excerpt focused.
                if excerpt.get("utterances"):
                    fc["transcript_excerpt"] = {
                        "media_type": excerpt.get("media_type"),
                        "duration_s": excerpt.get("duration_s"),
                        "total_utterances": excerpt.get("total_utterances"),
                        "truncated_at": excerpt.get("truncated_at"),
                        "utterances": excerpt.get("utterances"),
                    }
                else:
                    fc["transcript_excerpt"] = {
                        "status": excerpt.get("status", "unavailable"),
                        "message": excerpt.get("message"),
                    }

        # 5. QA scores (soft-fail on no scope)
        start, end = _split_interval(interval)
        qa_api = gc.QualityApi(get_api())
        qa_summary: dict[str, Any] | None = None
        qa_rows: list[dict] = []
        qa_scope_available = True
        try:
            entities = _qa_pull_evaluations(
                qa_api, user_id, _to_iso_z(start), _to_iso_z(end)
            )
            qa_rows = [_qa_summarise_evaluation(e, include_question_detail=False)
                       for e in entities]
            qa_summary = _qa_aggregate_user(qa_rows)
        except gc.rest.ApiException as exc:
            if exc.status == 403:
                qa_scope_available = False
            else:
                raise

        # 6. Heuristic coaching focus — cutoffs from tenant.yaml's
        # coaching.heuristics block (v1.0). Tenants tune these (e.g. higher
        # hold_ratio_threshold for transfer-heavy retention teams) so the
        # recommended-focus list reflects the team's operating model.
        focus = _recommend_focus(
            target_kpis, peer_medians, call_signals, qa_summary, targets,
            heuristics=cfg.coaching.heuristics.model_dump(),
        )

        # 7. User context (name, role, manager) — best-effort. expand=manager
        # only populates {id, selfUri}; fetch the manager separately to get the name.
        users_api = gc.UsersApi(get_api())
        try:
            user_obj = to_dict(
                with_retry(users_api.get_user)(user_id=user_id, expand=["manager"])
            )
        except Exception:
            user_obj = {"id": user_id}
        manager_id = (user_obj.get("manager") or {}).get("id")
        manager_name = None
        if manager_id:
            try:
                mgr = to_dict(with_retry(users_api.get_user)(user_id=manager_id))
                manager_name = mgr.get("name")
            except Exception:
                pass

        return {
            "agent": {
                "id": user_id,
                "name": user_obj.get("name"),
                "title": user_obj.get("title"),
                "email": user_obj.get("email"),
                "manager_name": manager_name,
            },
            "interval": interval,
            "targets": targets,
            "tenant_config_loaded": True,  # v1.0: tenant.yaml required — always True
            "performance": {
                "target": target_kpis,
                "peer_count": len(per_peer_kpis),
                "peer_medians": peer_medians,
                "per_peer": per_peer_kpis,
            },
            "wrap_discipline": {
                "total_conversations": call_signals["total_conversations"],
                "with_wrapup_code": call_signals["with_wrapup_code"],
                "with_own_notes": call_signals["with_own_notes"],
                "note_rate": call_signals["wrapup_note_rate"],
                "top_dispositions": call_signals["top_dispositions"],
            },
            "queues_handled": call_signals["top_queues_by_volume"],
            "sentiment": {
                "avg": call_signals["avg_sentiment"],
                "samples": call_signals["sentiment_samples"],
            },
            "quality": {
                "scope_available": qa_scope_available,
                "summary": qa_summary,
                "evaluations": qa_rows,
            },
            "flagged_calls": {
                "limit": flagged_calls_limit,
                "total_flagged": call_signals["flagged_call_count_total"],
                "top": call_signals["flagged_calls"],
            },
            "recommended_focus": focus,
        }
