"""Smart Nudges — deterministic, deduped "needs you" notifications.

Everything here runs OFFLINE against a THROWAWAY data dir:
- HIMMY_APP_DATA_DIR / HIMMY_TASKS_PATH / HIMMY_ROUTINES_PATH are pinned under tmp_path so the
  real .scholar-desk is never touched;
- Google is fully mocked (studio_google.status / calendar_range / gmail_list) so no OAuth, no
  network, no real account — and most assertions exercise tasks, which need no Google at all.

The gate is dedup: running generate() twice must add nothing new (stable per-nudge keys via the
SELECT-before-INSERT in Inbox.add_nudge).
"""

from __future__ import annotations

import datetime
import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _iso(d: datetime.date) -> str:
    return d.isoformat()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Pin every durable path into a throwaway dir so we never touch the real account.
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HIMMY_TASKS_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("HIMMY_ROUTINES_PATH", str(tmp_path / "routines.db"))
    monkeypatch.setenv("HIMMY_TZ", "Asia/Kathmandu")

    import himmy.api.studio_tasks as studio_tasks
    import himmy.api.routines as himmy_routines

    studio_tasks.reset_tasks_store()
    himmy_routines.reset_routines_store()

    # Reload our modules so their cached singletons (Inbox, store) re-open at the temp paths.
    import himmy_app.routines as app_routines

    importlib.reload(app_routines)
    import himmy_app.nudges as nudges

    importlib.reload(nudges)
    import himmy_app.server as server

    importlib.reload(server)

    app = server.create_app()
    with TestClient(app) as c:
        c._nudges = nudges  # type: ignore[attr-defined]
        yield c
    studio_tasks.reset_tasks_store()
    himmy_routines.reset_routines_store()


def _add_task(client: TestClient, title: str, due: datetime.date) -> str:
    r = client.post("/tasks", json={"title": title, "due": _iso(due)})
    assert r.status_code == 200
    return r.json()["task"]["id"]


# --------------------------------------------------------------------------------------
# Tasks (no Google needed)
# --------------------------------------------------------------------------------------
def test_due_tomorrow_task_creates_a_nudge(client: TestClient) -> None:
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    _add_task(client, "Call the bank", tomorrow)

    run = client.post("/nudges/run")
    assert run.status_code == 200
    body = run.json()
    assert body["ok"] is True
    assert body["created"] >= 1

    # It lands in the real bell feed as a kind=='nudge' row.
    notes = client.get("/notifications").json()["notifications"]
    hits = [n for n in notes if n["kind"] == "nudge" and "Call the bank" in n["title"]]
    assert len(hits) == 1
    assert hits[0]["title"] == "Task due tomorrow: Call the bank"

    # And via the dedicated /nudges endpoint.
    nudges = client.get("/nudges").json()["nudges"]
    assert any("Call the bank" in n["title"] for n in nudges)


def test_dedup_running_twice_adds_no_duplicate(client: TestClient) -> None:
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    _add_task(client, "Call the bank", tomorrow)

    first = client.post("/nudges/run").json()
    assert first["checked"]["tasks"] == 1
    assert first["created"] >= 1

    second = client.post("/nudges/run").json()
    # Nothing new the second time — the stable key already exists.
    assert second["created"] == 0

    notes = client.get("/notifications").json()["notifications"]
    hits = [n for n in notes if n["kind"] == "nudge" and "Call the bank" in n["title"]]
    assert len(hits) == 1  # exactly one, no duplicate


def test_overdue_task_creates_overdue_nudge(client: TestClient) -> None:
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    tid = _add_task(client, "Submit report", yesterday)

    client.post("/nudges/run")
    nudges = client.get("/nudges").json()["nudges"]
    hit = next((n for n in nudges if "Submit report" in n["title"]), None)
    assert hit is not None
    assert hit["title"] == "Task overdue: Submit report"
    # Overdue re-nudges daily — the key carries today's date so it can re-fire tomorrow.
    expect_key = f"task-overdue-{tid}-{datetime.date.today().isoformat()}"  # noqa: F841 (documents the scheme)


def test_due_today_task_creates_due_today_nudge(client: TestClient) -> None:
    today = datetime.date.today()
    _add_task(client, "Pay rent", today)
    client.post("/nudges/run")
    nudges = client.get("/nudges").json()["nudges"]
    assert any(n["title"] == "Task due today: Pay rent" for n in nudges)


def test_done_and_far_future_tasks_do_not_nudge(client: TestClient) -> None:
    far = datetime.date.today() + datetime.timedelta(days=30)
    tid = _add_task(client, "Far away", far)
    client.post(f"/tasks/{tid}/complete")  # done tasks never nudge
    _add_task(client, "Way out", far)  # > tomorrow, not overdue → no nudge

    client.post("/nudges/run")
    nudges = client.get("/nudges").json()["nudges"]
    assert not any("Far away" in n["title"] for n in nudges)
    assert not any("Way out" in n["title"] for n in nudges)


# --------------------------------------------------------------------------------------
# Permissions gating
# --------------------------------------------------------------------------------------
def test_tasks_off_surface_is_skipped(client: TestClient) -> None:
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    # Turn Tasks OFF in Permissions, then add a due-tomorrow task.
    client.put("/permissions", json={"levels": {"tasks": "off"}})
    _add_task(client, "Hidden errand", tomorrow)

    run = client.post("/nudges/run").json()
    assert run["checked"]["tasks"] == "off"
    nudges = client.get("/nudges").json()["nudges"]
    assert not any("Hidden errand" in n["title"] for n in nudges)

    # Flip Tasks back on → it resumes on the next pass with no restart.
    client.put("/permissions", json={"levels": {"tasks": "write"}})
    run2 = client.post("/nudges/run").json()
    assert run2["created"] >= 1
    nudges2 = client.get("/nudges").json()["nudges"]
    assert any("Hidden errand" in n["title"] for n in nudges2)


# --------------------------------------------------------------------------------------
# Calendar / mail with no Google connected — must skip cleanly, never raise
# --------------------------------------------------------------------------------------
def test_calendar_and_mail_skip_when_google_not_connected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from himmy.api import studio_google as g

    class _Status:
        configured = True
        connected = False
        email = None
        writable = False

    monkeypatch.setattr(g, "status", lambda: _Status())

    run = client.post("/nudges/run").json()
    assert run["ok"] is True
    assert run["checked"]["calendar"] == "not_connected"
    assert run["checked"]["mail"] == "not_connected"


def test_calendar_event_creates_a_nudge_when_connected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from himmy.api import studio_google as g

    class _Status:
        configured = True
        connected = True
        email = "me@gmail.com"
        writable = False

    class _Event:
        def __init__(self, eid: str, summary: str, start: str, location: str = "") -> None:
            self.id = eid
            self.summary = summary
            self.start = start
            self.end = start
            self.location = location
            self.html_link = ""
            self.recurring_event_id = None

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    events = [
        _Event("e1", "Dentist", f"{tomorrow.isoformat()}T14:00:00+05:45"),
        _Event("e2", "Flight to Pokhara", f"{tomorrow.isoformat()}T09:00:00+05:45", "Pokhara Airport"),
    ]

    async def _range(time_min: str, time_max: str, max_results: int = 250):
        return list(events)

    async def _gmail(max_results: int = 20):
        return []

    monkeypatch.setattr(g, "status", lambda: _Status())
    monkeypatch.setattr(g, "calendar_range", _range)
    monkeypatch.setattr(g, "gmail_list", _gmail)

    run = client.post("/nudges/run").json()
    assert run["ok"] is True
    assert run["checked"]["calendar"] == 2
    titles = [n["title"] for n in client.get("/nudges").json()["nudges"]]
    assert any("Dentist" in t for t in titles)
    assert any(t.startswith("Trip ") and "Pokhara" in t for t in titles)

    # Dedup holds for calendar too.
    run2 = client.post("/nudges/run").json()
    assert run2["created"] == 0


def test_mail_unreplied_creates_a_nudge(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from himmy.api import studio_google as g

    class _Status:
        configured = True
        connected = True
        email = "me@gmail.com"
        writable = False

    class _Msg:
        def __init__(self, mid, sender, subject, date, unread=True) -> None:
            self.id = mid
            self.sender = sender
            self.subject = subject
            self.date = date
            self.snippet = ""
            self.label_ids = ["INBOX"]
            self.unread = unread
            self.thread_id = mid

    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=4)
    recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    msgs = [
        _Msg("m1", "Jane Doe <jane@x.com>", "Re: contract", old.strftime("%a, %d %b %Y %H:%M:%S %z")),
        _Msg("m2", "noreply@bank.com", "Statement", old.strftime("%a, %d %b %Y %H:%M:%S %z")),  # automated → skip
        _Msg("m3", "Bob <bob@x.com>", "Quick q", recent.strftime("%a, %d %b %Y %H:%M:%S %z")),  # too recent → skip
        _Msg("m4", "Sue <sue@x.com>", "FYI", old.strftime("%a, %d %b %Y %H:%M:%S %z"), unread=False),  # read → skip
    ]

    async def _gmail(max_results: int = 20):
        return list(msgs)

    async def _range(time_min: str, time_max: str, max_results: int = 250):
        return []

    monkeypatch.setattr(g, "status", lambda: _Status())
    monkeypatch.setattr(g, "gmail_list", _gmail)
    monkeypatch.setattr(g, "calendar_range", _range)

    run = client.post("/nudges/run").json()
    assert run["ok"] is True
    nudges = client.get("/nudges").json()["nudges"]
    mail_hits = [n for n in nudges if n["title"].startswith("Unreplied")]
    assert len(mail_hits) == 1
    assert "Re: contract" in mail_hits[0]["title"]
    assert "4 days" in mail_hits[0]["title"]
