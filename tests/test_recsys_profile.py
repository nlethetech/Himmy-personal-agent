"""The multi-topic taste profile: distinct research threads cluster apart, scoring is
max-over-centroids (a candidate relevant to ANY thread ranks high, not a blurred average),
cold-start works from typed interests alone, and the cache rebuilds on change.
"""

from __future__ import annotations

import time

import pytest

from himmy_app.config import load_config
from himmy_app.library import Library
from himmy_app.recsys.profile import build_profile, invalidate_profile_cache

_DIFFUSION = [
    ("Denoising Diffusion Probabilistic Models",
     "Diffusion models generate images by reversing a gradual Gaussian noising process, "
     "achieving low FID and high sample diversity, outperforming GANs on image synthesis."),
    ("Score-Based Generative Modeling",
     "Score matching and stochastic differential equations for diffusion-based image generation; "
     "sampling with Langevin dynamics and noise schedules."),
    ("Classifier-Free Guidance for Diffusion",
     "Guidance improves sample quality in denoising diffusion image generation models; trade-off "
     "between diversity and fidelity in the generative process."),
]
_RAINFALL = [
    ("Rainfall Variability and Crop Yields in the Sahel",
     "Millet and sorghum yields respond nonlinearly to rainfall in Sahelian rainfed agriculture; "
     "drought reduces harvests and threatens food security for farmers."),
    ("Drought, Irrigation and Agricultural Output",
     "Irrigation buffers crop yield losses from rainfall shortfalls; agronomic analysis of water "
     "stress on cereal production across semi-arid farmland."),
    ("Climate Shocks and Farm Incomes",
     "Rainfall shocks lower agricultural output and farm household incomes in developing rural "
     "economies dependent on monsoon crop cultivation."),
]


def _seed(lib: Library, items, *, highlight_first: bool = False) -> None:
    for i, (title, abstract) in enumerate(items):
        iid = f"itm_{title[:6].replace(' ', '')}_{i}"
        lib._insert({
            "id": iid, "type": "article", "title": title, "authors": ["A. Researcher"],
            "year": "2021", "venue": "Test", "doi": "", "url": "", "abstract": abstract,
            "tags": [], "pdf_path": "", "added_at": time.time(),
        })
    if highlight_first:
        first = f"itm_{items[0][0][:6].replace(' ', '')}_0"
        lib.add_highlight(first, 1, "yellow", "this passage is the key result I care about", "", [])


@pytest.fixture()
def cfg_with_library(tmp_path, monkeypatch):
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data"))
    cfg = load_config()
    lib = Library(cfg)
    _seed(lib, _DIFFUSION, highlight_first=True)
    _seed(lib, _RAINFALL)
    invalidate_profile_cache()
    return cfg


def test_distinct_threads_cluster_apart_and_both_score_high(cfg_with_library):
    prof = build_profile(cfg_with_library)
    # Two genuinely different research areas → at least two topic centroids (not one midpoint).
    assert prof.num_topics >= 2, f"expected the two threads to separate, got {prof.num_topics} topic(s)"

    diffusion_q = "a new approach to image generation with denoising diffusion and better FID"
    rainfall_q = "effect of drought and rainfall on millet crop yields for Sahel farmers"
    off_topic_q = "a study of medieval Italian poetry and Renaissance sonnet structure"

    s_diff, s_rain, s_off = prof.score(diffusion_q), prof.score(rainfall_q), prof.score(off_topic_q)
    # max-over-centroids: BOTH threads' candidates score clearly above an unrelated one.
    assert s_diff > s_off + 0.1, (s_diff, s_off)
    assert s_rain > s_off + 0.1, (s_rain, s_off)


def test_cold_start_from_interests_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data2"))
    cfg = load_config()
    Library(cfg)  # empty library
    from himmy_app.news import NewsService

    NewsService(cfg).set_interests(["diffusion models", "generative image synthesis"])
    invalidate_profile_cache()

    prof = build_profile(cfg)
    assert prof.num_topics >= 1, "cold-start must yield a usable (non-empty) profile from interests"
    on = prof.score("denoising diffusion models for image generation")
    off = prof.score("medieval Italian poetry and Renaissance sonnets")
    assert on > off + 0.1, (on, off)


def test_cache_rebuilds_when_corpus_changes(cfg_with_library):
    p1 = build_profile(cfg_with_library)
    p2 = build_profile(cfg_with_library)
    assert p1 is p2, "same corpus → cached profile reused"
    # Add a paper → signature changes → a fresh profile is built.
    _seed(Library(cfg_with_library), [("Transformers for Language", "Self-attention language models.")])
    p3 = build_profile(cfg_with_library)
    assert p3 is not p1, "adding a paper must rebuild the profile"
