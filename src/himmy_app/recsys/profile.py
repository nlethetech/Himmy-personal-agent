"""The multi-topic taste profile (recsys Phase 1-2).

Build a user model from the corpus the user actually reads — their library papers (with the
notes and highlights they wrote), their saved news, and their typed interests — as a SET of
topic centroids (their research threads), not one blurred average. Each document contributes
weighted by signal strength (a highlighted passage >> a written note >> an added paper >> a
saved article >> a typed interest) times an exponential recency decay, so current threads lead.

Scoring a candidate = MAX cosine over the centroids (relevant to ANY thread ranks high). The
embedder is the shared local fastembed model; clustering is a small pure-numpy k-means (sklearn
is not a dependency). Cold-start blends a typed-interest centroid that fades as real reading
accrues, so a brand-new/empty library still gets useful results.
"""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from himmy_app.config import HimmyConfig, load_config

#: Signal-strength weights (graded implicit-feedback confidence). A paper the user highlighted AND
#: noted gets the highlight weight (the strongest evidence present).
_W_HIGHLIGHT, _W_NOTE, _W_PAPER, _W_NEWS, _W_INTEREST = 1.0, 0.8, 0.5, 0.3, 0.15
#: Engaged READING time is the strongest implicit signal of genuine interest (you can read a
#: paper for an hour without ever highlighting it), so its bonus tops out above a single
#: highlight. It's ADDITIVE on top of the explicit-action weight — reading AND highlighting beats
#: either alone — and saturates (``_READ_TAU_MIN``) so one marathon session can't swamp the model.
_W_READ, _READ_TAU_MIN = 1.2, 20.0
_HALF_LIFE_DAYS = 60.0
#: Below this much accumulated corpus weight we still fold in typed interests (cold-start); above
#: it the demonstrated corpus dominates and declared interests fade out.
_INTEREST_FLOOR = 2.0
#: With fewer than this many corpus docs, clustering is unreliable — use one weighted centroid.
_MIN_DOCS_FOR_CLUSTERS = 5
_MAX_K = 6


def _embed(embedder: Any, texts: list[str]) -> list[list[float]]:
    """Embed texts synchronously. The fastembed embedder's ``embed_documents`` is a coroutine,
    so await it on a private loop (and tolerate being called from within a running loop)."""
    import asyncio

    result = embedder.embed_documents(list(texts))
    if not asyncio.iscoroutine(result):
        return result  # defensive: a sync embedder
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(result)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(result)).result()


def _normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=float)
    if m.ndim == 1:
        n = np.linalg.norm(m)
        return m / n if n else m
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def _to_epoch(ts: Any) -> float | None:
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(ts)).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _recency(ts: Any) -> float:
    e = _to_epoch(ts)
    if e is None:
        return 1.0
    age_days = max(0.0, (time.time() - e) / 86400.0)
    return 0.5 ** (age_days / _HALF_LIFE_DAYS)


def _paper_text(r: dict[str, Any]) -> str:
    parts: list[str] = [r.get("title") or "", r.get("abstract") or ""]
    parts.extend(h for h in (r.get("highlights") or []) if h)
    note = (r.get("notes") or "").strip()
    if note:
        parts.append(note)
    return "\n".join(p for p in parts if p).strip()


def _signal_weight(r: dict[str, Any]) -> float:
    if r.get("highlights"):
        return _W_HIGHLIGHT
    if (r.get("notes") or "").strip():
        return _W_NOTE
    return _W_PAPER


def _read_bonus(minutes: float) -> float:
    """Engaged reading minutes → a saturating weight bonus (~20 min ≈ 0.63·_W_READ, ~45 min ≈
    0.9·_W_READ). Capped so a single long session can't dominate the whole taste model."""
    if minutes <= 0:
        return 0.0
    return _W_READ * (1.0 - math.exp(-minutes / _READ_TAU_MIN))


# ---- the pure-numpy clustering --------------------------------------------------------------
def _kmeans(V: np.ndarray, w: np.ndarray, k: int, *, iters: int = 30, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """Spherical (cosine) weighted k-means on unit vectors ``V``. Deterministic (fixed seed)."""
    n = len(V)
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    # weighted k-means++ seeding (cosine distance = 1 - cos)
    first = int(rng.integers(n))
    centers = [V[first]]
    for _ in range(1, k):
        nearest = np.max(np.asarray(centers) @ V.T, axis=0)
        dist = np.clip(1.0 - nearest, 0.0, None) * w
        total = float(dist.sum())
        probs = (dist / total) if total > 0 else np.full(n, 1.0 / n)
        centers.append(V[int(rng.choice(n, p=probs))])
    C = _normalize(np.asarray(centers))
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        labels = np.argmax(V @ C.T, axis=1)
        new = []
        for j in range(k):
            mask = labels == j
            if not mask.any():
                new.append(C[j])
                continue
            new.append((V[mask] * w[mask, None]).sum(axis=0))
        C2 = _normalize(np.asarray(new))
        if np.allclose(C2, C):
            C = C2
            break
        C = C2
    return C, labels


def _silhouette(V: np.ndarray, labels: np.ndarray) -> float:
    n = len(V)
    if len(set(labels.tolist())) < 2:
        return -1.0
    D = 1.0 - (V @ V.T)
    np.fill_diagonal(D, 0.0)
    scores: list[float] = []
    uniq = set(labels.tolist())
    for i in range(n):
        same = labels == labels[i]
        same[i] = False
        a = float(D[i][same].mean()) if same.any() else 0.0
        b = math.inf
        for j in uniq:
            if j == labels[i]:
                continue
            m = labels == j
            if m.any():
                b = min(b, float(D[i][m].mean()))
        if b is math.inf:
            continue
        denom = max(a, b)
        scores.append(((b - a) / denom) if denom > 0 else 0.0)
    return float(np.mean(scores)) if scores else -1.0


def _cluster(V: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Choose K by silhouette and return (centroids, labels). One centroid for a tiny corpus."""
    n = len(V)
    if n < _MIN_DOCS_FOR_CLUSTERS:
        centroid = _normalize((V * w[:, None]).sum(axis=0, keepdims=True))
        return centroid, np.zeros(n, dtype=int)
    best_c, best_lab, best_s = None, None, -2.0
    for k in range(2, min(_MAX_K, n // 2) + 1):
        C, lab = _kmeans(V, w, k)
        s = _silhouette(V, lab)
        if s > best_s:
            best_c, best_lab, best_s = C, lab, s
    if best_c is None:  # n//2 < 2 → fall back to a single centroid
        centroid = _normalize((V * w[:, None]).sum(axis=0, keepdims=True))
        return centroid, np.zeros(n, dtype=int)
    return best_c, best_lab


# ---- the profile ----------------------------------------------------------------------------
class Profile:
    """A user taste model: K topic centroids + their 'heat'. Score candidates by max-cosine."""

    def __init__(self, centroids: np.ndarray, heat: list[float], embedder: Any) -> None:
        self.centroids = np.asarray(centroids, dtype=float) if len(centroids) else np.zeros((0, 0))
        self.heat = heat
        self._embedder = embedder

    @property
    def num_topics(self) -> int:
        return int(self.centroids.shape[0]) if self.centroids.size else 0

    def score_texts(self, texts: list[str]) -> list[float]:
        """Max cosine of each text to any topic centroid (0..1, higher = more your taste)."""
        if not texts:
            return []
        if self.num_topics == 0:
            return [0.0] * len(texts)
        V = _normalize(np.asarray(_embed(self._embedder, list(texts)), dtype=float))
        sims = V @ self.centroids.T  # (n, K)
        return [float(x) for x in np.max(sims, axis=1)]

    def score(self, text: str) -> float:
        out = self.score_texts([text])
        return out[0] if out else 0.0

    def score_and_assign(self, texts: list[str]) -> tuple[list[float], list[int]]:
        """Per text: (best cosine, index of the closest topic centroid). The index lets the
        recommender GROUP candidates by the user's distinct research threads. Topic -1 = no model."""
        if not texts:
            return [], []
        if self.num_topics == 0:
            return [0.0] * len(texts), [-1] * len(texts)
        V = _normalize(np.asarray(_embed(self._embedder, list(texts)), dtype=float))
        sims = V @ self.centroids.T  # (n, K)
        scores = [float(x) for x in np.max(sims, axis=1)]
        topics = [int(i) for i in np.argmax(sims, axis=1)]
        return scores, topics


def _gather(cfg: HimmyConfig) -> tuple[list[str], list[float], list[str]]:
    """Return (corpus_texts, corpus_weights, interest_terms) from the user's real data."""
    from himmy_app.library import Library
    from himmy_app.news import SavedNews

    # Engaged reading time per paper (best-effort) — the strongest interest signal, and the
    # source of the freshest recency (a paper read today is a current thread even if added long ago).
    reading_secs: dict[str, float] = {}
    last_read: dict[str, float] = {}
    try:
        from himmy_app.reading import ReadingStore

        store = ReadingStore(cfg)
        reading_secs = store.totals_by_item()
        last_read = store.last_read_by_item()
    except Exception:  # noqa: BLE001 - reading time is a bonus, never a dependency
        pass

    texts: list[str] = []
    weights: list[float] = []
    try:
        for r in Library(cfg).rag_records():
            t = _paper_text(r)
            if not t:
                continue
            texts.append(t)
            iid = r.get("id")
            base = _signal_weight(r) + _read_bonus(reading_secs.get(iid, 0.0) / 60.0)
            # Recency from whichever is later: when it was added, or when it was last read.
            added = _to_epoch(r.get("added_at")) or 0.0
            recency_ts = max(added, last_read.get(iid, 0.0))
            weights.append(base * _recency(recency_ts if recency_ts > 0 else r.get("added_at")))
    except Exception:  # noqa: BLE001 - a library hiccup must not break the profile
        pass
    try:
        for r in SavedNews(cfg).rag_records():
            t = (r.get("text") or r.get("title") or "").strip()
            if not t:
                continue
            texts.append(t[:4000])
            weights.append(_W_NEWS * _recency(r.get("saved_at")))
    except Exception:  # noqa: BLE001
        pass
    interests: list[str] = []
    try:
        from himmy_app.news import NewsService

        interests = [i for i in NewsService(cfg).get_interests() if str(i).strip()]
    except Exception:  # noqa: BLE001
        interests = []
    return texts, weights, interests


def _signature(cfg: HimmyConfig) -> tuple[Any, ...]:
    """A cheap signature so the cached profile rebuilds when the corpus/interests change."""
    from himmy_app.library import Library
    from himmy_app.news import NewsService, SavedNews

    sig: list[Any] = []
    try:
        for r in Library(cfg).rag_records():
            sig.append((r["id"], (r.get("notes") or "").strip(), tuple(r.get("highlights") or [])))
    except Exception:  # noqa: BLE001
        pass
    try:
        sig.append(("news", tuple(sorted(r.get("id", "") for r in SavedNews(cfg).rag_records()))))
    except Exception:  # noqa: BLE001
        pass
    try:
        sig.append(("interests", tuple(NewsService(cfg).get_interests())))
    except Exception:  # noqa: BLE001
        pass
    try:
        from himmy_app.reading import ReadingStore

        totals = ReadingStore(cfg).totals_by_item()
        # Rounded to whole minutes: the profile rebuilds when reading crosses a minute boundary —
        # responsive enough to matter, coarse enough not to rebuild on every 15s heartbeat.
        sig.append(("reading", tuple(sorted((k, round(v / 60.0)) for k, v in totals.items()))))
    except Exception:  # noqa: BLE001
        pass
    return tuple(sig)


_CACHE: dict[str, Any] = {"sig": None, "profile": None}


def invalidate_profile_cache() -> None:
    _CACHE["sig"] = None
    _CACHE["profile"] = None


def build_profile(cfg: HimmyConfig | None = None) -> Profile:
    """Build (or return the cached) multi-topic taste profile from the user's real data."""
    from himmy.toolkit import ToolkitConfig

    cfg = cfg or load_config()
    sig = _signature(cfg)
    if _CACHE["profile"] is not None and _CACHE["sig"] == sig:
        return _CACHE["profile"]

    embedder, dim = ToolkitConfig.from_env().build_embedder_and_dim()
    texts, weights, interests = _gather(cfg)

    centroids: list[np.ndarray] = []
    heat: list[float] = []
    corpus_weight = 0.0
    if texts:
        V = _normalize(np.asarray(_embed(embedder, texts), dtype=float))
        w = np.asarray(weights, dtype=float)
        w[w <= 0] = 1e-6
        C, labels = _cluster(V, w)
        for j in range(len(C)):
            centroids.append(C[j])
            heat.append(float(w[labels == j].sum()))
        corpus_weight = float(w.sum())

    # Cold-start / fade: typed interests count only while the demonstrated corpus is thin.
    if interests and corpus_weight < _INTEREST_FLOOR:
        IV = _normalize(np.asarray(_embed(embedder, interests), dtype=float))
        ic = _normalize(IV.mean(axis=0, keepdims=True))[0]
        centroids.append(ic)
        heat.append(max(1.0, _INTEREST_FLOOR - corpus_weight))

    C = np.asarray(centroids, dtype=float) if centroids else np.zeros((0, dim))
    profile = Profile(C, heat, embedder)
    _CACHE["sig"] = sig
    _CACHE["profile"] = profile
    return profile


__all__ = ["Profile", "build_profile", "invalidate_profile_cache"]
