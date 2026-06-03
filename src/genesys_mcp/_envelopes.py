"""Canonical soft-fail envelope used across MCP tools (v1.3+).

Pre-v1.3 every tool that soft-failed invented its own envelope shape:

    speech_analytics tools:   {"status": 404, "conversation_id": cid, "message": ...}
    lookup_external_contact:  {"status": 404, "match": None}
    queue_estimated_wait_time: {"queue_id": qid, "error": str(exc)}  (no status)

This made it hard for callers to do uniform soft-fail handling
(e.g. ``if result.get("status") == 404: skip``). v1.3 standardises every
soft-fail to use :func:`soft_fail_envelope` so callers can rely on a
single shape regardless of which tool returned the 404.

The canonical shape is:

    {
      "status": <int>,                  # HTTP status code (almost always 404)
      "message": "<human-readable>",    # what was missing + why
      "<id_kind>": "<id_value>",        # the id that was missing
      "kind": "<short noun>",           # what kind of thing 404'd
    }

Tests in ``tests/test_envelopes.py`` pin both the helper and that every
soft-fail-bearing tool returns this exact shape.
"""
from __future__ import annotations

from typing import Any


def soft_fail_envelope(
    *,
    status: int = 404,
    message: str,
    kind: str,
    **id_fields: Any,
) -> dict[str, Any]:
    """Build the canonical soft-fail envelope.

    Args:
        status: HTTP status code (default 404 — the common soft-fail case).
        message: One-line, human-readable description of what was missing.
        kind: Short noun naming the thing that 404'd ("recordings",
            "transcript url", "external contact", "conversation").
        **id_fields: The id that 404'd, keyed by what kind of id it is
            (``conversation_id=<uuid>``, ``user_id=<uuid>``, etc.). Use
            kwargs so the field name reads naturally in the response.

    Returns:
        A dict with ``status``, ``kind``, ``message``, and the id fields,
        in a canonical order that callers can rely on.
    """
    return {
        "status": status,
        "kind": kind,
        "message": message,
        **id_fields,
    }


def is_soft_fail(result: Any) -> bool:
    """Return True if ``result`` looks like a soft-fail envelope.

    Used by composition tools (e.g. ``agent_coaching_pack``) when iterating
    over per-call enrichment results — lets them check once and skip
    rather than guessing whether a particular dict shape is success or
    failure.
    """
    return (
        isinstance(result, dict)
        and "status" in result
        and isinstance(result["status"], int)
        and result["status"] >= 400
    )
