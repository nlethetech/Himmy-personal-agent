"""The Zotero connector — tools the agent uses to FIND things in your library.

These are the "look it up" half of the desk (the "read the papers" half is
:mod:`himmy_app.connectors.papers_rag`). Everything talks to Zotero's local API via
:class:`~himmy_app.connectors.zotero_client.ZoteroClient`; a closed Zotero produces a
friendly message, never a crash.
"""

from __future__ import annotations

from typing import Any

from himmy.services.tools.registry import ToolRegistry, register_local_tool

from himmy_app.config import HimmyConfig, load_config
from himmy_app.connectors.zotero_client import ZoteroClient, ZoteroUnavailable, citation


class ZoteroConnector:
    """Registers the library-search tools over the Zotero local API."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        cfg = config or load_config()
        self._client = ZoteroClient(cfg.zotero_items_url, cfg.zotero_collections_url)

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        client = self._client

        async def zotero_search(args: dict[str, Any]) -> dict[str, Any]:
            query = str(args.get("query") or "").strip()
            if not query:
                return {"ok": False, "message": "What should I search your library for?"}
            full_text = bool(args.get("full_text", False))
            limit = int(args.get("limit", 15))
            try:
                items = await client.search_items(query, limit=limit, full_text=full_text)
            except ZoteroUnavailable as exc:
                return {"ok": False, "message": str(exc)}
            return {
                "ok": True,
                "count": len(items),
                "results": [
                    {
                        "key": it["key"],
                        "citation": citation(it),
                        "title": it["title"],
                        "authors": it["authors"],
                        "year": it["year"],
                        "type": it["type"],
                        "doi": it["doi"],
                        "tags": it["tags"],
                    }
                    for it in items
                ],
                "note": "Items from the user's Zotero library. Cite by title/author/year.",
            }

        async def zotero_collections(args: dict[str, Any]) -> dict[str, Any]:
            try:
                cols = await client.list_collections()
            except ZoteroUnavailable as exc:
                return {"ok": False, "message": str(exc)}
            return {"ok": True, "count": len(cols), "collections": cols}

        async def zotero_collection_items(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args.get("collection") or "").strip()
            if not name:
                return {"ok": False, "message": "Which collection (folder) do you want listed?"}
            try:
                cols = await client.list_collections()
            except ZoteroUnavailable as exc:
                return {"ok": False, "message": str(exc)}
            # Accept either a collection key or a (case-insensitive) name.
            match = next(
                (c for c in cols if c.get("key") == name
                 or (c.get("name") or "").strip().lower() == name.lower()),
                None,
            )
            if not match:
                return {
                    "ok": False,
                    "message": f"No collection named '{name}'.",
                    "available": [c.get("name") for c in cols],
                }
            try:
                items = await client.collection_items(match["key"], limit=int(args.get("limit", 30)))
            except ZoteroUnavailable as exc:
                return {"ok": False, "message": str(exc)}
            return {
                "ok": True,
                "collection": match.get("name"),
                "count": len(items),
                "results": [
                    {"key": it["key"], "citation": citation(it), "tags": it["tags"]} for it in items
                ],
            }

        async def zotero_get_item(args: dict[str, Any]) -> dict[str, Any]:
            key = str(args.get("key") or "").strip()
            if not key:
                return {"ok": False, "message": "Give me the item key (from a search result)."}
            try:
                item = await client.get_item(key)
            except ZoteroUnavailable as exc:
                return {"ok": False, "message": str(exc)}
            if not item:
                return {"ok": False, "message": f"No item with key '{key}'."}
            return {"ok": True, "item": item, "citation": citation(item)}

        register_local_tool(
            registry, name="zotero_search", read_only=True, handler=zotero_search,
            description=(
                "Search the user's Zotero library for papers/books/items by title, author, or "
                "year. Pass `query`; set `full_text: true` to also search inside indexed PDF "
                "text; `limit` caps results (default 15). Returns each item's key (for "
                "follow-up), citation, authors, year, DOI and tags. Use this to find what the "
                "user has SAVED — not to answer questions FROM the papers (use ask_papers for that)."
            ),
            args_json_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms (title/author/topic)."},
                    "full_text": {"type": "boolean", "description": "Also search inside PDF text."},
                    "limit": {"type": "integer", "description": "Max results (default 15)."},
                },
                "required": ["query"],
            },
        )
        register_local_tool(
            registry, name="zotero_collections", read_only=True, handler=zotero_collections,
            description="List the user's Zotero collections (folders) with item counts.",
            args_json_schema={"type": "object", "properties": {}},
        )
        register_local_tool(
            registry, name="zotero_collection_items", read_only=True, handler=zotero_collection_items,
            description=(
                "List the top-level items in one Zotero collection (folder). Pass `collection` "
                "as the folder NAME or key; optional `limit` (default 30)."
            ),
            args_json_schema={
                "type": "object",
                "properties": {
                    "collection": {"type": "string", "description": "Collection name or key."},
                    "limit": {"type": "integer", "description": "Max items (default 30)."},
                },
                "required": ["collection"],
            },
        )
        register_local_tool(
            registry, name="zotero_get_item", read_only=True, handler=zotero_get_item,
            description=(
                "Get one Zotero item's full record (title, authors, year, abstract, DOI, tags) "
                "by its `key` (obtained from zotero_search)."
            ),
            args_json_schema={
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Zotero item key."}},
                "required": ["key"],
            },
        )
        return ["zotero_search", "zotero_collections", "zotero_collection_items", "zotero_get_item"]


__all__ = ["ZoteroConnector"]
