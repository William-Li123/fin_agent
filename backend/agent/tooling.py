from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import anyio

from backend.memory import MemoryService
from backend.observability import bootstrap_langsmith_env
from backend.rag import RAGService
from backend.sandbox import OpenSandboxManager
from backend.tools import ToolRegistry, register_memory_tools, register_rag_tools, register_terminal_tools
from backend.tools.mcp import (
    register_bing_cn_search_tools_from_json,
    register_fetch_tools_from_json,
    register_filesystem_tools,
    register_filesystem_tools_from_json,
)

DEFAULT_MCP_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "mcp"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class ToolBootstrapResult:
    registry: ToolRegistry
    tool_names: list[str]


def build_default_tool_registry(
    *,
    env_path: str | Path | None = None,
    rag_config_path: str | Path | None = None,
    mcp_config_dir: str | Path | None = None,
    memory_config_path: str | Path | None = None,
    rag_service: RAGService | None = None,
    memory_service: MemoryService | None = None,
    sandbox_manager: OpenSandboxManager | None = None,
    include_rag: bool = True,
    include_memory: bool = True,
    include_terminal: bool = True,
    include_filesystem: bool = True,
    include_fetch: bool = True,
    include_bing: bool = True,
    filesystem_allowed_dirs: Iterable[str | Path] | None = None,
    filesystem_cwd: str | Path | None = None,
) -> ToolBootstrapResult:
    bootstrap_langsmith_env(env_path)
    registry = ToolRegistry()

    if include_rag:
        register_rag_tools(
            registry,
            rag_service=rag_service,
            config_path=rag_config_path,
            env_path=env_path,
            overwrite=False,
        )
    if include_memory:
        register_memory_tools(
            registry,
            memory_service=memory_service,
            config_path=memory_config_path,
            env_path=env_path,
            overwrite=False,
        )
    if include_terminal:
        register_terminal_tools(
            registry,
            sandbox_manager=sandbox_manager,
            env_path=env_path,
            overwrite=False,
        )

    config_dir = Path(mcp_config_dir).resolve() if mcp_config_dir is not None else DEFAULT_MCP_CONFIG_DIR.resolve()
    anyio.run(
        _register_async_tools,
        registry,
        config_dir,
        env_path,
        include_filesystem,
        include_fetch,
        include_bing,
        filesystem_allowed_dirs,
        filesystem_cwd,
    )

    return ToolBootstrapResult(
        registry=registry,
        tool_names=[tool.name for tool in registry.list_tools()],
    )


async def _register_async_tools(
    registry: ToolRegistry,
    config_dir: Path,
    env_path: str | Path | None,
    include_filesystem: bool,
    include_fetch: bool,
    include_bing: bool,
    filesystem_allowed_dirs: Iterable[str | Path] | None,
    filesystem_cwd: str | Path | None,
) -> None:
    if include_filesystem:
        if filesystem_allowed_dirs is not None:
            await register_filesystem_tools(
                registry,
                allowed_directories=filesystem_allowed_dirs,
                cwd=filesystem_cwd,
                npm_cache_dir=PROJECT_ROOT / ".npm-cache",
            )
        else:
            await register_filesystem_tools_from_json(
                registry,
                config_path=config_dir / "filesystem.json",
                env_path=env_path,
            )
    if include_fetch:
        await register_fetch_tools_from_json(
            registry,
            config_path=config_dir / "fetch.json",
            env_path=env_path,
        )
    if include_bing:
        await register_bing_cn_search_tools_from_json(
            registry,
            config_path=config_dir / "bing_cn_search.json",
            env_path=env_path,
        )
