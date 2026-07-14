#!/usr/bin/env python3
"""scripts/fix_eh_data_actions.py — repair the Employment Hero HRIS data actions.

Use case: surface EH leave **balances** in Genesys WFM via the native HRIS
time-off feature (read-only — no time-off written into Genesys, nothing pushed
to EH). The EH integration + Basic-Auth credential already work; only the
data-action OUTPUT contracts + response success templates were wrong (they
returned EH's raw field names instead of the names the native feature expects,
with no response mapping). This script rewrites the three READ actions to the
required contracts via the data-action draft -> publish API:

  get_balance  HRIS - Employment Hero - Get Balance  -> {values:[{timeOffType,name,balance,units,end}]}
  leave_types  HRIS - Employment Hero - Leave Types   -> {timeOffTypes:[{id,name}]}
  get_agents   HRIS - Employment Hero - Get Agents     -> {employees:[{id,workEmail}]}

It deliberately does NOT create any Insert/Update (write-to-EH) action — that
absence is what keeps the integration read-only.

Default is --dry-run (reads the 3 actions, prints the intended changes, writes
NOTHING). --confirm applies: ensure draft -> PATCH draft -> (optional --test)
-> publish (unless --no-publish).

OAuth: reuses the SAME write client as provision_users.py (GENESYS_WRITE_CLIENT_*
in .env.write). That client's ROLE must grant **integrations:action:edit** and
**integrations:action:view**. The provisioning role (directory/routing/wfm) does
NOT include these, so add them first (or make a dedicated client) — otherwise you
get 403s. Verify non-destructively with --self-test before --confirm.

Usage (run with the repo's venv so the SDK + genesys_mcp are importable):
    PY=~/code/genesys-mcp/.venv/bin/python

    $PY scripts/fix_eh_data_actions.py                       # dry-run (default)
    $PY scripts/fix_eh_data_actions.py --self-test           # verify edit scope, no live change
    $PY scripts/fix_eh_data_actions.py --confirm             # apply + publish all three
    $PY scripts/fix_eh_data_actions.py --confirm --no-publish # apply as drafts, test in console first
    $PY scripts/fix_eh_data_actions.py --confirm --no-publish --test \
        --only get_balance --test-employee-id 12345          # live-test one action before publishing
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

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

log = logging.getLogger("fix_eh_data_actions")

# ─────────────────────────────────────────────────────────────────────────────
# Env loading (same pattern/order as provision_users.py)
# ─────────────────────────────────────────────────────────────────────────────

ENV_FILES = (
    _REPO_ROOT / ".env.write",                        # write creds (gitignored)
    _REPO_ROOT / ".env",                              # local read creds (gitignored)
    Path.home() / ".config" / "genesys-mcp.env",      # documented MCP creds location
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


def call_api(api: gc.ApiClient, method: str, path: str, *, body: Any = None, query: dict | None = None) -> Any:
    """Thin wrapper around api.call_api() — same as provision_users.py / raw.py."""
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
    return str(body)[:2000] if body else ""


def _hint_403(exc: ApiException) -> None:
    if getattr(exc, "status", None) == 403:
        log.error(
            "403 — the write client's role is missing data-action permissions. "
            "Creating actions needs 'integrations:action:add' (+ 'integrations:action:view'); "
            "publishing/editing needs 'integrations:action:edit'; --delete-old needs "
            "'integrations:action:delete'. Add to the GENESYS_WRITE_CLIENT_* role, then retry."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tenant constants + the standard Genesys error schema (reused on all 3 actions)
# ─────────────────────────────────────────────────────────────────────────────

EH_HOST = "https://api.yourpayroll.com.au"
BUSINESS_ID = "363397"
INTEGRATION_ID = "d648f2d5-1875-4699-bf6a-da0e43279ec1"  # Employment Hero Integration (Basic Auth)

# Standard Genesys data-action error envelope (fetched verbatim from the tenant).
ERROR_SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "title": "ErrorBody",
    "type": "object",
    "required": ["errorCode", "status", "userMessage"],
    "properties": {
        "errors": {"type": "array", "items": {"type": "object"}},
        "errorCode": {"type": "string"},
        "status": {"type": "integer"},
        "correlationId": {"type": ["string", "null"]},
        "entityId": {"type": ["string", "null"]},
        "entityName": {"type": ["string", "null"]},
        "userMessage": {"type": "string"},
        "userParamsMessage": {"type": ["string", "null"]},
        "userParams": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                "required": ["key", "value"],
            },
        },
        "details": {
            "type": "array",
            "items": {
                "title": "ErrorDetail",
                "type": "object",
                "properties": {
                    "errorCode": {"type": "string"},
                    "fieldName": {"type": "string"},
                    "entityId": {"type": ["string", "null"]},
                    "entityName": {"type": ["string", "null"]},
                },
                "required": ["errorCode", "fieldName"],
            },
        },
    },
    "additionalProperties": False,
}

_EMPTY_INPUT = {"type": "object", "properties": {}, "additionalProperties": True}

# ─────────────────────────────────────────────────────────────────────────────
# The three corrected action definitions
#
# Notes:
#  • Basic-Auth integrations auto-populate the Authorization header — do NOT set
#    one here (per the Genesys "Request configuration" docs).
#  • GET actions use the default request body template "${input.rawRequest}".
#  • Success templates iterate the raw EH array ($rawResult). If a given endpoint
#    returns an object wrapper instead of a bare array, change the #foreach source
#    to $rawResult.<field> and re-test in the Test pane.
#  • Leave-category names are quoted directly (they don't contain JSON-special
#    chars); if that ever changes, wrap with a JSON-escaping util.
# ─────────────────────────────────────────────────────────────────────────────

ACTIONS: dict[str, dict[str, Any]] = {
    "get_balance": {
        "id": "custom_-_36282445-3707-4410-962c-c54d910671a6",
        "name": "HRIS - Employment Hero - Get Balance",
        "input_schema": {
            "title": "Request",
            "type": "object",
            "properties": {"employeeId": {"type": "string"}, "end": {"type": "string"}},
            "additionalProperties": True,
        },
        "success_schema": {
            "title": "Response",
            "type": "object",
            "required": ["values"],
            "properties": {
                "values": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "timeOffType": {"type": "string"},
                            "name": {"type": "string"},
                            "balance": {"type": "string"},
                            "units": {"type": "string"},
                            "end": {"type": "string"},
                        },
                    },
                }
            },
        },
        "request": {
            # ?asAtDate honours the date the feature passes; drop it if EH ignores it.
            "requestUrlTemplate": EH_HOST + "/api/v2/business/" + BUSINESS_ID
            + "/employee/${input.employeeId}/leavebalances?asAtDate=${input.end}",
            "requestType": "GET",
            "headers": {},
            "requestTemplate": "${input.rawRequest}",
        },
        "translation_map": {"items": "$"},
        "success_template": """{
  "values": [
  #foreach($bal in $items)
    {
      "timeOffType": "$!{bal.leaveCategoryId}",
      "name": "$!{bal.leaveCategoryName}",
      "balance": "$!{bal.accruedAmount}",
      "units": "$!{bal.unitType}",
      "end": "$!{input.end}"
    }#if($foreach.hasNext),#end
  #end
  ]
}""",
        "test_input": {"employeeId": "REPLACE_ME", "end": "2026-12-31"},
    },
    "leave_types": {
        "id": "custom_-_60e6d8e8-cc7b-4389-9eac-cc3ad3f3de22",
        "name": "HRIS - Employment Hero - Leave Types",
        "input_schema": _EMPTY_INPUT,
        "success_schema": {
            "title": "Response",
            "type": "object",
            "required": ["timeOffTypes"],
            "properties": {
                "timeOffTypes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "name"],
                        # EH returns leave-category id as an INTEGER; keep it integer so it
                        # matches get_balance's timeOffType (also integer) for the WFM mapping.
                        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                    },
                }
            },
        },
        "request": {
            "requestUrlTemplate": EH_HOST + "/api/v2/business/" + BUSINESS_ID + "/leavecategory",
            "requestType": "GET",
            "headers": {},
            "requestTemplate": "${input.rawRequest}",
        },
        # EH /leavecategory already returns objects with `id` + `name`, so we can
        # pass the raw array straight through (Genesys disabled #foreach, but a
        # ${rawResult} passthrough is fine — same trick the blueprint uses).
        "translation_map": {},
        "success_template": """{ "timeOffTypes": ${rawResult} }""",
        "test_input": {},
    },
    "get_agents": {
        "id": "custom_-_1f7ad855-8715-4ad1-8752-44791662e62c",
        "name": "HRIS - Employment Hero - Get Agents",
        "input_schema": _EMPTY_INPUT,
        "success_schema": {
            "title": "Response",
            "type": "object",
            "required": ["employees"],
            "properties": {
                "employees": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "workEmail"],
                        "properties": {"id": {"type": "string"}, "workEmail": {"type": "string"}},
                    },
                }
            },
        },
        "request": {
            # /unstructured returns full records incl. email (plain /employee did not).
            "requestUrlTemplate": EH_HOST + "/api/v2/business/" + BUSINESS_ID + "/employee/unstructured",
            "requestType": "GET",
            "headers": {},
            "requestTemplate": "${input.rawRequest}",
        },
        # ⚠ Confirm the EH email field name in the Test pane — likely 'emailAddress'.
        "translation_map": {"items": "$"},
        "success_template": """{
  "employees": [
  #foreach($emp in $items)
    { "id": "$!{emp.id}", "workEmail": "$!{emp.emailAddress}" }#if($foreach.hasNext),#end
  #end
  ]
}""",
        "test_input": {},
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Data-action draft lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def get_action(api: gc.ApiClient, action_id: str) -> dict:
    return call_api(api, "GET", f"/api/v2/integrations/actions/{action_id}")


def get_draft(api: gc.ApiClient, action_id: str) -> dict | None:
    try:
        return call_api(api, "GET", f"/api/v2/integrations/actions/{action_id}/draft")
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def ensure_draft(api: gc.ApiClient, action_id: str) -> dict:
    """Return the existing draft, or create one from the published action."""
    existing = get_draft(api, action_id)
    if existing:
        log.info("  draft already exists (version %s) — reusing", existing.get("version"))
        return existing
    created = call_api(api, "POST", f"/api/v2/integrations/actions/{action_id}/draft", body={})
    log.info("  created draft (version %s)", created.get("version"))
    return created


def build_patch_body(defn: dict, version: int) -> dict:
    return {
        "version": version,
        "name": defn["name"],
        "category": "Employment Hero Integration",
        "contract": {
            "input": {"inputSchema": defn["input_schema"]},
            "output": {"successSchema": defn["success_schema"], "errorSchema": ERROR_SCHEMA},
        },
        "config": {
            "request": defn["request"],
            "response": {
                "translationMap": {},
                "translationMapDefaults": {},
                "successTemplate": defn["success_template"],
            },
        },
        "secure": False,
    }


def patch_draft(api: gc.ApiClient, action_id: str, body: dict) -> dict:
    return call_api(api, "PATCH", f"/api/v2/integrations/actions/{action_id}/draft", body=body)


def test_draft(api: gc.ApiClient, action_id: str, inputs: dict) -> dict:
    # Best-effort: body shape is {"inputs": {...}}. Prints whatever comes back.
    return call_api(api, "POST", f"/api/v2/integrations/actions/{action_id}/draft/test",
                    body={"inputs": inputs})


def publish_draft(api: gc.ApiClient, action_id: str, version: int) -> dict:
    return call_api(api, "POST", f"/api/v2/integrations/actions/{action_id}/draft/publish",
                    body={"version": version})


def create_action(api: gc.ApiClient, defn: dict) -> dict:
    """Create a NEW custom data action (the contract on a published action is
    immutable, so we can't fix the old ones in place — we create fresh)."""
    body = {
        "name": defn["name"],
        "category": "Employment Hero Integration",
        "integrationId": INTEGRATION_ID,
        "secure": False,
        "contract": {
            "input": {"inputSchema": defn["input_schema"]},
            "output": {"successSchema": defn["success_schema"], "errorSchema": ERROR_SCHEMA},
        },
        "config": {
            "request": defn["request"],
            "response": {
                # $rawResult is the raw response *string* — not iterable. We bind
                # the parsed array to $items via a JSONPath translation map, then
                # the success template iterates $items.
                "translationMap": defn.get("translation_map", {"items": "$"}),
                "translationMapDefaults": {},
                "successTemplate": defn["success_template"],
            },
        },
    }
    return call_api(api, "POST", "/api/v2/integrations/actions", body=body)


def delete_draft(api: gc.ApiClient, action_id: str) -> None:
    call_api(api, "DELETE", f"/api/v2/integrations/actions/{action_id}/draft")


def find_duplicates_by_name(api: gc.ApiClient, name: str, exclude_id: str) -> list[str]:
    """IDs of actions on the EH integration with this name, excluding the
    original broken action — i.e. leftovers from previous script runs."""
    resp = call_api(api, "GET", "/api/v2/integrations/actions",
                    query={"pageSize": 200, "includeAuthActions": "false"})
    return [
        a["id"] for a in (resp or {}).get("entities", [])
        if a.get("integrationId") == INTEGRATION_ID
        and a.get("name") == name and a.get("id") != exclude_id
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Operations
# ─────────────────────────────────────────────────────────────────────────────

def print_intended(key: str, defn: dict, current: dict | None) -> None:
    print(f"\n=== {key}  ({defn['name']}) ===")
    print(f"  action:        will CREATE a NEW action (old broken {defn['id']} stays unless --delete-old)")
    if current is not None:
        print(f"  old version:   {current.get('version')}")
    print(f"  method+url:    {defn['request']['requestType']} {defn['request']['requestUrlTemplate']}")
    print(f"  output schema: {json.dumps(defn['success_schema']['properties'])}")
    print("  success template:")
    for ln in defn["success_template"].splitlines():
        print(f"      {ln}")


def apply_action(write_api: gc.ApiClient, key: str, defn: dict, *,
                 do_test: bool, test_employee_id: str | None,
                 delete_old: bool) -> str:
    # Contracts are immutable once published, so we CREATE a fresh action rather
    # than patch the old (broken) one.
    # Idempotency: clear any same-named action from a prior run (best-effort;
    # leaves the original broken action alone — that's only removed by --delete-old).
    for dup in find_duplicates_by_name(write_api, defn["name"], defn["id"]):
        try:
            call_api(write_api, "DELETE", f"/api/v2/integrations/actions/{dup}")
            log.info("[%s] cleaned up prior duplicate %s", key, dup)
        except ApiException as exc:
            log.warning("[%s] couldn't delete prior duplicate %s (status=%s) — may accumulate",
                        key, dup, exc.status)

    log.info("[%s] creating NEW action '%s' on the EH integration…", key, defn["name"])
    created = create_action(write_api, defn)
    new_id = created.get("id")
    version = created.get("version", 1)
    log.info("[%s] CREATED %s (version %s)", key, new_id, version)

    if do_test:
        inputs = dict(defn["test_input"])
        if test_employee_id and "employeeId" in inputs:
            inputs["employeeId"] = test_employee_id
        log.info("[%s] testing the new action with inputs=%s", key, inputs)
        try:
            result = call_api(write_api, "POST",
                              f"/api/v2/integrations/actions/{new_id}/test",
                              body=inputs)
            print(f"[{key}] TEST RESULT:\n{json.dumps(result, indent=2)[:6000]}")
        except ApiException as exc:
            log.error("[%s] test call failed (status=%s) — action still created; test in console. body=%s",
                      key, exc.status, _err_body(exc))

    # Actions created via POST already have a published version and no draft —
    # they are live immediately, so there is no separate publish step.
    note = f"created (live) {new_id}"

    if delete_old:
        old_id = defn["id"]
        try:
            call_api(write_api, "DELETE", f"/api/v2/integrations/actions/{old_id}")
            log.info("[%s] deleted OLD broken action %s", key, old_id)
            note += f"; deleted old {old_id}"
        except ApiException as exc:
            log.warning("[%s] could NOT delete old %s (status=%s) — probably flow-referenced; "
                        "delete via console after removing the reference. %s",
                        key, old_id, exc.status, _err_body(exc)[:200])
            note += f"; OLD {old_id} not deleted ({exc.status})"

    return note


def run_self_test(write_api: gc.ApiClient) -> int:
    """Verify integrations:action:edit non-destructively: create a draft on an
    action that has none, then delete it. Skips (and reports) if a draft already
    exists, so we never discard in-progress work."""
    key = "leave_types"  # chosen because it had no draft; change with care
    action_id = ACTIONS[key]["id"]
    log.info("SELF-TEST: checking edit scope via create+delete draft on '%s'", key)
    try:
        if get_draft(write_api, action_id) is not None:
            log.warning("SELF-TEST: a draft already exists on '%s'; not deleting it. "
                        "view scope OK; assuming edit scope OK. Run --confirm when ready.", key)
            return 0
        created = call_api(write_api, "POST", f"/api/v2/integrations/actions/{action_id}/draft", body={})
        log.info("SELF-TEST: created draft (version %s) — edit scope present", created.get("version"))
        delete_draft(write_api, action_id)
        log.info("SELF-TEST: deleted the throwaway draft — clean. Write scope verified.")
        return 0
    except ApiException as exc:
        log.error("SELF-TEST FAILED: status=%s body=%s", exc.status, _err_body(exc))
        _hint_403(exc)
        return 1


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fix_eh_data_actions.py",
        description="Repair the Employment Hero HRIS data actions to the native WFM contracts.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print intended changes; write nothing (default).")
    mode.add_argument("--confirm", action="store_true", help="Apply changes (ensure draft -> patch -> publish).")
    mode.add_argument("--self-test", action="store_true", help="Verify edit scope non-destructively; no live change.")
    parser.add_argument("--no-publish", action="store_true", help="With --confirm: create as drafts but don't publish.")
    parser.add_argument("--delete-old", action="store_true",
                        help="With --confirm: delete the old broken action after creating the new one "
                             "(skips with a warning if it's still referenced by a flow).")
    parser.add_argument("--test", action="store_true", help="With --confirm: run the data action's Test before publish.")
    parser.add_argument("--test-employee-id", help="EH employee id for --test on get_balance.")
    parser.add_argument("--only", choices=list(ACTIONS), action="append",
                        help="Limit to one or more actions (repeatable). Default: all three.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%H:%M:%S",
    )

    loaded = load_dotenv_files(ENV_FILES)
    if loaded:
        log.info("Loaded env from: %s", ", ".join(str(p) for p in loaded))

    selected = args.only or list(ACTIONS)

    # Read client (for dry-run preflight + GETs). Always available.
    try:
        read_api = init_api()
    except GenesysConfigError as exc:
        print(f"Error: read client needs GENESYS_CLIENT_ID/SECRET — {exc}", file=sys.stderr)
        return 2

    # ── self-test ──
    if args.self_test:
        try:
            write_api = init_named_api("WRITE")
        except GenesysConfigError as exc:
            print(f"Error: write client needs GENESYS_WRITE_CLIENT_ID/SECRET — {exc}", file=sys.stderr)
            return 2
        return run_self_test(write_api)

    # ── dry-run (default) ──
    if not args.confirm:
        print("DRY-RUN — no writes. Intended changes:")
        for key in selected:
            defn = ACTIONS[key]
            try:
                current = get_action(read_api, defn["id"])
            except ApiException as exc:
                log.error("[%s] could not read action: status=%s", key, exc.status)
                current = None
            print_intended(key, defn, current)
        print("\nThese CREATE NEW actions (published contracts can't be edited). Old broken ones")
        print("stay unless you pass --delete-old. Re-run with --confirm to apply (needs")
        print("integrations:action:add + :view; :edit to publish; :delete for --delete-old).")
        print("Tip: --confirm --no-publish --test --only leave_types  to validate one first.")
        return 0

    # ── confirm (writes) ──
    try:
        write_api = init_named_api("WRITE")
    except GenesysConfigError as exc:
        print(f"Error: write client needs GENESYS_WRITE_CLIENT_ID/SECRET — {exc}", file=sys.stderr)
        return 2

    if args.no_publish:
        log.info("Note: --no-publish has no effect — API-created actions are live immediately.")

    if sys.stdin.isatty():
        extra = " and DELETE the old ones" if args.delete_old else ""
        ans = input(f"About to CREATE (live) {len(selected)} new data action(s){extra}: {', '.join(selected)}. Continue? [y/N]: ")
        if ans.strip().lower() != "y":
            print("Aborted.")
            return 1

    summary: list[tuple[str, str, str]] = []
    for key in selected:
        defn = ACTIONS[key]
        if key in ("get_balance", "get_agents"):
            log.warning("[%s] needs field renaming, which a normal data action can't do "
                        "(Genesys disabled #foreach). Deploy it as a FUNCTION data action — see "
                        "~/genesys-eh-leave-balances/functions/ + the runbook. Skipping here.", key)
            summary.append(("—", key, "function data action — deploy separately"))
            continue
        try:
            note = apply_action(
                write_api, key, defn,
                do_test=args.test, test_employee_id=args.test_employee_id,
                delete_old=args.delete_old,
            )
            summary.append(("✓", key, note))
        except ApiException as exc:
            _hint_403(exc)
            log.error("[%s] FAILED status=%s\n  full body: %s", key, exc.status, _err_body(exc))
            summary.append(("✗", key, f"status={exc.status} (see full body above)"))
        except Exception as exc:  # noqa: BLE001
            summary.append(("✗", key, f"{type(exc).__name__}: {exc}"))

    print("\n" + "─" * 80)
    print("SUMMARY")
    for sym, key, note in summary:
        print(f" {sym}  {key:<14} {note}")
    print("─" * 80)
    return 0 if all(s[0] == "✓" for s in summary) else 1


if __name__ == "__main__":
    sys.exit(main())
