from __future__ import annotations

import shlex
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from opensandbox import Sandbox
from opensandbox.models.execd import RunCommandOpts

from .config import SandboxConfig


class OpenSandboxManager:
    """Thin project-level wrapper around the OpenSandbox SDK."""

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    @property
    def workspace_root(self) -> Path:
        return self.config.workspace_root_path

    @property
    def server_config_path(self) -> Path:
        return self.config.server.output_path

    def prepare(self) -> Path:
        self.config.ensure_workspace_root()
        return self.config.write_server_config()

    def ensure_session_workspace(self, session_id: str) -> Path:
        return self.config.ensure_session_workspace(session_id)

    def describe_session_binding(self, session_id: str) -> dict[str, Any]:
        session_workspace = self.ensure_session_workspace(session_id)
        return {
            "session_id": session_id,
            "provider": self.config.provider,
            "workspace_host_path": str(session_workspace),
            "workspace_mount_path": self.config.runtime.workspace_mount_path,
            "network_default_action": self.config.runtime.network_default_action,
        }

    def build_connection_config(self):
        return self.config.connection.to_connection_config()

    def get_runtime_status(self) -> dict[str, Any]:
        docker_binary = shutil.which("docker")
        docker_status = self.check_docker_daemon()
        return {
            "provider": self.config.provider,
            "workspace_root": str(self.workspace_root),
            "session_workspace_root": str(self.config.session_workspace_root),
            "workspace_mount_path": self.config.runtime.workspace_mount_path,
            "sandbox_image": self.config.runtime.image,
            "server_domain": f"{self.config.connection.protocol}://{self.config.connection.domain}",
            "server_config_path": str(self.server_config_path),
            "server_config_exists": self.server_config_path.exists(),
            "api_key_configured": bool(self.config.connection.resolve_api_key()),
            "docker_binary": docker_binary,
            "docker_daemon": docker_status,
            "allowed_host_paths": list(dict.fromkeys([str(self.workspace_root), *self.config.server.allowed_host_paths])),
        }

    def check_docker_daemon(self) -> dict[str, Any]:
        docker_binary = shutil.which("docker")
        if not docker_binary:
            return {
                "available": False,
                "reachable": False,
                "error": "docker executable not found on PATH",
            }

        try:
            completed = subprocess.run(
                [docker_binary, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:
            return {
                "available": True,
                "reachable": False,
                "error": str(exc),
            }

        if completed.returncode != 0:
            error_text = (completed.stderr or completed.stdout or "").strip()
            return {
                "available": True,
                "reachable": False,
                "error": error_text or "docker daemon is not reachable",
            }

        return {
            "available": True,
            "reachable": True,
            "server_version": completed.stdout.strip(),
        }

    async def get_server_health(self) -> dict[str, Any]:
        base_url = f"{self.config.connection.protocol}://{self.config.connection.domain}"
        timeout = self.config.connection.request_timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{base_url}/health")
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text}
        return {
            "status_code": response.status_code,
            "ok": response.is_success,
            "payload": payload,
        }

    async def create_session_sandbox(
        self,
        session_id: str,
        *,
        read_only_workspace: bool = False,
        enable_network: bool = False,
        egress_targets: list[str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Sandbox:
        session_workspace = self.ensure_session_workspace(session_id)
        sandbox_metadata = {
            "session_id": session_id,
            "workspace_name": session_workspace.name,
        }
        sandbox_metadata.update(metadata or {})

        return await Sandbox.create(
            self.config.runtime.image,
            timeout=timedelta(seconds=self.config.runtime.timeout_seconds),
            ready_timeout=timedelta(seconds=self.config.runtime.ready_timeout_seconds),
            env=dict(self.config.runtime.environment),
            metadata=sandbox_metadata,
            resource=dict(self.config.runtime.resource),
            network_policy=self.config.build_network_policy(
                enable_network=enable_network,
                egress_targets=egress_targets,
            ),
            volumes=[self.config.build_workspace_volume(session_id, read_only=read_only_workspace)],
            connection_config=self.build_connection_config(),
        )

    def resolve_session_path(self, session_id: str, raw_path: str | Path | None) -> Path:
        session_workspace = self.ensure_session_workspace(session_id)
        if raw_path in (None, "", "."):
            return session_workspace
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = session_workspace / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(session_workspace)
        except ValueError as exc:
            raise ValueError(
                f"Path '{resolved}' is outside the current session workspace '{session_workspace}'."
            ) from exc
        return resolved

    def session_path_to_mount_path(self, session_id: str, raw_path: str | Path | None) -> str:
        session_workspace = self.ensure_session_workspace(session_id)
        resolved = self.resolve_session_path(session_id, raw_path)
        if resolved == session_workspace:
            return self.config.runtime.workspace_mount_path
        relative = resolved.relative_to(session_workspace).as_posix()
        return f"{self.config.runtime.workspace_mount_path}/{relative}"

    async def run_read_only_command(
        self,
        session_id: str,
        *,
        command: str,
        working_directory: str | None = None,
        timeout_seconds: float = 120.0,
        enable_network: bool = False,
        egress_targets: list[str] | None = None,
        envs: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        sandbox = await self.create_session_sandbox(
            session_id,
            read_only_workspace=True,
            enable_network=enable_network,
            egress_targets=egress_targets,
            metadata=metadata,
        )
        try:
            execution = await sandbox.commands.run(
                command,
                opts=RunCommandOpts(
                    working_directory=working_directory or self.config.runtime.workspace_mount_path,
                    timeout=timedelta(seconds=timeout_seconds),
                    envs=dict(envs or {}),
                ),
            )
            return {
                "sandbox_id": sandbox.id,
                "session_binding": self.describe_session_binding(session_id),
                "command": command,
                "working_directory": working_directory or self.config.runtime.workspace_mount_path,
                "exit_code": execution.exit_code,
                "stdout": [item.text for item in execution.logs.stdout],
                "stderr": [item.text for item in execution.logs.stderr],
                "result": [item.text for item in execution.result if item.text],
            }
        finally:
            await sandbox.close()

    async def run_python_file_read_only(
        self,
        session_id: str,
        *,
        path: str | Path,
        arguments: list[str] | None = None,
        timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        target_path = self.resolve_session_path(session_id, path)
        if target_path.suffix.lower() != ".py":
            raise ValueError("terminal_run_python_file only supports .py files.")
        if not target_path.is_file():
            raise ValueError(f"Python file not found: {target_path}")

        sandbox_file = self.session_path_to_mount_path(session_id, target_path)
        command_parts = ["python", sandbox_file, *(arguments or [])]
        command = " ".join(shlex.quote(part) for part in command_parts)
        return await self.run_read_only_command(
            session_id,
            command=command,
            working_directory=self.config.runtime.workspace_mount_path,
            timeout_seconds=timeout_seconds,
            envs={
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": "/tmp/pycache",
            },
            metadata={"tool_mode": "python_file"},
        )

    async def run_unittest_read_only(
        self,
        session_id: str,
        *,
        start_directory: str | Path | None = None,
        pattern: str = "test*.py",
        timeout_seconds: float = 180.0,
    ) -> dict[str, Any]:
        sandbox_target = self.session_path_to_mount_path(session_id, start_directory)
        command_parts = [
            "python",
            "-m",
            "unittest",
            "discover",
            "-s",
            sandbox_target,
            "-p",
            pattern,
        ]
        command = " ".join(shlex.quote(part) for part in command_parts)
        return await self.run_read_only_command(
            session_id,
            command=command,
            working_directory=self.config.runtime.workspace_mount_path,
            timeout_seconds=timeout_seconds,
            envs={
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": "/tmp/pycache",
            },
            metadata={"tool_mode": "unittest"},
        )

    async def run_smoke_command(
        self,
        session_id: str,
        *,
        command: str | None = None,
    ) -> dict[str, Any]:
        session_workspace = self.ensure_session_workspace(session_id)
        marker_file = session_workspace / "sandbox_probe.txt"
        marker_file.write_text("sandbox probe marker\n", encoding="utf-8")

        sandbox = await self.create_session_sandbox(session_id)
        try:
            execution = await sandbox.commands.run(
                command or "python -c \"import os; print(os.getcwd()); print(sorted(os.listdir()))\"",
                opts=RunCommandOpts(
                    working_directory=self.config.runtime.workspace_mount_path,
                    timeout=timedelta(seconds=30),
                ),
            )
            return {
                "sandbox_id": sandbox.id,
                "session_workspace": str(session_workspace),
                "command": command,
                "exit_code": execution.exit_code,
                "stdout": [item.text for item in execution.logs.stdout],
                "stderr": [item.text for item in execution.logs.stderr],
                "result": [item.text for item in execution.result if item.text],
            }
        finally:
            await sandbox.close()
