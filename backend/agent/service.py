from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
from collections.abc import Callable
from typing import Any
from typing import Iterable

from backend.llm import BaseLLMProvider, build_llm_provider
from backend.memory import MemoryService
from backend.observability import bootstrap_langsmith_env
from backend.observability.langsmith import serialize_agent_run_inputs, serialize_agent_run_outputs
from backend.rag import RAGService
from backend.sandbox import OpenSandboxManager
from backend.tools import ToolRegistry
from langsmith import traceable

from .graph import build_agent_graph
from .nodes import AgentNodes
from .prompts import PromptRepository
from .schemas import AgentEvent, AgentRunResult, PlanOutput
from .state import AgentState
from .tooling import build_default_tool_registry


class AgentService:
    def __init__(
        self,
        *,
        llm: BaseLLMProvider,
        registry: ToolRegistry,
        prompts: PromptRepository,
        max_iterations: int = 8,
        max_plan_steps: int = 5,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.prompts = prompts
        self.max_iterations = max_iterations
        self.max_plan_steps = max_plan_steps
        self._nodes = AgentNodes(llm=llm, registry=registry, prompts=prompts)
        self._graph = build_agent_graph(self._nodes)

    @traceable(
        run_type="chain",
        name="agent_run",
        process_inputs=serialize_agent_run_inputs,
        process_outputs=serialize_agent_run_outputs,
    )
    def run(
        self,
        user_query: str,
        *,
        session_id: str | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        event_handler: Callable[[AgentEvent], None] | None = None,
    ) -> AgentRunResult:
        self._nodes.set_event_handler(event_handler)
        if event_handler is not None:
            event_handler(
                AgentEvent(
                    event_type="run_started",
                    message="开始处理用户请求",
                    data={"session_id": session_id, "user_query": user_query},
                )
            )
        initial_state: AgentState = {
            "session_id": session_id,
            "user_query": user_query,
            "conversation_history": list(conversation_history or []),
            "max_iterations": self.max_iterations,
            "max_plan_steps": self.max_plan_steps,
            "tool_catalog": self._format_tool_catalog(),
            "registry": self.registry,
            "execution_history": [],
            "iteration_count": 0,
            "current_step_index": 0,
            "pending_status": None,
            "pending_tool_name": None,
            "pending_tool_arguments": {},
            "pending_reasoning": "",
            "pending_step_focus": "",
            "pending_advance_to_next_step": False,
            "draft_answer": "",
            "final_answer": "",
            "last_error": None,
        }
        try:
            final_state = self._graph.invoke(initial_state)
            plan = final_state["plan"]
            result = AgentRunResult(
                user_query=user_query,
                final_answer=str(final_state.get("final_answer", "")),
                plan=plan,
                iteration_count=int(final_state.get("iteration_count", 0)),
                execution_history=list(final_state.get("execution_history", [])),
                raw_state=_serialize_state(final_state),
            )
            if event_handler is not None:
                event_handler(
                    AgentEvent(
                        event_type="run_completed",
                        message="本轮对话处理完成",
                        data={
                            "session_id": session_id,
                            "iteration_count": result.iteration_count,
                        },
                    )
                )
            return result
        finally:
            self._nodes.set_event_handler(None)

    def _format_tool_catalog(self) -> str:
        sections: list[str] = []
        for tool in self.registry.list_tools():
            schema = json.dumps(_serialize_value(tool.input_schema), ensure_ascii=False)
            sections.append(
                "\n".join(
                    [
                        f"Tool: {tool.name}",
                        f"Description: {tool.description}",
                        f"Input schema: {schema}",
                    ]
                )
            )
        return "\n\n".join(sections) if sections else "(no tools available)"


def create_default_agent_service(
    *,
    llm_config_path: str | Path | None = None,
    env_path: str | Path | None = None,
    prompt_dir: str | Path | None = None,
    rag_config_path: str | Path | None = None,
    mcp_config_dir: str | Path | None = None,
    memory_config_path: str | Path | None = None,
    rag_service: RAGService | None = None,
    memory_service: MemoryService | None = None,
    sandbox_manager: OpenSandboxManager | None = None,
    max_iterations: int = 8,
    max_plan_steps: int = 5,
    filesystem_allowed_dirs: Iterable[str | Path] | None = None,
    filesystem_cwd: str | Path | None = None,
) -> AgentService:
    bootstrap_langsmith_env(env_path)
    llm = build_llm_provider(config_path=llm_config_path, env_path=env_path)
    tools = build_default_tool_registry(
        env_path=env_path,
        rag_config_path=rag_config_path,
        mcp_config_dir=mcp_config_dir,
        memory_config_path=memory_config_path,
        rag_service=rag_service,
        memory_service=memory_service,
        sandbox_manager=sandbox_manager,
        filesystem_allowed_dirs=filesystem_allowed_dirs,
        filesystem_cwd=filesystem_cwd,
    )
    return AgentService(
        llm=llm,
        registry=tools.registry,
        prompts=PromptRepository(prompt_dir=prompt_dir),
        max_iterations=max_iterations,
        max_plan_steps=max_plan_steps,
    )


def _serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _serialize_value(value)
        for key, value in state.items()
        if key != "registry"
    }


def _serialize_value(value: Any) -> Any:
    if isinstance(value, PlanOutput):
        return asdict(value)
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    return value
