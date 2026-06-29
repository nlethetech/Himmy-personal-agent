"""News index — multilingual embedding, cross-lingual story clustering, and RAG search.

Uses the REAL local multilingual embedder (so cross-lingual cosine is genuine) but MOCKS the
gray-zone LLM (no network). Conftest isolates the data dir, so the index DB is per-test.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import himmy_app.news_index as ni


@pytest.fixture()
def idx(monkeypatch):
    # The gray-zone judge: "same event" iff both headlines are about Oli (covers the EN↔NE pair).
    def fake_llm(self, a, b):
        key = lambda s: ("oli" in s.lower() or "ओली" in s)
        return key(a) and key(b)
    monkeypatch.setattr(ni.NewsIndex, "_llm_same_event", fake_llm)
    return ni.NewsIndex()


def _items(now):
    return [
        {"title": "PM Oli resigns amid coalition crisis", "url": "u1", "source": "Kathmandu Post",
         "snippet": "", "ts": now - 100, "category": "Nepal"},
        {"title": "ओलीले गठबन्धन संकटका बीच प्रधानमन्त्री पदबाट राजीनामा दिए", "url": "u2",
         "source": "Setopati", "snippet": "", "ts": now - 200, "category": "Nepal"},
        {"title": "Oli steps down as prime minister", "url": "u3", "source": "Online Khabar",
         "snippet": "", "ts": now - 300, "category": "Nepal"},
        {"title": "Nepal beats UAE by 5 wickets in cricket", "url": "u4", "source": "Ratopati",
         "snippet": "", "ts": now - 400, "category": "Nepal"},
    ]


def test_cross_lingual_story_merge(idx):
    asyncio.run(idx.ingest(_items(time.time()), category="Nepal"))
    stories = idx.stories(category="Nepal")
    assert len(stories) == 2                                    # Oli (3 reports) + cricket (1)
    oli = next(s for s in stories if s["report_count"] > 1)
    langs = {r["lang"] for r in oli["reports"]}
    assert langs == {"en", "ne"}                                # merged ACROSS languages
    assert oli["report_count"] == 3
    cricket = next(s for s in stories if s["report_count"] == 1)
    assert "cricket" in cricket["title"].lower()


def test_rag_search_is_cross_lingual(idx):
    asyncio.run(idx.ingest(_items(time.time()), category="Nepal"))
    hits = idx.search("prime minister resignation", k=4)
    assert hits and hits[0]["score"] > 0.4
    # the top hits are the Oli stories (incl. the Nepali one), not the cricket story
    assert any(h["lang"] == "ne" for h in hits[:3])
    assert "cricket" not in (hits[0]["title"].lower())


def test_dedup_by_url_on_reingest(idx):
    now = time.time()
    r1 = asyncio.run(idx.ingest(_items(now), category="Nepal"))
    r2 = asyncio.run(idx.ingest(_items(now), category="Nepal"))   # same urls again
    assert r1["ingested"] == 4 and r2["ingested"] == 0
    assert idx.stats()["articles"] == 4


def test_lang_detection():
    assert ni.detect_lang("Prime Minister resigns") == "en"
    assert ni.detect_lang("प्रधानमन्त्रीले राजीनामा दिए") == "ne"
