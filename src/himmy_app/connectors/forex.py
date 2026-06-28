"""Official Nepal Rastra Bank (NRB) foreign-exchange rates — keyless, host-pinned.

NRB publishes the country's official daily reference rates through a free, keyless JSON API at
``www.nrb.org.np/api/forex/v1/rates``. This connector reads it through the same guarded HTTP
helper every connector uses (:func:`himmy_app.connectors._net.safe_get_json`) so it inherits the
project's SSRF / redirect / content-type / size / retry defences, with the host pinned to
``www.nrb.org.np`` and nothing else — that allow-list is the real enforcing control.

The endpoint returns a ``data.payload`` list, one entry per published date in the requested
``from``/``to`` window. We always request a short trailing window (NRB can lag a day or skip
weekends/holidays) and pick the **latest** published date, so "today's rates" stays correct even
when today's sheet hasn't been published yet.

Graceful by design: a down upstream or an empty window returns a well-formed ``{"ok": False, ...}``
dict rather than raising — a missing forex sheet can never break a conversation. Rates are NRB's
official buy/sell per the currency's quoted ``unit`` (e.g. INR is per 100, JPY per 10); we surface
the ``unit`` verbatim and never silently rescale.

The public surface is the :class:`ForexConnector` (registers ``nrb_forex``) and the
:func:`nrb_forex` tool handler.
"""

from __future__ import annotations

import datetime
from typing import Any

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors._net import safe_get_json
from himmy_app.connectors._register import safe_register_local_tool

__all__ = ["ForexConnector", "nrb_forex"]

#: Keyless official NRB forex endpoint. Host-pinned — the allow-list is the enforcing control.
_HOST = "www.nrb.org.np"
_URL = "https://www.nrb.org.np/api/forex/v1/rates"
#: Look back a few days so weekends / holidays / a not-yet-published sheet still resolve to a date.
_LOOKBACK_DAYS = 6
#: The currencies we surface by default when the caller doesn't name any (the big, liquid ones).
_DEFAULT_CURRENCIES = ("USD", "EUR", "GBP", "INR", "AUD", "CNY", "JPY")


def _bs_for(date_ad: str | None) -> str | None:
    """Bikram Sambat ``YYYY-MM-DD`` for an AD ``YYYY-MM-DD`` string; None if it can't be derived.

    Best-effort and import-local so a calendar hiccup never sinks a perfectly good rate sheet.
    """
    if not date_ad:
        return None
    try:
        from himmy.nepal.calendar import ad_to_bs  # local import: keep module import light

        d = datetime.datetime.strptime(str(date_ad).strip()[:10], "%Y-%m-%d").date()
        bs = ad_to_bs(d)
        return f"{bs.year:04d}-{bs.month:02d}-{bs.day:02d}"
    except Exception:  # noqa: BLE001 — the BS date is a nicety, never load-bearing
        return None


def _num(value: Any) -> float | None:
    """Coerce NRB's string-quoted buy/sell to a finite float; None on anything unparseable."""
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf guard
        return None
    return f


def _int_unit(value: Any) -> int:
    """Coerce a currency ``unit`` to a positive int (defaults to 1 for a missing/garbled unit)."""
    try:
        u = int(round(float(value)))
    except (TypeError, ValueError):
        return 1
    return u if u >= 1 else 1


def _pick_latest_sheet(payload: list[Any]) -> dict[str, Any] | None:
    """Return the payload entry with the most recent ``date`` (NRB returns oldest-or-mixed order)."""
    best: dict[str, Any] | None = None
    best_date = ""
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        date = str(entry.get("date") or "").strip()
        if date >= best_date:  # ISO dates sort lexicographically; ties keep the later list entry
            best_date = date
            best = entry
    return best


def _parse_rates(
    rates: list[Any], wanted: frozenset[str] | None
) -> list[dict[str, Any]]:
    """Flatten NRB's nested ``rates`` into ``[{iso3,name,unit,buy,sell}]``, filtered to ``wanted``.

    A row missing both buy and sell, or with an unparseable iso3, is dropped — we never surface a
    half-empty rate. ``wanted`` is a set of upper-case iso3 codes (None means "all of them").
    """
    out: list[dict[str, Any]] = []
    for r in rates:
        if not isinstance(r, dict):
            continue
        cur = r.get("currency") or {}
        iso3 = str(cur.get("iso3") or "").strip().upper()
        if not iso3:
            continue
        if wanted is not None and iso3 not in wanted:
            continue
        buy = _num(r.get("buy"))
        sell = _num(r.get("sell"))
        if buy is None and sell is None:
            continue
        out.append({
            "iso3": iso3,
            "name": str(cur.get("name") or iso3).strip(),
            "unit": _int_unit(cur.get("unit")),
            "buy": buy,
            "sell": sell,
        })
    return out


def _normalise_currencies(args: dict[str, Any]) -> frozenset[str] | None:
    """Build the requested iso3 set from ``args['currencies']`` (list or comma string); None = all.

    An explicit empty/blank request falls back to the default big-currency set rather than 'all',
    so a vague "show me forex" stays tight. ``'all'`` / ``'*'`` explicitly asks for everything.
    """
    raw = args.get("currencies")
    if raw is None:
        return frozenset(_DEFAULT_CURRENCIES)
    if isinstance(raw, str):
        tokens = [t for t in raw.replace(",", " ").split() if t]
    elif isinstance(raw, (list, tuple, set)):
        tokens = [str(t) for t in raw]
    else:
        return frozenset(_DEFAULT_CURRENCIES)
    codes = {t.strip().upper() for t in tokens if t and t.strip()}
    if not codes:
        return frozenset(_DEFAULT_CURRENCIES)
    if "ALL" in codes or "*" in codes:
        return None  # caller explicitly wants every published currency
    return frozenset(codes)


async def nrb_forex(args: dict[str, Any]) -> dict[str, Any]:
    """Fetch the latest official NRB foreign-exchange rates.

    Args (all optional, in ``args``):
        currencies: a list (``["USD","INR"]``) or comma/space string (``"USD, INR"``) of iso3
            codes to return; ``"all"``/``"*"`` returns every published currency. Defaults to the
            big liquid ones (USD, EUR, GBP, INR, AUD, CNY, JPY).

    Returns a dict matching the shared contract::

        {"ok": True, "date": "YYYY-MM-DD", "date_bs": "YYYY-MM-DD", "base": "NPR",
         "rates": [{"iso3","name","unit","buy","sell"}],
         "caption": "NRB official mid-market; per <unit> units"}

    A down upstream / empty window returns ``{"ok": False, "message": ...}`` — never raises.
    """
    wanted = _normalise_currencies(args if isinstance(args, dict) else {})
    today = datetime.date.today()
    frm = (today - datetime.timedelta(days=_LOOKBACK_DAYS)).isoformat()
    to = today.isoformat()
    params = {"page": 1, "per_page": 100, "from": frm, "to": to}

    try:
        data = await safe_get_json(
            _URL,
            params=params,
            allow_hosts=(_HOST,),
            timeout=15.0,
            max_bytes=2_000_000,
            retries=1,
        )
    except Exception as exc:  # noqa: BLE001 — NetError + any transport error: degrade gracefully
        return {
            "ok": False,
            "message": f"Couldn't reach NRB forex right now ({type(exc).__name__}).",
        }

    payload = (((data or {}).get("data") or {}) if isinstance(data, dict) else {}).get("payload")
    if not isinstance(payload, list) or not payload:
        return {"ok": False, "message": "NRB returned no forex rates for the recent window."}

    sheet = _pick_latest_sheet(payload)
    if not sheet:
        return {"ok": False, "message": "NRB returned no usable forex sheet."}

    date_ad = str(sheet.get("date") or "").strip() or None
    rates = _parse_rates(sheet.get("rates") or [], wanted)
    if not rates:
        # A valid sheet but none of the requested currencies appear in it.
        asked = "all currencies" if wanted is None else ", ".join(sorted(wanted))
        return {
            "ok": False,
            "message": f"NRB sheet for {date_ad or 'the latest date'} has no rates for {asked}.",
            "date": date_ad,
        }

    return {
        "ok": True,
        "date": date_ad,
        "date_bs": _bs_for(date_ad),
        "base": "NPR",
        "rates": rates,
        "caption": "NRB official mid-market; per <unit> units",
    }


class ForexConnector:
    """Registers ``nrb_forex`` — official Nepal Rastra Bank foreign-exchange rates (keyless)."""

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        safe_register_local_tool(
            registry,
            name="nrb_forex",
            read_only=True,
            handler=nrb_forex,
            description=(
                "Get the LATEST official Nepal Rastra Bank (NRB) foreign-exchange rates against the "
                "Nepali Rupee (NPR). Optionally pass `currencies` as a list or comma string of iso3 "
                "codes (e.g. ['USD','INR'] or 'USD, INR'); omit it for the big ones (USD, EUR, GBP, "
                "INR, AUD, CNY, JPY), or pass 'all' for every published currency. Each rate carries "
                "its quoted `unit` (INR is per 100, JPY per 10) plus `buy` and `sell` in NPR. Returns "
                "the publication `date` (AD) and `date_bs` (Bikram Sambat). Use this for any "
                "'NPR to/from <currency>' or 'exchange rate / forex' question about Nepal."
            ),
            args_json_schema={
                "type": "object",
                "properties": {
                    "currencies": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "iso3 codes to return, e.g. ['USD','INR']; omit for the defaults.",
                    }
                },
            },
        )
        return ["nrb_forex"]
