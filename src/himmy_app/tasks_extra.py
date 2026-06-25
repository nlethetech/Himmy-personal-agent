"""Richer task fields, kept in a sidecar table keyed by himmy's task id.

himmy's core task store carries only title / due / priority / done. A researcher's planner needs
more — a note, a sub-step checklist, a repeat rule, a link to the paper a task is about, and a
time-block on the calendar. Rather than fork the framework store we keep those extras here and the
server merges them onto each task. Everything is optional; a task with no extras row behaves exactly
as before.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from himmy_app.config import HimmyConfig, load_config

#: The recurrence rules we support (kept deliberately small + obvious).
RECUR_CHOICES = {"", "daily", "weekly", "monthly"}

_FIELDS = ("notes", "subtasks", "recur", "paper_id", "paper_title", "scheduled_start", "scheduled_end", "event_id")


def _blank() -> dict[str, Any]:
    return {"notes": "", "subtasks": [], "recur": "", "paper_id": "", "paper_title": "",
            "scheduled_start": "", "scheduled_end": "", "event_id": ""}


class TaskExtrasStore:
    """Per-task notes / subtasks / recurrence / paper link / time-block."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        cfg = config or load_config()
        self._db = cfg.task_extras_db_path
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._db), timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS task_extras (
                    task_id TEXT PRIMARY KEY,
                    notes TEXT DEFAULT '',
                    subtasks TEXT DEFAULT '[]',   -- JSON list of {text, done}
                    recur TEXT DEFAULT '',        -- '' | daily | weekly | monthly
                    paper_id TEXT DEFAULT '',
                    paper_title TEXT DEFAULT '',
                    scheduled_start TEXT DEFAULT '',
                    scheduled_end TEXT DEFAULT '',
                    event_id TEXT DEFAULT '',      -- the calendar event this task is time-blocked into
                    updated REAL
                )"""
            )

    def _row(self, r: sqlite3.Row | None) -> dict[str, Any]:
        out = _blank()
        if not r:
            return out
        d = dict(r)
        out["notes"] = d.get("notes") or ""
        try:
            out["subtasks"] = json.loads(d.get("subtasks") or "[]")
        except Exception:  # noqa: BLE001
            out["subtasks"] = []
        for k in ("recur", "paper_id", "paper_title", "scheduled_start", "scheduled_end", "event_id"):
            out[k] = d.get(k) or ""
        return out

    # ---- read ----------------------------------------------------------------------------
    def get(self, task_id: str) -> dict[str, Any]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM task_extras WHERE task_id = ?", (task_id,)).fetchone()
        return self._row(r)

    def all(self) -> dict[str, dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM task_extras").fetchall()
        return {r["task_id"]: self._row(r) for r in rows}

    # ---- write ---------------------------------------------------------------------------
    def set(self, task_id: str, **fields: Any) -> dict[str, Any]:
        """Upsert only the supplied extra fields; others are left untouched."""
        cur = self.get(task_id)
        for k, v in fields.items():
            if k not in _FIELDS or v is None:
                continue
            if k == "recur" and v not in RECUR_CHOICES:
                continue
            cur[k] = v
        subtasks = json.dumps([
            {"text": str(s.get("text", "")).strip(), "done": bool(s.get("done"))}
            for s in (cur.get("subtasks") or []) if str(s.get("text", "")).strip()
        ])
        with self._conn() as c:
            c.execute(
                """INSERT INTO task_extras
                   (task_id, notes, subtasks, recur, paper_id, paper_title, scheduled_start, scheduled_end, event_id, updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     notes=excluded.notes, subtasks=excluded.subtasks, recur=excluded.recur,
                     paper_id=excluded.paper_id, paper_title=excluded.paper_title,
                     scheduled_start=excluded.scheduled_start, scheduled_end=excluded.scheduled_end,
                     event_id=excluded.event_id, updated=excluded.updated""",
                (task_id, cur["notes"], subtasks, cur["recur"], cur["paper_id"], cur["paper_title"],
                 cur["scheduled_start"], cur["scheduled_end"], cur["event_id"], time.time()),
            )
        return self.get(task_id)

    def delete(self, task_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM task_extras WHERE task_id = ?", (task_id,))


__all__ = ["TaskExtrasStore", "RECUR_CHOICES"]
