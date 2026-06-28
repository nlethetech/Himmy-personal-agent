"""Terminal entrypoint for Himmy.

Loads ``.env``, resolves config, and either answers a one-shot question
(``himmy-app "what do my papers say about X?"``) or opens an interactive REPL
(``himmy-app`` with no args). The same agent is also reachable the "himmy API way"
via ``himmy serve`` (see serve.sh) — both share one ``agent/agent.yaml``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

#: Repo root (…/Himmy) and the agent spec that ships under agent/.
#: In a PyInstaller-frozen build (the packaged Himmy.app) ``__file__`` lives INSIDE the
#: read-only bundle, so parents[2] is meaningless — the agent/ dir is bundled as data and
#: surfaces under ``sys._MEIPASS`` instead. Resolve from there when frozen.
if getattr(sys, "frozen", False):
    _ROOT = Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).resolve().parent)
else:
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


def _event_type(event: Any) -> str:
    """The event's type as a plain string (``TOOL_COMPLETED``, …), enum or not."""
    return getattr(getattr(event, "event_type", None), "value", None) or str(
        getattr(event, "event_type", "")
    )


def _tools_from_events(events: list[Any]) -> list[str]:
    """Pull the distinct tool names Himmy used this turn out of the event stream."""
    tools: list[str] = []
    for event in events:
        if _event_type(event) in ("TOOL_CALLED", "TOOL_COMPLETED"):
            payload = getattr(event, "payload", None) or {}
            name = payload.get("tool_name") or payload.get("tool") or payload.get("name")
            if name and name not in tools:
                tools.append(str(name))
    return tools


#: Hard cap on the JSON size of any single tool result carried to the renderer. himmy already
#: truncates a tool's result text to ~2 000 chars on the event; this is a second ceiling applied
#: AFTER redaction, so an unusually large structured result can never bloat the wire/SSE frame.
_RESULT_CAP_BYTES = 16384
#: When a result IS over the cap, we shrink it STRUCTURE-PRESERVINGLY (long strings truncated, long
#: lists kept to the first N) so the chat's rich cards still see a valid dict — never a half-cut
#: JSON string. Most connector results (e.g. ~7.5 KB for 50+ flights) fit whole under the cap.
_RESULT_LIST_KEEP = 12
_RESULT_STR_CAP = 800

#: PII-bearing free-text keys whose VALUE we drop entirely before a tool result/args reaches the
#: palette renderer. These carry mail/calendar bodies, recipient lists, descriptions, and notes —
#: content that must never leave the backend even though the *key* isn't a "secret" (so the kernel
#: key-based redactor leaves it untouched). Matched case-insensitively as a whole key.
_PII_TEXT_KEYS = frozenset({
    "body", "snippet", "description", "desc", "note", "notes", "attendees", "attendee",
    "bcc", "cc", "html_link", "htmllink", "raw", "thread", "content", "location", "address",
    # Mail/calendar identity & subject lines are doxing-grade PII. Subjects/summaries/titles
    # aren't email-shaped so _EMAIL_RE never touches them; sender/recipient/reply_to carry a
    # human display name alongside the @-address that masking alone leaves intact. None of these
    # keys appear in the rich flight/bus/food/weather cards, so dropping them everywhere is safe.
    "subject", "summary", "title", "sender", "recipient", "reply_to", "replyto",
})

#: PII-text keys that ALSO name legitimate, non-PII fields on the transport cards: a flight/bus
#: result carries ``from``/``to`` as city names ("Kathmandu" → "Pokhara") that the route line
#: renders. We therefore drop ``from``/``to``/``cc``/``bcc`` only for mail/calendar tools (where
#: they're recipients/senders), and leave them intact for everything else so the cards still draw.
_MAILCAL_PII_KEYS = frozenset({"from", "to", "cc", "bcc"})

#: Tools whose results are mail- or calendar-shaped — the only context in which ``from``/``to``
#: are recipient identities rather than transport endpoints. Matched as a name prefix so
#: ``mail_read``/``mail_list``/``mail_send``/``calendar_find``/``calendar_add``/… all qualify.
_MAILCAL_TOOL_PREFIXES = ("mail", "calendar", "gmail", "gcal", "email")

#: Email-address shape. Any string VALUE that contains an address (a mail `to`/`from`/`cc`, an
#: address echoed inside prose) is masked — so "Kathmandu" (a flight/bus `from`) survives but
#: "ram@example.com" (a mail `from`) never reaches the renderer.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_REDACTED = "[redacted]"


def _scrub_value(value: Any, *, mailcal: bool = False) -> Any:
    """Recursively mask PII in a (already key-redacted) value: drop PII-text keys, mask any
    email address found in a string. Lists/dicts recurse; scalars pass through.

    ``mailcal`` extends the dropped-key set with ``from``/``to``/``cc``/``bcc`` — recipient and
    sender identities on mail/calendar results. Those same keys are legitimate city endpoints on
    transport cards, so they're only dropped when the originating tool is mail/calendar-shaped.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            kl = str(k).lower()
            if kl in _PII_TEXT_KEYS or (mailcal and kl in _MAILCAL_PII_KEYS):
                out[k] = _REDACTED
            else:
                out[k] = _scrub_value(v, mailcal=mailcal)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item, mailcal=mailcal) for item in value]
    if isinstance(value, str):
        return _EMAIL_RE.sub(_REDACTED, value)
    return value


def _is_mailcal_tool(tool_name: Any) -> bool:
    """True when a tool's results are mail/calendar-shaped (so ``from``/``to`` are identities,
    not transport endpoints). Prefix match, case-insensitive."""
    name = str(tool_name or "").lower()
    return name.startswith(_MAILCAL_TOOL_PREFIXES)


def _redact_for_render(value: Any, *, tool_name: Any = None) -> Any:
    """Two-layer redaction for anything (tool args OR result) bound for the renderer.

    Layer 1 — the canonical himmy key-based redactor (``redact_mapping``): masks values under
    secret-looking keys (token/api_key/authorization/cookie/…) at ANY nesting depth, identically
    to the audit spine and the approvals UI. Layer 2 — our value-aware scrub: drops PII free-text
    keys (mail/calendar bodies, subjects, summaries, recipient lists, descriptions, notes) and
    masks email addresses found in any string. For mail/calendar tools it additionally drops
    ``from``/``to``/``cc``/``bcc`` (recipient/sender identities). Together they guarantee
    mail/calendar SUBJECTS + BODIES + identities, email addresses, and tokens/secrets NEVER reach
    the palette. Best-effort: a redaction hiccup yields ``[redacted]`` rather than the raw value.
    """
    try:
        from himmy.services.tools.security import redact_mapping
    except Exception:  # noqa: BLE001 - redaction is load-bearing; never ship the raw value
        redact_mapping = None
    mailcal = _is_mailcal_tool(tool_name)
    try:
        if isinstance(value, dict):
            keyed = redact_mapping(value) if redact_mapping is not None else dict(value)
            return _scrub_value(keyed, mailcal=mailcal)
        # Non-dict result (a bare string/list/number): no keys to key-redact, just value-scrub.
        return _scrub_value(value, mailcal=mailcal)
    except Exception:  # noqa: BLE001
        return _REDACTED


def _coerce_result(raw: Any) -> Any:
    """The TOOL_COMPLETED payload carries ``result`` as a JSON string (himmy serialises +
    truncates it). Parse it back to structured data when possible so known connector results can
    render as rich cards; otherwise keep the (string) text."""
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if s and s[0] in "{[":
            try:
                return json.loads(s)
            except Exception:  # noqa: BLE001 - not JSON (or himmy-truncated) → keep the text
                return raw
    return raw


def _json_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return len(str(value).encode("utf-8"))


def _shrink(value: Any) -> Any:
    """Shrink a value to fit the cap WITHOUT breaking its shape: long strings are truncated, long
    lists keep their first ``_RESULT_LIST_KEEP`` items, dicts recurse. A rich card therefore still
    receives a valid dict (with its key arrays + cheapest/booking_link intact), never a cut JSON."""
    if isinstance(value, str):
        return value if len(value) <= _RESULT_STR_CAP else value[:_RESULT_STR_CAP] + "…"
    if isinstance(value, list):
        return [_shrink(v) for v in value[:_RESULT_LIST_KEEP]]
    if isinstance(value, dict):
        return {k: _shrink(v) for k, v in value.items()}
    return value


def _cap_result(value: Any) -> Any:
    """Bound one (already-redacted) result for the SSE/HTTP frame. Returns the value untouched when
    it already fits; otherwise STRUCTURE-PRESERVINGLY shrinks it (so rich cards still parse it);
    only a pathological value that is still over-budget falls back to a short marker string."""
    if _json_bytes(value) <= _RESULT_CAP_BYTES:
        return value
    shrunk = _shrink(value)
    if _json_bytes(shrunk) <= _RESULT_CAP_BYTES:
        return shrunk
    return "[result too large to display]"


def _tool_results_from_events(events: list[Any]) -> list[dict[str, Any]]:
    """Typed, REDACTED, size-capped tool results in call order — the keystone the palette uses to
    render rich connector cards (flights, buses, food, weather).

    For every ``TOOL_COMPLETED`` event (in order) emit
    ``{"tool_name", "args": <redacted dict>, "result": <redacted, ≤4KB>}``. SECURITY: both args
    AND result pass through :func:`_redact_for_render`, so mail/calendar bodies, email addresses,
    and tokens/secrets are masked before anything reaches the renderer (this is the acceptance
    test). Best-effort: a bad event is skipped, never raised.
    """
    out: list[dict[str, Any]] = []
    for event in events:
        if _event_type(event) != "TOOL_COMPLETED":
            continue
        payload = getattr(event, "payload", None) or {}
        name = payload.get("tool_name") or payload.get("tool") or payload.get("name")
        if not name:
            continue
        try:
            args = dict(payload.get("tool_args") or {})
        except Exception:  # noqa: BLE001
            args = {}
        result = _coerce_result(payload.get("result"))
        out.append({
            "tool_name": str(name),
            "args": _redact_for_render(args, tool_name=name),
            "result": _cap_result(_redact_for_render(result, tool_name=name)),
        })
    return out


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

    # Enforce the user's live permissions (Settings → Permissions): filter the tool allowlist so
    # denied tools never reach the model, and tell the persona what's off so it declines gracefully.
    try:
        from himmy_app import permissions

        spec.tools = permissions.gate_tools(list(spec.tools), cfg)
        note = permissions.disabled_note(cfg)
        if note:
            spec.instructions = list(spec.instructions) + [note]
    except Exception:  # noqa: BLE001 - permissions must never break building the agent
        pass

    # Record what Himmy does (one capture point for every path): wrap on_event so each tool the
    # agent completes is logged to the activity log, then forward to the original callback.
    import inspect

    async def _on_event_logged(event: Any) -> None:
        try:
            from himmy_app import activity

            activity.observe(event, cfg)
        except Exception:  # noqa: BLE001 - logging must never disturb a turn
            pass
        if on_event is not None:
            res = on_event(event)
            if inspect.isawaitable(res):
                await res

    runtime, _registry = build_runtime_for_spec(
        spec, provider=cfg.provider, model=cfg.model, on_event=_on_event_logged, durable_defaults=True,
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
    """The pending (approval-gated) tool calls of a checkpoint, secrets redacted.

    SECURITY — INTENTIONAL HITL EXCEPTION (conscious, not an oversight): unlike the audited
    ``tool_results`` path (which runs the full :func:`_redact_for_render` two-layer PII scrub),
    a *pending* call's args deliberately reach the approval card in cleartext for the recipient,
    subject, and body. This is load-bearing for human-in-the-loop: the user MUST see exactly what
    they're approving before a ``mail_send``/``calendar_add`` runs — a ``[redacted]`` recipient or
    body would make the consent meaningless. The leak surface is therefore scoped to the user's
    OWN approval card for an action they themselves are about to authorize, not arbitrary tool
    output. We still run :func:`redact_tool_args` (himmy's ``redact_mapping``, Layer-1 secret-key
    masking) so tokens/api_keys/authorization/cookies can NEVER ride along in a pending arg, even
    though the human-facing fields pass through. A pending call is not a "tool result", so this is
    outside the acceptance test's scrubbed path by design.
    """
    cp = store.load(checkpoint_id)
    if cp is None:
        return []
    try:
        from himmy.runtime.checkpoint import redact_tool_args as _redact
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

    - normal answer → ``{awaiting_approval: False, reply, tools, tool_results}``
    - an approval-gated tool was called → ``{awaiting_approval: True, checkpoint_id,
      pending: [{tool_name, args}], tools, tool_results, reply: ""}`` — the run is PAUSED in a
      durable checkpoint; call :func:`resume_turn` after the human approves or rejects.

    ``tool_results`` is the typed, REDACTED, size-capped list of what each tool returned (see
    :func:`_tool_results_from_events`) — the keystone the palette renders as rich cards.
    ``tools`` (names only) stays for back-compat.
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
    tool_results = _tool_results_from_events(events)

    if result.stopped_reason == "awaiting_approval" and result.checkpoint_id:
        return {
            "awaiting_approval": True, "checkpoint_id": result.checkpoint_id,
            "pending": _pending_payload(cp_store, result.checkpoint_id),
            "reply": "", "tools": tools, "tool_results": tool_results,
        }

    reply = (result.final.output_text or "").strip()
    if store is not None and session_id:
        try: store.save(session_id, result.thread)
        except Exception: pass  # noqa: BLE001, E722
    return {"awaiting_approval": False, "reply": reply, "tools": tools,
            "tool_results": tool_results}


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
        return {"awaiting_approval": False, "reply": "", "tools": [], "tool_results": [],
                "error": f"{type(exc).__name__}: {exc}"}

    tools = _tools_from_events(events)
    tool_results = _tool_results_from_events(events)
    if result.stopped_reason == "awaiting_approval" and result.checkpoint_id:
        return {
            "awaiting_approval": True, "checkpoint_id": result.checkpoint_id,
            "pending": _pending_payload(cp_store, result.checkpoint_id),
            "reply": "", "tools": tools, "tool_results": tool_results,
        }

    reply = (result.final.output_text or "").strip()
    if session_id:
        try: session_store().save(session_id, result.thread)
        except Exception: pass  # noqa: BLE001, E722
    return {"awaiting_approval": False, "reply": reply, "tools": tools,
            "tool_results": tool_results}


def _trace_label(tool_name: str) -> str:
    """Human, DOXING-SAFE label for a tool's live trace frame — reuses the same map the activity
    log uses (``activity._ACTIONS``: tool_name → (surface, label, …)). Unknown/utility tools get a
    generic "Working…". NEVER includes arg values, so a trace can't leak a recipient or a query."""
    try:
        from himmy_app import activity

        spec = activity._ACTIONS.get(tool_name)
        if spec:
            return spec[1]
    except Exception:  # noqa: BLE001 - labelling must never disturb a stream
        pass
    return "Working…"


async def answer_stream(
    message: str,
    *,
    session_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one turn LIVE: tool-trace frames as the agent acts, then token deltas, then a final
    ``done`` frame carrying the full reply + typed tool_results. Persists to ``session_id``.

    Yields event dicts:
      - ``{"type": "tool", "label": <human label>}`` — emitted as each tool is CALLED/COMPLETED,
        interleaved with the act→observe loop so the palette shows "Looking up flights…" live.
        DOXING-SAFE: only the human label, never an arg value.
      - ``{"type": "token", "text": ...}`` — the final answer revealed progressively.
      - ``{"type": "done", "reply": ..., "tools": [names], "tool_results": [...], "session_id": ...}``.

    PLUMBING: the bounded tool loop is run as a BACKGROUND asyncio.Task; the runtime's on_event hook
    pushes trace frames onto an asyncio.Queue that this generator drains and yields, so traces flow
    WHILE the loop is still running. ABORTABLE: if the consumer (the SSE endpoint) is cancelled —
    client disconnect / Stop — the ``finally`` cancels that background task, and himmy's runtime
    unwinds the CancelledError with a partial-thread save. No new endpoint needed.

    HONEST NOTE on streaming granularity: this agent runs a BLOCKING output guardrail
    (grounding/PII/injection). himmy's ``stream_task`` cannot stream past a guard that can WITHHOLD
    content, so for this agent it delivers the guarded answer as ONE chunk after the guard runs. To
    keep the premium token-by-token feel WITHOUT defeating the guard, we re-emit that guard-cleared
    text as small word-group deltas here. When a future/looser guard allows true streaming,
    stream_task's real per-token deltas flow straight through.
    """
    from himmy.agents.base_agent.task import Task

    from himmy_app.config import load_config

    cfg = load_config()
    events: list[Any] = []
    # Live tool-trace channel: the on_event hook (running inside the agent task) pushes a frame on
    # every TOOL_CALLED/TOOL_COMPLETED; this generator drains it and yields it interleaved.
    trace_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _on_event(event: Any) -> None:
        events.append(event)
        if _event_type(event) in ("TOOL_CALLED", "TOOL_COMPLETED"):
            payload = getattr(event, "payload", None) or {}
            name = payload.get("tool_name") or payload.get("tool") or payload.get("name")
            if name:
                trace_q.put_nowait({"type": "tool", "label": _trace_label(str(name))})

    cp_store = approvals_store(cfg)
    runtime, persona, spec = _build_runtime(cfg, _on_event, checkpoint_store=cp_store)

    store = session_store()
    sid = session_id or "last"
    thread = _load_thread(store, persona, sid)

    # 1) Run the bounded tool loop with HITL ON as a BACKGROUND task, and drain the trace queue
    #    while it runs — so "Looking up flights…" reaches the UI before the answer is ready.
    task = spec.make_task(_build_prompt(message, None), title="turn")
    loop_task: asyncio.Task[Any] = asyncio.create_task(
        runtime.run_agent_loop(
            persona, task, thread=thread, max_turns=cfg.max_turns,
            llm_config=spec.to_llm_config(), hitl=True,
        )
    )
    try:
        # Interleave trace frames with progress: yield each queued tool label until the loop ends.
        while not loop_task.done():
            try:
                frame = await asyncio.wait_for(trace_q.get(), timeout=0.1)
            except (asyncio.TimeoutError, TimeoutError):
                continue
            yield frame
        # Flush any trace frames queued in the final tick after the loop finished.
        while not trace_q.empty():
            yield trace_q.get_nowait()
        result = await loop_task
    except (asyncio.CancelledError, GeneratorExit):
        # The consumer went away (client disconnect / Stop). Cancel the agent so himmy's runtime
        # saves the partial thread and unwinds, then propagate the cancellation.
        loop_task.cancel()
        try:
            await loop_task
        except BaseException:  # noqa: BLE001 - swallow the cancelled task's unwinding
            pass
        raise
    finally:
        # Defensive: if we leave this generator for ANY reason with the loop still running
        # (e.g. an exception below), make sure the background task can't outlive the request.
        if not loop_task.done():
            loop_task.cancel()

    tools = _tools_from_events(events)
    tool_results = _tool_results_from_events(events)

    # 1a) An approval-gated tool paused the run — surface it and stop. The UI shows an
    #     approval card; on Approve/Cancel it calls /ask/resume to continue this run.
    if result.stopped_reason == "awaiting_approval" and result.checkpoint_id:
        yield {"type": "approval", "checkpoint_id": result.checkpoint_id,
               "pending": _pending_payload(cp_store, result.checkpoint_id)}
        yield {"type": "done", "reply": "", "tools": tools, "tool_results": tool_results,
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

    yield {"type": "done", "reply": reply, "tools": tools,
           "tool_results": tool_results, "session_id": sid}


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
