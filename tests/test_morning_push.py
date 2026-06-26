"""Morning Push — the rich daily brief, seeded enabled at 07:00 HIMMY_TZ, pushed to the bell.

All OFFLINE against a throwaway data dir: HIMMY_APP_DATA_DIR / HIMMY_TASKS_PATH /
HIMMY_ROUTINES_PATH under tmp_path, HIMMY_TZ=Asia/Kathmandu. The model is never called — the
brief's _generate is monkeypatched to a fixed multi-section string. Asserts: the seed binds the
RICH prompt, fires daily 07:00 enabled; the seed is idempotent + doesn't clobber user edits; a
run lands the rich brief in the inbox; and the 07:00 generation populates the Today cache so the
card serves it with no second generation (one brief per morning).
"""

from __future__ import annotations

import datetime
import importlib
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HIMMY_TASKS_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("HIMMY_ROUTINES_PATH", str(tmp_path / "routines.db"))
    monkeypatch.setenv("HIMMY_TZ", "Asia/Kathmandu")

    import himmy.api.routines as himmy_routines
    import himmy.api.studio_tasks as studio_tasks

    studio_tasks.reset_tasks_store()
    himmy_routines.reset_routines_store()

    import himmy_app.brief as brief
    import himmy_app.routines as routines

    importlib.reload(brief)
    importlib.reload(routines)

    yield routines, brief

    studio_tasks.reset_tasks_store()
    himmy_routines.reset_routines_store()


# --------------------------------------------------------------------------------------
# Seed shape: rich prompt, daily 07:00, enabled
# --------------------------------------------------------------------------------------
def test_seed_is_rich_daily_0700_enabled(env) -> None:
    routines, brief = env
    routines.seed_default_routines()

    briefs = [r for r in routines.list_routines() if r["name"] == routines._BRIEFING_NAME]
    assert len(briefs) == 1
    r = briefs[0]
    assert r["enabled"] is True
    assert r["schedule"]["kind"] == "cron"
    assert r["schedule"]["expr"] == "0 7 * * *"
    assert r["prompt"] == brief._BRIEF_PROMPT  # rich prompt shared with the Today card


def test_seed_is_idempotent_and_does_not_clobber(env) -> None:
    routines, _ = env
    routines.seed_default_routines()
    routines.seed_default_routines()  # second call is a no-op
    briefs = [r for r in routines.list_routines() if r["name"] == routines._BRIEFING_NAME]
    assert len(briefs) == 1

    # A user who disables it keeps that choice across re-seeds.
    rid = briefs[0]["id"]
    routines.update_routine(rid, {"enabled": False})
    routines.seed_default_routines()
    again = next(r for r in routines.list_routines() if r["name"] == routines._BRIEFING_NAME)
    assert again["enabled"] is False


def test_old_daily_briefing_is_migrated_away(env) -> None:
    routines, _ = env
    # Simulate an existing install carrying the OLD seed.
    routines.create_routine(
        routines._OLD_BRIEFING_NAME,
        "thin prompt",
        {"kind": "cron", "expr": "30 6 * * 1-5"},
        enabled=False,
    )
    routines.seed_default_routines()
    names = [r["name"] for r in routines.list_routines()]
    assert routines._OLD_BRIEFING_NAME not in names  # old row deleted
    assert names.count(routines._BRIEFING_NAME) == 1  # exactly one new push


# --------------------------------------------------------------------------------------
# Run-now → the rich brief lands in the inbox (via the DailyBrief cache-sharing branch)
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_now_lands_rich_brief_and_shares_cache(env, monkeypatch: pytest.MonkeyPatch) -> None:
    routines, brief = env
    routines.seed_default_routines()
    rid = next(r["id"] for r in routines.list_routines() if r["name"] == routines._BRIEFING_NAME)

    rich = "Looks like a full day.\n\n**Today**\n- 2pm Dentist\n\n**Needs you**\n- Reply to Jane"

    calls = {"n": 0}

    async def _fake_generate(self):  # noqa: ANN001
        calls["n"] += 1
        return rich

    monkeypatch.setattr(brief.DailyBrief, "_generate", _fake_generate)

    out = await routines.get_scheduler().run_now(rid)
    assert out["ok"] is True
    assert calls["n"] == 1  # generated exactly once

    # The full rich brief lands in the bell as a kind=='result' notification.
    items = routines.get_inbox().list()
    hit = next((i for i in items if i["routine_name"] == routines._BRIEFING_NAME), None)
    assert hit is not None
    assert hit["kind"] == "result"
    assert hit["status"] == "ok"
    assert "**Today**" in hit["body"] and "**Needs you**" in hit["body"]

    # Cache-sharing: the Today card now serves the SAME text with NO second generation.
    res = await brief.DailyBrief().get()
    assert res["stale"] is False
    assert res["text"] == rich
    assert calls["n"] == 1  # still one — the brief was served from cache


# --------------------------------------------------------------------------------------
# Scheduling math: ~07:00 HIMMY_TZ + coalesce catch-up
# --------------------------------------------------------------------------------------
def test_scheduling_fires_at_0700_local_with_catchup(env) -> None:
    routines, _ = env
    from himmy.api.routines import Routine, Schedule, is_due, next_fire

    tz = ZoneInfo("Asia/Kathmandu")
    r = Routine(
        name=routines._BRIEFING_NAME,
        agent_path="/x",
        prompt="p",
        schedule=Schedule(kind="cron", expr="0 7 * * *", missed="coalesce"),
        enabled=True,
        created_at=datetime.datetime(2026, 6, 25, 12, 0, tzinfo=tz).isoformat(),
    )
    # Just before 07:00 local: next fire is today's 07:00 Kathmandu, not yet due.
    pre = datetime.datetime(2026, 6, 26, 6, 30, tzinfo=tz)
    nf = next_fire(r, pre)
    assert nf is not None
    assert nf.astimezone(tz).hour == 7 and nf.astimezone(tz).minute == 0
    assert is_due(r, pre) is False
    # At 07:05 with no prior run, coalesce still treats the 07:00 slot as due (catch-up).
    assert is_due(r, datetime.datetime(2026, 6, 26, 7, 5, tzinfo=tz)) is True
