#!/usr/bin/env python3
"""scripts/link_eh_agents.py — set each Genesys agent's HRIS external id from EH.

The native WFM HRIS feature normally matches EH employees to Genesys users by EMAIL,
but Prvidr's EH emails are mostly personal, so that fails. However EH's `externalId`
field holds the **Genesys user id** (verified on Lawrence Drayton). So we link directly:

  for each active EH employee:  genesysUserId = externalId,  ehEmployeeId = id
  -> PUT /api/v2/workforcemanagement/agents/{genesysUserId}/integrations/hris
       { selectedIntegrationId, associatedIntegrations:[{agentExternalId: ehEmployeeId, integrationId, locked:true}] }

It reads the pairs by executing the `HRIS - Employment Hero - Get Agents` data action
(which now returns id + workEmail + externalId), resolves each externalId against the
Genesys directory (skipping ones that aren't real/active users — e.g. ex-staff), and
only writes for confirmed agents.

Default --dry-run prints the full mapping; --confirm applies. --only-user pilots one
agent (e.g. Lawrence Drayton e47b91b4-2582-4c78-ae85-ff535ff124ea).

OAuth: write client (GENESYS_WRITE_CLIENT_* in .env.write); needs wfm:agent:edit
(for the PUT) + integrations:action:execute (to read EH via the data action) + directory:user:view.

    PY=~/code/genesys-mcp/.venv/bin/python
    $PY scripts/link_eh_agents.py                                   # dry-run, all agents
    $PY scripts/link_eh_agents.py --only-user e47b91b4-...          # dry-run, just Lawrence
    $PY scripts/link_eh_agents.py --only-user e47b91b4-... --confirm  # apply for Lawrence (pilot)
    $PY scripts/link_eh_agents.py --confirm                          # apply for everyone
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

import PureCloudPlatformClientV2 as gc
from PureCloudPlatformClientV2.rest import ApiException

from genesys_mcp.client import GenesysConfigError, init_api, init_named_api

log = logging.getLogger("link_eh_agents")

ENV_FILES = (
    _REPO_ROOT / ".env.write",
    _REPO_ROOT / ".env",
    Path.home() / ".config" / "genesys-mcp.env",
)
WFM_HRIS_INTEGRATION_ID = "1a2ca4e2-24a8-41b4-938a-0330a7c11ad7"  # "WFM Time-off HRIS Integration"
FUNCTION_INTEGRATION_ID = "0c605b17-82cd-4122-b997-87482a199aac"  # "Function Data Actions (EH)"
GET_AGENTS_NAME = "HRIS - Employment Hero - Get Agents"


def load_dotenv_files(paths) -> None:
    for path in paths:
        if not path.exists():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                if k and k not in os.environ:
                    os.environ[k] = v.strip().strip('"').strip("'")


def call_api(api, method, path, *, body=None, query=None) -> Any:
    return api.call_api(resource_path=path, method=method, query_params=query or {}, body=body,
                        header_params={"Accept": "application/json", "Content-Type": "application/json"},
                        auth_settings=["PureCloud OAuth"], response_type="object")


def _err(exc: ApiException) -> str:
    b = getattr(exc, "body", None)
    if isinstance(b, (bytes, bytearray)):
        b = b.decode("utf-8", "replace")
    return str(b)[:400] if b else ""


def find_get_agents_action(api) -> str:
    resp = call_api(api, "GET", "/api/v2/integrations/actions", query={"pageSize": 200, "includeAuthActions": "false"})
    # Prefer the FUNCTION-integration version (the new one that returns externalId);
    # there's also an old same-named action on the EH custom-rest integration.
    for a in (resp or {}).get("entities", []):
        if a.get("name") == GET_AGENTS_NAME and a.get("integrationId") == FUNCTION_INTEGRATION_ID:
            return a["id"]
    raise SystemExit(f"Could not find '{GET_AGENTS_NAME}' on the Function integration {FUNCTION_INTEGRATION_ID}.")


def fetch_employees(api, action_id) -> list[dict]:
    """Run the Get Agents action and return its employees list. Tries /execute,
    falls back to /test (parsing the transformed output)."""
    try:
        out = call_api(api, "POST", f"/api/v2/integrations/actions/{action_id}/execute", body={})
        if isinstance(out, dict) and "employees" in out:
            return out["employees"]
    except ApiException as exc:
        log.info("execute not available (%s); falling back to /test", getattr(exc, "status", "?"))
    # Fallback: /test returns a step trace; pull the "Apply output transformation" result.
    r = call_api(api, "POST", f"/api/v2/integrations/actions/{action_id}/test", body={})
    for op in r.get("operations", []):
        if op.get("name") == "Apply output transformation" and op.get("success"):
            res = op.get("result")
            if isinstance(res, str):
                res = json.loads(res)
            if isinstance(res, dict):
                return res.get("employees", [])
    raise SystemExit(f"Could not get employees from action {action_id}; test success={r.get('success')}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="link_eh_agents.py")
    p.add_argument("--confirm", action="store_true", help="Apply the HRIS associations (default: dry-run).")
    p.add_argument("--only-user", help="Limit to a single Genesys user id (pilot).")
    p.add_argument("--integration-id", default=WFM_HRIS_INTEGRATION_ID, help="WFM HRIS integration id.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv_files(ENV_FILES)

    read_api = init_api()
    # The write client is needed to EXECUTE the data action (integrations:action:execute)
    # to read EH — and for the PUTs. Read client handles directory lookups.
    try:
        write_api = init_named_api("WRITE")
    except GenesysConfigError as exc:
        print(f"Error: write client needs GENESYS_WRITE_CLIENT_* — {exc}", file=sys.stderr)
        return 2
    action_id = find_get_agents_action(read_api)
    log.info("Get Agents action: %s", action_id)
    employees = fetch_employees(write_api, action_id)
    log.info("EH (non-terminated) employees returned: %d", len(employees))

    # Build candidate links: genesysUserId(=externalId) -> ehId, resolving against the directory.
    rows = []  # (genesysUserId, ehId, genesysName, genesysEmail, status)
    for e in employees:
        gid, ehid = (e.get("externalId") or "").strip(), str(e.get("id") or "").strip()
        if not gid or not ehid:
            continue
        if args.only_user and gid != args.only_user:
            continue
        try:
            u = call_api(read_api, "GET", f"/api/v2/users/{gid}")
            rows.append((gid, ehid, u.get("name", "?"), u.get("email", "?"), u.get("state", "?")))
        except ApiException as exc:
            if getattr(exc, "status", None) == 404:
                rows.append((gid, ehid, "(not a Genesys user)", "", "missing"))
            else:
                rows.append((gid, ehid, f"(lookup error {exc.status})", "", "error"))

    linkable = [r for r in rows if r[4] == "active"]
    print(f"\n{'EH id':<10} {'Genesys user':<28} {'email':<32} {'state':<8} {'Genesys id'}")
    print("-" * 110)
    for gid, ehid, name, email, state in rows:
        flag = "" if state == "active" else "   <-- skip"
        print(f"{ehid:<10} {name[:27]:<28} {email[:31]:<32} {state:<8} {gid}{flag}")
    print("-" * 110)
    print(f"{len(linkable)} linkable active agents, {len(rows) - len(linkable)} skipped (not active Genesys users).")

    if not args.confirm:
        print("\nDRY-RUN — nothing written. Re-run with --confirm to set HRIS associations.")
        print("Tip: --only-user e47b91b4-2582-4c78-ae85-ff535ff124ea --confirm  to pilot on Lawrence first.")
        return 0

    ok = err = 0
    for gid, ehid, name, email, state in linkable:
        try:
            # 1) Set the Genesys user's Employee ID (employerInfo.employeeId) — this is what the
            #    balance workflow's "Get Employment Hero ID" reads to resolve the EH employee.
            u = call_api(write_api, "GET", f"/api/v2/users/{gid}", query={"expand": "employerInfo"})
            ei = dict(u.get("employerInfo") or {})
            ei["employeeId"] = ehid
            call_api(write_api, "PATCH", f"/api/v2/users/{gid}",
                     body={"version": u["version"], "employerInfo": ei})
            # 2) Also set the WFM HRIS agent association (the agent's HRIS affiliation).
            assoc = {"selectedIntegrationId": args.integration_id,
                     "associatedIntegrations": [
                         {"agentExternalId": ehid, "integrationId": args.integration_id, "locked": True}]}
            call_api(write_api, "PUT", f"/api/v2/workforcemanagement/agents/{gid}/integrations/hris", body=assoc)
            log.info("linked %s (%s) -> EH %s (employeeId + WFM affiliation)", name, gid, ehid)
            ok += 1
        except ApiException as exc:
            log.error("FAILED %s (%s): status=%s %s", name, gid, exc.status, _err(exc))
            err += 1
    print(f"\nDone: {ok} linked, {err} failed.")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
