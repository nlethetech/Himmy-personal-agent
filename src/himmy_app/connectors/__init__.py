"""Himmy's own connectors (built on himmy's tool registry).

A connector exposes ``register_tools(registry) -> list[str]`` and is wired into
:func:`himmy_app.agent_tools.register`.
"""

from __future__ import annotations

from himmy_app.connectors.finance import FinanceConnector
from himmy_app.connectors.media import MediaConnector
from himmy_app.connectors.papers_rag import PapersRagConnector
from himmy_app.connectors.zotero import ZoteroConnector

__all__ = ["ZoteroConnector", "PapersRagConnector", "MediaConnector", "FinanceConnector"]
