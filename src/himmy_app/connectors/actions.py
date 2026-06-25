"""Action tools — Himmy's "hands" for managing the user's stuff.

Low-risk, additive actions run directly (save an article, add a paper). Outward-facing actions
that leave the app (send mail) are marked ``requires_approval=True`` so the runtime PAUSES for
the user's OK before doing them — the human-in-the-loop approval the app surfaces as a card.
"""

from __future__ import annotations

from typing import Any

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors._register import safe_register_local_tool


class ActionsConnector:
    """Registers save_article / add_paper (direct) + mail_send (approval-gated)."""

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        def _google_connected() -> bool:
            try:
                from himmy.api import studio_google as g
                return bool(g.status().connected)
            except Exception:  # noqa: BLE001
                return False

        async def save_article(args: dict[str, Any]) -> dict[str, Any]:
            from himmy_app.config import load_config
            from himmy_app.news import SavedNews

            url = str(args.get("url") or "").strip()
            if not url:
                return {"ok": False, "message": "Need the article's URL to save it."}
            try:
                r = await SavedNews(load_config()).save(
                    {"url": url, "title": args.get("title"), "source": args.get("source")},
                    str(args.get("folder") or "Reading List"),
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Couldn't save the article: {exc}"}
            return {"ok": bool(r.get("ok")), "folder": r.get("folder")}

        async def add_paper(args: dict[str, Any]) -> dict[str, Any]:
            from himmy_app.config import load_config
            from himmy_app.library import Library

            ident = str(args.get("identifier") or args.get("doi") or args.get("arxiv") or "").strip()
            if not ident:
                return {"ok": False, "message": "Need a DOI or arXiv id (e.g. 10.1038/… or 1706.03762)."}
            try:
                r = await Library(load_config()).add_identifier(ident)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Couldn't add the paper: {exc}"}
            if r.get("ok") and r.get("item"):
                it = r["item"]
                return {"ok": True, "added": it.get("title"), "id": it.get("id"),
                        "duplicate": bool(r.get("duplicate"))}
            return {"ok": False, "message": r.get("message", "Couldn't add that paper.")}

        async def mail_send(args: dict[str, Any]) -> dict[str, Any]:
            if not _google_connected():
                return {"ok": False, "message": "Connect a Google account first."}
            to = str(args.get("to") or "").strip()
            subject = str(args.get("subject") or "").strip()
            body = str(args.get("body") or "").strip()
            if not to or not body:
                return {"ok": False, "message": "Need at least a recipient (`to`) and a `body`."}
            try:
                from himmy.api import studio_google as g
                r = await g.gmail_send(to, subject, body)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Couldn't send the email: {exc}"}
            return {"ok": bool(getattr(r, "ok", True)), "detail": getattr(r, "detail", "sent")}

        safe_register_local_tool(
            registry, name="save_article", read_only=False, handler=save_article,
            description=(
                "Save a news article (by its URL) to the user's Reading List so they can read it "
                "later and Himmy can reference it. Optional `folder`, `title`, `source`."
            ),
            args_json_schema={"type": "object", "properties": {
                "url": {"type": "string"}, "title": {"type": "string"},
                "source": {"type": "string"}, "folder": {"type": "string"}}, "required": ["url"]},
        )
        safe_register_local_tool(
            registry, name="add_paper", read_only=False, handler=add_paper,
            description=(
                "Add a paper to the user's Library by DOI or arXiv id (fetches the metadata and "
                "the open-access PDF). Pass `identifier` — a DOI like 10.1038/nphys1170 or an "
                "arXiv id like 1706.03762."
            ),
            args_json_schema={"type": "object", "properties": {
                "identifier": {"type": "string"}}, "required": ["identifier"]},
        )
        safe_register_local_tool(
            registry, name="mail_send", read_only=False, requires_approval=True, handler=mail_send,
            description=(
                "Send a plain-text email from the user's connected Gmail. This REQUIRES the user's "
                "approval before it sends — they confirm in the app, so just propose it. Pass "
                "`to`, `subject`, `body`."
            ),
            args_json_schema={"type": "object", "properties": {
                "to": {"type": "string"}, "subject": {"type": "string"},
                "body": {"type": "string"}}, "required": ["to", "body"]},
        )
        return ["save_article", "add_paper", "mail_send"]


__all__ = ["ActionsConnector"]
