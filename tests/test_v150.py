"""1.5.0: keep the server across Lambda invocations; cap what is logged.

Both were found reviewing a deployed server (the Anchorage Open Checkbook):

- The Lambda adapter shut the plugin manager down after every request and
  built it again for the next, because `asyncio.run` closes its loop and an
  HTTP client cannot outlive the loop it was made on. Plugin startup, which
  for that server is six upstream calls, ran on every request, `ping`
  included.
- Every request and response was logged in full twice, once by the HTTP
  layer and once by the JSON-RPC layer, with no size cap: one `tools/list`
  wrote about 98 KB of log.
"""

import asyncio
import json
import logging

import pytest

from core import logging_utils
from core.logging_utils import (
    cap_for_log,
    format_jsonrpc_request_log,
    format_jsonrpc_response_log,
    format_request_log,
    format_response_log,
    set_log_value_limit,
)
from core.validators import (
    ConfigurationError,
    get_http_settings,
    get_log_value_limit,
    validate_config,
)
from server import http_handler
from server.adapters import aws_lambda
from server.adapters.aws_lambda import lambda_handler

BASE_CONFIG = {"plugins": {"stub": {"enabled": True}}}


class FakePluginManager:
    """Counts how often the server is built and torn down."""

    built = 0
    shut_down = 0

    def __init__(self, config):
        self.config = config
        self.plugins = {}
        type(self).built += 1
        self.loop_at_build = None

    async def load_plugins(self):
        self.loop_at_build = asyncio.get_running_loop()

    async def shutdown(self):
        type(self).shut_down += 1

    def get_all_tools(self):
        return []


class Context:
    aws_request_id = "req-1"
    function_name = "fn"
    memory_limit_in_mb = 512


def ping_event(request_id=1):
    return {
        "httpMethod": "POST",
        "path": "/mcp",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "ping"}),
    }


@pytest.fixture
def fake_server(monkeypatch):
    FakePluginManager.built = 0
    FakePluginManager.shut_down = 0
    config = dict(BASE_CONFIG)
    monkeypatch.setattr(http_handler, "PluginManager", FakePluginManager)
    monkeypatch.setattr(http_handler, "_config", config)
    monkeypatch.setattr(http_handler, "_plugin_manager", None)
    monkeypatch.setattr(http_handler, "_mcp_server", None)
    monkeypatch.setattr(aws_lambda, "_handler", None)
    monkeypatch.setattr(aws_lambda, "_loop", None)
    yield config
    loop = aws_lambda._loop
    if loop is not None and not loop.is_closed():
        loop.close()
    set_log_value_limit(None)


class TestTheServerSurvivesAcrossInvocations:
    def test_the_plugin_manager_is_built_once(self, fake_server):
        for request_id in (1, 2, 3):
            response = lambda_handler(ping_event(request_id), Context())
            assert response["statusCode"] == 200
            assert json.loads(response["body"]) == {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {},
            }
        assert FakePluginManager.built == 1
        assert FakePluginManager.shut_down == 0
        assert http_handler._plugin_manager is not None

    def test_every_invocation_runs_on_the_same_open_loop(self, fake_server):
        lambda_handler(ping_event(), Context())
        loop = aws_lambda._loop
        lambda_handler(ping_event(), Context())
        # An HTTP client made during the first invocation is still usable
        # in the second only if this holds.
        assert aws_lambda._loop is loop
        assert not loop.is_closed()
        assert http_handler._plugin_manager.loop_at_build is loop

    def test_opting_out_rebuilds_the_plugin_per_request(self, fake_server):
        fake_server["server_reuse_across_invocations"] = False
        for _ in range(2):
            assert lambda_handler(ping_event(), Context())["statusCode"] == 200
        assert FakePluginManager.built == 2
        assert FakePluginManager.shut_down == 2
        assert http_handler._plugin_manager is None

    def test_an_unexpected_failure_discards_the_server(self, fake_server, monkeypatch):
        lambda_handler(ping_event(), Context())
        assert FakePluginManager.built == 1

        async def boom(self, **kwargs):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(http_handler.UniversalHTTPHandler, "handle_request", boom)
        response = lambda_handler(ping_event(), Context())
        assert response["statusCode"] == 500
        # Not carried into the next invocation in an unknown state.
        assert FakePluginManager.shut_down == 1
        assert http_handler._plugin_manager is None

    def test_a_closed_loop_is_replaced(self, fake_server):
        lambda_handler(ping_event(), Context())
        aws_lambda._loop.close()
        assert lambda_handler(ping_event(), Context())["statusCode"] == 200
        assert not aws_lambda._loop.is_closed()


class TestReuseSetting:
    def test_default_is_on(self):
        assert get_http_settings(dict(BASE_CONFIG))["reuse_server"] is True

    def test_false_turns_it_off(self):
        config = {**BASE_CONFIG, "server_reuse_across_invocations": False}
        assert get_http_settings(config)["reuse_server"] is False

    @pytest.mark.parametrize("bad", ["no", 0, 1, []])
    def test_anything_but_a_boolean_is_refused(self, bad):
        config = {**BASE_CONFIG, "server_reuse_across_invocations": bad}
        with pytest.raises(ConfigurationError, match="server_reuse_across_invocations"):
            get_http_settings(config)


class TestLoggedValuesAreCapped:
    @pytest.fixture(autouse=True)
    def default_limit(self):
        set_log_value_limit(None)
        yield
        set_log_value_limit(None)

    def test_a_small_value_is_logged_whole(self):
        value = {"name": "tool", "arguments": {"q": "x"}}
        assert cap_for_log(value) is value

    def test_a_large_value_becomes_a_summary(self):
        value = {"content": "x" * 50_000, "isError": False}
        capped = cap_for_log(value, keep=("isError",))
        assert capped["_truncated"] is True
        assert capped["_bytes"] > 50_000
        assert capped["_keys"] == ["content", "isError"]
        assert capped["isError"] is False
        assert len(json.dumps(capped)) < 300

    def test_a_kept_key_that_is_itself_large_is_dropped(self):
        value = {"name": "n" * 5_000, "arguments": "a" * 50_000}
        capped = cap_for_log(value, keep=("name",))
        assert "name" not in capped

    def test_large_params_still_say_which_tool_and_client(self):
        params = {
            "name": "query",
            "arguments": {"polygon": "p" * 50_000},
            "clientInfo": {"name": "claude-ai", "version": "1"},
        }
        logged = format_jsonrpc_request_log(1, "tools/call", params)["jsonrpc_params"]
        assert logged["_truncated"] is True
        assert logged["name"] == "query"
        assert logged["clientInfo"] == {"name": "claude-ai", "version": "1"}

    def test_a_large_result_is_capped(self):
        result = {"content": [{"type": "text", "text": "t" * 50_000}], "isError": False}
        logged = format_jsonrpc_response_log(1, "tools/call", result=result)
        assert logged["jsonrpc_result"]["_truncated"] is True
        assert logged["jsonrpc_result"]["isError"] is False
        assert logged["success"] is True

    def test_zero_turns_the_cap_off(self):
        set_log_value_limit(0)
        value = {"content": "x" * 50_000}
        assert cap_for_log(value) is value

    def test_a_value_that_cannot_be_measured_does_not_raise(self):
        class Unprintable:
            def __str__(self):
                raise RuntimeError("no")

        assert cap_for_log({"x": Unprintable()}) == {"_truncated": True}

    def test_the_http_layer_logs_sizes_not_bodies(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        request = format_request_log("r", "POST", "/mcp", {}, body)
        response = format_response_log("r", 200, {}, "é" * 10, 1.0)
        assert "request_body" not in request and "response_body" not in response
        assert request["request_bytes"] == len(body)
        assert response["response_bytes"] == 20  # bytes, not characters
        assert format_request_log("r", "POST", "/mcp", {}, b"abc")["request_bytes"] == 3
        assert format_request_log("r", "POST", "/mcp", {}, None)["request_bytes"] == 0


class TestLogLimitSetting:
    def test_unset_means_the_default(self):
        assert get_log_value_limit(dict(BASE_CONFIG)) is None
        assert get_log_value_limit({**BASE_CONFIG, "logging": {"level": "INFO"}}) is None

    def test_a_number_is_read(self):
        config = {**BASE_CONFIG, "logging": {"max_value_bytes": 0}}
        assert get_log_value_limit(config) == 0

    @pytest.mark.parametrize("bad", [-1, "8192", True, 1.5])
    def test_a_malformed_value_stops_the_config_loading(self, bad):
        config = {**BASE_CONFIG, "logging": {"max_value_bytes": bad}}
        with pytest.raises(ConfigurationError, match="max_value_bytes"):
            validate_config(config)

    def test_the_deployment_setting_is_applied_at_startup(self, fake_server):
        fake_server["logging"] = {"max_value_bytes": 123}
        lambda_handler(ping_event(), Context())
        assert logging_utils._log_value_max_bytes == 123


class TestOneRequestIsLoggedOnce:
    def test_a_large_result_is_not_written_to_the_log(self, fake_server, caplog, monkeypatch):
        big = "z" * 60_000

        monkeypatch.setattr(FakePluginManager, "get_all_tools", lambda self: [
            {"name": "t", "description": big, "inputSchema": {"type": "object"}}
        ])
        event = ping_event()
        event["body"] = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        with caplog.at_level(logging.INFO):
            response = lambda_handler(event, Context())
        assert response["statusCode"] == 200
        assert big in response["body"]  # the caller still gets all of it
        logged = "".join(
            json.dumps(
                {k: v for k, v in vars(record).items() if k not in ("args", "exc_info")},
                default=str,
            )
            for record in caplog.records
        )
        assert big not in logged
