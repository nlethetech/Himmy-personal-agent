# Himmy as a personal command center — unified roadmap (plan)
_2026-06-23, multi-agent design (NEPSE + planner + agent) + adversarial pressure-test, grounded in the code._

## Headline
Himmy becomes a single local command center where one approval-gated agent reasons across your research, your day, your money, and your comms — reusing the NEPSE Quant Desk you already built instead of rebuilding it, paper-only and with no live trading.

## Architecture (end state)
End-state: the Himmy desktop app (Electron + a FastAPI backend on :8131 running ONE himmy agent) stays the brain, and four "hands" hang off it through the patterns that already work. Pillar 1, the PLANNER, grows from a flat title-only checklist into a real day/life core: tasks gain due dates, priorities, projects, and cross-surface links (a task can point at a paper or a NEPSE position), with the genuinely new concepts (projects) living in an app-owned planner.db so the shared himmy tasks store is never forked. Pillar 2, the AGENT/chief-of-staff layer, is mostly a persona and wiring upgrade: the agent is told it runs money+time+research+comms as one remit, it remembers your strategy and risk tolerance, and one opt-in weekday morning brief proactively pulls all four domains together. Pillar 3, the NEPSE money hand, is a paper-only connector that registers read tools (portfolio, NAV, quotes, movers, today's signals) plus exactly one approval-gated write tool (paper_submit_order); it never touches a live-trading code path. The agent that ties them together is the existing ask_turn/resume_turn loop with durable memory, self-learning, grounding/PII guardrails, the HITL ApprovalCard, and the just-built Routines/Inbox layer — all reused, not rebuilt. A /today aggregator endpoint composes tasks + calendar + money into the one glanceable cockpit that is the literal "command center" payoff. The NEPSE-desk seam is the one load-bearing piece of new infrastructure: a thin subprocess bridge that talks to the Desk's engine_bridge under the Desk's OWN python interpreter and fails open everywhere.

## NEPSE↔Desk seam decision (binding)
DECISION: subprocess-only, under the Desk's own interpreter — NOT in-process import and NOT editable-install. This is binding and overrides both pillar designs, which both wrongly recommended an in-process `from backend.api.engine_bridge import ...` behind try/except. Verified this session: the Himmy venv has fastapi 0.137.2 / starlette 1.3.1 / pydantic 2.13.4 and NO pandas; the Desk venv has fastapi==0.136.3 / starlette==1.2.0 / pandas==3.0.3, and engine_bridge does `import pandas as pd` plus `from backend.quant_pro... / backend.workstation... / validation.transaction_costs` at module top. So an in-process import physically cannot work (pandas + the whole desk tree are absent from Himmy's venv), and editable-installing the desk would force fastapi/starlette downgrades that Himmy's own memory (himmy_dep_upper_bounds_gotcha) says break ~55 of its tests. The venvs are mutually exclusive. THE SEAM: a `src/himmy_app/desk_bridge.py` that spawns `$NEPSE_DESK_PYTHON` (the Desk's `.venv/bin/python`) running a tiny shim that dispatches a WHITELISTED engine_bridge function name + JSON args over line-delimited JSON-RPC, with `cwd`/`NEPSE_DB_FILE`/`HIMMY_NEPSE_ACCOUNT` set from config.py. Prefer a warm long-lived worker process (lazily started, health-checked, restarted on crash, hard per-call timeout) over spawn-per-call, because each cold start pays the pandas + SQLAlchemy + full-desk-import cost and would make the Today cockpit slow. The whitelist is the safety boundary: only the 18 verified paper-only function names dispatch — no arbitrary attribute/eval — so the agent can never reach a non-paper code path. Fail-open must be real and cover every failure mode (desk python missing, bad path, crash, timeout, malformed JSON, locked/empty/stale DB), each returning {connected:false}/{ok:false} so the Money card and money tools degrade exactly like the Google-not-connected cards.

## Safety posture
Paper-only, enforced structurally: Himmy only ever reaches engine_bridge, which is paper-only by construction (PaperExecutionService), and the subprocess shim whitelists exactly the 18 verified function names — no arbitrary attribute/eval — so the agent can never reach a live/real-trading module; no desk live-trading code is ever imported or wired, and no path requiring live broker credentials is ever added (paper reads are local SQLite). Exactly ONE write tool is exposed — paper_submit_order — and it is requires_approval=True, so it always parks an ApprovalCard rendering preview_order (fees + risk severity/reasons + same-day/circuit blocks) for full-context approval; there is never an auto-approve for money. Routines never auto-trade: routine-fired gated tools park as kind='approval' Inbox items (already handled in routines.py), and the morning brief uses only read tools + add_task, instructed 'no submitting orders, no calendar writes'. Grounding/no-fabrication: the persona requires money answers be grounded in real portfolio_view/live_signals/nav_history output with stale data labeled by as-of time, never invented — same anti-hallucination discipline the library answers enforce. Secrets/config: desk path, NEPSE_DB_FILE, account_id live in config.py/env, never hardcoded; the existing PII guardrail + redact_sensitive_args run on every approval card; memory is local-only single-user. Fail-open is mandatory and real across every failure mode so a hung subprocess can never block a turn or the cockpit.

## Phased roadmap

### Phase 0 — nepse · effort M
**Goal:** Prove the cross-venv subprocess seam works and fails open, before any money feature depends on it.

**What you get:** Confidence that Himmy can read your paper portfolio at all, without a hung subprocess ever freezing the agent or the cockpit.

- Create src/himmy_app/desk_bridge.py: a warm long-lived subprocess worker that launches $NEPSE_DESK_PYTHON (the Desk's .venv/bin/python at /Users/samriddhagc/LocalProjects/Nepse_quant_clean/.venv/bin/python) running a shim that dispatches WHITELISTED engine_bridge function names + JSON args over line-delimited JSON-RPC
- Whitelist EXACTLY the 18 verified engine_bridge functions (get_quote, get_ltp, get_movers, search_symbols, portfolio_view, record_nav_snapshot, nav_history, trades, open_orders, order_history, preview_order, same_day_block, circuit_block, submit_order, cancel_order, cancel_all_orders, live_signals, get_store) — no arbitrary attribute/eval dispatch
- Add config in src/himmy_app/config.py: NEPSE_DESK_PATH (cwd for the subprocess), NEPSE_DESK_PYTHON, NEPSE_DB_FILE (verified env var; desk falls back to data/nepse_market_data.db), HIMMY_NEPSE_ACCOUNT (default account_1)
- Implement worker lifecycle: lazy start, health-check, restart-on-crash, hard per-call timeout; fail-open returning {connected:false} on every failure mode (python missing / bad path / crash / timeout / malformed JSON / locked / empty / stale DB)
- Smoke test runnable from the himmy repo asserting portfolio_view(account_1) round-trips JSON AND every fail-open path degrades (no exception escapes to the agent turn)

### Phase 1 — planner · effort S
**Goal:** Un-dead the task due-date end-to-end and add priorities + smart sort — turn the flat checklist into a real to-do list.

**What you get:** You can finally give a task a deadline and a priority from the app; the red 'overdue' styling that exists but never fires becomes live, and the top of the list is always 'what to do next'. No desk dependency, ships in parallel with phase 0.

- Change TaskCreateRequest (server.py:130) from {title} to {title, due?, priority?} and pass them into the store — currently tasks_add (server.py:560) calls _tasks_store().add(title) dropping the due= the store/pack already accept; _task_dict (server.py:545) already exposes due
- Add PATCH /tasks/{id} in server.py for editing due/priority/done
- Additive idempotent ALTER TABLE migrations in himmy/api/studio_tasks.py (pragma table_info guard): priority INTEGER, project_id TEXT, link_type TEXT, link_ref TEXT, scheduled_event_id TEXT — additive so the shared himmy tasks pack and other consumers keep working
- Add api.tasks.add(title,{due,priority}) + api.tasks.patch(id,fields) in desktop/src/lib/api.ts; native <input type=date> + priority chips in the Tasks component (App.tsx:1788) and Today's add-row (App.tsx:444)
- Sort by (open first, overdue, priority desc, due asc) in Tasks (App.tsx:1828) and Today's openTasks.slice (App.tsx:438); priority chip on TaskRow (App.tsx:1882) so isOverdue (App.tsx:622) actually fires

### Phase 2 — planner · effort M
**Goal:** Projects (group tasks) and cross-surface links (task → paper or NEPSE position).

**What you get:** Your work stops being one undifferentiated list — 'what's left on the thesis' is separate from chores — and a task like 'Review NABIL thesis' deep-links straight to the position or the paper. This is what fuses four apps into one command center.

- Create src/himmy_app/planner_store.py with a ProjectsStore mirroring the studio_tasks singleton+SQLite pattern, in a SEPARATE app-owned .scholar-desk/planner.db (NOT himmy's tasks.db): projects(id,name,color,archived,created_at)
- Endpoints in server.py: GET/POST /projects, PUT/DELETE /projects/{id}; let GET /tasks?project= filter via the project_id column from phase 1
- Use the additive link_type/link_ref columns; on click route via the existing nav() switch (App.tsx:281): paper -> Library+open item, position/symbol -> portfolio surface
- UI: add a Projects lane to the Planner segmented control (App.tsx:1471) or a left rail grouping tasks by project; emit emitRefresh('tasks'/'projects') on mutation via the existing bus (App.tsx:53-59)

### Phase 3 — nepse · effort L
**Goal:** Register the paper-only NEPSE READ connector over the phase-0 subprocess bridge.

**What you get:** Himmy can finally see your money: 'how's my book doing', 'what fired today', portfolio P&L, equity curve, movers — all read-only, paper-only, and graceful when the desk isn't synced.

- Create src/himmy_app/connectors/nepse_desk.py (NepseDeskConnector mirroring ActionsConnector/GoogleCalendarConnector) registering read_only=True tools via safe_register_local_tool: portfolio_view, nav_history, get_quote, get_ltp, get_movers, search_symbols, live_signals, open_orders, order_history, trades, preview_order — all calling desk_bridge with the configured account_id
- Register it in agent_tools.py register() as a best-effort try/except block alongside the Google/actions ones (~line 70)
- Surface as-of / staleness: every money read must carry the data's freshness so the UI can show 'data as of <time>, desk not synced' — engine_bridge reads the desk's market_quotes/stock_prices SQLite tables which are stale when NEPSE is closed (closed most of the week) or the fetcher hasn't run; never present stale numbers as live
- App NEVER calls record_nav_snapshot (it is a WRITE that mutates NAV history and can contend on the account lock) — only nav_history is read

### Phase 4 — cross · effort M
**Goal:** The /today aggregator + Money card — the one glanceable cockpit.

**What you get:** One screen, one fast load, showing your whole life: open/overdue/today's tasks, today's & next calendar events, and a Money card (NAV + day P&L + equity-curve last point + positions). The literal command-center payoff, read-only and paper-only.

- New GET /today in server.py composing existing helpers: _tasks_store().list(), the calendar range, and the read-only money bridge (portfolio_view -> nav/day_pnl/positions; nav_history -> equity curve last point)
- Rebuild the Today component (App.tsx:319) to call api.today() once; add a 4th 'Money' card (NAV + day P&L sparkline) with an as-of/staleness label alongside the existing Up next / To do / Jump back in / Usage cards
- Keep live-refresh via useRefreshSignal('tasks'|'calendar'|...); Money card fails open to a 'Connect the Quant Desk' state exactly like the Google-not-connected cards

### Phase 5 — agent · effort S
**Goal:** Persona upgrade to cross-domain chief-of-staff + deliberate life-memory.

**What you get:** Himmy stops behaving like a citations bot and starts connecting the dots — recalling your risk tolerance before any money comment, grounding money answers in real portfolio_view data, and treating its money remit as one with calendar/research. Cheap, high leverage, activates everything else.

- Edit agent/agent.yaml name/description/role/instructions: broaden role to 'personal chief-of-staff across research, money, time, and comms'; add a money instruction section mirroring the existing calendar section (which tools, ground in real data, recall risk profile first); add a 'connect the dots across surfaces' instruction
- Reinforce paper-only in the persona ('you can PROPOSE paper trades for approval; you have no live-trading ability') and grounding ('never fabricate prices/positions/P&L — read them from the tools; label stale data with its as-of time')
- Extend agent.yaml remember categories to portfolio strategy / risk tolerance / sizing rules / standing preferences and instruct RECALL before money advice — reuses the already-wired memory pack + auto-recall in agent_tools.py; no new storage
- Optional Today onboarding card / disabled seed routine that interviews the user once and calls remember for each profile fact

### Phase 6 — nepse · effort M
**Goal:** The single approval-gated paper_submit_order write tool, with preview_order in the ApprovalCard.

**What you get:** Himmy can propose a paper trade you approve with one tap, seeing the real fees and risk before you commit — inside the same assistant that runs your calendar and reads your papers. Zero regulatory/financial risk: paper-only by construction.

- Add paper_submit_order to nepse_desk.py with requires_approval=True so it always parks an ApprovalCard via the existing SqliteCheckpointStore (.scholar-desk/approvals.db) and, when fired by a routine, parks as a kind='approval' Inbox item — never auto-executes
- ApprovalCard must render preview_order output (TransactionCostModel fees, PreTradeRiskService severity/reasons, same_day_block/circuit_block result) so approval is never a bare 'approve?'; reuse the exact mail_send/calendar_add HITL machinery (ask_turn park -> resume_turn)
- Pass the configured account_id; the desk's own same_day_block/circuit_block/PreTradeRiskService run as the inner guard inside the subprocess
- DEFER cancel_order/cancel_all_orders until asked — ship reads + one gated submit only

### Phase 7 — agent · effort M
**Goal:** ONE opt-in weekday morning brief routine (measure cost before adding any second).

**What you get:** Each weekday morning Himmy hands you one skimmable brief spanning NEPSE pre-open + today's calendar + open/overdue tasks + one library connection — instead of you opening four tabs. The single best 'feels proactive' lever at one metered run/day.

- Edit _BRIEFING_PROMPT in src/himmy_app/routines.py to span four domains (portfolio_view + live_signals + movers -> calendar_find -> list_tasks -> ask_papers); keep it enabled=False (opt-in), weekday cron already used ('30 6 * * 1-5')
- Read tools ONLY: the routine proposes, never trades and never writes calendar — the prompt must say 'no submitting orders, no calendar writes' mirroring the existing no-mail/no-events guard; any gated action correctly parks in the Inbox awaiting a tap
- Brief must tolerate offline/stale desk data (label as-of, show 'desk not synced')
- COST CEILING: cap at 1 routine/day on gemini-2.5-flash; measure the per-run cost before adding any second routine

## Recommended first phase
Phase 0 — the seam spike (desk_bridge.py subprocess worker). It is the one binding correction to the pillar designs (the venvs are verified mutually exclusive, so in-process import is impossible), and ALL money value is blocked on it. Build it first and prove portfolio_view(account_1) round-trips JSON and that every fail-open path degrades. Phase 1 (un-dead due dates) can and should run in parallel since it has zero desk dependency, but Phase 0 is the gating risk that must be retired before anything else.

## Deferred (explicitly out of scope for now)
- Goals, habits, study/time-tracking stores (goals/habits/habit_log/time_log, progress rings, streaks, focus timer) — a whole second product; defer until tasks-with-due-dates + projects have actually been used
- Full unified provenance/activity audit spine (append-only kind='activity' rows for every chat+routine tool call) — enterprise-flavored; the existing ApprovalCards + Inbox already give the trust surface for one user
- Additional cross-surface routines (market-close wrap, frequent signal-watch that spawns tasks) — each is a metered gemini-2.5-flash run; ship one morning brief, measure cost, then decide
- Local task recurrence engine (recur column + daily roll-forward) — push recurrence to Google Calendar RRULE (already supported) instead of building a second system
- cancel_order / cancel_all_orders write tools — defer until the user asks; ship reads + one gated submit
- Batch approval (approve a trade + a calendar block in one tap) — keep strictly one-checkpoint-one-decision for auditability
- App ever calling record_nav_snapshot (it mutates the paper book and can contend on the account lock) — read nav_history only
- Personal budgeting / non-NEPSE finance and any live/real-trading capability — out of scope by safety design

## Integration risks (designed around)
- VENV PIN CONFLICT IS FATAL TO IN-PROCESS IMPORT (verified): himmy's venv has fastapi 0.137.2 / starlette 1.3.1 / pydantic 2.13.4 and NO pandas; the desk pins fastapi==0.136.3 / starlette==1.2.0 / pandas==3.0.3 and engine_bridge does `import pandas as pd` at module top plus imports the whole backend.workstation/quant_pro/backtesting tree. himmy memory (himmy_dep_upper_bounds_gotcha) explicitly says fastapi 0.137/starlette 1.x is what BREAKS himmy (~55 test fails) and you must pin <0.137. So you cannot editable-install the desk backend into the himmy venv (it would force a downgrade that breaks himmy) and you cannot in-process `from backend.api.engine_bridge import ...` (pandas missing, transitive desk deps absent). BOTH the Planner pillar and the Proactive-Agent pillar recommend exactly this in-process bridge (`desk_bridge.py`/`nepse_bridge.py` behind try/except, 'avoid subprocess unless venvs prove irreconcilable') — the recon proves the venvs ARE irreconcilable. Only Pillar 1's SUBPROCESS seam (run engine_bridge under the DESK's own .venv/bin/python, JSON over stdin/stdout) is viable. This is the single most important correction to the designs.
- HEAVY/STATEFUL DESK IMPORT: engine_bridge transitively imports PaperExecutionService, PreTradeRiskService, RuntimeStore, TransactionCostModel, get_db_connection, and (lazily) backtesting.nepse_realism. Even in a subprocess, each cold start pays the pandas + SQLAlchemy + full desk-backend import cost. A per-call subprocess (spawn python, import, run, exit) will add seconds of latency to every quote/portfolio read and make the Today cockpit slow. Mitigation: a persistent long-lived subprocess worker (one desk-venv python process speaking line-delimited JSON-RPC, started lazily, kept warm) rather than spawn-per-call — but that adds process-lifecycle/restart/health management the designs don't account for.
- MARKET-DATA / FETCHER DEPENDENCY (verified): get_quote/get_ltp/get_movers/live_signals read the desk's `market_quotes` and `stock_prices` SQLite tables (DB path from env NEPSE_DB_FILE, else data/nepse_market_data.db). These are only populated if the desk's fetcher has run. When NEPSE is closed (it is closed most of the week per the trading-week note) or the desk hasn't synced, the data is STALE, and live_signals is best-effort (reads a precomputed signals payload). The designs' Money card and morning brief must surface a staleness/as-of timestamp and a 'desk not synced' state, not present stale numbers as live — Pillar 2's last openQuestion flags this correctly; it must be a build requirement, not a question.
- ACCOUNT + DATA-DIR CONFIG IS MULTI-VARIABLE, not just account_id: making the bridge work needs (a) the configured paper account_id for RuntimeStore.for_account (memory says account_1), (b) NEPSE_DB_FILE pointing at the desk's market DB, AND (c) the desk runtime/accounts dir (PAPER_ACCOUNTS_DIR under the desk RUNTIME_DIR) reachable from the subprocess. RuntimeStore takes interprocess file LOCKS (threading.RLock + flock per account path) — so if the desk's own web SaaS or TUI is running against the same account concurrently, writes (submit_order/cancel) can contend. Single-user mitigates this but the design must assume the desk app may also be open.
- FAIL-OPEN MUST BE REAL, NOT ASPIRATIONAL: the designs lean on the `_google_connected()` fail-open pattern (return {connected:false} on ImportError). With the subprocess seam the failure modes are richer than an ImportError: desk venv python missing, desk path wrong, subprocess crash/timeout, malformed JSON, DB locked, empty/stale tables. Every money tool and the /today aggregator must treat ALL of these as 'desk not connected / no data' and degrade — a hung subprocess must time out so it can never block the agent turn or the cockpit load.
- OFFLINE BEHAVIOUR: the read tools work offline as long as the desk DB has data (they read SQLite, not the network), which is good. But live_signals/quotes reflect the last fetch only; there is no live price when offline. Routines that fire a 'pre-open brief' on a schedule must tolerate offline/stale (already the fail-open requirement). The Google-backed planner features (calendar scheduling) genuinely need network; the local task/project/goal/habit stores are fully offline — keep that split clean.

## Safety must-haves
- PAPER-ONLY, ENFORCED STRUCTURALLY: only ever expose engine_bridge functions (which are paper-only by construction via PaperExecutionService). NEVER import or wire any desk live/real-trading module. The subprocess shim must whitelist EXACTLY the verified engine_bridge function names (get_quote, get_ltp, get_movers, search_symbols, portfolio_view, record_nav_snapshot, nav_history, trades, open_orders, order_history, preview_order, same_day_block, circuit_block, submit_order, cancel_order, cancel_all_orders, live_signals) — no arbitrary attribute/eval dispatch, so the agent can never reach a non-paper code path.
- EXACTLY ONE write tool gated: paper_submit_order MUST be requires_approval=True so it always parks an ApprovalCard via the existing SqliteCheckpointStore; cancel tools (if built) also gated. The ApprovalCard MUST render preview_order output (fees from TransactionCostModel, PreTradeRiskService severity/reasons, same_day_block/circuit_block result) so the user approves with full cost/risk context — never a bare 'approve?'.
- ROUTINES NEVER AUTO-TRADE: keep the existing rule that routine-fired gated tools PARK as kind='approval' inbox items (already handled correctly in routines.py). The morning brief / signal routines must use ONLY read money tools + add_task + inbox — they propose, never execute. The agent.yaml routine prompts must say 'no submitting orders, no calendar writes' (mirror the existing briefing prompt's no-mail/no-events guard).
- GROUNDING / NO FABRICATION: the persona must require money answers be grounded in actual portfolio_view/live_signals/nav_history output — never invent prices, positions, or P&L — same anti-hallucination discipline the library answers enforce. Stale data must be labeled with an as-of timestamp, not presented as live.
- SECRETS / DESK PATH: the desk path, NEPSE_DB_FILE, and account_id go in config/env (config.py), not hardcoded; the PII input guardrail already redacts secrets and memory is local-only. Don't log raw order args or account internals into the inbox/activity rows without the existing redact_sensitive_args pass. No desk credentials needed for paper reads (it's local SQLite) — keep it that way; never add a path that would require live broker creds.