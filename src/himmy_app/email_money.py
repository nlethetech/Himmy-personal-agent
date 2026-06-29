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
                    "snippet": snippet[:400], "date": date})
    return out


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
                "expenses": [], "message": "No new spending emails in your recent inbox."}

    # 3) one cheap model pass turns the candidates into structured expenses
    listing = "\n".join(
        f"{i+1}. message_id={c['id']} | from {c['from']} | {c['date']} | "
        f"SUBJECT: {c['subject']} | {c['snippet']}"
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
    expenses: list[dict[str, Any]] = []
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
        if add:
            saved = store.add(draft, source="email")
            expenses.append(saved)
        else:
            expenses.append(draft)

    # 4) remember every candidate we looked at (filed or not) so we never re-file a receipt
    _save_seen(cfg, seen | {c["id"] for c in cands})

    total = round(sum(float(e["amount"]) for e in expenses), 2)
    ccy = expenses[0]["currency"] if expenses else _DEFAULT_CCY
    if not expenses:
        msg = f"Scanned {len(cands)} likely receipts — nothing I was confident enough to log."
    else:
        msg = (f"Found and logged {len(expenses)} expense(s) from your email "
               f"totalling {ccy} {total:,.0f}." if add
               else f"Found {len(expenses)} expense(s) in your email totalling {ccy} {total:,.0f}.")
    return {"ok": True, "scanned": len(messages), "candidates": len(cands),
            "found": len(expenses), "added": len(expenses) if add else 0,
            "currency_total": total, "expenses": expenses, "message": msg}


__all__ = ["scan_email_for_spending"]
