"""Frozen-backend entry point for the packaged Himmy app.

PyInstaller turns this into the standalone ``himmy-backend`` (``himmy-backend.exe`` on Windows)
that Electron spawns when the app is packaged — no system Python, no venv, no terminal.

It mirrors ``himmy_app.server:main`` but passes the ASGI app OBJECT to uvicorn instead of the
``"himmy_app.server:app"`` import string — the string form makes uvicorn re-import the module by
name, which is brittle inside a frozen bundle. Reading host/port from the environment keeps it
identical to the dev backend, so Electron can inject HIMMY_APP_PORT / HIMMY_APP_TOKEN /
HIMMY_APP_DATA_DIR exactly as it does in dev.

On Windows the exe is built *windowed* (console=False, so no black cmd window appears), which
means it has no reliable stdout when launched by Electron. So we ALSO mirror logs to a file under
the per-user data dir. On macOS this is purely additive — stdout still flows to Electron's stdio.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
from pathlib import Path


def _log_dir() -> Path:
    base = os.environ.get("HIMMY_APP_DATA_DIR")
    if not base:
        # Mirror config.py's per-user default as a last resort (Windows %APPDATA%, else ~).
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        base = os.path.join(appdata, "Himmy")
    d = Path(base) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _configure_file_logging() -> None:
    """Send root + uvicorn logs to a file so a windowed (no-console) exe still logs."""
    try:
        handler = logging.FileHandler(_log_dir() / "backend.log", encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
    except Exception:
        # Never let logging setup crash the backend.
        pass


def main() -> None:
    # Frozen apps that ever fork (some deps spawn workers) need this before anything else.
    multiprocessing.freeze_support()

    _configure_file_logging()

    host = os.environ.get("HIMMY_APP_HOST", "127.0.0.1")
    port = int(os.environ.get("HIMMY_APP_PORT", "8131"))

    # Import the configured ASGI app and run it directly.
    from himmy_app.server import app
    import uvicorn

    # print() is best-effort (no console on a windowed Windows exe); the file handler above also
    # captures the startup line + uvicorn's INFO logs.
    try:
        print(f"Himmy backend (frozen) -> http://{host}:{port}", flush=True)
    except Exception:
        pass
    logging.getLogger("himmy.backend").info(
        "Himmy backend (frozen) -> http://%s:%s", host, port
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
