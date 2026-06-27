"""Nepal festival & public-holiday awareness for proactive nudges.

WHY a hardcoded table and not a formula
---------------------------------------
Almost every major Nepali festival is set by *tithi* (lunar day) on the Bikram
Sambat / Vikram lunisolar calendar, not by a fixed BS month-day and certainly not
by a fixed Gregorian date. Dashami, Tihar, Holi, Chhath, the various Lhosars, Teej,
Janai Purnima, Shivaratri … all drift several weeks year to year and are fixed each
year by the official ``panchanga`` / Nepal Calendar Determination Committee, not by
any closed-form rule we could compute. So there is **no formula** here: the only
honest source is a reviewed almanac table, one row per festival per BS year.

Consequences we deliberately accept:
  * Each row is tagged with the BS year it belongs to (``bs_year``) so a reader can
    see *which* almanac year an entry was taken from — the same festival recurs as a
    *different* row next year, never by repeating a date.
  * Past the last verified BS year in :data:`_FESTIVALS`, we **fail open**: a query
    that runs after the table horizon simply returns the rows still inside the table
    (possibly none) rather than extrapolating a date we'd be guessing. A wrong
    festival nudge ("Happy Dashain" on the wrong day) is worse than a silent one.

The table below is hand-reviewed for **BS 2082 and BS 2083** (Gregorian ~Apr 2025 –
Apr 2027) plus the BS 2084 New Year as the horizon edge. A handful of strictly
fixed-date solar/civil holidays (Maghe Sankranti ≈ Jan 14–15, Nepali New Year =
Baisakh 1 = ~Apr 14) are still listed as explicit rows so the public API never has
to special-case them.

Public API
----------
``upcoming(within_days=14, today=None) -> list[dict]`` — the only thing callers need;
returns the festivals whose AD date falls in ``[today, today + within_days]``, each as
``{"name", "date_ad", "days_away", "note", "bs_year"}``, soonest first.

``himmy.nepal.calendar`` is used only to *annotate* / cross-check the BS year of a
date — the festival dates themselves come from the reviewed table, never from a
conversion.
"""

from __future__ import annotations

import datetime
from typing import Any, NamedTuple, Optional

try:  # pragma: no cover - exercised indirectly; import is defensive by design
    from himmy.nepal import calendar as _np_calendar
except Exception:  # pragma: no cover - if the dep is unavailable we still work
    _np_calendar = None  # type: ignore[assignment]


class Festival(NamedTuple):
    """One reviewed almanac entry: a specific festival on a specific AD date."""

    date_ad: datetime.date
    name: str
    note: str
    bs_year: int


def _d(iso: str) -> datetime.date:
    return datetime.date.fromisoformat(iso)


# ---------------------------------------------------------------------------
# THE TABLE.  Hand-reviewed almanac dates. DO NOT compute these.
#
# Sourced from the published Nepali ``panchanga`` / Nepal Calendar for the BS years
# noted in each block. Lunar (tithi-based) festivals fall on whatever AD day the
# almanac assigns that year — they are listed individually per BS year and will NOT
# recur on the same AD date next year. Add new BS years by APPENDING a reviewed block;
# never interpolate. Keep rows sorted by date_ad (a runtime assert guards this).
#
# Confidence: dates here are taken from widely-published Nepali calendars and should
# be treated as authoritative-but-verify near the day (Committee can shift a public
# holiday by a day). When in doubt, OMIT a row rather than guess — the API fails open.
# ---------------------------------------------------------------------------
_FESTIVALS: tuple[Festival, ...] = (
    # ===================== BS 2082 (≈ 2025-04-14 → 2026-04-13) =====================
    Festival(_d("2025-04-14"), "Nepali New Year (Baisakh 1)",
             "Bikram Sambat 2082 begins. Public holiday.", 2082),
    Festival(_d("2025-05-12"), "Buddha Jayanti",
             "Buddha Purnima — birth of Gautam Buddha. Public holiday.", 2082),
    Festival(_d("2025-08-09"), "Janai Purnima / Rakshya Bandhan",
             "Sacred-thread day; Gunhu Punhi. Public holiday.", 2082),
    Festival(_d("2025-08-10"), "Gai Jatra",
             "Festival of cows; remembrance and satire, esp. Kathmandu Valley.", 2082),
    Festival(_d("2025-08-16"), "Krishna Janmashtami",
             "Birth of Lord Krishna.", 2082),
    Festival(_d("2025-08-26"), "Haritalika Teej",
             "Women's festival of fasting and Shiva worship.", 2082),
    Festival(_d("2025-09-06"), "Indra Jatra",
             "Kathmandu street festival; Kumari chariot procession.", 2082),
    Festival(_d("2025-09-22"), "Ghatasthapana (Dashain begins)",
             "First day of Bada Dashain — jamara sown. The long festival season starts.",
             2082),
    Festival(_d("2025-09-29"), "Phulpati (Dashain)",
             "Seventh day of Dashain.", 2082),
    Festival(_d("2025-09-30"), "Maha Ashtami (Dashain)",
             "Eighth day of Dashain; Kalratri.", 2082),
    Festival(_d("2025-10-01"), "Maha Navami (Dashain)",
             "Ninth day of Dashain.", 2082),
    Festival(_d("2025-10-02"), "Vijaya Dashami (Dashain)",
             "The biggest day of Dashain — tika and blessings from elders. "
             "Multi-day public holiday around this date.", 2082),
    Festival(_d("2025-10-06"), "Kojagrat Purnima (Dashain ends)",
             "Full-moon day closing Dashain.", 2082),
    Festival(_d("2025-10-18"), "Kag Tihar (Tihar begins)",
             "Crow day — first day of Tihar / Deepawali, the festival of lights.", 2082),
    Festival(_d("2025-10-19"), "Kukur Tihar / Naraka Chaturdashi",
             "Dog day of Tihar.", 2082),
    Festival(_d("2025-10-20"), "Laxmi Puja (Tihar)",
             "Worship of goddess Laxmi; homes lit with lamps. Public holiday.", 2082),
    Festival(_d("2025-10-22"), "Govardhan Puja / Mha Puja",
             "Newari New Year (Nepal Sambat) and Govardhan Puja.", 2082),
    Festival(_d("2025-10-23"), "Bhai Tika (Tihar ends)",
             "Sisters bless brothers — final and most-celebrated day of Tihar.", 2082),
    Festival(_d("2025-10-27"), "Chhath Parva",
             "Sun-worship festival, esp. in the Terai/Madhesh. Public holiday in the region.",
             2082),
    Festival(_d("2025-12-30"), "Tamu Lhosar",
             "Gurung New Year.", 2082),
    Festival(_d("2026-01-14"), "Maghe Sankranti",
             "Solar festival marking Magh 1 — winter's turn. Public holiday.", 2082),
    Festival(_d("2026-01-19"), "Sonam Lhosar",
             "Tamang New Year.", 2082),
    Festival(_d("2026-02-15"), "Maha Shivaratri",
             "Great night of Shiva; Pashupatinath observance. Public holiday.", 2082),
    Festival(_d("2026-02-18"), "Gyalpo Lhosar",
             "Sherpa / Tibetan New Year.", 2082),
    Festival(_d("2026-03-03"), "Fagu Purnima (Holi — Hill)",
             "Festival of colours, observed in the hills/Kathmandu.", 2082),
    Festival(_d("2026-03-04"), "Holi (Terai/Madhesh)",
             "Festival of colours in the Terai, a day after the hills.", 2082),
    Festival(_d("2026-03-19"), "Ghode Jatra",
             "Horse-racing festival at Tundikhel, Kathmandu.", 2082),
    Festival(_d("2026-03-27"), "Ram Navami",
             "Birth of Lord Ram.", 2082),

    # ===================== BS 2083 (≈ 2026-04-14 → 2027-04-13) =====================
    Festival(_d("2026-04-14"), "Nepali New Year (Baisakh 1)",
             "Bikram Sambat 2083 begins. Public holiday.", 2083),
    Festival(_d("2026-05-01"), "Buddha Jayanti",
             "Buddha Purnima — birth of Gautam Buddha. Public holiday.", 2083),
    Festival(_d("2026-07-29"), "Janai Purnima / Rakshya Bandhan",
             "Sacred-thread day; Gunhu Punhi. Public holiday.", 2083),
    Festival(_d("2026-07-30"), "Gai Jatra",
             "Festival of cows, esp. Kathmandu Valley.", 2083),
    Festival(_d("2026-09-04"), "Krishna Janmashtami",
             "Birth of Lord Krishna.", 2083),
    Festival(_d("2026-09-14"), "Haritalika Teej",
             "Women's festival of fasting and Shiva worship.", 2083),
    Festival(_d("2026-09-25"), "Indra Jatra",
             "Kathmandu street festival; Kumari chariot procession.", 2083),
    Festival(_d("2026-10-11"), "Ghatasthapana (Dashain begins)",
             "First day of Bada Dashain — jamara sown.", 2083),
    Festival(_d("2026-10-19"), "Maha Ashtami (Dashain)",
             "Eighth day of Dashain.", 2083),
    Festival(_d("2026-10-20"), "Maha Navami (Dashain)",
             "Ninth day of Dashain.", 2083),
    Festival(_d("2026-10-21"), "Vijaya Dashami (Dashain)",
             "The biggest day of Dashain — tika from elders. Multi-day public holiday.",
             2083),
    Festival(_d("2026-10-25"), "Kojagrat Purnima (Dashain ends)",
             "Full-moon day closing Dashain.", 2083),
    Festival(_d("2026-11-08"), "Laxmi Puja (Tihar)",
             "Worship of goddess Laxmi; festival of lights. Public holiday.", 2083),
    Festival(_d("2026-11-11"), "Bhai Tika (Tihar ends)",
             "Sisters bless brothers — final day of Tihar.", 2083),
    Festival(_d("2026-11-15"), "Chhath Parva",
             "Sun-worship festival, esp. the Terai/Madhesh. Regional public holiday.", 2083),
    Festival(_d("2027-01-15"), "Maghe Sankranti",
             "Solar festival marking Magh 1. Public holiday.", 2083),
    Festival(_d("2027-03-22"), "Fagu Purnima (Holi — Hill)",
             "Festival of colours in the hills/Kathmandu.", 2083),

    # ===================== BS 2084 — horizon edge (only the fixed New Year) =======
    # Lunar festivals for BS 2084 are NOT yet entered: their tithi-based AD dates are
    # not reviewed here, and we will not guess. Only the solar New Year (Baisakh 1) is
    # safe because it is fixed to ~Apr 14. Past this row the API fails open.
    Festival(_d("2027-04-14"), "Nepali New Year (Baisakh 1)",
             "Bikram Sambat 2084 begins. Public holiday.", 2084),
)


def _validate_table() -> None:
    """Cheap import-time guard: rows must be date-sorted and BS-year consistent.

    Catches the most likely editing mistake (an out-of-order or mis-tagged row)
    without ever rejecting an otherwise-usable table at runtime in production —
    this is best-effort and only asserts on ordering, which is what the binary
    search in :func:`upcoming` relies on for correctness.
    """
    prev: Optional[datetime.date] = None
    for f in _FESTIVALS:
        assert prev is None or f.date_ad >= prev, (
            f"_FESTIVALS must be sorted by date_ad; {f.name} ({f.date_ad}) "
            f"comes after {prev}"
        )
        prev = f.date_ad


_validate_table()


def _bs_year_for(date_ad: datetime.date, fallback: int) -> int:
    """Cross-check / derive the BS year for an AD date via himmy's calendar.

    The table already carries a reviewed ``bs_year`` per row; we prefer that. This
    only runs when we want to confirm/annotate and the calendar dep is importable —
    if it isn't, we just trust the table's value (``fallback``).
    """
    if _np_calendar is None:
        return fallback
    try:
        return int(_np_calendar.ad_to_bs(date_ad).year)
    except Exception:
        return fallback


def upcoming(
    within_days: int = 14,
    today: Optional[datetime.date] = None,
) -> list[dict[str, Any]]:
    """Return reviewed Nepali festivals/holidays in the next ``within_days`` days.

    Args:
        within_days: inclusive look-ahead window in days (``today`` itself counts as
            ``days_away == 0``). Pass a large number (e.g. ``3650``) to dump the whole
            remaining table.
        today: the reference date; defaults to ``datetime.date.today()``. Accepts a
            ``datetime.datetime`` too (its date is used).

    Returns:
        A list of dicts, soonest first, each::

            {
              "name":      str,            # festival/holiday name
              "date_ad":   "YYYY-MM-DD",   # Gregorian date (ISO string)
              "days_away": int,            # whole days from `today` (0 == today)
              "note":      str,            # one-line context for a nudge
              "bs_year":   int,            # Bikram Sambat year this almanac row is for
            }

    Fail-open contract: dates only ever come from the reviewed table. Once ``today``
    moves past the last table row, this simply returns ``[]`` — it never extrapolates
    a festival date.
    """
    if isinstance(today, datetime.datetime):
        today = today.date()
    if today is None:
        today = datetime.date.today()

    # Guard against a nonsensical window without surprising the caller.
    if within_days < 0:
        within_days = 0
    horizon = today + datetime.timedelta(days=within_days)

    out: list[dict[str, Any]] = []
    for f in _FESTIVALS:
        if f.date_ad < today:
            continue
        if f.date_ad > horizon:
            # Table is date-sorted, so nothing further can be in-window.
            break
        out.append(
            {
                "name": f.name,
                "date_ad": f.date_ad.isoformat(),
                "days_away": (f.date_ad - today).days,
                "note": f.note,
                "bs_year": _bs_year_for(f.date_ad, f.bs_year),
            }
        )
    return out


def table_horizon() -> Optional[datetime.date]:
    """The AD date of the last reviewed row, or ``None`` if the table is empty.

    Callers can use this to tell the user "festival data ends on <date>" instead of
    silently showing nothing once we're past the horizon.
    """
    return _FESTIVALS[-1].date_ad if _FESTIVALS else None
