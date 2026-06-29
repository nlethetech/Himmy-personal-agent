"""Today's plan — Himmy prioritises the task board into a focused daily to-do.

Offline + deterministic: the model pass is monkeypatched off so the deterministic ranking (overdue
→ due today → due soon → important) is exercised, plus the empty case and the per-day cache. The
conftest isolates the tasks store, so seeding here never touches the real board.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from himmy_app.config import load_config
from himmy_app.dayplan import DayPlan, _deterministic


def _seed(title: str, due: str | None = None, priority: int = 0) -> None:
    from himmy.api.studio_tasks import get_tasks_store

    get_tasks_store().add(title=title, due=due, priority=priority)


@pytest.fixture()
def cfg(monkeypatch):
    # belt-and-suspenders over conftest: reset the himmy tasks-store singleton per test
    import himmy.api.studio_tasks as st

    monkeypatch.setattr(st, "_STORE", None, raising=False)
    # Isolate the calendar from tests — the Google token lives in the keychain (not the data dir),
    # so without this _today_events would read the REAL connected calendar. The task path is what
    # these tests exercise; the calendar merge is verified live.
    import himmy_app.dayplan as dp

    async def _no_events(_cfg):
        return []
    monkeypatch.setattr(dp, "_today_events", _no_events)
    return load_config()


def test_deterministic_ordering():
    today = datetime.date.today()
    y = (today - datetime.timedelta(days=3)).isoformat()
    t = today.isoformat()
    soon = (today + datetime.timedelta(days=2)).isoformat()
    tasks = [
        {"id": "1", "title": "Undated low", "due": None, "priority": 0},
        {"id": "2", "title": "Due soon", "due": soon, "priority": 0},
        {"id": "3", "title": "Overdue", "due": y, "priority": 0},
        {"id": "4", "title": "Due today", "due": t, "priority": 0},
        {"id": "5", "title": "Important undated", "due": None, "priority": 3},
    ]
    out = _deterministic(tasks)
    assert [p["title"] for p in out[:3]] == ["Overdue", "Due today", "Due soon"]
    assert out[0]["reason"] == "overdue" and out[1]["reason"] == "due today"


def test_empty_when_no_tasks_or_events(cfg):
    # No open tasks and no connected calendar in the test env → an empty plan.
    r = asyncio.run(DayPlan(cfg).get(force=True))
    assert r["ok"] and r["open_tasks"] == 0 and r["total"] == 0 and r["items"] == []


def test_plan_from_tasks_offline(cfg, monkeypatch):
    import himmy_app.dayplan as dp

    async def _no_model(_cfg, _tasks):   # force the deterministic path (no network)
        return None
    monkeypatch.setattr(dp, "_model_plan", _no_model)

    today = datetime.date.today()
    _seed("Pay rent", (today - datetime.timedelta(days=1)).isoformat())  # overdue
    _seed("Ship demo", today.isoformat())                                # due today
    _seed("Someday idea", None)                                          # undated
    r = asyncio.run(DayPlan(cfg).get(force=True))
    assert r["open_tasks"] == 3
    titles = [p["title"] for p in r["items"] if p["kind"] == "task"]
    assert titles[0] == "Pay rent" and titles[1] == "Ship demo"          # urgent first
    # a completed task drops out of the next plan
    from himmy.api.studio_tasks import get_tasks_store
    store = get_tasks_store()
    done_id = [t.id for t in store.list() if t.title == "Pay rent"][0]
    store.set_done(done_id, True)
    r2 = asyncio.run(DayPlan(cfg).get(force=True))
    assert "Pay rent" not in [p["title"] for p in r2["items"]]


def test_done_tracker_toggles_and_resets(cfg):
    dp = DayPlan(cfg)
    assert dp._done_today() == set()
    dp.toggle_done("evt-1", True)
    assert "evt-1" in dp._done_today()
    dp.toggle_done("evt-1", False)
    assert "evt-1" not in dp._done_today()


def test_cache_reused_until_tasks_change(cfg, monkeypatch):
    import himmy_app.dayplan as dp

    calls = {"n": 0}

    async def _counting(_cfg, tasks):
        calls["n"] += 1
        return {"note": "x", "plan": [{"task_id": tasks[0]["id"], "title": tasks[0]["title"],
                                       "due": tasks[0].get("due"), "reason": "go"}]}
    monkeypatch.setattr(dp, "_model_plan", _counting)

    _seed("Only task", None)
    asyncio.run(DayPlan(cfg).get())          # builds (1 model call)
    asyncio.run(DayPlan(cfg).get())          # same task set → cached, no new call
    assert calls["n"] == 1
    _seed("New task", None)                   # task set changed → re-plan
    asyncio.run(DayPlan(cfg).get())
    assert calls["n"] == 2
