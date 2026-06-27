"""Keyless weather forecasts for ``do_concierge`` trips (Open-Meteo).

This module is the one honest weather surface used by the Trips/concierge feature. It talks to
`Open-Meteo <https://open-meteo.com>`_, which is **keyless** and free, and it goes through the
same guarded HTTP helper every connector uses (:func:`himmy_app.connectors._net.safe_get_json`)
so it inherits the project's SSRF / redirect / content-type / size / retry defences. The host is
pinned to ``api.open-meteo.com`` and nothing else.

Two design choices keep this *honest* — the product promise is that we never invent a forecast:

* **A real forecast only within the horizon.** Open-Meteo's daily model reaches ~16 days out. If
  the dates the user asked about fall beyond that horizon we set ``in_forecast_window=False`` and
  lead the one-line ``summary`` with the *season* (a deterministic, month-derived Nepal weather
  pattern) instead of a fabricated daily forecast.
* **Graceful degradation.** Any upstream failure, bad geocode, or out-of-range coordinate returns a
  well-formed ``{"ok": False, ...}`` dict (still carrying the seasonal pattern, which needs no
  network) rather than raising — so a missing forecast can never break a trip plan.

The public surface is a single coroutine, :func:`forecast`.
"""

from __future__ import annotations

import math
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any

from himmy_app.connectors._net import safe_get_json

__all__ = ["forecast", "wmo_to_label_emoji", "nepal_season"]

# Pin to Open-Meteo's forecast host. Keyless; the allow-list is the real enforcing control.
_HOST = "api.open-meteo.com"
_URL = "https://api.open-meteo.com/v1/forecast"
# Open-Meteo's daily model reaches ~16 days; we request the full horizon so window checks are honest.
_FORECAST_DAYS = 16


# ---------------------------------------------------------------------------
# WMO weather-code -> (label, emoji)
# ---------------------------------------------------------------------------
#
# Open-Meteo reports WMO 4677 present-weather codes. We collapse them into the handful of buckets
# the contract asks for: clear / cloud / fog / rain / snow / showers / thunderstorm.
_WMO_BUCKETS: tuple[tuple[frozenset[int], str, str], ...] = (
    (frozenset({0}), "Clear", "☀️"),                       # ☀️
    (frozenset({1, 2, 3}), "Cloudy", "⛅"),                      # ⛅
    (frozenset({45, 48}), "Fog", "\U0001f32b️"),               # 🌫️
    (frozenset(range(51, 68)), "Rain", "\U0001f327️"),         # 🌧️ (51-67: drizzle/rain/freezing)
    (frozenset(range(71, 78)), "Snow", "❄️"),             # ❄️ (71-77)
    (frozenset({80, 81, 82}), "Showers", "\U0001f326️"),       # 🌦️
    (frozenset({95, 96, 97, 98, 99}), "Thunderstorm", "⛈️"),  # ⛈️
)


def wmo_to_label_emoji(code: int | None) -> tuple[str, str]:
    """Map a WMO weather code to a short ``(label, emoji)`` pair.

    Unknown / missing codes fall back to a neutral ``("Unknown", "🌡️")`` so callers never crash on
    a code Open-Meteo adds in future.
    """
    try:
        c = int(code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ("Unknown", "\U0001f321️")  # 🌡️
    for codes, label, emoji in _WMO_BUCKETS:
        if c in codes:
            return (label, emoji)
    return ("Unknown", "\U0001f321️")  # 🌡️


# ---------------------------------------------------------------------------
# Month -> Nepal seasonal pattern
# ---------------------------------------------------------------------------
#
# Deterministic, no-network fallback. Nepal's lowland/hill climate runs on the South Asian monsoon
# calendar: dry cool winter, hot pre-monsoon, the Jun-Sep monsoon, then a clear post-monsoon.
_SEASONS: dict[int, str] = {
    1: "Winter (Dec-Feb): cold, dry and often clear; chilly mornings",
    2: "Winter (Dec-Feb): cold, dry and often clear; chilly mornings",
    3: "Pre-monsoon (Mar-May): warming up, hazy, occasional afternoon thunderstorms",
    4: "Pre-monsoon (Mar-May): warming up, hazy, occasional afternoon thunderstorms",
    5: "Pre-monsoon (Mar-May): hot and humid, building afternoon thunderstorms",
    6: "Monsoon (Jun-Sep): afternoon showers likely; humid, lush, occasional heavy rain",
    7: "Monsoon (Jun-Sep): afternoon showers likely; humid, lush, occasional heavy rain",
    8: "Monsoon (Jun-Sep): afternoon showers likely; humid, lush, occasional heavy rain",
    9: "Monsoon (Jun-Sep): afternoon showers likely; humid, lush, occasional heavy rain",
    10: "Post-monsoon (Oct-Nov): clear skies, mild and dry; the prime trekking window",
    11: "Post-monsoon (Oct-Nov): clear skies, mild and dry; the prime trekking window",
    12: "Winter (Dec-Feb): cold, dry and often clear; chilly mornings",
}


def nepal_season(month: int) -> str:
    """Return the Nepal seasonal weather pattern for a 1-12 ``month`` (clamped, never raises)."""
    try:
        m = int(month)
    except (TypeError, ValueError):
        m = datetime.now().month
    if m < 1 or m > 12:
        m = ((m - 1) % 12) + 1
    return _SEASONS[m]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _finite_in_range(value: Any, lo: float, hi: float) -> float | None:
    """Coerce ``value`` to a finite float within ``[lo, hi]``; return None if it isn't."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    if f < lo or f > hi:
        return None
    return f


def _parse_iso_date(value: Any) -> _date | None:
    """Parse a ``YYYY-MM-DD`` string (or a date/datetime) into a date; None on anything else."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce to float, treating None/missing/NaN as ``default`` (Open-Meteo uses null for gaps)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _int(value: Any, default: int = 0) -> int:
    """Coerce to a rounded int, with a safe default for null/missing."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return int(round(f))


def _empty(season: str, summary: str, *, in_window: bool = False) -> dict[str, Any]:
    """A well-formed failure/degraded result (no daily data, but still honest about the season)."""
    return {
        "ok": False,
        "current": None,
        "daily": [],
        "in_forecast_window": in_window,
        "season": season,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def forecast(
    lat: float,
    lon: float,
    *,
    start: str | _date | None = None,
    end: str | _date | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Return an honest weather forecast for ``(lat, lon)`` via keyless Open-Meteo.

    Args:
        lat: Latitude, finite, ``-90..90``.
        lon: Longitude, finite, ``-180..180``.
        start: Optional first date of interest (``YYYY-MM-DD`` or a date). Defaults to today.
        end: Optional last date of interest. Defaults to ``start + days - 1``.
        days: Window length in days when ``end`` is not given (clamped to 1..16). Defaults to 7.

    Returns:
        A dict matching the shared contract::

            {
              "ok": bool,
              "current": {temp_c, code, label, emoji, humidity, wind_kmh} | None,
              "daily": [ {date, code, label, emoji, t_max, t_min, rain_pct, rain_mm} ],
              "in_forecast_window": bool,
              "season": str,
              "summary": str,
            }

        ``daily`` only contains days within ``[start, end]`` (or the next ``days``). If the requested
        dates lie beyond Open-Meteo's ~16-day horizon, ``in_forecast_window`` is False and ``summary``
        leads with the season rather than a fabricated daily forecast. Never raises — any failure
        degrades to ``{"ok": False, ...}`` while still reporting the seasonal pattern.
    """
    # --- date window (needed even on a network failure, for the season line) -------------------
    today = _date.today()
    start_d = _parse_iso_date(start) or today
    end_d = _parse_iso_date(end)
    try:
        n = int(days)
    except (TypeError, ValueError):
        n = 7
    n = max(1, min(_FORECAST_DAYS, n))
    if end_d is None:
        end_d = start_d + timedelta(days=n - 1)
    if end_d < start_d:
        start_d, end_d = end_d, start_d

    # Season is derived from the START month (when the trip begins) and never needs the network.
    season = nepal_season(start_d.month)

    # Is the requested window reachable by the ~16-day daily model? The horizon starts *today*.
    horizon_last = today + timedelta(days=_FORECAST_DAYS - 1)
    in_window = (end_d >= today) and (start_d <= horizon_last)

    # --- validate coordinates ------------------------------------------------------------------
    vlat = _finite_in_range(lat, -90.0, 90.0)
    vlon = _finite_in_range(lon, -180.0, 180.0)
    if vlat is None or vlon is None:
        return _empty(
            season,
            f"Coordinates unavailable; expect {season.split(':', 1)[0].strip()} conditions.",
        )

    # --- fetch ---------------------------------------------------------------------------------
    params: dict[str, Any] = {
        "latitude": vlat,
        "longitude": vlon,
        "timezone": "auto",
        "forecast_days": _FORECAST_DAYS,
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,precipitation_sum"
        ),
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
    }
    try:
        data = await safe_get_json(
            _URL,
            params=params,
            allow_hosts=(_HOST,),
            timeout=8.0,
            max_bytes=1_000_000,
            retries=1,
        )
    except Exception:  # noqa: BLE001 — NetError + any transport error: weather must never break a trip plan
        # Degrade gracefully: no live data, but still honest about the season.
        summary = (
            f"{season} (live forecast unavailable right now)."
            if in_window
            else f"{season} Live forecast not available for these dates."
        )
        return _empty(season, summary, in_window=False)

    if not isinstance(data, dict):
        return _empty(season, f"{season} (no forecast data).", in_window=False)

    # --- current conditions --------------------------------------------------------------------
    current: dict[str, Any] | None = None
    cur = data.get("current")
    if isinstance(cur, dict):
        code = _int(cur.get("weather_code"))
        label, emoji = wmo_to_label_emoji(code)
        current = {
            "temp_c": round(_num(cur.get("temperature_2m")), 1),
            "code": code,
            "label": label,
            "emoji": emoji,
            "humidity": _int(cur.get("relative_humidity_2m")),
            "wind_kmh": round(_num(cur.get("wind_speed_10m")), 1),
        }

    # --- daily series, filtered to the requested window ----------------------------------------
    daily_out: list[dict[str, Any]] = []
    daily = data.get("daily")
    if isinstance(daily, dict):
        dates = daily.get("time") or []
        codes = daily.get("weather_code") or []
        tmax = daily.get("temperature_2m_max") or []
        tmin = daily.get("temperature_2m_min") or []
        pprob = daily.get("precipitation_probability_max") or []
        psum = daily.get("precipitation_sum") or []

        def _at(seq: Any, i: int) -> Any:
            return seq[i] if isinstance(seq, list) and i < len(seq) else None

        for i, dstr in enumerate(dates):
            d = _parse_iso_date(dstr)
            if d is None or d < start_d or d > end_d:
                continue
            code = _int(_at(codes, i))
            label, emoji = wmo_to_label_emoji(code)
            daily_out.append(
                {
                    "date": d.isoformat(),
                    "code": code,
                    "label": label,
                    "emoji": emoji,
                    "t_max": round(_num(_at(tmax, i)), 1),
                    "t_min": round(_num(_at(tmin, i)), 1),
                    "rain_pct": max(0, min(100, _int(_at(pprob, i)))),
                    "rain_mm": round(_num(_at(psum, i)), 1),
                }
            )

    # If we asked for dates inside the horizon but got nothing back, the window is effectively out.
    has_daily = bool(daily_out)
    in_window_final = in_window and has_daily

    # --- one honest summary line ---------------------------------------------------------------
    summary = _build_summary(season, daily_out, current, in_window_final)

    return {
        "ok": True,
        "current": current,
        "daily": daily_out,
        "in_forecast_window": in_window_final,
        "season": season,
        "summary": summary,
    }


def _build_summary(
    season: str,
    daily: list[dict[str, Any]],
    current: dict[str, Any] | None,
    in_window: bool,
) -> str:
    """One honest line: the real daily forecast when in-window, else the seasonal pattern."""
    if not in_window or not daily:
        # Out of horizon (or no daily data): lead with the season, never a fabricated forecast.
        return f"{season} Forecast available closer to your dates."

    # In-window: summarise the real series we actually have.
    highs = [d["t_max"] for d in daily]
    lows = [d["t_min"] for d in daily]
    hi = max(highs)
    lo = min(lows)
    wet_days = [d for d in daily if d["rain_pct"] >= 50 or d["rain_mm"] >= 1.0]
    n = len(daily)

    if len(wet_days) == 0:
        rain_phrase = "mostly dry"
    elif len(wet_days) == n:
        rain_phrase = "wet most days — pack a rain layer"
    else:
        rain_phrase = f"rain on {len(wet_days)} of {n} day{'s' if n != 1 else ''}"

    span = "the day" if n == 1 else f"the next {n} days"
    return f"Forecast for {span}: highs {hi:.0f}°C / lows {lo:.0f}°C, {rain_phrase}."
