"""Shared pytest fixtures.

The MCP server keeps its PluginManager and MCPServer as module-level
globals in ``server.http_handler`` so a warm Lambda container can reuse
them across invocations. That is right for production and hostile to
tests: anything a test assigns to those names outlives the test and is
visible to every test that runs afterwards, in any file.

That is not hypothetical. ``test_http_handler.py`` installs a MagicMock
to prove the initializer reuses an existing instance; the Lambda
adapter's cleanup path then does ``await _plugin_manager.shutdown()``,
and a plain MagicMock is not awaitable, so ``test_aws_lambda.py`` failed
with "'MagicMock' object can't be awaited". It only stayed hidden
because test_aws_lambda sorts before test_http_handler alphabetically --
running the two files in the other order, or shuffling test order, or
renaming either file, surfaces it.

Restoring the globals around every test fixes the whole class of
problem rather than the one instance of it.
"""

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def reset_http_handler_globals():
    """Snapshot and restore ``server.http_handler``'s module globals.

    Autouse, so a test that stubs these out cannot leak into any later
    test regardless of file or execution order.
    """
    from server import http_handler

    saved_plugin_manager = http_handler._plugin_manager
    saved_mcp_server = http_handler._mcp_server
    saved_config = http_handler._config
    try:
        yield
    finally:
        http_handler._plugin_manager = saved_plugin_manager
        http_handler._mcp_server = saved_mcp_server
        http_handler._config = saved_config


FIXTURE_DEPLOYMENT = Path(__file__).parent / "fixtures" / "deployment"


@pytest.fixture(autouse=True)
def fixture_deployment(monkeypatch):
    """Point plugin discovery at the stand-in deployment in fixtures/.

    The core ships no plugins of its own, so without this there would be
    nothing to discover. ``sys.path`` is restored afterwards because
    resolving a plugin root may put that root on it.
    """
    from core.plugin_manager import PLUGIN_ROOT_ENV

    saved_path = list(sys.path)
    monkeypatch.setenv(PLUGIN_ROOT_ENV, str(FIXTURE_DEPLOYMENT))
    try:
        yield FIXTURE_DEPLOYMENT
    finally:
        sys.path[:] = saved_path
