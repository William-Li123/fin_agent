from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ..core.client import MCPToolAdapter, discover_mcp_tools, register_mcp_tools
from ..core.config import MCPConfigError, MCPServerConfig, load_env_file, load_json_config, read_optional_bool, read_optional_float, read_optional_string
from ...registry import ToolRegistry

DEFAULT_SERVER_NAME = "fetch"
DEFAULT_TOOL_NAMESPACE = "fetch"
DEFAULT_LAUNCH_MODE = "python_module"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "mcp" / "fetch.json"


def build_fetch_server_config(
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    tool_namespace: str = DEFAULT_TOOL_NAMESPACE,
    launch_mode: str = DEFAULT_LAUNCH_MODE,
    command: str | None = None,
    user_agent: str | None = None,
    ignore_robots_txt: bool = False,
    proxy_url: str | None = None,
    cwd: str | Path | None = None,
    read_timeout_seconds: float | None = 60.0,
    extra_env: dict[str, str] | None = None,
) -> MCPServerConfig:
    resolved_cwd = Path(cwd).resolve() if cwd is not None else Path.cwd()
    normalized_launch_mode = launch_mode.strip().lower()

    if normalized_launch_mode == "python_module":
        resolved_command = command or sys.executable
        args = ["-m", "mcp_server_fetch"]
    elif normalized_launch_mode == "uvx":
        resolved_command = command or "uvx"
        args = ["mcp-server-fetch"]
    else:
        raise MCPConfigError("MCP_FETCH_LAUNCH_MODE must be 'python_module' or 'uvx'.")

    if user_agent:
        args.extend(["--user-agent", user_agent])
    if ignore_robots_txt:
        args.append("--ignore-robots-txt")
    if proxy_url:
        args.extend(["--proxy-url", proxy_url])

    return MCPServerConfig.from_command(
        server_name=server_name,
        command=resolved_command,
        args=args,
        env=extra_env or {},
        cwd=resolved_cwd,
        read_timeout_seconds=read_timeout_seconds,
        tool_namespace=tool_namespace,
    )


def build_fetch_server_config_from_json(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> MCPServerConfig:
    payload = load_json_config(config_path or DEFAULT_CONFIG_PATH, env_path=env_path)
    if not payload.get("enabled", True):
        raise MCPConfigError("Fetch MCP is disabled in its JSON config.")
    transport = str(payload.get("transport") or "stdio").lower()
    if transport != "stdio":
        raise MCPConfigError("Fetch MCP adapter currently supports only 'stdio' transport.")

    extra_env = payload.get("env") or {}
    if extra_env and not isinstance(extra_env, dict):
        raise MCPConfigError("'env' in fetch JSON config must be an object.")

    return build_fetch_server_config(
        server_name=str(payload.get("server_name") or DEFAULT_SERVER_NAME),
        tool_namespace=str(payload.get("namespace") or DEFAULT_TOOL_NAMESPACE),
        launch_mode=str(payload.get("launch_mode") or DEFAULT_LAUNCH_MODE),
        command=_normalize_optional_string(payload.get("command")),
        user_agent=_normalize_optional_string(payload.get("user_agent")),
        ignore_robots_txt=_coerce_bool(payload.get("ignore_robots_txt"), default=False),
        proxy_url=_normalize_optional_string(payload.get("proxy_url")),
        cwd=payload.get("cwd"),
        read_timeout_seconds=_coerce_optional_float(payload.get("read_timeout_seconds"), default=60.0),
        extra_env={str(key): str(value) for key, value in extra_env.items()},
    )


def build_fetch_server_config_from_env(
    *,
    env_path: str | Path | None = None,
    default_workspace: str | Path | None = None,
) -> MCPServerConfig:
    load_env_file(env_path)

    workspace = Path(default_workspace).resolve() if default_workspace is not None else Path.cwd()
    return build_fetch_server_config(
        server_name=read_optional_string("MCP_FETCH_SERVER_NAME") or DEFAULT_SERVER_NAME,
        tool_namespace=read_optional_string("MCP_FETCH_NAMESPACE") or DEFAULT_TOOL_NAMESPACE,
        launch_mode=read_optional_string("MCP_FETCH_LAUNCH_MODE") or DEFAULT_LAUNCH_MODE,
        command=read_optional_string("MCP_FETCH_COMMAND"),
        user_agent=read_optional_string("MCP_FETCH_USER_AGENT"),
        ignore_robots_txt=read_optional_bool("MCP_FETCH_IGNORE_ROBOTS_TXT", default=False),
        proxy_url=read_optional_string("MCP_FETCH_PROXY_URL"),
        cwd=read_optional_string("MCP_FETCH_CWD") or workspace,
        read_timeout_seconds=read_optional_float("MCP_FETCH_READ_TIMEOUT_SECONDS", default=60.0),
    )


async def discover_fetch_tools(
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    tool_namespace: str = DEFAULT_TOOL_NAMESPACE,
    launch_mode: str = DEFAULT_LAUNCH_MODE,
    command: str | None = None,
    user_agent: str | None = None,
    ignore_robots_txt: bool = False,
    proxy_url: str | None = None,
    cwd: str | Path | None = None,
    read_timeout_seconds: float | None = 60.0,
    extra_env: dict[str, str] | None = None,
) -> list[MCPToolAdapter]:
    server_config = build_fetch_server_config(
        server_name=server_name,
        tool_namespace=tool_namespace,
        launch_mode=launch_mode,
        command=command,
        user_agent=user_agent,
        ignore_robots_txt=ignore_robots_txt,
        proxy_url=proxy_url,
        cwd=cwd,
        read_timeout_seconds=read_timeout_seconds,
        extra_env=extra_env,
    )
    return await discover_mcp_tools(server_config)


async def discover_fetch_tools_from_json(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> list[MCPToolAdapter]:
    server_config = build_fetch_server_config_from_json(
        config_path=config_path,
        env_path=env_path,
    )
    return await discover_mcp_tools(server_config)


async def discover_fetch_tools_from_env(
    *,
    env_path: str | Path | None = None,
    default_workspace: str | Path | None = None,
) -> list[MCPToolAdapter]:
    server_config = build_fetch_server_config_from_env(
        env_path=env_path,
        default_workspace=default_workspace,
    )
    return await discover_mcp_tools(server_config)


async def register_fetch_tools(
    registry: ToolRegistry,
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    tool_namespace: str = DEFAULT_TOOL_NAMESPACE,
    launch_mode: str = DEFAULT_LAUNCH_MODE,
    command: str | None = None,
    user_agent: str | None = None,
    ignore_robots_txt: bool = False,
    proxy_url: str | None = None,
    cwd: str | Path | None = None,
    read_timeout_seconds: float | None = 60.0,
    extra_env: dict[str, str] | None = None,
    overwrite: bool = False,
) -> list[MCPToolAdapter]:
    server_config = build_fetch_server_config(
        server_name=server_name,
        tool_namespace=tool_namespace,
        launch_mode=launch_mode,
        command=command,
        user_agent=user_agent,
        ignore_robots_txt=ignore_robots_txt,
        proxy_url=proxy_url,
        cwd=cwd,
        read_timeout_seconds=read_timeout_seconds,
        extra_env=extra_env,
    )
    return await register_mcp_tools(registry, server_config, overwrite=overwrite)


async def register_fetch_tools_from_json(
    registry: ToolRegistry,
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
    overwrite: bool = False,
) -> list[MCPToolAdapter]:
    server_config = build_fetch_server_config_from_json(
        config_path=config_path,
        env_path=env_path,
    )
    return await register_mcp_tools(registry, server_config, overwrite=overwrite)


async def register_fetch_tools_from_env(
    registry: ToolRegistry,
    *,
    env_path: str | Path | None = None,
    default_workspace: str | Path | None = None,
    overwrite: bool = False,
) -> list[MCPToolAdapter]:
    server_config = build_fetch_server_config_from_env(
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


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise MCPConfigError(f"Expected a boolean value, got: {value!r}")
