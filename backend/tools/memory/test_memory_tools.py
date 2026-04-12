from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tools import ToolRegistry
from backend.tools.memory import register_memory_tools


def main() -> None:
    registry = ToolRegistry()
    register_memory_tools(registry)
    print("registered tools:")
    for name in sorted(tool.name for tool in registry.list_tools()):
        print(f"- {name}")

    status = registry.invoke("memory_get_status")
    print("\nmemory_get_status:")
    print(status.text)
    if status.structured_content is not None:
        print(json.dumps(status.structured_content, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
