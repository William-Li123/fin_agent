"""One research capability boundary with host-verified provenance.

Terminal stdout and ``emit`` values are data. They cannot create a verified
source or calculation receipt; only successful host tools and host SDK calls
can update the observation ledger.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from .connectors import MCPConnectors
from .audit import redact
from .records import HostRecordBroker
from .registry import ToolRegistry
from .schemas import ToolDefinition, ToolResult
from .store import FinanceStore
from .terminal import FinanceTerminal, FinanceTerminalTool, TerminalConfig
from .tools import FinanceTool, build_finance_tools


READ_TOOLS = {"finance_search_evidence", "finance_read_table", "finance_read_document"}
CALCULATION_TOOLS = {"finance_calculate", "finance_normalize_value", "finance_compare_periods"}


def _source_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        identity = value.get("evidence_id")
        found = {identity} if isinstance(identity, str) else set()
        for child in value.values():
            found.update(_source_ids(child))
        return found
    if isinstance(value, list):
        return set().union(*(_source_ids(child) for child in value)) if value else set()
    return set()


class ResearchWorkspace:
    """Documents, financial methods, terminal, optional connectors and receipts.

    ``input_paths`` contains sanitized JSON files with exactly id/question/
    pre_text/post_text/table. Raw-format parsing belongs to ``finagent.ingestion``.
    Each input is copied byte-for-byte into a per-run snapshot so evidence hashes
    remain stable and workers never see input-parent directories.
    """

    def __init__(self, input_paths: list[Path], run_dir: Path, *,
                 terminal_backend: str = "docker", memory_store=None,
                 session_id: str | None = None, scope_id: str | None = None,
                 mcp_config: Path | None = None, workspace: Path | None = None,
                 terminal_config: TerminalConfig | None = None):
        if not input_paths:
            raise ValueError("A research workspace requires at least one document")
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        snapshot = self.run_dir / "input_documents"
        snapshot.mkdir(exist_ok=True)
        expected_files = set()
        for source in input_paths:
            path = Path(source).resolve()
            if not path.is_file() or path.suffix.lower() != ".json":
                raise ValueError("Research inputs must be sanitized JSON files")
            if any(part.lower() in {"raw", "gold"} for part in path.parts):
                raise ValueError("Raw and gold inputs cannot be exposed to research tools")
            if path.stat().st_size > 10_000_000:
                raise ValueError("Input document exceeds the 10 MB limit")
            data = path.read_bytes()
            name = hashlib.sha256(data).hexdigest() + ".json"
            expected_files.add(name)
            target = snapshot / name
            if target.exists() and target.read_bytes() != data:
                raise ValueError("Input snapshot has changed unexpectedly")
            target.write_bytes(data)
        if {path.name for path in snapshot.glob("*.json")} != expected_files:
            raise ValueError("Existing run directory contains a different input scope; create a new run")
        self.store = FinanceStore(snapshot, self.run_dir)
        catalog = self.store.list_documents()
        scope_catalog = sorted(({"document_id": item["document_id"], "source_sha256": item["source_sha256"]}
                                for item in catalog), key=lambda item: item["document_id"])
        actual_scope = hashlib.sha256(json.dumps(scope_catalog, sort_keys=True,
                                                 ensure_ascii=False).encode("utf-8")).hexdigest()
        if scope_id is not None and scope_id != actual_scope:
            raise ValueError("Supplied scope_id does not match the documents and content hashes")
        self.scope_id = actual_scope
        self.observed_evidence_ids: set[str] = set()
        self.observed_calculations: dict[str, dict[str, Any]] = {}
        self.observed_artifacts: dict[str, dict[str, Any]] = {}
        self.last_trusted_observation: dict[str, Any] = {}
        self.connectors = MCPConnectors(mcp_config, trace_path=self.run_dir / "mcp_trace.jsonl")
        self.records = HostRecordBroker(self.run_dir, connectors=self.connectors, memory_store=memory_store)
        if memory_store is not None:
            evidence = [record for item in catalog for record in self.store.evidence_for_document(item["document_id"])]
            memory_store.bind_scope(self.scope_id, catalog, evidence)
        if session_id is not None:
            self.records.bind_session(session_id, self.scope_id)
        config = terminal_config or TerminalConfig(backend=terminal_backend)
        self.terminal = FinanceTerminal(self.store, workspace or self.run_dir / "workspace",
                                        self.run_dir, config, self.records.dispatch)
        self.terminal.observed_evidence_ids = self.observed_evidence_ids
        self.registry = ToolRegistry()
        self.registry.register_many(build_finance_tools(store=self.store))
        self.registry.register(FinanceTerminalTool(self.terminal))
        if self.connectors.servers:
            self.registry.register(FinanceTool(ToolDefinition(
                name="finance_mcp", description="Call an explicitly configured MCP server and allowlisted tool. "
                + json.dumps(self.connectors.describe(), ensure_ascii=False), input_schema={
                    "type": "object", "properties": {
                        "server": {"type": "string"}, "operation": {"type": "string", "enum": ["list_tools", "call_tool"]},
                        "tool": {"type": "string"}, "arguments": {"type": "object"}},
                    "required": ["server", "operation"], "additionalProperties": False}),
                self.store, lambda args: self.connectors.dispatch({"kind": "mcp", **args})))
        self._closed = False

    def catalog(self) -> list[dict[str, Any]]:
        return self.registry.as_openai_tools()

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.last_trusted_observation = {}
        try:
            if self._closed:
                raise ValueError("Research workspace is closed")
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object")
            if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 1_000_000:
                raise ValueError("Tool arguments exceed 1 MB")
            references = arguments.get("evidence_ids", [])
            if not isinstance(references, list) or any(ref not in self.observed_evidence_ids for ref in references):
                raise ValueError("Cited evidence must first be observed through read/search")
            result = self.registry.invoke(name, arguments)
        except (KeyError, ValueError, TypeError, OSError, RuntimeError) as error:
            result = ToolResult(tool_name=name, success=False, error=str(error))
        payload = result.model_dump(mode="json", exclude={"raw"})
        content = result.structured_content
        if result.success and isinstance(content, dict):
            if name in READ_TOOLS:
                self.observed_evidence_ids.update(_source_ids(content) & self.store.known_evidence_ids)
                self.last_trusted_observation = deepcopy(content)
            elif name in CALCULATION_TOOLS:
                self.observed_calculations[content["calculation_id"]] = deepcopy(content)
                self.last_trusted_observation = deepcopy(content)
            elif name in {"finance_render_chart", "finance_write_report"}:
                self.observed_artifacts[content["artifact_id"]] = deepcopy(content)
                self.last_trusted_observation = deepcopy(content)
            elif name == "finance_terminal" and content.get("status") == "completed":
                job = self.terminal._jobs.get(content.get("job_id"))
                if job is not None:
                    # These objects were produced by the host-side broker. No
                    # recursive inspection of worker output participates here.
                    self.observed_evidence_ids.update(job.sdk.observed_evidence_ids & self.store.known_evidence_ids)
                    self.observed_calculations.update(deepcopy(job.sdk.observed_calculations))
                    self.observed_artifacts.update(deepcopy(job.sdk.observed_artifacts))
                    self.last_trusted_observation = {
                        "sources": self.store.get_evidence(sorted(job.sdk.observed_evidence_ids)),
                        "calculations": list(deepcopy(job.sdk.observed_calculations).values()),
                        "artifacts": list(deepcopy(job.sdk.observed_artifacts).values()),
                        "status": "completed", "job_id": job.run_id}
                    payload["trusted_observation"] = deepcopy(self.last_trusted_observation)
        try:
            record = json.dumps(redact({"name": name, "arguments": arguments, "result": payload}),
                                ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            record = json.dumps(redact({"name": str(name), "arguments": "invalid non-JSON arguments", "result": payload}))
        with (self.run_dir / "tools.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(record + "\n")
        return payload

    def close(self) -> None:
        if not self._closed:
            self.terminal.close()
            self.connectors.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
