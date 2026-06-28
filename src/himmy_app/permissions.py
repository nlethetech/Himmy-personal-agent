"""Live permissions — what Himmy is allowed to do, per connection.

A small, user-controlled access layer surfaced in Settings → Permissions. Each *surface*
(Email, Calendar, Tasks, Foodmandu, Daraz, Buddha Air, Web, …) has a level the user picks
(e.g. Email = Off / Read only / Read & send). The chosen level maps to a set of agent tools.

Enforcement is at the only seam that matters: :func:`gate_tools` filters the agent spec's tool
allowlist for every request, so a denied tool is never even shown to the model — it can't be
called. :func:`disabled_note` returns a short line injected into the persona so Himmy *gracefully*
explains what's turned off ("email sending is off in Settings → Permissions") instead of failing.

Defaults grant full access (mirroring how the app behaved before this layer existed), so turning
the page on changes nothing until the user dials something down. Levels persist to
``permissions.json`` in the data dir; every read is fresh, so changes take effect on the next turn
with no restart.
"""

from __future__ import annotations

import json
from typing import Any

from himmy_app.config import HimmyConfig, load_config

#: The permission catalogue. Each surface: a level the user picks → the tools it grants.
#: ``requires`` marks surfaces that also need an external connection (Google) to actually work.
SURFACES: list[dict[str, Any]] = [
    {
        "key": "mail", "label": "Email", "service": "Gmail", "requires": "google",
        "desc": "Read your inbox, and optionally draft & send mail on your behalf.",
        "default": "send",
        "levels": [
            {"value": "off", "label": "No access"},
            {"value": "read", "label": "Read only"},
            {"value": "send", "label": "Read & send"},
        ],
        "tools": {
            "read": ["mail_list", "mail_read"],
            "send": ["mail_list", "mail_read", "mail_send", "mail_reply", "mail_draft"],
        },
    },
    {
        "key": "calendar", "label": "Calendar", "service": "Google Calendar", "requires": "google",
        "desc": "See your schedule, and optionally add or change events.",
        "default": "write",
        "levels": [
            {"value": "off", "label": "No access"},
            {"value": "read", "label": "Read only"},
            {"value": "write", "label": "Read & edit"},
        ],
        "tools": {
            "read": ["calendar_find"],
            "write": ["calendar_find", "calendar_add", "calendar_edit", "calendar_remove"],
        },
    },
    {
        "key": "tasks", "label": "Tasks", "service": "Your task board",
        "desc": "Read your to-dos, and optionally add or complete them.",
        "default": "write",
        "levels": [
            {"value": "off", "label": "No access"},
            {"value": "read", "label": "Read only"},
            {"value": "write", "label": "Read & edit"},
        ],
        "tools": {
            "read": ["list_tasks"],
            "write": ["list_tasks", "add_task", "complete_task"],
        },
    },
    {
        "key": "food", "label": "Foodmandu", "service": "Food delivery (Nepal)",
        "desc": "Search restaurants and read menus.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["foodmandu_search", "foodmandu_dishes", "foodmandu_menu"]},
    },
    {
        "key": "shopping", "label": "Daraz", "service": "Online shopping (Nepal)",
        "desc": "Search products and deals.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["daraz_search"]},
    },
    {
        "key": "flights", "label": "Buddha Air", "service": "Domestic flights (Nepal)",
        "desc": "Look up live fares and flight times.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["buddha_air_flights"]},
    },
    {
        "key": "buses", "label": "Bussewa", "service": "Bus tickets (Nepal)",
        "desc": "Find live bus departures, fares and seats; book on bussewa.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["bussewa_buses"]},
    },
    {
        "key": "nepse", "label": "NEPSE", "service": "Nepal stock prices",
        "desc": "Look up live NEPSE share prices and recent OHLCV.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["nepse_price"]},
    },
    {
        "key": "forex", "label": "Forex", "service": "NRB exchange rates",
        "desc": "Check official Nepal Rastra Bank foreign-exchange rates.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["nrb_forex"]},
    },
    {
        "key": "air_quality", "label": "Air quality", "service": "AQI (Nepal & worldwide)",
        "desc": "Check the air quality index (AQI) for a place.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["air_quality"]},
    },
    {
        "key": "web", "label": "Web search", "service": "The open web",
        "desc": "Search and read pages from the wider web.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["web_search", "web_fetch"]},
    },
    {
        "key": "live_data", "label": "Live data", "service": "Weather, places, facts",
        "desc": "Check the forecast, locate a place, look up quick facts.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["weather", "geocode", "wikipedia"]},
    },
    {
        "key": "library", "label": "Library", "service": "Your saved papers",
        "desc": "Read and search your own library, and add papers/articles.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["ask_papers", "index_papers", "add_paper", "save_article"]},
    },
    {
        "key": "files", "label": "Files & media", "service": "Things you upload",
        "desc": "Read files you send Himmy — including reading images/screenshots and "
                "transcribing voice notes.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["read_image", "transcribe_audio"]},
    },
    {
        "key": "memory", "label": "Memory", "service": "What Himmy remembers",
        "desc": "Remember durable facts about you and recall them later.",
        "default": "on",
        "levels": [{"value": "off", "label": "Off"}, {"value": "on", "label": "On"}],
        "tools": {"on": ["remember", "recall"]},
    },
]

_BY_KEY = {s["key"]: s for s in SURFACES}

#: Every tool governed by some surface — anything NOT here (calculator, current_time) is always on.
_GOVERNED: set[str] = {t for s in SURFACES for lvl in s["tools"].values() for t in lvl}


def _path(cfg: HimmyConfig):
    return cfg.data_dir / "permissions.json"


def _defaults() -> dict[str, str]:
    return {s["key"]: s["default"] for s in SURFACES}


def current(cfg: HimmyConfig | None = None) -> dict[str, str]:
    """The user's chosen level per surface, defaulting to full access."""
    cfg = cfg or load_config()
    levels = _defaults()
    try:
        saved = json.loads(_path(cfg).read_text())
        for k, v in (saved or {}).items():
            if k in _BY_KEY and any(opt["value"] == v for opt in _BY_KEY[k]["levels"]):
                levels[k] = v
    except Exception:  # noqa: BLE001 - first run / corrupt → defaults (full access)
        pass
    return levels


def save(levels: dict[str, str], cfg: HimmyConfig | None = None) -> dict[str, str]:
    """Persist (validated) levels; unknown keys/values are ignored."""
    cfg = cfg or load_config()
    merged = current(cfg)
    for k, v in (levels or {}).items():
        if k in _BY_KEY and any(opt["value"] == v for opt in _BY_KEY[k]["levels"]):
            merged[k] = v
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(merged, indent=2))
    return merged


def level_of(key: str, cfg: HimmyConfig | None = None) -> str:
    return current(cfg).get(key, _BY_KEY.get(key, {}).get("default", "off"))


def allowed_tools(cfg: HimmyConfig | None = None) -> set[str]:
    """All governed tool names permitted at the current levels."""
    levels = current(cfg)
    allowed: set[str] = set()
    for s in SURFACES:
        allowed |= set(s["tools"].get(levels.get(s["key"], "off"), []))
    return allowed


def gate_tools(spec_tools: list[str], cfg: HimmyConfig | None = None) -> list[str]:
    """Filter an agent tool allowlist: keep ungoverned (utility) tools always; keep governed
    tools only when the user's current level grants them."""
    allowed = allowed_tools(cfg)
    return [t for t in spec_tools if t not in _GOVERNED or t in allowed]


def disabled_note(cfg: HimmyConfig | None = None) -> str:
    """A persona line so Himmy gracefully explains what's turned off (or empty if all-on)."""
    levels = current(cfg)
    phrases: list[str] = []
    for s in SURFACES:
        lvl = levels.get(s["key"], s["default"])
        if lvl == s["default"]:
            continue
        if lvl == "off":
            phrases.append(f"{s['label']} is OFF")
        elif lvl == "read":
            phrases.append(f"{s['label']} is READ-ONLY (you can view but not change/send)")
        else:
            phrases.append(f"{s['label']} is limited to '{lvl}'")
    if not phrases:
        return ""
    return (
        "ACCESS LIMITS — the user has restricted some capabilities in Settings → Permissions: "
        + "; ".join(phrases)
        + ". If asked to do one of these, do NOT attempt it — briefly say it's turned off and that "
        "they can enable it in Settings → Permissions."
    )


def catalog(cfg: HimmyConfig | None = None, *, google_connected: bool = False,
            google_email: str | None = None) -> dict[str, Any]:
    """The full picture for the Settings UI: each surface, its options, the current level, and
    (for connection-backed surfaces) whether the underlying account is connected."""
    levels = current(cfg)
    out: list[dict[str, Any]] = []
    for s in SURFACES:
        item = {
            "key": s["key"], "label": s["label"], "service": s.get("service", ""),
            "desc": s["desc"], "levels": s["levels"], "level": levels[s["key"]],
            "requires": s.get("requires"),
            "granted_tools": sorted(s["tools"].get(levels[s["key"]], [])),
        }
        if s.get("requires") == "google":
            item["connected"] = bool(google_connected)
            item["account"] = google_email
        out.append(item)
    return {"ok": True, "surfaces": out, "levels": levels}


__all__ = ["SURFACES", "current", "save", "level_of", "allowed_tools", "gate_tools",
           "disabled_note", "catalog"]
