"""JSON mailbox clients for scoped host financial and configured MCP tools.

This file is copied into the execution workspace. It imports no host backend.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

_lock = threading.Lock()
_METHODS = {"list_documents", "search_evidence", "read_document", "read_table", "calculate",
            "normalize_value", "compare_periods", "render_chart", "write_report"}


def _call(payload: dict):
    root = Path(os.environ["FINANCE_RPC_DIR"])
    token = uuid4().hex
    request = root / ("request_" + token + ".json")
    response = root / ("response_" + token + ".json")
    payload["request_id"] = token
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 512_000:
        raise ValueError("RPC request exceeds 512000 bytes")
    timeout = float(os.environ.get("FINANCE_RPC_TIMEOUT", "30"))
    with _lock:
        temporary = request.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(request)
        deadline = time.monotonic() + timeout
        while not response.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Host RPC response deadline reached")
            time.sleep(0.015)
        result = json.loads(response.read_text(encoding="utf-8"))
        response.unlink(missing_ok=True)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "Host RPC failed"))
    return result["result"]


def _compact_table(table):
    """Preserve every cell value, identity and position; share document metadata.

    The complete host response remains in the RPC trace. Repeating the document
    hash and the whole row body in every cell can overflow a model observation.
    """
    if not isinstance(table, dict) or not isinstance(table.get("rows"), list):
        return table
    result = dict(table)
    hashes = set()
    rows = []
    keep = ("evidence_id", "text", "row", "column", "locator", "row_label", "column_header", "unit", "currency")
    for row in table["rows"]:
        cells = []
        for cell in row["cells"]:
            if cell.get("document_id") not in (None, table.get("document_id")):
                raise ValueError("Table response contains a different document")
            if cell.get("source_sha256"):
                hashes.add(cell["source_sha256"])
            cells.append({key: cell[key] for key in keep if key in cell})
        rows.append({**row, "cells": cells})
    if len(hashes) > 1:
        raise ValueError("Table response contains inconsistent document hashes")
    if hashes:
        result["source_sha256"] = next(iter(hashes))
    result["rows"] = rows
    return result


def _compact_search(result):
    """Deduplicate context quotations without dropping text, IDs or locations."""
    if not isinstance(result, dict) or not isinstance(result.get('hits'), list):
        return result
    projected = dict(result)
    documents, context, hits = {}, {}, []
    for hit in result['hits']:
        document_id = hit.get('document_id')
        digest = hit.get('source_sha256')
        if document_id and digest:
            if document_id in documents and documents[document_id] != digest:
                raise ValueError('Search response contains inconsistent document hashes')
            documents[document_id] = digest
        slim = {key: value for key, value in hit.items() if key != 'source_sha256'}
        # Narrative body equals text; table body may contain other row values,
        # so retain it unless it is an exact duplicate of the selected text.
        if slim.get('body') == slim.get('text'):
            slim.pop('body', None)
        for field in ('context_before', 'context_after', 'context_heading'):
            if field not in hit:
                continue
            items = hit[field] if isinstance(hit[field], list) else [hit[field]]
            refs = []
            for paragraph in items:
                if not isinstance(paragraph, dict) or not paragraph.get('evidence_id'):
                    raise ValueError('Search context is missing an evidence identity')
                identity = paragraph['evidence_id']
                value = {**paragraph, 'document_id': document_id}
                if identity in context and context[identity] != value:
                    raise ValueError('Search context contains inconsistent evidence')
                context[identity] = value
                refs.append(identity)
            slim[field] = refs if isinstance(hit[field], list) else refs[0]
        hits.append(slim)
    projected.update(hits=hits, documents=documents, context_paragraphs=context)
    return projected


class _Finance:
    def __getattr__(self, name):
        if name not in _METHODS:
            raise AttributeError("Unknown financial SDK method")
        def invoke(**arguments):
            compact = arguments.pop("compact", True) if name in {"read_table", "search_evidence"} else False
            if type(compact) is not bool:
                raise ValueError(f"{name} compact must be boolean")
            result = _call({"kind": "finance", "name": name,
                            "arguments": {key: value for key, value in arguments.items() if value is not None}})
            if compact:
                return _compact_table(result) if name == "read_table" else _compact_search(result)
            return result
        return invoke


class _MCP:
    def call(self, server: str, tool: str, arguments: dict | None = None):
        return _call({"kind": "mcp", "server": server, "tool": tool,
                      "arguments": arguments or {}})

    def list_tools(self, server: str):
        return _call({"kind": "mcp", "server": server, "operation": "list_tools"})


class _Records:
    def read(self, path: str, offset: int = 0, limit: int = 12000):
        return _call({"kind": "record", "path": path, "offset": offset, "limit": limit})


_emit_bytes = 0
_emit_count = 0


def emit(value):
    """Save a small JSON result independently of noisy standard output."""
    global _emit_bytes, _emit_count
    encoded = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    with _lock:
        if _emit_count >= 64 or _emit_bytes + len(encoded) > 64_000:
            raise ValueError("emit limit is 64 results / 64000 JSON bytes per execution")
        with Path(os.environ["FINANCE_EMIT_PATH"]).open("ab") as handle:
            handle.write(encoded)
        _emit_count += 1
        _emit_bytes += len(encoded)


finance = _Finance()
mcp = _MCP()
records = _Records()
