from __future__ import annotations

import atexit
from pathlib import Path
from typing import Any, Mapping

import anyio

from backend.memory import MemoryService, build_memory_service

from ..base import BaseTool
from ..registry import ToolRegistry
from ..schemas import ToolContentItem, ToolDefinition, ToolResult, ToolSource


class _MemoryBaseTool(BaseTool):
    def __init__(self, definition: ToolDefinition, memory_service: MemoryService) -> None:
        super().__init__(definition)
        self._memory_service = memory_service

    async def ainvoke(self, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        normalized_arguments = dict(arguments or {})
        try:
            return await anyio.to_thread.run_sync(self._invoke_sync, normalized_arguments)
        except Exception as exc:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=str(exc),
                content=[ToolContentItem(text=f"{self.name} failed: {exc}")],
                metadata={"tool_name": self.name},
            )

    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        raise NotImplementedError


class MemoryGetStatusTool(_MemoryBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        status = self._memory_service.get_runtime_status()
        summary = (
            f"oracle_configured={status.oracle_configured}, "
            f"oracle_reachable={status.oracle_reachable}, "
            f"schema_ready={status.schema_ready}, "
            f"embedding_ready={status.embedding_ready}, "
            f"llm_ready={status.llm_ready}"
        )
        return ToolResult(
            tool_name=self.name,
            success=status.oracle_configured and status.embedding_ready,
            content=[ToolContentItem(text=summary)],
            structured_content={
                "oracle_configured": status.oracle_configured,
                "oracle_reachable": status.oracle_reachable,
                "schema_ready": status.schema_ready,
                "embedding_ready": status.embedding_ready,
                "llm_ready": status.llm_ready,
                "messages": status.messages,
                "details": status.details,
            },
        )


class MemoryAppendConversationTurnTool(_MemoryBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        session_id = _require_string(arguments, "session_id")
        user_message = _require_string(arguments, "user_message")
        assistant_message = _require_string(arguments, "assistant_message")
        result = self._memory_service.append_conversation_turn(
            session_id,
            user_message=user_message,
            assistant_message=assistant_message,
        )
        text = (
            f"Stored conversation turn for session_id={result.session_id}; "
            f"char_count={result.char_count}; "
            f"unsummarized_char_count={result.unsummarized_char_count}; "
            f"compaction_triggered={result.compaction_triggered}"
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=text)],
            structured_content={
                "session_id": result.session_id,
                "turn_id": result.turn_id,
                "char_count": result.char_count,
                "unsummarized_char_count": result.unsummarized_char_count,
                "compaction_triggered": result.compaction_triggered,
                "created_summary_ids": result.created_summary_ids,
            },
            metadata={"session_id": result.session_id},
        )


class MemoryCompactSessionTool(_MemoryBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        session_id = _require_string(arguments, "session_id")
        force = _optional_bool(arguments.get("force"), default=False)
        result = self._memory_service.compact_session(session_id, force=force)
        text = (
            f"session_id={result.session_id}; compacted={result.compacted}; "
            f"created_summary_count={len(result.created_summary_ids)}; "
            f"remaining_unsummarized_chars={result.remaining_unsummarized_chars}"
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=text)],
            structured_content={
                "session_id": result.session_id,
                "compacted": result.compacted,
                "created_summary_ids": result.created_summary_ids,
                "compacted_turn_ids": result.compacted_turn_ids,
                "remaining_unsummarized_chars": result.remaining_unsummarized_chars,
                "message": result.message,
                "summaries": [
                    {
                        "summary_id": item.summary_id,
                        "summary_title": item.summary_title,
                        "summary_text": item.summary_text,
                        "source_turn_ids": item.source_turn_ids,
                        "source_turn_count": item.source_turn_count,
                        "source_char_count": item.source_char_count,
                    }
                    for item in result.summaries
                ],
            },
            metadata={"session_id": result.session_id},
        )


class MemorySearchSummaryTool(_MemoryBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        session_id = _require_string(arguments, "session_id")
        query = _require_string(arguments, "query")
        top_k = _optional_int(arguments.get("top_k"))
        result = self._memory_service.search_summary(session_id, query, top_k=top_k)
        lines = [
            f"{index}. {hit.summary_title or 'summary'} score={hit.score:.4f} turns={hit.source_turn_count}"
            for index, hit in enumerate(result.hits, start=1)
            if hit.score is not None
        ]
        text = "\n".join(lines) if lines else "No summary memory hits found."
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=text)],
            structured_content={
                "session_id": result.session_id,
                "query": result.query,
                "top_k": result.top_k,
                "hits": [
                    {
                        "summary_id": hit.summary_id,
                        "summary_title": hit.summary_title,
                        "summary_text": hit.summary_text,
                        "source_turn_ids": hit.source_turn_ids,
                        "source_turn_count": hit.source_turn_count,
                        "source_char_count": hit.source_char_count,
                        "score": hit.score,
                        "created_at": hit.created_at,
                        "updated_at": hit.updated_at,
                    }
                    for hit in result.hits
                ],
            },
            metadata={"session_id": result.session_id},
        )


class MemoryBuildSummaryContextTool(_MemoryBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        session_id = _require_string(arguments, "session_id")
        query = _require_string(arguments, "query")
        top_k = _optional_int(arguments.get("top_k"))
        max_context_chars = _optional_int(arguments.get("max_context_chars"))
        result = self._memory_service.build_summary_context(
            session_id,
            query,
            top_k=top_k,
            max_context_chars=max_context_chars,
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=result.context_text or "No summary context built.")],
            structured_content={
                "session_id": result.session_id,
                "query": result.query,
                "context_text": result.context_text,
                "hits": [
                    {
                        "summary_id": hit.summary_id,
                        "summary_title": hit.summary_title,
                        "summary_text": hit.summary_text,
                        "source_turn_ids": hit.source_turn_ids,
                        "source_turn_count": hit.source_turn_count,
                        "source_char_count": hit.source_char_count,
                        "score": hit.score,
                    }
                    for hit in result.hits
                ],
            },
            metadata={"session_id": result.session_id},
        )


class MemoryGetPersonaTool(_MemoryBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        owner_scope = _optional_string(arguments.get("owner_scope"))
        limit = _optional_int(arguments.get("limit"))
        always_include_only = _optional_bool(arguments.get("always_include_only"), default=False)
        result = self._memory_service.get_persona_context(
            owner_scope=owner_scope,
            limit=limit,
            always_include_only=always_include_only,
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=result.context_text or "No persona memory entries found.")],
            structured_content={
                "owner_scope": result.owner_scope,
                "context_text": result.context_text,
                "items": [
                    {
                        "persona_id": item.persona_id,
                        "memory_key": item.memory_key,
                        "memory_value": item.memory_value,
                        "category": item.category,
                        "importance": item.importance,
                        "always_include": item.always_include,
                        "source_note": item.source_note,
                        "created_at": item.created_at,
                        "updated_at": item.updated_at,
                    }
                    for item in result.items
                ],
            },
            metadata={"owner_scope": result.owner_scope},
        )


class MemoryUpsertPersonaTool(_MemoryBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        memory_key = _require_string(arguments, "memory_key")
        memory_value = _require_string(arguments, "memory_value")
        category = _optional_string(arguments.get("category")) or "preference"
        importance = _optional_float(arguments.get("importance"), default=0.6) or 0.6
        always_include = _optional_bool(arguments.get("always_include"), default=True)
        source_note = _optional_string(arguments.get("source_note"))
        owner_scope = _optional_string(arguments.get("owner_scope"))

        result = self._memory_service.upsert_persona_memory(
            memory_key=memory_key,
            memory_value=memory_value,
            category=category,
            importance=importance,
            always_include=always_include,
            source_note=source_note,
            owner_scope=owner_scope,
        )
        text = (
            f"Updated persona memory {result.memory_key} for owner_scope={result.owner_scope}; "
            f"category={result.category}; always_include={result.always_include}; importance={result.importance:.2f}"
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=text)],
            structured_content={
                "persona_id": result.persona_id,
                "owner_scope": result.owner_scope,
                "memory_key": result.memory_key,
                "memory_value": result.memory_value,
                "category": result.category,
                "importance": result.importance,
                "always_include": result.always_include,
                "source_note": result.source_note,
                "created_at": result.created_at,
                "updated_at": result.updated_at,
            },
            metadata={"owner_scope": result.owner_scope},
        )


def build_memory_tools(
    *,
    memory_service: MemoryService | None = None,
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> list[BaseTool]:
    service = memory_service or build_memory_service(config_path=config_path, env_path=env_path)
    atexit.register(_safe_close_memory_service, service)

    return [
        MemoryGetStatusTool(
            ToolDefinition(
                name="memory_get_status",
                description=(
                    "Inspect whether the Oracle-backed memory system is ready. "
                    "Use this to check Oracle connectivity, schema readiness, local embedding model availability, and whether summary compaction can call the configured LLM."
                ),
                input_schema={"type": "object", "properties": {}},
                source=ToolSource.MEMORY,
            ),
            service,
        ),
        MemoryAppendConversationTurnTool(
            ToolDefinition(
                name="memory_append_conversation_turn",
                description=(
                    "Persist one completed conversation turn into session-scoped raw conversation memory. "
                    "This stores the user's message and the agent's final answer exactly as raw memory. "
                    "Use it only when both sides of the turn are already known. If the unsummarized raw conversation tail exceeds the configured character limit, this tool automatically triggers summary compaction."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "The current session id. Always pass the current_session_id from the agent context.",
                        },
                        "user_message": {"type": "string", "description": "The user's completed message for this turn."},
                        "assistant_message": {
                            "type": "string",
                            "description": "The agent's finalized answer for the same turn.",
                        },
                    },
                    "required": ["session_id", "user_message", "assistant_message"],
                },
                source=ToolSource.MEMORY,
            ),
            service,
        ),
        MemoryCompactSessionTool(
            ToolDefinition(
                name="memory_compact_session",
                description=(
                    "Force or trigger compaction of the unsummarized raw conversation tail for one session into summary memory. "
                    "Use this when a session has accumulated enough raw turns that durable compressed memory should be created, or when you want to proactively generate summary memory before searching it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "The current session id whose unsummarized conversation tail should be compacted.",
                        },
                        "force": {
                            "type": "boolean",
                            "description": "When true, compact even if the unsummarized tail is still below the configured character threshold.",
                        },
                    },
                    "required": ["session_id"],
                },
                source=ToolSource.MEMORY,
            ),
            service,
        ),
        MemorySearchSummaryTool(
            ToolDefinition(
                name="memory_search_summary",
                description=(
                    "Run semantic retrieval over summary memory for the current session. "
                    "Use this when the immediate conversation context is not enough and older compressed session history may contain the answer."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "The current session id. Always pass the current_session_id from the agent context.",
                        },
                        "query": {"type": "string", "description": "What you want to recover from older compressed session memory."},
                        "top_k": {"type": "integer", "minimum": 1, "description": "Optional number of summary hits to return."},
                    },
                    "required": ["session_id", "query"],
                },
                source=ToolSource.MEMORY,
            ),
            service,
        ),
        MemoryBuildSummaryContextTool(
            ToolDefinition(
                name="memory_build_summary_context",
                description=(
                    "Retrieve summary memory for the current session and format it into a compact context block for the LLM. "
                    "Use this when you need older compressed session history injected back into the prompt instead of just seeing ranked hits."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "The current session id. Always pass the current_session_id from the agent context.",
                        },
                        "query": {"type": "string", "description": "What older session context you need to recover."},
                        "top_k": {"type": "integer", "minimum": 1},
                        "max_context_chars": {"type": "integer", "minimum": 1},
                    },
                    "required": ["session_id", "query"],
                },
                source=ToolSource.MEMORY,
            ),
            service,
        ),
        MemoryGetPersonaTool(
            ToolDefinition(
                name="memory_get_persona",
                description=(
                    "Load persistent persona memory for the current user scope. "
                    "Use this when stable user preferences, working style, repeated constraints, or identity facts may improve the answer. "
                    "The tool returns a compact context block plus the underlying persona items."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "owner_scope": {
                            "type": "string",
                            "description": "Optional persona owner scope. Omit this in the current single-user setup unless you need a non-default owner scope.",
                        },
                        "limit": {"type": "integer", "minimum": 1},
                        "always_include_only": {
                            "type": "boolean",
                            "description": "When true, return only persona entries marked to always load into context.",
                        },
                    },
                },
                source=ToolSource.MEMORY,
            ),
            service,
        ),
        MemoryUpsertPersonaTool(
            ToolDefinition(
                name="memory_upsert_persona",
                description=(
                    "Insert or update one persistent persona memory entry. "
                    "Use this only when the user clearly states a durable preference, dislike, recurring constraint, identity fact, or working style that should persist across future sessions. "
                    "Do not use it for temporary goals that belong only to the current turn."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "memory_key": {
                            "type": "string",
                            "description": "Stable key for the persona fact, for example preferred_language or code_review_style.",
                        },
                        "memory_value": {"type": "string", "description": "The durable persona value to persist."},
                        "category": {
                            "type": "string",
                            "description": "Optional category such as preference, constraint, identity, or workstyle.",
                        },
                        "importance": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Optional ranking weight used when selecting persona items for context.",
                        },
                        "always_include": {
                            "type": "boolean",
                            "description": "When true, this persona entry should usually be loaded into context automatically.",
                        },
                        "source_note": {
                            "type": "string",
                            "description": "Optional short evidence note describing where this durable persona fact came from.",
                        },
                        "owner_scope": {
                            "type": "string",
                            "description": "Optional owner scope. Omit this in the current single-user setup unless you need a non-default owner scope.",
                        },
                    },
                    "required": ["memory_key", "memory_value"],
                },
                source=ToolSource.MEMORY,
            ),
            service,
        ),
    ]


def register_memory_tools(
    registry: ToolRegistry,
    *,
    memory_service: MemoryService | None = None,
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
    overwrite: bool = False,
) -> list[BaseTool]:
    tools = build_memory_tools(memory_service=memory_service, config_path=config_path, env_path=env_path)
    registry.register_many(tools, overwrite=overwrite)
    return tools


def _require_string(arguments: Mapping[str, Any], key: str) -> str:
    value = str(arguments.get(key, "")).strip()
    if not value:
        raise ValueError(f"Missing required string argument: {key}")
    return value


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _optional_float(value: Any, *, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    return float(value)


def _optional_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_close_memory_service(service: MemoryService) -> None:
    try:
        service.close()
    except Exception:
        pass
