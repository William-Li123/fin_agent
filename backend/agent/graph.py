from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, StateGraph

from .nodes import AgentNodes
from .state import AgentState


def build_agent_graph(nodes: AgentNodes):
    graph = StateGraph(AgentState)
    graph.add_node("planner", nodes.planner)
    graph.add_node("executor", nodes.executor)
    graph.add_node("tool", nodes.tool)
    graph.add_node("finalizer", nodes.finalizer)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "executor")
    graph.add_conditional_edges(
        "executor",
        _route_after_executor,
        {
            "tool": "tool",
            "planner": "planner",
            "finalizer": "finalizer",
        },
    )
    graph.add_edge("tool", "executor")
    graph.add_edge("finalizer", END)
    return graph.compile()


def _route_after_executor(state: AgentState) -> Literal["tool", "planner", "finalizer"]:
    status = (state.get("pending_status") or "finish").strip().lower()
    if status == "tool":
        return "tool"
    if status == "replan":
        return "planner"
    return "finalizer"
