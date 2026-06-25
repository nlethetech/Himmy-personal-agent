"""Scholarly source adapters — retrieve candidate papers the way a research assistant would.

The point of this module is COVERAGE across disciplines. arXiv (the old recommender's only
source) is STEM-dominated, so a reader of economics / political economy / political theory gets
nothing relevant. These adapters instead hit databases that index *all* of scholarship and follow
the literature graph from the papers the user actually reads:

  * OpenAlex      — ~250M works, every field; per-work ``related_works`` + research ``concepts``.
  * Semantic Scholar — its recommendations API ("readers of these papers also read…").
  * Crossref      — every DOI, every journal; broad keyword search.
  * arXiv         — kept as ONE minor source (the quantitative-econ / CS slice).

Every adapter is async, shares one HTTP client, has its own timeout, and SWALLOWS its errors to
return ``[]`` — so one slow or rate-limited source never sinks the whole recommendation. All are
free and need no API key (OpenAlex/Crossref get a ``mailto`` for the polite pool).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

_MAILTO = "hello@himmy.app"
_UA = "Himmy/0.1 (mailto:hello@himmy.app)"
#: A soft recency floor. Theory papers stay relevant for years, so this is generous (not "this
#: year only") — it just keeps decades-old keyword hits from crowding out current scholarship.
_RECENT_FROM = "2017-01-01"


# ---- normalised candidate -------------------------------------------------------------------
def _candidate(
    *, title: str, abstract: str = "", authors: list[str] | None = None, year: str = "",
    venue: str = "", doi: str = "", arxiv: str = "", url: str = "", citations: int = 0,
    source: str = "", concepts: list[str] | None = None, fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "title": title.strip(), "abstract": (abstract or "").strip(),
        "authors": [a for a in (authors or []) if a], "year": str(year or ""),
        "venue": (venue or "").strip(), "doi": _norm_doi(doi), "arxiv": (arxiv or "").strip(),
        "url": url or "", "citations": int(citations or 0), "source": source,
        "concepts": concepts or [],
        # top-level discipline labels ("Economics", "Computer Science", …) used to keep the
        # recommendations inside the reader's actual field(s) and demote off-domain papers.
        "fields": [f for f in (fields or []) if f],
    }


def _norm_doi(doi: str | None) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    d = d.replace("https://doi.org/", "").replace("http://doi.org/", "").replace("doi:", "")
    return d.strip()


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


# ---- OpenAlex -------------------------------------------------------------------------------
_OA_BASE = "https://api.openalex.org/works"


def _oa_short_id(work_id: str) -> str:
    """``https://openalex.org/W123`` → ``W123`` (the short form filters use)."""
    return (work_id or "").rstrip("/").split("/")[-1]


def _abstract_from_inverted(inv: dict[str, list[int]] | None) -> str:
    """OpenAlex stores abstracts as an inverted index {word: [positions]}; rebuild the prose."""
    if not inv:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)[:1500]


def _oa_to_candidate(w: dict[str, Any]) -> dict[str, Any] | None:
    title = w.get("title") or w.get("display_name") or ""
    if not title:
        return None
    authors = [(a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])]
    venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    doi = _norm_doi(w.get("doi"))
    all_concepts = w.get("concepts") or []
    concepts = [
        c.get("display_name", "")
        for c in all_concepts
        if (c.get("score") or 0) >= 0.3 and 1 <= (c.get("level") or 0) <= 3
    ]
    # Level-0 concepts ARE OpenAlex's top-level disciplines ("Economics", "Computer science", …).
    fields = [c.get("display_name", "") for c in all_concepts if (c.get("level") == 0) and (c.get("score") or 0) >= 0.2]
    cand = _candidate(
        title=title, abstract=_abstract_from_inverted(w.get("abstract_inverted_index")),
        authors=authors, year=w.get("publication_year") or "", venue=venue, doi=doi,
        url=(f"https://doi.org/{doi}" if doi else (w.get("id") or "")),
        citations=w.get("cited_by_count") or 0, source="openalex", concepts=concepts[:6], fields=fields,
    )
    cand["_oa_id"] = _oa_short_id(w.get("id", ""))  # so a related candidate can name its seed paper
    return cand


async def openalex_resolve(client: httpx.AsyncClient, *, doi: str = "", title: str = "") -> dict[str, Any] | None:
    """Fetch one OpenAlex work (with its ``related_works`` + ``concepts``) for a seed paper."""
    try:
        if doi:
            r = await client.get(f"{_OA_BASE}/doi:{_norm_doi(doi)}", params={"mailto": _MAILTO})
            if r.status_code == 200:
                return r.json()
        if title:
            r = await client.get(_OA_BASE, params={"search": title, "per_page": 1, "mailto": _MAILTO})
            if r.status_code == 200:
                items = r.json().get("results") or []
                return items[0] if items else None
    except Exception:  # noqa: BLE001
        return None
    return None


async def openalex_by_ids(client: httpx.AsyncClient, short_ids: list[str]) -> list[dict[str, Any]]:
    """Batch-fetch metadata for OpenAlex works by id (used for a seed's related works)."""
    out: list[dict[str, Any]] = []
    for i in range(0, len(short_ids), 50):  # OpenAlex allows up to 50 ids per OR-filter
        chunk = [s for s in short_ids[i : i + 50] if s]
        if not chunk:
            continue
        try:
            r = await client.get(_OA_BASE, params={
                "filter": f"ids.openalex:{'|'.join(chunk)}", "per_page": 50, "mailto": _MAILTO,
            })
            if r.status_code == 200:
                for w in r.json().get("results") or []:
                    c = _oa_to_candidate(w)
                    if c:
                        out.append(c)
        except Exception:  # noqa: BLE001
            continue
    return out


async def openalex_search(client: httpx.AsyncClient, query: str, *, per_page: int = 12) -> list[dict[str, Any]]:
    try:
        r = await client.get(_OA_BASE, params={
            "search": query, "filter": f"from_publication_date:{_RECENT_FROM}",
            "sort": "relevance_score:desc", "per_page": per_page, "mailto": _MAILTO,
        })
        if r.status_code != 200:
            return []
        return [c for w in (r.json().get("results") or []) if (c := _oa_to_candidate(w))]
    except Exception:  # noqa: BLE001
        return []


async def openalex_by_concepts(client: httpx.AsyncClient, concept_ids: list[str], *, per_page: int = 12) -> list[dict[str, Any]]:
    """Recent, well-cited works in the user's own research concepts (from their seed papers)."""
    if not concept_ids:
        return []
    try:
        r = await client.get(_OA_BASE, params={
            "filter": f"concepts.id:{'|'.join(concept_ids[:4])},from_publication_date:{_RECENT_FROM}",
            "sort": "cited_by_count:desc", "per_page": per_page, "mailto": _MAILTO,
        })
        if r.status_code != 200:
            return []
        return [c for w in (r.json().get("results") or []) if (c := _oa_to_candidate(w))]
    except Exception:  # noqa: BLE001
        return []


# ---- Semantic Scholar (recommendations) -----------------------------------------------------
_S2_REC = "https://api.semanticscholar.org/recommendations/v1/papers"


async def semantic_scholar_recommend(client: httpx.AsyncClient, positive_dois: list[str]) -> list[dict[str, Any]]:
    """"Readers of these papers also read…" — S2's recommender, seeded by the user's DOIs."""
    seeds = [f"DOI:{_norm_doi(d)}" for d in positive_dois if d][:10]
    if not seeds:
        return []
    try:
        r = await client.post(
            _S2_REC,
            params={
                "fields": "title,abstract,year,authors,venue,externalIds,citationCount,fieldsOfStudy",
                "limit": 30,
            },
            json={"positivePaperIds": seeds},
        )
        if r.status_code != 200:  # 429 (rate-limited) or 404 (unknown seeds) → just yield nothing
            return []
        papers = r.json().get("recommendedPapers") or []
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for p in papers:
        ext = p.get("externalIds") or {}
        out.append(_candidate(
            title=p.get("title") or "", abstract=p.get("abstract") or "",
            authors=[a.get("name", "") for a in (p.get("authors") or [])],
            year=p.get("year") or "", venue=p.get("venue") or "",
            doi=ext.get("DOI") or "", arxiv=ext.get("ArXiv") or "",
            url=(f"https://doi.org/{_norm_doi(ext.get('DOI'))}" if ext.get("DOI") else ""),
            citations=p.get("citationCount") or 0, source="semanticscholar",
            fields=p.get("fieldsOfStudy") or [],
        ))
    return out


# ---- Semantic Scholar (TLDR summaries, batched by DOI) --------------------------------------
_S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"


async def semantic_scholar_tldrs(client: httpx.AsyncClient, dois: list[str]) -> dict[str, str]:
    """``{doi: one-line AI TLDR}`` for the papers S2 knows — one batched request for all of them."""
    ids = [f"DOI:{_norm_doi(d)}" for d in dois if d][:400]
    if not ids:
        return {}
    try:
        r = await client.post(_S2_BATCH, params={"fields": "externalIds,tldr"}, json={"ids": ids})
        if r.status_code != 200:
            return {}
        data = r.json()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for obj in data or []:
        if not obj:
            continue
        text = (obj.get("tldr") or {}).get("text")
        doi = _norm_doi((obj.get("externalIds") or {}).get("DOI"))
        if doi and text:
            out[doi] = text.strip()
    return out


# ---- OpenAlex: follow the authors & venues the reader reads ----------------------------------
async def openalex_by_author(client: httpx.AsyncClient, name: str, *, per_page: int = 8) -> list[dict[str, Any]]:
    """Recent works by an author the reader follows (resolve the author, then their newest works)."""
    if not name:
        return []
    try:
        ar = await client.get("https://api.openalex.org/authors", params={"search": name, "per_page": 1, "mailto": _MAILTO})
        if ar.status_code != 200 or not (ar.json().get("results") or []):
            return []
        aid = _oa_short_id(ar.json()["results"][0].get("id", ""))
        wr = await client.get(_OA_BASE, params={
            "filter": f"authorships.author.id:{aid},from_publication_date:{_RECENT_FROM}",
            "sort": "publication_date:desc", "per_page": per_page, "mailto": _MAILTO,
        })
        if wr.status_code != 200:
            return []
        return [c for w in (wr.json().get("results") or []) if (c := _oa_to_candidate(w))]
    except Exception:  # noqa: BLE001
        return []


async def openalex_by_venue(client: httpx.AsyncClient, venue: str, *, per_page: int = 8) -> list[dict[str, Any]]:
    """Recent works in a journal the reader reads (resolve the source, then its newest works)."""
    if not venue:
        return []
    try:
        sr = await client.get("https://api.openalex.org/sources", params={"search": venue, "per_page": 1, "mailto": _MAILTO})
        if sr.status_code != 200 or not (sr.json().get("results") or []):
            return []
        sid = _oa_short_id(sr.json()["results"][0].get("id", ""))
        wr = await client.get(_OA_BASE, params={
            "filter": f"primary_location.source.id:{sid},from_publication_date:{_RECENT_FROM}",
            "sort": "publication_date:desc", "per_page": per_page, "mailto": _MAILTO,
        })
        if wr.status_code != 200:
            return []
        return [c for w in (wr.json().get("results") or []) if (c := _oa_to_candidate(w))]
    except Exception:  # noqa: BLE001
        return []


# ---- Crossref -------------------------------------------------------------------------------
async def crossref_search(client: httpx.AsyncClient, query: str, *, rows: int = 12) -> list[dict[str, Any]]:
    try:
        r = await client.get("https://api.crossref.org/works", params={
            "query": query, "rows": rows, "filter": f"from-pub-date:{_RECENT_FROM}",
            "sort": "relevance", "order": "desc",
        }, headers={"User-Agent": _UA})
        if r.status_code != 200:
            return []
        items = r.json().get("message", {}).get("items", [])
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for m in items:
        title = (m.get("title") or [""])[0]
        if not title:
            continue
        dp = (m.get("issued") or {}).get("date-parts") or [[None]]
        year = str(dp[0][0]) if dp and dp[0] and dp[0][0] else ""
        authors = [
            " ".join(p for p in [a.get("given", ""), a.get("family", "")] if p).strip()
            for a in m.get("author", [])
        ]
        doi = _norm_doi(m.get("DOI"))
        out.append(_candidate(
            title=_clean(title), abstract=_clean(m.get("abstract") or ""), authors=authors,
            year=year, venue=(m.get("container-title") or [""])[0], doi=doi,
            url=(f"https://doi.org/{doi}" if doi else (m.get("URL") or "")),
            citations=m.get("is-referenced-by-count") or 0, source="crossref",
        ))
    return out


# ---- arXiv (one minor source) ---------------------------------------------------------------
_ARXIV_FIELD = {
    "cs": "Computer Science", "econ": "Economics", "q-fin": "Economics", "stat": "Mathematics",
    "math": "Mathematics", "physics": "Physics", "cond-mat": "Physics", "astro-ph": "Physics",
    "hep": "Physics", "quant-ph": "Physics", "gr-qc": "Physics", "eess": "Engineering",
    "q-bio": "Biology",
}


def _arxiv_field(category: str) -> str:
    prefix = (category or "").split(".")[0].lower()
    return _ARXIV_FIELD.get(prefix, "")


async def arxiv_search(client: httpx.AsyncClient, terms: list[str], *, limit: int = 12) -> list[dict[str, Any]]:
    if not terms:
        return []
    q = " OR ".join(f'all:"{t}"' for t in terms[:5])
    try:
        r = await client.get("https://export.arxiv.org/api/query", params={
            "search_query": q, "sortBy": "relevance", "max_results": limit,
        })
        if r.status_code != 200:
            return []
        xml = r.text
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for e in re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL):
        def tag(n: str) -> str:
            m = re.search(rf"<{n}>(.*?)</{n}>", e, re.DOTALL)
            return _clean(m.group(1)) if m else ""
        aid = ""
        m = re.search(r"<id>https?://arxiv\.org/abs/([^<]+)</id>", e)
        if m:
            aid = m.group(1).split("v")[0]
        cat_m = re.search(r'<(?:arxiv:primary_category|category)[^>]*term="([^"]+)"', e)
        field = _arxiv_field(cat_m.group(1)) if cat_m else ""
        out.append(_candidate(
            title=tag("title"), abstract=tag("summary")[:900],
            authors=[_clean(a) for a in re.findall(r"<author>\s*<name>(.*?)</name>", e, re.DOTALL)],
            year=tag("published")[:4], venue="arXiv", arxiv=aid,
            url=f"https://arxiv.org/abs/{aid}" if aid else "", source="arxiv",
            fields=[field] if field else [],
        ))
    return out


__all__ = [
    "openalex_resolve", "openalex_by_ids", "openalex_search", "openalex_by_concepts",
    "openalex_by_author", "openalex_by_venue", "semantic_scholar_recommend",
    "semantic_scholar_tldrs", "crossref_search", "arxiv_search", "_oa_short_id",
]
