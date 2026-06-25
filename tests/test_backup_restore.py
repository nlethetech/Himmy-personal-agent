"""Whole-workspace backup/restore is a DATA-LOSS-CRITICAL path — these tests guard it.

They prove: (1) a backup captures every real store (not just papers), incl. data sitting in
a WAL sidecar; (2) regenerable caches are excluded; (3) restore brings every store back and
replaces live state; (4) restore snapshots the current workspace BEFORE overwriting; and
(5) a corrupt / non-Himmy zip changes nothing.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from himmy_app.config import load_config
from himmy_app.library import Library


def _write_marker(path: Path, marker: str, *, wal: bool = False) -> None:
    conn = sqlite3.connect(str(path))
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS marker(v TEXT)")
        conn.execute("INSERT INTO marker(v) VALUES (?)", (marker,))
        conn.commit()
    finally:
        conn.close()


def _markers(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT v FROM marker ORDER BY v")]
    finally:
        conn.close()


@pytest.fixture()
def lib(tmp_path, monkeypatch) -> Library:
    data = tmp_path / "data"
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(data))
    cfg = load_config()
    library = Library(cfg)  # creates library.db + library_files/
    # Seed the real per-store databases the way the running app produces them.
    _write_marker(data / "library.db", "lib")              # extra table alongside the real schema
    _write_marker(data / "conversations.db", "conv")
    _write_marker(data / "tasks.db", "tasks")
    _write_marker(data / "routines.db", "routine", wal=True)  # WAL: row lives in -wal, not the .db
    _write_marker(data / "inbox.db", "inbox")
    (data / "usage.json").write_text(json.dumps({"life_cost": 1.23}))
    # Regenerable caches that backup MUST skip.
    _write_marker(data / "papers_cache.db", "cache")
    (data / "news_cache.json").write_text("{}")
    # A PDF in the file store.
    (library._files / "paper1.pdf").write_bytes(b"%PDF-1.4 fake")
    return library


def test_backup_captures_whole_workspace_and_skips_caches(lib, tmp_path):
    out = lib.backup(str(tmp_path / "out"))
    names = set(zipfile.ZipFile(out).namelist())

    # Every real store is present…
    for f in ("library.db", "conversations.db", "tasks.db", "routines.db", "inbox.db",
              "usage.json", "manifest.json", "library_files/paper1.pdf"):
        assert f in names, f"{f} missing from backup ({names})"
    # …and regenerable caches are excluded.
    assert "papers_cache.db" not in names
    assert "news_cache.json" not in names
    # No raw WAL/SHM sidecars leak in.
    assert not any(n.endswith(("-wal", "-shm")) for n in names)

    manifest = json.loads(zipfile.ZipFile(out).read("manifest.json"))
    assert manifest["version"] == 2 and manifest["app"] == "himmy"
    assert "papers" in manifest["counts"]


def test_backup_captures_wal_resident_rows(lib, tmp_path):
    # routines.db's row was committed in WAL mode → a raw file copy would miss it; the
    # snapshot must include it.
    out = lib.backup(str(tmp_path / "out"))
    snap = tmp_path / "extract" / "routines.db"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(zipfile.ZipFile(out).read("routines.db"))
    assert _markers(snap) == ["routine"]


def test_restore_brings_everything_back_and_replaces_live_state(lib, tmp_path):
    data = lib._db.parent
    out = lib.backup(str(tmp_path / "out"))

    # Simulate damage/loss AFTER the backup:
    _write_marker(data / "library.db", "POST")        # extra row that must be gone after restore
    (data / "conversations.db").unlink()              # whole store deleted
    (data / "usage.json").write_text("{}")            # settings clobbered
    (lib._files / "paper1.pdf").unlink()              # PDF deleted

    res = lib.restore(out)
    assert res["ok"] is True
    assert "snapshot" in res

    # library.db replaced (POST gone, original back); deleted store restored; settings + PDF back.
    assert _markers(data / "library.db") == ["lib"]
    assert (data / "conversations.db").exists() and _markers(data / "conversations.db") == ["conv"]
    assert _markers(data / "routines.db") == ["routine"]
    assert json.loads((data / "usage.json").read_text())["life_cost"] == 1.23
    assert (lib._files / "paper1.pdf").read_bytes() == b"%PDF-1.4 fake"

    # A safety snapshot of the pre-restore state exists and holds the damaged 'POST' row.
    snap_dir = data.parent / res["snapshot"]
    assert snap_dir.is_dir()
    assert "POST" in _markers(snap_dir / "library.db")


def test_restore_rejects_a_bad_zip_without_changing_anything(lib, tmp_path):
    data = lib._db.parent
    before = _markers(data / "library.db")

    not_a_zip = tmp_path / "junk.zip"
    not_a_zip.write_text("this is not a zip")
    res = lib.restore(str(not_a_zip))
    assert res["ok"] is False
    assert _markers(data / "library.db") == before  # untouched

    # A valid zip that isn't a Himmy backup is also refused.
    stranger = tmp_path / "stranger.zip"
    with zipfile.ZipFile(stranger, "w") as z:
        z.writestr("hello.txt", "hi")
    res2 = lib.restore(str(stranger))
    assert res2["ok"] is False
    assert _markers(data / "library.db") == before


def test_restore_missing_file(lib, tmp_path):
    res = lib.restore(str(tmp_path / "nope.zip"))
    assert res["ok"] is False and "not found" in res["message"].lower()
