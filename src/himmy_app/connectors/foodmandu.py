"""Foodmandu (Nepal food delivery) restaurant search — find places + an order link.

Foodmandu exposes an OPEN JSON API (no auth/signature): restaurants come from
``GET foodmandu.com/webapi/api/Vendor/GetVendors1?Keyword=...&DeliveryZoneId=1&...`` and each
restaurant's order page is ``foodmandu.com/Restaurant/Details/{id}``. We search by keyword
(dish / cuisine / restaurant name) and hand the user the order link to browse the menu and
order themselves — no auto-order, no payment. Defaults to the Kathmandu delivery zone (1).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors._net import NetError, safe_get_json
from himmy_app.connectors._register import safe_register_local_tool

_API = "https://foodmandu.com/webapi/api"
# Only ever talk to Foodmandu's own API host; a redirect/DNS answer to anything else is refused.
_ALLOW_HOSTS = ("foodmandu.com",)
_HEADERS = {
    "Origin": "https://foodmandu.com",
    "Referer": "https://foodmandu.com/",
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}
_IMG_BASE = "https://images.foodmandu.com"


def _order_link(vendor_id: Any) -> str:
    return f"https://foodmandu.com/Restaurant/Details/{vendor_id}"


def _vendor_id_from_link(link: str) -> str:
    """Pull the vendor id out of a .../Restaurant/Details/{id} order link."""
    return (link or "").rstrip("/").rsplit("/", 1)[-1]


def _img(path: str) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return f"{_IMG_BASE}/{path.lstrip('/')}"


async def _resolve_vendor(query: str) -> dict[str, Any] | None:
    """Best-effort restaurant-name -> {id, name, image, order_link} via the vendor search."""
    res = await foodmandu_search({"query": query, "limit": 1})
    rows = res.get("restaurants") or []
    if not rows:
        return None
    r = rows[0]
    return {"id": _vendor_id_from_link(r.get("order_link") or ""), "name": r.get("name"),
            "image": r.get("image"), "order_link": r.get("order_link")}


async def foodmandu_menu(args: dict[str, Any]) -> dict[str, Any]:
    """A restaurant's full menu (dishes by category) from Foodmandu — public, no auth."""
    vid = str(args.get("vendor_id") or "").strip()
    name = str(args.get("restaurant") or args.get("query") or args.get("name") or "").strip()
    resolved_name = name
    order_link = ""
    if not vid and name:
        v = await _resolve_vendor(name)
        if not v or not v.get("id"):
            return {"ok": False, "message": f"Couldn't find '{name}' on Foodmandu."}
        vid, resolved_name, order_link = v["id"], v.get("name") or name, v.get("order_link") or ""
    if not vid:
        return {"ok": False, "message": "Need a restaurant name or vendor_id."}
    if not order_link:
        order_link = _order_link(vid)
    try:
        cats = await safe_get_json(
            f"{_API}/v2/Product/GetVendorProductsBySubCategoryV2",
            params={"VendorId": vid, "show": ""}, headers=_HEADERS,
            allow_hosts=_ALLOW_HOSTS,
        )
        if not isinstance(cats, list):
            cats = []
    except NetError as exc:
        return {"ok": False, "message": f"Couldn't read the menu ({type(exc).__name__})."}
    categories: list[dict[str, Any]] = []
    total = 0
    for c in cats:
        items: list[dict[str, Any]] = []
        for it in (c.get("items") or []):
            price = it.get("price")
            old = it.get("oldprice")
            items.append({
                "id": it.get("productId"),
                "name": (it.get("name") or "").strip(),
                "price": price,
                "was": old if (old and float(old or 0) > float(price or 0)) else None,
                "desc": (it.get("productDesc") or "").strip(),
                "image": _img(it.get("ProductImage") or it.get("ProductGridImage") or ""),
                "popular": bool(it.get("IsFavouriteProduct")),
                "tag": (it.get("itemDisplayTag") or "").strip(),
            })
        if items:
            total += len(items)
            categories.append({"category": (c.get("category") or "").strip(), "items": items})
    return {
        "ok": True, "vendor_id": vid, "restaurant": resolved_name,
        "categories": categories, "item_count": total, "order_link": order_link,
    }


async def foodmandu_search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or args.get("keyword") or "").strip()
    if not query:
        return {"ok": False, "message": "What food or restaurant should I look for on Foodmandu?"}
    limit = max(1, min(int(args.get("limit") or 6), 12))
    zone = int(args.get("delivery_zone_id") or 1)  # 1 = Kathmandu (Foodmandu's main zone)
    params = {
        "Cuisine": "", "DeliveryZoneId": zone, "IsFavorite": "false", "IsRecent": "false",
        "Keyword": query, "LocationLat": 0, "LocationLng": 0, "PageNo": 1, "PageSize": limit,
        "SortBy": 4, "VendorName": "",
    }
    try:
        vendors = await safe_get_json(
            f"{_API}/Vendor/GetVendors1", params=params, headers=_HEADERS,
            allow_hosts=_ALLOW_HOSTS,
        )
        if not isinstance(vendors, list):
            vendors = []
    except NetError as exc:
        return {"ok": False, "message": f"Couldn't reach Foodmandu ({type(exc).__name__})."}
    out: list[dict[str, Any]] = []
    for v in vendors:
        vid = v.get("Id")
        if not vid:
            continue
        out.append({
            "vendor_id": str(vid),
            "name": (v.get("Name") or "").strip(),
            "cuisine": (v.get("CuisineTags") or v.get("Cuisine") or "").strip(" |"),
            "rating": v.get("VendorRating"),
            "open_now": not bool(v.get("IsVendorClosed")),
            "hours": (v.get("OpeningHours") or "").strip(),
            "address": (v.get("Address1") or "").strip(),
            "distance": (v.get("DeliveryDistanceStr") or "").strip(),
            "delivers": bool(v.get("AcceptsDeliveryOrder")),
            # The vendor's current OFFER banner ("Xtreme Mo:Mo Combo starting at Rs. 295"), if any.
            "promo": (v.get("PromoText") or "").strip(),
            "image": (v.get("VendorListingWebImageName") or v.get("VendorCoverImageName")
                      or v.get("VendorLogoImageName") or "").strip(),
            "order_link": _order_link(vid),
        })
    return {
        "ok": True, "query": query, "count": len(out), "restaurants": out,
        "note": "Open a restaurant's order_link to see the menu and place the order yourself.",
    }


def _norm(s: str) -> str:
    """Lowercased, punctuation-stripped text for loose dish matching ('Mo:Mo' → 'mo mo')."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _dish_matches(tokens: list[str], name: str) -> bool:
    """True if every query token appears in the dish name (spaced OR compacted, so 'momo'
    matches 'Mo:Mo' and 'mo mo')."""
    n = _norm(name)
    compact = n.replace(" ", "")
    return all((t in n) or (t in compact) for t in tokens)


def _discount_pct(price: Any, was: Any) -> int:
    try:
        p, w = float(price or 0), float(was or 0)
    except (TypeError, ValueError):
        return 0
    return round((w - p) / w * 100) if w > p > 0 else 0


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def _fetch_menu_items(vid: str) -> list[dict[str, Any]]:
    """A vendor's menu items, flattened across categories (best-effort: [] on any failure)."""
    try:
        cats = await safe_get_json(
            f"{_API}/v2/Product/GetVendorProductsBySubCategoryV2",
            params={"VendorId": vid, "show": ""}, headers=_HEADERS, allow_hosts=_ALLOW_HOSTS,
        )
    except NetError:
        return []
    items: list[dict[str, Any]] = []
    if isinstance(cats, list):
        for c in cats:
            items.extend(c.get("items") or [])
    return items


async def foodmandu_dishes(args: dict[str, Any]) -> dict[str, Any]:
    """Search Foodmandu at the DISH level: find the actual dishes matching a query across many
    restaurants (not just the restaurants), each with its price, any discount, and the
    restaurant's rating — so good options lead and cheap, low-rated ones are filtered out.

    Filters: ``max_price`` / ``min_price`` (NPR), ``min_rating`` (default 3.2 — drops the bad
    cheap places), ``open_only``. ``prefer`` is a free-text taste hint that nudges ranking. Also
    returns ``offers`` (discounted dishes) and ``promos`` (vendor offer banners).
    """
    query = str(args.get("query") or args.get("keyword") or "").strip()
    if not query:
        return {"ok": False, "message": "What dish should I look for on Foodmandu?"}
    max_price = _to_float(args.get("max_price"), 0.0) or None
    min_price = _to_float(args.get("min_price"), 0.0) or None
    min_rating = _to_float(args.get("min_rating"), 3.2)
    limit = max(1, min(int(args.get("limit") or 14), 30))
    open_only = bool(args.get("open_only", False))
    prefer_tokens = [t for t in _norm(str(args.get("prefer") or "")).split() if t]

    vsearch = await foodmandu_search({"query": query, "limit": 12})
    vendors = vsearch.get("restaurants") or []
    if not vendors:
        return {"ok": True, "query": query, "dishes": [], "offers": [], "promos": [],
                "message": f"No Foodmandu results for '{query}'."}

    query_tokens = [t for t in _norm(query).split() if t]
    menus = await asyncio.gather(
        *[_fetch_menu_items(v["vendor_id"]) for v in vendors], return_exceptions=True)

    dishes: list[dict[str, Any]] = []
    for v, items in zip(vendors, menus):
        if isinstance(items, BaseException):
            continue
        vrating = _to_float(v.get("rating"))
        per_vendor = 0
        for it in items:
            name = (it.get("name") or "").strip()
            if not name or not _dish_matches(query_tokens, name):
                continue
            price = _to_float(it.get("price"))
            if price <= 0:
                continue
            if (max_price and price > max_price) or (min_price and price < min_price):
                continue
            disc = _discount_pct(it.get("price"), it.get("oldprice"))
            popular = bool(it.get("IsFavouriteProduct"))
            score = (vrating + (0.4 if popular else 0.0) + min(disc, 50) / 100.0
                     + (0.3 if v.get("open_now") else 0.0))
            if prefer_tokens and any(t in _norm(name) for t in prefer_tokens):
                score += 0.6
            dishes.append({
                "name": name, "price": price,
                "was": _to_float(it.get("oldprice")) if disc else None, "discount_pct": disc,
                "desc": (it.get("productDesc") or "").strip()[:160],
                "image": _img(it.get("ProductImage") or it.get("ProductGridImage") or ""),
                "popular": popular, "restaurant": v.get("name"), "vendor_id": v.get("vendor_id"),
                "rating": vrating or None, "open_now": bool(v.get("open_now")),
                "promo": v.get("promo") or "", "order_link": v.get("order_link"),
                "_score": score, "_vrating": vrating,
            })
            per_vendor += 1
            if per_vendor >= 4:  # variety: at most 4 dishes from any one restaurant
                break

    # Filter the "cheap bad ones": drop dishes from sub-min_rating restaurants — unless doing so
    # would leave too little to show, in which case keep them (better than an empty result).
    good = [d for d in dishes if d["_vrating"] >= min_rating]
    pool = good if len(good) >= 4 else dishes
    if open_only:
        pool = [d for d in pool if d["open_now"]] or pool
    pool.sort(key=lambda d: (-d["_score"], d["price"]))
    top = pool[:limit]
    for d in top:
        d.pop("_score", None)
        d.pop("_vrating", None)

    offers = sorted((d for d in top if d.get("discount_pct")),
                    key=lambda d: -d["discount_pct"])[:8]
    promos = [{"restaurant": v["name"], "promo": v["promo"], "order_link": v["order_link"],
               "rating": v.get("rating")} for v in vendors if v.get("promo")][:6]
    return {"ok": True, "query": query, "count": len(top), "dishes": top,
            "offers": offers, "promos": promos,
            "note": "Dishes across Foodmandu restaurants, best-rated first; open an order_link to order."}


class FoodmanduConnector:
    """Registers ``foodmandu_search`` — Foodmandu restaurant search + an order link."""

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        safe_register_local_tool(
            registry, name="foodmandu_search", read_only=True, handler=foodmandu_search,
            description=(
                "Search FOODMANDU (Nepal food delivery; defaults to Kathmandu) for restaurants by a "
                "`query` — a dish, cuisine, or restaurant name (e.g. 'momo', 'pizza', 'KFC', "
                "'Newari', 'biryani'). Returns matching restaurants with rating, cuisine, whether "
                "they're OPEN now, distance, and an `order_link` the user opens to see the menu and "
                "place the order THEMSELVES — you do NOT order or pay. Optional `limit`. Prefer "
                "open restaurants and lead with the best-rated. Foodmandu is Nepal-only (Kathmandu, "
                "Pokhara, Chitwan, Butwal)."
            ),
            args_json_schema={"type": "object", "properties": {
                "query": {"type": "string"}, "limit": {"type": "integer"},
                "delivery_zone_id": {"type": "integer"}},
                "required": ["query"]},
        )
        safe_register_local_tool(
            registry, name="foodmandu_dishes", read_only=True, handler=foodmandu_dishes,
            description=(
                "Find actual DISHES on Foodmandu (Nepal) matching a `query` (a food item — 'momo', "
                "'chicken burger', 'biryani') ACROSS many restaurants — not just the restaurants. "
                "Returns each dish with its price (NPR), any discount (`was`/`discount_pct`), the "
                "restaurant and its rating, plus `offers` (discounted dishes) and `promos` (vendor "
                "offer banners). Use this whenever the user wants to compare a specific food, find "
                "the best/cheapest version of a dish, or hunt for deals. Filters: `max_price` / "
                "`min_price` (NPR) for a budget, `min_rating` (default 3.2 — keeps out the cheap "
                "low-rated places), `open_only`, and `prefer` (a taste hint like 'chicken spicy' "
                "to match the user's preferences). Lead with well-rated dishes in their budget, "
                "call out discounts, and give the order_link — you do NOT order or pay."
            ),
            args_json_schema={"type": "object", "properties": {
                "query": {"type": "string"},
                "max_price": {"type": "number", "description": "Budget cap in NPR."},
                "min_price": {"type": "number"},
                "min_rating": {"type": "number", "description": "Min restaurant rating (default 3.2)."},
                "open_only": {"type": "boolean"},
                "prefer": {"type": "string", "description": "Taste hint to boost matching dishes."},
                "limit": {"type": "integer"}},
                "required": ["query"]},
        )
        safe_register_local_tool(
            registry, name="foodmandu_menu", read_only=True, handler=foodmandu_menu,
            description=(
                "Read a restaurant's MENU from Foodmandu (Nepal). Pass `restaurant` (the name, e.g. "
                "'Bota Mo:Mo', 'Roadhouse Pizza') or a `vendor_id`. Returns the dishes grouped by "
                "category, each with name, price (NPR), description and whether it's popular — so you "
                "can recommend specific dishes, compare prices, or answer 'what's good here'. Use this "
                "whenever the user asks what a place serves or wants a dish recommendation. It does NOT "
                "place an order; to order, point them to the restaurant's Foodmandu page."
            ),
            args_json_schema={"type": "object", "properties": {
                "restaurant": {"type": "string"}, "vendor_id": {"type": "string"}}},
        )
        return ["foodmandu_search", "foodmandu_dishes", "foodmandu_menu"]


__all__ = ["FoodmanduConnector", "foodmandu_search", "foodmandu_dishes"]
