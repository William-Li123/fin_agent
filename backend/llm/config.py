from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

LLMProviderType = Literal["api", "vllm", "ollama"]

SUPPORTED_PROVIDERS: set[str] = {"api", "vllm", "ollama"}
LOCAL_HOSTS: set[str] = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
PLACEHOLDER_API_KEYS: set[str] = {"", "empty", "dummy", "none", "null", "your_api_key_here"}
PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "llm.json"


class LLMConfigError(ValueError):
    """Raised when the LLM provider cannot be configured from config or environment."""


@dataclass(slots=True)
class LLMConfig:
    provider: LLMProviderType
    model_id: str
    base_url: str
    api_key: str | None
    timeout_seconds: float = 120.0
    default_temperature: float | None = 0.0
    max_tokens: int | None = None
    ollama_num_ctx: int | None = None
    ollama_keep_alive: str | None = None
    config_path: Path | None = None
    env_path: Path | None = None

    @property
    def is_local(self) -> bool:
        return _extract_hostname(self.base_url) in LOCAL_HOSTS


def find_env_file(start: str | Path | None = None) -> Path | None:
    current_path = Path(start).resolve() if start is not None else Path(__file__).resolve()
    current_dir = current_path if current_path.is_dir() else current_path.parent

    for candidate_dir in (current_dir, *current_dir.parents):
        candidate = candidate_dir / ".env"
        if candidate.exists():
            return candidate

    return None


def find_llm_config_file(start: str | Path | None = None) -> Path | None:
    current_path = Path(start).resolve() if start is not None else Path(__file__).resolve()
    current_dir = current_path if current_path.is_dir() else current_path.parent

    for candidate_dir in (current_dir, *current_dir.parents):
        for candidate in (
            candidate_dir / "config" / "llm.json",
            candidate_dir / "backend" / "config" / "llm.json",
        ):
            if candidate.exists():
                return candidate

    return None


def load_llm_json_config(
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


def load_llm_config(
    config_path: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
) -> LLMConfig:
    payload = load_llm_json_config(config_path=config_path, env_path=env_path)

    if not payload.get("enabled", True):
        raise LLMConfigError("LLM configuration is disabled.")

    model_id = _normalize_optional_string(_coerce_string(payload.get("model_id")))
    if model_id is None:
        raise LLMConfigError("Missing required config field: model_id")

    provider = _resolve_provider(
        explicit_provider=_normalize_optional_string(_coerce_string(payload.get("provider"))) or "auto",
        base_url=_normalize_optional_string(_coerce_string(payload.get("base_url"))),
        api_key=_resolve_api_key(payload),
        model_id=model_id,
    )
    base_url = _normalize_base_url(provider, _normalize_optional_string(_coerce_string(payload.get("base_url"))))
    api_key = _resolve_api_key(payload)

    resolved_config_path = payload.get("_resolved_config_path")
    resolved_env_path = payload.get("_resolved_env_path")

    return LLMConfig(
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=_coerce_optional_float(payload.get("timeout_seconds"), default=120.0),
        default_temperature=_coerce_optional_float(payload.get("default_temperature"), default=0.0),
        max_tokens=_coerce_optional_int(payload.get("max_tokens")),
        ollama_num_ctx=_coerce_optional_int(payload.get("ollama_num_ctx")),
        ollama_keep_alive=_normalize_optional_string(_coerce_string(payload.get("ollama_keep_alive"))),
        config_path=Path(resolved_config_path).resolve() if isinstance(resolved_config_path, str) else None,
        env_path=Path(resolved_env_path).resolve() if isinstance(resolved_env_path, str) else None,
    )


def _resolve_env_path(env_path: str | Path | None) -> Path | None:
    if env_path is None:
        return find_env_file()

    candidate = Path(env_path).resolve()
    if not candidate.exists():
        raise LLMConfigError(f"Env file not found: {candidate}")

    return candidate


def _resolve_config_path(config_path: str | Path | None) -> Path:
    if config_path is None:
        discovered = find_llm_config_file()
        if discovered is not None:
            return discovered
        if DEFAULT_CONFIG_PATH.exists():
            return DEFAULT_CONFIG_PATH.resolve()
        raise LLMConfigError("LLM config file not found. Expected backend/config/llm.json.")

    candidate = Path(config_path).resolve()
    if not candidate.exists():
        raise LLMConfigError(f"LLM config file not found: {candidate}")

    return candidate


def _resolve_provider(
    *,
    explicit_provider: str,
    base_url: str | None,
    api_key: str | None,
    model_id: str,
) -> LLMProviderType:
    normalized_provider = explicit_provider.strip().lower()
    if normalized_provider in SUPPORTED_PROVIDERS:
        return normalized_provider  # type: ignore[return-value]
    if normalized_provider and normalized_provider != "auto":
        raise LLMConfigError("provider must be one of: auto, api, vllm, ollama.")

    if base_url:
        lower_url = base_url.lower()
        hostname = _extract_hostname(base_url)
        if "ollama" in lower_url or ":11434" in lower_url or "/api/chat" in lower_url:
            return "ollama"
        if hostname in LOCAL_HOSTS:
            return "vllm"
        return "api"

    if _has_real_api_key(api_key):
        return "api"

    if ":" in model_id:
        return "ollama"

    raise LLMConfigError(
        "Unable to auto-detect the provider. Set provider explicitly in backend/config/llm.json "
        "or supply enough information to distinguish api, vllm, or ollama."
    )


def _resolve_api_key(payload: dict[str, Any]) -> str | None:
    direct_api_key = _normalize_optional_string(_coerce_string(payload.get("api_key")))
    if direct_api_key is not None:
        return direct_api_key

    api_key_env = _normalize_optional_string(_coerce_string(payload.get("api_key_env")))
    if api_key_env is None:
        return None

    api_key = _normalize_optional_string(os.getenv(api_key_env))
    if api_key is None:
        raise LLMConfigError(
            f"api_key_env points to '{api_key_env}', but that environment variable is missing or empty."
        )
    return api_key


def _normalize_base_url(provider: LLMProviderType, raw_base_url: str | None) -> str:
    base_url = raw_base_url or _default_base_url(provider)
    base_url = _ensure_scheme(base_url).rstrip("/")

    if provider == "ollama":
        return _normalize_ollama_base_url(base_url)

    return base_url


def _default_base_url(provider: LLMProviderType) -> str:
    if provider == "api":
        return "https://api.openai.com/v1"
    if provider == "vllm":
        return "http://127.0.0.1:8000/v1"
    return "http://127.0.0.1:11434"


def _normalize_ollama_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")

    for suffix in ("/api/chat", "/api/generate", "/v1"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break

    normalized = parsed._replace(path=path, params="", query="", fragment="")
    return urlunparse(normalized).rstrip("/")


def _ensure_scheme(base_url: str) -> str:
    if "://" in base_url:
        return base_url
    return f"http://{base_url}"


def _extract_hostname(base_url: str) -> str:
    parsed = urlparse(_ensure_scheme(base_url))
    return (parsed.hostname or "").lower()


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
        return {
            key: _resolve_template_variables(item, context)
            for key, item in value.items()
        }
    return value


def _coerce_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _coerce_optional_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(f"Expected a float value, got: {value!r}") from exc


def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(f"Expected an integer value, got: {value!r}") from exc


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None

    stripped = value.strip()
    return stripped or None


def _has_real_api_key(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    return normalized not in PLACEHOLDER_API_KEYS
