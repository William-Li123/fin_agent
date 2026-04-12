from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv

from .errors import MemoryConfigError

PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "memory.json"

DistanceType = Literal["COSINE", "DOT", "EUCLIDEAN"]


@dataclass(slots=True)
class OracleSettings:
    user: str | None = None
    password: str | None = None
    dsn: str | None = None
    min_pool_size: int = 1
    max_pool_size: int = 4
    pool_increment: int = 1
    ping_interval_seconds: int = 60
    stmt_cache_size: int = 20

    @property
    def is_configured(self) -> bool:
        return all([self.user, self.password, self.dsn])


@dataclass(slots=True)
class ConversationMemorySettings:
    table_name: str = "conversation_memory"
    char_limit: int = 10000
    recent_turns_limit: int = 12


@dataclass(slots=True)
class SummaryMemorySettings:
    table_name: str = "summary_memory"
    session_index_name: str = "summary_memory_session_idx"
    vector_index_name: str = "summary_memory_embedding_idx"
    top_k: int = 4
    max_context_chars: int = 6000
    distance: DistanceType = "COSINE"
    use_approximate_search: bool = True
    target_accuracy: int = 95
    ivf_neighbor_partitions: int = 32


@dataclass(slots=True)
class PersonaMemorySettings:
    table_name: str = "persona_memory"
    default_owner_scope: str = "default_user"
    max_items_in_context: int = 12


@dataclass(slots=True)
class EmbeddingSettings:
    provider: str = "sentence_transformers"
    model_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "models" / "embedding")
    device: str = "cuda"
    dimension: int | None = None
    batch_size: int = 8
    normalize_embeddings: bool = True
    max_seq_length: int | None = 512


@dataclass(slots=True)
class SummarizationSettings:
    prompt_name: str = "summary_compaction"
    llm_config_path: Path | None = None
    temperature: float = 0.0
    max_source_chars: int = 16000
    max_summary_chars: int = 4000


@dataclass(slots=True)
class MemoryConfig:
    enabled: bool = True
    oracle: OracleSettings = field(default_factory=OracleSettings)
    conversation: ConversationMemorySettings = field(default_factory=ConversationMemorySettings)
    summary: SummaryMemorySettings = field(default_factory=SummaryMemorySettings)
    persona: PersonaMemorySettings = field(default_factory=PersonaMemorySettings)
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    summarization: SummarizationSettings = field(default_factory=SummarizationSettings)
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


def find_memory_config_file(start: str | Path | None = None) -> Path | None:
    current_path = Path(start).resolve() if start is not None else Path(__file__).resolve()
    current_dir = current_path if current_path.is_dir() else current_path.parent
    for candidate_dir in (current_dir, *current_dir.parents):
        for candidate in (
            candidate_dir / "config" / "memory.json",
            candidate_dir / "backend" / "config" / "memory.json",
        ):
            if candidate.exists():
                return candidate
    return None


def load_memory_json_config(
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


def load_memory_config(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> MemoryConfig:
    payload = load_memory_json_config(config_path=config_path, env_path=env_path)
    if not payload.get("enabled", True):
        raise MemoryConfigError("Memory configuration is disabled.")

    oracle_payload = _coerce_mapping(payload.get("oracle"))
    conversation_payload = _coerce_mapping(payload.get("conversation"))
    summary_payload = _coerce_mapping(payload.get("summary"))
    persona_payload = _coerce_mapping(payload.get("persona"))
    embedding_payload = _coerce_mapping(payload.get("embedding"))
    summarization_payload = _coerce_mapping(payload.get("summarization"))

    config = MemoryConfig(
        enabled=bool(payload.get("enabled", True)),
        oracle=OracleSettings(
            user=_normalize_optional_string(oracle_payload.get("user")),
            password=_normalize_optional_string(oracle_payload.get("password")),
            dsn=_normalize_optional_string(oracle_payload.get("dsn")),
            min_pool_size=_coerce_positive_int(oracle_payload.get("min_pool_size"), default=1, field_name="oracle.min_pool_size"),
            max_pool_size=_coerce_positive_int(oracle_payload.get("max_pool_size"), default=4, field_name="oracle.max_pool_size"),
            pool_increment=_coerce_positive_int(oracle_payload.get("pool_increment"), default=1, field_name="oracle.pool_increment"),
            ping_interval_seconds=_coerce_positive_int(
                oracle_payload.get("ping_interval_seconds"),
                default=60,
                field_name="oracle.ping_interval_seconds",
            ),
            stmt_cache_size=_coerce_positive_int(oracle_payload.get("stmt_cache_size"), default=20, field_name="oracle.stmt_cache_size"),
        ),
        conversation=ConversationMemorySettings(
            table_name=_coerce_identifier(conversation_payload.get("table_name"), "conversation.table_name", default="conversation_memory"),
            char_limit=_coerce_positive_int(conversation_payload.get("char_limit"), default=10000, field_name="conversation.char_limit"),
            recent_turns_limit=_coerce_positive_int(
                conversation_payload.get("recent_turns_limit"),
                default=12,
                field_name="conversation.recent_turns_limit",
            ),
        ),
        summary=SummaryMemorySettings(
            table_name=_coerce_identifier(summary_payload.get("table_name"), "summary.table_name", default="summary_memory"),
            session_index_name=_coerce_identifier(
                summary_payload.get("session_index_name"),
                "summary.session_index_name",
                default="summary_memory_session_idx",
            ),
            vector_index_name=_coerce_identifier(
                summary_payload.get("vector_index_name"),
                "summary.vector_index_name",
                default="summary_memory_embedding_idx",
            ),
            top_k=_coerce_positive_int(summary_payload.get("top_k"), default=4, field_name="summary.top_k"),
            max_context_chars=_coerce_positive_int(
                summary_payload.get("max_context_chars"),
                default=6000,
                field_name="summary.max_context_chars",
            ),
            distance=_coerce_distance(summary_payload.get("distance"), default="COSINE"),
            use_approximate_search=bool(summary_payload.get("use_approximate_search", True)),
            target_accuracy=_coerce_positive_int(
                summary_payload.get("target_accuracy"),
                default=95,
                field_name="summary.target_accuracy",
            ),
            ivf_neighbor_partitions=_coerce_positive_int(
                summary_payload.get("ivf_neighbor_partitions"),
                default=32,
                field_name="summary.ivf_neighbor_partitions",
            ),
        ),
        persona=PersonaMemorySettings(
            table_name=_coerce_identifier(persona_payload.get("table_name"), "persona.table_name", default="persona_memory"),
            default_owner_scope=_coerce_required_string(
                persona_payload.get("default_owner_scope"),
                "persona.default_owner_scope",
            ),
            max_items_in_context=_coerce_positive_int(
                persona_payload.get("max_items_in_context"),
                default=12,
                field_name="persona.max_items_in_context",
            ),
        ),
        embedding=EmbeddingSettings(
            provider=_coerce_required_string(embedding_payload.get("provider"), "embedding.provider"),
            model_path=_coerce_path(embedding_payload.get("model_path"))
            or Path(__file__).resolve().parent / "models" / "embedding",
            device=_coerce_required_string(embedding_payload.get("device"), "embedding.device"),
            dimension=_coerce_optional_positive_int(embedding_payload.get("dimension"), field_name="embedding.dimension"),
            batch_size=_coerce_positive_int(embedding_payload.get("batch_size"), default=8, field_name="embedding.batch_size"),
            normalize_embeddings=bool(embedding_payload.get("normalize_embeddings", True)),
            max_seq_length=_coerce_optional_positive_int(embedding_payload.get("max_seq_length"), field_name="embedding.max_seq_length"),
        ),
        summarization=SummarizationSettings(
            prompt_name=_coerce_required_string(summarization_payload.get("prompt_name"), "summarization.prompt_name"),
            llm_config_path=_coerce_path(summarization_payload.get("llm_config_path")),
            temperature=_coerce_non_negative_float(
                summarization_payload.get("temperature"),
                default=0.0,
                field_name="summarization.temperature",
            ),
            max_source_chars=_coerce_positive_int(
                summarization_payload.get("max_source_chars"),
                default=16000,
                field_name="summarization.max_source_chars",
            ),
            max_summary_chars=_coerce_positive_int(
                summarization_payload.get("max_summary_chars"),
                default=4000,
                field_name="summarization.max_summary_chars",
            ),
        ),
        config_path=Path(payload["_resolved_config_path"]).resolve() if payload.get("_resolved_config_path") else None,
        env_path=Path(payload["_resolved_env_path"]).resolve() if payload.get("_resolved_env_path") else None,
    )
    _validate_memory_config(config)
    return config


def _validate_memory_config(config: MemoryConfig) -> None:
    if not config.embedding.device.lower().startswith("cuda"):
        raise MemoryConfigError("embedding.device must use CUDA for this project.")
    if config.summary.target_accuracy > 100:
        raise MemoryConfigError("summary.target_accuracy must be <= 100.")
    if config.oracle.max_pool_size < config.oracle.min_pool_size:
        raise MemoryConfigError("oracle.max_pool_size must be >= oracle.min_pool_size.")


def _resolve_env_path(env_path: str | Path | None) -> Path | None:
    if env_path is None:
        return find_env_file()
    candidate = Path(env_path).resolve()
    if not candidate.exists():
        raise MemoryConfigError(f"Env file not found: {candidate}")
    return candidate


def _resolve_config_path(config_path: str | Path | None) -> Path:
    if config_path is None:
        discovered = find_memory_config_file()
        if discovered is not None:
            return discovered
        if DEFAULT_CONFIG_PATH.exists():
            return DEFAULT_CONFIG_PATH.resolve()
        raise MemoryConfigError("Memory config file not found. Expected backend/config/memory.json.")
    candidate = Path(config_path).resolve()
    if not candidate.exists():
        raise MemoryConfigError(f"Memory config file not found: {candidate}")
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
        raise MemoryConfigError(f"Expected a JSON object, got {type(value).__name__}.")
    return value


def _normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_required_string(value: Any, field_name: str) -> str:
    normalized = _normalize_optional_string(value)
    if normalized is None:
        raise MemoryConfigError(f"Missing required config field: {field_name}")
    return normalized


def _coerce_identifier(value: Any, field_name: str, *, default: str) -> str:
    normalized = _normalize_optional_string(value) or default
    if not IDENTIFIER_PATTERN.match(normalized):
        raise MemoryConfigError(f"{field_name} must be a valid unquoted Oracle identifier.")
    return normalized.upper()


def _coerce_distance(value: Any, *, default: DistanceType) -> DistanceType:
    normalized = (_normalize_optional_string(value) or default).upper()
    if normalized not in {"COSINE", "DOT", "EUCLIDEAN"}:
        raise MemoryConfigError("summary.distance must be one of: COSINE, DOT, EUCLIDEAN.")
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
        raise MemoryConfigError(f"Expected integer for {field_name}, got {value!r}") from exc
    if parsed <= 0:
        raise MemoryConfigError(f"{field_name} must be > 0.")
    return parsed


def _coerce_optional_positive_int(value: Any, *, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    return _coerce_positive_int(value, default=1, field_name=field_name)


def _coerce_non_negative_float(value: Any, *, default: float, field_name: str) -> float:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MemoryConfigError(f"Expected float for {field_name}, got {value!r}") from exc
    if parsed < 0:
        raise MemoryConfigError(f"{field_name} must be >= 0.")
    return parsed
