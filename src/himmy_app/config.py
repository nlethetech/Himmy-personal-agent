"""Runtime configuration for Himmy — provider, model, durable paths, Zotero.

No secret VALUES live here, only names + non-secret knobs. The provider defaults to
``openrouter`` (gemini-2.5-flash) because that is the backend whose native function-calling
actually fires our Zotero / RAG tools — the local claude-cli text protocol does not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Default inference backend + model. OpenRouter's gemini-2.5-flash is a strong, inexpensive
#: tool-caller. Override with HIMMY_APP_PROVIDER / HIMMY_APP_MODEL.
DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODEL = "google/gemini-2.5-flash"

#: Durable audit/run/memory + the papers text cache live under here (kept out of git).
#: NOTE: the on-disk folder stays ".scholar-desk" on purpose even though the product is now
#: "Himmy" — it holds the user's REAL library, sign-ins, and memory. Renaming it would orphan
#: that data, and the folder name is never shown in the UI. The same goes for HIMMY_MEMORY_SUBJECT
#: and the RAG KB ids below: kept stable for data continuity, not branding.
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / ".scholar-desk"

#: Zotero's built-in local API (Zotero must be running). "users/0" = your local "My Library".
DEFAULT_ZOTERO_API_BASE = "http://localhost:23119/api"
DEFAULT_ZOTERO_LIBRARY = "users/0"


@dataclass(frozen=True)
class HimmyConfig:
    """Resolved, non-secret runtime configuration."""

    provider: str
    model: str | None
    data_dir: Path
    zotero_api_base: str
    zotero_library: str
    max_turns: int = 8

    @property
    def store_path(self) -> Path:
        return self.data_dir / "storage.db"

    @property
    def papers_cache_path(self) -> Path:
        """SQLite cache of extracted paper text, so PDF extraction happens once."""
        return self.data_dir / "papers_cache.db"

    @property
    def reading_db_path(self) -> Path:
        """SQLite log of engaged reading time per paper (drives recsys + the Today home)."""
        return self.data_dir / "reading.db"

    @property
    def feedback_db_path(self) -> Path:
        """SQLite store of 'not interested' dismissals (teaches the recommender what to avoid)."""
        return self.data_dir / "feedback.db"

    @property
    def task_extras_db_path(self) -> Path:
        """Sidecar store for richer task fields (notes, subtasks, recurrence, paper link,
        time-block) that himmy's core task store doesn't carry — keyed by the himmy task id."""
        return self.data_dir / "task_extras.db"

    @property
    def zotero_items_url(self) -> str:
        return f"{self.zotero_api_base.rstrip('/')}/{self.zotero_library.strip('/')}/items"

    @property
    def zotero_collections_url(self) -> str:
        return f"{self.zotero_api_base.rstrip('/')}/{self.zotero_library.strip('/')}/collections"


def load_config() -> HimmyConfig:
    """Build :class:`HimmyConfig` from the environment and export himmy's durable paths."""
    data_dir = Path(os.environ.get("HIMMY_APP_DATA_DIR") or str(DEFAULT_DATA_DIR)).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    provider = (os.environ.get("HIMMY_APP_PROVIDER") or DEFAULT_PROVIDER).strip()
    model = (os.environ.get("HIMMY_APP_MODEL") or DEFAULT_MODEL).strip() or None
    max_turns = int(os.environ.get("HIMMY_APP_MAX_TURNS") or "8")

    zotero_api_base = (os.environ.get("ZOTERO_API_BASE") or DEFAULT_ZOTERO_API_BASE).strip()
    zotero_library = (os.environ.get("ZOTERO_LIBRARY") or DEFAULT_ZOTERO_LIBRARY).strip()

    # himmy durable store + long-term memory live under .scholar-desk/ regardless of cwd.
    os.environ.setdefault("HIMMY_STORE_PATH", str(data_dir / "storage.db"))
    os.environ.setdefault("HIMMY_MEMORY_PATH", str(data_dir / "memory.db"))
    os.environ.setdefault("HIMMY_EMBEDDER", "fastembed")
    os.environ.setdefault("HIMMY_MEMORY_SUBJECT", "scholar-desk")
    # Pin the tasks board to a fixed, cwd-independent path so the agent's tasks tools and
    # the server's /tasks endpoints share the SAME SQLite store (otherwise himmy's
    # get_tasks_store() resolves .himmy/tasks.db relative to the process cwd).
    os.environ.setdefault("HIMMY_TASKS_PATH", str(data_dir / "tasks.db"))
    # Pin the durable conversation store (Cmd-K persistent chats) to a fixed, cwd-independent
    # path under .scholar-desk/ so /sessions, /ask, and /ask/stream all read & write one DB
    # (otherwise conversations_db_path() resolves .himmy/conversations.db relative to cwd).
    os.environ.setdefault("HIMMY_CONVERSATIONS_PATH", str(data_dir / "conversations.db"))
    # Pin the routines store (saved automations / schedules) to a fixed, cwd-independent path
    # under .scholar-desk/ so the in-app scheduler and the /routines endpoints share ONE DB
    # (otherwise himmy's get_routines_store() resolves .himmy/routines.db relative to the
    # process cwd, and the Electron-spawned backend's cwd is not guaranteed stable across
    # launches — saved automations would silently "disappear" after a restart).
    os.environ.setdefault("HIMMY_ROUTINES_PATH", str(data_dir / "routines.db"))
    # Wall-clock schedules (daily / cron) are interpreted in this timezone unless a routine
    # overrides it. Defaults to Nepal local time; override with HIMMY_TZ in .env.
    os.environ.setdefault("HIMMY_TZ", "Asia/Kathmandu")
    # Use a WRITABLE secrets backend so the Google sign-in (Mail/Calendar) can persist the
    # user's OAuth client + tokens. On macOS this is the system keychain (tokens never touch
    # disk in plaintext); on other platforms himmy falls back to an encrypted file store
    # under the secrets dir. Default env-only secrets are read-only → Connect would no-op.
    os.environ.setdefault("HIMMY_SECRETS", "keychain")

    return HimmyConfig(
        provider=provider,
        model=model,
        data_dir=data_dir,
        zotero_api_base=zotero_api_base,
        zotero_library=zotero_library,
        max_turns=max_turns,
    )


__all__ = [
    "HimmyConfig",
    "load_config",
    "DEFAULT_PROVIDER",
    "DEFAULT_MODEL",
    "DEFAULT_DATA_DIR",
]
