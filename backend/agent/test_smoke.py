from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.agent import build_agent_service


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a smoke test for the LangGraph agent.")
    parser.add_argument(
        "--query",
        default="请先调用 rag_list_indexed_sources 工具，然后用中文列出当前知识库里已经索引的文档名。",
        help="User query to send into the agent.",
    )
    parser.add_argument(
        "--llm-config",
        default=str(Path(__file__).resolve().parents[1] / "config" / "llm.json"),
        help="Path to the LLM JSON config file.",
    )
    parser.add_argument(
        "--rag-config",
        default=str(Path(__file__).resolve().parents[1] / "config" / "rag.json"),
        help="Path to the RAG JSON config file.",
    )
    parser.add_argument(
        "--mcp-config-dir",
        default=str(Path(__file__).resolve().parents[1] / "config" / "mcp"),
        help="Directory containing MCP JSON config files.",
    )
    args = parser.parse_args()

    agent = build_agent_service(
        llm_config_path=args.llm_config,
        rag_config_path=args.rag_config,
        mcp_config_dir=args.mcp_config_dir,
    )
    result = agent.run(args.query)

    print("=== Plan ===")
    print(json.dumps(result.plan.raw or {}, ensure_ascii=False, indent=2))
    print("\n=== Execution History ===")
    print(json.dumps(result.execution_history, ensure_ascii=False, indent=2))
    print("\n=== Final Answer ===")
    print(result.final_answer)


if __name__ == "__main__":
    main()
