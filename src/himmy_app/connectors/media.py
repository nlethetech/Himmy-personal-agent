"""Media connector — let Himmy READ images and HEAR audio (the one gap in a text-only framework).

The himmy framework's inference layer is **text-only** by design: ``InferenceMessage.content`` is a
plain string and no provider adapter builds a multimodal payload. So a screenshot or a voice note
can't ride ``build_inference_for``. This connector fills that gap the framework-native way:

  * two GENERIC, framework-agnostic core functions — :func:`image_to_text` and
    :func:`audio_to_text` (``bytes → text``) — that make a thin, isolated DIRECT call to the
    configured provider's multimodal API. They depend on nothing app-specific, so they are
    ready to be **promoted into the himmy framework** as a ``multimodal`` service/pack later;
  * a :class:`MediaConnector` that exposes them as first-class agent **tools** (``read_image`` /
    ``transcribe_audio``) over the user's uploaded attachments, registered through himmy's tool
    registry exactly like every other connector.

Once the framework grows native multimodal inference, the two core functions can switch to it with
no change to the tool interface or the attachment pipeline that calls them.

Design rules (provider-aware + fail-soft):
  * OpenRouter / OpenAI / openai-compatible → chat-completions content parts (``image_url`` for
    images, ``input_audio`` for audio). Anthropic → image blocks (no audio). Anything else → "".
  * A missing key, an unreachable host, or a non-multimodal model returns "" — never an exception
    that could break an upload or a turn.
  * The key is resolved the FRAMEWORK way (``himmy.config.secrets.get_secret``), so it works whether
    the key lives in the OS keychain or the environment.

HONEST NOTE: a direct provider call can't be metered into ``/usage`` (the framework can't carry the
image/audio), exactly like the app's existing ``library._ai_extract``. That's the unavoidable cost
of text-only inference; everything downstream of the extracted text stays on the framework path.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from himmy.services.tools.registry import ToolRegistry

from himmy_app.config import HimmyConfig, load_config
from himmy_app.connectors._register import safe_register_local_tool

# --------------------------------------------------------------------------------------------
# Core (generic, framework-agnostic, upstreamable): bytes -> text via a direct multimodal call
# --------------------------------------------------------------------------------------------

#: provider id -> chat-completions base URL. openai-compatible resolves its base from the env the
#: model picker exports (HIMMY_OPENAI_COMPAT_BASE_URL); anthropic is handled on its own path.
_OPENAI_STYLE_BASE = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
}

#: Audio container -> the ``format`` string OpenAI-style ``input_audio`` expects.
_AUDIO_FORMAT = {
    "audio/ogg": "ogg", "audio/oga": "ogg", "application/ogg": "ogg", "audio/opus": "ogg",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "mp4", "audio/m4a": "m4a",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "webm", "audio/flac": "flac",
}


def _key(provider: str) -> str:
    """The API key for ``provider``, resolved via himmy's secret chain (keychain → env)."""
    try:
        from himmy.config.secrets import get_secret

        from himmy_app.provider_keys import PROVIDER_KEY_NAMES

        name = PROVIDER_KEY_NAMES.get(provider)
        return (get_secret(name) or "") if name else ""
    except Exception:  # noqa: BLE001 - a secret lookup must never raise into an upload/turn
        return ""


def _openai_base(provider: str) -> str | None:
    """The chat-completions base for an OpenAI-style provider, or None if unsupported here."""
    if provider in _OPENAI_STYLE_BASE:
        return _OPENAI_STYLE_BASE[provider]
    if provider == "openai-compatible":
        import os

        base = (os.environ.get("HIMMY_OPENAI_COMPAT_BASE_URL") or "").strip().rstrip("/")
        return base or None
    return None


async def _chat_parts(cfg: HimmyConfig, system: str, parts: list[dict[str, Any]]) -> str:
    """Run ONE multimodal chat-completions turn (OpenAI-style) and return the text reply.

    ``parts`` is the user message's content array (a text instruction plus an image_url/input_audio
    part). Returns "" on any failure so the caller degrades gracefully.
    """
    if cfg.provider == "anthropic":
        return await _anthropic_image(cfg, system, parts)
    base = _openai_base(cfg.provider)
    key = _key(cfg.provider)
    if not base or not key:
        return ""
    model = cfg.model or "google/gemini-2.5-flash"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model, "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": parts},
                    ],
                },
            )
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:  # noqa: BLE001 - any provider/transport error → graceful empty
        return ""


async def _anthropic_image(cfg: HimmyConfig, system: str, parts: list[dict[str, Any]]) -> str:
    """Anthropic's Messages API variant (image blocks only; it has no audio input)."""
    key = _key("anthropic")
    if not key:
        return ""
    blocks: list[dict[str, Any]] = []
    for p in parts:
        if p.get("type") == "text":
            blocks.append({"type": "text", "text": p.get("text", "")})
        elif p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                head, b64 = url.split(";base64,", 1)
                media = head.split("data:", 1)[-1] or "image/png"
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": media, "data": b64}})
    if not blocks:
        return ""
    model = cfg.model or "claude-haiku-4-5-20251001"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "Content-Type": "application/json"},
                json={"model": model, "max_tokens": 1500, "system": system,
                      "messages": [{"role": "user", "content": blocks}]},
            )
        resp.raise_for_status()
        data = resp.json()
        chunks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(chunks).strip()
    except Exception:  # noqa: BLE001
        return ""


def vision_available(cfg: HimmyConfig | None = None) -> bool:
    """True when the configured provider can read images here (so the UI can be honest)."""
    cfg = cfg or load_config()
    if cfg.provider == "anthropic":
        return bool(_key("anthropic"))
    return bool(_openai_base(cfg.provider) and _key(cfg.provider))


async def image_to_text(data: bytes, mime: str, cfg: HimmyConfig | None = None) -> str:
    """Read the TEXT (and a one-line description) out of an image/screenshot.

    Transcribes any text in the image verbatim and appends a 'Description:' line, so the result is
    useful both as immediate chat context and as a RAG document. Returns "" if the provider can't
    see images.
    """
    cfg = cfg or load_config()
    b64 = base64.b64encode(data).decode("ascii")
    mime = mime or "image/png"
    system = (
        "You extract information from an image for a personal assistant. Transcribe ALL readable "
        "text in the image verbatim and faithfully (preserve numbers, dates, names, totals, line "
        "items, tables). Then add a final line 'Description: …' summarising what the image shows in "
        "one sentence. If there is no readable text, just give the description. Output plain text."
    )
    parts = [
        {"type": "text", "text": "Read this image."},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]
    return await _chat_parts(cfg, system, parts)


async def audio_to_text(data: bytes, mime: str, cfg: HimmyConfig | None = None) -> str:
    """Transcribe a voice note / audio clip to text. "" if the provider can't hear audio.

    Uses the OpenAI-style ``input_audio`` content part (supported by audio-capable models such as
    Google Gemini 2.5 on OpenRouter). Anthropic has no audio input → "".
    """
    cfg = cfg or load_config()
    if cfg.provider == "anthropic":
        return ""
    fmt = _AUDIO_FORMAT.get((mime or "").lower().split(";")[0], "ogg")
    b64 = base64.b64encode(data).decode("ascii")
    system = (
        "You transcribe a voice note for a personal assistant. Return ONLY the verbatim "
        "transcription of the speech, with no preamble, commentary, or quotation marks."
    )
    parts = [
        {"type": "text", "text": "Transcribe this audio."},
        {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}},
    ]
    return await _chat_parts(cfg, system, parts)


# --------------------------------------------------------------------------------------------
# Connector — expose the core as first-class agent tools over the user's uploaded attachments
# --------------------------------------------------------------------------------------------
class MediaConnector:
    """Registers ``read_image`` (OCR a screenshot/photo) + ``transcribe_audio`` (a voice note).

    Both operate on an uploaded ATTACHMENT (by id, or the most recent of that media type), read its
    stored bytes, and run the matching core function. This is the framework-native seam over the
    generic core above — the same shape every other connector uses.
    """

    def __init__(self, config: HimmyConfig | None = None) -> None:
        self._cfg = config or load_config()

    def _resolve(self, kind: str, attachment_id: str) -> dict[str, Any] | None:
        from himmy_app.attachments import AttachmentStore

        store = AttachmentStore(self._cfg)
        att = store.get(attachment_id) if attachment_id else store.latest(kind)
        return att

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        cfg = self._cfg

        async def read_image(args: dict[str, Any]) -> dict[str, Any]:
            from himmy_app.attachments import AttachmentStore

            att = self._resolve("image", str(args.get("attachment_id") or "").strip())
            if not att:
                return {"ok": False, "message": "No image attachment found to read. Ask the user "
                        "to drop a screenshot or photo into the chat (or send one on Telegram)."}
            blob = AttachmentStore(cfg).blob_bytes(att["id"])
            if not blob:
                return {"ok": False, "message": "That image's file is missing."}
            text = await image_to_text(blob, att.get("mime") or "image/png", cfg)
            if not text:
                return {"ok": False, "message": "Couldn't read that image — the current model may "
                        "not support vision. Switch to OpenRouter (gemini-2.5-flash) in "
                        "Account → Preferences."}
            return {"ok": True, "name": att.get("name"), "text": text}

        async def transcribe_audio(args: dict[str, Any]) -> dict[str, Any]:
            from himmy_app.attachments import AttachmentStore

            att = self._resolve("audio", str(args.get("attachment_id") or "").strip())
            if not att:
                return {"ok": False, "message": "No audio attachment found to transcribe."}
            blob = AttachmentStore(cfg).blob_bytes(att["id"])
            if not blob:
                return {"ok": False, "message": "That audio's file is missing."}
            text = await audio_to_text(blob, att.get("mime") or "audio/ogg", cfg)
            if not text:
                return {"ok": False, "message": "Couldn't transcribe that audio — the current "
                        "model may not support audio. OpenRouter (gemini-2.5-flash) does."}
            return {"ok": True, "name": att.get("name"), "text": text}

        names: list[str] = []
        n = safe_register_local_tool(
            registry, name="read_image", read_only=True, handler=read_image,
            description=(
                "Read the text and content out of an IMAGE the user uploaded (a screenshot, a photo "
                "of a bill/whiteboard/menu, a scanned page). Optional `attachment_id`; omit it to "
                "read the most recently uploaded image. Returns the transcribed text plus a one-line "
                "description. Use this whenever the user refers to a picture/screenshot they sent."
            ),
            args_json_schema={"type": "object", "properties": {
                "attachment_id": {"type": "string",
                                  "description": "The uploaded image's id; omit for the latest."}}},
        )
        if n:
            names.append(n)
        n = safe_register_local_tool(
            registry, name="transcribe_audio", read_only=True, handler=transcribe_audio,
            description=(
                "Transcribe a VOICE NOTE or audio clip the user sent (e.g. on Telegram) to text. "
                "Optional `attachment_id`; omit it to transcribe the most recent audio. Returns the "
                "verbatim transcription. Use this when the user sends or refers to a voice message."
            ),
            args_json_schema={"type": "object", "properties": {
                "attachment_id": {"type": "string",
                                  "description": "The uploaded audio's id; omit for the latest."}}},
        )
        if n:
            names.append(n)
        return names


__all__ = ["MediaConnector", "image_to_text", "audio_to_text", "vision_available"]
