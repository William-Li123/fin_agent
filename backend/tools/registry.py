from __future__ import annotations

from typing import Any, Iterable, Mapping

from langsmith import traceable

from backend.observability.langsmith import serialize_tool_inputs, serialize_tool_outputs

from .base import BaseTool
from .schemas import ToolDefinition, ToolResult


class ToolRegistry:
    """Registry and dispatch layer for all tools exposed to the agent."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool, *, overwrite: bool = False) -> BaseTool:
        if not overwrite and tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool
        return tool

    def register_many(self, tools: Iterable[BaseTool], *, overwrite: bool = False) -> list[BaseTool]:
        registered_tools: list[BaseTool] = []
        for tool in tools:
            registered_tools.append(self.register(tool, overwrite=overwrite))
        return registered_tools

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Tool '{name}' is not registered.") from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def list_definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def as_openai_tools(self) -> list[dict[str, Any]]:
        return [tool.as_openai_tool() for tool in self._tools.values()]

    @traceable(
        run_type="tool",
        name="tool_registry_invoke",
        process_inputs=serialize_tool_inputs,
        process_outputs=serialize_tool_outputs,
    )
    def invoke(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        return self.get(name).invoke(arguments)

    @traceable(
        run_type="tool",
        name="tool_registry_ainvoke",
        process_inputs=serialize_tool_inputs,
        process_outputs=serialize_tool_outputs,
    )
    async def ainvoke(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        return await self.get(name).ainvoke(arguments)
