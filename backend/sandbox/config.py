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
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import Host, NetworkPolicy, NetworkRule, Volume

PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
SAFE_SESSION_SEGMENT_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


class SandboxConfigError(ValueError):
    """Raised when sandbox configuration cannot be resolved."""


@dataclass(slots=True)
class SandboxConnectionSettings:
    protocol: str = "http"
    domain: str = "127.0.0.1:8080"
    api_key_env: str | None = None
    request_timeout_seconds: float = 30.0
    debug: bool = False

    def resolve_api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        raw_value = os.getenv(self.api_key_env)
        if raw_value is None:
            return None
        resolved = raw_value.strip()
        return resolved or None

    def to_connection_config(self) -> ConnectionConfig:
        return ConnectionConfig(
            protocol=self.protocol,
            domain=self.domain,
            api_key=self.resolve_api_key(),
            request_timeout=_seconds_to_duration(self.request_timeout_seconds),
            debug=self.debug,
        )


@dataclass(slots=True)
class SandboxRuntimeSettings:
    image: str = "python:3.11-slim"
    workspace_mount_path: str = "/workspace"
    timeout_seconds: float = 1800.0
    ready_timeout_seconds: float = 60.0
    resource: dict[str, str] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    network_default_action: str = "deny"
    default_egress_targets: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SandboxServerSettings:
    config_output_path: str
    host: str = "127.0.0.1"
    port: int = 8080
    api_key_env: str | None = None
    max_sandbox_timeout_seconds: int | None = 86400
    runtime_type: str = "docker"
    execd_image: str = "opensandbox/execd:v1.0.10"
    egress_image: str = "opensandbox/egress:v1.0.6"
    egress_mode: str = "dns"
    allowed_host_paths: list[str] = field(default_factory=list)
    network_mode: str = "bridge"
    drop_capabilities: list[str] = field(default_factory=list)
    no_new_privileges: bool = True
    pids_limit: int | None = 256
    ingress_mode: str = "direct"
    log_level: str = "INFO"
    log_to_file: bool = True

    def resolve_api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        raw_value = os.getenv(self.api_key_env)
        if raw_value is None:
            return None
        resolved = raw_value.strip()
        return resolved or None

    @property
    def output_path(self) -> Path:
        return Path(self.config_output_path).resolve()


@dataclass(slots=True)
class SandboxConfig:
    provider: str
    workspace_root: str
    session_workspace_subdir: str
    connection: SandboxConnectionSettings
    runtime: SandboxRuntimeSettings
    server: SandboxServerSettings

    @property
    def workspace_root_path(self) -> Path:
        return Path(self.workspace_root).resolve()

    @property
    def session_workspace_root(self) -> Path:
        return (self.workspace_root_path / self.session_workspace_subdir).resolve()

    def ensure_workspace_root(self) -> Path:
        self.workspace_root_path.mkdir(parents=True, exist_ok=True)
        self.session_workspace_root.mkdir(parents=True, exist_ok=True)
        return self.workspace_root_path

    def ensure_session_workspace(self, session_id: str) -> Path:
        self.ensure_workspace_root()
        session_path = (self.session_workspace_root / self._sanitize_session_segment(session_id)).resolve()
        session_path.mkdir(parents=True, exist_ok=True)
        return session_path

    def build_workspace_volume(self, session_id: str, *, read_only: bool = False) -> Volume:
        session_workspace = self.ensure_session_workspace(session_id)
        return Volume(
            name=f"workspace-{self._sanitize_session_segment(session_id)}",
            host=Host(path=str(session_workspace)),
            mountPath=self.runtime.workspace_mount_path,
            readOnly=read_only,
        )

    def build_network_policy(
        self,
        *,
        enable_network: bool = False,
        egress_targets: list[str] | None = None,
    ) -> NetworkPolicy | None:
        targets = list(egress_targets or self.runtime.default_egress_targets)
        if enable_network and not targets:
            return NetworkPolicy(defaultAction="allow", egress=[])

        if not enable_network and not targets:
            return NetworkPolicy(defaultAction="deny", egress=[])

        return NetworkPolicy(
            defaultAction=self.runtime.network_default_action,
            egress=[NetworkRule(action="allow", target=target) for target in targets],
        )

    def render_server_toml(self) -> str:
        allowed_host_paths = list(dict.fromkeys([str(self.workspace_root_path), *self.server.allowed_host_paths]))
        lines = [
            "# Generated by backend/sandbox/config.py",
            "# Only host paths under agent_workspace are allowed for sandbox bind mounts.",
            "",
            "[server]",
            f"host = {json.dumps(self.server.host)}",
            f"port = {self.server.port}",
        ]
        resolved_api_key = self.server.resolve_api_key()
        if resolved_api_key:
            lines.append(f"api_key = {json.dumps(resolved_api_key)}")
        if self.server.max_sandbox_timeout_seconds is not None:
            lines.append(f"max_sandbox_timeout_seconds = {self.server.max_sandbox_timeout_seconds}")

        lines.extend(
            [
                "",
                "[log]",
                f"level = {json.dumps(self.server.log_level)}",
                f"file_enabled = {_toml_bool(self.server.log_to_file)}",
                "",
                "[runtime]",
                f"type = {json.dumps(self.server.runtime_type)}",
                f"execd_image = {json.dumps(self.server.execd_image)}",
                "",
                "[egress]",
                f"image = {json.dumps(self.server.egress_image)}",
                f"mode = {json.dumps(self.server.egress_mode)}",
                "",
                "[storage]",
                f"allowed_host_paths = {_toml_string_array(allowed_host_paths)}",
                "",
                "[docker]",
                f"network_mode = {json.dumps(self.server.network_mode)}",
                f"drop_capabilities = {_toml_string_array(self.server.drop_capabilities)}",
                f"no_new_privileges = {_toml_bool(self.server.no_new_privileges)}",
            ]
        )
        if self.server.pids_limit is not None:
            lines.append(f"pids_limit = {self.server.pids_limit}")

        lines.extend(
            [
                "",
                "[ingress]",
                f"mode = {json.dumps(self.server.ingress_mode)}",
                "",
            ]
        )
        return "\n".join(lines)

    def write_server_config(self) -> Path:
        output_path = self.server.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(self.render_server_toml(), encoding="utf-8")
        return output_path

    @staticmethod
    def _sanitize_session_segment(value: str) -> str:
        normalized = SAFE_SESSION_SEGMENT_PATTERN.sub("_", value.strip())
        normalized = normalized.strip("._-")
        return normalized or "default"


def load_sandbox_config(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> SandboxConfig:
    payload = _load_json_config(config_path or _default_config_path(), env_path=env_path)
    if not payload.get("enabled", True):
        raise SandboxConfigError("Sandbox is disabled in its JSON config.")

    connection_payload = _require_mapping(payload, "connection")
    runtime_payload = _require_mapping(payload, "runtime")
    server_payload = _require_mapping(payload, "server")

    workspace_root = str(payload.get("workspace_root") or "").strip()
    if not workspace_root:
        raise SandboxConfigError("Sandbox config requires 'workspace_root'.")

    return SandboxConfig(
        provider=str(payload.get("provider") or "opensandbox"),
        workspace_root=workspace_root,
        session_workspace_subdir=str(payload.get("session_workspace_subdir") or "sessions"),
        connection=SandboxConnectionSettings(
            protocol=str(connection_payload.get("protocol") or "http"),
            domain=str(connection_payload.get("domain") or "127.0.0.1:8080"),
            api_key_env=_optional_string(connection_payload.get("api_key_env")),
            request_timeout_seconds=_optional_float(connection_payload.get("request_timeout_seconds"), default=30.0) or 30.0,
            debug=bool(connection_payload.get("debug", False)),
        ),
        runtime=SandboxRuntimeSettings(
            image=str(runtime_payload.get("image") or "python:3.11-slim"),
            workspace_mount_path=str(runtime_payload.get("workspace_mount_path") or "/workspace"),
            timeout_seconds=_optional_float(runtime_payload.get("timeout_seconds"), default=1800.0) or 1800.0,
            ready_timeout_seconds=_optional_float(runtime_payload.get("ready_timeout_seconds"), default=60.0) or 60.0,
            resource={str(key): str(value) for key, value in _mapping_or_empty(runtime_payload.get("resource")).items()},
            environment={str(key): str(value) for key, value in _mapping_or_empty(runtime_payload.get("environment")).items()},
            network_default_action=str(runtime_payload.get("network_default_action") or "deny"),
            default_egress_targets=[str(item) for item in runtime_payload.get("default_egress_targets") or []],
        ),
        server=SandboxServerSettings(
            config_output_path=str(server_payload.get("config_output_path") or (Path.cwd() / "opensandbox.server.toml")),
            host=str(server_payload.get("host") or "127.0.0.1"),
            port=int(server_payload.get("port") or 8080),
            api_key_env=_optional_string(server_payload.get("api_key_env")),
            max_sandbox_timeout_seconds=_optional_int(server_payload.get("max_sandbox_timeout_seconds"), default=86400),
            runtime_type=str(server_payload.get("runtime_type") or "docker"),
            execd_image=str(server_payload.get("execd_image") or "opensandbox/execd:v1.0.10"),
            egress_image=str(server_payload.get("egress_image") or "opensandbox/egress:v1.0.6"),
            egress_mode=str(server_payload.get("egress_mode") or "dns"),
            allowed_host_paths=[str(item) for item in server_payload.get("allowed_host_paths") or []],
            network_mode=str(server_payload.get("network_mode") or "bridge"),
            drop_capabilities=[str(item) for item in server_payload.get("drop_capabilities") or []],
            no_new_privileges=bool(server_payload.get("no_new_privileges", True)),
            pids_limit=_optional_int(server_payload.get("pids_limit"), default=256),
            ingress_mode=str(server_payload.get("ingress_mode") or "direct"),
            log_level=str(server_payload.get("log_level") or "INFO"),
            log_to_file=bool(server_payload.get("log_to_file", True)),
        ),
    )


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "sandbox.json"


def _load_json_config(config_path: str | Path, *, env_path: str | Path | None = None) -> dict[str, Any]:
    resolved_env_path = _load_env_file(env_path)
    resolved_config_path = Path(config_path).resolve()
    if not resolved_config_path.exists():
        raise SandboxConfigError(f"Sandbox config file not found: {resolved_config_path}")

    with resolved_config_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    project_root = _infer_project_root(resolved_config_path, resolved_env_path)
    context = _build_template_context(project_root)
    return _resolve_template_variables(payload, context)


def _load_env_file(env_path: str | Path | None = None) -> Path | None:
    if env_path is None:
        resolved_env_path = _find_env_file()
    else:
        resolved_env_path = Path(env_path).resolve()
        if not resolved_env_path.exists():
            raise SandboxConfigError(f"Env file not found: {resolved_env_path}")

    if resolved_env_path is not None:
        load_dotenv(dotenv_path=resolved_env_path, override=False)

    return resolved_env_path


def _find_env_file(start: str | Path | None = None) -> Path | None:
    current_path = Path(start).resolve() if start is not None else Path(__file__).resolve()
    current_dir = current_path if current_path.is_dir() else current_path.parent

    for candidate_dir in (current_dir, *current_dir.parents):
        candidate = candidate_dir / ".env"
        if candidate.exists():
            return candidate

    return None


def _infer_project_root(config_path: Path, env_path: Path | None) -> Path:
    if env_path is not None:
        return env_path.parent.resolve()

    for candidate in (config_path.parent, *config_path.parents):
        if (candidate / "backend").is_dir():
            return candidate.resolve()

    return config_path.parent.resolve()


def _build_template_context(project_root: Path) -> dict[str, str]:
    context = {key: value for key, value in os.environ.items()}
    context.update(
        {
            "PROJECT_ROOT": str(project_root),
            "BACKEND_ROOT": str(project_root / "backend"),
            "PYTHON_EXECUTABLE": sys.executable,
            "NODE_EXECUTABLE": shutil.which("node") or "",
            "NPX_COMMAND": shutil.which("npx") or "npx",
        }
    )
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


def _require_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise SandboxConfigError(f"Sandbox config requires '{key}' to be an object.")
    return value


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SandboxConfigError("Expected a mapping for sandbox config.")
    return value


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _optional_float(value: Any, *, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SandboxConfigError(f"Expected a numeric sandbox config value, got: {value!r}") from exc


def _optional_int(value: Any, *, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SandboxConfigError(f"Expected an integer sandbox config value, got: {value!r}") from exc


def _seconds_to_duration(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_string_array(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(value) for value in values) + "]"
