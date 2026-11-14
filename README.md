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
opencontext-core @ git+https://github.com/codeforanchorage/opencontext-core@v1.0.0
```

Each deployment upgrades on its own schedule by changing that tag.

To work on the core and a deployment together, install the core editable
into the deployment's environment:

```
pip install -e ../opencontext-core
```

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
| `server_allowed_origins` | claude.ai, claude.com, console.anthropic.com, MCP Inspector on localhost | The complete browser-origin allowlist; the first entry is the default CORS origin |

Exactly one plugin must be enabled under `plugins:`.

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
