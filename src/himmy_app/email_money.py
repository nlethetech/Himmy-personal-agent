"""Find the user's SPENDING in their email — receipts, order confirmations, charges, bills.

Money lives in Himmy's brain (the finance chat tools), and this is how Himmy fills the ledger by
itself: it reads recent Gmail, keeps the likely-spending emails, and runs ONE cheap model pass that
turns each genuine purchase into a structured expense. Promotions, OTPs, shipping pings, refunds and
balance alerts are dropped. Filed expenses are tagged ``source="email"`` and every scanned message
is remembered (``finance_email_seen.json``) so a re-scan never files the same receipt twice.

Reuses the framework's Gmail reader (``studio_google.gmail_list``) and the app's expense ledger and
inference provider — nothing here re-implements mail or the model.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from himmy_app.config import HimmyConfig, load_config
from himmy_app.finance import CATEGORIES, ExpenseStore, _clean_category, _clean_date, _to_amount

#: Words that mark an email as *likely* a real spend — a cheap pre-filter so the model only ever
#: looks at a handful of candidates, not the whole inbox.
_SPEND_HINTS = (
    "receipt", "order", "invoice", "payment", "paid", "purchase", "transaction", "charged",
    "your order", "order confirmation", "order confirmed", "payment received", "payment confirmation",
    "subscription", "renewed", "renewal", "billed", "bill", "total", "amount", "checkout",
    "thanks for your order", "thank you for your purchase", "e-receipt", "tax invoice",
)
#: Words that usually mean it is NOT a spend (marketing / noise) — drop even if a hint matched.
_NOISE_HINTS = (
    "% off", "sale", "deal", "coupon", "promo", "newsletter", "unsubscribe", "verify your",
    "one-time password", "otp", "security code", "shipped", "out for delivery", "refund",
    "has been refunded", "balance is", "statement is ready", "we miss you", "flash sale",
)
#: Currency default for email-found spend (the user is currently in the US — see About-Me location).
_DEFAULT_CCY = "USD"
#: At most this many email BODIES are fetched per scan (for receipts whose amount isn't in the
#: snippet) — bounds cost while still catching spend that only shows up in the full email.
MAX_BODY_FETCH = 8
#: A money-looking pattern: a currency mark before a number, or a bare decimal amount (12.34).
_MONEY_RE = re.compile(r"(?:[$₹£€]|\b(?:rs|npr|usd|inr|eur|gbp)\b\.?)\s*\d|\d[\d,]*\.\d{2}\b", re.I)


def _has_amount(text: str) -> bool:
    return bool(_MONEY_RE.search(text or ""))


def _seen_path(cfg: HimmyConfig):
    return cfg.data_dir / "finance_email_seen.json"


def _load_seen(cfg: HimmyConfig) -> set[str]:
    try:
        d = json.loads(_seen_path(cfg).read_text(encoding="utf-8"))
        return set(d.get("ids") or [])
    except Exception:  # noqa: BLE001
        return set()


def _save_seen(cfg: HimmyConfig, ids: set[str]) -> None:
    with contextlib.suppress(Exception):
        # keep it bounded — the most recent ~2000 message ids is plenty to stop re-filing
        keep = sorted(ids)[-2000:]
        _seen_path(cfg).write_text(json.dumps({"ids": keep}), encoding="utf-8")


def _looks_like_spend(text: str) -> bool:
    t = text.lower()
    if any(n in t for n in _NOISE_HINTS):
        return False
    return any(h in t for h in _SPEND_HINTS)


def _system_prompt() -> str:
    cats = ", ".join(CATEGORIES)
    return (
        "You extract the user's real SPENDING from a list of their emails. For each email that is "
        "clearly the user PAYING money — a receipt, order/payment confirmation, a charge, a bill "
        "paid, a subscription renewal, a ride or food-delivery charge — output ONE expense. IGNORE: "
        "promotions/sales/coupons, newsletters, OTP/verification codes, shipping or delivery updates "
        "with no charge, refunds, bank balance or statement alerts, and anything where you cannot "
        "find an amount. Return ONLY a JSON array (no prose). Each item: "
        '{"message_id": "<exact id given>", "merchant": "<who was paid>", "amount": <number>, '
        '"currency": "<e.g. USD, NPR>", "date": "<YYYY-MM-DD>", "category": "<one of: ' + cats + '>", '
        '"confidence": <0..1 how sure this is a real spend>}. Use the email\'s date if the text has '
        "none. Omit any email that is not a clear spend or has no amount. Use the EXACT message_id."
    )


def _candidates(messages: list[Any]) -> list[dict[str, str]]:
    """Recent messages narrowed to those that look like a spend, as light dicts for the model."""
    out: list[dict[str, str]] = []
    for m in messages:
        mid = str(getattr(m, "id", "") or "")
        sender = str(getattr(m, "sender", "") or "")
        subject = str(getattr(m, "subject", "") or "")
        snippet = str(getattr(m, "snippet", "") or "")
        date = str(getattr(m, "date", "") or "")
        if not mid:
            continue
        if not _looks_like_spend(f"{subject}\n{snippet}"):
            continue
        out.append({"id": mid, "from": sender, "subject": subject,
                    "snippet": snippet[:400], "text": snippet[:400], "date": date})
    return out


async def _enrich_with_bodies(cands: list[dict[str, str]], g: Any) -> int:
    """For candidates whose subject+snippet has NO amount, fetch the email body (bounded) so the
    model can read the total off the full receipt. Returns how many bodies were fetched."""
    fetched = 0
    for c in cands:
        if fetched >= MAX_BODY_FETCH:
            break
        if _has_amount(f"{c['subject']}\n{c['snippet']}"):
            continue                      # amount already visible — no need to open it
        try:
            m = await g.gmail_get(c["id"])
            body = str(getattr(m, "body", "") or getattr(m, "snippet", "") or "")
            if body:
                c["text"] = body[:1500]
                fetched += 1
        except Exception:  # noqa: BLE001 - one unreadable message never blocks the rest
            continue
    return fetched


def _parse_array(content: str) -> list[dict[str, Any]]:
    m = re.search(r"\[.*\]", content or "", re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


async def scan_email_for_spending(
    cfg: HimmyConfig | None = None, *, lookback: int = 40,
    min_confidence: float = 0.6, add: bool = True,
) -> dict[str, Any]:
    """Read recent Gmail, extract genuine spending, and (by default) file it to the ledger.

    Returns ``{ok, scanned, candidates, found, added, currency_total, expenses, message}``. Skips
    any message scanned on a previous run, so it is safe to call repeatedly. Best-effort throughout.
    """
    cfg = cfg or load_config()

    # 1) Gmail connected?
    try:
        from himmy.api import studio_google as g

        if not g.status().connected:
            return {"ok": False, "message": "No Google account is connected — connect it in the Mail tab, "
                    "then I can scan your inbox for spending."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Couldn't reach Gmail ({type(exc).__name__})."}

    # 2) pull recent inbox, drop ones already scanned, keep the likely-spend ones
    try:
        messages = await g.gmail_list(max(5, min(int(lookback), 60)))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Couldn't read your inbox: {exc}"}

    seen = _load_seen(cfg)
    cands = [c for c in _candidates(messages) if c["id"] not in seen]
    if not cands:
        return {"ok": True, "scanned": len(messages), "candidates": 0, "found": 0, "added": 0,
                "duplicates": 0, "expenses": [], "message": "No new spending emails in your recent inbox."}

    # 2b) SMART: for receipts whose amount isn't in the snippet, open the body to read the total
    await _enrich_with_bodies(cands, g)

    # 3) one cheap model pass turns the candidates into structured expenses
    listing = "\n".join(
        f"{i+1}. message_id={c['id']} | from {c['from']} | {c['date']} | "
        f"SUBJECT: {c['subject']} | {c['text']}"
        for i, c in enumerate(cands)
    )
    try:
        from himmy.cli.provider import build_inference_for
        from himmy.services.inference.models import InferenceMessage, InferenceRequest

        svc = build_inference_for(cfg.provider, cfg.model)
        resp = await svc.run(InferenceRequest(
            messages=[InferenceMessage(role="system", content=_system_prompt()),
                      InferenceMessage(role="user", content="Emails:\n" + listing)],
            generation_params={"temperature": 0}, timeout_seconds=60,
        ))
        rows = _parse_array(resp.output_text or "")
    except Exception as exc:  # noqa: BLE001
        # remember we looked at these so a retry doesn't loop on the same batch forever
        _save_seen(cfg, seen | {c["id"] for c in cands})
        return {"ok": False, "message": f"Couldn't read the emails ({type(exc).__name__}).",
                "scanned": len(messages), "candidates": len(cands)}

    by_id = {c["id"]: c for c in cands}
    store = ExpenseStore(cfg)
    existing = store.list(limit=500)                          # for SMART dedup vs the current ledger
    expenses: list[dict[str, Any]] = []
    duplicates = 0
    for r in rows:
        mid = str(r.get("message_id") or "").strip()
        if mid not in by_id:                                  # only the candidates we actually sent
            continue
        amount = _to_amount(r.get("amount"))
        conf = float(r.get("confidence") or 0)
        if amount <= 0 or conf < min_confidence:
            continue
        c = by_id[mid]
        draft = {
            "date": _clean_date(r.get("date") or c.get("date")),
            "merchant": str(r.get("merchant") or "").strip()[:120] or "Expense",
            "amount": amount,
            "currency": (str(r.get("currency") or _DEFAULT_CCY).strip()[:8] or _DEFAULT_CCY).upper(),
            "category": _clean_category(r.get("category")),
            "note": f"from email: {c.get('subject', '')}".strip()[:300],
        }
        if _is_duplicate(draft, existing):                    # already in the ledger (e.g. logged by hand)
            duplicates += 1
            continue
        if add:
            saved = store.add(draft, source="email")
            existing.append(saved)                            # so two identical receipts don't both file
            expenses.append(saved)
        else:
            expenses.append(draft)

    # 4) remember every candidate we looked at (filed or not) so we never re-file a receipt
    _save_seen(cfg, seen | {c["id"] for c in cands})

    total = round(sum(float(e["amount"]) for e in expenses), 2)
    ccy = expenses[0]["currency"] if expenses else _DEFAULT_CCY
    dup_note = f" ({duplicates} already in your ledger, skipped)" if duplicates else ""
    if not expenses:
        msg = (f"Scanned {len(cands)} likely receipts — nothing new to log{dup_note}."
               if duplicates else
               f"Scanned {len(cands)} likely receipts — nothing I was confident enough to log.")
    else:
        verb = "logged" if add else "found"
        msg = f"{verb.capitalize()} {len(expenses)} expense(s) from your email totalling {ccy} {total:,.0f}{dup_note}."
    return {"ok": True, "scanned": len(messages), "candidates": len(cands),
            "found": len(expenses), "added": len(expenses) if add else 0, "duplicates": duplicates,
            "currency_total": total, "expenses": expenses, "message": msg}


def _is_duplicate(draft: dict[str, Any], existing: list[dict[str, Any]], *, day_window: int = 2) -> bool:
    """True if the ledger already holds a matching expense — same merchant, same amount, within a
    couple of days — so an email receipt never double-counts something already recorded by hand."""
    import datetime as _dt

    merch = str(draft.get("merchant") or "").strip().lower()
    amt = round(float(draft.get("amount") or 0), 2)
    try:
        ddate = _dt.date.fromisoformat(str(draft.get("date"))[:10])
    except Exception:  # noqa: BLE001
        ddate = None
    for e in existing:
        if round(float(e.get("amount") or 0), 2) != amt:
            continue
        if str(e.get("merchant") or "").strip().lower() != merch:
            continue
        if ddate is not None:
            try:
                edate = _dt.date.fromisoformat(str(e.get("date"))[:10])
                if abs((edate - ddate).days) > day_window:
                    continue
            except Exception:  # noqa: BLE001
                pass
        return True
    return False


__all__ = ["scan_email_for_spending"]
