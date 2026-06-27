"""Foodmandu (Nepal food delivery) restaurant search — find places + an order link.

Foodmandu exposes an OPEN JSON API (no auth/signature): restaurants come from
``GET foodmandu.com/webapi/api/Vendor/GetVendors1?Keyword=...&DeliveryZoneId=1&...`` and each
restaurant's order page is ``foodmandu.com/Restaurant/Details/{id}``. We search by keyword
(dish / cuisine / restaurant name) and hand the user the order link to browse the menu and
order themselves — no auto-order, no payment. Defaults to the Kathmandu delivery zone (1).
"""

from __future__ import annotations

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
            "name": (v.get("Name") or "").strip(),
            "cuisine": (v.get("CuisineTags") or v.get("Cuisine") or "").strip(" |"),
            "rating": v.get("VendorRating"),
            "open_now": not bool(v.get("IsVendorClosed")),
            "hours": (v.get("OpeningHours") or "").strip(),
            "address": (v.get("Address1") or "").strip(),
            "distance": (v.get("DeliveryDistanceStr") or "").strip(),
            "delivers": bool(v.get("AcceptsDeliveryOrder")),
            "image": (v.get("VendorListingWebImageName") or v.get("VendorCoverImageName")
                      or v.get("VendorLogoImageName") or "").strip(),
            "order_link": _order_link(vid),
        })
    return {
        "ok": True, "query": query, "count": len(out), "restaurants": out,
        "note": "Open a restaurant's order_link to see the menu and place the order yourself.",
    }


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
        return ["foodmandu_search", "foodmandu_menu"]


__all__ = ["FoodmanduConnector", "foodmandu_search"]
