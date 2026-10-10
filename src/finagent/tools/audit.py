"""Redact credential-shaped strings before writing host audit records."""
import re
from typing import Any

_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_SECRET_FIELDS = {"api_key", "apikey", "authorization", "access_token", "refresh_token", "password"}


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _TOKEN.sub("[REDACTED_API_KEY]", value)
    if isinstance(value, dict):
        return {key: "[REDACTED]" if str(key).lower() in _SECRET_FIELDS else redact(child)
                for key, child in value.items()}
    if isinstance(value, list):
        return [redact(child) for child in value]
    return value
