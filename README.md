# opencontext-core

The shared server core for OpenContext MCP deployments. One copy of the
protocol and transport code, used by every server instead of a vendored
copy in each repo.

It contains:

- `core/` — MCP JSON-RPC handling for both protocol eras (the legacy
  `initialize` handshake through 2025-11-25, and stateless 2026-07-28),
  the plugin manager, the plugin interfaces, config validation, logging.
- `server/` — the cloud-agnostic HTTP handler and the AWS Lambda adapter
  (`server.adapters.aws_lambda.lambda_handler`).

It contains no plugins and nothing specific to any one deployment. A
deployment is its own repo: its plugin under `plugins/` or
`custom_plugins/`, its `config.yaml`, its infrastructure.

The import names are `core` and `server`, not `opencontext_core`, so that
plugins (`from core.interfaces import ...`) and Lambda handler strings
work unchanged when a repo switches from vendored copies to this package.

## Using it in a deployment

Pin a tag in the deployment's `requirements.txt`:

```
opencontext-core @ git+https://github.com/codeforanchorage/opencontext-core@v1.5.0
```

Each deployment upgrades on its own schedule by changing that tag.

To work on the core and a deployment together, install the core editable
into the deployment's environment:

```
pip install -e ../opencontext-core
```

### Packaging for Lambda

Install the requirements into the package directory, then copy the
deployment's own `plugins/` beside them. Do not copy `core/` or
`server/`: the install puts them there.

With `uv pip install --target ... --python-platform ... --python-version
...` one step is enough. With plain `pip` it is not: pip will not build a
package while `--platform`/`--python-version` are set, and a git-tag
requirement has to be built. Install everything else with
`--only-binary :all:` and the platform flags first, then this package on
its own with `--no-deps` and no platform flags (it is pure Python). That
means the deployment's `requirements.txt` must itself list this
package's dependencies: `pydantic`, `PyYAML`, `python-json-logger>=3.1.0`.
The converted Boston deployment's `scripts/deploy.sh` shows both paths.

### Where plugins are found

`core.plugin_manager.plugin_root()` resolves, in order:

1. `$OPENCONTEXT_PLUGIN_ROOT`, when set.
2. The directory the `core` package sits in, when `plugins/` or
   `custom_plugins/` is there too. This is the Lambda zip, where
   dependencies are installed next to the deployment's code.
3. `$LAMBDA_TASK_ROOT`, else the current directory. This is local
   development, where `core` lives in site-packages: run from the
   deployment's repo root.

### Per-deployment settings (`config.yaml`)

| Key | Default | Meaning |
|---|---|---|
| `server_name`, `server_version`, `server_website_url` | `OpenContext`, `1.0.0`, none | `serverInfo` |
| `instructions` | the plugin's `get_instructions()`, else none | MCP `instructions` text; config wins over the plugin |
| `server_mcp_paths` | `["/mcp"]` | Request paths served as MCP routes |
| `server_max_body_bytes` | no limit | Reject larger request bodies with HTTP 413 before parsing. Leave unset if any tool takes large arguments |
| `server_reuse_across_invocations` | `true` | On Lambda, keep the plugin manager, the plugin and its HTTP client alive between invocations of a warm container. `false` rebuilds the plugin for every request, as before 1.5.0 |
| `logging.max_value_bytes` | `8192` | Largest serialized size of one value logged from a JSON-RPC message (params, result, error); a larger one is logged as a summary. `0` = no cap |
| `server_allowed_origins` | claude.ai, claude.com, console.anthropic.com, MCP Inspector on localhost | The complete browser-origin allowlist; the first entry is the default CORS origin |

Exactly one plugin must be enabled under `plugins:`.

### Environment

`OPENCONTEXT_CONFIG` may hold the config as serialized JSON (what
infrastructure sets when the config is small enough for an
environment variable) or a path to a YAML file (convenient locally).
When it is empty, `config.yaml` is read from `$LAMBDA_TASK_ROOT`, else
the current directory.

## Changes

**1.5.0** — the Lambda adapter keeps the server between invocations, and
logging is bounded. No protocol change. Read the first item before
upgrading a deployment.

- **A plugin now lives as long as its warm container, not one request.**
  The adapter used to shut the plugin manager down after every request
  and build it again for the next, because `asyncio.run` closes its loop
  and an HTTP client cannot outlive the loop it was made on. Plugin
  `initialize()` therefore ran on every request, `ping` and `tools/list`
  included. On the server where this was found that was six upstream
  calls and a new TLS context each time. The adapter now runs every
  invocation on one event loop and keeps the plugin manager.
  What to check in a plugin before upgrading:
  - State on `self` (caches, counters, a per-request deadline) now
    persists across requests. Anything that assumed a fresh object per
    request needs a lifetime or a reset.
  - The HTTP client is reused, so its connection pool survives a
    container being frozen between invocations. A plugin should retry
    a transport error once, as it should have already.
  - `initialize()` runs once per container, so a startup fetch that
    failed is not retried on the next request unless the plugin does so.
  Set `server_reuse_across_invocations: false` to keep the old behaviour.
  If an invocation fails outside the handler's own error handling, the
  server is discarded and rebuilt on the next request.
- **A request is logged once, and large values are summarised.** The HTTP
  layer logged the whole request and response body and the JSON-RPC
  layer logged them again as `jsonrpc_params` / `jsonrpc_result`, with no
  size limit: one `tools/list` wrote about 98 KB of log. The HTTP lines
  now carry `request_bytes` / `response_bytes` instead of
  `request_body` / `response_body`. A `jsonrpc_params`, `jsonrpc_result`
  or `jsonrpc_error` over `logging.max_value_bytes` (default 8192) is
  logged as `{"_truncated": true, "_bytes": N, "_keys": [...]}`, keeping
  `name`, `clientInfo` and `protocolVersion` from params and `isError`,
  `resultType`, `protocolVersion` and `serverInfo` from results, so
  queries on `jsonrpc_params.name` and `jsonrpc_params.clientInfo.name`
  are unaffected. A query that read `request_body` or `response_body`
  must move to the `jsonrpc_*` fields.
- A malformed `logging.max_value_bytes` or
  `server_reuse_across_invocations` stops the config loading.

**1.4.1** — hardening from a review of 1.4.0. No protocol change.

- A response is always valid JSON. A NaN or Infinity in a tool result
  was written as a bare token, which strict parsers reject, so the
  client lost the whole response with HTTP 200; it is now `null`. A
  value JSON cannot encode (a datetime) raised outside the request's
  error handling and became a 500 with `id: null`; it is now its
  string form, with the request id kept.
- `tools/call` with a `name` that is not a string is -32602, not
  -32603 with a traceback.
- `logging.level` left empty or set to a number no longer crashes the
  handler module at import.
- An empty `server_name` / `server_version` falls back to the default
  instead of sending `serverInfo.name: null`.
- A Lambda event whose `requestContext` is null is served.

**1.4.0** — fixes from two independent code reviews of 1.3.1.

- A tool that raises unexpectedly no longer has its exception text sent
  to the client: the result says the tool failed and gives an `Error ID`,
  and the exception is logged under that ID. `ToolInputError` (and the
  core's own `InvalidToolParamsError` / `UnknownToolError`) still pass
  their message through, because the plugin wrote it for the caller. A
  plain `ValueError` is scrubbed; raise `ToolInputError` or return a
  `ToolResult` for a message a model should see.
- A config delivered as JSON in `OPENCONTEXT_CONFIG` gets the same
  validation as a YAML file. A malformed `server_*` setting used to be
  silently replaced by the defaults; now the server refuses to start.
- Bodies a caller can use to provoke a 500 are refused with 400 /
  `-32700`: nested too deep to parse, not UTF-8, not valid base64. A
  non-UTF-8 body is never decoded leniently and run. Request logging
  caps how deep it follows nested arguments (logs only).
- One header normalisation (`server.http_handler.normalize_headers`) for
  the handler and the Lambda adapter, accepting any mapping, so a
  preflight and its request cannot disagree about the Origin.
- A legacy-era notification (no `id`) no longer executes a tool.
- The 404 for an unknown path no longer lists the configured routes.
- `server_max_body_bytes` is measured on the bytes received.

**1.3.1** — test-only: one test still asserted the pre-1.3.0 error text,
so the v1.3.0 tag has a failing test. No code change. Pin this instead.

**1.3.0** — hardening brought over from the eBird deployment. An
internal error (`-32603`) no longer sends the exception text to the
client: `data` is `Error ID: <id>`, and the full exception is logged
under that ID. The HTTP handler treats header names
case-insensitively and a missing body as empty. New optional
`server_max_body_bytes`.

**1.2.0** — `initialize` with a protocol version the server does not
implement is answered with the newest legacy revision it does
(`2025-11-25`), as the spec recommends, instead of `2025-03-26`. A request
naming no version still gets `2025-03-26`.

**1.1.0** — brought over from the Living Atlas deployment, plus one
fix. `tools/call` treats `arguments: null` as no arguments, and
rejects a non-object `params` by name. The request log line carries
`mcp_protocol_version` and `mcp_protocol_version_assumed`.
`OPENCONTEXT_CONFIG` may be a YAML file path. `config.yaml` is read
as UTF-8 on every platform.

**1.0.0** — extracted from the Anchorage GIS deployment.

## Development

```
uv venv && uv pip install -e ".[dev]"
pytest -n auto
ruff check .
```

Run the suite on Python 3.11 as well as the newest runtime before
tagging: deployments run both.

Do not run `ruff format` across the repo; the source is hand-wrapped and
not format-clean.

## Releasing

Bump `version` in `pyproject.toml` and `__version__` in
`core/__init__.py`, push `main`, and wait for the CI workflow to go green
on that commit. Only then tag `vX.Y.Z` and push the tag. CI runs lint and
the suite serially and in parallel on Python 3.11 and 3.14, and checks
that the two version strings and the tag agree. (v1.3.0 was tagged with a
failing test before this workflow existed.) Nothing changes for any
deployment until it moves its pin.

## Origin

Extracted from the Anchorage GIS deployment of
[OpenContext](https://github.com/CityOfBoston/OpenContext) (City of
Boston Department of Innovation and Technology, MIT).
