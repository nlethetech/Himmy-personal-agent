"""Papers RAG — "ask Himmy about my papers", grounded and cited, over the DAYBOOK LIBRARY.

Pipeline:
  Himmy library (library.db + library_files/)  ->  per-paper text (title + authors + venue +
  abstract + full PDF text)  ->  himmy KnowledgeBase (hybrid BM25 + dense)  ->  ask_papers(query)
  returns ranked passages, each with a citation.

The KB is rebuilt in-memory and cached; it auto-refreshes whenever the library's set of items
changes (a paper added or removed), so the agent always reads the user's current collection.
Extracted PDF text is cached on disk (``papers_cache.db``) so extraction happens once per file.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any

from himmy.services.knowledge.models import DocumentInput
from himmy.services.knowledge.retrieval.config import RetrievalConfig
from himmy.services.knowledge.retrieval.reranker import (
    RerankerProtocol,
    build_reranker,
    fastembed_rerank_available,
)
from himmy.services.knowledge.service import KnowledgeBase
from himmy.services.knowledge.sqlite_backend import SqliteKnowledgeBackend
from himmy.services.tools.registry import ToolRegistry, register_local_tool
from himmy.toolkit import ToolkitConfig

from himmy_app.attachments import AttachmentStore
from himmy_app.config import HimmyConfig, load_config
from himmy_app.library import Library
from himmy_app.news import SavedNews

_MAX_CHARS = 60_000
KB_WORKSPACE_ID = "scholar-desk"
KB_CLIENT_ID = "scholar"
KB_NAME = "papers"

_log = logging.getLogger("himmy_app.papers_rag")

#: Cached probe verdict across rebuilds: None = not yet probed; a reranker = working
#: (model loaded once); False = confirmed unloadable here, so we don't re-probe.
_RERANKER: RerankerProtocol | bool | None = None


async def _maybe_reranker() -> RerankerProtocol | None:
    """Return a working cross-encoder reranker, or None to degrade to plain hybrid.

    The cross-encoder is precise but optional: if fastembed isn't installed or the
    ONNX model can't be fetched/loaded (e.g. offline first run), we quietly fall back
    to BM25+dense hybrid so the library is always searchable. The model loads lazily
    on first use, so we probe it once with a tiny rerank and cache the verdict.
    """
    global _RERANKER
    if _RERANKER is False:
        return None
    if _RERANKER is not None:  # already probed and working
        return _RERANKER  # type: ignore[return-value]
    if not fastembed_rerank_available():
        _log.info("RAG reranker unavailable (fastembed not installed); using plain hybrid.")
        _RERANKER = False
        return None
    try:
        reranker = build_reranker("fastembed")
        # Force the lazy model load now via a tiny probe; failure -> graceful fallback.
        await reranker.rerank("probe", [("probe", "probe")])
        _RERANKER = reranker
        _log.info("RAG cross-encoder reranker enabled.")
        return reranker
    except Exception as exc:  # noqa: BLE001 - any load/runtime failure must degrade, not break
        _log.warning("RAG reranker could not load (%s); using plain hybrid.", exc)
        _RERANKER = False
        return None

#: Process-wide cache: one warm index per cache-db path.
_INDEX_CACHE: dict[str, "PapersIndex"] = {}


def _citation(r: dict[str, Any]) -> str:
    authors = r.get("authors") or []
    if authors:
        who = authors[0] + (" et al." if len(authors) > 1 else "")
    else:
        who = r.get("venue") or "Unknown"
    year = r.get("year") or "n.d."
    venue = r.get("venue")
    tail = f", {venue}" if venue and venue != who else ""
    return f'{who} ({year}){tail} — "{r.get("title") or "(untitled)"}"'


def _pdf_text(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(p))
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
            if total > _MAX_CHARS:
                break
        return "\n".join(parts)[:_MAX_CHARS]
    except Exception:  # noqa: BLE001 - a malformed PDF must not break indexing
        return ""


class PapersIndex:
    """Builds and serves the searchable index over the Himmy library."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        cfg = config or load_config()
        self._cfg = cfg
        self._lib = Library(cfg)
        self._news = SavedNews(cfg)
        self._att = AttachmentStore(cfg)
        self._cache_path = cfg.papers_cache_path
        # Disk-backed vector store so the index SURVIVES restarts: on the next launch we
        # resolve the existing KB and only NEW/changed papers re-embed (content-hash dedup),
        # instead of re-embedding the whole library on the first ask (~2-3 min cold freeze).
        # Lives alongside the other .scholar-desk stores; regenerable, so backup skips it.
        self._index_path = cfg.data_dir / "papers_index.db"
        self._backend = SqliteKnowledgeBackend(str(self._index_path))
        self._kb: KnowledgeBase | None = None
        self._kb_id: str | None = None
        self._indexed_ids: set[str] = set()
        # Per-record signatures (id + notes + highlights) so an annotation edit triggers a
        # re-sync even when the set of paper ids is unchanged.
        self._indexed_keys: set[tuple[Any, ...]] = set()
        self._lock = asyncio.Lock()
        self._ensure_cache()

    # ---- on-disk PDF-text cache ---------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        # timeout so a concurrent writer (e.g. the background prewarm thread) waits instead of
        # erroring with "database is locked".
        c = sqlite3.connect(str(self._cache_path), timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _ensure_cache(self) -> None:
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS rag_text (item_id TEXT PRIMARY KEY, text TEXT)")

    def _cached_text(self, item_id: str) -> str | None:
        with self._conn() as c:
            r = c.execute("SELECT text FROM rag_text WHERE item_id = ?", (item_id,)).fetchone()
        return r["text"] if r else None

    def _store_text(self, item_id: str, text: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO rag_text VALUES (?, ?)", (item_id, text))

    def _record_text(self, r: dict[str, Any]) -> str:
        header = "\n".join(
            x for x in [
                r.get("title"), ", ".join(r.get("authors") or []),
                r.get("venue"), r.get("abstract"),
            ] if x
        )
        body = ""
        if r.get("pdf_path"):
            cached = self._cached_text(r["id"])
            if cached is None:
                cached = _pdf_text(r["pdf_path"])
                self._store_text(r["id"], cached)
            body = cached
        elif r.get("text"):  # saved-news records carry their full text already
            body = r["text"]
        text = (header + "\n\n" + body).strip() if body else header
        return text[:_MAX_CHARS]

    # ---- merged record set: library papers + saved news + uploaded attachments ----------
    def _records(self) -> list[dict[str, Any]]:
        return self._lib.rag_records() + self._news.rag_records() + self._att.rag_records()

    # ---- KB lifecycle (disk-backed; warm across restarts) -------------------------------
    async def _kb_handle(self) -> tuple[KnowledgeBase, str]:
        """Resolve-or-create the persisted KB once per process; reuse it thereafter.

        On a restart this RESOLVES the index already on disk (so search works immediately and
        re-ingest skips re-embedding). If the embedder's vector dimension no longer matches the
        stored index (the embedder was swapped), the stale index is dropped and rebuilt.
        """
        if self._kb is not None and self._kb_id is not None:
            return self._kb, self._kb_id
        embedder, dim = ToolkitConfig.from_env().build_embedder_and_dim()
        reranker = await _maybe_reranker()
        retrieval = (
            RetrievalConfig(mode="hybrid", rerank=True, reranker=reranker)
            if reranker is not None else RetrievalConfig(mode="hybrid")
        )
        kb = KnowledgeBase(storage=None, embedder=embedder, backend=self._backend, retrieval=retrieval)
        rec = await kb.resolve_kb(workspace_id=KB_WORKSPACE_ID, client_id=KB_CLIENT_ID, name=KB_NAME)
        if rec is not None and rec.vector_dim != dim:
            await kb.delete_kb(rec.kb_id)  # embedder changed → stored vectors are unusable
            rec = None
        if rec is None:
            rec = await kb.create_kb(
                workspace_id=KB_WORKSPACE_ID, client_id=KB_CLIENT_ID, name=KB_NAME, vector_dim=dim,
            )
        self._kb, self._kb_id = kb, rec.kb_id
        return kb, rec.kb_id

    def _doc_inputs(self, r: dict[str, Any]) -> list[DocumentInput]:
        """The document(s) one record contributes to the index.

        Always the paper/article body; PLUS — when present — the user's OWN per-paper note
        and their PDF highlights as SEPARATE, clearly-labelled documents. Keeping the
        annotations as their own docs means they're never truncated by the body-size cap, are
        independently retrievable (a query about X surfaces the highlight about X even if the
        paper sprawls), and are tagged so the assistant can attribute them to the user. Each
        has a stable ``source_uri`` (``<id>`` / ``note:<id>`` / ``hl:<id>``) so a re-ingest
        dedups (unchanged → no re-embed), an edit REPLACES, and a removal can be pruned by uri.
        """
        pid = str(r["id"])
        title = r.get("title") or "this source"
        base_meta = {
            "id": r["id"], "title": r.get("title"), "authors": r.get("authors", []),
            "year": r.get("year"), "venue": r.get("venue"), "doi": r.get("doi"),
            "url": r.get("url"), "citation": _citation(r),
        }
        docs: list[DocumentInput] = []
        body = self._record_text(r)
        if body.strip():
            docs.append(DocumentInput(text=body, source_uri=pid, metadata={**base_meta, "kind": "paper"}))
        note = (r.get("notes") or "").strip()
        if note:
            txt = f'[The user\'s OWN note on "{title}"]\n{note}'
            docs.append(DocumentInput(
                text=txt[:_MAX_CHARS], source_uri=f"note:{pid}", metadata={**base_meta, "kind": "note"}))
        highlights = [h for h in (r.get("highlights") or []) if h.strip()]
        if highlights:
            joined = "\n".join(f"• {h}" for h in highlights)
            txt = f'[The user\'s OWN highlights from "{title}" — passages they marked as important]\n{joined}'
            docs.append(DocumentInput(
                text=txt[:_MAX_CHARS], source_uri=f"hl:{pid}", metadata={**base_meta, "kind": "highlight"}))
        return docs

    @staticmethod
    def _record_keys(records: list[dict[str, Any]]) -> set[tuple[Any, ...]]:
        """A per-record signature set used to decide if a re-sync is needed. Unlike a plain
        id-set it also reflects the user's notes + highlights, so ADDING a highlight or note to
        an existing paper re-indexes it (the id-set alone wouldn't have changed)."""
        return {
            (r["id"], (r.get("notes") or "").strip(), tuple(r.get("highlights") or []))
            for r in records
        }

    async def _sync(self, records: list[dict[str, Any]]) -> None:
        """Bring the persisted index in line with ``records``: ingest new/changed docs (dedup
        keeps it warm), then prune documents — paper, note, OR highlight — no longer present
        (a removed paper, a cleared note, deleted highlights)."""
        kb, kb_id = await self._kb_handle()
        inputs: list[DocumentInput] = []
        keep: set[str] = set()
        for r in records:
            for d in self._doc_inputs(r):
                inputs.append(d)
                if d.source_uri is not None:
                    keep.add(d.source_uri)
        if inputs:
            await kb.ingest_documents(kb_id, inputs)
        try:
            for src, content_hash in await self._backend.list_document_identities(kb_id):
                if src is not None and src not in keep:
                    doc = await self._backend.get_document(kb_id, src, content_hash)
                    if doc is not None:
                        await kb.delete_document(kb_id, doc.document_id)
        except Exception:  # noqa: BLE001 - pruning is best-effort; never break a search over it
            pass
        self._indexed_ids = {r["id"] for r in records}
        self._indexed_keys = self._record_keys(records)

    async def _ensure(self) -> tuple[KnowledgeBase | None, str | None]:
        records = self._records()
        keys = self._record_keys(records)
        if self._kb is not None and keys == self._indexed_keys:
            return self._kb, self._kb_id
        async with self._lock:
            records = self._records()
            keys = self._record_keys(records)
            if self._kb is not None and keys == self._indexed_keys:
                return self._kb, self._kb_id
            await self._sync(records)
            return self._kb, self._kb_id

    async def refresh(self, *, force: bool = False) -> dict[str, Any]:
        if force:
            # Wipe the cached PDF text AND the persisted vector index so everything re-embeds.
            with self._conn() as c:
                c.execute("DELETE FROM rag_text")
            kb, kb_id = await self._kb_handle()
            try:
                await kb.delete_kb(kb_id)
            except Exception:  # noqa: BLE001
                pass
            self._kb, self._kb_id = None, None
        self._indexed_ids, self._indexed_keys = set(), set()
        await self._ensure()
        return {
            "ok": True, "indexed": len(self._indexed_ids),
            "library_items": self._lib.count(), "saved_news": len(self._news.rag_records()),
            "attachments": self._att.count(),
        }

    async def sync(self) -> dict[str, Any]:
        """Bring the index in line with the current sources NOW (incremental — only new/changed
        docs embed, removed ones prune). Used after an attachment is uploaded or deleted so the
        file is searchable immediately rather than on the next ask. Best-effort, never raises."""
        try:
            await self._ensure()
            return {"ok": True, "indexed": len(self._indexed_ids)}
        except Exception as exc:  # noqa: BLE001 - a warm-sync hiccup must not fail the upload
            return {"ok": False, "message": f"{type(exc).__name__}"}

    async def search(self, query: str, *, top_k: int = 8) -> list[dict[str, Any]]:
        kb, kb_id = await self._ensure()
        if kb is None or kb_id is None:
            return []
        chunks = await kb.search(
            kb_id, query, top_k=max(1, min(int(top_k), 16)),
            workspace_id=KB_WORKSPACE_ID, client_id=KB_CLIENT_ID,
        )
        out: list[dict[str, Any]] = []
        for c in chunks:
            meta = c.metadata or {}
            out.append({
                "citation": meta.get("citation"), "title": meta.get("title"),
                "authors": meta.get("authors", []), "year": meta.get("year"),
                "doi": meta.get("doi"), "id": meta.get("id"),
                # "paper" | "note" | "highlight" — so the answer can attribute the user's own marks.
                "kind": meta.get("kind", "paper"),
                "passage": (c.text or "")[:1600],
            })
        return out


def _get_index(config: HimmyConfig | None = None) -> PapersIndex:
    cfg = config or load_config()
    key = str(cfg.papers_cache_path)
    idx = _INDEX_CACHE.get(key)
    if idx is None:
        idx = PapersIndex(cfg)
        _INDEX_CACHE[key] = idx
    return idx


class PapersRagConnector:
    """Registers ask_papers (chat-with-your-library) + index_papers (rebuild)."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        self._cfg = config or load_config()

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        cfg = self._cfg

        async def ask_papers(args: dict[str, Any]) -> dict[str, Any]:
            query = str(args.get("query") or "").strip()
            if not query:
                return {"ok": False, "message": "What do you want to ask the library?"}
            try:
                results = await _get_index(cfg).search(query, top_k=int(args.get("top_k", 8)))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Paper search failed: {exc}"}
            if not results:
                return {
                    "ok": True, "results": [],
                    "message": "No papers in the library yet. Add some in the Library tab first.",
                }
            return {
                "ok": True, "results": results,
                "note": "Passages from the user's OWN library. Answer from these and cite each source.",
            }

        async def index_papers(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return await _get_index(cfg).refresh(force=bool(args.get("force", False)))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Indexing failed: {exc}"}

        register_local_tool(
            registry, name="ask_papers", read_only=True, handler=ask_papers,
            description=(
                "Answer a question FROM the full text of the user's own collection — their saved "
                "papers + PDFs, the news articles they saved to read later, AND any FILES they "
                "uploaded to Himmy (PDFs, Word docs, spreadsheets, screenshots/photos Himmy read, "
                "voice notes Himmy transcribed) — returning ranked passages each with a citation. "
                "Use for 'summarise this paper', 'what does my library say about X', 'what was that "
                "article I saved about Y', and 'what did that file/contract/screenshot I sent say'. "
                "Pass `query`; optional `top_k` (default 8). For a SUMMARY or deep explanation of one "
                "source, raise `top_k` to 12-16 so you get broad coverage to summarise from. Always "
                "cite the sources."
            ),
            args_json_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The research question."},
                    "top_k": {
                        "type": "integer",
                        "description": "How many passages (default 8; use 12-16 to summarise one source).",
                    },
                },
                "required": ["query"],
            },
        )
        register_local_tool(
            registry, name="index_papers", read_only=False, handler=index_papers,
            description=(
                "Rebuild the search index over the user's library (after adding papers). "
                "Pass `force: true` to re-read every PDF. Usually auto-runs, so rarely needed."
            ),
            args_json_schema={
                "type": "object",
                "properties": {"force": {"type": "boolean", "description": "Re-read all PDFs."}},
            },
        )
        return ["ask_papers", "index_papers"]


__all__ = ["PapersRagConnector", "PapersIndex", "_get_index"]
