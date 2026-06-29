"""Himmy's proactive brain — an always-on chief-of-staff layer.

The idea: a small, high-quality stream of "Himmy noticed …" OBSERVATIONS that watch across
surfaces (tasks, Money/finance, calendar, mail), each with a one-tap action and a ready-to-run
instruction Himmy's agent can execute (through the normal HITL approval gate, so risky actions
auto-pause). The brain runs in two stages:

  1. DETERMINISTIC RULES (no model, free, robust) — tasks due/overdue, Money over/near the saved
     Food budget + this-week spend spikes, an upcoming calendar meeting in the next ~hour ("prep"),
     focused mail unreplied for 3+ days. Every category is PERMISSION-GATED via
     :func:`himmy_app.permissions.level_of` and best-effort (one bad source never blocks the rest).
  2. ONE CHEAP MODEL PASS — a single :func:`build_inference_for` completion over a compact JSON
     snapshot produces the cross-surface CONNECT-THE-DOTS observations (a bill in mail → log to
     Money; an email deadline → a task + a calendar hold; meeting prep). The model returns ONLY a
     JSON list; parse is FAIL-OPEN (a flaky/empty/garbled response simply adds nothing).

DEDUPE + NOISE DISCIPLINE: each observation has a stable ``key``; we never re-create what is
already active/dismissed/snoozed, and we cap the active set to ~6. Snooze hides until its
``snooze_until``.

PUSH: a NEW important observation is mirrored into the SAME notification Inbox the bell + macOS
notification already read (via :meth:`Inbox.add_nudge` with a stable key) AND, when a Telegram bot
is linked, pushed via :func:`himmy_app.telegram.push` — both respecting the ``proactive_level``
setting and QUIET HOURS (22:00–07:00 local, no interrupts).

EXECUTE: an observation's action runs its ``instruction`` through :func:`himmy_app.cli.ask_turn`
(full tools + HITL), so a risky action (send/spend/calendar change) parks for approval instead of
firing silently — proactivity NEVER acts silently on anything risky.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import re
import sqlite3
import uuid
from typing import Any

from himmy_app import permissions as perms
from himmy_app.config import HimmyConfig, load_config

# ---------------------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------------------
#: How often the always-on background loop refreshes (seconds). ~45 min.
PROACTIVE_INTERVAL_S = 45 * 60
#: Cap on the active observation set so the brain is a short, high-quality list, never a flood.
MAX_ACTIVE = 6
#: A focused message must be unread AND at least this old to count as "gone unreplied".
UNREPLIED_DAYS = 3
#: "Meeting soon" prep window — a timed calendar event starting within this many minutes.
PREP_WINDOW_MIN = 75
#: This-week spend-spike threshold: flag when the last 7 days outpace a normal week by this factor.
SPIKE_FACTOR = 1.6
#: Don't bother flagging a tiny spike — only weeks above this NPR amount.
SPIKE_MIN_NPR = 1500.0
#: Near-budget threshold: warn when Food spend this month reaches this fraction of the budget.
BUDGET_NEAR_FRAC = 0.85
#: Quiet hours (local wall-clock, inclusive start / exclusive end) — no push interrupts.
QUIET_START_H = 22
QUIET_END_H = 7
#: The proactive levels, least→most active. Default is the fullest.
PROACTIVE_LEVELS = ("off", "gentle", "calm", "always")
DEFAULT_LEVEL = "always"
#: Observation kinds (for the frontend to render an icon/badge per type).
KINDS = ("deterministic", "connect", "prep", "budget", "mail", "task", "trip")


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> datetime.date:
    return datetime.date.today()


def _local_now() -> datetime.datetime:
    """Wall-clock 'now' in the configured timezone (HIMMY_TZ → Asia/Kathmandu, else UTC).

    Reuses :func:`himmy_app.routines._local_zone` so quiet-hours + rundown timing read the same
    Nepal-local clock the rest of the app anchors to.
    """
    from himmy_app.routines import _local_zone

    return datetime.datetime.now(_local_zone())


def in_quiet_hours(now: datetime.datetime | None = None) -> bool:
    """True if the LOCAL wall-clock is inside quiet hours (22:00–07:00) — no push interrupts."""
    n = now or _local_now()
    h = n.hour
    # Window wraps midnight: 22,23,0,...,6 are quiet; 7..21 are awake.
    if QUIET_START_H > QUIET_END_H:
        return h >= QUIET_START_H or h < QUIET_END_H
    return QUIET_START_H <= h < QUIET_END_H


# ---------------------------------------------------------------------------------------
# proactive_level — a tiny JSON in the data dir (mirrors telegram.json / model.json)
# ---------------------------------------------------------------------------------------
def _level_path(cfg: HimmyConfig):
    return cfg.data_dir / "proactive.json"


def get_level(cfg: HimmyConfig | None = None) -> str:
    """The current proactive level (off|gentle|calm|always); defaults to ``always``."""
    cfg = cfg or load_config()
    try:
        d = json.loads(_level_path(cfg).read_text(encoding="utf-8"))
        lvl = str(d.get("level") or "").strip().lower()
        if lvl in PROACTIVE_LEVELS:
            return lvl
    except Exception:  # noqa: BLE001 - first run / corrupt file → default
        pass
    return DEFAULT_LEVEL


def set_level(level: str, cfg: HimmyConfig | None = None) -> str:
    """Persist the proactive level; invalid values fall back to the default. Returns the saved value."""
    cfg = cfg or load_config()
    lvl = str(level or "").strip().lower()
    if lvl not in PROACTIVE_LEVELS:
        lvl = DEFAULT_LEVEL
    p = _level_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"level": lvl}), encoding="utf-8")
    return lvl


# ---------------------------------------------------------------------------------------
# ObservationStore — proactive.db (add / list-active / get / dismiss / snooze, deduped by key)
# ---------------------------------------------------------------------------------------
class ObservationStore:
    """A tiny SQLite store of proactive observations, deduped by a stable ``key``."""

    def __init__(self, cfg: HimmyConfig | None = None) -> None:
        cfg = cfg or load_config()
        self._cfg = cfg
        self._conn = sqlite3.connect(str(cfg.data_dir / "proactive.db"), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id            TEXT PRIMARY KEY,
                key           TEXT UNIQUE,
                kind          TEXT NOT NULL DEFAULT 'deterministic',
                title         TEXT NOT NULL,
                detail        TEXT NOT NULL DEFAULT '',
                action_label  TEXT NOT NULL DEFAULT '',
                instruction   TEXT NOT NULL DEFAULT '',
                surface       TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'active',  -- active|done|dismissed|snoozed
                snooze_until  TEXT,
                created       TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def _row(self, r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"],
            "key": r["key"],
            "kind": r["kind"],
            "title": r["title"],
            "detail": r["detail"],
            "action_label": r["action_label"],
            "instruction": r["instruction"],
            "surface": r["surface"],
            "status": r["status"],
            "snooze_until": r["snooze_until"],
            "created": r["created"],
        }

    def get(self, obs_id: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
        return self._row(r) if r else None

    def get_by_key(self, key: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM observations WHERE key = ?", (key,)).fetchone()
        return self._row(r) if r else None

    def exists_key(self, key: str) -> bool:
        """True if an observation with this key already exists in ANY status.

        This is the dedup heart: we NEVER re-create an observation the user has already seen,
        whether it's still active, done, dismissed, or snoozed.
        """
        return self._conn.execute(
            "SELECT 1 FROM observations WHERE key = ?", (key,)
        ).fetchone() is not None

    def add(self, obs: dict[str, Any]) -> dict[str, Any] | None:
        """Insert a new observation keyed by ``key``; no-op (return None) if the key already exists."""
        key = str(obs.get("key") or "").strip()
        if not key or self.exists_key(key):
            return None
        # Defensive cap (consistent with notice()'s own pre-check): never let the ACTIVE set grow
        # past MAX_ACTIVE, even for a caller outside notice(). Keeps the stored set bounded; the
        # noise discipline that makes the proactive layer feel like signal, not spam.
        if self.active_count() >= MAX_ACTIVE:
            return None
        kind = str(obs.get("kind") or "deterministic").strip() or "deterministic"
        nid = uuid.uuid4().hex
        created = _iso(_now_utc())
        self._conn.execute(
            "INSERT INTO observations (id, key, kind, title, detail, action_label, instruction,"
            " surface, status, snooze_until, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                nid, key, kind,
                str(obs.get("title") or "").strip()[:160],
                str(obs.get("detail") or "").strip()[:400],
                str(obs.get("action_label") or "").strip()[:60],
                str(obs.get("instruction") or "").strip()[:600],
                str(obs.get("surface") or "").strip()[:40],
                "active", None, created,
            ),
        )
        self._conn.commit()
        return self.get(nid)

    def list_active(self, *, limit: int = MAX_ACTIVE) -> list[dict[str, Any]]:
        """Active observations, newest first. A snoozed row whose time has passed re-activates."""
        self._wake_snoozed()
        rows = self._conn.execute(
            "SELECT * FROM observations WHERE status = 'active' ORDER BY created DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def active_count(self) -> int:
        self._wake_snoozed()
        r = self._conn.execute(
            "SELECT COUNT(*) AS n FROM observations WHERE status = 'active'"
        ).fetchone()
        return int(r["n"]) if r else 0

    def _wake_snoozed(self) -> None:
        """Re-activate any snoozed observation whose snooze_until has passed."""
        now = _iso(_now_utc())
        self._conn.execute(
            "UPDATE observations SET status = 'active', snooze_until = NULL "
            "WHERE status = 'snoozed' AND snooze_until IS NOT NULL AND snooze_until <= ?",
            (now,),
        )
        self._conn.commit()

    def dismiss(self, obs_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE observations SET status = 'dismissed' WHERE id = ?", (obs_id,)
        )
        self._conn.commit()
        return cur.rowcount == 1

    def mark_done(self, obs_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE observations SET status = 'done' WHERE id = ?", (obs_id,)
        )
        self._conn.commit()
        return cur.rowcount == 1

    def snooze(self, obs_id: str, hours: float) -> dict[str, Any] | None:
        until = _iso(_now_utc() + datetime.timedelta(hours=max(0.0, float(hours))))
        cur = self._conn.execute(
            "UPDATE observations SET status = 'snoozed', snooze_until = ? WHERE id = ?",
            (until, obs_id),
        )
        self._conn.commit()
        return self.get(obs_id) if cur.rowcount == 1 else None


_STORE: ObservationStore | None = None


def get_store() -> ObservationStore:
    global _STORE
    if _STORE is None:
        _STORE = ObservationStore(load_config())
    return _STORE


# ---------------------------------------------------------------------------------------
# Signal gathering — permission-gated, best-effort, across tasks / Money / calendar / mail
# ---------------------------------------------------------------------------------------
def _rfc3339_z(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _food_budget(cfg: HimmyConfig) -> float | None:
    """The user's saved Food budget (NPR) from the confirmed profile vault, or None.

    Mirrors :meth:`himmy_app.do_concierge.DoConcierge.food_budget` (a 'Food budget'/food+spend
    label), reading ONLY the user-confirmed ``details`` vault — never the learned layer.
    """
    try:
        from himmy_app import user_profile

        prof = user_profile.load(cfg)
        details = (prof.get("user") or {}).get("details") or {}
        for label, value in details.items():
            ll = str(label).lower()
            if "food" in ll and ("budget" in ll or "spend" in ll or "price" in ll):
                m = re.search(r"(\d[\d,]*\.?\d*)", str(value or ""))
                if m:
                    try:
                        v = float(m.group(1).replace(",", ""))
                        if v > 0:
                            return v
                    except (TypeError, ValueError):
                        pass
    except Exception:  # noqa: BLE001 - the vault is optional
        return None
    return None


async def gather_signals(cfg: HimmyConfig | None = None) -> dict[str, Any]:
    """Collect a compact, permission-gated snapshot of the user's cross-surface state.

    Every category is gated on ``perms.level_of(<surface>) != 'off'`` and wrapped so one failing
    source never blocks the others. The returned dict is BOTH the input the deterministic rules
    read AND the compact JSON the connect-the-dots model pass sees.
    """
    cfg = cfg or load_config()
    now = _now_utc()
    today = _today()
    snap: dict[str, Any] = {
        "now": _iso(now),
        "today": today.isoformat(),
        "tasks": [],
        "finance": {},
        "calendar": [],
        "mail": [],
        "errors": {},
    }

    # ---- Tasks (himmy tasks store) ----
    try:
        if perms.level_of("tasks", cfg) != "off":
            from himmy.api.studio_tasks import get_tasks_store

            for t in get_tasks_store().list():
                if t.done or not t.due:
                    continue
                try:
                    due = datetime.date.fromisoformat(str(t.due)[:10])
                except Exception:  # noqa: BLE001
                    continue
                snap["tasks"].append({
                    "id": t.id,
                    "title": (t.title or "").strip() or "Untitled task",
                    "due": due.isoformat(),
                    "overdue": due < today,
                    "due_today": due == today,
                })
    except Exception as exc:  # noqa: BLE001
        snap["errors"]["tasks"] = f"{type(exc).__name__}: {exc}"

    # ---- Finance / Money (ExpenseStore + Food budget vault) ----
    try:
        if perms.level_of("finance", cfg) != "off":
            from himmy_app.finance import ExpenseStore

            store = ExpenseStore(cfg)
            month = store.summary("month")
            week = store.summary("week")
            food_month = next(
                (c["total"] for c in month.get("by_category", []) if c["category"] == "Food"),
                0.0,
            )
            snap["finance"] = {
                "currency": month.get("currency", "NPR"),
                "month_total": month.get("total", 0.0),
                "week_total": week.get("total", 0.0),
                "food_month": round(float(food_month), 2),
                "food_budget": _food_budget(cfg),
                "by_category_month": month.get("by_category", []),
            }
    except Exception as exc:  # noqa: BLE001
        snap["errors"]["finance"] = f"{type(exc).__name__}: {exc}"

    # ---- Calendar (only if Google connected) ----
    try:
        if perms.level_of("calendar", cfg) != "off":
            from himmy.api import studio_google as g

            if g.status().connected:
                time_min = _rfc3339_z(now)
                time_max = _rfc3339_z(now + datetime.timedelta(days=2))
                events = await g.calendar_range(time_min, time_max, 250)
                for e in events[:50]:
                    start = (getattr(e, "start", "") or "").strip()
                    if not start:
                        continue
                    mins_away = _minutes_until(start, now)
                    snap["calendar"].append({
                        "id": getattr(e, "id", ""),
                        "summary": (getattr(e, "summary", "") or "Event").strip() or "Event",
                        "start": start,
                        "location": (getattr(e, "location", "") or "").strip(),
                        "minutes_away": mins_away,
                        "timed": len(start) > 10,
                    })
    except Exception as exc:  # noqa: BLE001
        snap["errors"]["calendar"] = f"{type(exc).__name__}: {exc}"

    # ---- Mail (only if connected) ----
    try:
        if perms.level_of("mail", cfg) != "off":
            from himmy.api import studio_google as g

            if g.status().connected:
                from himmy_app.server import _normalize_sender, is_automated, load_mail_rules

                msgs = await g.gmail_list(50)
                muted = set(load_mail_rules(cfg)["muted"])
                for m in msgs:
                    if not getattr(m, "unread", False):
                        continue
                    if is_automated(m.sender):
                        continue
                    if _normalize_sender(m.sender) in muted:
                        continue
                    age = _mail_age_days(m.date, now)
                    if age is None or age < UNREPLIED_DAYS:
                        continue
                    snap["mail"].append({
                        "id": m.id,
                        "subject": (m.subject or "").strip() or "(no subject)",
                        "sender": _sender_name(m.sender),
                        "age_days": age,
                    })
    except Exception as exc:  # noqa: BLE001
        snap["errors"]["mail"] = f"{type(exc).__name__}: {exc}"

    return snap


def _minutes_until(start: str, now: datetime.datetime) -> int | None:
    """Whole minutes from ``now`` until a timed RFC3339 start, or None for an all-day date."""
    if len(start) <= 10:
        return None
    try:
        dt = datetime.datetime.fromisoformat(start)
    except Exception:  # noqa: BLE001
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int((dt - now).total_seconds() // 60)


def _mail_age_days(raw_date: str, now: datetime.datetime) -> int | None:
    if not raw_date:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw_date)
    except Exception:  # noqa: BLE001
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return max(0, (now - dt).days)


def _sender_name(sender: str) -> str:
    name, addr = email.utils.parseaddr(sender or "")
    return (name or addr or sender or "someone").strip()


# ---------------------------------------------------------------------------------------
# Deterministic rules — turn the snapshot into observations (no model)
# ---------------------------------------------------------------------------------------
def _deterministic_observations(snap: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    today = snap.get("today") or _today().isoformat()

    # --- Tasks due today / overdue ---
    for t in snap.get("tasks", []):
        title = t["title"]
        if t.get("overdue"):
            out.append({
                "key": f"task-overdue-{t['id']}-{today}",
                "kind": "task",
                "surface": "tasks",
                "title": f"Overdue: {title}",
                "detail": f"'{title}' was due {t['due']} and isn't done yet.",
                "action_label": "Reschedule",
                "instruction": f"My task '{title}' is overdue (was due {t['due']}). "
                               "Help me reschedule it to a realistic new due date.",
            })
        elif t.get("due_today"):
            out.append({
                "key": f"task-due-{t['id']}-{today}",
                "kind": "task",
                "surface": "tasks",
                "title": f"Due today: {title}",
                "detail": f"'{title}' is due today.",
                "action_label": "Block time",
                "instruction": f"My task '{title}' is due today. Find a free slot on my calendar "
                               "today and propose blocking time to finish it.",
            })

    # --- Money: over / near the Food budget, and a this-week spend spike ---
    fin = snap.get("finance") or {}
    budget = fin.get("food_budget")
    food_month = float(fin.get("food_month") or 0.0)
    cur = fin.get("currency") or "NPR"
    month_key = today[:7]
    if budget:
        budget = float(budget)
        if food_month > budget:
            out.append({
                "key": f"budget-food-over-{month_key}",
                "kind": "budget",
                "surface": "finance",
                "title": f"Over your Food budget",
                "detail": f"Food spend this month is {cur} {food_month:,.0f}, over your "
                          f"{cur} {budget:,.0f} budget.",
                "action_label": "Review Money",
                "instruction": "Show me my Food spending this month broken down, and suggest where "
                               "I could cut back to get back under my Food budget.",
            })
        elif food_month >= budget * BUDGET_NEAR_FRAC:
            pct = int(round(100 * food_month / budget))
            out.append({
                "key": f"budget-food-near-{month_key}",
                "kind": "budget",
                "surface": "finance",
                "title": "Nearing your Food budget",
                "detail": f"Food spend is {cur} {food_month:,.0f} — about {pct}% of your "
                          f"{cur} {budget:,.0f} budget.",
                "action_label": "Review Money",
                "instruction": "Summarise my Food spending this month and how much room is left "
                               "before I hit my Food budget.",
            })

    week_total = float(fin.get("week_total") or 0.0)
    month_total = float(fin.get("month_total") or 0.0)
    # Rough baseline: an average week ≈ month/4. Flag when this week clearly outpaces it.
    avg_week = month_total / 4.0 if month_total else 0.0
    if (
        week_total >= SPIKE_MIN_NPR
        and avg_week > 0
        and week_total >= avg_week * SPIKE_FACTOR
    ):
        out.append({
            "key": f"spend-spike-{_week_key(today)}",
            "kind": "budget",
            "surface": "finance",
            "title": "Spending up this week",
            "detail": f"You've spent {cur} {week_total:,.0f} in the last 7 days — well above your "
                      "usual week.",
            "action_label": "Review Money",
            "instruction": "Show me what I spent in the last 7 days by category and flag anything "
                           "unusual compared to a normal week.",
        })

    # --- Calendar: a meeting in the next ~hour → a prep observation ---
    for e in snap.get("calendar", []):
        mins = e.get("minutes_away")
        if not e.get("timed") or mins is None:
            continue
        if 0 <= mins <= PREP_WINDOW_MIN:
            summary = e["summary"]
            out.append({
                "key": f"prep-{e['id']}-{e['start'][:16]}",
                "kind": "prep",
                "surface": "calendar",
                "title": f"Prep for {summary}",
                "detail": f"'{summary}' starts in about {mins} min.",
                "action_label": "Prep me",
                "instruction": f"I have '{summary}' in about {mins} minutes. Pull together a quick "
                               "prep: what it's about, who's involved, and any related mail or notes.",
            })

    # --- Mail: focused mail unreplied 3+ days ---
    for m in snap.get("mail", []):
        subject = m["subject"]
        out.append({
            "key": f"mail-unreplied-{m['id']}",
            "kind": "mail",
            "surface": "mail",
            "title": f"Unreplied {m['age_days']}d: {subject}",
            "detail": f"'{subject}' from {m['sender']} has been unread for {m['age_days']} days.",
            "action_label": "Draft a reply",
            "instruction": f"Draft a reply to the email '{subject}' from {m['sender']}. Show me the "
                           "draft before sending.",
        })

    return out


def _week_key(today_iso: str) -> str:
    """An ISO year-week key (so a spend-spike re-dedups week-over-week)."""
    try:
        d = datetime.date.fromisoformat(today_iso)
    except Exception:  # noqa: BLE001
        d = _today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


# ---------------------------------------------------------------------------------------
# The one cheap model pass — cross-surface "connect the dots" (fail-open)
# ---------------------------------------------------------------------------------------
_CONNECT_SYSTEM = (
    "You are Himmy's proactive brain. You are given a compact JSON snapshot of the user's day "
    "across tasks, finance (Money), calendar and mail. Find at most 3 GENUINELY useful "
    "CROSS-SURFACE observations that connect two surfaces — e.g. a bill mentioned in mail that "
    "should be logged to Money; an email deadline that should become a task plus a calendar hold; "
    "prep needed for an upcoming meeting given related mail. Do NOT restate a single-surface fact "
    "(an overdue task on its own, a budget overage on its own) — the deterministic layer already "
    "handles those. Only surface something that genuinely helps and is actionable in one tap.\n"
    "Return ONLY a JSON array (no prose, no markdown). Each item must be an object with keys: "
    "key (short stable slug), kind (one of: connect, prep, budget, mail, task, trip), title (<=8 "
    "words), detail (one short line of why), action_label (e.g. 'Log to Money', 'Draft a reply', "
    "'Block time'), instruction (a single natural-language command Himmy's agent can run to DO it), "
    "surface (the primary surface: tasks|finance|calendar|mail). If nothing is worth surfacing, "
    "return []."
)


async def _connect_observations(cfg: HimmyConfig, snap: dict[str, Any]) -> list[dict[str, Any]]:
    """One cheap model completion over the snapshot → cross-surface observations. FAIL-OPEN."""
    # Connecting the dots needs at least one "rich" surface (mail or calendar) plus something to
    # connect it to. If there's no mail and no calendar there's nothing cross-surface to reason
    # over, so skip the model call entirely (free + quiet).
    if not snap.get("mail") and not snap.get("calendar"):
        return []
    try:
        from himmy.cli.provider import build_inference_for
        from himmy.services.inference.models import InferenceMessage, InferenceRequest

        # Compact the snapshot (drop verbose category lists the model doesn't need).
        compact = {
            "today": snap.get("today"),
            "tasks": snap.get("tasks", [])[:10],
            "finance": {
                k: v for k, v in (snap.get("finance") or {}).items()
                if k != "by_category_month"
            },
            "calendar": snap.get("calendar", [])[:10],
            "mail": snap.get("mail", [])[:8],
        }
        service = build_inference_for(cfg.provider, cfg.model)
        req = InferenceRequest(
            messages=[
                InferenceMessage(role="system", content=_CONNECT_SYSTEM),
                InferenceMessage(role="user", content=json.dumps(compact, ensure_ascii=False)),
            ],
            generation_params={"temperature": 0.2},
            timeout_seconds=45.0,
        )
        resp = await service.run(req)
        return _parse_connect(resp.output_text or "")
    except Exception:  # noqa: BLE001 - the model pass is best-effort; failure adds nothing
        return []


def _parse_connect(text: str) -> list[dict[str, Any]]:
    """Parse the model's JSON array, fail-open. Tolerates ```json fences and surrounding prose."""
    s = (text or "").strip()
    if not s:
        return []
    # Strip code fences if present.
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s).rstrip("`").strip()
    # Find the first JSON array in the blob.
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(s[start:end + 1])
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(data[:3]):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        # Title + instruction MUST be real strings — a model returning a list/dict here would
        # coerce to a truthy "['a']" and slip a garbage observation past the guard below.
        title = item.get("title")
        instruction = item.get("instruction")
        if not isinstance(title, str) or not isinstance(instruction, str):
            continue
        title, instruction = title.strip(), instruction.strip()
        if not title or not instruction:
            continue
        kind = str(item.get("kind") or "connect").strip().lower()
        if kind not in KINDS:
            kind = "connect"
        # Namespace the key so the model can't collide with deterministic keys or repeat itself.
        slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-") or f"item-{i}"
        out.append({
            "key": f"connect-{slug}",
            "kind": kind,
            "surface": str(item.get("surface") or "").strip()[:40],
            "title": title[:160],
            "detail": str(item.get("detail") or "").strip()[:400],
            "action_label": str(item.get("action_label") or "Do it").strip()[:60],
            "instruction": instruction[:600],
        })
    return out


# ---------------------------------------------------------------------------------------
# Importance + push (bell Inbox via add_nudge + Telegram), level + quiet-hours aware
# ---------------------------------------------------------------------------------------
#: Which kinds count as "important" enough to push at the 'calm' level (urgent-only).
_URGENT_KINDS = {"prep", "task", "budget"}


async def _push(obs: dict[str, Any], cfg: HimmyConfig, level: str) -> bool:
    """Push a new observation OUT (Telegram), honoring level + quiet hours.

    Returns True if a push was dispatched (so the caller can count it). The IN-APP notification
    centre (the bell) + the macOS notification + the badge read observations directly from
    ``/proactive`` — so this no longer mirrors them into the notification Inbox (which double-showed
    each one as both an actionable card and a plain notification). This now only handles the
    external Telegram push.

    - off / gentle: never push (items appear silently in the bell's "Himmy noticed" section).
    - calm: push ONLY urgent kinds (prep/task/budget), and never during quiet hours.
    - always: push everything, but still never during quiet hours.
    """
    if level in ("off", "gentle"):
        return False
    if level == "calm" and obs.get("kind") not in _URGENT_KINDS:
        return False
    if in_quiet_hours():
        return False

    title = obs.get("title") or "Himmy noticed something"
    body = obs.get("detail") or ""
    # Telegram (no-op if not linked).
    try:
        from himmy_app import telegram

        await telegram.push(f"Himmy noticed: {title}\n{body}".strip(), cfg)
    except Exception:  # noqa: BLE001 - a push must never break the scan
        pass
    return True


# ---------------------------------------------------------------------------------------
# notice() — the orchestrator: gather → rules + model → merge/dedupe/cap/store → push
# ---------------------------------------------------------------------------------------
async def notice(cfg: HimmyConfig | None = None, *, push: bool = True) -> dict[str, Any]:
    """One full proactive pass. Returns a small summary dict.

    1. Gather a permission-gated cross-surface snapshot.
    2. Deterministic rules → observations (no model).
    3. ONE cheap model pass over the snapshot → connect-the-dots observations (fail-open).
    4. Merge + dedupe by stable key (never re-create dismissed/snoozed/active), cap the ACTIVE
       set to ~6, store the new ones.
    5. Push NEW observations into the bell Inbox + Telegram, respecting proactive_level + quiet
       hours (skipped entirely when ``push`` is False, e.g. a manual list refresh).
    """
    cfg = cfg or load_config()
    level = get_level(cfg)
    store = get_store()
    summary: dict[str, Any] = {"ok": True, "created": 0, "pushed": 0, "level": level}

    if level == "off":
        summary["skipped"] = "level_off"
        return summary

    snap = await gather_signals(cfg)
    summary["errors"] = snap.get("errors", {})

    candidates = _deterministic_observations(snap)
    candidates += await _connect_observations(cfg, snap)

    created = 0
    pushed = 0
    seen_keys: set[str] = set()
    for obs in candidates:
        key = obs.get("key")
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        # Respect the active cap: stop creating NEW ones once we're at the ceiling.
        if store.active_count() >= MAX_ACTIVE:
            break
        if store.exists_key(key):
            continue  # already active / dismissed / snoozed → never re-create
        row = store.add(obs)
        if row is None:
            continue
        created += 1
        if push and await _push(row, cfg, level):
            pushed += 1

    summary["created"] = created
    summary["pushed"] = pushed
    summary["active"] = store.active_count()
    return summary


# ---------------------------------------------------------------------------------------
# execute() — run an observation's instruction through the HITL agent
# ---------------------------------------------------------------------------------------
async def execute(obs_id: str, cfg: HimmyConfig | None = None) -> dict[str, Any]:
    """Run an observation's ``instruction`` through :func:`himmy_app.cli.ask_turn`.

    Returns the ask_turn result (incl. ``awaiting_approval`` + ``checkpoint_id`` when a risky tool
    parked for approval) plus the observation. The observation is marked ``done`` only when the
    turn COMPLETED without parking (a parked turn stays active until the user approves/rejects via
    the normal approvals flow). Returns ``{ok: False}`` if the id is unknown.
    """
    cfg = cfg or load_config()
    store = get_store()
    obs = store.get(obs_id)
    if obs is None:
        return {"ok": False, "error": "not_found"}
    instruction = (obs.get("instruction") or "").strip()
    if not instruction:
        return {"ok": False, "error": "no_instruction", "observation": obs}

    from himmy_app.cli import ask_turn

    try:
        res = await ask_turn(instruction)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "observation": obs}

    if not res.get("awaiting_approval"):
        store.mark_done(obs_id)

    return {"ok": True, "result": res, "observation": store.get(obs_id) or obs}


# ---------------------------------------------------------------------------------------
# rundown() — morning / evening recap composed from the active observations (brief.py style)
# ---------------------------------------------------------------------------------------
async def rundown(cfg: HimmyConfig | None = None, *, part: str = "morning") -> dict[str, Any]:
    """Compose a short morning rundown or evening recap from the active observations.

    Reuses the brief.py pattern (one :func:`ask_turn` over the observation list, fail-open to a
    deterministic plain-text fallback so a missing model never yields an empty rundown). Pushes the
    text into the bell Inbox so it surfaces like the Morning Brief does.
    """
    cfg = cfg or load_config()
    store = get_store()
    obs = store.list_active(limit=MAX_ACTIVE)
    part = "evening" if str(part).lower().startswith("e") else "morning"

    bullets = [f"- {o['title']}: {o['detail']}".rstrip(": ") for o in obs]
    fallback = (
        ("Here's what I'm watching for you:\n" + "\n".join(bullets))
        if bullets
        else ("Nothing needs you right now — you're all clear."
              if part == "morning" else "Quiet day — nothing outstanding from me.")
    )

    text = fallback
    if obs:
        lead = (
            "Write a warm, brief MORNING rundown (2-4 short lines) of what needs the user today, "
            "based ONLY on these observations Himmy gathered. Lead with the most important. Don't "
            "invent anything not listed."
            if part == "morning"
            else "Write a warm, brief EVENING recap (2-4 short lines) of what's still open for the "
                 "user, based ONLY on these observations Himmy gathered. Keep it reassuring and "
                 "short. Don't invent anything not listed."
        )
        payload = json.dumps(
            [{"title": o["title"], "detail": o["detail"]} for o in obs], ensure_ascii=False
        )
        try:
            from himmy_app.cli import ask_turn

            res = await ask_turn(f"{lead}\n\nObservations:\n{payload}")
            reply = (res.get("reply") or "").strip()
            if reply:
                text = reply
        except Exception:  # noqa: BLE001 - fall back to the deterministic text
            pass

    # Surface it in the bell like the Morning Brief does (deduped per day + part).
    try:
        from himmy_app.routines import get_inbox

        day = _local_now().date().isoformat()
        label = "Morning rundown" if part == "morning" else "Evening recap"
        get_inbox().add_nudge(key=f"proactive-{part}-{day}", title=label, body=text)
    except Exception:  # noqa: BLE001
        pass

    return {"ok": True, "part": part, "text": text, "count": len(obs)}


__all__ = [
    "ObservationStore",
    "get_store",
    "gather_signals",
    "notice",
    "execute",
    "rundown",
    "get_level",
    "set_level",
    "in_quiet_hours",
    "PROACTIVE_LEVELS",
    "PROACTIVE_INTERVAL_S",
    "DEFAULT_LEVEL",
    "MAX_ACTIVE",
]
