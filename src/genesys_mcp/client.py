"""Genesys Cloud SDK bootstrap: region config, OAuth, and a retry helper.

Designed so the rest of the codebase treats ``get_api()`` as the one place to
obtain an authenticated ``PureCloudPlatformClientV2.ApiClient``. A second client
(write-scoped, used only by ``scripts/provision_users.py``) can be loaded via
``init_named_api("WRITE")`` without touching the read-only singleton.

Trust model
-----------
The conversational MCP only ever calls ``init_api()``, which reads from the
``GENESYS_CLIENT_*`` env-var family. The standalone provisioning script calls
``init_named_api("WRITE")``, which reads ``GENESYS_WRITE_CLIENT_*``. The two
clients are kept in different containers (the global singleton vs. the
``_named_clients`` dict) so loading the write client cannot affect anything the
MCP server hands to Claude. Server-side OAuth scope on the read-only client
remains the load-bearing guarantee that no write can succeed via the MCP.

The startup guard (``_assert_no_write_env_overlap``) warns if both env-var
families are set in the same process — that's the only way the wrong client
could end up active in the MCP, and it's easy to detect cheaply.
"""

from __future__ import annotations

import logging
import os
import time
import warnings
from functools import wraps
from typing import Callable, TypeVar

warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"PureCloudPlatformClientV2.*")

import PureCloudPlatformClientV2 as gc
from PureCloudPlatformClientV2.rest import ApiException

logger = logging.getLogger(__name__)

_REGION_HOSTS = {
    "ap-southeast-2": gc.PureCloudRegionHosts.ap_southeast_2,
    "us-east-1": gc.PureCloudRegionHosts.us_east_1,
    "eu-west-1": gc.PureCloudRegionHosts.eu_west_1,
}

# Default (read-only) singleton — preserved for back-compat with all existing tools.
_api_client: gc.ApiClient | None = None
# Additional clients keyed by env-prefix suffix (e.g. "WRITE"). Loaded explicitly.
_named_clients: dict[str, gc.ApiClient] = {}


class GenesysConfigError(RuntimeError):
    """Raised at startup when required environment is missing."""


def _read_config(prefix: str = "GENESYS") -> tuple[str, str, str]:
    """Read OAuth credentials from env. ``prefix`` selects the variable family.

    - ``"GENESYS"`` → reads ``GENESYS_CLIENT_ID``/``GENESYS_CLIENT_SECRET``
      (read-only client used by the MCP server).
    - ``"GENESYS_WRITE"`` → reads ``GENESYS_WRITE_CLIENT_ID``/``GENESYS_WRITE_CLIENT_SECRET``
      (write-scoped client loaded only by ``scripts/provision_users.py``).

    ``GENESYS_REGION`` is shared across both clients (single tenant).
    """
    client_id = os.environ.get(f"{prefix}_CLIENT_ID")
    client_secret = os.environ.get(f"{prefix}_CLIENT_SECRET")
    region = os.environ.get("GENESYS_REGION", "ap-southeast-2")

    missing = [
        k
        for k, v in [(f"{prefix}_CLIENT_ID", client_id), (f"{prefix}_CLIENT_SECRET", client_secret)]
        if not v
    ]
    if missing:
        raise GenesysConfigError(f"Missing required env var(s): {', '.join(missing)}")
    if region not in _REGION_HOSTS:
        raise GenesysConfigError(
            f"Unsupported GENESYS_REGION={region!r}. Known: {sorted(_REGION_HOSTS)}"
        )
    return client_id, client_secret, region  # type: ignore[return-value]


def _build_client(prefix: str) -> gc.ApiClient:
    client_id, client_secret, region = _read_config(prefix)
    host = _REGION_HOSTS[region].get_api_host()
    gc.configuration.host = host  # global; both clients share the same region

    api = gc.ApiClient()
    api.get_client_credentials_token(client_id, client_secret)
    logger.info("Genesys ApiClient ready: prefix=%s region=%s host=%s", prefix, region, host)
    return api


def assert_mcp_env_clean() -> None:
    """Loud warning if write credentials are visible to the MCP server process.

    Call this **only** from the MCP server lifespan — not from
    ``scripts/provision_users.py``, which legitimately needs both credential
    families in scope (read client for the template snapshot, write client for
    the writes).

    The MCP server should never see ``GENESYS_WRITE_CLIENT_*``; those are for
    the standalone provisioning script. If they're set in the same process,
    someone has mis-configured a launch script — either harmless (the MCP
    keeps using the read client) or worrying (write creds leaked into a
    long-running daemon). Either way, surface it rather than silently ignore.
    """
    if os.environ.get("GENESYS_WRITE_CLIENT_ID") or os.environ.get("GENESYS_WRITE_CLIENT_SECRET"):
        logger.warning(
            "GENESYS_WRITE_CLIENT_* is set in this process. The MCP server only loads "
            "the read-only client (GENESYS_CLIENT_*); write credentials should be "
            "scoped to the shell that runs scripts/provision_users.py."
        )
    read_id = os.environ.get("GENESYS_CLIENT_ID")
    write_id = os.environ.get("GENESYS_WRITE_CLIENT_ID")
    if read_id and write_id and read_id == write_id:
        raise GenesysConfigError(
            "GENESYS_CLIENT_ID and GENESYS_WRITE_CLIENT_ID resolve to the same value. "
            "These must be two distinct OAuth clients with different scope sets."
        )


def init_api() -> gc.ApiClient:
    """Fetch the OAuth token for the default (read-only) client and cache it.

    Does **not** check for env-var overlap — that check belongs in the MCP
    server's lifespan via :func:`assert_mcp_env_clean`. The provisioning script
    legitimately holds both credential sets, so calling this from there must
    not warn.
    """
    global _api_client
    _api_client = _build_client("GENESYS")
    return _api_client


def init_named_api(suffix: str) -> gc.ApiClient:
    """Fetch the OAuth token for a non-default client (e.g. ``"WRITE"``).

    Keyed by the env-var prefix suffix: ``"WRITE"`` reads ``GENESYS_WRITE_CLIENT_*``.
    Cached separately from the default singleton so loading a write client never
    affects the read-only conversational MCP.
    """
    api = _build_client(f"GENESYS_{suffix}")
    _named_clients[suffix] = api
    return api


def get_api() -> gc.ApiClient:
    if _api_client is None:
        raise RuntimeError("Genesys client not initialised — call init_api() at startup")
    return _api_client


def get_named_api(suffix: str) -> gc.ApiClient:
    api = _named_clients.get(suffix)
    if api is None:
        raise RuntimeError(
            f"Named client {suffix!r} not initialised — call init_named_api({suffix!r})"
        )
    return api


T = TypeVar("T")

# Status codes that warrant a retry. 401 = expired token, 429 = rate limit,
# 409 = optimistic-concurrency race (e.g. group `version`), 5xx = transient gateway.
_TRANSIENT_5XX = {502, 503, 504}


def with_retry_for(refresh: Callable[[], object] | None) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Build a retry decorator that knows how to refresh the right client on 401.

    ``refresh`` is invoked with no args after a 401 to re-fetch the token for the
    client this call uses. Pass ``init_api`` for the read-only client, a partial
    binding ``functools.partial(init_named_api, "WRITE")`` for the write client,
    or ``None`` to skip 401-refresh and let the exception bubble.

    Retries up to 3 attempts on:
      - 401 (refresh + retry, only if ``refresh`` is not None)
      - 429 (sleep ``Retry-After`` then retry)
      - 409 (immediate retry; caller is responsible for refetching ``version``)
      - 502/503/504 (exponential backoff: 1s, 2s, 4s)
    All other exceptions raise immediately.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            attempts = 3
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except ApiException as exc:
                    last = attempt == attempts
                    if exc.status == 401 and refresh is not None and not last:
                        logger.warning(
                            "Genesys 401; re-fetching credentials (attempt %d/%d)", attempt, attempts
                        )
                        refresh()
                        continue
                    if exc.status == 429 and not last:
                        retry_after = float((exc.headers or {}).get("Retry-After", "2"))
                        logger.warning(
                            "Genesys 429; sleeping %.1fs (attempt %d/%d)",
                            retry_after,
                            attempt,
                            attempts,
                        )
                        time.sleep(retry_after)
                        continue
                    if exc.status in _TRANSIENT_5XX and not last:
                        backoff = 2 ** (attempt - 1)
                        logger.warning(
                            "Genesys %d; sleeping %ds (attempt %d/%d)",
                            exc.status,
                            backoff,
                            attempt,
                            attempts,
                        )
                        time.sleep(backoff)
                        continue
                    if exc.status == 409 and not last:
                        # Caller should refetch any optimistic-concurrency `version`
                        # field before the next attempt; we just allow the retry.
                        logger.warning(
                            "Genesys 409 conflict; retrying (attempt %d/%d)", attempt, attempts
                        )
                        continue
                    raise
            raise RuntimeError("unreachable")  # for type checker

        return wrapper

    return decorator


def with_retry(fn: Callable[..., T]) -> Callable[..., T]:
    """Back-compat shim: retry against the default (read-only) client.

    All existing tools use this single-arg form. It refreshes the global
    ``_api_client`` on 401 and retries the same status codes as
    ``with_retry_for`` (401, 409, 429, 502/503/504).
    """
    return with_retry_for(init_api)(fn)


def to_dict(obj) -> dict:
    """Convert an SDK model (or list of models) to a JSON-safe dict via the SDK's serializer."""
    return get_api().sanitize_for_serialization(obj)
