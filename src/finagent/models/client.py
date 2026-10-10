"""A text-only OpenAI-compatible client with mandatory persistent accounting."""
from __future__ import annotations

import json
import os
import threading
from typing import Any
from urllib.parse import urlsplit

from .budget import BudgetAccountingError, BudgetLedger, _integer, _label


class ModelResponseError(RuntimeError):
    """A billed response did not contain a usable, complete text message."""


def validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise ValueError("Model base URL must be text.")
    try:
        parsed = urlsplit(base_url)
        _ = parsed.port
    except ValueError:
        raise ValueError("Malformed model base URL.") from None
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(char.isspace() for char in base_url)):
        raise ValueError("Model base URL cannot include credentials, queries, fragments or whitespace.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("A remote model API requires HTTPS; HTTP is allowed only for local test servers.")
    if parsed.hostname == "platform.deepseek.com" or "/api_keys" in parsed.path:
        raise ValueError("The API-key management console is not a model API. Use https://api.deepseek.com.")
    return base_url.rstrip("/")


def normalize_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, str]], int]:
    if not isinstance(messages, list) or not messages:
        raise ValueError("At least one text message is required.")
    normalized = []
    for item in messages:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise ValueError("Only role/content text messages are supported; hidden payload fields are rejected.")
        if item["role"] not in {"system", "user", "assistant"} or not isinstance(item["content"], str):
            raise ValueError("Messages must have a supported role and string content.")
        normalized.append({"role": item["role"], "content": item["content"]})
    # UTF-8 byte length upper-bounds ordinary text tokenization. The allowance
    # covers role framing; any provider violation is accounted and freezes calls.
    serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    upper = len(serialized) + 64 * len(normalized) + 512
    if upper > 120_000:
        raise ValueError("Input exceeds the configured conservative 120,000-token reservation limit.")
    return normalized, upper


class ModelClient:
    def __init__(self, ledger: BudgetLedger, api_key: str | None = None, *,
                 base_url: str = "https://api.deepseek.com", model: str = "deepseek-flash",
                 max_output_tokens: int = 2048, timeout: float = 90,
                 client: Any = None) -> None:
        self.ledger = ledger
        self.base_url = validate_base_url(base_url)
        self.model = _label(model, "model")
        self.max_output_tokens = self._output_limit(max_output_tokens)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
            raise ValueError("timeout must be a finite value between 0 and 600 seconds.")
        self.timeout = timeout
        self._api_key = api_key
        self._client = client
        self._lock = threading.Lock()
        self.last_usage: dict[str, Any] | None = None
        self.last_call_id: str | None = None

    def __repr__(self) -> str:
        return f"ModelClient(model={self.model!r}, base_url={self.base_url!r}, api_key=<redacted>)"

    @staticmethod
    def _output_limit(value: Any) -> int:
        result = _integer(value, "max_output_tokens", positive=True)
        if result > 16_384:
            raise ValueError("max_output_tokens cannot exceed 16,384 in this client.")
        return result

    def _get_client(self) -> Any:
        if self._client is None:
            key = self._api_key or os.environ.get("DEEPSEEK_API_KEY")
            if not isinstance(key, str) or not key.strip() or any(char.isspace() for char in key):
                raise ValueError("Set DEEPSEEK_API_KEY to a valid nonempty key before requesting a completion.")
            from openai import OpenAI
            self._client = OpenAI(api_key=key, base_url=self.base_url, timeout=self.timeout, max_retries=0)
        return self._client

    def complete(self, messages: list[dict[str, Any]], *, stage: str,
                 max_output_tokens: int | None = None) -> str:
        normalized, input_upper = normalize_messages(messages)
        output_upper = self._output_limit(self.max_output_tokens if max_output_tokens is None else max_output_tokens)
        _label(stage, "stage")
        with self._lock:
            self.last_usage = None
            self.last_call_id = None
            client = self._get_client()  # Configuration errors must not create a reservation.
            reservation = self.ledger.reserve(input_upper, output_upper, stage=stage, model=self.model)
            self.last_call_id = reservation.call_id
            try:
                response = client.chat.completions.create(
                    model=self.model, messages=normalized, max_tokens=output_upper, temperature=0,
                    stream=False, extra_body={"thinking": {"type": "disabled"}})
            except BaseException as exc:
                self.ledger.mark_unknown(reservation.call_id, "transport_error" if isinstance(exc, Exception) else "interrupted")
                if not isinstance(exc, Exception):
                    raise
                raise BudgetAccountingError("Model request failed; billing is uncertain and further calls are frozen.") from None
            usage = getattr(response, "usage", None)
            try:
                if hasattr(usage, "model_dump"):
                    usage = usage.model_dump()
                self.last_usage = self.ledger.settle(reservation.call_id, usage)
            except BudgetAccountingError:
                raise
            except Exception:
                self.ledger.mark_unknown(reservation.call_id, "settlement_error")
                raise BudgetAccountingError("Model usage could not be recorded; further calls are frozen.") from None
            choices = getattr(response, "choices", None)
            if not choices:
                raise ModelResponseError("Model returned no choice; verified usage was recorded.")
            choice = choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                raise ModelResponseError("Model output reached the token limit; verified usage was recorded.")
            content = getattr(getattr(choice, "message", None), "content", None)
            if not isinstance(content, str) or not content.strip():
                raise ModelResponseError("Model returned no text; verified usage was recorded.")
            return content
