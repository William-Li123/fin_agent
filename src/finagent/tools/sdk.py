"""JSON-only financial capabilities for composed, local code execution.

The SDK reuses the existing synchronous tool handlers, rather than nesting
BaseTool.invoke/anyio.run inside another tool's active event loop. It exposes
neither filesystem handles nor an LLM client to the code interpreter.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import InvalidOperation
import json
import math
from pathlib import Path
import threading
import time
from typing import Any

from .store import FinanceStore
from .audit import redact
from .tools import _validate, build_finance_tools


SDK_METHOD_NAMES = (
    "list_documents", "search_evidence", "read_document", "read_table", "calculate",
    "normalize_value", "compare_periods", "render_chart", "write_report",
)


class FinanceSDKError(ValueError):
    """A financial operation failed; prior completed operations are retained."""


class SDKCallBudget:
    """Share a bounded operation count across batches in one Agent instance."""
    def __init__(self, max_calls: int = 64) -> None:
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise ValueError("max_calls must be a positive integer")
        self.max_calls, self.calls = max_calls, 0
        self._lock = threading.Lock()

    def admit(self) -> None:
        with self._lock:
            if self.calls >= self.max_calls:
                raise FinanceSDKError("Agent SDK operation limit reached")
            self.calls += 1


def _json_copy(value: Any, *, max_bytes: int) -> Any:
    # Check types, aggregate bytes and structure before serializing a full value.
    # Exact types also prevent custom __str__/JSON conversion hooks at this edge.
    total, nodes = 0, 0
    active: set[int] = set()

    def consume(count: int) -> None:
        nonlocal total
        total += count
        if total > max_bytes:
            raise FinanceSDKError("SDK value exceeds the permitted byte size")

    def visit(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > 10_000 or depth > 64:
            raise FinanceSDKError("SDK JSON structure exceeds the permitted size/depth")
        kind = type(item)
        if item is None or kind is bool:
            consume(5)
            return item
        if kind is int:
            if item.bit_length() > 1024:
                raise FinanceSDKError("SDK integer exceeds the supported size")
            consume(len(str(item)))
            return item
        if kind is float:
            if not math.isfinite(item):
                raise FinanceSDKError("SDK values must be finite JSON data")
            consume(len(json.dumps(item, allow_nan=False)))
            return item
        if kind is str:
            if len(item) > max_bytes:
                raise FinanceSDKError("SDK string exceeds the permitted byte size")
            consume(len(json.dumps(item, ensure_ascii=False).encode("utf-8")))
            return item
        if kind not in {list, dict}:
            raise FinanceSDKError("SDK values must contain plain JSON types")
        if len(item) > 4096 or id(item) in active:
            raise FinanceSDKError("SDK collection is oversized or cyclic")
        active.add(id(item))
        consume(2 + max(0, len(item) - 1))
        try:
            if kind is list:
                return [visit(child, depth + 1) for child in item]
            copied = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise FinanceSDKError("SDK object keys must be strings")
                visit(key, depth + 1)
                consume(1)
                copied[key] = visit(child, depth + 1)
            return copied
        finally:
            active.remove(id(item))

    return visit(value, 0)


class FinanceSDK:
    def __init__(self, store: FinanceStore, *, trace_path: Path | None = None,
                 max_calls: int = 32, execution_id: str | None = None,
                 max_value_bytes: int = 512_000,
                 shared_budget: SDKCallBudget | None = None,
                 initial_evidence_ids: set[str] | None = None) -> None:
        for label, value in (("max_calls", max_calls), ("max_value_bytes", max_value_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        self._tools = {tool.name.removeprefix("finance_"): tool for tool in build_finance_tools(store=store)}
        self.trace_path = Path(trace_path) if trace_path is not None else None
        self.max_calls, self.max_value_bytes = max_calls, max_value_bytes
        self.execution_id, self.shared_budget = execution_id, shared_budget
        self.calls = 0
        self.call_summaries: list[dict[str, Any]] = []
        self.observed_evidence_ids: set[str] = set(initial_evidence_ids or ())
        self.observed_calculations: dict[str, dict[str, Any]] = {}
        self.observed_artifacts: dict[str, dict[str, Any]] = {}

    def _record(self, payload: dict[str, Any]) -> None:
        if self.trace_path is None:
            return
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"execution_id": self.execution_id,
                  "timestamp": datetime.now(UTC).isoformat(), **payload}
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(redact(record), ensure_ascii=False, allow_nan=False) + "\n")

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in SDK_METHOD_NAMES or name not in self._tools:
            raise FinanceSDKError("Unknown financial SDK method")
        if not isinstance(arguments, dict):
            raise FinanceSDKError("SDK arguments must be a JSON object")
        if self.calls >= self.max_calls:
            raise FinanceSDKError("Batch SDK operation limit reached")
        arguments = _json_copy(arguments, max_bytes=self.max_value_bytes)
        if self.shared_budget is not None:
            self.shared_budget.admit()
        self.calls += 1
        sequence, started = self.calls, time.monotonic()
        self._record({"event": "sdk_started", "sequence": sequence,
                      "name": name, "arguments": arguments})
        try:
            tool = self._tools[name]
            refs = arguments.get("evidence_ids", [])
            if not isinstance(refs, list) or any(ref not in self.observed_evidence_ids for ref in refs):
                raise FinanceSDKError("Cited evidence must first be observed through read/search")
            result = tool.handler(_validate(arguments, tool.input_schema))
            result = _json_copy(result, max_bytes=self.max_value_bytes)
            self._record({"event": "sdk_completed", "sequence": sequence, "name": name,
                          "arguments": arguments, "result": result,
                          "elapsed_seconds": round(time.monotonic() - started, 6)})
            if name in {"calculate", "normalize_value", "compare_periods"}:
                self.observed_calculations[result["calculation_id"]] = deepcopy(result)
            if name in {"render_chart", "write_report"}:
                self.observed_artifacts[result["artifact_id"]] = deepcopy(result)
            if name in {"read_table", "search_evidence", "read_document"}:
                def collect(value):
                    if isinstance(value, dict):
                        if isinstance(value.get("evidence_id"), str):
                            self.observed_evidence_ids.add(value["evidence_id"])
                        for child in value.values():
                            collect(child)
                    elif isinstance(value, list):
                        for child in value:
                            collect(child)
                collect(result)
        except (ValueError, TypeError, KeyError, OSError, InvalidOperation) as exc:
            self.call_summaries.append({"sequence": sequence, "name": name, "success": False})
            self._record({"event": "sdk_failed", "sequence": sequence, "name": name,
                          "arguments": arguments, "error": str(exc),
                          "elapsed_seconds": round(time.monotonic() - started, 6)})
            raise FinanceSDKError(f"{name} failed: {exc}") from exc
        self.call_summaries.append({"sequence": sequence, "name": name, "success": True})
        return deepcopy(result)

    def list_documents(self) -> dict[str, Any]:
        return self.call("list_documents", {})

    def read_document(self, *, document_id: str | None = None, offset: int = 0,
                      max_chars: int = 40000) -> dict[str, Any]:
        arguments = {"offset": offset, "max_chars": max_chars}
        if document_id is not None:
            arguments["document_id"] = document_id
        return self.call("read_document", arguments)

    def search_evidence(self, *, query: str, document_id: str | None = None, top_k: int = 5) -> dict[str, Any]:
        args = {"query": query, "top_k": top_k}
        if document_id is not None:
            args["document_id"] = document_id
        return self.call("search_evidence", args)

    def read_table(self, *, document_id: str | None = None, row_start: int = 0,
                   row_end: int | None = None, columns: list[int] | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"row_start": row_start}
        for name, value in (("document_id", document_id), ("row_end", row_end), ("columns", columns)):
            if value is not None:
                args[name] = value
        return self.call("read_table", args)

    def calculate(self, *, expression: str, variables: dict | None = None,
                  evidence_ids: list[str] | None = None) -> dict[str, Any]:
        return self.call("calculate", {"expression": expression, "variables": variables or {},
                                       "evidence_ids": evidence_ids or []})

    def normalize_value(self, *, value: str, unit: str = "number", target_unit: str = "number",
                        currency: str | None = None, evidence_ids: list[str] | None = None) -> dict[str, Any]:
        args = {"value": value, "unit": unit, "target_unit": target_unit, "evidence_ids": evidence_ids or []}
        if currency is not None:
            args["currency"] = currency
        return self.call("normalize_value", args)

    def compare_periods(self, *, current: str, previous: str,
                        evidence_ids: list[str] | None = None) -> dict[str, Any]:
        return self.call("compare_periods", {"current": current, "previous": previous, "evidence_ids": evidence_ids or []})

    def render_chart(self, *, title: str, labels: list[str], values: list[str],
                     evidence_ids: list[str] | None = None, filename: str | None = None) -> dict[str, Any]:
        args = {"title": title, "labels": labels, "values": values, "evidence_ids": evidence_ids or []}
        if filename is not None:
            args["filename"] = filename
        return self.call("render_chart", args)

    def write_report(self, *, title: str, body: str, evidence_ids: list[str],
                     filename: str | None = None) -> dict[str, Any]:
        args = {"title": title, "body": body, "evidence_ids": evidence_ids}
        if filename is not None:
            args["filename"] = filename
        return self.call("write_report", args)
