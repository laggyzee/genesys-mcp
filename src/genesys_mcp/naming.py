"""Internal name-resolution cache.

Many Genesys responses return UUIDs for queues, users, and wrap-up codes. This
module gives the tool layer a cheap way to enrich responses with names without
hammering the API. Lazy-loaded, in-memory, with a TTL so stale entries refresh.

Usage:

    from genesys_mcp.naming import resolver
    name = resolver.queue_name(queue_id)            # returns str | None
    names = resolver.queue_names([qid1, qid2])      # returns dict[id, name]

The resolver is process-local (one per MCP server instance) and read-only —
it never writes back to Genesys. If a lookup misses, returns ``None`` rather
than raising, so callers can fall back to the raw id.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

import PureCloudPlatformClientV2 as gc

from genesys_mcp.client import get_api, with_retry

logger = logging.getLogger(__name__)

# 30-min TTL — a queue/user rename is uncommon and tolerable for half an hour.
_TTL_SECONDS = 1800


class _Cache:
    """One-shot fetch-on-miss cache with a wall-clock TTL."""

    def __init__(self, kind: str, fetch_all):
        self._kind = kind
        self._fetch_all = fetch_all
        self._lock = threading.Lock()
        self._items: dict[str, str] = {}
        self._loaded_at: float = 0.0

    def _refresh_if_needed(self) -> None:
        if not self._items or (time.time() - self._loaded_at) > _TTL_SECONDS:
            with self._lock:
                if not self._items or (time.time() - self._loaded_at) > _TTL_SECONDS:
                    try:
                        self._items = self._fetch_all()
                        self._loaded_at = time.time()
                        logger.info("naming: %s cache loaded (%d entries)", self._kind, len(self._items))
                    except Exception as exc:
                        logger.warning("naming: %s cache refresh failed: %s", self._kind, exc)

    def get(self, item_id: str) -> str | None:
        if not item_id:
            return None
        self._refresh_if_needed()
        return self._items.get(item_id)

    def get_many(self, item_ids: Iterable[str]) -> dict[str, str | None]:
        self._refresh_if_needed()
        return {i: self._items.get(i) for i in item_ids if i}


def _fetch_queues() -> dict[str, str]:
    api = gc.RoutingApi(get_api())
    out: dict[str, str] = {}
    page = 1
    while True:
        resp = with_retry(api.get_routing_queues)(page_size=200, page_number=page)
        for q in (resp.entities or []):
            if q.id and q.name:
                out[q.id] = q.name
        if not resp.entities or len(resp.entities) < 200:
            break
        page += 1
    return out


def _fetch_wrapup_codes() -> dict[str, str]:
    api = gc.RoutingApi(get_api())
    out: dict[str, str] = {}
    page = 1
    while True:
        resp = with_retry(api.get_routing_wrapupcodes)(page_size=500, page_number=page)
        for c in (resp.entities or []):
            if c.id and c.name:
                out[c.id] = c.name
        if not resp.entities or len(resp.entities) < 500:
            break
        page += 1
    return out


# Users are fetched lazily one-by-one and cached, since the org may have many
# inactive accounts and we don't need the full list up-front. Cache is
# unbounded but capped by typical org size.
class _UserCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, str] = {}
        self._loaded_at: dict[str, float] = {}

    def _is_fresh(self, uid: str) -> bool:
        ts = self._loaded_at.get(uid, 0.0)
        return (time.time() - ts) < _TTL_SECONDS

    def get(self, user_id: str) -> str | None:
        if not user_id:
            return None
        if user_id in self._items and self._is_fresh(user_id):
            return self._items[user_id]
        api = gc.UsersApi(get_api())
        try:
            resp = with_retry(api.get_user)(user_id=user_id)
            name = getattr(resp, "name", None)
            if name:
                with self._lock:
                    self._items[user_id] = name
                    self._loaded_at[user_id] = time.time()
                return name
        except Exception as exc:
            logger.debug("naming: user %s lookup failed: %s", user_id, exc)
        return None

    def get_many(self, user_ids: Iterable[str]) -> dict[str, str | None]:
        return {uid: self.get(uid) for uid in user_ids if uid}


class Resolver:
    """Public façade. One instance lives in the module; tools import it."""

    def __init__(self) -> None:
        self._queues = _Cache("queues", _fetch_queues)
        self._wrapups = _Cache("wrapup_codes", _fetch_wrapup_codes)
        self._users = _UserCache()

    def queue_name(self, queue_id: str) -> str | None:
        return self._queues.get(queue_id)

    def queue_names(self, queue_ids: Iterable[str]) -> dict[str, str | None]:
        return self._queues.get_many(queue_ids)

    def wrapup_name(self, wrapup_id: str) -> str | None:
        return self._wrapups.get(wrapup_id)

    def wrapup_names(self, wrapup_ids: Iterable[str]) -> dict[str, str | None]:
        return self._wrapups.get_many(wrapup_ids)

    def user_name(self, user_id: str) -> str | None:
        return self._users.get(user_id)

    def user_names(self, user_ids: Iterable[str]) -> dict[str, str | None]:
        return self._users.get_many(user_ids)


resolver = Resolver()
