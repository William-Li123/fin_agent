"""Optional, allowlisted MCP stdio connections owned by the host broker.

No network or vendor account is configured by default. Protocol credentials
remain in the host process; terminal code only sends JSON RPC arguments.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any

from .audit import redact


class MCPConnectionError(ValueError):
    pass


class MCPStdioConnection:
    def __init__(self, name: str, config: dict[str, Any], *, trace_path: Path | None = None):
        command = config.get("command")
        if not isinstance(command, list) or not command or not all(type(s) is str and s for s in command):
            raise MCPConnectionError("MCP command must be a nonempty argument array")
        allowed = config.get("allowed_tools", [])
        if not isinstance(allowed, list) or not all(type(s) is str and s for s in allowed):
            raise MCPConnectionError("MCP allowed_tools must be an explicit name array")
        timeout = config.get("timeout_seconds", 20)
        if type(timeout) not in {int, float} or not 0 < timeout <= 120:
            raise MCPConnectionError("MCP timeout must be in (0, 120]")
        self.name, self.command, self.allowed = name, command, set(allowed)
        self.timeout, self.trace_path = float(timeout), trace_path
        self.protocol_version = config.get("protocol_version", "2025-11-25")
        self.env_names = config.get("env_names", [])
        if not isinstance(self.env_names, list) or not all(type(s) is str for s in self.env_names):
            raise MCPConnectionError("env_names must contain environment variable names only")
        self.proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._responses: queue.Queue = queue.Queue(maxsize=128)
        self._sequence = 0
        self._catalog: dict[str, dict] | None = None

    def _read_responses(self, proc, responses) -> None:
        try:
            assert proc.stdout is not None
            while True:
                line = proc.stdout.readline(1_048_577)
                if not line:
                    responses.put(MCPConnectionError("MCP server closed stdout"), timeout=1)
                    return
                if len(line) > 1_048_576:
                    raise MCPConnectionError("MCP response exceeds 1 MiB")
                payload = json.loads(line)
                if isinstance(payload, dict) and "id" in payload:
                    if "method" in payload:
                        # Sampling, elicitation and server-initiated requests are
                        # deliberately unsupported; do not leave the server waiting.
                        self._send({"jsonrpc": "2.0", "id": payload["id"],
                                    "error": {"code": -32601, "message": "Client request capability unavailable"}})
                    else:
                        responses.put(payload, timeout=1)
        except Exception as exc:
            try:
                responses.put(MCPConnectionError(str(exc)), timeout=1)
            except queue.Full:
                pass

    def _send(self, payload: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        wire = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(wire) > 65_536:
            raise MCPConnectionError("MCP request exceeds 64 KiB")
        stream = self.proc.stdin
        completed: queue.Queue = queue.Queue(maxsize=1)
        def write():
            try:
                with self._send_lock:
                    stream.write(wire + b"\n")
                    stream.flush()
                completed.put(None)
            except (OSError, ValueError) as exc:
                completed.put(exc)
        threading.Thread(target=write, daemon=True).start()
        try:
            error = completed.get(timeout=self.timeout)
        except queue.Empty as exc:
            self.close()
            raise MCPConnectionError("MCP request write timed out; connection closed") from exc
        if error is not None:
            raise MCPConnectionError("MCP server input closed") from error

    def _request(self, method: str, params: dict) -> dict:
        self._sequence += 1
        sequence = self._sequence
        deadline = time.monotonic() + self.timeout
        self._send({"jsonrpc": "2.0", "id": sequence, "method": method, "params": params})
        while True:
            if time.monotonic() >= deadline:
                self.close()
                raise MCPConnectionError("MCP request timed out; connection closed")
            try:
                response = self._responses.get(timeout=max(deadline - time.monotonic(), .001))
            except queue.Empty as exc:
                self.close()
                raise MCPConnectionError("MCP request timed out; connection closed") from exc
            if isinstance(response, Exception):
                self.close()
                raise response
            if response.get("id") != sequence:
                continue
            if "error" in response:
                raise MCPConnectionError(f"MCP protocol error: {response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise MCPConnectionError("MCP result must be an object")
            return result

    def _connect(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC", "HOME"}}
        for name in self.env_names:
            if name in os.environ:
                environment[name] = os.environ[name]
        environment["PYTHONIOENCODING"] = "utf-8"
        self._responses = queue.Queue(maxsize=128)
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=environment,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        threading.Thread(target=self._read_responses, args=(self.proc, self._responses), daemon=True).start()
        try:
            result = self._request("initialize", {"protocolVersion": self.protocol_version,
                                                  "capabilities": {}, "clientInfo": {"name": "finance-agent", "version": "1.0"}})
            if result.get("capabilities", {}).get("tools") is None:
                raise MCPConnectionError("MCP server does not advertise tools")
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._catalog = None
        except Exception:
            self.close()
            raise

    def discover(self) -> list[dict]:
        with self._lock:
            self._connect()
            catalog, cursor = {}, None
            for _ in range(32):
                result = self._request("tools/list", {"cursor": cursor} if cursor else {})
                for tool in result.get("tools", []):
                    if isinstance(tool, dict) and tool.get("name") in self.allowed:
                        catalog[tool["name"]] = tool
                cursor = result.get("nextCursor")
                if not cursor:
                    self._catalog = catalog
                    return list(catalog.values())
            raise MCPConnectionError("MCP tool pagination exceeds 32 pages")

    def call(self, tool: str, arguments: dict) -> dict:
        if tool not in self.allowed:
            raise MCPConnectionError("MCP tool is outside the configured allowlist")
        if type(arguments) is not dict:
            raise MCPConnectionError("MCP arguments must be an object")
        with self._lock:
            self._connect()
            if self._catalog is None:
                self.discover()
            if tool not in (self._catalog or {}):
                raise MCPConnectionError("Allowlisted MCP tool was not advertised by the server")
            result = self._request("tools/call", {"name": tool, "arguments": arguments})
            if self.trace_path is not None:
                self.trace_path.parent.mkdir(parents=True, exist_ok=True)
                with self.trace_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(redact({"kind": "mcp", "server": self.name, "tool": tool,
                                             "arguments": arguments, "result": result}), ensure_ascii=False) + "\n")
            if result.get("isError"):
                raise MCPConnectionError(f"MCP tool returned an error: {result.get('content', [])}")
            return result

    def close(self) -> None:
        if self.proc is not None:
            proc, self.proc = self.proc, None
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    stream.close()
        self._catalog = None


class MCPConnectors:
    def __init__(self, config_path: Path | None = None, *, trace_path: Path | None = None):
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig")) if config_path else {"servers": {}}
        servers = config.get("servers")
        if not isinstance(servers, dict) or not all(type(name) is str and isinstance(value, dict) for name, value in servers.items()):
            raise MCPConnectionError("MCP configuration requires a servers object")
        self.servers = {name: MCPStdioConnection(name, value, trace_path=trace_path) for name, value in servers.items()}

    def dispatch(self, request: dict) -> dict:
        server = request.get("server")
        if server not in self.servers:
            raise MCPConnectionError("MCP server is not configured")
        if request.get("operation") == "list_tools":
            return {"tools": self.servers[server].discover()}
        if request.get("operation") not in {None, "call_tool"}:
            raise MCPConnectionError("Unsupported MCP operation")
        return self.servers[server].call(request.get("tool", ""), request.get("arguments", {}))

    def describe(self) -> dict:
        return {name: {"allowed_tools": sorted(connection.allowed), "transport": "stdio"}
                for name, connection in self.servers.items()}

    def close(self) -> None:
        for connection in self.servers.values():
            connection.close()
