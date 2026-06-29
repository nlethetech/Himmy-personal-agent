"""News index — the whole news corpus embedded into a local vector store.

This is the OSINT desk pattern, offline and production-grade:

  * EVERY fetched article (Nepali AND English) is embedded with a MULTILINGUAL model and persisted —
    so the corpus is RAG-searchable (Himmy can answer over all the news, not just what you saved),
    and reports of the SAME event MERGE ACROSS LANGUAGES into one story.
  * Articles cluster into STORIES by cosine similarity: a clear match (>= HIGH) auto-joins; an
    ambiguous "gray-zone" pair is validated by ONE cheap model call (cached) — exactly the OSINT
    "embeddings do the work, the LLM only judges the gray zone" split; everything else is a new story.
  * Bounded + robust: the model loads lazily off the event loop, embeds/clustering run in a thread,
    the index is pruned to a rolling window, and every step degrades gracefully (a missing embedder
    or model never breaks ingestion or the feed).

Embedder: a local ``fastembed`` multilingual model (``paraphrase-multilingual-MiniLM-L12-v2`` by
default, covers Nepali) — no per-article API cost. Override via ``HIMMY_NEWS_EMBED_MODEL``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sqlite3
import time
import uuid
from typing import Any

import numpy as np

from himmy_app.config import HimmyConfig, load_config

#: Multilingual sentence-embedding model (Nepali + English). 384-dim, ~470MB, local/offline.
_EMBED_MODEL = os.environ.get(
    "HIMMY_NEWS_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
#: Rolling retention — news is perishable; keep ~10 days so stories accrue reports but stay bounded.
RETENTION_DAYS = int(os.environ.get("HIMMY_NEWS_RETENTION_DAYS") or "10")
#: Clustering thresholds (cosine). >= HIGH → same story; [GRAY_LOW, HIGH) → ask the model; else new.
#: Cross-lingual same-event lands ~0.55-0.65, same-language ~0.8+, different events ~<0.2.
SIM_HIGH = float(os.environ.get("HIMMY_NEWS_SIM_HIGH") or "0.62")
SIM_GRAY_LOW = float(os.environ.get("HIMMY_NEWS_SIM_GRAY_LOW") or "0.46")
#: Cap model gray-zone checks per ingest so a big batch can't run up cost.
MAX_GRAY_CHECKS = int(os.environ.get("HIMMY_NEWS_MAX_GRAY") or "16")


# ---------------------------------------------------------------------------------------
# Multilingual embedder — lazy, process-wide, loaded off the event loop
# ---------------------------------------------------------------------------------------
_EMBEDDER: Any = None
_EMBED_DIM = 384


def _embedder() -> Any:
    global _EMBEDDER
    if _EMBEDDER is None:
        from fastembed import TextEmbedding

        _EMBEDDER = TextEmbedding(_EMBED_MODEL)
    return _EMBEDDER


def embed_texts(texts: list[str]) -> np.ndarray:
    """L2-normalised embeddings for ``texts`` as a (n, dim) float32 array. Empty input → (0, dim)."""
    if not texts:
        return np.zeros((0, _EMBED_DIM), dtype=np.float32)
    vecs = np.asarray(list(_embedder().embed(list(texts))), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def detect_lang(text: str) -> str:
    """'ne' if the text is mostly Devanagari, else 'en' (good enough for a news headline tag)."""
    dev = len(re.findall(r"[ऀ-ॿ]", text or ""))
    return "ne" if dev >= 3 else "en"


def _to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# ---------------------------------------------------------------------------------------
# NewsIndex — SQLite store of embedded articles + story clustering + semantic search
# ---------------------------------------------------------------------------------------
class NewsIndex:
    def __init__(self, config: HimmyConfig | None = None) -> None:
        self._cfg = config or load_config()
        self._db = str(self._cfg.data_dir / "news_index.db")
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db, timeout=20)
        c.row_factory = sqlite3.Row
        return c

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS articles (
                    id TEXT PRIMARY KEY, url TEXT UNIQUE, title TEXT, source TEXT, lang TEXT,
                    category TEXT, ts REAL, snippet TEXT, image TEXT, story_id TEXT,
                    emb BLOB, created REAL
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_art_ts ON articles(ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_art_story ON articles(story_id)")
            c.execute(
                "CREATE TABLE IF NOT EXISTS gray_cache (pair TEXT PRIMARY KEY, same INTEGER, created REAL)"
            )

    # ---- ingest: embed new articles, cluster into stories, persist -----------------------
    async def ingest(self, items: list[dict[str, Any]], *, category: str = "") -> dict[str, Any]:
        """Embed + cluster + store new articles (dedup by url). Heavy work runs off the loop."""
        try:
            return await asyncio.to_thread(self._ingest_sync, items, category)
        except Exception as exc:  # noqa: BLE001 - ingestion is best-effort; never break the caller
            return {"ok": False, "ingested": 0, "error": f"{type(exc).__name__}: {exc}"}

    def _ingest_sync(self, items: list[dict[str, Any]], category: str) -> dict[str, Any]:
        # 1) keep only items we haven't stored (by url) and have a title
        fresh: list[dict[str, Any]] = []
        with self._conn() as c:
            for it in items:
                url = (it.get("url") or "").strip()
                title = (it.get("title") or "").strip()
                if not url or not title:
                    continue
                if c.execute("SELECT 1 FROM articles WHERE url = ?", (url,)).fetchone():
                    continue
                fresh.append(it)
        if not fresh:
            return {"ok": True, "ingested": 0}

        # 2) embed the new headlines (title + snippet gives the model a little more signal)
        vecs = embed_texts([f"{it.get('title', '')}. {it.get('snippet', '')}".strip()[:400] for it in fresh])

        # 3) load the recent pool (existing stories) once, then cluster incrementally
        pool = self._recent_pool()                       # [(story_id, vec, title, lang)]
        gray_budget = MAX_GRAY_CHECKS
        rows: list[tuple] = []
        now = time.time()
        for it, vec in zip(fresh, vecs):
            lang = it.get("lang") or detect_lang(it.get("title", ""))
            story_id, used_gray = self._assign_story(it, vec, lang, pool, gray_budget)
            if used_gray:
                gray_budget -= 1
            pool.append((story_id, vec, it.get("title", ""), lang))
            rows.append((
                uuid.uuid4().hex, it.get("url", ""), it.get("title", ""), it.get("source", ""),
                lang, it.get("category") or category, float(it.get("ts") or now),
                it.get("snippet", ""), it.get("image", ""), story_id, _to_blob(vec), now,
            ))

        with self._conn() as c:
            c.executemany(
                "INSERT OR IGNORE INTO articles (id,url,title,source,lang,category,ts,snippet,image,"
                "story_id,emb,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows,
            )
        self._prune()
        return {"ok": True, "ingested": len(rows)}

    def _assign_story(self, it: dict[str, Any], vec: np.ndarray, lang: str,
                      pool: list[tuple], gray_budget: int) -> tuple[str, bool]:
        """Pick the story a new article belongs to: best cosine in the pool → HIGH auto-join, gray
        zone → one cached model check, else a fresh story id. Returns (story_id, used_gray_check)."""
        if not pool:
            return uuid.uuid4().hex, False
        mat = np.asarray([p[1] for p in pool], dtype=np.float32)
        sims = mat @ vec
        best = int(np.argmax(sims))
        score = float(sims[best])
        if score >= SIM_HIGH:
            return pool[best][0], False
        if score >= SIM_GRAY_LOW and gray_budget > 0:
            same = self._gray_same_event(it.get("title", ""), pool[best][2])
            if same:
                return pool[best][0], True
            return uuid.uuid4().hex, True
        return uuid.uuid4().hex, False

    def _recent_pool(self) -> list[list[Any]]:
        cutoff = time.time() - RETENTION_DAYS * 86400
        with self._conn() as c:
            rows = c.execute(
                "SELECT story_id, emb, title, lang FROM articles WHERE ts >= ? ORDER BY ts DESC LIMIT 4000",
                (cutoff,),
            ).fetchall()
        return [[r["story_id"], _from_blob(r["emb"]), r["title"], r["lang"]] for r in rows if r["emb"]]

    # ---- gray-zone: ONE cheap model call deciding an ambiguous pair, cached ---------------
    def _gray_same_event(self, title_a: str, title_b: str) -> bool:
        key = "||".join(sorted([title_a.strip().lower()[:160], title_b.strip().lower()[:160]]))
        with self._conn() as c:
            row = c.execute("SELECT same FROM gray_cache WHERE pair = ?", (key,)).fetchone()
            if row is not None:
                return bool(row["same"])
        same = self._llm_same_event(title_a, title_b)
        with contextlib.suppress(Exception):
            with self._conn() as c:
                c.execute("INSERT OR REPLACE INTO gray_cache (pair, same, created) VALUES (?,?,?)",
                          (key, 1 if same else 0, time.time()))
        return same

    def _llm_same_event(self, title_a: str, title_b: str) -> bool:
        """Ask the model whether two headlines report the SAME real-world event. Fail-closed (False)
        so an outage never wrongly merges distinct stories."""
        try:
            from himmy.cli.provider import build_inference_for
            from himmy.services.inference.models import InferenceMessage, InferenceRequest

            svc = build_inference_for(self._cfg.provider, self._cfg.model)
            sys = ("You judge whether two news headlines (possibly different languages) report the "
                   "SAME specific real-world event/story. Answer ONLY 'yes' or 'no'. Same ongoing "
                   "topic but different events = no.")
            resp = asyncio_run_blocking(svc.run(InferenceRequest(
                messages=[InferenceMessage(role="system", content=sys),
                          InferenceMessage(role="user", content=f"A: {title_a}\nB: {title_b}")],
                generation_params={"temperature": 0}, timeout_seconds=20,
            )))
            return (resp.output_text or "").strip().lower().startswith("y")
        except Exception:  # noqa: BLE001
            return False

    # ---- semantic search over the corpus (RAG) ------------------------------------------
    def search(self, query: str, *, k: int = 12, days: int | None = None) -> list[dict[str, Any]]:
        """Top-k articles by cosine to the query (any language). Powers the search_news RAG tool."""
        q = (query or "").strip()
        if not q:
            return []
        qv = embed_texts([q])[0]
        cutoff = time.time() - (days or RETENTION_DAYS) * 86400
        with self._conn() as c:
            rows = c.execute(
                "SELECT url,title,source,lang,category,ts,snippet,image,story_id,emb FROM articles "
                "WHERE ts >= ? ORDER BY ts DESC LIMIT 5000", (cutoff,),
            ).fetchall()
        if not rows:
            return []
        mat = np.asarray([_from_blob(r["emb"]) for r in rows], dtype=np.float32)
        sims = mat @ qv
        order = np.argsort(-sims)[: max(1, k)]
        out = []
        for i in order:
            r = rows[int(i)]
            out.append({"title": r["title"], "url": r["url"], "source": r["source"], "lang": r["lang"],
                        "category": r["category"], "ago": _ago(r["ts"]), "snippet": r["snippet"],
                        "score": round(float(sims[int(i)]), 3)})
        return out

    # ---- serve clustered STORIES (cross-lingual merged) for the feed ---------------------
    def stories(self, *, category: str | None = None, limit: int = 45,
                lang: str | None = None) -> list[dict[str, Any]]:
        """Recent stories, each = a lead report + all its cross-outlet/cross-language reports."""
        cutoff = time.time() - RETENTION_DAYS * 86400
        sql = ("SELECT url,title,source,lang,category,ts,snippet,image,story_id FROM articles "
               "WHERE ts >= ?")
        args: list[Any] = [cutoff]
        if category:
            sql += " AND category = ?"
            args.append(category)
        sql += " ORDER BY ts DESC LIMIT 3000"
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(sql, args).fetchall()]
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(r["story_id"], []).append(r)
        stories: list[dict[str, Any]] = []
        for members in groups.values():
            members.sort(key=lambda x: x.get("ts") or 0, reverse=True)
            lead = members[0]
            if lang and lead.get("lang") != lang:
                # prefer a lead in the requested display language when the story has one
                pref = next((m for m in members if m.get("lang") == lang), None)
                if pref:
                    lead = pref
            srcs = list(dict.fromkeys(m.get("source", "") for m in members if m.get("source")))
            stories.append({
                "title": lead["title"], "url": lead["url"], "source": lead["source"],
                "image": next((m["image"] for m in members if m.get("image")), ""),
                "snippet": lead.get("snippet", ""), "ts": lead["ts"], "ago": _ago(lead["ts"]),
                "lang": lead.get("lang", "en"), "category": lead.get("category", ""),
                "report_count": len(members),
                "reports": [{"source": m.get("source", ""), "url": m.get("url", ""),
                             "title": m.get("title", ""), "lang": m.get("lang", ""),
                             "ago": _ago(m.get("ts"))} for m in members[:8]],
            })
        stories.sort(key=lambda s: s.get("ts") or 0, reverse=True)
        return stories[: max(1, limit)]

    def _prune(self) -> None:
        cutoff = time.time() - RETENTION_DAYS * 86400
        with contextlib.suppress(Exception):
            with self._conn() as c:
                c.execute("DELETE FROM articles WHERE ts < ?", (cutoff,))
                c.execute("DELETE FROM gray_cache WHERE created < ?", (time.time() - 30 * 86400,))

    def stats(self) -> dict[str, Any]:
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM articles").fetchone()["n"]
            s = c.execute("SELECT COUNT(DISTINCT story_id) n FROM articles").fetchone()["n"]
        return {"articles": int(n), "stories": int(s), "model": _EMBED_MODEL}


def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    secs = max(0, int(time.time() - float(ts)))
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def asyncio_run_blocking(coro: Any) -> Any:
    """Run a coroutine to completion from a SYNC context (the ingest thread). Uses a private loop so
    it never touches a running loop on the caller's thread."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_INDEX: NewsIndex | None = None


def get_news_index() -> NewsIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = NewsIndex(load_config())
    return _INDEX


__all__ = ["NewsIndex", "get_news_index", "embed_texts", "detect_lang", "RETENTION_DAYS"]
