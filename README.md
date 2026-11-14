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
opencontext-core @ git+https://github.com/codeforanchorage/opencontext-core@v1.3.1
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
| `server_allowed_origins` | claude.ai, claude.com, console.anthropic.com, MCP Inspector on localhost | The complete browser-origin allowlist; the first entry is the default CORS origin |

Exactly one plugin must be enabled under `plugins:`.

### Environment

`OPENCONTEXT_CONFIG` may hold the config as serialized JSON (what
infrastructure sets when the config is small enough for an
environment variable) or a path to a YAML file (convenient locally).
When it is empty, `config.yaml` is read from `$LAMBDA_TASK_ROOT`, else
the current directory.

## Changes

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
`core/__init__.py`, tag `vX.Y.Z`, push the tag. Nothing changes for any
deployment until it moves its pin.

## Origin

Extracted from the Anchorage GIS deployment of
[OpenContext](https://github.com/CityOfBoston/OpenContext) (City of
Boston Department of Innovation and Technology, MIT).
