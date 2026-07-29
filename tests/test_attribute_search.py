"""Pin the v1.20 ``search_conversations_by_attribute`` rewrite.

The endpoint only supports conversationId / startTime / endTime / divisionId
as searchable fields — the v1.8–v1.19 attribute EXACT criterion drew a 400
"Search not supported." on every call. v1.20 pulls DATE_RANGE-on-startTime
windows and filters attributes client-side.

These tests pin:

- Request-body shape (DATE_RANGE on startTime ONLY — no attribute criterion,
  no sort fields, cursor echo)
- ≤4h window chunking, newest first, covering the whole interval
- Client-side key/value filtering (any-value default, exact-value opt-in)
- NPS auto-detection incl. whole-number decimals + no-response sentinels
- available_keys discovery + missing-key / case-mismatch notes
- 30-day retention clamping (with and without any scannable remainder)
- queue/agent enrichment via the batched analytics details query
  (best-effort: failure nulls the fields and adds a note)
- max_results truncation honesty
- v1.5 envelope contract (top-level interval + as_of_utc)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

_SEARCH_PATH = "/api/v2/conversations/participants/attributes/search"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _recent_interval(hours: float = 4) -> str:
    """An interval ending now-ish so the 30-day retention clamp never bites."""
    end = datetime.now(timezone.utc).replace(microsecond=0)
    return f"{_iso(end - timedelta(hours=hours))}/{_iso(end)}"


def _row(*, conv_id, attrs=None, start="2026-07-27T22:30:00.000Z",
         extra_participants=None):
    """One fake search-result row in the endpoint's REAL shape (verified live)."""
    participants = [{
        "participantPurpose": "customer",
        "participantAttributes": dict(attrs or {}),
        "participantId": f"p-{conv_id}",
        "sessionIds": [f"s-{conv_id}"],
    }]
    participants.extend(extra_participants or [])
    return {
        "conversationId": conv_id,
        "startTime": start,
        "endTime": "2026-07-27T22:45:00.000Z",
        "divisionIds": ["div-1"],
        "participantData": participants,
        "truncatedData": False,
        "_type": "participant_attributes",
    }


def _make_fake_api(pages=None):
    """Fake api_client for the raw search calls.

    ``pages`` is a list of response bodies handed out one per request (the
    last one repeats if requests keep coming). Defaults to always-empty.
    """
    captured: dict[str, list[dict]] = {"calls": []}
    queue = list(pages or [{"results": []}])

    class FakeApi:
        def call_api(self, **kwargs):
            captured["calls"].append(kwargs)
            return queue.pop(0) if len(queue) > 1 else queue[0]

    return FakeApi, captured


class _FakeAnalyticsNamespace:
    """Stub for the ``gc`` module attribute: routes AnalyticsApi().post_… ."""

    def __init__(self, responder):
        self.captured_bodies: list[dict] = []
        outer = self

        class _Api:
            def __init__(self, _client):
                pass

            def post_analytics_conversations_details_query(self, body):
                outer.captured_bodies.append(body)
                return responder(body)

        self.AnalyticsApi = _Api


def _call_tool(args, monkeypatch, *, fake_api, enrich=None):
    import PureCloudPlatformClientV2 as gc
    from genesys_mcp import client as gen_client
    from genesys_mcp.tools import attribute_search
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(attribute_search, "get_api", lambda: fake_api)
    monkeypatch.setattr(gen_client, "_api_client", gc.ApiClient())
    if enrich is None:
        # Most tests aren't about enrichment — stub it to a no-op so matched
        # rows don't trigger the analytics client.
        monkeypatch.setattr(attribute_search, "_enrich_rows", lambda *a, **k: None)
    else:
        monkeypatch.setattr(attribute_search, "gc", enrich)

    app = FastMCP(name="t")
    attribute_search.register(app)
    result = asyncio.run(app.call_tool("search_conversations_by_attribute", args))
    text = getattr(result[0], "text", None) or result[0].get("text")
    return json.loads(text)


# ─────────────────────── request body shape ───────────────────────


class TestRequestBodyShape:
    def test_date_range_on_start_time_is_the_only_criterion(self, monkeypatch):
        FakeApi, captured = _make_fake_api()
        _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval()},
            monkeypatch, fake_api=FakeApi(),
        )
        body = captured["calls"][0]["body"]
        assert len(body["query"]) == 1
        dr = body["query"][0]
        assert dr["type"] == "DATE_RANGE"
        assert dr["fields"] == ["startTime"]
        assert dr["startValue"].endswith("Z")
        assert dr["endValue"].endswith("Z")
        # The old attribute criterion / sort fields drew the 400.
        assert "sortBy" not in body
        assert "sortOrder" not in body

    def test_attribute_value_never_reaches_the_request(self, monkeypatch):
        FakeApi, captured = _make_fake_api()
        _call_tool(
            {"attribute_key": "outcome", "attribute_value": "Resolved",
             "interval": _recent_interval()},
            monkeypatch, fake_api=FakeApi(),
        )
        for call in captured["calls"]:
            assert len(call["body"]["query"]) == 1
            assert call["body"]["query"][0]["type"] == "DATE_RANGE"
            assert "Resolved" not in json.dumps(call["body"])

    def test_endpoint_path(self, monkeypatch):
        FakeApi, captured = _make_fake_api()
        _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval()},
            monkeypatch, fake_api=FakeApi(),
        )
        assert captured["calls"][0]["resource_path"] == _SEARCH_PATH
        assert captured["calls"][0]["method"] == "POST"

    def test_cursor_echoed_within_window(self, monkeypatch):
        FakeApi, captured = _make_fake_api(pages=[
            {"results": [_row(conv_id="c1")], "cursor": "CURSOR-1"},
            {"results": [_row(conv_id="c2")]},
        ])
        _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert len(captured["calls"]) == 2
        first, second = (c["body"] for c in captured["calls"])
        assert "cursor" not in first
        assert second["cursor"] == "CURSOR-1"
        # Same window on both requests — the cursor continues it.
        assert second["query"] == first["query"]


# ─────────────────────── window chunking ───────────────────────


class TestWindowChunking:
    def test_24h_interval_chunks_into_six_4h_windows_newest_first(self, monkeypatch):
        interval = _recent_interval(24)
        start_iso, end_iso = interval.split("/")
        FakeApi, captured = _make_fake_api()
        _call_tool(
            {"attribute_key": "NPS Score", "interval": interval},
            monkeypatch, fake_api=FakeApi(),
        )
        windows = [(c["body"]["query"][0]["startValue"],
                    c["body"]["query"][0]["endValue"]) for c in captured["calls"]]
        assert len(windows) == 6
        assert windows[0][1] == end_iso
        assert windows[-1][0] == start_iso
        # Newest first, contiguous, each ≤4h.
        for (lo, hi), (next_lo, next_hi) in zip(windows, windows[1:]):
            assert lo == next_hi
        for lo, hi in windows:
            span = datetime.fromisoformat(hi.replace("Z", "+00:00")) - \
                datetime.fromisoformat(lo.replace("Z", "+00:00"))
            assert span <= timedelta(hours=4)

    def test_partial_trailing_window(self, monkeypatch):
        FakeApi, captured = _make_fake_api()
        _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(5)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert len(captured["calls"]) == 2


# ─────────────────────── client-side filtering ───────────────────────


class TestClientSideFiltering:
    def test_any_value_default_matches_every_format(self, monkeypatch):
        rows = [
            _row(conv_id="c1", attrs={"NPS Score": "9"}),
            _row(conv_id="c2", attrs={"NPS Score": "9.0"}),
            _row(conv_id="c3", attrs={"NPS Score": "N/A"}),
            _row(conv_id="c4", attrs={"outcome": "Resolved"}),  # no NPS key
            _row(conv_id="c5", attrs={}),
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["totals"]["conversation_count"] == 3
        assert out["totals"]["conversations_scanned"] == 5
        assert sorted(r["attribute_value"] for r in out["conversations"]) == \
            ["9", "9.0", "N/A"]

    def test_explicit_value_is_exact_string_match(self, monkeypatch):
        rows = [
            _row(conv_id="c1", attrs={"NPS Score": "9"}),
            _row(conv_id="c2", attrs={"NPS Score": "9.0"}),
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "attribute_value": "9",
             "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["totals"]["conversation_count"] == 1
        assert out["conversations"][0]["conversation_id"] == "c1"

    def test_key_found_on_any_participant(self, monkeypatch):
        rows = [_row(
            conv_id="c1", attrs={"routing": "x"},
            extra_participants=[{
                "participantPurpose": "external",
                "participantAttributes": {"NPS Score": "8"},
                "participantId": "p2", "sessionIds": ["s2"],
            }],
        )]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["conversations"][0]["attribute_value"] == "8"


# ─────────────────────── NPS detection ───────────────────────


class TestNpsDetection:
    def test_nps_positive_case_all_0_to_10(self, monkeypatch):
        # 12 detractors (0-6), 45 passives (7-8), 85 promoters (9-10) = 142
        # NPS = (85 - 12) / 142 * 100 = 51.4
        nps_values: list[str] = (
            ["0"] * 3 + ["3"] * 4 + ["6"] * 5
            + ["7"] * 20 + ["8"] * 25
            + ["9"] * 40 + ["10"] * 45
        )
        rows = [
            _row(conv_id=f"c{i}", attrs={"NPS Score": v})
            for i, v in enumerate(nps_values)
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        ns = out["numeric_summary"]
        assert ns["count"] == 142
        nps = ns["nps"]
        assert nps["detractors_0_6"] == 12
        assert nps["passives_7_8"] == 45
        assert nps["promoters_9_10"] == 85
        assert nps["score"] == 51.4

    def test_whole_number_decimals_count_as_nps(self, monkeypatch):
        rows = [
            _row(conv_id="c1", attrs={"NPS Score": "9.0"}),
            _row(conv_id="c2", attrs={"NPS Score": "10"}),
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        nps = out["numeric_summary"]["nps"]
        assert nps["promoters_9_10"] == 2
        assert nps["score"] == 100.0

    def test_no_response_sentinels_dont_break_nps(self, monkeypatch):
        rows = [
            _row(conv_id="c1", attrs={"NPS Score": "9"}),
            _row(conv_id="c2", attrs={"NPS Score": "N/A"}),
            _row(conv_id="c3", attrs={"NPS Score": "2"}),
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        ns = out["numeric_summary"]
        assert ns["count"] == 2
        assert ns["no_response_count"] == 1
        assert ns["nps"]["score"] == 0.0  # 1 promoter - 1 detractor of 2

    def test_out_of_range_value_disables_nps(self, monkeypatch):
        rows = [
            _row(conv_id=f"c{i}", attrs={"NPS Score": v})
            for i, v in enumerate(["5", "7", "11"])
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        ns = out["numeric_summary"]
        assert ns["count"] == 3
        assert ns["nps"] is None

    def test_mixed_text_disables_nps_but_not_numeric_summary(self, monkeypatch):
        rows = [
            _row(conv_id="c1", attrs={"score": "9"}),
            _row(conv_id="c2", attrs={"score": "Resolved"}),
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        ns = out["numeric_summary"]
        assert ns["count"] == 1
        assert ns["non_numeric_count"] == 1
        assert ns["nps"] is None

    def test_all_text_values_yield_null_numeric_summary(self, monkeypatch):
        rows = [
            _row(conv_id="c1", attrs={"outcome": "Resolved"}),
            _row(conv_id="c2", attrs={"outcome": "Unresolved"}),
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "outcome", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["numeric_summary"] is None


# ─────────────────────── value distribution ───────────────────────


class TestValueDistribution:
    def test_distribution_sorted_by_count_desc(self, monkeypatch):
        values = ["Resolved", "Unresolved", "Resolved", "Escalated",
                  "Unresolved", "Resolved"]
        rows = [
            _row(conv_id=f"c{i}", attrs={"outcome": v})
            for i, v in enumerate(values)
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "outcome", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        dist = out["value_distribution"]
        assert [r["value"] for r in dist] == ["Resolved", "Unresolved", "Escalated"]
        assert [r["count"] for r in dist] == [3, 2, 1]
        assert sum(r["percentage"] for r in dist) == pytest.approx(100.0, abs=0.5)


# ─────────────────────── key discovery ───────────────────────


class TestAvailableKeys:
    def test_keys_counted_per_conversation(self, monkeypatch):
        rows = [
            _row(conv_id="c1", attrs={"outcome": "x", "MSN": "1"}),
            _row(conv_id="c2", attrs={"outcome": "y"}),
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "outcome", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        keys = out["available_keys"]
        assert keys["total_distinct"] == 2
        assert {"key": "outcome", "conversations": 2} in keys["top"]
        assert {"key": "MSN", "conversations": 1} in keys["top"]

    def test_missing_key_adds_note(self, monkeypatch):
        rows = [_row(conv_id="c1", attrs={"outcome": "x"})]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["totals"]["conversation_count"] == 0
        assert any("available_keys" in n for n in out["notes"])

    def test_case_mismatch_suggests_real_key(self, monkeypatch):
        rows = [_row(conv_id="c1", attrs={"NPS Score": "9"})]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "nps score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert any("'NPS Score'" in n for n in out["notes"])


# ─────────────────────── retention clamping ───────────────────────


class TestRetentionClamp:
    def test_fully_expired_window_makes_no_api_calls(self, monkeypatch):
        end = datetime.now(timezone.utc) - timedelta(days=40)
        interval = f"{_iso(end - timedelta(days=1))}/{_iso(end)}"
        FakeApi, captured = _make_fake_api()
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": interval},
            monkeypatch, fake_api=FakeApi(),
        )
        assert captured["calls"] == []
        assert out["totals"]["conversation_count"] == 0
        assert any("retention" in n for n in out["notes"])

    def test_straddling_window_clamps_start(self, monkeypatch):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        interval = f"{_iso(now - timedelta(days=31))}/{_iso(now - timedelta(days=29, hours=23))}"
        FakeApi, captured = _make_fake_api()
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": interval},
            monkeypatch, fake_api=FakeApi(),
        )
        assert any("clamped" in n for n in out["notes"])
        # Scan covers only the ~1h inside retention, not the requested day+.
        assert len(captured["calls"]) == 1
        # Envelope still echoes the REQUESTED interval.
        assert out["interval"] == interval


# ─────────────────────── enrichment ───────────────────────


def _details_conv(cid, *, agent_uid=None, queue_id=None):
    participants = []
    if queue_id:
        participants.append({
            "participantId": "pa", "purpose": "acd",
            "sessions": [{"segments": [{"queueId": queue_id}]}],
        })
    if agent_uid:
        participants.append({
            "participantId": "pb", "purpose": "agent", "userId": agent_uid,
            "sessions": [{"segments": [{"queueId": queue_id}] if queue_id else []}],
        })
    return {"conversationId": cid, "participants": participants}


class TestEnrichment:
    def test_queue_and_agent_filled_from_details_query(self, monkeypatch):
        rows = [_row(conv_id="c1", attrs={"NPS Score": "9"})]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        fake_gc = _FakeAnalyticsNamespace(
            lambda body: {"conversations": [
                _details_conv("c1", agent_uid="agent-42", queue_id="q-1"),
            ]},
        )
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(), enrich=fake_gc,
        )
        row = out["conversations"][0]
        assert row["agent_user_id"] == "agent-42"
        assert row["queue_id"] == "q-1"
        # One batched query, filtered by conversationId predicates.
        assert len(fake_gc.captured_bodies) == 1
        preds = fake_gc.captured_bodies[0]["conversationFilters"][0]["predicates"]
        assert [p["value"] for p in preds] == ["c1"]
        assert all(p["dimension"] == "conversationId" for p in preds)

    def test_batches_of_50_ids(self, monkeypatch):
        rows = [
            _row(conv_id=f"c{i}", attrs={"NPS Score": "9"})
            for i in range(60)
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        fake_gc = _FakeAnalyticsNamespace(lambda body: {"conversations": []})
        _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(), enrich=fake_gc,
        )
        sizes = [
            len(b["conversationFilters"][0]["predicates"])
            for b in fake_gc.captured_bodies
        ]
        assert sizes == [50, 10]

    def test_enrichment_failure_is_soft(self, monkeypatch):
        rows = [_row(conv_id="c1", attrs={"NPS Score": "9"})]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])

        def _boom(body):
            raise RuntimeError("403 forbidden")

        fake_gc = _FakeAnalyticsNamespace(_boom)
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(), enrich=fake_gc,
        )
        row = out["conversations"][0]
        assert row["agent_user_id"] is None
        assert row["queue_id"] is None
        assert row["attribute_value"] == "9"
        assert any("enrichment failed" in n for n in out["notes"])


# ─────────────────────── envelope + edges ───────────────────────


class TestEnvelopeAndEdges:
    def test_top_level_interval_and_as_of_utc(self, monkeypatch):
        interval = _recent_interval()
        FakeApi, _ = _make_fake_api()
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": interval},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["interval"] == interval
        assert out["as_of_utc"].endswith("Z")
        assert out["attribute_key"] == "NPS Score"
        assert out["attribute_value"] is None
        assert isinstance(out["notes"], list)

    def test_empty_result_is_safe(self, monkeypatch):
        FakeApi, _ = _make_fake_api()
        out = _call_tool(
            {"attribute_key": "outcome", "attribute_value": "Resolved",
             "interval": _recent_interval()},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["totals"]["conversation_count"] == 0
        assert out["totals"]["conversations_scanned"] == 0
        assert out["value_distribution"] == []
        assert out["numeric_summary"] is None
        assert out["conversations"] == []
        assert out["available_keys"]["total_distinct"] == 0

    def test_max_results_truncation_is_flagged(self, monkeypatch):
        rows = [
            _row(conv_id=f"c{i}", attrs={"NPS Score": "9"})
            for i in range(3)
        ]
        FakeApi, captured = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(8),
             "max_results": 1},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["totals"]["conversation_count"] == 1
        assert out["totals"]["truncated"] is True
        assert any("max_results" in n for n in out["notes"])
        # Scan stopped early: only the first (newest) window was queried.
        assert len(captured["calls"]) == 1

    def test_rows_sorted_newest_first(self, monkeypatch):
        rows = [
            _row(conv_id="old", attrs={"NPS Score": "1"},
                 start="2026-07-27T20:00:00.000Z"),
            _row(conv_id="new", attrs={"NPS Score": "2"},
                 start="2026-07-27T23:00:00.000Z"),
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert [r["conversation_id"] for r in out["conversations"]] == ["new", "old"]

    def test_full_mode_includes_capped_raw(self, monkeypatch):
        rows = [_row(conv_id="c1", attrs={"outcome": "x"})]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "outcome", "interval": _recent_interval(2),
             "mode": "full"},
            monkeypatch, fake_api=FakeApi(),
        )
        assert len(out["_raw"]["results"]) == 1

    def test_invalid_mode_raises(self, monkeypatch):
        FakeApi, _ = _make_fake_api()
        with pytest.raises(Exception, match="mode must be 'summary' or 'full'"):
            _call_tool(
                {"attribute_key": "NPS Score", "interval": _recent_interval(),
                 "mode": "bogus"},
                monkeypatch, fake_api=FakeApi(),
            )


def _make_routing_fake_api(responder):
    """Fake api_client whose response is computed from the request body."""
    captured: dict[str, list[dict]] = {"calls": []}

    class FakeApi:
        def call_api(self, **kwargs):
            captured["calls"].append(kwargs)
            return responder(kwargs["body"])

    return FakeApi, captured


# ─────────────────────── truncation honesty ───────────────────────


class TestTruncationHonesty:
    def test_discarded_matches_flag_truncated_even_at_scan_end(self, monkeypatch):
        # 3 matches on the FINAL page of the FINAL window, no cursor left —
        # max_results=1 discards two. truncated must still be true.
        rows = [
            _row(conv_id=f"c{i}", attrs={"NPS Score": "9"})
            for i in range(3)
        ]
        FakeApi, captured = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2),
             "max_results": 1},
            monkeypatch, fake_api=FakeApi(),
        )
        assert len(captured["calls"]) == 1  # single window, nothing unscanned
        assert out["totals"]["conversation_count"] == 1
        assert out["totals"]["truncated"] is True
        assert any("discarded" in n for n in out["notes"])

    def test_page_budget_stop_is_flagged(self, monkeypatch):
        from genesys_mcp.tools import attribute_search
        monkeypatch.setattr(attribute_search, "_MAX_PAGES_TOTAL", 2)

        def responder(body):
            return {"results": [_row(conv_id=f"c-{len(captured['calls'])}",
                                     attrs={"outcome": "x"})],
                    "cursor": "more"}

        FakeApi, captured = _make_routing_fake_api(responder)
        out = _call_tool(
            {"attribute_key": "outcome", "interval": _recent_interval(8)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert len(captured["calls"]) == 2
        assert out["totals"]["truncated"] is True
        assert any("scan budget" in n for n in out["notes"])

    def test_earlier_window_truncation_not_reset_by_later_cap_stop(self, monkeypatch):
        from genesys_mcp.tools import attribute_search
        monkeypatch.setattr(attribute_search, "_MAX_PAGES_PER_WINDOW", 1)

        interval = _recent_interval(8)  # two 4h windows
        newest_end = interval.split("/")[1]

        def responder(body):
            if body["query"][0]["endValue"] == newest_end:
                # Window 1: page guard trips with a cursor still pending.
                return {"results": [_row(conv_id="w1", attrs={})],
                        "cursor": "pending"}
            # Window 2: exactly max_results matches, cleanly terminated.
            return {"results": [_row(conv_id="w2", attrs={"NPS Score": "9"})]}

        FakeApi, _ = _make_routing_fake_api(responder)
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": interval,
             "max_results": 1},
            monkeypatch, fake_api=FakeApi(),
        )
        # Window 1's unscanned remainder must keep truncated true even though
        # window 2's max_results stop had nothing left unscanned.
        assert out["totals"]["truncated"] is True

    def test_truncated_data_rows_add_note(self, monkeypatch):
        row = _row(conv_id="c1", attrs={"outcome": "x"})
        row["truncatedData"] = True
        FakeApi, _ = _make_fake_api(pages=[{"results": [row]}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert any("truncatedData" in n for n in out["notes"])


# ─────────────────────── scan behaviours ───────────────────────


class TestScanBehaviours:
    def test_boundary_conversation_counted_once(self, monkeypatch):
        # The same conversation returned by two adjacent windows (inclusive
        # DATE_RANGE boundary) must be matched and scanned exactly once.
        dupe = _row(conv_id="dupe", attrs={"NPS Score": "9"})
        FakeApi, captured = _make_routing_fake_api(lambda body: {"results": [dupe]})
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(8)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert len(captured["calls"]) == 2
        assert out["totals"]["conversations_scanned"] == 1
        assert out["totals"]["conversation_count"] == 1

    def test_cursor_resets_between_windows(self, monkeypatch):
        interval = _recent_interval(8)
        newest_end = interval.split("/")[1]

        def responder(body):
            if body["query"][0]["endValue"] == newest_end and not body.get("cursor"):
                return {"results": [_row(conv_id="a", attrs={})], "cursor": "w1-c"}
            return {"results": []}

        FakeApi, captured = _make_routing_fake_api(responder)
        _call_tool(
            {"attribute_key": "NPS Score", "interval": interval},
            monkeypatch, fake_api=FakeApi(),
        )
        # calls: w1 page1 (no cursor), w1 page2 (cursor), w2 page1 (NO cursor)
        assert len(captured["calls"]) == 3
        assert captured["calls"][1]["body"]["cursor"] == "w1-c"
        assert "cursor" not in captured["calls"][2]["body"]

    def test_long_span_uses_12h_windows(self, monkeypatch):
        FakeApi, captured = _make_fake_api()
        _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(8 * 24)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert len(captured["calls"]) == 16  # 8 days / 12h

    def test_non_string_attribute_values_are_stringified(self, monkeypatch):
        rows = [
            _row(conv_id="c1", attrs={"NPS Score": 9}),      # JSON number
            _row(conv_id="c2", attrs={"NPS Score": True}),    # JSON bool
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert sorted(r["attribute_value"] for r in out["conversations"]) == \
            ["9", "True"]
        ns = out["numeric_summary"]
        assert ns["count"] == 1
        assert ns["non_numeric_count"] == 1

    def test_nan_inf_values_do_not_crash(self, monkeypatch):
        rows = [
            _row(conv_id=f"c{i}", attrs={"NPS Score": v})
            for i, v in enumerate(["9", "NaN", "inf"])
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        ns = out["numeric_summary"]
        assert ns["count"] == 1
        assert ns["non_numeric_count"] == 2
        assert ns["nps"] is None

    def test_full_mode_raw_cap_adds_note(self, monkeypatch):
        from genesys_mcp.tools import attribute_search
        monkeypatch.setattr(attribute_search, "_RAW_CAP", 1)
        rows = [_row(conv_id=f"c{i}", attrs={"outcome": "x"}) for i in range(2)]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        out = _call_tool(
            {"attribute_key": "outcome", "interval": _recent_interval(2),
             "mode": "full"},
            monkeypatch, fake_api=FakeApi(),
        )
        assert len(out["_raw"]["results"]) == 1
        assert any("_raw capped" in n for n in out["notes"])

    def test_scanned_interval_absent_without_clamp(self, monkeypatch):
        FakeApi, _ = _make_fake_api()
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(),
        )
        assert "scanned_interval" not in out

    def test_scanned_interval_reported_on_clamp(self, monkeypatch):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        interval = f"{_iso(now - timedelta(days=31))}/{_iso(now - timedelta(days=29, hours=23))}"
        FakeApi, _ = _make_fake_api()
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": interval},
            monkeypatch, fake_api=FakeApi(),
        )
        assert out["interval"] == interval
        scanned = out["scanned_interval"]
        assert scanned != interval
        assert scanned.endswith(interval.split("/")[1])


# ─────────────────────── enrichment edges ───────────────────────


class TestEnrichmentEdges:
    def test_enrichment_interval_padded_one_hour(self, monkeypatch):
        interval = _recent_interval(2)
        start_iso, end_iso = interval.split("/")
        rows = [_row(conv_id="c1", attrs={"NPS Score": "9"})]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        fake_gc = _FakeAnalyticsNamespace(lambda body: {"conversations": []})
        _call_tool(
            {"attribute_key": "NPS Score", "interval": interval},
            monkeypatch, fake_api=FakeApi(), enrich=fake_gc,
        )
        got = fake_gc.captured_bodies[0]["interval"]
        got_start, got_end = (
            datetime.fromisoformat(p.replace("Z", "+00:00"))
            for p in got.split("/")
        )
        assert got_start == datetime.fromisoformat(start_iso.replace("Z", "+00:00")) - timedelta(hours=1)
        assert got_end == datetime.fromisoformat(end_iso.replace("Z", "+00:00")) + timedelta(hours=1)

    def test_enrich_cap_adds_note(self, monkeypatch):
        from genesys_mcp.tools import attribute_search
        monkeypatch.setattr(attribute_search, "_ENRICH_MAX", 2)
        rows = [
            _row(conv_id=f"c{i}", attrs={"NPS Score": "9"})
            for i in range(3)
        ]
        FakeApi, _ = _make_fake_api(pages=[{"results": rows}])
        fake_gc = _FakeAnalyticsNamespace(lambda body: {"conversations": []})
        out = _call_tool(
            {"attribute_key": "NPS Score", "interval": _recent_interval(2)},
            monkeypatch, fake_api=FakeApi(), enrich=fake_gc,
        )
        assert sum(len(b["conversationFilters"][0]["predicates"])
                   for b in fake_gc.captured_bodies) == 2
        assert any("Only the first 2" in n for n in out["notes"])
