"""Smart Nudges — gentle, deterministic, deduped proactive notifications.

The idea: surface the few things that genuinely need attention — a task due/overdue, an
upcoming calendar event or trip, a human email gone unreplied — in the SAME notifications bell
the app already has, without bothering the user. They are generated MOSTLY DETERMINISTICALLY
(no LLM call — fast, free, robust) so the scheduler can run them every few hours and a manual
``POST /nudges/run`` is a safe test trigger.

WHY this lives in a plain async function (and the lifespan loop), not a himmy Routine:
nudges are deterministic and must NOT run through ``ask_turn`` (no model budget, no chat
history, no HITL park) — they are a system feed, not a saved automation.

REUSE, never re-implement, the existing data access:
  - Tasks via :func:`himmy.api.studio_tasks.get_tasks_store` (exactly how server._tasks_store
    and planner.py read them);
  - Calendar via :mod:`himmy.api.studio_google` (``status`` + ``calendar_range``), the same
    calls /calendar/range makes;
  - Mail via the same ``gmail_list`` the Mail tab + digest use, with the SAME muted/automated
    filters so we only nudge on real, human, attention-worthy mail.

PERMISSIONS: every run checks ``perms.level_of(<surface>)`` and skips a category whose surface
is ``off`` (mirroring how gate_tools would deny the read tool) — so a nudge for a denied
surface never fires, and toggling a surface OFF stops it on the next pass with no restart.

DEDUP: each nudge has a stable key (see below); :meth:`Inbox.add_nudge` no-ops if the key is
already present, so running :func:`generate` twice in the same window adds nothing new.
"""

from __future__ import annotations

import datetime
import email.utils
import os
from typing import Any

from himmy_app import permissions as perms
from himmy_app.config import HimmyConfig, load_config
from himmy_app.routines import get_inbox

#: How often the background loop regenerates nudges (seconds). ~3h, env-overridable.
NUDGE_INTERVAL_S = float(os.environ.get("HIMMY_NUDGE_INTERVAL") or 3 * 3600)
#: A message must be unread AND at least this old to count as "gone unreplied".
UNREPLIED_DAYS = 3
#: How far ahead we look for calendar events/trips.
CAL_HORIZON_DAYS = 2
#: How far ahead we look for a major festival worth a heads-up (the example says ~10 days).
FESTIVAL_HORIZON_DAYS = 10
#: Caps so a noisy inbox/calendar can't flood the bell in one pass.
MAX_MAIL_NUDGES = 5
MAX_CAL_NUDGES = 10
#: A nudge per *every* almanac row would be noisy (Dashain alone has ~6 sub-day rows). We only
#: nudge on the headline festivals people actually plan around, and we collapse each festival's
#: sub-days + aliases into one FAMILY. Each entry maps a tuple of case-insensitive substrings of
#: the festival name (from :func:`himmy_app.festivals.upcoming`) to a stable family slug used in
#: the dedup key. Order matters: first match wins (so "fagu" → holi before any later entry).
_FESTIVAL_FAMILIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("dashain",), "dashain"),
    (("tihar", "deepawali", "bhai tika", "laxmi puja"), "tihar"),
    (("chhath",), "chhath"),
    (("holi", "fagu"), "holi"),
    (("nepali new year",), "nepali-new-year"),
    (("lhosar",), "lhosar"),
    (("teej",), "teej"),
    (("buddha jayanti",), "buddha-jayanti"),
    (("shivaratri",), "shivaratri"),
    (("janai purnima",), "janai-purnima"),
)
#: For "everyone travels home" festivals we add a concierge travel hook (the Dashain example).
_TRAVEL_FESTIVALS = ("dashain", "tihar", "deepawali", "nepali new year")

#: Light keyword check to phrase a trip/flight nudge differently from a normal event.
_TRIP_WORDS = (
    "flight", "airport", "boarding", "departure", "buddha air", "yeti airlines",
    "nepal airlines", "qatar", "emirates", "indigo", "airways", "airlines",
)


def _today() -> datetime.date:
    return datetime.date.today()


def _rfc3339_z(dt: datetime.datetime) -> str:
    """An RFC3339 UTC timestamp with a trailing Z (the form calendar_range expects)."""
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def generate(cfg: HimmyConfig | None = None) -> dict[str, Any]:
    """The single entry point: gather the three categories and write deduped nudge rows.

    Each category runs under its OWN try/except so one failing source never blocks the others
    (the same defensive style as brief.py / the news loop). Returns a small summary dict.
    """
    cfg = cfg or load_config()
    inbox = get_inbox()
    now = datetime.datetime.now(datetime.timezone.utc)
    today = _today()
    created = 0
    checked: dict[str, Any] = {}

    try:
        # Warm the live holiday feed (cached, ≤7-day TTL) so the sync upcoming() below sees
        # fresh dates; refresh() never raises and falls back to a snapshot when offline.
        from himmy_app import festivals

        await festivals.refresh()
        created += _festival_nudges(inbox, today, checked)
    except Exception as exc:  # noqa: BLE001 - one bad source never blocks the others
        checked["festivals_error"] = f"{type(exc).__name__}: {exc}"
    try:
        created += _task_nudges(cfg, inbox, today, checked)
    except Exception as exc:  # noqa: BLE001 - one bad source never blocks the others
        checked["tasks_error"] = f"{type(exc).__name__}: {exc}"
    try:
        created += await _calendar_nudges(cfg, inbox, now, checked)
    except Exception as exc:  # noqa: BLE001
        checked["calendar_error"] = f"{type(exc).__name__}: {exc}"
    try:
        created += await _mail_nudges(cfg, inbox, now, checked)
    except Exception as exc:  # noqa: BLE001
        checked["mail_error"] = f"{type(exc).__name__}: {exc}"

    return {"ok": True, "created": created, "checked": checked}


# ---------------------------------------------------------------------------------------
# Festivals — the first proactive *concierge* moment. A heads-up when a major Nepali
# festival is ~10 days out, with a travel hook for the "everyone goes home" ones. Reuses
# the reviewed almanac table in himmy_app.festivals; fully deterministic, no model.
# ---------------------------------------------------------------------------------------
def _festival_nudges(inbox: Any, today: datetime.date, checked: dict[str, Any]) -> int:
    from himmy_app.festivals import upcoming

    # A festival like Dashain spans several almanac rows (Ghatasthapana, Ashtami, Navami,
    # Dashami …). Collapse each FAMILY to its SOONEST in-window row so the bell shows one
    # "Dashain in N days", not three. First-seen wins because upcoming() is date-sorted.
    families: dict[str, dict[str, Any]] = {}
    for fest in upcoming(within_days=FESTIVAL_HORIZON_DAYS, today=today):
        name = str(fest.get("name") or "").strip()
        fam = _festival_family(name)
        if fam is None or fam in families:
            continue
        families[fam] = fest

    created = 0
    for fam, fest in families.items():
        name = str(fest.get("name") or "").strip()
        days = int(fest.get("days_away") or 0)
        # Key on the FAMILY + AD year only (not the sub-day date, not days_away) so neither the
        # countdown nor which sub-row is soonest can re-fire it — SELECT-before-INSERT dedup
        # then guarantees exactly one nudge per festival per year.
        key = f"festival-{str(fest.get('date_ad') or '')[:4]}-{fam}"
        title, body = _festival_copy(name, days, str(fest.get("note") or ""))
        if inbox.add_nudge(key=key, title=title, body=body) is not None:
            created += 1
    checked["festivals"] = len(families)
    return created


def _festival_family(name: str) -> str | None:
    """Map an almanac row to a stable major-festival FAMILY slug, or None if not major.

    Several aliases collapse to one family (Deepawali→tihar, Fagu→holi) so all of a
    festival's sub-days share a single dedup key and a single nudge.
    """
    low = name.lower()
    for keys, fam in _FESTIVAL_FAMILIES:
        if any(k in low for k in keys):
            return fam
    return None


def _festival_copy(name: str, days: int, note: str) -> tuple[str, str]:
    """Short, friendly copy. The 'going home' festivals get a concierge travel hook."""
    short = _festival_short_name(name)
    when = "today" if days <= 0 else ("tomorrow" if days == 1 else f"in {days} days")
    low = name.lower()
    if any(k in low for k in _TRAVEL_FESTIVALS):
        # e.g. "Dashain's in 9 days — buses sell out fast. Want me to check
        # Kathmandu->Pokhara fares?"
        title = f"{short} {when}"
        body = (
            f"{short}'s {when} — buses sell out fast. "
            "Want me to check Kathmandu->Pokhara fares?"
        )
        return title, body
    title = f"{short} {when}"
    body = f"{short} is {when}." + (f" {note}" if note else "")
    return title, body


def _festival_short_name(name: str) -> str:
    """A friendly headline from a full almanac name, e.g.

    'Ghatasthapana (Dashain begins)' -> 'Dashain'; 'Fagu Purnima (Holi — Hill)' -> 'Holi'.
    Prefer the parenthetical festival-family word when present, else the bare name.
    """
    low = name.lower()
    for fam, label in (
        ("dashain", "Dashain"), ("tihar", "Tihar"), ("deepawali", "Tihar"),
        ("chhath", "Chhath"), ("holi", "Holi"), ("fagu", "Holi"),
        ("nepali new year", "Nepali New Year"), ("lhosar", name.split(" Lhosar")[0] + " Lhosar"
                                                 if "lhosar" in low else "Lhosar"),
        ("teej", "Teej"), ("buddha jayanti", "Buddha Jayanti"),
        ("shivaratri", "Maha Shivaratri"), ("janai purnima", "Janai Purnima"),
    ):
        if fam in low:
            return label
    return name


# ---------------------------------------------------------------------------------------
# Tasks — due tomorrow / due today / overdue. Deterministic, no model.
# ---------------------------------------------------------------------------------------
def _task_nudges(cfg: HimmyConfig, inbox: Any, today: datetime.date, checked: dict[str, Any]) -> int:
    if perms.level_of("tasks", cfg) == "off":
        checked["tasks"] = "off"
        return 0
    from himmy.api.studio_tasks import get_tasks_store

    created = 0
    n_seen = 0
    for t in get_tasks_store().list():
        if t.done or not t.due:
            continue
        try:
            due = datetime.date.fromisoformat(str(t.due)[:10])
        except Exception:  # noqa: BLE001 - skip an un-parseable/blank due rather than throwing
            continue
        n_seen += 1
        title = (t.title or "").strip() or "Untitled task"
        if due <= today:
            # Overdue (incl. due today is handled below). Re-nudge daily while it stays overdue.
            when = "today" if due == today else due.isoformat()
            key = (
                f"task-due-{t.id}-{due.isoformat()}"
                if due == today
                else f"task-overdue-{t.id}-{today.isoformat()}"
            )
            label = "Task due today" if due == today else "Task overdue"
            body = (
                f"'{title}' is due today." if due == today
                else f"'{title}' was due {when} and isn't done yet."
            )
            if inbox.add_nudge(key=key, title=f"{label}: {title}", body=body) is not None:
                created += 1
        elif due == today + datetime.timedelta(days=1):
            key = f"task-due-{t.id}-{due.isoformat()}"
            if inbox.add_nudge(
                key=key,
                title=f"Task due tomorrow: {title}",
                body=f"'{title}' is due tomorrow ({due.isoformat()}).",
            ) is not None:
                created += 1
    checked["tasks"] = n_seen
    return created


# ---------------------------------------------------------------------------------------
# Calendar — events / trips in the next couple of days. Reuses studio_google.calendar_range.
# ---------------------------------------------------------------------------------------
async def _calendar_nudges(cfg: HimmyConfig, inbox: Any, now: datetime.datetime, checked: dict[str, Any]) -> int:
    if perms.level_of("calendar", cfg) == "off":
        checked["calendar"] = "off"
        return 0
    from himmy.api import studio_google as g

    if not g.status().connected:
        checked["calendar"] = "not_connected"
        return 0
    time_min = _rfc3339_z(now)
    time_max = _rfc3339_z(now + datetime.timedelta(days=CAL_HORIZON_DAYS))
    events = await g.calendar_range(time_min, time_max, 250)
    created = 0
    for e in events[:MAX_CAL_NUDGES]:
        start = (e.start or "").strip()
        if not start:
            continue
        try:
            event_date = start[:10]  # YYYY-MM-DD for both all-day and timed events
            datetime.date.fromisoformat(event_date)  # validate, else skip silently
        except Exception:  # noqa: BLE001
            continue
        title, body = _calendar_copy(e, start, event_date)
        key = f"cal-{e.id}-{event_date}"
        if inbox.add_nudge(key=key, title=title, body=body) is not None:
            created += 1
    checked["calendar"] = len(events)
    return created


def _calendar_copy(e: Any, start: str, event_date: str) -> tuple[str, str]:
    """Short copy for a calendar nudge — trip/flight phrasing when it looks like travel."""
    summary = (getattr(e, "summary", "") or "Event").strip() or "Event"
    location = (getattr(e, "location", "") or "").strip()
    blob = f"{summary} {location}".lower()
    today = _today()
    try:
        d = datetime.date.fromisoformat(event_date)
        days = (d - today).days
    except Exception:  # noqa: BLE001
        days = 0
    when = "today" if days <= 0 else ("tomorrow" if days == 1 else f"in {days} days")

    if any(w in blob for w in _TRIP_WORDS):
        dest = location or summary
        return f"Trip {when}: {summary}", f"{summary} ({dest}) — {when}."

    # Timed event → include the clock time; all-day → just the day.
    time_str = _local_time_str(start)
    if time_str:
        return f"{when.capitalize()} {time_str}: {summary}", f"{summary} at {time_str}, {when}."
    return f"{when.capitalize()}: {summary}", f"{summary} — {when} (all day)."


def _local_time_str(start: str) -> str:
    """A friendly clock time from an RFC3339 dateTime, or '' for an all-day date."""
    if len(start) <= 10:  # 'YYYY-MM-DD' all-day
        return ""
    try:
        dt = datetime.datetime.fromisoformat(start)
    except Exception:  # noqa: BLE001
        return ""
    # %-I isn't portable to every libc; strip a leading zero by hand.
    return dt.strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------------------------------
# Mail — human messages gone unreplied (unread + a few days old). Same filters as the Mail tab.
# ---------------------------------------------------------------------------------------
async def _mail_nudges(cfg: HimmyConfig, inbox: Any, now: datetime.datetime, checked: dict[str, Any]) -> int:
    if perms.level_of("mail", cfg) == "off":
        checked["mail"] = "off"
        return 0
    from himmy.api import studio_google as g

    if not g.status().connected:
        checked["mail"] = "not_connected"
        return 0
    # Same public helpers the Mail tab + digest use, so nudges match what the inbox shows.
    from himmy_app.server import _normalize_sender, is_automated, load_mail_rules

    msgs = await g.gmail_list(50)
    muted = set(load_mail_rules(cfg)["muted"])
    created = 0
    for m in msgs:
        if created >= MAX_MAIL_NUDGES:
            break
        if not getattr(m, "unread", False):
            continue
        if is_automated(m.sender):
            continue
        if _normalize_sender(m.sender) in muted:
            continue
        age = _mail_age_days(m.date, now)
        if age is None or age < UNREPLIED_DAYS:
            continue
        subject = (m.subject or "").strip() or "(no subject)"
        sender = _sender_name(m.sender)
        key = f"mail-unreplied-{m.id}"
        title = f"Unreplied {age} days: {subject}"
        body = f"'{subject}' from {sender} has been unread for {age} days."
        if inbox.add_nudge(key=key, title=title, body=body) is not None:
            created += 1
    checked["mail"] = len(msgs)
    return created


def _mail_age_days(raw_date: str, now: datetime.datetime) -> int | None:
    """Age in whole days of a Gmail ``Date`` header, or None if it can't be parsed."""
    if not raw_date:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw_date)
    except Exception:  # noqa: BLE001 - a malformed Date header just skips this message
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return max(0, (now - dt).days)


def _sender_name(sender: str) -> str:
    """The display name from a From header, falling back to the bare address."""
    name, addr = email.utils.parseaddr(sender or "")
    return (name or addr or sender or "someone").strip()


# ---------------------------------------------------------------------------------------
# Listing helper — the /nudges endpoint reads the same inbox the bell does.
# ---------------------------------------------------------------------------------------
def list_nudges(limit: int = 50) -> list[dict[str, Any]]:
    """The current nudge rows (kind=='nudge') from the shared inbox, newest first."""
    return get_inbox().list_by_kind("nudge", limit=limit)


__all__ = ["generate", "list_nudges", "NUDGE_INTERVAL_S"]
