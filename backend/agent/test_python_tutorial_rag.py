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
    parser = argparse.ArgumentParser(description="Test the agent on a Python tutorial question that should use RAG.")
    parser.add_argument(
        "--query",
        default=(
            "请根据当前知识库中的 Python 教程，解释 Python 的 for...else 语句什么时候执行，"
            "并给一个最小可运行示例。优先使用 RAG 工具，不要联网。"
        ),
        help="Query sent to the agent.",
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

    rag_tool_calls = [
        item
        for item in result.execution_history
        if str(item.get("tool_name", "")).startswith("rag_")
    ]

    print("=== Query ===")
    print(args.query)
    print("\n=== Plan ===")
    print(json.dumps(result.plan.raw or {}, ensure_ascii=False, indent=2))
    print("\n=== Execution History ===")
    print(json.dumps(result.execution_history, ensure_ascii=False, indent=2))
    print("\n=== Final Answer ===")
    print(result.final_answer)
    print("\n=== Validation ===")
    print(
        json.dumps(
            {
                "used_rag_tool": bool(rag_tool_calls),
                "rag_tool_names": [item.get("tool_name") for item in rag_tool_calls],
                "iteration_count": result.iteration_count,
                "final_answer_non_empty": bool(result.final_answer.strip()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if not rag_tool_calls:
        raise SystemExit("Validation failed: the agent did not use any rag_* tool.")
    if not result.final_answer.strip():
        raise SystemExit("Validation failed: the agent returned an empty final answer.")


if __name__ == "__main__":
    main()
