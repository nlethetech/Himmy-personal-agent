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
_MAX_VOICE_CHARS = 400     # the "how they write" one-liner Himmy matches when drafting for them
_MAX_BLOCK_CHARS = 1800    # hard cap on the prompt block we inject every turn
_MAX_DETAILS = 24          # cap the label→value "vault" (home airport, budget, …)

#: How fresh the learned layer must be before the background auto-learn re-runs (24h).
LEARN_MIN_INTERVAL_S = 24 * 3600

#: Confidence buckets allowed on a pending vault suggestion (frontend renders these verbatim).
_CONFIDENCE = ("low", "med", "high")
#: Cap on stored pending suggestions (keeps the file + the confirm UI small).
_MAX_SUGGESTIONS = 12


def _empty_layer() -> dict[str, Any]:
    return {"about": "", "voice": "", "projects": [], "people": [], "topics": [],
            "preferences": [], "details": {}}


def _clean_layer(src: dict[str, Any] | None) -> dict[str, Any]:
    src = src or {}
    layer = _empty_layer()
    layer["about"] = (str(src.get("about") or "")).strip()[:_MAX_ABOUT_CHARS]
    layer["voice"] = (str(src.get("voice") or "")).strip()[:_MAX_VOICE_CHARS]
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


def _clean_suggestion(src: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalise one pending vault suggestion, or ``None`` if it isn't usable.

    Shape (the contract the /profile/suggestions endpoint serves and the confirm UI renders):
        {"key": str, "value": str, "source": str, "confidence": "low"|"med"|"high"}
    Provenance is always ``inferred`` — a suggestion is a *candidate*, never an applied fact.
    """
    if not isinstance(src, dict):
        return None
    key = str(src.get("key") or "").strip()[:60]
    value = str(src.get("value") or "").strip()[:200]
    if not key or not value:
        return None
    conf = str(src.get("confidence") or "low").strip().lower()
    if conf not in _CONFIDENCE:
        conf = "low"
    source = str(src.get("source") or "inferred from your activity").strip()[:160]
    return {"key": key, "value": value, "source": source,
            "confidence": conf, "provenance": "inferred"}


def _clean_suggestions(raw: Any) -> list[dict[str, Any]]:
    """De-dup (by key, case-insensitive) and cap a list of pending suggestions."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for s in (raw or []):
        c = _clean_suggestion(s)
        if not c:
            continue
        kk = c["key"].lower()
        if kk in seen:
            continue
        seen.add(kk)
        out.append(c)
    return out[:_MAX_SUGGESTIONS]


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
        # Pending vault facts Himmy INFERRED but must not auto-write — they wait for the user to
        # confirm them (POST /profile/suggestions/apply) before they ever enter the real vault.
        "suggestions": _clean_suggestions(data.get("suggestions")),
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
    out["voice"] = (u.get("voice") or l.get("voice") or "").strip()  # USER voice wins
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
    if m.get("voice"):
        lines.append(
            "How they write (match this voice when drafting mail or messages on their behalf): "
            + m["voice"]
        )
    if not lines:
        return ""
    block = (
        "About the user you're helping (use it to personalize your help and actions; "
        "don't recite it back unless asked):\n" + "\n".join(f"- {x}" for x in lines)
    )
    return block[:_MAX_BLOCK_CHARS]


# ---- learning (distill the learned layer from real activity) ----------------------------
#: Every source key gather_signals produces (so a partial collect still yields a full shape).
_SIGNAL_KEYS = (
    "papers", "tags", "notes", "tasks", "interests",
    "correspondents", "task_notes", "chat_topics", "read_papers",
    # Concierge "Do" taste — what the user actually orders / thumbs (cross-pollination).
    "do_orders", "do_liked", "do_disliked",
)


def _empty_signals() -> dict[str, list[str]]:
    return {k: [] for k in _SIGNAL_KEYS}


def _looks_human(addr: str) -> bool:
    """Filter obvious non-human addresses (no-reply@, news@, deals@, …) so 'people' stays clean."""
    a = (addr or "").strip().lower()
    if not a or "@" not in a:
        return False
    local = a.split("@", 1)[0]
    bad = ("no-reply", "noreply", "no_reply", "donotreply", "do-not-reply", "do_not_reply",
           "notifications", "notification", "notify", "mailer-daemon", "postmaster", "bounce",
           "support", "automated", "news", "newsletter", "info", "hello", "team", "updates",
           "update", "marketing", "promo", "offers", "deals", "store", "shop", "account",
           "accounts", "billing", "invoice", "receipt", "welcome", "alert", "digest", "members",
           "membership", "rewards", "orders", "service", "feedback", "survey", "care", "customer",
           "sales", "contact", "help")
    return not any(b in local for b in bad)


#: Display-name / domain hints that mark a sender as a brand/newsletter rather than a person.
_BRAND_NAME_WORDS = ("deals", "team", "newsletter", "updates", "store", "shop", "offers", "rewards",
                     "no-reply", "noreply", "notifications", "support", "sales", "alerts", "digest",
                     "official", " inc", " llc", " ltd", "the ")
_BRAND_DOMAINS = ("tiktok", "walmart", "amazon", "facebookmail", "meta.com", "instagram", "linkedin",
                  "twitter", "netflix", "spotify", "uber", "paypal", "ebay", "aliexpress", "daraz",
                  "booking.", "expedia", "mailchimp", "substack", "medium.com", "quora", "reddit",
                  "youtube", "pinterest", "temu", "shein", "glassdoor", "indeed", "coursera")


def _is_brandish(name: str, addr: str) -> bool:
    """True if a sender looks like a company/newsletter (not a real individual)."""
    n = (name or "").strip().lower()
    if any(w in n for w in _BRAND_NAME_WORDS):
        return True
    domain = addr.split("@", 1)[-1] if "@" in addr else ""
    return any(b in domain for b in _BRAND_DOMAINS)


def gather_signals(cfg: HimmyConfig | None = None) -> dict[str, list[str]]:
    """Collect the high-signal traces of what the user actually works on. All best-effort.

    Synchronous: covers library, tasks (+ their sidecar notes/paper links), typed interests,
    recent chat topics, and the papers they've read most. The async-only signal — email
    correspondents — is folded in by :func:`gather_signals_async`; here it stays empty.
    """
    cfg = cfg or load_config()
    sig = _empty_signals()

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

    try:  # task sidecars — the notes they jotted on a task + the paper a task is about
        from himmy_app.tasks_extra import TaskExtrasStore

        for extra in TaskExtrasStore(cfg).all().values():
            note = (extra.get("notes") or "").strip()
            if note:
                sig["task_notes"].append(note[:300])
            ptitle = (extra.get("paper_title") or "").strip()
            if ptitle:
                sig["task_notes"].append(f"(working on paper) {ptitle}")
    except Exception:  # noqa: BLE001
        pass

    try:  # typed news interests
        from himmy_app.news import NewsService

        sig["interests"] = [str(x).strip() for x in (NewsService(cfg).get_interests() or []) if str(x).strip()]
    except Exception:  # noqa: BLE001
        pass

    try:  # recent chat topics — only thread titles / the first user message (privacy + tokens)
        from himmy_app import cli

        store = cli.session_store()
        for info in store.list_sessions(limit=12):
            thread = store.load(info.session_id)
            if thread is None:
                continue
            for msg in getattr(thread, "messages", []):
                if str(getattr(getattr(msg, "role", ""), "value", getattr(msg, "role", ""))) == "user":
                    text = (getattr(msg, "content", "") or "").strip()
                    if text:
                        sig["chat_topics"].append(text[:160])
                    break  # first user message only
    except Exception:  # noqa: BLE001
        pass

    try:  # the papers they've actually READ the most (reading-time, not just saved)
        from himmy_app.library import Library
        from himmy_app.reading import ReadingStore

        totals = ReadingStore(cfg).totals_by_item()  # item_id -> seconds
        lib = Library(cfg)
        for iid, secs in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:10]:
            if secs < 60:  # ignore drive-by opens
                continue
            it = lib.get(iid)
            title = (it.get("title") or "").strip() if it else ""
            if title:
                sig["read_papers"].append(title)
    except Exception:  # noqa: BLE001
        pass

    # Concierge "Do" taste — fold the user's REAL orders + thumbs into the durable profile, so
    # what they actually buy/eat/like (not just what they read) feeds every Cmd-K turn. Pure
    # local SQLite via the existing stores; each is its own fail-open island.
    try:  # what they've put in / ordered via the Do cart (dishes + products), grouped by place
        from himmy_app.do_concierge import DoCart

        for grp in DoCart(cfg).view().get("groups", []):
            place = (grp.get("place") or "").strip()
            for it in grp.get("items", []):
                name = (it.get("name") or "").strip()
                if not name:
                    continue
                sig["do_orders"].append(
                    f"{name} (from {place})" if place and place.lower() != "other" else name
                )
    except Exception:  # noqa: BLE001
        pass

    try:  # the tags/picks they've thumbed up / down on concierge cards
        from himmy_app.do_concierge import DoFeedback

        dismissed, weights = DoFeedback(cfg).signals()
        for tag, net in sorted(weights.items(), key=lambda kv: kv[1], reverse=True):
            t = (tag or "").strip()
            if not t:
                continue
            if net > 0:
                sig["do_liked"].append(t)
            elif net < 0:
                sig["do_disliked"].append(t)
    except Exception:  # noqa: BLE001
        pass

    # de-dup + cap each source so the prompt stays lean
    for k in sig:
        sig[k] = list(dict.fromkeys(sig[k]))[:40]
    return sig


async def gather_signals_async(cfg: HimmyConfig | None = None) -> dict[str, list[str]]:
    """:func:`gather_signals` plus the async-only email-correspondent signal.

    Email is gated by Settings → Permissions (we never touch the inbox if Email is Off) and by an
    actually-connected Google account. Each step is its own fail-open island so a flaky inbox can
    never break the learn step or the scheduler.
    """
    cfg = cfg or load_config()
    sig = gather_signals(cfg)

    try:  # email correspondents — who recurs in the inbox (humans only, frequency-ranked)
        from himmy_app import permissions

        if permissions.level_of("mail", cfg) != "off":
            from himmy.api import studio_google as g

            if g.status().connected:
                from email.utils import parseaddr

                tally: dict[str, int] = {}
                names: dict[str, str] = {}
                for m in await g.gmail_list(30):
                    name, addr = parseaddr(getattr(m, "sender", "") or "")
                    addr = (addr or "").strip().lower()
                    if not _looks_human(addr) or _is_brandish(name, addr):
                        continue
                    tally[addr] = tally.get(addr, 0) + 1
                    if name.strip() and addr not in names:
                        names[addr] = name.strip()
                for addr, _n in sorted(tally.items(), key=lambda kv: kv[1], reverse=True)[:12]:
                    if tally[addr] < 2:  # prefer senders that recur (cut newsletter noise)
                        continue
                    sig["correspondents"].append(names.get(addr) or addr)
    except Exception:  # noqa: BLE001
        pass

    sig["correspondents"] = list(dict.fromkeys(sig["correspondents"]))[:40]
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
        f"Papers they've actually read the most (by reading time):\n{_bul(sig['read_papers'])}\n\n"
        f"Tags they apply:\n{_bul(sig['tags'])}\n\n"
        f"Notes they wrote on papers:\n{_bul(sig['notes'])}\n\n"
        f"Their open tasks:\n{_bul(sig['tasks'])}\n\n"
        f"Notes / linked papers on their tasks:\n{_bul(sig['task_notes'])}\n\n"
        f"People who recur in their email:\n{_bul(sig['correspondents'])}\n\n"
        f"What they've recently asked Himmy about:\n{_bul(sig['chat_topics'])}\n\n"
        f"Topics they said they're interested in:\n{_bul(sig['interests'])}\n\n"
        f"Food / products they've actually ordered (Do cart):\n{_bul(sig['do_orders'])}\n\n"
        f"Things they've thumbed UP on the concierge:\n{_bul(sig['do_liked'])}\n\n"
        f"Things they've thumbed DOWN on the concierge:\n{_bul(sig['do_disliked'])}\n\n"
        "=== WHAT YOU PREVIOUSLY INFERRED (refine/keep what still holds, drop the stale) ===\n"
        f"{json.dumps(learned, ensure_ascii=False)}\n\n"
        "=== OUTPUT ===\n"
        "Return ONLY a JSON object with these keys:\n"
        '  "about": a 1-2 sentence summary of who they are and what they focus on,\n'
        '  "voice": 1-2 sentences on HOW they write (tone, length, formality, sign-off) inferred '
        "ONLY from their own first-person writing (their notes, chat messages); use \"\" if there "
        "is no first-person writing to judge from,\n"
        '  "projects": up to 6 concrete projects / research threads they are working on,\n'
        '  "people": up to 6 REAL INDIVIDUAL people the user actually corresponds or collaborates '
        'with (co-authors, colleagues, friends, family), each as "Name — relationship" (e.g. '
        '"Asha Rai — co-author"). NEVER include companies, brands, stores, apps, newsletters, or '
        "automated senders; if no real person is clearly evidenced, use [],\n"
        '  "topics": up to 8 subjects they care about,\n'
        '  "preferences": up to 4 inferred preferences for how to help them (omit if unclear).\n'
        "Each list is an array of short strings. Use [] for a section with no evidence and \"\" for "
        'an empty "voice". Output the JSON object and nothing else.'
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
    layer = _clean_layer(data)
    # SECURITY: the learned (machine-inferred) layer must NEVER carry the `details` vault.
    # Vault facts (home airport, budget, address, …) are *gated* — they may only enter the vault
    # via infer_suggestions → apply_suggestions after the user confirms them. The learn prompt
    # never asks for `details`, but a prompt-injected paper/note/email could try to smuggle one
    # in; dropping it here closes that silent auto-write + flights-auto-misroute path.
    layer["details"] = {}
    return layer


#: City name -> Buddha Air-style airport code, for inferring a *candidate* home airport from where
#: the user most often appears to fly/travel. Kept tiny + local (no network, no model).
_CITY_TO_AIRPORT = {
    "kathmandu": "KTM", "ktm": "KTM", "pokhara": "PKR", "pkr": "PKR",
    "bhairahawa": "BWA", "bhairawa": "BWA", "lumbini": "BWA", "siddharthanagar": "BWA",
    "biratnagar": "BIR", "bharatpur": "BHR", "chitwan": "BHR", "nepalgunj": "KEP",
    "janakpur": "JKR", "dhangadhi": "DHI", "simara": "SIF", "tumlingtar": "TMI",
}
#: Words that, near a money amount, hint at a travel/spend budget the user mentioned.
_BUDGET_HINT_WORDS = ("budget", "spend", "afford", "per day", "/day", "a day", "max", "under",
                      "around", "roughly", "trip", "travel")


def _count_mentions(needle: str, haystacks: list[str]) -> int:
    """How many distinct signal strings mention ``needle`` (case-insensitive whole-ish word)."""
    n = needle.lower()
    pat = re.compile(rf"\b{re.escape(n)}", re.IGNORECASE)
    return sum(1 for h in haystacks if pat.search(h or ""))


def infer_suggestions(sig: dict[str, list[str]], learned: dict[str, Any] | None = None,
                      existing_details: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Infer CANDIDATE vault facts from local signals — never written, only offered.

    Returns pending suggestions for: home airport, favourite cuisines, budget band. Per the
    contract, a fact is only offered at 'med'/'high' confidence when **>= 2 corroborating
    signals** back it (a single mention stays 'low'); gated keys (home airport / budget) are
    NEVER auto-written — they live here until the user confirms them. Pure-local, fail-open.
    """
    existing = {k.lower() for k in (existing_details or {})}
    learned = learned or {}
    suggestions: list[dict[str, Any]] = []

    # One flat corpus of the user's free-text + structured traces to corroborate against.
    corpus: list[str] = []
    for k in ("do_orders", "chat_topics", "task_notes", "notes", "interests", "topics",
              "do_liked", "tasks", "papers"):
        corpus.extend(sig.get(k, []) or [])
    corpus.extend(learned.get("topics") or [])
    corpus.extend(learned.get("projects") or [])
    about = str(learned.get("about") or "")
    if about:
        corpus.append(about)

    def _conf(n: int) -> str:
        return "high" if n >= 3 else "med" if n >= 2 else "low"

    # 1) Home airport — from the city that recurs most across the user's travel/chat traces.
    if "home airport" not in existing:
        city_hits: dict[str, int] = {}
        for city in _CITY_TO_AIRPORT:
            c = _count_mentions(city, corpus)
            if c:
                city_hits[city] = c
        if city_hits:
            top_city, n = max(city_hits.items(), key=lambda kv: kv[1])
            code = _CITY_TO_AIRPORT[top_city]
            suggestions.append({
                "key": "home airport", "value": code,
                "source": f"You mention {top_city.title()} most ({n}× in your activity)",
                "confidence": _conf(n)})

    # 2) Favourite cuisines — what they actually order + thumb up (a SAFE, non-gated key, but still
    #    offered for confirmation so the vault stays user-owned).
    if "favourite cuisines" not in existing and "favorite cuisines" not in existing:
        cuisines = ("momo", "pizza", "newari", "thakali", "chowmein", "burger", "sekuwa",
                    "biryani", "sushi", "korean", "thai", "indian", "italian", "chinese")
        hits: dict[str, int] = {}
        food_corpus = (sig.get("do_orders") or []) + (sig.get("do_liked") or []) \
            + (sig.get("interests") or [])
        for cu in cuisines:
            c = _count_mentions(cu, food_corpus)
            if c:
                hits[cu] = c
        ranked = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)[:3]
        if ranked:
            total = sum(c for _, c in ranked)
            suggestions.append({
                "key": "favourite cuisines",
                "value": ", ".join(cu.title() for cu, _ in ranked),
                "source": "From what you order and thumb up on the Do page",
                "confidence": _conf(total)})

    # 3) Budget band — a money amount that recurs near budget/spend language.
    if "budget" not in existing and "trip budget" not in existing:
        amounts: dict[str, int] = {}
        for line in corpus:
            low = (line or "").lower()
            if not any(w in low for w in _BUDGET_HINT_WORDS):
                continue
            for m in re.findall(r"(?:rs\.?|npr|रू)\s?([\d,]{3,})", low):
                amt = m.replace(",", "")
                if amt.isdigit():
                    amounts[amt] = amounts.get(amt, 0) + 1
        if amounts:
            top_amt, n = max(amounts.items(), key=lambda kv: kv[1])
            suggestions.append({
                "key": "budget", "value": f"NPR {int(top_amt):,}",
                "source": "A spend figure that recurs when you talk about budget",
                "confidence": _conf(n)})

    return _clean_suggestions(suggestions)


async def learn(cfg: HimmyConfig | None = None) -> dict[str, Any]:
    """Refresh the learned layer from current activity. Fail-open; returns the profile + status."""
    cfg = cfg or load_config()
    prof = load(cfg)
    sig = await gather_signals_async(cfg)
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
    # GATED auto-fill: infer candidate vault facts (home airport, cuisines, budget) but DO NOT
    # write them — stash them as pending suggestions the user must confirm. Merge with any
    # still-pending ones so an existing suggestion isn't lost, and never re-offer a fact the user
    # has already saved into the real vault. Fully fail-open.
    try:
        # Only the user-confirmed vault gates re-offers (the learned layer carries no details).
        confirmed_details = dict((prof.get("user", {}).get("details") or {}))
        inferred = infer_suggestions(sig, learned, confirmed_details)
        prof["suggestions"] = _clean_suggestions(inferred + (prof.get("suggestions") or []))
    except Exception:  # noqa: BLE001 - suggestions are a nicety, never block a good learn
        prof["suggestions"] = prof.get("suggestions") or []
    save(prof, cfg)
    return {"ok": True, "profile": prof}


# ---- gated vault auto-fill: pending suggestions the user confirms ------------------------
def get_suggestions(cfg: HimmyConfig | None = None) -> dict[str, Any]:
    """The pending vault facts Himmy inferred and is offering for confirmation.

    Backs ``GET /profile/suggestions``. Each item is
    ``{"key", "value", "source", "confidence": low|med|high}`` (plus ``provenance: inferred``).
    A suggestion never enters the real vault until :func:`apply_suggestions` confirms it.
    """
    cfg = cfg or load_config()
    return {"ok": True, "suggestions": load(cfg).get("suggestions", [])}


def apply_suggestions(keys: list[str], cfg: HimmyConfig | None = None) -> dict[str, Any]:
    """Confirm specific pending suggestions → write ONLY those into ``profile.user.details``.

    Backs ``POST /profile/suggestions/apply`` with body ``{"keys": [str]}``. This is the *only*
    path by which an inferred fact (incl. the gated home-airport / budget) ever reaches the vault —
    and only for the keys the user explicitly confirmed. Applied suggestions are then dropped from
    the pending list. Unknown keys are ignored.
    """
    cfg = cfg or load_config()
    prof = load(cfg)
    wanted = {str(k).strip().lower() for k in (keys or []) if str(k).strip()}
    pending = prof.get("suggestions") or []
    applied: list[dict[str, str]] = []

    details = dict((prof.get("user") or {}).get("details") or {})
    keep: list[dict[str, Any]] = []
    for s in pending:
        if s["key"].lower() in wanted:
            details[s["key"]] = s["value"]          # confirmed → into the real user vault
            applied.append({"key": s["key"], "value": s["value"]})
        else:
            keep.append(s)                           # not confirmed → stays pending

    # Re-clean the user layer through the vault rules (caps keys/values), drop applied suggestions.
    user_layer = dict(prof.get("user") or {})
    user_layer["details"] = details
    prof["user"] = _clean_layer(user_layer)
    prof["suggestions"] = _clean_suggestions(keep)
    save(prof, cfg)
    return {"ok": True, "applied": applied, "profile": prof}


async def maybe_auto_learn(cfg: HimmyConfig | None = None) -> dict[str, Any]:
    """Background-driver entry: refresh the learned layer only when it's stale AND there's signal.

    Bounds cost — at most one model call per :data:`LEARN_MIN_INTERVAL_S` — and is fully fail-open
    so a hiccup never disturbs the scheduler that calls it. Returns ``{ok, profile, ...}`` like
    :func:`learn`, with ``skipped`` set when nothing was done.
    """
    cfg = cfg or load_config()
    prof = load(cfg)
    if time.time() - float(prof.get("learned_at") or 0.0) <= LEARN_MIN_INTERVAL_S:
        return {"ok": False, "skipped": "fresh", "profile": prof}
    try:
        sig = await gather_signals_async(cfg)
    except Exception:  # noqa: BLE001 - signal collection must never break the loop
        sig = {}
    if not any(sig.values()):
        return {"ok": False, "skipped": "no_signal", "profile": prof}
    return await learn(cfg)


__all__ = ["load", "save_user_layer", "render_for_prompt", "gather_signals",
           "gather_signals_async", "learn", "maybe_auto_learn",
           "infer_suggestions", "get_suggestions", "apply_suggestions"]
