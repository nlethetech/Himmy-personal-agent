# Himmy app — capability-gap roadmap (himmy framework → Daybook/Himmy app)
_Generated 2026-06-23 from a 7-agent mapping workflow over himmy-agent-test + scholar-desk._

## Headline
Stand up himmy's already-built Routines scheduler inside the app FIRST — it's the one thing that lets anything fire on a timer (briefings, digests, reminders, upkeep), and the framework ships the whole subsystem so it's mostly wiring, not new engine. Sequence everything else by impact-to-effort, correcting the proposal where the work is already done: the privacy guardrail pipeline is ALREADY live (only the reversible DLP vault + a 'what was redacted' badge remain), and the RAG embedder is ALREADY semantic (the real win is just persisting the index). Add the capabilities the proposal missed: whole-workspace backup (today only papers are backed up — a real data-loss risk once routines/memory accumulate), proper connectors, drop-a-PDF metadata extraction, and feeding the user's own highlights into search.

## Build first
Wire himmy's in-process RoutineScheduler into the FastAPI app via a new lifespan in create_app() (server.py:175 — confirmed NO lifespan exists today; app=create_app() at server.py:826 is a plain FastAPI), pin HIMMY_ROUTINES_PATH into the .scholar-desk data dir in config.py (mirroring the existing HIMMY_STORE_PATH/HIMMY_MEMORY_PATH/HIMMY_TASKS_PATH/HIMMY_CONVERSATIONS_PATH setdefaults at config.py:73-84), add 'cron' + set HIMMY_TZ=Asia/Kathmandu, and add a thin Routines CRUD delegating to get_routines_store()/get_scheduler() (NOT the multi-tenant /v1 router). Nothing else in the automation chain can fire until this exists. PRECEDE it with a one-routine spike (an 'every 5 min' test) to verify resolve_canonical_storage() + studio_service.stream_agent_run actually run end-to-end through agent/agent.yaml inside this custom non-BFF app — that chain has never run here.

## Recommended first sprint
1. VERIFY-FIRST SPIKE: seed ONE Routine bound by agent_path to agent/agent.yaml on a kind='every', minutes=5 schedule and confirm it fires through the real agent (resolve_canonical_storage + studio_service.stream_agent_run) and lands a result — this de-risks the whole effort estimate before building UI.
2. FOUNDATION: add the FastAPI lifespan to create_app() (get_scheduler().start() + await catch_up_on_launch() on startup, await stop() on shutdown), pin HIMMY_ROUTINES_PATH into .scholar-desk in config.py, add 'cron' to the himmy extras in requirements.txt and set HIMMY_TZ=Asia/Kathmandu — all in ONE change. Then add ~5 endpoints (GET/POST /routines, PUT/DELETE /routines/{id}, POST /routines/{id}/run-now) calling notify_change() after every write, plus a Routines sub-tab in the Planner.
3. PARALLEL QUICK WIN (no scheduler dependency, effort=S): persist the RAG index by passing backend=SqliteKnowledgeBackend(<data_dir>/papers_index.db) in papers_rag.py:183-184 (change ONLY backend=None — do NOT touch the embedder; it already uses build_embedder_and_dim() so recall is already semantic). Kills the 2-3 min cold-start re-embed.
4. Widen backup() in library.py:558-568 to a whole-workspace zip (conversations.db, memory.db, tasks.db, usage.json, storage.db, and the new routines.db) and make restore non-destructive — do this BEFORE routines/memory state accumulates, so the first automation user isn't a data-loss case.
5. Build the native macOS notification + in-app inbox (electron Notification is confirmed unused in main.cjs) so the FIRST scheduled briefing is actually seen, not silently logged — this must precede any user-facing automation payoff.

## Scheduling / automation features

### Wire the in-process RoutineScheduler into the app + a Routines CRUD (THE FOUNDATION) — effort M / impact high
**What you get:** The app can run saved automations on a schedule while it's open: 'every weekday 6:30am summarize my new arXiv papers', 'every 6 hours check for new citations', 'July 1 at 9am remind me to renew'. Until this exists, NOTHING in the app can fire on a timer — every other automation below sits on this.

**How:** Add a FastAPI lifespan to create_app() at server.py:175 (confirmed NO lifespan today; app=create_app() at :826 is plain FastAPI()). On startup: get_scheduler().start() + await catch_up_on_launch(); on shutdown: await stop(). Pin HIMMY_ROUTINES_PATH into .scholar-desk in config.py next to the existing setdefaults at config.py:73-84 (get_routines_store() resolves .himmy/routines.db relative to cwd, and the Electron-spawned backend's cwd is not guaranteed stable, so without this pin saved automations silently 'disappear' across launches). Add ~5 thin endpoints delegating to get_routines_store() and (for run-now) await get_scheduler().run_now(id); call notify_change() after every CRUD write. Copy the start/notify/catch_up wiring from himmy's studio_routines.py _wake_scheduler helper but do NOT mount /v1 (RBAC/multi-tenant machinery the single-user app doesn't use). INTEGRATION RISK to verify first (routines.py:1140-1210): the single-user agent_path path runs through studio_service.stream_agent_run + resolve_canonical_storage(), NOT the durable RunDispatcher, so it can skip the worker — but that chain has never run inside scholar-desk's custom non-BFF app, so spike one test routine first. load_studio_spec honors tools_module so the app's custom tools (ask_papers, calendar_*) WILL be available.

**Files:** /Users/samriddhagc/LocalProjects/scholar-desk/src/himmy_app/server.py, /Users/samriddhagc/LocalProjects/scholar-desk/src/himmy_app/config.py, /Users/samriddhagc/LocalProjects/scholar-desk/desktop/src/lib/api.ts, /Users/samriddhagc/LocalProjects/scholar-desk/desktop/src/App.tsx

### Add the 'cron' extra + Kathmandu timezone (PREREQUISITE, not a footnote) — effort S / impact high
**What you get:** Schedules expressed as real calendar rules ('weekdays at 6:30am', 'the 1st of every month') actually fire — and they fire at Nepal local time, and a job missed while the Mac slept fires exactly once on wake instead of being skipped or backfilled in a storm.

**How:** requirements.txt today is himmy[toolkit,api,openai,embeddings] — NO cron (confirmed). Add 'cron' to the extras. Set HIMMY_TZ=Asia/Kathmandu (the user is in Nepal) so '30 6 * * 1-5' and 'daily 07:00' mean local time. Rely on missed='coalesce' for laptop-sleep safety. This must land in the SAME change as the foundation, because the briefing/reminder routines below would otherwise be seeded with cron strings that never fire. (kind='every'/'daily' work with zero extra deps, so the reminders routine is safe even without this — but any cron grammar exposed in the UI needs the extra.)

**Files:** /Users/samriddhagc/LocalProjects/scholar-desk/requirements.txt, /Users/samriddhagc/LocalProjects/scholar-desk/src/himmy_app/config.py

### Native macOS notification + in-app inbox for routine results — effort M / impact high
**What you get:** When a scheduled briefing/digest/reminder finishes, the user gets a real macOS notification and an inbox badge in-app — so automations are actually SEEN, not silently logged. Build this BEFORE reminders/upkeep, or those run invisibly. Also surfaces 'routine needs approval' when an automation hits a gated tool, parking it safely instead of acting unattended.

**How:** Routines write their result into a new 'inbox' table; expose GET /notifications. In main.cjs add ipcMain.handle('notify:show', ...) using electron's Notification (confirmed UNUSED today — main.cjs only spawns/kills the backend) + a preload.cjs method (window.himmy.notify). The renderer polls /notifications (mirroring the existing 5s/15s polls in App.tsx) and on a new entry calls window.himmy.notify(...) and shows a toolbar badge. Approval-gated runs park as awaiting_approval and route into the existing HITL UI (ApprovalCard, /ask/resume).

**Files:** /Users/samriddhagc/LocalProjects/scholar-desk/desktop/electron/main.cjs, /Users/samriddhagc/LocalProjects/scholar-desk/desktop/electron/preload.cjs, /Users/samriddhagc/LocalProjects/scholar-desk/src/himmy_app/server.py, /Users/samriddhagc/LocalProjects/scholar-desk/desktop/src/App.tsx

### Scheduled morning briefing + daily news digest routine (the flagship experience) — effort M / impact high
**What you get:** A 'Daily Briefing' that runs unattended (weekdays 6:30 Kathmandu time): new papers in the library, top saved news, today's calendar, open/overdue tasks — ready and waiting when the user opens the app. The signature recurring experience for a personal research workspace.

**How:** Seed a built-in 'Daily Briefing' Routine via get_routines_store().upsert() pointing at agent/agent.yaml with a prompt that uses existing tools (ask_papers, list_tasks, gmail_inbox, calendar_find). Use Schedule(kind='cron', expr='30 6 * * 1-5', timezone='Asia/Kathmandu', missed='coalesce'). REQUIRES the 'cron' extra (above). Persist each result into the inbox table so the Today home grid renders the latest in a Card (Today() around App.tsx:340).

**Files:** /Users/samriddhagc/LocalProjects/scholar-desk/src/himmy_app/server.py, /Users/samriddhagc/LocalProjects/scholar-desk/src/himmy_app/news.py, /Users/samriddhagc/LocalProjects/scholar-desk/desktop/src/App.tsx

### Real task due-dates + reminder routine (act on the dead `due` column) — effort M / impact med
**What you get:** Tasks get a real date picker and the app actually reminds the user when something is due or overdue — today the `due` column exists in the store and is returned to the UI but NOTHING reads or fires on it, and add_task only accepts a title.

**How:** (a) add_task takes only `title` (agent_tools.py) and there's no /tasks edit endpoint — add a due param to the create path and a PUT /tasks/{id} reschedule endpoint, plus a date picker in TaskRow (App.tsx ~1819; isOverdue at App.tsx:559 already best-effort parses, waiting for real data). (b) Seed a 'Reminders' Routine (kind='every', hours=1) that lists tasks and flags due/overdue ones, delivering via the inbox/notification channel. Use kind='every' (NOT cron) so it fires with zero extra deps.

**Files:** /Users/samriddhagc/LocalProjects/scholar-desk/src/himmy_app/agent_tools.py, /Users/samriddhagc/LocalProjects/scholar-desk/src/himmy_app/server.py, /Users/samriddhagc/LocalProjects/scholar-desk/desktop/src/App.tsx

### Periodic library enrichment + new-citation routine — effort M / impact med
**What you get:** The library quietly keeps itself tidy: papers missing metadata get auto-enriched and the user is periodically alerted to new arXiv recommendations matching their interests — set-and-forget upkeep instead of manual per-paper enrich clicks.

**How:** Seed a 'Library upkeep' Routine (kind='every', hours=6) that scans library items lacking metadata and calls the existing enrich path (library.py POST /library/{id}/enrich), plus a leg pulling news recommendations (news.py / api.news.recommendations) to flag new arXiv candidates. Deliver via the inbox channel. enrich depends on OPENROUTER_API_KEY (silently no-ops without it), so the routine should skip gracefully when absent. Optionally add a save-to-library shortcut endpoint (recommendations currently have none and no dedup against existing items).

**Files:** /Users/samriddhagc/LocalProjects/scholar-desk/src/himmy_app/server.py, /Users/samriddhagc/LocalProjects/scholar-desk/src/himmy_app/library.py, /Users/samriddhagc/LocalProjects/scholar-desk/src/himmy_app/news.py

### Always-on background worker (EITHER/OR upgrade, not an additive layer) — effort M / impact med
**What you get:** Scheduled briefings/digests/reminders keep firing even when the app window is closed (e.g. an overnight digest is ready at 7am whether or not the window was open), because timing no longer depends on the FastAPI process staying alive.

**How:** IMPORTANT — this is an ALTERNATIVE firing mechanism, not a consumer of the in-process scheduler: the worker brings up its OWN RoutineScheduler, so it's worker OR the in-process loop, not both stacked. Option A: spawn `.venv/bin/python -m himmy worker --store <data-dir>/runs.db` in main.cjs next to the existing backend spawn (main.cjs:77), kill on before-quit (main.cjs:135). Option B (survives full quit): a macOS launchd job running `himmy routines run-now <id>`; the cross-process flock prevents collision. The in-process scheduler (foundation) already covers 'app is open', so this is the 'machine asleep / app closed' upgrade — lower priority.

**Files:** /Users/samriddhagc/LocalProjects/scholar-desk/desktop/electron/main.cjs, /Users/samriddhagc/LocalProjects/scholar-desk/requirements.txt

## Other features

### Persist the papers RAG index with SqliteKnowledgeBackend (kill the cold-start rebuild) — effort S / impact high
**What you get:** 'Ask my library' works instantly on launch instead of spinning 2-3 minutes re-embedding every PDF. The knowledge base survives restarts; only genuinely new/changed papers get embedded thanks to content-hash dedup.

**Uses:** himmy KnowledgeBase backend=SqliteKnowledgeBackend(path) — the offline twin of pgvector; its docstring explicitly names this Daybook ~2-min re-embed problem

**How:** CORRECTION to the proposal: papers_rag.py:177/183-184 ALREADY uses ToolkitConfig.from_env().build_embedder_and_dim() (NOT a hardcoded DeterministicEmbedder), and himmy[...,embeddings] ships fastembed, so recall is already semantic — do NOT swap the embedder (risk of regression for no gain). The ONLY accurate change is backend=None -> SqliteKnowledgeBackend(<data_dir>/papers_index.db) so embeddings+chunks+fingerprint persist and content-addressed ingest skips unchanged papers next launch. KB_WORKSPACE_ID/KB_CLIENT_ID/KB_NAME already keep the KB stable by name. Optionally bind api.index(force) (already in api.ts:223 but UNwired) to a 'Rebuild knowledge base' button.

### Whole-workspace backup + restore (NOT just library.db + PDFs) — MISSED by the proposal — effort S / impact high
**What you get:** A real 'back up everything' that protects conversations, durable memory, tasks, usage history, and saved routines — not just papers. Today backup() zips ONLY library.db + library_files/ (verified library.py:558-568) and restore is destructive, so a sync mishap silently loses chat history, remembered facts, and the task board.

**Uses:** None — pure app-side change, no framework dependency, high-trust payoff

**How:** Widen backup() (library.py:565-568 currently writes only library.db + library_files/*) to also include conversations.db, memory.db, tasks.db, usage.json, storage.db, and the new routines.db. Make restore() non-destructive (back up current state before overwrite). SEQUENCE THIS EARLY — the moment Routines/self-learning/memory-consolidation start accumulating valuable durable state, the library-only backup becomes a real data-loss risk; the first scheduled-automation user should not be a data-loss case.

### Memory auto-injection + consolidation (silent cross-session recall) — effort S / impact med
**What you get:** Himmy stops being a stateless palette and quietly recalls the user's projects, preferences, and prior conclusions every turn — without being asked to recall — and stops storing duplicate facts.

**Uses:** himmy MemoryContextAdapter (auto-injects recalled memories with a noise floor) + MemoryConsolidator (ADD/UPDATE/DELETE/NOOP instead of blind append). The app already wires register_memory_pack but uses neither.

**How:** The memory pack is already registered in agent_tools.py against .scholar-desk/memory.db (HIMMY_MEMORY_PATH set in config.py). (1) construct MemoryContextAdapter(memory, top_k=, tiers=) and register it on the ContextService in cli.py's runtime build so the agent sees relevant prior facts each turn with no explicit recall call; set memory_subject per-workspace. (2) Enable memory_consolidate in ToolkitConfig so re-stating a fact updates rather than duplicates. Both offline and fail-open.

### Constrained-decoding metadata extraction for the Library (drop-a-PDF -> guaranteed-valid fields) — MISSED by the proposal — effort S / impact med
**What you get:** Dropping a PDF auto-fills a fillable title/authors/venue/year/DOI form that always parses, and citation objects that never break — replacing the current 'AI reads page 1 as prose' enrich path with schema-constrained output.

**Uses:** himmy structured output (CONSTRAINED_DECODING_KEY + output_json_schema, fail-open, maturity=built)

**How:** library enrich currently has the model read page 1 and parse prose. Add output_json_schema + metadata={'constrained_decoding':true} to the enrich call so title/authors/venue/year/DOI come back as a guaranteed-valid object instead of fragile free text. This is the highest-value application of constrained decoding (the proposal buried it as an aside under task due-dates). Pairs with a fillable form in the library detail UI.

### Surface PDF highlights/annotations into the durable RAG index — MISSED by the proposal — effort S / impact med
**What you get:** ask_papers and Deep Research can cite the user's OWN margin highlights and notes — the highest-signal text in the library — not just raw paper text.

**Uses:** himmy KnowledgeBase ingest (on the now-durable SqliteKnowledgeBackend)

**How:** Today highlights are NOT surfaced to RAG (only the item-level note is). Once the durable backend lands, ingest each highlight/annotation as an additional document (tagged to its paper) in papers_rag.py. Small change, outsized retrieval-quality impact. Depends on the RAG-persistence feature.

### Thumbs up/down on answers feeding self-learning — effort S / impact med
**What you get:** A simple thumbs up/down on each assistant answer measurably shifts which tools Himmy prefers next time — flaky tools/connectors get deprioritized and the assistant adapts to this one user.

**Uses:** himmy OutcomeRecorder.record(score, source=USER_FEEDBACK) feeding the already-ON self-learning loop (LearningService + ToolReputationProvider)

**How:** CLARIFICATION: the self-learning ENGINE is already on (self_learning: true confirmed at agent.yaml:156; LearningService/ToolReputationProvider/LearnedHintsContextAdapter already mine TOOL_FAILED/TOOL_COMPLETED). The genuinely-unwired delta is only the OUTCOME_SCORED user signal: set outcome_weight>0 in agent.yaml, add POST /feedback {session_id,turn,score} calling OutcomeRecorder.record(score, source=OutcomeSource.USER_FEEDBACK) against the same SqliteSessionStore the app uses, and add a thumbs control to the chat Bubble (App.tsx ~2882).

### Reversible DLP vault + 'what was redacted' badge (RESCOPED — guardrails are ALREADY live) — effort S / impact med
**What you get:** The user sees a small 'redacted N items' indicator and can recover the original text — peace of mind that personal info handling is visible and reversible.

**Uses:** himmy SqliteTokenVaultBackend (local, reversible DLP) + surfacing GuardrailTriggers

**How:** MAJOR CORRECTION to the proposal: PII redaction, injection screening, AND tool-arg redaction are ALREADY ACTIVE TODAY. Verified at from_spec.py:281-298 — build_runtime_for_spec already constructs input/output guardrail pipelines from spec.guardrails plus pre/post tool-execution hooks (build_guardrail_pre_hook/post_hook at lines 510-511), and agent.yaml:142-144 declares guardrails: [pii, injection]. The proposal's 'redact before prompts leave for the cloud' work is DONE. The genuinely-unwired delta is ONLY: (a) the reversible DLP TokenVault — use SqliteTokenVaultBackend(<data_dir>/vault.db); and (b) a UI 'what was redacted' indicator surfacing GuardrailTriggers in the chat. Much smaller than the proposal implied.

### Declare paper APIs as ConnectorSpec YAML + a managed 'Connections' settings panel — MISSED by the proposal — effort M / impact med
**What you get:** A trustworthy Connections settings surface (enable/test/status per integration) where paper-API and side-effecting tools get SSRF guarding, secrets-layer credentials, rate limiting, idempotency, and requires_approval HITL for free — instead of today's ad-hoc hand-rolled connectors.

**Uses:** himmy CONNECTORS framework (ConnectorSpec YAML / OutboundToolConnector SDK + ConnectorService catalog, maturity=built)

**How:** scholar-desk hand-rolls connectors via safe_register_local_tool, bypassing the SDK and losing SSRF/secrets/rate-limit/idempotency/HITL. The arXiv/Crossref/Semantic-Scholar/Unpaywall REST integrations already in library.py can be re-expressed as ConnectorSpec YAML, and ConnectorService can back a 'Connections' Settings UI. Lowest-effort framework-native path; entirely absent from the proposals.

### Deep Research council (multi-agent gather->draft->critique->revise) — effort L / impact med
**What you get:** A more capable 'Deep Research' mode that fans a question across specialist sub-agents and shows intermediate reasoning, plus long reports that survive an app restart and can be resumed.

**Uses:** himmy MultiAgentOrchestrator (handoff/delegate teams) + StateGraph (durable interrupt/resume on the already-wired SqliteCheckpointStore)

**How:** research.py already runs PlannerOrchestrator + reflect via _build_runtime. Upgrade to a MultiAgentOrchestrator AgentTeam: a 'searcher' (ask_papers + web), an 'analyst' synthesizer, and a 'critic'. Model long reports as a StateGraph with NodeInterrupt so partials persist across restarts. The web leg is keyless DuckDuckGo and usually empty without HIMMY_SEARCH_API_KEY — pair with a search-key setting. Persist briefs (currently saved nowhere) so past research is browsable.

### Time-travel + related-facts knowledge graph over memory — MISSED by the proposal — effort M / impact low
**What you get:** A 'what did I believe as of <date>' view and a related-items panel surfacing linked notes/papers/tasks for whatever the user is viewing — turning memory from a flat fact list into a navigable graph.

**Uses:** himmy memory bi-temporal recall (as_of/active_only) + typed MemoryLink graph + traverse_graph + recall(max_hops=)

**How:** The memory-auto-injection feature adopts only the adapter + consolidation; this adds the temporal/graph layer. Use as_of point-in-time recall for the time-travel view and MemoryLink/traverse_graph for a related-facts panel (paper->note->task). A distinctive research-desk feature; build after memory auto-injection.

### In-library full-text/semantic search surface (distinct from the chat agent) — effort S / impact low
**What you get:** A 'search everything I've read' surface over the durable knowledge base — direct semantic/full-text search of the library, not just chat. Today list() filters only title/author/venue/tags; full-text is ONLY via the RAG agent.

**Uses:** himmy KnowledgeBase.search over the now-durable SqliteKnowledgeBackend

**How:** Once the RAG index is persisted, expose a GET /library/search endpoint backed by KnowledgeBase.search and a search box in the Library tab — nearly free given the durable backend. Depends on the RAG-persistence feature.

### Activity & Provenance timeline (EntityRecord/RunEvent audit spine) — effort M / impact low
**What you get:** A trustworthy 'what Himmy did' timeline: tools called, what it read, what it remembered, why it chose an answer, with per-answer source links.

**Uses:** himmy EntityRecord audit spine + RunEvent stream (TOOL_*, OUTCOME_SCORED, MEMORY_*, LEARNING_APPLIED) read off the storage.db the app already has

**How:** storage.db already exists in .scholar-desk and SqliteSessionStore is wired. Pass an EntityRegistry into MemoryService so memory writes project onto the spine, read the RunEvent stream to build GET /activity, render as a read-only Planner sub-tab (the sub-tab array at App.tsx:1415). EntityLinks relate a derived note to its source paper and the run that produced it. Mostly observability over data already being generated.

### Regression benchmark suite for the research agent (internal eng gate) — effort M / impact low
**What you get:** Confidence that any prompt/model/tool change actually improves the assistant before it ships, plus a principled way to pick the smallest local model that still answers well (matters for an offline-first desktop app).

**Uses:** himmy BenchmarkRunner + graders (arg-level/trajectory) + judge + stats (`himmy bench`) or AgentEvalHarness.evaluate_agent

**How:** Author a small in-repo YAML suite (ask_papers retrieval, citation formatting, calendar/gmail tool selection, 'should NOT call a tool' irrelevance cases) and run BenchmarkRunner against the app's configured model. Use as a check before changing agent.yaml/prompts/tools and to compare candidate local models. Not user-facing — high developer-velocity value, cheap to stand up.

## Verification notes
All critique corrections verified against the codebase: (1) requirements.txt is himmy[toolkit,api,openai,embeddings] with NO cron extra; (2) server.py has create_app() at :175 and app=create_app() at :826 with NO lifespan and zero scheduling code; (3) config.py:73-84 has the existing HIMMY_STORE/MEMORY/TASKS/CONVERSATIONS_PATH setdefaults but NO routines pin; (4) papers_rag.py:177/183-184 ALREADY uses build_embedder_and_dim() with backend=None (so the proposal's embedder-swap is wrong — only backend persistence is needed); (5) agent.yaml:142-144 declares guardrails: [pii, injection] and :156 self_learning: true (so the privacy pipeline is already live — only the DLP vault + redaction badge remain); (6) library.py:558-568 backup() zips only library.db + library_files/ and restore is destructive (the missed whole-workspace-backup risk). Two things I could not fully verify without deeper inspection and flagged as VERIFY-FIRST in the roadmap: that resolve_canonical_storage() + studio_service.stream_agent_run run end-to-end through agent_path inside this custom non-BFF app (the spike in sprint item 1 de-risks the foundation's effort estimate), and that electron's Notification import works in main.cjs (confirmed unused, not confirmed importable). The worker (always-on) feature was re-labeled as an EITHER/OR alternative to the in-process scheduler, not an additive dependsOn layer, per the critique — running both means two schedulers (flock-guarded but redundant).