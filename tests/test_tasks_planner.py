"""Planner Phase 1: tasks gain due dates + priorities + a PATCH editor.

These run WITHOUT a model — they exercise the /tasks wiring end-to-end against a TEMP
data dir (never the real .scholar-desk), plus the himmy store/pack contract the feature
leans on (the shared `tasks` pack must keep registering, and must be able to set `due`).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Pin EVERY himmy durable path into a throwaway dir so we never touch the real account.
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HIMMY_TASKS_PATH", str(tmp_path / "tasks.db"))
    # Reset the cwd-keyed tasks-store singleton so it re-opens at the temp path.
    import himmy.api.studio_tasks as studio_tasks

    studio_tasks.reset_tasks_store()
    # Build a fresh app bound to this temp config (create_app() reads env at call time).
    import himmy_app.server as server

    importlib.reload(server)
    app = server.create_app()
    with TestClient(app) as c:
        yield c
    studio_tasks.reset_tasks_store()


def test_add_task_with_due_and_priority(client: TestClient) -> None:
    r = client.post("/tasks", json={"title": "Finish lit review", "due": "2030-01-15", "priority": 3})
    assert r.status_code == 200
    t = r.json()["task"]
    assert t["title"] == "Finish lit review"
    assert t["due"] == "2030-01-15"
    assert t["priority"] == 3
    assert t["done"] is False
    assert "id" in t and "created_at" in t


def test_add_task_defaults_when_no_due_or_priority(client: TestClient) -> None:
    r = client.post("/tasks", json={"title": "bare task"})
    assert r.status_code == 200
    t = r.json()["task"]
    assert t["due"] is None
    assert t["priority"] == 0


def test_add_task_clamps_and_blank_due(client: TestClient) -> None:
    # priority is clamped to 0..3; a blank/whitespace due becomes null.
    r = client.post("/tasks", json={"title": "x", "due": "   ", "priority": 9})
    t = r.json()["task"]
    assert t["due"] is None
    assert t["priority"] == 3


def test_patch_task_due_priority_done(client: TestClient) -> None:
    tid = client.post("/tasks", json={"title": "edit me"}).json()["task"]["id"]

    r = client.patch(f"/tasks/{tid}", json={"due": "2031-02-02", "priority": 2})
    assert r.status_code == 200
    t = r.json()["task"]
    assert t["due"] == "2031-02-02"
    assert t["priority"] == 2
    assert t["done"] is False

    # A partial patch leaves the untouched fields alone.
    t2 = client.patch(f"/tasks/{tid}", json={"done": True}).json()["task"]
    assert t2["done"] is True
    assert t2["due"] == "2031-02-02"
    assert t2["priority"] == 2


def test_patch_unknown_task_is_404(client: TestClient) -> None:
    r = client.patch("/tasks/does-not-exist", json={"priority": 1})
    assert r.status_code == 404


def test_list_carries_priority_and_due(client: TestClient) -> None:
    client.post("/tasks", json={"title": "a", "priority": 1, "due": "2030-03-03"})
    items = client.get("/tasks").json()["tasks"]
    assert any(t["priority"] == 1 and t["due"] == "2030-03-03" for t in items)


def test_tasks_pack_still_registers_and_sets_due(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared himmy tasks pack must keep registering AND be able to set due (+priority),
    # so the AGENT can give deadlines — the additive priority column must not break it.
    monkeypatch.setenv("HIMMY_TASKS_PATH", str(tmp_path / "pack_tasks.db"))
    import himmy.api.studio_tasks as studio_tasks

    studio_tasks.reset_tasks_store()
    try:
        from himmy.services.tools.registry import ToolRegistry
        from himmy.toolkit.config import ToolkitConfig
        from himmy.toolkit.tasks_pack import register_tasks_pack

        registry = ToolRegistry()
        register_tasks_pack(registry, ToolkitConfig())
        names = {t.name for t in registry.list()}
        assert {"list_tasks", "add_task", "complete_task"} <= names

        add = registry.handler_for("add_task")
        out = add({"title": "deadline task", "due": "2030-12-31", "priority": 2})
        assert out["added"] is True
        assert out["due"] == "2030-12-31"
        assert out["priority"] == 2

        # The store row carries due + priority back through list_tasks.
        listed = registry.handler_for("list_tasks")({})
        row = next(t for t in listed["tasks"] if t["title"] == "deadline task")
        assert row["due"] == "2030-12-31"
        assert row["priority"] == 2
    finally:
        studio_tasks.reset_tasks_store()


def test_store_migration_is_idempotent(tmp_path: Path) -> None:
    # Opening an existing DB twice (re-running _migrate) must not raise — proves the
    # pragma-guarded ALTER is idempotent on a DB that already has the priority column.
    from himmy.api.studio_tasks import TasksStore

    path = str(tmp_path / "mig.db")
    s1 = TasksStore(path)
    s1.add("t", due="2030-01-01", priority=3)
    s1.close()
    s2 = TasksStore(path)  # re-open: _migrate runs again, must be a no-op
    rows = s2.list()
    assert rows and rows[0].priority == 3
    s2.close()
