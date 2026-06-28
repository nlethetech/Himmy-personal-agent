"""Telegram bridge — talk to your Himmy from Telegram.

Polls the Telegram Bot API (via httpx — no extra dependency) for messages, runs each through the
SAME agent as the app (``ask_turn`` — so Telegram shares your Permissions, memory, tools, and
profile), and replies. It is **private to you**: the first chat to message a freshly-linked bot
becomes the owner (the bot token is secret, so trust-on-first-use is safe), and every other chat is
politely turned away. Approval-gated actions (e.g. sending mail) are confirmed by replying
"yes"/"no" in the chat. The bridge only runs when you set a bot token in Settings → Connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import re
from typing import Any

import httpx

from himmy_app.config import HimmyConfig, load_config

_API = "https://api.telegram.org"


# --------------------------------------------------------------------------------------------
# config (token + linked owner), persisted to telegram.json in the data dir
# --------------------------------------------------------------------------------------------
def _path(cfg: HimmyConfig):
    return cfg.data_dir / "telegram.json"


def load_tg(cfg: HimmyConfig | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    try:
        d = json.loads(_path(cfg).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_tg(data: dict[str, Any], cfg: HimmyConfig | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    cur = load_tg(cfg)
    cur.update(data)
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    return cur


def status(cfg: HimmyConfig | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    d = load_tg(cfg)
    tok = str(d.get("token") or "")
    return {
        "ok": True,
        "configured": bool(tok),
        "linked": bool(d.get("owner_chat_id")),
        "owner_chat_id": d.get("owner_chat_id"),
        "username": d.get("username"),
        "running": bool(_bridge and _bridge.running),
    }


# --------------------------------------------------------------------------------------------
# tiny markdown -> Telegram HTML (so **bold** / lists / links render, never raw ** )
# --------------------------------------------------------------------------------------------
def _to_html(md: str) -> str:
    text = html.escape(md or "")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)            # **bold**
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)  # *italic*
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)  # [t](url)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.M)          # bullets
    return text.strip()


def _chunks(s: str, n: int = 3900) -> list[str]:
    return [s[i:i + n] for i in range(0, len(s), n)] or [""]


# --------------------------------------------------------------------------------------------
# the bridge — a long-poll worker; one per process, started/stopped by the lifespan or endpoints
# --------------------------------------------------------------------------------------------
class TelegramBridge:
    def __init__(self, cfg: HimmyConfig | None = None) -> None:
        self.cfg = cfg or load_config()
        self.running = False
        self._task: asyncio.Task[Any] | None = None
        self._offset = 0
        self._pending: dict[int, str] = {}  # chat_id -> checkpoint_id awaiting yes/no

    # ---- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        if not load_tg(self.cfg).get("token"):
            return  # nothing to do until a token is set
        self._task = asyncio.get_event_loop().create_task(self._run(), name="himmy-telegram")

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def restart(self) -> None:
        await self.stop()
        self.start()

    # ---- the poll loop ---------------------------------------------------------------------
    async def _run(self) -> None:
        self.running = True
        d = load_tg(self.cfg)
        self._offset = int(d.get("offset") or 0)
        token = str(d.get("token") or "")
        # Record the bot's @username once (nice for the UI), best-effort.
        try:
            me = await self._api(token, "getMe", {})
            if me.get("ok"):
                save_tg({"username": me["result"].get("username")}, self.cfg)
        except Exception:  # noqa: BLE001
            pass
        try:
            while self.running:
                try:
                    res = await self._api(token, "getUpdates",
                                          {"offset": self._offset, "timeout": 25}, timeout=35)
                except Exception:  # noqa: BLE001 - transient network/poll error → brief backoff
                    await asyncio.sleep(3)
                    continue
                for upd in (res.get("result") or []):
                    self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                    save_tg({"offset": self._offset}, self.cfg)
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    chat = (msg.get("chat") or {}).get("id")
                    if chat is not None:
                        with contextlib.suppress(Exception):
                            await self._dispatch(token, int(chat), msg)
        finally:
            self.running = False

    # ---- message handling ------------------------------------------------------------------
    async def _dispatch(self, token: str, chat_id: int, msg: dict[str, Any]) -> None:
        """Owner-gate the chat, then route to the media (file/photo/voice) or the text handler."""
        d = load_tg(self.cfg)
        owner = d.get("owner_chat_id")
        # Pair on first contact (the token is secret → trust-on-first-use is safe).
        if not owner:
            save_tg({"owner_chat_id": chat_id}, self.cfg)
            await self._send(token, chat_id,
                             "✅ Linked! I'm your Himmy now — ask me anything: your day, your mail, "
                             "tasks, the news, a flight, a trip… You can also send me files, photos "
                             "and voice notes and I'll read them. I share the permissions you set in "
                             "the app.")
            return
        if int(owner) != chat_id:
            await self._send(token, chat_id, "Sorry — this is a private assistant. 🔒")
            return

        # A file / photo / voice note? Read it (and answer its caption if there is one).
        if self._media_ref(msg)[0]:
            await self._handle_media(token, chat_id, msg)
            return

        text = (msg.get("text") or "").strip()
        if text:
            await self._handle_text(token, chat_id, text)

    async def _handle_text(self, token: str, chat_id: int, text: str) -> None:
        if text.lower() in {"/start", "/help"}:
            await self._send(token, chat_id, "Hi 👋 I'm Himmy. Ask me about your day, mail, tasks, "
                             "news, food, flights or a trip — or send me a file, a photo, or a voice "
                             "note and I'll read it. Actions that send or change things ask you to "
                             "reply 'yes' to confirm.")
            return

        # Resolve a pending approval with a yes/no reply.
        pend = self._pending.get(chat_id)
        if pend and text.lower() in {"yes", "y", "ok", "confirm", "no", "n", "cancel", "stop"}:
            approved = text.lower() in {"yes", "y", "ok", "confirm"}
            self._pending.pop(chat_id, None)
            from himmy_app.cli import resume_turn

            await self._send(token, chat_id, "Working on it…" if approved else "Okay, cancelled.")
            res = await resume_turn(pend, approved=approved, session_id=f"tg-{chat_id}")
            await self._reply(token, chat_id, res)
            return

        await self._api(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
        from himmy_app.cli import ask_turn

        res = await ask_turn(text, session_id=f"tg-{chat_id}")
        await self._reply(token, chat_id, res)

    # ---- attachments (documents / photos / voice notes) ------------------------------------
    @staticmethod
    def _media_ref(msg: dict[str, Any]) -> tuple[str, str, str]:
        """(file_id, filename, mime) for a media message, or ("", "", "") if it isn't one.

        Telegram sends a photo as several sizes (we take the largest), a voice note as ``voice``
        (OGG/Opus), and any other file as ``document`` (with its real name + mime).
        """
        if msg.get("document"):
            doc = msg["document"]
            return (str(doc.get("file_id") or ""), str(doc.get("file_name") or "file"),
                    str(doc.get("mime_type") or ""))
        if msg.get("photo"):
            sizes = msg["photo"] or []
            if sizes:
                return (str(sizes[-1].get("file_id") or ""), "photo.jpg", "image/jpeg")
        if msg.get("voice"):
            v = msg["voice"]
            return (str(v.get("file_id") or ""), "voice-note.ogg", str(v.get("mime_type") or "audio/ogg"))
        if msg.get("audio"):
            a = msg["audio"]
            return (str(a.get("file_id") or ""), str(a.get("file_name") or "audio"),
                    str(a.get("mime_type") or "audio/mpeg"))
        return ("", "", "")

    async def _download_file(self, token: str, file_id: str) -> bytes | None:
        """Resolve a Telegram file_id to its bytes (getFile → download)."""
        try:
            gf = await self._api(token, "getFile", {"file_id": file_id})
            if not gf.get("ok"):
                return None
            file_path = (gf.get("result") or {}).get("file_path")
            if not file_path:
                return None
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.get(f"{_API}/file/bot{token}/{file_path}")
            return r.content if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            return None

    async def _handle_media(self, token: str, chat_id: int, msg: dict[str, Any]) -> None:
        """Download an attachment, read it into Himmy's knowledge, and either answer its caption or
        confirm receipt. Reuses the SAME pipeline as the app (AttachmentStore + the papers RAG)."""
        file_id, name, mime = self._media_ref(msg)
        await self._api(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
        data = await self._download_file(token, file_id)
        if not data:
            await self._send(token, chat_id, "I couldn't download that file, sorry — try again?")
            return

        from himmy_app import permissions
        from himmy_app.attachments import AttachmentStore

        read_media = permissions.level_of("files", self.cfg) != "off"
        att = await AttachmentStore(self.cfg).ingest(
            name, data, mime, source="telegram", session_id=f"tg-{chat_id}", read_media=read_media)
        # Warm the index so a follow-up question finds it immediately (best-effort).
        try:
            from himmy_app.connectors.papers_rag import _get_index

            await _get_index(self.cfg).sync()
        except Exception:  # noqa: BLE001
            pass

        if att.get("empty"):
            await self._send(
                token, chat_id,
                f"📎 Got “{att['name']}”, but I couldn't read it. Reading images/voice notes may be "
                "off (Settings → Permissions), or the current model can't see/hear — OpenRouter "
                "(gemini-2.5-flash) can. PDFs and text always work.")
            return

        caption = (msg.get("caption") or "").strip()
        if caption:
            # Answer the caption, grounded on the file's contents (inlined as context for this turn).
            from himmy_app.cli import ask_turn

            prompt = (f"{caption}\n\n[The user attached a file “{att['name']}”. Its contents:]\n"
                      f"{att['text']}")
            res = await ask_turn(prompt, session_id=f"tg-{chat_id}")
            await self._reply(token, chat_id, res)
            return

        did = {"image": "read your image", "audio": "transcribed your voice note"}.get(
            att.get("kind", ""), f"read “{att['name']}”")
        await self._send(token, chat_id,
                         f"📎 Got it — I've {did} ({att['chars']:,} chars in). Ask me anything "
                         "about it.")

    async def _reply(self, token: str, chat_id: int, res: dict[str, Any]) -> None:
        if res.get("awaiting_approval") and res.get("checkpoint_id"):
            self._pending[chat_id] = res["checkpoint_id"]
            pend = (res.get("pending") or [{}])[0]
            what = str(pend.get("tool_name") or "this action").replace("_", " ")
            await self._send(token, chat_id, f"I'd like to *{what}* for you. Reply *yes* to approve "
                             "or *no* to cancel.")
            return
        reply = (res.get("reply") or "").strip() or "(no reply)"
        await self._send(token, chat_id, reply)

    # ---- Telegram API ----------------------------------------------------------------------
    async def _send(self, token: str, chat_id: int, text: str) -> None:
        for part in _chunks(text):
            ok = await self._send_one(token, chat_id, part, html_mode=True)
            if not ok:  # HTML parse failed → resend as plain text rather than drop it
                await self._send_one(token, chat_id, part, html_mode=False)

    async def _send_one(self, token: str, chat_id: int, text: str, *, html_mode: bool) -> bool:
        body: dict[str, Any] = {"chat_id": chat_id,
                                "text": _to_html(text) if html_mode else text,
                                "disable_web_page_preview": True}
        if html_mode:
            body["parse_mode"] = "HTML"
        try:
            r = await self._api(token, "sendMessage", body)
            return bool(r.get("ok"))
        except Exception:  # noqa: BLE001
            return False

    async def _api(self, token: str, method: str, body: dict[str, Any], *, timeout: float = 20) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{_API}/bot{token}/{method}", json=body)
        return r.json()


# Module singleton (the lifespan + the /telegram endpoints share it).
_bridge: TelegramBridge | None = None


def get_bridge(cfg: HimmyConfig | None = None) -> TelegramBridge:
    global _bridge
    if _bridge is None:
        _bridge = TelegramBridge(cfg)
    return _bridge


async def verify_token(token: str) -> dict[str, Any]:
    """Check a bot token with getMe (so the UI can confirm it before saving)."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{_API}/bot{token.strip()}/getMe")
        d = r.json()
        if d.get("ok"):
            return {"ok": True, "username": d["result"].get("username"), "name": d["result"].get("first_name")}
        return {"ok": False, "message": "Telegram rejected that token."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}"}


__all__ = ["TelegramBridge", "get_bridge", "load_tg", "save_tg", "status", "verify_token"]
