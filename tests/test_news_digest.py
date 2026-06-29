"""News digest engine + the 'News Digest → Telegram' routine.

The digest pulls each section's feed and writes a short summary (one model pass, with a deterministic
headline-list fallback so a digest always appears). The routine is seeded OFF and special-cased to
build the digest and push it to Telegram. Feeds and the model are mocked.
"""

from __future__ import annotations

import asyncio

import pytest

from himmy_app.news import NewsService


@pytest.fixture()
def mocked_feed(monkeypatch):
    async def fake_feed(self, cat, force=False):
        return {"ok": True, "items": [
            {"title": f"{cat} headline {i}", "snippet": f"detail {i}"} for i in range(4)
        ]}
    monkeypatch.setattr(NewsService, "feed", fake_feed)


def test_digest_falls_back_to_headlines_without_model(mocked_feed, monkeypatch):
    import himmy.cli.provider as prov

    def boom(*_a, **_k):
        raise RuntimeError("no model")
    monkeypatch.setattr(prov, "build_inference_for", boom)

    r = asyncio.run(NewsService().digest(categories=["Nepal", "World"]))
    assert r["ok"] and r["count"] == 8
    assert "Nepal" in r["text"] and "World" in r["text"]
    assert "headline 0" in r["text"]                 # real headlines, deterministic fallback


def test_digest_uses_model_when_available(mocked_feed, monkeypatch):
    import himmy.cli.provider as prov

    class _Resp:
        def __init__(self, t): self.output_text = t

    class _Svc:
        async def run(self, _req): return _Resp("**Nepal**\n• Budget passes\n\n**World**\n• Summit opens")
    monkeypatch.setattr(prov, "build_inference_for", lambda _p, _m: _Svc())

    r = asyncio.run(NewsService().digest(categories=["Nepal", "World"]))
    assert r["ok"] and "Budget passes" in r["text"] and "Summit opens" in r["text"]


def test_digest_empty_when_no_news(monkeypatch):
    async def empty_feed(self, cat, force=False):
        return {"ok": True, "items": []}
    monkeypatch.setattr(NewsService, "feed", empty_feed)
    r = asyncio.run(NewsService().digest())
    assert r["ok"] is False and r["count"] == 0


def test_seed_creates_disabled_news_digest():
    from himmy_app.routines import _NEWS_DIGEST_NAME, list_routines, seed_default_routines

    seed_default_routines()
    rows = [r for r in list_routines() if r["name"] == _NEWS_DIGEST_NAME]
    assert len(rows) == 1
    assert rows[0]["enabled"] is False                # opt-in: user turns it on


def test_news_digest_routine_pushes_to_telegram(monkeypatch):
    """The 'News Digest' routine builds a digest and pushes it to Telegram, landing in the inbox."""
    import himmy_app.news as news_mod
    import himmy_app.telegram as tg
    from himmy_app.routines import (AppScheduler, _NEWS_DIGEST_NAME, get_inbox,
                                    get_routines_store, seed_default_routines)

    seed_default_routines()
    routine = next(r for r in get_routines_store().list() if r.name == _NEWS_DIGEST_NAME)

    async def fake_digest(self, *a, **k):
        return {"ok": True, "text": "**Nepal**\n• Big story", "count": 1, "sections": []}
    monkeypatch.setattr(news_mod.NewsService, "digest", fake_digest)

    pushed = {}
    async def fake_push(text, cfg=None):
        pushed["text"] = text
        return True
    monkeypatch.setattr(tg, "push", fake_push)

    status, preview, err = asyncio.run(AppScheduler()._fire(routine))
    assert status == "ok" and err is None
    assert "Big story" in pushed["text"]              # pushed to Telegram
    bodies = [n["body"] for n in get_inbox().list(limit=10) if n["routine_name"] == _NEWS_DIGEST_NAME]
    assert any("Big story" in b and "sent to your Telegram" in b for b in bodies)
