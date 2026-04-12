from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


PlanComplexity = Literal["simple", "complex"]
ExecutorStatus = Literal["tool", "finish", "replan"]


@dataclass(slots=True)
class PlanOutput:
    task_complexity: PlanComplexity
    goal: str
    planning_notes: str
    steps: list[str]
    done_when: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutorDecision:
    status: ExecutorStatus
    reasoning: str
    step_focus: str
    advance_to_next_step: bool = False
    tool_name: str | None = None
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None
    replan_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentRunResult:
    user_query: str
    final_answer: str
    plan: PlanOutput
    iteration_count: int
    execution_history: list[dict[str, Any]] = field(default_factory=list)
    raw_state: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentEvent:
    event_type: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
