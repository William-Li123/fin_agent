from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .schemas import SessionRecord, SessionSummary, TurnRecord

DEFAULT_STORAGE_DIR = Path(__file__).resolve().parent / "storage"


class SessionStore:
    def __init__(self, storage_dir: str | Path | None = None) -> None:
        self.storage_dir = Path(storage_dir).resolve() if storage_dir is not None else DEFAULT_STORAGE_DIR.resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def list_sessions(self) -> list[SessionSummary]:
        sessions: list[SessionSummary] = []
        for path in sorted(self.storage_dir.glob("*.json")):
            record = self.load_session(path.stem)
            sessions.append(
                SessionSummary(
                    session_id=record.session_id,
                    name=record.name,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    turn_count=len(record.turns),
                )
            )
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions

    def create_session(self, name: str, *, metadata: dict | None = None) -> SessionRecord:
        now = _utc_now_iso()
        session = SessionRecord(
            session_id=str(uuid.uuid4()),
            name=name.strip() or "Untitled Session",
            created_at=now,
            updated_at=now,
            turns=[],
            metadata=dict(metadata or {}),
        )
        self.save_session(session)
        return session

    def load_session(self, session_id: str) -> SessionRecord:
        path = self._session_path(session_id)
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        return SessionRecord(
            session_id=str(payload["session_id"]),
            name=str(payload["name"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            turns=[
                TurnRecord(
                    turn_id=str(item["turn_id"]),
                    user_message=str(item["user_message"]),
                    assistant_message=str(item["assistant_message"]),
                    created_at=str(item["created_at"]),
                    plan=dict(item.get("plan", {})),
                    iteration_count=int(item.get("iteration_count", 0)),
                    execution_history=list(item.get("execution_history", [])),
                )
                for item in payload.get("turns", [])
            ],
            metadata=dict(payload.get("metadata", {})),
        )

    def save_session(self, session: SessionRecord) -> None:
        path = self._session_path(session.session_id)
        with path.open("w", encoding="utf-8") as file:
            json.dump(session.to_dict(), file, ensure_ascii=False, indent=2)

    def append_turn(
        self,
        session_id: str,
        *,
        user_message: str,
        assistant_message: str,
        plan: dict | None = None,
        iteration_count: int = 0,
        execution_history: Iterable[dict] | None = None,
    ) -> SessionRecord:
        session = self.load_session(session_id)
        turn = TurnRecord(
            turn_id=str(uuid.uuid4()),
            user_message=user_message,
            assistant_message=assistant_message,
            created_at=_utc_now_iso(),
            plan=dict(plan or {}),
            iteration_count=iteration_count,
            execution_history=list(execution_history or []),
        )
        session.turns.append(turn)
        session.updated_at = _utc_now_iso()
        self.save_session(session)
        return session

    def rename_session(self, session_id: str, name: str) -> SessionRecord:
        session = self.load_session(session_id)
        session.name = name.strip() or session.name
        session.updated_at = _utc_now_iso()
        self.save_session(session)
        return session

    def update_metadata(
        self,
        session_id: str,
        metadata: dict,
        *,
        replace_existing: bool = False,
    ) -> SessionRecord:
        session = self.load_session(session_id)
        if replace_existing:
            session.metadata = dict(metadata)
        else:
            merged = dict(session.metadata)
            merged.update(metadata)
            session.metadata = merged
        session.updated_at = _utc_now_iso()
        self.save_session(session)
        return session

    def build_conversation_history(self, session: SessionRecord, *, max_turns: int = 12) -> list[dict[str, str]]:
        relevant_turns = session.turns[-max_turns:]
        return [
            {
                "user_message": turn.user_message,
                "assistant_message": turn.assistant_message,
            }
            for turn in relevant_turns
        ]

    def _session_path(self, session_id: str) -> Path:
        return self.storage_dir / f"{session_id}.json"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
