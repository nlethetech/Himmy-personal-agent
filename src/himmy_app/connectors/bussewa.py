"""Bussewa (Nepal BUS tickets) search — live trips + a booking deep-link.

bussewa.com.np exposes a clean public JSON API that its own site uses (no key, no auth):

    GET /bus/route                    -> ["Kathmandu","Pokhara","Sauraha",...]  (the city list)
    GET /bus/trips?from=&to=&date=BS  -> [ {busName, busType, departureTime, ticketPrice,
                                            availableSeat, amenitiesList, tripIdHash, ...}, ... ]
    GET /bus/trip-detail/{tripIdHash} -> boarding/dropping points, photos, reviews, policies
    GET /bus/seat-layout/{tripIdHash} -> live seat map

Two quirks, both handled here: the ``date`` is **Bikram Sambat** (e.g. ``2083-03-17``), and the
cities are plain names. Booking needs a login + payment, so — exactly like ``buddha_air`` — we
read the live trips and hand the user a working booking deep-link to bussewa's own results page
to finish ("find the offer, here's the link").
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors._net import (
    NetError,
    read_json_snapshot,
    safe_get_json,
    write_json_snapshot,
)
from himmy_app.connectors._register import safe_register_local_tool

_BASE = "https://bussewa.com.np"
#: Only bussewa itself may answer our requests — a redirect anywhere else is refused (SSRF guard).
_ALLOW_HOSTS = ("bussewa.com.np",)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
    "Referer": f"{_BASE}/",
}
_cities_cache: list[str] = []
#: Cities change rarely; persist the list to disk so a transient outage / captcha at request
#: time degrades to slightly-stale resolution data instead of losing it entirely.
_CITIES_TTL_S = 24 * 60 * 60


def _cities_snapshot_path() -> Path:
    """On-disk cache file for the bussewa city list, under the app data dir.

    Mirrors ``config.DEFAULT_DATA_DIR`` resolution (HIMMY_APP_DATA_DIR → ``.scholar-desk``)
    without importing the full config, keeping this connector self-contained.
    """
    data_dir = os.environ.get("HIMMY_APP_DATA_DIR")
    base = Path(data_dir).expanduser() if data_dir else Path(__file__).resolve().parents[3] / ".scholar-desk"
    return base / "connector_cache" / "bussewa_cities.json"

#: Common spellings / nearby hubs → a city bussewa actually lists (deterministic, no model).
_ALIASES = {
    "KTM": "Kathmandu", "KATH": "Kathmandu", "KATHMANDU": "Kathmandu",
    "PKR": "Pokhara", "POKHRA": "Pokhara",
    "BHAIRAWA": "Bhairahawa", "BHAIRHAWA": "Bhairahawa", "SIDDHARTHANAGAR": "Bhairahawa",
    "NARAYANGADH": "Bharatpur", "NARAYANGARH": "Bharatpur",
    "NEPALJUNG": "Nepalgunj", "NEPALGANJ": "Nepalgunj",
    "DHANGADI": "Dhangadhi",
    "CHITWAN": "Sauraha",  # the tourist hub most riders mean by "Chitwan"
    "TANSEN": "Palpa",     # Tansen IS Palpa town — bussewa lists buses under "Palpa"
}

#: Hill/trek destinations bussewa lists but runs NO direct bus to — travellers ride to a highway
#: hub and transfer. Used only as a fallback when the destination itself returns zero buses, and
#: surfaced honestly as "via <hub>" so the trip plan stays truthful.
_HUBS: dict[str, tuple[str, str]] = {
    "BANDIPUR": ("Dumre", "get off at Dumre, then a short local ride/taxi up to Bandipur"),
    "GORKHA": ("Aabukhaireni", "change at Aabukhaireni for an onward bus to Gorkha"),
}


def _cities() -> list[str]:
    """A cached list of every city bussewa serves — OFFLINE only (no network).

    Serves the in-process cache, else a fresh-enough (≤24h) disk snapshot written by a prior
    live load. Synchronous on purpose so non-async callers (e.g. the Buses autocomplete
    endpoint) keep working; the live fetch lives in :func:`_load_cities`. Never raises.
    """
    if _cities_cache:
        return _cities_cache
    cached = read_json_snapshot(_cities_snapshot_path(), _CITIES_TTL_S)
    if isinstance(cached, list):
        _cities_cache.extend(s for s in (str(c).strip() for c in cached) if s)
    return _cities_cache


async def _load_cities() -> list[str]:
    """Ensure the city list is loaded, going to the network through the guarded helper.

    Three tiers, best-effort: in-process / fresh disk cache → a live ``/bus/route`` fetch
    (every call guarded by :func:`safe_get_json` so a hiccup/captcha/redirect can't slip an HTML
    body through, persisted to a 24h disk snapshot) → an existing disk snapshot if the live call
    fails. Resolution still works off a slightly-stale list when bussewa hiccups; an empty list
    just means the search tries the raw name. Never raises.
    """
    cached = _cities()  # in-memory or a still-fresh (≤24h) snapshot — no refetch needed
    if cached:
        return cached
    snapshot = _cities_snapshot_path()
    try:
        data = await safe_get_json(
            f"{_BASE}/bus/route", headers=_HEADERS, allow_hosts=_ALLOW_HOSTS, timeout=15
        )
        if isinstance(data, list):
            cities = [s for s in (str(c).strip() for c in data) if s]
            if cities:
                _cities_cache.extend(cities)
                try:  # persist for next time; a write failure must not break resolution
                    write_json_snapshot(snapshot, cities)
                except OSError:
                    pass
                return _cities_cache
    except NetError:  # hiccup/captcha/redirect/oversize — fall back to a stale snapshot below
        pass
    stale = read_json_snapshot(snapshot, -1.0)  # ignore TTL: stale data beats nothing here
    if isinstance(stale, list):
        _cities_cache.extend(s for s in (str(c).strip() for c in stale) if s)
    return _cities_cache


async def _resolve(place: str) -> str | None:
    """Map a free-text place to a city bussewa lists: alias → exact → substring (case-insensitive)."""
    p = (place or "").strip()
    if not p:
        return None
    p = _ALIASES.get(p.upper(), p)
    cities = await _load_cities()
    if not cities:  # the route list is down — fall back to the cleaned input, let the search decide
        return p
    low = {c.lower(): c for c in cities}
    if p.lower() in low:
        return low[p.lower()]
    for c in cities:  # substring either way: "Pokhara" ~ "Pokhara Buspark", "Tansen" ~ "Palpa, tansen"
        cl = c.lower()
        if len(p) >= 3 and (p.lower() in cl or cl in p.lower()):
            return c
    return None


def _to_bs(date: str) -> str | None:
    """Normalise a date to bussewa's Bikram-Sambat ``YYYY-MM-DD``.

    Accepts an AD ISO date (``2026-07-01``) and converts; passes a BS date (year ≥ 2070) through.
    Returns ``None`` if it can't make sense of the input.
    """
    date = (date or "").strip()
    if not date:
        return None
    # already Bikram Sambat? (Nepali years are ~2070+)
    parts = date.replace("/", "-").split("-")
    if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) >= 2070:
        y, m, d = (int(x) for x in parts)
        return f"{y:04d}-{m:02d}-{d:02d}"
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%d-%b-%Y"):
        try:
            ad = datetime.datetime.strptime(date, fmt).date()
            break
        except ValueError:
            ad = None
    if ad is None:
        return None
    from himmy.nepal.calendar import ad_to_bs

    bs = ad_to_bs(ad)
    return f"{bs.year:04d}-{bs.month:02d}-{bs.day:02d}"


def _booking_link(frm: str, to: str, bs_date: str) -> str:
    return f"{_BASE}/trips?from={quote(frm)}&to={quote(to)}&date={quote(bs_date)}"


def _trip_view(t: dict[str, Any]) -> dict[str, Any]:
    """Project one raw bussewa trip into the lean shape the concierge + agent show."""
    try:
        fare = round(float(t.get("ticketPrice") or 0), 2)
    except (TypeError, ValueError):
        fare = None
    bargain = None
    if t.get("bargainApplicable") or t.get("is_bargain_applicable"):
        try:
            bargain = round(float(t.get("minBargainPrice") or 0), 2) or None
        except (TypeError, ValueError):
            bargain = None
    return {
        "operator": t.get("busName") or t.get("operatorName"),
        "bus_type": t.get("busType"),
        "route": t.get("routeName"),
        "depart": t.get("departureTime"),
        "arrive": t.get("estimated_end_time"),
        "journey_hours": t.get("journeyHour"),
        "fare_npr": fare,
        "min_bargain_npr": bargain,
        "seats_available": t.get("availableSeat"),
        "amenities": t.get("amenitiesList") or [],
        "rating": t.get("rating") or 0,
        "review_count": int(t.get("reviewCount") or 0),
        "trip_id": t.get("tripIdHash"),
        "date_ad": t.get("tripDate"),
        "date_bs": t.get("tripDateNp"),
    }


async def _fetch_trips(frm: str, to: str, bs_date: str) -> list[dict[str, Any]]:
    """Raw ``/bus/trips`` list for a route + BS date (raises NetError on transport/JSON error)."""
    raw = await safe_get_json(
        f"{_BASE}/bus/trips",
        params={"from": frm, "to": to, "date": bs_date},
        headers=_HEADERS,
        allow_hosts=_ALLOW_HOSTS,
        timeout=25,
    )
    return raw if isinstance(raw, list) else []


async def bussewa_buses(args: dict[str, Any]) -> dict[str, Any]:
    frm_in = str(args.get("origin") or args.get("from") or "").strip()
    to_in = str(args.get("destination") or args.get("to") or "").strip()
    date_in = str(args.get("date") or "").strip()
    limit = max(1, min(int(args.get("limit") or 8), 20))
    if not frm_in or not to_in:
        return {"ok": False, "message": "Need an origin and a destination (e.g. Kathmandu → Pokhara)."}
    frm = await _resolve(frm_in)
    to = await _resolve(to_in)
    if not frm:
        return {"ok": False, "message": f"Couldn't recognise '{frm_in}' as a bussewa city."}
    if not to:
        return {"ok": False, "message": f"Couldn't recognise '{to_in}' as a bussewa city."}
    # No date → default to a few days out (today's buses have usually left / sold out).
    bs_date = _to_bs(date_in) or _to_bs((datetime.date.today() + datetime.timedelta(days=3)).isoformat())
    if not bs_date:
        return {"ok": False, "message": "Couldn't read that date — try YYYY-MM-DD."}
    link = _booking_link(frm, to, bs_date)
    try:
        raw = await _fetch_trips(frm, to, bs_date)
    except Exception as exc:  # noqa: BLE001 - the deep-link is the fallback
        return {"ok": True, "trips_available": False, "from": frm, "to": to, "date_bs": bs_date,
                "booking_link": link,
                "message": f"Couldn't read live buses ({type(exc).__name__}); open the link to see buses and book."}
    # Hub fallback: a hill/trek spot with no DIRECT bus → ride to the nearest highway hub.
    via = None
    if not raw:
        hub = _HUBS.get(to.upper()) or _HUBS.get(to_in.upper())
        if hub:
            hub_city, note = hub
            try:
                raw2 = await _fetch_trips(frm, hub_city, bs_date)
            except Exception:  # noqa: BLE001
                raw2 = []
            if raw2:
                via = {"hub": hub_city, "for": to, "note": note}
                to, raw, link = hub_city, raw2, _booking_link(frm, hub_city, bs_date)
    if not raw:
        return {"ok": True, "trips_available": False, "from": frm, "to": to, "date_bs": bs_date,
                "booking_link": link,
                "message": "No buses came back for that day — open the link to check other dates."}
    buses = [_trip_view(t) for t in raw]
    buses = [b for b in buses if b["fare_npr"] is not None]
    buses.sort(key=lambda b: (b["fare_npr"], -(b["rating"] or 0)))
    date_ad = buses[0]["date_ad"] if buses else None
    return {
        "ok": True, "trips_available": bool(buses), "from": frm, "to": to, "via": via,
        "date_bs": bs_date, "date_ad": date_ad, "currency": "NPR",
        "count": len(buses), "buses": buses[:limit],
        "cheapest": (buses[0] if buses else None),
        "booking_link": link,
    }


class BussewaConnector:
    """Registers ``bussewa_buses`` — live Nepal bus tickets (bussewa) + a booking deep-link."""

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        safe_register_local_tool(
            registry, name="bussewa_buses", read_only=True, handler=bussewa_buses,
            description=(
                "Search Nepal BUS tickets (bussewa.com.np) and LIVE departures for a route + date. "
                "Pass `origin` and `destination` as Nepali city names (Kathmandu/KTM, Pokhara, Sauraha "
                "or Chitwan, Lumbini, Bharatpur, Butwal, Biratnagar, Dharan, Janakpur, Nepalgunj, ...) "
                "and `date` as YYYY-MM-DD (AD — it is converted to the Nepali date bussewa needs). "
                "Returns each bus (operator, type, depart/arrive time, journey hours, fare in NPR, "
                "seats left, amenities, rating), the cheapest, and a `booking_link` the user opens to "
                "pick a seat and book on bussewa. Great for routes flights don't cover (Chitwan, "
                "Lumbini) or budget travel. Default the origin to Kathmandu when the user doesn't give one."
            ),
            args_json_schema={"type": "object", "properties": {
                "origin": {"type": "string"}, "destination": {"type": "string"},
                "date": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["origin", "destination"]},
        )
        return ["bussewa_buses"]


__all__ = ["BussewaConnector", "bussewa_buses", "_cities", "_load_cities", "_resolve", "_to_bs"]
