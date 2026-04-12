from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "backend" / "config" / "llm.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm import LLMConfigError, build_llm_provider, load_llm_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test the configured LLM provider connection."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to the LLM JSON config file.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional path to a specific .env file for secret values.",
    )
    parser.add_argument(
        "--mode",
        choices=("config", "chat", "both"),
        default="chat",
        help="config only validates configuration, chat sends a real request, both runs both.",
    )
    parser.add_argument(
        "--message",
        default="Reply with exactly: OK",
        help="Short user message used for the connectivity test.",
    )
    parser.add_argument(
        "--system",
        default="You are a connectivity test assistant. Keep the reply minimal.",
        help="System prompt used for the connectivity test.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Override temperature for the test request.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=32,
        help="Override max tokens for the test request.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        config = load_llm_config(config_path=args.config, env_path=args.env_file)
    except LLMConfigError as exc:
        print(f"[ERROR] Invalid LLM configuration: {exc}")
        return 1

    print("[INFO] Loaded LLM configuration")
    print(
        json.dumps(
            {
                "provider": config.provider,
                "model_id": config.model_id,
                "base_url": config.base_url,
                "config_path": str(config.config_path) if config.config_path else None,
                "env_path": str(config.env_path) if config.env_path else None,
                "is_local": config.is_local,
                "api_key_present": bool(config.api_key),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if args.mode == "config":
        print("[PASS] Configuration resolved successfully.")
        return 0

    try:
        provider = build_llm_provider(config_path=args.config, env_path=args.env_file)
    except Exception as exc:
        print(f"[ERROR] Failed to build provider: {exc}")
        return 1

    messages: list[dict[str, str]] = [
        {"role": "system", "content": args.system},
        {"role": "user", "content": args.message},
    ]

    try:
        response = provider.invoke(
            messages,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
        )
    except Exception as exc:
        print(f"[ERROR] Connection test failed: {type(exc).__name__}: {exc}")
        return 1

    print("[PASS] LLM request completed successfully.")
    print(
        json.dumps(
            {
                "provider": response.provider,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "usage": response.usage,
                "content": response.content,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if args.mode == "both":
        print("[PASS] Configuration and chat connectivity checks both succeeded.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
