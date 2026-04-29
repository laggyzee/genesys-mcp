"""Genesys Cloud SDK bootstrap: region config, OAuth, and a retry helper.

Designed so the rest of the codebase treats ``get_api()`` as the one place to
obtain an authenticated ``PureCloudPlatformClientV2.ApiClient``.
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

_api_client: gc.ApiClient | None = None


class GenesysConfigError(RuntimeError):
    """Raised at startup when required environment is missing."""


def _read_config() -> tuple[str, str, str]:
    client_id = os.environ.get("GENESYS_CLIENT_ID")
    client_secret = os.environ.get("GENESYS_CLIENT_SECRET")
    region = os.environ.get("GENESYS_REGION", "ap-southeast-2")

    missing = [k for k, v in [("GENESYS_CLIENT_ID", client_id), ("GENESYS_CLIENT_SECRET", client_secret)] if not v]
    if missing:
        raise GenesysConfigError(f"Missing required env var(s): {', '.join(missing)}")
    if region not in _REGION_HOSTS:
        raise GenesysConfigError(
            f"Unsupported GENESYS_REGION={region!r}. Known: {sorted(_REGION_HOSTS)}"
        )
    return client_id, client_secret, region  # type: ignore[return-value]


def init_api() -> gc.ApiClient:
    """Fetch the OAuth token and cache the ApiClient for reuse."""
    global _api_client
    client_id, client_secret, region = _read_config()
    host = _REGION_HOSTS[region].get_api_host()
    gc.configuration.host = host

    api = gc.ApiClient()
    api.get_client_credentials_token(client_id, client_secret)
    _api_client = api
    logger.info("Genesys ApiClient ready: region=%s host=%s", region, host)
    return api


def get_api() -> gc.ApiClient:
    if _api_client is None:
        raise RuntimeError("Genesys client not initialised — call init_api() at startup")
    return _api_client


T = TypeVar("T")


def with_retry(fn: Callable[..., T]) -> Callable[..., T]:
    """Retry ``fn`` on 429 (rate limit) and 401 (expired token) up to 3 attempts."""

    @wraps(fn)
    def wrapper(*args, **kwargs) -> T:
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                return fn(*args, **kwargs)
            except ApiException as exc:
                if exc.status == 401 and attempt < attempts:
                    logger.warning("Genesys 401; re-fetching client-credentials token (attempt %d/%d)", attempt, attempts)
                    init_api()
                    continue
                if exc.status != 429 or attempt == attempts:
                    raise
                retry_after = float((exc.headers or {}).get("Retry-After", "2"))
                logger.warning("Genesys 429; sleeping %.1fs (attempt %d/%d)", retry_after, attempt, attempts)
                time.sleep(retry_after)
        raise RuntimeError("unreachable")  # for type checker

    return wrapper


def to_dict(obj) -> dict:
    """Convert an SDK model (or list of models) to a JSON-safe dict via the SDK's serializer."""
    return get_api().sanitize_for_serialization(obj)
