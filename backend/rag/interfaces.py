from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, Sequence

from .schemas import DocumentChunk, IndexedSource, RetrievedChunk, SourceDocument


class DocumentConverter(Protocol):
    def convert(self, path: str | Path) -> SourceDocument:
        ...


class TextChunker(Protocol):
    def chunk(self, document: SourceDocument) -> list[DocumentChunk]:
        ...


class TextEmbedder(Protocol):
    @property
    def dimension(self) -> int:
        ...

    def is_configured(self) -> bool:
        ...

    def describe_status(self) -> dict[str, Any]:
        ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        ...

    def embed_query(self, text: str) -> list[float]:
        ...


class TextReranker(Protocol):
    def is_configured(self) -> bool:
        ...

    def describe_status(self) -> dict[str, Any]:
        ...

    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        *,
        top_n: int | None = None,
    ) -> list[RetrievedChunk]:
        ...


class VectorStore(Protocol):
    def collection_name(self) -> str:
        ...

    def is_ready(self) -> bool:
        ...

    def describe_status(self) -> dict[str, Any]:
        ...

    def ensure_collection(self, vector_size: int) -> None:
        ...

    def upsert_chunks(self, chunks: Sequence[DocumentChunk], vectors: Sequence[Sequence[float]]) -> None:
        ...

    def search(
        self,
        query_vector: Sequence[float],
        *,
        limit: int,
        score_threshold: float | None = None,
        source_ids: Sequence[str] | None = None,
        source_paths: Sequence[str] | None = None,
    ) -> list[RetrievedChunk]:
        ...

    def delete_sources(
        self,
        *,
        source_ids: Sequence[str] | None = None,
        source_paths: Sequence[str] | None = None,
    ) -> None:
        ...

    def list_indexed_sources(self, *, limit: int = 256) -> list[IndexedSource]:
        ...

    def close(self) -> None:
        ...
