# Himmy — state-of-the-art reading-based recommender (plan)
_2026-06-23, multi-agent design + adversarial pressure-test, grounded in the code._

## Headline
Replace the hand-typed keyword arXiv recommender with a local, free, multi-topic taste model built from the papers, notes, and highlights the user actually reads — clustered into their handful of research threads, expanded along the citation graph, ranked entirely on the existing fastembed embedder, and slowly improved by a single dismiss button.

## Architecture (end state)
The system models the user as a SET of topic centroids (their 3-6 research threads), not one averaged vector — this is the single most important fix, killing the "average of two unrelated tastes" failure. On each refresh it loads the ~30 corpus records via Library.rag_records() and SavedNews.rag_records() (in scholar-desk/src/himmy_app/library.py and news.py) and re-embeds them with the SHARED local fastembed embedder (ToolkitConfig.from_env().build_embedder_and_dim() — the same model and dimension the RAG index uses, so vectors are directly comparable). Re-embedding ~30 short records is trivially cheap and deliberately avoids both coupling to himmy's private knowledge_chunks schema (there is no public vector-fetch API) and "database is locked" contention with the live papers_index.db prewarm. Each record contributes to its thread weighted by signal strength (highlight 1.0 > note 0.8 > added paper 0.5 > saved news 0.3 > typed interest 0.15 — the kind tag is already written by papers_rag) times an exponential recency decay (half-life ~60 days, using existing items.added_at / saved_news.saved_at / highlights.created; notes have no timestamp so they fall back to their paper's added_at). Mean-pooled per-document vectors are clustered with a pure-numpy k-means (sklearn is NOT in the venv — confirmed numpy 2.4.6 only), K chosen small with a hard K=1 fallback under ~5 docs. Typed interests are embedded into the same space as a fading pseudo-centroid (alpha = floor / (floor + corpus_weight)) so a brand-new user still gets results and the declared signal fades as real reading accrues — this also fixes today's bug where an empty interests list returns nothing. CANDIDATES come from two channels, unioned and disk-cached on a 6-24h TTL: (1) the existing arXiv recency query, serialized with backoff so it never bursts; and (2) an OpenAlex related_works / citing channel seeded from the user's own library DOIs and arXiv ids (reusing library.py's httpx client, DOI/arXiv regexes, and the existing generic hello@himmy.app polite UA — never the user's personal email). Semantic Scholar is best-effort only because it rate-limits hard keyless. RANKING is 100% local and free: batch-embed each candidate's title+abstract once, score = MAX cosine over the thread centroids (so a niche second interest is never washed out), gate out anything already in the library or already dismissed, dedup by DOI/arXiv-id then normalized-title then a >0.93 cosine near-dup gate, then re-order with MMR (lambda ~0.7) for diversity plus a couple of "fresh direction" slots from adjacent clusters for serendipity. An optional final cross-encoder rerank on the top ~30 reuses papers_rag._maybe_reranker (already cached and gracefully degrading). The metered LLM (OpenRouter gemini-2.5-flash) NEVER touches scoring; it is used only for optional, cached, one-line "why recommended" blurbs, template-first and skipped entirely when offline or keyless. LEARNING starts minimal: a single dismiss / "not interested" control feeds an append-only events table in library.db (which IS backed up); dismissed ids hard-exclude from candidates and apply a small negative weight to the nearest centroid. That is the only true negative a content model can use, and it is the realistic ceiling of online learning for a single user until many months of dismiss/save events accumulate.

## Cost posture
LOCAL / FREE (the entire hot path): all embedding via the existing fastembed model (no API cost), all clustering in pure numpy (no sklearn, no new deps), all scoring (max-over-centroids cosine, MMR, dedup, novelty) in numpy, and the optional cross-encoder rerank via the already-installed fastembed reranker. Profile + ranking run on every view with zero network. API-FREE (network but no billing): candidate generation via arXiv (already wired) and OpenAlex (keyless, polite hello@himmy.app UA), disk-cached on a 6-24h TTL so re-ranks are instant and offline-tolerant; arXiv serialized with backoff to respect its burst limit; Semantic Scholar best-effort only. METERED (the only paid touch, strictly off the ranking path): OpenRouter gemini-2.5-flash used solely for optional 'why recommended' blurbs — template-first, at most ~8 tiny calls per refresh, cached by candidate id so identical recs never re-bill, and skipped entirely when offline or the key is absent. NEW STORAGE (free, local): a small append-only rec_events table in library.db (backed up) plus a regenerable candidate-pool cache (added to _BACKUP_SKIP).

## New data to capture
- A minimal append-only events table in library.db (NOT a new recsys.db — confirmed _BACKUP_SKIP excludes only papers_cache.db/papers_index.db/news_cache.json, so a new DB would silently never be backed up and dismissals would be lost on reinstall). Schema scoped to EXPLICIT signals only for now: rec_events(id, ts, kind, target_id, target_type, meta) where kind in {dismiss, save}. target_id is the DOI / arXiv id / url already used for dedup.
- A 'dismiss / not interested' control on the existing RecommendedMain cards in desktop/src/App.tsx (the surface already exists with an Add action and Refresh but no dismiss). This is the single highest-value new signal.
- A disk-cached candidate pool (TTL 6-24h) keyed by DOI/arXiv id storing candidate metadata + abstract + cached embedding + source channel, so re-ranking is instant and offline-tolerant and arXiv is never hit in bursts. Treat as regenerable (add to _BACKUP_SKIP).
- DEFERRED, do NOT capture yet: impression / open / dwell-ms / click-out telemetry. These require instrumenting Electron reader focus/blur and card-render lifecycle hooks that do not exist, and only pay off once a learned model can consume them — which will not exist for many months. Ship dismiss+save first.

## Evaluation
Be honest about scale: a single user with ~30 papers and zero interaction history has no statistical power for the academic offline harness (leave-one-out recall@k/nDCG with as-of-T candidate reconstruction) or online A/B (McNemar/Wilson) — those are methodology theater here and are explicitly DEFERRED. Instead, evaluate at the level that actually works for one user: (1) Manual sanity bench during build — a tiny fixture of the user's real library run through the profile, asserting the threads separate sensibly (a 'protein folding' paper and an 'LLM agents' paper land in different clusters, not one midpoint) and that max-over-centroids beats single-mean on a hand-picked niche-interest case. (2) Lightweight live signal once Phase 6 ships — track save-rate (saves / recommendations shown) and dismiss-rate from the rec_events log as simple SQL aggregates the user can eyeball over time; a rising save-rate and falling dismiss-rate after dismisses are honored is the real-world 'it got better' signal. (3) Diversity watchdog — log the number of distinct threads represented in each served top-8 so we can confirm MMR is preventing single-topic collapse. (4) Regression guard — an in-repo test asserting the recommender never returns empty (cold-start blend) and never re-shows library/dismissed ids. Promote nothing on synthetic metrics; trust the user's save/dismiss behavior over months as the ground truth.

## Phased plan

### 1. Phase 1 — Multi-topic taste profile (the core) — effort M
**Goal:** Build the user model that everything else hangs off: cluster what the user actually reads into their few research threads, weighted by how strongly they engaged and how recently.

**What you get:** The recommender finally understands you have, say, three distinct interests instead of blurring them into one meaningless average — so a paper relevant to ANY of your threads scores high. Pure local computation, no network, no API cost, no new dependencies.

- New module src/himmy_app/recsys/profile.py: load ~30 records via Library.rag_records() + SavedNews.rag_records(); embed with the SHARED ToolkitConfig.from_env().build_embedder_and_dim() embedder (re-embed, do not read papers_index.db raw SQL).
- Signal weights in one dict (highlight 1.0 / note 0.8 / paper 0.5 / news 0.3 / interest 0.15) reading the kind tag papers_rag already writes; add explicit kind='news' to SavedNews.rag_records metadata for cleanliness.
- Exponential recency decay (half-life ~60d) using items.added_at / saved_news.saved_at / highlights.created; notes fall back to their paper's added_at (no schema change initially).
- Mean-pool vectors per source id, then pure-numpy k-means (~30 lines), K from a tiny range with hard K=1 fallback under ~5 docs. Memo-cache like papers_rag's _INDEX_CACHE, invalidated on the same record-keys signature.
- Reuse himmy's _cosine semantics so scores match the index.

### 2. Phase 2 — Cold-start blend — effort S
**Goal:** Make the recommender useful from day one and never return an empty list.

**What you get:** A brand-new user (or one with an empty corpus) still gets relevant recommendations from their typed interests, and that declared signal smoothly fades as real reading accrues — no jarring cliff when you add your fifth paper. Directly fixes today's bug where empty interests returns nothing.

- In recsys/profile.py, embed news.get_interests() (up to 24 terms) with the SAME embedder into one fading pseudo-centroid.
- Blend weight alpha = floor / (floor + total_decayed_corpus_weight); below ~5 docs skip clustering and rank off a single blended centroid.
- No new state — corpus weight is already computed in Phase 1.

### 3. Phase 3 — Citation-graph + arXiv candidate generation — effort M
**Goal:** Stop discovering papers only by 6 stale keywords; find papers like the ones you actually chose.

**What you get:** The biggest discovery-quality jump: foundational and follow-up work surfaces via the citation graph seeded from YOUR library, not a keyword string. Cached so it is instant and works offline, and never trips arXiv rate limits.

- New src/himmy_app/recsys/sources.py: keep the existing news._arxiv recency query (serialize with backoff); ADD an OpenAlex related_works / cited_by channel seeded from library DOIs/arXiv ids, reusing library.py httpx + DOI/arXiv regexes + the generic hello@himmy.app UA (never the personal email).
- Reconstruct OpenAlex abstract_inverted_index to text; candidates with no abstract degrade to title-only (down-weighted, not dropped).
- Disk-cached candidate pool (TTL 6-24h) keyed by DOI/arXiv id, added to _BACKUP_SKIP. Semantic Scholar /recommendations best-effort only (keyless rate limits).
- Parallelizable with Phases 1-2 (no dependency on the profile).

### 4. Phase 4 — Local ranking: max-over-centroids + MMR — effort M
**Goal:** Turn candidates + profile into a ranked, diverse, non-redundant top-8, entirely on the free local embedder.

**What you get:** Recommendations that span your different interests instead of 8 near-duplicates from your hottest topic, never re-show what is already in your library, and stay 100% free and offline-tolerant.

- recsys/ranking.py: batch-embed candidate title+abstract via the shared embedder; score = max cosine over thread centroids.
- Gate out ids already in Library.list() / SavedNews.urls() and (later) dismissed ids; dedup by DOI/arXiv-id, then normalized-title (reuse library.py _title_match/norm), then >0.93 cosine near-dup gate.
- MMR (lambda ~0.7) over the top ~30 for diversity + a couple of adjacent-cluster 'fresh direction' slots for serendipity.
- Optional cross-encoder rerank on the shortlist reusing papers_rag._maybe_reranker (cached, graceful fallback). Share the SAME cached embedder instance; tolerate it being unavailable offline like _maybe_reranker does.

### 5. Phase 5 — Wire the endpoint + UI, leave news For-You intact — effort M
**Goal:** Ship the new ranker behind the existing Recommended surface without regressing the news feed.

**What you get:** The Recommended tab you already have now shows genuinely personalized papers, with clear graceful-degradation states (offline / no interests / tiny corpus) and a working Refresh.

- New paper-recommender module/endpoint, leaving news._for_you() and the shared get_interests() RSS feed untouched (they share the interests list — must not regress).
- Point RecommendedMain (desktop/src/App.tsx, served via api.news.recommendations() / server.py GET /news/recommendations) at the new endpoint; touch App.tsx + api.ts + server.py.
- Add clear empty/offline/tiny-corpus states and a manual Refresh affordance (mostly already present in RecommendedMain).

### 6. Phase 6 — Dismiss control + minimal event log — effort M
**Goal:** Capture the one genuinely new, high-value signal: explicit negatives.

**What you get:** A 'not interested' button that actually teaches the recommender to stop showing you that kind of thing — the only true negative a content model can learn from. Survives reinstall because it lives in the backed-up library.db.

- Append-only rec_events table in library.db (kind in {dismiss, save}); tiny EventLog class mirroring SavedNews' sqlite pattern in news.py.
- Add a dismiss / 'not interested' control to RecommendedMain cards (App.tsx + api.ts + server.py).
- Feed dismissed ids into the candidate gate (hard exclude) and apply a small negative weight (~-0.4) to the nearest thread centroid.
- Backfill existing add/save/highlight as positive context if useful; do NOT build dwell/impression telemetry.

### 7. Phase 7 — Cached 'why recommended' blurbs (optional polish) — effort S
**Goal:** Explain each recommendation to build trust, without leaking cost.

**What you get:** Each card can show a one-line 'recommended because it cites a paper you highlighted' reason — template-generated for free, with optional LLM phrasing polish, cached so you pay for each blurb at most once and never when offline.

- Template-first reasons from the local data (nearest thread label via top-TF terms of cluster titles; 'cites a paper you highlighted'; 'a fresh direction').
- Optional gemini-2.5-flash polish reusing library._ai_extract's OpenRouter call shape (OPENROUTER_API_KEY, HIMMY_APP_MODEL, temp 0), at most ~8 tiny calls per refresh, cached by candidate id, skipped offline/keyless.

### 8. Phase 8 — DEFERRED: learned reranker + rigorous eval — effort L
**Goal:** Only after many months of real dismiss/save events accumulate.

**What you get:** Nothing now. Explicitly parked to avoid gold-plating: with ~30 papers and zero events there is no training data and a single user generates no traffic for A/B significance.

- Do NOT build: logistic-regression reranker with snapshot/promotion machinery, the leave-one-out / temporal-split offline eval harness, McNemar/Wilson A/B testing, epsilon-greedy/bandit exploration, or dwell/impression telemetry.
- Revisit only when the event log holds a few hundred labeled events; until then the centroid-cosine heuristic IS the model.

## Recommended first phase
Phase 1 — the multi-topic taste profile (src/himmy_app/recsys/profile.py). It is the confirmed core every other phase depends on, delivers the highest-leverage fix (multi-interest vs. averaged taste), reuses vectors and the kind tag the app already produces, needs no network and no new dependencies (pure-numpy k-means since sklearn is absent), and re-embedding the ~30 records sidesteps both schema coupling and index-lock contention. Build it first, then Phase 2 (cold-start) immediately after so the feature is never empty.

## Cut as over-engineered
- The full learned re-ranker (logistic-regression-over-features, periodically re-fit, with persisted coefficient snapshots, trained_at/n_samples auditing, and (1-beta) online-centroid + beta-learned-weights two-timescale blend) — Design 3. With ~30 papers and ZERO interaction events today, there is no training data; it cannot be fit until ~40+ labeled events exist (the design itself admits this and gates it behind a heuristic). Building the fit/snapshot/promotion machinery now is pure gold-plating; the centroid-cosine heuristic IS the model for the foreseeable future. Defer entirely.
- The offline leave-one-out / temporal-split evaluation harness (recall@k/MAP/nDCG via himmy retrieval_eval, rec_eval_runs table, as-of-T candidate reconstruction) — Design 3. Reconstructing candidates 'as of time T' by re-querying arXiv/OpenAlex with a date ceiling is both unreliable (APIs don't cleanly reproduce a historical candidate set) and rate-limit-heavy, and with a handful of positives the metrics have no statistical power. This is methodology theater for a single-user app.
- A/B testing between reranker snapshots with McNemar + Wilson intervals + 50/50 session-hash interleaving + topic-entropy watchdog (Design 3). A single user generates no traffic for paired significance testing; there is nothing to A/B. This is the most clearly mis-scoped layer in any of the three designs.
- The full impression/open/dwell-ms/click_out event telemetry suite (Designs 1 and 3). dwell_ms and impression-without-open require instrumenting Electron reader focus/blur and card-render lifecycle that don't exist as hooks today; these signals only pay off once there's a learned model to consume them — which there isn't. Ship only the explicit dismiss + save events first; defer dwell/impression telemetry until a model can actually use them.
- epsilon-greedy / UCB / Thompson exploration framing (Designs 2 and 3). The serendipity GOAL is right, but MMR diversity + a couple of 'fresh direction' slots from an adjacent cluster already deliver it deterministically. Bandit machinery presupposes a feedback loop and reward signal that don't exist yet; an explicit epsilon-exploration bandit is premature.
- Per-highlight 'more like this' as a SEPARATE OpenAlex /works?search= recall channel (Design 2). Highlights are already upweighted into the thread centroids and already embedded in the index; firing an extra rate-limited API search per highlight is marginal recall for added API load and complexity. Fold highlight strength into centroid weighting instead.
- Venue/author 'follows' as its own recall channel with OpenAlex id resolution + caching (Design 2). Reasonable eventually, but it's a fourth recall channel on top of arXiv + citation-graph + per-cluster queries; for ~30 papers it adds plumbing for little marginal discovery. Defer until the core two-channel recall is proven.
- Novelty 'banding' as a distinct scored term separate from dedup (Design 2). The >0.93 cosine dedup against the library already removes the user's own papers/near-dupes; a separate mid-band down-weight term is fine-tuning that adds a tunable knob with little payoff at this scale. The hard dedup gate is enough initially.
- A dedicated recsys.db as a NEW third store (Design 3). Confirmed _BACKUP_SKIP only excludes papers_cache.db/papers_index.db/news_cache.json, so a new recsys.db would silently NOT be backed up — meaning dismissals (hard to recreate) would be lost on reinstall. Put events/state in library.db (already backed up) instead of inventing a new unbacked-up DB.

## Feasibility risks (designed around)
- sklearn is NOT installed in the app venv (verified: only numpy 2.4.6, no scikit-learn/scipy). Both Design 1 (silhouette KMeans) and Design 2 (sklearn KMeans, TF-IDF/KeyBERT keyphrasing) name sklearn as the primary path. Either add a heavy new dependency (against the lean local-first ethos) or commit up front to the pure-numpy k-means fallback. TF-IDF cluster labels also need a hand-rolled implementation, not sklearn.
- No public per-chunk vector-read API on SqliteKnowledgeBackend (methods are search/dense_ranked/get_chunk/list_document_identities/get_document — no 'list all chunk embeddings'). Reading stored vectors means raw SQL against the internal knowledge_chunks.embedding JSON column (couples the recommender to himmy's private schema, which could change), OR re-embedding the ~30 records each run. Re-embed is recommended at this size but the designs' 'read vectors directly' claim overstates the available API.
- Cold-start is acute and real: ~30 papers, and (verified) recommendations() returns {papers:[]} when the interests list is empty — so a brand-new user gets nothing. Clustering 30 mean-pooled doc vectors into 3-6 topics is statistically thin; K must scale down hard and fall back to K=1 / single blended centroid below ~5 docs.
- NO negative signals exist anywhere today (confirmed: no read/open/dwell/dismiss tracking). Every design's negative-handling (push centroid away on dismiss, label 0 for dismiss in the reranker) is inert until the dismiss control ships and the user actually uses it. The recommender is positive-only on day one regardless of design ambition.
- arXiv rate-limits rapid bursts and the current single _arxiv call already sits at that edge; the citation-graph + per-cluster-query expansion multiplies outbound calls. Must serialize arXiv with backoff and route bulk recall to OpenAlex, with disk caching, or risk throttling/blocking.
- OpenAlex returns abstracts as an inverted index needing reconstruction, and some works have none; candidates with no abstract degrade to title-only embedding (weaker). Semantic Scholar's keyless tier rate-limits hard — relying on its /recommendations as a primary channel is fragile; keep it best-effort.
- OpenRouter gemini-2.5-flash is metered and the user is cost-sensitive: any path that lets the LLM touch ranking, or regenerates blurbs on every render, leaks cost. Blurbs must be cached-by-id, capped to shown cards, and fully skippable offline/keyless.
- Concurrency with the live persisted index: papers_index.db is written by the RAG ingest path (background prewarm + auto-refresh on library change) using a shared SqliteKnowledgeBackend. A recommender reading that DB via raw SQL concurrently risks 'database is locked' contention. Re-embedding from library.db (read-only on the index) sidesteps this; raw-SQL vector reads need the same timeout/retry care papers_rag already applies to papers_cache.db.
- Offline tolerance: the whole candidate-generation layer (arXiv/OpenAlex/S2) is network-dependent. The ranker must degrade to a cached candidate pool when offline, and the feature must never error out or go empty — it should fall back to today's behavior.
- items.notes has no timestamp column (confirmed). Recency decay for the 'note' signal needs either a small ALTER TABLE adding notes_updated_at set in set_note, or a fallback to the paper's added_at. The approximation is acceptable to start; the schema change is the cleaner eventual fix.