"""Health check — verifies the MCP is wired up correctly end-to-end.

Probes the OAuth client against one representative endpoint per scope so the
operator gets a green/red checklist instead of discovering scope gaps deep
inside a workflow with a cryptic 403. Also validates the tenant config and
checks that the companion skills are symlinked into the Claude-Code skills
directory.

Exposed two ways:

- ``mcp_health_check`` MCP tool — LLM-callable; Claude can invoke it when a
  workflow fails and surface the structured findings.
- ``python -m genesys_mcp.health_check`` CLI — invoked by ``install.sh``
  during onboarding to verify the install succeeded.

Both surfaces share the same underlying ``run_health_check()`` function so
output is identical across paths.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp import __version__
from genesys_mcp.client import get_api, to_dict, with_retry
from genesys_mcp.tenant import (
    TenantConfigError,
    default_config_path,
    load_config,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ScopeProbe:
    """One scope's representative endpoint + remediation message."""

    scope: str
    description: str
    probe: Callable[[gc.ApiClient], Any]
    remediation: str


def _probe_analytics(api: gc.ApiClient) -> Any:
    # Minimal aggregates query: 1-minute window, 1 metric, no filter.
    aapi = gc.AnalyticsApi(api)
    return aapi.post_analytics_conversations_aggregates_query({
        "interval": "2026-01-01T00:00:00Z/2026-01-01T00:01:00Z",
        "metrics": ["tAnswered"],
    })


def _probe_conversations(api: gc.ApiClient) -> Any:
    # get_conversation with a fake-but-well-formed UUID. The scope check is
    # whether Genesys returns 404 (scope granted, no such convo) vs 403
    # (scope missing). We treat 404 as success in _check_scope.
    capi = gc.ConversationsApi(api)
    return capi.get_conversation(
        conversation_id="00000000-0000-0000-0000-000000000000",
    )


def _probe_recordings(api: gc.ApiClient) -> Any:
    rapi = gc.RecordingApi(api)
    return rapi.get_orphanrecordings(page_size=1, page_number=1)


def _probe_users(api: gc.ApiClient) -> Any:
    uapi = gc.UsersApi(api)
    return uapi.get_users(page_size=1, page_number=1, state="active")


def _probe_routing(api: gc.ApiClient) -> Any:
    rapi = gc.RoutingApi(api)
    return rapi.get_routing_queues(page_size=1, page_number=1)


def _probe_sta(api: gc.ApiClient) -> Any:
    sapi = gc.SpeechTextAnalyticsApi(api)
    return sapi.get_speechandtextanalytics_settings()


def _probe_external_contacts(api: gc.ApiClient) -> Any:
    eapi = gc.ExternalContactsApi(api)
    return eapi.get_externalcontacts_contacts(page_size=1, page_number=1)


def _probe_wfm(api: gc.ApiClient) -> Any:
    wapi = gc.WorkforceManagementApi(api)
    return wapi.get_workforcemanagement_managementunits(page_size=1, page_number=1)


def _probe_quality(api: gc.ApiClient) -> Any:
    qapi = gc.QualityApi(api)
    # Listing forms is cheaper than searching evaluations and exercises the
    # same scope.
    return qapi.get_quality_forms_evaluations(page_size=1, page_number=1)


def _probe_presence_definitions(api: gc.ApiClient) -> Any:
    papi = gc.PresenceApi(api)
    return papi.get_presencedefinitions(page_size=1, page_number=1)


_SCOPES: tuple[_ScopeProbe, ...] = (
    _ScopeProbe(
        scope="analytics:readonly",
        description="Required — powers queue_performance, agent_performance, "
                    "repeat_caller_*, agent_coaching_pack and the entire "
                    "monthly-report skill",
        probe=_probe_analytics,
        remediation="Genesys Admin → Integrations → OAuth → your client's role "
                    "→ add Analytics > readonly",
    ),
    _ScopeProbe(
        scope="conversations:readonly",
        description="Required — powers get_conversation, search_conversations, "
                    "routing_diagnostic, and the conv-details walk used by "
                    "every report tool",
        probe=_probe_conversations,
        remediation="Genesys Admin → Integrations → OAuth → your client's role "
                    "→ add Conversation > readonly",
    ),
    _ScopeProbe(
        scope="users:readonly",
        description="Required — powers find_user, list_users, get_user_*, and "
                    "any tool that resolves an agent name → user id",
        probe=_probe_users,
        remediation="Genesys Admin → Integrations → OAuth → your client's role "
                    "→ add Directory > User > view",
    ),
    _ScopeProbe(
        scope="routing:readonly",
        description="Required — powers list_queues, get_queue_members, "
                    "live_wallboard, queue_observation, and routing_diagnostic",
        probe=_probe_routing,
        remediation="Genesys Admin → Integrations → OAuth → your client's role "
                    "→ add Routing > readonly",
    ),
    _ScopeProbe(
        scope="recordings:readonly",
        description="Optional — powers list_recordings, get_recording_url",
        probe=_probe_recordings,
        remediation="Genesys Admin → Integrations → OAuth → your client's role "
                    "→ add Recording > readonly",
    ),
    _ScopeProbe(
        scope="speech-and-text-analytics:readonly",
        description="Optional but recommended — powers get_conversation_summary, "
                    "get_conversation_sentiment, get_transcript_url, and the "
                    "sentiment section of agent_coaching_pack",
        probe=_probe_sta,
        remediation="Genesys Admin → Integrations → OAuth → your client's role "
                    "→ add Speech and Text Analytics > readonly",
    ),
    _ScopeProbe(
        scope="external-contacts:readonly",
        description="Optional — powers lookup_external_contact (CRM lookup by "
                    "phone/email)",
        probe=_probe_external_contacts,
        remediation="Genesys Admin → Integrations → OAuth → your client's role "
                    "→ add External Contacts > readonly",
    ),
    _ScopeProbe(
        scope="workforce-management:readonly",
        description="Optional — powers wfm_schedule, list_management_units, "
                    "agent_adherence_review",
        probe=_probe_wfm,
        remediation="Genesys Admin → Integrations → OAuth → your client's role "
                    "→ add Workforce Management > readonly",
    ),
    _ScopeProbe(
        scope="quality:readonly",
        description="Optional (v0.5+) — powers qa_evaluations and the QA "
                    "section of agent_coaching_pack",
        probe=_probe_quality,
        remediation="Genesys Admin → Integrations → OAuth → your client's role "
                    "→ add Quality > readonly",
    ),
    _ScopeProbe(
        scope="presence-definitions:view",
        description="Optional — powers list_org_presences (used by the "
                    "tenant-setup wizard to find your pre-break presence)",
        probe=_probe_presence_definitions,
        remediation="Genesys Admin → Integrations → OAuth → your client's role "
                    "→ add Presence Definitions > view",
    ),
)


def _check_scope(probe: _ScopeProbe) -> dict:
    api = get_api()
    try:
        with_retry(probe.probe)(api)
        return {
            "scope": probe.scope,
            "description": probe.description,
            "status": "ok",
            "remediation": None,
        }
    except gc.rest.ApiException as exc:
        # 404 on a fake-but-well-formed id proves the scope is granted —
        # Genesys gates 403 (auth) before 404 (resource). Treat as success.
        if exc.status == 404:
            return {
                "scope": probe.scope,
                "description": probe.description,
                "status": "ok",
                "remediation": None,
            }
        if exc.status == 403:
            return {
                "scope": probe.scope,
                "description": probe.description,
                "status": "missing",
                "remediation": probe.remediation,
            }
        return {
            "scope": probe.scope,
            "description": probe.description,
            "status": f"error_{exc.status}",
            "error": str(exc.reason),
            "remediation": probe.remediation,
        }
    except Exception as exc:
        return {
            "scope": probe.scope,
            "description": probe.description,
            "status": "error",
            "error": str(exc),
            "remediation": probe.remediation,
        }


def _check_tenant_config(api: gc.ApiClient | None = None) -> dict:
    cfg_path = default_config_path()
    out: dict[str, Any] = {
        "path": str(cfg_path),
        "exists": cfg_path.exists(),
        "loaded_ok": False,
        "warnings": [],
        "errors": [],
    }
    if not cfg_path.exists():
        out["errors"].append(
            "No tenant.yaml at the resolved path. Run the genesys-tenant-setup "
            "skill to generate one, or copy "
            "skills/cc-monthly-report/tenant.example.yaml to the path above and "
            "edit by hand."
        )
        return out
    try:
        cfg = load_config(cfg_path)
        out["loaded_ok"] = True
        out["tenant_name"] = cfg.tenant.name
        out["short_name"] = cfg.tenant.short_name
        out["brand_count"] = len(cfg.brands.names)
        out["mu_count"] = len(cfg.management_units.ids)
        out["schema_version"] = cfg.schema_version
        out["operating_model"] = {
            "has_pre_break_presence": cfg.operating_model.has_pre_break_presence,
            "has_brand_structure": cfg.operating_model.has_brand_structure,
            "expected_channels": cfg.operating_model.expected_channels,
        }
        # Lightweight warning checks
        risky_skips = [
            s for s in cfg.queues.skip_substrings
            if s.lower() in ("test", "sales", "support", "service")
        ]
        if risky_skips:
            out["warnings"].append(
                f"queues.skip_substrings contains {risky_skips!r} — this may "
                f"hide real customer-facing queues whose names happen to "
                f"include these substrings."
            )

        # v1.0: sample queue match-rate against the configured name pattern.
        # A tenant whose queues mostly don't match the pattern probably has
        # the wrong pattern configured — surface this concretely.
        if api is not None:
            _check_queue_pattern_match_rate(api, cfg, out)
            _check_specialist_roles_resolve(api, cfg, out)

    except TenantConfigError as exc:
        out["errors"].append(str(exc))
    return out


def _check_queue_pattern_match_rate(
    api: gc.ApiClient, cfg: Any, out: dict,
) -> None:
    """Sample the first page of queues and warn if < 80% match the pattern."""
    from genesys_mcp.queue_parser import compute_pattern_match_rate

    try:
        routing_api = gc.RoutingApi(api)
        page = to_dict(routing_api.get_routing_queues(page_size=100, page_number=1))
        queue_names = [q.get("name") for q in (page.get("entities") or []) if q.get("name")]
    except Exception as exc:
        out["warnings"].append(
            f"could not sample-test queue name pattern (list_queues failed: {exc})"
        )
        return

    if not queue_names:
        return  # nothing to test against

    # Strip out substrings the tenant explicitly skips
    filtered = [
        n for n in queue_names
        if not any(s in n for s in cfg.queues.skip_substrings)
    ]
    if not filtered:
        return

    rate = compute_pattern_match_rate(filtered, cfg.queues.name_pattern)
    out["queue_pattern_match_rate"] = round(rate, 3)
    out["queues_sampled"] = len(filtered)
    if cfg.queues.name_pattern is None:
        return  # null pattern → no expectations
    if rate < 0.8:
        out["warnings"].append(
            f"queues.name_pattern {cfg.queues.name_pattern!r} matches only "
            f"{int(rate * 100)}% of {len(filtered)} sampled queues. Either "
            f"set queues.name_pattern_match_required: false (allows fallback "
            f"to function-only naming for non-matching queues), or update the "
            f"pattern to one that fits this tenant's queue names."
        )


def _check_specialist_roles_resolve(
    api: gc.ApiClient, cfg: Any, out: dict,
) -> None:
    """Verify the configured specialist roles resolve to at least one user."""
    try:
        users_api = gc.UsersApi(api)
        page = to_dict(users_api.get_users(page_size=100, page_number=1, state="active"))
        users = page.get("entities") or []
    except Exception as exc:
        out["warnings"].append(
            f"could not verify specialist_roles against active users "
            f"(list_users failed: {exc})"
        )
        return

    titles_seen = {u.get("title") for u in users if u.get("title")}
    matches = [t for t in cfg.specialist_roles if t in titles_seen]
    out["specialist_roles_matched"] = matches
    if not matches:
        sample = sorted(titles_seen)[:5] if titles_seen else []
        out["warnings"].append(
            f"specialist_roles {cfg.specialist_roles!r} match no active "
            f"user titles. Sample titles in the first page of active users: "
            f"{sample!r}. Update specialist_roles in tenant.yaml or rerun "
            f"genesys-tenant-setup to re-discover."
        )


def _check_skills_linked() -> list[dict]:
    """Look for the three skills in the standard Claude Code skill dirs."""
    candidates: list[Path] = []
    for env_name in ("CLAUDE_CODE_SKILLS_DIR",):
        # Honour explicit override if a future env var lands.
        if env_name in os.environ:
            candidates.append(Path(os.environ[env_name]).expanduser())
    candidates.extend([
        Path.home() / ".claude" / "skills",
        Path.home() / ".agents" / "skills",
    ])
    skills = ["cc-monthly-report", "cc-coaching-prep", "genesys-tenant-setup"]
    out: list[dict] = []
    for name in skills:
        linked_at: str | None = None
        for cand in candidates:
            target = cand / name
            if target.exists() and target.is_symlink():
                linked_at = str(target)
                break
            if target.exists():
                linked_at = str(target) + " (directory, not symlink)"
                break
        out.append({
            "skill": name,
            "linked_at": linked_at,
            "ok": linked_at is not None,
            "remediation": (
                None if linked_at
                else f"ln -s \"$(pwd)/skills/{name}\" ~/.claude/skills/{name}"
            ),
        })
    return out


def _verdict(scopes: list[dict], config: dict, skills: list[dict]) -> tuple[str, list[str]]:
    blockers: list[str] = []
    required_scopes = {"analytics:readonly", "conversations:readonly",
                       "users:readonly", "routing:readonly"}
    for s in scopes:
        if s["scope"] in required_scopes and s["status"] != "ok":
            blockers.append(
                f"required scope {s['scope']!r} not granted ({s['status']})"
            )
    if not config.get("loaded_ok") and not config.get("exists"):
        blockers.append(
            "tenant.yaml missing — most tenant-aware tools will use fallback "
            "defaults; the skills will refuse to run"
        )
    elif config.get("errors"):
        blockers.append("tenant.yaml present but failed validation")
    # Skill links are warnings, not blockers — the MCP tools work without them
    if blockers:
        return "blocked", blockers
    warnings = config.get("warnings", []) + [
        s["remediation"] for s in skills if not s["ok"]
    ]
    if warnings:
        return "ready_with_warnings", blockers
    return "ready", blockers


def run_health_check() -> dict:
    """Run every health check and return a structured report.

    Shared between the MCP tool and the CLI entry point so both surfaces
    produce identical findings.
    """
    scopes = [_check_scope(p) for p in _SCOPES]
    # Pass the shared API client to the config check so the v1.0 sample
    # checks (queue-pattern match rate, specialist-role resolution) can
    # hit /routing/queues + /users without re-initialising.
    config = _check_tenant_config(api=get_api())
    skills = _check_skills_linked()
    verdict, blockers = _verdict(scopes, config, skills)
    return {
        "mcp_version": __version__,
        "oauth": {
            "region": os.environ.get("GENESYS_REGION", "ap-southeast-2"),
            "scopes_tested": scopes,
        },
        "tenant_config": config,
        "skills_linked": skills,
        "verdict": verdict,
        "blockers": blockers,
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def mcp_health_check() -> dict:
        """End-to-end health check — OAuth scopes, tenant config, and skill links.

        Invoke this when a workflow fails or after a fresh install. Returns a
        structured report:

        - ``oauth.scopes_tested`` — one row per scope with ``status`` (ok /
          missing / error_NNN) and a concrete ``remediation`` string for any
          gap. Required scopes (analytics, conversations, users, routing) are
          flagged as blockers; optional scopes only as warnings.
        - ``mcp_version`` — the installed genesys-mcp package version. Use this
          to confirm that a deployed image actually contains the expected release.
        - ``tenant_config`` — whether ``~/.config/genesys-mcp/tenant.yaml``
          exists and validates against the schema; surfaces warnings for
          risky skip-substrings or empty pre-break-presence-id.
        - ``skills_linked`` — whether each of the three companion skills
          (cc-monthly-report, cc-coaching-prep, genesys-tenant-setup) is
          symlinked into the Claude Code skills directory; concrete
          ``ln -s`` remediation when not.
        - ``verdict`` — ``ready`` / ``ready_with_warnings`` / ``blocked``
        - ``blockers`` — flat list of human-readable blocking issues

        Probes one cheap representative endpoint per scope (1-row paginated
        listings, 1-minute aggregates window) — no real workload, ~1s total.
        """
        return run_health_check()
