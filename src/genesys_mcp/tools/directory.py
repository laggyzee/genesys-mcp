"""Directory tools: queues, users, user lookup."""

from __future__ import annotations

import logging
from typing import Any

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


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
        query: str = Field(
            description="Free-text search across name and email. Returns ranked matches.",
        ),
        page_size: int = Field(default=10, ge=1, le=25),
    ) -> dict:
        """Search for users by name OR email. Use this when you only have a person's name.

        Backed by /api/v2/users/search with TERM-style query against name+email fields.
        For exact email match prefer find_user_by_email.
        """
        api = gc.SearchApi(get_api())
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
    ) -> dict:
        """Single-call live presence for a list of users. Returns systemPresence
        (Available / Break / Meal / Away / Offline / etc.), routing status, and last
        status timestamp. Lighter-weight than pulling users/aggregates.

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
                out.append({
                    "user_id": uid,
                    "name": data.get("name"),
                    "system_presence": presence.get("systemPresence") or presence_def.get("systemPresence"),
                    "presence_definition_id": presence_def.get("id"),
                    "presence_message": presence.get("message"),
                    "presence_modified": presence.get("modifiedDate"),
                    "routing_status": routing.get("status"),
                    "routing_status_start": routing.get("startTime"),
                })
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
