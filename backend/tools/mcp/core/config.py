from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.client.stdio import StdioServerParameters

PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


class MCPConfigError(ValueError):
    """Raised when MCP server configuration cannot be resolved."""


@dataclass(slots=True)
class MCPServerConfig:
    server_name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    read_timeout_seconds: float | None = 60.0
    tool_namespace: str | None = None
    encoding: str = "utf-8"
    encoding_error_handler: str = "strict"

    @property
    def namespace(self) -> str:
        return self.tool_namespace or self.server_name

    def to_server_parameters(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env or None,
            cwd=self.cwd,
            encoding=self.encoding,
            encoding_error_handler=self.encoding_error_handler,
        )

    @classmethod
    def from_command(
        cls,
        *,
        server_name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        read_timeout_seconds: float | None = 60.0,
        tool_namespace: str | None = None,
    ) -> "MCPServerConfig":
        return cls(
            server_name=server_name,
            command=command,
            args=list(args or []),
            env=dict(env or {}),
            cwd=str(Path(cwd).resolve()) if cwd is not None else None,
            read_timeout_seconds=read_timeout_seconds,
            tool_namespace=tool_namespace,
        )


def find_env_file(start: str | Path | None = None) -> Path | None:
    current_path = Path(start).resolve() if start is not None else Path(__file__).resolve()
    current_dir = current_path if current_path.is_dir() else current_path.parent

    for candidate_dir in (current_dir, *current_dir.parents):
        candidate = candidate_dir / ".env"
        if candidate.exists():
            return candidate

    return None


def load_env_file(env_path: str | Path | None = None) -> Path | None:
    if env_path is None:
        resolved_env_path = find_env_file()
    else:
        resolved_env_path = Path(env_path).resolve()
        if not resolved_env_path.exists():
            raise MCPConfigError(f"Env file not found: {resolved_env_path}")

    if resolved_env_path is not None:
        load_dotenv(dotenv_path=resolved_env_path, override=False)

    return resolved_env_path


def load_json_config(
    config_path: str | Path,
    *,
    env_path: str | Path | None = None,
    extra_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    resolved_env_path = load_env_file(env_path)
    resolved_config_path = Path(config_path).resolve()
    if not resolved_config_path.exists():
        raise MCPConfigError(f"Config file not found: {resolved_config_path}")

    with resolved_config_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    project_root = _infer_project_root(resolved_config_path, resolved_env_path)
    context = _build_template_context(project_root, extra_context=extra_context)
    return _resolve_template_variables(payload, context)


def read_optional_string(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def read_optional_float(name: str, default: float | None = None) -> float | None:
    raw_value = read_optional_string(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise MCPConfigError(f"{name} must be a float, got: {raw_value}") from exc


def read_optional_bool(name: str, default: bool = False) -> bool:
    raw_value = read_optional_string(name)
    if raw_value is None:
        return default

    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise MCPConfigError(f"{name} must be a boolean, got: {raw_value}")


def read_optional_path_list(name: str) -> list[str]:
    raw_value = read_optional_string(name)
    if raw_value is None:
        return []
    return [part.strip() for part in raw_value.split(os.pathsep) if part.strip()]


def _infer_project_root(config_path: Path, env_path: Path | None) -> Path:
    if env_path is not None:
        return env_path.parent.resolve()

    for candidate in (config_path.parent, *config_path.parents):
        if (candidate / "backend").is_dir():
            return candidate.resolve()

    return config_path.parent.resolve()


def _build_template_context(
    project_root: Path,
    *,
    extra_context: dict[str, str] | None = None,
) -> dict[str, str]:
    context = {key: value for key, value in os.environ.items()}
    context.update(
        {
            "PROJECT_ROOT": str(project_root),
            "BACKEND_ROOT": str(project_root / "backend"),
            "PYTHON_EXECUTABLE": sys.executable,
            "NODE_EXECUTABLE": shutil.which("node") or "",
            "NPX_COMMAND": shutil.which("npx") or "npx",
            "UVX_COMMAND": shutil.which("uvx") or "uvx",
        }
    )
    if extra_context:
        context.update(extra_context)
    return context


def _resolve_template_variables(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, str):
        return PLACEHOLDER_PATTERN.sub(lambda match: context.get(match.group(1), ""), value)
    if isinstance(value, list):
        return [_resolve_template_variables(item, context) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_template_variables(item, context)
            for key, item in value.items()
        }
    return value
