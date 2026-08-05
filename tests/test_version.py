"""Release metadata must agree with the installed package."""

from importlib.metadata import version

from genesys_mcp import __version__


def test_package_version_is_current_release():
    assert __version__ == version("genesys-mcp") == "1.22.1"
