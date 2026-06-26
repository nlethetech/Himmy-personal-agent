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

#: How long a generated board stays fresh before a background refresh recomputes it.
_TTL = float(os.environ.get("HIMMY_DO_TTL") or str(6 * 3600))

#: How many picks each rail shows (rails are padded to this so the layout stays full).
_TARGETS = {"food": 4, "deals": 4, "foryou": 4, "flights": 3}

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

    def signals(self) -> tuple[set[str], dict[str, int]]:
        """Return (dismissed keys, tag -> net weight) for biasing candidates."""
        dismissed: set[str] = set()
        weights: dict[str, int] = {}
        with self._conn() as c:
            for r in c.execute("SELECT key, kind, tags FROM do_feedback"):
                if r["kind"] == "down":
                    dismissed.add(r["key"])
                delta = 1 if r["kind"] == "up" else -1
                for t in json.loads(r["tags"] or "[]"):
                    if t:
                        weights[t.lower()] = weights.get(t.lower(), 0) + delta
        return dismissed, weights


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
    """The user's saved label→value details (home airport, cuisines, budget, …)."""
    try:
        from himmy_app import user_profile

        prof = user_profile.load(cfg)
        merged = prof.get("learned") or {}
        # render_for_prompt merges layers; for raw details we merge both layers ourselves.
        details: dict[str, str] = {}
        for layer in ("learned", "user"):
            for k, v in ((prof.get(layer) or {}).get("details") or {}).items():
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


def _home_airport(vault: dict[str, str]) -> str:
    for label, value in vault.items():
        if "airport" in label.lower() or "home base" in label.lower():
            v = value.strip().upper()
            if v:
                return v.split()[0]
    return "KTM"


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
        fresh = cached and (time.time() - cached.get("generated_at", 0)) < _TTL
        if cached and fresh and not force:
            return {**cached["board"], "stale": False, "generated_at": cached["iso"]}
        if force:
            board = await self._generate(ai=True)
            self._write_cache(board)
            return {**board, "stale": False, "generated_at": self._read_cache()["iso"]}
        # No cache, or stale: build the free rules board NOW (instant), refresh the AI behind it.
        if cached:
            self._spawn_refresh()
            return {**cached["board"], "stale": True, "generated_at": cached["iso"]}
        board = await self._generate(ai=False)        # cold start: free + fast, no model
        self._write_cache(board)
        self._spawn_refresh()                          # warm the AI layer in the background
        return {**board, "stale": True, "generated_at": self._read_cache()["iso"]}

    def feedback(self, kind: str, key: str, rail: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        out = self.fb.record(kind, key, rail, tags)
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
        self._air_cache[key] = resolved
        return resolved

    async def flights(self, origin: str, destination: str, date: str = "") -> dict[str, Any]:
        if not self._on("flights"):
            return {"ok": False, "flights": [], "from": origin, "to": destination, "date": date,
                    "message": "Flights (Buddha Air) is turned off in Settings → Permissions."}
        from himmy_app.connectors.buddha_air import buddha_air_flights

        if not date:
            date = (datetime.date.today() + datetime.timedelta(days=8)).isoformat()
        # Smart-route both endpoints first (so "Bhairawa", "Lumbini", typos all work).
        o = await self._resolve_airport(origin)
        d = await self._resolve_airport(destination)
        return await buddha_air_flights({
            "origin": o or origin, "destination": d or destination, "date": date,
        })

    # ---- trips: a day-by-day roadmap of places/activities (grounded in real OSM spots) -------
    async def _geocode(self, place: str) -> tuple[float, float] | None:
        """Resolve a place name to coords via OpenStreetMap Nominatim (keyless, best-effort)."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "HimmyApp/1.0 (concierge)"}) as c:
                r = await c.get("https://nominatim.openstreetmap.org/search",
                                params={"q": place, "format": "json", "limit": 1})
            d = r.json()
            if d:
                return float(d[0]["lat"]), float(d[0]["lon"])
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _pois(self, lat: float, lon: float) -> list[dict[str, str]]:
        """Real nearby attractions/viewpoints/historic/natural spots via OSM Overpass."""
        import httpx

        q = (f'[out:json][timeout:25];('
             f'node["tourism"~"attraction|viewpoint|museum|theme_park"](around:8000,{lat},{lon});'
             f'node["historic"](around:8000,{lat},{lon});'
             f'node["leisure"="park"](around:8000,{lat},{lon});'
             f'node["natural"="peak"](around:12000,{lat},{lon}););out body 40;')
        try:
            async with httpx.AsyncClient(timeout=35, headers={"User-Agent": "HimmyApp/1.0 (concierge)"}) as c:
                r = await c.post("https://overpass-api.de/api/interpreter", data={"data": q})
            out: list[dict[str, str]] = []
            seen: set[str] = set()
            for e in r.json().get("elements", []):
                t = e.get("tags", {})
                name = (t.get("name") or "").strip()
                if not name or name.lower() in seen or name.isdigit():
                    continue
                seen.add(name.lower())
                out.append({"name": name, "kind": t.get("tourism") or t.get("historic")
                            or t.get("leisure") or t.get("natural") or "spot"})
            return out[:25]
        except Exception:  # noqa: BLE001
            return []

    async def trip(self, destination: str, days: int = 2) -> dict[str, Any]:
        destination = (destination or "").strip()
        if not destination:
            return {"ok": False, "message": "Where would you like to go?"}
        days = max(1, min(int(days or 2), 7))
        cache_key = f"{destination.lower()}|{days}"
        cached = self._trip_cache().get(cache_key)
        if cached:
            return cached
        # Ground the plan in REAL local spots so Himmy curates rather than invents.
        geo = await self._geocode(destination)
        pois = await self._pois(*geo) if geo else []
        poi_str = "; ".join(f"{p['name']} ({p['kind']})" for p in pois) or "(none found — use your own knowledge)"
        # Optional "getting there" — a Buddha Air flight from the user's home airport.
        getting_there = None
        if self._on("flights"):
            code = await self._resolve_airport(destination)
            home = _home_airport(_vault(self.cfg))
            if code and code != home:
                try:
                    from himmy_app.connectors.buddha_air import buddha_air_flights

                    date = (datetime.date.today() + datetime.timedelta(days=14)).isoformat()
                    fr = await buddha_air_flights({"origin": home, "destination": code, "date": date})
                    if fr.get("ok") and fr.get("cheapest"):
                        getting_there = {"from": home, "to": code, "cheapest": fr["cheapest"],
                                         "booking_link": fr.get("booking_link")}
                except Exception:  # noqa: BLE001
                    pass
        roadmap = await self._plan_trip(destination, days, poi_str)
        if not roadmap:
            return {"ok": False, "message": "Couldn't build a plan just now — try again."}
        out = {"ok": True, "destination": destination, "days": days,
               "getting_there": getting_there, **roadmap}
        self._trip_cache_write(cache_key, out)
        return out

    async def _plan_trip(self, destination: str, days: int, poi_str: str) -> dict[str, Any] | None:
        from himmy_app import user_profile
        from himmy.cli.provider import build_inference_for
        from himmy.services.inference.models import InferenceMessage, InferenceRequest

        profile = user_profile.render_for_prompt(cfg=self.cfg) or "(no saved profile)"
        system = (
            "You are Himmy, a sharp Nepal travel planner. Build a realistic DAY-BY-DAY roadmap of "
            "places to visit and things to do — no maps, just a clear plan. Ground it in your real "
            "knowledge of the destination AND the verified local spots provided; never invent places "
            "that don't exist. Tailor to the user's profile where relevant. Reply with ONLY JSON: "
            '{"summary":"<one warm sentence>","itinerary":[{"day":1,"title":"<short theme>",'
            '"items":[{"name":"<place/activity>","category":"<Nature|Culture|Food|Adventure|Relax|'
            'Shopping>","desc":"<one sentence>","tip":"<short optional tip>"}]}],"tips":["<short '
            'practical tip>"]}. 2-4 items per day; be specific and local.'
        )
        user = (f"Destination: {destination}\nDays: {days}\nVerified local spots (OSM): {poi_str}\n\n"
                f"About the traveller:\n{profile}\n\nBuild the roadmap.")
        try:
            svc = build_inference_for(self.cfg.provider, self.cfg.model)
            resp = await svc.run(InferenceRequest(
                messages=[InferenceMessage(role="system", content=system),
                          InferenceMessage(role="user", content=user)],
                generation_params={"temperature": 0.4}, timeout_seconds=70.0))
            data = _extract_json(resp.output_text or "")
            if isinstance(data, dict) and data.get("itinerary"):
                return {"summary": str(data.get("summary") or "").strip(),
                        "itinerary": data.get("itinerary") or [], "tips": data.get("tips") or []}
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
        with contextlib.suppress(Exception):
            (self.cfg.data_dir / "trips_cache.json").write_text(json.dumps(cache, ensure_ascii=False),
                                                                encoding="utf-8")

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
    async def _candidates(self, vault: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        dismissed, weights = self.fb.signals()
        food_seeds = _seeds_from_vault(vault, _FOOD_VAULT_HINTS, _FOOD_SEEDS)
        deal_seeds = _seeds_from_vault(vault, _DEAL_VAULT_HINTS, _DEAL_SEEDS)
        shop_seeds = _seeds_from_vault(vault, _SHOP_VAULT_HINTS, _SHOP_SEEDS)
        # Skip any rail whose surface the user turned off in Settings → Permissions.
        food, deals, foryou, flights = await asyncio.gather(
            self._food(food_seeds, dismissed, weights) if self._on("food") else self._none(),
            self._deals(deal_seeds, dismissed, weights) if self._on("shopping") else self._none(),
            self._shop_foryou(shop_seeds, dismissed, weights) if self._on("shopping") else self._none(),
            self._flights(vault, dismissed) if self._on("flights") else self._none(),
            return_exceptions=True,
        )
        return {
            "food": food if isinstance(food, list) else [],
            "deals": deals if isinstance(deals, list) else [],
            "foryou": foryou if isinstance(foryou, list) else [],
            "flights": flights if isinstance(flights, list) else [],
        }

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
        cands = await self._candidates(vault)
        board = self._deterministic_board(cands)        # always have a free, complete board
        if not ai:
            return board
        try:
            enriched = await self._personalize(cands, vault)
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

    async def _personalize(self, cands: dict[str, list[dict[str, Any]]], vault: dict[str, str]) -> dict[str, Any] | None:
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
            "Drop anything weak rather than padding."
        )
        user = (
            f"Local time: {now:%A %d %b, %I:%M %p}.\n\nAbout the user:\n{profile}\n\n"
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
        with contextlib.suppress(Exception):
            self._cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


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
