from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .config import EmbeddingSettings
from .errors import ModelNotReadyError
from .utils import directory_has_runtime_files


class SentenceTransformerEmbedder:
    """Local embedding adapter backed by a SentenceTransformer model on CUDA."""

    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        self._model: Any | None = None
        self._dimension: int | None = None

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            model = self._ensure_model()
            dimension = model.get_sentence_embedding_dimension()
            if dimension is None:
                raise ModelNotReadyError("The embedding model did not expose an embedding dimension.")
            self._dimension = int(dimension)
        return self._dimension

    def is_configured(self) -> bool:
        return self._model_path_ready()

    def describe_status(self) -> dict[str, Any]:
        model_path = self.settings.model_path
        return {
            "provider": self.settings.provider,
            "device": self.settings.device,
            "model_path": str(model_path),
            "model_path_exists": model_path.exists(),
            "model_path_has_files": self._model_path_ready(),
        }

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        embeddings = model.encode(
            list(texts),
            batch_size=self.settings.batch_size,
            normalize_embeddings=self.settings.normalize_embeddings,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        if not vectors:
            raise ModelNotReadyError("The embedding model returned no query vector.")
        return vectors[0]

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model

        if not self.settings.device.lower().startswith("cuda"):
            raise ModelNotReadyError("Embedding device must use CUDA.")

        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            raise ModelNotReadyError(
                "SentenceTransformer dependencies are missing. Install torch and sentence-transformers."
            ) from exc

        if not torch.cuda.is_available():
            raise ModelNotReadyError("CUDA is not available for the embedding model.")

        model_path = self.settings.model_path
        if not self._model_path_ready():
            raise ModelNotReadyError(
                f"Local embedding model files were not found in {model_path}. "
                "Put the downloaded model directory there before ingesting or querying."
            )

        model = SentenceTransformer(str(model_path), device=self.settings.device)
        if self.settings.max_seq_length is not None:
            model.max_seq_length = self.settings.max_seq_length
        self._model = model
        return model

    def _model_path_ready(self) -> bool:
        model_path = Path(self.settings.model_path)
        return directory_has_runtime_files(model_path)
