"""Real, unpaid local process tests plus separately identified Docker contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from finagent.tools.store import FinanceStore
from finagent.tools.terminal import FinanceTerminal, TerminalConfig, build_finance_terminal_tool


@pytest.fixture
def terminal_case(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    document = {"id": "corp/a", "question": "Revenue growth?", "pre_text": ["USD millions"],
                "post_text": [], "table": [["Revenue", "2019", "2020"], ["Net", "100", "110"]]}
    (inputs / "a.json").write_text(json.dumps(document), encoding="utf-8")
    (inputs / "excluded.json").write_text(json.dumps(dict(document, id="corp/b")), encoding="utf-8")
    store = FinanceStore(inputs, tmp_path / "artifacts", selected_document_id="corp/a")
    workspace, output = tmp_path / "workspace", tmp_path / "host_output"
    runtime = FinanceTerminal(store, workspace, output, TerminalConfig(backend="trusted-local"))
    yield runtime, store, workspace, output
    runtime.close()


def test_real_python_import_pandas_sdk_emit_and_persistent_files(terminal_case):
    terminal, _, workspace, _ = terminal_case
    result = terminal.execute("python", code='''
import pandas as pd
from pathlib import Path
table = finance.read_table(row_start=1, row_end=2)
cells = table["rows"][0]["cells"]
df = pd.DataFrame({"value": [cells[1]["text"], cells[2]["text"]]})
df.to_csv("values.csv", index=False)
refs = [cells[1]["evidence_id"], cells[2]["evidence_id"]]
growth = finance.compare_periods(current=cells[2]["text"], previous=cells[1]["text"], evidence_ids=refs)
emit({"growth": growth["change_percent"], "sources": refs, "rows": len(df)})
''')
    assert result["status"] == "completed", result
    assert result["outputs"][0]["growth"] == "10"
    assert result["outputs"][0]["rows"] == 2
    assert len(result["outputs"][0]["sources"]) == 2
    assert result["sdk_calls"] == result["rpc_operations"] == 2
    assert (workspace / "values.csv").read_text().splitlines() == ["value", "100", "110"]
    followup = terminal.execute("python", code='from pathlib import Path\nemit(Path("values.csv").read_text())')
    assert followup["status"] == "completed"
    assert "110" in followup["outputs"][0]
    receipt = json.loads(Path(result["record_path"]).read_text(encoding="utf-8"))
    assert receipt["code_sha256"] and receipt["limits"]["backend"] == "trusted-local"
    assert receipt["outputs"] == result["outputs"]
    events = [json.loads(line) for line in Path(result["trace_path"]).read_text(encoding="utf-8").splitlines()]
    assert len([event for event in events if event["event"] == "sdk_completed"]) == 2


def test_real_shell_and_file_actions(terminal_case):
    terminal, _, _, _ = terminal_case
    command = 'Set-Content -LiteralPath "shell.txt" -Value "shell-ok" -Encoding UTF8' if os.name == "nt" else 'printf "shell-ok\\n" > shell.txt'
    result = terminal.execute("shell", command=command)
    assert result["status"] == "completed", result
    assert "shell-ok" in terminal.execute("read_file", path="shell.txt")["content"]
    terminal.execute("write_file", path="reports/note.txt", content="Persistent note")
    assert terminal.execute("read_file", path="reports/note.txt")["content"] == "Persistent note"
    names = {item["name"] for item in terminal.execute("list_files")["files"]}
    assert "reports" in names and "shell.txt" in names and ".finance-runtime" not in names


def test_parent_secrets_are_not_forwarded(terminal_case, monkeypatch):
    terminal, _, _, _ = terminal_case
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-sentinel-deepseek")
    monkeypatch.setenv("OPENAI_API_KEY", "test-sentinel-openai")
    monkeypatch.setenv("CUSTOM_VENDOR_TOKEN", "test-sentinel-custom")
    result = terminal.execute("python", code='import os\nemit({k: os.environ.get(k) for k in ["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "CUSTOM_VENDOR_TOKEN"]})')
    assert result["status"] == "completed"
    assert all(value is None for value in result["outputs"][0].values())


def test_stdout_stderr_excerpts_retain_full_logs_and_emitted_sources(terminal_case):
    terminal, _, _, _ = terminal_case
    result = terminal.execute("python", code='import sys\nprint("A" * 20000)\nprint("B" * 20000, file=sys.stderr)\nemit({"sources": ["ev_fixture"]})')
    assert result["status"] == "completed", result
    assert result["stdout_truncated"] and result["stderr_truncated"]
    assert len(result["stdout"]) <= terminal.config.max_output_chars
    assert Path(result["stdout_path"]).stat().st_size > 20000
    assert Path(result["stderr_path"]).stat().st_size > 20000
    assert result["outputs"] == [{"sources": ["ev_fixture"]}]


def test_failure_keeps_prior_emit_file_and_financial_effect(terminal_case):
    terminal, store, workspace, _ = terminal_case
    result = terminal.execute("python", code='from pathlib import Path\nPath("partial.txt").write_text("kept")\nemit(finance.calculate(expression="1+2"))\nraise RuntimeError("intentional failure")')
    assert result["status"] == "error" and result["exit_code"] != 0
    assert result["outputs"][0]["value"] == "3"
    assert (workspace / "partial.txt").read_text() == "kept"
    assert len(json.loads((store.output_dir / "manifest.json").read_text())["calculations"]) == 1
    assert "intentional failure" in Path(result["stderr_path"]).read_text(encoding="utf-8")
    assert json.loads(Path(result["record_path"]).read_text())["status"] == "error"


def _alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    exit_code = wintypes.DWORD()
    try:
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
    finally:
        kernel.CloseHandle(handle)


def test_hard_timeout_terminates_real_child_process(terminal_case):
    terminal, _, workspace, _ = terminal_case
    code = '''
import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path("child.pid").write_text(str(child.pid))
while True:
    time.sleep(1)
'''
    started = time.monotonic()
    result = terminal.execute("python", code=code, timeout_seconds=1)
    assert result["status"] == "timeout", result
    assert time.monotonic() - started < 8
    child_pid = int((workspace / "child.pid").read_text())
    assert not _alive(child_pid), f"Child process {child_pid} survived timeout"


def test_normal_parent_completion_also_removes_children(terminal_case):
    terminal, _, workspace, _ = terminal_case
    code = 'import subprocess, sys\nfrom pathlib import Path\nchild=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\nPath("child.pid").write_text(str(child.pid))'
    result = terminal.execute("python", code=code)
    assert result["status"] == "completed", result
    assert not _alive(int((workspace / "child.pid").read_text()))


def test_background_job_poll_terminate_and_close(terminal_case):
    terminal, _, _, _ = terminal_case
    initial = terminal.execute("start_job", code='import time\nprint("started", flush=True)\ntime.sleep(60)', timeout_seconds=120)
    assert initial["status"] == "running"
    polled = terminal.execute("poll_job", job_id=initial["job_id"])
    assert polled["run_id"] == initial["run_id"]
    ended = terminal.execute("terminate_job", job_id=initial["job_id"])
    assert ended["status"] == "terminated"
    other = terminal.execute("start_job", code="import time\ntime.sleep(60)", timeout_seconds=120)
    terminal.close()
    assert terminal._jobs[other["job_id"]].done.is_set()
    assert terminal._jobs[other["job_id"]].tree.process.poll() is not None
    with pytest.raises(ValueError, match="closed"):
        terminal.execute("python", code="print('after-close')")


def test_independent_supervisor_times_out_background_job_without_polling(terminal_case):
    terminal, _, _, _ = terminal_case
    result = terminal.execute("start_job", code="import time\ntime.sleep(60)", timeout_seconds=0.5)
    job = terminal._jobs[result["job_id"]]
    assert job.done.wait(5)
    assert job.status == "timeout" and job.tree.process.poll() is not None


@pytest.mark.parametrize("path", ["../escape.txt", "C:\\escape.txt" if os.name == "nt" else "/escape.txt", ".finance-runtime/file.txt"])
def test_file_api_rejects_escaping_and_runtime_paths(terminal_case, path):
    terminal, _, _, _ = terminal_case
    with pytest.raises(ValueError):
        terminal.execute("write_file", path=path, content="bad")


def test_input_snapshot_contains_only_selected_sanitized_document(terminal_case):
    terminal, _, _, _ = terminal_case
    files = list(terminal.inputs_dir.glob("*.json"))
    assert len(files) == 1
    snapshot = json.loads(files[0].read_text())
    assert snapshot["id"] == "corp/a" and set(snapshot) == {"id", "question", "pre_text", "post_text", "table"}
    result = terminal.execute("python", code='import os, json\nfrom pathlib import Path\nemit([json.loads(p.read_text())["id"] for p in Path(os.environ["FINANCE_INPUTS_DIR"]).glob("*.json")])')
    assert result["outputs"] == [["corp/a"]]


def test_finance_and_mcp_share_actual_per_run_and_runtime_limits(terminal_case):
    original, store, workspace, output = terminal_case
    original.close()
    runtime = FinanceTerminal(store, workspace, output, TerminalConfig(backend="trusted-local", max_sdk_calls=2, max_total_sdk_calls=3),
                              extra_rpc=lambda request: {"server": request["server"], "operation": request.get("operation", "call")})
    try:
        first = runtime.execute("python", code='emit(finance.calculate(expression="1+2"))\nemit(mcp.list_tools("fixture"))\nemit(finance.calculate(expression="3+4"))')
        assert first["status"] == "error"
        assert first["sdk_calls"] == 1 and first["rpc_operations"] == 2 and first["agent_sdk_calls"] == 2
        assert first["outputs"][1]["operation"] == "list_tools"
        second = runtime.execute("python", code='emit(mcp.call("fixture","test",{}))\nfinance.calculate(expression="5+6")')
        assert second["status"] == "error"
        assert second["rpc_operations"] == 1 and second["agent_sdk_calls"] == 3
        assert second["sdk_calls"] == 0
    finally:
        runtime.close()


def test_default_docker_fails_closed_without_host_execution(terminal_case, monkeypatch):
    _, store, workspace, output = terminal_case
    runtime = FinanceTerminal(store, workspace, output)
    monkeypatch.setattr(runtime, "_docker_ready", lambda: (False, "Docker unavailable; no host fallback"))
    try:
        result = runtime.execute("python", code='from pathlib import Path\nPath("host-ran.txt").write_text("BAD")')
        assert result["status"] == "error" and "no host fallback" in result["error"]
        assert not (workspace / "host-ran.txt").exists()
        assert result["sdk_calls"] == 0
        assert Path(result["record_path"]).is_file()
    finally:
        runtime.close()


def test_docker_command_contract_fake_probe_not_real_container(terminal_case, monkeypatch):
    _, store, workspace, output = terminal_case
    runtime = FinanceTerminal(store, workspace, output)
    monkeypatch.setattr(runtime, "_docker_ready", lambda: (True, None))
    from finagent.tools.terminal import _Job
    from finagent.tools.sdk import FinanceSDK
    internal = workspace / ".finance-runtime" / "fake"
    internal.mkdir(parents=True)
    job = _Job("run_fake", "python", "print(1)", None, time.monotonic(), 30, output, internal / "rpc", FinanceSDK(store))
    command, env = runtime._command(job, internal / "code.py", internal)
    try:
        joined = " ".join(command)
        assert "--network none" in joined and "--cap-drop ALL" in joined and "no-new-privileges" in joined
        assert "--pids-limit 64" in joined and "--memory 512m" in joined and "--cpus 1.0" in joined
        assert "--read-only" in joined and "--pull never" in joined
        assert len([arg for arg in command if arg.startswith("type=bind")]) == 2
        assert f"source={runtime.inputs_dir},target=/inputs,readonly" in joined
        assert str(store.inputs_dir) not in joined and str(store.output_dir) not in joined
        assert "DEEPSEEK_API_KEY" not in env
    finally:
        runtime.close()


def test_tool_validation_and_structured_errors(terminal_case):
    original, store, workspace, output = terminal_case
    original.close()
    tool = build_finance_terminal_tool(store, workspace, output, TerminalConfig(backend="trusted-local"))
    try:
        assert tool.name == "finance_terminal"
        assert not tool.invoke({"action": "python", "code": "print(1)", "unknown": 1}).success
        failed = tool.invoke({"action": "python", "code": 'emit("before"); raise RuntimeError("after")'})
        assert not failed.success and failed.structured_content["outputs"] == ["before"]
        successful = tool.invoke({"action": "python", "code": "emit(1 + 2)"})
        assert successful.success and successful.structured_content["outputs"] == [3]
        file_failure = tool.invoke({"action": "read_file", "path": "missing.txt"})
        assert not file_failure.success
        assert Path(file_failure.structured_content["record_path"]).is_file()
    finally:
        tool.terminal.close()


def test_archive_reader_direct_action_and_python_rpc(terminal_case):
    runtime, _, _, _ = terminal_case
    requests = []
    def dispatch(request):
        requests.append(request)
        assert request["kind"] == "record"
        return {"path": request["path"], "content": "archived observation", "next_offset": 20}
    runtime.extra_rpc = dispatch
    direct = runtime.execute("read_record", path="authorized/record.json", offset=0, limit=100)
    assert direct["status"] == "completed" and direct["content"] == "archived observation"
    assert direct["agent_sdk_calls"] == 1
    assert Path(direct["record_path"]).is_file()
    result = runtime.execute("python", code='emit(records.read("authorized/record.json", limit=100))')
    assert result["status"] == "completed", result
    assert result["outputs"][0]["content"] == "archived observation"
    assert result["rpc_operations"] == 1 and result["agent_sdk_calls"] == 2
    assert len(requests) == 2
    trace = [json.loads(line) for line in Path(result["rpc_trace_path"]).read_text().splitlines()]
    assert trace[0]["request"]["kind"] == "record"
    assert trace[0]["response"]["result"]["content"] == "archived observation"
    with pytest.raises(ValueError, match="limit"):
        runtime.execute("read_record", path="anything", limit=12001)


def test_zero_exit_with_malformed_emit_is_failure(terminal_case):
    runtime, _, _, _ = terminal_case
    result = runtime.execute("python", code='import os\nfrom pathlib import Path\nPath(os.environ["FINANCE_EMIT_PATH"]).write_text("malformed JSON")')
    assert result["exit_code"] == 0
    assert result["status"] == "error" and result["emits_error"]
    assert "Invalid emitted results" in result["error"]


def test_log_limit_applies_even_when_program_exits_quickly(terminal_case):
    original, store, workspace, output = terminal_case
    original.close()
    runtime = FinanceTerminal(store, workspace, output, TerminalConfig(backend="trusted-local", max_log_bytes=1000))
    try:
        result = runtime.execute("python", code='print("x" * 10000)')
        assert result["status"] == "output_limit", result
        assert result["stdout_bytes"] > 1000
        assert Path(result["stdout_path"]).is_file()
    finally:
        runtime.close()


def test_internal_path_guard_rejects_symlinked_parent(terminal_case, monkeypatch):
    runtime, _, workspace, _ = terminal_case
    parent = workspace / ".finance-runtime"
    candidate = parent / "run_fake" / "rpc" / ("request_" + "a" * 32 + ".json")
    # Deterministic Windows-compatible link simulation; the guard must inspect
    # ancestors, independently of privileges to create a real Windows symlink.
    actual_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda item: item == parent or actual_is_symlink(item))
    assert not runtime._safe_internal_path(candidate)


def test_file_size_and_emit_count_are_bounded(terminal_case):
    runtime, _, _, _ = terminal_case
    with pytest.raises(ValueError, match="max_file_bytes"):
        runtime.execute("write_file", path="oversized.txt", content="x" * (runtime.config.max_file_bytes + 1))
    result = runtime.execute("python", code='for i in range(65):\n    emit(i)')
    assert result["status"] == "error"
    assert result["outputs"] == list(range(64))


def test_worker_emitted_valid_id_does_not_upgrade_host_observation_ledger(terminal_case):
    runtime, store, _, _ = terminal_case
    valid_but_unread = next(iter(store.known_evidence_ids))
    result = runtime.execute("python", code="emit({'sources': [" + repr(valid_but_unread) + "]})")
    assert result["status"] == "completed"
    assert result["outputs"] == [{"sources": [valid_but_unread]}]
    assert result["observed_evidence_ids"] == []


@pytest.mark.parametrize("kwargs", [{"backend": "automatic"}, {"timeout_seconds": float("inf")}, {"max_jobs": True}, {"timeout_seconds": 121}])
def test_invalid_config_rejected(kwargs):
    with pytest.raises(ValueError):
        TerminalConfig(**kwargs)
