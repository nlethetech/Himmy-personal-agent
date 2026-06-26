"""Scheduled automations ("Routines") for Himmy — saved prompts that run on a timer.

WHY this module instead of himmy's built-in RoutineScheduler:

himmy ships a complete, production-hardened Routines layer (cron expressions, IANA
timezones, catch-up, multi-node leader election, quotas). We REUSE the valuable, fiddly
parts of it as a library — the validated :class:`Schedule` model, the durable
``RoutinesStore`` (SQLite, migration-tested), and the pure due-math (:func:`is_due` /
:func:`next_fire`, which give us cron + timezone correctness for free).

But himmy's *firing* path (``_run_headless`` → ``studio_service.stream_agent_run`` →
the canonical run store) is the multi-tenant Studio pipeline, which this single-user app
does not use — and it keeps only a short capped preview of the result. So we drive the
schedule with our OWN small in-process loop that fires each routine through the app's
proven agent path (:func:`himmy_app.cli.ask_turn` — the exact code /ask uses, with the
same tools, guardrails, memory, self-learning, and HITL approval gating) and captures the
FULL result into an app-owned inbox the UI can show. Identical-to-chat behaviour, full
result text, and none of the never-run canonical-store machinery.

The bookkeeping per fire mirrors himmy's reference ``_execute_locked`` exactly: claim the
run with a CONDITIONAL ``mark_started`` (the anchor advance doubles as an overlap guard so
a slow run can't re-trigger itself), run it under a wall-clock timeout, then ``record_result``
+ ``advance_next_fire``.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

# Reused from himmy: validated schedule model, durable store, and pure due-math.
from himmy.api.routines import (
    Routine,
    Schedule,
    get_routines_store,
    is_due,
    next_fire,
)

from himmy_app.cli import _SPEC  # the app's agent.yaml — every routine binds to this agent
from himmy_app.config import load_config

#: How long a single unattended run may take before we cancel it (seconds).
RUN_TIMEOUT_S = 300.0
#: Loop never sleeps longer than this, so CRUD changes and clock drift are picked up even if
#: the early-wake signal is missed. Sleep-to-next-fire caps to this.
MAX_SLEEP_S = 30.0
#: Short preview persisted on the routine row (full text lives in the inbox).
PREVIEW_CHARS = 280


def _local_zone() -> Any:
    """The timezone wall-clock 'daily' schedules anchor to — HIMMY_TZ (Asia/Kathmandu), else UTC.

    himmy's cron path resolves its own zone (resolve_zone(HIMMY_TZ)); 'every'/'at' are
    instant-based and tz-agnostic. Only 'daily' reads the tz of the ``now`` we pass it, so we
    must hand it a zone-aware now (Nepal by default) — otherwise 'every day at 07:00' would
    fire at 07:00 UTC (12:45 in Kathmandu) instead of 07:00 local.
    """
    name = os.environ.get("HIMMY_TZ") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a bad tz name falls back to UTC, never crashes
        return UTC


def _now() -> datetime:
    return datetime.now(_local_zone())


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------------------
# Inbox — full results of routine runs, plus "needs approval" parks. App-owned (himmy's
# RoutinesStore only keeps a capped preview on the routine row).
# ---------------------------------------------------------------------------------------
class Inbox:
    """A tiny SQLite store of routine outputs the UI shows as notifications."""

    def __init__(self, path: str) -> None:
        # check_same_thread=False: the scheduler loop and the (async) endpoints both touch
        # this from the same event-loop thread, but FastAPI may also hop a threadpool — the
        # connection is only ever used for short synchronous statements, so this is safe.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbox (
                id            TEXT PRIMARY KEY,
                routine_id    TEXT,
                routine_name  TEXT NOT NULL,
                kind          TEXT NOT NULL DEFAULT 'result',  -- result | approval | error | nudge
                title         TEXT NOT NULL,
                body          TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'ok',
                checkpoint_id TEXT,
                created_at    TEXT NOT NULL,
                read          INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Idempotent, pragma-guarded additive migrations (same pattern as the himmy tasks store).

        Adds the nullable ``nudge_key`` column that Smart Nudges dedup on — a stable per-nudge key
        that :meth:`add_nudge` checks before inserting. The column is additive (existing rows get
        NULL) so add()/list()/unread_count() behaviour is unchanged.
        """
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(inbox)").fetchall()}
        if "nudge_key" not in cols:
            self._conn.execute("ALTER TABLE inbox ADD COLUMN nudge_key TEXT")

    def add(
        self,
        *,
        routine_id: str | None,
        routine_name: str,
        title: str,
        body: str = "",
        kind: str = "result",
        status: str = "ok",
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        nid = uuid.uuid4().hex
        created = _iso(_now())
        self._conn.execute(
            "INSERT INTO inbox (id, routine_id, routine_name, kind, title, body, status,"
            " checkpoint_id, created_at, read) VALUES (?,?,?,?,?,?,?,?,?,0)",
            (nid, routine_id, routine_name, kind, title, body, status, checkpoint_id, created),
        )
        self._conn.commit()
        return self.get(nid) or {}

    def add_nudge(
        self,
        *,
        key: str,
        title: str,
        body: str = "",
        routine_name: str = "Himmy",
    ) -> dict[str, Any] | None:
        """Insert a deduped ``kind='nudge'`` row, keyed by the stable ``key``.

        SELECT-before-INSERT: if a row with this ``nudge_key`` already exists we no-op and return
        ``None`` (so :func:`himmy_app.nudges.generate` is idempotent — running it twice in the same
        window adds nothing new). A bare ``ALTER ADD COLUMN`` gives no uniqueness, and a UNIQUE
        index can't be added retroactively over existing NULLs, so the explicit lookup IS the
        dedup. Otherwise inserts a fresh nudge row and returns it.
        """
        existing = self._conn.execute(
            "SELECT 1 FROM inbox WHERE nudge_key = ?", (key,)
        ).fetchone()
        if existing is not None:
            return None
        nid = uuid.uuid4().hex
        created = _iso(_now())
        self._conn.execute(
            "INSERT INTO inbox (id, routine_id, routine_name, kind, title, body, status,"
            " checkpoint_id, created_at, read, nudge_key) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
            (nid, None, routine_name, "nudge", title, body, "ok", None, created, key),
        )
        self._conn.commit()
        return self.get(nid) or {}

    def list_by_kind(self, kind: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """The most recent rows of one ``kind`` (e.g. the nudge feed), newest first."""
        rows = self._conn.execute(
            "SELECT * FROM inbox WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    def _row(self, r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"],
            "routine_id": r["routine_id"],
            "routine_name": r["routine_name"],
            "kind": r["kind"],
            "title": r["title"],
            "body": r["body"],
            "status": r["status"],
            "checkpoint_id": r["checkpoint_id"],
            "created_at": r["created_at"],
            "read": bool(r["read"]),
        }

    def list(self, *, limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM inbox"
        if unread_only:
            sql += " WHERE read = 0"
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [self._row(r) for r in self._conn.execute(sql, (limit,)).fetchall()]

    def get(self, nid: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM inbox WHERE id = ?", (nid,)).fetchone()
        return self._row(r) if r else None

    def unread_count(self) -> int:
        r = self._conn.execute("SELECT COUNT(*) AS n FROM inbox WHERE read = 0").fetchone()
        return int(r["n"]) if r else 0

    def mark_read(self, nid: str, read: bool = True) -> bool:
        cur = self._conn.execute(
            "UPDATE inbox SET read = ? WHERE id = ?", (1 if read else 0, nid)
        )
        self._conn.commit()
        return cur.rowcount == 1

    def mark_all_read(self) -> int:
        cur = self._conn.execute("UPDATE inbox SET read = 1 WHERE read = 0")
        self._conn.commit()
        return cur.rowcount

    def delete(self, nid: str) -> bool:
        cur = self._conn.execute("DELETE FROM inbox WHERE id = ?", (nid,))
        self._conn.commit()
        return cur.rowcount == 1


_INBOX: Inbox | None = None


def get_inbox() -> Inbox:
    global _INBOX
    if _INBOX is None:
        cfg = load_config()
        _INBOX = Inbox(str(cfg.data_dir / "inbox.db"))
    return _INBOX


# ---------------------------------------------------------------------------------------
# Routine CRUD (thin wrappers over himmy's RoutinesStore, with the app's agent baked in).
# ---------------------------------------------------------------------------------------
def _routine_dict(r: Routine) -> dict[str, Any]:
    """Serialise a routine for the UI (the agent binding is an internal detail)."""
    return {
        "id": r.id,
        "name": r.name,
        "prompt": r.prompt,
        "schedule": r.schedule.model_dump(exclude_none=True),
        "schedule_desc": r.schedule.describe(),
        "enabled": bool(r.enabled),
        "last_status": r.last_status,
        "last_run_at": r.last_run_at,
        "last_preview": r.last_preview,
        "last_error": r.last_error,
        "next_fire_at": r.next_fire_at,
        "created_at": r.created_at,
    }


def _build_schedule(spec: dict[str, Any]) -> Schedule:
    """Build a validated :class:`Schedule` from a UI payload.

    Accepts ``{kind: 'daily', at: 'HH:MM'}``, ``{kind: 'every', hours: N}``,
    ``{kind: 'cron', expr: '...'}``, or ``{kind: 'at', at_datetime: 'ISO'}``; an optional
    ``timezone`` overrides the HIMMY_TZ default for wall-clock kinds.
    """
    return Schedule(**spec)


def list_routines() -> list[dict[str, Any]]:
    return [_routine_dict(r) for r in get_routines_store().list()]


def get_routine(routine_id: str) -> dict[str, Any] | None:
    r = get_routines_store().get(routine_id)
    return _routine_dict(r) if r else None


def create_routine(name: str, prompt: str, schedule: dict[str, Any], *, enabled: bool = True) -> dict[str, Any]:
    cfg = load_config()
    r = Routine(
        name=name.strip() or "Untitled routine",
        agent_path=str(_SPEC),          # single-user-local binding (satisfies the model invariant)
        prompt=prompt.strip(),
        schedule=_build_schedule(schedule),
        provider=cfg.provider,
        model=cfg.model,
        enabled=enabled,
    )
    saved = get_routines_store().upsert(r)
    # Seed next_fire_at so the UI can show "next run" immediately (advisory; the loop recomputes).
    nf = next_fire(saved, _now())
    if nf is not None:
        get_routines_store().advance_next_fire(saved.id, _iso(nf))
        saved = get_routines_store().get(saved.id) or saved
    return _routine_dict(saved)


def update_routine(routine_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    store = get_routines_store()
    cur = store.get(routine_id)
    if cur is None:
        return None
    data = cur.model_dump()
    if "name" in patch and patch["name"] is not None:
        data["name"] = str(patch["name"]).strip() or cur.name
    if "prompt" in patch and patch["prompt"] is not None:
        data["prompt"] = str(patch["prompt"]).strip()
    if "enabled" in patch and patch["enabled"] is not None:
        data["enabled"] = bool(patch["enabled"])
    if patch.get("schedule"):
        data["schedule"] = _build_schedule(patch["schedule"]).model_dump()
    data["updated_at"] = _iso(_now())
    saved = store.upsert(Routine(**data))
    nf = next_fire(saved, _now())
    store.advance_next_fire(saved.id, _iso(nf) if nf else None)
    return _routine_dict(store.get(saved.id) or saved)


def delete_routine(routine_id: str) -> bool:
    return get_routines_store().delete(routine_id)


# ---------------------------------------------------------------------------------------
# The scheduler — an in-process asyncio loop. Started/stopped by the FastAPI lifespan.
# ---------------------------------------------------------------------------------------
class AppScheduler:
    """Fires due routines through the app's own agent and records full results.

    Single-process, single-user. Correctness rests on himmy's CONDITIONAL ``mark_started``
    (claims a due routine exactly once and advances the due-anchor so it can't re-fire
    mid-run) plus an in-memory ``_running`` set as a belt-and-braces overlap guard.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._running: set[str] = set()

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="himmy-routine-scheduler")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def notify_change(self) -> None:
        """Wake the loop now (after CRUD) so a new/edited routine re-plans immediately."""
        self._wake.set()

    # -- the loop ---------------------------------------------------------------
    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must never kill the loop
                pass
            await self._sleep_until_next()

    async def _tick(self) -> None:
        now = _now()
        store = get_routines_store()
        for r in store.list():
            if not r.enabled or r.id in self._running:
                continue
            if not is_due(r, now):
                continue
            # Conditional claim: stamp last_run_at (the due-anchor) only if it still holds the
            # value we evaluated due-ness against — at-most-once even if ticks overlap.
            claimed = store.mark_started(r.id, _iso(now), expected_last_run_at=r.last_run_at)
            if not claimed:
                continue
            self._running.add(r.id)
            # Fire concurrently so one slow routine doesn't block others this tick.
            asyncio.create_task(self._fire_and_record(r), name=f"routine:{r.id}")

    async def _sleep_until_next(self) -> None:
        """Sleep until the soonest next fire (capped), waking early on notify_change()."""
        now = _now()
        soonest: float = MAX_SLEEP_S
        for r in get_routines_store().list():
            if not r.enabled:
                continue
            nf = next_fire(r, now)
            if nf is None:
                continue
            soonest = min(soonest, max(0.0, (nf - now).total_seconds()))
        delay = max(1.0, min(MAX_SLEEP_S, soonest))
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        finally:
            self._wake.clear()

    # -- one run ----------------------------------------------------------------
    async def _fire_and_record(self, routine: Routine) -> None:
        store = get_routines_store()
        try:
            status, preview, error = await asyncio.wait_for(
                self._fire(routine), timeout=RUN_TIMEOUT_S
            )
        except (asyncio.TimeoutError, TimeoutError):
            status, preview, error = "timeout", "", f"run exceeded {RUN_TIMEOUT_S:.0f}s"
            get_inbox().add(
                routine_id=routine.id, routine_name=routine.name, kind="error",
                title=f"{routine.name} timed out", body=error or "", status="timeout",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            status, preview, error = "error", "", f"{type(exc).__name__}: {exc}"
            get_inbox().add(
                routine_id=routine.id, routine_name=routine.name, kind="error",
                title=f"{routine.name} failed", body=error or "", status="error",
            )
        finally:
            self._running.discard(routine.id)
        store.record_result(routine.id, status=status, preview=preview[:PREVIEW_CHARS], error=error)
        nf = next_fire(store.get(routine.id) or routine, _now())
        store.advance_next_fire(routine.id, _iso(nf) if nf else None)

    async def _fire(self, routine: Routine) -> tuple[str, str, str | None]:
        """Run the routine's prompt through the app's agent; write the result to the inbox.

        Returns ``(status, preview, error)`` for the routine row. Uses ``ask_turn`` (HITL on)
        so an approval-gated tool PARKS as a 'needs approval' inbox item instead of firing
        unattended — the scheduler never auto-approves.

        The seeded Morning Brief is special-cased through :class:`~himmy_app.brief.DailyBrief` so
        its 07:00 generation populates ``brief_cache.json`` — the Today card then serves that same
        cache instantly (one brief per morning, no double generation). The rich brief is read-only
        (its prompt forbids any send/mutate), so this path never needs HITL. A user who RENAMES the
        routine in the UI drops cache-sharing and falls back to ask_turn(prompt) below — still
        correct, just may generate twice that day.
        """
        from himmy_app.cli import ask_turn

        if routine.name == _BRIEFING_NAME:
            from himmy_app.brief import DailyBrief

            text = (await DailyBrief(load_config()).get_or_make(force=False)).strip()
            get_inbox().add(
                routine_id=routine.id,
                routine_name=routine.name,
                kind="result",
                title=routine.name,
                body=text or "(no output)",
                status="ok",
            )
            return "ok", text, None

        res = await ask_turn(routine.prompt)  # no session_id: routines don't pollute chat history
        if res.get("awaiting_approval"):
            cp = res.get("checkpoint_id")
            pending = res.get("pending") or []
            names = ", ".join(p.get("tool_name", "?") for p in pending) or "an action"
            self_inbox = get_inbox()
            self_inbox.add(
                routine_id=routine.id,
                routine_name=routine.name,
                kind="approval",
                title=f"{routine.name} needs your approval",
                body=f"This routine wants to run: {names}. Open it to approve or cancel.",
                status="awaiting_approval",
                checkpoint_id=cp,
            )
            return "awaiting_approval", f"awaiting approval: {names}", None

        reply = (res.get("reply") or "").strip()
        get_inbox().add(
            routine_id=routine.id,
            routine_name=routine.name,
            kind="result",
            title=routine.name,
            body=reply or "(no output)",
            status="ok",
        )
        return "ok", reply, None

    async def run_now(self, routine_id: str) -> dict[str, Any]:
        """Fire a routine immediately (the 'Run now' button), inline, returning its result."""
        store = get_routines_store()
        r = store.get(routine_id)
        if r is None:
            return {"ok": False, "error": "routine not found"}
        if r.id in self._running:
            return {"ok": False, "error": "this routine is already running"}
        self._running.add(r.id)
        store.mark_started(r.id, _iso(_now()))  # unconditional: a manual run always fires
        try:
            status, preview, error = await asyncio.wait_for(self._fire(r), timeout=RUN_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            status, preview, error = "timeout", "", f"run exceeded {RUN_TIMEOUT_S:.0f}s"
        except Exception as exc:  # noqa: BLE001
            status, preview, error = "error", "", f"{type(exc).__name__}: {exc}"
        finally:
            self._running.discard(r.id)
        store.record_result(r.id, status=status, preview=preview[:PREVIEW_CHARS], error=error)
        nf = next_fire(store.get(r.id) or r, _now())
        store.advance_next_fire(r.id, _iso(nf) if nf else None)
        return {"ok": status == "ok", "status": status, "preview": preview, "error": error}


_SCHEDULER: AppScheduler | None = None


def get_scheduler() -> AppScheduler:
    global _SCHEDULER
    if _SCHEDULER is None:
        _SCHEDULER = AppScheduler()
    return _SCHEDULER


# ---------------------------------------------------------------------------------------
# Built-in seed: the flagship "Morning Brief" — the rich daily brief, pushed at ~07:00.
# ---------------------------------------------------------------------------------------
#: A marker so we seed the briefing exactly once (idempotent across restarts). Bumped from the
#: old "Daily Briefing" so existing installs are UPGRADED to the enabled 07:00 rich push (the old
#: disabled-weekday-thin row would otherwise survive its name match — see the migration below).
_BRIEFING_NAME = "Morning Brief"
#: The previous seed's name — migrated away (deleted) on first seed so two briefing routines
#: never coexist. The thin _BRIEFING_PROMPT it used is gone; the rich prompt now lives in
#: brief._BRIEF_PROMPT, shared word-for-word with the Today card.
_OLD_BRIEFING_NAME = "Daily Briefing"


def seed_default_routines() -> None:
    """Create the built-in Morning Brief once — the rich daily brief, daily 07:00, ENABLED.

    This is the "morning push": the rich brief (brief._BRIEF_PROMPT — the exact content the Today
    card shows) fires at 07:00 in HIMMY_TZ and lands in the inbox the bell reads, so it reaches the
    user before they open the app. ``missed="coalesce"`` means a backend that boots after 07:00
    still fires once that morning (the catch-up a push wants).

    Idempotent + don't-clobber: if a Morning Brief already exists we leave it untouched, so the
    user's edits/enabled-state survive restarts. One-time migration: an existing install's old
    'Daily Briefing' row (disabled, weekday 06:30, thin prompt) is deleted so it doesn't linger
    alongside the new push.
    """
    from himmy_app.brief import _BRIEF_PROMPT  # the rich prompt, shared with the Today card

    store = get_routines_store()
    if any(r.name == _BRIEFING_NAME for r in store.list()):
        return
    # One-time upgrade: drop the superseded 'Daily Briefing' seed if it's still around.
    for r in store.list():
        if r.name == _OLD_BRIEFING_NAME:
            store.delete(r.id)
    cfg = load_config()
    store.upsert(
        Routine(
            name=_BRIEFING_NAME,
            agent_path=str(_SPEC),
            prompt=_BRIEF_PROMPT,
            # Daily 07:00 in the configured timezone (HIMMY_TZ → Asia/Kathmandu); coalesce so a
            # late boot still pushes once that morning.
            schedule=Schedule(kind="cron", expr="0 7 * * *", missed="coalesce"),
            provider=cfg.provider,
            model=cfg.model,
            enabled=True,  # the morning push is on by default
        )
    )
