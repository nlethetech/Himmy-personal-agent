"""Test isolation — keep the WHOLE suite away from the user's real workspace.

``himmy_app.config.load_config()`` pins the durable store paths (tasks / storage / memory /
conversations / routines) via ``os.environ.setdefault(...)``. That setdefault STICKS for the rest
of the process once any code resolves it against the default data dir — so a test that seeds, say,
the tasks store could silently write into the real ``~/.scholar-desk`` (this actually happened: the
proactive tests dropped "Pay the electricity bill" tasks onto the live board).

This autouse fixture runs before EVERY test: it points ``HIMMY_APP_DATA_DIR`` at a fresh tmp dir and
CLEARS the derived path vars, so each test's first ``load_config()`` re-resolves them against the
isolated tmp dir. Per-test fixtures that set their own ``HIMMY_APP_DATA_DIR`` still work (the derived
paths follow whichever data dir is active when load_config first runs in that test). Net effect: no
test can ever touch the real workspace, regardless of call order or setdefault stickiness.
"""

from __future__ import annotations

import pytest

#: The path env vars config.load_config() pins via setdefault — cleared per test so they re-resolve
#: against the isolated tmp data dir instead of carrying a sticky real-workspace value across tests.
_DERIVED_PATH_VARS = (
    "HIMMY_TASKS_PATH",
    "HIMMY_STORE_PATH",
    "HIMMY_MEMORY_PATH",
    "HIMMY_CONVERSATIONS_PATH",
    "HIMMY_ROUTINES_PATH",
)


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    for var in _DERIVED_PATH_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "workspace"))
    yield
