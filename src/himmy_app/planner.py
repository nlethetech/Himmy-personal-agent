"""Himmy, plan my week — draft a time-blocked schedule the user approves.

The user asks Himmy to "plan my week"; we gather their OPEN, unscheduled tasks (merged with the
sidecar extras for due / priority / notes), tell a real LLM today's date and the working-hours
window, and ask it to lay the most due-soon / high-priority work into Mon–Fri slots over the next
few days — ~60–90 minutes each, never overlapping. The model returns a JSON object of blocks; we
parse it robustly, drop any leftover overlaps, and hand the draft back for the user to review and
push to their calendar.

Fail-open by design: if no model is configured (no ``OPENROUTER_API_KEY``) or the call fails, we
return ``{"ok": False, "blocks": [], "message": ...}`` so the UI can explain rather than crash.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from typing import Any

import httpx

from himmy_app.config import HimmyConfig, load_config

#: Working-hours window the model schedules into (Mon–Fri, local wall-clock hours).
_WORK_START = 9
_WORK_END = 18


def _open_candidates(cfg: HimmyConfig) -> list[dict[str, Any]]:
    """OPEN, NOT-done, NOT-yet-scheduled tasks — merged with their sidecar extras.

    Read the task store exactly the way ``server.py`` does (himmy's shared tasks pack), then
    overlay :class:`TaskExtrasStore` so each candidate carries due / priority / notes and we can
    skip anything that already has a time-block (``scheduled_start``).
    """
    from himmy.api.studio_tasks import get_tasks_store

    from himmy_app.tasks_extra import TaskExtrasStore

    extras = TaskExtrasStore(cfg).all()
    out: list[dict[str, Any]] = []
    for t in get_tasks_store().list():
        if getattr(t, "done", False):
            continue
        ex = extras.get(t.id, {})
        if ex.get("scheduled_start"):
            continue  # already time-blocked — leave it where the user put it
        out.append({
            "task_id": t.id,
            "title": t.title,
            "due": t.due or "",
            "priority": int(getattr(t, "priority", 0) or 0),
            "notes": (ex.get("notes") or "")[:240],
        })
    return out


def _build_prompt(candidates: list[dict[str, Any]], *, today: datetime.date, days: int) -> str:
    horizon = today + datetime.timedelta(days=days - 1)
    lines = []
    for i, c in enumerate(candidates):
        due = f" · due {c['due']}" if c["due"] else " · no due date"
        prio = {0: "none", 1: "low", 2: "medium", 3: "high"}.get(c["priority"], "none")
        note = f"\n   NOTE: {c['notes']}" if c["notes"] else ""
        lines.append(f'{i}. (task_id={c["task_id"]}) {c["title"]}{due} · priority {prio}{note}')
    tasklist = "\n".join(lines)
    return (
        "You are a focused research-planning assistant. Today is "
        f"{today.isoformat()} ({today.strftime('%A')}). Draft a time-blocked weekly schedule from "
        "the OPEN tasks below.\n\n"
        "RULES:\n"
        f"- Only schedule on weekdays (Mon–Fri) within working hours {_WORK_START:02d}:00–{_WORK_END:02d}:00, "
        f"between {today.isoformat()} and {horizon.isoformat()} inclusive.\n"
        "- Give each task ONE block of 60 to 90 minutes.\n"
        "- Schedule the most due-soon and highest-priority tasks first (earlier in the week).\n"
        "- No two blocks may overlap in time on the same day.\n"
        "- You do not have to schedule every task — only what realistically fits.\n\n"
        f"OPEN TASKS:\n{tasklist}\n\n"
        'Reply with ONLY a JSON object of this exact shape, nothing else:\n'
        '{"blocks": [{"task_id": "<the task_id>", "title": "<task title>", '
        '"day": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM", '
        '"reason": "<one short clause on why now>"}]}'
    )


def _parse_blocks(content: str, *, valid_ids: set[str], today: datetime.date, days: int) -> list[dict[str, Any]]:
    """Pull the JSON object out of the model reply and coerce it into clean, non-overlapping blocks."""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return []
    raw = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []

    lo = today
    hi = today + datetime.timedelta(days=days - 1)
    # day -> list of (start_minute, end_minute) already accepted, to reject overlaps.
    taken: dict[str, list[tuple[int, int]]] = {}
    out: list[dict[str, Any]] = []
    for b in raw:
        if not isinstance(b, dict):
            continue
        day = str(b.get("day") or "").strip()
        start = str(b.get("start") or "").strip()
        end = str(b.get("end") or "").strip()
        title = str(b.get("title") or "").strip()
        task_id = str(b.get("task_id") or "").strip()
        if not (day and start and end and title):
            continue
        try:
            d = datetime.date.fromisoformat(day)
            sm = _hhmm_to_min(start)
            em = _hhmm_to_min(end)
        except Exception:  # noqa: BLE001
            continue
        if sm is None or em is None or em <= sm:
            continue
        if d < lo or d > hi or d.weekday() > 4:  # weekdays only, inside horizon
            continue
        # Reject a block that overlaps one we've already accepted on the same day.
        if any(sm < oe and om < em for om, oe in taken.get(day, [])):
            continue
        taken.setdefault(day, []).append((sm, em))
        out.append({
            "task_id": task_id if task_id in valid_ids else "",
            "title": title,
            "day": day,
            "start": _min_to_hhmm(sm),
            "end": _min_to_hhmm(em),
            "reason": str(b.get("reason") or "").strip(),
        })
    out.sort(key=lambda x: (x["day"], x["start"]))
    return out


def _hhmm_to_min(s: str) -> int | None:
    m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h < 24 and 0 <= mi < 60):
        return None
    return h * 60 + mi


def _min_to_hhmm(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


async def suggest_week(cfg: HimmyConfig | None = None, *, days: int = 7) -> dict[str, Any]:
    """Draft a time-blocked weekly plan from the user's open tasks via the real LLM.

    Returns ``{"ok": True, "blocks": [...]}`` on success, or ``{"ok": False, "blocks": [],
    "message": ...}`` when there is nothing to schedule, no model is configured, or the call fails.
    """
    cfg = cfg or load_config()
    candidates = _open_candidates(cfg)
    if not candidates:
        return {"ok": False, "blocks": [], "message": "No open, unscheduled tasks to plan."}

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return {"ok": False, "blocks": [],
                "message": "Connect a model (set OPENROUTER_API_KEY) to plan your week."}

    today = datetime.date.today()
    prompt = _build_prompt(candidates, today=today, days=days)
    model = os.environ.get("HIMMY_APP_MODEL", "google/gemini-2.5-flash")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "temperature": 0.2,
                      "messages": [{"role": "user", "content": prompt}]},
            )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "blocks": [], "message": f"Couldn't reach the planner: {type(exc).__name__}"}

    valid_ids = {c["task_id"] for c in candidates}
    blocks = _parse_blocks(content, valid_ids=valid_ids, today=today, days=days)
    if not blocks:
        return {"ok": False, "blocks": [], "message": "The planner didn't return any usable blocks."}
    return {"ok": True, "blocks": blocks}


__all__ = ["suggest_week"]
