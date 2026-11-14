"""Stand-in plugin for the core's own tests.

The core ships no plugins; a deployment brings its own. This one exists
so discovery and loading can be exercised against a real directory. It
is named ``ckan`` only because that is the name the plugin-manager tests
have always enabled in their configs.
"""

from typing import Any, Dict, List

from core.interfaces import MCPPlugin, PluginType, ToolDefinition, ToolResult


class StandInPlugin(MCPPlugin):
    plugin_name = "ckan"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "0.0.0"

    async def initialize(self) -> bool:
        self._initialized = True
        return True

    async def shutdown(self) -> None:
        self._initialized = False

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="echo",
                description="Return the arguments it was given.",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        return ToolResult(
            content=[{"type": "text", "text": str(arguments)}], success=True
        )

    async def health_check(self) -> bool:
        return True
