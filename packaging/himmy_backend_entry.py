"""Frozen-backend entry point for the packaged Himmy.app.

This is what PyInstaller turns into the standalone ``himmy-backend`` binary that the
Electron app spawns when it is packaged (no system Python, no venv, no terminal).

It mirrors ``himmy_app.server:main`` but passes the ASGI app OBJECT to uvicorn instead of
the ``"himmy_app.server:app"`` import string — the string form makes uvicorn re-import the
module by name, which is brittle inside a one-file/one-dir frozen bundle. Reading host/port
from the environment keeps it identical to the dev backend, so the Electron main process can
inject HIMMY_APP_PORT / HIMMY_APP_TOKEN / HIMMY_APP_DATA_DIR exactly as it does in dev.
"""

from __future__ import annotations

import multiprocessing
import os


def main() -> None:
    # Frozen apps that ever fork (some deps spawn workers) need this before anything else.
    multiprocessing.freeze_support()

    host = os.environ.get("HIMMY_APP_HOST", "127.0.0.1")
    port = int(os.environ.get("HIMMY_APP_PORT", "8131"))

    # Import the configured ASGI app and run it directly.
    from himmy_app.server import app
    import uvicorn

    print(f"Himmy backend (frozen) -> http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
