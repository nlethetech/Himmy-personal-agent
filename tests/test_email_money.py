"""Email spending discovery — the keyword pre-filter, the extract→file pipeline, and the
no-double-file guarantee on re-scan. Gmail and the model are mocked, and conftest isolates the data
dir, so this never touches a real inbox or the real ledger.
"""

from __future__ import annotations

import asyncio
import types

import pytest

import himmy_app.email_money as em
from himmy_app.config import load_config


def test_spend_filter_keeps_receipts_drops_noise():
    assert em._looks_like_spend("Your order receipt — total $42") is True
    assert em._looks_like_spend("Payment confirmation for your subscription") is True
    assert em._looks_like_spend("FLASH SALE — 50% off everything") is False
    assert em._looks_like_spend("Your one-time password is 123456") is False
    assert em._looks_like_spend("Your package has shipped") is False


def _msg(i, sender, subject, snippet, date):
    return types.SimpleNamespace(id=f"m{i}", sender=sender, subject=subject, snippet=snippet, date=date)


@pytest.fixture()
def mocked(monkeypatch):
    from himmy.api import studio_google as g

    monkeypatch.setattr(g, "status", lambda: types.SimpleNamespace(connected=True))

    async def fake_list(_n):
        return [
            _msg(1, "receipts@uber.com", "Your trip receipt", "Total $18.40 — thanks for riding", "2026-06-28"),
            _msg(2, "no-reply@amazon.com", "Your order confirmation", "Order total: $63.20", "2026-06-27"),
            _msg(3, "deals@store.com", "FLASH SALE 50% off", "Biggest sale of the year", "2026-06-27"),
            _msg(4, "security@bank.com", "Your one-time password", "OTP 884211", "2026-06-28"),
        ]
    monkeypatch.setattr(g, "gmail_list", fake_list)

    import himmy.cli.provider as prov

    class _Resp:
        def __init__(self, t): self.output_text = t

    class _Svc:
        async def run(self, _req):
            return _Resp(
                '[{"message_id":"m1","merchant":"Uber","amount":18.40,"currency":"USD",'
                '"date":"2026-06-28","category":"Transport","confidence":0.95},'
                '{"message_id":"m2","merchant":"Amazon","amount":63.20,"currency":"USD",'
                '"date":"2026-06-27","category":"Shopping","confidence":0.9},'
                '{"message_id":"m3","merchant":"Store","amount":0,"currency":"USD",'
                '"date":"2026-06-27","category":"Shopping","confidence":0.1}]'
            )
    monkeypatch.setattr(prov, "build_inference_for", lambda _p, _m: _Svc())
    return load_config()


def test_scan_extracts_and_files_real_spend(mocked):
    r = asyncio.run(em.scan_email_for_spending(mocked, lookback=20))
    assert r["ok"] and r["candidates"] == 2 and r["found"] == 2 and r["added"] == 2
    got = {e["merchant"]: e for e in r["expenses"]}
    assert got["Uber"]["amount"] == 18.40 and got["Uber"]["currency"] == "USD"
    assert got["Amazon"]["category"] == "Shopping"
    assert all(e["source"] == "email" for e in r["expenses"])


def test_rescan_does_not_double_file(mocked):
    asyncio.run(em.scan_email_for_spending(mocked, lookback=20))
    r2 = asyncio.run(em.scan_email_for_spending(mocked, lookback=20))
    assert r2["added"] == 0 and r2["candidates"] == 0   # every message already seen


def test_preview_mode_does_not_save(mocked):
    r = asyncio.run(em.scan_email_for_spending(mocked, lookback=20, add=False))
    assert r["found"] == 2 and r["added"] == 0
    from himmy_app.finance import ExpenseStore
    assert ExpenseStore(mocked).list(limit=10) == []     # nothing written in preview mode
