"""Wave 3 — "Himmy, plan my week".

The planner gathers OPEN, unscheduled tasks and asks a real LLM to lay them into weekday slots.
These tests keep the suite OFFLINE + deterministic by monkeypatching the OpenRouter HTTP call, and
prove: (1) a model reply parses into clean blocks, (2) overlapping blocks on the same day are
dropped, (3) out-of-range / weekend / malformed blocks are rejected, and (4) it fails open with no
API key and with no open tasks.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest


@pytest.fixture()
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Pin every durable path into a throwaway dir so we never touch the real account.
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HIMMY_TASKS_PATH", str(tmp_path / "tasks.db"))
    import himmy.api.studio_tasks as studio_tasks

    studio_tasks.reset_tasks_store()
    from himmy_app.config import load_config

    yield load_config()
    studio_tasks.reset_tasks_store()


def _add_task(title: str, *, due: str | None = None, priority: int = 0) -> str:
    from himmy.api.studio_tasks import get_tasks_store

    t = get_tasks_store().add(title, due=due, priority=priority)
    return t.id


def _next_weekday(base: datetime.date) -> datetime.date:
    """First weekday on/after ``base`` (so generated days land inside the horizon + Mon–Fri)."""
    d = base
    while d.weekday() > 4:
        d += datetime.timedelta(days=1)
    return d


def _patch_llm(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    """Replace the OpenRouter POST so suggest_week parses a canned reply, fully offline."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": content}}]}

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def post(self, *a, **k):
            return _Resp()

    import himmy_app.planner as planner

    monkeypatch.setattr(planner.httpx, "AsyncClient", _Client)


async def test_parses_model_json_into_blocks(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    import himmy_app.planner as planner

    tid = _add_task("Finish lit review", due="2099-01-01", priority=3)
    day = _next_weekday(datetime.date.today()).isoformat()
    reply = json.dumps({"blocks": [
        {"task_id": tid, "title": "Finish lit review", "day": day,
         "start": "09:00", "end": "10:30", "reason": "high priority"},
    ]})
    _patch_llm(monkeypatch, reply)

    out = await planner.suggest_week(cfg)
    assert out["ok"] is True
    assert len(out["blocks"]) == 1
    b = out["blocks"][0]
    assert b["task_id"] == tid
    assert b["title"] == "Finish lit review"
    assert b["start"] == "09:00" and b["end"] == "10:30"


async def test_drops_overlapping_blocks(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    import himmy_app.planner as planner

    a = _add_task("A", priority=2)
    b = _add_task("B", priority=1)
    day = _next_weekday(datetime.date.today()).isoformat()
    reply = json.dumps({"blocks": [
        {"task_id": a, "title": "A", "day": day, "start": "09:00", "end": "10:30", "reason": ""},
        # Overlaps A on the same day → must be dropped.
        {"task_id": b, "title": "B", "day": day, "start": "10:00", "end": "11:00", "reason": ""},
    ]})
    _patch_llm(monkeypatch, reply)

    out = await planner.suggest_week(cfg)
    assert out["ok"] is True
    assert [bl["title"] for bl in out["blocks"]] == ["A"]


async def test_rejects_out_of_range_and_malformed(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    import himmy_app.planner as planner

    tid = _add_task("Valid", priority=2)
    good_day = _next_weekday(datetime.date.today()).isoformat()
    far = (datetime.date.today() + datetime.timedelta(days=60)).isoformat()  # past the 7-day horizon
    reply = json.dumps({"blocks": [
        {"task_id": tid, "title": "Valid", "day": good_day, "start": "09:00", "end": "10:00", "reason": ""},
        {"task_id": tid, "title": "TooFar", "day": far, "start": "09:00", "end": "10:00", "reason": ""},
        {"task_id": tid, "title": "BadTime", "day": good_day, "start": "25:00", "end": "26:00", "reason": ""},
        {"task_id": tid, "title": "EndBeforeStart", "day": good_day, "start": "14:00", "end": "13:00", "reason": ""},
        {"title": "NoDay", "start": "09:00", "end": "10:00", "reason": ""},
    ]})
    _patch_llm(monkeypatch, reply)

    out = await planner.suggest_week(cfg)
    assert out["ok"] is True
    assert [bl["title"] for bl in out["blocks"]] == ["Valid"]


async def test_unknown_task_id_is_blanked(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    import himmy_app.planner as planner

    _add_task("Real task", priority=1)
    day = _next_weekday(datetime.date.today()).isoformat()
    reply = json.dumps({"blocks": [
        {"task_id": "ghost-id", "title": "Real task", "day": day,
         "start": "09:00", "end": "10:00", "reason": ""},
    ]})
    _patch_llm(monkeypatch, reply)

    out = await planner.suggest_week(cfg)
    assert out["ok"] is True
    # A task_id the model invented (not in the open set) is blanked so we never mis-link a task.
    assert out["blocks"][0]["task_id"] == ""


async def test_fails_open_without_api_key(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    import himmy_app.planner as planner

    _add_task("Something to do", priority=2)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    out = await planner.suggest_week(cfg)
    assert out["ok"] is False
    assert out["blocks"] == []
    assert out["message"]


async def test_fails_open_with_no_open_tasks(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    import himmy_app.planner as planner

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    out = await planner.suggest_week(cfg)
    assert out["ok"] is False
    assert out["blocks"] == []
    assert "No open" in out["message"]


async def test_scheduled_tasks_are_not_candidates(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    import himmy_app.planner as planner
    from himmy_app.tasks_extra import TaskExtrasStore

    tid = _add_task("Already blocked", priority=2)
    TaskExtrasStore(cfg).set(tid, scheduled_start="2099-01-01T09:00:00", scheduled_end="2099-01-01T10:00:00")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    # The only task is already scheduled → no candidates → fails open before any LLM call.
    out = await planner.suggest_week(cfg)
    assert out["ok"] is False
    assert "No open" in out["message"]
