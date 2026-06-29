"""News RAG connector — let Himmy SEARCH the whole embedded news corpus in chat.

Every fetched article (Nepali + English) is embedded into the news index; this exposes a semantic
search over it as a tool, so Himmy can answer "what's the latest on X" / "any news about Y" from the
LIVE corpus — not just the handful the user saved. Read-only; degrades to empty if the index is cold.
"""

from __future__ import annotations

import asyncio
from typing import Any

from himmy.services.tools.registry import ToolRegistry

from himmy_app.config import HimmyConfig, load_config
from himmy_app.connectors._register import safe_register_local_tool


class NewsRAGConnector:
    """Registers ``search_news`` — semantic search over the embedded news corpus."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        self._cfg = config or load_config()

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        async def search_news(args: dict[str, Any]) -> dict[str, Any]:
            query = str(args.get("query") or "").strip()
            if not query:
                return {"ok": False, "message": "What should I search the news for?"}
            try:
                from himmy_app.news_index import get_news_index

                k = max(1, min(int(args.get("k") or 8), 25))
                hits = await asyncio.to_thread(get_news_index().search, query, k=k)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Couldn't search the news ({type(exc).__name__})."}
            if not hits:
                return {"ok": True, "results": [], "message": "No recent news matched that yet."}
            return {"ok": True, "count": len(hits), "results": [
                {"title": h["title"], "source": h["source"], "ago": h.get("ago", ""),
                 "lang": h.get("lang", ""), "url": h.get("url", ""), "snippet": h.get("snippet", "")[:200]}
                for h in hits
            ]}

        n = safe_register_local_tool(
            registry, name="search_news", read_only=True, handler=search_news,
            description=(
                "Semantically search ALL recent news Himmy has gathered — the embedded news corpus "
                "(Nepali AND English outlets), not just saved articles. Use for 'what's the news on "
                "X', 'latest on Y', 'has anything happened with Z'. Cross-lingual: an English query "
                "finds Nepali coverage too. Returns matching headlines with source + how long ago. "
                "Summarise the results for the user and cite the outlets."
            ),
            args_json_schema={"type": "object", "properties": {
                "query": {"type": "string"}, "k": {"type": "integer"}}, "required": ["query"]},
        )
        return [n] if n else []


__all__ = ["NewsRAGConnector"]
