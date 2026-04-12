from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv

from .errors import RAGConfigError

DistanceType = Literal["cosine", "dot", "euclid"]
QdrantMode = Literal["local", "remote"]

PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "rag.json"
DEFAULT_SEPARATORS = ["\n\n", "\n", "。", ". ", "！", "! ", "？", "? ", "；", "; ", "，", ", ", " "]


@dataclass(slots=True)
class QdrantSettings:
    mode: QdrantMode = "local"
    path: Path | None = None
    url: str | None = None
    api_key: str | None = None
    collection_name: str = "knowledge_chunks"
    distance: DistanceType = "cosine"
    on_disk_payload: bool = True


@dataclass(slots=True)
class ChunkingSettings:
    chunk_size: int = 1200
    chunk_overlap: int = 240
    min_chunk_chars: int = 120
    separators: list[str] = field(default_factory=lambda: list(DEFAULT_SEPARATORS))


@dataclass(slots=True)
class EmbeddingSettings:
    provider: str = "sentence_transformers"
    model_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "models" / "embedding")
    device: str = "cuda"
    batch_size: int = 16
    normalize_embeddings: bool = True
    max_seq_length: int | None = 512


@dataclass(slots=True)
class RerankingSettings:
    enabled: bool = False
    provider: str = "cross_encoder"
    model_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "models" / "reranker")
    device: str = "cuda"
    top_n: int = 5
    batch_size: int = 8
    max_length: int | None = 512


@dataclass(slots=True)
class RetrievalSettings:
    top_k: int = 8
    prefetch_k: int = 24
    score_threshold: float | None = None
    max_context_chars: int = 12000


@dataclass(slots=True)
class QueryExpansionSettings:
    enabled: bool = True
    llm_config_path: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1] / "config" / "llm.json")
    prompt_name: str = "query_expansion"
    max_alternate_queries: int = 2
    include_hypothetical_answer: bool = True


@dataclass(slots=True)
class IngestionSettings:
    skip_hidden_files: bool = True
    supported_extensions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RAGConfig:
    enabled: bool = True
    documents_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2] / "raw_data")
    qdrant: QdrantSettings = field(default_factory=QdrantSettings)
    chunking: ChunkingSettings = field(default_factory=ChunkingSettings)
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    reranking: RerankingSettings = field(default_factory=RerankingSettings)
    retrieval: RetrievalSettings = field(default_factory=RetrievalSettings)
    query_expansion: QueryExpansionSettings = field(default_factory=QueryExpansionSettings)
    ingestion: IngestionSettings = field(default_factory=IngestionSettings)
    config_path: Path | None = None
    env_path: Path | None = None


def find_env_file(start: str | Path | None = None) -> Path | None:
    current_path = Path(start).resolve() if start is not None else Path(__file__).resolve()
    current_dir = current_path if current_path.is_dir() else current_path.parent

    for candidate_dir in (current_dir, *current_dir.parents):
        candidate = candidate_dir / ".env"
        if candidate.exists():
            return candidate

    return None


def find_rag_config_file(start: str | Path | None = None) -> Path | None:
    current_path = Path(start).resolve() if start is not None else Path(__file__).resolve()
    current_dir = current_path if current_path.is_dir() else current_path.parent

    for candidate_dir in (current_dir, *current_dir.parents):
        for candidate in (
            candidate_dir / "config" / "rag.json",
            candidate_dir / "backend" / "config" / "rag.json",
        ):
            if candidate.exists():
                return candidate

    return None


def load_rag_json_config(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> dict[str, Any]:
    resolved_env_path = _resolve_env_path(env_path)
    if resolved_env_path is not None:
        load_dotenv(dotenv_path=resolved_env_path, override=False)

    resolved_config_path = _resolve_config_path(config_path)
    with resolved_config_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    project_root = _infer_project_root(resolved_config_path, resolved_env_path)
    context = _build_template_context(project_root)
    resolved_payload = _resolve_template_variables(payload, context)
    resolved_payload["_resolved_config_path"] = str(resolved_config_path)
    if resolved_env_path is not None:
        resolved_payload["_resolved_env_path"] = str(resolved_env_path)
    return resolved_payload


def load_rag_config(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> RAGConfig:
    payload = load_rag_json_config(config_path=config_path, env_path=env_path)
    if not payload.get("enabled", True):
        raise RAGConfigError("RAG configuration is disabled.")

    qdrant_payload = _coerce_mapping(payload.get("qdrant"))
    chunking_payload = _coerce_mapping(payload.get("chunking"))
    embedding_payload = _coerce_mapping(payload.get("embedding"))
    reranking_payload = _coerce_mapping(payload.get("reranking"))
    retrieval_payload = _coerce_mapping(payload.get("retrieval"))
    query_expansion_payload = _coerce_mapping(payload.get("query_expansion"))
    ingestion_payload = _coerce_mapping(payload.get("ingestion"))

    config = RAGConfig(
        enabled=bool(payload.get("enabled", True)),
        documents_root=_coerce_path(payload.get("documents_root"))
        or Path(__file__).resolve().parents[2] / "raw_data",
        qdrant=QdrantSettings(
            mode=_coerce_qdrant_mode(qdrant_payload.get("mode"), default="local"),
            path=_coerce_path(qdrant_payload.get("path")),
            url=_normalize_optional_string(qdrant_payload.get("url")),
            api_key=_normalize_optional_string(qdrant_payload.get("api_key")),
            collection_name=_coerce_required_string(qdrant_payload.get("collection_name"), "qdrant.collection_name"),
            distance=_coerce_distance(qdrant_payload.get("distance"), default="cosine"),
            on_disk_payload=bool(qdrant_payload.get("on_disk_payload", True)),
        ),
        chunking=ChunkingSettings(
            chunk_size=_coerce_positive_int(chunking_payload.get("chunk_size"), default=1200, field_name="chunking.chunk_size"),
            chunk_overlap=_coerce_non_negative_int(
                chunking_payload.get("chunk_overlap"),
                default=240,
                field_name="chunking.chunk_overlap",
            ),
            min_chunk_chars=_coerce_positive_int(
                chunking_payload.get("min_chunk_chars"),
                default=120,
                field_name="chunking.min_chunk_chars",
            ),
            separators=_coerce_string_list(chunking_payload.get("separators")) or list(DEFAULT_SEPARATORS),
        ),
        embedding=EmbeddingSettings(
            provider=_coerce_required_string(embedding_payload.get("provider"), "embedding.provider"),
            model_path=_coerce_path(embedding_payload.get("model_path"))
            or Path(__file__).resolve().parent / "models" / "embedding",
            device=_coerce_required_string(embedding_payload.get("device"), "embedding.device"),
            batch_size=_coerce_positive_int(embedding_payload.get("batch_size"), default=16, field_name="embedding.batch_size"),
            normalize_embeddings=bool(embedding_payload.get("normalize_embeddings", True)),
            max_seq_length=_coerce_optional_positive_int(embedding_payload.get("max_seq_length"), field_name="embedding.max_seq_length"),
        ),
        reranking=RerankingSettings(
            enabled=bool(reranking_payload.get("enabled", False)),
            provider=_coerce_required_string(reranking_payload.get("provider"), "reranking.provider"),
            model_path=_coerce_path(reranking_payload.get("model_path"))
            or Path(__file__).resolve().parent / "models" / "reranker",
            device=_coerce_required_string(reranking_payload.get("device"), "reranking.device"),
            top_n=_coerce_positive_int(reranking_payload.get("top_n"), default=5, field_name="reranking.top_n"),
            batch_size=_coerce_positive_int(reranking_payload.get("batch_size"), default=8, field_name="reranking.batch_size"),
            max_length=_coerce_optional_positive_int(reranking_payload.get("max_length"), field_name="reranking.max_length"),
        ),
        retrieval=RetrievalSettings(
            top_k=_coerce_positive_int(retrieval_payload.get("top_k"), default=8, field_name="retrieval.top_k"),
            prefetch_k=_coerce_positive_int(retrieval_payload.get("prefetch_k"), default=24, field_name="retrieval.prefetch_k"),
            score_threshold=_coerce_optional_float(retrieval_payload.get("score_threshold"), field_name="retrieval.score_threshold"),
            max_context_chars=_coerce_positive_int(
                retrieval_payload.get("max_context_chars"),
                default=12000,
                field_name="retrieval.max_context_chars",
            ),
        ),
        query_expansion=QueryExpansionSettings(
            enabled=bool(query_expansion_payload.get("enabled", True)),
            llm_config_path=_coerce_path(query_expansion_payload.get("llm_config_path"))
            or Path(__file__).resolve().parents[1] / "config" / "llm.json",
            prompt_name=_coerce_required_string(
                query_expansion_payload.get("prompt_name") or "query_expansion",
                "query_expansion.prompt_name",
            ),
            max_alternate_queries=_coerce_non_negative_int(
                query_expansion_payload.get("max_alternate_queries"),
                default=2,
                field_name="query_expansion.max_alternate_queries",
            ),
            include_hypothetical_answer=bool(query_expansion_payload.get("include_hypothetical_answer", True)),
        ),
        ingestion=IngestionSettings(
            skip_hidden_files=bool(ingestion_payload.get("skip_hidden_files", True)),
            supported_extensions=_normalize_extensions(_coerce_string_list(ingestion_payload.get("supported_extensions"))),
        ),
        config_path=Path(payload["_resolved_config_path"]).resolve() if payload.get("_resolved_config_path") else None,
        env_path=Path(payload["_resolved_env_path"]).resolve() if payload.get("_resolved_env_path") else None,
    )
    _validate_rag_config(config)
    return config


def _validate_rag_config(config: RAGConfig) -> None:
    if config.chunking.chunk_overlap >= config.chunking.chunk_size:
        raise RAGConfigError("chunking.chunk_overlap must be smaller than chunking.chunk_size.")
    if not config.embedding.device.lower().startswith("cuda"):
        raise RAGConfigError("embedding.device must use CUDA for this project.")
    if config.reranking.enabled and not config.reranking.device.lower().startswith("cuda"):
        raise RAGConfigError("reranking.device must use CUDA when reranking is enabled.")
    if config.qdrant.mode == "local":
        if config.qdrant.path is None:
            raise RAGConfigError("qdrant.path is required when qdrant.mode is 'local'.")
    elif not config.qdrant.url:
        raise RAGConfigError("qdrant.url is required when qdrant.mode is 'remote'.")


def _resolve_env_path(env_path: str | Path | None) -> Path | None:
    if env_path is None:
        return find_env_file()

    candidate = Path(env_path).resolve()
    if not candidate.exists():
        raise RAGConfigError(f"Env file not found: {candidate}")
    return candidate


def _resolve_config_path(config_path: str | Path | None) -> Path:
    if config_path is None:
        discovered = find_rag_config_file()
        if discovered is not None:
            return discovered
        if DEFAULT_CONFIG_PATH.exists():
            return DEFAULT_CONFIG_PATH.resolve()
        raise RAGConfigError("RAG config file not found. Expected backend/config/rag.json.")

    candidate = Path(config_path).resolve()
    if not candidate.exists():
        raise RAGConfigError(f"RAG config file not found: {candidate}")
    return candidate


def _infer_project_root(config_path: Path, env_path: Path | None) -> Path:
    if env_path is not None:
        return env_path.parent.resolve()

    for candidate in (config_path.parent, *config_path.parents):
        if (candidate / "backend").is_dir():
            return candidate.resolve()

    return config_path.parent.resolve()


def _build_template_context(project_root: Path) -> dict[str, str]:
    context = {key: value for key, value in os.environ.items()}
    context.update(
        {
            "PROJECT_ROOT": str(project_root),
            "BACKEND_ROOT": str(project_root / "backend"),
            "PYTHON_EXECUTABLE": sys.executable,
        }
    )
    return context


def _resolve_template_variables(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, str):
        return PLACEHOLDER_PATTERN.sub(lambda match: context.get(match.group(1), ""), value)
    if isinstance(value, list):
        return [_resolve_template_variables(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_template_variables(item, context) for key, item in value.items()}
    return value


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RAGConfigError(f"Expected a JSON object, got {type(value).__name__}.")
    return value


def _coerce_required_string(value: Any, field_name: str) -> str:
    normalized = _normalize_optional_string(value)
    if normalized is None:
        raise RAGConfigError(f"Missing required config field: {field_name}")
    return normalized


def _normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _coerce_qdrant_mode(value: Any, *, default: QdrantMode) -> QdrantMode:
    normalized = (_normalize_optional_string(value) or default).lower()
    if normalized not in {"local", "remote"}:
        raise RAGConfigError("qdrant.mode must be 'local' or 'remote'.")
    return normalized  # type: ignore[return-value]


def _coerce_distance(value: Any, *, default: DistanceType) -> DistanceType:
    normalized = (_normalize_optional_string(value) or default).lower()
    if normalized not in {"cosine", "dot", "euclid"}:
        raise RAGConfigError("qdrant.distance must be one of: cosine, dot, euclid.")
    return normalized  # type: ignore[return-value]


def _coerce_path(value: Any) -> Path | None:
    normalized = _normalize_optional_string(value)
    if normalized is None:
        return None
    return Path(normalized).resolve()


def _coerce_positive_int(value: Any, *, default: int, field_name: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RAGConfigError(f"Expected integer for {field_name}, got {value!r}") from exc
    if parsed <= 0:
        raise RAGConfigError(f"{field_name} must be > 0.")
    return parsed


def _coerce_non_negative_int(value: Any, *, default: int, field_name: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RAGConfigError(f"Expected integer for {field_name}, got {value!r}") from exc
    if parsed < 0:
        raise RAGConfigError(f"{field_name} must be >= 0.")
    return parsed


def _coerce_optional_positive_int(value: Any, *, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    return _coerce_positive_int(value, default=1, field_name=field_name)


def _coerce_optional_float(value: Any, *, field_name: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RAGConfigError(f"Expected float for {field_name}, got {value!r}") from exc


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RAGConfigError(f"Expected a JSON array of strings, got {type(value).__name__}.")
    result: list[str] = []
    for item in value:
        normalized = _normalize_optional_string(item)
        if normalized is not None:
            result.append(normalized)
    return result


def _normalize_extensions(extensions: list[str]) -> list[str]:
    normalized: list[str] = []
    for extension in extensions:
        lowered = extension.lower()
        if not lowered.startswith("."):
            lowered = f".{lowered}"
        normalized.append(lowered)
    return normalized
