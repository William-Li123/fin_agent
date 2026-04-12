from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anyio

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "backend" / "config" / "mcp" / "fetch.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tools import ToolRegistry
from backend.tools.mcp import register_fetch_tools_from_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test for the official fetch MCP server."
    )
    parser.add_argument(
        "--url",
        default="https://www.python.org/",
        help="URL to fetch through the MCP server.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to the fetch MCP JSON config.",
    )
    parser.add_argument(
        "--max-length",
        type=int,
        default=600,
        help="Maximum number of characters to return.",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    registry = ToolRegistry()

    tools = await register_fetch_tools_from_json(
        registry,
        config_path=args.config,
    )

    print("[INFO] Registered fetch MCP tools")
    print(json.dumps([tool.name for tool in tools], ensure_ascii=False, indent=2))

    result = await registry.ainvoke(
        "fetch__fetch",
        {
            "url": args.url,
            "max_length": args.max_length,
            "raw": False,
        },
    )
    print("[PASS] fetch succeeded")
    print(result.text)

    return 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(main))
