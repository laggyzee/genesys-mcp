"""Pure calculation layer for the Repeat Contacts & FCR report (v1.24+).

No Genesys calls live here — the tool module fetches conversations and
External Contacts, this module turns them into numbers. Keeping it pure is
what makes the methodology testable: lookback boundaries, chains, same-day
repeats, channel rules and identity precedence are all pinned against this
file without a tenant.

Pipeline:

    raw analytics conversation ──slim_conversation──▶ Contact
    Contacts ──resolve_identities──▶ {conversation_id: Identity}
    Contacts + identities ──build_report──▶ results / breakdown / drilldown
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

FCR_LABEL = "FCR (Amaysim methodology)"

VOICE = "voice"
MESSAGING = "messaging"
COMBINED = "combined"
CHANNELS = (VOICE, MESSAGING, COMBINED)

LINKED = "external_contact_linked"
LOOKUP = "external_contact_lookup"
RAW_KEY = "raw_key"
UNIDENTIFIED = "unidentified"
METHODS = (LINKED, LOOKUP, RAW_KEY, UNIDENTIFIED)

METHODOLOGY = (
    "A contact is one Genesys conversation, counted once regardless of how "
    "many sessions, segments or agents it had. A contact is a REPEAT if the "
    "same customer had any prior in-scope contact within the preceding N "
    "days, for any reason (no intent or wrap-up matching); the repeat is "
    "attributed to the later contact. Repeat Contact Rate (N-day, channel) = "
    "repeat contacts of that channel in the reporting period / total contacts "
    "of that channel in the reporting period. FCR (Amaysim methodology) = "
    "1 - Repeat Contact Rate: it is derived from repeat contacts, not "
    "measured directly (no survey or agent disposition is involved). Voice "
    "and Messaging rates only match a prior contact on the same channel; the "
    "Combined rate matches a prior contact on either channel. Contacts from "
    "the lookback (period start minus the largest window) are used only to "
    "find prior contacts and are never counted in the numerator or "
    "denominator. A contact that is itself a repeat can be the prior for a "
    "later contact, so chains count each link. Day boundaries use the "
    "configured timezone and 'within N days' compares local calendar dates. "
    "Customers are identified by their Genesys External Contact (resolved to "
    "the canonical contact when merged), then by an External Contact lookup "
    "on the raw identifier, then by the normalised raw identifier itself "
    "(E.164 phone, email, web messaging user, social handle). Contacts with "
    "no identifier are counted in the denominator but are never repeats."
)


# ───────────────────────────── records ─────────────────────────────

@dataclass(frozen=True, slots=True)
class Contact:
    """One conversation, reduced to what the report needs."""

    conversation_id: str
    start: datetime  # timezone-aware UTC
    channel: str  # VOICE | MESSAGING
    queue_ids: tuple[str, ...] = ()  # in order of first touch
    handled: bool = True  # an agent interacted
    queued: bool = True  # entered an ACD queue
    external_contact_id: str | None = None
    raw_kind: str | None = None  # phone | sms | email | web_messaging_user | social:<type>
    raw_value: str | None = None  # as seen on the conversation, not normalised

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.conversation_id,
            "start": iso_z(self.start),
            "channel": self.channel,
            "queues": list(self.queue_ids),
            "handled": self.handled,
            "queued": self.queued,
            "ec": self.external_contact_id,
            "rk": self.raw_kind,
            "rv": self.raw_value,
        }

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> "Contact":
        return cls(
            conversation_id=rec["id"],
            start=parse_utc(rec["start"]),
            channel=rec["channel"],
            queue_ids=tuple(rec.get("queues") or ()),
            handled=bool(rec.get("handled")),
            queued=bool(rec.get("queued")),
            external_contact_id=rec.get("ec"),
            raw_kind=rec.get("rk"),
            raw_value=rec.get("rv"),
        )


@dataclass(frozen=True, slots=True)
class Identity:
    customer_key: str | None
    method: str


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ───────────────────────────── phone normalisation ─────────────────────────────

# country → (calling code, national trunk prefix, international dialling prefixes)
_DIAL_RULES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "AU": ("61", "0", ("0011", "00")),
    "NZ": ("64", "0", ("00",)),
    "GB": ("44", "0", ("00",)),
    "IE": ("353", "0", ("00",)),
    "US": ("1", "", ("011",)),
    "CA": ("1", "", ("011",)),
    "SG": ("65", "", ("001", "00")),
    "ZA": ("27", "0", ("00",)),
    "IN": ("91", "0", ("00",)),
    "PH": ("63", "0", ("00",)),
}
# Length of a national significant number, used to recognise a number that
# already carries the country code but lost its "+".
_NSN_LENGTHS: dict[str, tuple[int, ...]] = {"AU": (9,), "NZ": (8, 9, 10), "GB": (9, 10), "US": (10,), "CA": (10,)}


def normalise_phone(raw: str | None, default_country: str = "AU") -> str | None:
    """Best-effort E.164. Returns ``None`` when there is no usable number.

    ``tel:`` / ``sip:`` wrappers and URI parameters are stripped. Anonymous,
    private and non-numeric SIP callers return ``None`` (unidentifiable).
    """
    if not raw:
        return None
    value = raw.strip()
    lowered = value.lower()
    if lowered.startswith("tel:"):
        value = value[4:]
    elif lowered.startswith(("sip:", "sips:")):
        value = value.split(":", 1)[1].split("@", 1)[0]
    value = value.split(";", 1)[0].strip()
    has_plus = value.startswith("+")
    if re.search(r"[A-Za-z]", value):
        return None  # 'anonymous', 'Private', a SIP user name, ...
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None

    if has_plus:
        return f"+{digits}" if 7 <= len(digits) <= 15 else None

    code, trunk, intl_prefixes = _DIAL_RULES.get(default_country.upper(), ("", "", ("00",)))
    for prefix in intl_prefixes:
        if digits.startswith(prefix) and len(digits) - len(prefix) >= 7:
            return f"+{digits[len(prefix):]}"
    if trunk and digits.startswith(trunk) and len(digits) > len(trunk) + 5:
        return f"+{code}{digits[len(trunk):]}"
    nsn_lengths = _NSN_LENGTHS.get(default_country.upper(), ())
    if code and digits.startswith(code) and (len(digits) - len(code)) in nsn_lengths:
        return f"+{digits}"
    if code and len(digits) in nsn_lengths:
        return f"+{code}{digits}"
    # Unrecognised national shape: still a stable key, just not provably E.164.
    return digits if len(digits) >= 6 else None


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Message types whose customer address is a phone number.
_PHONE_MESSAGE_TYPES = {"sms", "whatsapp"}


def raw_customer_key(
    contact: Contact,
    *,
    default_country: str = "AU",
    messaging_keys: Iterable[str] = ("email", "sms", "web_messaging_user", "social"),
) -> str | None:
    """Normalised raw-identifier key, or ``None`` if the contact has none.

    Phone-number identifiers share one ``phone:`` namespace across voice, SMS
    and WhatsApp, so a call and an SMS from the same number resolve to the
    same customer even without an External Contact.
    """
    kind, value = contact.raw_kind, (contact.raw_value or "").strip()
    if not kind or not value:
        return None
    if kind == "phone":
        number = normalise_phone(value, default_country)
        return f"phone:{number}" if number else None

    allowed = set(messaging_keys)
    if kind == "sms":
        if "sms" not in allowed:
            return None
        number = normalise_phone(value, default_country)
        return f"phone:{number}" if number else None
    if kind == "email":
        if "email" not in allowed or not _EMAIL_RE.match(value):
            return None
        return f"email:{value.lower()}"
    if kind == "web_messaging_user":
        return f"webmsg:{value.lower()}" if "web_messaging_user" in allowed else None
    if kind.startswith("social:"):
        if "social" not in allowed:
            return None
        platform = kind.split(":", 1)[1]
        if platform in _PHONE_MESSAGE_TYPES:
            number = normalise_phone(value, default_country)
            if number:
                return f"phone:{number}"
        return f"social:{platform}:{value.lower()}"
    return None


# ───────────────────────────── conversation → Contact ─────────────────────────────

_CUSTOMER_PURPOSES = ("customer", "external")


def _session_start(session: dict[str, Any]) -> str:
    starts = [seg.get("segmentStart") for seg in session.get("segments") or [] if seg.get("segmentStart")]
    return min(starts) if starts else ""


def _raw_identifier(session: dict[str, Any], channel: str) -> tuple[str | None, str | None]:
    outbound = (session.get("direction") or "").lower() == "outbound"
    if channel == VOICE:
        value = session.get("dnis") if outbound else session.get("ani")
        return ("phone", value) if value else (None, None)

    value = session.get("addressTo") if outbound else session.get("addressFrom")
    if not value:
        return None, None
    message_type = (session.get("messageType") or "").lower()
    if _EMAIL_RE.match(value.strip()):
        return "email", value
    if message_type == "sms":
        return "sms", value
    if message_type in ("webmessaging", ""):
        return "web_messaging_user", value
    return f"social:{message_type}", value


def slim_conversation(
    conv: dict[str, Any],
    *,
    voice_media: Iterable[str] = ("voice",),
    messaging_media: Iterable[str] = ("message",),
) -> Contact | None:
    """Reduce an analytics conversation-detail row to a :class:`Contact`.

    Returns ``None`` when the row has no id / start or no session on a
    reported channel (e.g. a callback-only or email conversation).
    """
    conversation_id = conv.get("conversationId")
    start_raw = conv.get("conversationStart")
    if not conversation_id or not start_raw:
        return None

    media_channel = {m: VOICE for m in voice_media}
    media_channel.update({m: MESSAGING for m in messaging_media})

    participants = conv.get("participants") or []
    customer_sessions: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    any_sessions: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for participant in participants:
        for session in participant.get("sessions") or []:
            if (session.get("mediaType") or "").lower() not in media_channel:
                continue
            entry = (_session_start(session), participant, session)
            any_sessions.append(entry)
            # The jobs archive labels the caller ``customer``; the synchronous
            # query uses ``external`` for the same participant.
            if participant.get("purpose") in _CUSTOMER_PURPOSES:
                customer_sessions.append(entry)

    customer_sessions.sort(key=lambda e: e[0])
    any_sessions.sort(key=lambda e: e[0])
    first = (customer_sessions or any_sessions or [None])[0]
    if first is None:
        return None
    channel = media_channel[(first[2].get("mediaType") or "").lower()]

    external_contact_id: str | None = None
    raw_kind: str | None = None
    raw_value: str | None = None
    for _start, participant, session in customer_sessions:
        if not external_contact_id and participant.get("externalContactId"):
            external_contact_id = participant["externalContactId"]
        if raw_kind is None and media_channel.get((session.get("mediaType") or "").lower()) == channel:
            raw_kind, raw_value = _raw_identifier(session, channel)

    queue_touches: list[tuple[str, str]] = []
    queued = False
    handled = False
    for participant in participants:
        purpose = participant.get("purpose")
        for session in participant.get("sessions") or []:
            for segment in session.get("segments") or []:
                queue_id = segment.get("queueId")
                if purpose == "acd":
                    queued = True
                if queue_id and purpose in ("acd", "agent"):
                    queue_touches.append((segment.get("segmentStart") or "", queue_id))
                if purpose == "agent" and participant.get("userId") and segment.get("segmentType") == "interact":
                    handled = True

    queue_ids = tuple(dict.fromkeys(qid for _ts, qid in sorted(queue_touches)))
    return Contact(
        conversation_id=conversation_id,
        start=parse_utc(start_raw),
        channel=channel,
        queue_ids=queue_ids,
        handled=handled,
        queued=queued or bool(queue_ids),
        external_contact_id=external_contact_id,
        raw_kind=raw_kind,
        raw_value=raw_value,
    )


def dedupe_contacts(contacts: Iterable[Contact]) -> list[Contact]:
    """One Contact per conversationId, chronological (id breaks ties)."""
    seen: dict[str, Contact] = {}
    for contact in contacts:
        seen.setdefault(contact.conversation_id, contact)
    return sorted(seen.values(), key=lambda c: (c.start, c.conversation_id))


def in_scope(
    contact: Contact,
    *,
    include_abandoned: bool,
    include_pre_queue: bool,
    queue_filter: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """Scope applies to prior contacts too — an excluded contact is never a prior."""
    if not contact.handled:
        if contact.queued and not include_abandoned:
            return False
        if not contact.queued and not include_pre_queue:
            return False
    if queue_filter and not queue_filter.intersection(contact.queue_ids):
        return False
    return True


# ───────────────────────────── identity ─────────────────────────────

CanonicalResolver = Callable[[set[str]], dict[str, str]]
IdentifierLookup = Callable[[str, str], "str | None"]


def lookup_target(raw_key: str) -> tuple[str, str] | None:
    """``(identifier_kind, value)`` for keys an External Contact lookup can serve."""
    kind, _, rest = raw_key.partition(":")
    if kind in ("phone", "email") and rest:
        return kind, rest
    if kind == "social":
        platform, _, handle = rest.partition(":")
        if platform and handle:
            return f"social:{platform}", handle
    return None  # web messaging user ids are not searchable identifiers


def resolve_identities(
    contacts: list[Contact],
    *,
    mode: str = "external_contact_first",
    enable_lookup: bool = True,
    resolve_canonical: bool = True,
    max_lookups: int = 5000,
    default_country: str = "AU",
    messaging_keys: Iterable[str] = ("email", "sms", "web_messaging_user", "social"),
    canonical_resolver: CanonicalResolver | None = None,
    lookup: IdentifierLookup | None = None,
    priority_ids: set[str] | None = None,
) -> tuple[dict[str, Identity], dict[str, Any]]:
    """Resolve every contact to one customer key.

    Precedence: External Contact link (canonical) → External Contact lookup by
    raw identifier → normalised raw identifier → unidentified.

    ``lookup(kind, value)`` returns the *canonical* contact id for exactly one
    match, else ``None``. ``priority_ids`` (the reporting-period contacts) are
    looked up first so a ``max_lookups`` cap degrades the lookback, not the
    period being reported.
    """
    messaging_keys = tuple(messaging_keys)
    raw_keys = {
        c.conversation_id: raw_customer_key(c, default_country=default_country, messaging_keys=messaging_keys)
        for c in contacts
    }
    stats: dict[str, Any] = {
        "external_contacts_seen": 0,
        "external_contacts_merged": 0,
        "local_identifier_matches": 0,
        "lookups_attempted": 0,
        "lookups_matched": 0,
        "lookups_skipped_over_cap": 0,
    }
    identities: dict[str, Identity] = {}

    if mode == "raw_only":
        for c in contacts:
            key = raw_keys[c.conversation_id]
            identities[c.conversation_id] = Identity(key, RAW_KEY if key else UNIDENTIFIED)
        return identities, stats

    linked_ids = {c.external_contact_id for c in contacts if c.external_contact_id}
    stats["external_contacts_seen"] = len(linked_ids)
    canonical: dict[str, str] = {}
    if resolve_canonical and canonical_resolver and linked_ids:
        canonical = canonical_resolver(set(linked_ids)) or {}
    stats["external_contacts_merged"] = sum(1 for k, v in canonical.items() if v and v != k)

    # An identifier already seen on a *linked* conversation in this run names
    # its External Contact without an API call — and stops a linked call and
    # an unlinked call from the same number landing on different keys.
    seen_on_linked: dict[str, set[str]] = {}
    for c in contacts:
        if not c.external_contact_id:
            continue
        contact_id = canonical.get(c.external_contact_id) or c.external_contact_id
        identities[c.conversation_id] = Identity(f"ec:{contact_id}", LINKED)
        raw_key = raw_keys[c.conversation_id]
        if raw_key:
            seen_on_linked.setdefault(raw_key, set()).add(contact_id)

    pending: dict[str, list[Contact]] = {}
    for c in contacts:
        if c.conversation_id in identities:
            continue
        raw_key = raw_keys[c.conversation_id]
        if not raw_key:
            identities[c.conversation_id] = Identity(None, UNIDENTIFIED)
            continue
        owners = seen_on_linked.get(raw_key)
        if owners and len(owners) == 1:
            identities[c.conversation_id] = Identity(f"ec:{next(iter(owners))}", LOOKUP)
            stats["local_identifier_matches"] += 1
            continue
        pending.setdefault(raw_key, []).append(c)

    priority_ids = priority_ids or set()
    ordered_keys = sorted(
        pending,
        key=lambda k: (not any(c.conversation_id in priority_ids for c in pending[k]), k),
    )
    for raw_key in ordered_keys:
        matched: str | None = None
        target = lookup_target(raw_key)
        # An identifier linked to several different contacts is ambiguous —
        # "exactly one match" fails, so it stays a raw key.
        ambiguous = len(seen_on_linked.get(raw_key, ())) > 1
        if enable_lookup and lookup and target and not ambiguous:
            if stats["lookups_attempted"] >= max_lookups:
                stats["lookups_skipped_over_cap"] += 1
            else:
                stats["lookups_attempted"] += 1
                matched = lookup(*target)
                if matched:
                    stats["lookups_matched"] += 1
        for c in pending[raw_key]:
            identities[c.conversation_id] = (
                Identity(f"ec:{matched}", LOOKUP) if matched else Identity(raw_key, RAW_KEY)
            )
    return identities, stats


# ───────────────────────────── classification ─────────────────────────────

@dataclass(slots=True)
class _Cell:
    total: int = 0
    repeat: int = 0

    def add(self, is_repeat: bool) -> None:
        self.total += 1
        if is_repeat:
            self.repeat += 1


def rate_fields(total: int, repeat: int) -> dict[str, Any]:
    """Repeat rate + its inverse. ``None`` (not 0) when there were no contacts."""
    if total <= 0:
        return {"repeat_rate": None, "fcr": None, "repeat_rate_pct": None, "fcr_pct": None}
    rate = repeat / total
    return {
        "repeat_rate": round(rate, 4),
        "fcr": round(1 - rate, 4),
        "repeat_rate_pct": round(rate * 100, 2),
        "fcr_pct": round((1 - rate) * 100, 2),
    }


def _coverage(counts: dict[str, int], total: int) -> dict[str, dict[str, Any]]:
    return {
        method: {
            "count": counts.get(method, 0),
            "pct": round(counts.get(method, 0) / total * 100, 2) if total else None,
        }
        for method in METHODS
    }


def _attributed_queue(contact: Contact, queue_filter: set[str] | frozenset[str]) -> str | None:
    for queue_id in contact.queue_ids:
        if not queue_filter or queue_id in queue_filter:
            return queue_id
    return None


def build_report(
    contacts: list[Contact],
    identities: dict[str, Identity],
    *,
    period_start: date,
    period_end: date,
    windows_days: list[int],
    channels: Iterable[str] = CHANNELS,
    tz: ZoneInfo | str = "Australia/Sydney",
    queue_filter: set[str] | frozenset[str] = frozenset(),
    breakdown: Iterable[str] = (),
    drilldown: bool = False,
    drilldown_max_rows: int = 2000,
    low_coverage_warning_pct: float = 70.0,
    queue_name: Callable[[str], "str | None"] | None = None,
) -> dict[str, Any]:
    """Classify repeats and aggregate per window × channel.

    ``contacts`` must already be de-duplicated and scope-filtered, and must
    include the lookback slice; only contacts whose local date falls inside
    ``[period_start, period_end]`` are counted.
    """
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    windows = sorted(set(windows_days))
    wanted = set(channels)
    channel_list = [ch for ch in CHANNELS if ch in wanted]
    breakdown = set(breakdown)
    ordered = sorted(contacts, key=lambda c: (c.start, c.conversation_id))
    local_dates = {c.conversation_id: c.start.astimezone(zone).date() for c in ordered}

    results: list[dict[str, Any]] = []
    by_day_rows: list[dict[str, Any]] = []
    by_queue_rows: list[dict[str, Any]] = []
    drill_rows: list[dict[str, Any]] = []
    drill_truncated = False

    for channel in channel_list:
        totals = {w: _Cell() for w in windows}
        by_day: dict[tuple[int, date], _Cell] = {}
        by_queue: dict[tuple[int, str | None], _Cell] = {}
        method_counts: dict[str, int] = {}
        drill_counts = {w: 0 for w in windows}
        # Most recent prior per customer: it has the smallest day gap, so it
        # decides every window at once.
        last_seen: dict[str, Contact] = {}

        for contact in ordered:
            if channel != COMBINED and contact.channel != channel:
                continue
            identity = identities.get(contact.conversation_id) or Identity(None, UNIDENTIFIED)
            key = identity.customer_key
            local_date = local_dates[contact.conversation_id]
            prior = last_seen.get(key) if key else None
            if key:
                last_seen[key] = contact
            if not (period_start <= local_date <= period_end):
                continue  # lookback: matching only

            method_counts[identity.method] = method_counts.get(identity.method, 0) + 1
            gap = (local_date - local_dates[prior.conversation_id]).days if prior else None
            queue_id = _attributed_queue(contact, queue_filter)
            for window in windows:
                is_repeat = gap is not None and gap <= window
                totals[window].add(is_repeat)
                if "day" in breakdown:
                    by_day.setdefault((window, local_date), _Cell()).add(is_repeat)
                if "queue" in breakdown:
                    by_queue.setdefault((window, queue_id), _Cell()).add(is_repeat)
                if drilldown and is_repeat and prior is not None:
                    if drill_counts[window] >= drilldown_max_rows:
                        drill_truncated = True
                        continue
                    drill_counts[window] += 1
                    drill_rows.append({
                        "window_days": window,
                        "report_channel": channel,
                        "conversation_id": contact.conversation_id,
                        "conversation_start": iso_z(contact.start),
                        "channel": contact.channel,
                        "customer_key": key,
                        "identity_method": identity.method,
                        "prior_conversation_id": prior.conversation_id,
                        "prior_conversation_start": iso_z(prior.start),
                        "prior_channel": prior.channel,
                        "days_since_prior": gap,
                        "queue_id": queue_id,
                        "queue": queue_name(queue_id) if (queue_name and queue_id) else None,
                    })

        channel_total = sum(method_counts.values())
        coverage = _coverage(method_counts, channel_total)
        warnings: list[dict[str, str]] = []
        if channel == COMBINED and channel_total:
            ec_share = (method_counts.get(LINKED, 0) + method_counts.get(LOOKUP, 0)) / channel_total * 100
            if ec_share < low_coverage_warning_pct:
                warnings.append({
                    "code": "combined_rate_likely_understated",
                    "message": (
                        f"Only {ec_share:.1f}% of Combined contacts resolved to an External "
                        f"Contact (threshold {low_coverage_warning_pct:g}%). Raw identifiers "
                        "rarely link a call to a message, so the Combined repeat rate is "
                        "likely understated and Combined FCR overstated."
                    ),
                })

        for window in windows:
            cell = totals[window]
            results.append({
                "window_days": window,
                "channel": channel,
                "total_contacts": cell.total,
                "repeat_contacts": cell.repeat,
                **rate_fields(cell.total, cell.repeat),
                "fcr_label": FCR_LABEL,
                "unidentified_contacts": method_counts.get(UNIDENTIFIED, 0),
                "raw_key_contacts": method_counts.get(RAW_KEY, 0),
                "coverage": coverage,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "warnings": list(warnings),
            })

        if "day" in breakdown:
            day = period_start
            while day <= period_end:
                for window in windows:
                    cell = by_day.get((window, day), _Cell())
                    by_day_rows.append({
                        "date": day.isoformat(), "window_days": window, "channel": channel,
                        "total_contacts": cell.total, "repeat_contacts": cell.repeat,
                        **rate_fields(cell.total, cell.repeat),
                    })
                day += timedelta(days=1)
        if "queue" in breakdown:
            for (window, queue_id), cell in by_queue.items():
                by_queue_rows.append({
                    "queue_id": queue_id,
                    "queue": (queue_name(queue_id) if (queue_name and queue_id) else None)
                    or ("No queue" if queue_id is None else queue_id),
                    "window_days": window, "channel": channel,
                    "total_contacts": cell.total, "repeat_contacts": cell.repeat,
                    **rate_fields(cell.total, cell.repeat),
                })

    by_queue_rows.sort(key=lambda r: (r["channel"], r["window_days"], -r["total_contacts"], r["queue"] or ""))
    out: dict[str, Any] = {"results": results}
    if breakdown:
        out["breakdown"] = {}
        if "day" in breakdown:
            out["breakdown"]["by_day"] = by_day_rows
        if "queue" in breakdown:
            out["breakdown"]["by_queue"] = by_queue_rows
    if drilldown:
        out["drilldown"] = {
            "rows": drill_rows,
            "row_count": len(drill_rows),
            "max_rows_per_window_channel": drilldown_max_rows,
            "truncated": drill_truncated,
        }
    return out
