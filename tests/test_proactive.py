"""Himmy's proactive brain — deterministic rules, dedup, dismiss/snooze, and store mechanics.

These prove the no-model core of :mod:`himmy_app.proactive` against a FRESH scratch data dir
(``HIMMY_APP_DATA_DIR`` under ``tmp_path``), so nothing touches the live workspace:

  * seed a task due today + a saved Food budget (Rs 300) in the profile vault + a Rs 2000 Food
    expense, run ``notice`` with the model pass disabled, and assert a budget observation AND a
    task observation were created;
  * run ``notice`` again and assert NO duplicates are created (stable-key dedup);
  * dismiss one and assert it leaves the active list;
  * snooze one and assert it is hidden (and re-activates once its time passes).

The single cheap connect-the-dots model pass is monkeypatched to a no-op here (it's exercised live
elsewhere) so the deterministic layer is tested offline and deterministically.
"""

from __future__ import annotations

import asyncio

import pytest

from himmy_app.config import load_config


@pytest.fixture()
def scratch(tmp_path, monkeypatch):
    """A fresh data dir + reset module singletons so the proactive store/inbox are isolated."""
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data"))
    # Reset the cached singletons so they re-open under the scratch data dir.
    import himmy_app.proactive as proactive
    import himmy_app.routines as routines

    monkeypatch.setattr(proactive, "_STORE", None)
    monkeypatch.setattr(routines, "_INBOX", None)
    # Stub the model pass to a no-op so the test is offline + deterministic.
    async def _no_connect(_cfg, _snap):
        return []

    monkeypatch.setattr(proactive, "_connect_observations", _no_connect)
    return load_config()


def _seed_task_due_today(cfg) -> None:
    from himmy.api.studio_tasks import get_tasks_store

    import datetime

    store = get_tasks_store()
    # studio_tasks Task store: create a task due today via its public API.
    store.add(title="Pay the electricity bill", due=datetime.date.today().isoformat())


def _seed_food_budget(cfg, npr: int = 300) -> None:
    from himmy_app import user_profile

    user_profile.save_user_layer({"details": {"Food budget": f"Rs {npr}"}}, cfg)


def _seed_food_expense(cfg, amount: float = 2000.0) -> None:
    from himmy_app.finance import ExpenseStore

    ExpenseStore(cfg).add({"merchant": "Restaurant", "amount": amount, "category": "Food"})


# ---------------------------------------------------------------------------------------
def test_level_default_and_set(scratch):
    from himmy_app import proactive

    assert proactive.get_level(scratch) == "always"
    assert proactive.set_level("gentle", scratch) == "gentle"
    assert proactive.get_level(scratch) == "gentle"
    # invalid → default
    assert proactive.set_level("nonsense", scratch) == "always"


def test_notice_creates_budget_and_task_then_dedups(scratch):
    from himmy_app import proactive

    _seed_task_due_today(scratch)
    _seed_food_budget(scratch, 300)
    _seed_food_expense(scratch, 2000.0)

    # Push off (no quiet-hours / bell coupling) so the test is about creation + dedup.
    summary = asyncio.run(proactive.notice(scratch, push=False))
    assert summary["ok"] is True
    assert summary["created"] >= 2

    active = proactive.get_store().list_active()
    kinds = {o["kind"] for o in active}
    assert "budget" in kinds, f"expected a budget observation, got {active}"
    assert "task" in kinds, f"expected a task observation, got {active}"

    # A budget-over observation, since 2000 > 300.
    budget = next(o for o in active if o["kind"] == "budget")
    assert "budget" in budget["title"].lower()
    assert budget["action_label"]
    assert budget["instruction"]

    # Run AGAIN — stable keys mean nothing new is created.
    again = asyncio.run(proactive.notice(scratch, push=False))
    assert again["created"] == 0
    assert len(proactive.get_store().list_active()) == len(active)


def test_dismiss_removes_from_active(scratch):
    from himmy_app import proactive

    _seed_task_due_today(scratch)
    asyncio.run(proactive.notice(scratch, push=False))

    store = proactive.get_store()
    active = store.list_active()
    assert active
    target = active[0]
    assert store.dismiss(target["id"]) is True

    remaining = store.list_active()
    assert target["id"] not in {o["id"] for o in remaining}
    # And a fresh notice does NOT re-create the dismissed one (key already exists).
    asyncio.run(proactive.notice(scratch, push=False))
    assert target["id"] not in {o["id"] for o in store.list_active()}
    assert store.get_by_key(target["key"])["status"] == "dismissed"


def test_snooze_hides_then_reactivates(scratch):
    from himmy_app import proactive

    _seed_food_budget(scratch, 300)
    _seed_food_expense(scratch, 2000.0)
    asyncio.run(proactive.notice(scratch, push=False))

    store = proactive.get_store()
    target = store.list_active()[0]

    # Snooze into the future → hidden from the active list.
    snoozed = store.snooze(target["id"], hours=5)
    assert snoozed["status"] == "snoozed"
    assert target["id"] not in {o["id"] for o in store.list_active()}

    # Snooze into the PAST → next list_active() wakes it back up.
    store.snooze(target["id"], hours=-1)
    assert target["id"] in {o["id"] for o in store.list_active()}


def test_active_cap(scratch):
    from himmy_app import proactive

    store = proactive.get_store()
    # Add more than the cap; list_active never returns more than MAX_ACTIVE.
    for i in range(proactive.MAX_ACTIVE + 4):
        store.add({
            "key": f"manual-{i}", "kind": "task", "title": f"t{i}",
            "detail": "d", "action_label": "x", "instruction": "do x", "surface": "tasks",
        })
    assert len(store.list_active()) == proactive.MAX_ACTIVE
