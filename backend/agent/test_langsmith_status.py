from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.agent import build_agent_service
from backend.observability import langsmith_is_enabled


def main() -> None:
    parser = argparse.ArgumentParser(description="Check LangSmith readiness and optionally run a traced agent query.")
    parser.add_argument(
        "--run-query",
        action="store_true",
        help="Run a small agent query after checking LangSmith readiness.",
    )
    parser.add_argument(
        "--query",
        default="请先调用 rag_list_indexed_sources 工具，然后用中文列出当前知识库里已经索引的文档名。",
        help="Query used when --run-query is passed.",
    )
    args = parser.parse_args()

    enabled = langsmith_is_enabled()
    print(f"langsmith_enabled={enabled}")
    if not enabled:
        print("Missing LangSmith runtime config. Set LANGSMITH_TRACING=true and LANGSMITH_API_KEY in the project .env.")
        return

    print("LangSmith runtime config looks enabled.")
    if not args.run_query:
        return

    agent = build_agent_service()
    result = agent.run(args.query)
    print("Agent run completed. Check the configured LangSmith project for a new trace.")
    print(f"final_answer_length={len(result.final_answer)}")
    print(f"iteration_count={result.iteration_count}")


if __name__ == "__main__":
    main()
