from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Sequence

import httpx
from langsmith import traceable
from openai import AsyncOpenAI, OpenAI

from backend.observability.langsmith import serialize_llm_inputs, serialize_llm_outputs

from .config import LLMConfig
from .schemas import ChatMessage, LLMResponse


class BaseLLMProvider(ABC):
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @traceable(
        run_type="llm",
        name="llm_invoke",
        process_inputs=serialize_llm_inputs,
        process_outputs=serialize_llm_outputs,
    )
    def invoke(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return self.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    @traceable(
        run_type="llm",
        name="llm_ainvoke",
        process_inputs=serialize_llm_inputs,
        process_outputs=serialize_llm_outputs,
    )
    async def ainvoke(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self.achat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    @abstractmethod
    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        raise NotImplementedError

    @abstractmethod
    async def achat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        raise NotImplementedError

    @abstractmethod
    def healthcheck(self) -> dict[str, Any]:
        raise NotImplementedError

    def _normalize_messages(self, messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
        normalized_messages: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role is None or content is None:
                raise ValueError("Each chat message must include both 'role' and 'content'.")
            normalized_messages.append({key: value for key, value in message.items() if value is not None})
        return normalized_messages

    def _resolve_temperature(self, override: float | None) -> float | None:
        return self.config.default_temperature if override is None else override

    def _resolve_max_tokens(self, override: int | None) -> int | None:
        return self.config.max_tokens if override is None else override


class OpenAICompatibleLLMProvider(BaseLLMProvider):
    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        api_key = config.api_key or "EMPTY"
        self._client = OpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )
        self._async_client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        response = self._client.chat.completions.create(
            **self._build_request(messages, temperature=temperature, max_tokens=max_tokens, **kwargs)
        )
        return self._parse_openai_response(response)

    async def achat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        response = await self._async_client.chat.completions.create(
            **self._build_request(messages, temperature=temperature, max_tokens=max_tokens, **kwargs)
        )
        return self._parse_openai_response(response)

    def healthcheck(self) -> dict[str, Any]:
        models = self._client.models.list()
        return {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
            "available_models": [model.id for model in models.data],
        }

    def _build_request(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.config.model_id,
            "messages": self._normalize_messages(messages),
        }

        resolved_temperature = self._resolve_temperature(temperature)
        resolved_max_tokens = self._resolve_max_tokens(max_tokens)

        if resolved_temperature is not None:
            request["temperature"] = resolved_temperature
        if resolved_max_tokens is not None:
            request["max_tokens"] = resolved_max_tokens

        request.update(kwargs)
        return request

    def _parse_openai_response(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message
        usage = None
        if getattr(response, "usage", None) is not None:
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(
            content=_stringify_content(getattr(message, "content", "")),
            model=getattr(response, "model", self.config.model_id),
            provider=self.config.provider,
            usage=usage,
            finish_reason=getattr(choice, "finish_reason", None),
            tool_calls=_extract_tool_calls(message),
            raw_response=_model_dump(response),
        )


class OllamaLLMProvider(BaseLLMProvider):
    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )
        self._async_client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        response = self._client.post(
            "/api/chat",
            json=self._build_request(messages, temperature=temperature, max_tokens=max_tokens, **kwargs),
        )
        response.raise_for_status()
        return self._parse_ollama_response(response.json())

    async def achat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        response = await self._async_client.post(
            "/api/chat",
            json=self._build_request(messages, temperature=temperature, max_tokens=max_tokens, **kwargs),
        )
        response.raise_for_status()
        return self._parse_ollama_response(response.json())

    def healthcheck(self) -> dict[str, Any]:
        response = self._client.get("/api/tags")
        response.raise_for_status()
        payload = response.json()
        models = payload.get("models", [])
        return {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
            "available_models": [model.get("name") for model in models],
        }

    def _build_request(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        options: dict[str, Any] = kwargs.pop("options", {}).copy()

        resolved_temperature = self._resolve_temperature(temperature)
        resolved_max_tokens = self._resolve_max_tokens(max_tokens)
        if resolved_temperature is not None:
            options["temperature"] = resolved_temperature
        if resolved_max_tokens is not None:
            options["num_predict"] = resolved_max_tokens
        if self.config.ollama_num_ctx is not None:
            options["num_ctx"] = self.config.ollama_num_ctx

        request: dict[str, Any] = {
            "model": self.config.model_id,
            "messages": self._normalize_messages(messages),
            "stream": False,
        }
        if options:
            request["options"] = options
        if self.config.ollama_keep_alive is not None:
            request["keep_alive"] = self.config.ollama_keep_alive

        request.update(kwargs)
        return request

    def _parse_ollama_response(self, payload: dict[str, Any]) -> LLMResponse:
        message = payload.get("message", {})
        usage = None
        if payload.get("prompt_eval_count") is not None or payload.get("eval_count") is not None:
            prompt_tokens = payload.get("prompt_eval_count", 0)
            completion_tokens = payload.get("eval_count", 0)
            usage = {
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }

        return LLMResponse(
            content=_stringify_content(message.get("content", "")),
            model=payload.get("model", self.config.model_id),
            provider=self.config.provider,
            usage=usage,
            finish_reason=payload.get("done_reason"),
            tool_calls=message.get("tool_calls"),
            raw_response=payload,
        )


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def _extract_tool_calls(message: Any) -> list[dict[str, Any]] | None:
    raw_tool_calls = getattr(message, "tool_calls", None)
    if not raw_tool_calls:
        return None

    serialized_calls: list[dict[str, Any]] = []
    for tool_call in raw_tool_calls:
        if hasattr(tool_call, "model_dump"):
            serialized_calls.append(tool_call.model_dump())
            continue

        function_data = getattr(tool_call, "function", None)
        serialized_calls.append(
            {
                "id": getattr(tool_call, "id", None),
                "type": getattr(tool_call, "type", None),
                "function": {
                    "name": getattr(function_data, "name", None),
                    "arguments": getattr(function_data, "arguments", None),
                },
            }
        )

    return serialized_calls


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    return {"value": value}
