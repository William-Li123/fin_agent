from __future__ import annotations

from pathlib import Path

from backend.observability import bootstrap_langsmith_env

from .config import LLMConfig, load_llm_config
from .providers import BaseLLMProvider, OllamaLLMProvider, OpenAICompatibleLLMProvider


def build_llm_provider(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> BaseLLMProvider:
    bootstrap_langsmith_env(env_path)
    config = load_llm_config(config_path=config_path, env_path=env_path)
    return build_llm_provider_from_config(config)


def build_llm_provider_from_config(config: LLMConfig) -> BaseLLMProvider:
    if config.provider == "ollama":
        return OllamaLLMProvider(config)
    return OpenAICompatibleLLMProvider(config)
