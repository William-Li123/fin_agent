from __future__ import annotations

import json
from typing import Any, Sequence

from backend.llm import BaseLLMProvider, build_llm_provider

from .config import MemoryConfig
from .embedder import SentenceTransformerMemoryEmbedder
from .errors import MemoryStoreError
from .prompts import MemoryPromptRepository
from .schemas import (
    ConversationAppendResult,
    ConversationMemoryRecord,
    MemoryRuntimeStatus,
    PersonaContext,
    SummaryCompactionResult,
    SummaryContext,
    SummaryMemoryRecord,
    SummarySearchResult,
)
from .store import OracleMemoryStore


class MemoryService:
    """High-level entry point for Oracle-backed conversation, summary, and persona memory."""

    def __init__(
        self,
        *,
        config: MemoryConfig,
        store: OracleMemoryStore,
        embedder: SentenceTransformerMemoryEmbedder,
        prompts: MemoryPromptRepository,
        llm: BaseLLMProvider | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.embedder = embedder
        self.prompts = prompts
        self._llm = llm

    def get_runtime_status(self) -> MemoryRuntimeStatus:
        messages: list[str] = []
        oracle_configured = self.store.is_configured
        oracle_reachable = self.store.ping() if oracle_configured else False
        schema_ready = self.store.has_schema() if oracle_reachable else False
        embedding_ready = self.embedder.is_configured()
        llm_ready = self._llm_ready()

        if not oracle_configured:
            messages.append("Set ORACLE_USER, ORACLE_PASSWORD, and ORACLE_DSN before using Oracle-backed memory.")
        elif not oracle_reachable:
            messages.append("Oracle memory store is configured but the database is not reachable.")
        if not embedding_ready:
            messages.append(
                f"Place a local embedding model inside {self.config.embedding.model_path} before querying summary memory."
            )
        if not llm_ready:
            messages.append("The summarization LLM is not ready. Summary compaction will fail until the LLM provider is configured.")

        return MemoryRuntimeStatus(
            oracle_configured=oracle_configured,
            oracle_reachable=oracle_reachable,
            schema_ready=schema_ready,
            embedding_ready=embedding_ready,
            llm_ready=llm_ready,
            messages=messages,
            details={
                "config_path": str(self.config.config_path) if self.config.config_path else None,
                "oracle": self.store.describe_status(),
                "embedding": self.embedder.describe_status(),
                "summarization": {
                    "prompt_name": self.config.summarization.prompt_name,
                    "llm_config_path": str(self.config.summarization.llm_config_path)
                    if self.config.summarization.llm_config_path
                    else None,
                },
            },
        )

    def append_conversation_turn(
        self,
        session_id: str,
        *,
        user_message: str,
        assistant_message: str,
        turn_id: str | None = None,
    ) -> ConversationAppendResult:
        self._ensure_schema()
        record = self.store.insert_conversation_turn(
            session_id=session_id,
            user_message=user_message,
            assistant_message=assistant_message,
            turn_id=turn_id,
        )
        pending_chars = self.store.get_unsummarized_char_count(session_id)
        compaction_result = SummaryCompactionResult(
            session_id=session_id,
            compacted=False,
            remaining_unsummarized_chars=pending_chars,
            message="No compaction was needed.",
        )
        if pending_chars > self.config.conversation.char_limit:
            compaction_result = self.compact_session(session_id)

        return ConversationAppendResult(
            session_id=session_id,
            turn_id=record.turn_id,
            char_count=record.char_count,
            unsummarized_char_count=compaction_result.remaining_unsummarized_chars,
            compaction_triggered=compaction_result.compacted,
            created_summary_ids=list(compaction_result.created_summary_ids),
        )

    def compact_session(self, session_id: str, *, force: bool = False) -> SummaryCompactionResult:
        self._ensure_schema()
        pending_turns = self.store.get_unsummarized_turns(session_id)
        if not pending_turns:
            return SummaryCompactionResult(
                session_id=session_id,
                compacted=False,
                remaining_unsummarized_chars=0,
                message="There are no unsummarized turns for this session.",
            )

        pending_chars = sum(turn.char_count for turn in pending_turns)
        if not force and pending_chars <= self.config.conversation.char_limit:
            return SummaryCompactionResult(
                session_id=session_id,
                compacted=False,
                remaining_unsummarized_chars=pending_chars,
                message="The unsummarized tail is still within the configured character limit.",
            )

        created_summaries: list[SummaryMemoryRecord] = []
        compacted_turn_ids: list[str] = []
        remaining_turns = list(pending_turns)

        while remaining_turns:
            if not force and sum(item.char_count for item in remaining_turns) <= self.config.conversation.char_limit:
                break

            batch = self._select_compaction_batch(remaining_turns)
            if not batch:
                break

            source_conversation = "\n\n---\n\n".join(turn.raw_text for turn in batch)
            llm_payload = self._summarize_batch(
                session_id=session_id,
                turns=batch,
                source_conversation=source_conversation,
            )
            summary_text = llm_payload["summary_text"]
            summary_title = llm_payload.get("summary_title")
            embedding = self.embedder.embed_query(summary_text)
            source_turn_ids = [turn.turn_id for turn in batch]
            summary_record = self.store.insert_summary(
                session_id=session_id,
                summary_title=summary_title,
                summary_text=summary_text,
                source_conversation=source_conversation,
                source_turn_ids=source_turn_ids,
                source_turn_count=len(batch),
                source_char_count=sum(turn.char_count for turn in batch),
                embedding=embedding,
            )
            self.store.mark_turns_summarized(source_turn_ids, summary_id=summary_record.summary_id)
            created_summaries.append(summary_record)
            compacted_turn_ids.extend(source_turn_ids)
            processed = set(source_turn_ids)
            remaining_turns = [turn for turn in remaining_turns if turn.turn_id not in processed]

        remaining_chars = self.store.get_unsummarized_char_count(session_id)
        return SummaryCompactionResult(
            session_id=session_id,
            compacted=bool(created_summaries),
            created_summary_ids=[item.summary_id for item in created_summaries],
            compacted_turn_ids=compacted_turn_ids,
            remaining_unsummarized_chars=remaining_chars,
            summaries=created_summaries,
            message="Session compaction completed." if created_summaries else "No compaction was performed.",
        )

    def search_summary(self, session_id: str, query: str, *, top_k: int | None = None) -> SummarySearchResult:
        self._ensure_schema()
        normalized_top_k = top_k or self.config.summary.top_k
        query_vector = self.embedder.embed_query(query)
        hits = self.store.search_summaries(session_id=session_id, query_vector=query_vector, top_k=normalized_top_k)
        return SummarySearchResult(session_id=session_id, query=query, top_k=normalized_top_k, hits=hits)

    def build_summary_context(
        self,
        session_id: str,
        query: str,
        *,
        top_k: int | None = None,
        max_context_chars: int | None = None,
    ) -> SummaryContext:
        result = self.search_summary(session_id, query, top_k=top_k)
        budget = max_context_chars or self.config.summary.max_context_chars

        rendered_parts: list[str] = []
        kept_hits: list[SummaryMemoryRecord] = []
        used_chars = 0

        for index, hit in enumerate(result.hits, start=1):
            title = hit.summary_title or f"summary batch {index}"
            segment = f"[{index}] {title}\n{hit.summary_text.strip()}".strip()
            if not segment:
                continue
            remaining = budget - used_chars
            if remaining <= 0:
                break
            if len(segment) > remaining:
                segment = segment[: max(remaining - 16, 0)].rstrip()
                if not segment:
                    break
                segment = f"{segment}\n[truncated]"
            rendered_parts.append(segment)
            kept_hits.append(hit)
            used_chars += len(segment) + 5

        return SummaryContext(
            session_id=session_id,
            query=query,
            context_text="\n\n---\n\n".join(rendered_parts),
            hits=kept_hits,
        )

    def get_persona_context(
        self,
        *,
        owner_scope: str | None = None,
        limit: int | None = None,
        always_include_only: bool = False,
    ) -> PersonaContext:
        self._ensure_schema()
        normalized_owner_scope = (owner_scope or self.config.persona.default_owner_scope).strip() or self.config.persona.default_owner_scope
        normalized_limit = limit or self.config.persona.max_items_in_context
        items = self.store.list_persona_entries(
            owner_scope=normalized_owner_scope,
            limit=normalized_limit,
            always_include_only=always_include_only,
        )
        lines = [f"- [{item.category}] {item.memory_key}: {item.memory_value}" for item in items]
        return PersonaContext(
            owner_scope=normalized_owner_scope,
            context_text="\n".join(lines),
            items=items,
        )

    def upsert_persona_memory(
        self,
        *,
        memory_key: str,
        memory_value: str,
        category: str = "preference",
        importance: float = 0.6,
        always_include: bool = True,
        source_note: str | None = None,
        owner_scope: str | None = None,
    ):
        self._ensure_schema()
        normalized_owner_scope = (owner_scope or self.config.persona.default_owner_scope).strip() or self.config.persona.default_owner_scope
        return self.store.upsert_persona_entry(
            owner_scope=normalized_owner_scope,
            memory_key=memory_key.strip(),
            memory_value=memory_value.strip(),
            category=category.strip() or "preference",
            importance=float(importance),
            always_include=bool(always_include),
            source_note=source_note.strip() if isinstance(source_note, str) and source_note.strip() else None,
        )

    def close(self) -> None:
        self.store.close()

    def _select_compaction_batch(self, turns: Sequence[ConversationMemoryRecord]) -> list[ConversationMemoryRecord]:
        max_source_chars = self.config.summarization.max_source_chars
        char_limit = self.config.conversation.char_limit
        batch: list[ConversationMemoryRecord] = []
        total_chars = 0

        for turn in turns:
            if batch and total_chars + turn.char_count > max_source_chars:
                break
            batch.append(turn)
            total_chars += turn.char_count
            if total_chars >= char_limit:
                break

        if not batch and turns:
            batch = [turns[0]]
        return batch

    def _summarize_batch(
        self,
        *,
        session_id: str,
        turns: Sequence[ConversationMemoryRecord],
        source_conversation: str,
    ) -> dict[str, str]:
        prompt = self.prompts.render(
            self.config.summarization.prompt_name,
            {
                "session_id": session_id,
                "turn_count": len(turns),
                "source_char_count": len(source_conversation),
                "max_summary_chars": self.config.summarization.max_summary_chars,
                "source_conversation": source_conversation,
            },
        )
        response = self._llm_provider().invoke(
            [{"role": "system", "content": prompt}],
            temperature=self.config.summarization.temperature,
        )
        payload = _extract_json_object(response.content)
        summary_title = None
        summary_text = response.content.strip()

        if isinstance(payload, dict):
            summary_title = _normalize_optional_text(payload.get("summary_title"))
            summary_text = _normalize_optional_text(payload.get("summary_text")) or summary_text

        summary_text = summary_text.strip()
        if not summary_text:
            raise MemoryStoreError("Summary compaction returned an empty summary.")
        if len(summary_text) > self.config.summarization.max_summary_chars:
            summary_text = summary_text[: self.config.summarization.max_summary_chars].rstrip()
        return {"summary_title": summary_title or "", "summary_text": summary_text}

    def _ensure_schema(self) -> None:
        self.store.ensure_schema(self.embedder.dimension)

    def _llm_ready(self) -> bool:
        try:
            self._llm_provider()
            return True
        except Exception:
            return False

    def _llm_provider(self) -> BaseLLMProvider:
        if self._llm is None:
            self._llm = build_llm_provider(config_path=self.config.summarization.llm_config_path)
        return self._llm


def _normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
    if not raw_text:
        return None

    candidates = [raw_text.strip()]
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            candidates.append("\n".join(lines[1:-1]).strip())

    brace_start = raw_text.find("{")
    if brace_start >= 0:
        brace_end = _find_matching_brace(raw_text, brace_start)
        if brace_end is not None:
            candidates.append(raw_text[brace_start : brace_end + 1].strip())

    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _find_matching_brace(text: str, start_index: int) -> int | None:
    depth = 0
    in_string = False
    escape = False

    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None
