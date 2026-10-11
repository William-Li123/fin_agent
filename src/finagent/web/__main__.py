"""Run one local HTTP server without initializing a budget or making API calls."""
from __future__ import annotations

import argparse
from pathlib import Path


def serve(state_dir: Path, host: str = "127.0.0.1", port: int = 8000,
          frontend_dir: Path | None = None):
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The web interface binds to loopback only")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    try:
        import uvicorn

        from .app import create_app
    except ImportError as error:
        raise RuntimeError('Install the optional web dependencies: pip install -e ".[web]"') from error
    if frontend_dir is None:
        candidate = Path(__file__).resolve().parents[3] / "frontend" / "dist"
        if candidate.is_dir():
            frontend_dir = candidate
    app = create_app(state_dir, frontend_dir, allowed_ports={port, 5173})
    uvicorn.run(app, host=host, port=port, workers=1, access_log=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local financial research workspace")
    parser.add_argument("--state", type=Path, default=Path(".finagent"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--frontend", type=Path)
    args = parser.parse_args(argv)
    serve(args.state, args.host, args.port, args.frontend)


if __name__ == "__main__":
    main()
