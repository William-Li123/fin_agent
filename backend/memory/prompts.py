from __future__ import annotations

import re
from pathlib import Path
from typing import Any


PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
DEFAULT_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


class MemoryPromptRepository:
    def __init__(self, prompt_dir: str | Path | None = None) -> None:
        self.prompt_dir = Path(prompt_dir).resolve() if prompt_dir is not None else DEFAULT_PROMPT_DIR.resolve()

    def load(self, name: str) -> str:
        return (self.prompt_dir / f"{name}.md").read_text(encoding="utf-8")

    def render(self, name: str, values: dict[str, Any]) -> str:
        template = self.load(name)

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            value = values.get(key)
            return "" if value is None else str(value)

        return PLACEHOLDER_PATTERN.sub(replace, template)
