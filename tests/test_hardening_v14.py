"""Regression tests for the defects found by the v1.3.1 review.

Each class names the defect. Where the old behaviour was reachable only
through the real PluginManager or the real Lambda adapter, the test goes
through that real object, not a mock of it: the v1.3.0 scrubbing test
mocked PluginManager.execute_tool to raise, which the real one never
does, and so passed while tool exceptions still leaked.
"""

import asyncio
import base64
import json
import logging
import os
from unittest.mock import patch

import pytest

from core.interfaces import (
    InvalidToolParamsError,
    MCPPlugin,
    PluginType,
    ToolDefinition,
    ToolInputError,
    ToolResult,
    UnknownToolError,
)
from core.mcp_server import MCPServer
from core.plugin_manager import PluginManager
from core.validators import ConfigurationError
from server import http_handler
from server.adapters import aws_lambda
from server.http_handler import UniversalHTTPHandler

BASE_CONFIG = {"plugins": {"stub": {"enabled": True}}}
SECRET = "secret /var/task/x.py upstream said token=abc123"


class RaisingPlugin(MCPPlugin):
    plugin_name = "stub"
    plugin_type = PluginType.CUSTOM_API

    def __init__(self, exc):
        super().__init__({})
        self.exc = exc
        self.calls = 0

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
        self.calls += 1
        if self.exc is None:
            return ToolResult(content=[{"type": "text", "text": "ok"}], success=True)
        raise self.exc

    async def health_check(self) -> bool:
        return True


def _real_server(exc=None):
    """An MCPServer over a REAL PluginManager holding one plugin."""
    manager = PluginManager(dict(BASE_CONFIG))
    plugin = RaisingPlugin(exc)
    manager.plugins["stub"] = plugin
    manager._register_tools("stub", plugin)
    manager._initialized = True
    return MCPServer(manager), plugin


def _call(i=1, **extra):
    request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "stub__t", "arguments": {}},
        **extra,
    }
    if i is not None:
        request["id"] = i
    return request


class TestToolExceptionsAreScrubbed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            UnknownToolError("stub__other", "stub__t"),
            InvalidToolParamsError("bad shape: x"),
        ],
    )
    async def test_the_cores_own_caller_errors_pass_through_too(self, exc):
        server, _ = _real_server(exc)
        response = await server.handle_request(_call())
        text = response["result"]["content"][0]["text"]
        assert text == str(exc)
        assert "Error ID" not in text

    @pytest.mark.asyncio
    async def test_scrubbed_message_does_not_guess_at_a_cause(self):
        server, _ = _real_server(RuntimeError(SECRET))
        response = await server.handle_request(_call())
        text = response["result"]["content"][0]["text"]
        assert text.startswith("The tool failed unexpectedly. Error ID: ")
        assert "unavailable" not in text and "upstream" not in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [RuntimeError(SECRET), ValueError(SECRET), KeyError(SECRET), OSError(SECRET)],
    )
    async def test_unexpected_exception_text_never_reaches_the_client(
        self, exc, caplog
    ):
        server, _ = _real_server(exc)
        with caplog.at_level(logging.ERROR, logger="core.plugin_manager"):
            response = await server.handle_request(_call())

        wire = json.dumps(response)
        assert "secret" not in wire and "abc123" not in wire
        result = response["result"]
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert "Error ID: " in text

        error_id = text.rsplit("Error ID: ", 1)[1].strip()
        records = [r for r in caplog.records if getattr(r, "error_id", None)]
        assert [r.error_id for r in records] == [error_id]
        assert "abc123" in records[0].getMessage()
        assert records[0].exc_info is not None

    @pytest.mark.asyncio
    async def test_tool_input_error_message_is_passed_through(self, caplog):
        """The plugin wrote this for the caller; a model needs it to
        correct the call. No error ID, no traceback."""
        message = "topic is required -- pass a 1-2 word subject"
        server, _ = _real_server(ToolInputError(message))
        with caplog.at_level(logging.WARNING, logger="core.plugin_manager"):
            response = await server.handle_request(_call())

        result = response["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == message
        assert "Error ID" not in json.dumps(response)
        ours = [
            r for r in caplog.records if "rejected its arguments" in r.getMessage()
        ]
        assert ours and all(r.exc_info is None for r in ours)
        assert all(r.levelno == logging.WARNING for r in ours)

    @pytest.mark.asyncio
    async def test_scrubbed_in_the_modern_era_too(self):
        server, _ = _real_server(RuntimeError(SECRET))
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        request = _call()
        request["params"]["_meta"] = meta
        response = await server.handle_request(request)
        assert "abc123" not in json.dumps(response)


class TestLegacyNotificationsDoNotExecute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("extra", [{}, {"id": None}])
    async def test_tools_call_notification_runs_nothing(self, extra):
        server, plugin = _real_server(None)
        response = await server.handle_request(_call(i=None, **extra))
        assert response is None
        assert plugin.calls == 0

    @pytest.mark.asyncio
    async def test_the_same_call_with_an_id_does_run(self):
        server, plugin = _real_server(None)
        response = await server.handle_request(_call(i=5))
        assert response["id"] == 5 and plugin.calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("extra", [{}, {"id": 7}])
    async def test_initialized_is_accepted_with_or_without_an_id(self, extra):
        """Some clients send it with an id. It was always accepted with no
        result (HTTP 202); an error here would fail their handshake."""
        server, _ = _real_server(None)
        request = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            **extra,
        }
        assert await server.handle_request(request) is None
        http = await server.handle_http_request(json.dumps(request))
        assert http["statusCode"] == 202


class TestEnvConfigIsValidated:
    """OPENCONTEXT_CONFIG as JSON used to skip validation, and one bad
    `server_*` key silently swapped ALL the HTTP settings for defaults."""

    BAD = [
        {"server_max_body_bytes": "1000"},
        {"server_allowed_origins": []},
        {"server_allowed_origins": "https://only.example"},
        {"server_mcp_paths": ["mcp"]},
        {"plugins": "nope"},
        {"plugins": {}},
        {"plugins": {"a": {"enabled": True}, "b": {"enabled": True}}},
    ]

    @pytest.mark.parametrize("bad", BAD)
    def test_malformed_setting_is_a_configuration_error(self, bad, monkeypatch):
        config = {
            **BASE_CONFIG,
            "server_allowed_origins": ["https://only.example"],
            **bad,
        }
        monkeypatch.setenv("OPENCONTEXT_CONFIG", json.dumps(config))
        http_handler._config = None
        with pytest.raises(ConfigurationError):
            http_handler._load_config()
        assert http_handler._config is None  # nothing half-loaded is cached

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", BAD[:4])
    async def test_malformed_setting_stops_the_server_with_500(
        self, bad, monkeypatch
    ):
        """Fail closed: no request is served under silently-defaulted
        settings. The plugin named is the fixture one, which really
        loads, so the CONTROL below is served: the 500 is caused by the
        bad setting and nothing else."""
        good = {"plugins": {"ckan": {"enabled": True}}}

        async def ping(config):
            monkeypatch.setenv("OPENCONTEXT_CONFIG", json.dumps(config))
            http_handler._config = None
            http_handler._plugin_manager = None
            http_handler._mcp_server = None
            return await UniversalHTTPHandler().handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

        status, _, _ = await ping(good)
        assert status == 200  # control

        status, _, body = await ping({**good, **bad})
        assert status == 500
        assert "server_" not in body  # config detail is not echoed

    def test_valid_env_config_loads_with_its_settings(self, monkeypatch):
        config = {
            **BASE_CONFIG,
            "server_allowed_origins": ["https://only.example"],
            "server_max_body_bytes": 1000,
        }
        monkeypatch.setenv("OPENCONTEXT_CONFIG", json.dumps(config))
        http_handler._config = None
        settings = UniversalHTTPHandler._http_settings()
        assert settings["allowed_origins"] == frozenset({"https://only.example"})
        assert settings["max_body_bytes"] == 1000


class TestHostileInputIsARefusalNotACrash:
    @staticmethod
    async def _post(body, headers=None, config=None):
        http_handler._config = config or dict(BASE_CONFIG)
        server, _ = _real_server(None)
        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server", server),
        ):
            return await UniversalHTTPHandler().handle_request(
                method="POST", path="/mcp", body=body, headers=headers
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        ["[" * 200_000, '{"a":' * 100_000, b"\xff\xfe\x00bad", "{not json", ""],
    )
    async def test_unparseable_body_is_400_parse_error(self, body):
        status, _, out = await self._post(body, {})
        assert status == 400
        assert json.loads(out)["error"]["code"] == -32700

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [12345, "[1, 2]", '"text"', "null"])
    async def test_json_that_is_not_an_object_is_400_invalid_request(self, body):
        status, _, out = await self._post(body, {})
        assert status == 400
        assert json.loads(out)["error"]["code"] == -32600

    @pytest.mark.asyncio
    async def test_deep_but_parseable_nesting_does_not_crash_logging(self):
        depth = 400
        body = (
            '{"jsonrpc": "2.0", "id": 1, "method": "ping", "x": '
            + "[" * depth
            + "]" * depth
            + "}"
        )
        status, _, _ = await self._post(body, {})
        assert status == 200

    @pytest.mark.asyncio
    async def test_bytes_body_with_a_size_limit_is_measured_not_crashed(self):
        config = {**BASE_CONFIG, "server_max_body_bytes": 10}
        status, _, _ = await self._post(b"x" * 11, {}, config)
        assert status == 413

    @pytest.mark.asyncio
    async def test_size_limit_counts_bytes_received_not_a_re_encoding(self):
        """Five invalid bytes are five bytes: under a 10-byte limit they
        are an unparseable body (400), not an oversized one (413)."""
        config = {**BASE_CONFIG, "server_max_body_bytes": 10}
        status, _, out = await self._post(b"\xff" * 5, {}, config)
        assert status == 400
        assert json.loads(out)["error"]["code"] == -32700

    @pytest.mark.asyncio
    async def test_non_utf8_body_is_refused_not_run_on_corrupted_text(self):
        """Latin-1 bytes inside a JSON string must not reach a tool as
        U+FFFD: that would be an authoritative-looking wrong answer."""
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"stub__t","arguments":{"q":"Pe\xf1a"}}}'
        http_handler._config = dict(BASE_CONFIG)
        server, plugin = _real_server(None)
        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server", server),
        ):
            status, _, out = await UniversalHTTPHandler().handle_request(
                method="POST", path="/mcp", body=body, headers={}
            )
        assert status == 400
        assert json.loads(out)["error"]["code"] == -32700
        assert plugin.calls == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("depth", [100, 1500, 3000])
    async def test_deeply_nested_arguments_are_not_a_500(self, depth):
        """The log sanitizer recursed once per level, outside any try."""
        nested = "[" * depth + "]" * depth
        body = (
            '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
            '{"name":"stub__t","arguments":{"x":' + nested + "}}}"
        )
        status, _, _ = await self._post(body, {})
        assert status in (200, 400)

    @pytest.mark.asyncio
    async def test_already_parsed_body_is_served(self):
        status, _, _ = await self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {}
        )
        assert status == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "headers",
        [
            None,
            "not-a-dict",
            ["origin", "https://evil.example"],
            {"origin": ["https://evil.example"]},
            {"Origin": None},
            {1: "x"},
        ],
    )
    async def test_odd_header_shapes_do_not_raise(self, headers):
        status, _, _ = await self._post(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), headers
        )
        assert status in (200, 403)

    @pytest.mark.asyncio
    async def test_list_valued_origin_is_refused(self):
        status, _, _ = await self._post(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            {"origin": ["https://claude.ai"]},
        )
        assert status == 403


class TestHeaderMappingsAreNotDropped:
    """Headers handed over as a non-dict mapping used to be discarded
    whole -- Origin included -- and a request with no Origin is allowed."""

    @staticmethod
    def _shapes(pairs):
        import types
        from email.message import Message

        message = Message()
        for key, value in pairs.items():
            message[key] = value

        class ItemsOnly:
            def items(self):
                return list(pairs.items())

        return [
            dict(pairs),
            types.MappingProxyType(dict(pairs)),
            message,
            ItemsOnly(),
        ]

    @pytest.mark.asyncio
    async def test_disallowed_origin_is_refused_whatever_the_mapping_type(self):
        http_handler._config = dict(BASE_CONFIG)
        server, _ = _real_server(None)
        for headers in self._shapes({"Origin": "https://evil.example"}):
            with (
                patch("server.http_handler._initialize_server"),
                patch("server.http_handler._mcp_server", server),
            ):
                status, _, _ = await UniversalHTTPHandler().handle_request(
                    method="POST",
                    path="/mcp",
                    body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                    headers=headers,
                )
            assert status == 403, type(headers).__name__

    @pytest.mark.asyncio
    async def test_version_header_is_seen_whatever_the_mapping_type(self):
        http_handler._config = dict(BASE_CONFIG)
        server, _ = _real_server(None)
        for headers in self._shapes({"MCP-Protocol-Version": "1999-01-01"}):
            with (
                patch("server.http_handler._initialize_server"),
                patch("server.http_handler._mcp_server", server),
            ):
                status, _, body = await UniversalHTTPHandler().handle_request(
                    method="POST",
                    path="/mcp",
                    body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                    headers=headers,
                )
            assert status == 400, type(headers).__name__
            assert json.loads(body)["error"]["code"] == -32022

    def test_things_that_are_not_mappings_mean_no_headers(self):
        from server.http_handler import normalize_headers

        for junk in (None, "x", 5, ["origin", "https://evil.example"], object()):
            assert normalize_headers(junk) == {}


class TestOddBodyTypes:
    @staticmethod
    async def _post(body, config=None):
        http_handler._config = config or dict(BASE_CONFIG)
        server, _ = _real_server(None)
        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server", server),
        ):
            return await UniversalHTTPHandler().handle_request(
                method="POST", path="/mcp", body=body, headers={}
            )

    PING = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'

    @pytest.mark.asyncio
    async def test_memoryview_and_bytearray_bodies_are_served(self):
        for body in (memoryview(self.PING), bytearray(self.PING), self.PING):
            status, _, _ = await self._post(body)
            assert status == 200, type(body).__name__

    @pytest.mark.asyncio
    async def test_memoryview_is_measured_in_bytes_for_the_limit(self):
        config = {**BASE_CONFIG, "server_max_body_bytes": len(self.PING)}
        status, _, _ = await self._post(memoryview(self.PING), config)
        assert status == 200
        config = {**BASE_CONFIG, "server_max_body_bytes": len(self.PING) - 1}
        status, _, _ = await self._post(memoryview(self.PING), config)
        assert status == 413

    @pytest.mark.asyncio
    async def test_parsed_body_json_cannot_express_is_refused_not_raised(self):
        status, _, out = await self._post({"a": {1, 2}})
        assert status == 400
        assert json.loads(out)["error"]["code"] == -32700


class TestNotFoundDoesNotAdvertiseRoutes:
    @pytest.mark.asyncio
    async def test_404_names_only_the_requested_path(self):
        http_handler._config = {
            **BASE_CONFIG,
            "server_mcp_paths": ["/mcp", "/mcp-keyed"],
        }
        status, _, body = await UniversalHTTPHandler().handle_request(
            method="POST", path="/probe", body="{}", headers={}
        )
        assert status == 404
        assert "/mcp-keyed" not in body
        assert "/probe" in body


class TestLambdaAdapter:
    """Through the real lambda_handler."""

    class Ctx:
        aws_request_id = "t"
        function_name = "t"
        memory_limit_in_mb = 128

        def get_remaining_time_in_millis(self):
            return 10000

    def _invoke(self, event):
        http_handler._config = dict(BASE_CONFIG)
        server, _ = _real_server(None)
        http_handler._mcp_server = server
        http_handler._plugin_manager = server.plugin_manager
        # A fresh real handler: never whatever an earlier test cached.
        aws_lambda._handler = None
        # Config comes from the seeded _config; the variable is cleared
        # for the call only and restored afterwards.
        with patch.dict(os.environ, {"OPENCONTEXT_CONFIG": ""}):
            return aws_lambda.lambda_handler(event, self.Ctx())

    def test_error_path_survives_an_event_that_is_not_a_dict(self):
        """The adapter's own except branch must not raise while building
        the 500 it returns."""
        http_handler._config = dict(BASE_CONFIG)
        aws_lambda._handler = None
        for event in (None, "not-an-event", ["x"]):
            response = aws_lambda.lambda_handler(event, self.Ctx())
            assert response["statusCode"] == 500
            assert "Traceback" not in response["body"]

    @pytest.mark.parametrize("wrap", ["\n", "\r\n", " "])
    def test_base64_body_with_whitespace_is_still_served(self, wrap):
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        encoded = base64.b64encode(raw).decode()
        response = self._invoke(
            {
                "httpMethod": "POST",
                "path": "/mcp",
                "headers": {},
                "body": encoded[:20] + wrap + encoded[20:] + wrap,
                "isBase64Encoded": True,
            }
        )
        assert response["statusCode"] == 200

    @pytest.mark.parametrize("name", ["origin", "Origin", "ORIGIN", "oRiGiN"])
    def test_preflight_origin_lookup_ignores_case(self, name):
        response = self._invoke(
            {
                "httpMethod": "OPTIONS",
                "path": "/mcp",
                "headers": {name: "https://evil.example"},
                "body": None,
            }
        )
        assert response["statusCode"] == 403

    @pytest.mark.parametrize(
        "body, encoded",
        [
            (base64.b64encode(b"\xff\xfe\x00not utf8").decode(), True),
            ("%%% not base64 %%%", True),
            ("[" * 200_000, False),
        ],
    )
    def test_undecodable_or_hostile_body_is_400(self, body, encoded):
        response = self._invoke(
            {
                "httpMethod": "POST",
                "path": "/mcp",
                "headers": {"content-type": "application/json"},
                "body": body,
                "isBase64Encoded": encoded,
            }
        )
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == -32700

    @pytest.mark.parametrize(
        "headers",
        [
            {"Origin": "https://claude.ai", "origin": "https://evil.example"},
            {"origin": "https://evil.example", "Origin": "https://claude.ai"},
            {"origin": ["https://claude.ai"]},
            {"ORIGIN": "https://evil.example"},
            {"Origin": "https://claude.ai"},
        ],
    )
    def test_preflight_and_request_agree_about_the_origin(self, headers):
        """One normalisation for both: a preflight must never be allowed
        for an origin whose actual request is refused, or the reverse."""
        ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        preflight = self._invoke(
            {
                "httpMethod": "OPTIONS",
                "path": "/mcp",
                "headers": headers,
                "body": None,
            }
        )
        request = self._invoke(
            {"httpMethod": "POST", "path": "/mcp", "headers": headers, "body": ping}
        )
        assert (preflight["statusCode"] == 403) == (request["statusCode"] == 403)

    def test_non_string_header_keys_are_not_a_500(self):
        response = self._invoke(
            {
                "httpMethod": "POST",
                "path": "/mcp",
                "headers": {1: "x", "content-type": "application/json"},
                "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            }
        )
        assert response["statusCode"] == 200

    def test_base64_non_utf8_body_does_not_reach_a_tool(self):
        raw = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"stub__t","arguments":{"q":"Pe\xf1a"}}}'
        response = self._invoke(
            {
                "httpMethod": "POST",
                "path": "/mcp",
                "headers": {},
                "body": base64.b64encode(raw).decode(),
                "isBase64Encoded": True,
            }
        )
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == -32700

    def test_base64_utf8_body_is_served(self):
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        response = self._invoke(
            {
                "httpMethod": "POST",
                "path": "/mcp",
                "headers": {},
                "body": base64.b64encode(raw).decode(),
                "isBase64Encoded": True,
            }
        )
        assert response["statusCode"] == 200

    def test_list_valued_origin_does_not_break_the_error_path(self):
        response = self._invoke(
            {
                "httpMethod": "POST",
                "path": "/mcp",
                "headers": {"origin": ["https://evil.example"]},
                "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            }
        )
        assert response["statusCode"] in (200, 403)
        assert isinstance(response["body"], str)


def test_event_loop_is_usable_after_adapter_tests():
    """The adapter manages its own loop; make sure it left a working one
    behind for the async tests that follow."""
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(asyncio.sleep(0, result=1)) == 1
    finally:
        loop.close()
