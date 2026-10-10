"""Explicit local capability registration and dispatch."""
from typing import Iterable, Mapping, Any

from .base import BaseTool
from .schemas import ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool, *, overwrite: bool = False) -> BaseTool:
        if tool.name in self._tools and not overwrite:
            raise ValueError(f"Tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        return tool

    def register_many(self, tools: Iterable[BaseTool], *, overwrite: bool = False) -> list[BaseTool]:
        return [self.register(tool, overwrite=overwrite) for tool in tools]

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"Tool {name!r} is not registered")
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def as_openai_tools(self) -> list[dict]:
        return [tool.as_openai_tool() for tool in self._tools.values()]

    def invoke(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        return self.get(name).invoke(arguments)

    async def ainvoke(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        return await self.get(name).ainvoke(arguments)
