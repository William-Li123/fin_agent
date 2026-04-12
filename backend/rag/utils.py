from __future__ import annotations

import hashlib
import mimetypes
import uuid
from pathlib import Path


def normalize_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [line.rstrip() for line in normalized.split("\n")]
    collapsed = "\n".join(lines)
    return collapsed.strip()


def stable_source_id(path: Path) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, path.resolve().as_posix()))


def stable_chunk_id(source_id: str, chunk_index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_id}:{chunk_index}"))


def infer_mime_type(path: Path) -> str | None:
    mime_type, _ = mimetypes.guess_type(path.name)
    return mime_type


def is_hidden_path(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def directory_has_runtime_files(path: Path) -> bool:
    if not path.exists():
        return False

    ignored_names = {"readme.md", ".gitkeep"}
    for candidate in path.rglob("*"):
        if candidate.is_file() and candidate.name.lower() not in ignored_names:
            return True
    return False


def deterministic_embedding(text: str, *, dimension: int) -> list[float]:
    values = [0.0] * dimension
    for token in text.lower().split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimension
        values[bucket] += 1.0

    norm = sum(value * value for value in values) ** 0.5
    if norm == 0:
        return values
    return [value / norm for value in values]
