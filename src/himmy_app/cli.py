"""Terminal entrypoint for Himmy.

Loads ``.env``, resolves config, and either answers a one-shot question
(``himmy-app "what do my papers say about X?"``) or opens an interactive REPL
(``himmy-app`` with no args). The same agent is also reachable the "himmy API way"
via ``himmy serve`` (see serve.sh) — both share one ``agent/agent.yaml``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

#: Repo root (…/Himmy) and the agent spec that ships under agent/.
_ROOT = Path(__file__).resolve().parents[2]
_SPEC = _ROOT / "agent" / "agent.yaml"
_ENV = _ROOT / ".env"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines into os.environ (never overrides what's set)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _tools_from_events(events: list[Any]) -> list[str]:
    """Pull the distinct tool names Himmy used this turn out of the event stream."""
    tools: list[str] = []
    for event in events:
        etype = getattr(getattr(event, "event_type", None), "value", None) or str(
            getattr(event, "event_type", "")
        )
        if etype in ("TOOL_CALLED", "TOOL_COMPLETED"):
            payload = getattr(event, "payload", None) or {}
            name = payload.get("tool_name") or payload.get("tool") or payload.get("name")
            if name and name not in tools:
                tools.append(str(name))
    return tools


def _build_runtime(cfg: Any, on_event: Any, *, checkpoint_store: Any = None) -> tuple[Any, Any, Any]:
    """Build the himmy runtime + persona + spec for one request.

    Returns ``(runtime, persona, spec)``. A fresh runtime per request keeps the
    server's already-running event loop happy (durable defaults wire the same
    memory/store paths every time), and lets us pass an optional resumed thread in.
    A ``checkpoint_store`` enables human-in-the-loop: approval-gated tools pause the
    run into a durable checkpoint instead of executing.
    """
    from himmy.runtime.from_spec import build_runtime_for_spec, load_spec_file

    spec = load_spec_file(str(_SPEC), provider=cfg.provider, model=cfg.model)
    runtime, _registry = build_runtime_for_spec(
        spec, provider=cfg.provider, model=cfg.model, on_event=on_event, durable_defaults=True,
        checkpoint_store=checkpoint_store,
    )
    return runtime, spec.to_persona(), spec


def approvals_store(cfg: Any | None = None) -> Any:
    """The durable HITL checkpoint store (``.scholar-desk/approvals.db``).

    File-backed, so a fresh store on the same path (one is built per request) always
    sees the persisted ``awaiting_approval`` checkpoints — that's how an approve/reject
    from a later request resumes a run a previous request paused.
    """
    from himmy.runtime.checkpoint import SqliteCheckpointStore

    from himmy_app.config import load_config

    c = cfg or load_config()
    return SqliteCheckpointStore(str(c.data_dir / "approvals.db"))


def _pending_payload(store: Any, checkpoint_id: str) -> list[dict[str, Any]]:
    """The pending (approval-gated) tool calls of a checkpoint, secrets redacted."""
    cp = store.load(checkpoint_id)
    if cp is None:
        return []
    try:
        from himmy.runtime.checkpoint import redact_sensitive_args as _redact
    except Exception:  # noqa: BLE001 - redaction is best-effort
        _redact = None
    out: list[dict[str, Any]] = []
    for ptc in getattr(cp, "pending_tool_calls", []) or []:
        args = dict(getattr(ptc, "args", {}) or {})
        if _redact is not None:
            try:
                args = _redact(args)
            except Exception:  # noqa: BLE001
                pass
        out.append({
            "tool_call_id": getattr(ptc, "tool_call_id", ""),
            "tool_name": getattr(ptc, "tool_name", ""),
            "args": args,
        })
    return out


def session_store() -> Any:
    """The durable conversation store backing the Cmd-K assistant's history.

    ``conversations_db_path()`` is pinned to ``.scholar-desk/conversations.db`` in
    :func:`himmy_app.config.load_config`, so every request — /ask, /ask/stream,
    /sessions — reads and writes one on-disk database regardless of cwd.
    """
    from himmy.config.project import conversations_db_path
    from himmy.runtime.session import SqliteSessionStore

    return SqliteSessionStore(conversations_db_path())


def _empty_thread(persona: Any) -> Any:
    from himmy.agents.base_agent.thread import ChatThread

    return ChatThread(agent_id=persona.agent_id)


def _load_thread(store: Any, persona: Any, session_id: str | None) -> Any:
    """Resume ``session_id``'s saved thread, or open a fresh one."""
    if session_id:
        existing = store.load(session_id)
        if existing is not None:
            return existing
    return _empty_thread(persona)


def _build_prompt(message: str, history: list[str] | None) -> str:
    """Frame the user message with any caller-supplied history hints.

    When a persistent ``session_id`` is used the prior turns already live in the
    resumed ChatThread, so ``history`` is normally ``None``; this stays for the
    legacy stateless /ask callers that pass a rolling history list.
    """
    if not history:
        return message
    recent = "\n".join(f"- {h}" for h in history[-8:])
    return f"Recent conversation (old → new):\n{recent}\n\nCurrent message: {message}"


async def answer(
    message: str,
    *,
    history: list[str] | None = None,
    session_id: str | None = None,
) -> tuple[str, list[str]]:
    """Run one question through himmy; return (reply, tool_names).

    When ``session_id`` is given the prior conversation is loaded from the durable
    store, this turn runs on top of it, and the updated thread is saved back — so
    the next call with the same id continues the conversation. With no
    ``session_id`` the behaviour is exactly the old stateless one-shot.
    """
    from himmy_app.config import load_config

    cfg = load_config()
    events: list[Any] = []

    async def _on_event(event: Any) -> None:
        events.append(event)

    runtime, persona, spec = _build_runtime(cfg, _on_event)

    store = session_store() if session_id else None
    thread = _load_thread(store, persona, session_id) if store else None

    task = spec.make_task(_build_prompt(message, history), title="turn")
    result = await runtime.run_agent_loop(
        persona, task, thread=thread, max_turns=cfg.max_turns, llm_config=spec.to_llm_config()
    )
    reply = (result.final.output_text or "").strip()

    if store is not None and session_id:
        try:
            store.save(session_id, result.thread)
        except Exception:  # noqa: BLE001 - never fail a turn just because persistence hiccupped
            pass

    return reply, _tools_from_events(events)


async def ask_turn(message: str, *, session_id: str | None = None) -> dict[str, Any]:
    """Run one turn with HITL ON. Returns a dict:

    - normal answer → ``{awaiting_approval: False, reply, tools}``
    - an approval-gated tool was called → ``{awaiting_approval: True, checkpoint_id,
      pending: [{tool_name, args}], tools, reply: ""}`` — the run is PAUSED in a durable
      checkpoint; call :func:`resume_turn` after the human approves or rejects.
    """
    from himmy_app.config import load_config

    cfg = load_config()
    events: list[Any] = []

    async def _on_event(event: Any) -> None:
        events.append(event)

    cp_store = approvals_store(cfg)
    runtime, persona, spec = _build_runtime(cfg, _on_event, checkpoint_store=cp_store)

    store = session_store() if session_id else None
    thread = _load_thread(store, persona, session_id) if store else None

    task = spec.make_task(_build_prompt(message, None), title="turn")
    result = await runtime.run_agent_loop(
        persona, task, thread=thread, max_turns=cfg.max_turns,
        llm_config=spec.to_llm_config(), hitl=True,
    )
    tools = _tools_from_events(events)

    if result.stopped_reason == "awaiting_approval" and result.checkpoint_id:
        return {
            "awaiting_approval": True, "checkpoint_id": result.checkpoint_id,
            "pending": _pending_payload(cp_store, result.checkpoint_id),
            "reply": "", "tools": tools,
        }

    reply = (result.final.output_text or "").strip()
    if store is not None and session_id:
        try: store.save(session_id, result.thread)
        except Exception: pass  # noqa: BLE001, E722
    return {"awaiting_approval": False, "reply": reply, "tools": tools}


async def resume_turn(checkpoint_id: str, *, approved: bool, session_id: str | None = None) -> dict[str, Any]:
    """Resume a paused run after the human approves (execute) or rejects the gated tool.

    Returns the same shape as :func:`ask_turn` — the continuation may itself pause on a
    further approval-gated tool, in which case ``awaiting_approval`` is True again.
    """
    from himmy_app.config import load_config

    cfg = load_config()
    events: list[Any] = []

    async def _on_event(event: Any) -> None:
        events.append(event)

    cp_store = approvals_store(cfg)
    runtime, persona, spec = _build_runtime(cfg, _on_event, checkpoint_store=cp_store)

    try:
        result = await runtime.resume_agent_loop(
            checkpoint_id, approved=approved, llm_config=spec.to_llm_config(),
        )
    except Exception as exc:  # noqa: BLE001 - a bad/already-resolved checkpoint must not 500
        return {"awaiting_approval": False, "reply": "", "tools": [],
                "error": f"{type(exc).__name__}: {exc}"}

    tools = _tools_from_events(events)
    if result.stopped_reason == "awaiting_approval" and result.checkpoint_id:
        return {
            "awaiting_approval": True, "checkpoint_id": result.checkpoint_id,
            "pending": _pending_payload(cp_store, result.checkpoint_id),
            "reply": "", "tools": tools,
        }

    reply = (result.final.output_text or "").strip()
    if session_id:
        try: session_store().save(session_id, result.thread)
        except Exception: pass  # noqa: BLE001, E722
    return {"awaiting_approval": False, "reply": reply, "tools": tools}


async def answer_stream(
    message: str,
    *,
    session_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one turn token-by-token, then persist it to ``session_id``.

    Yields event dicts: ``{"type": "token", "text": ...}`` for each text delta and a
    final ``{"type": "done", "reply": ..., "tools": [...]}``. Mirrors the REPL's
    pattern (himmy/cli/repl.py): a tool-using agent buffers its act→observe loop,
    then we re-issue ONE no-tool synthesis turn through ``stream_task`` so the user
    sees the final answer arrive live. On openrouter (OpenAI-compatible) the synthesis
    goes through ``openai_manager.generate_stream`` — a real provider token stream.

    HONEST NOTE on streaming granularity: this agent runs a BLOCKING output guardrail
    (grounding/PII/injection). himmy's ``stream_task`` cannot stream past a guard that
    can WITHHOLD content (an already-streamed secret can't be recalled), so for this
    agent it delivers the guarded answer as ONE chunk after the guard runs. To keep the
    premium token-by-token feel WITHOUT defeating the guard, we re-emit that
    guard-cleared text as small word-group deltas here. When a future/looser guard
    allows true streaming, stream_task's real per-token deltas flow straight through.
    """
    import asyncio

    from himmy.agents.base_agent.task import Task

    from himmy_app.config import load_config

    cfg = load_config()
    events: list[Any] = []

    async def _on_event(event: Any) -> None:
        events.append(event)

    cp_store = approvals_store(cfg)
    runtime, persona, spec = _build_runtime(cfg, _on_event, checkpoint_store=cp_store)

    store = session_store()
    sid = session_id or "last"
    thread = _load_thread(store, persona, sid)

    # 1) Run the bounded tool loop with HITL ON (buffered — can't stream mid-loop).
    task = spec.make_task(_build_prompt(message, None), title="turn")
    result = await runtime.run_agent_loop(
        persona, task, thread=thread, max_turns=cfg.max_turns,
        llm_config=spec.to_llm_config(), hitl=True,
    )
    tools = _tools_from_events(events)

    # 1a) An approval-gated tool paused the run — surface it and stop. The UI shows an
    #     approval card; on Approve/Cancel it calls /ask/resume to continue this run.
    if result.stopped_reason == "awaiting_approval" and result.checkpoint_id:
        yield {"type": "approval", "checkpoint_id": result.checkpoint_id,
               "pending": _pending_payload(cp_store, result.checkpoint_id)}
        yield {"type": "done", "reply": "", "tools": tools,
               "session_id": sid, "awaiting_approval": True,
               "checkpoint_id": result.checkpoint_id}
        return

    thread = result.thread
    buffered = (result.final.output_text or "").strip()

    # 2) Re-issue ONE no-tool synthesis turn (tools unbound) and forward its deltas.
    synth = Task(
        title="scholar-desk-synthesis",
        prompt=(
            "Using only the conversation and tool results above, answer the user's "
            "last message directly and completely. Do not call any tools."
        ),
        context={"tool_names": []},
    )
    pieces: list[str] = []  # raw deltas from stream_task (1 buffered chunk under the guard)
    try:
        async for delta in runtime.stream_task(
            persona, synth, thread=thread, llm_config=spec.to_llm_config()
        ):
            if getattr(delta, "event_type", None) is not None:
                continue  # structured (non-text) event — skip
            text = getattr(delta, "delta", "") or ""
            if text:
                pieces.append(text)
    except Exception:  # noqa: BLE001 - streaming failed; fall back to the buffered answer
        pieces = []

    reply = ("".join(pieces)).strip() or buffered

    # 3) Emit the (guard-cleared) reply as progressive token deltas. If stream_task
    #    genuinely streamed many pieces, send them as-is; if it buffered into one chunk,
    #    re-chunk by word-groups so the UI still reveals text progressively.
    if len(pieces) > 1:
        for piece in pieces:
            yield {"type": "token", "text": piece}
    elif reply:
        words = reply.split(" ")
        for i in range(0, len(words), 3):
            group = " ".join(words[i : i + 3])
            if i + 3 < len(words):
                group += " "
            yield {"type": "token", "text": group}
            await asyncio.sleep(0.012)  # gentle reveal cadence

    # 4) Persist the conversation (the synthesis turn already appended to the thread).
    try:
        store.save(sid, thread)
    except Exception:  # noqa: BLE001
        pass

    yield {"type": "done", "reply": reply, "tools": tools, "session_id": sid}


#: himmy's make_task wraps every prompt in this template line; strip it so the user sees
#: their own words in the history sidebar and resumed transcript, not the framing.
_TASK_PREFIX = "You are assigned with the following task:"
#: The synthesis turn (token-streaming re-issue) injects this prompt; it's plumbing, not a
#: real user message, so it's hidden from the resumed transcript and the title preview.
_SYNTH_MARKER = "Using only the conversation and tool results above"


def _clean_user_text(content: str) -> str:
    """Unwrap make_task's framing so stored user messages read as the user typed them."""
    text = (content or "").strip()
    if text.startswith(_TASK_PREFIX):
        text = text[len(_TASK_PREFIX):].strip()
    return text


def _thread_messages(thread: Any) -> list[dict[str, str]]:
    """User/assistant turns as ``[{role, content}]`` — synthesis plumbing turns dropped.

    Each streamed turn appends TWO user/assistant pairs to the thread: the real turn and
    the no-tool synthesis re-issue (same answer). We keep the real user message + the
    final assistant answer and drop the synthesis pair so the resumed transcript reads as
    one clean exchange per turn.
    """
    out: list[dict[str, str]] = []
    skip_next_assistant = False
    for m in getattr(thread, "messages", []):
        role = getattr(getattr(m, "role", None), "value", None) or str(getattr(m, "role", ""))
        raw = (getattr(m, "content", "") or "").strip()
        if role == "user":
            if _SYNTH_MARKER in raw:
                # Drop the synthesis prompt AND the assistant answer it produces; the prior
                # assistant message already holds the same final answer.
                skip_next_assistant = True
                continue
            text = _clean_user_text(raw)
            if text:
                out.append({"role": "user", "content": text})
        elif role == "assistant":
            if skip_next_assistant:
                skip_next_assistant = False
                continue
            if raw:
                out.append({"role": "assistant", "content": raw})
    return out


def _session_preview(store: Any, session_id: str) -> str:
    """The first real user message of a session, trimmed — the human-readable title."""
    try:
        thread = store.load(session_id)
    except Exception:  # noqa: BLE001
        return ""
    if thread is None:
        return ""
    for m in getattr(thread, "messages", []):
        role = getattr(getattr(m, "role", None), "value", None) or str(getattr(m, "role", ""))
        if role != "user":
            continue
        raw = (getattr(m, "content", "") or "").strip()
        if _SYNTH_MARKER in raw:
            continue
        first = (_clean_user_text(raw).splitlines() or [""])[0]
        return (first[:80] + "…") if len(first) > 80 else first
    return ""


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    """Recent Cmd-K conversations, newest first, each with a title preview."""
    store = session_store()
    infos = store.list_sessions(limit=limit)
    return [
        {
            "session_id": info.session_id,
            "updated_at": info.updated_at,
            "message_count": info.message_count,
            "title": _session_preview(store, info.session_id) or "New chat",
        }
        for info in infos
    ]


def get_session(session_id: str) -> dict[str, Any] | None:
    """The user/assistant messages of one session, for resuming the palette."""
    store = session_store()
    thread = store.load(session_id)
    if thread is None:
        return None
    return {"session_id": session_id, "messages": _thread_messages(thread)}


def delete_session(session_id: str) -> bool:
    """Remove a saved conversation from the durable store."""
    store = session_store()
    try:
        return bool(store._store.delete(session_id))
    except Exception:  # noqa: BLE001
        return False


def _print_reply(reply: str, tools: list[str]) -> None:
    print("\n" + (reply or "(no answer)"))
    if tools:
        print(f"\n  · tools: {', '.join(tools)}")


def main() -> None:
    _load_dotenv(_ENV)
    args = sys.argv[1:]

    if args:  # one-shot
        reply, tools = asyncio.run(answer(" ".join(args)))
        _print_reply(reply, tools)
        return

    # interactive REPL
    print("Himmy — your academic research assistant. Type a question, or 'exit'.")
    history: list[str] = []
    while True:
        try:
            msg = input("\nyou › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not msg:
            continue
        if msg.lower() in {"exit", "quit", ":q"}:
            break
        try:
            reply, tools = asyncio.run(answer(msg, history=history))
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive on any error
            print(f"\n  ! error: {type(exc).__name__}: {exc}")
            continue
        _print_reply(reply, tools)
        history.append(f"you: {msg}")
        history.append(f"desk: {reply}")


if __name__ == "__main__":
    main()
