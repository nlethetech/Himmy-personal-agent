"""The "Do" page — a smart Nepal concierge over the flights / food / shopping connectors.

This is the recommendation brain behind the Do tab. It is **hybrid by design** so it is smart
*and* cheap on the user's model usage:

* **Instant, free picks (rules).** Candidates come straight from the live connectors
  (``foodmandu_search`` / ``daraz_search`` / ``buddha_air_flights``) and are ranked by plain
  signals — open-now, rating, discount, the user's saved preferences (the profile "vault"), and
  what they've thumbed. No model call. This always renders.
* **One cheap AI pass (concierge).** A SINGLE batched completion re-ranks all three rails at once
  and writes the short personal "why" per pick + a friendly headline. It reuses the app's
  configured model exactly like the agent (``build_inference_for`` → cost is auto-metered into
  ``/usage``), and it only runs when the cache is stale or a refresh is forced — never on a plain
  page open. So glancing at the page costs nothing; the smart layer refreshes in the background.

The board is cached to ``do_cache.json`` (TTL ``HIMMY_DO_TTL`` secs). ``board()`` serves the warm
cache instantly and refreshes behind it (the same serve-cache-then-refresh trick the News hub
uses). Every pick carries a deep-link the user opens to finish the order/booking themselves — the
concierge never spends money.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import os
import re
import sqlite3
import time
from typing import Any

from himmy_app.config import HimmyConfig, load_config
from himmy_app.connectors import _net

#: How long a generated board stays fresh before a background refresh recomputes it.
_TTL = float(os.environ.get("HIMMY_DO_TTL") or str(6 * 3600))

#: How many picks each rail shows (rails are padded to this so the layout stays full).
_TARGETS = {"food": 4, "deals": 4, "foryou": 4, "flights": 3}

#: Hard ceiling per board rail. Sits ABOVE the slowest warm path (a ~25s connector + the flights
#: rail's parallel Buddha Air calls) so a healthy-but-slow rail is never trimmed — only a truly
#: hung connector is dropped (→ []), so one stuck rail can't stall the whole board.
_RAIL_TIMEOUT = float(os.environ.get("HIMMY_DO_RAIL_TIMEOUT") or "40")

#: Bound on the in-memory positive airport-resolution cache (FIFO-evicted). Empty/failed
#: resolutions are NEVER cached (a transient miss must not poison the entry).
_AIR_CACHE_MAX = 256

#: How far ahead a travel date may be before we treat it as un-bookable. ~330 days mirrors a
#: typical airline/bus booking window: beyond it the providers can't sell a ticket, so a live
#: fare lookup would just return an empty/confusing result. trip() clamps to this; flights/buses
#: reject past it with a friendly message.
_MAX_BOOK_HORIZON_DAYS = 330

#: "For You" shopping seeds (Daraz, interest-driven not discount-driven) + the vault labels we
#: read them from. Falls back to broad lifestyle categories until the user saves interests.
_SHOP_SEEDS = ["books", "home decor", "kitchen", "backpack", "headphones"]
_SHOP_VAULT_HINTS = ("interest", "shop", "hobby", "gadget", "wishlist", "want", "like")

#: Fast aliases for common Nepali city spellings/alt-names → a name the sector resolver knows.
#: (Anything not here falls back to Himmy/the model in `_resolve_airport`.)
_CITY_ALIASES = {
    "ktm": "Kathmandu", "kath": "Kathmandu", "pkr": "Pokhara",
    "bhairawa": "Bhairahawa", "bhairhawa": "Bhairahawa", "bhairawaa": "Bhairahawa",
    "siddharthanagar": "Bhairahawa", "sunauli": "Bhairahawa", "lumbini": "Bhairahawa", "bwa": "Bhairahawa",
    "biratnagar": "Biratnagar", "birat": "Biratnagar", "nepalganj": "Nepalgunj", "nepaljung": "Nepalgunj",
    "chitwan": "Bharatpur", "narayangarh": "Bharatpur", "narayangadh": "Bharatpur",
    "janakpurdham": "Janakpur", "dhangadi": "Dhangadhi", "dhangari": "Dhangadhi",
}

#: Default seeds when the vault tells us nothing — broad, popular Nepal queries.
_FOOD_SEEDS = ["momo", "pizza", "chowmein", "burger", "newari"]
_DEAL_SEEDS = ["headphones", "smart watch", "kitchen", "sneakers", "power bank"]
_DEAL_VAULT_HINTS = ("shop", "interest", "buy", "wishlist", "gadget", "hobby")
_FOOD_VAULT_HINTS = ("food", "cuisine", "diet", "eat", "favourite", "favorite", "dish")

#: Feedback recency: tags decay with this half-life (a thumb from ~21 days ago counts half), and a
#: thumb-down counts as "recent" (steer the AI away from it) for this window.
_FB_HALF_LIFE_S = float(os.environ.get("HIMMY_DO_FB_HALF_LIFE") or str(21 * 24 * 3600))
_FB_RECENT_S = float(os.environ.get("HIMMY_DO_FB_RECENT") or str(14 * 24 * 3600))

#: Coarse price bands (NPR) a thumb is tagged by, so taste generalises across items, not rows.
_PRICE_BANDS = ((800.0, "cheap"), (3000.0, "mid"))  # <800 cheap, <3000 mid, else premium


# --------------------------------------------------------------------------------------------
# tiny learning store: thumbs-down / thumbs-up on concierge picks (down-/up-weight by key+tags)
# --------------------------------------------------------------------------------------------
class DoFeedback:
    """Remembers picks the user dismissed and tags they liked, to bias the free ranking."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        cfg = config or load_config()
        self._db = cfg.feedback_db_path
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._db), timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS do_feedback (
                    key TEXT PRIMARY KEY, kind TEXT, rail TEXT, tags TEXT, at REAL
                )"""
            )

    def record(self, kind: str, key: str, rail: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        key = (key or "").strip()
        if not key or kind not in {"down", "up"}:
            return {"ok": False, "error": "need a key and kind=up|down"}
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO do_feedback (key, kind, rail, tags, at) VALUES (?,?,?,?,?)",
                (key, kind, rail, json.dumps(tags or []), time.time()),
            )
        return {"ok": True, "key": key, "kind": kind}

    def signals(self) -> tuple[set[str], dict[str, float]]:
        """Return ``(dismissed keys, tag -> recency-decayed net weight)`` for biasing candidates.

        Taste is keyed by COARSE tags (cuisine + a cheap/mid/premium price band, see
        :func:`_coarsen_feedback_tags`) — not exact item names — so a thumb teaches a category, not
        one disposable row. Every signal is weighted by an exponential RECENCY DECAY (half-life
        ``_FB_HALF_LIFE_S``) so stale taste fades and a fresh thumb dominates.
        """
        dismissed, weights, _recent = self._signals_full()
        return dismissed, weights

    def recent_down_tags(self) -> set[str]:
        """The coarse tags the user thumbed DOWN recently (within ``_FB_RECENT_S``) — fed into the
        AI re-rank so it won't immediately re-surface a just-dismissed cuisine / price band."""
        return self._signals_full()[2]

    def _signals_full(self) -> tuple[set[str], dict[str, float], set[str]]:
        """``(dismissed keys, recency-decayed tag weights, recently-dismissed tags)`` in one pass."""
        dismissed: set[str] = set()
        weights: dict[str, float] = {}
        recent_down: set[str] = set()
        now = time.time()
        with self._conn() as c:
            for r in c.execute("SELECT key, kind, tags, at FROM do_feedback"):
                if r["kind"] == "down":
                    dismissed.add(r["key"])
                age = max(0.0, now - float(r["at"] or now))
                decay = 0.5 ** (age / _FB_HALF_LIFE_S)        # 1.0 fresh → fades with age
                delta = (1.0 if r["kind"] == "up" else -1.0) * decay
                for t in json.loads(r["tags"] or "[]"):
                    if not t:
                        continue
                    t = t.lower()
                    weights[t] = weights.get(t, 0.0) + delta
                    if r["kind"] == "down" and age <= _FB_RECENT_S:
                        recent_down.add(t)
        return dismissed, weights, recent_down


# --------------------------------------------------------------------------------------------
# the tray: a Himmy-side cart of dishes + products, grouped by place, that the user checks out
# themselves (opening the restaurant/product page). No vendor login — that's the later rung.
# --------------------------------------------------------------------------------------------
class DoCart:
    def __init__(self, config: HimmyConfig | None = None) -> None:
        cfg = config or load_config()
        self._db = cfg.data_dir / "do_cart.db"
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._db), timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS cart (
                    key TEXT PRIMARY KEY, source TEXT, place TEXT, checkout_link TEXT,
                    name TEXT, price REAL, qty INTEGER, image TEXT, link TEXT, at REAL
                )"""
            )

    def add(self, item: dict[str, Any]) -> dict[str, Any]:
        key = str(item.get("key") or "").strip()
        name = str(item.get("name") or "").strip()
        if not key or not name:
            return {"ok": False, "error": "need key + name"}
        with self._conn() as c:
            row = c.execute("SELECT qty FROM cart WHERE key=?", (key,)).fetchone()
            qty = (row["qty"] if row else 0) + int(item.get("qty") or 1)
            c.execute(
                """INSERT INTO cart (key, source, place, checkout_link, name, price, qty, image, link, at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET qty=excluded.qty""",
                (key, str(item.get("source") or "shop"), str(item.get("place") or ""),
                 str(item.get("checkout_link") or item.get("link") or ""), name,
                 float(item.get("price") or 0), qty, str(item.get("image") or ""),
                 str(item.get("link") or ""), time.time()),
            )
        return {"ok": True, **self.view()}

    def set_qty(self, key: str, qty: int) -> dict[str, Any]:
        with self._conn() as c:
            if qty <= 0:
                c.execute("DELETE FROM cart WHERE key=?", (key,))
            else:
                c.execute("UPDATE cart SET qty=? WHERE key=?", (int(qty), key))
        return {"ok": True, **self.view()}

    def remove(self, key: str) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("DELETE FROM cart WHERE key=?", (key,))
        return {"ok": True, **self.view()}

    def clear(self) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("DELETE FROM cart")
        return {"ok": True, **self.view()}

    def view(self) -> dict[str, Any]:
        with self._conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM cart ORDER BY at")]
        groups: dict[str, dict[str, Any]] = {}
        total = 0.0
        count = 0
        for r in rows:
            g = groups.setdefault(r["place"] or "Other", {
                "place": r["place"] or "Other", "source": r["source"],
                "checkout_link": r["checkout_link"], "items": [], "subtotal": 0.0})
            line = float(r["price"] or 0) * int(r["qty"] or 1)
            g["items"].append({"key": r["key"], "name": r["name"], "price": r["price"],
                               "qty": r["qty"], "image": r["image"], "link": r["link"]})
            g["subtotal"] += line
            total += line
            count += int(r["qty"] or 1)
        return {"groups": list(groups.values()), "total": round(total, 2), "count": count}


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------
def _vault(cfg: HimmyConfig) -> dict[str, str]:
    """The user's CONFIRMED label→value details (home airport, cuisines, budget, …).

    Reads ONLY the ``user`` layer — the gated, user-confirmed vault. The machine-inferred
    ``learned`` layer is deliberately excluded so action-affecting reads (flights origin, budget,
    cuisine seeding) are driven solely by facts the user has explicitly confirmed via
    ``apply_suggestions`` — never by anything auto-inferred or prompt-injected.
    """
    try:
        from himmy_app import user_profile

        prof = user_profile.load(cfg)
        details: dict[str, str] = {}
        for k, v in ((prof.get("user") or {}).get("details") or {}).items():
            details[str(k)] = str(v)
        return details
    except Exception:  # noqa: BLE001 - the vault is optional
        return {}


def _seeds_from_vault(vault: dict[str, str], hints: tuple[str, ...], fallback: list[str]) -> list[str]:
    """Pull comma/space-separated seed terms from any vault value whose label matches a hint."""
    picked: list[str] = []
    for label, value in vault.items():
        if any(h in label.lower() for h in hints):
            for part in re.split(r"[,/;]| and ", value):
                part = part.strip()
                if part and len(part) <= 30:
                    picked.append(part)
    # de-dup, keep order; fall back to the broad defaults when the vault says nothing.
    seen: set[str] = set()
    out = [p for p in picked if not (p.lower() in seen or seen.add(p.lower()))]
    return (out or fallback)[:4]


def _discount_pct(text: str) -> int:
    m = re.search(r"(\d{1,2})\s*%", text or "")
    return int(m.group(1)) if m else 0


def _rating(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _trip_plan_signals(vault: dict[str, str]) -> str:
    """Non-identifying planning signals for the trip-planner prompt.

    SECURITY/PRIVACY: the trip plan is a SHAREABLE artifact. We deliberately do NOT pass the full
    free-text profile (``render_for_prompt``) into the planner — its about/projects/topics/voice
    prose can embed sensitive FACTS (health, diet, employer) and the user's NAME, which then leak
    into the exported markdown (denylist scrubbing of paraphrased facts is inherently leaky). Here
    we hand the model ONLY the few structured, non-identifying levers a plan actually needs —
    dietary detail and budget band, pulled from the confirmed vault — and nothing else.
    """
    lines: list[str] = []
    for label, value in vault.items():
        low = label.lower()
        v = str(value).strip()
        if not v:
            continue
        if any(h in low for h in ("diet", "cuisine", "food", "vegetarian", "vegan", "halal")):
            lines.append(f"Dietary / food preference: {v}")
        elif "budget" in low or "spend" in low:
            lines.append(f"Travel budget: {v}")
    return "\n".join(lines) if lines else "(no specific dietary or budget preferences saved)"


def _home_airport(vault: dict[str, str]) -> str:
    for label, value in vault.items():
        if "airport" in label.lower() or "home base" in label.lower():
            v = value.strip().upper()
            if v:
                return v.split()[0]
    return "KTM"


def _price_band(price: Any) -> str:
    """Bucket a NPR price into a coarse cheap/mid/premium band (None → '')."""
    if price is None:
        return ""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return ""
    for ceiling, name in _PRICE_BANDS:
        if p < ceiling:
            return name
    return "premium"


def _price_from_subtitle(subtitle: Any) -> float | None:
    """Pull the NPR amount out of a 'Rs 1,299' style subtitle (None if absent)."""
    m = re.search(r"([\d,]+)", str(subtitle or ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _coarsen_feedback_tags(tags: list[str] | None) -> list[str]:
    """Normalise the tags posted with a thumb into COARSE taste keys: keep short cuisine/seed words,
    turn any 'Rs 1,299' price token into a cheap/mid/premium band, and drop long item-name-looking
    strings (so taste keys to the category, not one specific dish/product)."""
    out: list[str] = []
    for raw in (tags or []):
        t = str(raw or "").strip()
        if not t:
            continue
        price = _price_from_subtitle(t) if re.search(r"(?i)\brs\b|[\d,]{2,}", t) else None
        if price is not None:
            band = _price_band(price)
            if band:
                out.append(band)
            continue
        # keep short, category-like tokens; skip long free-text (likely an exact item name)
        if len(t) <= 24 and len(t.split()) <= 3:
            out.append(t.lower())
    seen: set[str] = set()
    return [t for t in out if not (t in seen or seen.add(t))]


def _coarse_tags(rail: str, c: dict[str, Any]) -> list[str]:
    """COARSE taste tags for a candidate: its cuisine/interest seed + a price band — NOT the exact
    item name. A thumb on one item then teaches the whole category (e.g. 'momo', 'mid')."""
    tags: list[str] = []
    seed = str(c.get("tag") or "").strip().lower()
    if seed:
        tags.append(seed)
    if rail == "food":
        cui = str(c.get("subtitle") or "").split("|")[0].strip().lower()
        if cui and cui != seed:
            tags.append(cui)
    else:  # deals / foryou carry a price in the subtitle ("Rs 1,299")
        band = _price_band(_price_from_subtitle(c.get("subtitle")))
        if band:
            tags.append(band)
    # de-dup, keep order
    seen: set[str] = set()
    return [t for t in tags if not (t in seen or seen.add(t))]


#: Airport buffer (hours) added to air time to estimate a flight's true door-to-door duration
#: (check-in, security, boarding, taxi, baggage, transfers). Labelled an ESTIMATE in the output.
_FLIGHT_AIRPORT_BUFFER_H = 3.0
#: Fallback domestic air time (hours) when depart/arrive times can't be parsed (~50 min hop).
_FLIGHT_DEFAULT_AIR_H = 50.0 / 60.0


def _parse_clock_minutes(value: Any) -> int | None:
    """Parse a clock string ('14:30', '2:30 PM', '1430') into minutes-since-midnight (None if not)."""
    s = str(value or "").strip()
    if not s:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?", s)
    if not m:
        m4 = re.fullmatch(r"(\d{2})(\d{2})", s)  # bare HHMM
        if not m4:
            return None
        hh, mm, ap = int(m4.group(1)), int(m4.group(2)), None
    else:
        hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap:
        ap = ap.lower()
        if ap == "pm" and hh != 12:
            hh += 12
        elif ap == "am" and hh == 12:
            hh = 0
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return hh * 60 + mm


def _flight_air_hours(cheapest: dict[str, Any]) -> float:
    """Estimate the in-air hop length from the flight's depart/arrive clock times; fall back to the
    typical domestic ~50 min when they can't be parsed. ALWAYS only an estimate of the air leg."""
    dep = _parse_clock_minutes(cheapest.get("depart"))
    arr = _parse_clock_minutes(cheapest.get("arrive"))
    if dep is not None and arr is not None:
        diff = (arr - dep) % (24 * 60)  # handle wrap past midnight
        if 10 <= diff <= 300:           # sane domestic range (10 min–5 h)
            return diff / 60.0
    return _FLIGHT_DEFAULT_AIR_H


def _fmt_duration(hours: float) -> str:
    """Human duration label: '~50 min' under an hour, else '2h' / '2h 30m'."""
    total_min = int(round(hours * 60))
    if total_min < 60:
        return f"~{total_min} min"
    h, m = divmod(total_min, 60)
    return f"{h}h" if m == 0 else f"{h}h {m}m"


def _parse_travel_date(date: str | None) -> tuple[datetime.date | None, str]:
    """Validate a ``YYYY-MM-DD`` travel date for a LIVE fare lookup (flights / buses).

    Unlike :meth:`DoConcierge._parse_trip_date` (which silently *defaults* a bad/empty date so the
    planner always produces something), this is the strict gate the booking-style endpoints use:
    it returns ``(date, "")`` only for a parseable date that is neither in the past nor beyond the
    ~330-day booking horizon, and otherwise ``(None, message)`` with a friendly reason. An empty
    string is treated as "no date supplied" (``(None, "")``) so callers can apply their own default.
    """
    s = (date or "").strip()
    if not s:
        return None, ""
    try:
        parsed = datetime.date.fromisoformat(s[:10])
    except (TypeError, ValueError):
        return None, "Please give the date as YYYY-MM-DD."
    today = datetime.date.today()
    if parsed < today:
        return None, "That date is in the past — pick a date from today onwards."
    if parsed > today + datetime.timedelta(days=_MAX_BOOK_HORIZON_DAYS):
        return None, "That date is too far ahead to book — try a date within the next ~11 months."
    return parsed, ""


def _round_trip_fare(leg: dict[str, Any] | None) -> tuple[int | None, bool]:
    """Return ``(round_trip_fare_npr, is_round_trip)`` for a flight/bus getting-there leg.

    Prefers the connector's authoritative ``round_trip_total_npr`` (cheapest out + cheapest back).
    Falls back, in order, to: cheapest-out + cheapest-return when both legs are present; otherwise
    the one-way ``cheapest`` fare DOUBLED as an estimate (so a comparison can still be drawn). The
    second element flags whether a true return leg was used (vs a doubled one-way estimate). The
    fare is ``None`` only when even the one-way fare can't be parsed — the caller treats that as a
    missing leg and skips the comparison. NO network or model calls.
    """
    if not isinstance(leg, dict):
        return None, False

    def _as_int(value: Any) -> int | None:
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return None

    # 1) connector-computed round-trip total (cheapest out + cheapest back) — most trustworthy.
    total = _as_int(leg.get("round_trip_total_npr"))
    if total is not None:
        return total, True
    out_fare = _as_int((leg.get("cheapest") or {}).get("fare_npr"))
    # 2) compose it ourselves if a return leg came back.
    ret_fare = _as_int((leg.get("return_cheapest") or {}).get("fare_npr"))
    if out_fare is not None and ret_fare is not None:
        return out_fare + ret_fare, True
    # 3) honest fallback: double the one-way fare (no real return data).
    if out_fare is not None:
        return out_fare * 2, False
    return None, False


def _transport_compare(getting_there: dict[str, Any] | None,
                       by_bus: dict[str, Any] | None) -> dict[str, Any] | None:
    """Deterministic fly-vs-bus comparison built ONLY from already-fetched flight + bus data.

    Returns ``None`` unless BOTH a flight and a bus exist. Fares are ROUND-TRIP totals (cheapest
    outbound + cheapest return, via :func:`_round_trip_fare`) so the two options compare like for
    like; when a leg has no real return data its one-way fare is doubled as an estimate (flagged in
    ``fare_is_estimate``). The flight's ``duration_label`` is an ESTIMATE (air time + ~3h airport
    buffer) and is flagged ``duration_is_estimate``; the bus uses the connector's REAL
    ``journey_hours``. No network or model calls.
    """
    if not (getting_there and by_bus):
        return None
    fc = getting_there.get("cheapest") or {}
    bc = by_bus.get("cheapest") or {}

    flight_fare_i, flight_rt_real = _round_trip_fare(getting_there)
    bus_fare_i, bus_rt_real = _round_trip_fare(by_bus)
    if flight_fare_i is None or bus_fare_i is None:
        return None

    # Flight door-to-door is an ESTIMATE: in-air hop + the airport buffer.
    flight_hours = _flight_air_hours(fc) + _FLIGHT_AIRPORT_BUFFER_H
    # Bus uses the connector's REAL journey_hours (fall back to its label only if absent).
    try:
        bus_hours = float(bc.get("journey_hours"))
    except (TypeError, ValueError):
        bus_hours = 0.0

    flight_opt = {
        "mode": "flight", "label": "Flight (Buddha Air)", "fare_npr": flight_fare_i,
        "fare_is_round_trip": True, "fare_is_estimate": not flight_rt_real,
        "duration_label": _fmt_duration(flight_hours), "duration_is_estimate": True,
        "depart": fc.get("depart"), "book_link": getting_there.get("booking_link"),
    }
    bus_opt = {
        "mode": "bus", "label": "Bus (bussewa)", "fare_npr": bus_fare_i,
        "fare_is_round_trip": True, "fare_is_estimate": not bus_rt_real,
        "duration_label": (_fmt_duration(bus_hours) if bus_hours > 0 else "—"),
        "duration_is_estimate": False,
        "depart": bc.get("depart"), "book_link": by_bus.get("booking_link"),
    }

    fare_delta = abs(flight_fare_i - bus_fare_i)
    # Verdict: the flight wins on time, the bus on price. Pick the time winner as the default
    # "winner" (most people fly to save the day) but be honest about the price trade-off. Fares are
    # round-trip, so the "saves a day" framing covers both legs.
    if bus_hours > 0 and flight_hours < bus_hours:
        winner = "flight"
        saved_h = bus_hours - flight_hours
        reason = (f"Flying saves about {_fmt_duration(saved_h)} each way door-to-door for "
                  f"NPR {fare_delta:,} more round-trip.")
        time_note = (f"Flight ~{_fmt_duration(flight_hours)} door-to-door each way (estimate) vs "
                     f"bus {_fmt_duration(bus_hours)}.")
    else:
        winner = "bus"
        reason = (f"The bus is NPR {fare_delta:,} cheaper round-trip"
                  + (" with comparable time." if bus_hours > 0 else "."))
        time_note = (f"Bus {_fmt_duration(bus_hours)} vs flight ~{_fmt_duration(flight_hours)} "
                     f"door-to-door each way (estimate)." if bus_hours > 0
                     else "Bus journey time unavailable.")

    return {
        "options": [flight_opt, bus_opt],
        "verdict": {"winner": winner, "reason": reason, "fare_delta_npr": fare_delta,
                    "time_note": time_note, "fares_are_round_trip": True},
        "disclaimer": ("Fares are round-trip (cheapest outbound + return); flight time is "
                       "door-to-door estimate incl. airport buffer."),
    }


# --------------------------------------------------------------------------------------------
# the concierge
# --------------------------------------------------------------------------------------------
class DoConcierge:
    def __init__(self, config: HimmyConfig | None = None) -> None:
        self.cfg = config or load_config()
        self._cache = self.cfg.data_dir / "do_cache.json"
        self.fb = DoFeedback(self.cfg)
        self._refreshing = False
        self._air_cache: dict[str, str] = {}  # smart airport resolutions (text → code)

    # ---- public: the board, served warm with a background refresh ---------------------------
    async def board(self, *, force: bool = False) -> dict[str, Any]:
        cached = self._read_cache()
        # A suppressed/failed write can leave a None or partial cache — guard every field read so
        # a serving read never TypeErrors on cached["iso"]/cached["board"].
        valid = bool(cached and isinstance(cached.get("board"), dict))
        fresh = valid and (time.time() - cached.get("generated_at", 0)) < _TTL
        if valid and fresh and not force:
            return {**cached["board"], "stale": False, "generated_at": cached.get("iso")}
        if force:
            board = await self._generate(ai=True)
            self._write_cache(board)
            return {**board, "stale": False, "generated_at": self._cache_iso()}
        # No cache, or stale: build the free rules board NOW (instant), refresh the AI behind it.
        if valid:
            self._spawn_refresh()
            return {**cached["board"], "stale": True, "generated_at": cached.get("iso")}
        board = await self._generate(ai=False)        # cold start: free + fast, no model
        self._write_cache(board)
        self._spawn_refresh()                          # warm the AI layer in the background
        return {**board, "stale": True, "generated_at": self._cache_iso()}

    def feedback(self, kind: str, key: str, rail: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        # Store taste against COARSE tags (cuisine + cheap/mid/premium price band), never the exact
        # item name, so a single thumb teaches the whole category rather than one disposable row.
        out = self.fb.record(kind, key, rail, _coarsen_feedback_tags(tags))
        self._spawn_refresh()  # let the next board reflect the new taste
        return out

    # ---- permissions: the Concierge respects Settings → Permissions too --------------------
    def _on(self, key: str) -> bool:
        try:
            from himmy_app import permissions

            return permissions.level_of(key, self.cfg) != "off"
        except Exception:  # noqa: BLE001 - if permissions can't load, default to allowed
            return True

    async def _none(self) -> list[dict[str, Any]]:
        return []

    async def _rail_guarded(self, coro: Any) -> list[dict[str, Any]]:
        """Run one rail with a hard ceiling so a single hung connector can't stall the whole board.

        The cap sits ABOVE the slowest warm path (a ~25s connector + the flights rail's parallel
        Buddha Air calls) so a healthy-but-slow rail is never trimmed — only a truly stuck one is
        dropped, returning ``[]`` so the rest of the board still renders.
        """
        try:
            return await asyncio.wait_for(coro, timeout=_RAIL_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            return []

    # ---- restaurant detail: the full menu + dishes recommended for the user ------------------
    def _food_pref_tokens(self) -> set[str]:
        """Words from the user's saved favourite foods/cuisines (e.g. {'momo','pizza','sekuwa'})."""
        toks: set[str] = set()
        for label, value in _vault(self.cfg).items():
            if any(h in label.lower() for h in _FOOD_VAULT_HINTS):
                for part in re.split(r"[,/;]| and ", value):
                    for w in part.split():
                        if len(w) >= 3:
                            toks.add(w.strip().lower())
        return toks

    async def restaurant_detail(self, vendor_id: str = "", name: str = "") -> dict[str, Any]:
        if not self._on("food"):
            return {"ok": False, "message": "Food (Foodmandu) is turned off in Settings → Permissions."}
        from himmy_app.connectors.foodmandu import foodmandu_menu

        menu = await foodmandu_menu({"vendor_id": vendor_id, "restaurant": name})
        if not menu.get("ok"):
            return menu
        tokens = self._food_pref_tokens()
        recommended: list[dict[str, Any]] = []
        all_items: list[dict[str, Any]] = []
        for cat in menu["categories"]:
            for it in cat["items"]:
                # Normalise punctuation so a token like "momo" matches a dish named "Mo:Mo".
                hay = re.sub(r"[^a-z0-9 ]", "", f"{it.get('name', '')} {it.get('desc', '')} "
                             f"{cat.get('category', '')}".lower())
                match = any(t in hay for t in tokens)
                it["recommended"] = match
                enriched = {**it, "category": cat["category"], "_pref": match}
                all_items.append(enriched)
                if match or it.get("popular"):
                    recommended.append(enriched)
        recommended.sort(key=lambda x: (not x["_pref"], not x.get("popular"), float(x.get("price") or 1e9)))
        # Fallback so the section is never empty: the cheapest handful of dishes.
        if not recommended:
            recommended = sorted(all_items, key=lambda x: float(x.get("price") or 1e9))[:6]
        return {**menu, "recommended": recommended[:8]}

    # ---- flight tickets: live Buddha Air fares for a route + date ----------------------------
    async def _resolve_airport(self, text: str) -> str:
        """Map whatever the user typed to a Buddha Air airport code — deterministically first,
        then via Himmy (the model) for misspellings/alt-names it doesn't know. Cached."""
        text = (text or "").strip()
        if not text:
            return ""
        from himmy_app.connectors.buddha_air import _resolve, sector_options

        # 1) exact / substring match against the live sector list
        code = _resolve(text)
        if code:
            return code
        # 2) fast alias table for common Nepali spellings / alternate names
        alias = _CITY_ALIASES.get(text.lower())
        if alias and (code := _resolve(alias)):
            return code
        # 3) smart routing via Himmy — only when the cheap paths miss
        key = text.lower()
        if key in self._air_cache:
            return self._air_cache[key]
        opts = await asyncio.to_thread(sector_options)
        codes = {c.upper() for c, _ in opts}
        resolved = ""
        if opts:
            listing = "; ".join(f"{name} = {c}" for c, name in opts)
            try:
                from himmy.cli.provider import build_inference_for
                from himmy.services.inference.models import InferenceMessage, InferenceRequest

                svc = build_inference_for(self.cfg.provider, self.cfg.model)
                resp = await svc.run(InferenceRequest(
                    messages=[
                        InferenceMessage(role="system", content=(
                            "You map a Nepali place the user typed to its Buddha Air airport CODE. "
                            "Reply with ONLY the 3-letter code from the provided list, or NONE if there is "
                            "no reasonable match. Handle misspellings and alternate/local names "
                            "(e.g. Bhairawa/Siddharthanagar/Lumbini -> Bhairahawa; Chitwan/Narayangarh -> "
                            "Bharatpur; KTM -> Kathmandu).")),
                        InferenceMessage(role="user", content=f"Airports: {listing}\n\nUser typed: {text!r}\nCode:"),
                    ],
                    generation_params={"temperature": 0.0}, timeout_seconds=30.0,
                ))
                m = re.search(r"[A-Za-z]{3}", resp.output_text or "")
                if m and m.group(0).upper() in codes:
                    resolved = m.group(0).upper()
            except Exception:  # noqa: BLE001 - smart routing is best-effort
                resolved = ""
        # Only cache a POSITIVE resolution — caching the empty miss would poison the entry and
        # stop a later (transient-failure) retry from ever resolving. Keep the cache bounded.
        if resolved:
            if len(self._air_cache) >= _AIR_CACHE_MAX:
                self._air_cache.pop(next(iter(self._air_cache)), None)
            self._air_cache[key] = resolved
        return resolved

    async def flights(self, origin: str, destination: str, date: str = "",
                      return_date: str = "") -> dict[str, Any]:
        if not self._on("flights"):
            return {"ok": False, "flights": [], "from": origin, "to": destination, "date": date,
                    "message": "Flights (Buddha Air) is turned off in Settings → Permissions."}
        from himmy_app.connectors.buddha_air import buddha_air_flights

        # Bound the outbound date: reject a past / malformed / too-far-ahead date (a live fare lookup
        # for an un-sellable date is just empty/confusing). An empty date defaults to today+8.
        depart_d, msg = _parse_travel_date(date)
        if msg:
            return {"ok": False, "flights": [], "from": origin, "to": destination, "date": date,
                    "message": msg}
        if depart_d is None:
            depart_d = datetime.date.today() + datetime.timedelta(days=8)
        date = depart_d.isoformat()
        # Validate the return leg the same way AND enforce the depart<=return invariant trip() gets
        # for free — a return before the outbound would yield a nonsensical "inbound precedes
        # outbound" quote. On any problem we simply drop the return leg and quote one-way.
        ret = ""
        if (return_date or "").strip():
            ret_d, ret_msg = _parse_travel_date(return_date)
            if ret_msg:
                return {"ok": False, "flights": [], "from": origin, "to": destination, "date": date,
                        "message": f"Return date: {ret_msg}"}
            if ret_d is not None:
                if ret_d < depart_d:
                    return {"ok": False, "flights": [], "from": origin, "to": destination,
                            "date": date,
                            "message": "Return date must be on or after the departure date."}
                ret = ret_d.isoformat()
        # Smart-route both endpoints first (so "Bhairawa", "Lumbini", typos all work).
        o = await self._resolve_airport(origin)
        d = await self._resolve_airport(destination)
        req: dict[str, Any] = {"origin": o or origin, "destination": d or destination, "date": date}
        # When a return date is given the connector parses data.inbound too and adds the round-trip
        # fields (round_trip / return_flights / return_cheapest / round_trip_total_npr); one-way is
        # unchanged when it's absent.
        if ret:
            req["return_date"] = ret
        return await buddha_air_flights(req)

    async def buses(self, origin: str, destination: str, date: str = "") -> dict[str, Any]:
        if not self._on("buses"):
            return {"ok": False, "buses": [], "from": origin, "to": destination, "date": date,
                    "message": "Buses (bussewa) is turned off in Settings → Permissions."}
        from himmy_app.connectors.bussewa import bussewa_buses

        # Bound the date the same way flights does: a past / malformed / too-far-ahead date can't be
        # sold, so reject it with a friendly message rather than fetch an empty result. Empty → today+3.
        travel_d, msg = _parse_travel_date(date)
        if msg:
            return {"ok": False, "buses": [], "from": origin, "to": destination, "date": date,
                    "message": msg}
        if travel_d is None:
            travel_d = datetime.date.today() + datetime.timedelta(days=3)
        date = travel_d.isoformat()
        return await bussewa_buses({"origin": origin, "destination": destination, "date": date})

    # ---- trips: a day-by-day roadmap of places/activities (grounded in real OSM spots) -------
    async def _geocode(self, place: str) -> tuple[float, float] | None:
        """Resolve a place name to coords via OpenStreetMap Nominatim (keyless, best-effort).

        The ``place`` here is model/agent-controlled (the weather_forecast tool routes through this),
        so the fetch goes through the SAME guarded helper every connector uses — SSRF/redirect/
        content-type/size guards plus a fixed host allow-list — rather than a raw httpx client.
        """
        try:
            d = await _net.safe_get_json(
                "https://nominatim.openstreetmap.org/search",
                params={"q": place, "format": "json", "limit": 1},
                headers={"User-Agent": "HimmyApp/1.0 (concierge)"},
                allow_hosts=("nominatim.openstreetmap.org",),
                timeout=15,
                max_bytes=1_000_000,
            )
            if d:
                return float(d[0]["lat"]), float(d[0]["lon"])
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _osm_places(self, lat: float, lon: float) -> dict[str, list[dict[str, Any]]]:
        """ONE Overpass call → real attractions, hotels and restaurants near a point (keyless).
        Combined into a single request to stay polite to the free server (no rate-limit storms).

        Goes through the guarded POST helper (fixed host allow-list + SSRF/redirect/content-type/
        size caps) for the same reason :meth:`_geocode` does."""
        q = (f'[out:json][timeout:30];('
             f'node["tourism"~"attraction|viewpoint|museum|theme_park"](around:8000,{lat},{lon});'
             f'node["historic"](around:8000,{lat},{lon});'
             f'node["natural"="peak"](around:12000,{lat},{lon});'
             f'node["tourism"~"hotel|guest_house|hostel|resort"](around:7000,{lat},{lon});'
             f'way["tourism"~"hotel|guest_house|hostel|resort"](around:7000,{lat},{lon});'
             f'node["amenity"="restaurant"](around:6000,{lat},{lon}););out tags 260;')
        attractions: list[dict[str, Any]] = []
        hotels: list[dict[str, Any]] = []
        restaurants: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            resp = await _net.safe_post_json(
                "https://overpass-api.de/api/interpreter",
                data={"data": q},
                headers={"User-Agent": "HimmyApp/1.0 (concierge)"},
                allow_hosts=("overpass-api.de",),
                timeout=40,
                max_bytes=10_000_000,
            )
            for e in (resp.get("elements", []) if isinstance(resp, dict) else []):
                t = e.get("tags", {})
                name = (t.get("name") or "").strip()
                if not name or name.isdigit() or name.lower() in seen:
                    continue
                seen.add(name.lower())
                tourism = t.get("tourism", "")
                area = (t.get("addr:suburb") or t.get("addr:neighbourhood") or t.get("addr:street") or "").strip()
                if tourism in ("hotel", "guest_house", "hostel", "resort"):
                    hotels.append({"name": name, "type": "guesthouse" if tourism == "guest_house" else tourism,
                                   "stars": str(t.get("stars") or "").strip(), "area": area})
                elif t.get("amenity") == "restaurant":
                    restaurants.append({"name": name, "cuisine": (t.get("cuisine") or "").replace("_", " ")})
                else:
                    attractions.append({"name": name, "kind": tourism or t.get("historic")
                                        or t.get("natural") or "spot"})
        except Exception:  # noqa: BLE001 - best-effort; the model can plan on its own knowledge
            pass
        return {"attractions": attractions[:25], "hotels": hotels[:30], "restaurants": restaurants[:25]}

    @staticmethod
    def _hotel_book_link(name: str, dest: str) -> str:
        from urllib.parse import quote_plus

        return f"https://www.booking.com/searchresults.html?ss={quote_plus(f'{name}, {dest}')}"

    @staticmethod
    def _weather_brief(weather: dict[str, Any] | None) -> str:
        """A COMPACT, planner-ready weather brief (a few short lines) the itinerary can adapt to.

        Honest by construction — it forwards exactly what :mod:`himmy_app.weather` reports: the real
        per-day chips when the dates are inside the ~16-day forecast window, otherwise just the
        season line (never a fabricated daily forecast). Returns ``""`` when no usable weather is
        available so the planner simply omits the weather-shaping instruction.
        """
        if not isinstance(weather, dict):
            return ""
        season = str(weather.get("season") or "").strip()
        summary = str(weather.get("summary") or "").strip()
        daily = weather.get("daily") if isinstance(weather.get("daily"), list) else []
        in_window = bool(weather.get("in_forecast_window"))
        lines: list[str] = []
        if season:
            lines.append(f"Season: {season}")
        if in_window and daily:
            chips: list[str] = []
            for d in daily[:7]:
                if not isinstance(d, dict):
                    continue
                date = str(d.get("date") or "").strip()
                label = str(d.get("label") or "").strip()
                try:
                    hi = round(float(d.get("t_max")))
                    lo = round(float(d.get("t_min")))
                except (TypeError, ValueError):
                    hi = lo = None  # type: ignore[assignment]
                try:
                    rain = int(d.get("rain_pct"))
                except (TypeError, ValueError):
                    rain = None  # type: ignore[assignment]
                bits = [date] if date else []
                if label:
                    bits.append(label)
                if hi is not None and lo is not None:
                    bits.append(f"{hi}/{lo}°C")
                if rain is not None:
                    bits.append(f"rain {rain}%")
                if bits:
                    chips.append(" ".join(bits))
            if chips:
                lines.append("Daily forecast (real): " + "; ".join(chips))
        elif summary:
            # Out of the forecast window: forward the honest seasonal summary, not a fake forecast.
            lines.append(summary)
        return "\n".join(lines)

    async def trip(self, destination: str, days: int = 2, style: str = "comfort",
                   date: str | None = None, round_trip: bool = True) -> dict[str, Any]:
        destination = (destination or "").strip()
        if not destination:
            return {"ok": False, "message": "Where would you like to go?"}
        days = max(1, min(int(days or 2), 7))
        style = style if style in ("budget", "comfort", "luxury") else "comfort"
        # DEPARTURE date. Default to today+7 so it sits inside the ~16-day forecast window and the
        # weather we attach is a REAL forecast, not just a seasonal guess. The RETURN date is the
        # departure plus the trip length, so both travel legs and the forecast cover the whole stay.
        depart_d = self._parse_trip_date(date)
        return_d = depart_d + datetime.timedelta(days=days)
        depart_iso, return_iso = depart_d.isoformat(), return_d.isoformat()
        # cache_key carries the date (+ round-trip flag) so different departure dates / trip kinds
        # never collide on a stale plan (the weather + fares are date-specific).
        cache_key = f"{destination.lower()}|{days}|{style}|{depart_iso}|{'rt' if round_trip else 'ow'}"
        cached = self._trip_cache().get(cache_key)
        if cached:
            return cached
        # Ground the plan in REAL local spots/hotels/restaurants so Himmy curates rather than invents.
        geo = await self._geocode(destination)
        places = await self._osm_places(*geo) if geo else {"attractions": [], "hotels": [], "restaurants": []}
        # Getting there — a Buddha Air flight from the user's home airport (also feeds the budget).
        # Fetched ROUND-TRIP (pass return_date) when round_trip is on, so the trip carries both legs
        # and a round_trip_total; the budget/compare then price the whole journey, not one way.
        getting_there, flight_fare = None, None
        if self._on("flights"):
            code = await self._resolve_airport(destination)
            home = _home_airport(_vault(self.cfg))
            if code and code != home:
                try:
                    from himmy_app.connectors.buddha_air import buddha_air_flights

                    req: dict[str, Any] = {"origin": home, "destination": code, "date": depart_iso}
                    if round_trip:
                        req["return_date"] = return_iso
                    fr = await buddha_air_flights(req)
                    if fr.get("ok") and fr.get("cheapest"):
                        flight_fare = fr["cheapest"].get("fare_npr")
                        getting_there = {
                            "from": home, "to": code, "cheapest": fr["cheapest"],
                            "booking_link": fr.get("booking_link"),
                            "round_trip": bool(fr.get("round_trip")),
                            "return_date": fr.get("return_date"),
                            "return_flights": fr.get("return_flights") or [],
                            "return_cheapest": fr.get("return_cheapest"),
                            "round_trip_total_npr": fr.get("round_trip_total_npr"),
                        }
                except Exception:  # noqa: BLE001
                    pass
        # Getting there by BUS — covers routes flights don't (Chitwan, Lumbini) and budget travel.
        # Fetch the RETURN bus leg too so the ground option is round-trip like the flight; missing
        # one direction must never break the plan (the return leg is purely additive).
        by_bus, bus_fare = None, None
        if self._on("buses"):
            try:
                from himmy_app.connectors.bussewa import bussewa_buses

                vault = _vault(self.cfg)
                home_city = (vault.get("Home city") or vault.get("home_city") or "Kathmandu").strip()
                br = await bussewa_buses({"origin": home_city, "destination": destination,
                                          "date": depart_iso})
                if br.get("ok") and br.get("cheapest"):
                    bus_fare = br["cheapest"].get("fare_npr")
                    by_bus = {"from": br.get("from"), "to": br.get("to"), "cheapest": br["cheapest"],
                              "count": br.get("count"), "via": br.get("via"),
                              "booking_link": br.get("booking_link")}
                    if round_trip:
                        # The return bus leg (best-effort): reverse the route on the return date.
                        try:
                            rb = await bussewa_buses({"origin": destination, "destination": home_city,
                                                      "date": return_iso})
                            if rb.get("ok") and rb.get("cheapest"):
                                ret_fare = rb["cheapest"].get("fare_npr")
                                by_bus.update({
                                    "round_trip": True, "return_date": return_iso,
                                    "return_cheapest": rb["cheapest"],
                                    "return_count": rb.get("count"),
                                })
                                try:
                                    by_bus["round_trip_total_npr"] = int(round(
                                        float(bus_fare) + float(ret_fare)))
                                except (TypeError, ValueError):
                                    pass
                        except Exception:  # noqa: BLE001 - the return leg is additive, never required
                            pass
            except Exception:  # noqa: BLE001
                pass
        # WEATHER — reuse the SAME geocoded lat/lon we already fetched for OSM places (no extra
        # geocode) and ask for a forecast spanning the stay. Honest + graceful: out-of-window dates
        # come back as a seasonal-only forecast, and any failure returns a well-formed {"ok": False}.
        weather: dict[str, Any] | None = None
        if geo:
            try:
                from himmy_app import weather as weather_mod

                weather = await weather_mod.forecast(geo[0], geo[1], start=depart_iso, end=return_iso)
            except Exception:  # noqa: BLE001 - missing weather must NEVER break the plan
                weather = None
        weather_brief = self._weather_brief(weather)
        # Round-trip fares for the budget line — so "Stay/Food + Travel" prices the whole journey.
        flight_rt_fare = _round_trip_fare(getting_there)[0] if getting_there else None
        bus_rt_fare = _round_trip_fare(by_bus)[0] if by_bus else None
        plan = await self._plan_trip(destination, days, style, places, flight_rt_fare, bus_rt_fare,
                                     weather_brief=weather_brief)
        if not plan:
            return {"ok": False, "message": "Couldn't build a plan just now — try again."}
        for h in plan.get("hotels", []):  # a booking deep-link for each picked hotel
            h["book_link"] = self._hotel_book_link(str(h.get("name") or ""), destination)
        out = {"ok": True, "destination": destination, "days": days, "style": style,
               "date": depart_iso, "return_date": return_iso, "round_trip": bool(round_trip),
               "getting_there": getting_there, "by_bus": by_bus, "weather": weather, **plan}
        # Deterministic fly-vs-bus compare (reuses what we already fetched — no extra calls). Only
        # present when BOTH a flight and a bus exist; totals are ROUND-TRIP and the flight duration
        # is an explicit ESTIMATE.
        out["transport_compare"] = _transport_compare(getting_there, by_bus)
        self._trip_cache_write(cache_key, out)
        return out

    @staticmethod
    def _parse_trip_date(date: str | None) -> datetime.date:
        """The trip's DEPARTURE date. Parses a ``YYYY-MM-DD`` string; an absent/invalid/past date
        defaults to today+7 so the dates sit inside the ~16-day forecast window (real weather). An
        absurd far-future date (beyond the ~330-day booking horizon, e.g. year 9999) is CLAMPED to
        the horizon so the downstream live-fare lookups stay meaningful instead of driving calls for
        dates the providers can't sell."""
        today = datetime.date.today()
        default = today + datetime.timedelta(days=7)
        s = (date or "").strip()
        if not s:
            return default
        try:
            parsed = datetime.date.fromisoformat(s[:10])
        except (TypeError, ValueError):
            return default
        # Never plan into the past — a stale date would also fall outside the forecast horizon.
        if parsed < today:
            return default
        # Cap absurd far-future dates at the booking horizon so fares stay sellable.
        horizon = today + datetime.timedelta(days=_MAX_BOOK_HORIZON_DAYS)
        return parsed if parsed <= horizon else horizon

    async def _plan_trip(self, destination: str, days: int, style: str,
                         places: dict[str, list[dict[str, Any]]], flight_fare: float | None,
                         bus_fare: float | None = None,
                         weather_brief: str = "") -> dict[str, Any] | None:
        from himmy.cli.provider import build_inference_for
        from himmy.services.inference.models import InferenceMessage, InferenceRequest

        # PRIVACY: pass only non-identifying planning signals (diet, budget) — NOT the full
        # free-text profile — because this plan is exported/shared (see _trip_plan_signals).
        profile = _trip_plan_signals(_vault(self.cfg))
        attr = "; ".join(f"{p['name']} ({p['kind']})" for p in places["attractions"]) or "(use your own knowledge)"
        hotel_lines = "; ".join(
            f"{h['name']} [{h['type']}{', ' + h['stars'] + '★' if h.get('stars') else ''}"
            f"{', ' + h['area'] if h.get('area') else ''}]" for h in places["hotels"]) or "(none found)"
        rest_lines = "; ".join(f"{r['name']}{' (' + r['cuisine'] + ')' if r.get('cuisine') else ''}"
                               for r in places["restaurants"]) or "(none found)"
        # The fares handed in are already ROUND-TRIP totals (cheapest out + cheapest back, computed
        # by the caller) — use them directly for the travel line; do NOT double them again.
        if flight_fare:
            fare_note = (f"Use ~NPR {int(flight_fare)} for the round-trip TRAVEL budget line "
                         f"(Buddha Air flight, return included).")
            if bus_fare:
                fare_note += (f" A cheaper round-trip bus is also available (~NPR {int(bus_fare)}, bussewa) — "
                              f"if the style is 'budget', use the bus fare for the travel line instead.")
        elif bus_fare:
            fare_note = (f"No flight is available — use ~NPR {int(bus_fare)} for the round-trip TRAVEL "
                         f"budget line (bus, bussewa, return included).")
        else:
            fare_note = "No flight/bus fare available — omit the travel line or set it to 0."
        # Weather shaping: when we have a (real or seasonal) brief, tell the planner to ADAPT the
        # itinerary to it and ALWAYS add a packing tip. Honest — the brief only carries a real daily
        # forecast when the dates are inside the ~16-day window (else it's the season line only).
        if weather_brief.strip():
            weather_note = (
                "\n\nWeather for the trip dates (adapt the plan to it):\n"
                f"{weather_brief.strip()}\n"
                "ADAPT the itinerary to this weather: on rainy/wet days favour INDOOR or cultural "
                "options (museums, temples, cafes, markets); on clear mornings front-load outdoor "
                "highlights (a sunrise viewpoint, a hike, a lake). ALWAYS include one weather-aware "
                "PACKING tip in 'tips' (e.g. a rain layer for showers, warm layers for cold mornings, "
                "sun protection for clear hot days)."
            )
        else:
            # No usable weather — still guarantee a packing tip so the plan always carries one.
            weather_note = ("\n\nNo specific weather is available; still include one sensible PACKING "
                            "tip in 'tips' for this destination and season.")
        system = (
            "You are Himmy, a sharp Nepal travel planner. Produce a PREMIUM, realistic plan shaped by the "
            "traveller's STYLE (budget/comfort/luxury) — style drives the hotel choice, the budget, and the "
            "pace. GROUND everything in the real data given: pick HOTELS only from the hotel list and EAT "
            "spots only from the restaurant list (you may add a famous attraction from your own knowledge). "
            "When weather for the trip dates is provided, ADAPT the day plan to it (indoor/cultural on wet "
            "days, outdoor highlights on clear mornings) and ALWAYS add a weather-aware packing tip. "
            "Estimate costs from typical Nepal prices and give realistic NPR ranges. Reply with ONLY JSON: {"
            '"summary":"<one warm sentence>",'
            '"budget":{"currency":"NPR","per_person":true,"total_min":<int>,"total_max":<int>,'
            '"breakdown":[{"label":"<Flights|Stay|Food|Activities|Local transport>","min":<int>,"max":<int>,'
            '"note":"<short optional>"}]},'
            '"hotels":[{"name":"<from list>","type":"<hotel|guesthouse|hostel|resort>","area":"<short>",'
            '"why":"<one sentence matched to the style>"}],'
            '"eat":[{"name":"<from list>","cuisine":"<short>","why":"<one sentence>"}],'
            '"itinerary":[{"day":1,"title":"<short theme>","items":[{"name":"<place/activity>",'
            '"category":"<Nature|Culture|Food|Adventure|Relax|Shopping>","desc":"<one sentence>",'
            '"tip":"<short optional>"}]}],"tips":["<short practical tip>"]}. '
            "Pick 3 hotels and 3-4 eat spots that fit the style; 2-4 items per day; be specific and local."
        )
        user = (f"Destination: {destination}\nDays: {days}\nTravel style: {style}\n{fare_note}"
                f"{weather_note}\n\n"
                f"Hotels (real, OSM): {hotel_lines}\n\nRestaurants (real, OSM): {rest_lines}\n\n"
                f"Attractions (real, OSM): {attr}\n\nTraveller preferences (do NOT name the "
                f"traveller or restate these facts in your prose; just let them shape the plan):\n"
                f"{profile}\n\nBuild the plan.")
        try:
            svc = build_inference_for(self.cfg.provider, self.cfg.model)
            resp = await svc.run(InferenceRequest(
                messages=[InferenceMessage(role="system", content=system),
                          InferenceMessage(role="user", content=user)],
                generation_params={"temperature": 0.4}, timeout_seconds=95.0))
            data = _extract_json(resp.output_text or "")
            if isinstance(data, dict) and data.get("itinerary"):
                return {"summary": str(data.get("summary") or "").strip(),
                        "budget": data.get("budget") or {}, "hotels": data.get("hotels") or [],
                        "eat": data.get("eat") or [], "itinerary": data.get("itinerary") or [],
                        "tips": data.get("tips") or []}
        except Exception:  # noqa: BLE001
            pass
        return None

    def _trip_cache(self) -> dict[str, Any]:
        try:
            return json.loads((self.cfg.data_dir / "trips_cache.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _trip_cache_write(self, key: str, value: dict[str, Any]) -> None:
        cache = self._trip_cache()
        cache[key] = value
        # keep the cache small
        if len(cache) > 30:
            cache = dict(list(cache.items())[-30:])
        # Atomic write (temp + os.replace) so a kill/overlap can't truncate trips_cache.json.
        with contextlib.suppress(Exception):
            _net.atomic_write_text(self.cfg.data_dir / "trips_cache.json",
                                   json.dumps(cache, ensure_ascii=False))

    # ---- inline search over food (Foodmandu) + shopping (Daraz) ------------------------------
    async def search(self, query: str, kind: str = "food") -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"ok": False, "results": [], "message": "Type something to search."}
        surface = "shopping" if kind == "shop" else "food"
        if not self._on(surface):
            label = "Shopping (Daraz)" if kind == "shop" else "Food (Foodmandu)"
            return {"ok": False, "results": [], "message": f"{label} is turned off in Settings → Permissions."}
        if kind == "shop":
            from himmy_app.connectors.daraz import daraz_search

            res = await daraz_search({"query": query, "limit": 12})
            out = [{"key": p.get("product_link") or p.get("name"), "title": p.get("name"),
                    "subtitle": f"Rs {p.get('price_npr')}",
                    "was": f"Rs {p.get('original_price_npr')}" if p.get("discount") else None,
                    "discount": p.get("discount") or "", "rating": _rating(p.get("rating")),
                    "meta": p.get("sold") or "", "image": p.get("image") or "",
                    "link": p.get("product_link"), "why": ""} for p in res.get("products", [])]
            return {"ok": True, "kind": "shop", "query": query, "results": out}
        from himmy_app.connectors.foodmandu import _vendor_id_from_link, foodmandu_search

        res = await foodmandu_search({"query": query, "limit": 12})
        out = [{"key": r.get("order_link"),
                "vendor_id": _vendor_id_from_link(r.get("order_link") or ""),
                "title": r.get("name"), "subtitle": r.get("cuisine"),
                "rating": _rating(r.get("rating")), "open_now": bool(r.get("open_now")),
                "image": r.get("image") or "", "link": r.get("order_link"), "why": ""}
               for r in res.get("restaurants", [])]
        return {"ok": True, "kind": "food", "query": query, "results": out}

    # ---- candidate generation (free, deterministic, live) -----------------------------------
    async def _candidates(
        self, vault: dict[str, str]
    ) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
        """Return ``(rails, recent_down_tags)`` — the live candidate rails plus the coarse tags the
        user thumbed DOWN recently (threaded into the AI re-rank so it won't re-surface them)."""
        dismissed, weights, recent_down_tags = self.fb._signals_full()
        food_seeds = _seeds_from_vault(vault, _FOOD_VAULT_HINTS, _FOOD_SEEDS)
        deal_seeds = _seeds_from_vault(vault, _DEAL_VAULT_HINTS, _DEAL_SEEDS)
        shop_seeds = _seeds_from_vault(vault, _SHOP_VAULT_HINTS, _SHOP_SEEDS)
        # Skip any rail whose surface the user turned off in Settings → Permissions. Each rail is
        # wrapped in a per-rail timeout (_rail_guarded) so one hung connector can't stall the board.
        food, deals, foryou, flights = await asyncio.gather(
            self._rail_guarded(self._food(food_seeds, dismissed, weights)) if self._on("food") else self._none(),
            self._rail_guarded(self._deals(deal_seeds, dismissed, weights)) if self._on("shopping") else self._none(),
            self._rail_guarded(self._shop_foryou(shop_seeds, dismissed, weights)) if self._on("shopping") else self._none(),
            self._rail_guarded(self._flights(vault, dismissed)) if self._on("flights") else self._none(),
            return_exceptions=True,
        )
        rails = {
            "food": food if isinstance(food, list) else [],
            "deals": deals if isinstance(deals, list) else [],
            "foryou": foryou if isinstance(foryou, list) else [],
            "flights": flights if isinstance(flights, list) else [],
        }
        # Item-level taste: drop candidates whose COARSE tag was just thumbed down (not only the
        # exact dismissed key), so a freshly-dismissed category doesn't re-appear on this board.
        if recent_down_tags:
            for rail_name, items in rails.items():
                rails[rail_name] = [
                    c for c in items
                    if not (set(_coarse_tags(rail_name, c)) & recent_down_tags)
                ]
        return rails, recent_down_tags

    async def _shop_foryou(self, seeds: list[str], dismissed: set[str], weights: dict[str, int]) -> list[dict[str, Any]]:
        """Daraz items related to the user's interests — ranked by rating, NOT by discount."""
        from himmy_app.connectors.daraz import daraz_search

        results = await asyncio.gather(
            *[daraz_search({"query": s, "limit": 6, "sort": "popular"}) for s in seeds],
            return_exceptions=True,
        )
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for seed, res in zip(seeds, results):
            if not isinstance(res, dict):
                continue
            for p in res.get("products", []):
                key = p.get("product_link") or p.get("name")
                rating = _rating(p.get("rating"))
                if not key or key in seen or key in dismissed or rating < 3.8:
                    continue
                seen.add(key)
                out.append({
                    "key": key, "title": p.get("name"),
                    "subtitle": f"Rs {p.get('price_npr')}",
                    "was": f"Rs {p.get('original_price_npr')}" if p.get("discount") else None,
                    "discount": p.get("discount") or "", "rating": rating, "meta": p.get("sold") or "",
                    "image": p.get("image") or "", "link": p.get("product_link"), "tag": seed,
                    "_score": 4 * rating + 0.5 * weights.get(seed.lower(), 0),
                })
        out.sort(key=lambda x: x["_score"], reverse=True)
        return out[:8]

    async def _food(self, seeds: list[str], dismissed: set[str], weights: dict[str, int]) -> list[dict[str, Any]]:
        from himmy_app.connectors.foodmandu import foodmandu_search

        results = await asyncio.gather(
            *[foodmandu_search({"query": s, "limit": 5}) for s in seeds], return_exceptions=True
        )
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for seed, res in zip(seeds, results):
            if not isinstance(res, dict):
                continue
            for r in res.get("restaurants", []):
                key = r.get("order_link") or r.get("name")
                if not key or key in seen or key in dismissed:
                    continue
                seen.add(key)
                out.append({
                    "key": key, "title": r.get("name"), "subtitle": r.get("cuisine"),
                    "rating": _rating(r.get("rating")), "open_now": bool(r.get("open_now")),
                    "meta": r.get("hours") or r.get("distance") or "", "link": r.get("order_link"),
                    "image": r.get("image") or "", "tag": seed,
                    "_score": _rating(r.get("rating")) + (2.0 if r.get("open_now") else 0.0)
                              + 0.5 * weights.get(seed.lower(), 0),
                })
        out.sort(key=lambda x: x["_score"], reverse=True)
        return out[:8]

    async def _deals(self, seeds: list[str], dismissed: set[str], weights: dict[str, int]) -> list[dict[str, Any]]:
        from himmy_app.connectors.daraz import daraz_search

        results = await asyncio.gather(
            *[daraz_search({"query": s, "limit": 8}) for s in seeds], return_exceptions=True
        )
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for seed, res in zip(seeds, results):
            if not isinstance(res, dict):
                continue
            for p in res.get("products", []):
                key = p.get("product_link") or p.get("name")
                pct = _discount_pct(p.get("discount") or "")
                rating = _rating(p.get("rating"))
                # a "deal" must be a real discount on a decently-rated item, not junk.
                if not key or key in seen or key in dismissed or pct < 15 or rating < 3.8:
                    continue
                seen.add(key)
                out.append({
                    "key": key, "title": p.get("name"),
                    "subtitle": f"Rs {p.get('price_npr')}", "was": f"Rs {p.get('original_price_npr')}",
                    "discount": p.get("discount"), "rating": rating, "meta": p.get("sold") or "",
                    "image": p.get("image") or "", "link": p.get("product_link"), "tag": seed,
                    "_score": pct + 4 * rating + 0.5 * weights.get(seed.lower(), 0),
                })
        out.sort(key=lambda x: x["_score"], reverse=True)
        return out[:8]

    async def _flights(self, vault: dict[str, str], dismissed: set[str]) -> list[dict[str, Any]]:
        from himmy_app.connectors.buddha_air import buddha_air_flights

        home = _home_airport(vault)
        date = (datetime.date.today() + datetime.timedelta(days=8)).isoformat()
        targets = [t for t in ("KTM", "PKR", "BWA", "BIR", "BHR") if t != home][:3]
        routes = [(home, t) for t in targets] or [("KTM", "PKR")]
        results = await asyncio.gather(
            *[buddha_air_flights({"origin": o, "destination": d, "date": date}) for o, d in routes],
            return_exceptions=True,
        )
        out: list[dict[str, Any]] = []
        for (o, d), res in zip(routes, results):
            if not isinstance(res, dict):
                continue
            key = f"{o}-{d}"
            if key in dismissed:
                continue
            cheapest = res.get("cheapest") or {}
            fare = cheapest.get("fare_npr")
            out.append({
                "key": key, "title": f"{o} → {d}",
                "subtitle": (f"Rs {fare:,.0f}" if isinstance(fare, (int, float)) else "See fares"),
                "meta": (f"{cheapest.get('flight','')} · {cheapest.get('depart','')}".strip(" ·")
                         if cheapest else date),
                "fare_npr": fare, "date": date, "link": res.get("booking_link"), "tag": "flight",
                "_score": -(fare or 1e9),
            })
        out.sort(key=lambda x: x["_score"], reverse=True)
        return out

    # ---- the one cheap AI pass: re-rank + write the personal "why" ---------------------------
    async def _generate(self, *, ai: bool) -> dict[str, Any]:
        vault = _vault(self.cfg)
        cands, recent_down_tags = await self._candidates(vault)
        board = self._deterministic_board(cands)        # always have a free, complete board
        if not ai:
            return board
        try:
            enriched = await self._personalize(cands, vault, recent_down_tags)
            if enriched:
                board = enriched
        except Exception:  # noqa: BLE001 - the deterministic board is the fallback
            pass
        return board

    def _why_template(self, rail: str, c: dict[str, Any]) -> str:
        """A non-redundant secondary line (the badges already show rating / open / discount)."""
        if rail == "food":
            cui = str(c.get("subtitle") or "").split("|")[0].strip()
            return cui or ("Open now" if c.get("open_now") else "Opens later")
        if rail == "deals":
            sold = str(c.get("meta") or "").strip()
            return f"{sold} · popular pick" if sold else "Limited-time deal"
        if rail == "foryou":
            tag = str(c.get("tag") or "").strip()
            return f"Popular in {tag}" if tag else "Picked for your interests"
        return str(c.get("meta") or c.get("date") or "")  # flights: flight no · time

    def _assemble_rail(self, rail: str, cand_rail: list[dict[str, Any]],
                       ai_picks: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """AI-chosen items first (with their 'why'), then pad with the next-best candidates."""
        used: set[int] = set()
        out: list[dict[str, Any]] = []
        for item in (ai_picks or []):
            try:
                idx = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if idx in used or not (0 <= idx < len(cand_rail)):
                continue
            used.add(idx)
            why = str(item.get("why") or "").strip()
            out.append({**cand_rail[idx], "why": why or self._why_template(rail, cand_rail[idx]),
                        "ai": bool(why)})
        for i, c in enumerate(cand_rail):
            if len(out) >= _TARGETS[rail]:
                break
            if i in used:
                continue
            out.append({**c, "why": self._why_template(rail, c), "ai": False})
        return out[:_TARGETS[rail]]

    def _deterministic_board(self, cands: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        """A complete board with template 'why' lines — used cold and as the AI fallback."""
        return {
            "ok": True,
            "headline": "Here's what's good in Nepal right now.",
            "food": self._assemble_rail("food", cands["food"], None),
            "deals": self._assemble_rail("deals", cands["deals"], None),
            "foryou": self._assemble_rail("foryou", cands["foryou"], None),
            "flights": self._assemble_rail("flights", cands["flights"], None),
            "ai": False,
        }

    async def _personalize(self, cands: dict[str, list[dict[str, Any]]], vault: dict[str, str],
                           recent_down_tags: set[str] | None = None) -> dict[str, Any] | None:
        """ONE batched completion: pick + order the best per rail and write a one-line why."""
        # Shrink candidates to just what the model needs (id + the human-readable bits).
        def slim(rail: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{"id": i, "name": c.get("title"), "info": c.get("subtitle"),
                     "rating": c.get("rating"), "open": c.get("open_now"),
                     "discount": c.get("discount"), "meta": c.get("meta")} for i, c in enumerate(rail)]

        payload = {"food": slim(cands["food"]), "deals": slim(cands["deals"]),
                   "foryou": slim(cands["foryou"]), "flights": slim(cands["flights"])}
        if not any(payload.values()):
            return None

        from himmy_app import user_profile

        now = datetime.datetime.now()
        profile = user_profile.render_for_prompt(cfg=self.cfg) or "(no saved profile yet)"
        system = (
            "You are Himmy, the user's personal Nepal concierge. From the candidate lists, pick and "
            "ORDER the best few for THIS user and write a short, warm one-line reason for each pick "
            "('why'). Use their profile/preferences and the time of day (lunch vs dinner, weekday vs "
            "weekend). Be specific and honest — never invent a place, price, or rating not in the "
            "candidates. Reply with ONLY a JSON object, no prose, of the form: "
            '{"headline": "<friendly one-liner>", "food": [{"id": <int>, "why": "<reason>"}], '
            '"deals": [...], "foryou": [...], "flights": [...]}. food = restaurants; deals = '
            "discounted products; foryou = products matching the user's interests; flights = trips. "
            "Use up to 4 each (2 flights); ids refer to the candidate 'id' fields within that rail. "
            "Drop anything weak rather than padding. If a 'recently dismissed' list is given, those "
            "are categories/price-bands the user just thumbed DOWN — deprioritise anything matching "
            "them and do NOT re-surface a just-dismissed type at the top."
        )
        # Tell the model what the user just rejected (coarse cuisine / price-band tags) so it won't
        # immediately re-surface a category they dismissed. Fully local — derived from feedback.
        down = sorted(t for t in (recent_down_tags or set()) if t)
        down_note = (f"\n\nRecently dismissed (avoid re-surfacing these): {', '.join(down)}."
                     if down else "")
        user = (
            f"Local time: {now:%A %d %b, %I:%M %p}.\n\nAbout the user:\n{profile}{down_note}\n\n"
            f"Candidates (JSON):\n{json.dumps(payload, ensure_ascii=False)}"
        )

        from himmy.cli.provider import build_inference_for
        from himmy.services.inference.models import InferenceMessage, InferenceRequest

        service = build_inference_for(self.cfg.provider, self.cfg.model)
        resp = await service.run(InferenceRequest(
            messages=[InferenceMessage(role="system", content=system),
                      InferenceMessage(role="user", content=user)],
            generation_params={"temperature": 0.3}, timeout_seconds=60.0,
        ))
        picks = _extract_json(resp.output_text or "")
        if not isinstance(picks, dict):
            return None
        # Assemble each rail from the model's ordered picks, padded with the next-best candidates
        # so the layout always stays full even when the model only ranks a couple.
        food = self._assemble_rail("food", cands["food"], picks.get("food"))
        deals = self._assemble_rail("deals", cands["deals"], picks.get("deals"))
        foryou = self._assemble_rail("foryou", cands["foryou"], picks.get("foryou"))
        flights = self._assemble_rail("flights", cands["flights"], picks.get("flights"))
        if not (food or deals or foryou or flights):
            return None
        return {
            "ok": True,
            "headline": str(picks.get("headline") or "Here's what's good in Nepal right now.").strip(),
            "food": food, "deals": deals, "foryou": foryou, "flights": flights, "ai": True,
        }

    # ---- cache plumbing ---------------------------------------------------------------------
    def _spawn_refresh(self) -> None:
        if self._refreshing:
            return

        async def _run() -> None:
            self._refreshing = True
            try:
                board = await self._generate(ai=True)
                self._write_cache(board)
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._refreshing = False

        with contextlib.suppress(RuntimeError):  # no running loop (e.g. unit test) → skip
            asyncio.get_running_loop().create_task(_run())

    def _cache_iso(self) -> str:
        """The iso of the cache we just wrote — falls back to now if the write was suppressed,
        so the served board always carries a sane generated_at (never None from a failed read)."""
        cached = self._read_cache()
        if isinstance(cached, dict) and cached.get("iso"):
            return str(cached["iso"])
        return datetime.datetime.now().isoformat(timespec="seconds")

    def _read_cache(self) -> dict[str, Any] | None:
        try:
            return json.loads(self._cache.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def _write_cache(self, board: dict[str, Any]) -> None:
        now = time.time()
        payload = {"generated_at": now,
                   "iso": datetime.datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                   "board": board}
        # Atomic write (temp + os.replace) so a kill/overlap can't truncate do_cache.json.
        with contextlib.suppress(Exception):
            _net.atomic_write_text(self._cache, json.dumps(payload, ensure_ascii=False))


def _extract_json(text: str) -> Any:
    """Tolerant JSON parse: the model's reply may be wrapped in prose or a ```json fence."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        with contextlib.suppress(Exception):
            return json.loads(text[start:end + 1])
    return None


__all__ = ["DoConcierge", "DoFeedback", "DoCart"]
