"""Real Python/shell terminal with persistent files, owned jobs and scoped RPC.

Docker is the default, fails closed, and never falls back to host execution.
``trusted-local`` is an explicit opt-in to unrestricted execution as the current
user. Its path checks and cleaned environment are hygiene, not a sandbox.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

import anyio

from .contracts import BaseTool, ToolContentItem, ToolDefinition, ToolResult
from .audit import redact
from .sdk import FinanceSDK, FinanceSDKError, SDKCallBudget, _json_copy
from .store import FinanceStore
from .terminal_process import ProcessTree
from .tools import _validate


@dataclass(frozen=True)
class TerminalConfig:
    backend: str = "docker"
    docker_image: str = "finance-agent-terminal:local"
    docker_executable: str = "docker"
    timeout_seconds: float = 30
    max_timeout_seconds: float = 120
    max_output_chars: int = 12_000
    max_file_bytes: int = 1_000_000
    max_log_bytes: int = 20_000_000
    max_jobs: int = 4
    max_sdk_calls: int = 32
    max_total_sdk_calls: int = 64
    memory: str = "512m"
    cpus: float = 1.0
    pids_limit: int = 64
    allow_network: bool = False

    def __post_init__(self) -> None:
        if self.backend not in {"docker", "trusted-local"}:
            raise ValueError("backend must be docker or trusted-local")
        for name in ("timeout_seconds", "max_timeout_seconds", "cpus"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.timeout_seconds > self.max_timeout_seconds:
            raise ValueError("timeout_seconds exceeds max_timeout_seconds")
        for name in ("max_output_chars", "max_file_bytes", "max_log_bytes", "max_jobs",
                     "max_sdk_calls", "max_total_sdk_calls", "pids_limit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.allow_network, bool):
            raise ValueError("allow_network must be boolean")
        if not re.fullmatch(r"[1-9][0-9]*[kKmMgG]?", self.memory):
            raise ValueError("memory must be a positive Docker memory limit")
        if not isinstance(self.docker_image, str) or not self.docker_image or self.docker_image.startswith("-"):
            raise ValueError("A Docker image name is required")


def _save_json(path: Path, payload: dict) -> None:
    if path.name == "receipt.json":
        payload = redact(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _clean_environment() -> dict[str, str]:
    # An allowlist avoids leaking arbitrary vendor keys/tokens from the parent.
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC",
               "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    return env


def _excerpt(path: Path, limit: int) -> tuple[str, bool, int]:
    if not path.exists():
        return "", False, 0
    size = path.stat().st_size
    with path.open("rb") as handle:
        beginning = handle.read(limit * 4)
        text = beginning.decode("utf-8", errors="replace")
        if size <= len(beginning) and len(text) <= limit:
            return text, False, size
        marker = "\n... output truncated; full log saved ...\n"
        keep = max(1, (limit - len(marker)) // 2)
        handle.seek(max(0, size - keep * 4))
        ending = handle.read(keep * 4).decode("utf-8", errors="replace")
    return (text[:keep] + marker + ending[-keep:])[:limit], True, size


@dataclass
class _Job:
    run_id: str
    action: str
    code: str | None
    command: str | None
    started: float
    timeout: float
    directory: Path
    rpc_dir: Path
    sdk: FinanceSDK
    tree: ProcessTree | None = None
    container_name: str | None = None
    status: str = "running"
    error: str | None = None
    rpc_operations: int = 0
    rpc_summaries: list[dict] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    thread: threading.Thread | None = None
    broker_thread: threading.Thread | None = None
    exit_code: int | None = None
    finished: float | None = None


class TerminalActionError(ValueError):
    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


class FinanceTerminal:
    def __init__(self, store: FinanceStore, workspace: Path, output_dir: Path,
                 config: TerminalConfig | None = None,
                 extra_rpc: Callable[[dict], dict] | None = None) -> None:
        self.store = store
        original = Path(workspace).absolute()
        if any(part.is_symlink() for part in (original, *original.parents)):
            raise ValueError("Workspace cannot contain symlink components")
        self.workspace = original.resolve()
        self.output_dir = Path(output_dir).resolve()
        if self.output_dir == self.workspace or self.workspace in self.output_dir.parents:
            raise ValueError("Host receipts/output_dir must remain outside terminal workspace")
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or TerminalConfig()
        self.extra_rpc = extra_rpc
        self.session_id = "terminal_" + uuid4().hex
        self.shared_budget = SDKCallBudget(self.config.max_total_sdk_calls)
        self.observed_evidence_ids: set[str] = set()
        self.inputs_dir = self.output_dir / "terminal_inputs" / self.session_id
        self.inputs_dir.mkdir(parents=True)
        # Export only sanitized, scoped documents. Never mount the whole dataset
        # root, raw sources, benchmark gold, or arbitrary host input parents.
        for document in self.store.list_documents():
            identifier = document["document_id"]
            name = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:24] + ".json"
            _save_json(self.inputs_dir / name, self.store.get_document(identifier))
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _path(self, supplied: str, *, allow_root: bool = False) -> Path:
        if not isinstance(supplied, str) or not supplied or "\x00" in supplied:
            raise ValueError("path must be a nonempty relative workspace path")
        relative = Path(supplied)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise ValueError("path must stay inside the session workspace")
        candidate = self.workspace / relative
        for part in (candidate, *candidate.parents):
            if part == self.workspace:
                break
            if part.is_symlink():
                raise ValueError("Symlink file access is not permitted")
        resolved = candidate.resolve()
        if resolved == self.workspace and not allow_root:
            raise ValueError("A file path is required")
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise ValueError("path escapes workspace")
        if relative.parts and relative.parts[0] == ".finance-runtime":
            raise ValueError("Runtime files are managed internally")
        return resolved

    def _timeout(self, value: Any) -> float:
        timeout = self.config.timeout_seconds if value is None else value
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout):
            raise ValueError("timeout_seconds must be a finite number")
        if not 0 < timeout <= self.config.max_timeout_seconds:
            raise ValueError("timeout_seconds exceeds the configured range")
        return float(timeout)

    def _docker_ready(self) -> tuple[bool, str | None]:
        binary = shutil.which(self.config.docker_executable)
        if binary is None:
            return False, "Docker executable is unavailable; no host fallback is permitted"
        try:
            probe = subprocess.run([binary, "info", "--format", "{{.ServerVersion}}"],
                                   env=_clean_environment(), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if probe.returncode:
                return False, "Docker daemon is unavailable; no host fallback is permitted"
        except (OSError, subprocess.TimeoutExpired):
            return False, "Docker daemon probe failed; no host fallback is permitted"
        return True, None

    def status(self) -> dict[str, Any]:
        ready, error = (True, None) if self.config.backend == "trusted-local" else self._docker_ready()
        return {"status": "closed" if self._closed else "completed", "session_id": self.session_id,
                "backend": self.config.backend, "backend_ready": ready, "backend_error": error,
                "security_boundary": "docker_container" if self.config.backend == "docker" else "none_trusted_host_execution",
                "workspace": str(self.workspace), "inputs_dir": "/inputs" if self.config.backend == "docker" else str(self.inputs_dir),
                "agent_sdk_calls": self.shared_budget.calls,
                "max_total_sdk_calls": self.shared_budget.max_calls,
                "jobs": [{"job_id": job.run_id, "status": job.status, "exit_code": job.exit_code}
                         for job in self._jobs.values()]}

    def execute(self, action: str, **arguments: Any) -> dict[str, Any]:
        if action not in {"write_file", "read_file", "list_files", "read_record"}:
            return self._execute(action, **arguments)
        run_id = "run_" + uuid4().hex
        record_path = self.output_dir / "terminal_runs" / run_id / "receipt.json"
        started = time.monotonic()
        caught = None
        try:
            result = self._execute(action, **arguments)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            result, caught = {"status": "error", "error": str(exc)}, exc
        result.update(run_id=run_id, session_id=self.session_id, backend=self.config.backend,
                      record_path=str(record_path), elapsed_seconds=round(time.monotonic() - started, 6),
                      agent_sdk_calls=self.shared_budget.calls,
                      max_total_sdk_calls=self.config.max_total_sdk_calls)
        description = {key: value for key, value in arguments.items() if key != "content"}
        content = arguments.get("content")
        if isinstance(content, str):
            description.update(content_chars=len(content), content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest())
        _save_json(record_path, {"timestamp": datetime.now(UTC).isoformat(), "action": action,
                               "arguments": description, "limits": asdict(self.config), **result})
        if caught is not None:
            raise TerminalActionError(str(caught), result) from caught
        return result

    def _execute(self, action: str, **arguments: Any) -> dict[str, Any]:
        if self._closed:
            raise ValueError("Terminal session is closed")
        if action == "status":
            return self.status()
        if action == "read_record":
            if self.extra_rpc is None:
                raise ValueError("No host archive reader is configured")
            offset, limit = arguments.get("offset", 0), arguments.get("limit", 12000)
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12000:
                raise ValueError("offset must be nonnegative and limit must be 1..12000")
            supplied_path = arguments.get("path")
            if not isinstance(supplied_path, str) or not supplied_path:
                raise ValueError("A host record path is required")
            self.shared_budget.admit()
            result = _json_copy(self.extra_rpc({"kind": "record", "path": supplied_path, "offset": offset, "limit": limit}), max_bytes=512_000)
            if not isinstance(result, dict):
                raise ValueError("Host archive reader must return a JSON object")
            return {**result, "status": "completed"}
        if action == "write_file":
            path = self._path(arguments.get("path"))
            content = arguments.get("content")
            if not isinstance(content, str) or len(content.encode("utf-8")) > self.config.max_file_bytes:
                raise ValueError("content must be text within max_file_bytes")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"status": "completed", "path": str(path), "relative_path": path.relative_to(self.workspace).as_posix(),
                    "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if action == "read_file":
            path = self._path(arguments.get("path"))
            if path.stat().st_size > self.config.max_file_bytes:
                raise ValueError("File exceeds max_file_bytes; inspect using a script")
            content = path.read_text(encoding="utf-8")
            text = content[:self.config.max_output_chars]
            return {"status": "completed", "path": str(path), "content": text,
                    "truncated": len(content) > len(text), "bytes": path.stat().st_size}
        if action == "list_files":
            path = self._path(arguments.get("path", "."), allow_root=True)
            items = []
            for item in sorted(path.iterdir(), key=lambda entry: entry.name):
                if item.name == ".finance-runtime":
                    continue
                items.append({"name": item.name, "type": "symlink" if item.is_symlink() else "directory" if item.is_dir() else "file",
                              "bytes": None if item.is_symlink() or item.is_dir() else item.stat().st_size})
                if len(items) == 200:
                    break
            return {"status": "completed", "path": str(path), "files": items, "limit": 200}
        if action in {"python", "shell", "start_job"}:
            job = self._start(action, arguments)
            if action == "start_job":
                return self._result(job)
            job.done.wait(job.timeout + 15)
            if not job.done.is_set():
                self._terminate(job, "error", "Execution supervisor failed to finalize on time")
            return self._result(job)
        if action in {"poll_job", "terminate_job"}:
            identifier = arguments.get("job_id")
            if identifier not in self._jobs:
                raise ValueError("Unknown job_id for this terminal session")
            job = self._jobs[identifier]
            if action == "terminate_job":
                self._terminate(job, "terminated", "Job terminated by caller")
            return self._result(job)
        raise ValueError("Unknown terminal action")

    def _start(self, action: str, arguments: dict) -> _Job:
        code, command = arguments.get("code"), arguments.get("command")
        if action == "python" and command is not None or action == "shell" and code is not None:
            raise ValueError("python accepts code; shell accepts command")
        if (code is None) == (command is None):
            raise ValueError("Supply exactly one of code or command")
        text = code if code is not None else command
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > self.config.max_file_bytes:
            raise ValueError("code/command must be nonempty text within max_file_bytes")
        timeout = self._timeout(arguments.get("timeout_seconds"))
        with self._lock:
            if sum(not job.done.is_set() for job in self._jobs.values()) >= self.config.max_jobs:
                raise ValueError("Concurrent job limit reached")
            run_id = "run_" + uuid4().hex
            directory = self.output_dir / "terminal_runs" / run_id
            directory.mkdir(parents=True)
            internal = self.workspace / ".finance-runtime" / run_id
            if not self._safe_internal_path(internal):
                raise ValueError("Runtime directory has been redirected outside its permitted workspace")
            internal.mkdir(parents=True)
            rpc_dir = internal / "rpc"
            rpc_dir.mkdir()
            client = Path(__file__).parent / "terminal_worker" / "finance_runtime_sdk.py"
            shutil.copyfile(client, internal / "finance_runtime_sdk.py")
            shutil.copyfile(client.with_name("launch.py"), internal / "launch.py")
            script = internal / ("code.py" if code is not None else "command.ps1" if os.name == "nt" and self.config.backend == "trusted-local" else "command.sh")
            if script.suffix == ".ps1":
                script.write_text("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()\n" + text, encoding="utf-8-sig")
            else:
                script.write_text(text, encoding="utf-8")
            sdk = FinanceSDK(self.store, trace_path=directory / "sdk_trace.jsonl", max_calls=self.config.max_sdk_calls,
                             execution_id=run_id, shared_budget=self.shared_budget,
                             initial_evidence_ids=self.observed_evidence_ids)
            job = _Job(run_id, action, code, command, time.monotonic(), timeout, directory, rpc_dir, sdk)
            self._jobs[run_id] = job
            try:
                argv, env = self._command(job, script, internal)
                stdout = (directory / "stdout.log").open("wb")
                try:
                    stderr = (directory / "stderr.log").open("wb")
                    try:
                        job.tree = ProcessTree(argv, cwd=str(self.workspace), env=env, stdin=subprocess.DEVNULL,
                                               stdout=stdout, stderr=stderr)
                    finally:
                        stderr.close()
                finally:
                    stdout.close()
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                job.status, job.error = "error", str(exc)
                job.finished = time.monotonic()
                job.done.set()
                self._persist(job)
                return job
            self._persist(job)
            job.broker_thread = threading.Thread(target=self._broker, args=(job,), daemon=True, name=run_id + "_rpc")
            job.thread = threading.Thread(target=self._watch, args=(job,), daemon=True, name=run_id + "_watch")
            job.broker_thread.start()
            job.thread.start()
            return job

    def _command(self, job: _Job, script: Path, internal: Path) -> tuple[list[str], dict[str, str]]:
        env = _clean_environment()
        if self.config.backend == "trusted-local":
            env.update(FINANCE_RPC_DIR=str(job.rpc_dir), FINANCE_INPUTS_DIR=str(self.inputs_dir),
                       FINANCE_RPC_TIMEOUT=str(job.timeout), FINANCE_EMIT_PATH=str(internal / "emits.jsonl"),
                       PYTHONPATH=str(internal))
            if job.code is not None:
                return [sys.executable, "-u", str(internal / "launch.py"), str(script)], env
            if os.name == "nt":
                shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
                if not shell:
                    raise ValueError("PowerShell executable is unavailable")
                return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)], env
            return ["/bin/sh", str(script)], env
        ready, error = self._docker_ready()
        if not ready:
            raise ValueError(error)
        job.container_name = "finance-" + job.run_id.replace("_", "-")
        relative = internal.relative_to(self.workspace).as_posix()
        inside = "/workspace/" + relative
        command = [self.config.docker_executable, "run", "--rm", "--pull", "never", "--name", job.container_name,
                   "--network", "bridge" if self.config.allow_network else "none",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(self.config.pids_limit),
                   "--memory", self.config.memory, "--cpus", str(self.config.cpus), "--read-only",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--workdir", "/workspace",
                   "--mount", f"type=bind,source={self.workspace},target=/workspace",
                   "--mount", f"type=bind,source={self.inputs_dir},target=/inputs,readonly",
                   "--env", "PYTHONUNBUFFERED=1", "--env", "PYTHONUTF8=1",
                   "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=" + inside,
                   "--env", "FINANCE_RPC_DIR=" + inside + "/rpc", "--env", "FINANCE_INPUTS_DIR=/inputs",
                   "--env", "FINANCE_EMIT_PATH=" + inside + "/emits.jsonl",
                   "--env", "FINANCE_RPC_TIMEOUT=" + str(job.timeout), self.config.docker_image]
        command.extend(["python", "-u", inside + "/launch.py", inside + "/" + script.name] if job.code is not None else ["/bin/sh", inside + "/" + script.name])
        return command, env

    def _safe_rpc_file(self, path: Path, job: _Job) -> bool:
        return self._safe_internal_path(path) and path.resolve().parent == job.rpc_dir.resolve()

    def _safe_internal_path(self, path: Path) -> bool:
        # Check the entire chain. Comparing two resolved paths alone would let
        # both sides follow a worker-created parent symlink outside workspace.
        for part in (path, *path.parents):
            if part.is_symlink():
                return False
            if part == self.workspace:
                resolved = path.resolve()
                return resolved != self.workspace and self.workspace in resolved.parents
        return False

    def _broker(self, job: _Job) -> None:
        while not job.stop.is_set():
            for path in sorted(job.rpc_dir.glob("request_*.json")):
                if job.stop.is_set():
                    break
                if not re.fullmatch(r"request_[0-9a-f]{32}\.json", path.name) or not self._safe_rpc_file(path, job):
                    continue
                response = path.with_name(path.name.replace("request_", "response_", 1))
                request = None
                rpc_started = time.monotonic()
                try:
                    if path.stat().st_size > 512_000:
                        raise ValueError("RPC request exceeds permitted size")
                    request = _json_copy(json.loads(path.read_text(encoding="utf-8")), max_bytes=512_000)
                    if not isinstance(request, dict) or request.get("request_id") != path.stem.removeprefix("request_"):
                        raise ValueError("Invalid RPC request identity")
                    if set(request).difference({"request_id", "kind", "name", "arguments", "server", "tool", "operation", "path", "offset", "limit"}):
                        raise ValueError("Unknown RPC request fields")
                    if job.rpc_operations >= self.config.max_sdk_calls:
                        raise FinanceSDKError("Execution business operation limit reached")
                    kind = request.get("kind")
                    if kind == "finance":
                        before = job.sdk.calls
                        try:
                            result = job.sdk.call(request.get("name"), request.get("arguments", {}))
                        finally:
                            job.rpc_operations += job.sdk.calls - before
                    elif kind in {"mcp", "record"}:
                        if self.extra_rpc is None:
                            raise ValueError("No MCP connectors are configured")
                        self.shared_budget.admit()
                        job.rpc_operations += 1
                        result = _json_copy(self.extra_rpc(request), max_bytes=512_000)
                    else:
                        raise ValueError("Unknown RPC capability kind")
                    payload = {"ok": True, "result": result}
                    job.rpc_summaries.append({"kind": kind, "name": request.get("name", request.get("tool")), "success": True})
                except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
                    payload = {"ok": False, "error": str(exc)}
                    job.rpc_summaries.append({"success": False, "error": str(exc)})
                try:
                    with (job.directory / "rpc_trace.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(redact({"timestamp": datetime.now(UTC).isoformat(), "run_id": job.run_id,
                                     "request": request, "response": payload,
                                     "elapsed_seconds": round(time.monotonic() - rpc_started, 6)}),
                                     ensure_ascii=False, allow_nan=False) + "\n")
                    if self._safe_rpc_file(response, job):
                        _save_json(response, payload)
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            job.stop.wait(0.015)

    def _stop_container(self, job: _Job) -> None:
        if job.container_name is None:
            return
        try:
            subprocess.run([self.config.docker_executable, "rm", "-f", job.container_name],
                           env=_clean_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _finish(self, job: _Job, status: str, error: str | None = None) -> None:
        with job.lock:
            if job.done.is_set():
                return
            job.stop.set()
            if job.container_name:
                self._stop_container(job)
            if job.tree:
                job.tree.close()
                job.exit_code = job.tree.process.returncode
            job.status, job.error = status, error
            job.finished = time.monotonic()
            if job.broker_thread and job.broker_thread is not threading.current_thread():
                job.broker_thread.join(timeout=1)
            emitted_error = self._result(job).get("emits_error")
            if job.status == "completed" and emitted_error:
                job.status, job.error = "error", "Invalid emitted results: " + emitted_error
            self._persist(job)
            job.done.set()

    def _watch(self, job: _Job) -> None:
        while not job.stop.is_set():
            size = sum(path.stat().st_size for path in (job.directory / "stdout.log", job.directory / "stderr.log") if path.exists())
            if size > self.config.max_log_bytes:
                self._finish(job, "output_limit", "Output log byte limit reached; process tree terminated")
                return
            exit_code = job.tree.process.poll()
            if exit_code is not None:
                self._finish(job, "completed" if exit_code == 0 else "error",
                             None if exit_code == 0 else f"Process exited with code {exit_code}")
                return
            if time.monotonic() - job.started >= job.timeout:
                self._finish(job, "timeout", "Execution hard timeout reached; owned process tree terminated")
                return
            job.stop.wait(0.02)

    def _terminate(self, job: _Job, status: str, error: str) -> None:
        self._finish(job, status, error)

    def _result(self, job: _Job) -> dict[str, Any]:
        stdout, out_truncated, out_bytes = _excerpt(job.directory / "stdout.log", self.config.max_output_chars)
        stderr, err_truncated, err_bytes = _excerpt(job.directory / "stderr.log", self.config.max_output_chars)
        emits = []
        emits_error = None
        emitted_file = job.rpc_dir.parent / "emits.jsonl"
        if emitted_file.exists():
            try:
                if not self._safe_internal_path(emitted_file) or emitted_file.stat().st_size > 64_000:
                    raise ValueError("Emitted result exceeds permitted size or uses symlink")
                for line in emitted_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        emits.append(_json_copy(json.loads(line), max_bytes=64_000))
                    if len(emits) > 64:
                        raise ValueError("Too many emitted results")
            except (ValueError, TypeError, OSError) as exc:
                emits_error = str(exc)
        result = {"status": job.status, "run_id": job.run_id, "job_id": job.run_id,
                  "session_id": self.session_id, "backend": self.config.backend,
                  "security_boundary": "docker_container" if self.config.backend == "docker" else "none_trusted_host_execution",
                  "exit_code": job.exit_code, "outputs": emits,
                  "observed_evidence_ids": sorted(getattr(job.sdk, "observed_evidence_ids", set())),
                  "record_path": str(job.directory / "receipt.json"), "trace_path": str(job.directory / "sdk_trace.jsonl"),
                  "rpc_trace_path": str(job.directory / "rpc_trace.jsonl"),
                  "sdk_calls": job.sdk.calls, "rpc_operations": job.rpc_operations,
                  "agent_sdk_calls": self.shared_budget.calls,
                  "stdout": stdout, "stderr": stderr,
                  "stdout_truncated": out_truncated, "stderr_truncated": err_truncated,
                  "stdout_bytes": out_bytes, "stderr_bytes": err_bytes,
                  "stdout_path": str(job.directory / "stdout.log"), "stderr_path": str(job.directory / "stderr.log"),
                  "workspace": str(self.workspace), "elapsed_seconds": round((job.finished or time.monotonic()) - job.started, 6),
                  "timeout_seconds": job.timeout, "max_sdk_calls": self.config.max_sdk_calls,
                  "max_total_sdk_calls": self.config.max_total_sdk_calls,
                  "sdk_call_summaries": list(job.sdk.call_summaries), "rpc_summaries": list(job.rpc_summaries),
                  "emits_error": emits_error}
        if job.error:
            result["error"] = job.error
        return result

    def _persist(self, job: _Job) -> None:
        _save_json(job.directory / "receipt.json", {"timestamp": datetime.now(UTC).isoformat(),
                   "action": job.action, "code": job.code, "command": job.command,
                   "code_sha256": hashlib.sha256((job.code or job.command or "").encode("utf-8")).hexdigest(),
                   "limits": asdict(self.config), **self._result(job)})

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for job in self._jobs.values():
                if not job.done.is_set():
                    self._terminate(job, "terminated", "Terminal session closed; owned process tree terminated")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FinanceTerminalTool(BaseTool):
    def __init__(self, terminal: FinanceTerminal) -> None:
        self.terminal = terminal
        super().__init__(ToolDefinition(name="finance_terminal",
            description="Real persistent terminal. Actions: python(code), shell(command), write_file(path,content), "
                        "read_file(path), list_files(path='.'), start_job(code OR command), poll_job(job_id), "
                        "terminate_job(job_id), read_record(path,offset=0,limit=12000), status. "
                        "Python can import pandas and pypdf when installed. "
                        "finance/mcp/records/emit are injected and importable from finance_runtime_sdk. "
                        "records.read reads authorized archived observations/host logs through the host broker. "
                        "Use finance.calculate/compare_periods for final financial arithmetic and preserve evidence_ids. "
                        "Files persist across executions; Python globals and shell environments do not. "
                        "Default Docker has no network. Read input JSON from FINANCE_INPUTS_DIR. "
                        "stdout/stderr are excerpts; full logs and receipts are retained. Failed runs retain files/effects.",
            input_schema={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["python", "shell", "write_file", "read_file", "list_files",
                                                         "start_job", "poll_job", "terminate_job", "read_record", "status"]},
                "code": {"type": "string"}, "command": {"type": "string"}, "path": {"type": "string"},
                "content": {"type": "string"}, "job_id": {"type": "string"}, "timeout_seconds": {"type": "number"},
                "offset": {"type": "integer"}, "limit": {"type": "integer"}},
                "required": ["action"], "additionalProperties": False},
            metadata={"profile": "finance", "execution": "real_terminal", "backend": terminal.config.backend}))

    async def ainvoke(self, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        try:
            args = _validate(arguments if arguments is not None else {}, self.input_schema)
            action = args.pop("action")
            result = await anyio.to_thread.run_sync(lambda: self.terminal.execute(action, **args))
            success = result["status"] in {"completed", "running"}
            return ToolResult(tool_name=self.name, success=success, structured_content=result,
                              error=None if success else result.get("error", "Terminal action failed"),
                              content=[ToolContentItem(text=json.dumps(result, ensure_ascii=False, allow_nan=False))])
        except TerminalActionError as exc:
            return ToolResult(tool_name=self.name, success=False, error=str(exc), structured_content=exc.result,
                              content=[ToolContentItem(text=json.dumps(exc.result, ensure_ascii=False))])
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            return ToolResult(tool_name=self.name, success=False, error=str(exc),
                              structured_content={"status": "error", "error": str(exc)},
                              content=[ToolContentItem(text="finance_terminal failed: " + str(exc))])


def build_finance_terminal_tool(store: FinanceStore, workspace: Path, output_dir: Path,
                                config: TerminalConfig | None = None,
                                extra_rpc: Callable[[dict], dict] | None = None) -> FinanceTerminalTool:
    return FinanceTerminalTool(FinanceTerminal(store, workspace, output_dir, config, extra_rpc))
