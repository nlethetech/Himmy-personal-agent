"""Foodmandu (Nepal food delivery) restaurant search — find places + an order link.

Foodmandu exposes an OPEN JSON API (no auth/signature): restaurants come from
``GET foodmandu.com/webapi/api/Vendor/GetVendors1?Keyword=...&DeliveryZoneId=1&...`` and each
restaurant's order page is ``foodmandu.com/Restaurant/Details/{id}``. We search by keyword
(dish / cuisine / restaurant name) and hand the user the order link to browse the menu and
order themselves — no auto-order, no payment. Defaults to the Kathmandu delivery zone (1).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors._register import safe_register_local_tool

_API = "https://foodmandu.com/webapi/api"
_HEADERS = {
    "Origin": "https://foodmandu.com",
    "Referer": "https://foodmandu.com/",
    "User-Agent": "Mozilla/5.0",
}


def _order_link(vendor_id: Any) -> str:
    return f"https://foodmandu.com/Restaurant/Details/{vendor_id}"


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
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{_API}/Vendor/GetVendors1?{urlencode(params)}", headers=_HEADERS)
        vendors = r.json()
        if not isinstance(vendors, list):
            vendors = []
    except Exception as exc:  # noqa: BLE001
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
        return ["foodmandu_search"]


__all__ = ["FoodmanduConnector", "foodmandu_search"]
