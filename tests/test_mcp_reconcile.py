"""Regression tests for the mcp-reconcile build script.

Pre-v0.10 this file had zero tests despite being the **release-time
correctness validator** for every other skill. Each row generator gets
direct coverage on the dict shapes produced by the underlying tools, so
a future refactor that breaks shape contracts trips here loudly.

Historically this skill caught two real bugs: the v0.9.1
``_agent_voice_rows`` derived-block read and the v0.9.1
``_aggregates_for_users`` accumulator. Those motivated formalising
these row generators with tests.
"""
from __future__ import annotations

import pytest


# ── _queue_rows ──

class TestQueueRows:
    def _qp_payload(self, queue_id: str, media: str, answered: int,
                    sl_pct: float, aht_s: float) -> dict:
        return {"results": [{
            "group": {"queueId": queue_id, "mediaType": media},
            "data": [{
                "metrics": [],
                "derived": {
                    "answered": answered,
                    "service_level_pct": sl_pct,
                    "avg_handle_s": aht_s,
                },
            }],
        }]}

    def test_voice_row_pulls_derived_fields(self, build_checklist_reconcile):
        qp = self._qp_payload("q1", "voice", answered=1247, sl_pct=82.4, aht_s=330)
        rows = build_checklist_reconcile._queue_rows(
            qp, qmap={"q1": ["BrandA", "BrandA - Sales"]},
        )
        assert len(rows) == 1
        assert rows[0]["queue_name"] == "BrandA - Sales"
        assert rows[0]["brand"] == "BrandA"
        assert rows[0]["media"] == "voice"
        assert rows[0]["answered"] == 1247
        assert rows[0]["sl_pct"] == 82.4

    def test_unknown_queue_id_falls_back_to_id_as_name(self, build_checklist_reconcile):
        qp = self._qp_payload("q-unknown", "voice", 10, 80.0, 300)
        rows = build_checklist_reconcile._queue_rows(qp, qmap={})
        assert rows[0]["queue_name"] == "q-unknown"
        assert rows[0]["brand"] == "?"

    def test_drops_non_voice_non_message_media(self, build_checklist_reconcile):
        qp = self._qp_payload("q1", "callback", 50, 100.0, 0)
        rows = build_checklist_reconcile._queue_rows(
            qp, qmap={"q1": ["BrandA", "BrandA - Outbound"]},
        )
        assert rows == [], "callback rows shouldn't reach the reconcile checklist"

    def test_results_sorted_by_brand_then_queue_then_media(
        self, build_checklist_reconcile,
    ):
        qp = {"results": [
            {"group": {"queueId": "q2", "mediaType": "voice"},
             "data": [{"metrics": [], "derived": {"answered": 5}}]},
            {"group": {"queueId": "q1", "mediaType": "voice"},
             "data": [{"metrics": [], "derived": {"answered": 10}}]},
        ]}
        qmap = {"q1": ["BrandA", "BrandA - Sales"], "q2": ["BrandA", "BrandA - Billing"]}
        rows = build_checklist_reconcile._queue_rows(qp, qmap)
        # BrandA - Billing should sort before BrandA - Sales
        assert rows[0]["queue_name"] == "BrandA - Billing"


# ── _agent_voice_rows (the v0.9.1 derived-block bug regression test) ──

class TestAgentVoiceRows:
    def _ap_payload(self, uid: str, answered: int,
                    handle_count: int, handle_sum_ms: int) -> dict:
        return {"results": [{
            "group": {"userId": uid, "mediaType": "voice"},
            "data": [{
                "metrics": [
                    {"metric": "tAnswered", "stats": {"count": answered}},
                    {"metric": "tHandle", "stats": {"count": handle_count, "sum": handle_sum_ms}},
                ],
            }],
        }]}

    def test_reads_raw_metrics_not_derived_block(self, build_checklist_reconcile):
        # 60 voice answered, 60×8s avg = 480000ms handle sum, AHT 8s.
        payload = self._ap_payload("u1", answered=60, handle_count=60, handle_sum_ms=480_000)
        rows = build_checklist_reconcile._agent_voice_rows(
            payload, user_roles={"u1": ["Test Agent", "Customer Service Specialist"]},
        )
        assert len(rows) == 1
        assert rows[0]["voice_answered"] == 60
        assert rows[0]["voice_aht_s"] == pytest.approx(8.0)

    def test_returns_empty_on_pre_v091_derived_block_payload(
        self, build_checklist_reconcile,
    ):
        # The buggy shape: only a derived block, no raw metrics. Pre-v0.9.1
        # this returned a row with derived.answered; post-fix it returns 0.
        payload = {"results": [{
            "group": {"userId": "u1", "mediaType": "voice"},
            "data": [{"derived": {"answered": 60, "avg_handle_s": 8.0}, "metrics": []}],
        }]}
        rows = build_checklist_reconcile._agent_voice_rows(
            payload, user_roles={"u1": ["Test Agent", "Customer Service Specialist"]},
        )
        assert rows == [], "must read raw metrics, never derived block on agent_performance"

    def test_filters_agents_under_five_calls(self, build_checklist_reconcile):
        payload = self._ap_payload("u1", answered=3, handle_count=3, handle_sum_ms=10_000)
        rows = build_checklist_reconcile._agent_voice_rows(payload, user_roles={})
        assert rows == [], "agents with <5 voice calls aren't useful for reconciliation"

    def test_sorted_by_answered_desc(self, build_checklist_reconcile):
        payload = {"results": [
            {"group": {"userId": "u1", "mediaType": "voice"},
             "data": [{"metrics": [
                 {"metric": "tAnswered", "stats": {"count": 19}},
                 {"metric": "tHandle", "stats": {"count": 19, "sum": 100_000}},
             ]}]},
            {"group": {"userId": "u2", "mediaType": "voice"},
             "data": [{"metrics": [
                 {"metric": "tAnswered", "stats": {"count": 60}},
                 {"metric": "tHandle", "stats": {"count": 60, "sum": 100_000}},
             ]}]},
        ]}
        roles = {"u1": ["A", "Specialist"], "u2": ["B", "Specialist"]}
        rows = build_checklist_reconcile._agent_voice_rows(payload, roles)
        assert [r["voice_answered"] for r in rows] == [60, 19]


# ── _qa_rows ──

class TestQaRows:
    def test_returns_empty_when_scope_unavailable(self, build_checklist_reconcile):
        qa = {"scope_available": False}
        assert build_checklist_reconcile._qa_rows(qa, user_roles={}) == []

    def test_returns_rows_when_scope_available(self, build_checklist_reconcile):
        qa = {
            "scope_available": True,
            "per_user": {
                "u1": {"summary": {"n_evaluations": 5, "avg_score": 85.0, "pass_rate": 0.8}},
                "u2": {"summary": {"n_evaluations": 0}},  # filtered out
            },
        }
        rows = build_checklist_reconcile._qa_rows(qa, user_roles={"u1": ["Anna", "Spec"]})
        assert len(rows) == 1
        assert rows[0]["name"] == "Anna"
        assert rows[0]["n_evaluations"] == 5
        assert rows[0]["avg_score"] == 85.0


# ── _adherence_rows ──

class TestAdherenceRows:
    def test_only_pre_break_overruns_surfaced(self, build_checklist_reconcile):
        brk = {"users": [
            {"user_id": "u1", "pre_break_overrun_total_min": 42.5,
             "pre_break_overrun_count": 1},
            {"user_id": "u2", "pre_break_overrun_total_min": 0,
             "pre_break_overrun_count": 0},
            {"user_id": "u3", "pre_break_overrun_total_min": 10.0,
             "pre_break_overrun_count": 2},
        ]}
        rows = build_checklist_reconcile._adherence_rows(
            brk, user_roles={"u1": ["A", "Spec"], "u3": ["C", "Spec"]},
        )
        assert len(rows) == 2
        assert rows[0]["name"] == "A"  # 42.5 sorts first
        assert rows[0]["pre_break_overrun_min"] == 42.5

    def test_capped_at_top_15(self, build_checklist_reconcile):
        brk = {"users": [
            {"user_id": f"u{i}", "pre_break_overrun_total_min": float(20 - i),
             "pre_break_overrun_count": 1}
            for i in range(20)
        ]}
        rows = build_checklist_reconcile._adherence_rows(brk, user_roles={})
        assert len(rows) == 15


# ── Markdown render smoke test ──

class TestRenderMarkdown:
    def test_render_md_includes_section_headers(self, build_checklist_reconcile):
        md = build_checklist_reconcile._render_md(
            period="1-24 May 2026",
            interval="2026-04-30T14:00:00Z/2026-05-24T14:00:00Z",
            q_rows=[{"queue_name": "BrandA - Sales", "brand": "BrandA", "media": "voice",
                     "answered": 1247, "sl_pct": 82.4, "avg_handle_s": 330}],
            agent_rows=[{"user_id": "u1", "name": "Anna", "role": "Specialist",
                         "voice_answered": 19, "voice_aht_s": 8.0}],
            qa_rows=[],
            adherence_rows=[],
        )
        assert "1-24 May 2026" in md
        assert "BrandA - Sales" in md
        assert "Anna" in md
        # The checklist must always render the section headers, even when a section is empty
        assert "Queues" in md or "Queue" in md
