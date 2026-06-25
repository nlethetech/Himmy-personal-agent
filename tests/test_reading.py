"""Engaged reading-time tracking: heartbeats accumulate per paper but are clamped (so the
number can't be inflated) and tiny accidental opens are dropped; reading time then becomes a
strong recsys signal AND revives the recency of an old-but-recently-read paper; and the whole
thing round-trips over HTTP.
"""

from __future__ import annotations

import time

import pytest

from himmy_app.config import load_config
from himmy_app.library import Library
from himmy_app.reading import _MAX_HEARTBEAT_DELTA, _MIN_SESSION_SECONDS, ReadingStore
from himmy_app.recsys.profile import _gather, _signature


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data"))
    return load_config()


def _insert_paper(lib: Library, iid: str, title: str, abstract: str, *, added_at: float | None = None) -> None:
    lib._insert({
        "id": iid, "type": "article", "title": title, "authors": ["A. Researcher"],
        "year": "2021", "venue": "Test", "doi": "", "url": "", "abstract": abstract,
        "tags": [], "pdf_path": "", "added_at": added_at if added_at is not None else time.time(),
    })


# ---- the store ------------------------------------------------------------------------------
def test_heartbeats_accumulate_within_a_session(cfg):
    s = ReadingStore(cfg)
    for _ in range(4):
        s.record_heartbeat("sess-A", "paper-1", 12.0)
    assert s.item_seconds("paper-1") == pytest.approx(48.0)


def test_single_beat_is_clamped_so_time_cannot_be_inflated(cfg):
    s = ReadingStore(cfg)
    r = s.record_heartbeat("sess-B", "paper-2", 999_999.0)
    assert r["session_seconds"] == pytest.approx(_MAX_HEARTBEAT_DELTA)
    assert s.item_seconds("paper-2") == pytest.approx(_MAX_HEARTBEAT_DELTA)


def test_tiny_accidental_opens_are_dropped(cfg):
    s = ReadingStore(cfg)
    s.record_heartbeat("blip", "paper-3", _MIN_SESSION_SECONDS - 2)
    assert s.item_seconds("paper-3") == 0.0
    assert "paper-3" not in s.totals_by_item()


def test_stats_today_week_total_are_ordered(cfg):
    s = ReadingStore(cfg)
    s.record_heartbeat("sess-now", "paper-1", 20.0)
    st = s.stats()
    assert st["today_seconds"] >= 20.0
    assert st["week_seconds"] >= st["today_seconds"]
    assert st["total_seconds"] >= st["week_seconds"]


def test_missing_ids_are_rejected(cfg):
    s = ReadingStore(cfg)
    assert s.record_heartbeat("", "paper-1", 10.0)["ok"] is False
    assert s.record_heartbeat("sess", "", 10.0)["ok"] is False


# ---- recsys integration (no embeddings: _gather computes weights before embedding) ----------
def _weight_for(cfg, title_substr: str) -> float:
    texts, weights, _ = _gather(cfg)
    for t, w in zip(texts, weights):
        if title_substr.lower() in t.lower():
            return w
    raise AssertionError(f"no gathered text contained {title_substr!r}")


def test_reading_time_raises_a_papers_recsys_weight(cfg):
    lib = Library(cfg)
    _insert_paper(lib, "p_read", "Quantum Error Correction", "Stabiliser codes and fault tolerance.")
    _insert_paper(lib, "p_cold", "Medieval Italian Poetry", "Renaissance sonnet structure and meter.")

    before = _weight_for(cfg, "Quantum Error Correction")
    # ~30 engaged minutes on the quantum paper (well above one highlight's worth of signal).
    store = ReadingStore(cfg)
    for _ in range(60):
        store.record_heartbeat("s1", "p_read", 30.0)  # 60 * 30s = 30 min
    after = _weight_for(cfg, "Quantum Error Correction")
    cold = _weight_for(cfg, "Medieval Italian Poetry")

    assert after > before + 0.5, (before, after)        # reading clearly lifts the weight
    assert after > cold, (after, cold)                  # the read paper now outweighs the unread one


def test_recent_reading_revives_an_old_papers_recency(cfg):
    lib = Library(cfg)
    # Added 200 days ago → its recency (60-day half-life) is tiny on added_at alone.
    old = time.time() - 200 * 86400
    _insert_paper(lib, "p_old", "Topological Data Analysis", "Persistent homology of point clouds.", added_at=old)

    stale = _weight_for(cfg, "Topological Data Analysis")
    store = ReadingStore(cfg)
    for _ in range(20):
        store.record_heartbeat("s2", "p_old", 30.0)  # read it just now
    revived = _weight_for(cfg, "Topological Data Analysis")
    # Both the reading bonus AND the refreshed recency (last_read ~ now) push the weight up a lot.
    assert revived > stale * 3, (stale, revived)


def test_signature_changes_when_reading_accrues(cfg):
    lib = Library(cfg)
    _insert_paper(lib, "p_sig", "Sig Paper", "Some abstract.")
    sig_before = _signature(cfg)
    for _ in range(3):
        ReadingStore(cfg).record_heartbeat("s3", "p_sig", 30.0)  # cross a whole-minute boundary
    assert _signature(cfg) != sig_before


# ---- HTTP round-trip ------------------------------------------------------------------------
def test_heartbeat_endpoint_round_trips(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    import himmy_app.server as srv

    with TestClient(srv.create_app()) as client:
        r = client.post("/reading/heartbeat", json={"session_id": "h1", "item_id": "x1", "seconds": 18.0})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert client.get("/reading/item/x1").json()["seconds"] == pytest.approx(18.0)
        assert client.get("/reading/stats").json()["today_seconds"] >= 18.0
