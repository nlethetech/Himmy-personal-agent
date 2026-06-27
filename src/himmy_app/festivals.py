"""Nepali festival / public-holiday awareness — DYNAMIC, from an authoritative live feed.

Nepali festivals (Dashain, Tihar, Holi, Chhath, …) are lunar / tithi-based, so their Gregorian
date shifts every year and CANNOT be computed from a fixed Bikram-Sambat formula. Rather than
hand-maintain a table that silently goes stale, we read the dates from Google's public
**"Holidays in Nepal"** calendar — a maintained, lunar-correct iCalendar feed that needs no API
key and updates itself each year:

    https://calendar.google.com/calendar/ical/en.np%23holiday%40group.v.calendar.google.com/public/basic.ics

The feed is fetched through the guarded HTTP helper (:func:`himmy_app.connectors._net.safe_get_text`
— SSRF / allow-host / redirect / content-type / size / retry guards), parsed, and cached to a disk
snapshot so a transient outage degrades to slightly-stale dates instead of nothing.

Public API (sync, what callers need)::

    upcoming(within_days=14, today=None) -> list[dict]   # festivals in [today, today+within_days]

plus an async warmer the scheduler calls so the sync ``upcoming`` reads a fresh cache::

    await refresh(force=False) -> int                    # fetch + cache; returns event count

``upcoming`` reads ONLY the cached/snapshot feed and never extrapolates — if the cache is cold
and offline it returns ``[]`` (fail-open), so the worst case is "no festival nudge", never a wrong
date. The feed only publishes ~a year ahead, which is fine: nudges look just ~10-14 days out.
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path
from typing import Any, Optional

from himmy_app.connectors._net import (
    NetError,
    read_json_snapshot,
    safe_get_text,
    write_json_snapshot,
)

#: Google's public "Holidays in Nepal" iCalendar feed (keyless, maintained, lunar-correct).
_FEED_URL = (
    "https://calendar.google.com/calendar/ical/"
    "en.np%23holiday%40group.v.calendar.google.com/public/basic.ics"
)
#: Only Google's calendar host may answer — a redirect anywhere else is refused (SSRF guard).
_ALLOW_HOSTS = ("calendar.google.com",)
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Accept": "text/calendar"}
#: Festival dates don't change once published — a weekly refresh is plenty.
_TTL_S = 7 * 24 * 60 * 60

#: Parsed events live in-process once loaded; the disk snapshot survives restarts/outages.
_events: Optional[list[dict[str, str]]] = None

#: Short, friendly CONTEXT copy per festival family (descriptions only — NOT dates). Optional; a
#: nudge that recognises the family adds its own travel hook on top of this.
_NOTES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("dashain",), "Nepal's biggest festival — families travel home; buses and flights fill up."),
    (("tihar", "deepawali", "bhai tika", "laxmi puja"),
     "Festival of lights — the second big travel rush of the year."),
    (("chhath",), "Major across the Tarai — heavy travel toward Janakpur / Birgunj."),
    (("holi", "fagu"), "Festival of colours."),
    (("lhosar",), "Himalayan new year (Tamang / Sherpa / Gurung)."),
    (("nepali new year", "navavarsha", "new year"), "Bikram Sambat new year."),
)


def _snapshot_path() -> Path:
    """On-disk cache for the parsed holiday feed, under the app data dir (HIMMY_APP_DATA_DIR)."""
    data_dir = os.environ.get("HIMMY_APP_DATA_DIR")
    base = Path(data_dir).expanduser() if data_dir else Path(__file__).resolve().parents[2] / ".scholar-desk"
    return base / "connector_cache" / "nepal_holidays.json"


def _is_ics_ct(content_type: str | None) -> bool:
    """Strict content-type gate: a captcha/HTML body must be rejected before we try to parse it."""
    main = (content_type or "").split(";", 1)[0].strip().lower()
    return main in ("text/calendar", "text/plain")


def _unescape(value: str) -> str:
    """Unescape an iCalendar TEXT value (RFC 5545: ``\\,`` ``\\;`` ``\\n`` ``\\\\``)."""
    return (
        value.replace("\\n", " ").replace("\\N", " ")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    ).strip()


def _parse_ics(text: str) -> list[dict[str, str]]:
    """Parse VEVENTs from an iCalendar body into ``[{"date":"YYYY-MM-DD","name":...}]`` (date-sorted)."""
    # Unfold RFC-5545 folded lines (a CRLF/LF followed by a space or tab continues the line).
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n[ \t]", "", text)
    events: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for line in text.split("\n"):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur and cur.get("date") and cur.get("name"):
                events.append({"date": cur["date"], "name": cur["name"]})
            cur = None
        elif cur is not None and ":" in line:
            prop = line.split(":", 1)[0]
            if prop.startswith("DTSTART"):
                digits = re.sub(r"\D", "", line.split(":", 1)[1])[:8]
                if len(digits) == 8:
                    cur["date"] = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
            elif prop.startswith("SUMMARY"):
                cur["name"] = _unescape(line.split(":", 1)[1])
    # De-dup (date, name) and sort soonest-first.
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for e in sorted(events, key=lambda x: x["date"]):
        key = (e["date"], e["name"])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _normalise(rows: Any) -> list[dict[str, str]]:
    if not isinstance(rows, list):
        return []
    return [
        {"date": str(e["date"]), "name": str(e["name"])}
        for e in rows
        if isinstance(e, dict) and e.get("date") and e.get("name")
    ]


def _cached_events() -> list[dict[str, str]]:
    """The parsed feed from memory, else any on-disk snapshot (stale is fine for a read). Never raises."""
    global _events
    if _events is not None:
        return _events
    _events = _normalise(read_json_snapshot(_snapshot_path(), -1.0))  # -1 → ignore TTL on read
    return _events


async def refresh(*, force: bool = False) -> int:
    """Ensure the holiday feed is loaded + reasonably fresh; fetch + cache it if not. Never raises.

    A fresh (≤ :data:`_TTL_S`) snapshot is reused without a network call. Otherwise the feed is
    fetched through the guarded helper and re-snapshotted; if that fails we fall back to whatever
    snapshot exists (stale beats nothing). The scheduler awaits this before generating nudges.
    """
    global _events
    if not force:
        fresh = read_json_snapshot(_snapshot_path(), _TTL_S)
        if isinstance(fresh, list) and fresh:
            _events = _normalise(fresh)
            return len(_events)
    try:
        text = await safe_get_text(
            _FEED_URL, headers=_HEADERS, allow_hosts=_ALLOW_HOSTS, timeout=20.0, content_ok=_is_ics_ct
        )
        parsed = _parse_ics(text)
        if parsed:
            _events = parsed
            try:  # persist for next time; a write failure must not break the nudge
                write_json_snapshot(_snapshot_path(), parsed)
            except OSError:
                pass
            return len(parsed)
    except NetError:  # hiccup / captcha / redirect / oversize — fall back to a stale snapshot
        pass
    _events = _normalise(read_json_snapshot(_snapshot_path(), -1.0))
    return len(_events)


def _bs_year(d: datetime.date) -> int:
    """The Bikram-Sambat year for an AD date (best-effort; 0 if conversion is unavailable)."""
    try:
        from himmy.nepal.calendar import ad_to_bs

        return int(ad_to_bs(d).year)
    except Exception:  # noqa: BLE001 - bs_year is a nicety, never load-bearing
        return 0


def _note_for(name: str) -> str:
    low = name.lower()
    for keys, note in _NOTES:
        if any(k in low for k in keys):
            return note
    return ""


def upcoming(
    within_days: int = 14,
    today: Optional[datetime.date] = None,
) -> list[dict[str, Any]]:
    """Return Nepali festivals / public holidays in the next ``within_days`` days (soonest first).

    Reads ONLY the cached live feed (see :func:`refresh`) — it never extrapolates a date, so once
    the cache runs out it simply returns fewer/zero rows. Each row::

        {
          "name":      str,            # festival / holiday name (from the feed)
          "date_ad":   "YYYY-MM-DD",   # Gregorian date (ISO string)
          "days_away": int,            # whole days from `today` (0 == today)
          "note":      str,            # one-line context for a nudge ("" if none)
          "bs_year":   int,            # Bikram Sambat year (0 if unavailable)
        }

    Args:
        within_days: inclusive look-ahead window (``today`` itself counts as ``days_away == 0``).
        today: reference date; defaults to ``datetime.date.today()`` (a datetime's date is used).
    """
    if isinstance(today, datetime.datetime):
        today = today.date()
    if today is None:
        today = datetime.date.today()
    if within_days < 0:
        within_days = 0
    horizon = today + datetime.timedelta(days=within_days)

    out: list[dict[str, Any]] = []
    for e in _cached_events():
        try:
            d = datetime.date.fromisoformat(e["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if d < today or d > horizon:
            continue
        name = str(e.get("name") or "").strip()
        out.append(
            {
                "name": name,
                "date_ad": d.isoformat(),
                "days_away": (d - today).days,
                "note": _note_for(name),
                "bs_year": _bs_year(d),
            }
        )
    out.sort(key=lambda x: x["date_ad"])
    return out


def feed_horizon() -> Optional[datetime.date]:
    """The AD date of the furthest cached holiday, or ``None`` — how far the live feed currently reaches."""
    dates: list[datetime.date] = []
    for e in _cached_events():
        try:
            dates.append(datetime.date.fromisoformat(e["date"]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(dates) if dates else None


# Back-compat alias (older callers asked for the data horizon under this name).
table_horizon = feed_horizon
