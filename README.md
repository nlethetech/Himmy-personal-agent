# Himmy

**Himmy** is a native macOS personal assistant — a calm, premium workspace that runs your research
*and* your day, with one AI that works across every surface. Talk to it in plain language and it
reads your papers, plans your week, books your trip, and keeps you ahead of what matters.

Two ways to use it:

- **The workspace** — a clean macOS app (Today · News · Library · Concierge · Mail) you navigate like
  any native app.
- **Himmy, the ⌘K assistant** — summon it from anywhere with **⌘K** and ask. It reads your library,
  acts across your surfaces, and pauses for your approval before anything risky.

Built **on** the [himmy agent framework](https://github.com/nlethetech/himmy-framework) (consumed as a
library). Runs on OpenRouter `google/gemini-2.5-flash` (a strong, inexpensive tool-caller) or a local
Ollama model.

> The app, the assistant, and the project are all just **Himmy**. (Internally the Python package is
> `himmy_app` so it never collides with the `himmy` framework it's built on.)

---

## What it can do

### 🧠 Himmy — the ⌘K assistant
- **Reads your library.** `ask_papers` retrieves passages from the *full text* of your PDFs (hybrid
  BM25 + dense search) and cites every source — including **your own highlights and notes**. It never
  invents a paper, author, or finding.
- **Acts for you.** Save an article, add a paper, manage tasks & calendar, draft & send mail, plan a
  trip — Himmy calls the right tool instead of just describing it. **Risky actions pause for approval**
  (a human-in-the-loop card in the palette) before they happen.
- **Deep research.** Plans a question, searches your library *and* the web, and writes a cited brief.
- **Durable memory.** Learns your projects, people, and writing voice and uses them everywhere.
- Streaming replies rendered as rich markdown (tables for lists, never raw `**`).

### ☀️ Today
Your morning at a glance — a **daily brief** Himmy writes for you (what to focus on, what's due,
what's slipping), your tasks, your next calendar event, "jump back in" to recent reading, and a live
**usage / cost meter**.

### 📚 Library — your reference manager
- Add papers by **DOI / arXiv / PDF** (drag-drop), or one-click from the browser with the **Save to
  Himmy** extension.
- Built-in **PDF reader** with text highlights (4 colours) + per-highlight and per-paper **notes**.
- **Auto-fill with Himmy** (AI metadata enrich → authoritative title / authors / DOI) and **full-PDF
  fetch** (arXiv / Unpaywall open-access).
- **Collections & tags**; **citations** in APA / MLA / BibTeX (export a whole `.bib`).
- Everything you read *and annotate* feeds the RAG, so Himmy answers from what you marked.

### 📰 News
A premium news reader (Nepal + world) — non-LLM RSS by category, an in-app **reader** (Safari-Reader
style, not the browser), **save to folders** (saved articles feed the RAG), **For You** recommendations
from your interests, and one-tap **Summarize**.

### 🧳 Concierge — a smart Nepal concierge
One place to get things done in Nepal, each pick a real deep-link (Himmy never spends your money):
- **Eat** — Foodmandu restaurants + menus & recommended dishes.
- **Products** — Daraz search with prices & deals.
- **Flights** — Buddha Air live fares, **one-way or round-trip**.
- **Buses** — bussewa live departures, fares & seats (with smart hub fallbacks, e.g. *via Dumre for
  Bandipur*).
- **Trips** — a premium **day-by-day roadmap** grounded in real places (OpenStreetMap): a budget,
  hotels, where-to-eat, a **fly-vs-bus comparison** (price *and* time), live **weather for your dates**
  (per-day forecast + the plan adapts to rain + a packing tip), and a **shareable** itinerary.
- A **tray/cart** with self-checkout links, search, a **For You** rail, and it **learns your taste**
  from what you thumbs-up/down. **Festival-aware nudges** (Dashain / Tihar travel rush) come from the
  live Nepali holiday calendar — no hardcoded dates.

### 🗓️ Planner
Tasks with **due dates, priorities, notes, subtasks, and recurrence**; a **week time-grid** you can
drag tasks into; and **"plan my week"** where Himmy drafts reviewable time-blocks.

### ✉️ Mail & Calendar
Over a connected **Google** account: read your inbox and run a full **calendar** (create / edit /
delete, recurring-aware). Sending mail and creating events are **approval-gated**.

### 🔁 Routines & proactivity
Saved **automations on a schedule** (daily / weekly / cron) with a notifications bell and native macOS
notifications — plus **smart nudges** Himmy raises on its own (a task due, an unreplied email, a trip
or festival coming up).

### 🔒 Trust & control
- **Permissions** — granular per-connection access (e.g. Calendar *read-only*, Email *off*). It's
  *enforced*: a denied tool never reaches the model.
- **Activity** — a plain-English log of everything Himmy did.
- **Telegram** — chat with Himmy from your phone.
- **Backup & restore** — your whole workspace to a single zip.

---

## Private & local-first

Everything you create — library, PDFs, highlights, memory, sign-ins — lives in a hidden local folder
(`.scholar-desk/`) on your Mac. Google tokens are kept in the **macOS keychain**. The only thing that
leaves your machine is the text you send to your chosen model (OpenRouter or a local Ollama model that
never leaves the laptop). All outbound connector calls go through one hardened HTTP layer (SSRF guard +
host allow-lists, redirect validation, content-type & size caps), and shareable artifacts (like an
exported trip) are scrubbed of your personal details.

## How it's built

- **App** — Electron + React + Vite + Tailwind; SF Pro, Lucide icons, native macOS vibrancy. Lives in
  `desktop/`.
- **Backend** — a thin FastAPI service (`src/himmy_app/`, on `:8131`) the Electron app spawns; it wraps
  the **himmy** agent (one agent, many connectors) with the app's own tools.
- **Framework** — [himmy](https://github.com/nlethetech/himmy-framework) provides the agent loop, tool
  registry, RAG/knowledge base, memory, guardrails, routines, and HITL approval machinery.

## Setup

```bash
# 1) The himmy framework (separate repo — Himmy is built ON it, consumed as a library)
git clone https://github.com/nlethetech/himmy-framework.git ../himmy-framework

# 2) Python backend
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e '../himmy-framework[toolkit,api,openai,embeddings,nepal,cron]'
uv pip install -e '.[dev]'

# 3) Mac app
cd desktop && npm install
```

Add an `OPENROUTER_API_KEY` to a `.env` next to this README (not committed) — or switch to a local
Ollama model from the app's model picker.

## Run

```bash
# The Mac app (what you use day to day) — launches Vite + Electron + the Python backend:
cd desktop && npm run dev

# Headless / terminal:
himmy-app                              # interactive terminal chat
himmy-app "what have I saved on X?"    # one-shot
./serve.sh                             # the backend API alone (FastAPI on :8131)
```

## Your data

Keep `.scholar-desk/` in iCloud Drive / Dropbox to back it up and use Himmy on another Mac. The folder
keeps its old name on purpose — renaming it would orphan your real data, and you never see it.

---

*Himmy is a personal project, currently macOS-only, and the Concierge surfaces are Nepal-focused
(Foodmandu, Daraz, Buddha Air, bussewa). It needs the himmy framework and a model key (or local Ollama)
to run.*
