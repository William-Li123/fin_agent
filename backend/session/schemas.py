from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class TurnRecord:
    turn_id: str
    user_message: str
    assistant_message: str
    created_at: str
    plan: dict[str, Any] = field(default_factory=dict)
    iteration_count: int = 0
    execution_history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    name: str
    created_at: str
    updated_at: str
    turns: list[TurnRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["turns"] = [turn.to_dict() for turn in self.turns]
        return payload


@dataclass(slots=True)
class SessionSummary:
    session_id: str
    name: str
    created_at: str
    updated_at: str
    turn_count: int
