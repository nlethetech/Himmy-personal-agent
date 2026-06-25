# Himmy

**Himmy** is a native macOS personal research & productivity assistant. Talk to it in plain
language and it works across your whole desk: your **reference library** (read the full text of
your papers with citations — RAG), your **tasks**, your **news**, and your **mail & calendar**.
One assistant, every surface.

Built ON [himmy](../himmy-agent-test) (the framework, consumed as a library — same as yetidai).
Runs on OpenRouter `google/gemini-2.5-flash`, a strong, inexpensive tool-caller.

> The Mac app, the assistant, and the project are all just **Himmy**. (Internally the Python
> package is `himmy_app` so it never collides with the `himmy` framework it's built on.)

## What it can do

- **Your library** — add papers by DOI / arXiv / PDF, read them in a built-in PDF reader with
  highlights & notes, organise with collections & tags, and export citations (APA / MLA / BibTeX).
- **Chat with your papers** — `ask_papers` retrieves passages from the full text of your PDFs
  (hybrid BM25 + dense search) and cites every source. Himmy never invents references.
- **Run your day** — tasks board, a news reader with recommendations, and read-only **Mail** +
  read/write **Calendar** over a connected Google account (sending mail & calendar changes are
  approval-gated).
- **Plus** durable memory (`remember` / `recall`), web search, calculator, current time.

## Setup

```bash
cd ~/LocalProjects/scholar-desk
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e '../himmy-agent-test[toolkit,api,openai,embeddings]'   # framework + extras
uv pip install -e '.[dev]'                                               # this project
```

Your OpenRouter key lives in `.env` (not committed).

## Run

```bash
# The Mac app (what you actually use day to day):
cd desktop && npm install && npm run dev     # Vite + Electron

# Headless / terminal:
himmy-app                          # interactive terminal chat
himmy-app "what have I saved on X?"      # one-shot
./chat.sh                          # himmy's own chat REPL over the same agent
./serve.sh                         # the backend API alone (FastAPI on :8131)
```

## Your data

Everything you create — library, PDFs, highlights, memory, sign-ins — lives in the hidden
`.scholar-desk/` folder next to this README. That folder keeps its old name on purpose: renaming
it would orphan your real data, and you never see it. Keep it in iCloud Drive / Dropbox to back
it up and use Himmy on another Mac.

## Roadmap

- **Proactive Himmy** — a "Today" home Himmy writes for you each morning (calendar + due tasks +
  important mail + new papers/news worth your time).
- **Deeper memory** — Himmy learns your projects, writing, and key contacts and uses them everywhere.
- **More actions** — richer email drafting, natural-language calendar, linking papers ↔ tasks.
- Persistence upgrade: swap the in-memory KB for himmy's pgvector backend if the library gets huge.
