from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import Iterable

from ..core.client import MCPToolAdapter, discover_mcp_tools, register_mcp_tools
from ..core.config import MCPConfigError, MCPServerConfig, load_env_file, load_json_config, read_optional_float, read_optional_path_list, read_optional_string
from ...registry import ToolRegistry

DEFAULT_SERVER_NAME = "filesystem"
DEFAULT_TOOL_NAMESPACE = "filesystem"
DEFAULT_COMMAND = "npx"
DEFAULT_PACKAGE = "@modelcontextprotocol/server-filesystem"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "mcp" / "filesystem.json"


def build_filesystem_server_config(
    allowed_directories: Iterable[str | Path],
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    tool_namespace: str = DEFAULT_TOOL_NAMESPACE,
    command: str = DEFAULT_COMMAND,
    npm_cache_dir: str | Path | None = None,
    cwd: str | Path | None = None,
    read_timeout_seconds: float | None = 60.0,
    extra_env: dict[str, str] | None = None,
) -> MCPServerConfig:
    normalized_directories = [str(Path(path).resolve()) for path in allowed_directories]
    if not normalized_directories:
        raise ValueError("Filesystem MCP requires at least one allowed directory.")

    resolved_cwd = Path(cwd).resolve() if cwd is not None else Path.cwd()
    resolved_cache_dir = Path(npm_cache_dir).resolve() if npm_cache_dir is not None else resolved_cwd / ".npm-cache"

    env = {"npm_config_cache": str(resolved_cache_dir)}
    env.update(extra_env or {})

    return MCPServerConfig.from_command(
        server_name=server_name,
        command=command,
        args=["-y", DEFAULT_PACKAGE, *normalized_directories],
        env=env,
        cwd=resolved_cwd,
        read_timeout_seconds=read_timeout_seconds,
        tool_namespace=tool_namespace,
    )


def build_filesystem_server_config_from_json(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> MCPServerConfig:
    payload = load_json_config(config_path or DEFAULT_CONFIG_PATH, env_path=env_path)
    if not payload.get("enabled", True):
        raise MCPConfigError("Filesystem MCP is disabled in its JSON config.")
    transport = str(payload.get("transport") or "stdio").lower()
    if transport != "stdio":
        raise MCPConfigError("Filesystem MCP adapter currently supports only 'stdio' transport.")

    allowed_directories = payload.get("allowed_dirs", [])
    if not isinstance(allowed_directories, list) or not allowed_directories:
        raise MCPConfigError("Filesystem MCP JSON config requires a non-empty 'allowed_dirs' array.")

    command = str(payload.get("command") or DEFAULT_COMMAND)
    package_name = str(payload.get("package") or DEFAULT_PACKAGE)
    extra_env = payload.get("env") or {}
    if extra_env and not isinstance(extra_env, dict):
        raise MCPConfigError("'env' in filesystem JSON config must be an object.")

    server_config = build_filesystem_server_config(
        allowed_directories=allowed_directories,
        server_name=str(payload.get("server_name") or DEFAULT_SERVER_NAME),
        tool_namespace=str(payload.get("namespace") or DEFAULT_TOOL_NAMESPACE),
        command=command,
        npm_cache_dir=payload.get("npm_cache_dir"),
        cwd=payload.get("cwd"),
        read_timeout_seconds=_coerce_optional_float(payload.get("read_timeout_seconds"), default=60.0),
        extra_env={str(key): str(value) for key, value in extra_env.items()},
    )
    server_config.args = ["-y", package_name, *[str(Path(path).resolve()) for path in allowed_directories]]
    return server_config


def build_filesystem_server_config_from_env(
    *,
    env_path: str | Path | None = None,
    default_workspace: str | Path | None = None,
) -> MCPServerConfig:
    load_env_file(env_path)

    workspace = Path(default_workspace).resolve() if default_workspace is not None else Path.cwd()
    allowed_dirs = read_optional_path_list("MCP_FILESYSTEM_ALLOWED_DIRS") or [str(workspace)]

    return build_filesystem_server_config(
        allowed_directories=allowed_dirs,
        server_name=read_optional_string("MCP_FILESYSTEM_SERVER_NAME") or DEFAULT_SERVER_NAME,
        tool_namespace=read_optional_string("MCP_FILESYSTEM_NAMESPACE") or DEFAULT_TOOL_NAMESPACE,
        command=read_optional_string("MCP_FILESYSTEM_COMMAND") or DEFAULT_COMMAND,
        npm_cache_dir=read_optional_string("MCP_FILESYSTEM_NPM_CACHE_DIR") or (workspace / ".npm-cache"),
        cwd=read_optional_string("MCP_FILESYSTEM_CWD") or workspace,
        read_timeout_seconds=read_optional_float("MCP_FILESYSTEM_READ_TIMEOUT_SECONDS", default=60.0),
    )


async def discover_filesystem_tools(
    allowed_directories: Iterable[str | Path],
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    tool_namespace: str = DEFAULT_TOOL_NAMESPACE,
    command: str = DEFAULT_COMMAND,
    npm_cache_dir: str | Path | None = None,
    cwd: str | Path | None = None,
    read_timeout_seconds: float | None = 60.0,
    extra_env: dict[str, str] | None = None,
) -> list[MCPToolAdapter]:
    server_config = build_filesystem_server_config(
        allowed_directories,
        server_name=server_name,
        tool_namespace=tool_namespace,
        command=command,
        npm_cache_dir=npm_cache_dir,
        cwd=cwd,
        read_timeout_seconds=read_timeout_seconds,
        extra_env=extra_env,
    )
    return await discover_mcp_tools(server_config)


async def discover_filesystem_tools_from_json(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> list[MCPToolAdapter]:
    server_config = build_filesystem_server_config_from_json(
        config_path=config_path,
        env_path=env_path,
    )
    return await discover_mcp_tools(server_config)


async def discover_filesystem_tools_from_env(
    *,
    env_path: str | Path | None = None,
    default_workspace: str | Path | None = None,
) -> list[MCPToolAdapter]:
    server_config = build_filesystem_server_config_from_env(
        env_path=env_path,
        default_workspace=default_workspace,
    )
    return await discover_mcp_tools(server_config)


async def register_filesystem_tools(
    registry: ToolRegistry,
    allowed_directories: Iterable[str | Path],
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    tool_namespace: str = DEFAULT_TOOL_NAMESPACE,
    command: str = DEFAULT_COMMAND,
    npm_cache_dir: str | Path | None = None,
    cwd: str | Path | None = None,
    read_timeout_seconds: float | None = 60.0,
    extra_env: dict[str, str] | None = None,
    overwrite: bool = False,
) -> list[MCPToolAdapter]:
    server_config = build_filesystem_server_config(
        allowed_directories,
        server_name=server_name,
        tool_namespace=tool_namespace,
        command=command,
        npm_cache_dir=npm_cache_dir,
        cwd=cwd,
        read_timeout_seconds=read_timeout_seconds,
        extra_env=extra_env,
    )
    return await register_mcp_tools(registry, server_config, overwrite=overwrite)


async def register_filesystem_tools_from_json(
    registry: ToolRegistry,
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
    overwrite: bool = False,
) -> list[MCPToolAdapter]:
    server_config = build_filesystem_server_config_from_json(
        config_path=config_path,
        env_path=env_path,
    )
    return await register_mcp_tools(registry, server_config, overwrite=overwrite)


async def register_filesystem_tools_from_env(
    registry: ToolRegistry,
    *,
    env_path: str | Path | None = None,
    default_workspace: str | Path | None = None,
    overwrite: bool = False,
) -> list[MCPToolAdapter]:
    server_config = build_filesystem_server_config_from_env(
        env_path=env_path,
        default_workspace=default_workspace,
    )
    return await register_mcp_tools(registry, server_config, overwrite=overwrite)


def _coerce_optional_float(value: Any, *, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise MCPConfigError(f"Expected a numeric timeout value, got: {value!r}") from exc
