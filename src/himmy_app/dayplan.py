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


def _today() -> str:
    return datetime.date.today().isoformat()


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
        self._cache = self.cfg.data_dir / "dayplan_cache.json"

    def _read(self) -> dict[str, Any] | None:
        try:
            return json.loads(self._cache.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def _write(self, payload: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self._cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    async def get(self, *, force: bool = False) -> dict[str, Any]:
        """Today's plan. Served from the daily cache unless stale/forced; regenerates from the
        current task board (model rank, deterministic fallback). Always returns a well-formed dict."""
        tasks = _open_tasks(self.cfg)
        if not tasks:
            return {"ok": True, "date": _today(), "note": "", "plan": [], "open": 0}

        cached = self._read()
        # Reuse today's cache only when the open-task set hasn't changed (so completing/adding a task
        # re-plans), and not when forced.
        cur_ids = sorted(t["id"] for t in tasks)
        if (cached and not force and cached.get("date") == _today()
                and cached.get("task_ids") == cur_ids):
            return {"ok": True, "date": cached["date"], "note": cached.get("note", ""),
                    "plan": cached.get("plan", []), "open": len(tasks), "cached": True}

        built = await _model_plan(self.cfg, tasks) or {"note": "", "plan": _deterministic(tasks)}
        payload = {"date": _today(), "note": built["note"], "plan": built["plan"],
                   "task_ids": cur_ids}
        self._write(payload)
        return {"ok": True, "date": _today(), "note": built["note"], "plan": built["plan"],
                "open": len(tasks)}


__all__ = ["DayPlan", "MAX_PLAN"]
