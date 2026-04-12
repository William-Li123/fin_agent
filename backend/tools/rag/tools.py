from __future__ import annotations

import atexit
from pathlib import Path
from typing import Any, Mapping

import anyio

from backend.rag import RAGService, build_rag_service

from ..base import BaseTool
from ..registry import ToolRegistry
from ..schemas import ToolContentItem, ToolDefinition, ToolResult, ToolSource


class _RAGBaseTool(BaseTool):
    def __init__(self, definition: ToolDefinition, rag_service: RAGService) -> None:
        super().__init__(definition)
        self._rag_service = rag_service

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


class RAGGetStatusTool(_RAGBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        status = self._rag_service.get_runtime_status()
        summary = (
            f"collection={status.collection_name}, "
            f"qdrant_ready={status.qdrant_ready}, "
            f"embedding_ready={status.embedding_ready}, "
            f"reranker_ready={status.reranker_ready}, "
            f"cuda_ready={status.cuda_ready}"
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=summary)],
            structured_content={
                "collection_name": status.collection_name,
                "qdrant_ready": status.qdrant_ready,
                "embedding_ready": status.embedding_ready,
                "reranker_ready": status.reranker_ready,
                "cuda_ready": status.cuda_ready,
                "messages": status.messages,
                "details": status.details,
            },
            metadata={"collection_name": status.collection_name},
        )


class RAGIngestPathsTool(_RAGBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        paths = _normalize_path_list(arguments.get("paths"))
        summary = self._rag_service.ingest_paths(paths)
        text = (
            f"Ingested {summary.succeeded_files}/{summary.total_files} files into "
            f"{summary.collection_name}; total_chunks={summary.total_chunks}, failed_files={summary.failed_files}."
        )
        return ToolResult(
            tool_name=self.name,
            success=summary.failed_files == 0,
            content=[ToolContentItem(text=text)],
            structured_content={
                "collection_name": summary.collection_name,
                "total_files": summary.total_files,
                "succeeded_files": summary.succeeded_files,
                "failed_files": summary.failed_files,
                "total_chunks": summary.total_chunks,
                "results": [
                    {
                        "source_path": result.source_path,
                        "success": result.success,
                        "source_id": result.source_id,
                        "title": result.title,
                        "chunk_count": result.chunk_count,
                        "error": result.error,
                        "metadata": result.metadata,
                    }
                    for result in summary.results
                ],
            },
            metadata={"collection_name": summary.collection_name},
        )


class RAGSearchTool(_RAGBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        query = _require_string(arguments, "query")
        top_k = _optional_int(arguments.get("top_k"))
        source_paths = _normalize_path_list(arguments.get("source_paths"))
        source_ids = _normalize_string_list(arguments.get("source_ids"))
        result = self._rag_service.search(query, top_k=top_k, source_paths=source_paths, source_ids=source_ids)
        lines = [
            f"{index}. {hit.source_name} chunk {hit.chunk_index + 1}/{hit.chunk_count} score={hit.score:.4f}"
            for index, hit in enumerate(result.hits, start=1)
        ]
        text = "\n".join(lines) if lines else "No retrieval hits found."
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=text)],
            structured_content={
                "query": result.query,
                "collection_name": result.collection_name,
                "top_k": result.top_k,
                "reranked": result.reranked,
                "query_expansion": _serialize_query_expansion(result.query_expansion),
                "hits": [
                    {
                        "chunk_id": hit.chunk_id,
                        "source_id": hit.source_id,
                        "source_path": hit.source_path,
                        "source_name": hit.source_name,
                        "chunk_index": hit.chunk_index,
                        "chunk_count": hit.chunk_count,
                        "text": hit.text,
                        "score": hit.score,
                        "rerank_score": hit.rerank_score,
                        "metadata": hit.metadata,
                    }
                    for hit in result.hits
                ],
            },
            metadata={"collection_name": result.collection_name},
        )


class RAGBuildContextTool(_RAGBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        query = _require_string(arguments, "query")
        top_k = _optional_int(arguments.get("top_k"))
        max_context_chars = _optional_int(arguments.get("max_context_chars"))
        source_paths = _normalize_path_list(arguments.get("source_paths"))
        source_ids = _normalize_string_list(arguments.get("source_ids"))
        result = self._rag_service.build_context(
            query,
            top_k=top_k,
            max_context_chars=max_context_chars,
            source_paths=source_paths,
            source_ids=source_ids,
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=result.context_text or "No retrieval context built.")],
            structured_content={
                "query": result.query,
                "context_text": result.context_text,
                "query_expansion": _serialize_query_expansion(result.query_expansion),
                "hits": [
                    {
                        "chunk_id": hit.chunk_id,
                        "source_id": hit.source_id,
                        "source_path": hit.source_path,
                        "source_name": hit.source_name,
                        "chunk_index": hit.chunk_index,
                        "chunk_count": hit.chunk_count,
                        "text": hit.text,
                        "score": hit.score,
                        "rerank_score": hit.rerank_score,
                        "metadata": hit.metadata,
                    }
                    for hit in result.hits
                ],
            },
        )


class RAGListIndexedSourcesTool(_RAGBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        limit = _optional_int(arguments.get("limit")) or 256
        sources = self._rag_service.list_indexed_sources(limit=limit)
        lines = [f"{source.source_name} chunks={source.chunk_count}" for source in sources]
        text = "\n".join(lines) if lines else "No indexed sources found."
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=text)],
            structured_content={
                "sources": [
                    {
                        "source_id": source.source_id,
                        "source_path": source.source_path,
                        "source_name": source.source_name,
                        "chunk_count": source.chunk_count,
                        "metadata": source.metadata,
                    }
                    for source in sources
                ]
            },
        )


class RAGDeletePathsTool(_RAGBaseTool):
    def _invoke_sync(self, arguments: dict[str, Any]) -> ToolResult:
        paths = _normalize_path_list(arguments.get("paths"))
        self._rag_service.delete_paths(paths)
        text = f"Deleted indexed entries for {len(paths)} paths."
        return ToolResult(
            tool_name=self.name,
            success=True,
            content=[ToolContentItem(text=text)],
            structured_content={"deleted_paths": paths},
        )


def build_rag_tools(
    *,
    rag_service: RAGService | None = None,
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> list[BaseTool]:
    service = rag_service or build_rag_service(config_path=config_path, env_path=env_path)
    atexit.register(_safe_close_rag_service, service)

    return [
        RAGGetStatusTool(
            ToolDefinition(
                name="rag_get_status",
                description="Inspect whether the local RAG system is ready, including Qdrant, CUDA, and local model availability.",
                input_schema={"type": "object", "properties": {}},
                source=ToolSource.RAG,
            ),
            service,
        ),
        RAGIngestPathsTool(
            ToolDefinition(
                name="rag_ingest_paths",
                description="Ingest selected local files or directories into the RAG index.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Absolute or project-relative file or directory paths to ingest.",
                        }
                    },
                    "required": ["paths"],
                },
                source=ToolSource.RAG,
            ),
            service,
        ),
        RAGSearchTool(
            ToolDefinition(
                name="rag_search",
                description="Search indexed knowledge chunks and return ranked retrieval hits.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1},
                        "source_paths": {"type": "array", "items": {"type": "string"}},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["query"],
                },
                source=ToolSource.RAG,
            ),
            service,
        ),
        RAGBuildContextTool(
            ToolDefinition(
                name="rag_build_context",
                description="Retrieve knowledge and format it into a compact context block ready to pass into the LLM.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1},
                        "max_context_chars": {"type": "integer", "minimum": 1},
                        "source_paths": {"type": "array", "items": {"type": "string"}},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["query"],
                },
                source=ToolSource.RAG,
            ),
            service,
        ),
        RAGListIndexedSourcesTool(
            ToolDefinition(
                name="rag_list_indexed_sources",
                description="List which source files are already indexed in the current RAG collection.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1}
                    },
                },
                source=ToolSource.RAG,
            ),
            service,
        ),
        RAGDeletePathsTool(
            ToolDefinition(
                name="rag_delete_paths",
                description="Delete indexed entries for selected file paths from the current RAG collection.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Absolute or project-relative file paths to remove from the index.",
                        }
                    },
                    "required": ["paths"],
                },
                source=ToolSource.RAG,
            ),
            service,
        ),
    ]


def register_rag_tools(
    registry: ToolRegistry,
    *,
    rag_service: RAGService | None = None,
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
    overwrite: bool = False,
) -> list[BaseTool]:
    tools = build_rag_tools(rag_service=rag_service, config_path=config_path, env_path=env_path)
    registry.register_many(tools, overwrite=overwrite)
    return tools


def _normalize_path_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Expected a list of paths.")
    return [str(Path(item).resolve()) for item in value if str(item).strip()]


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Expected a list of strings.")
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _require_string(arguments: Mapping[str, Any], key: str) -> str:
    value = str(arguments.get(key, "")).strip()
    if not value:
        raise ValueError(f"Missing required string argument: {key}")
    return value


def _safe_close_rag_service(service: RAGService) -> None:
    try:
        service.close()
    except Exception:
        pass


def _serialize_query_expansion(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "original_query": value.original_query,
        "expanded_query": value.expanded_query,
        "alternate_queries": list(value.alternate_queries),
        "hypothetical_answer": value.hypothetical_answer,
        "search_queries": list(value.search_queries),
    }
