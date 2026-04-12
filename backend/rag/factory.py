from __future__ import annotations

from pathlib import Path

from .chunking import OverlappingTextChunker
from .config import RAGConfig, load_rag_config
from .converters import MarkItDownDocumentConverter
from .embedder import SentenceTransformerEmbedder
from .query_expansion import LLMQueryExpander
from .reranker import CrossEncoderReranker
from .service import RAGService
from .store import QdrantVectorStore


def build_rag_service(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
    config: RAGConfig | None = None,
    converter: MarkItDownDocumentConverter | None = None,
    chunker: OverlappingTextChunker | None = None,
    embedder: SentenceTransformerEmbedder | None = None,
    reranker: CrossEncoderReranker | None = None,
    vector_store: QdrantVectorStore | None = None,
) -> RAGService:
    resolved_config = config or load_rag_config(config_path=config_path, env_path=env_path)

    return RAGService(
        config=resolved_config,
        converter=converter or MarkItDownDocumentConverter(),
        chunker=chunker
        or OverlappingTextChunker(
            chunk_size=resolved_config.chunking.chunk_size,
            chunk_overlap=resolved_config.chunking.chunk_overlap,
            min_chunk_chars=resolved_config.chunking.min_chunk_chars,
            separators=resolved_config.chunking.separators,
        ),
        embedder=embedder or SentenceTransformerEmbedder(resolved_config.embedding),
        vector_store=vector_store or QdrantVectorStore(resolved_config.qdrant),
        reranker=reranker
        or (
            CrossEncoderReranker(resolved_config.reranking)
            if resolved_config.reranking.enabled
            else None
        ),
        query_expander=(
            LLMQueryExpander(resolved_config.query_expansion)
            if resolved_config.query_expansion.enabled
            else None
        ),
    )
