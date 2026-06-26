"""Deeper Memory of You — the personalization layer (user_profile).

These tests keep the suite OFFLINE + deterministic by monkeypatching the OpenRouter HTTP call
(the same pattern as test_planner). They prove the NEW "voice" + richer "people" memory:
  * ``_clean_layer`` keeps + caps the voice scalar;
  * ``_merge_layers`` lets the USER voice win over the learned one;
  * ``render_for_prompt`` injects a "How they write" line;
  * a learn() run distills people ("Name — relationship") / projects / voice, and NEVER
    overwrites a pre-set user layer (user facts always win in the rendered block);
  * the email-correspondent signal respects Settings → Permissions and fails open;
  * ``maybe_auto_learn`` skips when fresh and runs when stale.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from himmy_app import user_profile
from himmy_app.config import load_config


@pytest.fixture()
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Pin every durable path into a throwaway dir so we never touch the real account.
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HIMMY_TASKS_PATH", str(tmp_path / "tasks.db"))
    return load_config()


def _patch_llm(monkeypatch: pytest.MonkeyPatch, content: str) -> dict[str, int]:
    """Replace the OpenRouter POST so learn() parses a canned reply, fully offline.

    Returns a small dict whose ``calls`` counter lets a test assert the model was (not) hit.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    counter = {"calls": 0}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": content}}]}

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def post(self, *a, **k):
            counter["calls"] += 1
            return _Resp()

    monkeypatch.setattr(user_profile.httpx, "AsyncClient", _Client)
    return counter


_LEARNED_JSON = json.dumps({
    "about": "An economist studying conflict and agriculture.",
    "voice": "Writes concise, direct, lowercase, signs off \"thanks\".",
    "projects": ["Conflict-agriculture panel"],
    "people": ["Asha Rai — co-author"],
    "topics": ["agricultural economics"],
    "preferences": ["concise answers"],
})


# ---- the scalar voice field -----------------------------------------------------------------
def test_clean_layer_keeps_and_caps_voice() -> None:
    layer = user_profile._clean_layer({"voice": "  hi there  "})
    assert layer["voice"] == "hi there"
    long = user_profile._clean_layer({"voice": "x" * 9000})
    assert len(long["voice"]) == user_profile._MAX_VOICE_CHARS


def test_merge_layers_user_voice_wins() -> None:
    prof = {
        "user": user_profile._clean_layer({"voice": "Formal, long emails."}),
        "learned": user_profile._clean_layer({"voice": "Casual one-liners."}),
    }
    assert user_profile._merge_layers(prof)["voice"] == "Formal, long emails."

    # with no user voice, the learned one fills in
    prof["user"] = user_profile._clean_layer({})
    assert user_profile._merge_layers(prof)["voice"] == "Casual one-liners."


def test_render_includes_voice_line() -> None:
    prof = {
        "user": user_profile._clean_layer({"voice": "Concise and lowercase."}),
        "learned": user_profile._empty_layer(),
        "learned_at": 0.0,
    }
    block = user_profile.render_for_prompt(prof)
    assert "How they write" in block
    assert "Concise and lowercase." in block


# ---- learning distills people / projects / voice -------------------------------------------
@pytest.mark.asyncio
async def test_learn_populates_people_projects_voice(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(monkeypatch, _LEARNED_JSON)
    # give it at least one signal so learn() doesn't fail open before the model call
    user_profile.save(
        {"user": user_profile._empty_layer(),
         "learned": user_profile._empty_layer(), "learned_at": 0.0}, cfg)
    monkeypatch.setattr(user_profile, "gather_signals_async",
                        _stub_signals({"chat_topics": ["how do i model conflict shocks"]}))

    res = await user_profile.learn(cfg)
    assert res["ok"] is True
    prof = user_profile.load(cfg)
    learned = prof["learned"]
    assert learned["people"] == ["Asha Rai — co-author"]
    assert learned["projects"] == ["Conflict-agriculture panel"]
    assert learned["voice"].startswith("Writes concise")


@pytest.mark.asyncio
async def test_learn_does_not_overwrite_user_layer_and_user_wins(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm(monkeypatch, _LEARNED_JSON)
    monkeypatch.setattr(user_profile, "gather_signals_async",
                        _stub_signals({"chat_topics": ["modelling shocks"]}))
    # pre-set the USER layer: it must survive the learn() run and win in the render
    user_profile.save_user_layer(
        {"about": "I am a founder.", "people": ["My boss — manager"],
         "voice": "Formal, long emails."}, cfg)

    await user_profile.learn(cfg)
    prof = user_profile.load(cfg)
    # user layer untouched
    assert prof["user"]["voice"] == "Formal, long emails."
    assert prof["user"]["people"] == ["My boss — manager"]
    # and it wins in the rendered block
    block = user_profile.render_for_prompt(prof)
    assert "Formal, long emails." in block
    assert "Writes concise" not in block  # learned voice loses to the user's
    # the user's person sorts first; the learned one is appended after
    people_line = next(li for li in block.splitlines() if li.startswith("- Key people:"))
    assert people_line.index("My boss — manager") < people_line.index("Asha Rai — co-author")


# ---- permissions gate + fail-open on the email signal --------------------------------------
@pytest.mark.asyncio
async def test_email_signal_gated_off_by_permissions(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    from himmy_app import permissions

    permissions.save({"mail": "off"}, cfg)

    # If gating worked, gmail_list is never imported/called; trip a flag if it is.
    called = {"hit": False}

    async def _boom(*a, **k):  # pragma: no cover - must NOT run
        called["hit"] = True
        return []

    import himmy.api.studio_google as g
    monkeypatch.setattr(g, "gmail_list", _boom)

    sig = await user_profile.gather_signals_async(cfg)
    assert sig["correspondents"] == []
    assert called["hit"] is False


@pytest.mark.asyncio
async def test_email_signal_fail_open_when_google_not_connected(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    from himmy_app import permissions

    permissions.save({"mail": "read"}, cfg)
    # force "no account connected" (the test box may actually have one) → empty, no crash
    import himmy.api.studio_google as g

    monkeypatch.setattr(
        g, "status",
        lambda: g.GoogleStatus(configured=True, connected=False, email=None, writable=False))
    sig = await user_profile.gather_signals_async(cfg)
    assert sig["correspondents"] == []


# ---- auto-learn gating ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_maybe_auto_learn_skips_when_fresh(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    counter = _patch_llm(monkeypatch, _LEARNED_JSON)
    user_profile.save(
        {"user": user_profile._empty_layer(),
         "learned": user_profile._empty_layer(), "learned_at": time.time()}, cfg)

    res = await user_profile.maybe_auto_learn(cfg)
    assert res["ok"] is False
    assert res.get("skipped") == "fresh"
    assert counter["calls"] == 0  # the model was NOT called


@pytest.mark.asyncio
async def test_maybe_auto_learn_runs_when_stale(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    counter = _patch_llm(monkeypatch, _LEARNED_JSON)
    user_profile.save(
        {"user": user_profile._empty_layer(),
         "learned": user_profile._empty_layer(),
         "learned_at": time.time() - 2 * 24 * 3600}, cfg)
    monkeypatch.setattr(user_profile, "gather_signals_async",
                        _stub_signals({"chat_topics": ["conflict shocks"]}))

    res = await user_profile.maybe_auto_learn(cfg)
    assert res["ok"] is True
    assert counter["calls"] == 1


# ---- helpers --------------------------------------------------------------------------------
def _stub_signals(overrides: dict[str, list[str]]):
    """An async stand-in for gather_signals_async that returns a fixed (non-empty) signal set."""
    async def _gather(_cfg=None):
        sig = user_profile._empty_signals()
        sig.update(overrides)
        return sig

    return _gather
