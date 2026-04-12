from __future__ import annotations

from pathlib import Path

from backend.llm import BaseLLMProvider, build_llm_provider

from .config import load_memory_config
from .embedder import SentenceTransformerMemoryEmbedder
from .prompts import MemoryPromptRepository
from .service import MemoryService
from .store import OracleMemoryStore


def build_memory_service(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
    llm: BaseLLMProvider | None = None,
) -> MemoryService:
    config = load_memory_config(config_path=config_path, env_path=env_path)
    return MemoryService(
        config=config,
        store=OracleMemoryStore(config),
        embedder=SentenceTransformerMemoryEmbedder(config.embedding),
        prompts=MemoryPromptRepository(),
        llm=llm or _build_summary_llm(config, env_path=env_path),
    )


def _build_summary_llm(config, *, env_path: str | Path | None = None) -> BaseLLMProvider | None:
    llm_config_path = config.summarization.llm_config_path
    if llm_config_path is None:
        try:
            return build_llm_provider(env_path=env_path)
        except Exception:
            return None
    try:
        return build_llm_provider(config_path=llm_config_path, env_path=env_path)
    except Exception:
        return None
