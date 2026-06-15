# Design: Provision-users phone step + double-click launchers

- **Date:** 2026-06-16
- **Status:** Approved (design), pending spec review
- **Component:** `scripts/provision_users.py`, `scripts/README.md`, new clickable launchers in `scripts/`

## Goal

Two changes to the existing bulk-provisioning workflow:

1. **Create a named WebRTC phone** for each new agent (name `Firstname.Lastname`, site
   `Prvidr Sydney`, assigned to the person), so agents get a deterministic phone instead of
   relying solely on Genesys' auto-provisioned WebRTC station.
2. **A double-clickable entry point** for non-technical operators on both macOS and Windows that
   opens a terminal and interactively asks for the template email and the new-starter emails — no
   command-line knowledge required.

## Background / current behaviour

`scripts/provision_users.py` clones a template agent onto new agents over a 7-step flow
(`create → patch → skills → languages → groups → wfm → invite`), dry-run by default, writes only
with `--confirm`. It uses a tightly-scoped write OAuth client (`GENESYS_WRITE_CLIENT_*`) with 7
narrow permissions and **deliberately no phone step** — the README states the script targets
WebRTC-only deployments where Genesys auto-provisions a station on first sign-in.

This design intentionally departs from that assumption: Prvidr wants an explicit, named phone per
agent at a specific site.

## Key constraints discovered

- `POST /api/v2/telephony/providers/edges/phones` requires permission **`telephony:plugin:all`**
  (type ANY). Genesys has **no narrower** "add phone" scope. Reading phone base settings requires
  the same permission. This is a meaningful blast-radius increase over the script's current 7 narrow
  scopes — **decision: accept it on the existing write client** (operator-confirmed).
- `Prvidr Sydney` site exists: id `93149b41-958a-4121-8787-15c671b145c8`, region `ap-southeast-2`.
- Create-phone required body fields (verified against the API schema): `name`, `site` (`{id}`),
  `phoneBaseSettings` (`{id}`), `lines` (array; each line requires `name`). Assigning the phone to a
  user is done via `webRtcUser: {id}`. The line needs `lineBaseSettings: {id}` to function.
- Repo is uv-managed with a working `.venv` at the repo root; `.venv/bin/python` imports the script's
  dependencies cleanly.

## Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Where `telephony:plugin:all` lives | Add to the existing `Provisioning (write)` OAuth role |
| Phone-step failure mode | Best-effort: log a warning and continue (user still provisioned) |
| Phone step position in flow | Between `wfm` and `invite` (invite stays last) |
| Self-test behaviour | Read-only permission check, **no** throwaway phone created |
| Interactive email entry | One email per line, blank line to finish (also accepts comma/space-separated) |
| Phone name source | Derived from email local-part → `Firstname.Lastname` |

## Detailed design

### 1. Phone step in `provision_users.py`

- Add `"phone"` to `STEPS`: `create → patch → skills → languages → groups → wfm → phone → invite`.
- New helper `derive_phone_name(email)`: same parsing as `derive_full_name` but joins parts with `.`
  (`jane.doe@x.com` → `Jane.Doe`).
- Module constant `PHONE_SITE_NAME = "Prvidr Sydney"`, overridable via env
  (e.g. `PROVISION_PHONE_SITE`). Optional env override for the WebRTC base-settings id
  (`PROVISION_PHONE_BASE_SETTINGS_ID`) for the ambiguous-discovery fallback.
- **Telephony resolution (once per run, cached in the snapshot):**
  - Resolve site by name → site id (fall back to the known id constant).
  - List phone base settings, select the WebRTC one (by `phoneMetaBase`), and read its
    `lineBaseSettings` id for the line. Store both in the snapshot so it's fetched once.
  - This resolution uses the **write** client (only it has `telephony:plugin:all`), so it runs at
    execute time, not during a read-only dry-run.
- **Per-user phone step (`execute_user`):**
  1. `GET .../edges/phones?name=Firstname.Lastname` — if a phone with that exact name already
     exists, log and skip creation (do not reassign someone else's phone).
  2. Otherwise `POST .../edges/phones` with
     `{name, site:{id}, phoneBaseSettings:{id}, lines:[{name, lineBaseSettings:{id}}], webRtcUser:{id:<new user>}}`.
  3. On any `ApiException`: log a warning, mark the step done, and continue (best-effort — matches the
     WFM step). The user remains provisioned; WebRTC auto-provisions on login regardless.
  - Writes to the per-user ledger like every other step, so reruns skip it.

### 2. Dry-run plan + banner

- `print_plan` gains a `PHONE:` line: `create WebRTC phone "Firstname.Lastname" at site Prvidr Sydney,
  assign to user`. During a pure dry-run (read client only) the base-settings id is not shown — it
  resolves at execute time — and that is stated in the line.

### 3. Self-test

- `--self-test` does **not** create a throwaway phone. Instead the `phone` step performs a read-only
  `GET .../edges/phonebasesettings` to confirm the role holds `telephony:plugin:all`; a 403 fails the
  self-test with the same "missing scope" guidance as other steps. No telephony side effects, nothing
  to clean up.

### 4. `--interactive` mode

- New `--interactive` flag. When set, the script:
  1. Validates write creds are present (executes after confirm); if missing, prints the existing
     setup help and exits.
  2. Prompts: template email → new-starter emails (one per line, blank to finish; comma/space
     separated also accepted).
  3. Snapshots the template, prints the template banner + per-user dry-run plan.
  4. Prompts `Proceed? [y/N]`; on `y`, runs the full execute path with writes.
- Reuses existing snapshot/plan/execute functions — no duplicated logic. Mutually compatible with
  `--template-allowlist` and `--refresh-template`.

### 5. Clickable launchers (new files in `scripts/`)

Thin bootstrappers; all real logic stays in Python.

- **`provision_users.command`** (macOS, double-click → Terminal):
  - Resolves repo root from its own path (`scripts/..`).
  - Prefers `<repo>/.venv/bin/python`, falls back to `python3`.
  - Runs `python scripts/provision_users.py --interactive`.
  - Keeps the window open at the end (`read -r -p "Press Enter to close…"`).
  - Made executable (`chmod +x`).
- **`provision_users.bat`** (Windows, double-click → cmd):
  - Resolves repo root via `%~dp0..`.
  - Prefers `<repo>\.venv\Scripts\python.exe`, falls back to `py -3` then `python`.
  - Runs the script with `--interactive`; ends with `pause`.

### 6. Docs (`scripts/README.md`)

- Add `telephony:plugin:all` to the Phase 0 permission table, with a note that it is broad
  (full telephony plugin admin) and that no narrower scope exists.
- Update "What gets executed (7 steps)" → 8 steps, documenting the phone step and its best-effort
  nature.
- Add a "Just double-click it" quick-start describing the `.command` / `.bat` launchers and the
  interactive prompts, for non-technical operators.
- Note the new phone-related env overrides.

## Testing

- Unit test `derive_phone_name` (dotted name, multi-part, hyphen/underscore handling).
- Unit-test the phone-step body builder (correct shape, `webRtcUser` set, line carries
  `lineBaseSettings`) with a mocked `call_api`, mirroring existing test patterns in `tests/`.
- Test best-effort failure: a phone `ApiException` is swallowed, the step is marked done, and the user
  is still reported as provisioned.
- Test the collision path: an existing phone with the same name → no POST issued.
- Manual: `--self-test` against a known agent confirms the telephony permission read check passes;
  double-click each launcher on its OS to confirm it opens a terminal and prompts.

## Out of scope

- Off-boarding / phone deletion.
- Desk/SIP phones (still WebRTC only).
- Reassigning or renaming existing phones.
- Per-row site overrides (single site per batch via the constant/env).

## Assumptions to verify during implementation

- Exactly one WebRTC `phoneMetaBase` base-settings entry exists; if more, use the env-override id.
- The repo `.venv` is the intended runtime for operators on their machines (it is on this machine).
