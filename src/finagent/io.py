"""Small shared serialization utilities; no model or domain dependencies."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def digest(value) -> str:
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def runtime_fingerprint() -> str:
    """Hash the installed Python implementation, independent of workspace paths."""
    root = Path(__file__).parent
    return digest({path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sorted(root.rglob("*.py"))})


def redact(text: str) -> str:
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_KEY]", text)


def save_json(path: str | Path, value) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(redact(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

