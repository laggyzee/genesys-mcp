"""Queue-name → (brand, channel, function) parser with v1.0 fallback rules.

Pre-v1.0 the skills assumed every queue name matched
``cfg.queues.name_pattern`` (default ``"{brand} - {channel} - {function}"``).
Queues that didn't match were silently dropped — fine for Prvidr (most
queues match) but disastrous for tenants whose naming is different.

v1.0 introduces two tenant-config knobs:

- ``queues.name_pattern: null`` — no structured naming. All queues are
  treated as ``function`` with empty brand/channel.
- ``queues.name_pattern_match_required: false`` — non-matching queues
  fall back to the full queue name as ``function`` (and empty
  brand/channel). Match-mostly tenants get a complete picture instead
  of silent drops.

The build scripts call :func:`parse_queue_name` per queue and either
keep, fall back, or skip per the tenant's settings.
"""
from __future__ import annotations

import re
from typing import NamedTuple


class QueueParts(NamedTuple):
    """Parsed components of a queue name.

    ``matched`` is False when the queue didn't match the configured pattern
    and a fallback was applied. Callers use this to colour-tag fallback
    rows differently in reports (e.g. "(legacy)" in brand).
    """

    brand: str
    channel: str
    function: str
    matched: bool


_PLACEHOLDER_RE = re.compile(r"\{(brand|channel|function)\}")


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert ``"{brand} - {channel} - {function}"`` to a regex with named groups.

    Non-placeholder text is escaped so separators like ``" - "`` match literally.
    """
    parts: list[str] = []
    last_end = 0
    for match in _PLACEHOLDER_RE.finditer(pattern):
        # Literal chunk before the placeholder
        if match.start() > last_end:
            parts.append(re.escape(pattern[last_end:match.start()]))
        # The placeholder itself → named regex group (.+? non-greedy)
        parts.append(f"(?P<{match.group(1)}>.+?)")
        last_end = match.end()
    # Trailing literal
    if last_end < len(pattern):
        parts.append(re.escape(pattern[last_end:]))
    return re.compile(f"^{''.join(parts)}$")


def parse_queue_name(
    queue_name: str,
    pattern: str | None,
    *,
    match_required: bool = True,
) -> QueueParts | None:
    """Parse ``queue_name`` against ``pattern`` with the v1.0 fallback rules.

    Args:
        queue_name: The full Genesys queue name (e.g. ``"Coles - Voice - Sales"``).
        pattern: The tenant's ``queues.name_pattern`` (e.g.
            ``"{brand} - {channel} - {function}"``) or ``None`` for no
            structured naming.
        match_required: When ``True`` (default) and the queue doesn't match
            the pattern, returns ``None`` so the caller can skip it. When
            ``False`` falls back to treating the full queue name as
            ``function`` with empty brand/channel.

    Returns:
        ``QueueParts`` on a successful parse or fallback; ``None`` only
        when ``match_required`` is True and the queue doesn't match.
    """
    # No structured naming: every queue is just a function.
    if pattern is None:
        return QueueParts(brand="", channel="", function=queue_name, matched=False)

    regex = _pattern_to_regex(pattern)
    m = regex.match(queue_name)
    if m:
        return QueueParts(
            brand=m.group("brand") if "brand" in m.groupdict() else "",
            channel=m.group("channel") if "channel" in m.groupdict() else "",
            function=m.group("function") if "function" in m.groupdict() else "",
            matched=True,
        )

    # No match → either skip or fall back.
    if match_required:
        return None
    return QueueParts(brand="", channel="", function=queue_name, matched=False)


def compute_pattern_match_rate(
    queue_names: list[str], pattern: str | None,
) -> float:
    """Return the fraction of ``queue_names`` that match ``pattern`` cleanly.

    Used by ``mcp_health_check`` to warn when a tenant's configured
    pattern doesn't actually fit most of their queues. Always returns
    ``1.0`` for ``pattern=None`` (no pattern → vacuously matched).
    """
    if not queue_names:
        return 1.0
    if pattern is None:
        return 1.0
    regex = _pattern_to_regex(pattern)
    matched = sum(1 for n in queue_names if regex.match(n))
    return matched / len(queue_names)
