from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.client import MCPToolAdapter, discover_mcp_tools, register_mcp_tools
from ..core.config import MCPConfigError, MCPServerConfig, load_env_file, load_json_config, read_optional_float, read_optional_string
from ...registry import ToolRegistry

DEFAULT_SERVER_NAME = "bing_cn_search"
DEFAULT_TOOL_NAMESPACE = "bing"
DEFAULT_COMMAND = "cmd"
DEFAULT_PACKAGE = "bing-cn-mcp"
DEFAULT_LAUNCH_MODE = "cmd_npx"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "mcp" / "bing_cn_search.json"


def build_bing_cn_search_server_config(
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    tool_namespace: str = DEFAULT_TOOL_NAMESPACE,
    launch_mode: str = DEFAULT_LAUNCH_MODE,
    command: str | None = None,
    package_name: str = DEFAULT_PACKAGE,
    npm_cache_dir: str | Path | None = None,
    cwd: str | Path | None = None,
    read_timeout_seconds: float | None = 60.0,
    extra_env: dict[str, str] | None = None,
) -> MCPServerConfig:
    resolved_cwd = Path(cwd).resolve() if cwd is not None else Path.cwd()
    resolved_cache_dir = Path(npm_cache_dir).resolve() if npm_cache_dir is not None else resolved_cwd / ".npm-cache"

    env = {"npm_config_cache": str(resolved_cache_dir)}
    env.update(extra_env or {})

    normalized_launch_mode = launch_mode.strip().lower()
    if normalized_launch_mode == "cmd_npx":
        resolved_command = command or DEFAULT_COMMAND
        args = ["/c", "npx", "-y", package_name]
    elif normalized_launch_mode == "npx":
        resolved_command = command or "npx"
        args = ["-y", package_name]
    else:
        raise MCPConfigError("Bing CN MCP launch_mode must be 'cmd_npx' or 'npx'.")

    return MCPServerConfig.from_command(
        server_name=server_name,
        command=resolved_command,
        args=args,
        env=env,
        cwd=resolved_cwd,
        read_timeout_seconds=read_timeout_seconds,
        tool_namespace=tool_namespace,
    )


def build_bing_cn_search_server_config_from_json(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> MCPServerConfig:
    payload = load_json_config(config_path or DEFAULT_CONFIG_PATH, env_path=env_path)
    if not payload.get("enabled", True):
        raise MCPConfigError("Bing CN search MCP is disabled in its JSON config.")
    transport = str(payload.get("transport") or "stdio").lower()
    if transport != "stdio":
        raise MCPConfigError("Bing CN search MCP adapter currently supports only 'stdio' transport.")

    extra_env = payload.get("env") or {}
    if extra_env and not isinstance(extra_env, dict):
        raise MCPConfigError("'env' in Bing CN search JSON config must be an object.")

    return build_bing_cn_search_server_config(
        server_name=str(payload.get("server_name") or DEFAULT_SERVER_NAME),
        tool_namespace=str(payload.get("namespace") or DEFAULT_TOOL_NAMESPACE),
        launch_mode=str(payload.get("launch_mode") or DEFAULT_LAUNCH_MODE),
        command=_normalize_optional_string(payload.get("command")),
        package_name=str(payload.get("package") or DEFAULT_PACKAGE),
        npm_cache_dir=payload.get("npm_cache_dir"),
        cwd=payload.get("cwd"),
        read_timeout_seconds=_coerce_optional_float(payload.get("read_timeout_seconds"), default=60.0),
        extra_env={str(key): str(value) for key, value in extra_env.items()},
    )


def build_bing_cn_search_server_config_from_env(
    *,
    env_path: str | Path | None = None,
    default_workspace: str | Path | None = None,
) -> MCPServerConfig:
    load_env_file(env_path)

    workspace = Path(default_workspace).resolve() if default_workspace is not None else Path.cwd()
    return build_bing_cn_search_server_config(
        server_name=read_optional_string("MCP_BING_CN_SEARCH_SERVER_NAME") or DEFAULT_SERVER_NAME,
        tool_namespace=read_optional_string("MCP_BING_CN_SEARCH_NAMESPACE") or DEFAULT_TOOL_NAMESPACE,
        launch_mode=read_optional_string("MCP_BING_CN_SEARCH_LAUNCH_MODE") or DEFAULT_LAUNCH_MODE,
        command=read_optional_string("MCP_BING_CN_SEARCH_COMMAND"),
        package_name=read_optional_string("MCP_BING_CN_SEARCH_PACKAGE") or DEFAULT_PACKAGE,
        npm_cache_dir=read_optional_string("MCP_BING_CN_SEARCH_NPM_CACHE_DIR") or (workspace / ".npm-cache"),
        cwd=read_optional_string("MCP_BING_CN_SEARCH_CWD") or workspace,
        read_timeout_seconds=read_optional_float("MCP_BING_CN_SEARCH_READ_TIMEOUT_SECONDS", default=60.0),
    )


async def discover_bing_cn_search_tools(
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    tool_namespace: str = DEFAULT_TOOL_NAMESPACE,
    launch_mode: str = DEFAULT_LAUNCH_MODE,
    command: str | None = None,
    package_name: str = DEFAULT_PACKAGE,
    npm_cache_dir: str | Path | None = None,
    cwd: str | Path | None = None,
    read_timeout_seconds: float | None = 60.0,
    extra_env: dict[str, str] | None = None,
) -> list[MCPToolAdapter]:
    server_config = build_bing_cn_search_server_config(
        server_name=server_name,
        tool_namespace=tool_namespace,
        launch_mode=launch_mode,
        command=command,
        package_name=package_name,
        npm_cache_dir=npm_cache_dir,
        cwd=cwd,
        read_timeout_seconds=read_timeout_seconds,
        extra_env=extra_env,
    )
    return await discover_mcp_tools(server_config)


async def discover_bing_cn_search_tools_from_json(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> list[MCPToolAdapter]:
    server_config = build_bing_cn_search_server_config_from_json(
        config_path=config_path,
        env_path=env_path,
    )
    return await discover_mcp_tools(server_config)


async def discover_bing_cn_search_tools_from_env(
    *,
    env_path: str | Path | None = None,
    default_workspace: str | Path | None = None,
) -> list[MCPToolAdapter]:
    server_config = build_bing_cn_search_server_config_from_env(
        env_path=env_path,
        default_workspace=default_workspace,
    )
    return await discover_mcp_tools(server_config)


async def register_bing_cn_search_tools(
    registry: ToolRegistry,
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    tool_namespace: str = DEFAULT_TOOL_NAMESPACE,
    launch_mode: str = DEFAULT_LAUNCH_MODE,
    command: str | None = None,
    package_name: str = DEFAULT_PACKAGE,
    npm_cache_dir: str | Path | None = None,
    cwd: str | Path | None = None,
    read_timeout_seconds: float | None = 60.0,
    extra_env: dict[str, str] | None = None,
    overwrite: bool = False,
) -> list[MCPToolAdapter]:
    server_config = build_bing_cn_search_server_config(
        server_name=server_name,
        tool_namespace=tool_namespace,
        launch_mode=launch_mode,
        command=command,
        package_name=package_name,
        npm_cache_dir=npm_cache_dir,
        cwd=cwd,
        read_timeout_seconds=read_timeout_seconds,
        extra_env=extra_env,
    )
    return await register_mcp_tools(registry, server_config, overwrite=overwrite)


async def register_bing_cn_search_tools_from_json(
    registry: ToolRegistry,
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
    overwrite: bool = False,
) -> list[MCPToolAdapter]:
    server_config = build_bing_cn_search_server_config_from_json(
        config_path=config_path,
        env_path=env_path,
    )
    return await register_mcp_tools(registry, server_config, overwrite=overwrite)


async def register_bing_cn_search_tools_from_env(
    registry: ToolRegistry,
    *,
    env_path: str | Path | None = None,
    default_workspace: str | Path | None = None,
    overwrite: bool = False,
) -> list[MCPToolAdapter]:
    server_config = build_bing_cn_search_server_config_from_env(
        env_path=env_path,
        default_workspace=default_workspace,
    )
    return await register_mcp_tools(registry, server_config, overwrite=overwrite)


def _normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _coerce_optional_float(value: Any, *, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise MCPConfigError(f"Expected a numeric timeout value, got: {value!r}") from exc
