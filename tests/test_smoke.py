"""Smoke tests: the tool surface assembles and Zotero failures degrade gracefully.

These run WITHOUT a model and WITHOUT Zotero open — they prove the wiring is sound.
"""

from __future__ import annotations

import pytest

from himmy_app.agent_tools import registered_tool_names
from himmy_app.config import load_config
from himmy_app.connectors.zotero_client import ZoteroClient, ZoteroUnavailable, citation, format_item


def test_config_defaults_to_openrouter_gemini() -> None:
    cfg = load_config()
    assert cfg.provider == "openrouter"
    assert cfg.model == "google/gemini-2.5-flash"
    assert cfg.zotero_items_url.endswith("/users/0/items")


def test_tool_surface_registers_expected_tools() -> None:
    # Himmy reads its own library.db directly now (no Zotero tools); assert today's surface.
    names = registered_tool_names()
    for expected in (
        "ask_papers", "index_papers", "add_paper", "save_article",
        "list_tasks", "add_task", "complete_task", "remember", "recall",
    ):
        assert expected in names, f"{expected} missing from {names}"


def test_format_and_cite_item() -> None:
    raw = {
        "key": "ABCD1234",
        "data": {
            "title": "Deep Learning for Soil Carbon",
            "creators": [{"firstName": "Jane", "lastName": "Smith"},
                         {"firstName": "Li", "lastName": "Wang"}],
            "date": "2021-05-01",
            "itemType": "journalArticle",
            "publicationTitle": "Nature",
            "DOI": "10.1000/xyz",
            "tags": [{"tag": "soil"}, {"tag": "ml"}],
        },
        "meta": {"creatorSummary": "Smith and Wang", "parsedDate": "2021-05-01"},
    }
    item = format_item(raw)
    assert item["title"] == "Deep Learning for Soil Carbon"
    assert item["authors"] == ["Jane Smith", "Li Wang"]
    assert item["year"] == "2021"
    assert item["tags"] == ["soil", "ml"]
    assert 'Smith and Wang (2021), Nature' in citation(item)


@pytest.mark.asyncio
async def test_zotero_unavailable_when_app_closed() -> None:
    # Point at a dead port so we deterministically hit a connection error.
    client = ZoteroClient("http://localhost:1/users/0/items", "http://localhost:1/users/0/collections")
    with pytest.raises(ZoteroUnavailable):
        await client.search_items("anything")
