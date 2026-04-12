from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.rag.chunking import OverlappingTextChunker
from backend.rag.config import (
    ChunkingSettings,
    EmbeddingSettings,
    IngestionSettings,
    QdrantSettings,
    RAGConfig,
    RerankingSettings,
    RetrievalSettings,
)
from backend.rag.converters import MarkItDownDocumentConverter
from backend.rag.schemas import RetrievedChunk
from backend.rag.service import RAGService
from backend.rag.store import QdrantVectorStore
from backend.rag.utils import deterministic_embedding


class DummyEmbedder:
    def __init__(self, *, dimension: int = 32) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def is_configured(self) -> bool:
        return True

    def describe_status(self) -> dict[str, object]:
        return {"provider": "dummy", "dimension": self._dimension}

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [deterministic_embedding(text, dimension=self._dimension) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return deterministic_embedding(text, dimension=self._dimension)


def main() -> None:
    with TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        docs_dir = temp_root / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        sample_file = docs_dir / "python_notes.md"
        sample_file.write_text(
            "# Python Basics\n\n"
            "Python functions can be passed around like values.\n\n"
            "Decorators wrap a callable and return another callable.\n\n"
            "PyTorch tensors can run on CUDA devices when the model uses CUDA.\n",
            encoding="utf-8",
        )

        config = RAGConfig(
            documents_root=docs_dir,
            qdrant=QdrantSettings(
                mode="local",
                path=temp_root / "qdrant",
                collection_name="smoke_chunks",
                distance="cosine",
                on_disk_payload=True,
            ),
            chunking=ChunkingSettings(chunk_size=120, chunk_overlap=30, min_chunk_chars=40),
            embedding=EmbeddingSettings(model_path=temp_root / "unused_embedding_model", device="cuda"),
            reranking=RerankingSettings(enabled=False, model_path=temp_root / "unused_reranker_model", device="cuda"),
            retrieval=RetrievalSettings(top_k=3, prefetch_k=6, score_threshold=None, max_context_chars=2000),
            ingestion=IngestionSettings(skip_hidden_files=True, supported_extensions=[]),
        )

        service = RAGService(
            config=config,
            converter=MarkItDownDocumentConverter(),
            chunker=OverlappingTextChunker(
                chunk_size=config.chunking.chunk_size,
                chunk_overlap=config.chunking.chunk_overlap,
                min_chunk_chars=config.chunking.min_chunk_chars,
                separators=config.chunking.separators,
            ),
            embedder=DummyEmbedder(),
            vector_store=QdrantVectorStore(config.qdrant),
            reranker=None,
        )

        try:
            summary = service.ingest_paths([sample_file])
            assert summary.succeeded_files == 1, summary
            assert summary.total_chunks >= 2, summary

            result = service.search("How do decorators work in Python?", top_k=2)
            assert result.hits, "Expected at least one retrieval hit."
            top_hit = result.hits[0]
            assert isinstance(top_hit, RetrievedChunk)

            context = service.build_context("How do decorators work in Python?", top_k=2)
            assert "Decorators" in context.context_text or "decorators" in context.context_text

            print("SMOKE TEST PASSED")
            print(f"collection={summary.collection_name}")
            print(f"chunks={summary.total_chunks}")
            print(f"top_hit_source={top_hit.source_name}")
            print(f"top_hit_score={top_hit.score:.4f}")
        finally:
            service.close()


if __name__ == "__main__":
    main()
