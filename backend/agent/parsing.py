from __future__ import annotations

import json
from typing import Any

from .schemas import ExecutorDecision, PlanOutput


def parse_plan_output(raw_text: str, *, user_query: str) -> PlanOutput:
    payload = _extract_json_object(raw_text)
    if payload is None:
        return _fallback_plan(user_query, raw_text)

    steps = payload.get("steps")
    normalized_steps = [str(step).strip() for step in steps if str(step).strip()] if isinstance(steps, list) else []
    if not normalized_steps:
        normalized_steps = ["Understand the request and answer it directly."]

    complexity = str(payload.get("task_complexity", "simple")).strip().lower()
    if complexity not in {"simple", "complex"}:
        complexity = "simple"

    return PlanOutput(
        task_complexity=complexity,  # type: ignore[arg-type]
        goal=str(payload.get("goal", user_query)).strip() or user_query,
        planning_notes=str(payload.get("planning_notes", "Use the fewest necessary steps.")).strip()
        or "Use the fewest necessary steps.",
        steps=normalized_steps,
        done_when=str(payload.get("done_when", "The request is fully answered.")).strip()
        or "The request is fully answered.",
        raw=payload,
    )


def parse_executor_decision(raw_text: str) -> ExecutorDecision:
    payload = _extract_json_object(raw_text)
    if payload is None:
        return ExecutorDecision(
            status="finish",
            reasoning="The model did not return valid JSON, so the raw response is treated as the final answer.",
            step_focus="Finalize the response",
            answer=raw_text.strip(),
            raw={"raw_text": raw_text},
        )

    status = str(payload.get("status", "finish")).strip().lower()
    if status not in {"tool", "finish", "replan"}:
        status = "finish"

    tool_arguments = payload.get("tool_arguments", {})
    if not isinstance(tool_arguments, dict):
        tool_arguments = {}

    advance_to_next_step = payload.get("advance_to_next_step", False)
    if not isinstance(advance_to_next_step, bool):
        advance_to_next_step = str(advance_to_next_step).strip().lower() in {"1", "true", "yes", "on"}

    decision = ExecutorDecision(
        status=status,  # type: ignore[arg-type]
        reasoning=str(payload.get("reasoning", "")).strip(),
        step_focus=str(payload.get("step_focus", "")).strip() or "Continue solving the task",
        advance_to_next_step=advance_to_next_step,
        tool_name=_normalize_optional_text(payload.get("tool_name")),
        tool_arguments=tool_arguments,
        answer=_normalize_optional_text(payload.get("answer")),
        replan_reason=_normalize_optional_text(payload.get("replan_reason")),
        raw=payload,
    )

    if decision.status == "tool" and not decision.tool_name:
        decision.status = "finish"
        decision.answer = decision.answer or "The model requested a tool but did not provide a valid tool name."
    if decision.status == "finish" and not decision.answer:
        decision.answer = "The task appears complete, but no answer text was provided."
    return decision


def _fallback_plan(user_query: str, raw_text: str) -> PlanOutput:
    return PlanOutput(
        task_complexity="simple",
        goal=user_query,
        planning_notes="Fallback plan generated because the planner output was not valid JSON.",
        steps=["Understand the request and answer it directly or with the minimum necessary tools."],
        done_when="The user receives a complete and correct answer.",
        raw={"raw_text": raw_text},
    )


def _normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
    if not raw_text:
        return None

    candidates = [raw_text.strip()]
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            candidates.append("\n".join(lines[1:-1]).strip())

    brace_start = raw_text.find("{")
    if brace_start >= 0:
        brace_end = _find_matching_brace(raw_text, brace_start)
        if brace_end is not None:
            candidates.append(raw_text[brace_start : brace_end + 1].strip())

    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _find_matching_brace(text: str, start_index: int) -> int | None:
    depth = 0
    in_string = False
    escape = False

    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return index

    return None
