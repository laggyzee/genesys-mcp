"""Directory tools: queues, users, user lookup."""

from __future__ import annotations

import logging
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


# v1.4: process-lifetime cache of presence-definition UUID → label. Presences
# change rarely (typically only when an admin adds/edits a custom presence),
# so a never-expire cache is the right trade-off. Restart the MCP server to
# refresh. When a UUID isn't in the cache (e.g. a presence added mid-session),
# we return label=None rather than re-fetching the entire definitions list.
_PRESENCE_LABEL_CACHE: dict[str, str] = {}
_PRESENCE_CACHE_LOADED: bool = False


def _load_presence_label_cache() -> None:
    """Populate _PRESENCE_LABEL_CACHE from /api/v2/presence/definitions.

    Called lazily on first request that needs a label. Subsequent requests
    hit the cache. Errors are swallowed (the caller gets label=None) so a
    permission gap on presence:definition:view doesn't break the broader
    presence-now query.
    """
    global _PRESENCE_CACHE_LOADED
    if _PRESENCE_CACHE_LOADED:
        return
    try:
        api = gc.PresenceApi(get_api())
        page_number = 1
        while True:
            resp = with_retry(api.get_presence_definitions)(
                page_size=200, page_number=page_number, deactivated="any",
            )
            entities = getattr(resp, "entities", None) or []
            for e in entities:
                pid = getattr(e, "id", None)
                labels = getattr(e, "language_labels", None) or {}
                label = next(iter(labels.values())) if labels else None
                if pid and label:
                    _PRESENCE_LABEL_CACHE[pid] = label
            if not entities or len(entities) < 200:
                break
            page_number += 1
            if page_number > 10:
                break
        _PRESENCE_CACHE_LOADED = True
        logger.info(
            "presence label cache loaded with %d entries",
            len(_PRESENCE_LABEL_CACHE),
        )
    except Exception as exc:
        logger.info(
            "presence label cache load failed (continuing without labels): %s",
            exc,
        )
        # Mark loaded anyway — don't retry every call. A restart re-attempts.
        _PRESENCE_CACHE_LOADED = True


def _presence_label_for(presence_definition_id: str | None) -> str | None:
    """Look up a presence label from the cache; load lazily on first call."""
    if not presence_definition_id:
        return None
    if not _PRESENCE_CACHE_LOADED:
        _load_presence_label_cache()
    return _PRESENCE_LABEL_CACHE.get(presence_definition_id)


def _queue_row(q: Any) -> dict:
    return {
        "id": q.id,
        "name": q.name,
        "member_count": getattr(q, "member_count", None),
        "division": getattr(getattr(q, "division", None), "name", None),
        "description": getattr(q, "description", None),
    }


def _user_row(u: Any) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "email": getattr(u, "email", None),
        "title": getattr(u, "title", None),
        "state": getattr(u, "state", None),
        "department": getattr(u, "department", None),
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_queues(
        name_contains: str | None = Field(
            default=None, description="Case-insensitive substring match on queue name."
        ),
        page_size: int = Field(default=100, ge=1, le=500),
        page_number: int = Field(default=1, ge=1),
    ) -> dict:
        """List routing queues. Use this to resolve a queue name to its id before calling analytics tools."""
        api = gc.RoutingApi(get_api())
        kwargs: dict[str, Any] = {"page_size": page_size, "page_number": page_number}
        if name_contains:
            kwargs["name"] = f"*{name_contains}*"
        resp = with_retry(api.get_routing_queues)(**kwargs)
        return {
            "total": resp.total,
            "page_number": resp.page_number,
            "page_size": resp.page_size,
            "queues": [_queue_row(q) for q in (resp.entities or [])],
        }

    @mcp.tool()
    def list_users(
        email_contains: str | None = Field(default=None, description="Substring match on email."),
        state: str = Field(
            default="active",
            description="User state filter: 'active', 'inactive', or 'deleted'.",
        ),
        page_size: int = Field(default=100, ge=1, le=500),
        page_number: int = Field(default=1, ge=1),
    ) -> dict:
        """List users (agents) in the organisation."""
        api = gc.UsersApi(get_api())
        resp = with_retry(api.get_users)(
            page_size=page_size, page_number=page_number, state=state
        )
        rows = [_user_row(u) for u in (resp.entities or [])]
        if email_contains:
            needle = email_contains.lower()
            rows = [r for r in rows if (r.get("email") or "").lower().find(needle) >= 0]
        return {
            "total": resp.total,
            "page_number": resp.page_number,
            "page_size": resp.page_size,
            "users": rows,
        }

    @mcp.tool()
    def find_user_by_email(
        email: str = Field(description="Exact email address to look up."),
    ) -> dict:
        """Resolve a user by email via the search API. Returns the first match or an empty result."""
        api = gc.SearchApi(get_api())
        body = {
            "pageSize": 5,
            "pageNumber": 1,
            "query": [
                {"type": "EXACT", "fields": ["email"], "value": email},
            ],
        }
        resp = with_retry(api.post_users_search)(body)
        results = to_dict(resp).get("results") or []
        return {"match_count": len(results), "user": results[0] if results else None}

    @mcp.tool()
    def find_user(
        query: str | None = Field(
            default=None,
            description=(
                "Single free-text search across name and email. Returns ranked "
                "matches. Mutually exclusive with `name_contains_list` — pass "
                "exactly one of the two."
            ),
        ),
        name_contains_list: list[str] | None = Field(
            default=None,
            description=(
                "(v1.4+) Batch mode: list of name fragments to resolve in one "
                "call. Each fragment runs as a separate TERM search "
                "concurrently; results are grouped per input query. Useful for "
                "TL workflows that need to resolve a target + peer set "
                "(typically 10-20 names) without N sequential round-trips."
            ),
        ),
        page_size: int = Field(default=10, ge=1, le=25),
    ) -> dict:
        """Search for users by name OR email. Use this when you only have a person's name.

        Backed by /api/v2/users/search with TERM-style query against name+email fields.
        For exact email match prefer find_user_by_email.

        **Single mode** (default): pass ``query`` — returns ``{match_count, users}``
        like every earlier version.

        **Batch mode** (v1.4+): pass ``name_contains_list`` — returns
        ``{matches: [{name_query, candidates}], unmatched: [name_query, ...]}``.
        Concurrent fan-out (bounded thread pool); wall time is roughly the
        slowest individual search, not the sum.
        """
        api = gc.SearchApi(get_api())

        # Validate the mutex contract up-front for clear errors.
        if query is None and not name_contains_list:
            raise ValueError(
                "find_user requires either `query` (single mode) or "
                "`name_contains_list` (batch mode)."
            )
        if query is not None and name_contains_list:
            raise ValueError(
                "find_user: pass either `query` or `name_contains_list`, not both."
            )

        # Single mode — original v1.0 shape, unchanged.
        if query is not None:
            body = {
                "pageSize": page_size,
                "pageNumber": 1,
                "query": [
                    {"type": "TERM", "fields": ["name", "email"], "value": query},
                ],
            }
            resp = with_retry(api.post_users_search)(body)
            results = to_dict(resp).get("results") or []
            return {"match_count": len(results), "users": results}

        # Batch mode — concurrent per-query search.
        from concurrent.futures import ThreadPoolExecutor

        def _search_one(q: str) -> tuple[str, list[dict]]:
            body = {
                "pageSize": page_size,
                "pageNumber": 1,
                "query": [
                    {"type": "TERM", "fields": ["name", "email"], "value": q},
                ],
            }
            try:
                resp = with_retry(api.post_users_search)(body)
            except Exception:
                # Surface the per-query failure as empty; total failures
                # still propagate via empty unmatched + zero candidates.
                return q, []
            return q, (to_dict(resp).get("results") or [])

        matches: list[dict] = []
        unmatched: list[str] = []
        # Keep concurrency modest — search is cheap but we don't want to
        # hammer Genesys with 50 simultaneous searches if someone passes
        # a long list.
        max_workers = min(8, len(name_contains_list))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_search_one, q) for q in name_contains_list]
            for fut in futures:
                q, candidates = fut.result()
                if candidates:
                    matches.append({"name_query": q, "candidates": candidates})
                else:
                    unmatched.append(q)
        return {
            "mode": "batch",
            "total_queries": len(name_contains_list),
            "matched_queries": len(matches),
            "matches": matches,
            "unmatched": unmatched,
        }

    @mcp.tool()
    def list_wrapup_codes(
        name_contains: str | None = Field(
            default=None, description="Case-insensitive substring filter on code name."
        ),
        page_size: int = Field(default=200, ge=1, le=500),
        page_number: int = Field(default=1, ge=1),
    ) -> dict:
        """List org-wide wrap-up codes. Use this to resolve wrapUpCode UUIDs returned by
        queue_performance / get_conversation into human-readable disposition names.
        """
        api = gc.RoutingApi(get_api())
        kwargs: dict[str, Any] = {"page_size": page_size, "page_number": page_number}
        if name_contains:
            kwargs["name"] = f"*{name_contains}*"
        resp = with_retry(api.get_routing_wrapupcodes)(**kwargs)
        codes = [
            {
                "id": c.id,
                "name": c.name,
                "division_id": getattr(getattr(c, "division", None), "id", None),
                "description": getattr(c, "description", None),
            }
            for c in (resp.entities or [])
        ]
        return {
            "total": resp.total,
            "page_number": resp.page_number,
            "page_size": resp.page_size,
            "wrapup_codes": codes,
        }

    @mcp.tool()
    def get_user_routing_status(
        user_ids: list[str] = Field(
            description="User ids to fetch routing status for. Use list_users / find_user to resolve names.",
        ),
    ) -> dict:
        """Current per-user routing status (INTERACTING / IDLE / OFF_QUEUE / NOT_RESPONDING)
        and the timestamp of the last status change. One row per user.
        """
        api = gc.UsersApi(get_api())
        out = []
        for uid in user_ids:
            try:
                resp = with_retry(api.get_user_routingstatus)(user_id=uid)
                data = to_dict(resp) or {}
                out.append({
                    "user_id": uid,
                    "routing_status": data.get("status"),
                    "start_time": data.get("startTime"),
                })
            except Exception as exc:
                out.append({"user_id": uid, "error": str(exc)})
        return {"results": out}

    @mcp.tool()
    def get_user_queues(
        user_id: str = Field(description="User id."),
        page_size: int = Field(default=100, ge=1, le=500),
        page_number: int = Field(default=1, ge=1),
        joined_only: bool = Field(
            default=True,
            description="If true, only return queues the user is currently joined to (typical use). Set false to include queues they're a member of but not joined.",
        ),
    ) -> dict:
        """List queues a user is a member of. Useful for capacity questions like
        'can we move someone onto the General queue right now?' — returns the queues
        this agent is configured to take, with their joined state.
        """
        api = gc.UsersApi(get_api())
        resp = with_retry(api.get_user_queues)(
            user_id=user_id, page_size=page_size, page_number=page_number, joined=joined_only
        )
        rows = [
            {
                "id": q.id,
                "name": q.name,
                "joined": getattr(q, "joined", None),
                "member_count": getattr(q, "member_count", None),
            }
            for q in (resp.entities or [])
        ]
        return {
            "total": resp.total,
            "page_number": resp.page_number,
            "page_size": resp.page_size,
            "queues": rows,
        }

    @mcp.tool()
    def list_routing_skills(
        name_contains: str | None = Field(
            default=None, description="Case-insensitive substring filter on skill name."
        ),
        page_size: int = Field(default=100, ge=1, le=500),
        page_number: int = Field(default=1, ge=1),
    ) -> dict:
        """List all routing skills configured in the org. Useful for understanding
        why one queue's EWT is high (skill-restricted agent pool).
        """
        api = gc.RoutingApi(get_api())
        kwargs: dict[str, Any] = {"page_size": page_size, "page_number": page_number}
        if name_contains:
            kwargs["name"] = f"*{name_contains}*"
        resp = with_retry(api.get_routing_skills)(**kwargs)
        skills = [{"id": s.id, "name": s.name} for s in (resp.entities or [])]
        return {
            "total": resp.total,
            "page_number": resp.page_number,
            "page_size": resp.page_size,
            "skills": skills,
        }

    @mcp.tool()
    def list_org_presences(
        name_contains: str | None = Field(
            default=None,
            description=(
                "Case-insensitive substring filter on presence label (e.g. "
                "'Pre Break', 'Coaching'). Skip the filter to get every "
                "org-level presence."
            ),
        ),
        deactivated: bool = Field(
            default=False,
            description="Include deactivated presences too (default false — active only).",
        ),
    ) -> dict:
        """List org-level presence definitions with their UUIDs + labels.

        v1.3+: closes the v1.0 "where do I find the pre-break presence id?"
        gap. The wizard auto-discovers it, but interactive users hitting the
        MCP fresh need a way to look it up by label.

        Useful for:

        - Tenant setup: *"What's the UUID for our 'Pre Break' presence?"*
        - break_overrun_report config: pass the returned id as
          ``pre_break_organization_presence_id``.
        - General audit: see every custom presence the org has defined
          (Coaching, Training, Project Work, etc.).

        Returns each presence's id, system presence base (e.g. 'BUSY',
        'AWAY'), and the org-defined label in the tenant's primary language.
        Endpoint: ``GET /api/v2/presence/definitions``. Needs
        ``presence:definition:view`` (typically bundled into ``presence:view``).
        """
        api = gc.PresenceApi(get_api())
        page_size = 200
        page_number = 1
        out: list[dict] = []
        while True:
            resp = with_retry(api.get_presence_definitions)(
                page_size=page_size,
                page_number=page_number,
                deactivated=str(bool(deactivated)).lower(),
            )
            entities = getattr(resp, "entities", None) or []
            for e in entities:
                # Each definition has languageLabels: {"en": "Pre Break", ...}
                labels = getattr(e, "language_labels", None) or {}
                # Prefer the first label; tenant primary language usually wins
                label = next(iter(labels.values())) if labels else None
                if name_contains and label and name_contains.lower() not in label.lower():
                    continue
                out.append({
                    "id": getattr(e, "id", None),
                    "system_presence": getattr(e, "system_presence", None),
                    "label": label,
                    "language_labels": labels,
                    "deactivated": getattr(e, "deactivated", False),
                })
            if not entities or len(entities) < page_size:
                break
            page_number += 1
            if page_number > 10:  # safety cap (org would need 2000+ presences)
                break
        return {"count": len(out), "presences": out}

    @mcp.tool()
    def get_user_skills(
        user_id: str = Field(description="User id."),
        page_size: int = Field(default=100, ge=1, le=500),
    ) -> dict:
        """Skills assigned to a user (with proficiency levels). Combines with list_routing_skills
        to map agent capability against queue requirements.
        """
        api = gc.UsersApi(get_api())
        resp = with_retry(api.get_user_routingskills)(
            user_id=user_id, page_size=page_size, page_number=1
        )
        rows = [
            {
                "id": s.id,
                "name": s.name,
                "proficiency": getattr(s, "proficiency", None),
                "state": getattr(s, "state", None),
            }
            for s in (resp.entities or [])
        ]
        return {"total": resp.total, "skills": rows}

    @mcp.tool()
    def get_user_presence_now(
        user_ids: list[str] = Field(
            description="User ids to fetch live presence + routing status for.",
        ),
        include_label: bool = Field(
            default=True,
            description=(
                "(v1.4+) When true (default), each user row includes a "
                "``presence_label`` resolved from the presence-definition UUID "
                "(e.g. 'Pre Break', 'Coaching'). Uses a process-lifetime cache "
                "of /api/v2/presence/definitions — typically one load per "
                "MCP server lifetime. Set to false to skip the lookup if "
                "the OAuth client lacks ``presence:definition:view``."
            ),
        ),
    ) -> dict:
        """Single-call live presence for a list of users. Returns systemPresence
        (Available / Break / Meal / Away / Offline / etc.), routing status, and last
        status timestamp. Lighter-weight than pulling users/aggregates.

        v1.4+: includes ``presence_label`` resolved from the presence-definition
        UUID (e.g. 'Pre Break', 'Coaching') — set ``include_label=False`` to
        skip.

        Uses GET /api/v2/users/{id}?expand=presence,routingStatus.
        """
        api = gc.UsersApi(get_api())
        out = []
        for uid in user_ids:
            try:
                resp = with_retry(api.get_user)(
                    user_id=uid, expand=["presence", "routingStatus"]
                )
                data = to_dict(resp) or {}
                presence = (data.get("presence") or {})
                presence_def = presence.get("presenceDefinition") or {}
                routing = data.get("routingStatus") or {}
                presence_def_id = presence_def.get("id")
                row = {
                    "user_id": uid,
                    "name": data.get("name"),
                    "system_presence": presence.get("systemPresence") or presence_def.get("systemPresence"),
                    "presence_definition_id": presence_def_id,
                    "presence_message": presence.get("message"),
                    "presence_modified": presence.get("modifiedDate"),
                    "routing_status": routing.get("status"),
                    "routing_status_start": routing.get("startTime"),
                }
                if include_label:
                    row["presence_label"] = _presence_label_for(presence_def_id)
                out.append(row)
            except Exception as exc:
                out.append({"user_id": uid, "error": str(exc)})
        return {"results": out}

    @mcp.tool()
    def get_queue_members(
        queue_id: str = Field(description="Queue id (see list_queues)."),
        page_size: int = Field(default=100, ge=1, le=500),
        page_number: int = Field(default=1, ge=1),
    ) -> dict:
        """List users who are members of a queue (includes their routing status)."""
        api = gc.RoutingApi(get_api())
        resp = with_retry(api.get_routing_queue_members)(
            queue_id=queue_id, page_size=page_size, page_number=page_number
        )
        return to_dict(resp)
