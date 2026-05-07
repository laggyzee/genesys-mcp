#!/usr/bin/env python3
"""scripts/provision_users.py — bulk-provision Genesys Cloud agents from a template.

See ./README.md for full documentation, including the one-off Genesys admin
setup (Phase 0). Quick reference:

    # First-time scope verification (creates a throwaway user; leaves it for manual deletion)
    python scripts/provision_users.py --self-test --template-email <existing-agent>@example.com

    # Dry-run a real batch (default — writes nothing)
    python scripts/provision_users.py --template-email <template>@example.com --emails new.txt

    # Actually execute
    python scripts/provision_users.py --template-email <template>@example.com --emails new.txt --confirm

The script reads OAuth credentials from the shell environment, falling back to
``<repo-root>/.env.write`` if present (gitignored — never commit it). The MCP
server only loads ``GENESYS_CLIENT_*``; this script additionally loads
``GENESYS_WRITE_CLIENT_*`` for the write-scoped operations.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable

# Make src/ importable when running from the repo root without an editable install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

import PureCloudPlatformClientV2 as gc
from PureCloudPlatformClientV2.rest import ApiException

from genesys_mcp.client import (
    GenesysConfigError,
    init_api,
    init_named_api,
    with_retry_for,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Files the script auto-loads, in priority order. First file to define a key
# wins; nothing overrides values already in os.environ. The script needs
# *both* the read client (for the template snapshot) and the write client
# (for the writes), so it deliberately loads more than just .env.write.
ENV_FILES = (
    _REPO_ROOT / ".env.write",                        # write creds (gitignored)
    _REPO_ROOT / ".env",                              # local read creds (gitignored)
    Path.home() / ".config" / "genesys-mcp.env",      # documented MCP creds location
)

LEDGER_BASE = Path(os.environ.get("PROVISION_LEDGER_DIR", "/tmp/provision_users"))
TEMPLATE_CACHE_DIR = Path(os.environ.get("PROVISION_TEMPLATE_CACHE", "/tmp"))

# Steps in execution order. Used by the per-user ledger to track resume points.
# Steps in execution order. "roles" is intentionally absent: this script targets
# tenants where authorisation roles are inherited from group membership (groups
# with rolesEnabled=true). On those tenants `PUT /users/{id}/roles` is redundant
# with adding the user to the right groups in step 5. If your tenant does NOT
# inherit roles from groups, add `authorization:grant:add` to the OAuth role and
# re-introduce a roles step before step 3.
STEPS = ("create", "patch", "skills", "languages", "groups", "wfm", "invite")

log = logging.getLogger("provision_users")

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_dotenv_files(paths: tuple[Path, ...]) -> list[Path]:
    """Tiny multi-file .env loader. Returns the paths it actually loaded.

    Reads each file in order. Only sets keys not already present in
    ``os.environ`` (so shell exports always win, and earlier files win over
    later ones). Honours ``KEY=value`` shape, ignores blank lines and ``#``
    comments, strips surrounding quotes.
    """
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


def derive_full_name(email: str) -> str:
    """``lawrence.drayton@x.com`` → ``"Lawrence Drayton"``.

    Best-effort — strips the domain, splits on dot/underscore/hyphen, title-cases
    each part. The dry-run plan prints the derived name and the operator
    confirms before any write happens.
    """
    local = email.split("@", 1)[0]
    parts = re.split(r"[._\-]+", local)
    return " ".join(p.capitalize() for p in parts if p) or local


def call_api(api: gc.ApiClient, method: str, path: str, *, body: Any = None, query: dict | None = None) -> Any:
    """Thin wrapper around ``api.call_api()`` for endpoints not wrapped by the SDK.

    Returns the parsed JSON body on success, raises ``ApiException`` on non-2xx.
    Same pattern as ``src/genesys_mcp/tools/raw.py`` (see [raw.py:62-71]).
    """
    return api.call_api(
        resource_path=path,
        method=method,
        query_params=query or {},
        body=body,
        header_params={"Accept": "application/json", "Content-Type": "application/json"},
        auth_settings=["PureCloud OAuth"],
        response_type="object",
    )


def _err_body(exc: ApiException) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", errors="replace")
    return str(body)[:500] if body else ""


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: Template snapshot
# ─────────────────────────────────────────────────────────────────────────────

def snapshot_template(
    read_api: gc.ApiClient,
    template_email: str,
    *,
    refresh: bool = False,
) -> dict:
    """Read everything we need from the template agent into a single dict.

    Cached to ``/tmp/template_<email>.json`` between runs. Pass ``refresh=True``
    (CLI: ``--refresh-template``) to bypass the cache when the template was
    edited in Genesys admin between dry-run and confirm.
    """
    safe_email = template_email.replace("/", "_").replace("\\", "_")
    cache_path = TEMPLATE_CACHE_DIR / f"template_{safe_email}.json"
    if cache_path.exists() and not refresh:
        log.info("Using cached template snapshot from %s (--refresh-template to re-fetch)", cache_path)
        return json.loads(cache_path.read_text())

    log.info("Snapshotting template agent: %s", template_email)
    retry = with_retry_for(init_api)

    # 1. Look up template by email (EXACT search).
    found = retry(lambda: call_api(
        read_api, "POST", "/api/v2/users/search",
        body={
            "pageSize": 1, "pageNumber": 1,
            "query": [{"type": "EXACT", "fields": ["email"], "value": template_email}],
        },
    ))()
    results = (found or {}).get("results") or []
    if not results:
        raise SystemExit(f"Template user not found: {template_email}")
    template_id = results[0]["id"]
    log.info("Template id: %s", template_id)

    # 2. Full user detail with the expansions we need to clone.
    # `groups` and `locations` come back inline when expanded — saves the
    # separate /groups/search call (which doesn't index by member-id anyway).
    expand = "manager,locations,addresses,acdAutoAnswer,profileSkills,division,groups"
    user = retry(lambda: call_api(
        read_api, "GET", f"/api/v2/users/{template_id}", query={"expand": expand},
    ))()

    # 3. Roles — response is `UserAuthorization` with the roles list under `.roles`
    # (NOT `.entities`). Falling back to `.entities` for any future API change.
    roles_resp = retry(lambda: call_api(
        read_api, "GET", f"/api/v2/users/{template_id}/roles",
    ))() or {}
    if isinstance(roles_resp, list):
        roles = roles_resp
    else:
        roles = roles_resp.get("roles") or roles_resp.get("entities") or []

    # 4. Routing skills.
    skills = retry(lambda: call_api(
        read_api, "GET", f"/api/v2/users/{template_id}/routingskills",
        query={"pageSize": 100},
    ))() or {}

    # 5. Routing languages.
    langs = retry(lambda: call_api(
        read_api, "GET", f"/api/v2/users/{template_id}/routinglanguages",
        query={"pageSize": 100},
    ))() or {}

    # 6. Group memberships — read inline from the expanded user (above).
    groups = user.get("groups") or []

    # 7. WFM management unit (best-effort — may 404 if template isn't in WFM).
    mu: dict | None = None
    try:
        mu = retry(lambda: call_api(
            read_api, "GET",
            f"/api/v2/workforcemanagement/agents/{template_id}/managementunit",
        ))()
    except ApiException as exc:
        log.warning("Template has no WFM management unit (or no WFM read perms): status=%s",
                    getattr(exc, "status", "?"))

    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "template_email": template_email,
        "template_id": template_id,
        "user": user,
        "roles": roles,
        "skills": skills.get("entities") or [],
        "languages": langs.get("entities") or [],
        "groups": groups,
        "wfm_management_unit": mu,
    }

    # Eyeball summary so shape problems surface at snapshot time, not at execute.
    mu_envelope = snapshot.get("wfm_management_unit") or {}
    wfm_mu_id = (mu_envelope.get("managementUnit") or mu_envelope).get("id")
    log.info(
        "Snapshot summary: roles=%d skills=%d languages=%d groups=%d wfm_mu_id=%s manager=%s locations=%d",
        len(snapshot["roles"]),
        len(snapshot["skills"]),
        len(snapshot["languages"]),
        len(snapshot["groups"]),
        wfm_mu_id or "(none)",
        bool(user.get("manager")),
        len(user.get("locations") or []),
    )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(snapshot, indent=2, default=str))
    log.info("Cached template snapshot to %s", cache_path)
    return snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Per-user ledger — enables resume-from-last-step on partial failure.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Ledger:
    email: str
    user_id: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    last_error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def is_done(self, step: str) -> bool:
        return step in self.completed_steps

    def mark_done(self, step: str) -> None:
        if step not in self.completed_steps:
            self.completed_steps.append(step)

    def save(self, dir_: Path) -> None:
        dir_.mkdir(parents=True, exist_ok=True)
        path = dir_ / f"{self.email.replace('/', '_')}.json"
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load_or_new(cls, dir_: Path, email: str) -> "Ledger":
        path = dir_ / f"{email.replace('/', '_')}.json"
        if path.exists():
            return cls(**json.loads(path.read_text()))
        return cls(email=email)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Per-user 8-step execution
# ─────────────────────────────────────────────────────────────────────────────

def execute_user(
    write_api: gc.ApiClient,
    snapshot: dict,
    target_email: str,
    target_name: str,
    ledger: Ledger,
    ledger_dir: Path,
    *,
    self_test: bool = False,
) -> Ledger:
    """Run the 8 provisioning steps for a single user.

    Each step writes to the ledger as it completes; on rerun the ledger lets us
    skip already-finished steps. Per-step errors raise after persisting the
    ledger so the caller can record the failure and move on (or stop).
    """
    retry = with_retry_for(partial(init_named_api, "WRITE"))

    def fail(step: str, exc: ApiException) -> None:
        ledger.last_error = f"{step}: status={exc.status} body={_err_body(exc)}"
        ledger.save(ledger_dir)
        log.error("[%s] step=%s FAILED: %s", target_email, step, ledger.last_error)
        raise

    # ─── Step 1: Create user ───────────────────────────────────────────────
    if not ledger.is_done("create"):
        body = {
            "name": target_name,
            "email": target_email,
            "divisionId": snapshot["user"]["division"]["id"],
        }
        try:
            new_user = retry(lambda: call_api(write_api, "POST", "/api/v2/users", body=body))()
            ledger.user_id = new_user["id"]
            ledger.mark_done("create")
            ledger.save(ledger_dir)
            log.info("[%s] CREATED user %s", target_email, ledger.user_id)
        except ApiException as exc:
            fail("create", exc)

    # ─── Step 2: PATCH identity (manager, locations, addresses, etc.) ─────
    if not ledger.is_done("patch"):
        # Refetch for fresh `version` (PATCH requires optimistic-concurrency).
        try:
            current = retry(lambda: call_api(
                write_api, "GET", f"/api/v2/users/{ledger.user_id}",
            ))()
        except ApiException as exc:
            fail("patch (refetch)", exc)
            return ledger  # type: ignore[unreachable]
        tpl = snapshot["user"]
        patch: dict[str, Any] = {"version": current["version"]}
        # Scalars / arrays where the PATCH shape matches the read shape.
        # Skip empties: PATCH treats `addresses: []` differently than "omit".
        for k in ("title", "department", "acdAutoAnswer", "profileSkills"):
            v = tpl.get(k)
            if v is not None and v != "":
                patch[k] = v
        if tpl.get("addresses"):  # only if non-empty; Genesys 400s on empty arrays here
            patch["addresses"] = tpl["addresses"]
        # NB: UpdateUser.manager is a BARE STRING (manager's user-id), not an
        # object — different shape from the read User.manager (which IS an
        # object with id+selfUri). Sending {"id": ...} returns 400 malformed.
        if tpl.get("manager") and tpl["manager"].get("id"):
            patch["manager"] = tpl["manager"]["id"]
        if tpl.get("locations"):
            # Each entry's id lives at locationDefinition.id (or top-level for
            # newer responses). Build the body in the same nested shape — the
            # API requires `locationDefinition` for assignment, not a flat id.
            cloned: list[dict] = []
            for loc in tpl["locations"]:
                loc_id = (
                    (loc.get("locationDefinition") or {}).get("id")
                    or loc.get("id")
                )
                if loc_id:
                    entry: dict = {"locationDefinition": {"id": loc_id}}
                    if loc.get("notes") is not None:
                        entry["notes"] = loc["notes"]
                    cloned.append(entry)
            if cloned:
                patch["locations"] = cloned
        try:
            retry(lambda: call_api(
                write_api, "PATCH", f"/api/v2/users/{ledger.user_id}", body=patch,
            ))()
            ledger.mark_done("patch")
            ledger.save(ledger_dir)
            log.info("[%s] PATCHED identity (fields=%s)", target_email, sorted(patch.keys()))
        except ApiException as exc:
            fail("patch", exc)

    # NB: there is no explicit "roles" step. This script targets tenants where
    # authorisation roles are inherited from group membership (rolesEnabled=true
    # on the relevant groups), so writing roles here would be redundant with
    # step 5 (groups). The snapshot still captures the template's roles for the
    # dry-run banner so the operator can sanity-check the cloned access posture.

    # ─── Step 3: Routing skills ───────────────────────────────────────────
    if not ledger.is_done("skills"):
        skills_body = [
            {"id": s["id"], "proficiency": s.get("proficiency", 0.0)}
            for s in snapshot["skills"] if isinstance(s, dict) and "id" in s
        ]
        if skills_body:
            try:
                retry(lambda: call_api(
                    write_api, "PUT", f"/api/v2/users/{ledger.user_id}/routingskills/bulk",
                    body=skills_body,
                ))()
                log.info("[%s] ASSIGNED %d routing skills", target_email, len(skills_body))
            except ApiException as exc:
                fail("skills", exc)
        else:
            log.info("[%s] no routing skills on template, skipping", target_email)
        ledger.mark_done("skills")
        ledger.save(ledger_dir)

    # ─── Step 4: Routing languages ────────────────────────────────────────
    if not ledger.is_done("languages"):
        langs_body = [
            {"id": l["id"], "proficiency": l.get("proficiency", 0.0)}
            for l in snapshot["languages"] if isinstance(l, dict) and "id" in l
        ]
        if langs_body:
            try:
                retry(lambda: call_api(
                    write_api, "PUT", f"/api/v2/users/{ledger.user_id}/routinglanguages/bulk",
                    body=langs_body,
                ))()
                log.info("[%s] ASSIGNED %d routing languages", target_email, len(langs_body))
            except ApiException as exc:
                fail("languages", exc)
        else:
            log.info("[%s] no routing languages on template, skipping", target_email)
        ledger.mark_done("languages")
        ledger.save(ledger_dir)

    # ─── Step 5: Group memberships (queues + roles follow via group rules) ──
    if not ledger.is_done("groups"):
        for g in snapshot["groups"]:
            group_id = g.get("id")
            if not group_id:
                log.warning("[%s] skipping group entry with no id: %s", target_email, g)
                continue
            group_name = g.get("name", group_id)
            try:
                # Fetch current group state for `version` and existing members.
                current_group = retry(lambda: call_api(
                    write_api, "GET", f"/api/v2/groups/{group_id}",
                ))()
                members = retry(lambda: call_api(
                    write_api, "GET", f"/api/v2/groups/{group_id}/members",
                    query={"pageSize": 200},
                ))() or {}
                member_ids = {m["id"] for m in (members.get("entities") or []) if "id" in m}
                if ledger.user_id in member_ids:
                    log.info("[%s] group %s: already a member, skip", target_email, group_name)
                    continue
                add_body = {
                    "memberIds": [ledger.user_id],
                    "version": current_group["version"],
                }
                # `with_retry_for` already retries 409 once with backoff; the
                # version we just fetched is fresh enough for that to converge.
                retry(lambda: call_api(
                    write_api, "POST", f"/api/v2/groups/{group_id}/members", body=add_body,
                ))()
                log.info("[%s] ADDED to group %s", target_email, group_name)
            except ApiException as exc:
                fail(f"groups({group_name})", exc)
        ledger.mark_done("groups")
        ledger.save(ledger_dir)

    # ─── Step 6: WFM management unit (async — 202 + poll) ────────────────
    if not ledger.is_done("wfm"):
        # The WFM endpoint returns a wrapped envelope: {user, managementUnit,
        # businessUnit}. The MU id we want lives at .managementUnit.id (not
        # at .id). Fall back to .id for forward-compat with API changes.
        mu_envelope = snapshot.get("wfm_management_unit") or {}
        mu = mu_envelope.get("managementUnit") or mu_envelope
        mu_id = mu.get("id")
        if mu_id:
            mu_name = mu.get("name", mu_id)
            try:
                retry(lambda: call_api(
                    write_api, "POST", "/api/v2/workforcemanagement/agents",
                    body={
                        "userIds": [ledger.user_id],
                        "destinationManagementUnitId": mu_id,
                    },
                ))()
            except ApiException as exc:
                fail("wfm (move)", exc)
            # Poll until the agent shows up in the destination MU (~10s budget).
            deadline = time.time() + 15
            visible = False
            while time.time() < deadline:
                try:
                    check = call_api(
                        write_api, "GET",
                        f"/api/v2/workforcemanagement/agents/{ledger.user_id}/managementunit",
                    )
                    if check and check.get("id") == mu_id:
                        visible = True
                        break
                except ApiException:
                    pass  # Not yet — keep polling.
                time.sleep(1)
            if visible:
                log.info("[%s] MOVED to WFM unit %s", target_email, mu_name)
            else:
                log.warning("[%s] WFM move accepted but not visible after 15s — continuing",
                            target_email)
        else:
            log.info("[%s] no WFM management unit on template, skipping", target_email)
        ledger.mark_done("wfm")
        ledger.save(ledger_dir)

    # ─── Step 7: Invite (must be last; skipped during self-test) ─────────
    if self_test:
        log.info("[%s] SKIP invite (self-test mode — would bounce off the .invalid domain)", target_email)
    elif not ledger.is_done("invite"):
        try:
            retry(lambda: call_api(
                write_api, "POST", f"/api/v2/users/{ledger.user_id}/invite",
                query={"force": "false"},
            ))()
            log.info("[%s] INVITE sent", target_email)
            ledger.mark_done("invite")
            ledger.save(ledger_dir)
        except ApiException as exc:
            if exc.status in (400, 409):
                # Already invited — treat as success.
                log.info("[%s] invite already pending (%d), treating as success", target_email, exc.status)
                ledger.mark_done("invite")
                ledger.save(ledger_dir)
            else:
                fail("invite", exc)

    return ledger


# ─────────────────────────────────────────────────────────────────────────────
# Self-test — provision a throwaway user, exercise every op, then delete.
# ─────────────────────────────────────────────────────────────────────────────

def run_self_test(
    write_api: gc.ApiClient,
    snapshot: dict,
    ledger_dir: Path,
    *,
    auto_cleanup: bool = False,
) -> int:
    ts = int(time.time())
    # ".invalid" is a reserved TLD (RFC 2606) — guaranteed never to resolve,
    # so any accidental invite-send bounces harmlessly. The "example" label is
    # arbitrary and doesn't need to match anything in your tenant.
    test_email = f"__provisioning_self_test_{ts}@example.invalid"
    test_name = f"Provisioning Self Test {ts}"
    ledger = Ledger(email=test_email)
    retry = with_retry_for(partial(init_named_api, "WRITE"))

    log.info("SELF-TEST starting: %s", test_email)
    ok = False
    last_step_attempted = "(create)"
    try:
        last_step_attempted = "create→invite (full flow)"
        execute_user(
            write_api, snapshot, test_email, test_name, ledger, ledger_dir,
            self_test=True,
        )
        log.info("SELF-TEST: all steps succeeded — OAuth role has all required write scopes")
        ok = True
    except ApiException as exc:
        log.error("SELF-TEST FAILED: status=%s body=%s", exc.status, _err_body(exc))
        log.error("Last successful step: %s",
                  ledger.completed_steps[-1] if ledger.completed_steps else "(none)")
        log.error("Likely missing permission for the failing step — check scripts/README.md.")
    except Exception as exc:
        log.error("SELF-TEST FAILED with unexpected error during %s: %s: %s",
                  last_step_attempted, type(exc).__name__, exc)
        log.error("Last successful step: %s",
                  ledger.completed_steps[-1] if ledger.completed_steps else "(none)")

    # Cleanup — opt-in. Default behaviour is to leave the throwaway user and
    # ask the operator to delete it manually, so the OAuth role doesn't need
    # `directory:user:delete` (the most destructive scope).
    if ledger.user_id:
        if auto_cleanup:
            try:
                retry(lambda: call_api(
                    write_api, "DELETE", f"/api/v2/users/{ledger.user_id}",
                ))()
                log.info("SELF-TEST: deleted throwaway user %s", ledger.user_id)
            except Exception as exc:
                log.warning(
                    "SELF-TEST: --auto-cleanup requested but DELETE failed (%s). "
                    "Delete manually in Genesys admin: %s", exc, ledger.user_id,
                )
        else:
            log.info(
                "SELF-TEST: throwaway user %s left in place (email %s). "
                "Delete manually in Genesys admin → People → search this email. "
                "Pass --auto-cleanup to attempt DELETE here (requires directory:user:delete).",
                ledger.user_id, test_email,
            )
    return 0 if ok else 1


# ─────────────────────────────────────────────────────────────────────────────
# Pre-checks and dry-run plan printing
# ─────────────────────────────────────────────────────────────────────────────

def find_user_by_email(read_api: gc.ApiClient, email: str) -> dict | None:
    retry = with_retry_for(init_api)
    found = retry(lambda: call_api(
        read_api, "POST", "/api/v2/users/search",
        body={
            "pageSize": 1, "pageNumber": 1,
            "query": [{"type": "EXACT", "fields": ["email"], "value": email}],
        },
    ))()
    results = (found or {}).get("results") or []
    return results[0] if results else None


def print_plan(snapshot: dict, target_email: str, target_name: str) -> None:
    user = snapshot["user"]
    print(f"  CREATE user (division={user.get('division', {}).get('name', '?')}, email={target_email}, name={target_name!r})")
    parts: list[str] = []
    if user.get("manager"):
        parts.append(f"manager={user['manager'].get('name', '?')}")
    if user.get("locations"):
        parts.append(f"locations=[{', '.join(l.get('name', '?') for l in user['locations'])}]")
    if user.get("acdAutoAnswer") is not None:
        parts.append(f"acdAutoAnswer={user['acdAutoAnswer']}")
    if user.get("title"):
        parts.append(f"title={user['title']!r}")
    if user.get("department"):
        parts.append(f"department={user['department']!r}")
    if user.get("profileSkills"):
        parts.append(f"profileSkills={user['profileSkills']}")
    print(f"  PATCH {', '.join(parts) if parts else '(no patch fields on template)'}")
    role_names = sorted(r.get("name", "?") for r in snapshot["roles"])
    print(f"  ROLES ({len(role_names)}, inherited via groups, not written): "
          f"{', '.join(role_names) if role_names else '(none)'}")
    skill_summary = ", ".join(f"{s.get('name', '?')}/{s.get('proficiency', 0)}" for s in snapshot["skills"])
    print(f"  SKILLS ({len(snapshot['skills'])}): {skill_summary or '(none)'}")
    lang_summary = ", ".join(f"{l.get('name', '?')}/{l.get('proficiency', 0)}" for l in snapshot["languages"])
    print(f"  LANGUAGES ({len(snapshot['languages'])}): {lang_summary or '(none)'}")
    group_names = ", ".join(g.get("name", "?") for g in snapshot["groups"])
    print(f"  GROUPS ({len(snapshot['groups'])}): {group_names or '(none)'}  (queues will follow via group→queue auto-assignment)")
    if snapshot.get("wfm_management_unit"):
        print(f"  WFM: move into management unit {snapshot['wfm_management_unit'].get('name', '?')}")
    print(f"  INVITE: send activation email to {target_email}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="provision_users.py",
        description="Bulk-provision Genesys Cloud agents from a template agent.",
        epilog="See scripts/README.md for the one-off Genesys admin setup (Phase 0).",
    )
    parser.add_argument("--template-email", help="Email of the agent to clone settings from. Required unless --self-test.")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--emails", help="File with one email per line (# comments and blank lines OK).")
    src.add_argument("--email", help="Single email (alternative to --emails).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print the plan and stop (default).")
    mode.add_argument("--confirm", action="store_true", help="Actually execute. Without this no writes happen.")
    parser.add_argument("--self-test", action="store_true",
        help="Provision a throwaway user and exercise every operation. "
             "Verifies the OAuth role has every required write scope. "
             "By default the throwaway user is LEFT IN PLACE for manual deletion "
             "(so the role doesn't need directory:user:delete).")
    parser.add_argument("--auto-cleanup", action="store_true",
        help="With --self-test: attempt to DELETE the throwaway user. Requires "
             "directory:user:delete on the OAuth role.")
    parser.add_argument("--refresh-template", action="store_true",
        help="Bypass the cached template snapshot at /tmp/template_<email>.json.")
    parser.add_argument("--reconcile", action="store_true",
        help="When a user already exists with no ledger, bring their config in line with the "
             "template anyway. Off by default to avoid silent overwrites.")
    parser.add_argument("--template-allowlist",
        help="File of approved template emails. If provided, --confirm refuses any "
             "--template-email not in this list. Defends against typos.")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load env files before anything reads os.environ.
    loaded_files = load_dotenv_files(ENV_FILES)
    if loaded_files:
        log.info("Loaded env from: %s", ", ".join(str(p) for p in loaded_files))

    if not args.template_email:
        parser.error("--template-email is required (use --self-test --template-email <known-good-agent>)")

    # Initialise the read-only client (always needed: snapshot + pre-check).
    try:
        read_api = init_api()
    except GenesysConfigError as exc:
        print(
            f"Error: {exc}\n\n"
            f"The script needs the READ-only OAuth credentials (GENESYS_CLIENT_ID/SECRET) for\n"
            f"the template snapshot and existing-user pre-checks. These usually live inside\n"
            f"Claude Code's MCP launcher config and are not visible to your shell. Put them\n"
            f"in any of these locations (the script searches in this order):\n"
            f"\n"
            f"  1. {ENV_FILES[0]}        (alongside the write creds — single file)\n"
            f"  2. {ENV_FILES[1]}              (separate file for read creds)\n"
            f"  3. {ENV_FILES[2]}\n"
            f"\n"
            f"Or export them in this shell:\n"
            f"  export GENESYS_CLIENT_ID=... GENESYS_CLIENT_SECRET=... GENESYS_REGION=ap-southeast-2\n",
            file=sys.stderr,
        )
        return 2

    # Initialise the write client only if we're going to write.
    will_write = args.confirm or args.self_test
    write_api: gc.ApiClient | None = None
    if will_write:
        try:
            write_api = init_named_api("WRITE")
        except GenesysConfigError as exc:
            print(
                f"Error: write client requires GENESYS_WRITE_CLIENT_ID/SECRET — {exc}\n"
                f"  Tip: paste them into {ENV_FILES[0]} or export them in this shell.",
                file=sys.stderr,
            )
            return 2

    # Phase 1: snapshot the template (always, even for dry-run).
    snapshot = snapshot_template(read_api, args.template_email, refresh=args.refresh_template)

    # Per-run ledger directory.
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    ledger_dir = LEDGER_BASE / run_id

    # Self-test branch.
    if args.self_test:
        if write_api is None:
            return 2
        return run_self_test(write_api, snapshot, ledger_dir, auto_cleanup=args.auto_cleanup)

    # Read input emails.
    if args.emails:
        text = Path(args.emails).read_text()
        emails = [
            line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    elif args.email:
        emails = [args.email]
    else:
        parser.error("--email or --emails is required (or use --self-test)")
        return 2  # unreachable

    if not emails:
        print("No emails to process.", file=sys.stderr)
        return 1

    # Template-confirmation banner — defends against the worst failure mode
    # (typo on --template-email resolving to a senior leader's account). Roles
    # are shown for sanity-checking even though they're inherited (not written)
    # so the operator can spot a mis-configured template at a glance.
    role_names = sorted(r.get("name", "?") for r in snapshot["roles"])
    mu_envelope = snapshot.get("wfm_management_unit") or {}
    mu_name = (mu_envelope.get("managementUnit") or mu_envelope).get("name") or "?"
    print()
    print(f"TEMPLATE: {snapshot['template_email']}  (\"{snapshot['user'].get('name', '?')}\")")
    print(f"  ROLES ({len(role_names)}, inherited via groups): {', '.join(role_names) or '(none)'}")
    print(f"  GROUPS ({len(snapshot['groups'])}): "
          f"{', '.join(g.get('name', '?') for g in snapshot['groups']) or '(none)'}")
    if mu_envelope:
        print(f"  WFM unit: {mu_name}")
    print()

    # Allowlist check.
    if args.template_allowlist and args.confirm:
        allowed = {
            line.strip() for line in Path(args.template_allowlist).read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        if args.template_email not in allowed:
            print(
                f"ERROR: --template-email {args.template_email} is not in allowlist "
                f"{args.template_allowlist}", file=sys.stderr,
            )
            return 2

    # Interactive template confirmation if confirming and on a TTY.
    if args.confirm and sys.stdin.isatty():
        try:
            ans = input("Continue with this template? [y/N]: ")
        except EOFError:
            ans = ""
        if ans.strip().lower() != "y":
            print("Aborted.")
            return 1

    # Phase 2/3: per-user processing.
    print(f"\nProcessing {len(emails)} email(s):\n")
    summary: list[tuple[str, str, str, str]] = []
    for i, email in enumerate(emails, 1):
        name = derive_full_name(email)
        print(f"[{i}/{len(emails)}] {email} → \"{name}\"")

        existing = find_user_by_email(read_api, email)
        ledger = Ledger.load_or_new(ledger_dir, email)
        if ledger.user_id is None and existing:
            ledger.user_id = existing["id"]

        # Idempotency: existing user with no per-run ledger → don't overwrite.
        if existing and not ledger.completed_steps and not args.reconcile:
            msg = "skipped — exists, no ledger (use --reconcile to bring in line with template)"
            print(f"  ⚠ {msg}")
            summary.append(("⊘", email, existing.get("id", "?"), msg))
            continue

        if not args.confirm:
            print_plan(snapshot, email, name)
            summary.append(("•", email, existing.get("id", "—") if existing else "—", "dry-run only"))
            continue

        if write_api is None:
            print("  (write client not initialised — should never happen)")
            return 2

        try:
            execute_user(write_api, snapshot, email, name, ledger, ledger_dir)
            note = "invite sent (expires 14 days)" if ledger.is_done("invite") else "completed (no invite)"
            symbol = "↻" if existing and ledger.completed_steps != list(STEPS) else "✓"
            summary.append((symbol, email, ledger.user_id or "?", note))
        except ApiException as exc:
            summary.append(("✗", email, ledger.user_id or "?",
                            f"failed: status={exc.status} body={_err_body(exc)[:120]}"))
        except Exception as exc:
            summary.append(("✗", email, ledger.user_id or "?", f"failed: {exc}"))

    # Phase 4: summary table.
    print("\n" + "─" * 90)
    print("SUMMARY")
    print("─" * 90)
    for sym, email, uid, note in summary:
        print(f" {sym}  {email:<40} {uid:<40} {note}")
    print("─" * 90)
    print(f"Ledger dir: {ledger_dir}")

    failed = sum(1 for s in summary if s[0] == "✗")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
