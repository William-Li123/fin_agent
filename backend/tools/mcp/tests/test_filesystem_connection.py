from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anyio

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "backend" / "config" / "mcp" / "filesystem.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tools import ToolRegistry
from backend.tools.mcp import register_filesystem_tools_from_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test for the official filesystem MCP server."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to the filesystem MCP JSON config.",
    )
    parser.add_argument(
        "--list-path",
        type=Path,
        default=PROJECT_ROOT / "agent_workspace",
        help="Directory to pass into filesystem__list_directory.",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    registry = ToolRegistry()

    tools = await register_filesystem_tools_from_json(
        registry,
        config_path=args.config,
    )

    print("[INFO] Registered filesystem MCP tools")
    print(json.dumps([tool.name for tool in tools], ensure_ascii=False, indent=2))

    list_allowed_result = await registry.ainvoke("filesystem__list_allowed_directories", {})
    print("[PASS] list_allowed_directories succeeded")
    print(list_allowed_result.text)

    list_directory_result = await registry.ainvoke(
        "filesystem__list_directory",
        {"path": str(args.list_path.resolve())},
    )
    print("[PASS] list_directory succeeded")
    print(list_directory_result.text)

    return 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(main))
