"""The recommendation engine — seed from what the user reads, retrieve across the literature.

Pipeline:
  1. SEEDS   — the user's top library papers, ranked by the recsys signal (reading time, then
               highlights/notes, decayed by recency). These are "what they actually read".
  2. EXPAND  — resolve each seed on OpenAlex to follow its ``related_works`` and pick up its
               research ``concepts``; ask Semantic Scholar's recommender what readers of those
               seeds also read; and run keyword search (OpenAlex + Crossref + arXiv) over the
               seeds' concepts and the user's typed interests. Every source is fault-tolerant.
  3. RANK    — drop anything already owned, score each candidate by the taste profile (cosine to
               the reading-weighted topic centroids) blended with recency + citations, hard-drop
               off-discipline papers, and record which research THREAD each one belongs to.
  4. DIGEST  — group the survivors into the reader's research threads (labelled from their
               concepts), pick a hero, and attach a specific reason ("Because you read X").

Serving is INSTANT: results are cached and served immediately; a stale cache is returned at once
while a fresh batch is computed in the background, and the backend keeps the cache warm on a timer.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import math
import os
import re
import time
from collections import Counter
from typing import Any

import httpx

from himmy_app.config import HimmyConfig, load_config
from himmy_app.recsys import sources
from himmy_app.recsys.sources import _norm_doi, _oa_short_id

_UA = "Himmy/0.1 (mailto:hello@himmy.app)"
_CACHE_TTL = 6 * 3600  # recommendations are cached for 6h, and auto-bust when the corpus changes

#: Data dirs with a background refresh in flight, so we never launch two at once.
_refreshing: set[str] = set()


def _title_key(title: str) -> str:
    """A loose title fingerprint for dedupe (lowercase alphanumeric words)."""
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").lower()))


def _shorten(text: str, n: int = 52) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


#: OpenAlex disambiguates concepts with a parenthetical discipline, e.g. "State (computer
#: science)" — a generic CS concept it wrongly pins on political-"state" papers. If that
#: discipline isn't one the reader works in, the concept is noise and makes a junk thread label.
_OTHER_DISCIPLINES = {
    "computer science", "physics", "biology", "chemistry", "medicine", "engineering",
    "mathematics", "geology", "materials science", "psychology", "astronomy",
}


def _concept_ok(name: str, user_fields_lc: set[str]) -> bool:
    m = re.search(r"\(([^)]+)\)", name or "")
    if m:
        qualifier = m.group(1).strip().lower()
        if qualifier in _OTHER_DISCIPLINES and qualifier not in user_fields_lc:
            return False
    return True


def _first_sentence(text: str, limit: int = 200) -> str:
    """Extractive fallback TLDR: the abstract's first sentence, trimmed."""
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"(.+?[.!?])\s", text[: limit + 60])
    return _shorten(m.group(1) if m else text, limit)


def _recency_year(year: str) -> float:
    try:
        y = int(str(year)[:4])
    except (TypeError, ValueError):
        return 0.5
    age = max(0, datetime.date.today().year - y)
    return 0.5 ** (age / 6.0)  # ~6-year half-life: recent papers favoured, classics not excluded


class Recommender:
    def __init__(self, config: HimmyConfig | None = None) -> None:
        self._cfg = config or load_config()
        self._cache_path = self._cfg.data_dir / "recs_cache.json"

    # ---- seeds: the user's most-engaged papers ------------------------------------------
    def _seed_papers(self, k: int = 8) -> list[dict[str, Any]]:
        from himmy_app.library import Library
        from himmy_app.reading import ReadingStore
        from himmy_app.recsys.profile import _read_bonus, _recency, _signal_weight, _to_epoch

        reading: dict[str, float] = {}
        last_read: dict[str, float] = {}
        try:
            store = ReadingStore(self._cfg)
            reading = store.totals_by_item()
            last_read = store.last_read_by_item()
        except Exception:  # noqa: BLE001
            pass

        scored: list[tuple[float, dict[str, Any]]] = []
        for r in Library(self._cfg).rag_records():
            iid = r.get("id")
            base = _signal_weight(r) + _read_bonus(reading.get(iid, 0.0) / 60.0)
            added = _to_epoch(r.get("added_at")) or 0.0
            ts = max(added, last_read.get(iid, 0.0))
            weight = base * _recency(ts if ts > 0 else r.get("added_at"))
            scored.append((weight, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"id": r["id"], "title": r.get("title") or "", "doi": _norm_doi(r.get("doi"))}
            for _, r in scored[:k]
        ]

    #: Aggregators / preprint servers make poor "follow this venue" sources (too broad / not a journal).
    _SKIP_VENUES = {
        "arxiv", "ssrn electronic journal", "ssrn", "repec: research papers in economics",
        "preprints.org", "social science research network", "research papers in economics",
    }

    def _top_authors_venues(self, seed_ids: set[str], n: int = 2) -> tuple[list[str], list[str]]:
        """The authors and journals the reader reads most — restricted to their dominant-cluster
        seed papers, so off-field outliers (e.g. the GPT-3 authors / a physics journal) don't leak
        in. To surface those authors'/journals' newest work."""
        from himmy_app.library import Library

        authors: Counter = Counter()
        venues: Counter = Counter()
        for r in Library(self._cfg).rag_records():
            if r.get("id") not in seed_ids:
                continue
            for a in (r.get("authors") or [])[:3]:
                a = (a or "").strip()
                if len(a) > 3:
                    authors[a] += 1
            v = (r.get("venue") or "").strip()
            if v and v.lower() not in self._SKIP_VENUES:
                venues[v] += 1
        return [a for a, _ in authors.most_common(n)], [v for v, _ in venues.most_common(n)]

    # ---- what to exclude (already have it) ----------------------------------------------
    def _known(self) -> tuple[set[str], set[str]]:
        from himmy_app.library import Library
        from himmy_app.news import SavedNews

        dois: set[str] = set()
        titles: set[str] = set()
        for r in Library(self._cfg).rag_records():
            d = _norm_doi(r.get("doi"))
            if d:
                dois.add(d)
            titles.add(_title_key(r.get("title") or ""))
        try:
            for r in SavedNews(self._cfg).rag_records():
                titles.add(_title_key(r.get("title") or ""))
        except Exception:  # noqa: BLE001
            pass
        try:
            from himmy_app.feedback import DismissalStore

            ds = DismissalStore(self._cfg)
            dois |= ds.dismissed_dois()           # never recommend a dismissed paper again
            titles |= ds.dismissed_title_keys()
        except Exception:  # noqa: BLE001
            pass
        return dois, titles

    # ---- cache --------------------------------------------------------------------------
    def _signature(self) -> list[Any]:
        """Cheap fingerprint so cached recs auto-refresh after the user reads / adds papers."""
        from himmy_app.library import Library
        from himmy_app.news import NewsService
        from himmy_app.reading import ReadingStore

        n = len(Library(self._cfg).rag_records())
        interests = list(NewsService(self._cfg).get_interests())
        mins = 0
        try:
            mins = round(sum(ReadingStore(self._cfg).totals_by_item().values()) / 60.0)
        except Exception:  # noqa: BLE001
            pass
        dismissed = 0
        try:
            from himmy_app.feedback import DismissalStore

            dismissed = DismissalStore(self._cfg).count()
        except Exception:  # noqa: BLE001
            pass
        return [n, interests, mins, dismissed]

    def _read_cache(self) -> dict[str, Any] | None:
        try:
            return json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def _write_cache(self, result: dict[str, Any]) -> None:
        try:
            self._cache_path.write_text(
                json.dumps({"at": time.time(), "sig": self._signature(), "result": result}),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    # ---- public entry: instant cache-first serving --------------------------------------
    async def recommend(self, *, limit: int = 24, force: bool = False) -> dict[str, Any]:
        """Return recommendations, fast. A fresh cache is served instantly; a stale one is served
        instantly too while a new batch computes in the background; only a cold cache blocks."""
        if force:
            return await self._compute_and_cache(limit=limit)

        cached = self._read_cache()
        if cached and cached.get("result"):
            # Always honour the latest dismissals, even against a cache computed before them.
            result = self._filter_dismissed(dict(cached["result"]))
            fresh = cached.get("sig") == self._signature() and (time.time() - cached.get("at", 0)) < _CACHE_TTL
            if fresh:
                result["cached"] = True
            else:
                result["stale"] = True          # serve now, refresh behind the scenes
                self._spawn_refresh(limit)
            return result
        # No cache yet (very first open) — compute once, synchronously.
        return await self._compute_and_cache(limit=limit)

    def _filter_dismissed(self, result: dict[str, Any]) -> dict[str, Any]:
        try:
            from himmy_app.feedback import DismissalStore

            ds = DismissalStore(self._cfg)
            dois, tkeys = ds.dismissed_dois(), ds.dismissed_title_keys()
        except Exception:  # noqa: BLE001
            return result
        if not dois and not tkeys:
            return result

        def keep(p: dict[str, Any]) -> bool:
            return not ((p.get("doi") and p["doi"] in dois) or _title_key(p.get("title", "")) in tkeys)

        result = dict(result)
        result["papers"] = [p for p in result.get("papers", []) if keep(p)]
        threads = []
        for t in result.get("threads", []):
            ps = [p for p in t.get("papers", []) if keep(p)]
            if ps:
                threads.append({**t, "papers": ps, "count": len(ps)})
        result["threads"] = threads
        hero = result.get("hero")
        if hero and not keep(hero):
            result["hero"] = threads[0]["papers"][0] if threads else (result["papers"][0] if result["papers"] else None)
        return result

    def _spawn_refresh(self, limit: int) -> None:
        key = str(self._cfg.data_dir)
        if key in _refreshing:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        _refreshing.add(key)

        async def _run() -> None:
            try:
                await self._compute_and_cache(limit=limit)
            except Exception:  # noqa: BLE001
                pass
            finally:
                _refreshing.discard(key)

        loop.create_task(_run())

    async def _compute_and_cache(self, *, limit: int) -> dict[str, Any]:
        result = await self._compute(limit=limit)
        self._write_cache(result)
        return result

    # ---- the engine ---------------------------------------------------------------------
    async def _compute(self, *, limit: int) -> dict[str, Any]:
        from himmy_app.news import NewsService

        seeds = self._seed_papers()
        interests = NewsService(self._cfg).get_interests()
        if not seeds and not interests:
            return {"ok": True, "papers": [], "threads": [], "hero": None}

        candidates, user_fields, user_concepts = await self._gather(seeds, interests)
        known_dois, known_titles = self._known()
        deduped = self._dedupe(candidates, known_dois, known_titles)
        ranked = await asyncio.to_thread(self._rank, deduped, interests, user_fields)

        threads = self._build_threads(ranked, user_concepts)
        papers = ranked[:limit]
        # Hero = the strongest paper in the strongest thread (always on-theme), else the top paper.
        hero = threads[0]["papers"][0] if threads else (papers[0] if papers else None)

        # Attach AI TLDR summaries to everything we'll show, in one batched request.
        displayed: dict[int, dict[str, Any]] = {id(p): p for p in papers}
        for t in threads:
            for p in t["papers"]:
                displayed[id(p)] = p
        if hero:
            displayed[id(hero)] = hero
        await self._enrich_tldrs(list(displayed.values()))

        # Strip private bookkeeping fields before returning / caching.
        for p in ranked:
            for k in ("_oa_id", "_via"):
                p.pop(k, None)
        return {"ok": True, "papers": papers, "threads": threads, "hero": hero, "fields": user_fields}

    async def _enrich_tldrs(self, papers: list[dict[str, Any]]) -> None:
        """Give every shown paper a one-line summary: Semantic Scholar's own AI TLDR where it has
        one (free), then the LLM for the rest, then the abstract's first sentence as a last resort."""
        dois = list({p["doi"] for p in papers if p.get("doi") and not p.get("tldr")})
        if dois:
            try:
                async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}) as client:
                    tldrs = await sources.semantic_scholar_tldrs(client, dois)
                for p in papers:
                    d = p.get("doi")
                    if d and tldrs.get(d) and not p.get("tldr"):
                        p["tldr"] = tldrs[d]
            except Exception:  # noqa: BLE001
                pass
        await self._ai_tldrs(papers)

    async def _ai_tldrs(self, papers: list[dict[str, Any]]) -> None:
        targets = [p for p in papers if not p.get("tldr") and (p.get("abstract") or p.get("title"))]
        if not targets:
            return
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:  # no model configured → extractive fallback
            for p in targets:
                if p.get("abstract"):
                    p["tldr"] = _first_sentence(p["abstract"])
            return
        targets = targets[:30]
        model = os.environ.get("HIMMY_APP_MODEL", "google/gemini-2.5-flash")
        blocks = [
            f'{i}. TITLE: {p.get("title", "")}\n   ABSTRACT: {(p.get("abstract") or "")[:600]}'
            for i, p in enumerate(targets)
        ]
        prompt = (
            "For each numbered paper below, write a ONE-sentence, plain-English TLDR (max 22 words) "
            "of its core finding or contribution — concrete, no fluff, don't start with 'This paper'. "
            'Reply with ONLY a JSON object mapping the number as a string to the sentence, e.g. '
            '{"0": "...", "1": "..."}.\n\n' + "\n\n".join(blocks)
        )
        data: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]},
                )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", content, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
        except Exception:  # noqa: BLE001
            data = {}
        for i, p in enumerate(targets):
            t = data.get(str(i)) or data.get(i)
            if t:
                p["tldr"] = str(t).strip()
            elif p.get("abstract"):
                p["tldr"] = _first_sentence(p["abstract"])

    async def _gather(self, seeds: list[dict[str, Any]], interests: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": _UA}) as client:
            # 1. Resolve seeds on OpenAlex (concurrently): disciplines, related works, concepts.
            resolved = await asyncio.gather(
                *[sources.openalex_resolve(client, doi=s["doi"], title=s["title"]) for s in seeds],
                return_exceptions=True,
            )
            per_seed: list[dict[str, Any]] = []
            for s, w in zip(seeds, resolved):
                if not isinstance(w, dict):
                    continue
                fields, c_ids, c_names = [], [], []
                for c in (w.get("concepts") or []):
                    score, level = (c.get("score") or 0), (c.get("level") or 0)
                    if level == 0 and score >= 0.25:
                        fields.append(c.get("display_name", ""))
                    if score >= 0.4 and 1 <= level <= 3:
                        c_ids.append(_oa_short_id(c.get("id", "")))
                        c_names.append(c.get("display_name", ""))
                per_seed.append({
                    "id": s["id"], "title": s["title"], "doi": s["doi"], "fields": fields,
                    "concept_ids": c_ids, "concept_names": c_names,
                    "related": [_oa_short_id(rid) for rid in (w.get("related_works") or [])[:8]],
                })

            # 2. Keep only seeds in the DOMINANT discipline cluster (so a couple of off-field
            #    outliers — e.g. an econ reader who saved the GPT-3 paper — can't hijack results).
            field_seedcount = Counter(f for ps in per_seed for f in set(ps["fields"]))
            kept = per_seed
            if field_seedcount:
                top_n = max(field_seedcount.values())
                core = {f for f, n in field_seedcount.items() if n >= 0.6 * top_n}
                kept = [ps for ps in per_seed if (set(ps["fields"]) & core)] or per_seed

            user_fields = sorted({f for ps in kept for f in ps["fields"] if f})
            kept_ids = {ps["id"] for ps in kept}
            seed_dois = [ps["doi"] for ps in kept if ps["doi"]]
            related_ids = [r for ps in kept for r in ps["related"]]
            # Map each related work back to the seed it came from, to name it in the reason line.
            related_seed: dict[str, str] = {}
            for ps in kept:
                for rid in ps["related"]:
                    related_seed.setdefault(rid, ps["title"])
            top_concept_ids = [cid for cid, _ in Counter(c for ps in kept for c in ps["concept_ids"]).most_common(4)]
            concept_counter = Counter(n for ps in kept for n in ps["concept_names"])
            top_concept_names = [n for n, _ in concept_counter.most_common(4)]
            # The reader's own research concepts (clean OpenAlex labels) become the digest threads.
            # Drop concepts qualified by an outside discipline (noise), and PREFER ones that recur
            # across ≥2 seed papers (a real, recurring research theme — not a one-off tag).
            uf_lc = {f.lower() for f in user_fields}
            clean = [(n, c) for n, c in concept_counter.most_common(30) if n and _concept_ok(n, uf_lc)]
            recurring = [n for n, c in clean if c >= 2]
            user_concepts = (recurring if len(recurring) >= 3 else [n for n, _ in clean])[:12]
            queries: list[str] = list(dict.fromkeys([*interests, *top_concept_names]))[:6]

            # 3. Fan out every source at once. Each job carries a reason string for its results.
            top_authors, top_venues = self._top_authors_venues(kept_ids)
            jobs: list[tuple[str, Any, str]] = [
                ("related", sources.openalex_by_ids(client, related_ids), ""),
                ("recommend", sources.semantic_scholar_recommend(client, seed_dois), "Readers of your papers also read this"),
                ("concept", sources.openalex_by_concepts(client, top_concept_ids), ""),
                ("arxiv", sources.arxiv_search(client, interests or top_concept_names), ""),
            ]
            for q in queries[:5]:
                jobs.append(("query", sources.openalex_search(client, q), ""))
                jobs.append(("query", sources.crossref_search(client, q), ""))
            for a in top_authors:
                jobs.append(("author", sources.openalex_by_author(client, a), f"New from {a}"))
            for v in top_venues:
                jobs.append(("venue", sources.openalex_by_venue(client, v), f"Latest in {_shorten(v, 40)}"))

            results = await asyncio.gather(*[coro for _, coro, _ in jobs], return_exceptions=True)

        candidates: list[dict[str, Any]] = []
        for (label, _, why_text), res in zip(jobs, results):
            if not isinstance(res, list):
                continue
            for c in res:
                c["_via"] = label
                if label == "related" and (seed_t := related_seed.get(c.get("_oa_id", ""))):
                    c["why"] = f"Because you read “{_shorten(seed_t)}”"
                elif why_text:
                    c["why"] = why_text
            candidates.extend(res)
        return candidates, user_fields, user_concepts

    def _dedupe(self, cands: list[dict[str, Any]], known_dois: set[str], known_titles: set[str]) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for c in cands:
            if not c.get("title"):
                continue
            doi = c.get("doi") or ""
            tkey = _title_key(c["title"])
            if (doi and doi in known_dois) or tkey in known_titles:
                continue  # already in the user's library / saved
            key = doi or c.get("arxiv") or tkey
            if key in by_key:  # same paper from two sources → enrich the one we keep
                ex = by_key[key]
                if not ex.get("abstract") and c.get("abstract"):
                    ex["abstract"] = c["abstract"]
                ex["citations"] = max(ex.get("citations", 0), c.get("citations", 0))
                if not ex.get("concepts") and c.get("concepts"):
                    ex["concepts"] = c["concepts"]
                if not ex.get("fields") and c.get("fields"):
                    ex["fields"] = c["fields"]
                if not ex.get("why") and c.get("why"):
                    ex["why"] = c["why"]
            else:
                by_key[key] = c
                order.append(key)
        return [by_key[k] for k in order]

    def _rank(self, cands: list[dict[str, Any]], interests: list[str], user_fields: list[str]) -> list[dict[str, Any]]:
        from himmy_app.recsys.profile import build_profile

        if not cands:
            return []
        uf = {f.lower() for f in user_fields}
        # HARD domain filter: drop any candidate whose field is entirely outside the reader's
        # discipline(s). No-field and unknown-fields papers pass and are judged on taste.
        if uf:
            cands = [c for c in cands if not (cf := {f.lower() for f in c.get("fields", []) if f}) or (cf & uf)]
            if not cands:
                return []

        # "Not interested" learning: down-weight concepts the reader has dismissed.
        dismissed_concepts: Counter = Counter()
        try:
            from himmy_app.feedback import DismissalStore

            dismissed_concepts = DismissalStore(self._cfg).concept_counts()
        except Exception:  # noqa: BLE001
            pass

        prof = build_profile(self._cfg)
        texts = [f"{c['title']}. {c.get('abstract', '')}"[:1200] for c in cands]
        scores = prof.score_texts(texts) if prof.num_topics else [0.0] * len(cands)
        max_cit = max((c.get("citations", 0) for c in cands), default=0) or 1

        out: list[dict[str, Any]] = []
        for c, taste in zip(cands, scores):
            recency = _recency_year(c.get("year", ""))
            citation = math.log1p(c.get("citations", 0)) / math.log1p(max_cit)
            graph_bonus = 0.04 if c.get("_via") in ("related", "recommend") else 0.0
            domain = 0.12 if (uf and {f.lower() for f in c.get("fields", []) if f} & uf) else 0.0
            penalty = 0.0
            if dismissed_concepts:
                hits = sum(dismissed_concepts.get(pc.lower(), 0) for pc in c.get("concepts", []))
                penalty = min(0.45, 0.08 * hits)  # each dismissal nudges its concepts down; capped
            # Citations are a WEAK signal on purpose — a 25k-cite landmark shouldn't outrank the
            # on-topic working paper the reader actually wants.
            c["score"] = float(taste) + domain + 0.10 * recency + 0.02 * citation + graph_bonus - penalty
            c["why"] = c.get("why") or self._why(c)
            out.append(c)
        out.sort(key=lambda x: x["score"], reverse=True)
        return out

    def _build_threads(self, ranked: list[dict[str, Any]], user_concepts: list[str], *, per_thread: int = 8, max_threads: int = 6, min_size: int = 3) -> list[dict[str, Any]]:
        """Group ranked candidates into the reader's OWN research concepts (clean OpenAlex labels
        derived from their seed papers). Each paper joins the highest-ranked concept it carries, so
        the threads are real research areas with good names — and papers matching none are excluded
        from the digest (that's what filters out the off-theme stragglers)."""
        if not user_concepts:
            return []
        concept_lc = [(c, c.lower()) for c in user_concepts]
        groups: dict[str, list[dict[str, Any]]] = {c: [] for c in user_concepts}
        for p in ranked:
            paper_concepts = {pc.lower() for pc in (p.get("concepts") or [])}
            match = next((orig for orig, lc in concept_lc if lc in paper_concepts), None)
            if match:
                groups[match].append(p)

        threads: list[dict[str, Any]] = []
        for concept in user_concepts:  # user-concept rank order
            ps = groups[concept]
            if len(ps) >= min_size:
                threads.append({"label": concept, "papers": ps[:per_thread], "count": len(ps),
                                "_score": ps[0].get("score", 0.0)})
        threads.sort(key=lambda t: t["_score"], reverse=True)
        for t in threads:
            t.pop("_score", None)
        return threads[:max_threads]

    def _why(self, c: dict[str, Any]) -> str:
        via = c.get("_via")
        if via in ("related", "recommend"):
            return "Related to papers you've read"
        if c.get("concepts"):
            return f"In your work on {c['concepts'][0]}"
        return "Matches your topics"


__all__ = ["Recommender"]
