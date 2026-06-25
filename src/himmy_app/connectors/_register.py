"""Resilient tool registration that survives himmy's durable content-addressing.

himmy's durable entity registry (``build_runtime_for_spec(durable_defaults=True)``)
content-addresses every ``tool_definition``: re-registering a tool whose payload has
EVOLVED since a prior run — e.g. we edited a description or turned on approval-gating —
raises a ``Content-address violation``. Crucially, :meth:`ToolRegistry.register` writes
the in-memory definition + handler BEFORE it projects the record onto the durable spine,
so the tool is already fully usable when that projection raises. Left unhandled, though,
the exception unwinds out of the connector and every tool declared AFTER the offending
one is silently never registered.

That is exactly the bug that left ``calendar_edit`` / ``calendar_remove`` missing while
``calendar_find`` / ``calendar_add`` (declared before them) survived — the model then
reported "unknown tool 'calendar_remove'".

``safe_register_local_tool`` isolates each tool. On a content-address violation it keeps
the already-registered, CURRENT-payload in-memory tool and moves on (only the append-only
audit record stays at the older version). Any other error is logged and skipped so one
bad tool can never take its neighbours down with it.
"""

from __future__ import annotations

import logging
from typing import Any

from himmy.services.tools.registry import register_local_tool

_log = logging.getLogger("himmy_app.connectors")


def safe_register_local_tool(registry: Any, *, name: str, **kwargs: Any) -> str | None:
    """Register one local tool; return its name, or ``None`` if it truly couldn't register.

    Never raises: a single tool's failure must not abort a connector's other tools.
    """
    try:
        register_local_tool(registry, name=name, **kwargs)
        return name
    except Exception as exc:  # noqa: BLE001 - isolate every tool's registration
        # A content-address violation is benign: register() set the in-memory
        # definition + handler (with the current payload) before the durable
        # projection raised, so the tool already works this run.
        if "Content-address violation" in str(exc):
            return name
        _log.warning("tool %r failed to register: %s", name, exc)
        return None


__all__ = ["safe_register_local_tool"]
