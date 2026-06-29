"""News-overhaul backend tests — NO network.

Everything that would touch the internet (httpx, the RSS/Google-News fetch layer, the recsys
recommender) is monkeypatched, and a per-test tmp data dir keeps the real ``.scholar-desk``
store untouched. We test the pure structure (SOURCES / CATEGORIES / TRUSTED_SOURCES), the
caching contract on ``feed()``, the taste-ranked "For You" feed (both the ranked path and the
graceful fallback), the background ``refresh_all`` helper, and the FastAPI endpoints.
"""

from __future__ import annotations

import asyncio

import pytest

from himmy_app import news as news_mod
from himmy_app.config import load_config
from himmy_app.news import (
    CATEGORIES,
    SOURCES,
    TRUSTED_SOURCES,
    NewsService,
    refresh_all,
)


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data"))
    return load_config()


# ---- SOURCES / CATEGORIES structure ----------------------------------------------------------
def test_sources_every_entry_has_name_and_https_url():
    assert SOURCES, "SOURCES must not be empty"
    for cat, feeds in SOURCES.items():
        assert feeds, f"category {cat!r} has no feeds"
        for entry in feeds:
            name, url = entry
            assert isinstance(name, str) and name.strip(), f"empty name in {cat}: {entry!r}"
            assert isinstance(url, str) and url, f"empty url in {cat}: {entry!r}"
            # All sources are https except CNN, whose only working RSS endpoint is plain http
            # (its https endpoint fails TLS) — documented in news.py.
            if "rss.cnn.com" in url:
                assert url.startswith("http://"), f"CNN url changed: {url!r}"
            else:
                assert url.startswith("https://"), f"non-https url in {cat}: {url!r}"


def test_categories_nonempty_and_for_you_leads():
    assert CATEGORIES, "CATEGORIES must not be empty"
    assert CATEGORIES[0] == "For You"
    # Every non-"For You" category should have a curated feed list backing it.
    for cat in CATEGORIES:
        if cat == "For You":
            continue
        assert cat in SOURCES, f"{cat!r} in CATEGORIES has no SOURCES entry"


# ---- TRUSTED_SOURCES + the trust filter ------------------------------------------------------
def test_trusted_sources_contains_curated_domains_and_excludes_junk():
    # A sampling of the curated/reputable domains that must be trusted.
    for good in ("kathmandupost.com", "bbc.co.uk", "theguardian.com", "arstechnica.com"):
        assert good in TRUSTED_SOURCES, f"{good} should be curated/trusted"
    # An obvious SEO-farm / junk domain must NOT be trusted.
    assert "totally-fake-clickbait-farm.example" not in TRUSTED_SOURCES


def test_is_trusted_keeps_trusted_drops_untrusted():
    items = [
        {"title": "Real story", "url": "https://www.bbc.co.uk/news/123", "source": "BBC"},
        {"title": "Sub story", "url": "https://news.ycombinator.com/item?id=1", "source": "HN"},
        {"title": "Junk", "url": "https://totally-fake-clickbait-farm.example/x", "source": "Farm"},
    ]
    kept = [it for it in items if news_mod._is_trusted(news_mod._domain(it["url"]))]
    titles = {it["title"] for it in kept}
    assert "Real story" in titles          # exact host
    assert "Sub story" in titles           # subdomain of a trusted host
    assert "Junk" not in titles            # untrusted → dropped


# ---- feed() caching --------------------------------------------------------------------------
def test_feed_serves_cache_within_ttl_and_refetches_on_force(cfg, monkeypatch):
    svc = NewsService(cfg)
    calls = {"n": 0}

    async def fake_category(self, cat):
        calls["n"] += 1
        return [{"title": f"Story {calls['n']}", "url": "https://bbc.co.uk/x", "ts": 0.0}]

    monkeypatch.setattr(NewsService, "_category", fake_category)

    first = asyncio.run(svc.feed("World"))
    assert first["ok"] and first["items"][0]["title"] == "Story 1"
    assert calls["n"] == 1

    # Second call within TTL → served from cache, NO refetch.
    cached = asyncio.run(svc.feed("World"))
    assert cached["items"][0]["title"] == "Story 1"
    assert calls["n"] == 1

    # force=True → bypass the cache and refetch.
    forced = asyncio.run(svc.feed("World", force=True))
    assert forced["items"][0]["title"] == "Story 2"
    assert calls["n"] == 2


# ---- taste-ranked "For You" ------------------------------------------------------------------
class _FakeProfile:
    """A stub taste model. ``score_texts`` scores by a keyword so we can assert RANKING."""

    num_topics = 2

    def score_texts(self, texts):
        # Higher score for titles mentioning "economics" — the user's pretend taste.
        return [0.9 if "economics" in t.lower() else 0.1 for t in texts]


def _stub_candidates(svc, monkeypatch, *, profile):
    """Wire the candidate-gathering + taste model so _for_you runs network-free."""
    async def fake_queries(self, client, interests):
        return list(interests) or ["economics"]

    async def fake_google(self, client, query, *, limit=8):
        return [
            {"title": "Markets and economics today", "url": "https://cnbc.com/a",
             "source": "CNBC", "snippet": "", "ts": 2.0, "topic": query},
            {"title": "A cat video went viral", "url": "https://clickbait.example/b",
             "source": "Clickbait", "snippet": "", "ts": 1.0, "topic": query},
        ]

    async def fake_pool(self):
        return [
            {"title": "Deep dive on economics policy", "url": "https://ft.com/c",
             "source": "FT", "snippet": "", "ts": 3.0},
        ]

    monkeypatch.setattr(NewsService, "_taste_queries", fake_queries)
    monkeypatch.setattr(NewsService, "_google_news", fake_google)
    monkeypatch.setattr(NewsService, "_curated_pool", fake_pool)
    monkeypatch.setattr(NewsService, "_build_profile", lambda self: profile)


def test_for_you_taste_ranked_with_reason(cfg, monkeypatch):
    svc = NewsService(cfg)
    _stub_candidates(svc, monkeypatch, profile=_FakeProfile())

    items = asyncio.run(svc._for_you(["economics"]))
    assert items, "expected ranked For You items"
    # The economics items must outrank the cat video (taste score dominates).
    assert "economics" in items[0]["title"].lower()
    assert "cat video" not in items[0]["title"].lower()
    # Every item carries a reason; taste-ranked path attaches a non-empty one.
    assert all("reason" in it for it in items)
    assert any(it["reason"] for it in items)


def test_for_you_fallback_no_profile_never_raises(cfg, monkeypatch):
    svc = NewsService(cfg)
    # No embedder / no reading history → _build_profile returns None (cold start).
    _stub_candidates(svc, monkeypatch, profile=None)

    items = asyncio.run(svc._for_you(["economics"]))
    assert items, "fallback should still return items"
    assert all("reason" in it for it in items)
    # Trusted sources (cnbc/ft) should lead the recency+reliability fallback over the junk host.
    assert "clickbait" not in (items[0].get("url") or "")


def test_for_you_empty_candidates_returns_empty(cfg, monkeypatch):
    svc = NewsService(cfg)

    async def no_queries(self, client, interests):
        return []

    async def no_google(self, client, query, *, limit=8):
        return []

    async def no_pool(self):
        return []

    monkeypatch.setattr(NewsService, "_taste_queries", no_queries)
    monkeypatch.setattr(NewsService, "_google_news", no_google)
    monkeypatch.setattr(NewsService, "_curated_pool", no_pool)
    monkeypatch.setattr(NewsService, "_build_profile", lambda self: None)

    items = asyncio.run(svc._for_you([]))
    assert items == []


# ---- background refresher --------------------------------------------------------------------
def test_refresh_all_refreshes_every_category(cfg, monkeypatch):
    svc = NewsService(cfg)
    seen: list[str] = []

    async def fake_feed(self, category, force=False):
        seen.append(category)
        assert force is True  # refresh_all forces a fresh pull
        return {"ok": True, "category": category, "items": []}

    monkeypatch.setattr(NewsService, "feed", fake_feed)
    # The corpus-embedding step does real network + embedding — no-op it; this test is the feed loop.
    import himmy_app.news as news_mod

    async def _noop_ingest(*_a, **_k):
        return {"ok": True, "ingested": 0}
    monkeypatch.setattr(news_mod, "ingest_corpus", _noop_ingest)
    asyncio.run(refresh_all(svc, force=True))
    # One iteration touches EVERY category. "For You" is refreshed LAST by design (it pulls a
    # breadth pool from the other categories, so refreshing them first leaves their caches warm).
    expected = [c for c in CATEGORIES if c != "For You"] + (["For You"] if "For You" in CATEGORIES else [])
    assert seen == expected


def test_refresh_all_one_failing_category_does_not_stop_the_loop(cfg, monkeypatch):
    svc = NewsService(cfg)
    seen: list[str] = []

    async def fake_feed(self, category, force=False):
        seen.append(category)
        if category == "World":
            raise RuntimeError("feed is down")
        return {"ok": True, "category": category, "items": []}

    monkeypatch.setattr(NewsService, "feed", fake_feed)
    import himmy_app.news as news_mod

    async def _noop_ingest(*_a, **_k):
        return {"ok": True, "ingested": 0}
    monkeypatch.setattr(news_mod, "ingest_corpus", _noop_ingest)
    asyncio.run(refresh_all(svc, force=True))  # must NOT raise
    # Every category was still attempted despite "World" exploding ("For You" runs last by design).
    expected = [c for c in CATEGORIES if c != "For You"] + (["For You"] if "For You" in CATEGORIES else [])
    assert seen == expected


# ---- endpoints (network mocked) --------------------------------------------------------------
@pytest.fixture()
def client(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from himmy_app.server import create_app

    # Mock every network-touching service method so endpoints stay offline.
    async def fake_feed(self, category, force=False):
        return {"ok": True, "category": category,
                "items": [{"title": f"{category} headline", "url": "https://bbc.co.uk/x"}]}

    async def fake_recs(self, force=False):
        return {"ok": True, "papers": [{"title": "A recommended paper", "doi": "10/x"}]}

    monkeypatch.setattr(NewsService, "feed", fake_feed)
    monkeypatch.setattr(NewsService, "recommendations", fake_recs)

    app = create_app()
    # Don't run the lifespan (it spawns the warm/refresh background loops); TestClient without
    # entering the context manager skips startup/shutdown.
    return TestClient(app)


def test_endpoint_categories(client):
    r = client.get("/news/categories")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["categories"] == CATEGORIES


def test_endpoint_feed(client):
    r = client.get("/news/feed", params={"cat": "World"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["category"] == "World"
    assert body["items"][0]["title"] == "World headline"


def test_endpoint_recommendations_papers(client):
    r = client.get("/news/recommendations")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["papers"][0]["title"] == "A recommended paper"
