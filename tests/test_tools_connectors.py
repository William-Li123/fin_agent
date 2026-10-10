"""Exercise a real stdio MCP subprocess and the host's scoped record boundary."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from finagent.tools.connectors import MCPConnectionError, MCPConnectors
from finagent.memory.store import MemoryStore
from finagent.tools.records import HostRecordBroker


SERVER_SOURCE = r'''
import json
import sys
import time
from pathlib import Path

audit = Path(sys.argv[1])
for wire in sys.stdin.buffer:
    request = json.loads(wire)
    with audit.open('a', encoding='utf-8') as output:
        output.write(json.dumps(request) + '\n')
    identity = request.get('id')
    if identity is None:
        continue
    method = request.get('method')
    if method == 'initialize':
        result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'offline-test', 'version': '1.0'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': name, 'description': name, 'inputSchema': {'type': 'object'}}
                            for name in ['echo', 'fail', 'slow', 'hidden']]}
    elif method == 'tools/call':
        params = request['params']
        name = params['name']
        if name == 'slow':
            time.sleep(3)
        result = {'content': [{'type': 'text', 'text': json.dumps(params['arguments'])}],
                  'structuredContent': params['arguments'], 'isError': name == 'fail'}
    else:
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': identity,
                                     'error': {'code': -32601, 'message': 'unknown method'}}) + '\n')
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': identity, 'result': result}) + '\n')
    sys.stdout.flush()
'''


@pytest.fixture
def connector(tmp_path):
    script = tmp_path / "mcp_fixture.py"
    script.write_text(SERVER_SOURCE, encoding="utf-8")
    audit = tmp_path / "server_requests.jsonl"
    config = tmp_path / "mcp_config.json"
    config.write_text(json.dumps({"servers": {"fixture": {"command": [sys.executable, "-u", str(script), str(audit)],
        "allowed_tools": ["echo", "fail", "slow", "absent"], "timeout_seconds": 2}}}), encoding="utf-8")
    connections = MCPConnectors(config, trace_path=tmp_path / "mcp_trace.jsonl")
    yield connections, audit, tmp_path
    connections.close()


def test_real_stdio_initialize_discover_call_and_trace(connector):
    connections, audit, directory = connector
    tools = connections.dispatch({"kind": "mcp", "operation": "list_tools", "server": "fixture"})["tools"]
    assert {tool["name"] for tool in tools} == {"echo", "fail", "slow"}
    result = connections.dispatch({"kind": "mcp", "server": "fixture", "tool": "echo", "arguments": {"ticker": "TEST", "value": 42}})
    assert result["structuredContent"] == {"ticker": "TEST", "value": 42}
    requests = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [request["method"] for request in requests] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert requests[0]["params"]["capabilities"] == {}
    trace = json.loads((directory / "mcp_trace.jsonl").read_text().strip())
    assert trace["kind"] == "mcp" and trace["server"] == "fixture" and trace["tool"] == "echo"
    assert trace["result"] == result


def test_unknown_server_and_tool_allowlist_rejected_before_start(connector):
    connections, audit, _ = connector
    with pytest.raises(MCPConnectionError, match="not configured"):
        connections.dispatch({"server": "unknown", "tool": "echo"})
    with pytest.raises(MCPConnectionError, match="allowlist"):
        connections.dispatch({"server": "fixture", "tool": "hidden"})
    assert not audit.exists()


def test_allowlisted_but_unadvertised_tool_rejected(connector):
    connections, audit, _ = connector
    with pytest.raises(MCPConnectionError, match="not advertised"):
        connections.dispatch({"server": "fixture", "tool": "absent"})
    assert not any(json.loads(line)["method"] == "tools/call" for line in audit.read_text().splitlines())


def test_mcp_tool_error_is_explicit_and_audited(connector):
    connections, _, directory = connector
    with pytest.raises(MCPConnectionError, match="tool returned an error"):
        connections.dispatch({"server": "fixture", "tool": "fail", "arguments": {"message": "test failure"}})
    trace = json.loads((directory / "mcp_trace.jsonl").read_text().strip())
    assert trace["result"]["isError"] is True


def test_real_mcp_timeout_closes_server_and_can_reconnect(connector):
    connections, _, _ = connector
    # Initialization is completed before measuring the short per-request limit.
    connections.dispatch({"server": "fixture", "operation": "list_tools"})
    start = time.monotonic()
    with pytest.raises(MCPConnectionError, match="timed out"):
        connections.dispatch({"server": "fixture", "tool": "slow", "arguments": {}})
    assert time.monotonic() - start < 4
    assert connections.servers["fixture"].proc is None
    result = connections.dispatch({"server": "fixture", "tool": "echo", "arguments": {"after_timeout": True}})
    assert result["structuredContent"] == {"after_timeout": True}


def test_arguments_must_be_an_object(connector):
    connections, _, _ = connector
    with pytest.raises(MCPConnectionError, match="object"):
        connections.dispatch({"server": "fixture", "tool": "echo", "arguments": ["bad"]})


def test_default_mcp_has_no_servers_or_network():
    connections = MCPConnectors()
    assert connections.describe() == {}
    with pytest.raises(MCPConnectionError):
        connections.dispatch({"server": "finance", "tool": "live_quotes"})
    connections.close()


def test_scoped_record_byte_ranges_and_relative_paths(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    raw = b"0123456789abcdef"
    (run / "trace.jsonl").write_bytes(raw)
    broker = HostRecordBroker(run)
    first = broker.dispatch({"kind": "record", "path": "trace.jsonl", "offset": 3, "limit": 5})
    assert first["content"] == "34567" and first["next_offset"] == 8 and first["has_more"] is True
    last = broker.dispatch({"kind": "record", "path": "trace.jsonl", "offset": 14, "limit": 5})
    assert last["content"] == "ef" and last["next_offset"] == 16 and last["has_more"] is False
    beyond = broker.dispatch({"kind": "record", "path": "trace.jsonl", "offset": 100, "limit": 5})
    assert beyond["content"] == "" and beyond["next_offset"] == 100


@pytest.mark.parametrize("arguments", [{"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 12001}, {"limit": True}, {"limit": "100"}])
def test_record_range_validation(tmp_path, arguments):
    (tmp_path / "trace.txt").write_text("trace", encoding="utf-8")
    with pytest.raises(ValueError, match="offset"):
        HostRecordBroker(tmp_path).dispatch({"kind": "record", "path": "trace.txt", **arguments})


def test_record_scope_prevents_sibling_and_parent_access(tmp_path):
    run = tmp_path / "current"
    run.mkdir()
    (tmp_path / "other.json").write_text('{"private": "other question"}', encoding="utf-8")
    broker = HostRecordBroker(run)
    for path in ("../other.json", str(tmp_path / "other.json")):
        with pytest.raises(ValueError, match="scope"):
            broker.dispatch({"kind": "record", "path": path})


def test_record_session_binding_only_allows_same_session_scope(tmp_path):
    run = tmp_path / "current"
    run.mkdir()
    memory = MemoryStore(tmp_path / "memory.sqlite")
    scope = "a" * 64
    permitted = memory.records_dir / "session_one" / scope
    permitted.mkdir(parents=True)
    (permitted / "turn.json").write_text('{"answer": "same scope"}', encoding="utf-8")
    other_scope = memory.records_dir / "session_one" / ("b" * 64)
    other_scope.mkdir(parents=True)
    (other_scope / "turn.json").write_text('{"answer": "other scope"}', encoding="utf-8")
    other_session = memory.records_dir / "session_two" / scope
    other_session.mkdir(parents=True)
    (other_session / "turn.json").write_text('{"answer": "other session"}', encoding="utf-8")
    broker = HostRecordBroker(run, memory_store=memory)
    broker.bind_session("session_one", scope)
    assert "same scope" in broker.dispatch({"kind": "record", "path": str(permitted / "turn.json")})["content"]
    for path in (other_scope / "turn.json", other_session / "turn.json", memory.path):
        with pytest.raises(ValueError, match="scope"):
            broker.dispatch({"kind": "record", "path": str(path)})


def test_record_symlinks_rejected(tmp_path):
    (tmp_path / "target.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(tmp_path / "target.json")
    except OSError as error:
        pytest.skip(f"OS does not permit this symlink fixture: {error}")
    with pytest.raises(ValueError, match="symlink"):
        HostRecordBroker(tmp_path).dispatch({"kind": "record", "path": str(link)})


def test_record_binary_file_and_missing_connector_rejected(tmp_path):
    (tmp_path / "private.sqlite").write_bytes(b"binary")
    broker = HostRecordBroker(tmp_path)
    with pytest.raises(ValueError, match="text output"):
        broker.dispatch({"kind": "record", "path": "private.sqlite"})
    with pytest.raises(ValueError, match="connectors"):
        broker.dispatch({"kind": "mcp", "server": "unknown"})
