"""Recommender: a local, free taste model over what the user actually reads.

Phase 1-2 (this module's :mod:`profile`): a MULTI-TOPIC user profile — the user's few research
threads as separate centroids, signal-weighted (highlight > note > paper > saved news > typed
interest) and recency-decayed — plus a cold-start blend with typed interests. Scoring is
max-cosine over the centroids, so a candidate relevant to ANY thread ranks high (no "average of
two unrelated tastes" failure). Everything runs on the local fastembed embedder; no network, no
LLM, no new dependencies (pure numpy).
"""

from himmy_app.recsys.profile import Profile, build_profile, invalidate_profile_cache

__all__ = ["Profile", "build_profile", "invalidate_profile_cache"]
