from __future__ import annotations

import json
from typing import Any, TypedDict

from backend.tools import ToolRegistry

from .schemas import PlanOutput


class AgentState(TypedDict, total=False):
    session_id: str | None
    user_query: str
    conversation_history: list[dict[str, str]]
    max_iterations: int
    max_plan_steps: int
    tool_catalog: str
    registry: ToolRegistry
    plan: PlanOutput
    plan_summary: str
    current_step_index: int
    execution_history: list[dict[str, Any]]
    iteration_count: int
    pending_status: str | None
    pending_tool_name: str | None
    pending_tool_arguments: dict[str, Any]
    pending_reasoning: str
    pending_step_focus: str
    pending_advance_to_next_step: bool
    draft_answer: str
    final_answer: str
    last_error: str | None


def summarize_plan(plan: PlanOutput) -> str:
    lines = [
        f"Task complexity: {plan.task_complexity}",
        f"Goal: {plan.goal}",
        f"Planning notes: {plan.planning_notes}",
        "Steps:",
    ]
    for index, step in enumerate(plan.steps, start=1):
        lines.append(f"{index}. {step}")
    lines.append(f"Done when: {plan.done_when}")
    return "\n".join(lines)


def current_step_text(plan: PlanOutput, step_index: int) -> str:
    if plan.steps and 0 <= step_index < len(plan.steps):
        return plan.steps[step_index]
    if plan.steps:
        return plan.steps[-1]
    return "Answer the user's request directly."


def format_execution_history(history: list[dict[str, Any]], *, max_chars: int = 6000) -> str:
    if not history:
        return "(no actions taken yet)"

    lines: list[str] = []
    for index, item in enumerate(history, start=1):
        lines.append(f"[{index}] status={item.get('status', 'unknown')} step={item.get('step_focus', '')}")
        lines.append(f"reasoning: {item.get('reasoning', '')}")
        if item.get("tool_name"):
            lines.append(f"tool: {item['tool_name']}")
            if item.get("tool_arguments"):
                lines.append(f"arguments: {json.dumps(item['tool_arguments'], ensure_ascii=False)}")
        if item.get("observation"):
            lines.append(f"observation: {item['observation']}")
        if item.get("answer"):
            lines.append(f"answer: {item['answer']}")
        lines.append("")

    rendered = "\n".join(lines).strip()
    if len(rendered) <= max_chars:
        return rendered
    return f"{rendered[:max_chars].rstrip()}\n...[truncated]"


def format_conversation_history(history: list[dict[str, str]], *, max_chars: int = 4000) -> str:
    if not history:
        return "(no previous turns)"

    lines: list[str] = []
    for index, turn in enumerate(history, start=1):
        user_message = str(turn.get("user_message", "")).strip()
        assistant_message = str(turn.get("assistant_message", "")).strip()
        lines.append(f"Turn {index} user: {user_message}")
        lines.append(f"Turn {index} assistant: {assistant_message}")
        lines.append("")

    rendered = "\n".join(lines).strip()
    if len(rendered) <= max_chars:
        return rendered
    return f"{rendered[:max_chars].rstrip()}\n...[truncated]"


def sanitize_tool_output(value: Any, *, max_chars: int = 2400) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except TypeError:
            text = str(value)

    text = text.strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}\n...[truncated]"


def format_session_context(state: AgentState) -> str:
    session_id = state.get("session_id")
    if session_id:
        return f"current_session_id: {session_id}"
    return "current_session_id: (none)"
