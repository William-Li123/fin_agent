from __future__ import annotations

import json
from typing import Any

from backend.llm import BaseLLMProvider, build_llm_provider

from .config import QueryExpansionSettings
from .prompts import RAGPromptRepository
from .schemas import QueryExpansionResult


class LLMQueryExpander:
    def __init__(
        self,
        settings: QueryExpansionSettings,
        *,
        llm: BaseLLMProvider | None = None,
        prompt_repo: RAGPromptRepository | None = None,
    ) -> None:
        self.settings = settings
        self._llm = llm
        self.prompt_repo = prompt_repo or RAGPromptRepository()

    def is_enabled(self) -> bool:
        return self.settings.enabled

    def expand(self, query: str) -> QueryExpansionResult:
        normalized_query = str(query).strip()
        if not normalized_query:
            return QueryExpansionResult(
                original_query=query,
                expanded_query=query,
                search_queries=[query],
            )

        prompt = self.prompt_repo.render(
            self.settings.prompt_name,
            {
                "query": normalized_query,
                "max_alternate_queries": self.settings.max_alternate_queries,
            },
        )
        response = self._get_llm().invoke([{"role": "system", "content": prompt}], temperature=0)
        payload = _parse_json_object(response.content)

        expanded_query = _normalize_string(payload.get("expanded_query")) or normalized_query
        alternate_queries = _normalize_string_list(payload.get("alternate_queries"))[: self.settings.max_alternate_queries]
        hypothetical_answer = _normalize_string(payload.get("hypothetical_answer"))

        search_queries = _deduplicate_queries(
            [
                normalized_query,
                expanded_query,
                *alternate_queries,
                hypothetical_answer if self.settings.include_hypothetical_answer else None,
            ]
        )

        return QueryExpansionResult(
            original_query=normalized_query,
            expanded_query=expanded_query,
            alternate_queries=alternate_queries,
            hypothetical_answer=hypothetical_answer if self.settings.include_hypothetical_answer else None,
            search_queries=search_queries,
        )

    def _get_llm(self) -> BaseLLMProvider:
        if self._llm is None:
            self._llm = build_llm_provider(config_path=self.settings.llm_config_path)
        return self._llm


def _parse_json_object(text: str) -> dict[str, Any]:
    raw_text = text.strip()
    if not raw_text:
        return {}
    try:
        parsed = json.loads(raw_text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            parsed = json.loads(raw_text[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


def _normalize_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        text = _normalize_string(item)
        if text:
            normalized.append(text)
    return normalized


def _deduplicate_queries(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _normalize_string(value)
        if not text:
            continue
        normalized_key = text.casefold()
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        result.append(text)
    return result
