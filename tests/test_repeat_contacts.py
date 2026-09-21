"""Pin the Repeat Contacts & FCR methodology (v1.24).

Everything here targets the pure layer in ``genesys_mcp._repeat_contacts`` —
no tenant, no SDK. The rules under test are the report's definition, so a
change that breaks one of these is a methodology change, not a refactor:

- lookback contacts match but are never counted; the boundary is inclusive
  at exactly N local calendar days and exclusive at N + 1
- chains count every link; two contacts on one day make the second a repeat
- a contact never matches itself; duplicates collapse by conversationId
- Voice / Messaging match same-channel priors only; Combined matches either
- identity precedence: linked (canonical) → lookup → raw key → unidentified
- unidentified contacts sit in the denominator and are never repeats
- FCR is exactly 1 − repeat rate, and null (not 0 or 100) on no contacts
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from genesys_mcp._repeat_contacts import (
    COMBINED,
    FCR_LABEL,
    LINKED,
    LOOKUP,
    MESSAGING,
    RAW_KEY,
    UNIDENTIFIED,
    VOICE,
    Contact,
    Identity,
    build_report,
    dedupe_contacts,
    in_scope,
    normalise_phone,
    raw_customer_key,
    resolve_identities,
    slim_conversation,
)

SYD = ZoneInfo("Australia/Sydney")
PERIOD = (date(2026, 9, 1), date(2026, 9, 7))


def _c(cid, day, *, hour=10, channel=VOICE, ec=None, kind=None, value=None,
       queues=("q1",), handled=True, queued=True, month=9, year=2026):
    local = datetime(year, month, day, hour, 0, tzinfo=SYD)
    return Contact(
        conversation_id=cid, start=local.astimezone(timezone.utc), channel=channel,
        queue_ids=tuple(queues), handled=handled, queued=queued,
        external_contact_id=ec, raw_kind=kind, raw_value=value,
    )


def _run(contacts, *, windows=(7, 45), identities=None, **kwargs):
    contacts = dedupe_contacts(contacts)
    if identities is None:
        identities, _stats = resolve_identities(contacts)
    kwargs.setdefault("period_start", PERIOD[0])
    kwargs.setdefault("period_end", PERIOD[1])
    return build_report(contacts, identities, windows_days=list(windows), tz=SYD, **kwargs)


def _row(report, window, channel):
    return next(r for r in report["results"] if r["window_days"] == window and r["channel"] == channel)


# ───────────────────────────── lookback ─────────────────────────────

def test_lookback_contact_matches_but_is_never_counted():
    report = _run([
        _c("prior", 28, month=8, ec="A"),  # 4 days before a 1 Sep contact
        _c("cur", 1, ec="A"),
    ])
    row = _row(report, 7, VOICE)
    assert row["total_contacts"] == 1  # the lookback contact is not in the denominator
    assert row["repeat_contacts"] == 1


def test_window_boundary_is_inclusive_at_n_days_and_exclusive_after():
    report = _run([
        _c("p7", 25, month=8, ec="A"), _c("c7", 1, ec="A"),    # exactly 7 days
        _c("p8", 25, month=8, ec="B"), _c("c8", 2, ec="B"),    # 8 days
    ])
    assert _row(report, 7, VOICE)["repeat_contacts"] == 1
    assert _row(report, 45, VOICE)["repeat_contacts"] == 2


def test_gap_uses_local_calendar_days_not_elapsed_hours():
    # 23:00 on 25 Aug → 00:00 on 2 Sep is 7 days + 1 hour elapsed but 8 local
    # calendar days apart, so it is outside the 7-day window.
    report = _run([
        _c("p", 25, month=8, hour=23, ec="A"),
        _c("c", 2, hour=0, ec="A"),
    ])
    assert _row(report, 7, VOICE)["repeat_contacts"] == 0


def test_day_boundary_follows_the_configured_timezone():
    # 14:30Z on 31 Aug is already 00:30 on 1 Sep in Sydney → inside the period.
    contact = Contact("c", datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc), VOICE,
                      external_contact_id="A")
    assert _row(_run([contact]), 7, VOICE)["total_contacts"] == 1
    utc_report = build_report(
        [contact], {"c": Identity("ec:A", LINKED)}, period_start=PERIOD[0],
        period_end=PERIOD[1], windows_days=[7], tz="UTC",
    )
    assert _row(utc_report, 7, VOICE)["total_contacts"] == 0


# ───────────────────────────── chains / same day / self ─────────────────────────────

def test_chain_counts_each_link():
    report = _run([_c("a", 1, ec="A"), _c("b", 3, ec="A"), _c("c", 5, ec="A")])
    row = _row(report, 7, VOICE)
    assert (row["total_contacts"], row["repeat_contacts"]) == (3, 2)


def test_second_contact_on_the_same_day_is_a_repeat():
    report = _run([_c("am", 2, hour=9, ec="A"), _c("pm", 2, hour=15, ec="A")], drilldown=True)
    assert _row(report, 7, VOICE)["repeat_contacts"] == 1
    drill = [r for r in report["drilldown"]["rows"] if r["window_days"] == 7 and r["report_channel"] == VOICE]
    assert drill[0]["conversation_id"] == "pm"
    assert drill[0]["prior_conversation_id"] == "am"
    assert drill[0]["days_since_prior"] == 0


def test_a_contact_never_matches_itself_and_duplicates_collapse():
    report = _run([_c("same", 2, ec="A"), _c("same", 2, ec="A")])
    row = _row(report, 7, VOICE)
    assert (row["total_contacts"], row["repeat_contacts"]) == (1, 0)


# ───────────────────────────── channels ─────────────────────────────

def test_cross_channel_prior_only_counts_in_combined():
    report = _run([
        _c("call", 1, channel=VOICE, ec="A"),
        _c("msg", 3, channel=MESSAGING, ec="A"),
    ], drilldown=True)
    assert _row(report, 7, VOICE)["repeat_contacts"] == 0
    assert _row(report, 7, MESSAGING)["repeat_contacts"] == 0
    combined = _row(report, 7, COMBINED)
    assert (combined["total_contacts"], combined["repeat_contacts"]) == (2, 1)
    drill = next(r for r in report["drilldown"]["rows"] if r["report_channel"] == COMBINED and r["window_days"] == 7)
    assert (drill["channel"], drill["prior_channel"]) == (MESSAGING, VOICE)


def test_single_channel_skips_an_intervening_contact_on_the_other_channel():
    report = _run([
        _c("call1", 1, ec="A"),
        _c("msg", 2, channel=MESSAGING, ec="A"),
        _c("call2", 3, ec="A"),
    ], drilldown=True)
    voice = next(r for r in report["drilldown"]["rows"] if r["report_channel"] == VOICE and r["window_days"] == 7)
    assert voice["prior_conversation_id"] == "call1"
    combined = [r for r in report["drilldown"]["rows"] if r["report_channel"] == COMBINED and r["window_days"] == 7]
    assert {r["conversation_id"]: r["prior_conversation_id"] for r in combined} == {"msg": "call1", "call2": "msg"}


def test_channel_totals_split_and_sum_to_combined():
    report = _run([_c("v", 1, ec="A"), _c("m1", 1, channel=MESSAGING, ec="B"), _c("m2", 2, channel=MESSAGING, ec="C")])
    assert _row(report, 7, VOICE)["total_contacts"] == 1
    assert _row(report, 7, MESSAGING)["total_contacts"] == 2
    assert _row(report, 7, COMBINED)["total_contacts"] == 3


def test_requested_channels_limit_the_result_rows():
    report = _run([_c("v", 1, ec="A")], channels=[COMBINED], windows=(7,))
    assert [(r["window_days"], r["channel"]) for r in report["results"]] == [(7, COMBINED)]


# ───────────────────────────── identity ─────────────────────────────

def test_external_contact_link_wins_over_the_raw_identifier():
    contacts = [
        _c("a", 1, ec="A", kind="phone", value="tel:+61400000001"),
        _c("b", 2, ec="A", kind="phone", value="tel:+61400000999"),  # new number, same customer
    ]
    identities, _ = resolve_identities(contacts)
    assert identities["a"] == identities["b"] == Identity("ec:A", LINKED)
    assert _row(_run(contacts), 7, VOICE)["repeat_contacts"] == 1


def test_merged_contacts_resolve_to_the_canonical_id():
    contacts = [_c("a", 1, ec="old"), _c("b", 2, channel=MESSAGING, ec="survivor")]
    identities, stats = resolve_identities(
        contacts, canonical_resolver=lambda ids: {"old": "survivor", "survivor": "survivor"},
    )
    assert identities["a"].customer_key == identities["b"].customer_key == "ec:survivor"
    assert stats["external_contacts_merged"] == 1
    assert _row(_run(contacts, identities=identities), 7, COMBINED)["repeat_contacts"] == 1


def test_lookup_is_used_when_there_is_no_link():
    calls = []

    def lookup(kind, value):
        calls.append((kind, value))
        return "C9" if value == "+61400000002" else None

    contacts = [
        _c("a", 1, kind="phone", value="0400 000 002"),
        _c("b", 2, kind="phone", value="+61400000002"),
        _c("c", 2, kind="phone", value="+61400000003"),
    ]
    identities, stats = resolve_identities(contacts, lookup=lookup)
    assert identities["a"] == identities["b"] == Identity("ec:C9", LOOKUP)
    assert identities["c"] == Identity("phone:+61400000003", RAW_KEY)
    # one lookup per distinct normalised identifier, not per conversation
    assert sorted(calls) == [("phone", "+61400000002"), ("phone", "+61400000003")]
    assert (stats["lookups_attempted"], stats["lookups_matched"]) == (2, 1)


def test_identifier_seen_on_a_linked_contact_resolves_without_an_api_call():
    def lookup(_kind, _value):  # pragma: no cover - must not be reached
        raise AssertionError("no API lookup expected")

    contacts = [
        _c("linked", 1, ec="A", kind="phone", value="+61400000004"),
        _c("unlinked", 2, kind="phone", value="0400000004"),
    ]
    identities, stats = resolve_identities(contacts, lookup=lookup)
    assert identities["unlinked"] == Identity("ec:A", LOOKUP)
    assert stats["local_identifier_matches"] == 1


def test_identifier_linked_to_two_contacts_is_ambiguous_and_stays_raw():
    contacts = [
        _c("x", 1, ec="A", kind="phone", value="+61400000005"),
        _c("y", 1, ec="B", kind="phone", value="+61400000005"),
        _c("z", 2, kind="phone", value="+61400000005"),
    ]
    identities, stats = resolve_identities(contacts, lookup=lambda *_: "A")
    assert identities["z"] == Identity("phone:+61400000005", RAW_KEY)
    assert stats["lookups_attempted"] == 0


def test_lookup_toggle_and_raw_only_mode():
    contacts = [_c("a", 1, ec="A", kind="phone", value="+61400000006"),
                _c("b", 2, kind="phone", value="+61400000007")]
    lookup = lambda *_: "Z"  # noqa: E731

    off, _ = resolve_identities(contacts, enable_lookup=False, lookup=lookup)
    assert off["b"].method == RAW_KEY

    raw, _ = resolve_identities(contacts, mode="raw_only", lookup=lookup)
    assert raw["a"] == Identity("phone:+61400000006", RAW_KEY)
    assert raw["b"] == Identity("phone:+61400000007", RAW_KEY)


def test_lookup_cap_prioritises_period_contacts():
    contacts = [_c("lookback", 20, month=8, kind="phone", value="+61400000010"),
                _c("period", 2, kind="phone", value="+61400000011")]
    seen = []
    _identities, stats = resolve_identities(
        contacts, max_lookups=1, priority_ids={"period"},
        lookup=lambda kind, value: seen.append(value),
    )
    assert seen == ["+61400000011"]
    assert stats["lookups_skipped_over_cap"] == 1


def test_raw_keys_match_within_a_channel_but_web_messaging_cannot_link_to_voice():
    report = _run([
        _c("v1", 1, kind="phone", value="+61400000020"),
        _c("v2", 2, kind="phone", value="0400 000 020"),
        _c("m1", 3, channel=MESSAGING, kind="web_messaging_user", value="GUID-1"),
        _c("m2", 4, channel=MESSAGING, kind="web_messaging_user", value="guid-1"),
    ])
    assert _row(report, 7, VOICE)["repeat_contacts"] == 1
    assert _row(report, 7, MESSAGING)["repeat_contacts"] == 1
    combined = _row(report, 7, COMBINED)
    assert combined["repeat_contacts"] == 2  # no call→message link without an External Contact
    assert combined["raw_key_contacts"] == 4


def test_unidentified_contacts_are_in_the_denominator_and_never_repeat():
    report = _run([
        _c("anon1", 1, kind="phone", value="sip:Private@carrier.example"),
        _c("anon2", 2, kind="phone", value="sip:Private@carrier.example"),
        _c("none", 3),
        _c("known", 3, ec="A"),
    ])
    row = _row(report, 7, VOICE)
    assert (row["total_contacts"], row["repeat_contacts"], row["unidentified_contacts"]) == (4, 0, 3)
    assert row["coverage"][UNIDENTIFIED] == {"count": 3, "pct": 75.0}
    assert row["coverage"][LINKED] == {"count": 1, "pct": 25.0}


def test_coverage_is_per_channel_and_counts_period_contacts_only():
    report = _run([
        _c("lb", 20, month=8, ec="A"),
        _c("v", 1, ec="A"),
        _c("m", 1, channel=MESSAGING, kind="web_messaging_user", value="g"),
    ])
    assert _row(report, 7, VOICE)["coverage"][LINKED]["count"] == 1
    assert _row(report, 7, MESSAGING)["coverage"][RAW_KEY] == {"count": 1, "pct": 100.0}
    combined = _row(report, 7, COMBINED)["coverage"]
    assert (combined[LINKED]["pct"], combined[RAW_KEY]["pct"]) == (50.0, 50.0)


def test_low_external_contact_share_flags_combined_only():
    contacts = [_c("v", 1, kind="phone", value="+61400000030"),
                _c("m", 1, channel=MESSAGING, kind="web_messaging_user", value="g")]
    report = _run(contacts)
    assert [w["code"] for w in _row(report, 7, COMBINED)["warnings"]] == ["combined_rate_likely_understated"]
    assert _row(report, 7, VOICE)["warnings"] == []
    healthy = _run([_c("v", 1, ec="A"), _c("m", 1, channel=MESSAGING, ec="B")])
    assert _row(healthy, 7, COMBINED)["warnings"] == []


# ───────────────────────────── rates ─────────────────────────────

def test_fcr_is_the_exact_inverse_of_the_repeat_rate():
    report = _run([_c("a", 1, ec="A"), _c("b", 2, ec="A"), _c("c", 3, ec="B")])
    for row in report["results"]:
        if row["total_contacts"]:
            assert row["repeat_rate"] + row["fcr"] == pytest.approx(1.0)
            assert row["repeat_rate_pct"] + row["fcr_pct"] == pytest.approx(100.0)
        assert row["fcr_label"] == FCR_LABEL == "FCR (Amaysim methodology)"
    voice = _row(report, 7, VOICE)
    assert (voice["repeat_rate"], voice["fcr"]) == (0.3333, 0.6667)


def test_no_contacts_gives_null_rates_not_a_perfect_fcr():
    row = _row(_run([]), 7, MESSAGING)
    assert row["total_contacts"] == 0
    assert row["repeat_rate"] is None and row["fcr"] is None


# ───────────────────────────── breakdown / drilldown / scope ─────────────────────────────

def test_breakdown_by_day_covers_every_day_and_by_queue_attributes_first_queue():
    report = _run(
        [_c("a", 1, ec="A", queues=("q1", "q2")), _c("b", 2, ec="A", queues=("q2",))],
        windows=(7,), channels=[VOICE], breakdown=["day", "queue"],
        queue_name={"q1": "Billing", "q2": "Support"}.get,
    )
    days = report["breakdown"]["by_day"]
    assert [d["date"] for d in days] == [f"2026-09-0{n}" for n in range(1, 8)]
    assert days[1]["repeat_contacts"] == 1 and days[2]["repeat_rate"] is None
    queues = {q["queue"]: q for q in report["breakdown"]["by_queue"]}
    assert queues["Billing"]["repeat_contacts"] == 0
    assert (queues["Support"]["total_contacts"], queues["Support"]["repeat_contacts"]) == (1, 1)


def test_drilldown_is_capped_per_window_and_channel():
    contacts = [_c(f"c{n}", 1, hour=n, ec="A") for n in range(6)]
    report = _run(contacts, windows=(7,), channels=[VOICE], drilldown=True, drilldown_max_rows=2)
    assert report["drilldown"]["row_count"] == 2
    assert report["drilldown"]["truncated"] is True
    assert _row(report, 7, VOICE)["repeat_contacts"] == 5  # totals are never capped


def test_scope_applies_to_prior_contacts_too():
    abandoned = _c("abandoned", 1, ec="A", handled=False, queued=True)
    pre_queue = _c("ivr", 1, ec="A", handled=False, queued=False, queues=())
    other_queue = _c("other", 1, ec="A", queues=("q9",))
    assert not in_scope(abandoned, include_abandoned=False, include_pre_queue=False)
    assert in_scope(abandoned, include_abandoned=True, include_pre_queue=False)
    assert not in_scope(pre_queue, include_abandoned=True, include_pre_queue=False)
    assert in_scope(pre_queue, include_abandoned=False, include_pre_queue=True)
    assert not in_scope(other_queue, include_abandoned=True, include_pre_queue=True, queue_filter={"q1"})

    later = _c("later", 2, ec="A")
    scoped = [c for c in (abandoned, later) if in_scope(c, include_abandoned=False, include_pre_queue=False)]
    assert _row(_run(scoped), 7, VOICE)["repeat_contacts"] == 0


# ───────────────────────────── normalisation / slimming ─────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("tel:+61412345678", "+61412345678"),
    ("0412 345 678", "+61412345678"),
    ("(02) 9876 5432", "+61298765432"),
    ("61412345678", "+61412345678"),
    ("412345678", "+61412345678"),
    ("0011 44 20 7946 0958", "+442079460958"),
    ("sip:+61412345678@trunk.example;user=phone", "+61412345678"),
    ("sip:Private@carrier.example", None),
    ("anonymous", None),
    ("", None),
    (None, None),
])
def test_normalise_phone_au(raw, expected):
    assert normalise_phone(raw, "AU") == expected


def test_normalise_phone_respects_default_country():
    assert normalise_phone("(415) 555-0100", "US") == "+14155550100"
    assert normalise_phone("020 7946 0958", "GB") == "+442079460958"


def test_messaging_keys_gate_raw_identifiers_and_sms_shares_the_phone_namespace():
    sms = _c("s", 1, channel=MESSAGING, kind="sms", value="+61400000040")
    web = _c("w", 1, channel=MESSAGING, kind="web_messaging_user", value="G-1")
    mail = _c("e", 1, channel=MESSAGING, kind="email", value="Jane@Example.COM")
    assert raw_customer_key(sms) == "phone:+61400000040"
    assert raw_customer_key(mail) == "email:jane@example.com"
    assert raw_customer_key(web, messaging_keys=["email", "sms"]) is None
    assert raw_customer_key(sms, messaging_keys=["email"]) is None


def _conversation(**overrides):
    conv = {
        "conversationId": "conv-1",
        "conversationStart": "2026-09-01T01:00:00.000Z",
        "participants": [
            {"purpose": "external", "externalContactId": "EC-1", "sessions": [{
                "mediaType": "voice", "direction": "inbound", "ani": "tel:+61412345678",
                "segments": [{"segmentStart": "2026-09-01T01:00:00.000Z", "segmentType": "interact"}],
            }]},
            {"purpose": "acd", "sessions": [{"mediaType": "voice", "segments": [
                {"segmentStart": "2026-09-01T01:00:20.000Z", "segmentType": "interact", "queueId": "q-billing"},
            ]}]},
            {"purpose": "agent", "userId": "u1", "sessions": [{"mediaType": "voice", "segments": [
                {"segmentStart": "2026-09-01T01:00:40.000Z", "segmentType": "interact", "queueId": "q-billing"},
                {"segmentStart": "2026-09-01T01:05:00.000Z", "segmentType": "wrapup", "queueId": "q-billing"},
            ]}]},
        ],
    }
    conv.update(overrides)
    return conv


def test_slim_conversation_counts_one_contact_however_many_legs():
    contact = slim_conversation(_conversation())
    assert contact == Contact(
        conversation_id="conv-1", start=datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc),
        channel=VOICE, queue_ids=("q-billing",), handled=True, queued=True,
        external_contact_id="EC-1", raw_kind="phone", raw_value="tel:+61412345678",
    )
    assert Contact.from_record(contact.to_record()) == contact


def test_slim_conversation_flags_abandoned_pre_queue_and_messaging():
    conv = _conversation()
    conv["participants"] = conv["participants"][:2]  # queued, no agent
    abandoned = slim_conversation(conv)
    assert (abandoned.queued, abandoned.handled) == (True, False)

    conv["participants"] = conv["participants"][:1]  # never left the IVR
    ivr = slim_conversation(conv)
    assert (ivr.queued, ivr.handled, ivr.queue_ids) == (False, False, ())

    message = slim_conversation({
        "conversationId": "m-1", "conversationStart": "2026-09-01T02:00:00.000Z",
        "participants": [{"purpose": "customer", "sessions": [{
            "mediaType": "message", "messageType": "webmessaging", "direction": "inbound",
            "addressFrom": "abc-guid", "segments": [],
        }]}],
    })
    assert (message.channel, message.raw_kind, message.raw_value) == (MESSAGING, "web_messaging_user", "abc-guid")


def test_slim_conversation_ignores_unreported_media_and_bad_rows():
    assert slim_conversation({"conversationId": "x"}) is None
    email_only = {"conversationId": "e", "conversationStart": "2026-09-01T02:00:00.000Z",
                  "participants": [{"purpose": "customer", "sessions": [{"mediaType": "email"}]}]}
    assert slim_conversation(email_only) is None
