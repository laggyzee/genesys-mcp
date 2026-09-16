"""Pin v1.23 ``sentiment_summary``.

The gap this tool closes: pre-v1.23 the only sentiment surface was the
per-conversation ``get_conversation_sentiment`` — fine for coaching, useless
for "what was Brand X's sentiment last week?". This tool wraps the
transcript-aggregates endpoint (``POST /api/v2/analytics/transcripts/
aggregates/query``) which returns per-queue × media sentiment in one call.

These tests pin:

- Request body shape (groupBy queueId[+mediaType], the three metrics, the
  canonical outer-and-of-or filter, and *no* filter key when unfiltered —
  the endpoint accepts an org-wide query)
- Prior-interval computation and parallel current + prior firing
- Per-queue rows: mean = sum / count, phrase counts from oCustomerSentiment
- Brand rollup via tenant.yaml ``queues.name_pattern`` + ``skip_substrings``
- Graceful degradation when tenant.yaml is missing (by_brand = None + reason)
- Unattributed (no queueId) rows are kept separate, never folded into brands
- v1.5 envelope contract (top-level interval + as_of_utc)
- 403 → canonical soft-fail envelope naming the scope
- mode='full' carries the raw responses; 'summary' does not
"""
from __future__ import annotations

import asyncio
import json

import pytest
from PureCloudPlatformClientV2.rest import ApiException

_INTERVAL = "2026-09-04T14:00:00.000Z/2026-09-11T14:00:00.000Z"
_PRIOR = "2026-08-28T14:00:00Z/2026-09-04T14:00:00Z"

Q_COLES_GEN = "q-coles-general"
Q_COLES_RET = "q-coles-retention"
Q_ONEPASS_GEN = "q-onepass-general"
Q_COLES_HOLD = "q-coles-holding"

_QUEUE_NAMES = {
    Q_COLES_GEN: "Coles - General",
    Q_COLES_RET: "Coles - Retention",
    Q_ONEPASS_GEN: "OnePass - General",
    Q_COLES_HOLD: "Coles - Holding",
}


def _group(queue_id, media, *, n, sc, ssum, smin=-100, smax=100,
           pos=None, neg=None, csum=None):
    metrics = [
        {"metric": "nSpeechTextAnalyzedConversations", "stats": {"count": n}},
        {"metric": "oSentimentScore",
         "stats": {"count": sc, "sum": ssum, "min": smin, "max": smax}},
    ]
    if pos is not None or neg is not None:
        cust = {"count": (pos or 0) + (neg or 0), "sum": csum or 0,
                "max": 100, "min": -100}
        if pos:
            cust["countPositive"] = pos
        if neg:
            cust["countNegative"] = neg
        metrics.append({"metric": "oCustomerSentiment", "stats": cust})
    group = {"mediaType": media} if media else {}
    if queue_id:
        group["queueId"] = queue_id
    return {"group": group, "data": [{"interval": _INTERVAL, "metrics": metrics}]}


def _current_resp() -> dict:
    return {"results": [
        # Real-tenant shape: rows with no queueId are pre-queue / bot traffic.
        _group(None, "message", n=1000, sc=1000, ssum=-3600, pos=4, neg=521, csum=-51700),
        _group(Q_COLES_GEN, "voice", n=293, sc=365, ssum=11836, pos=452, neg=311, csum=14100),
        _group(Q_COLES_GEN, "message", n=308, sc=308, ssum=2374, pos=151, neg=202, csum=-5100),
        _group(Q_COLES_RET, "message", n=148, sc=148, ssum=-3782, pos=66, neg=333, csum=-26700),
        _group(Q_ONEPASS_GEN, "voice", n=218, sc=289, ssum=8310, pos=337, neg=367, csum=-3000),
        # Skip-listed queue: must be in by_queue, must NOT roll into a brand.
        _group(Q_COLES_HOLD, "voice", n=10, sc=10, ssum=-1000),
    ]}


def _prior_resp() -> dict:
    return {"results": [
        _group(Q_COLES_GEN, "voice", n=280, sc=350, ssum=7000),
        _group(Q_COLES_GEN, "message", n=300, sc=300, ssum=3000),
        _group(Q_ONEPASS_GEN, "voice", n=200, sc=250, ssum=5000),
    ]}


def _make_fake(*, current_resp, prior_resp=None, raise_status=None):
    captured: dict[str, list] = {"bodies": []}

    class FakeAnalyticsApi:
        def __init__(self, *a, **k):
            pass

        def post_analytics_transcripts_aggregates_query(self, body):
            captured["bodies"].append(body)
            if raise_status is not None:
                raise ApiException(status=raise_status, reason="Forbidden")
            if body["interval"] == _INTERVAL:
                return current_resp
            return prior_resp if prior_resp is not None else {"results": []}

    return FakeAnalyticsApi, captured


class _FakeResolver:
    def queue_names(self, ids):
        return {i: _QUEUE_NAMES.get(i) for i in ids}


def _fake_to_dict(obj):
    return obj


def _call_tool(args, monkeypatch, *, current_resp=None, prior_resp=None,
               raise_status=None, tenant_cfg="default"):
    import PureCloudPlatformClientV2 as gc
    from genesys_mcp import client as gen_client
    from genesys_mcp.tenant import TenantConfigError
    from genesys_mcp.tools import sentiment
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(sentiment, "to_dict", _fake_to_dict)
    FakeAnalyticsApi, captured = _make_fake(
        current_resp=current_resp if current_resp is not None else _current_resp(),
        prior_resp=prior_resp,
        raise_status=raise_status,
    )
    monkeypatch.setattr(sentiment.gc, "AnalyticsApi", FakeAnalyticsApi)
    monkeypatch.setattr(sentiment, "resolver", _FakeResolver())
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())

    if tenant_cfg == "default":
        class _Q:
            name_pattern = "{brand} - {function}"
            name_pattern_match_required = True
            skip_substrings = ["Holding", "Internal", "ZZZ_"]

        class _Cfg:
            queues = _Q()

        monkeypatch.setattr(sentiment, "load_config", lambda: _Cfg())
    elif tenant_cfg == "missing":
        def _boom():
            raise TenantConfigError("no tenant.yaml")
        monkeypatch.setattr(sentiment, "load_config", _boom)

    app = FastMCP(name="t")
    sentiment.register(app)
    result = asyncio.run(app.call_tool("sentiment_summary", args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text), captured


# ─────────────────────────── request shape ───────────────────────────

def test_body_unfiltered_has_no_filter_key_and_groups_by_queue_and_media(monkeypatch):
    _, cap = _call_tool({"interval": _INTERVAL}, monkeypatch, prior_resp=_prior_resp())
    body = cap["bodies"][0]
    assert body["interval"] == _INTERVAL
    assert body["groupBy"] == ["queueId", "mediaType"]
    assert set(body["metrics"]) == {
        "nSpeechTextAnalyzedConversations", "oSentimentScore", "oCustomerSentiment",
    }
    assert "filter" not in body
    assert "granularity" not in body  # one bucket per interval by design


def test_body_filters_use_canonical_and_of_or_shape(monkeypatch):
    _, cap = _call_tool(
        {"interval": _INTERVAL, "queue_ids": [Q_COLES_GEN, Q_COLES_RET],
         "media_types": ["voice"], "include_trend": False},
        monkeypatch,
    )
    body = cap["bodies"][0]
    assert body["filter"]["type"] == "and"
    clauses = body["filter"]["clauses"]
    assert clauses[0] == {"type": "or", "predicates": [
        {"dimension": "queueId", "value": Q_COLES_GEN},
        {"dimension": "queueId", "value": Q_COLES_RET},
    ]}
    assert clauses[1] == {"type": "or", "predicates": [
        {"dimension": "mediaType", "value": "voice"},
    ]}


def test_group_by_media_false_groups_by_queue_only(monkeypatch):
    _, cap = _call_tool(
        {"interval": _INTERVAL, "group_by_media": False, "include_trend": False},
        monkeypatch,
    )
    assert cap["bodies"][0]["groupBy"] == ["queueId"]


def test_trend_fires_current_and_prior(monkeypatch):
    out, cap = _call_tool({"interval": _INTERVAL}, monkeypatch, prior_resp=_prior_resp())
    intervals = sorted(b["interval"] for b in cap["bodies"])
    assert intervals == sorted([_INTERVAL, _PRIOR])
    assert out["prior_interval"] == _PRIOR


def test_no_trend_fires_once(monkeypatch):
    out, cap = _call_tool({"interval": _INTERVAL, "include_trend": False}, monkeypatch)
    assert len(cap["bodies"]) == 1
    assert out["prior_interval"] is None


# ─────────────────────────── per-queue rows ───────────────────────────

def _row(out, qid, media):
    return next(r for r in out["by_queue"]
                if r["queue_id"] == qid and r["media_type"] == media)


def test_by_queue_mean_and_phrase_counts(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL}, monkeypatch, prior_resp=_prior_resp())
    r = _row(out, Q_COLES_GEN, "voice")
    assert r["queue_name"] == "Coles - General"
    assert r["brand"] == "Coles"
    assert r["function"] == "General"
    assert r["analyzed_conversations"] == 293
    assert r["sentiment_records"] == 365
    assert r["mean_sentiment"] == pytest.approx(11836 / 365, abs=0.05)
    assert r["min_sentiment"] == -100 and r["max_sentiment"] == 100
    assert r["customer_phrases_positive"] == 452
    assert r["customer_phrases_negative"] == 311
    assert r["customer_phrase_positive_pct"] == pytest.approx(452 / 763 * 100, abs=0.05)
    # trend
    assert r["prior_mean_sentiment"] == pytest.approx(7000 / 350, abs=0.05)
    assert r["delta_sentiment"] == pytest.approx(11836 / 365 - 20.0, abs=0.05)


def test_by_queue_without_customer_metric_has_null_phrase_fields(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL, "include_trend": False}, monkeypatch)
    r = _row(out, Q_COLES_HOLD, "voice")
    assert r["customer_phrases_positive"] is None
    assert r["customer_phrases_negative"] is None
    assert r["customer_phrase_positive_pct"] is None
    assert r["prior_mean_sentiment"] is None
    assert r["delta_sentiment"] is None


def test_by_queue_sorted_worst_first(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL, "include_trend": False}, monkeypatch)
    means = [r["mean_sentiment"] for r in out["by_queue"]]
    assert means == sorted(means)


def test_unattributed_rows_kept_separate(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL, "include_trend": False}, monkeypatch)
    assert all(r["queue_id"] for r in out["by_queue"])
    assert len(out["unattributed"]) == 1
    u = out["unattributed"][0]
    assert u["media_type"] == "message"
    assert u["analyzed_conversations"] == 1000
    assert u["mean_sentiment"] == pytest.approx(-3.6)


# ─────────────────────────── brand rollup ───────────────────────────

def _brand(out, brand, media):
    return next(r for r in out["by_brand"]
                if r["brand"] == brand and r["media_type"] == media)


def test_brand_rollup_weighted_by_sentiment_records(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL}, monkeypatch, prior_resp=_prior_resp())
    coles_all = _brand(out, "Coles", None)
    # General voice + General message + Retention message; Holding excluded.
    assert coles_all["analyzed_conversations"] == 293 + 308 + 148
    assert coles_all["sentiment_records"] == 365 + 308 + 148
    expected = (11836 + 2374 - 3782) / (365 + 308 + 148)
    assert coles_all["mean_sentiment"] == pytest.approx(expected, abs=0.05)
    assert coles_all["customer_phrases_positive"] == 452 + 151 + 66
    assert coles_all["customer_phrases_negative"] == 311 + 202 + 333
    # prior: General voice + General message only
    assert coles_all["prior_mean_sentiment"] == pytest.approx(10000 / 650, abs=0.05)
    assert coles_all["delta_sentiment"] == pytest.approx(expected - 10000 / 650, abs=0.05)

    coles_voice = _brand(out, "Coles", "voice")
    assert coles_voice["analyzed_conversations"] == 293
    assert coles_voice["mean_sentiment"] == pytest.approx(11836 / 365, abs=0.05)

    onepass_all = _brand(out, "OnePass", None)
    assert onepass_all["analyzed_conversations"] == 218


def test_brand_rollup_excludes_skip_substring_queues_but_keeps_them_in_by_queue(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL, "include_trend": False}, monkeypatch)
    assert _row(out, Q_COLES_HOLD, "voice")["excluded_from_brand_rollup"] is True
    assert _row(out, Q_COLES_GEN, "voice")["excluded_from_brand_rollup"] is False
    assert out["by_brand_unavailable_reason"] is None


def test_brand_rollup_absent_without_tenant_config(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL, "include_trend": False},
                        monkeypatch, tenant_cfg="missing")
    assert out["by_brand"] is None
    assert "tenant" in out["by_brand_unavailable_reason"].lower()
    # by_queue still works, just without brand/function parsing
    r = _row(out, Q_COLES_GEN, "voice")
    assert r["queue_name"] == "Coles - General"
    assert r["brand"] is None


def test_brand_rollup_media_none_row_comes_first_per_brand(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL, "include_trend": False}, monkeypatch)
    brands = [r["brand"] for r in out["by_brand"]]
    assert brands == sorted(brands)
    first_coles = next(r for r in out["by_brand"] if r["brand"] == "Coles")
    assert first_coles["media_type"] is None


# ─────────────────────────── totals + envelope ───────────────────────────

def test_totals_cover_attributed_rows_only(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL}, monkeypatch, prior_resp=_prior_resp())
    t = out["totals"]
    assert t["analyzed_conversations"] == 293 + 308 + 148 + 218 + 10
    assert t["sentiment_records"] == 365 + 308 + 148 + 289 + 10
    assert t["unattributed_conversations"] == 1000
    assert t["prior_mean_sentiment"] == pytest.approx(15000 / 900, abs=0.05)


def test_envelope_contract(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL, "include_trend": False}, monkeypatch)
    assert list(out)[:2] == ["interval", "as_of_utc"]
    assert out["interval"] == _INTERVAL
    assert out["as_of_utc"].endswith("Z")
    assert out["mode"] == "summary"
    assert "raw" not in out
    assert out["scale"].startswith("-100")
    assert any("messaging" in n.lower() for n in out["notes"])


def test_full_mode_carries_raw(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL, "mode": "full"},
                        monkeypatch, prior_resp=_prior_resp())
    assert out["mode"] == "full"
    assert "current" in out["raw"] and "prior" in out["raw"]


def test_bad_mode_rejected(monkeypatch):
    with pytest.raises(Exception):
        _call_tool({"interval": _INTERVAL, "mode": "verbose"}, monkeypatch)


def test_403_soft_fails_naming_scope(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL, "include_trend": False},
                        monkeypatch, raise_status=403)
    assert out["status"] == 403
    assert out["kind"] == "sentiment_summary"
    assert "analytics:speechAndTextAnalyticsAggregates:view" in out["message"]
    assert out["interval"] == _INTERVAL


def test_empty_results_are_safe(monkeypatch):
    out, _ = _call_tool({"interval": _INTERVAL, "include_trend": False},
                        monkeypatch, current_resp={"results": []})
    assert out["by_queue"] == []
    assert out["by_brand"] == []
    assert out["totals"]["mean_sentiment"] is None
