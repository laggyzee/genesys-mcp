#!/usr/bin/env python3
"""scripts/deploy_eh_functions.py — deploy the two Employment Hero FUNCTION data actions.

Genesys disabled Velocity #foreach, so the two actions that must RENAME EH's array
fields can't be plain data actions — they're Genesys Cloud *Function* data actions
(Node.js). This deploys them via the Platform API:

  create draft (POST /integrations/actions/drafts)
   -> request upload URL (POST .../draft/function/upload)
   -> PUT the zip to the presigned URL
   -> set function handler/runtime (PUT .../draft/function)
   -> publish (POST .../draft/publish)

The Node.js code lives in ~/genesys-eh-leave-balances/functions/{get_balance,get_agents}.js
(each is zipped as index.js; handler = index.handler).

ONE-TIME CONSOLE PREREQ (you do this first):
  1. Admin > Integrations > Add > "Genesys Cloud Function Data Actions"; install it.
  2. On that integration, add a credential the function can read. Easiest: a
     "User Defined" credential with ONE field named `apiKey` = your EH/KeyPay API key.
  3. Note the integration's ID (Admin > Integrations > the integration > ... or the URL).
Then run this with --integration-id <id>.  (Different field name? pass --credential-field.)

OAuth: same write client as the other scripts (GENESYS_WRITE_CLIENT_* in .env.write).
Role needs: integrations:action:add, :edit, :view (and the function upload/publish are
covered by :edit). Default is --dry-run; --confirm applies.

    PY=~/code/genesys-mcp/.venv/bin/python
    $PY scripts/deploy_eh_functions.py --integration-id <fnIntegrationId>            # dry-run
    $PY scripts/deploy_eh_functions.py --integration-id <fnIntegrationId> --confirm  # deploy both
    $PY scripts/deploy_eh_functions.py --integration-id <id> --confirm --only get_balance
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

import PureCloudPlatformClientV2 as gc
from PureCloudPlatformClientV2.rest import ApiException

from genesys_mcp.client import GenesysConfigError, init_named_api

log = logging.getLogger("deploy_eh_functions")

ENV_FILES = (
    _REPO_ROOT / ".env.write",
    _REPO_ROOT / ".env",
    Path.home() / ".config" / "genesys-mcp.env",
)
FUNCTIONS_DIR = Path.home() / "genesys-eh-leave-balances" / "functions"


def load_dotenv_files(paths) -> list[Path]:
    loaded = []
    for path in paths:
        if not path.exists():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip().strip('"').strip("'")
        loaded.append(path)
    return loaded


def call_api(api: gc.ApiClient, method: str, path: str, *, body: Any = None, query: dict | None = None) -> Any:
    return api.call_api(
        resource_path=path, method=method, query_params=query or {}, body=body,
        header_params={"Accept": "application/json", "Content-Type": "application/json"},
        auth_settings=["PureCloud OAuth"], response_type="object",
    )


def _err(exc: ApiException) -> str:
    b = getattr(exc, "body", None)
    if isinstance(b, (bytes, bytearray)):
        b = b.decode("utf-8", "replace")
    return str(b)[:2000] if b else ""


ERROR_SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema#", "title": "ErrorBody", "type": "object",
    "required": ["errorCode", "status", "userMessage"],
    "properties": {
        "errorCode": {"type": "string"}, "status": {"type": "integer"},
        "userMessage": {"type": "string"}, "correlationId": {"type": ["string", "null"]},
        "errors": {"type": "array", "items": {"type": "object"}},
    }, "additionalProperties": True,
}

# ─── The two function actions ────────────────────────────────────────────────
# request_template builds the JSON event passed to the function. ${credentials.FIELD}
# is filled with --credential-field at runtime. The function returns {values|employees}.
FUNCTIONS: dict[str, dict[str, Any]] = {
    "get_balance": {
        "name": "HRIS - Employment Hero - Get Balance",
        "js": "get_balance.js",
        "input_schema": {
            "title": "Request", "type": "object",
            "properties": {"employeeId": {"type": "string"}, "end": {"type": "string"}},
            "additionalProperties": True,
        },
        "success_schema": {
            "title": "Response", "type": "object", "required": ["values"],
            "properties": {"values": {"type": "array", "items": {"type": "object", "properties": {
                "timeOffType": {"type": "integer"}, "name": {"type": "string"},
                "balance": {"type": "string"}, "units": {"type": "string"}, "end": {"type": "string"},
            }}}},
        },
        "request_template": '{"employeeId": "${input.employeeId}", "end": "${input.end}", "apiKey": "${credentials.%CRED%}"}',
    },
    "get_agents": {
        "name": "HRIS - Employment Hero - Get Agents",
        "js": "get_agents.js",
        "input_schema": {"title": "Request", "type": "object", "properties": {}, "additionalProperties": True},
        "success_schema": {
            "title": "Response", "type": "object", "required": ["employees"],
            "properties": {"employees": {"type": "array", "items": {"type": "object",
                "required": ["id", "workEmail"],
                "properties": {"id": {"type": "string"}, "workEmail": {"type": "string"},
                               "externalId": {"type": "string"}}}}},
        },
        "request_template": '{"apiKey": "${credentials.%CRED%}"}',
    },
    "to_sydney_date": {
        "name": "To Sydney Date",
        "js": "to_sydney_date.js",
        "input_schema": {"title": "Request", "type": "object",
                         "properties": {"value": {"type": "string"}}, "additionalProperties": True},
        "success_schema": {"title": "Response", "type": "object", "required": ["date"],
                           "properties": {"date": {"type": "string"}}},
        # No EH call / credential needed — pure date conversion.
        "request_template": '{"value": "${input.value}"}',
    },
    "eh_hr_create_leave": {
        "name": "EH HR - Create Leave Request",
        "js": "eh_hr_create_leave.js",
        "input_schema": {"title": "Request", "type": "object",
                         "required": ["employeeExternalId", "paidCategoryId", "fromDate"],
                         "properties": {"employeeExternalId": {"type": "string"}, "paidCategoryId": {"type": "string"},
                                        "fromDate": {"type": "string"}, "toDate": {"type": "string"},
                                        "payableMinutesCsv": {"type": "string"}, "comment": {"type": "string"}},
                         "additionalProperties": True},
        "success_schema": {"title": "Response", "type": "object", "required": ["ok"],
                           "properties": {"ok": {"type": "boolean"}, "createdIds": {"type": "string"},
                                          "paidHours": {"type": "number"}, "unpaidHours": {"type": "number"},
                                          "skipped": {"type": "string"}, "error": {"type": "string"}}},
        # Deploy to the EH HR function integration (31178bdb), whose userDefined credential holds the 4 secret fields.
        # orgId, datatableId, lwopCategoryId (EH "Leave Without Pay") are non-secret config literals.
        "request_template": (
            '{"gcClientId":"${credentials.gcClientId}","gcClientSecret":"${credentials.gcClientSecret}",'
            '"ehClientId":"${credentials.ehClientId}","ehClientSecret":"${credentials.ehClientSecret}",'
            '"datatableId":"db754cfc-2b80-4a1e-8be0-ccd92f3eff9b","orgId":"bb033ff2-b0c8-41a5-87b0-5a24f418ee7c",'
            '"lwopCategoryId":"9fcdff47-745b-421c-a550-9910fbd11e52",'
            '"employeeExternalId":"${input.employeeExternalId}","paidCategoryId":"${input.paidCategoryId}",'
            '"fromDate":"${input.fromDate}","toDate":"${input.toDate}",'
            '"payableMinutesCsv":"${input.payableMinutesCsv}","comment":"${input.comment}"}'
        ),
    },
}


def build_zip(js_path: Path) -> bytes:
    """Zip the JS file as index.js (handler = index.handler)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.js", js_path.read_text())
    return buf.getvalue()


def cleanup_prior(api, integration_id: str, name: str) -> None:
    """Delete any prior action/draft with this name on the function integration, so
    re-runs (and the failed-publish orphan) don't accumulate. Best-effort."""
    for path in ("/api/v2/integrations/actions/drafts", "/api/v2/integrations/actions"):
        try:
            resp = call_api(api, "GET", path, query={"pageSize": 200})
        except ApiException:
            continue
        for a in (resp or {}).get("entities", []):
            if a.get("name") != name or a.get("integrationId") not in (integration_id, None):
                continue
            aid = a.get("id")
            try:
                call_api(api, "DELETE", f"/api/v2/integrations/actions/{aid}")
                log.info("cleanup: deleted prior %s (%s)", name, aid)
            except ApiException:
                try:
                    call_api(api, "DELETE", f"/api/v2/integrations/actions/{aid}/draft")
                    log.info("cleanup: deleted prior draft %s (%s)", name, aid)
                except ApiException as e:
                    log.warning("cleanup: couldn't delete %s (%s): %s", name, aid, e.status)


def _wait_zip_applied(api, action_id: str, key: str, *, loops: int = 30, delay: int = 2) -> None:
    """The uploaded zip is processed asynchronously; publish fails until it's applied."""
    log.info("[%s] waiting for uploaded zip to be applied…", key)
    for _ in range(loops):
        fc = call_api(api, "GET", f"/api/v2/integrations/actions/{action_id}/draft/function")
        z = fc.get("zip") or {}
        status = (z.get("status") or "")
        if status:
            log.info("[%s]   zip status: %s", key, status)
        low = status.lower()
        if any(k in low for k in ("avail", "applied", "built", "ready", "success", "complete", "active", "done")):
            return
        if any(k in low for k in ("error", "fail", "invalid", "exception")):
            raise RuntimeError(f"zip processing failed: {status} {z.get('errorMessage', '')}")
        time.sleep(delay)
    log.warning("[%s] zip status not confirmed applied after %ss — attempting publish anyway", key, loops * delay)


def deploy_one(api, key, defn, *, integration_id, category, credential_field, runtime, timeout) -> str:
    name = defn["name"]
    cleanup_prior(api, integration_id, name)
    req_template = defn["request_template"].replace("%CRED%", credential_field)
    body = {
        "name": name, "category": category, "integrationId": integration_id, "secure": False,
        "contract": {
            "input": {"inputSchema": defn["input_schema"]},
            "output": {"successSchema": defn["success_schema"], "errorSchema": ERROR_SCHEMA},
        },
        "config": {
            "request": {"requestType": "POST", "headers": {}, "requestTemplate": req_template},
            # The function returns the payload object directly; Genesys stringifies the
            # whole return into rawResult, so pass it straight through to the contract.
            "response": {"translationMap": {}, "translationMapDefaults": {}, "successTemplate": "${rawResult}"},
        },
    }
    log.info("[%s] creating draft action on integration %s…", key, integration_id)
    draft = call_api(api, "POST", "/api/v2/integrations/actions/drafts", body=body)
    action_id = draft["id"]
    log.info("[%s] draft id %s (v%s)", key, action_id, draft.get("version"))

    js_path = FUNCTIONS_DIR / defn["js"]
    zip_bytes = build_zip(js_path)
    log.info("[%s] requesting upload URL for index.zip (%d bytes)…", key, len(zip_bytes))
    up = call_api(api, "POST", f"/api/v2/integrations/actions/{action_id}/draft/function/upload",
                  body={"fileName": "index.zip", "signedUrlTimeoutSeconds": 600})
    url, headers = up["url"], up.get("headers", {}) or {}
    log.info("[%s] uploading zip (PUT presigned URL)…", key)
    put = urllib.request.Request(url=url, data=zip_bytes, method="PUT", headers=headers)
    with urllib.request.urlopen(put) as r:  # noqa: S310 (Genesys-signed S3 URL)
        log.info("[%s] upload HTTP %s", key, r.status)

    log.info("[%s] setting function (handler=index.handler runtime=%s timeout=%ss)…", key, runtime, timeout)
    call_api(api, "PUT", f"/api/v2/integrations/actions/{action_id}/draft/function",
             body={"handler": "index.handler", "runtime": runtime, "timeoutSeconds": timeout})

    _wait_zip_applied(api, action_id, key)

    cur = call_api(api, "GET", f"/api/v2/integrations/actions/{action_id}/draft")
    ver = cur.get("version")
    log.info("[%s] publishing draft (v%s)…", key, ver)
    pub = call_api(api, "POST", f"/api/v2/integrations/actions/{action_id}/draft/publish", body={"version": ver})
    log.info("[%s] PUBLISHED %s (v%s)", key, action_id, pub.get("version"))
    return f"published {action_id}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="deploy_eh_functions.py")
    p.add_argument("--integration-id", help="ID of your 'Genesys Cloud Function Data Actions' integration.")
    p.add_argument("--credential-field", default="apiKey", help="Credential field name holding the EH API key (default: apiKey).")
    p.add_argument("--runtime", default="nodejs22.x")
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--only", choices=list(FUNCTIONS), action="append")
    p.add_argument("--confirm", action="store_true", help="Apply (default is dry-run).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv_files(ENV_FILES)
    selected = args.only or list(FUNCTIONS)

    for key in selected:
        js = FUNCTIONS_DIR / FUNCTIONS[key]["js"]
        if not js.exists():
            print(f"ERROR: missing {js}", file=sys.stderr)
            return 2

    if not args.confirm:
        print("DRY-RUN — no writes.\n")
        for key in selected:
            d = FUNCTIONS[key]
            rt = d["request_template"].replace("%CRED%", args.credential_field)
            print(f"=== {key} ({d['name']}) ===")
            print(f"  integration:   {args.integration_id or '(pass --integration-id)'}")
            print(f"  runtime:       {args.runtime}   handler: index.handler   timeout: {args.timeout}s")
            print(f"  zip:           {(FUNCTIONS_DIR / d['js'])} -> index.js ({len((FUNCTIONS_DIR / d['js']).read_text())} bytes)")
            print(f"  requestTemplate: {rt}")
            print(f"  output:        {json.dumps(d['success_schema']['properties'])}\n")
        print("Re-run with --integration-id <id> --confirm to deploy (needs the Function integration + apiKey credential set up first).")
        return 0

    if not args.integration_id:
        print("ERROR: --integration-id is required with --confirm.", file=sys.stderr)
        return 2
    try:
        api = init_named_api("WRITE")
    except GenesysConfigError as e:
        print(f"Error: write client needs GENESYS_WRITE_CLIENT_ID/SECRET — {e}", file=sys.stderr)
        return 2

    # Use the integration's own name as the action category (matches console behaviour).
    try:
        integ = call_api(api, "GET", f"/api/v2/integrations/{args.integration_id}")
        category = integ.get("name") or "Function Data Actions"
        log.info("Function integration: %s (%s)", category, args.integration_id)
    except ApiException as e:
        print(f"ERROR: couldn't read integration {args.integration_id}: {_err(e)}", file=sys.stderr)
        return 2

    if sys.stdin.isatty():
        if input(f"Deploy {len(selected)} function action(s): {', '.join(selected)}? [y/N]: ").strip().lower() != "y":
            print("Aborted."); return 1

    summary = []
    for key in selected:
        try:
            note = deploy_one(api, key, FUNCTIONS[key], integration_id=args.integration_id,
                              category=category, credential_field=args.credential_field,
                              runtime=args.runtime, timeout=args.timeout)
            summary.append(("✓", key, note))
        except ApiException as e:
            log.error("[%s] FAILED status=%s\n  body: %s", key, e.status, _err(e))
            summary.append(("✗", key, f"status={e.status} (see body above)"))
        except Exception as e:  # noqa: BLE001
            log.error("[%s] FAILED: %s", key, e)
            summary.append(("✗", key, str(e)))

    print("\n" + "─" * 70 + "\nSUMMARY")
    for s, k, n in summary:
        print(f" {s}  {k:<12} {n}")
    print("─" * 70)
    return 0 if all(s[0] == "✓" for s in summary) else 1


if __name__ == "__main__":
    sys.exit(main())
