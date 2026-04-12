from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


TRUTHY_VALUES = {"1", "true", "yes", "on"}


def bootstrap_langsmith_env(env_path: str | Path | None = None) -> Path | None:
    resolved_env_path = _resolve_env_path(env_path)
    if resolved_env_path is not None and resolved_env_path.exists():
        load_dotenv(dotenv_path=resolved_env_path, override=False)

    tracing_value = str(os.getenv("LANGSMITH_TRACING", "")).strip().lower()
    if tracing_value in TRUTHY_VALUES and not os.getenv("LANGSMITH_TRACING_V2"):
        os.environ["LANGSMITH_TRACING_V2"] = "true"

    return resolved_env_path


def langsmith_is_enabled(env_path: str | Path | None = None) -> bool:
    bootstrap_langsmith_env(env_path)
    tracing_value = str(os.getenv("LANGSMITH_TRACING") or os.getenv("LANGSMITH_TRACING_V2") or "").strip().lower()
    return tracing_value in TRUTHY_VALUES and bool(str(os.getenv("LANGSMITH_API_KEY", "")).strip())


def serialize_agent_run_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_query": str(inputs.get("user_query", "")),
    }


def serialize_agent_run_outputs(output: Any) -> dict[str, Any]:
    return _sanitize_value(
        {
            "final_answer": getattr(output, "final_answer", ""),
            "iteration_count": getattr(output, "iteration_count", 0),
            "plan": getattr(getattr(output, "plan", None), "raw", {}),
            "execution_history_count": len(getattr(output, "execution_history", []) or []),
        }
    )


def serialize_state_node_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    state = inputs.get("state", {})
    if not isinstance(state, dict):
        return {"state_type": type(state).__name__}

    plan = state.get("plan")
    plan_summary = state.get("plan_summary")
    execution_history = state.get("execution_history", [])
    return _sanitize_value(
        {
            "user_query": state.get("user_query"),
            "iteration_count": state.get("iteration_count", 0),
            "current_step_index": state.get("current_step_index", 0),
            "pending_status": state.get("pending_status"),
            "pending_tool_name": state.get("pending_tool_name"),
            "pending_tool_arguments": state.get("pending_tool_arguments", {}),
            "pending_step_focus": state.get("pending_step_focus"),
            "last_error": state.get("last_error"),
            "plan_summary": plan_summary,
            "plan": getattr(plan, "raw", None),
            "execution_history_count": len(execution_history) if isinstance(execution_history, list) else 0,
        }
    )


def serialize_state_node_outputs(output: Any) -> dict[str, Any]:
    if not isinstance(output, dict):
        return {"output_type": type(output).__name__}

    serialized = {
        "keys": list(output.keys()),
        "pending_status": output.get("pending_status"),
        "pending_tool_name": output.get("pending_tool_name"),
        "pending_tool_arguments": output.get("pending_tool_arguments"),
        "current_step_index": output.get("current_step_index"),
        "iteration_count": output.get("iteration_count"),
        "final_answer": output.get("final_answer"),
        "draft_answer": output.get("draft_answer"),
        "last_error": output.get("last_error"),
    }

    plan = output.get("plan")
    if plan is not None:
        serialized["plan"] = getattr(plan, "raw", None)
    history = output.get("execution_history")
    if isinstance(history, list):
        serialized["execution_history_count"] = len(history)
        if history:
            serialized["latest_history_item"] = history[-1]

    return _sanitize_value(serialized)


def serialize_llm_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    messages = inputs.get("messages", [])
    normalized_messages = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                normalized_messages.append({"value": str(message)})
                continue
            normalized_messages.append(
                {
                    "role": message.get("role"),
                    "name": message.get("name"),
                    "tool_call_id": message.get("tool_call_id"),
                    "content": _truncate_text(message.get("content")),
                }
            )

    return _sanitize_value(
        {
            "provider": getattr(inputs.get("self"), "config", None).provider if inputs.get("self") else None,
            "model_id": getattr(inputs.get("self"), "config", None).model_id if inputs.get("self") else None,
            "temperature": inputs.get("temperature"),
            "max_tokens": inputs.get("max_tokens"),
            "messages": normalized_messages,
        }
    )


def serialize_llm_outputs(output: Any) -> dict[str, Any]:
    return _sanitize_value(
        {
            "content": getattr(output, "content", ""),
            "model": getattr(output, "model", None),
            "provider": getattr(output, "provider", None),
            "usage": getattr(output, "usage", None),
            "finish_reason": getattr(output, "finish_reason", None),
            "tool_calls": getattr(output, "tool_calls", None),
        }
    )


def serialize_tool_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_value(
        {
            "tool_name": inputs.get("name"),
            "arguments": inputs.get("arguments", {}),
        }
    )


def serialize_tool_outputs(output: Any) -> dict[str, Any]:
    return _sanitize_value(
        {
            "tool_name": getattr(output, "tool_name", None),
            "success": getattr(output, "success", None),
            "error": getattr(output, "error", None),
            "text": getattr(output, "text", None),
            "structured_content": getattr(output, "structured_content", None),
            "metadata": getattr(output, "metadata", None),
        }
    )


def _resolve_env_path(env_path: str | Path | None) -> Path | None:
    if env_path is not None:
        candidate = Path(env_path).resolve()
        return candidate if candidate.exists() else None

    current = Path(__file__).resolve()
    for candidate_dir in (current.parent, *current.parents):
        candidate = candidate_dir / ".env"
        if candidate.exists():
            return candidate
    return None


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _truncate_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    try:
        return json.loads(json.dumps(value))
    except TypeError:
        return _truncate_text(str(value))


def _truncate_text(value: Any, *, max_chars: int = 4000) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}\n...[truncated]"
