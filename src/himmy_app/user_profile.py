"""Himmy's living model of YOU — the personalization layer.

A great daily assistant doesn't just answer; it knows who it's helping. This module keeps a
small, durable "about you" that the server prepends to every Cmd-K turn, so every answer and
action is personalized. It has two layers:

  * ``user``    — what you've told Himmy directly (edited in Settings). Never auto-changed.
  * ``learned`` — what Himmy has picked up from your real activity (refreshed by :func:`learn`).

:func:`render_for_prompt` collapses both into a compact block (user facts take priority).
:func:`learn` distills the learned layer from real signals — your library, the tags/notes/
highlights you wrote, your open tasks, your typed interests — via one cheap model call. It is
FAIL-OPEN: no API key, no activity, or a flaky call simply leaves the profile unchanged.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx

from himmy_app.config import HimmyConfig, load_config

#: The five sections of a profile layer. ``about`` is a short paragraph; the rest are lists.
_LIST_SECTIONS = ("projects", "people", "topics", "preferences")
_MAX_ITEMS = 12            # cap list length per section (keeps the injected block small)
_MAX_ABOUT_CHARS = 600
_MAX_BLOCK_CHARS = 1800    # hard cap on the prompt block we inject every turn
_MAX_DETAILS = 24          # cap the label→value "vault" (home airport, budget, …)


def _empty_layer() -> dict[str, Any]:
    return {"about": "", "projects": [], "people": [], "topics": [], "preferences": [], "details": {}}


def _clean_layer(src: dict[str, Any] | None) -> dict[str, Any]:
    src = src or {}
    layer = _empty_layer()
    layer["about"] = (str(src.get("about") or "")).strip()[:_MAX_ABOUT_CHARS]
    for k in _LIST_SECTIONS:
        items = [str(x).strip() for x in (src.get(k) or []) if str(x).strip()]
        # de-dup case-insensitively, preserve order
        seen: set[str] = set()
        out: list[str] = []
        for x in items:
            key = x.lower()
            if key not in seen:
                seen.add(key)
                out.append(x)
        layer[k] = out[:_MAX_ITEMS]
    # details: a small label→value vault Himmy uses when ACTING for the user (home airport,
    # preferred airline, budget, home address, dietary, loyalty #, spend limit, …).
    details: dict[str, str] = {}
    raw = src.get("details")
    if isinstance(raw, dict):
        for k, v in raw.items():
            kk, vv = str(k).strip()[:60], str(v).strip()[:200]
            if kk and vv:
                details[kk] = vv
    layer["details"] = dict(list(details.items())[:_MAX_DETAILS])
    return layer


def _path(cfg: HimmyConfig):
    return cfg.data_dir / "user_profile.json"


# ---- persistence ------------------------------------------------------------------------
def load(cfg: HimmyConfig | None = None) -> dict[str, Any]:
    """The full profile: ``{user, learned, learned_at}``, always well-formed."""
    cfg = cfg or load_config()
    try:
        data = json.loads(_path(cfg).read_text())
    except Exception:  # noqa: BLE001 - first run / corrupt file → empty profile
        data = {}
    return {
        "user": _clean_layer(data.get("user")),
        "learned": _clean_layer(data.get("learned")),
        "learned_at": float(data.get("learned_at") or 0.0),
    }


def save(prof: dict[str, Any], cfg: HimmyConfig | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prof, indent=2))
    return prof


def save_user_layer(sections: dict[str, Any], cfg: HimmyConfig | None = None) -> dict[str, Any]:
    """Persist what the user typed in Settings. Leaves the learned layer untouched."""
    cfg = cfg or load_config()
    prof = load(cfg)
    prof["user"] = _clean_layer(sections)
    return save(prof, cfg)


# ---- render (the always-on personalization block) ---------------------------------------
def _merge_layers(prof: dict[str, Any]) -> dict[str, Any]:
    """User facts take priority; learned facts fill in. De-duped, capped."""
    u, l = prof.get("user", {}), prof.get("learned", {})
    out = _empty_layer()
    out["about"] = (u.get("about") or l.get("about") or "").strip()
    for k in _LIST_SECTIONS:
        seen: set[str] = set()
        items: list[str] = []
        for src in ((u.get(k) or []), (l.get(k) or [])):
            for x in src:
                key = str(x).lower()
                if key not in seen:
                    seen.add(key)
                    items.append(x)
        out[k] = items[:_MAX_ITEMS]
    out["details"] = {**(l.get("details") or {}), **(u.get("details") or {})}  # user vault wins
    return out


def render_for_prompt(prof: dict[str, Any] | None = None, cfg: HimmyConfig | None = None) -> str:
    """A compact "About the user" block to prepend to a turn — or ``""`` if nothing is known."""
    prof = prof if prof is not None else load(cfg)
    m = _merge_layers(prof)
    lines: list[str] = []
    if m["about"]:
        lines.append(m["about"])
    for k, label in (
        ("projects", "Current projects"),
        ("people", "Key people"),
        ("topics", "Topics they care about"),
        ("preferences", "Preferences for how Himmy should help"),
    ):
        if m[k]:
            lines.append(f"{label}: " + "; ".join(m[k]))
    if m.get("details"):
        det = "; ".join(f"{k}: {v}" for k, v in m["details"].items())
        lines.append(
            "Their details (use these when acting on their behalf — booking, drafting, planning — "
            "so you never have to re-ask): " + det
        )
    if not lines:
        return ""
    block = (
        "About the user you're helping (use it to personalize your help and actions; "
        "don't recite it back unless asked):\n" + "\n".join(f"- {x}" for x in lines)
    )
    return block[:_MAX_BLOCK_CHARS]


# ---- learning (distill the learned layer from real activity) ----------------------------
def gather_signals(cfg: HimmyConfig | None = None) -> dict[str, list[str]]:
    """Collect the high-signal traces of what the user actually works on. All best-effort."""
    cfg = cfg or load_config()
    sig: dict[str, list[str]] = {
        "papers": [], "tags": [], "notes": [], "tasks": [], "interests": []
    }

    try:  # library: titles, the tags they apply, and the notes they write (high signal)
        from himmy_app.library import Library

        for it in Library(cfg).list()[:80]:
            title = (it.get("title") or "").strip()
            if title:
                sig["papers"].append(title)
            for tag in (it.get("tags") or []):
                if str(tag).strip():
                    sig["tags"].append(str(tag).strip())
            note = (it.get("notes") or "").strip()
            if note:
                sig["notes"].append(note[:300])
    except Exception:  # noqa: BLE001
        pass

    try:  # open tasks — what they're actively trying to get done
        from himmy.api.studio_tasks import get_tasks_store

        for t in get_tasks_store().list():
            if getattr(t, "done", False):
                continue
            title = getattr(t, "title", None)
            if title:
                sig["tasks"].append(str(title))
    except Exception:  # noqa: BLE001
        pass

    try:  # typed news interests
        from himmy_app.news import NewsService

        sig["interests"] = [str(x).strip() for x in (NewsService(cfg).get_interests() or []) if str(x).strip()]
    except Exception:  # noqa: BLE001
        pass

    # de-dup + cap each source so the prompt stays lean
    for k in sig:
        sig[k] = list(dict.fromkeys(sig[k]))[:40]
    return sig


def _build_learn_prompt(prof: dict[str, Any], sig: dict[str, list[str]]) -> str:
    learned = prof.get("learned") or _empty_layer()

    def _bul(xs: list[str]) -> str:
        return "\n".join(f"  - {x}" for x in xs) if xs else "  (none)"

    return (
        "You are building a concise profile of a person from traces of their own research and "
        "productivity activity, so an assistant can personalize its help. Infer ONLY what the "
        "evidence supports — do not invent. Write in the third person ('They ...'). Be specific "
        "and brief.\n\n"
        "=== EVIDENCE ===\n"
        f"Papers in their library:\n{_bul(sig['papers'])}\n\n"
        f"Tags they apply:\n{_bul(sig['tags'])}\n\n"
        f"Notes they wrote on papers:\n{_bul(sig['notes'])}\n\n"
        f"Their open tasks:\n{_bul(sig['tasks'])}\n\n"
        f"Topics they said they're interested in:\n{_bul(sig['interests'])}\n\n"
        "=== WHAT YOU PREVIOUSLY INFERRED (refine/keep what still holds, drop the stale) ===\n"
        f"{json.dumps(learned, ensure_ascii=False)}\n\n"
        "=== OUTPUT ===\n"
        "Return ONLY a JSON object with these keys:\n"
        '  "about": a 1-2 sentence summary of who they are and what they focus on,\n'
        '  "projects": up to 6 concrete projects / research threads they are working on,\n'
        '  "people": up to 6 named collaborators / authors / contacts that recur (name them),\n'
        '  "topics": up to 8 subjects they care about,\n'
        '  "preferences": up to 4 inferred preferences for how to help them (omit if unclear).\n'
        "Each list is an array of short strings. Use [] for a section with no evidence. "
        "Output the JSON object and nothing else."
    )


def _parse_learned(content: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", content or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return _clean_layer(data)


async def learn(cfg: HimmyConfig | None = None) -> dict[str, Any]:
    """Refresh the learned layer from current activity. Fail-open; returns the profile + status."""
    cfg = cfg or load_config()
    prof = load(cfg)
    sig = gather_signals(cfg)
    if not any(sig.values()):
        return {"ok": False, "profile": prof,
                "message": "Not enough to go on yet — save a few papers, add tags or notes, "
                           "or set some interests, then try again."}
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return {"ok": False, "profile": prof,
                "message": "Connect a model (set OPENROUTER_API_KEY) so Himmy can learn about you."}
    model = os.environ.get("HIMMY_APP_MODEL", "google/gemini-2.5-flash")
    prompt = _build_learn_prompt(prof, sig)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "temperature": 0.2,
                      "messages": [{"role": "user", "content": prompt}]},
            )
            resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "profile": prof,
                "message": f"Couldn't reach the model: {type(exc).__name__}"}
    learned = _parse_learned(content)
    if learned is None:
        return {"ok": False, "profile": prof,
                "message": "Himmy couldn't form a clear picture this time — try again later."}
    prof["learned"] = learned
    prof["learned_at"] = time.time()
    save(prof, cfg)
    return {"ok": True, "profile": prof}


__all__ = ["load", "save_user_layer", "render_for_prompt", "gather_signals", "learn"]
