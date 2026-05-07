# scripts/

> ⚠️ **DANGER ZONE — out-of-band writes against your tenant.** Everything in this directory talks to Genesys Cloud directly, **outside** the MCP server. Each script has its own credential and risk profile — read the relevant section before running. The MCP server itself ([../src/genesys_mcp/server.py](../src/genesys_mcp/server.py)) remains read-only and Claude cannot reach these scripts. They are deliberately operator-driven only.

---

## `provision_users.py` — bulk agent provisioning from a template

Creates new Genesys agents that mirror an existing "template" agent: same division, manager, location, ACD auto-answer, addresses, title/department, profile skills, routing skills + proficiency, routing languages, group memberships, and WFM management unit. Sends each new agent a Genesys activation email at the end.

### Tenant assumptions

This script bakes in two assumptions about how your tenant is configured. Both match a common contact-centre setup, but you should verify them before adopting:

1. **Authorisation roles inherit from group membership.** The relevant groups must have `rolesEnabled: true` and a configured role-set. The script never calls `PUT /users/{id}/roles` — adding a user to the right groups is what gives them the right roles. If your tenant assigns roles directly per user, you'll need to add the explicit roles step back (`authorization:grant:add` permission + a step 3 in `execute_user`); see the comment on `STEPS` in [provision_users.py](provision_users.py).

2. **Queue membership flows from group→queue auto-assignment.** The script never calls `/api/v2/routing/queues/{id}/members`. If your tenant requires direct queue assignment, add it as a new step.

The script also assumes WebRTC stations (no physical SIP phones) — Genesys auto-provisions a WebRTC station on first sign-in, so no `/telephony/providers/edges/phones` call is needed.

If any of these don't fit, this script is a starting point, not a drop-in.

### Phase 0 — one-off Genesys admin setup

1. **Create a dedicated OAuth client** in Genesys admin:
   - `Admin → Integrations → OAuth → Add Client`
   - Name: `Provisioning (write)` — distinct from the read-only MCP client
   - Grant type: **Client Credentials**

2. **Create a custom role** with these permissions and **only** these permissions:

   | Operation                          | Permission                       |
   |------------------------------------|----------------------------------|
   | Create user                        | `directory:user:add`             |
   | Edit user (manager, locations…)    | `directory:user:edit`            |
   | Bulk-assign routing skills         | `routing:skill:assign`           |
   | Bulk-assign routing languages      | `routing:language:assign`        |
   | Add to group                       | `directory:group:edit`           |
   | Move agent into WFM management unit | `wfm:agent:edit`                |
   | Send invite                        | `directory:user:setPassword`     |

   Name it something obvious like `Provisioning Agent Onboarder`. **Do not** grant `admin` — the whole point of this role is bounded blast radius.

   **`authorization:grant:add` is intentionally NOT in the list.** This script targets tenants where authorisation roles are inherited from group membership (`rolesEnabled: true` on the relevant groups), so it never calls `PUT /users/{id}/roles`. If your tenant assigns roles directly, add this permission back and re-introduce the roles step in `execute_user` (see the comment on `STEPS`).

   **`directory:user:delete` is intentionally NOT in the list.** The self-test leaves its throwaway user in place by default; you delete it manually in Genesys admin (People → search `__provisioning_self_test_*@example.invalid`). If you'd rather the self-test clean up after itself, add `directory:user:delete` to the role and run with `--auto-cleanup`.

3. **Assign the role to the OAuth client** (only — not to any human users).

4. **Paste the credentials** into a local file the script auto-loads:

   ```bash
   cd <path-to-genesys-mcp-repo>
   touch .env.write
   chmod 600 .env.write
   $EDITOR .env.write
   ```

   Add two lines:

   ```
   GENESYS_WRITE_CLIENT_ID=<paste-the-write-client-id>
   GENESYS_WRITE_CLIENT_SECRET=<paste-the-write-client-secret>
   ```

   `.env.write` is gitignored by the existing `.env.*` pattern. The MCP server intentionally does **not** load this file — only this script does.

   *Or*, if you'd rather not put secrets on disk, export them in the shell where you run the script:

   ```bash
   read -s -p "GENESYS_WRITE_CLIENT_ID: "     GENESYS_WRITE_CLIENT_ID;     echo; export GENESYS_WRITE_CLIENT_ID
   read -s -p "GENESYS_WRITE_CLIENT_SECRET: " GENESYS_WRITE_CLIENT_SECRET; echo; export GENESYS_WRITE_CLIENT_SECRET
   ```

5. **Verify the role has every required scope** with `--self-test`:

   ```bash
   python scripts/provision_users.py --self-test --template-email <existing-agent>@example.com
   ```

   This creates a throwaway user (`__provisioning_self_test_<ts>@example.invalid` — `.invalid` is a reserved TLD that never resolves, so any accidental invite-send bounces harmlessly) and runs every write step against them. The actual invite-send is skipped during self-test to keep noise out of mail logs. On success the throwaway user is **left in place** — delete it manually in Genesys admin afterwards. If any step 403s, the role is missing a scope — fix it in Genesys admin and rerun. **Do this before every production batch.**

   Add `--auto-cleanup` to attempt `DELETE /users/{id}` at the end (requires `directory:user:delete` on the role).

### Day-to-day use

Once Phase 0 is done, each batch is two commands:

```bash
# 1. Dry-run — prints the per-user plan, writes nothing
python scripts/provision_users.py \
    --template-email <template>@example.com \
    --emails ~/Documents/new_starters.txt

# 2. Confirm — actually executes
python scripts/provision_users.py \
    --template-email <template>@example.com \
    --emails ~/Documents/new_starters.txt \
    --confirm
```

The emails file is one address per line; `#` comments and blank lines are skipped. Names are derived from the email local-part (`new.starter.one@example.com` → `New Starter One`); the script prints the derived name and asks for interactive confirmation before any write when `--confirm` is set on a TTY.

### Flags

| Flag | What it does |
|---|---|
| `--template-email EMAIL` | Required. Email of the agent to clone settings from. |
| `--emails FILE` | One email per line; comments and blanks ignored. |
| `--email EMAIL` | Single email (alternative to `--emails`). |
| `--dry-run` | Print plan, write nothing. **Default**. |
| `--confirm` | Actually execute. Prompts for interactive confirmation if on a TTY. |
| `--self-test` | Provision throwaway user, exercise every write op. Leaves the user in place by default for manual deletion. |
| `--auto-cleanup` | With `--self-test`: also attempt `DELETE /users/{id}` (needs `directory:user:delete`). |
| `--refresh-template` | Bypass cached template snapshot at `/tmp/template_<email>.json`. Use after editing the template in Genesys admin between dry-run and confirm. |
| `--reconcile` | When a user already exists with no per-run ledger, bring their config in line with the template anyway. Off by default to avoid silent overwrites of pre-existing configurations. |
| `--template-allowlist FILE` | File of approved template emails. If provided, `--confirm` refuses any `--template-email` not on the list — defends against typos that might silently elevate every new hire by cloning the wrong template. |
| `-v` / `--verbose` | Debug-level logging. |

### What gets executed (7 steps per user)

In order:

1. `POST /api/v2/users` — create with name, email, division
2. `PATCH /api/v2/users/{id}` — manager, locations, addresses, ACD auto-answer, title, department, profile skills (refetches `version` first)
3. `PUT /api/v2/users/{id}/routingskills/bulk` — assign template's routing skills with proficiency
4. `PUT /api/v2/users/{id}/routinglanguages/bulk` — assign template's routing languages with proficiency
5. `POST /api/v2/groups/{groupId}/members` per group — refetches group `version` and skips already-members. **On tenants where `rolesEnabled: true` is configured on these groups and queues are auto-assigned by group, this step also gives the new user the right authorisation roles AND the right queues.** No separate roles or queues step is needed.
6. `POST /api/v2/workforcemanagement/agents` — async move into template's WFM management unit, polls for visibility (~15 s)
7. `POST /api/v2/users/{id}/invite?force=false` — Genesys emails the activation link

Invite is last so the user doesn't log in to a half-configured account. Genesys' default invite link is valid for 14 days — chase any new hires who haven't activated by then.

There is no explicit "assign roles" step. The template's roles are still snapshotted (and shown in the dry-run banner so you can sanity-check the access posture) but never written — they arrive automatically when the user is added to the correct groups in step 5. See "Tenant assumptions" above.

### Idempotency and partial-failure recovery

Every step writes to a per-user ledger at `/tmp/provision_users/<run-id>/<email>.json` as it completes. If the script dies mid-run (network blip, OAuth expiry, malformed email), rerun the same `--confirm` command and it will:

- For each email in the list, look for a matching ledger in the latest run-id directory
- Skip steps already marked `completed`
- Resume from the first incomplete step

Group memberships are pre-checked (`GET /api/v2/groups/{id}/members`) and skipped for already-members, so re-runs don't duplicate. The invite step treats `400/409` ("invite already pending") as success.

### Ledger location

```
/tmp/provision_users/
  └── 20260507_140000/         # one directory per script invocation
      ├── alice@example.com.json
      ├── bob@example.com.json
      └── carol@example.com.json
```

Each ledger file looks like:

```json
{
  "email": "alice@example.com",
  "user_id": "abc-123-def",
  "completed_steps": ["create", "patch", "skills", "languages", "groups", "wfm", "invite"],
  "last_error": null,
  "started_at": "2026-05-07T04:00:00.000Z"
}
```

`/tmp` is wiped on macOS reboot, which is fine for the typical provision-then-done workflow but means a multi-day partial run won't survive a restart. Override the location with `PROVISION_LEDGER_DIR=/persistent/path python scripts/provision_users.py …` if you need durability.

### Output

```
✓  new.starter.one@example.com   user-id-abc...   invite sent (expires 14 days)
↻  half.provisioned@example.com  user-id-jkl...   invite sent (expires 14 days)   [resumed]
⊘  existing.user@example.com     user-id-ghi...   skipped — exists, no ledger; rerun with --reconcile
✗  bad.email@example.com         user-id-mno...   failed at step 6 (wfm): status=403 ...
```

Exit code: `0` if all succeeded/skipped/resumed, `1` if any failed.

### Things that intentionally don't work

- **Physical SIP phones** — the script targets WebRTC-only deployments, so it never touches `/api/v2/telephony/providers/edges/phones`. Genesys auto-provisions a per-user WebRTC station on first sign-in. If you use desk phones, add a phone-cloning step.
- **Direct queue assignment** — see "Tenant assumptions" above; the script relies on group→queue auto-assignment.
- **Off-boarding / leavers** — different safety profile, would be its own script.
- **Per-row template overrides** — single template per batch; if you need multiple templates, run the script multiple times.
- **Bulk role/skill changes for existing agents** — onboarding only.

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Missing required env var(s): GENESYS_WRITE_CLIENT_ID, ...` | Either set them in `.env.write` or export them in your shell. The MCP server's read-only creds (`GENESYS_CLIENT_ID`) are not enough. |
| `Self-test failed at step X with 403` | The OAuth role is missing the scope for that step. See the table in Phase 0; add the missing permission to the role in Genesys admin. |
| `Self-test left throwaway user in place` | Expected default behaviour. Delete it from Genesys admin → People → search `__provisioning_self_test_*@example.invalid`. Add `--auto-cleanup` only if the role has `directory:user:delete`. |
| `Self-test could NOT delete throwaway user` (with `--auto-cleanup`) | Manually delete the throwaway user from Genesys admin. The role probably lacks `directory:user:delete`. |
| `Template user not found: <email>` | The template email doesn't match an existing user. Check spelling; try `find_user_by_email` via the read-only MCP. |
| `WFM move accepted but not visible after 15s` | The async move is slow today; the agent may still appear in the MU a few seconds later. Verify in Genesys admin or with the read-only MCP's `get_user_management_unit`. If they never appear, the destination MU may be in a business unit the OAuth client can't access. |
| `New user has no roles or queues despite group memberships` | Your tenant doesn't inherit roles from groups (or auto-assign queues from groups). See "Tenant assumptions" — you'll need to add explicit role/queue assignment steps. |
| All steps succeed but no invite arrives | Check Genesys admin for the user's `state` — if it's `inactive`, the invite step likely 4xx'd silently. Check the per-user ledger's `last_error`. |
