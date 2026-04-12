from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, TypedDict

ChatRole = Literal["system", "user", "assistant", "tool"]


class ChatMessage(TypedDict, total=False):
    role: ChatRole
    content: str
    name: str
    tool_call_id: str


@dataclass(slots=True)
class LLMResponse:
    content: str
    model: str
    provider: str
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    raw_response: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
