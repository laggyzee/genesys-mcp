"""Escape hatch: call any Genesys Cloud Platform API endpoint.

Every invocation is logged at INFO level to stderr so the user can see exactly
what the model attempted against the tenant. OAuth scope enforces read-only
posture — a mutating call will fail 403 server-side, not here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, with_retry

logger = logging.getLogger(__name__)

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def call_genesys_api(
        path: str = Field(
            description="Resource path, e.g. '/api/v2/routing/queues'. Must start with '/api/v2/'.",
        ),
        method: str = Field(
            default="GET",
            description="HTTP method. Non-GET calls will 403 unless the OAuth client has write scopes (it does not, in v1).",
        ),
        query: dict[str, Any] | None = Field(
            default=None, description="Query-string params, e.g. {'pageSize': 25}."
        ),
        body: dict[str, Any] | list[Any] | None = Field(
            default=None, description="JSON body for POST/PUT/PATCH (object or array)."
        ),
    ) -> dict:
        """Generic Genesys Cloud API call. Use the platform-api skill to find the correct path.

        Returns ``{"status": <code>, "data": <parsed-json-or-raw>}``. Non-2xx responses are
        returned rather than raised so the model can inspect error bodies directly.
        """
        method = method.upper()
        if method not in _ALLOWED_METHODS:
            raise ValueError(f"Unsupported HTTP method: {method}")
        if not path.startswith("/api/v2/"):
            raise ValueError(f"Path must start with '/api/v2/', got: {path!r}")

        body_summary = (
            list(body.keys()) if isinstance(body, dict)
            else f"[array of {len(body)}]" if isinstance(body, list)
            else None
        )
        logger.info("call_genesys_api %s %s query=%s body=%s",
                    method, path, query, body_summary)

        api = get_api()

        def _do() -> Any:
            return api.call_api(
                resource_path=path,
                method=method,
                query_params=query or {},
                body=body,
                header_params={"Accept": "application/json", "Content-Type": "application/json"},
                auth_settings=["PureCloud OAuth"],
                response_type="object",
            )

        try:
            data = with_retry(_do)()
            return {"status": 200, "data": data}
        except Exception as exc:
            status = getattr(exc, "status", None)
            payload = getattr(exc, "body", None)
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8", errors="replace")
            try:
                payload = json.loads(payload) if isinstance(payload, str) else payload
            except json.JSONDecodeError:
                pass
            return {"status": status or 0, "error": str(exc), "data": payload}
