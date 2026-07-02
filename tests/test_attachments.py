"""Attachments → RAG, the personality store, and the media-permission gate.

These prove the framework-backed file pipeline end to end WITHOUT any network: documents are
parsed via the (extended) himmy reader factory, stored, exposed to the RAG, and pruned on delete;
image/audio extraction is gated by the ``read_media`` flag (so an offline test never calls a model);
the personality store round-trips and shapes the injected directive; and the new "Files & media"
permission surface gates the media tools.
"""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from himmy_app.attachments import AttachmentStore, DocxReader, HtmlReader
from himmy_app.config import load_config


@pytest.fixture()
def store(tmp_path, monkeypatch) -> AttachmentStore:
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data"))
    return AttachmentStore(load_config())


def _docx_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    body = "".join(f"<w:p><w:r><w:t>{ln}</w:t></w:r></w:p>" for ln in text.split("\n"))
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr("word/document.xml",
                   f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


# ---- framework reader extensions --------------------------------------------------------
def test_docx_reader(tmp_path):
    p = tmp_path / "x.docx"
    p.write_bytes(_docx_bytes("Hello world\nSecond line"))
    out = DocxReader().read(str(p))
    assert "Hello world" in out and "Second line" in out


def test_html_reader(tmp_path):
    p = tmp_path / "x.html"
    p.write_text("<html><head><style>.a{}</style></head><body><p>Keep this</p>"
                 "<script>drop()</script></body></html>", encoding="utf-8")
    out = HtmlReader().read(str(p))
    assert "Keep this" in out
    assert "drop()" not in out and ".a{}" not in out


# ---- parsing must not block the event loop ----------------------------------------------
def test_ingest_offloads_parsing_from_event_loop(store, monkeypatch):
    """A heavy synchronous parse (big PDF/docx) must run on a WORKER THREAD, not the event loop —
    otherwise one upload freezes the whole backend. Prove it: while ingest() runs a deliberately
    slow _extract, a concurrent heartbeat coroutine must keep ticking, and the parse must land on a
    thread other than the loop's."""
    import threading
    import time

    main_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def slow_extract(kind, ext, path, data, mime):  # noqa: ANN001 - test stub
        seen["thread"] = threading.get_ident()
        time.sleep(0.3)  # simulate a heavy parse
        return "parsed body text"

    monkeypatch.setattr(store, "_extract", slow_extract)

    async def run():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            for _ in range(15):
                await asyncio.sleep(0.02)
                ticks += 1

        hb = asyncio.create_task(heartbeat())
        res = await store.ingest("big.txt", b"hello world", "text/plain")
        await hb
        return res, ticks

    res, ticks = asyncio.run(run())
    assert res["chars"] > 0 and "parsed" in res["text"]
    assert seen["thread"] != main_thread, "parse must run OFF the event-loop thread"
    assert ticks >= 5, "event loop stalled during parse — parsing is still blocking"


# ---- ingest / store / rag ---------------------------------------------------------------
def test_ingest_text_and_rag_record(store):
    att = asyncio.run(store.ingest("lease.txt", b"Rent Rs 35000. Deposit 2 months.", "text/plain"))
    assert att["kind"] == "doc" and att["chars"] > 0 and att["empty"] is False
    assert "Rent" in att["text"]
    # blob round-trips, latest resolves, count is 1
    assert store.blob_bytes(att["id"]) is not None
    assert store.latest("doc")["id"] == att["id"]
    assert store.count() == 1
    # rag_records carry the merged-source shape PapersIndex expects
    recs = store.rag_records()
    assert len(recs) == 1
    r = recs[0]
    assert r["id"] == f"att:{att['id']}" and r["venue"] == "Uploaded file"
    assert "Rent" in r["text"] and r["authors"] == ["your upload"]


def test_ingest_docx(store):
    att = asyncio.run(store.ingest("notes.docx", _docx_bytes("Project Aurora\nBudget NPR 500000"),
                                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    assert att["kind"] == "doc"
    assert "Aurora" in att["text"] and "500000" in att["text"]


def test_delete_prunes(store):
    a = asyncio.run(store.ingest("a.txt", b"alpha", "text/plain"))
    b = asyncio.run(store.ingest("b.txt", b"beta", "text/plain"))
    assert store.count() == 2
    store.delete(a["id"])
    assert store.count() == 1
    assert store.get(a["id"]) is None and store.get(b["id"]) is not None
    assert [r["id"] for r in store.rag_records()] == [f"att:{b['id']}"]


def test_image_kind_and_read_media_gate(store):
    # A 1x1 PNG. With read_media=False the image is STORED but NOT read (no model call) → empty.
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
           b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
    att = asyncio.run(store.ingest("shot.png", png, "image/png", read_media=False))
    assert att["kind"] == "image" and att["empty"] is True and att["chars"] == 0
    # an image with no extracted text is not indexed (nothing to retrieve)
    assert store.rag_records() == []


def test_voice_mime_detected_as_audio(store):
    att = asyncio.run(store.ingest("voice-note.ogg", b"\x00\x00", "audio/ogg", read_media=False))
    assert att["kind"] == "audio" and att["empty"] is True


# ---- personality store ------------------------------------------------------------------
def test_assistant_store_and_directive(tmp_path, monkeypatch):
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data"))
    from himmy_app import user_profile as up

    assert up.load_assistant()["style"] == "chief_of_staff"  # default
    up.save_assistant("friendly", "")
    assert up.load_assistant()["style"] == "friendly"
    assert "friendly" in up.persona_directive().lower()
    # custom note flows into the directive verbatim
    up.save_assistant("custom", "talk like a witty Nepali friend")
    assert "witty Nepali friend" in up.persona_directive()
    # an unknown style falls back to the default; an empty note on default still yields a directive
    up.save_assistant("nonsense", "")
    assert up.load_assistant()["style"] == "chief_of_staff"


# ---- permission surface for media tools -------------------------------------------------
def test_files_permission_gates_media_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data"))
    from himmy_app import permissions as perms

    tools = ["read_image", "transcribe_audio", "ask_papers"]
    assert "read_image" in perms.gate_tools(tools)             # default on
    perms.save({"files": "off"})
    gated = perms.gate_tools(tools)
    assert "read_image" not in gated and "transcribe_audio" not in gated
    assert "ask_papers" in gated                                # a different surface, unaffected


# ---- backup covers the attachment blobs -------------------------------------------------
def test_backup_restore_includes_attachment_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data"))
    from himmy_app.library import Library

    store = AttachmentStore(load_config())
    att = asyncio.run(store.ingest("keep.txt", b"remember me", "text/plain"))
    data_dir = load_config().data_dir
    assert (data_dir / "attachment_files" / f"{att['id']}.txt").exists()

    lib = Library(load_config())
    zip_path = lib.backup(str(tmp_path / "out"))
    names = zipfile.ZipFile(zip_path).namelist()
    assert "attachments.db" in names
    assert any(n.startswith("attachment_files/") for n in names)
