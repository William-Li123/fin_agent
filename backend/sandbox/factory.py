from __future__ import annotations

from pathlib import Path

from .config import load_sandbox_config
from .service import OpenSandboxManager


def build_sandbox_manager(
    *,
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> OpenSandboxManager:
    config = load_sandbox_config(config_path=config_path, env_path=env_path)
    return OpenSandboxManager(config)
