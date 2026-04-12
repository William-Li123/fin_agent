from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anyio

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sandbox import build_sandbox_manager
from backend.session import SessionStore
from backend.tools import ToolRegistry, register_terminal_tools


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test for custom terminal tools backed by OpenSandbox."
    )
    parser.add_argument(
        "--session-name",
        default="Terminal Tool Smoke",
        help="Name for the temporary smoke test session.",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    sandbox_manager = build_sandbox_manager()
    sandbox_manager.prepare()

    health = await sandbox_manager.get_server_health()
    if not health.get("ok"):
        print("[FAIL] OpenSandbox server is not healthy.")
        print(json.dumps(health, ensure_ascii=False, indent=2))
        return 1

    store = SessionStore()
    session = store.create_session(args.session_name)
    binding = sandbox_manager.describe_session_binding(session.session_id)
    session = store.update_metadata(session.session_id, {"sandbox": binding})

    workspace = sandbox_manager.ensure_session_workspace(session.session_id)
    (workspace / "hello.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n\n"
        "if __name__ == '__main__':\n"
        "    print('hello from sandbox')\n",
        encoding="utf-8",
    )
    (workspace / "test_hello.py").write_text(
        "import unittest\n"
        "import hello\n\n"
        "class TestHello(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(hello.add(2, 3), 5)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )

    registry = ToolRegistry()
    register_terminal_tools(registry)

    print("[INFO] Registered terminal tools")
    print(json.dumps([tool.name for tool in registry.list_tools()], ensure_ascii=False, indent=2))

    status_result = await registry.ainvoke(
        "terminal_get_session_status",
        {"session_id": session.session_id},
    )
    print("[PASS] terminal_get_session_status")
    print(status_result.text)

    run_file_result = await registry.ainvoke(
        "terminal_run_python_file",
        {
            "session_id": session.session_id,
            "path": "hello.py",
        },
    )
    print("[PASS] terminal_run_python_file")
    print(run_file_result.text)

    unittest_result = await registry.ainvoke(
        "terminal_run_python_unittest",
        {
            "session_id": session.session_id,
            "start_directory": ".",
            "pattern": "test_hello.py",
        },
    )
    print("[PASS] terminal_run_python_unittest")
    print(unittest_result.text)

    if not (status_result.success and run_file_result.success and unittest_result.success):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(main))
