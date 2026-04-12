from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .config import RerankingSettings
from .errors import ModelNotReadyError
from .schemas import RetrievedChunk
from .utils import directory_has_runtime_files


class CrossEncoderReranker:
    """Optional reranker backed by a local CrossEncoder model on CUDA."""

    def __init__(self, settings: RerankingSettings) -> None:
        self.settings = settings
        self._model: Any | None = None

    def is_configured(self) -> bool:
        return self._model_path_ready()

    def describe_status(self) -> dict[str, Any]:
        model_path = self.settings.model_path
        return {
            "provider": self.settings.provider,
            "enabled": self.settings.enabled,
            "device": self.settings.device,
            "model_path": str(model_path),
            "model_path_exists": model_path.exists(),
            "model_path_has_files": self._model_path_ready(),
        }

    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        *,
        top_n: int | None = None,
    ) -> list[RetrievedChunk]:
        if not candidates:
            return []

        model = self._ensure_model()
        pairs = [(query, candidate.text) for candidate in candidates]
        scores = model.predict(
            pairs,
            batch_size=self.settings.batch_size,
            show_progress_bar=False,
        )

        ranked: list[RetrievedChunk] = []
        for candidate, score in zip(candidates, scores, strict=True):
            ranked.append(
                RetrievedChunk(
                    chunk_id=candidate.chunk_id,
                    source_id=candidate.source_id,
                    source_path=candidate.source_path,
                    source_name=candidate.source_name,
                    chunk_index=candidate.chunk_index,
                    chunk_count=candidate.chunk_count,
                    text=candidate.text,
                    score=candidate.score,
                    rerank_score=float(score),
                    metadata=dict(candidate.metadata),
                )
            )

        ranked.sort(key=lambda item: item.rerank_score if item.rerank_score is not None else float("-inf"), reverse=True)
        return ranked[: top_n or self.settings.top_n]

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model

        if not self.settings.device.lower().startswith("cuda"):
            raise ModelNotReadyError("Reranker device must use CUDA.")

        try:
            import torch
            from sentence_transformers import CrossEncoder
        except Exception as exc:
            raise ModelNotReadyError(
                "CrossEncoder dependencies are missing. Install torch and sentence-transformers."
            ) from exc

        if not torch.cuda.is_available():
            raise ModelNotReadyError("CUDA is not available for the reranker.")

        model_path = self.settings.model_path
        if not self._model_path_ready():
            raise ModelNotReadyError(
                f"Local reranking model files were not found in {model_path}. "
                "Put the downloaded model directory there before enabling reranking."
            )

        model = CrossEncoder(str(model_path), device=self.settings.device, max_length=self.settings.max_length)
        self._model = model
        return model

    def _model_path_ready(self) -> bool:
        model_path = Path(self.settings.model_path)
        return directory_has_runtime_files(model_path)
