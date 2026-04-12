from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SourceDocument:
    source_id: str
    path: Path
    title: str | None
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DocumentChunk:
    chunk_id: str
    source_id: str
    source_path: str
    source_name: str
    chunk_index: int
    chunk_count: int
    text: str
    start_char: int
    end_char: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IngestionFileResult:
    source_path: str
    success: bool
    source_id: str | None = None
    title: str | None = None
    chunk_count: int = 0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IngestionSummary:
    collection_name: str
    total_files: int
    succeeded_files: int
    failed_files: int
    total_chunks: int
    results: list[IngestionFileResult] = field(default_factory=list)


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: str
    source_id: str
    source_path: str
    source_name: str
    chunk_index: int
    chunk_count: int
    text: str
    score: float
    rerank_score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class QueryExpansionResult:
    original_query: str
    expanded_query: str
    alternate_queries: list[str] = field(default_factory=list)
    hypothetical_answer: str | None = None
    search_queries: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RetrievalResult:
    query: str
    collection_name: str
    top_k: int
    reranked: bool
    hits: list[RetrievedChunk] = field(default_factory=list)
    query_expansion: QueryExpansionResult | None = None


@dataclass(slots=True)
class RetrievalContext:
    query: str
    context_text: str
    hits: list[RetrievedChunk] = field(default_factory=list)
    query_expansion: QueryExpansionResult | None = None


@dataclass(slots=True)
class IndexedSource:
    source_id: str
    source_path: str
    source_name: str
    chunk_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RAGRuntimeStatus:
    collection_name: str
    qdrant_ready: bool
    embedding_ready: bool
    reranker_ready: bool
    cuda_ready: bool
    messages: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
