from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolSource(str, Enum):
    LOCAL = "local"
    MCP = "mcp"
    RAG = "rag"
    MEMORY = "memory"


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    source: ToolSource = ToolSource.LOCAL
    metadata: dict[str, Any] = Field(default_factory=dict)

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolContentItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    type: str = "text"
    text: str | None = None
    data: str | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")
    resource: Any | None = None
    annotations: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class ToolResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_name: str
    success: bool = True
    content: list[ToolContentItem] = Field(default_factory=list)
    structured_content: Any | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: Any | None = None

    @property
    def text(self) -> str:
        text_parts = [item.text for item in self.content if item.text]
        if text_parts:
            return "\n".join(text_parts)
        if isinstance(self.structured_content, str):
            return self.structured_content
        if self.error:
            return self.error
        return ""
