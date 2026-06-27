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

import asyncio
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

import httpx

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors._net import (
    NetError,
    read_json_snapshot,
    safe_get_json,
    write_json_snapshot,
)
from himmy_app.connectors._register import safe_register_local_tool

_API = "https://admin.buddhaair.com/api"
_HASH_KEY = b"test@1234"  # recovered from the site's request interceptor (not a secret of ours)
_HEADERS = {
    "Origin": "https://www.buddhaair.com",
    "Referer": "https://www.buddhaair.com/",
    "User-Agent": "Mozilla/5.0",
    "Devicetype": "web",
}
#: Both the API host (admin) and the site host (www) — a redirect between them must stay allowed.
_ALLOW_HOSTS = ("admin.buddhaair.com", "www.buddhaair.com")
#: The sector map rarely changes; a day-old copy is fine and beats every route going unrecognised.
_SECTORS_TTL_S = 24 * 60 * 60
_sectors_cache: dict[str, str] = {}


def _data_dir() -> Path:
    """The app data dir (``.scholar-desk``), mirroring ``config.load_config`` without importing it.

    Kept dependency-light so this connector module imports cleanly on its own.
    """
    env = os.environ.get("HIMMY_APP_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[3] / ".scholar-desk"


def _sectors_snapshot_path() -> Path:
    return _data_dir() / "buddha_air_sectors.json"


def _buddha_hash(body_str: str) -> str:
    return "buddhaAir:" + hmac.new(_HASH_KEY, body_str.encode(), hashlib.sha512).hexdigest()


async def _fetch_index_data() -> Any:
    """One guarded GET of the public ``index-data`` endpoint (SSRF / redirect / content-safe)."""
    return await safe_get_json(
        f"{_API}/index-data",
        headers=_HEADERS,
        allow_hosts=_ALLOW_HOSTS,
        timeout=15,
    )


def _index_data_blocking() -> Any | None:
    """Synchronous wrapper around the async fetch; returns None on any network failure.

    Safe whether or not an event loop is already running: ``asyncio.run`` raises
    ``RuntimeError`` when called from inside a running loop (the server's case), which previously
    left the sector map empty on a cold process so every flight city came back 'unrecognised'.
    When a loop is already running we offload the fetch to a dedicated thread (with its OWN loop)
    so resolution still works. Best-effort — any failure yields None.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    try:
        if running is None:
            return asyncio.run(_fetch_index_data())
        # We're inside a running loop (server): run the fetch in a separate thread + loop.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(_fetch_index_data())).result()
    except NetError:
        return None
    except Exception:  # noqa: BLE001 - resolution is best-effort, never fatal
        return None


def _sectors_from_index(data: Any) -> dict[str, str]:
    """Build the {CODE-or-NAME (upper) -> sector_code} map from one index-data payload."""
    mapped: dict[str, str] = {}
    for s in ((data or {}).get("data") or {}).get("sectors-list", {}).get("data", []) or []:
        code = str(s["sector_code"]).strip()
        name = str(s["sector_name"]).strip()
        mapped[code.upper()] = code
        mapped[name.upper()] = code
        mapped[name.split("(")[0].strip().upper()] = code  # "BHADRAPUR (JHAPA)" -> "BHADRAPUR"
    return mapped


def _sectors() -> dict[str, str]:
    """A cached {CODE-or-NAME (upper) -> sector_code} map from the public index-data.

    Resolution survives a process restart and a failed first fetch via a 24h on-disk snapshot,
    so a transient blip never leaves every route unrecognised. We only ever cache a *non-empty*
    map — an empty/None resolution is a transient failure and must not be persisted, or it would
    permanently mis-brand a real city until the TTL expired.
    """
    if _sectors_cache:
        return _sectors_cache
    # On-disk snapshot first: a restart or a dead upstream still resolves routes.
    snap = read_json_snapshot(_sectors_snapshot_path(), _SECTORS_TTL_S)
    if isinstance(snap, dict) and snap:
        _sectors_cache.update({str(k): str(v) for k, v in snap.items()})
        return _sectors_cache
    mapped = _sectors_from_index(_index_data_blocking())
    if mapped:  # never cache/persist an empty (transient-failure) result
        _sectors_cache.update(mapped)
        try:
            write_json_snapshot(_sectors_snapshot_path(), mapped)
        except OSError:  # noqa: BLE001 - persistence is best-effort
            pass
    return _sectors_cache


def sector_options() -> list[tuple[str, str]]:
    """``[(code, display_name)]`` for every Buddha Air sector — for smart resolution / pickers."""
    out: list[tuple[str, str]] = []
    data = _index_data_blocking()
    for s in ((data or {}).get("data") or {}).get("sectors-list", {}).get("data", []) or []:
        out.append((str(s["sector_code"]).strip(), str(s["sector_name"]).strip()))
    return out


#: Common Nepali spellings / alternate names → the canonical sector name (deterministic, no model).
_ALIASES = {
    "BHAIRAWA": "BHAIRAHAWA", "BHAIRHAWA": "BHAIRAHAWA", "SIDDHARTHANAGAR": "BHAIRAHAWA",
    "LUMBINI": "BHAIRAHAWA", "SUNAULI": "BHAIRAHAWA", "KATH": "KATHMANDU",
    "NARAYANGARH": "BHARATPUR", "NARAYANGADH": "BHARATPUR", "CHITWAN": "BHARATPUR",
    "NEPALJUNG": "NEPALGUNJ", "NEPALGANJ": "NEPALGUNJ", "DHANGADI": "DHANGADHI",
}


def _resolve(place: str) -> str | None:
    s = _sectors()
    p = (place or "").strip().upper()
    if not p:
        return None
    p = _ALIASES.get(p, p)
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


def _is_json_ct(content_type: str | None) -> bool:
    if not content_type:
        return False
    main = content_type.split(";", 1)[0].strip().lower()
    return main in {"application/json", "text/json", "application/jsonrequest"} or main.endswith("+json")


#: Hard cap on the availability response body (mirrors safe_get_json's default).
_POST_MAX_BYTES = 5_000_000


async def _read_capped_post(resp: httpx.Response, max_bytes: int) -> bytes:
    """Stream a response body, aborting (NetError) once it exceeds ``max_bytes``."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise NetError(f"response exceeds size cap ({max_bytes} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


async def _availability_post(body_str: str) -> Any:
    """POST the signed availability request with the guards ``safe_get_json`` applies for GET.

    ``safe_get_json`` is GET-only and can't carry the body + signed ``BuddhaHash`` header this
    endpoint needs. The destination is a hardcoded constant on the already-allow-listed
    ``admin.buddhaair.com`` host, so the SSRF/allow-host check adds nothing here; what we DO
    replicate are the body-shaped protections: redirects are NOT auto-followed (a captcha/login
    302 raises instead of silently chasing into an HTML wall), the response must be 2xx with a
    JSON-ish content-type, the body is STREAMED and capped at ``_POST_MAX_BYTES`` (so a hostile or
    runaway response can't exhaust memory), and a non-JSON body raises :class:`NetError` rather
    than blowing up inside ``.json()``. Raises NetError on any of those so the caller's existing
    deep-link fallback engages cleanly. (No retry: a failed fare read degrades to the deep-link.)
    """
    headers = {**_HEADERS, "Content-Type": "application/json", "BuddhaHash": _buddha_hash(body_str)}
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as c:
        req = c.build_request(
            "POST", f"{_API}/V1/booking/availability", content=body_str, headers=headers
        )
        resp = await c.send(req, stream=True)
        try:
            if resp.is_redirect:
                raise NetError("unexpected redirect (captcha/login wall)")
            if resp.status_code >= 400:
                raise NetError(f"HTTP {resp.status_code} for request")
            if not _is_json_ct(resp.headers.get("content-type")):
                raise NetError(
                    f"non-JSON response (content-type={resp.headers.get('content-type', '?')!r})"
                )
            body = await _read_capped_post(resp, _POST_MAX_BYTES)
        finally:
            await resp.aclose()
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NetError(f"invalid JSON body ({exc.__class__.__name__})") from None


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
        d = await _availability_post(body_str)
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


__all__ = ["BuddhaAirConnector", "buddha_air_flights", "sector_options", "_resolve"]
