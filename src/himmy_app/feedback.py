"""Recommendation feedback — the "not interested" signal that makes recs learn.

When the reader dismisses a recommended paper we record two things:
  * the paper's identity (DOI / title), so it is NEVER recommended again; and
  * its research concepts, so the recommender can DOWN-WEIGHT that direction — dismiss a few
    "Poverty" papers and poverty candidates quietly sink, without you ever touching a setting.

Stored in its own SQLite file alongside the other ``.scholar-desk`` stores so it rides along with
backups and survives restarts.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from typing import Any

from himmy_app.config import HimmyConfig, load_config


def _title_key(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").lower()))


def _norm_doi(doi: str | None) -> str:
    if not doi:
        return ""
    return doi.strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "").replace("doi:", "").strip()


class DismissalStore:
    """Papers the reader marked 'not interested', plus the concepts to down-weight."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        cfg = config or load_config()
        self._db = cfg.feedback_db_path
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._db), timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS dismissals (
                    key TEXT PRIMARY KEY,   -- doi if present, else title fingerprint
                    doi TEXT, title TEXT, concepts TEXT, at REAL
                )"""
            )

    def dismiss(self, doi: str, title: str, concepts: list[str] | None) -> dict[str, Any]:
        doi = _norm_doi(doi)
        key = doi or _title_key(title)
        if not key:
            return {"ok": False, "error": "nothing to dismiss"}
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO dismissals (key, doi, title, concepts, at) VALUES (?,?,?,?,?)",
                (key, doi, title or "", json.dumps(concepts or []), time.time()),
            )
        return {"ok": True, "key": key}

    def dismissed_dois(self) -> set[str]:
        with self._conn() as c:
            return {r["doi"] for r in c.execute("SELECT doi FROM dismissals WHERE doi != ''")}

    def dismissed_title_keys(self) -> set[str]:
        with self._conn() as c:
            return {_title_key(r["title"]) for r in c.execute("SELECT title FROM dismissals") if r["title"]}

    def concept_counts(self) -> Counter:
        """How many dismissed papers carried each concept (lowercased) — the penalty weights."""
        counts: Counter = Counter()
        with self._conn() as c:
            for r in c.execute("SELECT concepts FROM dismissals"):
                try:
                    for concept in json.loads(r["concepts"] or "[]"):
                        if concept:
                            counts[concept.lower()] += 1
                except Exception:  # noqa: BLE001
                    continue
        return counts

    def count(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) AS n FROM dismissals").fetchone()["n"])


__all__ = ["DismissalStore"]
