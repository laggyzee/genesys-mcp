"""Genesys Cloud (AU) MCP server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("genesys-mcp")
except PackageNotFoundError:  # Source tree imported before installation.
    __version__ = "0+unknown"

__all__ = ["__version__"]
