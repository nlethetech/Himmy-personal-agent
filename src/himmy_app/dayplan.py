"""Today's plan — Himmy proactively turns your task board into a focused daily to-do.

Instead of staring at every open task, Himmy reads them each morning and picks the FEW that
genuinely deserve attention today — overdue first, then due, then important — in order, each with a
one-line "why". It's the chief-of-staff move: you open the app and the plan is already made.

One cheap model pass (``build_inference_for`` — cost auto-metered) ranks the tasks; if the model is
unavailable it falls back to a deterministic priority sort, so a plan always appears. Cached per day
(``dayplan_cache.json``) so it's instant and costs at most one pass a day. The titles always come
from the real task store (the model only picks ids + writes the reason), so nothing is hallucinated.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import re
from typing import Any

from himmy_app.config import HimmyConfig, load_config

#: How many tasks a daily plan surfaces — a focused few, not the whole board.
MAX_PLAN = 5

#: How many days of "what I planned vs what I actually did" records to keep (rolling).
HISTORY_DAYS = 90


def _today() -> str:
    return datetime.date.today().isoformat()


def _now_hhmm() -> str:
    """Current local wall-clock as HH:MM — used to decide if a timed item is now overdue."""
    return datetime.datetime.now().astimezone().strftime("%H:%M")


def _open_tasks(cfg: HimmyConfig) -> list[dict[str, Any]]:
    """Open (not-done) tasks as ``{id, title, due, priority}`` — best-effort, [] on any failure."""
    try:
        from himmy.api.studio_tasks import get_tasks_store

        out: list[dict[str, Any]] = []
        for t in get_tasks_store().list():
            if getattr(t, "done", False):
                continue
            tid = str(getattr(t, "id", "") or "")
            title = str(getattr(t, "title", "") or "").strip()
            if not tid or not title:
                continue
            out.append({
                "id": tid, "title": title,
                "due": str(getattr(t, "due", "") or "") or None,
                "priority": int(getattr(t, "priority", 0) or 0),
            })
        return out
    except Exception:  # noqa: BLE001 - the tasks store is best-effort
        return []


async def _today_events(cfg: HimmyConfig) -> list[dict[str, Any]]:
    """Today's calendar events as ``{id, title, time}`` (time = local HH:MM), time-ordered.

    Permission-gated (Calendar) and best-effort: [] if Calendar is off, Google isn't connected, or
    anything fails. The day's schedule IS most people's real to-do, so the plan is built from it."""
    try:
        from himmy_app import permissions

        if permissions.level_of("calendar", cfg) == "off":
            return []
        from himmy.api import studio_google as g

        if not g.status().connected:
            return []
        # Local day window WITH the machine tz offset (Google rejects an offset-less time window).
        now = datetime.datetime.now().astimezone()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        events = await g.calendar_range(start.isoformat(), (start + datetime.timedelta(days=1)).isoformat(), 50)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for e in events:
        summary = str(getattr(e, "summary", "") or "").strip()
        if not summary:
            continue
        start_s = str(getattr(e, "start", "") or "")
        m = re.search(r"T(\d{2}:\d{2})", start_s)   # local wall-clock HH:MM
        out.append({"id": str(getattr(e, "id", "") or ""), "title": summary,
                    "time": m.group(1) if m else "", "start": start_s})
    out.sort(key=lambda x: x["start"] or "~")
    return out


def _deterministic(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fallback ranking with no model: overdue → due today → due soon → high priority → the rest."""
    today = _today()

    def rank(t: dict[str, Any]) -> tuple[Any, ...]:
        due = t.get("due")
        overdue = bool(due and due < today)
        due_today = bool(due and due == today)
        return (not overdue, not due_today, due or "9999-99-99", -int(t.get("priority") or 0))

    out: list[dict[str, Any]] = []
    for t in sorted(tasks, key=rank)[:MAX_PLAN]:
        due = t.get("due")
        if due and due < today:
            reason = "overdue"
        elif due and due == today:
            reason = "due today"
        elif due:
            reason = f"due {due}"
        elif int(t.get("priority") or 0) >= 2:
            reason = "important"
        else:
            reason = "worth doing"
        out.append({"task_id": t["id"], "title": t["title"], "due": due, "reason": reason})
    return out


def _system_prompt() -> str:
    return (
        "You are Himmy, the user's chief of staff. From their OPEN tasks, build a focused to-do for "
        f"TODAY: pick the FEW (at most {MAX_PLAN}) that genuinely deserve attention today, IN ORDER "
        "— overdue first, then due today, then due soon, then anything important or a quick win. Be "
        "SELECTIVE; do NOT include everything. For each, write a SHORT reason (e.g. 'overdue 2 days', "
        "'due today', 'quick win', 'unblocks the rest'). Return ONLY a JSON object of the form "
        '{"note": "<one short encouraging line>", "plan": [{"task_id": "<exact id>", '
        '"reason": "<short>"}]}. Use the EXACT task_id strings provided. '
        f"Today is {_today()}."
    )


async def _model_plan(cfg: HimmyConfig, tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One cheap model pass to rank the tasks; None on any failure (caller falls back)."""
    listing = "\n".join(
        "- id=%s | %s%s%s" % (
            t["id"], t["title"],
            f" | due {t['due']}" if t.get("due") else "",
            f" | priority {t['priority']}" if t.get("priority") else "",
        )
        for t in tasks
    )
    try:
        from himmy.cli.provider import build_inference_for
        from himmy.services.inference.models import InferenceMessage, InferenceRequest

        svc = build_inference_for(cfg.provider, cfg.model)
        resp = await svc.run(InferenceRequest(
            messages=[InferenceMessage(role="system", content=_system_prompt()),
                      InferenceMessage(role="user", content="Open tasks:\n" + listing)],
            generation_params={"temperature": 0.2}, timeout_seconds=40,
        ))
        content = resp.output_text or ""
    except Exception:  # noqa: BLE001
        return None

    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None

    by_id = {t["id"]: t for t in tasks}
    plan: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (data.get("plan") or []):
        if not isinstance(item, dict):
            continue
        tid = str(item.get("task_id") or "").strip()
        if tid not in by_id or tid in seen:   # only real, non-duplicate task ids
            continue
        seen.add(tid)
        t = by_id[tid]
        plan.append({"task_id": tid, "title": t["title"], "due": t.get("due"),
                     "reason": str(item.get("reason") or "").strip()[:60]})
        if len(plan) >= MAX_PLAN:
            break
    if not plan:
        return None
    return {"note": str(data.get("note") or "").strip()[:160], "plan": plan}


class DayPlan:
    def __init__(self, config: HimmyConfig | None = None) -> None:
        self.cfg = config or load_config()
        self._cache = self.cfg.data_dir / "dayplan_cache.json"     # the task-ranking cache
        self._done = self.cfg.data_dir / "dayplan_done.json"       # which items are ticked TODAY
        self._history = self.cfg.data_dir / "dayplan_history.json"  # rolling done/missed record

    # ---- task-ranking cache (re-plans when the open-task set changes) -------------------
    def _read(self) -> dict[str, Any] | None:
        try:
            return json.loads(self._cache.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def _write(self, payload: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self._cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    async def _ranked_tasks(self, tasks: list[dict[str, Any]], *, force: bool) -> tuple[str, list[dict[str, Any]]]:
        """(note, ordered task items) — model-ranked, cached per day by the open-task set."""
        if not tasks:
            return "", []
        cached = self._read()
        cur_ids = sorted(t["id"] for t in tasks)
        if (cached and not force and cached.get("date") == _today()
                and cached.get("task_ids") == cur_ids):
            return cached.get("note", ""), cached.get("plan", [])
        built = await _model_plan(self.cfg, tasks) or {"note": "", "plan": _deterministic(tasks)}
        self._write({"date": _today(), "note": built["note"], "plan": built["plan"], "task_ids": cur_ids})
        return built["note"], built["plan"]

    # ---- per-day "done" tracker (for calendar items, which Google has no done-state for) -
    def _done_today(self) -> set[str]:
        try:
            d = json.loads(self._done.read_text(encoding="utf-8"))
            if d.get("date") == _today():
                return set(d.get("ids") or [])
        except Exception:  # noqa: BLE001
            pass
        return set()

    def toggle_done(self, item_id: str, done: bool) -> dict[str, Any]:
        """Tick / un-tick a plan item for today (used for calendar events). Resets daily."""
        ids = self._done_today()
        ids.add(item_id) if done else ids.discard(item_id)
        with contextlib.suppress(Exception):
            self._done.write_text(json.dumps({"date": _today(), "ids": sorted(ids)}), encoding="utf-8")
        return {"ok": True, "id": item_id, "done": done}

    # ---- record keeping: a rolling log of what was scheduled vs what got done -------------
    def _record_day(self, date: str, events: list[dict[str, Any]]) -> None:
        """Snapshot today's scheduled items + their done/missed status into the rolling history.

        Re-written each time the plan is read, so the last read of the day is the day's final
        record — a kept account of what was planned and what slipped, for review and for Himmy."""
        try:
            raw = json.loads(self._history.read_text(encoding="utf-8"))
            days = raw.get("days") if isinstance(raw, dict) else None
        except Exception:  # noqa: BLE001
            days = None
        if not isinstance(days, dict):
            days = {}
        days[date] = {
            "items": [{"id": e["id"], "title": e["title"], "time": e.get("time", ""),
                       "done": bool(e["done"]), "missed": bool(e["overdue"])} for e in events],
            "done": sum(1 for e in events if e["done"]),
            "missed": sum(1 for e in events if e["overdue"]),
        }
        for old in sorted(days)[:-HISTORY_DAYS]:   # prune to the most recent HISTORY_DAYS
            days.pop(old, None)
        with contextlib.suppress(Exception):
            self._history.write_text(json.dumps({"days": days}, ensure_ascii=False), encoding="utf-8")

    def history(self, days: int = 14) -> list[dict[str, Any]]:
        """Recent daily records, newest first — the kept account of scheduled vs done/missed."""
        try:
            raw = json.loads(self._history.read_text(encoding="utf-8"))
            days_map = raw.get("days") or {}
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        for date in sorted(days_map.keys(), reverse=True)[:max(1, days)]:
            rec = days_map[date]
            out.append({"date": date, "done": int(rec.get("done", 0)),
                        "missed": int(rec.get("missed", 0)), "items": rec.get("items", [])})
        return out

    # ---- the unified plan: today's calendar + the prioritised tasks ----------------------
    async def get(self, *, force: bool = False) -> dict[str, Any]:
        """Today's plan as a single checklist: today's CALENDAR events (time-ordered, the real
        backbone of most days) followed by the model-prioritised open TASKS. A timed event whose
        time has passed and isn't ticked is flagged ``overdue``. Always well-formed."""
        events = await _today_events(self.cfg)
        tasks = _open_tasks(self.cfg)
        done = self._done_today()
        now = _now_hhmm()
        note, ranked = await self._ranked_tasks(tasks, force=force)

        event_items: list[dict[str, Any]] = []
        for e in events:
            eid = e["id"]
            is_done = eid in done
            t = e.get("time", "")
            overdue = bool(t and not is_done and t < now)   # timed, past, and not ticked → missed
            event_items.append({"kind": "event", "id": eid, "title": e["title"],
                                "time": t, "done": is_done, "overdue": overdue})
        if event_items:
            self._record_day(_today(), event_items)         # keep the day's record up to date

        items: list[dict[str, Any]] = list(event_items)
        for t in ranked:
            items.append({"kind": "task", "id": t["task_id"], "title": t["title"],
                          "due": t.get("due"), "reason": t.get("reason", ""),
                          "done": False, "overdue": False})

        overdue_n = sum(1 for e in event_items if e["overdue"])
        if not note and events:
            note = "Your day at a glance — your schedule, plus anything that needs doing."
        return {"ok": True, "date": _today(), "note": note, "items": items,
                "events": len(events), "open_tasks": len(tasks), "overdue": overdue_n,
                "total": len(items)}


__all__ = ["DayPlan", "MAX_PLAN"]
