"""News hub — a real, non-LLM news reader.

Pulls headlines from curated RSS feeds, grouped into categories (Nepal, World, Business,
Technology) plus a personalised "For You" built from the user's topics. Deterministic: no model
in the loop — just fetch, parse, dedupe, sort by recency. Each category is cached briefly so
switching tabs is instant.

(The arXiv/OpenAlex/Himmy paper-recommendation helpers live on here too — they power the
separate "Recommended papers" surface, not this news reader.)
"""

from __future__ import annotations

import asyncio
import datetime
import html
import json
import os
import re
import sqlite3
import time
import warnings
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx

from himmy_app.config import HimmyConfig, load_config
from himmy_app.library import Library

#: A real desktop browser UA — some publishers gate the bare httpx default.
_READER_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
}
DEFAULT_FOLDER = "Reading List"

#: Curated, tested RSS sources per category (name, url).
SOURCES: dict[str, list[tuple[str, str]]] = {
    "Nepal": [
        ("The Kathmandu Post", "https://kathmandupost.com/rss"),
        ("Online Khabar", "https://english.onlinekhabar.com/feed"),
        ("Ratopati", "https://english.ratopati.com/feed"),
    ],
    "World": [
        ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("The Guardian", "https://www.theguardian.com/world/rss"),
    ],
    "Business": [
        ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/business/rss"),
    ],
    "Technology": [
        ("TechCrunch", "https://techcrunch.com/feed/"),
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ],
}
CATEGORIES = ["For You", "Nepal", "World", "Business", "Technology"]
_CACHE_TTL = 900  # seconds


def _clean(text: str) -> str:
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text or "", flags=re.DOTALL)  # unwrap CDATA
    text = html.unescape(text)                                                   # &lt; -> <
    text = re.sub(r"<[^>]+>", "", text)                                          # strip tags
    return re.sub(r"\s+", " ", text).strip()


def _ago(ts: float) -> str:
    if not ts:
        return ""
    secs = max(0, datetime.datetime.now(datetime.timezone.utc).timestamp() - ts)
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _parse_date(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:  # noqa: BLE001
        pass
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _image(block: str) -> str:
    for pat in (
        r'media:thumbnail[^>]*\burl="([^"]+)"',
        r'media:content[^>]*\burl="([^"]+)"',
        r'<enclosure[^>]*\burl="([^"]+)"[^>]*type="image',
        r'<enclosure[^>]*type="image[^"]*"[^>]*\burl="([^"]+)"',
        r'<link[^>]*rel="enclosure"[^>]*type="image[^"]*"[^>]*href="([^"]+)"',
        r'<img[^>]*\bsrc="([^"]+)"',
    ):
        m = re.search(pat, block, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


# ---- in-app article reading (Safari-Reader-style extraction) ----------------------------
def _traf_extract(html_text: str, url: str) -> dict[str, Any]:
    """Best-quality extraction via trafilatura → clean title/byline/paragraphs/lead image."""
    try:
        import trafilatura  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    try:
        out = trafilatura.extract(
            html_text, url=url, output_format="json", favor_recall=True,
            include_comments=False, include_images=False, include_tables=False,
        )
    except Exception:  # noqa: BLE001
        out = None
    if not out:
        return {}
    try:
        d = json.loads(out)
    except Exception:  # noqa: BLE001
        return {}
    text = (d.get("text") or "").strip()
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    return {
        "title": (d.get("title") or "").strip(),
        "author": (d.get("author") or "").strip(),
        "date": (d.get("date") or "").strip(),
        "image": (d.get("image") or "").strip(),
        "source": (d.get("sitename") or d.get("hostname") or "").strip(),
        "paragraphs": paras,
        "text": "\n\n".join(paras),
    }


def _bs4_extract(html_text: str, url: str) -> dict[str, Any]:
    """Fallback extraction using BeautifulSoup heuristics (no lxml needed)."""
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    warnings.filterwarnings("ignore", message="It looks like you're using an HTML parser")
    soup = BeautifulSoup(html_text, "html.parser")
    for t in soup(["script", "style", "noscript", "aside", "nav", "header", "footer", "form", "figure"]):
        t.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    paras: list[str] = []
    for p in root.find_all("p"):
        txt = _clean(p.get_text(" ", strip=True))
        if len(txt) >= 40:
            paras.append(txt)
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = _clean(h1.get_text())
    if not title and soup.title:
        title = _clean(soup.title.get_text())
    img = ""
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        img = og["content"]
    return {
        "title": title, "author": "", "date": "", "image": img,
        "source": urlparse(url).hostname or "", "paragraphs": paras, "text": "\n\n".join(paras),
    }


async def extract_article(url: str) -> dict[str, Any]:
    """Fetch a news URL and return clean, readable content for the in-app reader."""
    url = (url or "").strip()
    if not url.startswith("http"):
        return {"ok": False, "message": "That isn't a readable link."}
    html_text = ""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_READER_UA) as c:
            r = await c.get(url)
            if r.status_code == 200 and "html" in r.headers.get("content-type", "").lower():
                html_text = r.text
                url = str(r.url)  # resolved (handles publisher redirects)
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "Couldn't reach that article."}
    if not html_text:
        return {"ok": False, "message": "Couldn't load that article for reading."}

    data = await asyncio.to_thread(_traf_extract, html_text, url)
    if len(data.get("paragraphs") or []) < 3:
        fb = await asyncio.to_thread(_bs4_extract, html_text, url)
        if len(fb.get("paragraphs") or []) > len(data.get("paragraphs") or []):
            data = {**data, **{k: v for k, v in fb.items() if v}}
    if not data.get("paragraphs"):
        return {"ok": False, "message": "This page couldn't be parsed for reading — open the original."}
    # Backfill headline / lead image from page meta when the extractor left them blank.
    if not data.get("title"):
        data["title"] = _meta_title(html_text)
    if not data.get("image"):
        data["image"] = _meta_image(html_text)
    if not data.get("source"):
        data["source"] = urlparse(url).hostname or ""
    # Drop a leading paragraph that just repeats the headline.
    paras = data.get("paragraphs") or []
    if len(paras) > 1 and _norm(paras[0]) and _norm(paras[0]) == _norm(data.get("title", "")):
        paras = paras[1:]
        data["paragraphs"] = paras
        data["text"] = "\n\n".join(paras)
    return {"ok": True, "url": url, **data}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _meta_title(html_text: str) -> str:
    for pat in (r'<meta[^>]*property="og:title"[^>]*content="([^"]+)"',
                r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"):
        m = re.search(pat, html_text, re.IGNORECASE | re.DOTALL)
        if m:
            t = _clean(m.group(1))
            if t:
                return re.sub(r"\s*[|\-–—]\s*[^|\-–—]{0,40}$", "", t).strip() or t
    return ""


def _meta_image(html_text: str) -> str:
    for pat in (r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"',
                r'<meta[^>]*name="twitter:image"[^>]*content="([^"]+)"'):
        m = re.search(pat, html_text, re.IGNORECASE)
        if m:
            return html.unescape(m.group(1).strip())
    return ""


class NewsService:
    def __init__(self, config: HimmyConfig | None = None) -> None:
        cfg = config or load_config()
        self._cfg = cfg
        self._store = cfg.data_dir / "news.json"
        self._cache = cfg.data_dir / "news_cache.json"
        self._lib = Library(cfg)

    # ---- persisted interests (for "For You") --------------------------------------------
    def _read(self, path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def get_interests(self) -> list[str]:
        return self._read(self._store).get("interests", [])

    def set_interests(self, interests: list[str]) -> dict[str, Any]:
        data = self._read(self._store)
        data["interests"] = [str(i).strip() for i in interests if str(i).strip()][:24]
        self._store.write_text(json.dumps(data), encoding="utf-8")
        return {"ok": True, "interests": data["interests"]}

    # ---- RSS fetching -------------------------------------------------------------------
    async def _fetch_feed(self, client: httpx.AsyncClient, name: str, url: str) -> list[dict[str, Any]]:
        try:
            r = await client.get(url)
            xml = r.text if r.status_code == 200 else ""
        except Exception:  # noqa: BLE001
            return []
        blocks = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL) or re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL)
        out = []
        for b in blocks[:25]:
            def tag(n: str) -> str:
                m = re.search(rf"<{n}[^>]*>(.*?)</{n}>", b, re.DOTALL)
                return _clean(m.group(1)) if m else ""
            title = tag("title")
            link = tag("link")
            if not link:
                m = re.search(r'<link[^>]*href="([^"]+)"', b)
                link = m.group(1) if m else ""
            if not title or not link:
                continue
            ts = _parse_date(tag("pubDate") or tag("published") or tag("updated"))
            out.append({
                "title": title, "url": link, "source": name,
                "image": _image(b), "snippet": _clean(tag("description") or tag("summary"))[:160],
                "ts": ts, "ago": _ago(ts),
            })
        return out

    async def _category(self, cat: str) -> list[dict[str, Any]]:
        feeds = SOURCES.get(cat, [])
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (Himmy)"}) as c:
            results = await asyncio.gather(*[self._fetch_feed(c, n, u) for n, u in feeds])
        items = [it for sub in results for it in sub]
        seen: set[str] = set()
        deduped = []
        for it in sorted(items, key=lambda x: x["ts"], reverse=True):
            k = it["title"].lower()
            if k in seen:
                continue
            seen.add(k)
            deduped.append(it)
        return deduped[:45]

    async def _for_you(self, interests: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (Himmy)"}) as c:
            for kw in interests[:6]:
                try:
                    r = await c.get(f"https://news.google.com/rss/search?q={quote_plus(kw)}&hl=en-US&gl=US&ceid=US:en")
                    xml = r.text if r.status_code == 200 else ""
                except Exception:  # noqa: BLE001
                    xml = ""
                for b in re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)[:6]:
                    def tag(n: str) -> str:
                        m = re.search(rf"<{n}[^>]*>(.*?)</{n}>", b, re.DOTALL)
                        return _clean(m.group(1)) if m else ""
                    title = tag("title")
                    if not title or title.lower() in seen:
                        continue
                    seen.add(title.lower())
                    ts = _parse_date(tag("pubDate"))
                    out.append({
                        "title": title, "url": tag("link"), "source": tag("source"),
                        "image": "", "snippet": "", "ts": ts, "ago": _ago(ts), "topic": kw,
                    })
        return sorted(out, key=lambda x: x["ts"], reverse=True)[:30]

    # ---- public: a category feed, with a short cache ------------------------------------
    async def feed(self, category: str, force: bool = False) -> dict[str, Any]:
        cache = self._read(self._cache)
        entry = cache.get(category)
        now = datetime.datetime.now().timestamp()
        if not force and entry and (now - entry.get("at", 0)) < _CACHE_TTL:
            return {"ok": True, "category": category, "items": entry["items"], "fetched_at": entry.get("iso")}
        if category == "For You":
            interests = self.get_interests()
            if not interests:
                return {"ok": True, "category": category, "items": [], "needs_interests": True}
            items = await self._for_you(interests)
        else:
            items = await self._category(category)
        cache[category] = {"items": items, "at": now,
                           "iso": datetime.datetime.now().isoformat(timespec="seconds")}
        self._cache.write_text(json.dumps(cache), encoding="utf-8")
        return {"ok": True, "category": category, "items": items, "fetched_at": cache[category]["iso"]}

    def categories(self) -> list[str]:
        return CATEGORIES

    # ---- paper recommendations (for the future "Recommended" surface, NOT news) ---------
    async def _arxiv(self, interests: list[str], limit: int = 25) -> list[dict[str, Any]]:
        q = " OR ".join(f'all:"{kw}"' for kw in interests[:6])
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get("https://export.arxiv.org/api/query", params={
                    "search_query": q, "sortBy": "submittedDate", "sortOrder": "descending", "max_results": limit})
            r.raise_for_status()
            xml = r.text
        except Exception:  # noqa: BLE001
            return []
        out = []
        for e in re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL):
            def tag(n: str) -> str:
                m = re.search(rf"<{n}>(.*?)</{n}>", e, re.DOTALL)
                return _clean(m.group(1)) if m else ""
            aid = ""
            m = re.search(r"<id>https?://arxiv\.org/abs/([^<]+)</id>", e)
            if m:
                aid = m.group(1).split("v")[0]
            authors = [_clean(a) for a in re.findall(r"<author>\s*<name>(.*?)</name>", e, re.DOTALL)]
            out.append({"title": tag("title"), "abstract": tag("summary")[:700], "authors": authors,
                        "year": tag("published")[:4], "venue": "arXiv", "arxiv": aid, "doi": "",
                        "url": f"https://arxiv.org/abs/{aid}" if aid else ""})
        return out

    async def recommendations(self, force: bool = False) -> dict[str, Any]:
        """Paper recommendations from across the literature, seeded by what the user reads.

        Delegates to the multi-source :class:`~himmy_app.recsys.recommend.Recommender` (OpenAlex
        related-works + concepts, Semantic Scholar, Crossref, arXiv), ranked by the reading-weighted
        taste profile — so economics / political-theory readers get economics / political-theory
        papers, not just arXiv STEM. Falls back to the old arXiv-on-interests path only if the
        engine errors out, so this endpoint can never hard-fail.
        """
        try:
            from himmy_app.recsys.recommend import Recommender

            return await Recommender(self._cfg).recommend(force=force)
        except Exception:  # noqa: BLE001 - never let a recommender hiccup break the surface
            interests = self.get_interests()
            if not interests:
                return {"ok": True, "papers": []}
            return {"ok": True, "papers": (await self._arxiv(interests))[:8]}


def _news_id() -> str:
    return f"news_{int(time.time() * 1000):x}_{abs(hash(time.time())) % 100000:05d}"


class SavedNews:
    """Articles the user saved to read later — stored in library.db (so they ride along with
    backups) and fed to the papers RAG so Himmy can read them. Organised into simple folders."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        cfg = config or load_config()
        self._db = cfg.data_dir / "library.db"
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._db))
        c.row_factory = sqlite3.Row
        return c

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS saved_news (
                    id TEXT PRIMARY KEY, title TEXT, source TEXT, url TEXT UNIQUE,
                    image TEXT, author TEXT, published TEXT, snippet TEXT,
                    text TEXT, folder TEXT, saved_at REAL
                )"""
            )

    def _row(self, r: sqlite3.Row, *, with_text: bool = False) -> dict[str, Any]:
        d = dict(r)
        d["paragraphs"] = [p for p in (d.get("text") or "").split("\n\n") if p.strip()] if with_text else []
        if not with_text:
            d.pop("text", None)
        return d

    def folders(self) -> dict[str, Any]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT folder, COUNT(*) AS n FROM saved_news GROUP BY folder"
            ).fetchall()
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["folder"] or DEFAULT_FOLDER] = counts.get(r["folder"] or DEFAULT_FOLDER, 0) + r["n"]
        folders = [{"name": k, "count": v} for k, v in sorted(counts.items())]
        return {"total": sum(counts.values()), "folders": folders}

    def list(self, folder: str | None = None, query: str = "") -> list[dict[str, Any]]:
        with self._conn() as c:
            if folder:
                rows = c.execute(
                    "SELECT * FROM saved_news WHERE folder = ? ORDER BY saved_at DESC", (folder,)
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM saved_news ORDER BY saved_at DESC").fetchall()
        items = [self._row(r) for r in rows]
        q = query.strip().lower()
        if q:
            items = [
                it for it in items
                if q in (it.get("title") or "").lower()
                or q in (it.get("source") or "").lower()
                or q in (it.get("snippet") or "").lower()
            ]
        return items

    def urls(self) -> list[dict[str, str]]:
        """Light list of {id, url} so the UI can mark already-saved cards."""
        with self._conn() as c:
            rows = c.execute("SELECT id, url FROM saved_news").fetchall()
        return [{"id": r["id"], "url": r["url"]} for r in rows]

    def get(self, nid: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM saved_news WHERE id = ?", (nid,)).fetchone()
        return self._row(r, with_text=True) if r else None

    def _id_for_url(self, url: str) -> str | None:
        with self._conn() as c:
            r = c.execute("SELECT id FROM saved_news WHERE url = ?", (url,)).fetchone()
        return r["id"] if r else None

    async def save(self, payload: dict[str, Any], folder: str | None = None) -> dict[str, Any]:
        url = (payload.get("url") or "").strip()
        if not url:
            return {"ok": False, "message": "Nothing to save."}
        folder = (folder or DEFAULT_FOLDER).strip() or DEFAULT_FOLDER
        art = await extract_article(url)
        text = art.get("text", "") if art.get("ok") else ""
        title = (payload.get("title") or art.get("title") or url).strip()
        source = (payload.get("source") or art.get("source") or "").strip()
        image = (payload.get("image") or art.get("image") or "").strip()
        author = art.get("author", "")
        published = art.get("date", "")
        snippet = (payload.get("snippet") or (text[:220] if text else "")).strip()
        nid = self._id_for_url(url)
        with self._conn() as c:
            if nid:
                c.execute(
                    "UPDATE saved_news SET title=?,source=?,image=?,author=?,published=?,"
                    "snippet=?,text=?,folder=? WHERE id=?",
                    (title, source, image, author, published, snippet, text, folder, nid),
                )
            else:
                nid = _news_id()
                c.execute(
                    "INSERT INTO saved_news (id,title,source,url,image,author,published,"
                    "snippet,text,folder,saved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, title, source, url, image, author, published, snippet, text, folder, time.time()),
                )
        return {"ok": True, "id": nid, "folder": folder, "readable": bool(text)}

    def remove(self, nid: str) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("DELETE FROM saved_news WHERE id = ?", (nid,))
        return {"ok": True}

    def move(self, nid: str, folder: str) -> dict[str, Any]:
        folder = (folder or DEFAULT_FOLDER).strip() or DEFAULT_FOLDER
        with self._conn() as c:
            c.execute("UPDATE saved_news SET folder = ? WHERE id = ?", (folder, nid))
        return {"ok": True, "folder": folder}

    def rag_records(self) -> list[dict[str, Any]]:
        """Saved articles shaped for the papers RAG (text already extracted)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id,title,source,url,published,text FROM saved_news WHERE text != ''"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            out.append({
                "id": d["id"], "title": d["title"],
                "authors": [d["source"]] if d.get("source") else [],
                "year": (d.get("published") or "")[:4], "venue": d.get("source") or "",
                "abstract": "", "text": d["text"], "url": d.get("url"),
            })
        return out


__all__ = ["NewsService", "SavedNews", "CATEGORIES", "DEFAULT_FOLDER", "extract_article"]
