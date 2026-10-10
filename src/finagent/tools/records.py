"""Read only current-run or explicitly bound session/scope host records."""
from __future__ import annotations

from pathlib import Path


class HostRecordBroker:
    def __init__(self, output_dir: Path, *, connectors=None, memory_store=None):
        self.output_dir = Path(output_dir).resolve()
        self.connectors, self.memory_store = connectors, memory_store
        self.session_record_root: Path | None = None

    def bind_session(self, session_id: str, scope_id: str) -> None:
        from finagent.memory.store import _identifier
        import re
        _identifier(session_id, "session_id")
        if not re.fullmatch(r"[a-f0-9]{64}", scope_id):
            raise ValueError("Invalid record scope")
        if self.memory_store is not None:
            self.session_record_root = (self.memory_store.records_dir / session_id / scope_id).resolve()

    def dispatch(self, request: dict) -> dict:
        if request.get("kind") != "record":
            if self.connectors is None:
                raise ValueError("No external connectors are configured")
            return self.connectors.dispatch(request)
        path = request.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("Record path must be a nonempty string")
        supplied = Path(path)
        if not supplied.is_absolute():
            supplied = self.output_dir / supplied
        target = supplied.resolve()
        roots = [self.output_dir] + ([self.session_record_root] if self.session_record_root else [])
        root = next((root for root in roots if root in target.parents), None)
        if root is None:
            raise ValueError("Record is outside the current run or bound session scope")
        current = root
        for part in supplied.absolute().relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Record symlinks are unavailable")
        if target.suffix.lower() not in {".json", ".jsonl", ".log", ".txt", ".md", ".svg"} or not target.is_file():
            raise ValueError("Record must be an existing text output file")
        offset, limit = request.get("offset", 0), request.get("limit", 12000)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12000:
            raise ValueError("Record reads require nonnegative byte offset and 1–12000 byte limit")
        with target.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(limit)
        size = target.stat().st_size
        return {"status": "completed", "record_path": str(target), "content": data.decode("utf-8", errors="replace"),
                "offset": offset, "next_offset": offset + len(data), "size_bytes": size,
                "has_more": offset + len(data) < size}
