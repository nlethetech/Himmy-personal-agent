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


def test_skips_expense_already_in_ledger(mocked):
    from himmy_app.finance import ExpenseStore
    # log Uber $18.40 by hand first — the email scan must NOT double-count it
    ExpenseStore(mocked).add({"merchant": "Uber", "amount": 18.40, "currency": "USD",
                              "date": "2026-06-28", "category": "Transport"}, source="manual")
    r = asyncio.run(em.scan_email_for_spending(mocked, lookback=20))
    assert r["duplicates"] >= 1
    merchants = [e["merchant"] for e in r["expenses"]]
    assert "Uber" not in merchants and "Amazon" in merchants   # dup skipped, new one filed


def test_reads_body_when_amount_missing_from_snippet(monkeypatch):
    from himmy.api import studio_google as g

    monkeypatch.setattr(g, "status", lambda: types.SimpleNamespace(connected=True))

    async def fake_list(_n):
        # snippet has NO amount → the scan must open the body to find the total
        return [_msg(1, "orders@shop.com", "Your order confirmation", "Thanks for your order!", "2026-06-28")]
    monkeypatch.setattr(g, "gmail_list", fake_list)

    async def fake_get(mid):
        return types.SimpleNamespace(id=mid, body="Order total: $25.00 — paid to Shop", snippet="")
    monkeypatch.setattr(g, "gmail_get", fake_get)

    import himmy.cli.provider as prov
    captured = {}

    class _Resp:
        def __init__(self, t): self.output_text = t

    class _Svc:
        async def run(self, req):
            captured["user"] = req.messages[-1].content
            return _Resp('[{"message_id":"m1","merchant":"Shop","amount":25.0,"currency":"USD",'
                         '"date":"2026-06-28","category":"Shopping","confidence":0.9}]')
    monkeypatch.setattr(prov, "build_inference_for", lambda _p, _m: _Svc())

    r = asyncio.run(em.scan_email_for_spending(load_config(), lookback=10))
    assert r["found"] == 1 and r["expenses"][0]["amount"] == 25.0
    assert "Order total: $25.00" in captured["user"]      # the body was fed to the model


def test_proactive_scan_throttles_and_makes_one_observation(monkeypatch):
    import himmy_app.email_money as em2
    import himmy_app.proactive as p

    monkeypatch.setattr(p.perms, "level_of", lambda _surface, _c=None: "always")
    calls = {"n": 0}

    async def fake_scan(_cfg, add=True):
        calls["n"] += 1
        return {"ok": True, "found": 2, "currency_total": 81.6,
                "expenses": [{"currency": "USD", "amount": 18.4}, {"currency": "USD", "amount": 63.2}]}
    monkeypatch.setattr(em2, "scan_email_for_spending", fake_scan)

    cfg = load_config()
    obs1 = asyncio.run(p._email_spend_observations(cfg, "always"))
    assert len(obs1) == 1 and "2 purchases" in obs1[0]["title"] and obs1[0]["surface"] == "finance"
    assert calls["n"] == 1
    obs2 = asyncio.run(p._email_spend_observations(cfg, "always"))   # within throttle window
    assert obs2 == [] and calls["n"] == 1                            # did NOT scan again
