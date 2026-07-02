"""Attachments — "hand Himmy a file and it reads it", then remembers it.

A user drops a file into the ⌘K chat (or sends one on Telegram); Himmy turns it into TEXT and
keeps it, so it can answer about it now AND days later. The text flows into the SAME himmy
``KnowledgeBase`` that powers ``ask_papers`` (see :mod:`himmy_app.connectors.papers_rag`), so the
file becomes searchable + citeable with no new RAG machinery — attachments are simply a third
source merged alongside the library and saved news.

Parsing reuses the FRAMEWORK's document reader factory
(:class:`himmy.services.knowledge.readers.DocumentReaderFactory` — PDF/txt/md/csv/xlsx), which we
extend with a dependency-free ``.docx`` and ``.html`` reader registered straight into it. Images
and audio (which the text-only framework can't ingest) go through the :mod:`himmy_app.connectors.media`
connector's core (``image_to_text`` / ``audio_to_text``).

This module owns only the small blob+text store (an ``attachments.db`` catalogue + an
``attachment_files/`` directory) — the same way :class:`himmy_app.library.Library` owns the PDFs.
Both ride the workspace backup.
"""

from __future__ import annotations

import asyncio
import html
import re
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Any

from himmy.services.knowledge.readers import DocumentReader, DocumentReaderFactory

from himmy_app.config import HimmyConfig, load_config

#: Hard cap on the text we store/index per file (the KB caps the body at 60k anyway).
_MAX_TEXT = 200_000
#: How much extracted text we hand back for IMMEDIATE chat context (the turn right after upload).
#: Later turns retrieve from RAG instead, so this only needs to ground the first question.
_CONTEXT_CHARS = 14_000
#: A short, single-line preview for the "Files Himmy has read" list.
_PREVIEW_CHARS = 240

#: Extensions the (extended) framework reader factory can turn into text.
_DOC_EXTS = {".txt", ".md", ".csv", ".xlsx", ".xlsm", ".pdf", ".docx", ".html", ".htm"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif", ".tif", ".tiff"}
_AUDIO_EXTS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".webm", ".flac"}


# --------------------------------------------------------------------------------------------
# Extend the framework reader factory with .docx + .html (dependency-free)
# --------------------------------------------------------------------------------------------
class DocxReader(DocumentReader):
    """Reads a ``.docx`` (a zip whose ``word/document.xml`` holds the text runs). No dependency."""

    extensions = (".docx",)

    def read(self, path: str) -> str:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        # Paragraph + line-break tags become newlines; everything else is stripped to its text.
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:br\s*/?>", "\n", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        return html.unescape(text)


class HtmlReader(DocumentReader):
    """Reads an ``.html``/``.htm`` file to text (drops script/style, strips tags). No dependency."""

    extensions = (".html", ".htm")

    def read(self, path: str) -> str:
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
        raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
        raw = re.sub(r"(?i)</p\s*>", "\n", raw)
        text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        return re.sub(r"[ \t]+", " ", text).strip()


def _factory() -> DocumentReaderFactory:
    """The framework factory, extended with our .docx + .html readers."""
    f = DocumentReaderFactory()
    f.register(DocxReader())
    f.register(HtmlReader())
    return f


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _ext_for(name: str, mime: str) -> str:
    """A file extension for ``name`` (preferred) or inferred from ``mime`` (Telegram gives generic
    names but a real mime)."""
    ext = Path(name or "").suffix.lower()
    if ext:
        return ext
    m = (mime or "").lower().split(";")[0]
    by_mime = {
        "application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md",
        "text/csv": ".csv", "text/html": ".html",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
        "image/heic": ".heic", "image/tiff": ".tif",
        "audio/ogg": ".ogg", "audio/opus": ".ogg", "application/ogg": ".ogg", "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/wav": ".wav", "audio/webm": ".webm",
    }
    return by_mime.get(m, "")


def _kind_for(ext: str, mime: str) -> str:
    """One of: image | audio | doc | file — chosen by mime first, then extension."""
    m = (mime or "").lower()
    if m.startswith("image/") or ext in _IMAGE_EXTS:
        return "image"
    if m.startswith("audio/") or m == "application/ogg" or ext in _AUDIO_EXTS:
        return "audio"
    if ext in _DOC_EXTS:
        return "doc"
    return "file"


class AttachmentStore:
    """The catalogue (``attachments.db``) + blob directory (``attachment_files/``)."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        cfg = config or load_config()
        self._cfg = cfg
        self._db = cfg.data_dir / "attachments.db"
        self._files = cfg.data_dir / "attachment_files"
        self._files.mkdir(parents=True, exist_ok=True)
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY, name TEXT, kind TEXT, mime TEXT, ext TEXT,
                    size INTEGER, chars INTEGER, text TEXT, preview TEXT,
                    source TEXT, session_id TEXT, added_at REAL
                )"""
            )

    # ---- helpers ------------------------------------------------------------------------
    @staticmethod
    def _new_id() -> str:
        return f"att_{int(time.time() * 1000):x}"

    def _blob_path(self, att_id: str, ext: str) -> Path:
        return self._files / f"{att_id}{ext or ''}"

    def _extract(self, kind: str, ext: str, path: Path, data: bytes, mime: str) -> str:
        """Turn the stored file into text. Reuses the framework reader for docs; the media
        connector for images/audio; a best-effort UTF-8 decode for anything else."""
        if kind == "doc":
            try:
                return _factory().read(str(path))
            except Exception:  # noqa: BLE001 - a malformed doc must not break the upload
                pass
            return data.decode("utf-8", "ignore")
        if kind == "file":
            # An unknown type — try to read it as text; if it's binary this yields little, which is
            # fine (the file is still stored and listed).
            return data.decode("utf-8", "ignore")
        # image / audio are extracted asynchronously by :meth:`ingest` (they need an await).
        return ""

    # ---- ingest -------------------------------------------------------------------------
    async def ingest(self, name: str, data: bytes, mime: str = "",
                     *, source: str = "chat", session_id: str | None = None,
                     read_media: bool = True) -> dict[str, Any]:
        """Store ``data`` as an attachment, extract its text, and return a summary dict.

        The returned ``text`` (capped) is for IMMEDIATE context (the question asked alongside the
        upload); the full text is persisted and indexed into RAG for later. Always returns a
        well-formed dict — extraction failures just yield empty/short text, never an exception.

        ``read_media`` gates the image/audio extraction (the "Files & media" permission): when
        False, an image/voice file is still stored but not read by a model (no OCR/transcription).
        """
        att_id = self._new_id()
        ext = _ext_for(name, mime)
        kind = _kind_for(ext, mime)
        path = self._blob_path(att_id, ext)
        try:
            path.write_bytes(data)
        except Exception:  # noqa: BLE001
            path = self._blob_path(att_id, "")
            path.write_bytes(data)

        # Doc/file text extraction is CPU-heavy (a big PDF can take seconds) and SYNCHRONOUS, so
        # run it on a worker thread — otherwise parsing an upload freezes the whole backend (every
        # request + background loop stalls). image/audio need the async media core (and the user's
        # "Files & media" permission — when off we still store the file but don't read it).
        text = await asyncio.to_thread(self._extract, kind, ext, path, data, mime)
        if kind == "image" and read_media:
            from himmy_app.connectors.media import image_to_text

            text = await image_to_text(data, mime or "image/png", self._cfg)
        elif kind == "audio" and read_media:
            from himmy_app.connectors.media import audio_to_text

            text = await audio_to_text(data, mime or "audio/ogg", self._cfg)

        text = (text or "")[:_MAX_TEXT]
        preview = _collapse(text)[:_PREVIEW_CHARS]
        row = {
            "id": att_id, "name": (name or "file").strip() or "file", "kind": kind,
            "mime": mime or "", "ext": ext, "size": len(data), "chars": len(text),
            "text": text, "preview": preview, "source": source,
            "session_id": session_id or "", "added_at": time.time(),
        }
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO attachments
                   (id,name,kind,mime,ext,size,chars,text,preview,source,session_id,added_at)
                   VALUES (:id,:name,:kind,:mime,:ext,:size,:chars,:text,:preview,:source,
                           :session_id,:added_at)""",
                row,
            )
        return {
            "id": att_id, "name": row["name"], "kind": kind, "mime": row["mime"],
            "size": row["size"], "chars": row["chars"], "preview": preview,
            "text": text[:_CONTEXT_CHARS],
            # True when an image/audio file produced no text (no vision/audio model available).
            "empty": kind in {"image", "audio"} and not text,
        }

    # ---- reads --------------------------------------------------------------------------
    def _row(self, r: sqlite3.Row) -> dict[str, Any]:
        return dict(r)

    def get(self, att_id: str) -> dict[str, Any] | None:
        if not att_id:
            return None
        with self._conn() as c:
            r = c.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
        return self._row(r) if r else None

    def latest(self, kind: str = "") -> dict[str, Any] | None:
        with self._conn() as c:
            if kind:
                r = c.execute(
                    "SELECT * FROM attachments WHERE kind = ? ORDER BY added_at DESC LIMIT 1",
                    (kind,)).fetchone()
            else:
                r = c.execute(
                    "SELECT * FROM attachments ORDER BY added_at DESC LIMIT 1").fetchone()
        return self._row(r) if r else None

    def blob_bytes(self, att_id: str) -> bytes | None:
        att = self.get(att_id)
        if not att:
            return None
        p = self._blob_path(att_id, att.get("ext") or "")
        if not p.exists():  # fall back to the extensionless name if needed
            p = self._blob_path(att_id, "")
        try:
            return p.read_bytes() if p.exists() else None
        except Exception:  # noqa: BLE001
            return None

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        """Catalogue rows (newest first) WITHOUT the heavy ``text`` column — for the Files list."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT id,name,kind,mime,ext,size,chars,preview,source,session_id,added_at
                   FROM attachments ORDER BY added_at DESC LIMIT ?""", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) AS n FROM attachments").fetchone()["n"])

    def delete(self, att_id: str) -> dict[str, Any]:
        att = self.get(att_id)
        with self._conn() as c:
            c.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
        if att:
            p = self._blob_path(att_id, att.get("ext") or "")
            for cand in (p, self._blob_path(att_id, "")):
                try:
                    cand.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        return {"ok": True}

    # ---- RAG feed (merged into PapersIndex alongside library + saved news) ---------------
    def rag_records(self) -> list[dict[str, Any]]:
        """Records the papers RAG indexer merges in: each carries its extracted ``text`` and a
        synthetic citation so an answer can attribute "your uploaded file <name>". Files with no
        extracted text (an image the model couldn't read) are skipped — nothing to index."""
        out: list[dict[str, Any]] = []
        with self._conn() as c:
            rows = c.execute(
                "SELECT id,name,kind,text,added_at FROM attachments ORDER BY added_at").fetchall()
        for r in rows:
            text = (r["text"] or "").strip()
            if not text:
                continue
            year = time.strftime("%Y", time.localtime(r["added_at"] or time.time()))
            out.append({
                "id": f"att:{r['id']}", "title": r["name"], "authors": ["your upload"],
                "year": year, "venue": "Uploaded file", "doi": "", "url": "",
                "abstract": "", "pdf_path": None, "text": text,
                "notes": "", "highlights": [],
            })
        return out


__all__ = ["AttachmentStore", "DocxReader", "HtmlReader"]
