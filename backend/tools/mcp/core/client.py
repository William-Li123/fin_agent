from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Mapping

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from ...base import BaseTool
from ...registry import ToolRegistry
from ...schemas import ToolContentItem, ToolDefinition, ToolResult, ToolSource
from .config import MCPServerConfig

SAFE_TOOL_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")


class MCPToolAdapter(BaseTool):
    """Adapter that exposes a remote MCP tool through the local tool interface."""

    def __init__(
        self,
        *,
        server_config: MCPServerConfig,
        definition: ToolDefinition,
        remote_tool_name: str,
    ) -> None:
        super().__init__(definition)
        self.server_config = server_config
        self.remote_tool_name = remote_tool_name

    async def ainvoke(self, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        client = MCPStdioClient(self.server_config)
        return await client.call_tool(
            remote_tool_name=self.remote_tool_name,
            arguments=dict(arguments or {}),
            exposed_tool_name=self.name,
        )


class MCPStdioClient:
    """Thin stdio MCP client that can discover and invoke remote tools."""

    def __init__(self, server_config: MCPServerConfig) -> None:
        self.server_config = server_config

    @asynccontextmanager
    async def session(self):
        timeout = None
        if self.server_config.read_timeout_seconds is not None:
            timeout = timedelta(seconds=self.server_config.read_timeout_seconds)

        async with stdio_client(self.server_config.to_server_parameters()) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timeout,
            ) as session:
                await session.initialize()
                yield session

    async def list_tool_definitions(self) -> list[ToolDefinition]:
        async with self.session() as session:
            result = await session.list_tools()

        definitions: list[ToolDefinition] = []
        for tool in result.tools:
            definitions.append(
                ToolDefinition(
                    name=_qualify_tool_name(self.server_config.namespace, tool.name),
                    description=tool.description or f"MCP tool '{tool.name}' from server '{self.server_config.server_name}'.",
                    input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                    source=ToolSource.MCP,
                    metadata={
                        "server_name": self.server_config.server_name,
                        "tool_namespace": self.server_config.namespace,
                        "remote_tool_name": tool.name,
                        "transport": "stdio",
                        "title": tool.title,
                    },
                )
            )
        return definitions

    async def call_tool(
        self,
        *,
        remote_tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        exposed_tool_name: str | None = None,
    ) -> ToolResult:
        async with self.session() as session:
            result = await session.call_tool(remote_tool_name, dict(arguments or {}))

        content_items = [_coerce_tool_content_item(item) for item in result.content]
        error_text = None
        if result.isError:
            error_text = "\n".join(item.text for item in content_items if item.text) or "MCP tool returned an error."

        return ToolResult(
            tool_name=exposed_tool_name or remote_tool_name,
            success=not result.isError,
            content=content_items,
            structured_content=result.structuredContent,
            error=error_text,
            metadata={
                "server_name": self.server_config.server_name,
                "tool_namespace": self.server_config.namespace,
                "remote_tool_name": remote_tool_name,
                "transport": "stdio",
            },
            raw=result.model_dump(by_alias=True),
        )


async def discover_mcp_tools(server_config: MCPServerConfig) -> list[MCPToolAdapter]:
    client = MCPStdioClient(server_config)
    definitions = await client.list_tool_definitions()
    return [
        MCPToolAdapter(
            server_config=server_config,
            definition=definition,
            remote_tool_name=str(definition.metadata["remote_tool_name"]),
        )
        for definition in definitions
    ]


async def register_mcp_tools(
    registry: ToolRegistry,
    server_config: MCPServerConfig,
    *,
    overwrite: bool = False,
) -> list[MCPToolAdapter]:
    tools = await discover_mcp_tools(server_config)
    registry.register_many(tools, overwrite=overwrite)
    return tools


def _coerce_tool_content_item(item: Any) -> ToolContentItem:
    if hasattr(item, "model_dump"):
        payload = item.model_dump(by_alias=True)
    elif isinstance(item, dict):
        payload = item
    else:
        payload = {"type": "text", "text": str(item)}
    return ToolContentItem.model_validate(payload)


def _qualify_tool_name(namespace: str | None, tool_name: str) -> str:
    safe_name = _sanitize_tool_name(tool_name)
    if not namespace:
        return safe_name
    return f"{_sanitize_tool_name(namespace)}__{safe_name}"


def _sanitize_tool_name(value: str) -> str:
    normalized = SAFE_TOOL_NAME_PATTERN.sub("_", value.strip())
    normalized = normalized.strip("_")
    return normalized or "tool"
