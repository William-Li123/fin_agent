from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

import anyio

from .schemas import ToolDefinition, ToolResult


class BaseTool(ABC):
    """Base contract shared by all tool implementations."""

    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def description(self) -> str:
        return self.definition.description

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.definition.input_schema

    def as_openai_tool(self) -> dict[str, Any]:
        return self.definition.as_openai_tool()

    def invoke(self, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        normalized_arguments = dict(arguments or {})
        try:
            return anyio.run(self.ainvoke, normalized_arguments)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Synchronous invoke() for tool '{self.name}' failed inside an active event loop. "
                "Use await tool.ainvoke(...) instead."
            ) from exc

    @abstractmethod
    async def ainvoke(self, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        raise NotImplementedError
