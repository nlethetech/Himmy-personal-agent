"""Domestic/foreign split — Nepali outlets (Ratopati etc.) publish world news on the same feed, so
the Nepal section must keep only DOMESTIC stories and route the foreign ones to World. The classifier
is title-based (the body's 'Kathmandu.' dateline is on foreign stories too). Network is mocked.
"""

from __future__ import annotations

import asyncio

import pytest

from himmy_app.news import NewsService, _is_domestic_nepal


@pytest.mark.parametrize("title, domestic", [
    ("Bagmati Province Government Faces Criticism Over Budget Allocation", True),
    ("Congress Criticizes Rastriya Swatantra Party for Enrolling Minors", True),
    ("Nepal's First Milk Bank Faces High Demand", True),
    ("PM Oli to visit India next week", True),                 # Nepal–foreign relation → domestic
    ("Nepali workers stranded in Qatar return home", True),
    ("Azerbaijan Expresses Displeasure Over Israel's Recognition of Armenian Genocide", False),
    ("Hezbollah Accuses Israel of Repeated Ceasefire Violations", False),
    ("Putin Vows No Retreat on Annexed Ukrainian Regions", False),
    ("Taliban Tighten Restrictions in Afghanistan", False),
    ("Trump announces new tariffs on Europe", False),
])
def test_classifier(title, domestic):
    assert _is_domestic_nepal(title) is domestic


def _items(*titles):
    return [{"title": t, "url": f"http://x/{i}", "source": "Ratopati", "image": "",
             "snippet": "Kathmandu. " + t, "ts": 1000 - i, "ago": "1h"} for i, t in enumerate(titles)]


@pytest.fixture()
def mocked_feeds(monkeypatch):
    """Ratopati (Nepal outlet) carries 2 domestic + 2 foreign; BBC (World) carries 1 world story."""
    async def fake_fetch(self, client, name, url):
        if name == "Ratopati":
            return _items("Bagmati Province budget criticised",
                          "Nepal's milk bank in high demand",
                          "Israel strikes Gaza again",
                          "Russia and Ukraine resume talks")
        if name == "BBC World":
            return [{"title": "EU summit opens in Brussels", "url": "http://bbc/1", "source": "BBC World",
                     "image": "", "snippet": "...", "ts": 999, "ago": "1h"}]
        return []
    monkeypatch.setattr(NewsService, "_fetch_feed", fake_fetch)


def test_nepal_keeps_only_domestic(mocked_feeds):
    items = asyncio.run(NewsService()._category("Nepal"))
    titles = [it["title"] for it in items]
    assert any("Bagmati" in t for t in titles) and any("milk bank" in t for t in titles)
    assert not any("Israel" in t or "Russia" in t for t in titles)   # foreign routed out


def test_world_gets_nepali_outlets_foreign(mocked_feeds):
    items = asyncio.run(NewsService()._category("World"))
    titles = [it["title"] for it in items]
    assert any("Israel" in t for t in titles) and any("Russia" in t for t in titles)  # foreign in
    assert any("EU summit" in t for t in titles)                                       # intl outlet kept
    assert not any("Bagmati" in t or "milk bank" in t for t in titles)                 # domestic excluded


def test_merge_collapses_duplicate_coverage():
    """The same event from two outlets merges into one story carrying both reports; others stay."""
    from himmy_app.news import _merge_duplicates
    items = [
        {"title": "Special Court extends Bishnu Paudel remand by three days",
         "url": "http://x/1", "source": "The Kathmandu Post", "ts": 100, "ago": "1h"},
        {"title": "Court extends Bishnu Paudel remand period",
         "url": "http://x/2", "source": "Khabarhub", "ts": 99, "ago": "2h"},
        {"title": "Wild elephants destroy six homes in Udayapur",
         "url": "http://x/3", "source": "Online Khabar", "ts": 98, "ago": "3h"},
    ]
    m = _merge_duplicates(items)
    assert len(m) == 2                                  # two Paudel reports collapsed to one
    paudel = next(x for x in m if "Paudel" in x["title"])
    assert paudel["report_count"] == 2
    assert {r["source"] for r in paudel["reports"]} == {"The Kathmandu Post", "Khabarhub"}
    elephants = next(x for x in m if "elephants" in x["title"])
    assert elephants.get("report_count", 1) == 1        # a distinct story isn't over-merged


def test_developing_clusters_shared_coverage(monkeypatch):
    """Headlines sharing >=2 distinctive terms group into one developing story; loners don't."""
    def feed_items(*titles):
        return [{"title": t, "url": f"http://x/{i}", "source": f"src{i}", "image": "",
                 "snippet": "", "ts": 1000 - i, "ago": "1h"} for i, t in enumerate(titles)]

    async def fake_feed(self, cat, force=False):
        if cat == "World":
            return {"ok": True, "items": feed_items(
                "Supreme Court blocks Trump attempt to fire Federal Reserve official",
                "Supreme Court rejects Trump appeal in Carroll case",
                "Trump escalates fight with Supreme Court over agencies",
                "Local council debates new park budget",          # unrelated loner
            )}
        return {"ok": True, "items": []}
    monkeypatch.setattr(NewsService, "feed", fake_feed)

    r = asyncio.run(NewsService().developing(categories=["World"]))
    assert r["ok"]
    top = r["stories"][0]
    assert top["count"] == 3 and "Supreme Court" in top["title"]   # the 3 Trump/Court articles
    assert len(top["sources"]) == 3
    assert not any("park budget" in s["title"] for s in r["stories"])  # the loner isn't a story
