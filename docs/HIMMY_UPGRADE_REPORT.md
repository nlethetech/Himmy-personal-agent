# Daybook + Himmy Upgrade Report

*Plain-English build report. Date: 2026-06-18.*

This build pushed Daybook much deeper into the Himmy agent framework it runs on. Before, Daybook used only a thin slice of what Himmy can do. We turned on several high-value capabilities and added two brand-new tabs (Tasks, Mail/Calendar) plus a real chat experience for the Cmd-K assistant. The app still boots cleanly and nothing that worked before is broken.

---

## A. Himmy capability audit — what we had vs. what was sitting unused

**The verdict:** Before this build, Daybook used only about **10–15%** of Himmy. We had the core single-agent loop, 8 tools (your papers/RAG search + web + memory + small utilities), durable memory, and 2 input safety guardrails. A large, high-value chunk of the framework was switched off.

**What Daybook already used before this build:**
- Papers RAG — searching your own library (`ask_papers` / `index_papers`)
- Durable memory — Himmy remembering facts across chats (`remember` / `recall`)
- Web search and web page fetch (`web_search` / `web_fetch`)
- Calculator and current-time utilities
- Two input guardrails (PII detection + prompt-injection protection)
- Durable storage + the audit "spine" that records what the agent did

**Confirmed opportunities we found (and acted on):**
- Self-learning (the agent learning which tools are reliable)
- Auto-compaction (keeping long chats fast and on-budget)
- A sharper second-pass ranking of search results (cross-encoder reranker)
- Persistent chat with a history/resume sidebar
- Deep-research mode (plan → fan out → synthesize → reflect)

**Honest corrections from verification (important nuances):**
- **Mail/Calendar (Google pack) is real and production-ready**, but *safely* sending mail or booking events needs a human-approval step ("HITL") that Daybook does not yet run. So **read-only** Mail/Calendar is the clean win right now. (Note: `requires_approval` is not a real field in the agent config file, so this isn't a quick toggle.)
- **Streaming is real**, but on the OpenAI-compatible path (Daybook uses OpenRouter, not Anthropic). It also needs a dedicated streaming endpoint and front-end consumer — not a one-line switch.
- **Grounding/citation guardrail is already forced on** by Himmy automatically; adding it to the config would do nothing. Per-answer citation scoring would be net-new future work.
- **The Tasks pack** needs both code registration *and* the tool names added to the agent's allowlist *and* a Tasks UI — all three, which we did.
- **Library "recent papers" filtering is limited**: the year is stored as text and matched exactly, so true "recent N years" ranges would need new search parameters.

---

## B. What we built

### 1. Reliability upgrades (self-learning, auto-compaction, smarter search ranking) — **PARTIAL**

**Files changed:** `agent/agent.yaml`, `src/scholar_desk/connectors/papers_rag.py`

**What it does:** Two of three upgrades are live and verified; one was correctly left OFF.
- **Auto-compaction (DONE):** Long Himmy conversations now automatically trim older turns while keeping the 6 most recent exchanges verbatim, so chats stay fast and within budget.
- **Smarter search ranking (DONE):** When Himmy answers questions about your library, it now does a sharper second-pass ranking of passages (a "cross-encoder reranker"), so cited sources are more relevant. It loads the ranking model once (first library search after restart is a few seconds slower), then caches it. If the model can't load, it falls back gracefully to the normal search — it can never break search.
- **Self-learning (LEFT OFF on purpose):** Turning this on reproducibly broke *every* request with an `asyncio.run() cannot be called from a running event loop` error. The cause is a bug **in the Himmy library, not Daybook** (it tries to prime tool-reputation with a blocking call during agent build, which crashes inside Daybook's running server). Per the rules, we did **not** edit the shared Himmy library; we reverted the flag to `false` with an explanatory comment so the app stays fully working.

**How to use it:** Nothing for you to do — the agent already uses the new settings automatically.

**Blocker:** Self-learning needs a fix inside the Himmy framework first (priming reputation on the async path). Out of scope for this additive Daybook task.

### 2. Tasks tab — **DONE**

**Files changed:** `src/scholar_desk/config.py`, `src/scholar_desk/agent_tools.py`, `agent/agent.yaml`, `src/scholar_desk/server.py`, `desktop/src/lib/api.ts`, `desktop/src/App.tsx`

**What it does:** The Tasks tab is now real and shares **one task list** with the Himmy assistant. Add tasks, check them off (they strike through, turn green, and sink to the bottom), and delete on hover. The tab auto-refreshes every 4 seconds, so tasks you add via the Cmd-K command bar appear on their own.

**How to use it:** Open the Tasks tab, type a task, press Enter. Click the circle to complete; hover and click the trash to delete. You can also tell Himmy: *"add 'finish my literature review' to my tasks"*, *"what's on my list"*, or *"mark X as done"* — changes show up in the tab within seconds.

**Blocker:** None. Verified end-to-end on the real model — the agent's `add_task` / `list_tasks` / `complete_task` and the app's Tasks tab all hit the same database. (The Tasks UI data path and types are verified; it was not pixel-clicked in a live Electron window.)

### 3. Cmd-K assistant: persistent chat + streaming + history sidebar — **DONE**

**Files changed:** `src/scholar_desk/config.py`, `src/scholar_desk/cli.py`, `src/scholar_desk/server.py`, `desktop/src/lib/api.ts`, `desktop/src/App.tsx`

**What it does:** The Cmd-K palette (Himmy) now **remembers conversations**, **streams its reply progressively** instead of popping in all at once, and has a **history sidebar**. The old instant-answer path still works as a fallback.

**How to use it:** Press Cmd-K. Ask anything — the reply streams in and the conversation is remembered. Click the panel icon (top-left) to open history: each past chat shows a title and message count; click to resume, pencil for a new chat, trash to delete. Your active chat survives app refreshes. For tool-using questions (reading papers, remembering facts), expect ~30s — you'll see bouncing dots, then the text reveals.

**Honest note on streaming:** This agent runs a *blocking* safety guardrail on its output, so Himmy delivers the guard-cleared answer and we reveal it progressively as small word-groups client-side. The provider does stream natively under the hood; under a looser guard, raw per-token streaming would flow straight through. Verified live: multiple time-separated token events then a final "done", history lists newest-first, context carries across turns, resume and delete work.

**Blocker:** None functional. The streaming granularity is word-groups (a deliberate trade-off to not defeat the safety guard), not raw per-token deltas. Full Electron window was not pixel-verified, but every API the UI calls was exercised end-to-end.

### 4. Deep research mode — **DONE**

**Files changed:** `src/scholar_desk/research.py`, `src/scholar_desk/server.py`, `desktop/src/lib/api.ts`, `desktop/src/App.tsx`

**What it does:** A slower, explicit "Deep research" path that **plans** a question, **fans out two researchers** (one reads your library, one searches the web), **synthesizes** one cited brief, then runs a **reflection polish pass** — returning the plan steps, the brief, and sources so the UI shows its work.

**How to use it:** Press Cmd-K, type a research question, then click the **"Deep research"** pill (telescope icon) instead of Enter. It shows progress (typically ~60–120s; the library step is the slow part on a cold index), then renders the Plan, the cited Brief, and Sources. Normal Enter still does the fast streaming answer.

**Blocker / honest caveats:** Verified end-to-end live (one run returned a 6-step pipeline, a cited brief on "Attention Is All You Need", real sources). Two caveats: (1) the library index can be very CPU-heavy on a cold cache — if it times out, the brief is built from web findings only (graceful degrade by design; usually warm in real use). (2) Web results are empty unless a search provider key is set (`HIMMY_SEARCH_BACKEND` + `HIMMY_SEARCH_API_KEY`) — that's an environment/data matter, not a code bug. A topic with no matching paper and no web key honestly returns "not found" rather than fabricating.

### 5. Mail + Calendar (read-only) via Himmy's Google pack — **DONE (needs your Google sign-in to show real data)**

**Files changed:** `src/scholar_desk/agent_tools.py`, `agent/agent.yaml`, `src/scholar_desk/server.py`, `src/scholar_desk/config.py`, `desktop/src/lib/api.ts`, `desktop/src/App.tsx`, `desktop/electron/preload.cjs`, `desktop/electron/main.cjs`

**What it does:** The Mail and Calendar tabs are wired to Himmy's Google pack, **read-only**. The agent can read your inbox and upcoming events but **can never send mail or create events** (the write tools are registered but never offered to the model). Connecting uses your own Google OAuth client and stores tokens securely in the macOS keychain.

**How to use it:** Open Mail or Calendar. First time you'll see "Connect Google". Because connecting needs a Google OAuth client from your own Google Cloud project, the tab shows a one-time setup card: in Google Cloud create a "Web application" OAuth client, add the redirect URI shown (`http://127.0.0.1:8131/google/callback`), paste the client ID + secret, Save, then click "Connect Google". Your browser opens Google's consent screen; approve it and the tab flips to connected, listing your recent inbox / upcoming events. You can also ask Cmd-K *"what's in my inbox?"* or *"what's on my calendar today?"*. Disconnect anytime. Everything is read-only.

**Blocker:** We could **not** verify a real signed-in fetch — that needs a real Google account and a user-created Google Cloud OAuth client, which we can't create for you. Everything up to the live token exchange is verified (client storage, auth-URL generation, the not-connected / needs-setup states). The live consent → callback → real data round-trip is wired and should work, but is unverified end-to-end without your credentials.

---

## C. System health (this session's check)

All checks passed. Backend was restarted clean before testing.

| Check | Result |
|---|---|
| `GET /health` | **200** — `{ok:true, provider:openrouter, model:google/gemini-2.5-flash}` |
| `GET /library` | **200** (15 items) |
| `GET /news/saved/folders` | **200** |
| `GET /tasks` | **200** |
| `GET /sessions` | **200** |
| `GET /google/status` | **200** — `{configured:false, connected:false}` (expected until you sign in) |
| `POST /index` | **200** — `{indexed:17, library_items:15, saved_news:2}` (needs a JSON body, e.g. `{}`) |
| Type-check (`npx tsc --noEmit`) | Clean except the one pre-existing/accepted `Reader.tsx 'pdf.worker.min.mjs?url'` Vite import error |
| Frontend transform `App.tsx` | **200** |
| Frontend transform `api.ts` | **200** |
| Backend log error sweep | **0** errors/tracebacks/asyncio warnings |
| Agent sanity `/ask "PING"` | Returned `PING` correctly |

**Verdict: healthy.** The app boots, the agent answers, all new tabs respond, and nothing existing is broken.

---

## D. What still needs you / next

- **Connect Google to activate Mail + Calendar.** This is the one thing blocking live Mail/Calendar data. Follow the in-app setup card (create a Google Cloud OAuth web client, add the redirect URI, paste the client ID/secret, then Connect). Read-only only.
- **Sending mail / booking events** is intentionally not enabled. It's a future task that needs a human-approval ("HITL") step wired in so the agent can never send something without you confirming.
- **Self-learning** stays off until the Himmy framework is fixed upstream (the `asyncio.run()`-during-build bug). Not a Daybook change.
- **Web search in Deep research** returns nothing until a search-provider key is set (`HIMMY_SEARCH_BACKEND=tavily` + `HIMMY_SEARCH_API_KEY`). Library-only research already works when your paper index is warm.
- **Optional future polish:** true per-token streaming (needs a non-blocking output guard) and per-answer citation scoring.

---

## E. Post-build verification & fix (Claude, in-chat)

After the workflow, I re-ran a full end-to-end pass on a **fresh backend restart** and the actual app UI (not just the API). Confirmed live: the Tasks tab (with a task Himmy added on the real model), the Cmd-K palette streaming a reply, the history sidebar listing/resuming past chats, the Mail "Connect Google" setup card, and `ask_papers` citing real library papers.

**One regression caught & fixed:** the build added a startup "index-warm" that ran `asyncio.run()` inside a worker thread. That bound the shared search index's lock to a throwaway event loop, so — intermittently, depending on timing — every `ask_papers` / deep-research afterwards failed with *"Lock is bound to a different event loop."* (The workflow's own check passed by a timing race, so it slipped through.) **Fix:** removed the broken warm; the index now builds lazily on the server's own loop on first use. Re-verified: `/index` returns clean, `ask_papers` answers and cites "Attention Is All You Need," and deep research returns a library-grounded, cited brief. The only cost is that the *first* library question after launch rebuilds the index once (a few seconds) — correct over fast.
