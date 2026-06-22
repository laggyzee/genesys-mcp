"""Lightweight shape validators for the Genesys API responses the skills consume.

The v0.9.1 + v0.9.2 bugs were all variations of one theme: a build script
read a key or sub-key that doesn't exist on the actual response shape, got
``None`` or ``{}`` silently, and emitted an empty section without ever
raising. These validators stop that pattern by asserting the *envelope*
shape once at the top of each aggregator — no full row-by-row validation,
just enough to catch:

- missing top-level keys (the ``unresolved_repeaters`` vs ``repeaters`` bug)
- wrong-shaped containers (lists where dicts expected, etc.)
- absent ``derived`` blocks on results that need them (the three-times-fixed
  ``agent_performance``-vs-``queue_performance`` derived-block confusion)

Design notes:

- Validators raise :class:`ShapeError` with a path-style message identifying
  the exact field that's missing or wrong.
- They accept a ``source`` kwarg (e.g. ``"agent_performance"``) so the error
  names which tool produced the offending payload — speeds up debugging.
- Validators are *cheap*: O(1) envelope checks, O(N) walks only when a shape
  property requires it (e.g. "at least one bucket has derived"). No pydantic
  full-row validation, no per-field type coercion.
- They are deliberately *permissive* about optional fields. The goal is
  catching the silent-empty bug class, not enforcing a complete schema.
"""
from __future__ import annotations

from typing import Any


class ShapeError(ValueError):
    """Raised when a Genesys response doesn't match the expected envelope.

    The message format is::

        <source>: expected <what> at <path>, got <found>

    Catch this at the entrypoint to surface a clear "your data doesn't look
    like what we expect" message instead of a silent empty section.
    """


def _raise(source: str | None, path: str, expected: str, found: Any) -> None:
    src = f"{source}: " if source else ""
    raise ShapeError(f"{src}expected {expected} at {path}, got {type(found).__name__} ({found!r:.80})")


def assert_aggregates_envelope(
    resp: Any,
    *,
    source: str | None = None,
    expect_derived: bool = False,
) -> None:
    """Validate the analytics-aggregates response envelope.

    Shape::

        {
          "results": [
            {
              "group": {<userId|queueId|...>: ...},
              "data":  [{"interval": ..., "metrics": [{"metric": "...", "stats": {...}}], "derived": {...}?}, ...]
            }, ...
          ]
        }

    ``expect_derived=True`` enforces that at least one bucket carries a
    ``derived`` sub-dict. Use for ``queue_performance`` consumers (which do
    produce derived blocks) and explicitly *not* for ``agent_performance``
    consumers (which don't — calling with ``expect_derived=True`` against
    an agent_performance payload is exactly the bug v0.9.1/0.9.2 fixed in
    three files).
    """
    if not isinstance(resp, dict):
        _raise(source, "<root>", "dict", resp)
    results = resp.get("results")
    if not isinstance(results, list):
        _raise(source, ".results", "list", results)

    derived_seen = False
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            _raise(source, f".results[{i}]", "dict", r)
        group = r.get("group")
        if not isinstance(group, dict):
            _raise(source, f".results[{i}].group", "dict", group)
        data = r.get("data")
        if not isinstance(data, list):
            _raise(source, f".results[{i}].data", "list", data)
        for j, bucket in enumerate(data):
            if not isinstance(bucket, dict):
                _raise(source, f".results[{i}].data[{j}]", "dict", bucket)
            metrics = bucket.get("metrics")
            if metrics is not None and not isinstance(metrics, list):
                _raise(source, f".results[{i}].data[{j}].metrics", "list or None", metrics)
            if expect_derived and isinstance(bucket.get("derived"), dict):
                derived_seen = True

    if expect_derived and results and not derived_seen:
        # Permissive about empty results (no data → no derived possible),
        # but if there ARE results and none have a derived block, the
        # caller asked for the wrong shape — fail loud.
        raise ShapeError(
            f"{source + ': ' if source else ''}"
            "expected at least one bucket with a 'derived' block "
            "(this looks like agent_performance shape — "
            "use expect_derived=False or read raw metrics instead)"
        )


def assert_users_aggregates_envelope(
    resp: Any,
    *,
    source: str | None = None,
) -> None:
    """Validate the ``/api/v2/analytics/users/aggregates/query`` response envelope.

    v1.6+. The users/aggregates endpoint shares the same outer shape as
    conversations/aggregates — ``{results: [{group, data}]}`` — but the
    ``group`` dict keys differ (``userId``, ``routingStatus`` for the
    ``tAgentRoutingStatus`` metric; ``userId``, ``mediaType`` for
    conversation-level rollups). This validator pins the envelope plus
    the ``userId`` key on every group so a silent-empty bug class (the
    aggregator reading the wrong group field and quietly emitting zeros)
    can't repeat.
    """
    if not isinstance(resp, dict):
        _raise(source, "<root>", "dict", resp)
    results = resp.get("results")
    if not isinstance(results, list):
        _raise(source, ".results", "list", results)
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            _raise(source, f".results[{i}]", "dict", r)
        group = r.get("group")
        if not isinstance(group, dict):
            _raise(source, f".results[{i}].group", "dict", group)
        if "userId" not in group:
            raise ShapeError(
                f"{source + ': ' if source else ''}"
                f".results[{i}].group is missing 'userId' — this is the "
                "users/aggregates shape sentinel (v1.6)"
            )
        data = r.get("data")
        if not isinstance(data, list):
            _raise(source, f".results[{i}].data", "list", data)
        for j, bucket in enumerate(data):
            if not isinstance(bucket, dict):
                _raise(source, f".results[{i}].data[{j}]", "dict", bucket)
            metrics = bucket.get("metrics")
            if metrics is not None and not isinstance(metrics, list):
                _raise(source, f".results[{i}].data[{j}].metrics", "list or None", metrics)


def assert_conversation_detail_list(
    convs: Any,
    *,
    source: str | None = None,
) -> None:
    """Validate ``conversations/details/jobs`` async-job result.

    Shape:: ``[{conversationId, participants: [{userId?, sessions: [{...}]}, ...]}, ...]``

    Used by the coaching call-walk + repeat-caller deep-dive paths.
    """
    if not isinstance(convs, list):
        _raise(source, "<root>", "list", convs)
    for i, c in enumerate(convs):
        if not isinstance(c, dict):
            _raise(source, f"[{i}]", "dict", c)
        if "conversationId" not in c:
            _raise(source, f"[{i}].conversationId", "string", c.get("conversationId"))
        participants = c.get("participants")
        if participants is not None and not isinstance(participants, list):
            _raise(source, f"[{i}].participants", "list or None", participants)


def assert_repeat_caller_deep_dive(
    deep: Any,
    *,
    source: str | None = None,
) -> None:
    """Validate ``repeat_caller_deep_dive`` output shape.

    Top-level keys: ``interval``, ``scope``, ``org_rollup``, ``repeaters``.

    Critically, the row list lives under ``repeaters`` — NOT
    ``unresolved_repeaters`` (the key the pre-v0.9.2 daily-brief code mis-read).
    This validator pins that contract.
    """
    if not isinstance(deep, dict):
        _raise(source, "<root>", "dict", deep)
    if "repeaters" not in deep:
        # The legacy fallback key is acceptable too, but at least one must exist.
        if "unresolved_repeaters" not in deep:
            raise ShapeError(
                f"{source + ': ' if source else ''}"
                "expected 'repeaters' (or legacy 'unresolved_repeaters') key — "
                "this is the v0.9.2 daily-brief mis-key bug class"
            )
    rows = deep.get("repeaters") or deep.get("unresolved_repeaters") or []
    if not isinstance(rows, list):
        _raise(source, ".repeaters", "list", rows)
    org = deep.get("org_rollup")
    if org is not None and not isinstance(org, dict):
        _raise(source, ".org_rollup", "dict or None", org)


def assert_break_overrun_report(
    brk: Any,
    *,
    source: str | None = None,
) -> None:
    """Validate ``break_overrun_report`` output shape.

    Per-agent rows include all four overrun-related fields. The pre-v0.9.2
    ``adherence_flags`` bug came from reading only ``total_overrun_min`` and
    ignoring ``pre_break_overrun_total_min``. This validator pins that all
    four fields are present on every row so a future refactor can't silently
    drop one.
    """
    if not isinstance(brk, dict):
        _raise(source, "<root>", "dict", brk)
    users = brk.get("users")
    if not isinstance(users, list):
        _raise(source, ".users", "list", users)
    required_per_user = {
        "user_id", "total_overrun_min", "pre_break_overrun_total_min",
        "away_total_min", "overrun_count", "pre_break_overrun_count",
    }
    for i, u in enumerate(users):
        if not isinstance(u, dict):
            _raise(source, f".users[{i}]", "dict", u)
        missing = required_per_user - set(u.keys())
        if missing:
            raise ShapeError(
                f"{source + ': ' if source else ''}"
                f".users[{i}] is missing required fields: {sorted(missing)}"
            )
