from __future__ import annotations

from pathlib import Path
from typing import Sequence

import torch

from .config import RAGConfig
from .errors import DocumentConversionError
from .interfaces import DocumentConverter, TextChunker, TextEmbedder, TextReranker, VectorStore
from .query_expansion import LLMQueryExpander
from .schemas import (
    IngestionFileResult,
    IngestionSummary,
    QueryExpansionResult,
    RAGRuntimeStatus,
    RetrievalContext,
    RetrievalResult,
    RetrievedChunk,
)
from .utils import is_hidden_path, stable_source_id


class RAGService:
    """High-level entry point for ingestion and retrieval."""

    def __init__(
        self,
        *,
        config: RAGConfig,
        converter: DocumentConverter,
        chunker: TextChunker,
        embedder: TextEmbedder,
        vector_store: VectorStore,
        reranker: TextReranker | None = None,
        query_expander: LLMQueryExpander | None = None,
    ) -> None:
        self.config = config
        self.converter = converter
        self.chunker = chunker
        self.embedder = embedder
        self.vector_store = vector_store
        self.reranker = reranker
        self.query_expander = query_expander

    def get_runtime_status(self) -> RAGRuntimeStatus:
        messages: list[str] = []
        embedding_ready = self.embedder.is_configured()
        reranker_ready = self.reranker is None or self.reranker.is_configured()
        qdrant_ready = self.vector_store.is_ready()
        cuda_ready = torch.cuda.is_available()

        if not embedding_ready:
            messages.append(
                f"Place a local embedding model inside {self.config.embedding.model_path} before ingesting or querying."
            )
        if self.config.reranking.enabled and self.reranker is not None and not self.reranker.is_configured():
            messages.append(
                f"Place a local reranking model inside {self.config.reranking.model_path} before enabling reranking."
            )
        if not cuda_ready:
            messages.append("CUDA is not available. This RAG module is configured to require CUDA.")

        return RAGRuntimeStatus(
            collection_name=self.config.qdrant.collection_name,
            qdrant_ready=qdrant_ready,
            embedding_ready=embedding_ready,
            reranker_ready=reranker_ready,
            cuda_ready=cuda_ready,
            messages=messages,
            details={
                "config_path": str(self.config.config_path) if self.config.config_path else None,
                "documents_root": str(self.config.documents_root),
                "vector_store": self.vector_store.describe_status(),
                "embedding": self.embedder.describe_status(),
                "reranker": self.reranker.describe_status() if self.reranker is not None else None,
            },
        )

    def ingest_paths(self, paths: Sequence[str | Path]) -> IngestionSummary:
        file_paths = self._collect_files(paths)
        results: list[IngestionFileResult] = []
        prepared_chunks = []

        for file_path in file_paths:
            try:
                document = self.converter.convert(file_path)
                chunks = self.chunker.chunk(document)
                if not chunks:
                    raise DocumentConversionError(f"No chunks were produced for {file_path}")
                prepared_chunks.extend(chunks)
                results.append(
                    IngestionFileResult(
                        source_path=str(document.path),
                        success=True,
                        source_id=document.source_id,
                        title=document.title,
                        chunk_count=len(chunks),
                        metadata={"source_name": document.path.name},
                    )
                )
            except Exception as exc:
                results.append(
                    IngestionFileResult(
                        source_path=str(Path(file_path).resolve()),
                        success=False,
                        error=str(exc),
                    )
                )

        successful_source_ids = [item.source_id for item in results if item.success and item.source_id]
        if prepared_chunks:
            self.vector_store.ensure_collection(self.embedder.dimension)
            self.vector_store.delete_sources(source_ids=successful_source_ids)
            vectors = self.embedder.embed_documents([chunk.text for chunk in prepared_chunks])
            self.vector_store.upsert_chunks(prepared_chunks, vectors)

        succeeded_files = sum(1 for result in results if result.success)
        failed_files = len(results) - succeeded_files
        total_chunks = sum(result.chunk_count for result in results)

        return IngestionSummary(
            collection_name=self.config.qdrant.collection_name,
            total_files=len(results),
            succeeded_files=succeeded_files,
            failed_files=failed_files,
            total_chunks=total_chunks,
            results=results,
        )

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        source_paths: Sequence[str | Path] | None = None,
        source_ids: Sequence[str] | None = None,
    ) -> RetrievalResult:
        normalized_top_k = top_k or self.config.retrieval.top_k
        normalized_paths = [str(Path(path).resolve()) for path in (source_paths or [])]
        search_limit = normalized_top_k
        reranked = False
        query_expansion = self._expand_query(query)
        search_queries = query_expansion.search_queries if query_expansion is not None else [query]

        if self.config.reranking.enabled and self.reranker is not None and self.reranker.is_configured():
            search_limit = max(normalized_top_k, self.config.retrieval.prefetch_k)

        hits = self._search_across_queries(
            search_queries,
            limit=search_limit,
            score_threshold=self.config.retrieval.score_threshold,
            source_ids=source_ids,
            source_paths=normalized_paths,
        )

        if self.config.reranking.enabled and self.reranker is not None and self.reranker.is_configured():
            hits = self.reranker.rerank(query, hits, top_n=normalized_top_k)
            reranked = True
        else:
            hits = hits[:normalized_top_k]

        return RetrievalResult(
            query=query,
            collection_name=self.config.qdrant.collection_name,
            top_k=normalized_top_k,
            reranked=reranked,
            hits=hits,
            query_expansion=query_expansion,
        )

    def build_context(
        self,
        query: str,
        *,
        top_k: int | None = None,
        source_paths: Sequence[str | Path] | None = None,
        source_ids: Sequence[str] | None = None,
        max_context_chars: int | None = None,
    ) -> RetrievalContext:
        retrieval_result = self.search(
            query,
            top_k=top_k,
            source_paths=source_paths,
            source_ids=source_ids,
        )

        budget = max_context_chars or self.config.retrieval.max_context_chars
        rendered_parts: list[str] = []
        kept_hits = []
        used_chars = 0

        for index, hit in enumerate(retrieval_result.hits, start=1):
            header = f"[{index}] {hit.source_name} chunk {hit.chunk_index + 1}/{hit.chunk_count}"
            body = hit.text.strip()
            segment = f"{header}\n{body}".strip()
            if not segment:
                continue
            if used_chars and used_chars + 5 >= budget:
                break
            remaining = budget - used_chars
            if len(segment) > remaining:
                segment = segment[: max(remaining - 16, 0)].rstrip()
                if not segment:
                    break
                segment = f"{segment}\n[truncated]"
            rendered_parts.append(segment)
            kept_hits.append(hit)
            used_chars += len(segment) + 5
            if used_chars >= budget:
                break

        context_text = "\n\n---\n\n".join(rendered_parts)
        return RetrievalContext(
            query=query,
            context_text=context_text,
            hits=kept_hits,
            query_expansion=retrieval_result.query_expansion,
        )

    def list_indexed_sources(self, *, limit: int = 256):
        return self.vector_store.list_indexed_sources(limit=limit)

    def delete_paths(self, paths: Sequence[str | Path]) -> None:
        source_ids = [stable_source_id(Path(path).resolve()) for path in paths]
        source_paths = [str(Path(path).resolve()) for path in paths]
        self.vector_store.delete_sources(source_ids=source_ids, source_paths=source_paths)

    def close(self) -> None:
        self.vector_store.close()

    def _expand_query(self, query: str) -> QueryExpansionResult | None:
        if self.query_expander is None or not self.query_expander.is_enabled():
            return None
        return self.query_expander.expand(query)

    def _search_across_queries(
        self,
        queries: Sequence[str],
        *,
        limit: int,
        score_threshold: float | None,
        source_ids: Sequence[str] | None,
        source_paths: Sequence[str] | None,
    ) -> list[RetrievedChunk]:
        merged_hits: dict[str, RetrievedChunk] = {}
        for candidate_query in queries:
            query_vector = self.embedder.embed_query(candidate_query)
            hits = self.vector_store.search(
                query_vector,
                limit=limit,
                score_threshold=score_threshold,
                source_ids=source_ids,
                source_paths=source_paths,
            )
            for hit in hits:
                existing = merged_hits.get(hit.chunk_id)
                if existing is None or hit.score > existing.score:
                    merged_hits[hit.chunk_id] = hit

        return sorted(merged_hits.values(), key=lambda item: item.score, reverse=True)

    def _collect_files(self, paths: Sequence[str | Path]) -> list[Path]:
        resolved_files: list[Path] = []
        seen: set[Path] = set()

        for raw_path in paths:
            path = Path(raw_path).resolve()
            if not path.exists():
                raise FileNotFoundError(f"Input path does not exist: {path}")
            if path.is_dir():
                for nested in path.rglob("*"):
                    if nested.is_file():
                        self._try_append_path(nested, resolved_files, seen)
                continue
            self._try_append_path(path, resolved_files, seen)

        return resolved_files

    def _try_append_path(self, path: Path, resolved_files: list[Path], seen: set[Path]) -> None:
        if self.config.ingestion.skip_hidden_files and is_hidden_path(path):
            return
        if self.config.ingestion.supported_extensions and path.suffix.lower() not in self.config.ingestion.supported_extensions:
            return
        if path not in seen:
            seen.add(path)
            resolved_files.append(path)
