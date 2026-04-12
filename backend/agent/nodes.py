from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.llm import BaseLLMProvider
from langsmith import traceable

from backend.observability.langsmith import serialize_state_node_inputs, serialize_state_node_outputs
from backend.tools import ToolRegistry

from .parsing import parse_executor_decision, parse_plan_output
from .prompts import PromptRepository
from .schemas import AgentEvent
from .state import (
    AgentState,
    current_step_text,
    format_conversation_history,
    format_execution_history,
    format_session_context,
    sanitize_tool_output,
    summarize_plan,
)


class AgentNodes:
    def __init__(self, *, llm: BaseLLMProvider, registry: ToolRegistry, prompts: PromptRepository) -> None:
        self.llm = llm
        self.registry = registry
        self.prompts = prompts
        self.event_handler: Callable[[AgentEvent], None] | None = None

    def set_event_handler(self, event_handler: Callable[[AgentEvent], None] | None) -> None:
        self.event_handler = event_handler

    def _emit(self, event_type: str, message: str, **data: Any) -> None:
        if self.event_handler is None:
            return
        self.event_handler(AgentEvent(event_type=event_type, message=message, data=data))

    @traceable(
        run_type="chain",
        name="agent_planner_node",
        process_inputs=serialize_state_node_inputs,
        process_outputs=serialize_state_node_outputs,
    )
    def planner(self, state: AgentState) -> dict[str, Any]:
        self._emit("planning_started", "正在规划任务", user_query=state["user_query"])
        prompt = self.prompts.render(
            "planner",
            {
                "session_context": format_session_context(state),
                "conversation_history": format_conversation_history(state.get("conversation_history", [])),
                "user_query": state["user_query"],
                "available_tools": state["tool_catalog"],
                "tool_count": len(self.registry.list_tools()),
                "max_plan_steps": state.get("max_plan_steps", 5),
                "execution_history": format_execution_history(state.get("execution_history", []), max_chars=4000),
            },
        )
        response = self.llm.invoke([{"role": "system", "content": prompt}], temperature=0)
        plan = parse_plan_output(response.content, user_query=state["user_query"])
        current_step_index = min(state.get("current_step_index", 0), max(len(plan.steps) - 1, 0))
        self._emit(
            "planning_completed",
            "规划完成",
            task_complexity=plan.task_complexity,
            steps=plan.steps,
        )
        return {
            "plan": plan,
            "plan_summary": summarize_plan(plan),
            "current_step_index": current_step_index,
            "pending_status": None,
            "pending_tool_name": None,
            "pending_tool_arguments": {},
            "pending_reasoning": "",
            "pending_step_focus": "",
            "pending_advance_to_next_step": False,
            "last_error": None,
        }

    @traceable(
        run_type="chain",
        name="agent_executor_node",
        process_inputs=serialize_state_node_inputs,
        process_outputs=serialize_state_node_outputs,
    )
    def executor(self, state: AgentState) -> dict[str, Any]:
        step_focus = current_step_text(state["plan"], state.get("current_step_index", 0)) if state.get("plan") else ""
        self._emit(
            "execution_started",
            "正在执行当前步骤",
            current_step=step_focus,
            iteration_count=state.get("iteration_count", 0),
        )
        if state.get("iteration_count", 0) >= state.get("max_iterations", 8):
            fallback_answer = self._build_iteration_limit_answer(state)
            self._emit("execution_completed", "达到最大迭代次数，准备收尾", status="finish")
            return {
                "pending_status": "finish",
                "draft_answer": fallback_answer,
                "pending_reasoning": "Maximum iterations reached. Finalizing with the gathered evidence.",
                "pending_step_focus": "Finalize the response",
            }

        plan = state["plan"]
        step_index = state.get("current_step_index", 0)
        prompt = self.prompts.render(
            "executor",
            {
                "session_context": format_session_context(state),
                "conversation_history": format_conversation_history(state.get("conversation_history", [])),
                "user_query": state["user_query"],
                "plan_summary": state["plan_summary"],
                "current_step": current_step_text(plan, step_index),
                "step_index": step_index + 1,
                "step_count": max(len(plan.steps), 1),
                "available_tools": state["tool_catalog"],
                "execution_history": format_execution_history(state.get("execution_history", [])),
                "iteration_count": state.get("iteration_count", 0),
                "max_iterations": state.get("max_iterations", 8),
            },
        )
        response = self.llm.invoke([{"role": "system", "content": prompt}], temperature=0)
        decision = parse_executor_decision(response.content)

        if decision.status == "tool" and decision.tool_name and not self.registry.has(decision.tool_name):
            decision.status = "replan"
            decision.replan_reason = f"Requested tool '{decision.tool_name}' is not registered."

        self._emit(
            "execution_completed",
            "当前步骤决策完成",
            status=decision.status,
            tool_name=decision.tool_name,
            step_focus=decision.step_focus,
        )
        return {
            "pending_status": decision.status,
            "pending_tool_name": decision.tool_name,
            "pending_tool_arguments": decision.tool_arguments,
            "pending_reasoning": decision.reasoning,
            "pending_step_focus": decision.step_focus,
            "pending_advance_to_next_step": decision.advance_to_next_step,
            "draft_answer": decision.answer or state.get("draft_answer", ""),
            "last_error": decision.replan_reason if decision.status == "replan" else None,
        }

    @traceable(
        run_type="chain",
        name="agent_tool_node",
        process_inputs=serialize_state_node_inputs,
        process_outputs=serialize_state_node_outputs,
    )
    def tool(self, state: AgentState) -> dict[str, Any]:
        tool_name = state.get("pending_tool_name")
        arguments = state.get("pending_tool_arguments", {})
        if not tool_name:
            history = list(state.get("execution_history", []))
            history.append(
                {
                    "status": "error",
                    "step_focus": state.get("pending_step_focus", ""),
                    "reasoning": state.get("pending_reasoning", ""),
                    "observation": "No tool name was provided.",
                }
            )
            return {
                "execution_history": history,
                "iteration_count": state.get("iteration_count", 0) + 1,
                "pending_status": None,
                "pending_tool_name": None,
                "pending_tool_arguments": {},
            }

        self._emit("tool_started", f"正在调用工具 {tool_name}", tool_name=tool_name, arguments=arguments)
        result = self.registry.invoke(tool_name, arguments)
        observation = sanitize_tool_output(result.text or result.structured_content or result.error)
        history = list(state.get("execution_history", []))
        history.append(
            {
                "status": "tool_success" if result.success else "tool_error",
                "step_focus": state.get("pending_step_focus", ""),
                "reasoning": state.get("pending_reasoning", ""),
                "tool_name": tool_name,
                "tool_arguments": arguments,
                "observation": observation,
                "success": result.success,
            }
        )
        self._emit(
            "tool_completed",
            f"工具 {tool_name} 调用完成",
            tool_name=tool_name,
            success=result.success,
            observation=observation,
        )

        next_step_index = state.get("current_step_index", 0)
        plan = state["plan"]
        if state.get("pending_advance_to_next_step") and next_step_index < max(len(plan.steps) - 1, 0):
            next_step_index += 1

        return {
            "execution_history": history,
            "iteration_count": state.get("iteration_count", 0) + 1,
            "current_step_index": next_step_index,
            "pending_status": None,
            "pending_tool_name": None,
            "pending_tool_arguments": {},
            "pending_reasoning": "",
            "pending_step_focus": "",
            "pending_advance_to_next_step": False,
            "last_error": None if result.success else result.error,
        }

    @traceable(
        run_type="chain",
        name="agent_finalizer_node",
        process_inputs=serialize_state_node_inputs,
        process_outputs=serialize_state_node_outputs,
    )
    def finalizer(self, state: AgentState) -> dict[str, Any]:
        self._emit("finalizer_started", "正在整理最终答案")
        prompt = self.prompts.render(
            "finalizer",
            {
                "session_context": format_session_context(state),
                "conversation_history": format_conversation_history(state.get("conversation_history", [])),
                "user_query": state["user_query"],
                "plan_summary": state["plan_summary"],
                "execution_history": format_execution_history(state.get("execution_history", [])),
                "draft_answer": state.get("draft_answer", ""),
                "last_error": state.get("last_error") or "",
            },
        )
        response = self.llm.invoke([{"role": "system", "content": prompt}], temperature=0)
        final_answer = response.content.strip() or state.get("draft_answer", "").strip()
        if not final_answer:
            final_answer = self._build_iteration_limit_answer(state)
        self._emit("finalizer_completed", "最终答案已整理完成", final_answer=final_answer)
        return {"final_answer": final_answer}

    def _build_iteration_limit_answer(self, state: AgentState) -> str:
        history_text = format_execution_history(state.get("execution_history", []), max_chars=2400)
        if history_text == "(no actions taken yet)":
            return "The agent reached its iteration limit before completing any meaningful work."
        return (
            "The agent reached its iteration limit before confidently finishing the task. "
            "Here is the evidence gathered so far:\n\n"
            f"{history_text}"
        )
