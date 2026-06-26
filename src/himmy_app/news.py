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

#: Curated RSS sources per category (name, url). EVERY url here was live-validated (fetched and
#: confirmed to parse to >=3 items) — dead / 404 / Cloudflare-blocked feeds were dropped, so this
#: list ships only working sources. Reliability and INTERNATIONAL breadth are the curation rules:
#:   * World   — reputable global wire/broadsheet outlets across several countries (UK/US/Qatar/
#:               Germany/France), so coverage isn't single-nation.
#:   * Technology — a tight allow-list of respected tech outlets only (no SEO-farm aggregators).
#: Note: Reuters and the Associated Press no longer publish open public RSS feeds (their old
#: endpoints now 404 / refuse connections), so they're intentionally absent rather than dead.
SOURCES: dict[str, list[tuple[str, str]]] = {
    "Nepal": [
        ("The Kathmandu Post", "https://kathmandupost.com/rss"),
        ("Online Khabar", "https://english.onlinekhabar.com/feed"),
        ("Ratopati", "https://english.ratopati.com/feed"),
    ],
    # International desk: deliberately multi-country so it reads as world news, not one outlet's.
    "World": [
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("The Guardian", "https://www.theguardian.com/world/rss"),
        ("Deutsche Welle", "https://rss.dw.com/rdf/rss-en-world"),
        ("France 24", "https://www.france24.com/en/rss"),
        ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
    ],
    "Business": [
        ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/business/rss"),
        ("CNBC", "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
        ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("NPR Business", "https://feeds.npr.org/1006/rss.xml"),
    ],
    # Reliable-only tech: respected outlets with editorial standards; no clickbait aggregators.
    "Technology": [
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
        ("Wired", "https://www.wired.com/feed/rss"),
        ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
        ("IEEE Spectrum", "https://spectrum.ieee.org/feeds/feed.rss"),
        ("Hacker News", "https://hnrss.org/frontpage"),
        ("The Register", "https://www.theregister.com/headlines.atom"),
        ("Engadget", "https://www.engadget.com/rss.xml"),
        ("Rest of World", "https://restofworld.org/feed/latest/"),
    ],
}
#: "World" is the INTERNATIONAL section. Order matters: "For You" (the personalised, taste-ranked
#: feed) always leads.
CATEGORIES = ["For You", "Nepal", "World", "Business", "Technology"]

#: Reputable source DOMAINS used to filter search-derived results (e.g. the Google-News pool in
#: "For You") down to trustworthy outlets. It's the union of every curated feed's domain above
#: PLUS other well-known reputable outlets we don't run a standing feed for — so a Google-News hit
#: from one of these is trusted even though it isn't in SOURCES. Used to PREFER (not hard-require)
#: reliable sources, with a stricter bar for tech (where SEO farms are rife).
TRUSTED_SOURCES: set[str] = {
    # Nepal
    "kathmandupost.com", "onlinekhabar.com", "english.onlinekhabar.com", "ratopati.com",
    "english.ratopati.com", "thehimalayantimes.com", "nepalitimes.com",
    "myrepublica.nagariknetwork.com", "setopati.com",
    # World / international wires + broadsheets
    "bbc.co.uk", "bbc.com", "aljazeera.com", "theguardian.com", "dw.com", "france24.com",
    "npr.org", "cnn.com", "reuters.com", "apnews.com", "nytimes.com", "washingtonpost.com",
    "ft.com", "economist.com", "bloomberg.com", "wsj.com", "afp.com", "politico.com",
    "axios.com", "time.com", "theatlantic.com", "newyorker.com", "pbs.org",
    # Business / markets  (dowjones.io is a CDN domain, not a publisher identity → excluded;
    # MarketWatch is already covered by marketwatch.com.)
    "cnbc.com", "marketwatch.com", "forbes.com", "businessinsider.com", "fortune.com",
    # Technology (reliable allow-list).  (anandtech.com dropped: the editorial site shut down in
    # Jan 2024 and now only redirects to its forums.)
    "arstechnica.com", "theverge.com", "wired.com", "technologyreview.com", "spectrum.ieee.org",
    "ieee.org", "ycombinator.com", "news.ycombinator.com", "theregister.com", "engadget.com",
    "restofworld.org", "techcrunch.com", "nature.com", "sciencemag.org", "science.org",
    "404media.co", "theinformation.com", "tomshardware.com",
}

#: Google News RSS items carry the outlet as a plain text LABEL (e.g. "The New York Times", "BBC"),
#: not a hostname — so the domain-based TRUSTED_SOURCES check can't see them. This maps the common
#: labels reputable outlets return to their canonical domain, so search-pool items from trusted
#: outlets still earn the reliability bonus. Lowercased keys; matched case-insensitively.
_TRUSTED_SOURCE_LABELS: dict[str, str] = {
    "bbc": "bbc.com", "bbc news": "bbc.com", "al jazeera": "aljazeera.com",
    "the guardian": "theguardian.com", "guardian": "theguardian.com",
    "deutsche welle": "dw.com", "dw": "dw.com", "france 24": "france24.com",
    "npr": "npr.org", "cnn": "cnn.com", "reuters": "reuters.com",
    "associated press": "apnews.com", "ap": "apnews.com", "ap news": "apnews.com",
    "the new york times": "nytimes.com", "new york times": "nytimes.com",
    "the washington post": "washingtonpost.com", "washington post": "washingtonpost.com",
    "financial times": "ft.com", "the economist": "economist.com", "economist": "economist.com",
    "bloomberg": "bloomberg.com", "the wall street journal": "wsj.com", "wall street journal": "wsj.com",
    "politico": "politico.com", "axios": "axios.com", "time": "time.com",
    "the atlantic": "theatlantic.com", "the new yorker": "newyorker.com", "pbs": "pbs.org",
    "cnbc": "cnbc.com", "marketwatch": "marketwatch.com", "forbes": "forbes.com",
    "business insider": "businessinsider.com", "fortune": "fortune.com",
    "ars technica": "arstechnica.com", "the verge": "theverge.com", "wired": "wired.com",
    "mit technology review": "technologyreview.com", "ieee spectrum": "spectrum.ieee.org",
    "the register": "theregister.com", "engadget": "engadget.com", "rest of world": "restofworld.org",
    "techcrunch": "techcrunch.com", "404 media": "404media.co", "the information": "theinformation.com",
    "tom's hardware": "tomshardware.com", "nature": "nature.com", "science": "science.org",
    "the kathmandu post": "kathmandupost.com", "kathmandu post": "kathmandupost.com",
    "online khabar": "onlinekhabar.com", "onlinekhabar": "onlinekhabar.com",
    "ratopati": "ratopati.com", "the himalayan times": "thehimalayantimes.com",
    "nepali times": "nepalitimes.com", "setopati": "setopati.com",
}


def _source_is_trusted(item: dict[str, Any]) -> bool:
    """True if an item comes from a trusted outlet — by URL domain OR by a recognised source LABEL.
    Google-News items have opaque ``news.google.com`` URLs and a plain outlet name in ``source``,
    so the label fallback is what makes the reliability bonus actually fire for search results."""
    if _is_trusted(_domain(item.get("url") or "")) or _is_trusted(_domain(item.get("source") or "")):
        return True
    label = (item.get("source") or "").strip().lower()
    return _is_trusted(_TRUSTED_SOURCE_LABELS.get(label, ""))


def _domain(value: str) -> str:
    """Bare registrable-ish host for a URL or a source label, lowercased, ``www.`` stripped."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "://" in v or v.startswith("//") or "/" in v or "." in v:
        host = urlparse(v if "://" in v else f"//{v}", scheme="http").hostname or v
    else:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_trusted(domain: str) -> bool:
    """True if ``domain`` is one of TRUSTED_SOURCES (exact host or a subdomain of one)."""
    d = (domain or "").lower()
    if not d:
        return False
    return any(d == t or d.endswith("." + t) for t in TRUSTED_SOURCES)


_CACHE_TTL = 900  # seconds
#: How often the background refresher re-pulls every category (real-time freshness). Override via
#: HIMMY_NEWS_REFRESH_SECS. Kept >= the cache TTL so a refresh always recomputes rather than
#: bouncing off a still-fresh cache.
_REFRESH_SECS = max(60, int(os.environ.get("HIMMY_NEWS_REFRESH_SECS") or "900"))


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

    # ---- "For You" — a taste-RANKED personal feed -------------------------------------------
    async def _google_news(self, client: httpx.AsyncClient, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """A handful of fresh items for one taste query, via the Google News RSS search."""
        try:
            r = await client.get(
                f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
            )
            xml = r.text if r.status_code == 200 else ""
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        for b in re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)[:limit]:
            def tag(n: str) -> str:
                m = re.search(rf"<{n}[^>]*>(.*?)</{n}>", b, re.DOTALL)
                return _clean(m.group(1)) if m else ""
            title = tag("title")
            link = tag("link")
            if not title or not link:
                continue
            ts = _parse_date(tag("pubDate"))
            out.append({
                "title": title, "url": link, "source": tag("source"),
                "image": "", "snippet": _clean(tag("description"))[:160],
                "ts": ts, "ago": _ago(ts), "topic": query,
            })
        return out

    def _build_profile(self) -> Any:
        """Build the reading-weighted taste :class:`Profile`, or ``None`` if it can't be built
        (no embedder, or an empty corpus → zero topics). Sync + blocking (fastembed), so the caller
        runs it off the event loop. Never raises."""
        try:
            from himmy_app.recsys.profile import build_profile

            prof = build_profile(self._cfg)
            return prof if prof is not None and prof.num_topics else None
        except Exception:  # noqa: BLE001 - the embedder/profile is a bonus, never a dependency
            return None

    async def _taste_queries(self, client: httpx.AsyncClient, interests: list[str]) -> list[str]:
        """The user's STRONGEST taste signals as search terms: their own research CONCEPTS (derived
        from the papers they actually read, via the Recommender's seed→OpenAlex→concept path)
        blended with their typed interests. Concepts lead (the demonstrated signal); interests
        always come along. Best-effort — degrades to just the typed interests."""
        concepts: list[str] = []
        try:
            from himmy_app.recsys import sources as _src
            from himmy_app.recsys.recommend import Recommender

            seeds = Recommender(self._cfg)._seed_papers()
            resolved = await asyncio.gather(
                *[_src.openalex_resolve(client, doi=s.get("doi", ""), title=s.get("title", "")) for s in seeds[:5]],
                return_exceptions=True,
            )
            for w in resolved:
                if not isinstance(w, dict):
                    continue
                for c in (w.get("concepts") or []):
                    if (c.get("score") or 0) >= 0.4 and 1 <= (c.get("level") or 0) <= 3:
                        name = c.get("display_name", "")
                        if name:
                            concepts.append(name)
        except Exception:  # noqa: BLE001
            concepts = []
        # Dedupe, concepts first (demonstrated taste), then typed interests; cap the fan-out.
        return list(dict.fromkeys([*concepts, *interests]))[:8]

    async def _curated_pool(self) -> list[dict[str, Any]]:
        """A breadth pool for "For You": fresh items from the curated categories (World/Tech/
        Business), so the taste ranker always has reputable international + tech stories to rank
        even when search is thin. Best-effort per category."""
        cats = ["World", "Technology", "Business"]
        # Use the cache-aware public feed() (not _category directly): the background refresher writes
        # these categories' caches in the SAME pass before "For You" runs, so this hits the warm
        # cache and issues zero extra RSS requests — no double-fetching the same endpoints.
        results = await asyncio.gather(*[self.feed(c) for c in cats], return_exceptions=True)
        pool: list[dict[str, Any]] = []
        for res in results:
            if isinstance(res, dict) and isinstance(res.get("items"), list):
                pool.extend(res["items"][:18])
        return pool

    async def _for_you(self, interests: list[str]) -> list[dict[str, Any]]:
        """The personalised feed, RANKED by the reading-taste profile (not blind keyword search).

        1. CANDIDATES — a broad pool: a Google-News search on the user's strongest taste signals
           (their research concepts blended with typed interests) PLUS fresh items from the curated
           categories (so there's always reputable world / tech breadth to rank).
        2. RANK — embed each candidate's title(+snippet) and score by cosine to the reading-weighted
           topic centroids (recsys :class:`Profile`), blended with recency, with a small reliability
           bonus for items whose source is in TRUSTED_SOURCES.
        3. REASON — attach a short "Because you follow X" derived from the matched taste signal.

        Degrades gracefully at every step: if the profile/embedder is unavailable OR there's no
        reading history, it falls back to the original keyword + recency ordering. Never raises."""
        # Build the taste model off the event loop (fastembed blocks). None ⇒ no usable taste model.
        profile = await asyncio.to_thread(self._build_profile)

        # Brand-new user: no typed interests AND no reading-taste profile ⇒ nothing to personalise
        # on. Return empty so feed() surfaces the "Build your For You feed" onboarding prompt rather
        # than a generic curated pool. (With reading history OR typed interests we proceed below.)
        if not interests and profile is None:
            return []

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _add(items: list[dict[str, Any]]) -> None:
            for it in items:
                key = (it.get("title") or "").lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                candidates.append(it)

        # Run the curated breadth pool CONCURRENTLY with the search fan-out (a real task, started
        # before we await the searches) so wall-clock is the slower of the two, not their sum.
        curated_task = asyncio.create_task(self._curated_pool())
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0 (Himmy)"}) as c:
                queries = await self._taste_queries(c, interests)
                search_jobs = [self._google_news(c, q) for q in queries[:8]]
                search_res = await asyncio.gather(*search_jobs, return_exceptions=True)
                for res in search_res:
                    if isinstance(res, list):
                        _add(res)
        except Exception:  # noqa: BLE001 - candidate gathering must never hard-fail the feed
            pass
        try:
            _add(await curated_task)
        except Exception:  # noqa: BLE001 - curated pool is a bonus; never let it sink the feed
            pass

        if not candidates:
            return []

        # ---- RANK ---------------------------------------------------------------------------
        # No taste model (cold start / no embedder) → keyword + recency fallback, with the
        # reliability bonus still applied so reputable sources lead.
        if profile is None:
            for it in candidates:
                it["reason"] = (f"Because you follow {it['topic']}"
                                if it.get("topic") and it["topic"] in interests
                                else ("Top story" if not it.get("topic") else ""))
                it["_rank"] = it.get("ts", 0.0) + (1e9 if _source_is_trusted(it) else 0.0)
            ranked = sorted(candidates, key=lambda x: x.get("_rank", 0.0), reverse=True)
            for it in ranked:
                it.pop("_rank", None)
            return ranked[:30]

        # Taste-ranked: cosine of title(+snippet) to the reading-weighted centroids, blended with
        # recency and a reliability bonus, with the matched topic surfaced as the reason.
        try:
            texts = [f"{it.get('title', '')}. {it.get('snippet', '')}".strip()[:600] for it in candidates]
            taste = await asyncio.to_thread(profile.score_texts, texts)
        except Exception:  # noqa: BLE001 - scoring hiccup → recency fallback below
            taste = []
        if not taste or len(taste) != len(candidates):
            ranked = sorted(candidates, key=lambda x: x.get("ts", 0.0), reverse=True)
            for it in ranked:
                it.setdefault("reason", "")
            return ranked[:30]

        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        for it, t in zip(candidates, taste):
            age_days = max(0.0, (now - (it.get("ts", 0.0) or 0.0)) / 86400.0) if it.get("ts") else 3.0
            recency = 0.5 ** (age_days / 2.0)  # ~2-day half-life: news is perishable
            reliability = 0.05 if _source_is_trusted(it) else 0.0
            it["score"] = float(t) + 0.18 * recency + reliability
            it["reason"] = (f"Because you follow {it['topic']}"
                            if it.get("topic") else "Matches what you read")
        ranked = sorted(candidates, key=lambda x: x.get("score", 0.0), reverse=True)
        for it in ranked:
            it.pop("score", None)
        return ranked[:30]

    # ---- public: a category feed, with a short cache ------------------------------------
    async def feed(self, category: str, force: bool = False) -> dict[str, Any]:
        cache = self._read(self._cache)
        entry = cache.get(category)
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        if not force and entry and (now - entry.get("at", 0)) < _CACHE_TTL:
            return {"ok": True, "category": category, "items": entry["items"], "fetched_at": entry.get("iso")}
        if category == "For You":
            interests = self.get_interests()
            items = await self._for_you(interests)
            # Only ask the user to pick interests when we have NOTHING to personalise on — no typed
            # interests AND the reading-taste pool produced nothing. With reading history but no
            # typed interests, the taste profile still drives a real feed, so we serve it.
            if not items and not interests:
                return {"ok": True, "category": category, "items": [], "needs_interests": True}
        else:
            items = await self._category(category)
        cache[category] = {
            "items": items, "at": now,
            "iso": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        }
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


async def refresh_all(svc: NewsService, *, force: bool = True) -> None:
    """Re-pull EVERY category once, best-effort. A dead feed / slow source / single failed
    category can NEVER stop the pass — each category is wrapped so the loop always completes.
    This is the single iteration the server's background refresher runs on an interval."""
    # Refresh "For You" LAST: it pulls a curated breadth pool from World/Technology/Business via the
    # cache-aware feed(). Refreshing those categories first means their caches are warm (<1s old)
    # when "For You" runs, so the pool hits cache and re-fetches none of those RSS endpoints.
    ordered = [c for c in CATEGORIES if c != "For You"] + (["For You"] if "For You" in CATEGORIES else [])
    for cat in ordered:
        try:
            await svc.feed(cat, force=force)
        except Exception:  # noqa: BLE001 - one bad category must not stop the pass
            pass


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


def _hl_id() -> str:
    return f"nhl_{int(time.time() * 1000):x}_{abs(hash(time.time())) % 100000:05d}"


class NewsAnnotations:
    """Per-article notes + text highlights for the news reader, keyed by the article URL and
    stored in library.db (so they ride backups). News articles aren't library items, so these
    are URL-keyed and work whether or not the article is also saved to a folder."""

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
                "CREATE TABLE IF NOT EXISTS news_notes (url TEXT PRIMARY KEY, note TEXT, updated REAL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS news_summaries (url TEXT PRIMARY KEY, summary TEXT, created REAL)"
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS news_highlights (
                    id TEXT PRIMARY KEY, url TEXT, text TEXT, color TEXT, note TEXT, created REAL
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_news_hl_url ON news_highlights(url)")

    def get(self, url: str) -> dict[str, Any]:
        """Everything annotated on one article: the cached AI summary + free-text note + highlights."""
        url = (url or "").strip()
        with self._conn() as c:
            nr = c.execute("SELECT note FROM news_notes WHERE url = ?", (url,)).fetchone()
            sr = c.execute("SELECT summary FROM news_summaries WHERE url = ?", (url,)).fetchone()
            hrs = c.execute(
                "SELECT id, text, color, note, created FROM news_highlights WHERE url = ? ORDER BY created",
                (url,),
            ).fetchall()
        return {"note": (nr["note"] if nr else ""), "summary": (sr["summary"] if sr else ""),
                "highlights": [dict(r) for r in hrs]}

    def set_summary(self, url: str, summary: str) -> dict[str, Any]:
        """Cache (or, with an empty string, clear) the AI summary for an article."""
        url = (url or "").strip()
        with self._conn() as c:
            if (summary or "").strip():
                c.execute(
                    "INSERT INTO news_summaries (url, summary, created) VALUES (?, ?, ?) "
                    "ON CONFLICT(url) DO UPDATE SET summary = excluded.summary, created = excluded.created",
                    (url, summary, time.time()),
                )
            else:
                c.execute("DELETE FROM news_summaries WHERE url = ?", (url,))
        return {"ok": True}

    def set_note(self, url: str, note: str) -> dict[str, Any]:
        url = (url or "").strip()
        with self._conn() as c:
            c.execute(
                "INSERT INTO news_notes (url, note, updated) VALUES (?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET note = excluded.note, updated = excluded.updated",
                (url, note or "", time.time()),
            )
        return {"ok": True}

    def add_highlight(self, url: str, text: str, color: str = "yellow", note: str = "") -> dict[str, Any]:
        url = (url or "").strip()
        text = (text or "").strip()
        if not text:
            return {"ok": False, "message": "Nothing to highlight."}
        hid = _hl_id()
        created = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO news_highlights (id, url, text, color, note, created) VALUES (?, ?, ?, ?, ?, ?)",
                (hid, url, text, color or "yellow", note or "", created),
            )
        return {"ok": True, "highlight": {"id": hid, "text": text, "color": color or "yellow",
                                          "note": note or "", "created": created}}

    def update_highlight(self, hid: str, *, note: str | None = None, color: str | None = None) -> dict[str, Any]:
        sets: list[str] = []
        vals: list[Any] = []
        if note is not None:
            sets.append("note = ?")
            vals.append(note)
        if color is not None:
            sets.append("color = ?")
            vals.append(color)
        if not sets:
            return {"ok": True}
        vals.append(hid)
        with self._conn() as c:
            c.execute(f"UPDATE news_highlights SET {', '.join(sets)} WHERE id = ?", vals)
        return {"ok": True}

    def remove_highlight(self, hid: str) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("DELETE FROM news_highlights WHERE id = ?", (hid,))
        return {"ok": True}


__all__ = [
    "NewsService", "SavedNews", "NewsAnnotations", "CATEGORIES", "SOURCES", "TRUSTED_SOURCES",
    "DEFAULT_FOLDER", "extract_article", "refresh_all",
]
