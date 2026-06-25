"""Backend tests for the Mail tab logic in himmy_app.server.

Everything here runs OFFLINE and against a THROWAWAY data dir:
- the sender-rule store is pinned under tmp_path via HIMMY_APP_DATA_DIR so the real
  ``.scholar-desk`` is never read or written;
- Google is fully mocked (studio_google.status / gmail_list monkeypatched) so no OAuth,
  no network, and no real Gmail account is ever touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from himmy_app import server
from himmy_app.config import load_config
from himmy_app.server import (
    _gmail_category,
    _normalize_sender,
    is_automated,
    load_mail_rules,
    save_mail_rules,
)


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------
@pytest.fixture()
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A config whose data_dir is a throwaway tmp dir — the real .scholar-desk is untouched."""
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path))
    return load_config()


class _FakeMsg:
    """A stand-in for studio_google.GmailMessage carrying just what the inbox shaper reads."""

    def __init__(
        self,
        *,
        mid: str,
        sender: str,
        subject: str = "subj",
        snippet: str = "snip",
        date: str = "2026-06-25",
        label_ids: list[str] | None = None,
        unread: bool = False,
    ) -> None:
        self.id = mid
        self.sender = sender
        self.subject = subject
        self.snippet = snippet
        self.date = date
        self.label_ids = label_ids or []
        self.unread = unread


class _FakeStatus:
    def __init__(self, *, configured: bool = True, connected: bool = True) -> None:
        self.configured = configured
        self.connected = connected
        self.email = "me@gmail.com"
        self.writable = False


def _mock_google(monkeypatch: pytest.MonkeyPatch, messages: list[_FakeMsg], *, connected: bool = True) -> None:
    """Make the server's lazily-imported studio_google module connected + return ``messages``."""
    from himmy.api import studio_google as g

    monkeypatch.setattr(g, "status", lambda: _FakeStatus(configured=True, connected=connected))

    async def _fake_list(max_results: int = 20) -> list[_FakeMsg]:
        return list(messages)[:max_results]

    monkeypatch.setattr(g, "gmail_list", _fake_list)


# --------------------------------------------------------------------------------------
# category derivation
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["CATEGORY_PROMOTIONS"], "promotions"),
        (["CATEGORY_SOCIAL"], "social"),
        (["CATEGORY_UPDATES"], "updates"),
        (["CATEGORY_FORUMS"], "forums"),
        (["INBOX"], "focused"),          # Primary tab -> focused
        ([], "focused"),                  # no category labels -> focused default
    ],
)
def test_gmail_category(labels: list[str], expected: str) -> None:
    assert _gmail_category(labels) == expected


def test_gmail_category_promotions_wins_over_other_labels() -> None:
    # Promotions is checked first; mixed labels still resolve deterministically.
    assert _gmail_category(["INBOX", "CATEGORY_PROMOTIONS", "UNREAD"]) == "promotions"


def test_inbox_flags_unread_important_starred(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    msgs = [
        _FakeMsg(mid="1", sender="a@x.com", label_ids=["IMPORTANT", "STARRED"], unread=True),
        _FakeMsg(mid="2", sender="b@x.com", label_ids=[], unread=False),
    ]
    _mock_google(monkeypatch, msgs)
    with TestClient(server.create_app()) as client:
        rows = client.get("/mail/inbox", params={"force": True}).json()["messages"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["1"]["unread"] is True
    assert by_id["1"]["important"] is True
    assert by_id["1"]["starred"] is True
    assert by_id["2"]["unread"] is False
    assert by_id["2"]["important"] is False
    assert by_id["2"]["starred"] is False


# --------------------------------------------------------------------------------------
# is_automated
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sender",
    [
        "noreply@github.com",
        "notifications@slack.com",
        "MAILER-DAEMON@google.com",
        "no-reply@stripe.com",
        "No-Reply <No-Reply@Example.COM>",  # display-name form, mixed case
    ],
)
def test_is_automated_true(sender: str) -> None:
    assert is_automated(sender) is True


@pytest.mark.parametrize(
    "sender",
    [
        "person@gmail.com",
        "Jane Doe <jane.doe@company.com>",
        "",
        "not an address",
    ],
)
def test_is_automated_false(sender: str) -> None:
    assert is_automated(sender) is False


# --------------------------------------------------------------------------------------
# rules store (round-trips + sender normalization)
# --------------------------------------------------------------------------------------
def test_rules_store_starts_empty(cfg) -> None:
    assert load_mail_rules(cfg) == {"muted": [], "vip": []}


def test_rules_store_roundtrip_persists(cfg) -> None:
    save_mail_rules(cfg, {"muted": ["spam@x.com"], "vip": ["boss@y.com"]})
    again = load_mail_rules(cfg)
    assert again["muted"] == ["spam@x.com"]
    assert again["vip"] == ["boss@y.com"]
    # And it really hit disk under the throwaway data dir, not the real .scholar-desk.
    assert (cfg.data_dir / "mail_rules.json").exists()


def test_rules_store_dedups_and_lowercases(cfg) -> None:
    save_mail_rules(cfg, {"muted": ["A@X.com", "a@x.com", "  "], "vip": []})
    assert load_mail_rules(cfg)["muted"] == ["a@x.com"]


def test_normalize_sender_strips_display_name() -> None:
    assert _normalize_sender("Jane Smith <Jane@X.com>") == "jane@x.com"
    assert _normalize_sender("bare@x.com") == "bare@x.com"


def test_corrupt_rules_file_yields_empty(cfg) -> None:
    (cfg.data_dir / "mail_rules.json").write_text("{not json", encoding="utf-8")
    assert load_mail_rules(cfg) == {"muted": [], "vip": []}


# --------------------------------------------------------------------------------------
# /mail/rules endpoint round-trips ("Name <addr>" normalizes to bare addr)
# --------------------------------------------------------------------------------------
def test_mail_rules_endpoint_roundtrips(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_google(monkeypatch, [])
    with TestClient(server.create_app()) as client:
        # Empty to start.
        assert client.get("/mail/rules").json() == {"ok": True, "muted": [], "vip": []}

        # Mute via a display-name sender -> stored as the bare, lower-cased address.
        r = client.post("/mail/rules", json={"action": "mute", "sender": "Spammy Co <Loud@Promo.COM>"}).json()
        assert r["ok"] is True
        assert r["muted"] == ["loud@promo.com"]

        # VIP a sender; muting is independent.
        r = client.post("/mail/rules", json={"action": "vip", "sender": "boss@work.com"}).json()
        assert r["vip"] == ["boss@work.com"]
        assert r["muted"] == ["loud@promo.com"]

        # Persisted across a fresh GET.
        got = client.get("/mail/rules").json()
        assert got["muted"] == ["loud@promo.com"]
        assert got["vip"] == ["boss@work.com"]

        # unmute / unvip round-trip back to empty.
        client.post("/mail/rules", json={"action": "unmute", "sender": "loud@promo.com"})
        r = client.post("/mail/rules", json={"action": "unvip", "sender": "BOSS@work.com"}).json()
        assert r == {"ok": True, "muted": [], "vip": []}


def test_mail_rules_mute_clears_vip_and_vice_versa(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_google(monkeypatch, [])
    with TestClient(server.create_app()) as client:
        client.post("/mail/rules", json={"action": "vip", "sender": "x@x.com"})
        r = client.post("/mail/rules", json={"action": "mute", "sender": "x@x.com"}).json()
        assert r["muted"] == ["x@x.com"]
        assert r["vip"] == []  # muting cleared the VIP flag


def test_mail_rules_rejects_unknown_action(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_google(monkeypatch, [])
    with TestClient(server.create_app()) as client:
        r = client.post("/mail/rules", json={"action": "bogus", "sender": "x@x.com"}).json()
        assert r["ok"] is False


# --------------------------------------------------------------------------------------
# /mail/inbox excludes muted + marks vip/automated
# --------------------------------------------------------------------------------------
def test_inbox_excludes_muted_and_marks_vip_automated(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    msgs = [
        _FakeMsg(mid="vip", sender="Boss <boss@work.com>", label_ids=["INBOX"]),
        _FakeMsg(mid="muted", sender="Loud <loud@promo.com>", label_ids=["CATEGORY_PROMOTIONS"]),
        _FakeMsg(mid="auto", sender="noreply@github.com", label_ids=["CATEGORY_UPDATES"]),
        _FakeMsg(mid="human", sender="friend@gmail.com", label_ids=["INBOX"]),
    ]
    _mock_google(monkeypatch, msgs)
    with TestClient(server.create_app()) as client:
        # Set rules: mute loud@promo.com, VIP boss@work.com.
        client.post("/mail/rules", json={"action": "mute", "sender": "loud@promo.com"})
        client.post("/mail/rules", json={"action": "vip", "sender": "boss@work.com"})

        rows = client.get("/mail/inbox", params={"force": True}).json()["messages"]

    by_id = {r["id"]: r for r in rows}
    # Muted sender is dropped entirely.
    assert "muted" not in by_id
    assert set(by_id) == {"vip", "auto", "human"}
    # VIP flagged; not automated.
    assert by_id["vip"]["vip"] is True
    assert by_id["vip"]["automated"] is False
    # noreply@ marked automated; not VIP.
    assert by_id["auto"]["automated"] is True
    assert by_id["auto"]["vip"] is False
    assert by_id["auto"]["category"] == "updates"
    # Plain human: neither.
    assert by_id["human"]["vip"] is False
    assert by_id["human"]["automated"] is False


def test_inbox_not_connected_returns_empty(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_google(monkeypatch, [], connected=False)
    with TestClient(server.create_app()) as client:
        body = client.get("/mail/inbox").json()
    assert body == {"ok": True, "connected": False, "messages": []}


# --------------------------------------------------------------------------------------
# /mail/digest is resilient when the model fails (never hits the network)
# --------------------------------------------------------------------------------------
def test_digest_friendly_when_model_fails(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    # One focused message so the digest reaches the (mocked-to-fail) model call.
    msgs = [_FakeMsg(mid="f", sender="boss@work.com", label_ids=["INBOX"], unread=True)]
    _mock_google(monkeypatch, msgs)

    async def _boom(cfg: Any, system: str, user: str) -> str:
        raise RuntimeError("model offline")

    monkeypatch.setattr(server, "_summarize_mail", _boom)

    with TestClient(server.create_app()) as client:
        body = client.get("/mail/digest", params={"force": True}).json()

    # No cached summary -> a friendly ok:false, and crucially no exception / no network.
    assert body["ok"] is False
    assert "model offline" in body["message"]


def test_digest_stubbed_summary(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    msgs = [_FakeMsg(mid="f", sender="boss@work.com", label_ids=["INBOX"], unread=True)]
    _mock_google(monkeypatch, msgs)

    async def _stub(cfg: Any, system: str, user: str) -> str:
        return "- Boss is waiting on a reply."

    monkeypatch.setattr(server, "_summarize_mail", _stub)

    with TestClient(server.create_app()) as client:
        body = client.get("/mail/digest", params={"force": True}).json()

    assert body["ok"] is True
    assert "Boss is waiting" in body["summary"]


def test_digest_no_focused_mail_says_so(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only promotional mail -> nothing focused -> a friendly canned line, model never called.
    msgs = [_FakeMsg(mid="p", sender="ads@promo.com", label_ids=["CATEGORY_PROMOTIONS"])]
    _mock_google(monkeypatch, msgs)

    def _should_not_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("model must not be called when there is no focused mail")

    monkeypatch.setattr(server, "_summarize_mail", _should_not_run)

    with TestClient(server.create_app()) as client:
        body = client.get("/mail/digest", params={"force": True}).json()

    assert body["ok"] is True
    assert "attention" in body["summary"].lower()
