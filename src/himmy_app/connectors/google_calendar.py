"""Google Calendar agent tools — let Himmy READ, ADD, EDIT, and REMOVE events.

The himmy ``google`` pack ships ``gcal_events`` (read, no ids) + ``gcal_create``; to let the
agent edit or delete a specific event it needs the event's id, so this connector exposes a
richer set over ``himmy.api.studio_google``:

* ``calendar_find``   — upcoming events WITH their ids (so the agent can locate one to change)
* ``calendar_add``    — create an event
* ``calendar_edit``   — patch an existing event (only the fields given)
* ``calendar_remove`` — delete an event

All run against the connected Google account and require the ``calendar.events`` scope (already
granted at sign-in). They no-op with a friendly message when no account is connected.
"""

from __future__ import annotations

from typing import Any

from himmy.services.tools.registry import ToolRegistry

from himmy_app.connectors._register import safe_register_local_tool


def _event_dict(e: Any) -> dict[str, Any]:
    return {
        "id": e.id, "summary": e.summary, "start": e.start, "end": e.end,
        "location": e.location, "html_link": e.html_link,
        "recurring_event_id": getattr(e, "recurring_event_id", None),
    }


class GoogleCalendarConnector:
    """Registers calendar_find / calendar_add / calendar_edit / calendar_remove."""

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        from himmy.api import studio_google as g

        def _connected() -> bool:
            try:
                return bool(g.status().connected)
            except Exception:  # noqa: BLE001
                return False

        async def calendar_find(args: dict[str, Any]) -> dict[str, Any]:
            if not _connected():
                return {"ok": False, "connected": False, "message": "No Google account is connected."}
            try:
                events = await g.calendar_list(int(args.get("max", 50)))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Calendar read failed: {exc}"}
            q = str(args.get("query") or "").strip().lower()
            out: list[dict[str, Any]] = []
            seen_series: set[str] = set()
            for e in events:
                d = _event_dict(e)
                if q and q not in (d["summary"] or "").lower():
                    continue
                rid = d.get("recurring_event_id")
                if rid:  # collapse a repeating series to one row (its next occurrence)
                    if rid in seen_series:
                        continue
                    seen_series.add(rid)
                    d["repeats"] = True
                out.append(d)
            return {
                "ok": True, "events": out,
                "note": (
                    "To change/cancel ONE occurrence use its `id`. For a REPEATING event "
                    "(`repeats: true`), cancel the WHOLE series by passing its `recurring_event_id` "
                    "to calendar_remove (or to calendar_edit to change every occurrence)."
                ),
            }

        async def calendar_add(args: dict[str, Any]) -> dict[str, Any]:
            if not _connected():
                return {"ok": False, "message": "Connect a Google account first."}
            summary = str(args.get("summary") or "").strip()
            start, end = str(args.get("start") or "").strip(), str(args.get("end") or "").strip()
            if not summary or not start or not end:
                return {"ok": False, "message": "Need summary, start, and end (RFC3339, e.g. 2026-06-21T09:00:00Z)."}
            rec = args.get("recurrence")
            recurrence = None
            if rec:
                recurrence = [str(x) for x in rec] if isinstance(rec, list) else [str(rec)]
                recurrence = [r if r.upper().startswith("RRULE") else f"RRULE:{r}" for r in recurrence]
            try:
                e = await g.calendar_create(
                    summary, start, end,
                    all_day=bool(args.get("all_day", False)),
                    location=(args.get("location") or None),
                    recurrence=recurrence,
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Couldn't create the event: {exc}"}
            return {"ok": True, "event": _event_dict(e), "repeating": bool(recurrence)}

        async def calendar_edit(args: dict[str, Any]) -> dict[str, Any]:
            if not _connected():
                return {"ok": False, "message": "Connect a Google account first."}
            eid = str(args.get("event_id") or "").strip()
            if not eid:
                return {"ok": False, "message": "event_id is required (get it from calendar_find)."}
            try:
                e = await g.calendar_update(
                    eid,
                    summary=args.get("summary"), start=args.get("start"), end=args.get("end"),
                    all_day=bool(args.get("all_day", False)), location=args.get("location"),
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Couldn't update the event: {exc}"}
            return {"ok": True, "event": _event_dict(e)}

        async def calendar_remove(args: dict[str, Any]) -> dict[str, Any]:
            if not _connected():
                return {"ok": False, "message": "Connect a Google account first."}
            # recurring_event_id wins → deletes the WHOLE repeating series; else event_id → one event.
            rid = str(args.get("recurring_event_id") or "").strip()
            eid = str(args.get("event_id") or "").strip()
            # If the model handed us an instance id but asked for the series, derive the master.
            target = rid or eid
            if not target:
                return {"ok": False, "message": "Pass event_id (one event) or recurring_event_id (the series)."}
            try:
                await g.calendar_delete(target)
            except Exception as exc:  # noqa: BLE001
                # An instance id of a series can 404 on the master path; fall back to its master.
                if not rid and "_" in eid:
                    try:
                        await g.calendar_delete(eid.split("_", 1)[0])
                        return {"ok": True, "deleted": eid.split("_", 1)[0], "series": True}
                    except Exception as exc2:  # noqa: BLE001
                        return {"ok": False, "message": f"Couldn't delete the event: {exc2}"}
                return {"ok": False, "message": f"Couldn't delete the event: {exc}"}
            return {"ok": True, "deleted": target, "series": bool(rid)}

        _dt = {"type": "string", "description": "LOCAL wall-clock datetime, no timezone, e.g. 2026-06-21T13:00:00 for 1 PM; or YYYY-MM-DD if all_day."}
        safe_register_local_tool(
            registry, name="calendar_find", read_only=True, handler=calendar_find,
            description=(
                "List the user's upcoming Google Calendar events WITH their ids. Use this first "
                "when the user wants to change or cancel an event, to find the right `id`. "
                "Optional `query` filters by title; optional `max` (default 25)."
            ),
            args_json_schema={"type": "object", "properties": {
                "query": {"type": "string"}, "max": {"type": "integer"}}},
        )
        safe_register_local_tool(
            registry, name="calendar_add", read_only=False, requires_approval=True, handler=calendar_add,
            description=(
                "Create an event on the user's Google Calendar. Pass `summary`, `start`, `end`. "
                "For a timed event use RFC3339 with a zone (…Z); set `all_day: true` with "
                "YYYY-MM-DD dates for all-day. Optional `location`. For a REPEATING event pass "
                "`recurrence` — a list of one RRULE string. `start`/`end` are the FIRST occurrence. "
                "Examples: weekly on Tuesday until Aug 7 → "
                "[\"RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20260807T235959Z\"]; every weekday for 10 times "
                "→ [\"RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;COUNT=10\"]; daily → [\"RRULE:FREQ=DAILY\"]. "
                "BYDAY codes: SU MO TU WE TH FR SA. UNTIL is UTC YYYYMMDDT235959Z."
            ),
            args_json_schema={"type": "object", "properties": {
                "summary": {"type": "string"}, "start": _dt, "end": _dt,
                "all_day": {"type": "boolean"}, "location": {"type": "string"},
                "recurrence": {"type": "array", "items": {"type": "string"},
                               "description": "RRULE list for a repeating event, e.g. [\"RRULE:FREQ=WEEKLY;BYDAY=TU\"]."}},
                "required": ["summary", "start", "end"]},
        )
        safe_register_local_tool(
            registry, name="calendar_edit", read_only=False, requires_approval=True, handler=calendar_edit,
            description=(
                "Change an existing Google Calendar event. Pass its `event_id` (from "
                "calendar_find) plus only the fields to change: `summary`, `start`, `end`, "
                "`location`, `all_day`."
            ),
            args_json_schema={"type": "object", "properties": {
                "event_id": {"type": "string"}, "summary": {"type": "string"},
                "start": _dt, "end": _dt, "all_day": {"type": "boolean"},
                "location": {"type": "string"}}, "required": ["event_id"]},
        )
        safe_register_local_tool(
            registry, name="calendar_remove", read_only=False, requires_approval=True, handler=calendar_remove,
            description=(
                "Delete a Google Calendar event. To cancel an ENTIRE repeating series, pass its "
                "`recurring_event_id` (from calendar_find). To cancel a single one-off event or "
                "just one occurrence, pass `event_id`. Prefer `recurring_event_id` when the user "
                "says 'all of them' / 'the weekly …' / 'every'."
            ),
            args_json_schema={"type": "object", "properties": {
                "event_id": {"type": "string", "description": "id of a single event / one occurrence"},
                "recurring_event_id": {"type": "string", "description": "series id → deletes ALL occurrences"}}},
        )
        return ["calendar_find", "calendar_add", "calendar_edit", "calendar_remove"]


__all__ = ["GoogleCalendarConnector"]
