"""Deep research mode — an explicit, multi-step orchestration over the himmy runtime.

The Cmd-K palette answers in one reactive tool loop. "Deep research" is the slower,
deliberate sibling: it PLANS the question, FANS OUT two researchers concurrently (one
that reads the user's own library, one that searches the wider web), SYNTHESIZES a single
cited brief from both, then runs one REFLECT polish pass. It returns the plan, the cited
synthesis, and the gathered sources so the UI can show its work — never automatic, always
behind an explicit button.

It reuses ``himmy_app.cli._build_runtime`` (the same spec, persona, durable paths, and
OpenRouter/gemini-2.5-flash provider the rest of the app uses), so the deep-research persona
inherits Himmy's grounding/citation instructions verbatim. Built on himmy orchestrators:
``PlannerOrchestrator`` (plan) and ``orchestrators.reflection.reflect`` (final polish).

WHY THE LIBRARY RESEARCHER RETRIEVES DIRECTLY (not via an LLM tool loop):
The reliable, grounded core of this feature is the user's OWN library. Asking the model to
"decide" to call ``ask_papers`` is fragile — on a vague question gemini-2.5-flash often
replies with a clarifying question and never invokes the tool, so no grounded passages come
back. Instead we call the SAME RAG retriever ``ask_papers`` wraps —
``papers_rag._get_index(cfg).search(question)`` — DIRECTLY in Python. That deterministically
returns ranked passages, each with a real citation, regardless of the model's mood. The
synthesist then writes the brief FROM those verbatim passages, and the sources list is built
from the retriever's own citations (not scraped out of model prose) — so a library-grounded,
cited brief is produced every time the library has relevant material.

WHY THE WEB RESEARCHER IS BEST-EFFORT:
himmy's ``web_search`` defaults to a keyless DuckDuckGo HTML scrape, which is frequently
blocked/empty on a developer machine (no ``HIMMY_SEARCH_API_KEY`` for tavily/brave). So the
web leg is explicitly OPTIONAL: it runs the reactive tool loop, but an empty/failed web
search never sinks the brief — the library findings still produce a real cited synthesis.
Set ``HIMMY_SEARCH_BACKEND=tavily`` + ``HIMMY_SEARCH_API_KEY`` to light up the web leg.

Robustness: the whole run is bounded by a wall-clock deadline, every stage degrades
gracefully (a failed planner still researches; a failed worker still leaves the other's
findings; a failed reflect keeps the un-polished synthesis), and any hard failure returns a
well-formed ``{brief, sources, steps}`` rather than raising into the endpoint.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

#: Wall-clock budget for one deep-research run (seconds). Plan + concurrent researchers +
#: synthesis + reflect on gemini-2.5-flash typically land well inside this; the deadline
#: only guards against a stuck provider call.
_DEADLINE_SECONDS = 180.0

#: Per-leg cap for the LIBRARY retrieval. The server WARMS this index at startup (see
#: server.py:_warm_rag_index), so retrieval is normally ~1-2s. This generous bound only
#: matters if the user fires deep research while the cold first build is still in flight
#: (a full build over every PDF is CPU-bound and can take ~2-3 min); even then a stuck index
#: can never hang the brief past this cap — the web findings + synthesis still complete.
_LIBRARY_TIMEOUT = 170.0

#: Cap for the (best-effort) WEB researcher's reactive tool loop.
_WEB_TIMEOUT = 70.0

#: How many library passages to retrieve and ground the brief in.
_LIBRARY_TOP_K = 6

#: Tool set for the web researcher (a subset of agent.yaml's allowlist, already wired).
_WEB_TOOLS = ["web_search", "web_fetch", "current_time"]

#: A "Source:" / "Source -" line a grounding persona emits per citation. We harvest these
#: from the WEB researcher's answer (the library sources come from the retriever directly).
_SOURCE_RE = re.compile(r"^\s*(?:[-*•]\s*)?source\s*[:\-–]\s*(.+\S)", re.IGNORECASE)


def _persona_for(spec: Any, name: str, mission: str) -> Any:
    """Clone the spec's grounded persona with a researcher-specific name + mission.

    Reusing ``spec.to_persona()`` keeps Himmy's absolute grounding/citation rules; we only
    append the worker's narrow mission so the planner/fan-out turns stay on-task.
    """
    persona = spec.to_persona()
    try:
        persona = persona.model_copy(deep=True)
    except Exception:  # noqa: BLE001 - some persona impls aren't pydantic; mutate in place
        pass
    try:
        persona.name = name
        base = (getattr(persona, "description", "") or "").strip()
        persona.description = f"{base}\n\nYour focus for this task: {mission}".strip()
    except Exception:  # noqa: BLE001 - never fail the run over a cosmetic field
        pass
    return persona


def _extract_sources(*answers: str) -> list[str]:
    """Harvest distinct ``Source:`` citation lines from a researcher's answer."""
    out: list[str] = []
    seen: set[str] = set()
    for answer in answers:
        for line in (answer or "").splitlines():
            m = _SOURCE_RE.match(line)
            if not m:
                continue
            src = m.group(1).strip()
            key = src.lower()
            if src and key not in seen:
                seen.add(key)
                out.append(src)
    return out


def _merge_sources(*lists: list[str]) -> list[str]:
    """Concatenate source lists, dropping case-insensitive duplicates, preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for lst in lists:
        for src in lst:
            src = (src or "").strip()
            key = src.lower()
            if src and key not in seen:
                seen.add(key)
                out.append(src)
    return out


async def _plan(runtime: Any, spec: Any, question: str) -> list[str]:
    """Ask the planner for an ordered research plan; degrade to a sane default."""
    from himmy.orchestrators.planner import PlannerOrchestrator

    persona = _persona_for(
        spec,
        "research-planner",
        "Break the research question into a short ordered plan of concrete steps.",
    )
    try:
        planner = PlannerOrchestrator(runtime, max_steps=5)
        steps = await planner.plan_only(question, persona)
        steps = [s.strip() for s in steps if s and s.strip()]
        if steps:
            return steps[:5]
    except Exception:  # noqa: BLE001 - planning is best-effort; fall back below
        pass
    return [
        "Search the user's own library for directly relevant papers.",
        "Search the wider web for current, authoritative sources.",
        "Synthesise the findings into one cited answer.",
    ]


def _library_retrieve_blocking(cfg: Any, question: str) -> list[dict[str, Any]]:
    """Retrieve ranked library passages for ``question`` on a private event loop.

    Deliberately SYNCHRONOUS and self-contained so it can be handed to a worker thread (its
    fastembed/ONNX work is blocking, CPU-bound, and does NOT yield to asyncio). Calls the
    SAME retriever ``ask_papers`` wraps — so the deep-research library leg is grounded
    identically to the Cmd-K box, but DETERMINISTICALLY (no model decision to skip the tool).
    """
    from himmy_app.connectors.papers_rag import _get_index

    async def _go() -> list[dict[str, Any]]:
        return await _get_index(cfg).search(question, top_k=_LIBRARY_TOP_K)

    return asyncio.run(_go())


def _passages_block(passages: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Render retrieved passages as a grounded text block + their citation list."""
    if not passages:
        return "", []
    lines: list[str] = []
    sources: list[str] = []
    seen: set[str] = set()
    for p in passages:
        cite = (p.get("citation") or "").strip()
        passage = (p.get("passage") or "").strip()
        if not passage:
            continue
        label = cite or (p.get("title") or "library passage")
        lines.append(f"[{label}]\n{passage}")
        if cite:
            key = cite.lower()
            if key not in seen:
                seen.add(key)
                sources.append(cite)
    return "\n\n".join(lines), sources


async def _library_leg(cfg: Any, question: str) -> tuple[str, list[str]]:
    """Retrieve grounded library passages (deterministic); degrade to ("", []) on any failure.

    Runs the blocking retrieval in a thread, bounded by ``_LIBRARY_TIMEOUT`` so a cold/heavy
    fastembed index can never hang the brief. Returns (passages_text, citations).
    """
    loop = asyncio.get_running_loop()
    try:
        fut = loop.run_in_executor(None, lambda: _library_retrieve_blocking(cfg, question))
        passages = await asyncio.wait_for(asyncio.shield(fut), timeout=_LIBRARY_TIMEOUT)
        return _passages_block(passages or [])
    except Exception:  # noqa: BLE001 - incl. TimeoutError; the web leg + synthesis go on
        return "", []


def _web_worker_blocking(cfg: Any, question: str) -> str:
    """Run the WEB researcher to completion on a private event loop + runtime; return its answer.

    Self-contained for a worker thread: builds its OWN runtime (runtimes are tied to the loop
    that drives them) and runs the SAME reactive tool-loop the Cmd-K /ask box uses, with
    ``tool_names`` constrained to the web tools. Best-effort: if the keyless DuckDuckGo backend
    is blocked/empty, this simply returns little — the caller treats web as optional.
    """
    from himmy.agents.base_agent.task import Task
    from himmy.agents.base_agent.thread import ChatThread
    from himmy.services.inference.models import LLMConfig, ResponseFormat

    from himmy_app.cli import _build_runtime

    async def _ev(_e: Any) -> None:
        return None

    async def _go() -> str:
        runtime, _persona, spec = _build_runtime(cfg, _ev)
        persona = _persona_for(
            spec,
            "web_researcher",
            "Research the wider literature with web_search/web_fetch; cite each finding on "
            "its own 'Source:' line with the page title and URL. Never invent a source. If "
            "web search returns nothing, say so plainly in one line.",
        )
        base = spec.to_llm_config()
        try:
            llm = LLMConfig(
                model_key=getattr(base, "model_key", "default"),
                temperature=getattr(base, "temperature", 0.2),
                response_format=ResponseFormat.AUTO_TOOLS,
            )
        except Exception:  # noqa: BLE001 - fall back to the spec's own config
            llm = base
        objective = (
            "Use web_search (and web_fetch on the best hits) to research the wider, current "
            "literature on this question, then report concrete findings, each on its own line "
            "with a 'Source:' citation (title + URL). If web search returns no usable results, "
            "reply with exactly one line saying the web search returned nothing.\n\n"
            f"Question: {question}"
        )
        task = Task(title="research-web", prompt=objective, context={"tool_names": _WEB_TOOLS})
        result = await runtime.run_agent_loop(
            persona,
            task,
            thread=ChatThread(agent_id=persona.agent_id),
            max_turns=5,
            llm_config=llm,
        )
        return (result.final.output_text or "").strip()

    return asyncio.run(_go())


async def _web_leg(cfg: Any, question: str) -> str:
    """Run the best-effort web researcher in a thread; degrade any failure to "".

    Bounded by ``_WEB_TIMEOUT`` so a slow provider/search call can't stall the brief. The
    web leg is OPTIONAL — an empty/failed result never sinks the run.
    """
    loop = asyncio.get_running_loop()
    try:
        fut = loop.run_in_executor(None, lambda: _web_worker_blocking(cfg, question))
        return await asyncio.wait_for(asyncio.shield(fut), timeout=_WEB_TIMEOUT)
    except Exception:  # noqa: BLE001 - incl. TimeoutError; library findings still stand
        return ""


def _web_is_empty(web_answer: str) -> bool:
    """True when the web leg found nothing usable (blocked DDG, or an explicit 'nothing' line)."""
    txt = (web_answer or "").strip()
    if not txt:
        return True
    low = txt.lower()
    if "source:" in low:  # it produced at least one citation → has real findings
        return False
    # No citations and a short "nothing" disclaimer → treat as empty.
    if len(txt) < 400 and ("nothing" in low or "no usable" in low or "no result" in low):
        return True
    return False


async def _synthesize(
    runtime: Any,
    spec: Any,
    question: str,
    library_block: str,
    library_sources: list[str],
    web_answer: str,
    *,
    web_empty: bool,
) -> str:
    """Fuse the library passages + (optional) web findings into one cited brief.

    The brief is grounded in the verbatim retrieved passages; the synthesist must cite each
    library claim with the bracketed citation shown above its passage and reproduce those in
    a closing 'Sources' section. Web findings are folded in only when present.
    """
    from himmy.agents.base_agent.task import Task
    from himmy.agents.base_agent.thread import ChatThread

    persona = _persona_for(
        spec,
        "research-synthesist",
        "Fuse the retrieved library passages (and any web findings) into one clear, cited "
        "brief. Ground every claim in the material provided; keep library-grounded claims and "
        "outside-web claims clearly attributed; end with a 'Sources' section.",
    )

    lib_section = (
        f"== Passages retrieved from the user's OWN library "
        f"(each is preceded by its citation in [brackets]) ==\n{library_block}"
        if library_block
        else "== The user's library had no passages relevant to this question. =="
    )
    web_section = (
        "== The web search returned nothing usable (no search-provider key configured); "
        "rely on the library passages above and do NOT invent outside sources. =="
        if web_empty
        else f"== Findings from the wider web ==\n{web_answer}"
    )

    prompt = (
        f"Research question:\n{question}\n\n"
        f"{lib_section}\n\n"
        f"{web_section}\n\n"
        "Write a single, well-structured brief that directly answers the question. GROUND "
        "every claim in the material above — do NOT add facts, papers, or sources that are "
        "not present. Cite each library-grounded claim using the bracketed citation shown "
        "above its passage. Keep 'from your library' and 'from the web' clearly distinguished. "
        "Finish with a 'Sources' section that lists, on its own 'Source:' line each, every "
        "library citation you used and every web 'Source:' you relied on. If neither the "
        "library nor the web yielded anything relevant, say so plainly and suggest a narrower "
        "question — do not fabricate."
    )
    result = await runtime.run_task_detailed(
        persona,
        Task(title="deep-research-synthesis", prompt=prompt, context={"tool_names": []}),
        thread=ChatThread(agent_id=persona.agent_id),
        llm_config=spec.to_llm_config(),
    )
    return (result.output_text or "").strip()


async def _polish(runtime: Any, spec: Any, draft: str) -> str:
    """One reflect pass over the synthesis; returns the draft unchanged on any failure."""
    from himmy.orchestrators.reflection import reflect

    if not draft:
        return draft
    persona = _persona_for(
        spec,
        "research-reviewer",
        "Critique and improve the brief without inventing facts or sources.",
    )
    try:
        improved = await reflect(
            runtime,
            draft,
            persona=persona,
            criteria=(
                "accuracy, grounding (no claim or source beyond the draft), clarity, and "
                "completeness; keep every existing citation"
            ),
        )
        return (improved or draft).strip() or draft
    except Exception:  # noqa: BLE001 - polish is optional; keep the synthesis
        return draft


async def deep_research(question: str) -> dict[str, Any]:
    """Run the full deep-research pipeline and return ``{brief, sources, steps, ...}``.

    Always returns a well-formed dict (never raises): ``brief`` is the cited synthesis,
    ``sources`` the citations actually used (library citations from the retriever + any web
    'Source:' lines), ``steps`` the plan plus a status line per stage, and ``ok`` whether a
    usable grounded brief was produced. Bounded by ``_DEADLINE_SECONDS``.
    """
    question = (question or "").strip()
    if not question:
        return {
            "ok": False,
            "brief": "Ask a research question to start a deep dive.",
            "sources": [],
            "steps": [],
        }

    from himmy_app.cli import _build_runtime
    from himmy_app.config import load_config

    cfg = load_config()

    async def _on_event(_event: Any) -> None:  # events flow to the audit spine; unused here
        return None

    async def _run() -> dict[str, Any]:
        runtime, _persona, spec = _build_runtime(cfg, _on_event)
        steps: list[str] = []

        # 1) Plan.
        plan = await _plan(runtime, spec, question)
        steps.extend(plan)

        # 2) Fan out the two legs CONCURRENTLY: deterministic library retrieval + best-effort
        #    web researcher (each in its own thread so blocking work can't stall the request).
        library_block, library_sources, web_answer = "", [], ""
        try:
            (library_block, library_sources), web_answer = await asyncio.gather(
                _library_leg(cfg, question),
                _web_leg(cfg, question),
            )
        except Exception as exc:  # noqa: BLE001 - degrade to whatever we have
            steps.append(f"Researcher fan-out failed: {type(exc).__name__}: {exc}")

        web_empty = _web_is_empty(web_answer)
        n_lib = len(library_sources)
        if n_lib and not web_empty:
            steps.append(
                f"Gathered {n_lib} grounded passage(s) from your library and findings from "
                "the web (in parallel)."
            )
        elif n_lib:
            steps.append(
                f"Gathered {n_lib} grounded passage(s) from your library "
                "(web search returned nothing usable)."
            )
        elif not web_empty:
            steps.append(
                "Your library had nothing relevant; gathered findings from the web instead."
            )
        else:
            steps.append(
                "Neither your library nor the web returned anything relevant to this question."
            )

        web_sources = _extract_sources(web_answer) if not web_empty else []
        sources = _merge_sources(library_sources, web_sources)

        # 3) Synthesize one cited brief from the grounded material.
        try:
            brief = await _synthesize(
                runtime, spec, question, library_block, library_sources, web_answer,
                web_empty=web_empty,
            )
            steps.append("Synthesised a single cited brief from all findings.")
        except Exception as exc:  # noqa: BLE001
            # Fall back to the raw grounded material so the user still gets something real.
            brief = "\n\n".join(p for p in [library_block, web_answer] if p).strip()
            steps.append(f"Synthesis failed: {type(exc).__name__}: {exc}")

        # 4) Reflect / polish.
        polished = await _polish(runtime, spec, brief)
        if polished and polished != brief:
            steps.append("Ran a reflection pass to sharpen the brief.")
        brief = polished or brief

        # Sources: the retriever's library citations + web Source: lines + any extra the
        # final brief reproduced. The library citations are authoritative (real passages).
        sources = _merge_sources(library_sources, web_sources, _extract_sources(brief))

        # We have a real, grounded result if we got library passages or non-empty web findings.
        grounded = bool(library_sources) or (not web_empty and bool(web_answer))

        return {
            "ok": bool(brief) and grounded,
            "brief": brief or "I couldn't find anything relevant in your library or on the web "
                              "for that question. Try a narrower or differently-worded question.",
            "sources": sources,
            "steps": steps,
        }

    try:
        return await asyncio.wait_for(_run(), timeout=_DEADLINE_SECONDS)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "brief": (
                "The deep-research run took too long and was stopped. Try a narrower "
                "question, or use the regular Ask box for a quick answer."
            ),
            "sources": [],
            "steps": ["Timed out before finishing."],
        }
    except Exception as exc:  # noqa: BLE001 - the endpoint should never see a raw exception
        return {
            "ok": False,
            "brief": f"Deep research hit an error: {type(exc).__name__}: {exc}",
            "sources": [],
            "steps": [],
        }


__all__ = ["deep_research"]
