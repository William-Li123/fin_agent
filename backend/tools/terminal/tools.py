from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from backend.sandbox import OpenSandboxManager, build_sandbox_manager

from ..base import BaseTool
from ..registry import ToolRegistry
from ..schemas import ToolContentItem, ToolDefinition, ToolResult, ToolSource


class _TerminalBaseTool(BaseTool):
    def __init__(self, definition: ToolDefinition, sandbox_manager: OpenSandboxManager) -> None:
        super().__init__(definition)
        self._sandbox_manager = sandbox_manager

    async def ainvoke(self, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        normalized_arguments = dict(arguments or {})
        try:
            return await self._invoke_async(normalized_arguments)
        except Exception as exc:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=str(exc),
                content=[ToolContentItem(text=f"{self.name} failed: {exc}")],
                metadata={"tool_name": self.name},
            )

    async def _invoke_async(self, arguments: dict[str, Any]) -> ToolResult:
        raise NotImplementedError


class TerminalGetSessionStatusTool(_TerminalBaseTool):
    async def _invoke_async(self, arguments: dict[str, Any]) -> ToolResult:
        session_id = _require_session_id(arguments)
        binding = self._sandbox_manager.describe_session_binding(session_id)
        runtime_status = self._sandbox_manager.get_runtime_status()
        text = (
            f"session_id={binding['session_id']}, "
            f"workspace_host_path={binding['workspace_host_path']}, "
            f"workspace_mount_path={binding['workspace_mount_path']}, "
            f"docker_reachable={runtime_status['docker_daemon'].get('reachable', False)}"
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=text)],
            structured_content={
                "session_binding": binding,
                "runtime_status": runtime_status,
            },
            metadata={"session_id": session_id},
        )


class TerminalRunPythonFileTool(_TerminalBaseTool):
    async def _invoke_async(self, arguments: dict[str, Any]) -> ToolResult:
        session_id = _require_session_id(arguments)
        path = _require_string(arguments, "path")
        file_arguments = _normalize_string_list(arguments.get("arguments"))
        timeout_seconds = _optional_float(arguments.get("timeout_seconds"), default=120.0) or 120.0

        result = await self._sandbox_manager.run_python_file_read_only(
            session_id,
            path=path,
            arguments=file_arguments,
            timeout_seconds=timeout_seconds,
        )
        return _command_result_to_tool_result(self.name, result, success=(result.get("exit_code") == 0))


class TerminalRunUnittestTool(_TerminalBaseTool):
    async def _invoke_async(self, arguments: dict[str, Any]) -> ToolResult:
        session_id = _require_session_id(arguments)
        start_directory = arguments.get("start_directory")
        pattern = _optional_string(arguments.get("pattern")) or "test*.py"
        timeout_seconds = _optional_float(arguments.get("timeout_seconds"), default=180.0) or 180.0

        result = await self._sandbox_manager.run_unittest_read_only(
            session_id,
            start_directory=start_directory,
            pattern=pattern,
            timeout_seconds=timeout_seconds,
        )
        return _command_result_to_tool_result(self.name, result, success=(result.get("exit_code") == 0))


def build_terminal_tools(
    *,
    sandbox_manager: OpenSandboxManager | None = None,
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> list[BaseTool]:
    manager = sandbox_manager or build_sandbox_manager(config_path=config_path, env_path=env_path)
    manager.prepare()

    return [
        TerminalGetSessionStatusTool(
            ToolDefinition(
                name="terminal_get_session_status",
                description=(
                    "Inspect the sandbox binding for the current session. "
                    "Use this to confirm which session workspace is mounted into the sandbox and whether Docker is reachable. "
                    "This tool does not execute user code."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "The current agent session id. Always pass the current_session_id from the agent context.",
                        }
                    },
                    "required": ["session_id"],
                },
                source=ToolSource.LOCAL,
            ),
            manager,
        ),
        TerminalRunPythonFileTool(
            ToolDefinition(
                name="terminal_run_python_file",
                description=(
                    "Execute an existing Python file inside the current session sandbox. "
                    "The mounted session workspace is read-only, so this tool is for running code that already exists under the session workspace. "
                    "Use filesystem tools first if you need to create or edit files, then use this tool to execute the Python file and inspect stdout, stderr, and the exit code."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "The current agent session id. Always pass the current_session_id from the agent context.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Path to an existing .py file inside the current session workspace. Relative paths are resolved from the session workspace root.",
                        },
                        "arguments": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional positional arguments passed to the Python file.",
                        },
                        "timeout_seconds": {
                            "type": "number",
                            "minimum": 1,
                            "description": "Optional execution timeout in seconds.",
                        },
                    },
                    "required": ["session_id", "path"],
                },
                source=ToolSource.LOCAL,
            ),
            manager,
        ),
        TerminalRunUnittestTool(
            ToolDefinition(
                name="terminal_run_python_unittest",
                description=(
                    "Run Python unittest discovery inside the current session sandbox against a start directory under the current session workspace. "
                    "The session workspace is mounted read-only. Bytecode is redirected to /tmp so the shared workspace is not modified. "
                    "Use this tool to validate code, collect test failures, and return stdout, stderr, and the exit code without mutating the shared workspace."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "The current agent session id. Always pass the current_session_id from the agent context.",
                        },
                        "start_directory": {
                            "type": "string",
                            "description": "Optional directory path inside the current session workspace used as unittest discover -s. Defaults to the session workspace root.",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Optional unittest discovery filename pattern. Defaults to test*.py.",
                        },
                        "timeout_seconds": {
                            "type": "number",
                            "minimum": 1,
                            "description": "Optional execution timeout in seconds.",
                        },
                    },
                    "required": ["session_id"],
                },
                source=ToolSource.LOCAL,
            ),
            manager,
        ),
    ]


def register_terminal_tools(
    registry: ToolRegistry,
    *,
    sandbox_manager: OpenSandboxManager | None = None,
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
    overwrite: bool = False,
) -> list[BaseTool]:
    tools = build_terminal_tools(
        sandbox_manager=sandbox_manager,
        config_path=config_path,
        env_path=env_path,
    )
    registry.register_many(tools, overwrite=overwrite)
    return tools


def _command_result_to_tool_result(tool_name: str, result: dict[str, Any], *, success: bool) -> ToolResult:
    stdout = "\n".join(line for line in result.get("stdout", []) if line).strip()
    stderr = "\n".join(line for line in result.get("stderr", []) if line).strip()
    parts = [
        f"exit_code={result.get('exit_code')}",
        f"working_directory={result.get('working_directory')}",
    ]
    if stdout:
        parts.append("stdout:\n" + stdout)
    if stderr:
        parts.append("stderr:\n" + stderr)
    if not stdout and not stderr:
        parts.append("(no stdout/stderr output)")

    return ToolResult(
        tool_name=tool_name,
        success=success,
        content=[ToolContentItem(text="\n\n".join(parts))],
        structured_content=result,
        error=None if success else (stderr or f"{tool_name} exited with code {result.get('exit_code')}"),
        metadata={
            "session_id": result.get("session_binding", {}).get("session_id"),
            "sandbox_id": result.get("sandbox_id"),
            "exit_code": result.get("exit_code"),
        },
    )


def _require_session_id(arguments: Mapping[str, Any]) -> str:
    session_id = str(arguments.get("session_id", "")).strip()
    if not session_id:
        raise ValueError("Missing required string argument: session_id")
    return session_id


def _require_string(arguments: Mapping[str, Any], key: str) -> str:
    value = str(arguments.get(key, "")).strip()
    if not value:
        raise ValueError(f"Missing required string argument: {key}")
    return value


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Expected a list of strings.")
    return [str(item) for item in value]


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _optional_float(value: Any, *, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    return float(value)
