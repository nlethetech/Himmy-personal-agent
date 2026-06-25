"""Low-level async client for Zotero's built-in local API (the server Zotero runs on
:23119 when the app is open). It mirrors the Zotero Web API shape, so the same item/
collection/fulltext endpoints work locally with NO API key.

Shared by :mod:`himmy_app.connectors.zotero` (the search tools the agent calls) and
:mod:`himmy_app.connectors.papers_rag` (the indexer that pulls PDF text for RAG).

Everything degrades to a clear :class:`ZoteroUnavailable` when Zotero is not running, so a
closed app produces a friendly "open Zotero" message rather than a stack trace.
"""

from __future__ import annotations

from typing import Any

import httpx


class ZoteroUnavailable(RuntimeError):
    """Raised when the Zotero local API can't be reached (app closed / API off)."""


def _author_names(creators: list[dict[str, Any]] | None) -> list[str]:
    """Flatten Zotero ``creators`` into display names ("Jane Smith" / "World Bank")."""
    out: list[str] = []
    for c in creators or []:
        if not isinstance(c, dict):
            continue
        if c.get("name"):  # single-field creator (institutions)
            out.append(str(c["name"]).strip())
            continue
        parts = [str(c.get("firstName", "")).strip(), str(c.get("lastName", "")).strip()]
        name = " ".join(p for p in parts if p).strip()
        if name:
            out.append(name)
    return out


def format_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise one raw Zotero item into a compact, citation-ready dict."""
    data = raw.get("data") or {}
    meta = raw.get("meta") or {}
    authors = _author_names(data.get("creators"))
    year = ""
    parsed = meta.get("parsedDate") or data.get("date") or ""
    if parsed:
        # parsedDate is ISO-ish ("2021-03-01"); pull the leading year.
        year = str(parsed)[:4]
    tags = [t.get("tag") for t in (data.get("tags") or []) if isinstance(t, dict) and t.get("tag")]
    return {
        "key": raw.get("key") or data.get("key"),
        "title": (data.get("title") or data.get("caseName") or data.get("subject") or "").strip(),
        "authors": authors,
        "author_summary": meta.get("creatorSummary") or (authors[0] if authors else ""),
        "year": year,
        "type": data.get("itemType"),
        "publication": (data.get("publicationTitle") or data.get("bookTitle")
                        or data.get("publisher") or "").strip(),
        "doi": (data.get("DOI") or "").strip(),
        "url": (data.get("url") or "").strip(),
        "abstract": (data.get("abstractNote") or "").strip(),
        "tags": tags,
        "collections": data.get("collections") or [],
        "num_children": meta.get("numChildren", 0),
    }


def citation(item: dict[str, Any]) -> str:
    """A short human citation line, e.g. 'Smith et al. (2021), Nature — "Title"'."""
    who = item.get("author_summary") or (item.get("authors") or [""])[0] or "Unknown"
    year = item.get("year") or "n.d."
    title = item.get("title") or "(untitled)"
    pub = item.get("publication")
    tail = f", {pub}" if pub else ""
    return f'{who} ({year}){tail} — "{title}"'


class ZoteroClient:
    """Thin async wrapper over the Zotero local API. One instance per process is fine."""

    def __init__(self, items_url: str, collections_url: str, timeout: float = 8.0) -> None:
        self._items_url = items_url.rstrip("/")
        self._collections_url = collections_url.rstrip("/")
        self._timeout = timeout

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ZoteroUnavailable(
                "Can't reach Zotero. Make sure the Zotero app is open on this Mac "
                "(it serves the local API on port 23119 while running)."
            ) from exc
        except httpx.HTTPError as exc:  # pragma: no cover - network edge
            raise ZoteroUnavailable(f"Zotero request failed: {exc}") from exc
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def search_items(
        self, query: str, *, limit: int = 15, full_text: bool = False,
        item_type: str = "-attachment || -note",
    ) -> list[dict[str, Any]]:
        params = {
            "q": query,
            "qmode": "everything" if full_text else "titleCreatorYear",
            "itemType": item_type,
            "limit": max(1, min(int(limit), 50)),
            "sort": "date",
            "direction": "desc",
            "include": "data",
        }
        raw = await self._get(self._items_url, params) or []
        return [format_item(r) for r in raw]

    async def list_collections(self) -> list[dict[str, Any]]:
        raw = await self._get(self._collections_url) or []
        out = []
        for r in raw:
            data = r.get("data") or {}
            out.append({
                "key": r.get("key"),
                "name": data.get("name"),
                "num_items": (r.get("meta") or {}).get("numItems", 0),
                "parent": data.get("parentCollection") or None,
            })
        return out

    async def collection_items(self, collection_key: str, *, limit: int = 30) -> list[dict[str, Any]]:
        url = f"{self._collections_url}/{collection_key}/items/top"
        params = {"limit": max(1, min(int(limit), 100)), "include": "data", "itemType": "-attachment || -note"}
        raw = await self._get(url, params) or []
        return [format_item(r) for r in raw]

    async def get_item(self, key: str) -> dict[str, Any] | None:
        raw = await self._get(f"{self._items_url}/{key}", {"include": "data"})
        return format_item(raw) if raw else None

    async def item_children(self, key: str) -> list[dict[str, Any]]:
        raw = await self._get(f"{self._items_url}/{key}/children", {"include": "data"}) or []
        return raw

    async def item_fulltext(self, attachment_key: str) -> str | None:
        """Zotero's own indexed full text for a PDF attachment (no re-extraction needed)."""
        data = await self._get(f"{self._items_url}/{attachment_key}/fulltext")
        if isinstance(data, dict):
            content = data.get("content")
            return content if isinstance(content, str) and content.strip() else None
        return None

    async def all_top_items(self, *, page: int = 100, max_items: int = 5000) -> list[dict[str, Any]]:
        """Paginate every top-level (non-attachment/note) item — used by the indexer."""
        out: list[dict[str, Any]] = []
        start = 0
        while start < max_items:
            params = {
                "itemType": "-attachment || -note",
                "limit": page,
                "start": start,
                "include": "data",
            }
            raw = await self._get(self._items_url + "/top", params) or []
            if not raw:
                break
            out.extend(raw)
            if len(raw) < page:
                break
            start += page
        return out


__all__ = ["ZoteroClient", "ZoteroUnavailable", "format_item", "citation"]
