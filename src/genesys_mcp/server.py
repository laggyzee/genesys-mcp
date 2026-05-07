"""FastMCP server exposing read-only Genesys Cloud (AU) tools over stdio."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP

from genesys_mcp.client import assert_mcp_env_clean, init_api
from genesys_mcp.tools import (
    analytics,
    conversations,
    directory,
    external_contacts,
    presence,
    raw,
    reports,
    speech_analytics,
    wfm,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("genesys_mcp")


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
    # Server-only guard: warn if write creds leaked into the MCP env.
    # The provisioning script doesn't call this (it legitimately needs both).
    assert_mcp_env_clean()
    init_api()
    try:
        yield
    finally:
        logger.info("Genesys MCP shutting down")


mcp = FastMCP(
    name="genesys",
    instructions=(
        "Read-only access to a Genesys Cloud tenant via Client Credentials OAuth. "
        "Curated tools cover queues, users, analytics, conversations, recordings, "
        "presence, speech & text analytics, external contacts, and workforce management. "
        "Use call_genesys_api for anything not covered; consult the platform-api skill "
        "for endpoint discovery before invoking it."
    ),
    lifespan=lifespan,
)

directory.register(mcp)
analytics.register(mcp)
conversations.register(mcp)
presence.register(mcp)
reports.register(mcp)
speech_analytics.register(mcp)
external_contacts.register(mcp)
wfm.register(mcp)
raw.register(mcp)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
