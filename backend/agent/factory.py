from __future__ import annotations

from pathlib import Path
from typing import Iterable

from backend.memory import MemoryService
from backend.rag import RAGService
from backend.sandbox import OpenSandboxManager

from .service import AgentService, create_default_agent_service


def build_agent_service(
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
    return create_default_agent_service(
        llm_config_path=llm_config_path,
        env_path=env_path,
        prompt_dir=prompt_dir,
        rag_config_path=rag_config_path,
        mcp_config_dir=mcp_config_dir,
        memory_config_path=memory_config_path,
        rag_service=rag_service,
        memory_service=memory_service,
        sandbox_manager=sandbox_manager,
        max_iterations=max_iterations,
        max_plan_steps=max_plan_steps,
        filesystem_allowed_dirs=filesystem_allowed_dirs,
        filesystem_cwd=filesystem_cwd,
    )
