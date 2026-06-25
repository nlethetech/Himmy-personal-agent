"""Himmy's own thin HTTP API — the contract the Zotero plugin (and any future web
UI) calls. One small FastAPI app over the brain we already have (``cli.answer``).

Endpoints:
  GET  /health        -> {ok, provider, model, zotero_up}
  POST /ask           -> {ok, reply, tools}      body: {message, context?, history?}
  POST /index         -> index_papers stats      body: {force?}

This is deliberately NOT himmy's multi-tenant BFF (``himmy serve``, the /v1 control plane).
It's a single-user local endpoint sized for an embedded chat. CORS is wide-open because it
only ever binds to localhost.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncio

import json

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from himmy_app.cli import (
    _load_dotenv,
    _ENV,
    answer_stream,
    ask_turn,
    delete_session,
    get_session,
    list_sessions,
    resume_turn,
)
from himmy_app.config import load_config


class AskRequest(BaseModel):
    message: str
    context: str | None = None  # e.g. the paper currently selected in Zotero
    history: list[str] | None = None
    session_id: str | None = None  # Cmd-K persistent conversation id (optional)


class AskResponse(BaseModel):
    ok: bool
    reply: str
    tools: list[str] = []


class ResumeRequest(BaseModel):
    checkpoint_id: str
    approved: bool
    session_id: str | None = None


class IndexRequest(BaseModel):
    force: bool = False


class DoiRequest(BaseModel):
    identifier: str  # a DOI, DOI URL, or arXiv id


class FilesRequest(BaseModel):
    paths: list[str]


class UpdateItemRequest(BaseModel):
    fields: dict[str, Any]


class NoteRequest(BaseModel):
    note: str


class HighlightRequest(BaseModel):
    page: int
    color: str = "yellow"
    text: str = ""
    note: str = ""
    rects: list[Any] = []


class HighlightUpdate(BaseModel):
    note: str | None = None
    color: str | None = None


class ReadingHeartbeat(BaseModel):
    session_id: str
    item_id: str
    seconds: float = 0.0


class ReadingPosition(BaseModel):
    item_id: str
    page: int = 1
    frac: float = 0.0
    num_pages: int | None = None


class ProfileUpdate(BaseModel):
    # The user-authored layer of the "what Himmy knows about you" profile (edited in Settings).
    about: str = ""
    projects: list[str] = []
    people: list[str] = []
    topics: list[str] = []
    preferences: list[str] = []


def _compose_prompt(message: str, context: str | None) -> str:
    """Front every Cmd-K turn with what Himmy knows about the user, then the open-paper context.

    This is the always-on personalization lever: the agent sees a compact "about you" block on
    every turn, so its answers and actions fit the person. Best-effort — a profile hiccup just
    falls back to the bare message.
    """
    from himmy_app import user_profile

    parts: list[str] = []
    try:
        about = user_profile.render_for_prompt()
    except Exception:  # noqa: BLE001 - personalization must never break a turn
        about = ""
    if about:
        parts.append(about)
    if context and context.strip():
        parts.append(
            "Context — the paper/article the user is currently viewing in Himmy:\n"
            + context.strip()
        )
    if parts:
        parts.append("User question: " + message)
        return "\n\n".join(parts)
    return message


class CollectionRequest(BaseModel):
    name: str


class SaveRequest(BaseModel):
    doi: str = ""
    arxiv: str = ""
    pdf_url: str = ""
    title: str = ""
    authors: list[str] = []
    year: str = ""
    venue: str = ""
    url: str = ""


class RestoreRequest(BaseModel):
    path: str


class InterestsRequest(BaseModel):
    interests: list[str]


class NewsSaveRequest(BaseModel):
    url: str
    title: str = ""
    source: str = ""
    image: str = ""
    snippet: str = ""
    folder: str = "Reading List"


class NewsMoveRequest(BaseModel):
    folder: str


class DismissRequest(BaseModel):
    doi: str = ""
    title: str = ""
    concepts: list[str] = []


def _advance_due(due: str | None, recur: str) -> str:
    """Next due date for a recurring task: advance from its due (or today) by the repeat interval."""
    import calendar
    import datetime

    today = datetime.date.today()
    base = today
    if due:
        try:
            base = datetime.date.fromisoformat(due[:10])
        except ValueError:
            base = today
    if base < today:
        base = today  # never schedule the next occurrence in the past
    if recur == "weekly":
        nxt = base + datetime.timedelta(days=7)
    elif recur == "monthly":
        m = base.month % 12 + 1
        y = base.year + (1 if base.month == 12 else 0)
        nxt = datetime.date(y, m, min(base.day, calendar.monthrange(y, m)[1]))
    else:  # daily (and any unknown rule) → next day
        nxt = base + datetime.timedelta(days=1)
    return nxt.isoformat()


class TaskCreateRequest(BaseModel):
    title: str
    due: str | None = None        # free-text or YYYY-MM-DD; the store keeps it verbatim
    priority: int | None = None   # 0 none · 1 low · 2 medium · 3 high


class TaskPatchRequest(BaseModel):
    # All optional — only the supplied fields are written (PATCH semantics). None means
    # "leave unchanged"; the store cannot clear `due` through this path by design.
    due: str | None = None
    priority: int | None = None
    done: bool | None = None


class TaskExtrasRequest(BaseModel):
    # Richer task fields kept in the sidecar store; all optional, only-supplied-fields-written.
    notes: str | None = None
    subtasks: list[dict[str, Any]] | None = None
    recur: str | None = None
    paper_id: str | None = None
    paper_title: str | None = None
    scheduled_start: str | None = None
    scheduled_end: str | None = None
    event_id: str | None = None


class RoutineCreate(BaseModel):
    name: str
    prompt: str
    schedule: dict[str, Any]  # {kind: daily|every|cron|at, ...} — validated by himmy's Schedule
    enabled: bool = True


class RoutineUpdate(BaseModel):
    name: str | None = None
    prompt: str | None = None
    schedule: dict[str, Any] | None = None
    enabled: bool | None = None


class ResearchRequest(BaseModel):
    question: str


class GoogleClientRequest(BaseModel):
    client_id: str
    client_secret: str


class GoogleExchangeRequest(BaseModel):
    code: str
    state: str | None = None


class CalendarEventRequest(BaseModel):
    summary: str
    start: str
    end: str
    all_day: bool = False
    location: str | None = None
    recurrence: list[str] | None = None


class CalendarEventUpdate(BaseModel):
    summary: str | None = None
    start: str | None = None
    end: str | None = None
    all_day: bool = False
    location: str | None = None


def _google_redirect_uri() -> str:
    """The loopback OAuth redirect this server listens on for Google's callback.

    Google permits ``http://127.0.0.1:<port>/...`` loopback redirects for installed/web
    OAuth clients. The user must add this exact URI to their Google Cloud OAuth client's
    "Authorized redirect URIs". Host/port mirror the server's own bind (HIMMY_APP_PORT).
    """
    port = os.environ.get("HIMMY_APP_PORT", "8131")
    return f"http://127.0.0.1:{port}/google/callback"


def create_app() -> FastAPI:
    _load_dotenv(_ENV)
    cfg = load_config()

    from himmy_app import routines as routines_mod

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> Any:
        # Seed the built-in Daily Briefing (idempotent), then start the in-process scheduler
        # so saved automations fire while the backend runs. Stop it cleanly on shutdown.
        try:
            routines_mod.seed_default_routines()
        except Exception:  # noqa: BLE001 - a seed hiccup must never block startup
            pass
        routines_mod.get_scheduler().start()

        # Keep paper recommendations warm so opening "Recommended" is instant: compute on launch
        # (only if the cache is cold/stale) and refresh every 6h in the background.
        async def _warm_recs() -> None:
            from himmy_app.news import NewsService

            svc = NewsService(cfg)
            first = True
            while True:
                try:
                    await svc.recommendations(force=not first)
                except Exception:  # noqa: BLE001 - warming must never crash the server
                    pass
                first = False
                await asyncio.sleep(6 * 3600)

        warm_task = asyncio.create_task(_warm_recs())
        try:
            yield
        finally:
            warm_task.cancel()
            await routines_mod.get_scheduler().stop()

    app = FastAPI(title="Himmy", version="0.1.0", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    @app.middleware("http")
    async def _allow_private_network(request: Any, call_next: Any) -> Any:
        # Let the browser extension (a public/secure context) reach this localhost server.
        response = await call_next(request)
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    # The papers RAG index builds lazily on first ask_papers (on the server's own event loop).
    # (A background "warm" was tried — both a thread version, which the GIL still froze, and a
    # process-pool version, which destabilized the embed step — so it was reverted for stability.
    # The one-time first-run index build remains a known cold-start cost; a proper fix belongs in
    # himmy's KnowledgeBase, making embedding non-blocking off the event loop.)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        # Himmy owns its library now — no Zotero dependency.
        return {"ok": True, "provider": cfg.provider, "model": cfg.model}

    @app.post("/ask")
    async def ask(body: AskRequest) -> dict[str, Any]:
        message = body.message.strip()
        if not message:
            return {"ok": False, "reply": "Ask me something.", "tools": []}
        prompt = _compose_prompt(message, body.context)
        try:
            r = await ask_turn(prompt, session_id=body.session_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reply": f"Error: {type(exc).__name__}: {exc}", "tools": []}
        return {"ok": True, **r}

    @app.post("/ask/resume")
    async def ask_resume(body: ResumeRequest) -> dict[str, Any]:
        """Approve (execute) or reject the gated tool that paused a run, and continue it."""
        try:
            r = await resume_turn(body.checkpoint_id, approved=body.approved, session_id=body.session_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reply": f"Error: {type(exc).__name__}: {exc}", "tools": []}
        return {"ok": True, **r}

    @app.post("/ask/stream")
    async def ask_stream(body: AskRequest) -> Any:
        """Server-Sent-Events streaming of one turn for the Cmd-K palette.

        Emits ``data: {"type":"token","text":...}`` lines as tokens arrive, then a
        final ``data: {"type":"done","reply":...,"tools":[...],"session_id":...}``.
        The turn is persisted to ``session_id`` so the next call continues context.
        Clients that can't stream should fall back to POST /ask (unchanged).
        """
        message = body.message.strip()

        async def _gen() -> Any:
            if not message:
                yield "data: " + json.dumps(
                    {"type": "done", "reply": "Ask me something.", "tools": []}
                ) + "\n\n"
                return
            prompt = _compose_prompt(message, body.context)
            try:
                async for ev in answer_stream(prompt, session_id=body.session_id):
                    yield "data: " + json.dumps(ev) + "\n\n"
            except Exception as exc:  # noqa: BLE001
                yield "data: " + json.dumps(
                    {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
                ) + "\n\n"

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- Cmd-K persistent conversations (history sidebar) --------------------------------
    @app.get("/sessions")
    async def sessions_list() -> dict[str, Any]:
        return {"ok": True, "sessions": list_sessions()}

    @app.get("/sessions/{session_id}")
    async def sessions_get(session_id: str) -> dict[str, Any]:
        data = get_session(session_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"ok": True, **data}

    @app.delete("/sessions/{session_id}")
    async def sessions_delete(session_id: str) -> dict[str, Any]:
        return {"ok": delete_session(session_id)}

    # ---- deep research: explicit multi-step orchestration (plan → fan-out → synthesize →
    # reflect). Slower + more thorough than /ask; never automatic — driven by the UI button.
    @app.post("/research")
    async def research(body: ResearchRequest) -> dict[str, Any]:
        from himmy_app.research import deep_research

        question = body.question.strip()
        if not question:
            return {"ok": False, "brief": "Ask a research question.", "sources": [], "steps": []}
        try:
            return await deep_research(question)
        except Exception as exc:  # noqa: BLE001 - deep_research handles its own errors, but stay safe
            return {
                "ok": False,
                "brief": f"Error: {type(exc).__name__}: {exc}",
                "sources": [],
                "steps": [],
            }

    @app.post("/index")
    async def index(body: IndexRequest) -> dict[str, Any]:
        from himmy_app.connectors.papers_rag import _get_index

        try:
            return await _get_index(cfg).refresh(force=body.force)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    # ---- library (the real reference manager: direct, no agent in the loop) -------------
    from himmy_app.library import Library
    from himmy_app.reading import ReadingStore

    lib = Library(cfg)
    reading = ReadingStore(cfg)

    @app.get("/library")
    async def library_list(q: str = "", collection: str = "") -> dict[str, Any]:
        items = lib.list(q, collection_id=collection or None)
        return {"ok": True, "count": len(items), "items": items}

    # ---- reading time: the Reader posts engaged-seconds heartbeats; recsys + Today consume ----
    @app.post("/reading/heartbeat")
    async def reading_heartbeat(body: ReadingHeartbeat) -> dict[str, Any]:
        return reading.record_heartbeat(body.session_id, body.item_id, body.seconds)

    @app.get("/reading/stats")
    async def reading_stats() -> dict[str, Any]:
        return reading.stats()

    @app.get("/reading/totals")
    async def reading_totals() -> dict[str, Any]:
        return {"ok": True, "totals": reading.totals_by_item()}

    @app.get("/reading/item/{item_id}")
    async def reading_item(item_id: str) -> dict[str, Any]:
        return {"ok": True, "seconds": reading.item_seconds(item_id), "last_read": reading.last_read(item_id)}

    # ---- reading position: the Reader saves where you left off; it restores on reopen --------
    @app.post("/reading/position")
    async def reading_set_position(body: ReadingPosition) -> dict[str, Any]:
        return reading.set_position(body.item_id, body.page, body.frac, body.num_pages)

    @app.get("/reading/position/{item_id}")
    async def reading_get_position(item_id: str) -> dict[str, Any]:
        return {"ok": True, "position": reading.get_position(item_id)}

    # ---- "what Himmy knows about you": the personalization profile -----------------------
    @app.get("/profile")
    async def profile_get() -> dict[str, Any]:
        from himmy_app import user_profile
        return {"ok": True, "profile": user_profile.load(cfg)}

    @app.put("/profile")
    async def profile_put(body: ProfileUpdate) -> dict[str, Any]:
        """Save the user-authored layer (what you typed in Settings)."""
        from himmy_app import user_profile
        prof = user_profile.save_user_layer(body.model_dump(), cfg)
        return {"ok": True, "profile": prof}

    @app.post("/profile/learn")
    async def profile_learn() -> dict[str, Any]:
        """Refresh what Himmy has picked up about you from your real activity."""
        from himmy_app import user_profile
        return await user_profile.learn(cfg)

    @app.post("/library/dedupe")
    async def library_dedupe() -> dict[str, Any]:
        return lib.dedupe()

    @app.get("/library/{item_id}")
    async def library_get(item_id: str) -> dict[str, Any]:
        item = lib.get(item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Not found")
        return {"ok": True, "item": item}

    @app.get("/library/{item_id}/pdf")
    async def library_pdf(item_id: str) -> Any:
        path = lib.pdf_path(item_id)
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="No PDF for this item")
        return FileResponse(path, media_type="application/pdf")

    @app.post("/library/doi")
    async def library_add_doi(body: DoiRequest) -> dict[str, Any]:
        return await lib.add_identifier(body.identifier)

    @app.post("/library/files")
    async def library_add_files(body: FilesRequest) -> dict[str, Any]:
        return lib.add_files(body.paths)

    @app.put("/library/{item_id}")
    async def library_update(item_id: str, body: UpdateItemRequest) -> dict[str, Any]:
        return lib.update_item(item_id, body.fields)

    @app.post("/library/{item_id}/enrich")
    async def library_enrich(item_id: str) -> dict[str, Any]:
        return await lib.enrich(item_id)

    @app.post("/library/{item_id}/fetch-pdf")
    async def library_fetch_pdf(item_id: str) -> dict[str, Any]:
        return await lib.fetch_pdf(item_id)

    @app.put("/library/{item_id}/notes")
    async def library_set_note(item_id: str, body: NoteRequest) -> dict[str, Any]:
        return lib.set_note(item_id, body.note)

    @app.delete("/library/{item_id}")
    async def library_delete(item_id: str) -> dict[str, Any]:
        return lib.delete(item_id)

    # ---- highlights / annotations -------------------------------------------------------
    @app.get("/library/{item_id}/highlights")
    async def highlights_list(item_id: str) -> dict[str, Any]:
        return {"ok": True, "highlights": lib.list_highlights(item_id)}

    @app.post("/library/{item_id}/highlights")
    async def highlights_add(item_id: str, body: HighlightRequest) -> dict[str, Any]:
        h = lib.add_highlight(item_id, body.page, body.color, body.text, body.note, body.rects)
        return {"ok": True, "highlight": h}

    @app.put("/highlights/{hid}")
    async def highlights_update(hid: str, body: HighlightUpdate) -> dict[str, Any]:
        return {"ok": True, "highlight": lib.update_highlight(hid, note=body.note, color=body.color)}

    @app.delete("/highlights/{hid}")
    async def highlights_delete(hid: str) -> dict[str, Any]:
        return lib.delete_highlight(hid)

    @app.post("/library/{item_id}/highlights/export")
    async def highlights_export(item_id: str) -> dict[str, Any]:
        return lib.export_highlights_markdown(item_id)

    # ---- collections --------------------------------------------------------------------
    @app.get("/collections")
    async def collections_list() -> dict[str, Any]:
        return {"ok": True, "collections": lib.list_collections()}

    @app.post("/collections")
    async def collections_create(body: CollectionRequest) -> dict[str, Any]:
        return {"ok": True, "collection": lib.create_collection(body.name)}

    @app.put("/collections/{cid}")
    async def collections_rename(cid: str, body: CollectionRequest) -> dict[str, Any]:
        return lib.rename_collection(cid, body.name)

    @app.delete("/collections/{cid}")
    async def collections_delete(cid: str) -> dict[str, Any]:
        return lib.delete_collection(cid)

    @app.post("/collections/{cid}/items/{item_id}")
    async def collections_add_item(cid: str, item_id: str) -> dict[str, Any]:
        return lib.add_to_collection(item_id, cid)

    @app.delete("/collections/{cid}/items/{item_id}")
    async def collections_remove_item(cid: str, item_id: str) -> dict[str, Any]:
        return lib.remove_from_collection(item_id, cid)

    @app.get("/tags")
    async def tags_list() -> dict[str, Any]:
        return {"ok": True, "tags": lib.all_tags()}

    # ---- browser "Save to Himmy" -----------------------------------------------------
    @app.post("/save")
    async def library_save(body: SaveRequest) -> dict[str, Any]:
        return await lib.save(body.model_dump())

    # ---- backup / restore (sync via a cloud folder) ------------------------------------
    @app.post("/backup")
    async def library_backup() -> dict[str, Any]:
        try:
            path = lib.backup(str(Path.home() / "Downloads"))
            return {"ok": True, "path": path}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}

    @app.post("/restore")
    async def library_restore(body: RestoreRequest) -> dict[str, Any]:
        return lib.restore(body.path)

    @app.get("/datadir")
    async def library_datadir() -> dict[str, Any]:
        return {"ok": True, "path": str(cfg.data_dir)}

    # ---- news hub: live feeds + in-app reading + saved articles -------------------------
    from himmy_app.news import NewsService, SavedNews, extract_article

    news = NewsService(cfg)
    saved_news = SavedNews(cfg)

    @app.get("/news/interests")
    async def news_interests() -> dict[str, Any]:
        return {"ok": True, "interests": news.get_interests()}

    @app.put("/news/interests")
    async def news_set_interests(body: InterestsRequest) -> dict[str, Any]:
        return news.set_interests(body.interests)

    @app.get("/news/categories")
    async def news_categories() -> dict[str, Any]:
        return {"ok": True, "categories": news.categories()}

    @app.get("/news/feed")
    async def news_feed(cat: str = "For You", force: bool = False) -> dict[str, Any]:
        return await news.feed(cat, force=force)

    @app.get("/news/recommendations")
    async def news_recommendations(force: bool = False) -> dict[str, Any]:
        return await news.recommendations(force=force)

    @app.post("/recommendations/dismiss")
    async def recommendations_dismiss(body: DismissRequest) -> dict[str, Any]:
        from himmy_app.feedback import DismissalStore
        from himmy_app.recsys.recommend import Recommender

        result = DismissalStore(cfg).dismiss(body.doi, body.title, body.concepts)
        try:  # refill the pool in the background so the dismissed direction fades next time
            Recommender(cfg)._spawn_refresh(24)
        except Exception:  # noqa: BLE001
            pass
        return result

    # read an article inside the app (Safari-Reader-style extraction)
    @app.get("/news/article")
    async def news_article(url: str) -> dict[str, Any]:
        return await extract_article(url)

    # saved articles (folders) — these feed the papers RAG so Himmy can read them
    @app.get("/news/saved/folders")
    async def news_saved_folders() -> dict[str, Any]:
        return {"ok": True, **saved_news.folders()}

    @app.get("/news/saved/urls")
    async def news_saved_urls() -> dict[str, Any]:
        return {"ok": True, "urls": saved_news.urls()}

    @app.get("/news/saved")
    async def news_saved_list(folder: str = "", q: str = "") -> dict[str, Any]:
        return {"ok": True, "items": saved_news.list(folder or None, q)}

    @app.get("/news/saved/{nid}")
    async def news_saved_get(nid: str) -> dict[str, Any]:
        item = saved_news.get(nid)
        if not item:
            raise HTTPException(status_code=404, detail="Not found")
        return {"ok": True, "item": item}

    @app.post("/news/save")
    async def news_save(body: NewsSaveRequest) -> dict[str, Any]:
        return await saved_news.save(body.model_dump(), body.folder)

    @app.put("/news/saved/{nid}")
    async def news_saved_move(nid: str, body: NewsMoveRequest) -> dict[str, Any]:
        return saved_news.move(nid, body.folder)

    @app.delete("/news/saved/{nid}")
    async def news_saved_remove(nid: str) -> dict[str, Any]:
        return saved_news.remove(nid)

    # ---- tasks: the SAME board Himmy reads/writes (himmy tasks pack, shared SQLite) -------
    # config.load_config() pinned HIMMY_TASKS_PATH to .scholar-desk/tasks.db, so the agent's
    # add_task/list_tasks/complete_task and these endpoints hit one store regardless of cwd.
    def _tasks_store() -> Any:
        from himmy.api.studio_tasks import get_tasks_store

        return get_tasks_store()

    from himmy_app.tasks_extra import TaskExtrasStore, _blank as _blank_extras

    def _task_dict(t: Any, extras: dict[str, Any] | None = None) -> dict[str, Any]:
        d = {
            "id": t.id,
            "title": t.title,
            "done": bool(t.done),
            "due": t.due,
            # priority: 0 none · 1 low · 2 medium · 3 high. getattr keeps this resilient if a
            # store row predates the additive priority column.
            "priority": int(getattr(t, "priority", 0) or 0),
            "created_at": t.created_at,
        }
        d.update(extras or _blank_extras())  # notes / subtasks / recur / paper link / time-block
        return d

    @app.get("/tasks")
    async def tasks_list() -> dict[str, Any]:
        extras = TaskExtrasStore(cfg).all()
        items = [_task_dict(t, extras.get(t.id)) for t in _tasks_store().list()]
        open_count = sum(1 for t in items if not t["done"])
        return {"ok": True, "tasks": items, "open": open_count, "total": len(items)}

    @app.post("/tasks")
    async def tasks_add(body: TaskCreateRequest) -> dict[str, Any]:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Task title is required")
        # Clamp priority to 0..3; an empty due string is treated as "no due".
        priority = max(0, min(3, body.priority)) if body.priority is not None else 0
        due = body.due.strip() if body.due and body.due.strip() else None
        t = _tasks_store().add(title, due=due, priority=priority)
        return {"ok": True, "task": _task_dict(t)}

    @app.patch("/tasks/{task_id}")
    async def tasks_patch(task_id: str, body: TaskPatchRequest) -> dict[str, Any]:
        # Edit a task's due / priority / done in place. Only the supplied fields change.
        priority = (
            max(0, min(3, body.priority)) if body.priority is not None else None
        )
        due = body.due.strip() if body.due and body.due.strip() else body.due
        t = _tasks_store().update(
            task_id, due=due, priority=priority, done=body.done
        )
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"ok": True, "task": _task_dict(t, TaskExtrasStore(cfg).get(task_id))}

    @app.patch("/tasks/{task_id}/extras")
    async def tasks_set_extras(task_id: str, body: TaskExtrasRequest) -> dict[str, Any]:
        t = next((x for x in _tasks_store().list() if x.id == task_id), None)
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        extras = TaskExtrasStore(cfg).set(task_id, **fields)
        return {"ok": True, "task": _task_dict(t, extras)}

    @app.post("/tasks/{task_id}/complete")
    async def tasks_complete(task_id: str) -> dict[str, Any]:
        store = _tasks_store()
        t = next((x for x in store.list() if x.id == task_id), None)
        if not store.set_done(task_id, True):
            raise HTTPException(status_code=404, detail="Task not found")
        # If the task repeats, spawn the next occurrence (carrying its repeat rule + paper link).
        spawned = None
        ex = TaskExtrasStore(cfg)
        extras = ex.get(task_id)
        if extras.get("recur") and t is not None:
            nt = store.add(t.title, due=_advance_due(t.due, extras["recur"]),
                           priority=int(getattr(t, "priority", 0) or 0))
            ex.set(nt.id, recur=extras["recur"], paper_id=extras.get("paper_id", ""),
                   paper_title=extras.get("paper_title", ""))
            spawned = _task_dict(nt, ex.get(nt.id))
        return {"ok": True, "spawned": spawned}

    @app.delete("/tasks/{task_id}")
    async def tasks_delete(task_id: str) -> dict[str, Any]:
        ok = _tasks_store().delete(task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Task not found")
        TaskExtrasStore(cfg).delete(task_id)  # clean up the sidecar row
        return {"ok": True}

    @app.post("/planner/suggest")
    async def planner_suggest() -> dict[str, Any]:
        # "Himmy, plan my week" — draft a time-blocked schedule from open tasks via the real LLM.
        from himmy_app import planner as planner_mod

        return await planner_mod.suggest_week(cfg)

    # ---- routines: saved automations that run on a schedule -------------------------------
    # himmy supplies the validated Schedule model + durable store + cron/timezone due-math;
    # the app's in-process scheduler fires each routine through the SAME agent path as /ask
    # (himmy_app.routines), so results carry the same tools/guardrails/memory and land in the
    # notifications inbox below. CRUD wakes the scheduler so changes re-plan immediately.
    @app.get("/routines")
    async def routines_list() -> dict[str, Any]:
        return {"ok": True, "routines": routines_mod.list_routines()}

    @app.post("/routines")
    async def routines_create(body: RoutineCreate) -> dict[str, Any]:
        try:
            r = routines_mod.create_routine(
                body.name, body.prompt, body.schedule, enabled=body.enabled
            )
        except Exception as exc:  # noqa: BLE001 - surface a bad schedule as a 400, not a 500
            raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")
        routines_mod.get_scheduler().notify_change()
        return {"ok": True, "routine": r}

    @app.get("/routines/{routine_id}")
    async def routines_get(routine_id: str) -> dict[str, Any]:
        r = routines_mod.get_routine(routine_id)
        if r is None:
            raise HTTPException(status_code=404, detail="Routine not found")
        return {"ok": True, "routine": r}

    @app.put("/routines/{routine_id}")
    async def routines_update(routine_id: str, body: RoutineUpdate) -> dict[str, Any]:
        try:
            r = routines_mod.update_routine(routine_id, body.model_dump(exclude_unset=True))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")
        if r is None:
            raise HTTPException(status_code=404, detail="Routine not found")
        routines_mod.get_scheduler().notify_change()
        return {"ok": True, "routine": r}

    @app.delete("/routines/{routine_id}")
    async def routines_delete(routine_id: str) -> dict[str, Any]:
        ok = routines_mod.delete_routine(routine_id)
        routines_mod.get_scheduler().notify_change()
        if not ok:
            raise HTTPException(status_code=404, detail="Routine not found")
        return {"ok": True}

    @app.post("/routines/{routine_id}/run-now")
    async def routines_run_now(routine_id: str) -> dict[str, Any]:
        return await routines_mod.get_scheduler().run_now(routine_id)

    # ---- notifications: the inbox of routine results + "needs approval" parks --------------
    @app.get("/notifications")
    async def notifications_list(limit: int = 50, unread_only: bool = False) -> dict[str, Any]:
        ib = routines_mod.get_inbox()
        return {
            "ok": True,
            "notifications": ib.list(limit=limit, unread_only=unread_only),
            "unread": ib.unread_count(),
        }

    @app.post("/notifications/{nid}/read")
    async def notifications_read(nid: str) -> dict[str, Any]:
        return {"ok": routines_mod.get_inbox().mark_read(nid)}

    @app.post("/notifications/read-all")
    async def notifications_read_all() -> dict[str, Any]:
        return {"ok": True, "marked": routines_mod.get_inbox().mark_all_read()}

    @app.delete("/notifications/{nid}")
    async def notifications_delete(nid: str) -> dict[str, Any]:
        return {"ok": routines_mod.get_inbox().delete(nid)}

    # ---- Google: read-only Mail + Calendar (himmy studio_google, OAuth2 loopback) --------
    # Sign-in flow: the UI opens /google/auth-url in the system browser; Google redirects
    # back to /google/callback on THIS server, which exchanges the code server-side and
    # stores tokens in the secrets backend (keychain/file). The UI polls /google/status.
    # READ-ONLY by design here: only /mail/inbox and /calendar/events are exposed; sending
    # mail / creating events would need an approval (HITL) layer we have not built.
    def _google() -> Any:
        from himmy.api import studio_google as g

        return g

    def _google_status_dict() -> dict[str, Any]:
        """Status the UI needs: is a client configured, and is an account connected?"""
        try:
            s = _google().status()
            return {
                "ok": True,
                "configured": bool(s.configured),
                "connected": bool(s.connected),
                "email": s.email,
                "writable": bool(s.writable),
            }
        except Exception as exc:  # noqa: BLE001 - never crash the UI over a status read
            return {
                "ok": False,
                "configured": False,
                "connected": False,
                "email": None,
                "writable": False,
                "message": f"{type(exc).__name__}: {exc}",
            }

    @app.get("/google/status")
    async def google_status() -> dict[str, Any]:
        return _google_status_dict()

    @app.post("/google/client")
    async def google_set_client(body: GoogleClientRequest) -> dict[str, Any]:
        """Store the user's Google OAuth client_id/secret (one-time setup)."""
        cid = body.client_id.strip()
        secret = body.client_secret.strip()
        if not cid or not secret:
            return {"ok": False, "message": "Both client ID and secret are required."}
        try:
            _google().set_client(cid, secret)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        return _google_status_dict()

    @app.get("/google/auth-url")
    async def google_auth_url() -> dict[str, Any]:
        g = _google()
        s = g.status()
        if not s.configured:
            return {"ok": False, "needs_setup": True, "message": "Google client not configured."}
        try:
            url = g.auth_url(_google_redirect_uri())
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "url": url, "redirect_uri": _google_redirect_uri()}

    @app.post("/google/exchange")
    async def google_exchange(body: GoogleExchangeRequest) -> dict[str, Any]:
        """Exchange an authorization code for tokens (called by the in-app paste fallback)."""
        code = body.code.strip()
        if not code:
            return {"ok": False, "message": "No authorization code provided."}
        try:
            await _google().exchange_code(code, _google_redirect_uri(), body.state)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        return _google_status_dict()

    @app.get("/google/callback")
    async def google_callback(code: str = "", state: str = "", error: str = "") -> Any:
        """Loopback redirect target for Google's OAuth consent screen.

        Google redirects the system browser here with ``?code=…``; we exchange it
        server-side, persist the tokens, and render a small "you can close this" page.
        The in-app UI is polling /google/status and flips to connected on its own.
        """
        def _page(title: str, body: str, ok: bool) -> HTMLResponse:
            tint = "#34c759" if ok else "#ff453a"
            html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Himmy · Google</title><style>
html,body{{height:100%;margin:0}}
body{{display:flex;align-items:center;justify-content:center;
background:#1c1c1e;color:#f5f5f7;
font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif}}
.card{{max-width:420px;text-align:center;padding:40px}}
.dot{{width:46px;height:46px;border-radius:50%;margin:0 auto 20px;background:{tint}22;
display:flex;align-items:center;justify-content:center;color:{tint};font-size:24px}}
h1{{font-size:19px;font-weight:600;margin:0 0 8px}}
p{{font-size:14px;line-height:1.5;color:#aeaeb2;margin:0}}
</style></head><body><div class="card"><div class="dot">{'✓' if ok else '!'}</div>
<h1>{title}</h1><p>{body}</p></div></body></html>"""
            return HTMLResponse(content=html)

        if error:
            return _page("Connection cancelled", f"Google reported: {error}. You can close this tab.", False)
        if not code:
            return _page("Missing code", "No authorization code was returned. You can close this tab.", False)
        try:
            await _google().exchange_code(code, _google_redirect_uri(), state or None)
        except Exception as exc:  # noqa: BLE001
            return _page("Couldn't connect", f"{type(exc).__name__}: {exc}", False)
        return _page("Google connected", "Himmy is now linked to your Google account. You can close this tab and return to Himmy.", True)

    @app.post("/google/disconnect")
    async def google_disconnect() -> dict[str, Any]:
        try:
            _google().disconnect()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        return _google_status_dict()

    # Short-lived inbox cache: Gmail's list is N+1 (a fetch per message) and slow, so we serve the
    # last good inbox for a few seconds and let the UI revalidate quietly. `force=true` (the
    # Refresh button) always refetches; on a transient Gmail error we fall back to the cache.
    mail_cache: dict[str, Any] = {"messages": [], "at": 0.0}
    _MAIL_TTL = 45.0

    @app.get("/mail/inbox")
    async def mail_inbox(limit: int = 15, force: bool = False) -> dict[str, Any]:
        import time

        g = _google()
        s = g.status()
        if not s.configured:
            return {"ok": False, "needs_setup": True, "connected": False, "messages": []}
        if not s.connected:
            return {"ok": True, "connected": False, "messages": []}
        fresh = bool(mail_cache["messages"]) and (time.time() - mail_cache["at"]) < _MAIL_TTL
        if fresh and not force:
            return {"ok": True, "connected": True, "messages": mail_cache["messages"], "cached": True}
        try:
            msgs = await g.gmail_list(max(1, min(limit, 30)))
        except Exception as exc:  # noqa: BLE001
            if mail_cache["messages"]:  # serve the last good inbox rather than erroring out
                return {"ok": True, "connected": True, "messages": mail_cache["messages"],
                        "cached": True, "stale": True}
            return {"ok": False, "connected": True, "messages": [], "message": f"{type(exc).__name__}: {exc}"}
        out = [
            {"id": m.id, "from": m.sender, "subject": m.subject, "snippet": m.snippet, "date": m.date}
            for m in msgs
        ]
        mail_cache.update(messages=out, at=time.time())
        return {"ok": True, "connected": True, "messages": out, "cached": False}

    @app.get("/mail/message/{message_id}")
    async def mail_message(message_id: str) -> dict[str, Any]:
        """The full text of one inbox message — the Mail tab opens this when a row is clicked."""
        g = _google()
        s = g.status()
        if not s.connected:
            return {"ok": False, "connected": False, "message": "Connect a Google account first."}
        try:
            m = await g.gmail_get(message_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "connected": True, "message": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "connected": True, "email": {
            "id": m.id, "from": m.sender, "to": m.to, "subject": m.subject,
            "date": m.date, "body": (m.body or m.snippet or ""),
        }}

    def _event_dict(e: Any) -> dict[str, Any]:
        return {
            "id": e.id, "summary": e.summary, "start": e.start, "end": e.end,
            "location": e.location, "html_link": e.html_link,
            "recurring_event_id": getattr(e, "recurring_event_id", None),
        }

    @app.get("/calendar/events")
    async def calendar_events(limit: int = 15) -> dict[str, Any]:
        g = _google()
        s = g.status()
        if not s.configured:
            return {"ok": False, "needs_setup": True, "connected": False, "events": []}
        if not s.connected:
            return {"ok": True, "connected": False, "events": []}
        try:
            events = await g.calendar_list(max(1, min(limit, 50)))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "connected": True, "events": [], "message": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "connected": True, "events": [_event_dict(e) for e in events]}

    @app.get("/calendar/range")
    async def calendar_range(time_min: str, time_max: str) -> dict[str, Any]:
        """Events between two RFC3339 datetimes — powers the month grid."""
        g = _google()
        s = g.status()
        if not s.configured:
            return {"ok": False, "needs_setup": True, "connected": False, "events": []}
        if not s.connected:
            return {"ok": True, "connected": False, "events": []}
        try:
            events = await g.calendar_range(time_min, time_max, 250)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "connected": True, "events": [], "message": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "connected": True, "events": [_event_dict(e) for e in events]}

    @app.post("/calendar/events")
    async def calendar_create(body: CalendarEventRequest) -> dict[str, Any]:
        g = _google()
        if not g.status().connected:
            return {"ok": False, "message": "Connect Google first."}
        try:
            e = await g.calendar_create(
                body.summary, body.start, body.end,
                all_day=body.all_day, location=body.location, recurrence=body.recurrence,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "event": _event_dict(e)}

    @app.put("/calendar/events/{event_id}")
    async def calendar_update(event_id: str, body: CalendarEventUpdate) -> dict[str, Any]:
        g = _google()
        if not g.status().connected:
            return {"ok": False, "message": "Connect Google first."}
        try:
            e = await g.calendar_update(
                event_id, summary=body.summary, start=body.start, end=body.end,
                all_day=body.all_day, location=body.location,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "event": _event_dict(e)}

    @app.delete("/calendar/events/{event_id}")
    async def calendar_delete(event_id: str) -> dict[str, Any]:
        g = _google()
        if not g.status().connected:
            return {"ok": False, "message": "Connect Google first."}
        try:
            await g.calendar_delete(event_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        return {"ok": True}

    # ---- usage: token + cost accounting, read from himmy's global metrics registry ---------
    # himmy's single-agent runtime feeds EVERY inference event into a process-wide metrics
    # registry (input/output tokens + USD cost, priced off the LiteLLM table). Those counters
    # reset on backend restart, so we fold each poll's delta into a small persisted lifetime
    # tally under .scholar-desk/usage.json so an all-time figure survives restarts.
    _usage_path = cfg.data_dir / "usage.json"
    _USAGE_KEYS = ("tokens_in", "tokens_out", "cost", "calls")

    def _read_registry_usage() -> dict[str, float]:
        # Current process-wide totals since THIS backend started.
        try:
            from himmy.services.observability.metrics import get_registry

            reg = get_registry()
            calls = reg.inference_requests_total.value(("true",)) + reg.inference_requests_total.value(("false",))
            return {
                "tokens_in": float(reg.inference_input_tokens_total.value()),
                "tokens_out": float(reg.inference_output_tokens_total.value()),
                "cost": float(reg.inference_cost_usd_total.value()),
                "calls": float(calls),
            }
        except Exception:  # noqa: BLE001 - never break the UI over a metrics read
            return {"tokens_in": 0.0, "tokens_out": 0.0, "cost": 0.0, "calls": 0.0}

    @app.get("/usage")
    async def usage() -> dict[str, Any]:
        session = _read_registry_usage()
        try:
            store = json.loads(_usage_path.read_text())
        except Exception:  # noqa: BLE001
            store = {}
        lifetime: dict[str, float] = {}
        changed = False
        for k in _USAGE_KEYS:
            base = float(store.get("base_" + k, 0.0))
            life = float(store.get("life_" + k, 0.0))
            cur = session[k]
            # cur < base  ->  the backend restarted (counters reset)  ->  all of cur is new.
            delta = cur - base if cur >= base else cur
            if delta:
                life += delta
                changed = True
            store["base_" + k] = cur
            store["life_" + k] = life
            lifetime[k] = life
        if changed or not _usage_path.exists():
            try:
                _usage_path.write_text(json.dumps(store))
            except Exception:  # noqa: BLE001
                pass

        def _pack(d: dict[str, float]) -> dict[str, Any]:
            ti = int(round(d["tokens_in"]))
            to = int(round(d["tokens_out"]))
            return {
                "tokens_in": ti, "tokens_out": to, "tokens_total": ti + to,
                "cost": round(d["cost"], 6), "calls": int(round(d["calls"])),
            }

        return {
            "ok": True,
            "model": cfg.model,
            "session": _pack(session),
            "lifetime": _pack(lifetime),
        }

    return app


app = create_app()


def main() -> None:
    import os

    import uvicorn

    host = os.environ.get("HIMMY_APP_HOST", "127.0.0.1")
    port = int(os.environ.get("HIMMY_APP_PORT", "8131"))
    print(f"Himmy API → http://{host}:{port}  (POST /ask, /index, GET /health)")
    uvicorn.run("himmy_app.server:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
