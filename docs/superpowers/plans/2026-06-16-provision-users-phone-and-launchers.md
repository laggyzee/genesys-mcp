# Provision-users Phone Step + Double-Click Launchers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a best-effort WebRTC phone-creation step (name `Firstname.Lastname`, site `Prvidr Sydney`, assigned to the new user) to `scripts/provision_users.py`, plus an `--interactive` mode and double-clickable launchers for macOS and Windows.

**Architecture:** Keep all logic in Python (single source of truth); the `.command`/`.bat` files are thin bootstrappers that launch `provision_users.py --interactive`. New phone logic is factored into small pure/testable helpers (`derive_phone_name`, `resolve_phone_config`, `build_phone_body`, `create_phone_for_user`) wired into the existing 7-step flow as an 8th step between `wfm` and `invite`. The per-email processing loop is extracted into `process_one()` so both `main()` and interactive mode reuse it (DRY).

**Tech Stack:** Python 3, `PureCloudPlatformClientV2` SDK, `pytest` (run via `uv run --group test python -m pytest`), bash (`.command`), batch (`.bat`).

**Spec:** `docs/superpowers/specs/2026-06-16-provision-users-phone-and-launchers-design.md`

---

## File Structure

- **Modify** `scripts/provision_users.py` — phone constants + helpers, `STEPS` update, snapshot/exec wiring, `process_one()` extraction, `--interactive`, self-test perm-check, `--discover-phone-settings` diagnostic.
- **Create** `tests/test_provision_users.py` — unit tests for the new pure helpers (loaded via `importlib`, matching repo convention).
- **Create** `scripts/provision_users.command` — macOS double-click launcher.
- **Create** `scripts/provision_users.bat` — Windows double-click launcher.
- **Modify** `scripts/README.md` — Phase 0 permission, 8-step list, env overrides, double-click quick-start.

## Phase 0 dependency (operator action, not code)

The phone step needs **`telephony:plugin:all`** added to the `Provisioning (write)` OAuth role in Genesys admin. Until that is done, live phone creation and the `resolve_phone_config`/self-test telephony calls will `403`. The unit tests below are fully mocked and do **not** require it. Tasks 1–6 can be coded and unit-tested before Phase 0; live verification (Task 11) is gated on it.

---

### Task 1: `derive_phone_name` helper

**Files:**
- Modify: `scripts/provision_users.py` (add after `derive_full_name`, ~line 117)
- Test: `tests/test_provision_users.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_provision_users.py`:

```python
"""Unit tests for the pure helpers added to scripts/provision_users.py.

The script lives outside the package layout (scripts/), so we load it by path
via importlib — the same approach conftest.py uses for skills/*/build_report.py.
Only the pure/extracted helpers are tested; the live API orchestration
(snapshot_template, execute_user) is exercised via --self-test against a real
tenant, consistent with this repo's "don't unit-test 1:1 SDK calls" convention.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _load_provision():
    path = _REPO_ROOT / "scripts" / "provision_users.py"
    spec = importlib.util.spec_from_file_location("provision_users", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


pu = _load_provision()


class TestDerivePhoneName:
    @pytest.mark.parametrize("email,expected", [
        ("jane.doe@example.com", "Jane.Doe"),
        ("john_smith@example.com", "John.Smith"),
        ("mary-jane.watson@example.com", "Mary.Jane.Watson"),
        ("madonna@example.com", "Madonna"),
        ("a.b.c@x.io", "A.B.C"),
    ])
    def test_dotted_titlecase(self, email, expected):
        assert pu.derive_phone_name(email) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/lawrence/code/genesys-mcp && uv run --group test python -m pytest tests/test_provision_users.py -v`
Expected: FAIL with `AttributeError: module 'provision_users' has no attribute 'derive_phone_name'`

- [ ] **Step 3: Write minimal implementation**

In `scripts/provision_users.py`, immediately after `derive_full_name` (ends ~line 117), add:

```python
def derive_phone_name(email: str) -> str:
    """``jane.doe@x.com`` → ``"Jane.Doe"`` — the WebRTC phone display name.

    Same parsing as ``derive_full_name`` but joins the parts with a dot to match
    the Prvidr ``Firstname.Lastname`` phone naming convention.
    """
    local = email.split("@", 1)[0]
    parts = re.split(r"[._\-]+", local)
    return ".".join(p.capitalize() for p in parts if p) or local
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/lawrence/code/genesys-mcp && uv run --group test python -m pytest tests/test_provision_users.py -v`
Expected: PASS (5 cases)

- [ ] **Step 5: Commit**

```bash
cd /Users/lawrence/code/genesys-mcp
git add scripts/provision_users.py tests/test_provision_users.py
git commit -m "feat(provision): add derive_phone_name helper"
```

---

### Task 2: Phone constants + `build_phone_body`

**Files:**
- Modify: `scripts/provision_users.py` (constants near line 64; helper after `derive_phone_name`)
- Test: `tests/test_provision_users.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_provision_users.py`:

```python
class TestBuildPhoneBody:
    def test_shape(self):
        cfg = {
            "site_id": "site-1",
            "phone_base_settings_id": "pbs-1",
            "line_base_settings_id": "lbs-1",
        }
        body = pu.build_phone_body("Jane.Doe", "user-99", cfg)
        assert body == {
            "name": "Jane.Doe",
            "site": {"id": "site-1"},
            "phoneBaseSettings": {"id": "pbs-1"},
            "lines": [{"name": "Jane.Doe", "lineBaseSettings": {"id": "lbs-1"}}],
            "webRtcUser": {"id": "user-99"},
        }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/lawrence/code/genesys-mcp && uv run --group test python -m pytest tests/test_provision_users.py::TestBuildPhoneBody -v`
Expected: FAIL with `AttributeError: ... has no attribute 'build_phone_body'`

- [ ] **Step 3: Write minimal implementation**

In `scripts/provision_users.py`, add constants after the `TEMPLATE_CACHE_DIR` line (~line 65):

```python
# WebRTC phone provisioning. The site name defaults to Prvidr's; all four can be
# overridden by env for other tenants or when auto-discovery is ambiguous.
PHONE_SITE_NAME = os.environ.get("PROVISION_PHONE_SITE", "Prvidr Sydney")
PHONE_SITE_ID_OVERRIDE = os.environ.get("PROVISION_PHONE_SITE_ID")
PHONE_BASE_SETTINGS_ID_OVERRIDE = os.environ.get("PROVISION_PHONE_BASE_SETTINGS_ID")
PHONE_LINE_BASE_SETTINGS_ID_OVERRIDE = os.environ.get("PROVISION_PHONE_LINE_BASE_SETTINGS_ID")
# Genesys meta-base id identifying the WebRTC phone/line base settings.
WEBRTC_META_BASE_ID = "developer_webrtc.json"
```

Then add after `derive_phone_name`:

```python
def build_phone_body(phone_name: str, user_id: str, cfg: dict) -> dict:
    """Assemble the POST body for a WebRTC phone assigned to ``user_id``.

    ``webRtcUser`` is what binds the phone to the person; ``lines`` must carry a
    ``lineBaseSettings`` id or the line won't register. ``cfg`` comes from
    ``resolve_phone_config``.
    """
    return {
        "name": phone_name,
        "site": {"id": cfg["site_id"]},
        "phoneBaseSettings": {"id": cfg["phone_base_settings_id"]},
        "lines": [{"name": phone_name, "lineBaseSettings": {"id": cfg["line_base_settings_id"]}}],
        "webRtcUser": {"id": user_id},
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/lawrence/code/genesys-mcp && uv run --group test python -m pytest tests/test_provision_users.py::TestBuildPhoneBody -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/lawrence/code/genesys-mcp
git add scripts/provision_users.py tests/test_provision_users.py
git commit -m "feat(provision): add phone constants + build_phone_body"
```

---

### Task 3: `resolve_phone_config` (site + base settings discovery)

**Files:**
- Modify: `scripts/provision_users.py` (add after `build_phone_body`)
- Test: `tests/test_provision_users.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_provision_users.py`:

```python
class TestResolvePhoneConfig:
    def _fake_api_factory(self, sites, pbs, lbs):
        """Return a fake call_api dispatching on path. Mirrors {entities:[...]}."""
        def fake_call_api(api, method, path, *, body=None, query=None):
            if path.endswith("/sites"):
                return {"entities": sites}
            if path.endswith("/phonebasesettings"):
                return {"entities": pbs}
            if path.endswith("/linebasesettings"):
                return {"entities": lbs}
            raise AssertionError(f"unexpected path {path}")
        return fake_call_api

    def test_discovers_by_metabase(self, monkeypatch):
        sites = [{"id": "site-syd", "name": "Prvidr Sydney"},
                 {"id": "other", "name": "Somewhere Else"}]
        pbs = [{"id": "pbs-webrtc", "name": "WebRTC Phone",
                "phoneMetaBase": {"id": "developer_webrtc.json"}},
               {"id": "pbs-sip", "name": "Generic SIP",
                "phoneMetaBase": {"id": "generic_sip.json"}}]
        lbs = [{"id": "lbs-webrtc", "name": "WebRTC Line",
                "lineMetaBase": {"id": "developer_webrtc.json"}}]
        monkeypatch.setattr(pu, "call_api", self._fake_api_factory(sites, pbs, lbs))
        cfg = pu.resolve_phone_config(object())
        assert cfg == {"site_id": "site-syd",
                       "phone_base_settings_id": "pbs-webrtc",
                       "line_base_settings_id": "lbs-webrtc"}

    def test_falls_back_to_name_contains_webrtc(self, monkeypatch):
        sites = [{"id": "site-syd", "name": "Prvidr Sydney"}]
        pbs = [{"id": "pbs-webrtc", "name": "Acme WebRTC base"}]  # no metaBase field
        lbs = [{"id": "lbs-webrtc", "name": "Acme WebRTC line"}]
        monkeypatch.setattr(pu, "call_api", self._fake_api_factory(sites, pbs, lbs))
        cfg = pu.resolve_phone_config(object())
        assert cfg["phone_base_settings_id"] == "pbs-webrtc"
        assert cfg["line_base_settings_id"] == "lbs-webrtc"

    def test_site_not_found_raises(self, monkeypatch):
        monkeypatch.setattr(pu, "call_api",
                            self._fake_api_factory([{"id": "x", "name": "Nope"}], [], []))
        with pytest.raises(RuntimeError, match="site"):
            pu.resolve_phone_config(object())

    def test_ambiguous_webrtc_raises(self, monkeypatch):
        sites = [{"id": "site-syd", "name": "Prvidr Sydney"}]
        pbs = [{"id": "a", "name": "WebRTC one"}, {"id": "b", "name": "WebRTC two"}]
        monkeypatch.setattr(pu, "call_api", self._fake_api_factory(sites, pbs, []))
        with pytest.raises(RuntimeError, match="phone base settings"):
            pu.resolve_phone_config(object())

    def test_env_overrides_short_circuit(self, monkeypatch):
        monkeypatch.setattr(pu, "PHONE_SITE_ID_OVERRIDE", "env-site")
        monkeypatch.setattr(pu, "PHONE_BASE_SETTINGS_ID_OVERRIDE", "env-pbs")
        monkeypatch.setattr(pu, "PHONE_LINE_BASE_SETTINGS_ID_OVERRIDE", "env-lbs")
        def boom(*a, **k):
            raise AssertionError("should not call API when all overrides set")
        monkeypatch.setattr(pu, "call_api", boom)
        cfg = pu.resolve_phone_config(object())
        assert cfg == {"site_id": "env-site",
                       "phone_base_settings_id": "env-pbs",
                       "line_base_settings_id": "env-lbs"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/lawrence/code/genesys-mcp && uv run --group test python -m pytest tests/test_provision_users.py::TestResolvePhoneConfig -v`
Expected: FAIL with `AttributeError: ... has no attribute 'resolve_phone_config'`

- [ ] **Step 3: Write minimal implementation**

In `scripts/provision_users.py`, add after `build_phone_body`:

```python
def _pick_webrtc(entities: list[dict], label: str) -> str:
    """Pick the WebRTC entry from a phone/line base-settings list.

    Prefers an exact ``phoneMetaBase``/``lineMetaBase`` id match; falls back to a
    unique name containing 'webrtc'. Raises if it can't choose unambiguously, so
    the operator sets the matching env override rather than getting a wrong phone.
    """
    for e in entities:
        meta = (e.get("phoneMetaBase") or e.get("lineMetaBase") or {}).get("id", "")
        if meta == WEBRTC_META_BASE_ID and e.get("id"):
            return e["id"]
    named = [e for e in entities if "webrtc" in (e.get("name") or "").lower() and e.get("id")]
    if len(named) == 1:
        return named[0]["id"]
    candidates = [e.get("name") for e in entities]
    raise RuntimeError(
        f"Could not uniquely identify the WebRTC {label}. Set the matching env "
        f"override. Candidates seen: {candidates}"
    )


def resolve_phone_config(api: gc.ApiClient) -> dict:
    """Resolve the site id + WebRTC phone/line base-settings ids for phone creation.

    Honours env overrides first; otherwise reads from the tenant. Requires the
    ``telephony:plugin:all`` permission (uses the write client). Returns a dict with
    keys ``site_id``, ``phone_base_settings_id``, ``line_base_settings_id``.
    """
    site_id = PHONE_SITE_ID_OVERRIDE
    if not site_id:
        sites = call_api(api, "GET", "/api/v2/telephony/providers/edges/sites",
                         query={"pageSize": 100}) or {}
        match = [s for s in (sites.get("entities") or []) if s.get("name") == PHONE_SITE_NAME]
        if not match:
            raise RuntimeError(
                f"Phone site {PHONE_SITE_NAME!r} not found (set PROVISION_PHONE_SITE "
                f"or PROVISION_PHONE_SITE_ID)."
            )
        site_id = match[0]["id"]

    pbs_id = PHONE_BASE_SETTINGS_ID_OVERRIDE
    if not pbs_id:
        pbs = call_api(api, "GET", "/api/v2/telephony/providers/edges/phonebasesettings",
                       query={"pageSize": 100}) or {}
        pbs_id = _pick_webrtc(pbs.get("entities") or [], "phone base settings")

    lbs_id = PHONE_LINE_BASE_SETTINGS_ID_OVERRIDE
    if not lbs_id:
        lbs = call_api(api, "GET", "/api/v2/telephony/providers/edges/linebasesettings",
                       query={"pageSize": 100}) or {}
        lbs_id = _pick_webrtc(lbs.get("entities") or [], "line base settings")

    return {"site_id": site_id, "phone_base_settings_id": pbs_id, "line_base_settings_id": lbs_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/lawrence/code/genesys-mcp && uv run --group test python -m pytest tests/test_provision_users.py::TestResolvePhoneConfig -v`
Expected: PASS (5 cases)

- [ ] **Step 5: Commit**

```bash
cd /Users/lawrence/code/genesys-mcp
git add scripts/provision_users.py tests/test_provision_users.py
git commit -m "feat(provision): add resolve_phone_config with env overrides + webrtc discovery"
```

---

### Task 4: `create_phone_for_user` (collision-safe creation)

**Files:**
- Modify: `scripts/provision_users.py` (add after `resolve_phone_config`)
- Test: `tests/test_provision_users.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_provision_users.py`:

```python
class TestCreatePhoneForUser:
    CFG = {"site_id": "s", "phone_base_settings_id": "p", "line_base_settings_id": "l"}

    def test_skips_when_phone_name_exists(self, monkeypatch):
        calls = []
        def fake_call_api(api, method, path, *, body=None, query=None):
            calls.append((method, path, query, body))
            if method == "GET":
                return {"entities": [{"id": "existing-phone", "name": "Jane.Doe"}]}
            raise AssertionError("POST must not happen when phone exists")
        monkeypatch.setattr(pu, "call_api", fake_call_api)
        status, pid = pu.create_phone_for_user(object(), "Jane.Doe", "u1", self.CFG)
        assert status == "skipped"
        assert pid == "existing-phone"
        assert all(m == "GET" for m, *_ in calls)

    def test_creates_when_absent(self, monkeypatch):
        seen = {}
        def fake_call_api(api, method, path, *, body=None, query=None):
            if method == "GET":
                return {"entities": []}
            seen["body"] = body
            return {"id": "new-phone"}
        monkeypatch.setattr(pu, "call_api", fake_call_api)
        status, pid = pu.create_phone_for_user(object(), "Jane.Doe", "u1", self.CFG)
        assert status == "created"
        assert pid == "new-phone"
        assert seen["body"]["webRtcUser"] == {"id": "u1"}
        assert seen["body"]["name"] == "Jane.Doe"
        assert seen["body"]["lines"][0]["lineBaseSettings"] == {"id": "l"}

    def test_propagates_post_failure(self, monkeypatch):
        from PureCloudPlatformClientV2.rest import ApiException
        def fake_call_api(api, method, path, *, body=None, query=None):
            if method == "GET":
                return {"entities": []}
            raise ApiException(status=400, reason="bad")
        monkeypatch.setattr(pu, "call_api", fake_call_api)
        with pytest.raises(ApiException):
            pu.create_phone_for_user(object(), "Jane.Doe", "u1", self.CFG)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/lawrence/code/genesys-mcp && uv run --group test python -m pytest tests/test_provision_users.py::TestCreatePhoneForUser -v`
Expected: FAIL with `AttributeError: ... has no attribute 'create_phone_for_user'`

- [ ] **Step 3: Write minimal implementation**

In `scripts/provision_users.py`, add after `resolve_phone_config`:

```python
def create_phone_for_user(api: gc.ApiClient, phone_name: str, user_id: str, cfg: dict) -> tuple[str, str | None]:
    """Create the WebRTC phone unless one with this name already exists.

    Returns ``("skipped", existing_id)`` if a phone named ``phone_name`` already
    exists (we don't reassign someone else's phone), else ``("created", new_id)``.
    Raises ``ApiException`` on POST failure — the caller decides best-effort.
    """
    existing = call_api(
        api, "GET", "/api/v2/telephony/providers/edges/phones",
        query={"name": phone_name, "pageSize": 1},
    ) or {}
    ents = existing.get("entities") or []
    if ents:
        return ("skipped", ents[0].get("id"))
    created = call_api(
        api, "POST", "/api/v2/telephony/providers/edges/phones",
        body=build_phone_body(phone_name, user_id, cfg),
    ) or {}
    return ("created", created.get("id"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/lawrence/code/genesys-mcp && uv run --group test python -m pytest tests/test_provision_users.py::TestCreatePhoneForUser -v`
Expected: PASS (3 cases)

- [ ] **Step 5: Commit**

```bash
cd /Users/lawrence/code/genesys-mcp
git add scripts/provision_users.py tests/test_provision_users.py
git commit -m "feat(provision): add collision-safe create_phone_for_user"
```

---

### Task 5: Add `phone` to `STEPS` and wire the phone step into `execute_user`

**Files:**
- Modify: `scripts/provision_users.py` — `STEPS` (~line 74); `execute_user` signature (~line 294) and new step block before the invite step (~line 513)

- [ ] **Step 1: Update `STEPS`**

Replace (line 74):

```python
STEPS = ("create", "patch", "skills", "languages", "groups", "wfm", "invite")
```

with:

```python
STEPS = ("create", "patch", "skills", "languages", "groups", "wfm", "phone", "invite")
```

- [ ] **Step 2: Add `phone_cfg` parameter to `execute_user`**

Replace the `execute_user` signature (the `def execute_user(...)` block, lines ~294-303):

```python
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
```

with:

```python
def execute_user(
    write_api: gc.ApiClient,
    snapshot: dict,
    target_email: str,
    target_name: str,
    ledger: Ledger,
    ledger_dir: Path,
    *,
    self_test: bool = False,
    phone_cfg: dict | None = None,
) -> Ledger:
```

- [ ] **Step 3: Insert the phone step before the invite step**

In `execute_user`, find the comment line `# ─── Step 7: Invite (must be last; skipped during self-test) ─────` (~line 513) and insert this block **immediately before** it:

```python
    # ─── Step 7: WebRTC phone (best-effort; assigned to the user) ──────────
    if not ledger.is_done("phone"):
        if self_test:
            # No throwaway phone — just confirm the telephony permission is present.
            try:
                retry(lambda: call_api(
                    write_api, "GET",
                    "/api/v2/telephony/providers/edges/phonebasesettings",
                    query={"pageSize": 1},
                ))()
                log.info("[%s] phone perm check OK (telephony:plugin:all present)", target_email)
            except ApiException as exc:
                fail("phone (perm check)", exc)
        elif phone_cfg is None:
            log.warning("[%s] no WebRTC phone config resolved — skipping phone creation", target_email)
        else:
            phone_name = derive_phone_name(target_email)
            try:
                status, phone_id = create_phone_for_user(
                    write_api, phone_name, ledger.user_id, phone_cfg,
                )
                if status == "skipped":
                    log.info("[%s] phone %r already exists (id=%s), skipping",
                             target_email, phone_name, phone_id)
                else:
                    log.info("[%s] CREATED WebRTC phone %r (id=%s)",
                             target_email, phone_name, phone_id)
            except ApiException as exc:
                # Best-effort: WebRTC auto-provisions on first login anyway.
                log.warning(
                    "[%s] phone creation failed (status=%s): %s — continuing",
                    target_email, getattr(exc, "status", "?"), _err_body(exc),
                )
        ledger.mark_done("phone")
        ledger.save(ledger_dir)
```

Then renumber the existing invite comment from `Step 7` to `Step 8`:

Replace `    # ─── Step 7: Invite (must be last; skipped during self-test) ─────`
with `    # ─── Step 8: Invite (must be last; skipped during self-test) ─────`

- [ ] **Step 4: Verify the module still imports and tests pass**

Run: `cd /Users/lawrence/code/genesys-mcp && uv run --group test python -m pytest tests/test_provision_users.py -v`
Expected: PASS (all prior cases; no behaviour change to tested helpers)

Run: `cd /Users/lawrence/code/genesys-mcp && .venv/bin/python -c "import importlib.util,sys; sys.path.insert(0,'src'); s=importlib.util.spec_from_file_location('p','scripts/provision_users.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.STEPS)"`
Expected: prints `('create', 'patch', 'skills', 'languages', 'groups', 'wfm', 'phone', 'invite')`

- [ ] **Step 5: Commit**

```bash
cd /Users/lawrence/code/genesys-mcp
git add scripts/provision_users.py
git commit -m "feat(provision): wire best-effort WebRTC phone step into execute_user"
```

---

### Task 6: Extract `process_one` + `print_summary`; add phone line to `print_plan`; resolve `phone_cfg` in `main`

**Files:**
- Modify: `scripts/provision_users.py` — `print_plan` (~line 621-649); add `process_one`/`print_summary`/`resolve_phone_cfg_safe`; rework `main`'s per-user loop (~line 807-856)

- [ ] **Step 1: Add the PHONE line to `print_plan`**

In `print_plan`, find the WFM line block (~lines 647-648):

```python
    if snapshot.get("wfm_management_unit"):
        print(f"  WFM: move into management unit {snapshot['wfm_management_unit'].get('name', '?')}")
    print(f"  INVITE: send activation email to {target_email}")
```

Replace with:

```python
    if snapshot.get("wfm_management_unit"):
        print(f"  WFM: move into management unit {snapshot['wfm_management_unit'].get('name', '?')}")
    print(f"  PHONE: create WebRTC phone \"{derive_phone_name(target_email)}\" at site "
          f"{PHONE_SITE_NAME} (base settings resolved at execute time)")
    print(f"  INVITE: send activation email to {target_email}")
```

- [ ] **Step 2: Add `process_one`, `print_summary`, and `resolve_phone_cfg_safe` helpers**

Add immediately before `def main(` (~line 656):

```python
def process_one(
    email: str,
    name: str,
    snapshot: dict,
    read_api: gc.ApiClient,
    write_api: gc.ApiClient | None,
    ledger_dir: Path,
    phone_cfg: dict | None,
    *,
    confirm: bool,
    reconcile: bool,
) -> tuple[str, str, str, str]:
    """Process a single email: dry-run plan or full execute. Returns a summary row.

    Shared by ``main``'s batch loop and ``--interactive`` so both behave identically.
    """
    existing = find_user_by_email(read_api, email)
    ledger = Ledger.load_or_new(ledger_dir, email)
    if ledger.user_id is None and existing:
        ledger.user_id = existing["id"]

    if existing and not ledger.completed_steps and not reconcile:
        msg = "skipped — exists, no ledger (use --reconcile to bring in line with template)"
        print(f"  ⚠ {msg}")
        return ("⊘", email, existing.get("id", "?"), msg)

    if not confirm:
        print_plan(snapshot, email, name)
        return ("•", email, existing.get("id", "—") if existing else "—", "dry-run only")

    if write_api is None:
        return ("✗", email, "—", "write client not initialised")

    try:
        execute_user(write_api, snapshot, email, name, ledger, ledger_dir, phone_cfg=phone_cfg)
        note = "invite sent (expires 14 days)" if ledger.is_done("invite") else "completed (no invite)"
        symbol = "↻" if existing and ledger.completed_steps != list(STEPS) else "✓"
        return (symbol, email, ledger.user_id or "?", note)
    except ApiException as exc:
        return ("✗", email, ledger.user_id or "?",
                f"failed: status={exc.status} body={_err_body(exc)[:120]}")
    except Exception as exc:  # noqa: BLE001 — record and continue the batch
        return ("✗", email, ledger.user_id or "?", f"failed: {exc}")


def print_summary(summary: list[tuple[str, str, str, str]], ledger_dir: Path) -> int:
    """Print the summary table and return process exit code (0 ok, 1 if any failed)."""
    print("\n" + "─" * 90)
    print("SUMMARY")
    print("─" * 90)
    for sym, email, uid, note in summary:
        print(f" {sym}  {email:<40} {uid:<40} {note}")
    print("─" * 90)
    print(f"Ledger dir: {ledger_dir}")
    failed = sum(1 for s in summary if s[0] == "✗")
    return 0 if failed == 0 else 1


def resolve_phone_cfg_safe(write_api: gc.ApiClient) -> dict | None:
    """Resolve phone config once, logging (not raising) on failure so the rest of
    the batch still runs and the phone step degrades to a per-user warning."""
    try:
        cfg = resolve_phone_config(write_api)
        log.info("Phone config resolved: site=%s phoneBaseSettings=%s lineBaseSettings=%s",
                 cfg["site_id"], cfg["phone_base_settings_id"], cfg["line_base_settings_id"])
        return cfg
    except (ApiException, RuntimeError) as exc:
        log.error("Could not resolve WebRTC phone config (%s). Phone step will be "
                  "skipped for all users this run. Check telephony:plugin:all and the "
                  "PROVISION_PHONE_* env overrides.", exc)
        return None
```

- [ ] **Step 3: Rework `main`'s per-user loop to use the helpers**

In `main`, replace the entire block from `# Phase 2/3: per-user processing.` through the end of the summary printing and `return` (lines ~807-856):

```python
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
```

with:

```python
    # Resolve WebRTC phone config once (only when we'll actually write).
    phone_cfg = resolve_phone_cfg_safe(write_api) if (args.confirm and write_api) else None

    # Phase 2/3: per-user processing.
    print(f"\nProcessing {len(emails)} email(s):\n")
    summary: list[tuple[str, str, str, str]] = []
    for i, email in enumerate(emails, 1):
        name = derive_full_name(email)
        print(f"[{i}/{len(emails)}] {email} → \"{name}\"")
        summary.append(process_one(
            email, name, snapshot, read_api, write_api, ledger_dir, phone_cfg,
            confirm=args.confirm, reconcile=args.reconcile,
        ))

    # Phase 4: summary table.
    return print_summary(summary, ledger_dir)
```

- [ ] **Step 4: Verify import + tests**

Run: `cd /Users/lawrence/code/genesys-mcp && uv run --group test python -m pytest tests/test_provision_users.py -v`
Expected: PASS (all helper cases unaffected)

Run a dry-run smoke test (read-only; no writes) against a real template to confirm the PHONE line prints — substitute a real template + a fake new email:
`cd /Users/lawrence/code/genesys-mcp && .venv/bin/python scripts/provision_users.py --template-email <existing-agent>@prvidr.com --email test.newstarter@prvidr.com`
Expected: plan prints a `PHONE: create WebRTC phone "Test.Newstarter" at site Prvidr Sydney …` line; SUMMARY shows `• … dry-run only`.

- [ ] **Step 5: Commit**

```bash
cd /Users/lawrence/code/genesys-mcp
git add scripts/provision_users.py
git commit -m "refactor(provision): extract process_one/print_summary; add PHONE plan line + phone_cfg resolution"
```

---

### Task 7: `--interactive` mode + `--discover-phone-settings` diagnostic

**Files:**
- Modify: `scripts/provision_users.py` — argparse (~line 685), the `--template-email` required-check (~line 700), the `will_write` line (~line 725), and a new branch in `main` after the write-client init / before the snapshot (~line 738)

- [ ] **Step 1: Add the CLI flags**

In `main`, after the `-v/--verbose` argument (~line 685), add:

```python
    parser.add_argument("--interactive", action="store_true",
        help="Prompt for the template email and new-starter emails, show the plan, "
             "then ask before writing. Used by the double-click launchers.")
    parser.add_argument("--discover-phone-settings", action="store_true",
        help="Print the resolved WebRTC site/phone/line base-settings ids and exit "
             "(needs telephony:plugin:all on the write client). Use to set PROVISION_PHONE_* overrides.")
```

- [ ] **Step 2: Add the interactive input helper**

Add this function immediately before `def main(`:

```python
def prompt_interactive_inputs() -> tuple[str, list[str]]:
    """Prompt for a template email and new-starter emails (one per line, blank to end;
    a single comma/space-separated line also works). Returns (template_email, emails)."""
    print("\n=== Provision new agents from a template ===\n")
    template = input("Template agent email (existing user to copy from): ").strip()
    print("\nNew-starter emails — one per line. Blank line when done "
          "(or paste several separated by commas/spaces):")
    emails: list[str] = []
    while True:
        try:
            line = input("  > ").strip()
        except EOFError:
            break
        if not line:
            break
        parts = re.split(r"[,\s]+", line)
        emails.extend(p for p in parts if p)
    seen: set[str] = set()
    deduped = [e for e in emails if not (e in seen or seen.add(e))]
    return template, deduped
```

- [ ] **Step 3: Relax the `--template-email` required check**

Replace (~line 700):

```python
    if not args.template_email:
        parser.error("--template-email is required (use --self-test --template-email <known-good-agent>)")
```

with:

```python
    if not args.template_email and not (args.interactive or args.discover_phone_settings):
        parser.error("--template-email is required (use --self-test --template-email <known-good-agent>, "
                     "or --interactive to be prompted)")
```

- [ ] **Step 4: Make interactive/diagnostic init the write client**

Replace (~line 725):

```python
    will_write = args.confirm or args.self_test
```

with:

```python
    will_write = args.confirm or args.self_test or args.interactive or args.discover_phone_settings
```

- [ ] **Step 5: Add the diagnostic + interactive branch**

Immediately **after** the write-client init block and **before** `# Phase 1: snapshot the template` (~line 738), add:

```python
    # Diagnostic: print resolved phone settings and exit.
    if args.discover_phone_settings:
        if write_api is None:
            return 2
        try:
            cfg = resolve_phone_config(write_api)
        except (ApiException, RuntimeError) as exc:
            print(f"Could not resolve phone settings: {exc}", file=sys.stderr)
            return 1
        print("Resolved WebRTC phone settings:")
        print(f"  PROVISION_PHONE_SITE_ID={cfg['site_id']}")
        print(f"  PROVISION_PHONE_BASE_SETTINGS_ID={cfg['phone_base_settings_id']}")
        print(f"  PROVISION_PHONE_LINE_BASE_SETTINGS_ID={cfg['line_base_settings_id']}")
        return 0

    # Interactive mode: gather inputs, preview, confirm, execute.
    if args.interactive:
        if write_api is None:
            return 2
        template_email, emails = prompt_interactive_inputs()
        if not template_email:
            print("No template email given. Aborted.", file=sys.stderr)
            return 1
        if not emails:
            print("No new-starter emails given. Aborted.", file=sys.stderr)
            return 1
        args.template_email = template_email

        snapshot = snapshot_template(read_api, template_email, refresh=args.refresh_template)
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        ledger_dir = LEDGER_BASE / run_id

        # Preview (dry-run): print the plan for each.
        print(f"\nPLAN — template {template_email}, {len(emails)} new agent(s):\n")
        for i, email in enumerate(emails, 1):
            name = derive_full_name(email)
            print(f"[{i}/{len(emails)}] {email} → \"{name}\"")
            process_one(email, name, snapshot, read_api, write_api, ledger_dir, None,
                        confirm=False, reconcile=args.reconcile)

        ans = input("\nProceed and create these agents (with phones + invites)? [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted — nothing written.")
            return 1

        phone_cfg = resolve_phone_cfg_safe(write_api)
        print(f"\nProcessing {len(emails)} email(s):\n")
        summary: list[tuple[str, str, str, str]] = []
        for i, email in enumerate(emails, 1):
            name = derive_full_name(email)
            print(f"[{i}/{len(emails)}] {email} → \"{name}\"")
            summary.append(process_one(email, name, snapshot, read_api, write_api,
                                       ledger_dir, phone_cfg, confirm=True,
                                       reconcile=args.reconcile))
        return print_summary(summary, ledger_dir)
```

(All references — `snapshot_template`, `datetime`, `LEDGER_BASE`, `process_one`, `resolve_phone_cfg_safe`, `print_summary` — are already imported or defined earlier. Interactive `return`s before the non-interactive snapshot line, so that path is untouched.)

- [ ] **Step 6: Verify**

Run: `cd /Users/lawrence/code/genesys-mcp && uv run --group test python -m pytest tests/test_provision_users.py -v`
Expected: PASS

Run (pipe inputs; answers `n` so nothing is written):
`cd /Users/lawrence/code/genesys-mcp && printf '<existing-agent>@prvidr.com\ntest.newstarter@prvidr.com\n\nn\n' | .venv/bin/python scripts/provision_users.py --interactive`
Expected: prints the plan including the PHONE line, then "Aborted — nothing written."

- [ ] **Step 7: Commit**

```bash
cd /Users/lawrence/code/genesys-mcp
git add scripts/provision_users.py
git commit -m "feat(provision): add --interactive mode and --discover-phone-settings diagnostic"
```

---

### Task 8: macOS launcher `provision_users.command`

**Files:**
- Create: `scripts/provision_users.command`

- [ ] **Step 1: Create the launcher**

```bash
#!/bin/bash
# provision_users.command — double-click launcher (macOS).
# Opens Terminal and runs provision_users.py in interactive mode.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/.." && pwd)"
cd "$REPO"

if [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
else
  PY="$(command -v python3 || true)"
fi

if [ -z "${PY:-}" ]; then
  echo "ERROR: Python 3 not found. Install Python 3, or create the repo venv (.venv)."
  echo
  read -r -p "Press Enter to close…"
  exit 1
fi

"$PY" scripts/provision_users.py --interactive || true
echo
read -r -p "Press Enter to close…"
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x /Users/lawrence/code/genesys-mcp/scripts/provision_users.command`

- [ ] **Step 3: Verify it launches (manual)**

Run: `cd /Users/lawrence/code/genesys-mcp && printf '\n\n' | ./scripts/provision_users.command`
Expected: prints "No template email given. Aborted." then the press-Enter prompt; no path errors. (Double-clicking in Finder is the real operator path and opens Terminal automatically.)

- [ ] **Step 4: Commit**

```bash
cd /Users/lawrence/code/genesys-mcp
git add scripts/provision_users.command
git commit -m "feat(provision): add macOS double-click launcher"
```

---

### Task 9: Windows launcher `provision_users.bat`

**Files:**
- Create: `scripts/provision_users.bat`

- [ ] **Step 1: Create the launcher**

```bat
@echo off
REM provision_users.bat - double-click launcher (Windows).
REM Opens cmd and runs provision_users.py in interactive mode.
setlocal
set "REPO=%~dp0.."
pushd "%REPO%"

set "VENVPY=%REPO%\.venv\Scripts\python.exe"
if exist "%VENVPY%" (
  "%VENVPY%" scripts\provision_users.py --interactive
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -3 scripts\provision_users.py --interactive
  ) else (
    python scripts\provision_users.py --interactive
  )
)

popd
echo.
pause
```

- [ ] **Step 2: Verify syntax (manual / cross-check)**

Cannot run `.bat` on macOS. Cross-check by eye: `%~dp0` resolves to `scripts\` (trailing backslash), so `%REPO%` = `scripts\..` and `pushd` normalises it. Real verification happens on a Windows machine (double-click → cmd opens, prompts appear, `pause` holds the window).

- [ ] **Step 3: Commit**

```bash
cd /Users/lawrence/code/genesys-mcp
git add scripts/provision_users.bat
git commit -m "feat(provision): add Windows double-click launcher"
```

---

### Task 10: Update `scripts/README.md`

**Files:**
- Modify: `scripts/README.md` — Phase 0 permission table (~line 32-40), intro (~line 9), the "What gets executed" section (~line 120-134), self-test paragraph (~line 81), "intentionally don't work" (~line 183), and new quick-start/env sections.

- [ ] **Step 1: Add the telephony permission to the Phase 0 table**

In the permission table (~lines 32-40), add a row after the WFM row:

```markdown
   | Create WebRTC phone                 | `telephony:plugin:all`           |
```

And add a note paragraph after the `directory:user:delete` note (~line 46):

```markdown
   **`telephony:plugin:all` is broad.** Genesys has no granular "add phone" scope —
   this single permission grants full telephony plugin admin (edges, sites, trunks,
   phones). It is required for the phone step. If you do not want this breadth, leave
   the phone config unresolved (no telephony permission) — the step degrades to a
   per-user warning and WebRTC still auto-provisions on first sign-in.
```

- [ ] **Step 2: Update intro line and the step list**

Append to the opening description sentence (~line 9): ` Also creates a named WebRTC phone (Firstname.Lastname) at the configured site and assigns it to the new agent.`

In "What gets executed (7 steps per user)" (~line 120), change the heading to "What gets executed (8 steps per user)" and insert before the invite step:

```markdown
7. `POST /api/v2/telephony/providers/edges/phones` — create a WebRTC phone named `Firstname.Lastname` at site `Prvidr Sydney`, assigned to the user via `webRtcUser`. **Best-effort:** a failure (duplicate name, transient error, or unresolved config) logs a warning and continues — WebRTC auto-provisions on first sign-in regardless. Skipped if a phone with that name already exists.
```

and renumber the invite step to `8.`.

- [ ] **Step 3: Document env overrides + double-click quick-start**

Add a new subsection after "Day-to-day use" (~line 100):

```markdown
### Just double-click it (no command line)

Operators can run the whole flow without the terminal:

- **macOS:** double-click `scripts/provision_users.command` (Finder opens Terminal automatically).
- **Windows:** double-click `scripts\provision_users.bat`.

Either one prompts for the **template agent email**, then the **new-starter emails**
(one per line, blank line to finish), prints the plan, and asks for a final
`y` before creating anything. Both prefer the repo's `.venv`; on Windows they fall
back to `py -3`/`python` if there's no venv.

### Phone provisioning env overrides

The phone step auto-discovers the site and WebRTC base settings, but you can pin them:

| Env var | Effect |
|---|---|
| `PROVISION_PHONE_SITE` | Site name to look up (default `Prvidr Sydney`). |
| `PROVISION_PHONE_SITE_ID` | Skip the lookup; use this site id directly. |
| `PROVISION_PHONE_BASE_SETTINGS_ID` | Use this phone base-settings id (skip discovery). |
| `PROVISION_PHONE_LINE_BASE_SETTINGS_ID` | Use this line base-settings id (skip discovery). |

Run `python scripts/provision_users.py --discover-phone-settings` (after Phase 0)
to print the resolved ids in ready-to-paste env form.
```

- [ ] **Step 4: Update the self-test paragraph and "intentionally don't work"**

In the `--self-test` description (~line 81), add: ` The self-test also does a read-only check that the role holds telephony:plugin:all (it does not create a throwaway phone).`

In "Things that intentionally don't work" (~line 183), update the "Physical SIP phones" bullet so it's clear only SIP/desk phones are skipped — WebRTC phones are now created by step 7.

- [ ] **Step 5: Commit**

```bash
cd /Users/lawrence/code/genesys-mcp
git add scripts/README.md
git commit -m "docs(provision): document phone step, telephony perm, launchers, env overrides"
```

---

### Task 11: Live verification (gated on Phase 0)

**Prerequisite:** `telephony:plugin:all` added to the write OAuth role in Genesys admin.

- [ ] **Step 1: Self-test confirms the telephony permission**

Run: `cd /Users/lawrence/code/genesys-mcp && .venv/bin/python scripts/provision_users.py --self-test --template-email <existing-agent>@prvidr.com`
Expected: all steps succeed including `phone perm check OK (telephony:plugin:all present)`. Delete the throwaway user in Genesys admin afterwards.

- [ ] **Step 2: Confirm discovery resolves real ids**

Run: `cd /Users/lawrence/code/genesys-mcp && .venv/bin/python scripts/provision_users.py --discover-phone-settings`
Expected: prints `PROVISION_PHONE_SITE_ID=93149b41-…` plus the WebRTC phone/line base-settings ids. If it errors with "could not uniquely identify", set the printed candidate's id via the matching `PROVISION_PHONE_*` env var.

- [ ] **Step 3: One real new starter, end to end**

Run the interactive launcher (or `--email test.newstarter@prvidr.com --template-email <agent>@prvidr.com --confirm`), then verify in Genesys admin: the user exists, has the cloned skills/groups/WFM unit, and a WebRTC phone named `Test.Newstarter` at Prvidr Sydney assigned to them. Confirm the invite email arrived.

- [ ] **Step 4: Mark done** — no commit (verification only).

---

## Self-Review

**Spec coverage:**
- Phone step (name/site/assign, best-effort, before invite) → Tasks 2–6. ✓
- `telephony:plugin:all` on write client + Phase 0 → Task 10 + Task 11 prerequisite. ✓
- Auto-discovery + env overrides → Task 3. ✓
- Self-test read-only perm check (no throwaway phone) → Task 5 (self_test branch). ✓
- `--interactive` (template + emails one-per-line, preview, confirm) → Task 7. ✓
- macOS `.command` + Windows `.bat`, prefer `.venv` → Tasks 8, 9. ✓
- Docs (perm table, 8 steps, quick-start, env) → Task 10. ✓
- Collision handling (skip if name exists) → Task 4. ✓

**Placeholder scan:** No TBD/TODO; all code blocks complete; `<existing-agent>@prvidr.com` is an intentional operator-supplied value, not a code placeholder.

**Type consistency:** `resolve_phone_config` returns `{site_id, phone_base_settings_id, line_base_settings_id}`; `build_phone_body`, `create_phone_for_user`, the `execute_user` phone step, and tests all use those exact keys. `process_one(..., confirm=, reconcile=)` signature matches both call sites (main loop + interactive). `STEPS` includes `phone` so the `↻ vs ✓` summary logic stays correct.

**Decisions honoured:** telephony on existing write client (Task 10); best-effort failure (Task 5); phone between wfm/invite (Task 5); self-test read-only (Task 5); emails one-per-line (Task 7); `Firstname.Lastname` (Task 1).
