"""Richer tasks: the sidecar extras store (notes / subtasks / recurrence / paper link / time-block),
the recurrence date math, and the HTTP flow where completing a repeating task spawns the next one.
"""

from __future__ import annotations

import datetime

import pytest

from himmy_app.config import load_config
from himmy_app.server import _advance_due
from himmy_app.tasks_extra import TaskExtrasStore


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIMMY_TASKS_PATH", str(tmp_path / "data" / "tasks.db"))
    return load_config()


# ---- the sidecar store ----------------------------------------------------------------------
def test_extras_roundtrip_and_partial_update(cfg):
    s = TaskExtrasStore(cfg)
    s.set("t1", notes="read the methods section", recur="weekly")
    got = s.get("t1")
    assert got["notes"] == "read the methods section" and got["recur"] == "weekly"
    # a partial update leaves the other fields intact
    s.set("t1", paper_id="p42", paper_title="Rooted in Violence")
    got = s.get("t1")
    assert got["notes"] == "read the methods section"   # untouched
    assert got["paper_id"] == "p42" and got["paper_title"] == "Rooted in Violence"


def test_subtasks_are_sanitised(cfg):
    s = TaskExtrasStore(cfg)
    s.set("t1", subtasks=[{"text": "outline", "done": True}, {"text": "  ", "done": False}, {"text": "draft"}])
    subs = s.get("t1")["subtasks"]
    assert subs == [{"text": "outline", "done": True}, {"text": "draft", "done": False}]  # blank dropped


def test_invalid_recur_is_ignored(cfg):
    s = TaskExtrasStore(cfg)
    s.set("t1", recur="hourly")        # not a supported rule
    assert s.get("t1")["recur"] == ""


def test_all_and_delete(cfg):
    s = TaskExtrasStore(cfg)
    s.set("a", notes="x")
    s.set("b", notes="y")
    assert set(s.all()) == {"a", "b"}
    s.delete("a")
    assert set(s.all()) == {"b"}


# ---- recurrence date math (future dates so the "never in the past" guard doesn't interfere) ---
def test_advance_due_daily_and_weekly():
    base = datetime.date.today() + datetime.timedelta(days=40)
    assert _advance_due(base.isoformat(), "daily") == (base + datetime.timedelta(days=1)).isoformat()
    assert _advance_due(base.isoformat(), "weekly") == (base + datetime.timedelta(days=7)).isoformat()


def test_advance_due_monthly_clamps_to_month_length():
    import calendar
    yr = datetime.date.today().year + 1                 # a future January, so it isn't pinned to today
    last_feb = calendar.monthrange(yr, 2)[1]
    assert _advance_due(f"{yr}-01-31", "monthly") == f"{yr}-02-{last_feb:02d}"


def test_advance_due_never_in_the_past():
    nxt = _advance_due("2000-01-01", "daily")  # ancient due → next from today, not 2000
    assert datetime.date.fromisoformat(nxt) >= datetime.date.today()


# ---- HTTP: extras + a recurring task spawning its next occurrence ----------------------------
def test_http_extras_and_recurrence(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    import himmy_app.server as srv

    with TestClient(srv.create_app()) as client:
        tid = client.post("/tasks", json={"title": "Weekly advisor prep"}).json()["task"]["id"]
        # attach extras
        r = client.patch(f"/tasks/{tid}/extras", json={
            "recur": "weekly", "notes": "bring the draft", "paper_id": "p1", "paper_title": "Seed Paper",
            "subtasks": [{"text": "print slides", "done": False}],
        }).json()
        assert r["task"]["recur"] == "weekly" and r["task"]["paper_title"] == "Seed Paper"
        assert r["task"]["subtasks"][0]["text"] == "print slides"

        # the list view merges the extras onto the task
        listed = next(t for t in client.get("/tasks").json()["tasks"] if t["id"] == tid)
        assert listed["notes"] == "bring the draft"

        # completing a recurring task spawns the next occurrence, carrying the rule + paper link
        spawned = client.post(f"/tasks/{tid}/complete").json()["spawned"]
        assert spawned and spawned["recur"] == "weekly" and spawned["paper_title"] == "Seed Paper"
        assert spawned["id"] != tid
