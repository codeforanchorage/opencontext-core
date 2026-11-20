"""Regression tests for the defects found by the 2026-11 review of v1.4.0.

What they have in common: a value of an unexpected type reached a line
that assumed a friendlier one, and the result was either invalid JSON
with HTTP 200 or a crash that lost the request id.
"""

import datetime
import json
import logging
from unittest.mock import patch

import pytest

from core.interfaces import MCPPlugin, PluginType, ToolDefinition, ToolResult
from core.logging_utils import configure_json_logging
from core.mcp_server import MCPServer, dumps_response
from core.plugin_manager import PluginManager
from server import http_handler
from server.adapters import aws_lambda
from server.http_handler import UniversalHTTPHandler

BASE_CONFIG = {"plugins": {"stub": {"enabled": True}}}


class ResultPlugin(MCPPlugin):
    """Returns whatever structured content the test hands it."""

    plugin_name = "stub"
    plugin_type = PluginType.CUSTOM_API

    def __init__(self, structured):
        super().__init__({})
        self.structured = structured

    async def initialize(self) -> bool:
        return True

    async def shutdown(self) -> None:
        pass

    def get_tools(self):
        return [
            ToolDefinition(
                name="t", description="d", input_schema={"type": "object"}
            )
        ]

    async def execute_tool(self, tool_name, arguments):
        return ToolResult(
            content=[{"type": "text", "text": "ok"}],
            success=True,
            structured_content=self.structured,
        )

    async def health_check(self) -> bool:
        return True


def _server(structured=None, config=None):
    manager = PluginManager(dict(config or BASE_CONFIG))
    plugin = ResultPlugin(structured)
    manager.plugins["stub"] = plugin
    manager._register_tools("stub", plugin)
    manager._initialized = True
    return MCPServer(manager)


async def _post(body, structured=None):
    http_handler._config = dict(BASE_CONFIG)
    server = _server(structured)
    with (
        patch("server.http_handler._initialize_server"),
        patch("server.http_handler._mcp_server", server),
    ):
        return await UniversalHTTPHandler().handle_request(
            method="POST", path="/mcp", body=body, headers={}
        )


def _call(name="stub__t", i=7):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": i,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        }
    )


def _strict(text):
    """Parse as a strict JSON parser would: bare NaN/Infinity is an error."""

    def refuse(token):
        raise ValueError(f"not JSON: {token}")

    return json.loads(text, parse_constant=refuse)


class TestResponsesAreAlwaysValidJson:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad", [float("nan"), float("inf"), float("-inf")]
    )
    async def test_non_finite_number_in_a_result_becomes_null(self, bad):
        status, _, out = await _post(
            _call(), structured={"avg": bad, "rows": [{"v": bad}, {"v": 1.5}]}
        )
        assert status == 200
        body = _strict(out)
        structured = body["result"]["structuredContent"]
        assert structured == {"avg": None, "rows": [{"v": None}, {"v": 1.5}]}
        assert body["id"] == 7

    @pytest.mark.asyncio
    async def test_unencodable_value_keeps_the_request_id(self):
        """Was HTTP 500 with id null: the dump sat outside the request's
        error handling."""
        when = datetime.datetime(2026, 1, 1, 12, 0)
        status, _, out = await _post(_call(), structured={"d": when})
        assert status == 200
        body = _strict(out)
        assert body["id"] == 7
        assert body["result"]["structuredContent"] == {"d": str(when)}

    @pytest.mark.asyncio
    async def test_non_finite_request_id_is_not_echoed_as_a_bare_token(self):
        status, _, out = await _post(
            '{"jsonrpc":"2.0","id":NaN,"method":"ping"}'
        )
        _strict(out)  # must not raise

    def test_clean_payload_is_serialised_unchanged(self):
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"a": [1, 2.5, "x"]}}
        assert dumps_response(payload) == json.dumps(payload)


class TestMalformedToolName:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", [["x"], {"a": 1}, 5, True])
    async def test_non_string_name_is_invalid_params(self, name, caplog):
        server = _server()
        with caplog.at_level(logging.ERROR):
            response = await server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name},
                }
            )
        assert response["error"]["code"] == -32602
        assert "must be a string" in json.dumps(response["error"])
        # A malformed request is not a server fault: no traceback.
        assert not [r for r in caplog.records if r.exc_info]


class TestConfigValuesOfTheWrongType:
    @pytest.mark.parametrize("level", [None, "", 10, "NOPE", "debug"])
    def test_any_log_level_configures_without_raising(self, level):
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        try:
            configure_json_logging(level=level)
            expected = logging.DEBUG if level == "debug" else logging.INFO
            assert root.level == expected
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

    @pytest.mark.asyncio
    async def test_empty_server_name_falls_back_to_a_string(self):
        config = dict(BASE_CONFIG, server_name=None, server_version=None)
        server = _server(config=config)
        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            }
        )
        info = response["result"]["serverInfo"]
        assert info["name"] == "OpenContext"
        assert info["version"] == "1.0.0"


class TestLambdaEventShapes:
    class Ctx:
        aws_request_id = "rid"
        function_name = "t"
        memory_limit_in_mb = 128

        def get_remaining_time_in_millis(self):
            return 10000

    def test_null_request_context_is_served(self):
        http_handler._config = dict(BASE_CONFIG)
        server = _server()
        http_handler._mcp_server = server
        http_handler._plugin_manager = server.plugin_manager
        aws_lambda._handler = None
        event = {
            "httpMethod": "POST",
            "path": "/mcp",
            "headers": {"content-type": "application/json"},
            "requestContext": None,
            "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        }
        with patch.dict("os.environ", {"OPENCONTEXT_CONFIG": ""}):
            response = aws_lambda.lambda_handler(event, self.Ctx())
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["id"] == 1


class TestKeysAndCyclesJsonCannotExpress:
    """The v1.4.1 fix rewrote *values*. The other half of a JSON object
    is its keys: a dict keyed by a tuple, a Decimal or a datetime raises
    TypeError from json.dumps and `default=` is never consulted for keys,
    so the response was never built -- HTTP 500 with the id lost, for a
    payload that is otherwise fine.
    """

    @pytest.mark.asyncio
    async def test_a_result_keyed_by_coordinates_is_served(self):
        """The ordinary case: a plugin aggregating points by (lat, lon)."""
        status, _, out = await _post(_call(), structured={"rows": {(64.8, -147.7): 3}})

        assert status == 200
        body = _strict(out)
        assert body["id"] == 7
        assert body["result"]["structuredContent"] == {"rows": {"(64.8, -147.7)": 3}}

    @pytest.mark.asyncio
    async def test_a_non_finite_key_does_not_lose_the_response(self):
        status, _, out = await _post(_call(), structured={"rows": {float("nan"): 1}})

        assert status == 200
        body = _strict(out)
        assert body["result"]["structuredContent"] == {"rows": {"nan": 1}}

    @pytest.mark.asyncio
    async def test_a_reference_cycle_becomes_a_marker(self):
        """json.dumps reports a cycle as ValueError and the old fallback
        then recursed until RecursionError, again outside the request's
        error handling."""
        structured = {"rows": [1]}
        structured["self"] = structured

        status, _, out = await _post(_call(), structured=structured)

        assert status == 200
        body = _strict(out)
        assert body["result"]["structuredContent"] == {
            "rows": [1],
            "self": "[circular]",
        }

    def test_a_shared_value_is_still_duplicated_not_marked_circular(self):
        shared = {"x": 1}
        payload = {"jsonrpc": "2.0", "id": 1, "result": [shared, shared]}

        assert json.loads(dumps_response(payload))["result"] == [{"x": 1}, {"x": 1}]

    def test_a_payload_that_beats_every_coercion_still_answers(self):
        class Unprintable:
            def __str__(self):
                raise RuntimeError("no repr for you")

        out = dumps_response(
            {"jsonrpc": "2.0", "id": 5, "result": {"k": Unprintable()}}
        )

        assert _strict(out) == {
            "jsonrpc": "2.0",
            "id": 5,
            "error": {
                "code": -32603,
                "message": "Response could not be serialised",
            },
        }
