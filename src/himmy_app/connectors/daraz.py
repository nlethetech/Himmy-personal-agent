"""Daraz (Nepal online shopping) product search — find items + a buy link.

Daraz's storefront search is an OPEN JSON endpoint (no auth/signature): products come from
``GET www.daraz.com.np/catalog/?ajax=true&q=...&page=1`` with an ``x-requested-with`` header, and
each product's page is the absolute form of its ``itemUrl``. We search by keyword and hand the user
the product link (and a full search-results link) to view, compare and buy themselves — no
auto-purchase, no payment. Daraz Nepal prices are in NPR (Rs.).
"""

from __future__ import annotations

from typing import Any

import httpx

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors._register import safe_register_local_tool

_BASE = "https://www.daraz.com.np"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "x-requested-with": "XMLHttpRequest",
    "Referer": "https://www.daraz.com.np/",
    "Accept": "application/json, text/plain, */*",
}
#: friendly sort -> Daraz catalog ``sort`` param ("" = Daraz default / best match)
_SORTS = {"cheapest": "priceasc", "price_low": "priceasc", "expensive": "pricedesc",
          "price_high": "pricedesc", "popular": "", "best": "", "": ""}


def _abs(url: str) -> str:
    """Absolutise Daraz's protocol-relative ``//www.daraz.com.np/...`` product URLs."""
    url = (url or "").strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{_BASE}{url}"
    return url


def _search_link(query: str) -> str:
    from urllib.parse import quote_plus

    return f"{_BASE}/catalog/?q={quote_plus(query)}"


async def daraz_search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or args.get("keyword") or "").strip()
    if not query:
        return {"ok": False, "message": "What product should I look for on Daraz?"}
    limit = max(1, min(int(args.get("limit") or 6), 20))
    sort = _SORTS.get(str(args.get("sort") or "").strip().lower(), "")
    params: dict[str, Any] = {"ajax": "true", "isFirstRequest": "true", "page": 1, "q": query}
    if sort:
        params["sort"] = sort
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
            r = await c.get(f"{_BASE}/catalog/", params=params, headers=_HEADERS)
        items = (r.json().get("mods") or {}).get("listItems") or []
    except Exception as exc:  # noqa: BLE001 - the search link is the fallback
        return {"ok": True, "query": query, "count": 0, "products": [],
                "search_link": _search_link(query),
                "message": f"Couldn't read Daraz results ({type(exc).__name__}); open the search link."}
    out: list[dict[str, Any]] = []
    for it in items[:limit]:
        rating = it.get("ratingScore")
        try:
            rating = round(float(rating), 1) if rating else None
        except (TypeError, ValueError):
            rating = None
        out.append({
            "name": (it.get("name") or "").strip(),
            "price_npr": it.get("price"),
            "original_price_npr": it.get("originalPrice"),
            "discount": (it.get("discount") or "").strip(),
            "rating": rating,
            "reviews": it.get("review"),
            "sold": (it.get("itemSoldCntShow") or "").strip(),
            "location": (it.get("location") or "").strip(),
            "seller": (it.get("sellerName") or "").strip(),
            "brand": (it.get("brandName") or "").strip(),
            "in_stock": bool(it.get("inStock", True)),
            "product_link": _abs(it.get("itemUrl") or ""),
        })
    return {
        "ok": True, "query": query, "count": len(out), "currency": "NPR",
        "products": out, "search_link": _search_link(query),
        "note": "Open a product_link (or the search_link) to view details and buy yourself.",
    }


class DarazConnector:
    """Registers ``daraz_search`` — Daraz Nepal product search + a buy link."""

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        safe_register_local_tool(
            registry, name="daraz_search", read_only=True, handler=daraz_search,
            description=(
                "Search DARAZ (Nepal online shopping; prices in NPR/Rs.) for products by a `query` — "
                "any item, brand, or model (e.g. 'momo maker', 'iphone 15 case', 'running shoes', "
                "'office chair'). Returns matching products with price, original price, discount, "
                "rating, review count, units sold, seller and a `product_link` the user opens to view "
                "details and buy THEMSELVES — you do NOT purchase or pay. Optional `limit` and `sort` "
                "('cheapest' / 'expensive' / 'popular'). Lead with well-rated, in-stock items and "
                "mention the discount when there is one. Also returns a `search_link` to the full "
                "Daraz results. Daraz is Nepal-only."
            ),
            args_json_schema={"type": "object", "properties": {
                "query": {"type": "string"}, "limit": {"type": "integer"},
                "sort": {"type": "string", "enum": ["cheapest", "expensive", "popular"]}},
                "required": ["query"]},
        )
        return ["daraz_search"]


__all__ = ["DarazConnector", "daraz_search"]
