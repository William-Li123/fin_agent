from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.memory import build_memory_service


def main() -> None:
    service = build_memory_service()
    try:
        status = service.get_runtime_status()
        print(json.dumps(status.details | {
            "oracle_configured": status.oracle_configured,
            "oracle_reachable": status.oracle_reachable,
            "schema_ready": status.schema_ready,
            "embedding_ready": status.embedding_ready,
            "llm_ready": status.llm_ready,
            "messages": status.messages,
        }, ensure_ascii=False, indent=2))

        if not status.oracle_configured:
            print("SKIP: Oracle credentials are not configured.")
            return
        if not status.oracle_reachable:
            print("SKIP: Oracle is configured but not reachable.")
            return

        result = service.upsert_persona_memory(
            memory_key="preferred_language",
            memory_value="Chinese",
            category="preference",
            importance=0.9,
            always_include=True,
            source_note="memory smoke test",
        )
        print(
            json.dumps(
                {
                    "persona_id": result.persona_id,
                    "owner_scope": result.owner_scope,
                    "memory_key": result.memory_key,
                    "memory_value": result.memory_value,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        service.close()


if __name__ == "__main__":
    main()
