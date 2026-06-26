"""Buddha Air (Nepal DOMESTIC) flight search — live fares + a booking deep-link.

Buddha Air has no public fares API, but its Nuxt booking widget calls
``admin.buddhaair.com/api/V1/booking/availability`` behind a ``BuddhaHash`` request header.
That header is ``"buddhaAir:" + HMAC-SHA512(key="test@1234", JSON.stringify(body))`` (recovered
from the site's own request interceptor), where the body's string values are trimmed and
``type`` is ``"web"``. We replicate that to read live fares directly — no browser, no key.

Graceful by design: route names resolve via the PUBLIC ``sector-destinations`` endpoint, and if
the fare call ever fails (e.g. the signature scheme changes), we still hand the user a working
booking deep-link (``buddhaair.com/search/flight?sector=FROM-TO``) to complete the booking
themselves — exactly the "find the offer, here's the link" model.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
from typing import Any

import httpx

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors._register import safe_register_local_tool

_API = "https://admin.buddhaair.com/api"
_HASH_KEY = b"test@1234"  # recovered from the site's request interceptor (not a secret of ours)
_HEADERS = {
    "Origin": "https://www.buddhaair.com",
    "Referer": "https://www.buddhaair.com/",
    "User-Agent": "Mozilla/5.0",
    "Devicetype": "web",
}
_sectors_cache: dict[str, str] = {}


def _buddha_hash(body_str: str) -> str:
    return "buddhaAir:" + hmac.new(_HASH_KEY, body_str.encode(), hashlib.sha512).hexdigest()


def _sectors() -> dict[str, str]:
    """A cached {CODE-or-NAME (upper) -> sector_code} map from the public index-data."""
    if _sectors_cache:
        return _sectors_cache
    try:
        r = httpx.get(f"{_API}/index-data", headers=_HEADERS, timeout=15)
        for s in r.json()["data"]["sectors-list"]["data"]:
            code = str(s["sector_code"]).strip()
            name = str(s["sector_name"]).strip()
            _sectors_cache[code.upper()] = code
            _sectors_cache[name.upper()] = code
            _sectors_cache[name.split("(")[0].strip().upper()] = code  # "BHADRAPUR (JHAPA)" -> "BHADRAPUR"
    except Exception:  # noqa: BLE001 - resolution is best-effort
        pass
    return _sectors_cache


def _resolve(place: str) -> str | None:
    s = _sectors()
    p = (place or "").strip().upper()
    if not p:
        return None
    if p in s:
        return s[p]
    for k, v in s.items():
        if len(p) >= 3 and (p in k or k in p):
            return v
    return None


def _fmt_date(date: str) -> str:
    """Normalise an ISO / natural date to Buddha Air's ``DD-Mon-YYYY`` (e.g. 25-Jul-2026)."""
    date = (date or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(date, fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    return date


def _booking_link(frm: str, to: str) -> str:
    return f"https://www.buddhaair.com/search/flight?sector={frm}-{to}"


async def buddha_air_flights(args: dict[str, Any]) -> dict[str, Any]:
    frm_in = str(args.get("origin") or args.get("from") or "").strip()
    to_in = str(args.get("destination") or args.get("to") or "").strip()
    date = _fmt_date(str(args.get("date") or ""))
    adults = str(max(1, int(args.get("adults") or 1)))
    children = str(max(0, int(args.get("children") or 0)))
    if not frm_in or not to_in or not date:
        return {"ok": False, "message": "Need origin, destination, and a date (YYYY-MM-DD)."}
    frm, to = _resolve(frm_in), _resolve(to_in)
    if not frm:
        return {"ok": False, "message": f"Couldn't recognise '{frm_in}' as a Buddha Air city."}
    if not to:
        return {"ok": False, "message": f"Couldn't recognise '{to_in}' as a Buddha Air city."}
    link = _booking_link(frm, to)
    body = {
        "sector": f"{frm}-{to}", "is_royal_club": False, "triptype": "O", "departdate": date,
        "nationalityid": "NP", "adult": adults, "child": children, "type": "web",
    }
    body_str = json.dumps(body, separators=(",", ":"))
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.post(
                f"{_API}/V1/booking/availability", content=body_str,
                headers={**_HEADERS, "Content-Type": "application/json", "BuddhaHash": _buddha_hash(body_str)},
            )
        d = resp.json()
    except Exception as exc:  # noqa: BLE001 - the deep-link is the fallback
        return {"ok": True, "fares_available": False, "from": frm, "to": to, "date": date,
                "booking_link": link,
                "message": f"Couldn't read live fares ({type(exc).__name__}); open the link to see fares and book."}
    if not d.get("success"):
        return {"ok": True, "fares_available": False, "from": frm, "to": to, "date": date, "booking_link": link,
                "message": d.get("message") or "No fares came back; open the link to check availability."}
    flights: list[dict[str, Any]] = []
    for f in (d.get("data") or {}).get("outbound", []) or []:
        try:
            fare = f["airfare"]["faredetail"]["adult"]["totalfarenprusd"]
        except Exception:  # noqa: BLE001
            fare = None
        if fare is None:
            continue
        flights.append({
            "flight": f.get("flightno"), "depart": f.get("departuretime"), "arrive": f.get("arrivaltime"),
            "from": f.get("departurecity"), "to": f.get("arrivalcity"),
            "fare_npr": round(float(fare), 2), "class": f.get("classcode"),
        })
    flights.sort(key=lambda x: x["fare_npr"])
    return {
        "ok": True, "fares_available": bool(flights), "from": frm, "to": to, "date": date,
        "currency": "NPR", "flights": flights, "cheapest": (flights[0] if flights else None),
        "booking_link": link,
    }


class BuddhaAirConnector:
    """Registers ``buddha_air_flights`` — live Buddha Air domestic fares + a booking link."""

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        safe_register_local_tool(
            registry, name="buddha_air_flights", read_only=True, handler=buddha_air_flights,
            description=(
                "Search Buddha Air (Nepal DOMESTIC) flights and LIVE fares for a route + date. Pass "
                "`origin` and `destination` as Nepali city names or codes (Kathmandu/KTM, Pokhara/PKR, "
                "Bhairahawa/BWA, Biratnagar/BIR, Bharatpur/BHR, Janakpur/JKR, Nepalgunj/KEP, "
                "Dhangadhi/DHI, Simara/SIF, Surkhet/SKH, ...) and `date` as YYYY-MM-DD. Returns each "
                "flight (number, times, fare in NPR), the cheapest, and a `booking_link` the user "
                "opens to complete the booking themselves. Default the origin from the user's vault "
                "(home airport) when they don't give one. Buddha Air is DOMESTIC Nepal only — for "
                "international routes say so and don't use this tool."
            ),
            args_json_schema={"type": "object", "properties": {
                "origin": {"type": "string"}, "destination": {"type": "string"},
                "date": {"type": "string"}, "adults": {"type": "integer"}, "children": {"type": "integer"}},
                "required": ["origin", "destination", "date"]},
        )
        return ["buddha_air_flights"]


__all__ = ["BuddhaAirConnector", "buddha_air_flights"]
