from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ConversationMemoryRecord:
    turn_id: str
    session_id: str
    user_message: str
    assistant_message: str
    raw_text: str
    char_count: int
    summarized: bool
    summary_id: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class SummaryMemoryRecord:
    summary_id: str
    session_id: str
    summary_title: str | None
    summary_text: str
    source_conversation: str
    source_turn_ids: list[str] = field(default_factory=list)
    source_turn_count: int = 0
    source_char_count: int = 0
    score: float | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class PersonaMemoryRecord:
    persona_id: str
    owner_scope: str
    memory_key: str
    memory_value: str
    category: str
    importance: float
    always_include: bool
    source_note: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class ConversationAppendResult:
    session_id: str
    turn_id: str
    char_count: int
    unsummarized_char_count: int
    compaction_triggered: bool
    created_summary_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SummaryCompactionResult:
    session_id: str
    compacted: bool
    created_summary_ids: list[str] = field(default_factory=list)
    compacted_turn_ids: list[str] = field(default_factory=list)
    remaining_unsummarized_chars: int = 0
    summaries: list[SummaryMemoryRecord] = field(default_factory=list)
    message: str = ""


@dataclass(slots=True)
class SummarySearchResult:
    session_id: str
    query: str
    top_k: int
    hits: list[SummaryMemoryRecord] = field(default_factory=list)


@dataclass(slots=True)
class SummaryContext:
    session_id: str
    query: str
    context_text: str
    hits: list[SummaryMemoryRecord] = field(default_factory=list)


@dataclass(slots=True)
class PersonaContext:
    owner_scope: str
    context_text: str
    items: list[PersonaMemoryRecord] = field(default_factory=list)


@dataclass(slots=True)
class MemoryRuntimeStatus:
    oracle_configured: bool
    oracle_reachable: bool
    schema_ready: bool
    embedding_ready: bool
    llm_ready: bool
    messages: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
