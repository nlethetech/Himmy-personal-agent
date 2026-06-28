"""Activity log — a plain-English record of what Himmy actually did.

Every tool Himmy runs emits a ``TOOL_COMPLETED`` event carrying the tool name, its args, an
outcome, and the result. :func:`observe` turns each into one human line ("Sent an email to
ram@…", "Looked up flights KTM→PKR", "Added a task") and appends it to ``activity.db``, so the
user can see — and trust — exactly what their agent has been up to. It also flags actions that were
**blocked** by Settings → Permissions, so a denied attempt is visible rather than silent.

Capture is wired once, in :func:`himmy_app.cli._build_runtime`, by wrapping the runtime's
``on_event`` — so every path (chat, approvals, streaming) logs automatically.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from himmy_app.config import HimmyConfig, load_config

#: tool name -> (surface, human label, arg keys to show as detail). Tools not listed (calculator,
#: current_time) are skipped as noise. Surface drives the icon in the UI.
_ACTIONS: dict[str, tuple[str, str, list[str]]] = {
    "mail_send": ("mail", "Sent an email", ["to", "subject"]),
    "mail_reply": ("mail", "Replied to an email", ["subject"]),
    "mail_draft": ("mail", "Drafted an email", ["to", "subject"]),
    "mail_list": ("mail", "Checked your inbox", []),
    "mail_read": ("mail", "Read an email", []),
    "calendar_add": ("calendar", "Added a calendar event", ["summary", "start"]),
    "calendar_edit": ("calendar", "Edited a calendar event", ["summary"]),
    "calendar_remove": ("calendar", "Removed a calendar event", []),
    "calendar_find": ("calendar", "Checked your calendar", ["query"]),
    "add_task": ("tasks", "Added a task", ["title"]),
    "complete_task": ("tasks", "Completed a task", ["title"]),
    "list_tasks": ("tasks", "Checked your tasks", []),
    "foodmandu_search": ("food", "Searched Foodmandu", ["query"]),
    "foodmandu_menu": ("food", "Read a restaurant menu", ["restaurant", "vendor_id"]),
    "daraz_search": ("shopping", "Searched Daraz", ["query"]),
    "buddha_air_flights": ("flights", "Looked up flights", ["origin", "destination"]),
    "web_search": ("web", "Searched the web", ["query"]),
    "web_fetch": ("web", "Read a web page", ["url"]),
    "weather": ("live_data", "Checked the weather", []),
    "geocode": ("live_data", "Located a place", ["query", "place"]),
    "wikipedia": ("live_data", "Looked up a fact", ["query", "title"]),
    "ask_papers": ("library", "Searched your library", ["query"]),
    "index_papers": ("library", "Indexed your library", []),
    "read_image": ("files", "Read an image you sent", []),
    "transcribe_audio": ("files", "Transcribed a voice note", []),
    "add_paper": ("library", "Added a paper", ["doi", "arxiv", "id"]),
    "save_article": ("library", "Saved an article", ["title", "url"]),
    "remember": ("memory", "Remembered something", ["text", "content", "value"]),
    "recall": ("memory", "Recalled a memory", ["query"]),
}
_MAX_ROWS = 600  # keep the log bounded


def _db(cfg: HimmyConfig):
    return cfg.data_dir / "activity.db"


def _conn(cfg: HimmyConfig) -> sqlite3.Connection:
    c = sqlite3.connect(str(_db(cfg)), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, tool TEXT, surface TEXT, title TEXT, detail TEXT, status TEXT
        )"""
    )
    return c


def _event_type(event: Any) -> str:
    et = getattr(event, "event_type", None)
    return getattr(et, "value", None) or str(et or "")


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            d = json.loads(value)
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _detail(args: dict[str, Any], keys: list[str]) -> str:
    for k in keys:
        v = args.get(k)
        if v not in (None, "", []):
            s = str(v).strip()
            return s[:80]
    return ""


def observe(event: Any, cfg: HimmyConfig | None = None) -> None:
    """Record one completed tool call as an activity line (best-effort, never raises)."""
    try:
        if _event_type(event) != "TOOL_COMPLETED":
            return
        payload = getattr(event, "payload", None) or {}
        tool = str(payload.get("tool_name") or "").strip()
        spec = _ACTIONS.get(tool)
        if not spec:  # unlisted/utility tool → not worth logging
            return
        surface, title, keys = spec
        args = _as_dict(payload.get("tool_args"))
        detail = _detail(args, keys)
        result = _as_dict(payload.get("result"))
        outcome = str(payload.get("tool_outcome") or "").lower()
        # Status: blocked by permissions > failed > ok.
        msg = str(result.get("message") or "")
        if "Settings → Permissions" in msg or "turned off" in msg.lower():
            status = "blocked"
        elif "fail" in outcome or "error" in outcome or result.get("ok") is False:
            status = "failed"
        else:
            status = "ok"
        cfg = cfg or load_config()
        with _conn(cfg) as c:
            c.execute(
                "INSERT INTO activity (ts, tool, surface, title, detail, status) VALUES (?,?,?,?,?,?)",
                (time.time(), tool, surface, title, detail, status),
            )
            c.execute(
                "DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT ?)",
                (_MAX_ROWS,),
            )
    except Exception:  # noqa: BLE001 - logging must never disturb a turn
        pass


def recent(limit: int = 60, cfg: HimmyConfig | None = None) -> list[dict[str, Any]]:
    cfg = cfg or load_config()
    try:
        with _conn(cfg) as c:
            rows = c.execute(
                "SELECT ts, tool, surface, title, detail, status FROM activity ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 300)),),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def clear(cfg: HimmyConfig | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    try:
        with _conn(cfg) as c:
            c.execute("DELETE FROM activity")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


__all__ = ["observe", "recent", "clear"]
