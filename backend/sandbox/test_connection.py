from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anyio

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "backend" / "config" / "sandbox.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sandbox import build_sandbox_manager


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare and smoke test the OpenSandbox integration."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to the sandbox JSON config.",
    )
    parser.add_argument(
        "--check-health",
        action="store_true",
        help="Call the configured OpenSandbox server health endpoint.",
    )
    parser.add_argument(
        "--create-sandbox",
        action="store_true",
        help="Create a real sandbox and run a probe command.",
    )
    parser.add_argument(
        "--session-id",
        default="sandbox-smoke",
        help="Session ID used when creating the workspace and sandbox.",
    )
    parser.add_argument(
        "--command",
        default="python -c \"import os; print(os.getcwd()); print(sorted(os.listdir()))\"",
        help="Probe command to run inside the sandbox.",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    manager = build_sandbox_manager(config_path=args.config)
    server_config_path = manager.prepare()

    print("[INFO] Sandbox runtime status")
    print(json.dumps(manager.get_runtime_status(), ensure_ascii=False, indent=2))
    print(f"[INFO] Wrote OpenSandbox server config to: {server_config_path}")

    if args.check_health:
        try:
            health = await manager.get_server_health()
        except Exception as exc:
            print(f"[FAIL] Health check failed: {exc}")
            return 1

        print("[INFO] OpenSandbox server health")
        print(json.dumps(health, ensure_ascii=False, indent=2))
        if not health.get("ok"):
            return 1

    if args.create_sandbox:
        try:
            result = await manager.run_smoke_command(
                args.session_id,
                command=args.command,
            )
        except Exception as exc:
            print(f"[FAIL] Sandbox creation or probe command failed: {exc}")
            return 1

        print("[PASS] Sandbox probe command succeeded")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(main))
