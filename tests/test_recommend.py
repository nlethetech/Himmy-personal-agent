"""The multi-source recommender: it seeds from the user's dominant discipline (so a couple of
off-field outliers in the library can't hijack the results), retrieves across scholarly sources,
hard-drops papers outside the reader's field(s), de-dupes against what they already have, and
survives any single source failing.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from himmy_app.config import load_config
from himmy_app.feedback import DismissalStore
from himmy_app.library import Library
from himmy_app.recsys import sources
from himmy_app.recsys.recommend import Recommender, _first_sentence, _recency_year, _title_key


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data"))
    return load_config()


def _cand(title, *, fields=None, doi="", abstract="x", citations=0, via=None, year="2022"):
    c = sources._candidate(
        title=title, abstract=abstract, doi=doi, citations=citations, source="openalex",
        year=year, fields=fields or [],
    )
    if via:
        c["_via"] = via
    return c


# ---- pure helpers ---------------------------------------------------------------------------
def test_title_key_normalises():
    assert _title_key("The Role of  Intermediaries!") == "the role of intermediaries"


def test_recency_year_decays():
    cur = datetime.date.today().year
    assert _recency_year(str(cur)) > _recency_year(str(cur - 6)) > _recency_year(str(cur - 20))
    assert _recency_year("not-a-year") == 0.5


# ---- dedupe ---------------------------------------------------------------------------------
def test_dedupe_drops_papers_already_owned(cfg):
    rec = Recommender(cfg)
    cands = [
        _cand("New Econ Paper", doi="10.1/new"),
        _cand("Already In Library", doi="10.1/have"),
        _cand("Saved Already", doi="10.2/x"),
    ]
    out = rec._dedupe(cands, known_dois={"10.1/have"}, known_titles={_title_key("Saved Already")})
    titles = {c["title"] for c in out}
    assert titles == {"New Econ Paper"}


def test_dedupe_merges_the_same_paper_from_two_sources(cfg):
    rec = Recommender(cfg)
    thin = _cand("Same Paper", doi="10.5/same", abstract="", citations=2)
    rich = _cand("Same Paper", doi="10.5/same", abstract="a fuller abstract", citations=99)
    out = rec._dedupe([thin, rich], set(), set())
    assert len(out) == 1
    assert out[0]["abstract"] == "a fuller abstract" and out[0]["citations"] == 99


# ---- ranking + domain filter ----------------------------------------------------------------
def test_rank_hard_drops_off_domain_papers(cfg):
    rec = Recommender(cfg)
    cands = [
        _cand("Trade and Conflict", fields=["Economics"], doi="10/e"),
        _cand("Transformers for Vision", fields=["Computer Science"], doi="10/c"),
        _cand("Untagged Paper", fields=[], doi="10/u"),
    ]
    titles = {c["title"] for c in rec._rank(cands, interests=[], user_fields=["Economics"])}
    assert "Trade and Conflict" in titles           # in-discipline → kept
    assert "Untagged Paper" in titles               # no field label → benefit of the doubt
    assert "Transformers for Vision" not in titles  # purely off-domain → dropped


def test_build_threads_groups_by_user_concepts(cfg):
    rec = Recommender(cfg)
    ranked = [_cand(f"Trade {i}", doi=f"10/t{i}") for i in range(3)] \
        + [_cand(f"Conflict {i}", doi=f"10/c{i}") for i in range(3)] \
        + [_cand("Off-theme straggler", doi="10/x")]
    for p in ranked[:3]:
        p["concepts"] = ["International trade"]
    for p in ranked[3:6]:
        p["concepts"] = ["Civil war"]
    ranked[6]["concepts"] = ["Unrelated topic"]
    for i, p in enumerate(ranked):
        p["score"] = 1.0 - i * 0.01

    threads = rec._build_threads(ranked, ["International trade", "Civil war"], min_size=3)
    assert {t["label"] for t in threads} == {"International trade", "Civil war"}
    grouped = {p["title"] for t in threads for p in t["papers"]}
    assert "Off-theme straggler" not in grouped  # matches no research concept → excluded from digest


def test_rank_boosts_in_discipline_over_untagged(cfg):
    rec = Recommender(cfg)
    cands = [
        _cand("Untagged", fields=[], doi="10/u", year="2020"),
        _cand("Economics One", fields=["Economics"], doi="10/e", year="2020"),
    ]
    out = rec._rank(cands, interests=[], user_fields=["Economics"])
    assert out[0]["title"] == "Economics One"  # the domain boost wins the tie


# ---- the dominant-cluster seed logic (network-free via monkeypatched sources) ----------------
def test_gather_follows_dominant_cluster_not_outliers(cfg, monkeypatch):
    seeds = (
        [{"id": f"e{i}", "title": f"Econ {i}", "doi": f"10.1/e{i}"} for i in range(6)]
        + [{"id": "c0", "title": "GPT-3", "doi": "10.2/gpt"},
           {"id": "c1", "title": "Crop ML", "doi": "10.2/crop"}]
    )

    async def fake_resolve(_client, *, doi="", title=""):
        if doi.startswith("10.2"):  # the two CS outliers
            return {"title": title, "related_works": ["https://openalex.org/WCS"],
                    "concepts": [{"display_name": "Computer science", "level": 0, "score": 0.9, "id": "https://openalex.org/C1"}]}
        return {"title": title, "related_works": ["https://openalex.org/WECON"],
                "concepts": [{"display_name": "Economics", "level": 0, "score": 0.8, "id": "https://openalex.org/C2"}]}

    captured: dict[str, list] = {}

    async def cap_s2(_c, dois):
        captured["seed_dois"] = list(dois)
        return []

    async def cap_byids(_c, ids):
        captured["related"] = list(ids)
        return []

    async def empty(*_a, **_k):
        return []

    monkeypatch.setattr(sources, "openalex_resolve", fake_resolve)
    monkeypatch.setattr(sources, "semantic_scholar_recommend", cap_s2)
    monkeypatch.setattr(sources, "openalex_by_ids", cap_byids)
    monkeypatch.setattr(sources, "openalex_by_concepts", empty)
    monkeypatch.setattr(sources, "openalex_search", empty)
    monkeypatch.setattr(sources, "crossref_search", empty)
    monkeypatch.setattr(sources, "arxiv_search", empty)

    _cands, user_fields, _concepts = asyncio.run(Recommender(cfg)._gather(seeds, []))
    assert user_fields == ["Economics"]                              # CS excluded from the field set
    assert all(not d.startswith("10.2") for d in captured["seed_dois"])  # CS seeds not expanded
    assert "WCS" not in captured["related"]                          # CS related-works not pulled


def test_first_sentence_fallback():
    assert _first_sentence("Rainfall lowers yields. More text here.") == "Rainfall lowers yields."
    assert _first_sentence("") == ""


# ---- "not interested" learning --------------------------------------------------------------
def test_dismissal_store_records_identity_and_concepts(cfg):
    ds = DismissalStore(cfg)
    ds.dismiss("10.1/X", "Some Paper", ["Poverty", "Economics"])
    assert "10.1/x" in ds.dismissed_dois()       # normalised
    assert ds.count() == 1
    assert ds.concept_counts()["poverty"] == 1


def test_dismissal_by_title_when_no_doi(cfg):
    ds = DismissalStore(cfg)
    ds.dismiss("", "Title Only Paper", [])
    assert _title_key("Title Only Paper") in ds.dismissed_title_keys()


def test_filter_dismissed_drops_from_digest_and_repicks_hero(cfg):
    DismissalStore(cfg).dismiss("10/drop", "Drop Me", [])
    rec = Recommender(cfg)
    result = {
        "papers": [_cand("Keep", doi="10/keep"), _cand("Drop Me", doi="10/drop")],
        "threads": [{"label": "T", "count": 2, "papers": [_cand("Keep", doi="10/keep"), _cand("Drop Me", doi="10/drop")]}],
        "hero": _cand("Drop Me", doi="10/drop"),
    }
    out = rec._filter_dismissed(result)
    assert {p["title"] for p in out["papers"]} == {"Keep"}
    assert out["hero"]["title"] == "Keep"           # hero re-picked after the dismissed one fell out


def test_rank_penalises_dismissed_concepts(cfg):
    for i in range(3):                               # dismiss several "Poverty" papers
        DismissalStore(cfg).dismiss(f"10/p{i}", f"Pov {i}", ["Poverty"])
    pov = _cand("Poverty paper", doi="10/a")
    pov["concepts"] = ["Poverty"]
    trade = _cand("Trade paper", doi="10/b")
    trade["concepts"] = ["International trade"]
    out = Recommender(cfg)._rank([pov, trade], interests=[], user_fields=[])
    assert out[0]["title"] == "Trade paper"          # poverty sank after the dismissals


def test_top_authors_venues_restricted_to_seed_ids(cfg):
    lib = Library(cfg)
    base = {"type": "article", "year": "2020", "doi": "", "url": "", "abstract": "x", "tags": [], "pdf_path": ""}
    lib._insert({**base, "id": "s1", "title": "Econ seed", "authors": ["Jane Econ"], "venue": "World Development", "added_at": 1.0})
    lib._insert({**base, "id": "x1", "title": "ML outlier", "authors": ["Ashish Vaswani"], "venue": "NeurIPS", "added_at": 2.0})
    authors, venues = Recommender(cfg)._top_authors_venues({"s1"})  # only the in-cluster seed
    assert "Jane Econ" in authors and "Ashish Vaswani" not in authors
    assert "World Development" in venues


def test_gather_survives_a_failing_source(cfg, monkeypatch):
    seeds = [{"id": "e0", "title": "Econ", "doi": "10.1/e0"}]

    async def fake_resolve(_client, *, doi="", title=""):
        return {"title": title, "related_works": [],
                "concepts": [{"display_name": "Economics", "level": 0, "score": 0.8, "id": "https://openalex.org/C2"}]}

    async def boom(*_a, **_k):
        raise RuntimeError("source is down")

    async def one_ok(*_a, **_k):
        return [_cand("Surviving Econ Paper", fields=["Economics"], doi="10.9/ok")]

    monkeypatch.setattr(sources, "openalex_resolve", fake_resolve)
    monkeypatch.setattr(sources, "semantic_scholar_recommend", boom)   # this one explodes
    monkeypatch.setattr(sources, "openalex_by_ids", one_ok)            # this one still delivers
    monkeypatch.setattr(sources, "openalex_by_concepts", boom)
    monkeypatch.setattr(sources, "openalex_search", boom)
    monkeypatch.setattr(sources, "crossref_search", boom)
    monkeypatch.setattr(sources, "arxiv_search", boom)

    cands, _fields, _concepts = asyncio.run(Recommender(cfg)._gather(seeds, []))
    assert any(c["title"] == "Surviving Econ Paper" for c in cands)  # one failure ≠ total failure
