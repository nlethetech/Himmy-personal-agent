"""Register the Himmy tool surface onto a himmy :class:`ToolRegistry`.

This is the ``tools_module`` the agent spec points at (``himmy_app.agent_tools:register``);
himmy's ``build_runtime_for_spec`` imports and calls it once at startup. It assembles:

* himmy built-in ``utils`` (calculator, current_time) + ``web`` (search/fetch) packs;
* himmy's **memory** pack — durable ``remember`` / ``recall``;
* this project's **papers RAG** connector (ask_papers / index_papers) over the
  Himmy-owned library. (Himmy no longer uses Zotero; the agent reads library.db directly.)

Each step is best-effort: a missing optional dependency skips that family rather than
crashing the agent. Returns the registered tool names.
"""

from __future__ import annotations

from typing import Any

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors import PapersRagConnector
from himmy_app.connectors._register import safe_register_local_tool


async def _weather_forecast_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only forecast handler: a place name (geocoded) OR lat/lon -> weather.forecast(...).

    Accepts either ``place`` (a place name resolved to coordinates via the SAME keyless
    OpenStreetMap Nominatim lookup ``do_concierge`` uses) or an explicit ``lat``/``lon`` pair,
    plus optional ``start`` / ``end`` (``YYYY-MM-DD``) or ``days``. Returns the shared honest
    forecast contract from :func:`himmy_app.weather.forecast`. Never raises: a bad place or
    upstream hiccup comes back as a well-formed ``{"ok": False, ...}`` dict.
    """
    from himmy_app import weather

    place = (args.get("place") or "").strip()
    lat = args.get("lat")
    lon = args.get("lon")

    # Resolve a place name to coordinates with the SAME Nominatim helper do_concierge uses,
    # only when explicit coordinates weren't supplied.
    if (lat is None or lon is None) and place:
        try:
            from himmy_app.do_concierge import DoConcierge

            geo = await DoConcierge()._geocode(place)
        except Exception:  # noqa: BLE001 - geocoding is best-effort
            geo = None
        if geo is None:
            return {
                "ok": False,
                "current": None,
                "daily": [],
                "in_forecast_window": False,
                "season": "",
                "summary": f"Couldn't locate “{place}”. Try a clearer place name or pass lat/lon.",
            }
        lat, lon = geo

    if lat is None or lon is None:
        return {
            "ok": False,
            "current": None,
            "daily": [],
            "in_forecast_window": False,
            "season": "",
            "summary": "Need a place name or a lat/lon pair to fetch a forecast.",
        }

    return await weather.forecast(
        float(lat),
        float(lon),
        start=args.get("start"),
        end=args.get("end"),
        days=int(args.get("days") or 7),
    )


async def _air_quality_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only AIR-QUALITY handler: a place name (geocoded) OR lat/lon -> weather.air_quality(...).

    Accepts either ``place`` (a place name resolved to coordinates via the SAME keyless
    OpenStreetMap Nominatim lookup ``do_concierge`` / ``weather_forecast`` use) or an explicit
    ``lat``/``lon`` pair. Returns the shared honest AQI contract from
    :func:`himmy_app.weather.air_quality` (``us_aqi`` + ``category`` + ``pm2_5`` / ``pm10`` +
    ``advice``). Never raises: a bad place or upstream hiccup comes back as ``{"ok": False, ...}``.
    """
    from himmy_app import weather

    place = (args.get("place") or "").strip()
    lat = args.get("lat")
    lon = args.get("lon")

    # Resolve a place name to coordinates with the SAME Nominatim helper do_concierge uses,
    # only when explicit coordinates weren't supplied.
    if (lat is None or lon is None) and place:
        try:
            from himmy_app.do_concierge import DoConcierge

            geo = await DoConcierge()._geocode(place)
        except Exception:  # noqa: BLE001 - geocoding is best-effort
            geo = None
        if geo is None:
            return {
                "ok": False,
                "us_aqi": None,
                "category": "Unknown",
                "pm2_5": None,
                "pm10": None,
                "advice": f"Couldn't locate “{place}”. Try a clearer place name or pass lat/lon.",
            }
        lat, lon = geo

    if lat is None or lon is None:
        return {
            "ok": False,
            "us_aqi": None,
            "category": "Unknown",
            "pm2_5": None,
            "pm10": None,
            "advice": "Need a place name or a lat/lon pair to read the air quality.",
        }

    return await weather.air_quality(float(lat), float(lon))


def _nepali_date_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only NEPALI-DATE handler: convert an AD date (or today) to Bikram Sambat.

    Pass ``date`` (``YYYY-MM-DD`` AD) to convert a specific day, or omit it for today. Uses
    himmy's :mod:`himmy.nepal.calendar`. Returns the shared contract::

        {ok, ad: "YYYY-MM-DD", bs: "YYYY-MM-DD", bs_formatted: "<month> DD, YYYY", weekday}

    Never raises: a malformed date degrades to ``{"ok": False, "message": ...}``.
    """
    import datetime as _dt

    from himmy.nepal import calendar as _cal

    raw = (args.get("date") or "").strip() if isinstance(args, dict) else ""
    try:
        ad = _dt.date.fromisoformat(raw) if raw else _dt.date.today()
    except (TypeError, ValueError):
        return {
            "ok": False,
            "message": f"Couldn't read the date “{raw}”. Pass it as YYYY-MM-DD (AD), or omit it for today.",
        }

    try:
        bs = _cal.ad_to_bs(ad)
    except Exception as exc:  # noqa: BLE001 - conversion outside the supported range, etc.
        return {
            "ok": False,
            "message": f"Couldn't convert {ad.isoformat()} to Bikram Sambat ({type(exc).__name__}).",
        }

    return {
        "ok": True,
        "ad": ad.isoformat(),
        "bs": f"{bs.year:04d}-{bs.month:02d}-{bs.day:02d}",
        "bs_formatted": f"{bs.month_name()} {bs.day}, {bs.year}",
        "weekday": bs.weekday_name(),
    }


#: himmy built-in tool packs to bind alongside the academic tools.
#: ``tasks`` gives the agent list_tasks / add_task / complete_task over the shared task
#: board (the same SQLite store the server's /tasks endpoints read/write).
#: ``google`` gives the agent the connected Google account's read tools (gmail_inbox,
#: gcal_events) — registering the pack also exposes gmail_send/gcal_create, but the agent
#: spec's tools allowlist only admits the two READ-ONLY ones (no HITL layer is built yet).
#: When no Google account is connected the tools return a friendly hint, never crash.
#: ``data-sources`` is himmy's KEYLESS public-data pack — ``weather``, ``geocode``,
#: ``wikipedia`` — so Himmy can check the forecast, locate a place, and look up quick facts
#: with no API key.
_BUILTIN_PACKS = ["utils", "web", "data-sources", "tasks", "google"]


def register(registry: ToolRegistry) -> list[str]:
    """Register the tool surface; return the registered names (best-effort)."""
    registered: list[str] = []

    # --- himmy built-in packs (utils, web: calculator, current_time, web search/fetch) ----
    try:
        from himmy.toolkit import ToolkitConfig, register_packs

        before = {d.name for d in registry.list()}
        register_packs(registry, _BUILTIN_PACKS, ToolkitConfig())
        registered += [d.name for d in registry.list() if d.name not in before]
    except Exception:  # noqa: BLE001 - a missing optional extra must not break the agent
        pass

    # --- himmy memory pack: durable remember/recall (shares the spec's auto-recall store) --
    try:
        from himmy.toolkit import ToolkitConfig
        from himmy.toolkit.memory import register_memory_pack

        before = {d.name for d in registry.list()}
        register_memory_pack(registry, ToolkitConfig.from_env())
        registered += [d.name for d in registry.list() if d.name not in before]
    except Exception:  # noqa: BLE001 - memory is best-effort (e.g. no embedder available)
        pass

    # --- this project's connector: papers RAG over the Himmy library --------------------
    try:
        registered += PapersRagConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - a connector wiring hiccup must not break the agent
        pass

    # --- Google Calendar write tools: find / add / edit / remove --------------------------
    try:
        from himmy_app.connectors.google_calendar import GoogleCalendarConnector

        registered += GoogleCalendarConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; calendar editing just won't be offered
        pass

    # --- Himmy's "hands": save_article / add_paper (direct) + mail_send (approval-gated) ---
    try:
        from himmy_app.connectors.actions import ActionsConnector

        registered += ActionsConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort
        pass

    # --- Gmail hands: triage / read / reply (gated) / draft (reversible) -------------------
    try:
        from himmy_app.connectors.gmail_actions import GmailActionsConnector

        registered += GmailActionsConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; email actions just won't be offered
        pass

    # --- Buddha Air: live Nepal-domestic fares + a booking deep-link -----------------------
    try:
        from himmy_app.connectors.buddha_air import BuddhaAirConnector

        registered += BuddhaAirConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; flight search just won't be offered
        pass

    # --- Bussewa: live Nepal bus tickets + a booking deep-link -----------------------------
    try:
        from himmy_app.connectors.bussewa import BussewaConnector

        registered += BussewaConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; bus search just won't be offered
        pass

    # --- Foodmandu: Nepal food-delivery restaurant search + an order link ------------------
    try:
        from himmy_app.connectors.foodmandu import FoodmanduConnector

        registered += FoodmanduConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; food search just won't be offered
        pass

    # --- Daraz: Nepal online-shopping product search + a buy link --------------------------
    try:
        from himmy_app.connectors.daraz import DarazConnector

        registered += DarazConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; shopping search just won't be offered
        pass

    # --- Weather forecast: a place name (geocoded) OR lat/lon -> honest dated forecast ----
    # READ-ONLY. Distinct from the current-only `weather` data-source tool: this returns a
    # multi-day forecast for a SPECIFIC place + date window (honest about the ~16-day horizon).
    try:
        name = safe_register_local_tool(
            registry, name="weather_forecast", read_only=True,
            handler=_weather_forecast_tool,
            description=(
                "Get an honest multi-day WEATHER FORECAST for a specific place and date window. "
                "Pass `place` (a place name — it is geocoded for you) OR an explicit `lat`/`lon` "
                "pair, plus optional `start` and `end` (YYYY-MM-DD) or `days` (default 7). Returns "
                "the current conditions, a per-day forecast (high/low, rain %, conditions with an "
                "emoji), the Nepal seasonal pattern, and a one-line `summary`. It is honest about "
                "the model's ~16-day horizon: if the requested dates are beyond it, "
                "`in_forecast_window` is false and the summary leads with the SEASON instead of a "
                "fabricated daily forecast. Use this (NOT the current-only `weather` tool) whenever "
                "the user asks about the weather for a PLACE on a future DATE or over a trip's days."
            ),
            args_json_schema={"type": "object", "properties": {
                "place": {"type": "string"},
                "lat": {"type": "number"}, "lon": {"type": "number"},
                "start": {"type": "string"}, "end": {"type": "string"},
                "days": {"type": "integer"}}},
        )
        if name:
            registered.append(name)
    except Exception:  # noqa: BLE001 - best-effort; forecast just won't be offered
        pass

    # --- NEPSE stock price: latest price + recent OHLCV by symbol (Merolagani, keyless, NPR) --
    try:
        from himmy_app.connectors.nepse import NepseConnector

        registered += NepseConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; NEPSE prices just won't be offered
        pass

    # --- NRB forex: official Nepal Rastra Bank foreign-exchange rates vs NPR (keyless) --------
    try:
        from himmy_app.connectors.forex import ForexConnector

        registered += ForexConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; forex rates just won't be offered
        pass

    # --- Air quality: a place name (geocoded) OR lat/lon -> current US AQI + advice -----------
    # READ-ONLY. Distinct from weather/weather_forecast: this is the AIR QUALITY (smog/PM2.5)
    # reading, not the temperature/rain forecast.
    try:
        name = safe_register_local_tool(
            registry, name="air_quality", read_only=True,
            handler=_air_quality_tool,
            description=(
                "Get the CURRENT AIR QUALITY (smog / pollution) for a place. Pass `place` (a place "
                "name — it is geocoded for you) OR an explicit `lat`/`lon` pair. Returns the live US "
                "AQI number, its `category` (Good / Moderate / Unhealthy for Sensitive Groups / "
                "Unhealthy / Very Unhealthy / Hazardous), PM2.5 and PM10, and a one-line `advice` "
                "(e.g. mask up when it's high). Use this for any 'air quality / AQI / pollution / is "
                "it smoggy' question — NOT the `weather` tools, which give temperature and rain."
            ),
            args_json_schema={"type": "object", "properties": {
                "place": {"type": "string"},
                "lat": {"type": "number"}, "lon": {"type": "number"}}},
        )
        if name:
            registered.append(name)
    except Exception:  # noqa: BLE001 - best-effort; AQI just won't be offered
        pass

    # --- Nepali date: convert an AD date (or today) to Bikram Sambat -------------------------
    try:
        name = safe_register_local_tool(
            registry, name="nepali_date", read_only=True,
            handler=_nepali_date_tool,
            description=(
                "Convert a date to the NEPALI (Bikram Sambat) calendar. Pass `date` as YYYY-MM-DD "
                "(AD/Gregorian) to convert a specific day, or omit it for TODAY. Returns the AD date, "
                "the BS date (`bs` as YYYY-MM-DD and `bs_formatted` like 'Ashadh 14, 2083'), and the "
                "`weekday`. Use this whenever the user asks 'what's today's Nepali date', 'today in BS', "
                "or to convert any AD date to Bikram Sambat — never guess the BS date."
            ),
            args_json_schema={"type": "object", "properties": {
                "date": {"type": "string", "description": "AD date YYYY-MM-DD; omit for today"}}},
        )
        if name:
            registered.append(name)
    except Exception:  # noqa: BLE001 - best-effort; nepali_date just won't be offered
        pass

    # De-dup while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for name in registered:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def registered_tool_names(registry: ToolRegistry | None = None) -> list[str]:
    """Convenience: register into a fresh (or given) registry and return the names."""
    reg = registry if registry is not None else ToolRegistry()
    return register(reg)


__all__ = ["register", "registered_tool_names"]
