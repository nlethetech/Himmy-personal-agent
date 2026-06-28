"""Himmy's own reference library — a real, native paper store (NOT a Zotero wrapper).

Himmy owns the papers: a SQLite catalogue (`library.db`) plus a PDF store
(`library_files/`). Papers are added directly from the app — by DOI/arXiv id (metadata
fetched from Crossref / arXiv) or by importing PDF files — and listed, searched, and removed
without the agent in the loop. The agent (Himmy) reads this same store when asked, but is no
longer the way you manage your library.

This is the foundation for the rest of the reference-manager features (reader + highlights,
citations, collections, tags) which build on top of these items.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from himmy_app.config import HimmyConfig, load_config

_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
_ARXIV_RE = re.compile(r"(?:arxiv:)?\s*(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)


def _new_id() -> str:
    # time-ordered-ish id without external deps
    return f"itm_{int(time.time() * 1000):x}_{abs(hash(time.time())) % 100000:05d}"


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _first_page_text(path: str | None, max_pages: int = 2, max_chars: int = 4000) -> str:
    """First page(s) of a PDF as text — enough to identify the paper."""
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(p))
        parts: list[str] = []
        for page in reader.pages[:max_pages]:
            parts.append(page.extract_text() or "")
            if sum(len(x) for x in parts) > max_chars:
                break
        return "\n".join(parts)[:max_chars]
    except Exception:  # noqa: BLE001
        return ""


def _title_match(a: str, b: str) -> bool:
    """Loose check that two titles refer to the same paper (word overlap)."""
    def words(s: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", (s or "").lower()))
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) >= max(3, int(0.5 * len(wa)))


class Library:
    """The Himmy paper catalogue + PDF store."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        cfg = config or load_config()
        self._db = cfg.data_dir / "library.db"
        self._files = cfg.data_dir / "library_files"
        self._files.mkdir(parents=True, exist_ok=True)
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY,
                    type TEXT,
                    title TEXT,
                    authors TEXT,   -- JSON list of display names
                    year TEXT,
                    venue TEXT,
                    doi TEXT,
                    url TEXT,
                    abstract TEXT,
                    tags TEXT,      -- JSON list
                    pdf_path TEXT,
                    added_at REAL
                )"""
            )
            # migration: per-paper notes
            cols = {r[1] for r in c.execute("PRAGMA table_info(items)")}
            if "notes" not in cols:
                c.execute("ALTER TABLE items ADD COLUMN notes TEXT DEFAULT ''")
            # highlights/annotations on a paper's PDF
            c.execute(
                """CREATE TABLE IF NOT EXISTS highlights (
                    id TEXT PRIMARY KEY, item_id TEXT, page INTEGER, color TEXT,
                    text TEXT, note TEXT, rects TEXT, created REAL
                )"""
            )
            # collections (folders) + membership
            c.execute("CREATE TABLE IF NOT EXISTS collections (id TEXT PRIMARY KEY, name TEXT, created REAL)")
            c.execute(
                """CREATE TABLE IF NOT EXISTS item_collections (
                    item_id TEXT, collection_id TEXT, PRIMARY KEY(item_id, collection_id)
                )"""
            )

    # ---- reads -------------------------------------------------------------------------
    def _row(self, r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["authors"] = json.loads(d.get("authors") or "[]")
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["has_pdf"] = bool(d.get("pdf_path"))
        d.pop("pdf_path", None)
        return d

    def list(self, query: str = "", collection_id: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as c:
            if collection_id:
                rows = c.execute(
                    """SELECT i.* FROM items i
                       JOIN item_collections ic ON ic.item_id = i.id
                       WHERE ic.collection_id = ? ORDER BY i.added_at DESC""",
                    (collection_id,),
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM items ORDER BY added_at DESC").fetchall()
        items = [self._row(r) for r in rows]
        q = query.strip().lower()
        if q:
            items = [
                it for it in items
                if q in (it["title"] or "").lower()
                or any(q in a.lower() for a in it["authors"])
                or q in (it["venue"] or "").lower()
                or any(q in t.lower() for t in it.get("tags", []))
            ]
        return items

    def count(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"])

    def get(self, item_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if not r:
                return None
            d = self._row(r)
            d["collections"] = [
                row["collection_id"]
                for row in c.execute(
                    "SELECT collection_id FROM item_collections WHERE item_id = ?", (item_id,)
                )
            ]
        return d

    def pdf_path(self, item_id: str) -> str | None:
        with self._conn() as c:
            r = c.execute("SELECT pdf_path FROM items WHERE id = ?", (item_id,)).fetchone()
        return r["pdf_path"] if r and r["pdf_path"] else None

    def rag_records(self) -> list[dict[str, Any]]:
        """Items with the fields the RAG indexer needs — incl. the on-disk PDF path AND the
        user's OWN annotations (their per-paper ``notes`` and their PDF ``highlights``), so the
        assistant can search and cite what the user marked as important, not just the raw text.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT id,title,authors,year,venue,doi,abstract,pdf_path,notes,added_at FROM items"
            ).fetchall()
            hl_rows = c.execute(
                "SELECT item_id, text, note FROM highlights ORDER BY item_id, page, created"
            ).fetchall()
        # Group each paper's highlights into a list of strings ("<highlighted text> — <my note>").
        highlights_by_item: dict[str, list[str]] = {}
        for h in hl_rows:
            text = (h["text"] or "").strip()
            note = (h["note"] or "").strip()
            entry = f"{text} — {note}" if (text and note) else (text or note)
            if entry:
                highlights_by_item.setdefault(h["item_id"], []).append(entry)
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["authors"] = json.loads(d.get("authors") or "[]")
            d["notes"] = (d.get("notes") or "").strip()
            d["highlights"] = highlights_by_item.get(d["id"], [])
            out.append(d)
        return out

    def set_tags(self, item_id: str, tags: list[str]) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("UPDATE items SET tags = ? WHERE id = ?", (json.dumps(tags), item_id))
        return {"ok": True, "tags": tags}

    # ---- writes ------------------------------------------------------------------------
    def _insert(self, item: dict[str, Any]) -> dict[str, Any]:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO items
                   (id,type,title,authors,year,venue,doi,url,abstract,tags,pdf_path,added_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["id"], item.get("type"), item.get("title"),
                    json.dumps(item.get("authors", [])), item.get("year"),
                    item.get("venue"), item.get("doi"), item.get("url"),
                    item.get("abstract"), json.dumps(item.get("tags", [])),
                    item.get("pdf_path"), item.get("added_at", time.time()),
                ),
            )
        return item

    def _existing_by_doi(self, doi: str) -> dict[str, Any] | None:
        if not doi:
            return None
        with self._conn() as c:
            r = c.execute("SELECT * FROM items WHERE doi = ?", (doi.lower(),)).fetchone()
        return self._row(r) if r else None

    async def add_identifier(self, raw: str) -> dict[str, Any]:
        """Add a paper from a DOI, a DOI URL, or an arXiv id. Returns the item."""
        text = (raw or "").strip()
        if not text:
            return {"ok": False, "message": "Paste a DOI or arXiv id."}
        arx_m = _ARXIV_RE.search(text)
        # arXiv ids AND arXiv DOIs (10.48550/arXiv.xxxx) both resolve via the arXiv API;
        # Crossref 404s most arXiv DOIs.
        if arx_m and ("arxiv" in text.lower() or not _DOI_RE.search(text)):
            return await self._add_arxiv(arx_m.group(1))
        doi_m = _DOI_RE.search(text)
        if doi_m:
            return await self._add_doi(doi_m.group(0))
        return {"ok": False, "message": f"'{text}' doesn't look like a DOI or arXiv id."}

    async def _add_doi(self, doi: str) -> dict[str, Any]:
        doi = doi.rstrip(".").lower()
        existing = self._existing_by_doi(doi)
        if existing:
            return {"ok": True, "item": existing, "duplicate": True}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    f"https://api.crossref.org/works/{doi}",
                    headers={"User-Agent": "Himmy/0.1 (mailto:hello@himmy.app)"},
                )
            if resp.status_code == 404:
                return {"ok": False, "message": f"No record found for DOI {doi}."}
            resp.raise_for_status()
            m = resp.json().get("message", {})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"Lookup failed: {exc}"}

        authors = [
            " ".join(p for p in [a.get("given", ""), a.get("family", "")] if p).strip()
            for a in m.get("author", [])
        ]
        year = ""
        dp = (m.get("issued") or {}).get("date-parts") or [[None]]
        if dp and dp[0] and dp[0][0]:
            year = str(dp[0][0])
        item = {
            "id": _new_id(),
            "type": m.get("type") or "journal-article",
            "title": (m.get("title") or ["(untitled)"])[0],
            "authors": [a for a in authors if a],
            "year": year,
            "venue": (m.get("container-title") or [""])[0],
            "doi": doi,
            "url": m.get("URL") or f"https://doi.org/{doi}",
            "abstract": _strip_tags(m.get("abstract") or ""),
            "tags": [],
            "pdf_path": None,
            "added_at": time.time(),
        }
        item["pdf_path"] = await self._oa_pdf_for_doi(doi, item["id"])
        return {"ok": True, "item": self._insert(item)}

    async def _add_arxiv(self, arxiv_id: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    "https://export.arxiv.org/api/query",
                    params={"id_list": arxiv_id, "max_results": 1},
                )
            resp.raise_for_status()
            xml = resp.text
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"arXiv lookup failed: {exc}"}

        # Scope parsing to the <entry> block so we don't grab the feed's own <title>.
        em = re.search(r"<entry>(.*?)</entry>", xml, re.DOTALL)
        if not em:
            return {"ok": False, "message": f"No arXiv record for {arxiv_id}."}
        entry = em.group(1)

        def tag(name: str) -> str:
            m = re.search(rf"<{name}>(.*?)</{name}>", entry, re.DOTALL)
            return re.sub(r"\s+", " ", _strip_tags(m.group(1))).strip() if m else ""

        title = tag("title")
        if not title:
            return {"ok": False, "message": f"No arXiv record for {arxiv_id}."}
        authors = [
            re.sub(r"\s+", " ", a).strip()
            for a in re.findall(r"<author>\s*<name>(.*?)</name>", entry, re.DOTALL)
        ]
        published = tag("published")
        item = {
            "id": _new_id(),
            "type": "preprint",
            "title": title,
            "authors": [a.strip() for a in authors],
            "year": published[:4] if published else "",
            "venue": "arXiv",
            "doi": "",
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "abstract": tag("summary"),
            "tags": ["arxiv"],
            "pdf_path": None,
            "added_at": time.time(),
        }
        item["pdf_path"] = await self._download_pdf(f"https://arxiv.org/pdf/{arxiv_id}", item["id"])
        return {"ok": True, "item": self._insert(item)}

    def add_files(self, paths: list[str]) -> dict[str, Any]:
        """Import local PDF files into the library (copies them into the store)."""
        added: list[dict[str, Any]] = []
        for p in paths:
            src = Path(p)
            if not src.exists() or src.suffix.lower() != ".pdf":
                continue
            item_id = _new_id()
            dest = self._files / f"{item_id}.pdf"
            try:
                shutil.copy2(src, dest)
            except Exception:  # noqa: BLE001
                continue
            title, authors, year = self._pdf_meta(dest, src.stem)
            item = {
                "id": item_id, "type": "document", "title": title,
                "authors": authors, "year": year, "venue": "", "doi": "",
                "url": "", "abstract": "", "tags": [], "pdf_path": str(dest),
                "added_at": time.time(),
            }
            added.append(self._insert(item))
        return {"ok": True, "added": len(added), "items": added}

    def _pdf_meta(self, path: Path, fallback_title: str) -> tuple[str, list[str], str]:
        try:
            from pypdf import PdfReader

            meta = PdfReader(str(path)).metadata or {}
            title = (meta.get("/Title") or "").strip() or fallback_title
            author = (meta.get("/Author") or "").strip()
            authors = [a.strip() for a in re.split(r"[;,]", author) if a.strip()] if author else []
            return title, authors, ""
        except Exception:  # noqa: BLE001
            return fallback_title, [], ""

    # ---- notes & metadata edit ----------------------------------------------------------
    _EDITABLE = {"title", "year", "venue", "doi", "url", "abstract", "type"}

    def set_note(self, item_id: str, note: str) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("UPDATE items SET notes = ? WHERE id = ?", (note, item_id))
        return {"ok": True}

    def update_item(self, item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        sets, vals = [], []
        for k, v in fields.items():
            if k == "authors" and isinstance(v, list):
                sets.append("authors = ?")
                vals.append(json.dumps([str(a).strip() for a in v if str(a).strip()]))
            elif k == "tags" and isinstance(v, list):
                sets.append("tags = ?")
                vals.append(json.dumps([str(t).strip() for t in v if str(t).strip()]))
            elif k in self._EDITABLE:
                sets.append(f"{k} = ?")
                vals.append(str(v))
        if not sets:
            return {"ok": False, "message": "Nothing to update."}
        vals.append(item_id)
        with self._conn() as c:
            c.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", vals)
        return {"ok": True, "item": self.get(item_id)}

    # ---- AI-assisted enrichment (Himmy identifies a bare PDF) ----------------------------
    async def _ai_extract(self, text: str) -> dict[str, Any]:
        """Ask the model to pull {title, authors, year} from first-page text."""
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            return {}
        model = os.environ.get("HIMMY_APP_MODEL", "google/gemini-2.5-flash")
        prompt = (
            "From the first page of an academic paper below, extract its bibliographic "
            'metadata. Reply with ONLY a JSON object: {"title": "...", "authors": '
            '["First Last", ...], "year": "YYYY"}. No prose, no code fences.\n\n'
            f"FIRST PAGE:\n{text[:4000]}"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "temperature": 0,
                          "messages": [{"role": "user", "content": prompt}]},
                )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            return {}
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return {}
        try:
            d = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return {}
        return {
            "title": str(d.get("title", "")).strip(),
            "authors": [str(a).strip() for a in (d.get("authors") or []) if str(a).strip()],
            "year": str(d.get("year", "")).strip(),
        }

    async def _crossref_search(self, title: str) -> dict[str, Any]:
        if not title:
            return {}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    "https://api.crossref.org/works",
                    params={"query.bibliographic": title, "rows": 1},
                    headers={"User-Agent": "Himmy/0.1 (mailto:hello@himmy.app)"},
                )
            resp.raise_for_status()
            items = resp.json().get("message", {}).get("items", [])
        except Exception:  # noqa: BLE001
            return {}
        if not items:
            return {}
        m = items[0]
        cr_title = (m.get("title") or [""])[0]
        if not _title_match(title, cr_title):
            return {}
        authors = [
            " ".join(p for p in [a.get("given", ""), a.get("family", "")] if p).strip()
            for a in m.get("author", [])
        ]
        year = ""
        dp = (m.get("issued") or {}).get("date-parts") or [[None]]
        if dp and dp[0] and dp[0][0]:
            year = str(dp[0][0])
        return {
            "title": cr_title, "authors": [a for a in authors if a], "year": year,
            "venue": (m.get("container-title") or [""])[0], "doi": (m.get("DOI") or "").lower(),
        }

    async def enrich(self, item_id: str) -> dict[str, Any]:
        """Identify a bare PDF: Himmy reads page 1 → Crossref fills authoritative metadata."""
        item = self.get(item_id)
        if not item:
            return {"ok": False, "message": "Not found."}
        text = _first_page_text(self.pdf_path(item_id))
        if not text.strip():
            text = " ".join(filter(None, [item.get("title"), ", ".join(item.get("authors") or [])]))
        if not text.strip():
            return {"ok": False, "message": "No PDF text or title to identify this from."}

        ai = await self._ai_extract(text)
        title = (ai.get("title") or item.get("title") or "").strip()
        cr = await self._crossref_search(title) if title else {}

        fields: dict[str, Any] = {}
        if cr.get("title") or ai.get("title"):
            fields["title"] = cr.get("title") or ai.get("title")
        if cr.get("authors") or ai.get("authors"):
            fields["authors"] = cr.get("authors") or ai.get("authors")
        if cr.get("year") or ai.get("year"):
            fields["year"] = cr.get("year") or ai.get("year")
        if cr.get("venue"):
            fields["venue"] = cr["venue"]
        if cr.get("doi"):
            fields["doi"] = cr["doi"]
        if not fields:
            return {"ok": False, "message": "Couldn't identify this paper from its first page."}
        self.update_item(item_id, fields)
        return {"ok": True, "item": self.get(item_id), "source": "crossref" if cr else "himmy"}

    # ---- full-text PDF retrieval --------------------------------------------------------
    async def _download_pdf(self, url: str, item_id: str) -> str | None:
        if not url:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=60, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Himmy)"},
            ) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return None
            body = resp.content
            ctype = resp.headers.get("content-type", "").lower()
            if "pdf" not in ctype and body[:4] != b"%PDF":
                return None
            dest = self._files / f"{item_id}.pdf"
            dest.write_bytes(body)
            return str(dest)
        except Exception:  # noqa: BLE001
            return None

    async def _oa_pdf_for_doi(self, doi: str, item_id: str) -> str | None:
        """Find an open-access PDF for a DOI via Unpaywall (free)."""
        if not doi:
            return None
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                r = await client.get(
                    f"https://api.unpaywall.org/v2/{doi}", params={"email": "hello@himmy.app"}
                )
            if r.status_code != 200:
                return None
            loc = r.json().get("best_oa_location") or {}
            purl = loc.get("url_for_pdf")
        except Exception:  # noqa: BLE001
            return None
        return await self._download_pdf(purl, item_id) if purl else None

    def _set_pdf_path(self, item_id: str, path: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE items SET pdf_path = ? WHERE id = ?", (path, item_id))

    async def fetch_pdf(self, item_id: str) -> dict[str, Any]:
        """Backfill the full PDF for an item that was added metadata-only."""
        item = self.get(item_id)
        if not item:
            return {"ok": False, "message": "Not found."}
        if item.get("has_pdf"):
            return {"ok": True, "item": item, "already": True}
        url = (item.get("url") or "")
        doi = (item.get("doi") or "").lower()
        venue = (item.get("venue") or "").lower()
        path: str | None = None
        am = re.search(r"(\d{4}\.\d{4,5})", url) or re.search(r"(\d{4}\.\d{4,5})", doi)
        if am and ("arxiv" in url.lower() or "arxiv" in doi or "arxiv" in venue):
            path = await self._download_pdf(f"https://arxiv.org/pdf/{am.group(1)}", item_id)
        if not path and doi:
            path = await self._oa_pdf_for_doi(doi, item_id)
        if not path and url.lower().endswith(".pdf"):
            path = await self._download_pdf(url, item_id)
        if not path:
            return {"ok": False, "message": "Couldn't find a free full-text PDF for this paper."}
        self._set_pdf_path(item_id, path)
        return {"ok": True, "item": self.get(item_id)}

    # ---- backup / restore (whole-workspace, for cross-device sync via a cloud folder) ----
    #: Regenerable files we deliberately leave OUT of a backup — they rebuild on demand and
    #: would only bloat the zip (the papers text cache; the persisted RAG vector index, which
    #: re-embeds from the papers themselves; the 15-min news cache).
    _BACKUP_SKIP = {"papers_cache.db", "papers_index.db", "news_cache.json"}

    @staticmethod
    def _snapshot_sqlite(src: Path, dst: Path) -> None:
        """Write a CONSISTENT copy of a (possibly WAL-mode, possibly live) SQLite db.

        Uses SQLite's online-backup API rather than a raw file copy, so we never capture a
        half-written database or miss rows still sitting in a ``-wal`` sidecar — the backup
        API is explicitly safe against concurrent writers (the live backend holds these open).
        """
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            dst_conn = sqlite3.connect(str(dst))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

    def backup(self, dest_dir: str) -> str:
        """Zip the WHOLE workspace (catalogue + PDFs + chats + memory + tasks + routines +
        inbox + approvals + settings) into ``dest_dir``; return the file path.

        Every SQLite db is captured via a consistent snapshot; regenerable caches are skipped;
        a ``manifest.json`` records what's inside so :meth:`restore` can validate before it
        touches anything.
        """
        data_dir = self._db.parent
        dest = Path(dest_dir).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"himmy-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        included: list[str] = []
        with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            tmpp = Path(tmp)
            for f in sorted(data_dir.iterdir()):
                name = f.name
                if name in self._BACKUP_SKIP:
                    continue
                # WAL/SHM/journal sidecars are folded into the .db snapshot — never zip them raw.
                if name.endswith(("-wal", "-shm", "-journal")):
                    continue
                if f.is_dir():
                    # The user's binary stores: saved PDFs + uploaded attachment files. Both are
                    # real user data, so both ride the backup (other dirs — e.g. a prior
                    # .pre-restore snapshot — are skipped).
                    if name in ("library_files", "attachment_files"):
                        for blob in sorted(f.glob("*")):
                            if blob.is_file():
                                z.write(blob, f"{name}/{blob.name}")
                                included.append(f"{name}/{blob.name}")
                    continue
                if name.endswith(".db"):
                    snap = tmpp / name
                    try:
                        self._snapshot_sqlite(f, snap)
                        z.write(snap, name)
                    except Exception:  # noqa: BLE001 - a locked/odd db falls back to a raw copy
                        z.write(f, name)
                    included.append(name)
                else:  # plain files (usage.json, news.json, …)
                    z.write(f, name)
                    included.append(name)
            manifest = {
                "version": 2,
                "app": "himmy",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "counts": {"papers": self.count()},
                "files": included,
            }
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
        return str(path)

    def restore(self, zip_path: str) -> dict[str, Any]:
        """Replace the workspace from a backup zip — SAFELY.

        Order of operations matters: we (1) verify the zip's integrity and that it's a Himmy
        backup, (2) snapshot the CURRENT workspace to a timestamped ``.pre-restore`` folder
        BEFORE overwriting anything, then (3) write the backed-up files back, dropping any stale
        ``-wal``/``-shm`` sidecars so each restored db is authoritative. A corrupt or wrong zip
        therefore changes nothing, and a regretted restore is recoverable from the snapshot.
        """
        p = Path(zip_path).expanduser()
        if not p.exists():
            return {"ok": False, "message": "Backup file not found."}
        if not zipfile.is_zipfile(p):
            return {"ok": False, "message": "That file isn't a valid backup .zip — nothing was changed."}
        try:
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
                # (1) validate BEFORE touching anything.
                bad = z.testzip()
                if bad is not None:
                    return {"ok": False, "message": f"Backup is corrupt (failed at {bad}) — nothing was changed."}
                is_v2 = "manifest.json" in names
                if not is_v2 and "library.db" not in names:
                    return {"ok": False, "message": "That doesn't look like a Himmy backup — nothing was changed."}

                data_dir = self._db.parent
                # (2) safety snapshot of the live workspace BEFORE we overwrite.
                snap_dir = data_dir.parent / f"{data_dir.name}.pre-restore-{time.strftime('%Y%m%d-%H%M%S')}"
                shutil.copytree(data_dir, snap_dir, dirs_exist_ok=True)

                # (3) restore top-level files (dbs + json), then the binary stores.
                #: zip-prefix -> the on-disk directory it restores into (PDFs + uploaded files).
                blob_dirs = {"library_files/": self._files,
                             "attachment_files/": data_dir / "attachment_files"}
                seen_blob_prefixes: set[str] = set()
                for n in names:
                    if n == "manifest.json" or n.endswith("/"):
                        continue
                    prefix = next((p for p in blob_dirs if n.startswith(p)), None)
                    if prefix is not None:
                        seen_blob_prefixes.add(prefix)
                        continue
                    target = data_dir / Path(n).name
                    target.write_bytes(z.read(n))
                    if n.endswith(".db"):
                        for sx in ("-wal", "-shm"):
                            sidecar = data_dir / (Path(n).name + sx)
                            if sidecar.exists():
                                try:
                                    sidecar.unlink()
                                except Exception:  # noqa: BLE001
                                    pass
                # Each backed-up binary store fully REPLACES its live counterpart.
                for prefix in seen_blob_prefixes:
                    dest_dir = blob_dirs[prefix]
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    for f in dest_dir.glob("*"):
                        try:
                            f.unlink()
                        except Exception:  # noqa: BLE001
                            pass
                    for n in names:
                        if n.startswith(prefix) and not n.endswith("/"):
                            (dest_dir / Path(n).name).write_bytes(z.read(n))
            return {
                "ok": True,
                "restored": self.count(),
                "snapshot": snap_dir.name,
                "message": (
                    f"Restored your whole workspace. A safety copy of your previous data was saved "
                    f"to “{snap_dir.name}”. Quit and reopen Himmy to load everything."
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}

    async def save(self, p: dict[str, Any]) -> dict[str, Any]:
        """Save a paper from the browser extension (scraped page metadata)."""
        ident = (p.get("doi") or "").strip() or (p.get("arxiv") or "").strip()
        if ident:
            r = await self.add_identifier(ident)
            if r.get("ok") and r.get("item"):
                item = r["item"]
                if not item.get("has_pdf") and p.get("pdf_url"):
                    path = await self._download_pdf(p["pdf_url"], item["id"])
                    if path:
                        self._set_pdf_path(item["id"], path)
                        r["item"] = self.get(item["id"])
                return r
            # identifier lookup failed — fall through to metadata save
        title = (p.get("title") or "").strip()
        if not title:
            return {"ok": False, "message": "No DOI, arXiv id, or title found on this page."}
        item_id = _new_id()
        item = {
            "id": item_id, "type": "webpage", "title": title,
            "authors": [a for a in (p.get("authors") or []) if a],
            "year": (p.get("year") or "")[:4], "venue": p.get("venue") or "",
            "doi": (p.get("doi") or "").lower(), "url": p.get("url") or "",
            "abstract": "", "tags": [], "pdf_path": None, "added_at": time.time(),
        }
        if p.get("pdf_url"):
            item["pdf_path"] = await self._download_pdf(p["pdf_url"], item_id)
        return {"ok": True, "item": self._insert(item)}

    # ---- highlights ---------------------------------------------------------------------
    def _highlight(self, hid: str) -> dict[str, Any]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM highlights WHERE id = ?", (hid,)).fetchone()
        if not r:
            return {}
        d = dict(r)
        d["rects"] = json.loads(d.get("rects") or "[]")
        return d

    def add_highlight(self, item_id: str, page: int, color: str, text: str,
                      note: str, rects: list[Any]) -> dict[str, Any]:
        hid = "hl_" + _new_id()[4:]
        with self._conn() as c:
            c.execute(
                "INSERT INTO highlights VALUES (?,?,?,?,?,?,?,?)",
                (hid, item_id, int(page), color or "yellow", text or "", note or "",
                 json.dumps(rects or []), time.time()),
            )
        return self._highlight(hid)

    def list_highlights(self, item_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM highlights WHERE item_id = ? ORDER BY page, created", (item_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["rects"] = json.loads(d.get("rects") or "[]")
            out.append(d)
        return out

    def export_highlights_markdown(self, item_id: str) -> dict[str, Any]:
        """Write this paper's highlights + notes to a clean Markdown file in ~/Downloads.

        Returns {ok, path} (or {ok: False, message} if there's nothing to export).
        Mirrors backup()'s shape: builds the file, returns its path.
        """
        item = self.get(item_id)
        if not item:
            return {"ok": False, "message": "Paper not found."}
        highlights = self.list_highlights(item_id)
        item_note = (item.get("notes") or "").strip()
        if not highlights and not item_note:
            return {"ok": False, "message": "This paper has no highlights or notes to export yet."}

        title = (item.get("title") or "Untitled").strip()
        authors = item.get("authors") or []
        year = (item.get("year") or "").strip()
        venue = (item.get("venue") or "").strip()

        lines: list[str] = [f"# {title}", ""]
        meta_bits: list[str] = []
        if authors:
            meta_bits.append(", ".join(authors))
        if year:
            meta_bits.append(year)
        if venue:
            meta_bits.append(venue)
        if meta_bits:
            lines.append(" · ".join(meta_bits))
            lines.append("")
        lines.append(f"*Exported from Himmy on {time.strftime('%Y-%m-%d %H:%M')}*")
        lines.append("")

        if item_note:
            lines.append("## Notes")
            lines.append("")
            lines.append(item_note)
            lines.append("")

        if highlights:
            lines.append("## Highlights")
            lines.append("")
            last_page: int | None = None
            for h in highlights:
                page = int(h.get("page") or 0)
                if page != last_page:
                    lines.append(f"### Page {page + 1}")
                    lines.append("")
                    last_page = page
                text = (h.get("text") or "").strip()
                note = (h.get("note") or "").strip()
                if text:
                    quoted = "\n".join(f"> {ln}" for ln in text.splitlines())
                    lines.append(quoted)
                    lines.append("")
                if note:
                    lines.append(f"**Note:** {note}")
                    lines.append("")

        safe = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "-")[:60] or "paper"
        dest = Path("~/Downloads").expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{safe}-highlights-{time.strftime('%Y%m%d-%H%M%S')}.md"
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return {"ok": True, "path": str(path)}

    def update_highlight(self, hid: str, note: str | None = None,
                         color: str | None = None) -> dict[str, Any]:
        with self._conn() as c:
            if note is not None:
                c.execute("UPDATE highlights SET note = ? WHERE id = ?", (note, hid))
            if color is not None:
                c.execute("UPDATE highlights SET color = ? WHERE id = ?", (color, hid))
        return self._highlight(hid)

    def delete_highlight(self, hid: str) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("DELETE FROM highlights WHERE id = ?", (hid,))
        return {"ok": True}

    # ---- collections --------------------------------------------------------------------
    def create_collection(self, name: str) -> dict[str, Any]:
        cid = "col_" + _new_id()[4:]
        name = (name or "").strip() or "Untitled"
        with self._conn() as c:
            c.execute("INSERT INTO collections VALUES (?,?,?)", (cid, name, time.time()))
        return {"id": cid, "name": name, "count": 0}

    def list_collections(self) -> list[dict[str, Any]]:
        out = []
        with self._conn() as c:
            for r in c.execute("SELECT * FROM collections ORDER BY created").fetchall():
                n = c.execute(
                    "SELECT COUNT(*) AS n FROM item_collections WHERE collection_id = ?", (r["id"],)
                ).fetchone()["n"]
                out.append({"id": r["id"], "name": r["name"], "count": int(n)})
        return out

    def rename_collection(self, cid: str, name: str) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("UPDATE collections SET name = ? WHERE id = ?", ((name or "").strip(), cid))
        return {"ok": True}

    def delete_collection(self, cid: str) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("DELETE FROM collections WHERE id = ?", (cid,))
            c.execute("DELETE FROM item_collections WHERE collection_id = ?", (cid,))
        return {"ok": True}

    def add_to_collection(self, item_id: str, cid: str) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO item_collections VALUES (?,?)", (item_id, cid))
        return {"ok": True}

    def remove_from_collection(self, item_id: str, cid: str) -> dict[str, Any]:
        with self._conn() as c:
            c.execute(
                "DELETE FROM item_collections WHERE item_id = ? AND collection_id = ?", (item_id, cid)
            )
        return {"ok": True}

    # ---- tags ---------------------------------------------------------------------------
    def all_tags(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for it in self.list():
            for t in it.get("tags", []):
                counts[t] = counts.get(t, 0) + 1
        return [{"tag": k, "count": v} for k, v in sorted(counts.items())]

    # ---- delete -------------------------------------------------------------------------
    def delete(self, item_id: str) -> dict[str, Any]:
        with self._conn() as c:
            row = c.execute("SELECT pdf_path FROM items WHERE id = ?", (item_id,)).fetchone()
            c.execute("DELETE FROM items WHERE id = ?", (item_id,))
            c.execute("DELETE FROM highlights WHERE item_id = ?", (item_id,))
            c.execute("DELETE FROM item_collections WHERE item_id = ?", (item_id,))
        if row and row["pdf_path"]:
            try:
                Path(row["pdf_path"]).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True}

    # ---- de-duplication -----------------------------------------------------------------
    def dedupe(self) -> dict[str, Any]:
        """Collapse exact-duplicate items (same title + first author), keeping the richest copy.

        SAFE: never deletes an item that has highlights or notes — annotations are protected, so
        the worst case is a duplicate stays, never that your work is lost.
        """
        def norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for it in self.list():
            title = norm(it.get("title"))
            if not title:
                continue
            authors = it.get("authors") or []
            groups.setdefault((title, norm(authors[0]) if authors else ""), []).append(it)

        def _annotated(item_id: str, notes: str | None) -> bool:
            return bool((notes or "").strip()) or bool(self.list_highlights(item_id))

        removed: list[str] = []
        for group in groups.values():
            if len(group) < 2:
                continue
            # keep the richest copy: has a PDF, then has annotations, then has a DOI
            group.sort(key=lambda it: (
                it.get("has_pdf", False), _annotated(it["id"], it.get("notes")), bool(it.get("doi")),
            ), reverse=True)
            for dup in group[1:]:
                if _annotated(dup["id"], dup.get("notes")):
                    continue  # protect annotated copies — leave them in place
                self.delete(dup["id"])
                removed.append(dup["id"])
        return {"ok": True, "removed": len(removed), "ids": removed}


__all__ = ["Library"]
