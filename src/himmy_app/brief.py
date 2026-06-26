"""The daily brief — Himmy's proactive "here's your day", surfaced on the Today page.

The whole point is *push, not pull*: the moment you open Himmy, it has already pulled together
your day — schedule, what needs you, the weather, a heads-up — written warmly and personally. It
runs the brief through the normal agent (so it uses the real calendar/tasks/mail/weather tools,
respects your Permissions, and knows you from your profile), then caches it for the day. The
Today card serves the cache instantly and refreshes a stale (yesterday's) brief in the background,
so it's always there with no spinner and at most one generation per day (cheap on usage).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
from typing import Any

from himmy_app.config import HimmyConfig, load_config

_BRIEF_PROMPT = (
    "Write me a warm, personal morning brief for today — address me by my first name if you know "
    "it from my profile. FIRST actually CALL these tools and base everything ONLY on what they "
    "return (never claim you lack access to a tool you were given — call it): current_time (today's "
    "date and day); calendar_find (today's events); list_tasks (open, overdue, due today); weather "
    "(if my profile gives a home city/airport, geocode it first, then call weather); mail_list "
    "(scan the recent inbox and pick out anything that looks like it genuinely needs my reply). "
    "THEN write a tight, skimmable brief with short **bold headers**, in this order — SKIP any "
    "section whose tool returned nothing:\n"
    "- a one-line personal note on how the day looks (NOT a 'good morning/afternoon' greeting — the "
    "app already greets me; make it sound like you know me);\n"
    "- **Today** — today's schedule, or 'Nothing on the calendar';\n"
    "- **Needs you** — tasks due/overdue and emails that look like they need a reply, named "
    "specifically; if there's genuinely nothing, a brief reassuring line;\n"
    "- **Weather** — one line; gently flag it if it affects an outdoor event today;\n"
    "- **Heads up** — only if relevant (an upcoming trip/flight, a deadline soon).\n"
    "Keep it short and human. READ-ONLY: do NOT send mail, and do NOT add or change calendar "
    "events or tasks — just tell me about my day."
)


class DailyBrief:
    def __init__(self, config: HimmyConfig | None = None) -> None:
        self.cfg = config or load_config()
        self._cache = self.cfg.data_dir / "brief_cache.json"
        self._refreshing = False

    async def get(self, *, force: bool = False) -> dict[str, Any]:
        cached = self._read()
        today = datetime.date.today().isoformat()
        if cached and cached.get("date") == today and not force:
            return {"ok": True, "text": cached["text"], "generated_at": cached["iso"], "stale": False}
        # Stale (yesterday's) or forced or cold: kick a refresh; serve whatever we have meanwhile.
        self._spawn_refresh()
        if cached:
            return {"ok": True, "text": cached["text"], "generated_at": cached["iso"], "stale": True}
        return {"ok": True, "text": "", "generated_at": "", "stale": True, "generating": True}

    async def _generate(self) -> str:
        from himmy_app.cli import ask_turn

        try:
            res = await ask_turn(_BRIEF_PROMPT)  # no session_id: the brief doesn't pollute chat history
            return (res.get("reply") or "").strip()
        except Exception:  # noqa: BLE001 - a brief failure must never break Today
            return ""

    def _spawn_refresh(self) -> None:
        if self._refreshing:
            return

        async def _run() -> None:
            self._refreshing = True
            try:
                text = await self._generate()
                if text:
                    self._write(text)
            finally:
                self._refreshing = False

        with contextlib.suppress(RuntimeError):  # no running loop (e.g. a unit test) → skip
            asyncio.get_running_loop().create_task(_run())

    def _read(self) -> dict[str, Any] | None:
        try:
            return json.loads(self._cache.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def _write(self, text: str) -> None:
        now = datetime.datetime.now()
        payload = {"date": datetime.date.today().isoformat(),
                   "iso": now.isoformat(timespec="seconds"), "text": text}
        with contextlib.suppress(Exception):
            self._cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


__all__ = ["DailyBrief"]
