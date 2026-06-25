# Mail tab overhaul — plan

Goal: stop the "random mail" (filter by who/what) and fix the empty-looking layout.
Decisions (2026-06-25): **two-pane** layout · default view **Focused (people + important)** · build **all three phases** including the AI daily summary.

## The core discovery

Gmail already tags every inbox message with a category — `CATEGORY_PROMOTIONS`,
`CATEGORY_SOCIAL`, `CATEGORY_UPDATES`, `CATEGORY_FORUMS`, or Primary (real people) — plus an
`UNREAD` / `IMPORTANT` / `STARRED` flag. These arrive in the metadata we already download, but
`studio_google.py:_parse_message` discards them. Capturing them is nearly free and unlocks all the
filtering.

---

## Foundation (data layer)

**A. Capture categories + read-state — shared himmy framework (additive, safe).**
In `himmy-agent-test/himmy/api/studio_google.py`:
- `GmailMessage`: add `label_ids: list[str] = []`, `unread: bool = False`.
- `_parse_message` (and `_parse_message_full`): set `label_ids = msg.get("labelIds", [])`,
  `unread = "UNREAD" in label_ids`.
- Purely additive — yetidai and the other consumers never read these fields, so nothing else
  changes. Run the framework's gmail tests after.

**B. Derive + pass through — `himmy_app/server.py` `/mail/inbox`.**
- Raise the fetch cap (30 → ~50) so the category tabs have enough to show.
- For each message derive `category` (focused / promotions / social / updates / forums from the
  labels), `unread`, `important`, `starred`; include them in the response.

**C. Types — `desktop/src/lib/api.ts`.** Extend `MailMessage` with `category`, `unread`,
`important`, `starred`.

---

## Phase 1 — categories + search + two-pane redesign (the big wins)

Frontend, `desktop/src/App.tsx` (`MailTab` + new row/reader/panes):
- **Header**: account + unread count, Refresh, a **search box**, a **category tab bar**
  (`Focused · Promotions · Social · Updates · All`) with counts, and an **Unread-only** toggle.
  Opens on **Focused**.
- **Two-pane**: list on the left (~380px), reading pane on the right. Selecting a row highlights
  it and renders the body on the right (no full-view swap). Restyle the existing `MailReader`
  to live in the pane; keep its "Draft / Reply with Himmy" actions.
- **Row redesign**: colored initial **avatar**, sender, subject, snippet, time, **unread dot +
  bold**, a small **category chip**, hover quick-actions.
- **Date grouping** headers (Today / Yesterday / Earlier) and friendly **empty states** per tab.
- Filtering (tab + search + unread) runs client-side over the fetched list — instant.

## Phase 2 — sender rules (mute · VIP · people-only)

- Local store in `.scholar-desk/` (`mail_rules.json` or a tiny SQLite table): `muted_senders`,
  `vip_senders`. New endpoints `GET/POST /mail/rules`.
- **Mute** (row hover) → hide all future mail from that sender across every tab, with an "N muted"
  affordance to unhide.
- **VIP** → star a sender; they surface in Focused even if Gmail miscategorized, and sort to top.
- **People-only** toggle → `is_automated(sender)` heuristic (noreply / no-reply / notifications /
  donotreply / mailer …) hides every bot sender, leaving humans. Works even if the user has
  Gmail's category tabs turned off.
- Rules applied server-side so caches and tab counts stay correct.

## Phase 3 — AI daily inbox summary

- `GET /mail/digest` runs Himmy (gemini-2.5-flash) over the Focused inbox: who's waiting on a
  reply, anything time-sensitive — a short bullet list linking to the messages. Reuses the
  existing `mail_list` / `mail_read` tools. Cached (once a day / on demand), not per open.
- UI: a dismissible **"Today in your inbox"** card atop the Focused tab.

---

## Notes / risks
- Focused leans on Gmail's category tabs; if the user disabled them, most mail lands in Primary —
  the Phase-2 noreply heuristic + mute are the fallback (People-only doesn't need categories).
- 50 messages = 50 metadata calls (current per-message design); the existing cache + TTL covers it.
- No new OAuth scopes — `gmail.readonly` already covers labels.

## Build order
Phase 1 → verify live against the real inbox → Phase 2 → Phase 3. Commit after each phase
(authored `nlethetech`, no AI attribution).
