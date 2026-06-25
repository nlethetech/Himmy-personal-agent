"""Reading-time tracking — the honest 'dwell time' signal.

The Reader (the PDF view in the desktop app) reports ENGAGED seconds in small heartbeats: it
only counts time while its window is focused AND the user is actually active (scrolling, turning
pages, moving the mouse / pressing keys). Idle time and "left the paper open over lunch" never
count — the front-end pauses the clock, and this module ALSO clamps every heartbeat to a sane
maximum so a buggy or over-eager client can't inflate the numbers.

We durably accumulate those engaged seconds per paper (one row per reading *session*) and expose
the aggregates the two consumers need:

  * recsys (the taste profile) — total engaged time per paper is the strongest implicit signal of
    genuine interest, far better than "the user added this paper and never opened it". Also the
    most-recently-read time drives recency, so the threads you're reading NOW lead the profile.
  * the Today home — "you read 4.2 hours this week", and a per-paper "read 42m".
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from himmy_app.config import HimmyConfig, load_config

#: A heartbeat reports the engaged seconds since the last flush. The Reader flushes every ~15s,
#: so a legitimate delta is at most ~20s — anything larger is a bug, a clock jump, or tampering.
#: Clamping here is the server-side guarantee that reading time stays honest no matter the client.
_MAX_HEARTBEAT_DELTA = 30.0
#: Sessions below this are accidental opens / instant closes — excluded from every aggregate so
#: they neither pollute the recsys signal nor the "time read" you see.
_MIN_SESSION_SECONDS = 5.0


class ReadingStore:
    """Durable per-paper engaged-reading time, accumulated from the Reader's heartbeats."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        cfg = config or load_config()
        self._db = cfg.reading_db_path
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        # A short timeout so a concurrent writer (overlapping heartbeats) waits rather than erroring.
        conn = sqlite3.connect(str(self._db), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,        -- client-generated, stable for one open of a paper
                    item_id TEXT NOT NULL,
                    started_at REAL,
                    ended_at REAL,
                    engaged_seconds REAL DEFAULT 0
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_item ON sessions(item_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_ended ON sessions(ended_at)")
            # Resume point: where you last left off in each paper, so reopening it (after a tab
            # switch or an app restart) drops you back on the same page instead of page 1.
            c.execute(
                """CREATE TABLE IF NOT EXISTS positions (
                    item_id TEXT PRIMARY KEY,   -- one resume point per paper
                    page INTEGER DEFAULT 1,     -- 1-based page the reader was anchored on
                    frac REAL DEFAULT 0,        -- fraction scrolled within that page, [0, 1)
                    num_pages INTEGER,          -- total pages (for a "page X of Y" label)
                    updated_at REAL
                )"""
            )

    # ---- write ---------------------------------------------------------------------------
    def record_heartbeat(self, session_id: str, item_id: str, seconds: float) -> dict[str, Any]:
        """Add ``seconds`` of engaged time to a reading session (creating it on first beat).

        The delta is clamped to :data:`_MAX_HEARTBEAT_DELTA` — the honest-time guarantee that
        holds even if the client is buggy or hostile. Returns the running session + item totals.
        """
        sid = (session_id or "").strip()
        iid = (item_id or "").strip()
        if not sid or not iid:
            return {"ok": False, "error": "session_id and item_id are required"}
        delta = max(0.0, min(float(seconds or 0.0), _MAX_HEARTBEAT_DELTA))
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO sessions (id, item_id, started_at, ended_at, engaged_seconds)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     ended_at = excluded.ended_at,
                     engaged_seconds = engaged_seconds + excluded.engaged_seconds""",
                (sid, iid, now, now, delta),
            )
            session_seconds = float(
                c.execute("SELECT engaged_seconds FROM sessions WHERE id = ?", (sid,)).fetchone()[0]
            )
        return {
            "ok": True,
            "item_id": iid,
            "session_seconds": session_seconds,
            "item_seconds": self.item_seconds(iid),
        }

    # ---- reading position (resume where you left off) ------------------------------------
    def set_position(
        self, item_id: str, page: int, frac: float, num_pages: int | None = None
    ) -> dict[str, Any]:
        """Remember where the reader is in a paper. Last write wins — one resume point per paper."""
        iid = (item_id or "").strip()
        if not iid:
            return {"ok": False, "error": "item_id is required"}
        pg = max(1, int(page or 1))
        fr = min(1.0, max(0.0, float(frac or 0.0)))
        npg = int(num_pages) if num_pages else None
        with self._conn() as c:
            c.execute(
                """INSERT INTO positions (item_id, page, frac, num_pages, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(item_id) DO UPDATE SET
                     page = excluded.page,
                     frac = excluded.frac,
                     num_pages = COALESCE(excluded.num_pages, positions.num_pages),
                     updated_at = excluded.updated_at""",
                (iid, pg, fr, npg, time.time()),
            )
        return {"ok": True, "item_id": iid, "page": pg, "frac": fr}

    def get_position(self, item_id: str) -> dict[str, Any] | None:
        """The saved resume point for a paper, or ``None`` if it's never been read."""
        iid = (item_id or "").strip()
        if not iid:
            return None
        with self._conn() as c:
            r = c.execute(
                "SELECT page, frac, num_pages, updated_at FROM positions WHERE item_id = ?",
                (iid,),
            ).fetchone()
        if not r:
            return None
        return {
            "page": int(r["page"] or 1),
            "frac": float(r["frac"] or 0.0),
            "num_pages": int(r["num_pages"]) if r["num_pages"] is not None else None,
            "updated_at": float(r["updated_at"] or 0.0),
        }

    # ---- aggregates (read) ---------------------------------------------------------------
    def item_seconds(self, item_id: str) -> float:
        """Total engaged seconds across all of a paper's (non-trivial) reading sessions."""
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(engaged_seconds), 0) AS s FROM sessions "
                "WHERE item_id = ? AND engaged_seconds >= ?",
                (item_id, _MIN_SESSION_SECONDS),
            ).fetchone()
        return float(r["s"] or 0.0)

    def last_read(self, item_id: str) -> float | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT MAX(ended_at) AS t FROM sessions WHERE item_id = ? AND engaged_seconds >= ?",
                (item_id, _MIN_SESSION_SECONDS),
            ).fetchone()
        return float(r["t"]) if r and r["t"] is not None else None

    def totals_by_item(self) -> dict[str, float]:
        """``{item_id: total_engaged_seconds}`` — the recsys reading signal, in one query."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT item_id, SUM(engaged_seconds) AS s FROM sessions "
                "WHERE engaged_seconds >= ? GROUP BY item_id",
                (_MIN_SESSION_SECONDS,),
            ).fetchall()
        return {r["item_id"]: float(r["s"] or 0.0) for r in rows}

    def last_read_by_item(self) -> dict[str, float]:
        """``{item_id: last_read_epoch}`` — drives recency (threads read recently lead the profile)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT item_id, MAX(ended_at) AS t FROM sessions "
                "WHERE engaged_seconds >= ? GROUP BY item_id",
                (_MIN_SESSION_SECONDS,),
            ).fetchall()
        return {r["item_id"]: float(r["t"]) for r in rows if r["t"] is not None}

    def total_since(self, since_epoch: float) -> float:
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(engaged_seconds), 0) AS s FROM sessions "
                "WHERE ended_at >= ? AND engaged_seconds >= ?",
                (since_epoch, _MIN_SESSION_SECONDS),
            ).fetchone()
        return float(r["s"] or 0.0)

    def stats(self, now: float | None = None) -> dict[str, Any]:
        """Reading totals for the Today home: today, this week (rolling 7 days), all-time."""
        now = time.time() if now is None else now
        return {
            "ok": True,
            "today_seconds": self.total_since(now - 86400.0),
            "week_seconds": self.total_since(now - 7 * 86400.0),
            "total_seconds": self.total_since(0.0),
        }


__all__ = ["ReadingStore"]
