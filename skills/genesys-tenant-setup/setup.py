#!/usr/bin/env python3
"""skills/genesys-tenant-setup/setup.py — auto-discover tenant config from a Genesys Cloud tenant.

Run via the genesys-tenant-setup skill (which conducts the interview around
this script's discovery output) or directly:

    # Probe the tenant and print the discovery JSON to stdout
    python skills/genesys-tenant-setup/setup.py --discover

    # Write a YAML draft (with comments) to a path
    python skills/genesys-tenant-setup/setup.py --discover --draft /tmp/draft.yaml

    # Save a fully-resolved tenant config (after the interview fills in the gaps)
    python skills/genesys-tenant-setup/setup.py --save --config-json '{...}'

The discovery probes (via the read-only OAuth client) cover:

- Routing queues — infers `name_pattern` (2-segment vs 3-segment), brand list,
  function list, channel list (for 3-segment), and skip-substring suggestions
  from real queue names. This is the bit that prevents the Members-vs-Members-Mobile
  and pattern-arity bugs we hit migrating Lawrence's config.
- Management units — lists candidates with member-count and business-unit so the
  interviewer can pick the customer-facing one(s).
- Pre-break / drain presence — fuzzy name match on org-level presences.
- Active users — title histogram so the interviewer can confirm the
  specialist-role list.

Anything that requires policy judgement (tenant display name, AHT targets,
output filename pattern) is left for the interview prompts in SKILL.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Make src/ importable so we can use the shared TenantConfig loader without
# requiring an editable install. Mirrors the pattern used by build_report.py
# and scripts/provision_users.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

import PureCloudPlatformClientV2 as gc  # noqa: E402
from PureCloudPlatformClientV2.rest import ApiException  # noqa: E402

from genesys_mcp.client import (  # noqa: E402
    GenesysConfigError,
    init_api,
    with_retry_for,
)
from genesys_mcp.tenant import TenantConfig, dump_config, default_config_path  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Multi-file env loading (mirrors scripts/provision_users.py so the script
# works whether the user keeps creds in .env / .env.write / ~/.config/).
# ─────────────────────────────────────────────────────────────────────────────

ENV_FILES = (
    _REPO_ROOT / ".env.write",
    _REPO_ROOT / ".env",
    Path.home() / ".config" / "genesys-mcp.env",
)


def load_dotenv_files(paths: tuple[Path, ...]) -> list[Path]:
    loaded: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        loaded.append(path)
    return loaded


# ─────────────────────────────────────────────────────────────────────────────
# Probes
# ─────────────────────────────────────────────────────────────────────────────

# Substrings that, when present in the LAST segment of a queue name, almost
# always indicate a non-customer-facing queue. Suggested as defaults; the
# interviewer can add/remove based on what the tenant actually uses.
DEFAULT_SKIP_SUBSTRINGS = (
    "Holding", "Internal", "Jira", "Outbound Email", "Documents",
    "Supervisor", "ZZZ_", "Test", "test", "TIO",
)

# Function-name candidates that suggest a queue is customer-facing. Any
# queue whose last segment matches one of these (case-insensitive) is treated
# as a real customer-facing queue when inferring pattern + brands.
COMMON_FUNCTIONS = {
    "activation", "billing", "complaints", "general", "retention", "sales",
    "technical support", "tech support", "support", "service", "enquiries",
    "inquiries", "callback", "callbacks",
}

# Channel-name candidates that suggest a 3-segment pattern.
COMMON_CHANNELS = {"voice", "chat", "messaging", "message", "email", "callback", "sms"}

# Separators to probe for queue-name parsing. Order matters — try longer
# delimiters first so " - " wins over "-".
QUEUE_SEPARATORS = (" - ", " / ", " | ", " :: ", "_", ":")

# Country code → IANA timezone mapping. Sensible defaults for the most
# common Genesys regions; multi-region tenants override in tenant.yaml.
COUNTRY_TIMEZONES = {
    "AU": "Australia/Sydney",
    "NZ": "Pacific/Auckland",
    "US": "America/New_York",
    "CA": "America/Toronto",
    "GB": "Europe/London",
    "UK": "Europe/London",
    "IE": "Europe/Dublin",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "ES": "Europe/Madrid",
    "IT": "Europe/Rome",
    "NL": "Europe/Amsterdam",
    "BR": "America/Sao_Paulo",
    "MX": "America/Mexico_City",
    "JP": "Asia/Tokyo",
    "IN": "Asia/Kolkata",
    "SG": "Asia/Singapore",
    "HK": "Asia/Hong_Kong",
    "PH": "Asia/Manila",
    "ZA": "Africa/Johannesburg",
    "AE": "Asia/Dubai",
}

# Pre-break presence keyword set covering common English + EU translations.
# The probe iterates ALL language_labels on each presence (not just en_US)
# and matches any of these — handles tenants with non-English primary locales.
PRE_BREAK_KEYWORDS = re.compile(
    r"pre[\s_-]?break|drain|wind[\s_-]?down|"
    r"pr[ée][\s_-]?pause|avant[\s_-]?pause|"   # FR
    r"vor[\s_-]?pause|"                          # DE
    r"prepausa|antes[\s_-]?de[\s_-]?la[\s_-]?pausa",  # ES
    re.I,
)


def _retry_read():
    return with_retry_for(init_api)


def _list_all(call: Any, *, page_size: int = 200, max_pages: int = 50) -> list:
    """Generic paginator for any Genesys list endpoint that takes (page_size, page_number)."""
    out: list = []
    retry = _retry_read()
    for page in range(1, max_pages + 1):
        resp = retry(lambda: call(page_size=page_size, page_number=page))()
        entities = getattr(resp, "entities", None) or []
        out.extend(entities)
        if len(entities) < page_size:
            break
    return out


def _detect_separator(queue_names: list[str]) -> tuple[str, float]:
    """Pick the dominant separator from a list of queue names.

    Returns (separator, confidence). Confidence is the fraction of names that
    split into 2+ non-empty segments using the chosen separator. Defaults to
    ``" - "`` if no separator scores higher than 30%.
    """
    best_sep = " - "
    best_score = 0.0
    for sep in QUEUE_SEPARATORS:
        if not queue_names:
            continue
        hits = 0
        for name in queue_names:
            parts = [p.strip() for p in name.split(sep)]
            if len(parts) >= 2 and all(parts):
                hits += 1
        score = hits / len(queue_names)
        if score > best_score:
            best_score = score
            best_sep = sep
    return best_sep, best_score


def probe_queues(api: gc.ApiClient) -> dict:
    """Discover the queue-naming convention from real queue names.

    Returns a dict with:
      - detected_separator: e.g. " - ", "_", " / " — auto-detected from real
        queue names; no longer hardcoded to " - "
      - separator_confidence: fraction of queue names that split cleanly on
        the chosen separator
      - detected_pattern: "{brand} - {function}" or "{brand} - {channel} - {function}",
        with the detected separator interpolated
      - pattern_confidence: "high" | "medium" | "low"
      - brands, functions, channels, skip_substring_suggestions, matched_queues,
        skipped_queues_sample, all_queues — same as v0.5
    """
    routing_api = gc.RoutingApi(api)
    queues = _list_all(routing_api.get_routing_queues, page_size=200)
    queue_dicts = [{"id": q.id, "name": q.name, "member_count": q.member_count} for q in queues]

    # Pass 0: detect the dominant separator (was hardcoded to " - " pre-v0.6).
    sep, sep_confidence = _detect_separator([q["name"] for q in queue_dicts])

    # Pass 1: split each name on the detected separator and bucket by segment count.
    segments_by_count: dict[int, list[list[str]]] = defaultdict(list)
    for q in queue_dicts:
        parts = [p.strip() for p in q["name"].split(sep)]
        if all(parts) and len(parts) > 1:
            segments_by_count[len(parts)].append(parts)

    # Modal segment count = inferred pattern length.
    if not segments_by_count:
        return {
            "all_queues": queue_dicts,
            "detected_separator": sep,
            "separator_confidence": round(sep_confidence, 2),
            "detected_pattern": None,
            "pattern_confidence": "none",
            "brands": [],
            "functions": [],
            "channels": [],
            "skip_substring_suggestions": list(DEFAULT_SKIP_SUBSTRINGS),
            "matched_queues": 0,
            "skipped_queues_sample": [q["name"] for q in queue_dicts[:5]],
            "note": (
                f"No queue names matched a delimiter-separated shape with "
                f"separator {sep!r} — tenant may use a custom convention; "
                f"set queues.name_pattern manually in tenant.yaml."
            ),
        }

    seg_count = max(segments_by_count.keys(), key=lambda k: len(segments_by_count[k]))
    parts_list = segments_by_count[seg_count]

    # Confidence: if the modal count covers 80%+ of dashed queues, "high".
    total_dashed = sum(len(v) for v in segments_by_count.values())
    coverage = len(parts_list) / total_dashed if total_dashed else 0
    confidence = "high" if coverage >= 0.8 else ("medium" if coverage >= 0.5 else "low")

    # Pass 2: build brand/function (and channel) histograms from the modal-count slice.
    if seg_count == 2:
        # {brand} - {function}
        brand_for_function: dict[str, set[str]] = defaultdict(set)
        function_for_brand: dict[str, set[str]] = defaultdict(set)
        for parts in parts_list:
            brand, function = parts[0], parts[1]
            brand_for_function[function].add(brand)
            function_for_brand[brand].add(function)

        # A "real" function appears with multiple brands AND isn't an
        # internal-queue label. A "real" brand has at least one matching
        # function. This filters out one-off rows like "Fastter - TIO" and
        # internal-only labels like "Holding" / "Supervisor" that happen to
        # appear with multiple brands but should never be reported on.
        skip_set = set(DEFAULT_SKIP_SUBSTRINGS)
        def _is_real_function(f: str) -> bool:
            if f in skip_set:
                return False
            if any(sub in f for sub in DEFAULT_SKIP_SUBSTRINGS):
                return False
            return len(brand_for_function[f]) >= 2 or f.lower() in COMMON_FUNCTIONS

        valid_functions = {f for f in brand_for_function if _is_real_function(f)}
        valid_brands = {
            b for b, fns in function_for_brand.items() if fns & valid_functions
        }
        detected_pattern = f"{{brand}}{sep}{{function}}"
        channels: list[str] = []
        functions = sorted(valid_functions)
        brands = sorted(valid_brands)
    else:
        # {brand} - {channel} - {function} (3-seg) or longer.
        # Treat segment 0 as brand, segment -1 as function, segment 1 as channel.
        brand_set: set[str] = set()
        channel_set: set[str] = set()
        function_set: set[str] = set()
        for parts in parts_list:
            brand_set.add(parts[0])
            channel_set.add(parts[1])
            function_set.add(parts[-1])
        # Filter to plausible candidates.
        functions = sorted(f for f in function_set if len(f) > 2 and len(f) < 40)
        brands = sorted(b for b in brand_set if len(b) > 1 and len(b) < 40)
        channels = sorted(
            c for c in channel_set
            if c.lower() in COMMON_CHANNELS or len(c) <= 12
        )
        detected_pattern = f"{{brand}}{sep}{{channel}}{sep}{{function}}"

    # Pass 3: build skip-substring suggestions by looking at queue names that
    # didn't match the pattern OR have non-function last segments.
    skipped_names: list[str] = []
    suggested_skip: set[str] = set()
    for q in queue_dicts:
        parts = [p.strip() for p in q["name"].split(sep)]
        skip = False
        if len(parts) != seg_count:
            skip = True
        elif seg_count == 2 and parts[1] not in functions:
            skip = True
        for sub in DEFAULT_SKIP_SUBSTRINGS:
            if sub in q["name"]:
                suggested_skip.add(sub)
                skip = True
                break
        if skip:
            skipped_names.append(q["name"])

    matched = len(queue_dicts) - len(skipped_names)

    return {
        "all_queues": queue_dicts,
        "total_count": len(queue_dicts),
        "detected_separator": sep,
        "separator_confidence": round(sep_confidence, 2),
        "detected_pattern": detected_pattern,
        "pattern_confidence": confidence,
        "brands": brands,
        "functions": functions,
        "channels": channels,
        "skip_substring_suggestions": sorted(suggested_skip) or list(DEFAULT_SKIP_SUBSTRINGS),
        "matched_queues": matched,
        "skipped_queues_sample": skipped_names[:5],
    }


def probe_management_units(api: gc.ApiClient) -> list[dict]:
    """List WFM management units with their business unit + member count.

    Returns [] (with note) if the read-only client doesn't have WFM read perms.
    """
    wfm_api = gc.WorkforceManagementApi(api)
    retry = _retry_read()
    try:
        # The MU list endpoint is non-paginated for most tenants but may grow.
        resp = retry(lambda: wfm_api.get_workforcemanagement_managementunits(page_size=200))()
        units = []
        for mu in resp.entities or []:
            bu = getattr(mu, "business_unit", None)
            units.append({
                "id": mu.id,
                "name": mu.name,
                "business_unit_id": bu.id if bu else None,
                "business_unit_name": getattr(bu, "name", None) if bu else None,
            })
        return units
    except ApiException as exc:
        if exc.status == 403:
            return [{"_error": "no WFM read permission on this OAuth client"}]
        raise


def probe_pre_break_presence(api: gc.ApiClient) -> list[dict]:
    """Find candidates for the org-level "Pre Break" / drain presence.

    Strategy: list presence definitions, filter for ones whose primary label
    contains break/drain/wind-down keywords. Returns [] gracefully if the
    read-only client doesn't have presence:presenceDefinition:view.
    """
    presence_api = gc.PresenceApi(api)
    retry = _retry_read()
    # `get_presence_definitions` is non-paginated and takes `locale_code="ALL"`
    # to populate language_labels. Falls back to `get_presencedefinitions`
    # (older endpoint) if the new one isn't available on the OAuth role.
    try:
        resp = retry(lambda: presence_api.get_presence_definitions(locale_code="ALL"))()
    except ApiException as exc:
        if exc.status == 403:
            return [{"_error": "no presence:presenceDefinition:view on this OAuth client"}]
        # Try the older endpoint as a fallback (different perm gating in some tenants).
        try:
            resp = retry(lambda: presence_api.get_presencedefinitions())()
        except ApiException:
            raise exc

    # v0.6: match the PRE_BREAK_KEYWORDS regex against EVERY language_label
    # on each presence (not just en_US). Catches tenants with non-English
    # primary locales whose pre-break presence is labelled in their language.
    candidates = []
    for p in resp.entities or []:
        labels = getattr(p, "language_labels", None) or {}
        # Pull all label strings to check, plus the older .name field.
        label_pool: list[str] = list(labels.values())
        name_attr = getattr(p, "name", None)
        if name_attr:
            label_pool.append(name_attr)
        # Find the first label that matches; surface a non-empty primary
        # label for the interview prompt.
        matched_label = next(
            (lbl for lbl in label_pool if lbl and PRE_BREAK_KEYWORDS.search(lbl)),
            None,
        )
        if matched_label is None:
            continue
        primary_label = (
            labels.get("en_US") or labels.get("en-US")
            or labels.get("en_AU") or labels.get("en_GB")
            or matched_label or name_attr or ""
        )
        system = getattr(p, "system_presence", None)
        candidates.append({
            "id": p.id,
            "label": primary_label,
            "matched_label": matched_label,
            "all_labels": dict(labels),
            "system_presence": system,
        })
    return candidates


def probe_users(api: gc.ApiClient) -> dict:
    """List active users, group by title for specialist-role inference."""
    users_api = gc.UsersApi(api)
    retry = _retry_read()
    users: list[dict] = []
    for page in range(1, 21):  # cap at 4000 users
        resp = retry(lambda: users_api.get_users(state="active", page_size=200, page_number=page))()
        for u in resp.entities or []:
            users.append({
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "title": u.title,
                "department": u.department,
            })
        if len(resp.entities or []) < 200:
            break

    title_counts = Counter(u["title"] for u in users if u["title"])
    # Heuristic: titles containing "Specialist" or matching common defaults.
    suggested = sorted(
        t for t in title_counts
        if "specialist" in t.lower() or t.lower() in {"agent", "csr", "consultant"}
    )
    if not suggested:
        # Fall back: any title that has 5+ holders and isn't TL/Manager-shaped.
        suggested = sorted(
            t for t, c in title_counts.items()
            if c >= 5 and not re.search(r"team leader|manager|tl\b|head of", t, re.I)
        )
    return {
        "total_active": len(users),
        "title_counts": dict(title_counts.most_common()),
        "suggested_specialist_titles": suggested,
        "sample_users": users[:10],
    }


def probe_organisation(api: gc.ApiClient) -> dict:
    """Pull /organizations/me and map defaultCountryCode to a sensible timezone.

    Returns ``{country_code, suggested_timezone, organization_name}`` plus a
    ``timezone_confidence`` of 'high' (mapping known), 'low' (country unmapped,
    falling back to UTC). Multi-region tenants override in tenant.yaml.
    """
    org_api = gc.OrganizationApi(api)
    retry = _retry_read()
    try:
        org = retry(lambda: org_api.get_organizations_me())()
    except ApiException as exc:
        return {"_error": f"organisation read failed ({exc.status}): {exc.reason}"}

    country = getattr(org, "default_country_code", None) or ""
    country = country.upper()
    suggested_tz = COUNTRY_TIMEZONES.get(country, "UTC")
    return {
        "organization_name": getattr(org, "name", None),
        "country_code": country or None,
        "default_language": getattr(org, "default_language", None),
        "suggested_timezone": suggested_tz,
        "timezone_confidence": "high" if suggested_tz != "UTC" else "low",
    }


def probe_aht_baselines(
    api: gc.ApiClient, specialist_user_ids: list[str], days: int = 60,
) -> dict:
    """Pull per-user voice/message AHT for the last ``days`` days.

    For each media type (voice, message), computes:
      - team_aht_s: sum(tHandle) / sum(tAnswered) across all users
      - per_user_p10 / p25 / p50 / p75 / p90: percentiles of per-user AHT,
        filtered to users with enough volume to matter (>=20 answered)
      - suggested_target_s: p25 of per-user AHT (a target equal to current
        median would be no target at all; p25 is "top-performer median")

    Returns ``{"voice": {...}, "message": {...}, "acw": {...}}``. ACW only
    aggregates voice (message ACW is non-standard).

    Soft-fails if the user list is empty or analytics scope is missing.
    """
    if not specialist_user_ids:
        return {"_error": "no specialist user ids — probe_users must run first"}

    aapi = gc.AnalyticsApi(api)
    retry = _retry_read()

    # Build a UTC interval ending now, going back `days` days.
    from datetime import datetime, timedelta, timezone as _tz
    end = datetime.now(_tz.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    interval = f"{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}/{end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"

    body = {
        "interval": interval,
        "groupBy": ["userId", "mediaType"],
        "filter": {
            "type": "and",
            "clauses": [
                {"type": "or", "predicates": [
                    {"dimension": "userId", "value": uid}
                    for uid in specialist_user_ids
                ]},
                {"type": "or", "predicates": [
                    {"dimension": "mediaType", "value": m}
                    for m in ("voice", "message")
                ]},
            ],
        },
        "metrics": ["tAnswered", "tHandle", "tAcw"],
    }

    try:
        resp = retry(lambda: aapi.post_analytics_conversations_aggregates_query(body))()
    except ApiException as exc:
        if exc.status == 403:
            return {"_error": "no analytics read permission"}
        return {"_error": f"AHT baseline query failed ({exc.status}): {exc.reason}"}

    # Flatten per-user per-media stats.
    per_user_media: dict[tuple[str, str], dict] = {}
    serializer = api.sanitize_for_serialization
    resp_d = serializer(resp)
    for r in resp_d.get("results") or []:
        uid = r["group"].get("userId")
        media = r["group"].get("mediaType")
        if not uid or not media:
            continue
        stats = {}
        for bucket in r.get("data") or []:
            for m in bucket.get("metrics") or []:
                stats[m["metric"]] = m.get("stats", {})
        per_user_media[(uid, media)] = stats

    def _aht_p(media: str) -> dict:
        per_user_aht_s: list[float] = []
        sum_handle_ms = 0.0
        sum_answered = 0.0
        for (uid, m), stats in per_user_media.items():
            if m != media:
                continue
            answered = float(stats.get("tAnswered", {}).get("count", 0) or 0)
            handle_ms = float(stats.get("tHandle", {}).get("sum", 0) or 0)
            sum_handle_ms += handle_ms
            sum_answered += answered
            if answered >= 20:
                per_user_aht_s.append((handle_ms / 1000.0) / answered)
        team_aht_s = (sum_handle_ms / 1000.0) / sum_answered if sum_answered else None
        result = {
            "team_aht_s": round(team_aht_s, 1) if team_aht_s else None,
            "users_with_volume": len(per_user_aht_s),
            "sample_size_threshold": 20,
        }
        if per_user_aht_s:
            per_user_aht_s.sort()
            n = len(per_user_aht_s)
            def _pct(p: float) -> float:
                idx = max(0, min(n - 1, int(round(p * (n - 1)))))
                return round(per_user_aht_s[idx], 1)
            result.update({
                "p10_aht_s": _pct(0.10),
                "p25_aht_s": _pct(0.25),
                "p50_aht_s": _pct(0.50),
                "p75_aht_s": _pct(0.75),
                "p90_aht_s": _pct(0.90),
                "suggested_target_s": _pct(0.25),
            })
        return result

    def _acw_p() -> dict:
        per_user_acw_s: list[float] = []
        for (uid, m), stats in per_user_media.items():
            if m != "voice":
                continue
            answered = float(stats.get("tAnswered", {}).get("count", 0) or 0)
            acw_ms = float(stats.get("tAcw", {}).get("sum", 0) or 0)
            if answered >= 20:
                per_user_acw_s.append((acw_ms / 1000.0) / answered)
        result = {"users_with_volume": len(per_user_acw_s)}
        if per_user_acw_s:
            per_user_acw_s.sort()
            n = len(per_user_acw_s)
            def _pct(p):
                idx = max(0, min(n - 1, int(round(p * (n - 1)))))
                return round(per_user_acw_s[idx], 1)
            result.update({
                "p25_acw_s": _pct(0.25),
                "p50_acw_s": _pct(0.50),
                "p75_acw_s": _pct(0.75),
                "suggested_target_s": _pct(0.25),
            })
        return result

    return {
        "interval_days": days,
        "voice": _aht_p("voice"),
        "message": _aht_p("message"),
        "acw_voice": _acw_p(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Drafting + saving
# ─────────────────────────────────────────────────────────────────────────────

def build_draft(discovery: dict) -> dict:
    """Assemble a draft tenant.yaml dict from discovery output.

    Fields that the interview must fill in (tenant.name, tenant.short_name,
    targets) get placeholder values flagged with a leading `__SETUP__` so the
    interviewer can spot them.
    """
    q = discovery["queues"]
    pre_break_id = None
    if discovery["pre_break_candidates"] and not discovery["pre_break_candidates"][0].get("_error"):
        pre_break_id = discovery["pre_break_candidates"][0]["id"]

    mu_ids = [
        mu["id"] for mu in discovery["management_units"]
        if not mu.get("_error")
    ]
    bu_id = None
    for mu in discovery["management_units"]:
        if mu.get("business_unit_id"):
            bu_id = mu["business_unit_id"]
            break

    # Pull AHT auto-suggestions if probe_aht_baselines ran successfully.
    baselines = discovery.get("aht_baselines") or {}
    voice_target = (baselines.get("voice") or {}).get("suggested_target_s") or 285
    msg_target = (baselines.get("message") or {}).get("suggested_target_s") or 660
    acw_target = (baselines.get("acw_voice") or {}).get("suggested_target_s") or 15

    # Pull timezone suggestion from probe_organisation.
    org = discovery.get("organisation") or {}
    suggested_tz = org.get("suggested_timezone") or "UTC"

    draft = {
        "tenant": {
            "name": "__SETUP__ replace with your CC display name",
            "short_name": "__SETUP__",
            "timezone": suggested_tz,
        },
        "brands": {"names": q.get("brands") or []},
        "queues": {
            "name_pattern": q.get("detected_pattern") or "{brand} - {function}",
            "channels": q.get("channels") or ["Voice", "Chat"],
            "functions": q.get("functions") or [],
            "skip_substrings": q.get("skip_substring_suggestions") or [],
        },
        "management_units": {"ids": mu_ids},
        "business_unit": {"id": bu_id},
        "presence": {"pre_break_organisation_presence_id": pre_break_id},
        "specialist_roles": discovery["users"]["suggested_specialist_titles"]
                            or ["Specialist", "Customer Service Specialist"],
        "targets": {
            "voice_aht_s": int(round(voice_target)),
            "message_aht_s": int(round(msg_target)),
            "acw_s": int(round(acw_target)),
            "pre_break_min": 10,
            "fte_hours_per_month": 160,
        },
        "reports": {
            "output_dir": "~/Documents",
            "filename_pattern": "{tenant}-CC-{period}.html",
        },
    }
    return draft


def save_config(config_dict: dict, path: Path | str | None = None) -> Path:
    """Validate the dict against TenantConfig and write to YAML."""
    cfg = TenantConfig(**config_dict)
    target = Path(path).expanduser() if path else default_config_path()
    return dump_config(cfg, target)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="Auto-discover tenant config from a Genesys Cloud tenant.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--discover", action="store_true",
                     help="Probe the tenant; print discovery JSON to stdout.")
    mode.add_argument("--save", action="store_true",
                     help="Write a tenant config (after the interview fills in gaps).")
    parser.add_argument("--draft",
                       help="With --discover: also write a YAML draft to this path.")
    parser.add_argument("--config-json",
                       help="With --save: JSON-encoded tenant config to validate and write.")
    parser.add_argument("--out",
                       help="With --save: path to write to. Defaults to "
                            "~/.config/genesys-mcp/tenant.yaml (XDG-aware).")
    args = parser.parse_args(argv)

    load_dotenv_files(ENV_FILES)

    if args.save:
        if not args.config_json:
            parser.error("--save requires --config-json")
        try:
            config_dict = json.loads(args.config_json)
        except json.JSONDecodeError as exc:
            parser.error(f"--config-json is not valid JSON: {exc}")
        try:
            written = save_config(config_dict, args.out)
        except Exception as exc:
            print(f"ERROR: failed to validate/write config: {exc}", file=sys.stderr)
            return 1
        print(f"OK wrote tenant config to {written}")
        return 0

    # --discover path
    try:
        api = init_api()
    except GenesysConfigError as exc:
        print(
            f"ERROR: {exc}\n\n"
            f"The setup script needs the READ-only OAuth credentials "
            f"(GENESYS_CLIENT_ID/SECRET). Set them in your shell or in any of:\n"
            f"  - {ENV_FILES[0]}\n"
            f"  - {ENV_FILES[1]}\n"
            f"  - {ENV_FILES[2]}\n",
            file=sys.stderr,
        )
        return 2

    # Probes that don't depend on other probes run first.
    users = probe_users(api)
    queues = probe_queues(api)
    discovery: dict[str, Any] = {
        "organisation": probe_organisation(api),
        "queues": queues,
        "management_units": probe_management_units(api),
        "pre_break_candidates": probe_pre_break_presence(api),
        "users": users,
    }
    # AHT baselines need the specialist user list — resolve from the
    # suggested specialist titles + active users.
    specialist_titles = set(users.get("suggested_specialist_titles") or [])
    specialist_user_ids = [
        u["id"] for u in users.get("sample_users") or []
        if u.get("title") in specialist_titles
    ]
    # If the small `sample_users` list didn't surface enough specialists,
    # fall back to filtering the full title_counts pool (cheap — we already
    # paginated the users list once in probe_users; we just lost the ids).
    if len(specialist_user_ids) < 5:
        # Re-paginate to collect all matching ids — cap at 50 for the
        # aggregates query body size.
        users_api = gc.UsersApi(api)
        retry = _retry_read()
        for page in range(1, 21):
            try:
                resp = retry(lambda: users_api.get_users(
                    state="active", page_size=200, page_number=page,
                ))()
            except ApiException:
                break
            for u in resp.entities or []:
                if u.title in specialist_titles and u.id not in specialist_user_ids:
                    specialist_user_ids.append(u.id)
                    if len(specialist_user_ids) >= 50:
                        break
            if len(specialist_user_ids) >= 50:
                break
            if len(resp.entities or []) < 200:
                break
    discovery["aht_baselines"] = probe_aht_baselines(api, specialist_user_ids[:50])

    if args.draft:
        # Trim the all_queues list before printing the draft (it's noisy)
        draft = build_draft(discovery)
        # Validate the draft round-trips (with placeholder strings replaced) so
        # we know the shape is valid before the interview begins.
        # Just write the YAML directly via PyYAML for now (with header comments).
        import yaml  # already a dep
        path = Path(args.draft).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# DRAFT tenant config — produced by genesys-tenant-setup auto-discovery.\n"
            "# Fields marked __SETUP__ need to be filled in via the interview.\n"
            "# Do NOT save this file as ~/.config/genesys-mcp/tenant.yaml until\n"
            "# the placeholder values are replaced.\n"
            "#\n"
            "# Pattern detection confidence: " + discovery["queues"].get("pattern_confidence", "?") + "\n"
            "# Matched queues: " + str(discovery["queues"].get("matched_queues", 0)) + "\n"
            "# Skipped queues sample: " + ", ".join(discovery["queues"].get("skipped_queues_sample") or []) + "\n\n"
            + yaml.safe_dump(draft, sort_keys=False, default_flow_style=False)
        )
        print(f"OK draft written to {path}", file=sys.stderr)

    # Trim noisy fields from discovery output before JSON dump
    discovery["queues"].pop("all_queues", None)
    discovery["users"].pop("sample_users", None)
    print(json.dumps(discovery, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
