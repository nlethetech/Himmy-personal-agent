"""Gmail "hands" — Himmy can triage, read, reply to, and draft email.

The built-in google pack's ``gmail_inbox`` returns only from/subject/snippet — no message id — so
the agent can't act on a *specific* message. These tools fix that:

* ``mail_list``  — recent inbox WITH ids, so Himmy can target a message.
* ``mail_read``  — the full text of one message (to actually read / summarise / quote it).
* ``mail_reply`` — a correctly-THREADED reply (approval-gated: it leaves the mailbox).
* ``mail_draft`` — saves a reply (or a new email) to Gmail Drafts. Reversible — nothing is sent —
  so it runs directly; the user reviews and sends it themselves from Gmail.

Replies/drafts derive the recipient, the ``Re:`` subject, the thread, and the ``In-Reply-To`` /
``References`` headers from the original message, so the model only has to supply the body.
"""

from __future__ import annotations

from email.utils import parseaddr
from typing import Any

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors._register import safe_register_local_tool


def _reply_to(sender: str) -> str:
    """The bare address to reply to, pulled from a 'Name <addr>' From header."""
    name, addr = parseaddr(sender or "")
    return (addr or sender or "").strip()


def _re_subject(subject: str) -> str:
    s = (subject or "").strip() or "(no subject)"
    return s if s.lower().startswith("re:") else f"Re: {s}"


class GmailActionsConnector:
    """Registers mail_list / mail_read (read) + mail_reply (gated) + mail_draft (direct)."""

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        def _connected() -> bool:
            try:
                from himmy.api import studio_google as g
                return bool(g.status().connected)
            except Exception:  # noqa: BLE001
                return False

        async def mail_list(args: dict[str, Any]) -> dict[str, Any]:
            if not _connected():
                return {"ok": False, "message": "No Google account connected — connect it in the Mail tab."}
            from himmy.api import studio_google as g
            limit = max(1, min(int(args.get("limit") or 12), 30))
            try:
                msgs = await g.gmail_list(limit)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Couldn't read the inbox: {exc}"}
            return {"ok": True, "messages": [
                {"id": m.id, "from": m.sender, "subject": m.subject, "snippet": m.snippet, "date": m.date}
                for m in msgs
            ]}

        async def mail_read(args: dict[str, Any]) -> dict[str, Any]:
            if not _connected():
                return {"ok": False, "message": "No Google account connected."}
            mid = str(args.get("message_id") or "").strip()
            if not mid:
                return {"ok": False, "message": "Need the `message_id` (from mail_list) to read a message."}
            from himmy.api import studio_google as g
            try:
                m = await g.gmail_get(mid)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Couldn't read that message: {exc}"}
            return {"ok": True, "id": m.id, "from": m.sender, "to": m.to, "subject": m.subject,
                    "date": m.date, "body": (m.body or m.snippet or "")[:6000]}

        async def _compose_reply(args: dict[str, Any], *, draft: bool) -> dict[str, Any]:
            from himmy.api import studio_google as g
            mid = str(args.get("message_id") or "").strip()
            body = str(args.get("body") or "").strip()
            if not body:
                return {"ok": False, "message": "Need a `body` for the message."}
            cc = (str(args.get("cc") or "").strip() or None)
            if mid:  # reply to an existing message — derive everything from the original
                try:
                    orig = await g.gmail_get(mid)
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "message": f"Couldn't read the message to reply to: {exc}"}
                to = _reply_to(orig.sender)
                if not to:
                    return {"ok": False, "message": "Couldn't find a reply address on that message."}
                r = await g.gmail_send(
                    to, _re_subject(orig.subject), body, cc=cc,
                    thread_id=orig.thread_id, in_reply_to=orig.message_id_header,
                    references=orig.references, draft=draft,
                )
            else:  # a fresh email (draft path only — sending fresh mail uses mail_send)
                to = str(args.get("to") or "").strip()
                if not to:
                    return {"ok": False, "message": "Need a `message_id` to reply, or a `to` to draft a new email."}
                r = await g.gmail_send(to, str(args.get("subject") or ""), body, cc=cc, draft=draft)
            ok = bool(getattr(r, "ok", True))
            detail = str(getattr(r, "detail", "done"))
            if not ok:
                low = detail.lower()
                # Saving a draft needs gmail.compose; an account connected before that scope existed
                # will 403 here until it's reconnected. Say exactly that, not "auth error".
                if draft and any(s in low for s in ("scope", "insufficient", "permission", "403")):
                    return {"ok": False, "message": (
                        "Saving drafts needs one extra Gmail permission. Please reconnect your "
                        "Google account in the Mail tab (it asks once), then try again.")}
                return {"ok": False, "message": f"Gmail error: {detail}"}
            return {"ok": True, "to": to, "detail": detail}

        async def mail_reply(args: dict[str, Any]) -> dict[str, Any]:
            if not _connected():
                return {"ok": False, "message": "No Google account connected."}
            if not str(args.get("message_id") or "").strip():
                return {"ok": False, "message": "Need the `message_id` of the email to reply to (from mail_list)."}
            try:
                return await _compose_reply(args, draft=False)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Couldn't send the reply: {exc}"}

        async def mail_draft(args: dict[str, Any]) -> dict[str, Any]:
            if not _connected():
                return {"ok": False, "message": "No Google account connected."}
            try:
                return await _compose_reply(args, draft=True)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Couldn't save the draft: {exc}"}

        safe_register_local_tool(
            registry, name="mail_list", read_only=True, handler=mail_list,
            description=(
                "List the user's recent inbox messages WITH their ids. Use this first to find the "
                "specific email to read or reply to. Optional `limit` (default 12). Returns "
                "[{id, from, subject, snippet, date}]."
            ),
            args_json_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
        )
        safe_register_local_tool(
            registry, name="mail_read", read_only=True, handler=mail_read,
            description=(
                "Read the FULL text of one inbox message by `message_id` (from mail_list) — use it "
                "to actually read, summarise, or quote an email before replying."
            ),
            args_json_schema={"type": "object", "properties": {
                "message_id": {"type": "string"}}, "required": ["message_id"]},
        )
        safe_register_local_tool(
            registry, name="mail_reply", read_only=False, requires_approval=True, handler=mail_reply,
            description=(
                "Reply to an inbox message (correctly threaded). Pass the `message_id` (from "
                "mail_list) and your `body` — Himmy fills in the recipient, the Re: subject and the "
                "thread automatically. This SENDS, so it REQUIRES the user's approval: just call it "
                "with your best draft and the app will show an approval card. Optional `cc`."
            ),
            args_json_schema={"type": "object", "properties": {
                "message_id": {"type": "string"}, "body": {"type": "string"},
                "cc": {"type": "string"}}, "required": ["message_id", "body"]},
        )
        safe_register_local_tool(
            registry, name="mail_draft", read_only=False, handler=mail_draft,
            description=(
                "Save a reply (or a new email) to the user's Gmail DRAFTS without sending — "
                "reversible, so no approval needed; the user reviews and sends it from Gmail. For a "
                "reply pass `message_id` + `body`; for a new draft pass `to` + `body` (+ optional "
                "`subject`, `cc`). Prefer this when the user says 'draft' / 'write me a reply'."
            ),
            args_json_schema={"type": "object", "properties": {
                "message_id": {"type": "string"}, "to": {"type": "string"},
                "subject": {"type": "string"}, "body": {"type": "string"},
                "cc": {"type": "string"}}, "required": ["body"]},
        )
        return ["mail_list", "mail_read", "mail_reply", "mail_draft"]


__all__ = ["GmailActionsConnector"]
