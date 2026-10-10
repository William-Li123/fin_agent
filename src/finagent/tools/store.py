"""Scoped sanitized documents, stable evidence, and output manifests."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from threading import RLock
from typing import Any, Iterable
from uuid import uuid4

DOCUMENT_KEYS = {"id", "question", "pre_text", "post_text", "table"}
BLOCKED_COMPONENTS = {"gold", "raw"}


def _safe_path(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Path escapes the configured scope.")
    if BLOCKED_COMPONENTS.intersection(part.lower() for part in resolved.parts):
        raise ValueError("Raw data and gold directories are unavailable to tools.")
    return resolved


class FinanceStore:
    def __init__(self, inputs_dir: str | Path, output_dir: str | Path, *,
                 allowed_document_ids: Iterable[str] | None = None,
                 selected_document_id: str | None = None) -> None:
        supplied = Path(inputs_dir).resolve()
        self.inputs_dir = supplied.parent if supplied.is_file() else supplied
        _safe_path(supplied, self.inputs_dir)
        if not supplied.exists():
            raise ValueError("Finance inputs do not exist.")
        self.output_dir = Path(output_dir).resolve()
        _safe_path(self.output_dir, self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._documents: dict[str, dict[str, Any]] = {}
        self._sources: dict[str, dict[str, str]] = {}
        self._evidence: dict[str, dict[str, Any]] = {}
        self._manifest: dict[str, Any] = {"version": 1, "documents": [], "calculations": [], "artifacts": []}
        allowed = set(allowed_document_ids) if allowed_document_ids is not None else None
        if selected_document_id is not None:
            if allowed is not None and selected_document_id not in allowed:
                raise ValueError("Selected document is outside the allowed list.")
            allowed = {selected_document_id}
        paths = [supplied] if supplied.is_file() else sorted(supplied.rglob("*.json"))
        for path in paths:
            if path.name == "manifest.json" or BLOCKED_COMPONENTS.intersection(x.lower() for x in path.parts):
                continue
            path = _safe_path(path, self.inputs_dir)
            raw = path.read_bytes()
            doc = json.loads(raw)
            if not isinstance(doc, dict) or set(doc) != DOCUMENT_KEYS:
                raise ValueError("Input JSON must contain only id, question, pre_text, post_text, table.")
            doc_id = doc["id"]
            if not isinstance(doc_id, str) or not doc_id.strip():
                raise ValueError("Document id must be a nonempty string.")
            if allowed is not None and doc_id not in allowed:
                continue
            if doc_id in self._documents:
                raise ValueError("Duplicate document id.")
            if not isinstance(doc["question"], str):
                raise ValueError("Question must be a string.")
            for field in ("pre_text", "post_text"):
                if isinstance(doc[field], str):
                    doc[field] = [doc[field]]
                if not isinstance(doc[field], list) or not all(isinstance(x, str) for x in doc[field]):
                    raise ValueError("Document paragraphs must contain strings.")
            if not isinstance(doc["table"], list) or not all(
                isinstance(row, list) and all(isinstance(cell, str) for cell in row) for row in doc["table"]
            ):
                raise ValueError("Table rows must contain strings.")
            self._documents[doc_id] = doc
            self._sources[doc_id] = {"source_sha256": hashlib.sha256(raw).hexdigest(),
                                     "input_path": path.relative_to(self.inputs_dir).as_posix()}
            self._build_evidence(doc_id)
        if not self._documents or (allowed is not None and allowed.difference(self._documents)):
            raise ValueError("Requested finance document was not found.")
        self.selected_document_id = selected_document_id
        self._manifest["documents"] = self.list_documents()
        manifest_path = _safe_path(self.output_dir / "manifest.json", self.output_dir)
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (not isinstance(previous, dict) or previous.get("version") != 1
                    or previous.get("documents") != self._manifest["documents"]
                    or not isinstance(previous.get("calculations"), list)
                    or not isinstance(previous.get("artifacts"), list)):
                raise ValueError("Existing output manifest belongs to a different or invalid document scope.")
            self._manifest = previous
        self._persist_manifest()

    def list_documents(self) -> list[dict[str, Any]]:
        return [{"document_id": doc_id, "question": doc["question"], "row_count": len(doc["table"]),
                 **self._sources[doc_id]} for doc_id, doc in self._documents.items()]

    def get_document(self, document_id: str | None = None) -> dict[str, Any]:
        if document_id is None:
            if len(self._documents) != 1:
                raise ValueError("document_id is required for multiple documents.")
            document_id = next(iter(self._documents))
        if document_id not in self._documents:
            raise ValueError("Document is outside the permitted task scope.")
        return json.loads(json.dumps(self._documents[document_id], ensure_ascii=False))

    def _build_evidence(self, doc_id: str) -> None:
        doc = self._documents[doc_id]
        for section in ("pre_text", "post_text"):
            for index, text in enumerate(doc[section]):
                self._add_evidence(doc_id, f"{section}:{index}", text, text, section=section, paragraph=index)
        table = doc["table"]
        def numeric_cell(value: str) -> bool:
            return bool(re.fullmatch(r"[\s$€£+\-()\d,.%]+", value)) and bool(re.search(r"\d", value))
        # A reconciliation can start with an actual balance, not a header.
        # Do not apply that first balance/year as every later cell's column title.
        first = table[0] if table else []
        has_header = bool(first) and not any(numeric_cell(value) and not re.fullmatch(r"\s*(?:19|20)\d{2}\s*", value)
                                            for value in first[1:])
        if first and re.search(r"\b(?:19|20)\d{2}\b", first[0]) and any(numeric_cell(value) for value in first[1:]):
            has_header = False
        for row_index, row in enumerate(table):
            for col_index, cell in enumerate(row):
                header = table[0][col_index] if has_header and col_index < len(table[0]) else ""
                self._add_evidence(doc_id, f"table:r{row_index}:c{col_index}", cell,
                                   f"{header} | {' | '.join(row)}", section="table",
                                   row=row_index, column=col_index, column_header=header,
                                   row_label=row[0] if row else "")

    def _add_evidence(self, doc_id: str, locator: str, text: str, body: str, **metadata: Any) -> None:
        source = self._sources[doc_id]
        identity = json.dumps([doc_id, source["source_sha256"], locator, text], ensure_ascii=False)
        evidence_id = "ev_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        self._evidence[evidence_id] = {"evidence_id": evidence_id, "document_id": doc_id,
                                      "locator": locator, "text": text, "body": body, **source, **metadata}

    def evidence_for_document(self, document_id: str | None = None) -> list[dict[str, Any]]:
        doc_id = self.get_document(document_id)["id"]
        return [dict(x) for x in self._evidence.values() if x["document_id"] == doc_id]

    def validate_evidence_ids(self, ids: list[str]) -> list[str]:
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            raise ValueError("Evidence ids must be a list of strings.")
        if len(ids) != len(set(ids)) or any(x not in self._evidence for x in ids):
            raise ValueError("Duplicated, unknown or out-of-scope evidence id.")
        return list(ids)

    def get_evidence(self, evidence_ids: list[str]) -> list[dict[str, Any]]:
        return [dict(self._evidence[x]) for x in self.validate_evidence_ids(evidence_ids)]

    @property
    def known_evidence_ids(self) -> set[str]:
        return set(self._evidence)

    def save_calculation(self, result: dict[str, Any]) -> str:
        with self._lock:
            calculation_id = "calc_" + uuid4().hex[:16]
            self._manifest["calculations"].append({"calculation_id": calculation_id, **result})
            self._persist_manifest()
            return calculation_id

    def write_artifact(self, *, kind: str, content: str, extension: str,
                       evidence_ids: list[str], filename: str | None = None) -> dict[str, Any]:
        evidence = self.get_evidence(evidence_ids)
        if extension not in {".md", ".svg"}:
            raise ValueError("Unsupported artifact format.")
        filename = filename or f"{kind}_{uuid4().hex[:16]}{extension}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", filename) or not filename.endswith(extension):
            raise ValueError("Artifact filename must be a basename with the required extension.")
        path = _safe_path(self.output_dir / filename, self.output_dir)
        with self._lock:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            item = {"artifact_id": "artifact_" + uuid4().hex[:16], "kind": kind,
                    "path": str(path), "filename": filename,
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "evidence_ids": list(evidence_ids),
                    "sources": [{"evidence_id": x["evidence_id"], "document_id": x["document_id"],
                                 "source_sha256": x["source_sha256"], "locator": x["locator"]} for x in evidence]}
            self._manifest["artifacts"].append(item)
            self._persist_manifest()
            return item

    def _persist_manifest(self) -> None:
        target = _safe_path(self.output_dir / "manifest.json", self.output_dir)
        temporary = _safe_path(self.output_dir / f".manifest-{uuid4().hex}.tmp", self.output_dir)
        temporary.write_text(json.dumps(self._manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
