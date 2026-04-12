from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anyio

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "backend" / "config" / "mcp" / "bing_cn_search.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tools import ToolRegistry
from backend.tools.mcp import register_bing_cn_search_tools_from_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test for the Bing Chinese search MCP server."
    )
    parser.add_argument(
        "--query",
        default="Python decorators 教程",
        help="Search query to send through the MCP server.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=5,
        help="How many search results to request.",
    )
    parser.add_argument(
        "--crawl-count",
        type=int,
        default=1,
        help="How many search results to pass into crawl_webpage.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to the Bing CN search MCP JSON config.",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    registry = ToolRegistry()

    tools = await register_bing_cn_search_tools_from_json(
        registry,
        config_path=args.config,
    )

    print("[INFO] Registered Bing CN MCP tools")
    print(json.dumps([tool.name for tool in tools], ensure_ascii=False, indent=2))

    search_result = await registry.ainvoke(
        "bing__bing_search",
        {
            "query": args.query,
            "count": args.count,
            "offset": 0,
        },
    )
    print("[PASS] bing_search succeeded")
    print(search_result.text[:1500])

    parsed_search_payload = json.loads(search_result.text)
    search_hits = list(parsed_search_payload.get("results", []))
    if not search_hits:
        print("[WARN] No search results returned, skipping crawl_webpage.")
        return 0

    selected_hits = search_hits[: max(args.crawl_count, 1)]
    uuids = [str(hit["uuid"]) for hit in selected_hits if "uuid" in hit]
    url_map = {
        str(hit["uuid"]): str(hit["url"])
        for hit in selected_hits
        if "uuid" in hit and "url" in hit
    }

    crawl_result = await registry.ainvoke(
        "bing__crawl_webpage",
        {
            "uuids": uuids,
            "urlMap": url_map,
        },
    )
    print("[PASS] crawl_webpage succeeded")
    print(crawl_result.text[:1200])

    return 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(main))
