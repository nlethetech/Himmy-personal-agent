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

    # ---- candidate generation (free, deterministic, live) -----------------------------------
    async def _candidates(self, vault: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        dismissed, weights = self.fb.signals()
        food_seeds = _seeds_from_vault(vault, _FOOD_VAULT_HINTS, _FOOD_SEEDS)
        deal_seeds = _seeds_from_vault(vault, _DEAL_VAULT_HINTS, _DEAL_SEEDS)
        food, deals, flights = await asyncio.gather(
            self._food(food_seeds, dismissed, weights),
            self._deals(deal_seeds, dismissed, weights),
            self._flights(vault, dismissed),
            return_exceptions=True,
        )
        return {
            "food": food if isinstance(food, list) else [],
            "deals": deals if isinstance(deals, list) else [],
            "flights": flights if isinstance(flights, list) else [],
        }

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
                    "tag": seed,
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
                    "link": p.get("product_link"), "tag": seed,
                    "_score": pct + 4 * rating + 0.5 * weights.get(seed.lower(), 0),
                })
        out.sort(key=lambda x: x["_score"], reverse=True)
        return out[:8]

    async def _flights(self, vault: dict[str, str], dismissed: set[str]) -> list[dict[str, Any]]:
        from himmy_app.connectors.buddha_air import buddha_air_flights

        home = _home_airport(vault)
        date = (datetime.date.today() + datetime.timedelta(days=8)).isoformat()
        targets = [t for t in ("KTM", "PKR", "BWA") if t != home][:2]
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

    def _deterministic_board(self, cands: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        """A complete board with template 'why' lines — used cold and as the AI fallback."""
        def why_food(c: dict[str, Any]) -> str:
            bits = ["Open now" if c["open_now"] else "Opens later"]
            if c["rating"]:
                bits.append(f"⭐{c['rating']:.1f}")
            if c.get("subtitle"):
                bits.append(str(c["subtitle"]).split("|")[0].strip())
            return " · ".join(b for b in bits if b)

        def why_deal(c: dict[str, Any]) -> str:
            bits = []
            if c.get("discount"):
                bits.append(str(c["discount"]))
            if c["rating"]:
                bits.append(f"⭐{c['rating']:.1f}")
            if c.get("meta"):
                bits.append(str(c["meta"]))
            return " · ".join(bits)

        return {
            "ok": True,
            "headline": "Here's what's good in Nepal right now.",
            "food": [{**c, "why": why_food(c)} for c in cands["food"][:4]],
            "deals": [{**c, "why": why_deal(c)} for c in cands["deals"][:4]],
            "flights": [{**c, "why": (c.get("meta") or c.get("date") or "")} for c in cands["flights"][:2]],
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
                   "flights": slim(cands["flights"])}
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
            '"deals": [...], "flights": [...]}. Use up to 4 food, 4 deals, 2 flights; ids refer to the '
            "candidate 'id' fields. Drop anything weak rather than padding."
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

        def apply(rail_name: str, limit: int) -> list[dict[str, Any]]:
            chosen: list[dict[str, Any]] = []
            used: set[int] = set()
            for item in (picks.get(rail_name) or [])[:limit]:
                try:
                    idx = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                if idx in used or not (0 <= idx < len(cands[rail_name])):
                    continue
                used.add(idx)
                why = str(item.get("why") or "").strip()
                chosen.append({**cands[rail_name][idx], "why": why})
            return chosen

        food = apply("food", 4)
        deals = apply("deals", 4)
        flights = apply("flights", 2)
        if not (food or deals or flights):
            return None
        # Fall back per-rail to deterministic order if the model skipped a rail entirely.
        base = self._deterministic_board(cands)
        return {
            "ok": True,
            "headline": str(picks.get("headline") or base["headline"]).strip(),
            "food": food or base["food"],
            "deals": deals or base["deals"],
            "flights": flights or base["flights"],
            "ai": True,
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


__all__ = ["DoConcierge", "DoFeedback"]
