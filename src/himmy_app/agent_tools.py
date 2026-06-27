"""Register the Himmy tool surface onto a himmy :class:`ToolRegistry`.

This is the ``tools_module`` the agent spec points at (``himmy_app.agent_tools:register``);
himmy's ``build_runtime_for_spec`` imports and calls it once at startup. It assembles:

* himmy built-in ``utils`` (calculator, current_time) + ``web`` (search/fetch) packs;
* himmy's **memory** pack — durable ``remember`` / ``recall``;
* this project's **papers RAG** connector (ask_papers / index_papers) over the
  Himmy-owned library. (Himmy no longer uses Zotero; the agent reads library.db directly.)

Each step is best-effort: a missing optional dependency skips that family rather than
crashing the agent. Returns the registered tool names.
"""

from __future__ import annotations

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors import PapersRagConnector

#: himmy built-in tool packs to bind alongside the academic tools.
#: ``tasks`` gives the agent list_tasks / add_task / complete_task over the shared task
#: board (the same SQLite store the server's /tasks endpoints read/write).
#: ``google`` gives the agent the connected Google account's read tools (gmail_inbox,
#: gcal_events) — registering the pack also exposes gmail_send/gcal_create, but the agent
#: spec's tools allowlist only admits the two READ-ONLY ones (no HITL layer is built yet).
#: When no Google account is connected the tools return a friendly hint, never crash.
#: ``data-sources`` is himmy's KEYLESS public-data pack — ``weather``, ``geocode``,
#: ``wikipedia`` — so Himmy can check the forecast, locate a place, and look up quick facts
#: with no API key.
_BUILTIN_PACKS = ["utils", "web", "data-sources", "tasks", "google"]


def register(registry: ToolRegistry) -> list[str]:
    """Register the tool surface; return the registered names (best-effort)."""
    registered: list[str] = []

    # --- himmy built-in packs (utils, web: calculator, current_time, web search/fetch) ----
    try:
        from himmy.toolkit import ToolkitConfig, register_packs

        before = {d.name for d in registry.list()}
        register_packs(registry, _BUILTIN_PACKS, ToolkitConfig())
        registered += [d.name for d in registry.list() if d.name not in before]
    except Exception:  # noqa: BLE001 - a missing optional extra must not break the agent
        pass

    # --- himmy memory pack: durable remember/recall (shares the spec's auto-recall store) --
    try:
        from himmy.toolkit import ToolkitConfig
        from himmy.toolkit.memory import register_memory_pack

        before = {d.name for d in registry.list()}
        register_memory_pack(registry, ToolkitConfig.from_env())
        registered += [d.name for d in registry.list() if d.name not in before]
    except Exception:  # noqa: BLE001 - memory is best-effort (e.g. no embedder available)
        pass

    # --- this project's connector: papers RAG over the Himmy library --------------------
    try:
        registered += PapersRagConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - a connector wiring hiccup must not break the agent
        pass

    # --- Google Calendar write tools: find / add / edit / remove --------------------------
    try:
        from himmy_app.connectors.google_calendar import GoogleCalendarConnector

        registered += GoogleCalendarConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; calendar editing just won't be offered
        pass

    # --- Himmy's "hands": save_article / add_paper (direct) + mail_send (approval-gated) ---
    try:
        from himmy_app.connectors.actions import ActionsConnector

        registered += ActionsConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort
        pass

    # --- Gmail hands: triage / read / reply (gated) / draft (reversible) -------------------
    try:
        from himmy_app.connectors.gmail_actions import GmailActionsConnector

        registered += GmailActionsConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; email actions just won't be offered
        pass

    # --- Buddha Air: live Nepal-domestic fares + a booking deep-link -----------------------
    try:
        from himmy_app.connectors.buddha_air import BuddhaAirConnector

        registered += BuddhaAirConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; flight search just won't be offered
        pass

    # --- Bussewa: live Nepal bus tickets + a booking deep-link -----------------------------
    try:
        from himmy_app.connectors.bussewa import BussewaConnector

        registered += BussewaConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; bus search just won't be offered
        pass

    # --- Foodmandu: Nepal food-delivery restaurant search + an order link ------------------
    try:
        from himmy_app.connectors.foodmandu import FoodmanduConnector

        registered += FoodmanduConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; food search just won't be offered
        pass

    # --- Daraz: Nepal online-shopping product search + a buy link --------------------------
    try:
        from himmy_app.connectors.daraz import DarazConnector

        registered += DarazConnector().register_tools(registry)
    except Exception:  # noqa: BLE001 - best-effort; shopping search just won't be offered
        pass

    # De-dup while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for name in registered:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def registered_tool_names(registry: ToolRegistry | None = None) -> list[str]:
    """Convenience: register into a fresh (or given) registry and return the names."""
    reg = registry if registry is not None else ToolRegistry()
    return register(reg)


__all__ = ["register", "registered_tool_names"]
