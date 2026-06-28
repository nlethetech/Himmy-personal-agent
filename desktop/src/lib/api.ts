// Tiny client for Himmy's local backend (the himmy agent + connectors).
const PORT = (window as any).himmy?.backendPort ?? "8131";
const BASE = `http://127.0.0.1:${PORT}`;
// Per-launch shared secret the Electron main process injects (absent in a plain browser).
// Sent on every request so the backend's guard on the sensitive provider/key endpoints
// recognises us as the real app (and a stray web page, lacking it, is rejected).
const APP_TOKEN: string = (window as any).himmy?.appToken ?? "";

// Headers every request carries: JSON content-type (callers may omit on GET/DELETE) plus
// the app token when present.
function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(extra ?? {}) };
  if (APP_TOKEN) h["X-Himmy-Token"] = APP_TOKEN;
  return h;
}

export type Health = {
  ok: boolean;
  provider: string;
  model: string | null;
};
export type ModelOption = { id: string; label: string; cost: string; base_url?: string | null };
export type ModelProvider = {
  id: string; label: string; available: boolean; status: string;
  tools: boolean; free: boolean; models: ModelOption[];
};

export type AskResult = { ok: boolean; reply: string; tools: string[] };

// Deep research: the multi-step orchestration result (plan + cited synthesis + sources).
export type ResearchResult = {
  ok: boolean;
  brief: string;
  sources: string[];
  steps: string[];
};

async function jget<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

async function jpost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

async function jdelete<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: "DELETE", headers: authHeaders() });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

async function jput<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PUT",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

async function jpatch<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PATCH",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

export type Paper = {
  id: string;
  type: string;
  title: string;
  authors: string[];
  year: string;
  venue: string;
  doi: string;
  url: string;
  abstract: string;
  tags: string[];
  has_pdf: boolean;
  notes?: string;
  collections?: string[];
};

export type Rect = { x: number; y: number; w: number; h: number };
export type Highlight = {
  id: string;
  item_id: string;
  page: number;
  color: string;
  text: string;
  note: string;
  rects: Rect[];
};
export type Collection = { id: string; name: string; count: number };

export type NewsArticle = {
  title: string; url: string; source: string; image: string;
  snippet: string; ago: string; ts: number; topic?: string;
  // "For You" only: a short, human reason this story was surfaced ("Because you follow X").
  reason?: string;
};
// The /news/feed envelope. `fetched_at` is an ISO timestamp the UI renders as "updated Xm ago".
// It can be `null` for a legacy cache entry written before the `iso` field existed (the backend
// always emits a string on fresh writes), so the type allows null and the UI treats it as absent.
export type NewsFeed = {
  ok: boolean; category: string; items: NewsArticle[];
  fetched_at?: string | null; needs_interests?: boolean;
};
export type SavedArticle = {
  id: string; title: string; source: string; url: string; image: string;
  author?: string; published?: string; snippet: string; folder: string;
  saved_at?: number; text?: string; paragraphs?: string[];
};
export type ArticleContent = {
  ok: boolean; url: string; title: string; author?: string; date?: string;
  source?: string; image?: string; paragraphs: string[]; message?: string;
};
export type NewsFolder = { name: string; count: number };
export type NewsHighlight = { id: string; text: string; color: string; note: string; created: number };
export type Subtask = { text: string; done: boolean };
export type Task = {
  id: string;
  title: string;
  done: boolean;
  due: string | null;
  priority: number;   // 0 none · 1 low · 2 medium · 3 high
  created_at: string;
  // richer fields from the sidecar store (all optional)
  notes?: string;
  subtasks?: Subtask[];
  recur?: string;     // '' | daily | weekly | monthly
  paper_id?: string;
  paper_title?: string;
  scheduled_start?: string;  // ISO local wall-clock — the task's time-block
  scheduled_end?: string;
  event_id?: string;         // the calendar event this task is time-blocked into
};
export type TaskExtras = Partial<{
  notes: string; subtasks: Subtask[]; recur: string; paper_id: string; paper_title: string;
  scheduled_start: string; scheduled_end: string; event_id: string;
}>;
// "Plan my week" — the assistant's drafted, reviewable time-blocks.
export type PlanBlock = {
  task_id: string;   // "" when the block isn't tied to a task
  title: string;
  day: string;       // YYYY-MM-DD
  start: string;     // HH:MM (local wall-clock)
  end: string;       // HH:MM
  reason: string;
};
export type PlanResult = {
  ok: boolean; blocks: PlanBlock[]; message?: string;
};
export type RecPaper = {
  title: string; abstract: string; authors: string[]; year: string;
  venue: string; arxiv: string; doi: string; url: string; why?: string;
  citations?: number; source?: string; tldr?: string; concepts?: string[];
};
export type RecThread = { label: string; count: number; papers: RecPaper[] };
export type RecResult = {
  ok: boolean; papers: RecPaper[]; threads?: RecThread[]; hero?: RecPaper | null;
  stale?: boolean; cached?: boolean;
};

// The "Do" concierge — smart Nepal picks across food / shopping / flights, each with a deep-link.
export type DoPick = {
  key: string; title: string; subtitle?: string; meta?: string; why?: string;
  link?: string; rating?: number; open_now?: boolean; image?: string; ai?: boolean;
  discount?: string; was?: string; tag?: string; date?: string; vendor_id?: string;
  // Food dish search: the restaurant's current offer banner ("Combo from Rs 295"), if any.
  promo?: string;
};
// A Foodmandu vendor's offer banner surfaced alongside dish-search results.
export type DoPromo = { restaurant: string; promo: string; order_link: string; rating?: number };
// A smart, personalised search suggestion (built from the user's tastes + saved budget).
export type DoSuggestion = { label: string; query: string; kind: string; max_price?: number | null };
export type DoBoard = {
  ok: boolean; headline: string; ai?: boolean; stale?: boolean; generated_at?: string;
  food: DoPick[]; deals: DoPick[]; foryou: DoPick[]; flights: DoPick[];
};
// A restaurant's menu + the dishes recommended for the user.
export type DoMenuItem = {
  id: number; name: string; price: number; was?: number | null; desc?: string;
  image?: string; popular?: boolean; tag?: string; recommended?: boolean; category?: string;
};
export type DoMenuCategory = { category: string; items: DoMenuItem[] };
export type DoRestaurant = {
  ok: boolean; vendor_id: string; restaurant: string; order_link: string;
  item_count: number; categories: DoMenuCategory[]; recommended: DoMenuItem[]; message?: string;
};
// The tray — a Himmy-side cart, grouped by place.
export type DoCartItem = { key: string; name: string; price: number; qty: number; image?: string; link?: string };
export type DoCartGroup = { place: string; source: string; checkout_link: string; items: DoCartItem[]; subtotal: number };
export type DoCartView = { ok?: boolean; groups: DoCartGroup[]; total: number; count: number };
export type DoCartAdd = {
  key: string; name: string; price: number; qty?: number; source?: string;
  place?: string; image?: string; link?: string; checkout_link?: string;
};
// Permissions — what Himmy is allowed to do, per connection.
export type PermLevel = { value: string; label: string };
export type PermSurface = {
  key: string; label: string; service: string; desc: string;
  levels: PermLevel[]; level: string; requires?: string | null;
  granted_tools: string[]; connected?: boolean; account?: string | null;
};
export type PermsCatalog = { ok: boolean; surfaces: PermSurface[]; levels: Record<string, string> };

// Activity log — a plain-English record of what Himmy did.
export type ActivityItem = {
  ts: number; tool: string; surface: string; title: string; detail: string; status: string;
};
// Telegram bridge status.
export type TelegramStatus = {
  ok: boolean; configured: boolean; linked: boolean; owner_chat_id?: number | null;
  username?: string | null; running: boolean; message?: string;
};

// Live flight tickets (Buddha Air) for a route + date.
export type DoFlight = { flight: string; depart: string; arrive: string; from: string; to: string; fare_npr: number; class: string };
export type DoFlights = {
  ok: boolean; fares_available?: boolean; from: string; to: string; date: string;
  currency?: string; flights: DoFlight[]; cheapest?: DoFlight | null; booking_link?: string; message?: string;
  // Round-trip extras (only present when a return date was requested; one-way responses omit these).
  round_trip?: boolean;
  return_date?: string | null;          // "DD-Mon-YYYY" of the return leg
  return_flights?: DoFlight[];          // cheapest-first inbound legs
  return_cheapest?: DoFlight | null;
  round_trip_total_npr?: number | null; // cheapest outbound + cheapest return
};
// Bus tickets (bussewa) — live departures + fares + seats for a route, with a booking deep-link.
export type DoBus = {
  operator: string; bus_type?: string; route?: string; depart: string; arrive?: string;
  journey_hours?: number; fare_npr: number; min_bargain_npr?: number | null; seats_available?: number;
  amenities?: string[]; rating?: number; review_count?: number; trip_id?: string;
};
export type DoBusVia = { hub: string; for: string; note?: string };
export type DoBuses = {
  ok: boolean; trips_available?: boolean; from: string; to: string; via?: DoBusVia | null;
  date_bs?: string; date_ad?: string; currency?: string; count?: number;
  buses: DoBus[]; cheapest?: DoBus | null; booking_link?: string; message?: string;
};
// A trip roadmap — day-by-day places/activities for a destination, with budget, hotels & eat.
export type DoTripItem = { name: string; category?: string; desc?: string; tip?: string };
export type DoTripDay = { day: number; title: string; items: DoTripItem[] };
export type DoTripFlight = { from: string; to: string; cheapest?: DoFlight | null; booking_link?: string };
export type DoTripBus = { from: string; to: string; cheapest?: DoBus | null; count?: number; via?: DoBusVia | null; booking_link?: string };
export type DoTripBudgetRow = { label: string; min: number; max: number; note?: string };
export type DoTripBudget = {
  currency?: string; per_person?: boolean; total_min?: number; total_max?: number; breakdown?: DoTripBudgetRow[];
};
export type DoTripHotel = { name: string; type?: string; area?: string; why?: string; book_link?: string };
export type DoTripEat = { name: string; cuisine?: string; why?: string };
// Deterministic flight-vs-bus comparison, derived from getting_there + by_bus (no extra network/model
// calls). Only present when BOTH a flight and a bus exist — otherwise the field is null/absent.
export type DoTripTransportOption = {
  mode: "flight" | "bus";
  label: string;            // "Flight (Buddha Air)" | "Bus (bussewa)"
  fare_npr: number;
  duration_label: string;   // "~50 min" | "8h"
  // Flight door-to-door is an ESTIMATE (air time + ~3h airport buffer); bus journey_hours is real.
  duration_is_estimate: boolean;
  depart: string | null;
  book_link: string | null;
};
export type DoTripTransportVerdict = {
  winner: "flight" | "bus";
  reason: string;
  fare_delta_npr: number;
  time_note: string;
};
export type DoTripTransportCompare = {
  options: DoTripTransportOption[];
  verdict: DoTripTransportVerdict;
  disclaimer: string;       // "Flight time is door-to-door estimate incl. airport buffer."
};
// Weather for a destination — a current snapshot + per-day forecast (Open-Meteo, keyless). When the
// requested dates fall beyond the ~16-day forecast horizon, `in_forecast_window` is false and the UI
// leads with the seasonal `season` line instead of fake daily numbers.
export type DoWeatherCurrent = {
  temp_c: number; code: number; label: string; emoji: string; humidity: number; wind_kmh: number;
};
export type DoWeatherDay = {
  date: string;        // YYYY-MM-DD
  code: number; label: string; emoji: string;
  t_max: number; t_min: number; rain_pct: number; rain_mm: number;
};
export type DoWeather = {
  ok: boolean;
  current: DoWeatherCurrent | null;
  daily: DoWeatherDay[];
  season: string;             // Nepal seasonal pattern, e.g. "Monsoon (Jun-Sep): afternoon showers likely"
  summary: string;            // one honest line (real forecast if in-window, else seasonal)
  in_forecast_window: boolean;
};
// --- Markets: NEPSE price / NRB forex / Kathmandu air quality -------------------------------
// Live, host-pinned, keyless reads (Merolagani / Nepal Rastra Bank / Open-Meteo). Each degrades
// to `{ ok: false, ... }` rather than 500ing, so the UI cards must tolerate the failure shape.

// One daily OHLCV bar (already corp-action adjusted; NEVER re-adjusted). `v` is share volume.
export type DoNepseBar = { date: string; o: number; h: number; l: number; c: number; v: number };
// Latest NEPSE price + a recent OHLCV tail for a symbol (Merolagani, NPR). On failure the fields
// below `ok`/`symbol`/`message` are absent — the card narrows on `ok` before reading them.
export type DoNepse = {
  ok: boolean;
  symbol: string;
  price?: number;
  prev_close?: number | null;
  change?: number | null;
  change_pct?: number | null;
  currency?: string;                 // "NPR"
  ohlcv?: DoNepseBar[];              // last ~7 daily bars, oldest-first
  as_of?: string;                    // YYYY-MM-DD (AD) of the latest bar
  date_bs?: string;                  // Bikram Sambat YYYY-MM-DD of `as_of`
  source?: string;                   // "Merolagani"
  message?: string;                  // present on `ok: false` (bad symbol / upstream down)
};
// One NRB foreign-exchange rate against NPR, quoted per `unit` units of the foreign currency.
export type DoForexRate = { iso3: string; name: string; unit: number; buy: number; sell: number };
// Official Nepal Rastra Bank forex sheet (mid-market) for the latest published date.
export type DoForex = {
  ok: boolean;
  date?: string | null;              // YYYY-MM-DD (AD) of the published sheet
  date_bs?: string | null;           // Bikram Sambat YYYY-MM-DD of `date`
  base?: string;                     // "NPR"
  rates?: DoForexRate[];
  caption?: string;                  // "NRB official mid-market; per <unit> units" (not `note` — that's a PII-redacted key)
  message?: string;                  // present on `ok: false`
};
// Current air quality at a point (Open-Meteo, US AQI). Always well-formed; on failure `ok` is
// false, `us_aqi` is null and `category` is "Unknown" — the chip stays renderable either way.
export type DoAqi = {
  ok: boolean;
  us_aqi: number | null;
  category: string;                  // Good / Moderate / … / Hazardous / Unknown
  pm2_5: number | null;
  pm10: number | null;
  advice: string;
};

export type DoTrip = {
  ok: boolean; destination: string; days: number; style?: string; summary?: string;
  date?: string | null; return_date?: string | null;   // depart / return, ISO YYYY-MM-DD
  getting_there?: DoTripFlight | null; by_bus?: DoTripBus | null; budget?: DoTripBudget; hotels?: DoTripHotel[]; eat?: DoTripEat[];
  // Only present (non-null) when both a flight and a bus exist for the route.
  transport_compare?: DoTripTransportCompare | null;
  // Forecast for the destination over the trip window (graceful: absent when unavailable).
  weather?: DoWeather | null;
  itinerary: DoTripDay[]; tips?: string[]; message?: string;
};

// "What Himmy knows about you" — a user-authored layer + a layer Himmy learns from activity.
export type ProfileLayer = {
  about: string; voice: string; projects: string[]; people: string[]; topics: string[]; preferences: string[];
  details: Record<string, string>;
};
export type UserProfile = { user: ProfileLayer; learned: ProfileLayer; learned_at: number };

// Gated vault auto-fill: candidate facts Himmy inferred (home airport / budget / cuisines / …) but
// never auto-writes. Each must be confirmed by the user before it lands in profile.user.details.
// `confidence` reflects corroboration — only facts seen in >=2 signals are offered as 'med'/'high'.
export type ProfileSuggestion = {
  key: string;
  value: string;
  source: string;
  confidence: "low" | "med" | "high";
};

// Attachments — a file the user handed Himmy (in chat or via Telegram). Himmy extracts the text
// (framework readers for docs; the media connector for images/voice notes), keeps it, and indexes
// it into the same RAG as the library so ask_papers can find it later.
// `text` (capped) is returned only by the upload call, for grounding the immediately-next turn.
export type AttachmentResult = {
  id: string; name: string; kind: string; mime: string;
  size: number; chars: number; preview: string; text: string; empty: boolean;
};
export type AttachmentItem = {
  id: string; name: string; kind: string; mime: string; ext: string;
  size: number; chars: number; preview: string; source: string; session_id: string; added_at: number;
};
// Himmy's personality — how it talks to you (a tone preset + an optional free-text note).
export type AssistantStyleOpt = { id: string; label: string; blurb: string };
export type AssistantConfig = { style: string; note: string };

// Routines — saved automations that run on a schedule (himmy's Schedule model + cron/tz math,
// fired through the same agent as /ask; results land in notifications below).
export type RoutineSchedule = {
  kind: "daily" | "every" | "cron" | "at";
  at?: string;            // daily: "HH:MM"
  hours?: number;         // every: 1..168
  expr?: string;          // cron: "30 6 * * 1-5"
  at_datetime?: string;   // at: ISO instant (one-shot)
  timezone?: string;      // IANA, overrides HIMMY_TZ for wall-clock kinds
  missed?: string;
};
export type Routine = {
  id: string; name: string; prompt: string;
  schedule: RoutineSchedule; schedule_desc: string; enabled: boolean;
  last_status: string | null; last_run_at: string | null;
  last_preview: string; last_error: string | null;
  next_fire_at: string | null; created_at: string;
};
// One item in the notifications inbox — a routine result, an error, or a "needs approval" park.
export type NotificationItem = {
  id: string; routine_id: string | null; routine_name: string;
  kind: "result" | "approval" | "error";
  title: string; body: string; status: string;
  checkpoint_id: string | null; created_at: string; read: boolean;
};

// Google (read-only Mail + Calendar over the connected account).
export type GoogleStatus = {
  ok: boolean;
  configured: boolean;   // a Google OAuth client_id/secret is stored
  connected: boolean;    // an account is linked (we hold a refresh token)
  email: string | null;
  writable: boolean;     // the secrets backend can persist tokens
  message?: string;
};
export type MailMessage = {
  id: string; from: string; subject: string; snippet: string; date: string;
  // Derived server-side from Gmail's labels + the user's sender rules.
  category: "focused" | "promotions" | "social" | "updates" | "forums";
  unread: boolean;        // "UNREAD" label present
  important: boolean;     // "IMPORTANT" label present
  starred: boolean;       // "STARRED" label present
  vip: boolean;           // sender is on the user's VIP list
  automated: boolean;     // looks machine-sent (noreply/mailer-daemon/…)
};
export type MailFull = {
  id: string; from: string; to: string; subject: string; date: string; body: string;
};
export type CalendarEvent = {
  id: string | null; summary: string; start: string; end: string;
  location: string | null; html_link: string | null;
  recurring_event_id?: string | null;
};

// Cmd-K persistent conversations (the assistant's history sidebar).
export type ChatSession = {
  session_id: string;
  updated_at: string;
  message_count: number;
  title: string;
};
export type ChatMessage = { role: "user" | "assistant"; content: string };

// A tool call Himmy proposed that's waiting on the user's approval before it runs.
export type Pending = { tool_call_id?: string; tool_name: string; args: Record<string, any> };

// A completed tool call carried through to the renderer so the palette can draw rich connector
// cards. Both `args` and `result` are SERVER-REDACTED + size-capped: mail/calendar bodies, email
// addresses, tokens/secrets never reach the renderer. `result` stays loosely typed because each
// connector returns a different shape (a flight list, a weather snapshot, …); the App narrows it
// per `tool_name`.
export type ToolResult = {
  tool_name: string;
  args: Record<string, any>;
  result: unknown;
};

// A finished turn. When `awaiting_approval`, the run is PAUSED on a gated tool — approve/cancel
// via api.resume(checkpoint_id, …) to continue it. `tool_results` carries the typed, redacted
// outputs of the tools that ran (in call order); `tools` keeps the bare names for back-compat.
export type TurnResult = AskResult & {
  awaiting_approval?: boolean; checkpoint_id?: string; pending?: Pending[]; session_id?: string;
  tool_results?: ToolResult[];
};

// A streamed turn: token deltas + live tool-trace labels, then a terminal `done`. `onToken` fires
// per delta; `tool` carries a doxing-safe human label ("Looked up flights") with NO arg values.
export type StreamEvent =
  | { type: "token"; text: string }
  | { type: "tool"; label: string }
  | { type: "approval"; checkpoint_id: string; pending: Pending[] }
  | { type: "done"; reply: string; tools: string[]; session_id: string; tool_results?: ToolResult[]; awaiting_approval?: boolean; checkpoint_id?: string }
  | { type: "error"; message: string };

// Usage — token + cost accounting from himmy's metrics registry (priced via the LiteLLM table).
export type UsageTotals = {
  tokens_in: number; tokens_out: number; tokens_total: number; cost: number; calls: number;
};
export type Usage = {
  ok: boolean;
  model: string | null;
  session: UsageTotals;    // since the backend started
  lifetime: UsageTotals;   // persisted across restarts (approximate)
};

export type ReadingStats = {
  ok: boolean;
  today_seconds: number;
  week_seconds: number;
  total_seconds: number;
};

// In-app AI provider setup — lets a non-coder pick a provider, paste their key, and confirm it
// works, all without touching .env. A key set here is written through himmy's writable secrets
// layer (keychain on macOS, encrypted file elsewhere — the same path the Google sign-in uses) and
// is read back automatically by the inference provider on the next message. SECURITY: the backend
// NEVER returns a stored key value — only the `configured` boolean — so neither do these types.
export type ProviderInfo = {
  id: string;            // "openrouter" | "openai" | "anthropic" | "openai-compatible" | "ollama"
  label: string;        // human name, e.g. "OpenRouter", "Local (Ollama)"
  needs_key: boolean;   // false only for ollama (local, keyless)
  configured: boolean;  // a usable key is stored (or, for ollama, the local server is reachable)
  recommended?: boolean; // OpenRouter is badged "Recommended"
  key_url?: string;     // where the user gets a key ("" for custom/ollama)
  blurb?: string;       // one friendly line shown on the provider card
  default_model?: string | null; // a sensible starter model id per provider (null for custom/ollama)
};
// GET /provider/keys — the 5 providers (booleans only, never a key value) plus a top-level
// `ready` so the frontend can decide whether to show onboarding from a single fetch.
export type ProviderKeysResult = {
  ok: boolean;
  ready: boolean;
  providers: ProviderInfo[];
};
// POST /provider/key + DELETE /provider/key/{provider} — only booleans + a friendly error.
export type ProviderKeyResult = {
  ok: boolean;
  provider: string;
  configured: boolean;
  error?: string;       // present on ok:false (short, human, key-free — e.g. "Paste your key first.")
};
// GET /provider/status — is the app able to run inference right now?
export type ProviderStatus = { ok: boolean; ready: boolean };
// POST /provider/test — switches to provider/model (if supplied) then runs one tiny "ping" through
// the SAME runtime the app uses. On success: latency. On failure: a short, human `error`
// ("That key was rejected — check it and try again." / "Add your key first.").
export type ProviderTestResult = {
  ok: boolean;
  provider: string;
  model: string | null;
  latency_ms?: number;  // present on ok:true
  error?: string;       // present on ok:false
};

export const api = {
  health: () => jget<Health>("/health"),
  // Token + cost usage — what Himmy has used (this session + an all-time tally).
  usage: () => jget<Usage>("/usage"),
  ask: (message: string, history?: string[], context?: string, session_id?: string) =>
    jpost<TurnResult>("/ask", { message, history, context, session_id }),
  // Approve (execute) or cancel (reject) the gated tool that paused a run, and continue it.
  resume: (checkpoint_id: string, approved: boolean, session_id?: string) =>
    jpost<TurnResult>("/ask/resume", { checkpoint_id, approved, session_id }),
  // SSE streaming of one turn. Calls onToken per delta and onTrace per live tool-label; resolves
  // with the final answer (or an awaiting-approval result). Falls back to /ask automatically when
  // the stream can't be opened. The returned object exposes a `done` promise plus an `abort()`
  // handle that cancels the stream (and, server-side, the background agent task) — used by the
  // Stop button. An external `signal` may also be passed to drive the abort from a parent.
  askStream: (
    message: string,
    opts: {
      sessionId?: string;
      context?: string;
      onToken: (t: string) => void;
      onTrace?: (label: string) => void;
      signal?: AbortSignal;
    },
  ): { done: Promise<TurnResult>; abort: () => void } => {
    // Own an AbortController so callers always get a working abort(), even when they didn't pass a
    // signal. If they did pass one, chain it so either source aborts the underlying fetch.
    const controller = new AbortController();
    if (opts.signal) {
      if (opts.signal.aborted) controller.abort();
      else opts.signal.addEventListener("abort", () => controller.abort(), { once: true });
    }
    const done = (async (): Promise<TurnResult> => {
      let res: Response;
      try {
        res = await fetch(`${BASE}/ask/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message, session_id: opts.sessionId, context: opts.context }),
          signal: controller.signal,
        });
      } catch (e) {
        // An abort before the response opened: surface a partial (empty) turn, don't fall back.
        if (controller.signal.aborted) return { ok: true, reply: "", tools: [] };
        throw e;
      }
      if (!res.ok || !res.body) {
        // Stream unavailable — fall back to the buffered endpoint (which also carries approvals).
        const r = await api.ask(message, undefined, opts.context, opts.sessionId);
        if (!r.awaiting_approval) opts.onToken(r.reply);
        return r;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let final: TurnResult = { ok: true, reply: "", tools: [] };
      try {
        while (true) {
          const { value, done: streamDone } = await reader.read();
          if (streamDone) break;
          buf += decoder.decode(value, { stream: true });
          const frames = buf.split("\n\n");
          buf = frames.pop() ?? ""; // keep the trailing partial frame
          for (const frame of frames) {
            const line = frame.split("\n").find((l) => l.startsWith("data:"));
            if (!line) continue;
            let ev: StreamEvent;
            try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
            if (ev.type === "token") opts.onToken(ev.text);
            else if (ev.type === "tool") opts.onTrace?.(ev.label);
            else if (ev.type === "approval")
              final = { ...final, awaiting_approval: true, checkpoint_id: ev.checkpoint_id, pending: ev.pending };
            else if (ev.type === "done")
              final = { ...final, ok: true, reply: ev.reply, tools: ev.tools, session_id: ev.session_id,
                        tool_results: ev.tool_results ?? final.tool_results,
                        awaiting_approval: ev.awaiting_approval ?? final.awaiting_approval,
                        checkpoint_id: ev.checkpoint_id ?? final.checkpoint_id };
            else if (ev.type === "error")
              final = { ...final, ok: false, reply: `Error: ${ev.message}`, tools: [] };
          }
        }
      } catch (e) {
        // Aborted mid-stream (Stop) — return whatever partial reply we accumulated.
        if (controller.signal.aborted) return final;
        throw e;
      }
      return final;
    })();
    return { done, abort: () => controller.abort() };
  },
  sessions: {
    list: () => jget<{ ok: boolean; sessions: ChatSession[] }>("/sessions"),
    get: (id: string) =>
      jget<{ ok: boolean; session_id: string; messages: ChatMessage[] }>(`/sessions/${id}`),
    remove: (id: string) => jdelete<{ ok: boolean }>(`/sessions/${id}`),
  },
  // Deep research — slower, multi-step (plan → parallel library+web → synthesis → reflect).
  // Long-running (60–150s); never call automatically, only from the explicit button.
  research: (question: string) => jpost<ResearchResult>("/research", { question }),
  index: (force = false) => jpost<any>("/index", { force }),
  library: {
    list: (q = "", collection = "") => {
      const p = new URLSearchParams();
      if (q) p.set("q", q);
      if (collection) p.set("collection", collection);
      const qs = p.toString();
      return jget<{ ok: boolean; count: number; items: Paper[] }>(`/library${qs ? `?${qs}` : ""}`);
    },
    get: (id: string) => jget<{ ok: boolean; item: Paper }>(`/library/${id}`),
    pdfUrl: (id: string) => `${BASE}/library/${id}/pdf`,
    addDoi: (identifier: string) =>
      jpost<{ ok: boolean; item?: Paper; message?: string; duplicate?: boolean }>(
        "/library/doi", { identifier }
      ),
    addFiles: (paths: string[]) =>
      jpost<{ ok: boolean; added: number; items: Paper[] }>("/library/files", { paths }),
    save: (payload: Record<string, unknown>) =>
      jpost<{ ok: boolean; item?: Paper; message?: string }>("/save", payload),
    update: (id: string, fields: Record<string, unknown>) =>
      jput<{ ok: boolean; item: Paper }>(`/library/${id}`, { fields }),
    enrich: (id: string) =>
      jpost<{ ok: boolean; item?: Paper; message?: string; source?: string }>(`/library/${id}/enrich`, {}),
    fetchPdf: (id: string) =>
      jpost<{ ok: boolean; item?: Paper; message?: string; already?: boolean }>(`/library/${id}/fetch-pdf`, {}),
    setNote: (id: string, note: string) => jput<{ ok: boolean }>(`/library/${id}/notes`, { note }),
    remove: (id: string) => jdelete<{ ok: boolean }>(`/library/${id}`),
  },
  highlights: {
    list: (id: string) => jget<{ ok: boolean; highlights: Highlight[] }>(`/library/${id}/highlights`),
    add: (id: string, h: Omit<Highlight, "id" | "item_id">) =>
      jpost<{ ok: boolean; highlight: Highlight }>(`/library/${id}/highlights`, h),
    update: (hid: string, patch: { note?: string; color?: string }) =>
      jput<{ ok: boolean; highlight: Highlight }>(`/highlights/${hid}`, patch),
    remove: (hid: string) => jdelete<{ ok: boolean }>(`/highlights/${hid}`),
    exportMarkdown: (id: string) =>
      jpost<{ ok: boolean; path?: string; message?: string }>(
        `/library/${id}/highlights/export`, {}),
  },
  reading: {
    // The Reader posts engaged-seconds heartbeats; the backend clamps + accumulates them.
    heartbeat: (sessionId: string, itemId: string, seconds: number) =>
      jpost<{ ok: boolean; item_seconds?: number }>("/reading/heartbeat", {
        session_id: sessionId, item_id: itemId, seconds,
      }),
    // Final flush when the Reader closes / the window goes away — must survive teardown, so we
    // prefer sendBeacon (fire-and-forget, queued by the OS) and fall back to keepalive fetch.
    beacon: (sessionId: string, itemId: string, seconds: number) => {
      const payload = JSON.stringify({ session_id: sessionId, item_id: itemId, seconds });
      // keepalive fetch survives the Reader unmounting (like sendBeacon) but, unlike sendBeacon,
      // can omit credentials — required because the backend's CORS uses a wildcard origin, which
      // browsers reject for credentialed requests.
      try {
        fetch(`${BASE}/reading/heartbeat`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: payload, keepalive: true, credentials: "omit",
        });
      } catch { /* best effort — the periodic heartbeats already captured most of the time */ }
    },
    stats: () => jget<ReadingStats>("/reading/stats"),
    totals: () => jget<{ ok: boolean; totals: Record<string, number> }>("/reading/totals"),
    item: (id: string) => jget<{ ok: boolean; seconds: number; last_read: number | null }>(`/reading/item/${id}`),
    // Resume point: where you last left off in a paper, so reopening it lands on the right page.
    getPosition: (itemId: string) =>
      jget<{ ok: boolean; position: { page: number; frac: number; num_pages: number | null; updated_at: number } | null }>(
        `/reading/position/${itemId}`),
    setPosition: (itemId: string, page: number, frac: number, numPages: number | null) =>
      jpost<{ ok: boolean }>("/reading/position", { item_id: itemId, page, frac, num_pages: numPages }),
    // Final save on Reader teardown — keepalive so it survives the unmount / app close (mirrors beacon()).
    positionBeacon: (itemId: string, page: number, frac: number, numPages: number | null) => {
      const payload = JSON.stringify({ item_id: itemId, page, frac, num_pages: numPages });
      try {
        fetch(`${BASE}/reading/position`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: payload, keepalive: true, credentials: "omit",
        });
      } catch { /* best effort — the debounced saves already captured the recent position */ }
    },
  },
  profile: {
    get: () => jget<{ ok: boolean; profile: UserProfile }>("/profile"),
    saveUser: (sections: ProfileLayer) =>
      jput<{ ok: boolean; profile: UserProfile }>("/profile", sections),
    learn: () =>
      jpost<{ ok: boolean; profile: UserProfile; message?: string }>("/profile/learn", {}),
    // Pending vault facts Himmy inferred but won't auto-write (home airport / budget / cuisines / …).
    suggestions: () =>
      jget<{ ok: boolean; suggestions: ProfileSuggestion[] }>("/profile/suggestions"),
    // Confirm a subset of suggested keys — only these are written into profile.user.details.
    applySuggestions: (keys: string[]) =>
      jpost<{ ok: boolean; profile?: UserProfile; applied?: string[]; message?: string }>(
        "/profile/suggestions/apply", { keys }),
  },
  // Attachments — upload a file Himmy should read (multipart), list what it's read, forget one.
  attach: async (file: File, sessionId?: string): Promise<{ ok: boolean; attachment?: AttachmentResult; message?: string }> => {
    const fd = new FormData();
    fd.append("file", file);
    if (sessionId) fd.append("session_id", sessionId);
    // NB: no Content-Type header — the browser sets the multipart boundary itself.
    const res = await fetch(`${BASE}/attach`, { method: "POST", headers: authHeaders(), body: fd });
    if (!res.ok) throw new Error(`/attach → ${res.status}`);
    return res.json();
  },
  attachments: {
    list: () => jget<{ ok: boolean; attachments: AttachmentItem[] }>("/attachments"),
    remove: (id: string) => jdelete<{ ok: boolean }>(`/attachments/${id}`),
  },
  // Himmy's personality — how it talks to you (Settings → You → "How Himmy talks").
  assistant: {
    get: () => jget<{ ok: boolean; assistant: AssistantConfig; styles: AssistantStyleOpt[]; vision_available: boolean }>("/assistant"),
    set: (style: string, note: string) => jput<{ ok: boolean; assistant: AssistantConfig }>("/assistant", { style, note }),
  },
  collections: {
    list: () => jget<{ ok: boolean; collections: Collection[] }>("/collections"),
    create: (name: string) => jpost<{ ok: boolean; collection: Collection }>("/collections", { name }),
    rename: (cid: string, name: string) => jput<{ ok: boolean }>(`/collections/${cid}`, { name }),
    remove: (cid: string) => jdelete<{ ok: boolean }>(`/collections/${cid}`),
    addItem: (cid: string, id: string) => jpost<{ ok: boolean }>(`/collections/${cid}/items/${id}`, {}),
    removeItem: (cid: string, id: string) => jdelete<{ ok: boolean }>(`/collections/${cid}/items/${id}`),
  },
  tags: () => jget<{ ok: boolean; tags: { tag: string; count: number }[] }>("/tags"),
  backup: () => jpost<{ ok: boolean; path?: string; message?: string }>("/backup", {}),
  restore: (path: string) =>
    jpost<{ ok: boolean; restored?: number; message?: string }>("/restore", { path }),
  dataDir: () => jget<{ ok: boolean; path: string }>("/datadir"),
  models: {
    list: () => jget<{ ok: boolean; current: { provider: string; model: string | null }; providers: ModelProvider[] }>("/models"),
    set: (provider: string, model: string | null, base_url?: string | null) =>
      jput<{ ok: boolean; current?: { provider: string; model: string | null }; message?: string }>("/models", { provider, model, base_url: base_url ?? null }),
  },
  // In-app AI provider setup — pick a provider, paste a key, confirm it works. A key stored here
  // is written via himmy's writable secrets layer (keychain/file) and read back automatically by
  // the inference provider — no .env edit. NEVER sends back or echoes a stored key value.
  provider: {
    // The 5 providers (booleans only) + a top-level `ready` for the onboarding decision.
    keys: () => jget<ProviderKeysResult>("/provider/keys"),
    // Just the `ready` flag — cheap; the App fetches this on launch to decide whether to onboard.
    status: () => jget<ProviderStatus>("/provider/status"),
    // Store a pasted key for `provider`. Validated + persisted server-side; the value is never
    // logged or returned. Resolves with { ok, provider, configured } (or a friendly `error`).
    setKey: (provider: string, key: string) =>
      jpost<ProviderKeyResult>("/provider/key", { provider, key }),
    // Remove the stored key for `provider` (idempotent — absent is success).
    clearKey: (provider: string) =>
      jdelete<ProviderKeyResult>(`/provider/key/${encodeURIComponent(provider)}`),
    // Switch to provider/model (if supplied, reusing the /models path) then run one tiny "ping"
    // through the same runtime the app uses — the onboarding "Test & finish" confidence check.
    // On success: { ok:true, provider, model, latency_ms }; on failure: { ok:false, error }.
    test: (provider?: string, model?: string | null, base_url?: string | null) =>
      jpost<ProviderTestResult>("/provider/test", {
        provider: provider ?? null,
        model: model ?? null,
        base_url: base_url ?? null,
      }),
  },
  // Flat aliases (the names the onboarding/Settings UI calls). Same endpoints as `api.provider.*`.
  providerKeys: () => jget<ProviderKeysResult>("/provider/keys"),
  providerStatus: () => jget<ProviderStatus>("/provider/status"),
  setProviderKey: (provider: string, key: string) =>
    jpost<ProviderKeyResult>("/provider/key", { provider, key }),
  clearProviderKey: (provider: string) =>
    jdelete<ProviderKeyResult>(`/provider/key/${encodeURIComponent(provider)}`),
  testProvider: (provider?: string, model?: string | null, base_url?: string | null) =>
    jpost<ProviderTestResult>("/provider/test", {
      provider: provider ?? null,
      model: model ?? null,
      base_url: base_url ?? null,
    }),
  news: {
    interests: () => jget<{ ok: boolean; interests: string[] }>("/news/interests"),
    setInterests: (interests: string[]) =>
      jput<{ ok: boolean; interests: string[] }>("/news/interests", { interests }),
    categories: () => jget<{ ok: boolean; categories: string[] }>("/news/categories"),
    feed: (cat: string, force = false) =>
      jget<NewsFeed>(
        `/news/feed?cat=${encodeURIComponent(cat)}${force ? "&force=true" : ""}`
      ),
    recommendations: (force = false) =>
      jget<RecResult>(`/news/recommendations${force ? "?force=true" : ""}`),
    dismissRec: (p: { doi?: string; title?: string; concepts?: string[] }) =>
      jpost<{ ok: boolean }>("/recommendations/dismiss", {
        doi: p.doi || "", title: p.title || "", concepts: p.concepts || [],
      }),
    article: (url: string) =>
      jget<ArticleContent>(`/news/article?url=${encodeURIComponent(url)}`),
    saved: (folder = "", q = "") => {
      const p = new URLSearchParams();
      if (folder) p.set("folder", folder);
      if (q) p.set("q", q);
      const qs = p.toString();
      return jget<{ ok: boolean; items: SavedArticle[] }>(`/news/saved${qs ? `?${qs}` : ""}`);
    },
    savedFolders: () => jget<{ ok: boolean; total: number; folders: NewsFolder[] }>("/news/saved/folders"),
    savedUrls: () => jget<{ ok: boolean; urls: { id: string; url: string }[] }>("/news/saved/urls"),
    savedGet: (id: string) => jget<{ ok: boolean; item: SavedArticle }>(`/news/saved/${id}`),
    save: (payload: { url: string; title?: string; source?: string; image?: string; snippet?: string; folder?: string }) =>
      jpost<{ ok: boolean; id?: string; folder?: string; readable?: boolean; message?: string }>("/news/save", payload),
    move: (id: string, folder: string) => jput<{ ok: boolean; folder: string }>(`/news/saved/${id}`, { folder }),
    unsave: (id: string) => jdelete<{ ok: boolean }>(`/news/saved/${id}`),
    // per-article notes + text highlights (keyed by the article URL)
    annotations: (url: string) =>
      jget<{ ok: boolean; note: string; summary: string; highlights: NewsHighlight[] }>(`/news/annotations?url=${encodeURIComponent(url)}`),
    setNote: (url: string, note: string) => jput<{ ok: boolean }>("/news/notes", { url, note }),
    setSummary: (url: string, summary: string) => jput<{ ok: boolean }>("/news/summary", { url, summary }),
    addHighlight: (h: { url: string; text: string; color?: string; note?: string }) =>
      jpost<{ ok: boolean; highlight: NewsHighlight }>("/news/highlights", { color: "yellow", note: "", ...h }),
    updateHighlight: (hid: string, patch: { note?: string; color?: string }) =>
      jput<{ ok: boolean }>(`/news/highlights/${hid}`, patch),
    removeHighlight: (hid: string) => jdelete<{ ok: boolean }>(`/news/highlights/${hid}`),
  },

  // The "Do" concierge — flights / food / shopping picks. board() is cheap (served from a warm
  // cache; the one AI pass runs in the background), refresh() forces a regenerate.
  do: {
    board: (force = false) => jget<DoBoard>(`/do${force ? "?force=true" : ""}`),
    refresh: () => jpost<DoBoard>("/do/refresh", {}),
    feedback: (p: { kind: "up" | "down"; key: string; rail?: string; tags?: string[] }) =>
      jpost<{ ok: boolean }>("/do/feedback", { rail: "", tags: [], ...p }),
    restaurant: (opts: { id?: string; name?: string }) => {
      const p = new URLSearchParams();
      if (opts.id) p.set("id", opts.id);
      if (opts.name) p.set("name", opts.name);
      return jget<DoRestaurant>(`/do/restaurant?${p.toString()}`);
    },
    // Food search returns DISHES across restaurants (best-rated first); `maxPrice` is a budget cap,
    // `openOnly` limits to open places. Shop search returns Daraz products (params ignored there).
    search: (q: string, kind: "food" | "shop", maxPrice?: number | null, openOnly?: boolean) => {
      const p = new URLSearchParams({ q, kind });
      if (maxPrice) p.set("max_price", String(maxPrice));
      if (openOnly) p.set("open_only", "true");
      return jget<{ ok: boolean; kind: string; query: string; results: DoPick[]; promos?: DoPromo[] }>(
        `/do/search?${p.toString()}`);
    },
    // Smart, personalised search suggestions (the user's tastes + their saved food budget).
    suggestions: (kind: "food" | "shop") =>
      jget<{ ok: boolean; kind: string; budget: number | null; suggestions: DoSuggestion[] }>(
        `/do/suggestions?kind=${kind}`),
    // Pass returnDate (YYYY-MM-DD) to fetch a round-trip — the response then carries return_flights +
    // round_trip_total_npr. Omit it for a one-way search (the response stays backwards-compatible).
    flights: (from: string, to: string, date?: string, returnDate?: string) =>
      jget<DoFlights>(
        `/do/flights?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}` +
        `${date ? `&date=${date}` : ""}${returnDate ? `&return=${returnDate}` : ""}`),
    buses: (from: string, to: string, date?: string) =>
      jget<DoBuses>(`/do/buses?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}${date ? `&date=${date}` : ""}`),
    busCities: () => jget<{ ok: boolean; cities: string[] }>(`/do/bus-cities`),
    // Weather forecast for a point over a date window (Open-Meteo, keyless). Honest out-of-window:
    // when start/end fall beyond the ~16-day horizon the result leads with the seasonal pattern.
    weather: (lat: number, lon: number, start: string, end: string) =>
      jget<DoWeather>(
        `/do/weather?lat=${lat}&lon=${lon}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`),
    // date = depart date (YYYY-MM-DD); when supplied the trip carries a real weather forecast + uses it.
    trip: (dest: string, days = 2, style = "comfort", date?: string) =>
      jget<DoTrip>(
        `/do/trip?dest=${encodeURIComponent(dest)}&days=${days}&style=${style}${date ? `&date=${date}` : ""}`),
    // Export a trip as a SANITIZED shareable itinerary (markdown). The backend strips any
    // profile-derived phrasing, the user's name/email, and vault facts — it reads as a generic plan.
    tripExport: (dest: string, days = 2, style = "comfort") =>
      jget<{ ok: boolean; markdown: string; title: string }>(
        `/do/trip/export?dest=${encodeURIComponent(dest)}&days=${days}&style=${style}&fmt=md`),
    cart: {
      view: () => jget<DoCartView>("/do/cart"),
      add: (item: DoCartAdd) => jpost<DoCartView>("/do/cart/add", item),
      qty: (key: string, qty: number) => jpost<DoCartView>("/do/cart/qty", { key, qty }),
      remove: (key: string) => jpost<DoCartView>("/do/cart/remove", { key, qty: 0 }),
      clear: () => jpost<DoCartView>("/do/cart/clear", {}),
    },
  },

  permissions: {
    get: () => jget<PermsCatalog>("/permissions"),
    set: (levels: Record<string, string>) => jput<PermsCatalog>("/permissions", { levels }),
  },

  // Markets — live NEPSE price / NRB forex / Kathmandu air quality. Thin reads over the same
  // host-pinned, keyless connectors the chat tools use; each degrades to `{ ok: false, ... }`
  // server-side (never 500s), so callers narrow on `ok` before reading the rest.
  markets: {
    // Latest price + recent OHLCV for a NEPSE symbol (Merolagani, NPR). `symbol` is sanitised to
    // [A-Z0-9] server-side; `days` bounds the lookback (default 400, clamped 1..2000).
    nepse: (symbol: string, days?: number) =>
      jget<DoNepse>(
        `/nepse/price?symbol=${encodeURIComponent(symbol)}${days != null ? `&days=${days}` : ""}`),
    // Official NRB forex rates against NPR. `currencies` is an optional comma/space iso3 list
    // ("USD,INR") or "all"; omit it for the big liquid ones (USD/EUR/GBP/INR/AUD/CNY/JPY).
    forex: (currencies?: string) =>
      jget<DoForex>(`/forex${currencies ? `?currencies=${encodeURIComponent(currencies)}` : ""}`),
    // Current air quality at a point (Open-Meteo, US AQI). Defaults to Kathmandu server-side.
    aqi: (lat?: number, lon?: number) => {
      const p = new URLSearchParams();
      if (lat != null) p.set("lat", String(lat));
      if (lon != null) p.set("lon", String(lon));
      const qs = p.toString();
      return jget<DoAqi>(`/aqi${qs ? `?${qs}` : ""}`);
    },
  },

  activity: {
    get: (limit = 60) => jget<{ ok: boolean; items: ActivityItem[] }>(`/activity?limit=${limit}`),
    clear: () => jdelete<{ ok: boolean }>("/activity"),
  },

  // Telegram bridge — chat with Himmy from Telegram.
  telegram: {
    status: () => jget<TelegramStatus>("/telegram/status"),
    setToken: (token: string) => jput<TelegramStatus>("/telegram/config", { token }),
    unlink: () => jpost<TelegramStatus>("/telegram/unlink", {}),
    disconnect: () => jpost<TelegramStatus>("/telegram/disconnect", {}),
  },

  // The daily brief — Himmy's proactive "here's your day", shown on Today.
  brief: (force = false) =>
    jget<{ ok: boolean; text: string; generated_at?: string; stale?: boolean; generating?: boolean }>(
      `/brief${force ? "?force=true" : ""}`),
  tasks: {
    list: () => jget<{ ok: boolean; tasks: Task[]; open: number; total: number }>("/tasks"),
    add: (title: string, opts?: { due?: string | null; priority?: number }) =>
      jpost<{ ok: boolean; task: Task }>("/tasks", { title, ...(opts ?? {}) }),
    // Patch due / priority / done in place — only the supplied fields change.
    patch: (id: string, fields: { due?: string | null; priority?: number; done?: boolean }) =>
      jpatch<{ ok: boolean; task: Task }>(`/tasks/${id}`, fields),
    // Patch the richer sidecar fields (notes / subtasks / recurrence / paper link / time-block).
    setExtras: (id: string, fields: TaskExtras) =>
      jpatch<{ ok: boolean; task: Task }>(`/tasks/${id}/extras`, fields),
    complete: (id: string) => jpost<{ ok: boolean; spawned?: Task }>(`/tasks/${id}/complete`, {}),
    remove: (id: string) => jdelete<{ ok: boolean }>(`/tasks/${id}`),
  },
  // "Plan my week" — the assistant drafts a time-blocked schedule the user reviews + approves.
  planner: {
    suggest: () => jpost<PlanResult>("/planner/suggest", {}),
  },
  // Routines — saved automations that run on a schedule. CRUD + an immediate "run now".
  routines: {
    list: () => jget<{ ok: boolean; routines: Routine[] }>("/routines"),
    create: (name: string, prompt: string, schedule: RoutineSchedule, enabled = true) =>
      jpost<{ ok: boolean; routine: Routine }>("/routines", { name, prompt, schedule, enabled }),
    update: (
      id: string,
      patch: Partial<{ name: string; prompt: string; schedule: RoutineSchedule; enabled: boolean }>,
    ) => jput<{ ok: boolean; routine: Routine }>(`/routines/${id}`, patch),
    remove: (id: string) => jdelete<{ ok: boolean }>(`/routines/${id}`),
    runNow: (id: string) =>
      jpost<{ ok: boolean; status?: string; preview?: string; error?: string }>(
        `/routines/${id}/run-now`, {}),
  },
  // Notifications — the inbox of routine results / errors / approval parks.
  notifications: {
    list: (limit = 50, unreadOnly = false) =>
      jget<{ ok: boolean; notifications: NotificationItem[]; unread: number }>(
        `/notifications?limit=${limit}${unreadOnly ? "&unread_only=true" : ""}`),
    read: (id: string) => jpost<{ ok: boolean }>(`/notifications/${id}/read`, {}),
    readAll: () => jpost<{ ok: boolean; marked: number }>("/notifications/read-all", {}),
    remove: (id: string) => jdelete<{ ok: boolean }>(`/notifications/${id}`),
  },
  // Read-only Mail + Calendar over the connected Google account.
  google: {
    status: () => jget<GoogleStatus>("/google/status"),
    setClient: (client_id: string, client_secret: string) =>
      jpost<GoogleStatus>("/google/client", { client_id, client_secret }),
    authUrl: () =>
      jget<{ ok: boolean; url?: string; redirect_uri?: string; needs_setup?: boolean; message?: string }>(
        "/google/auth-url"
      ),
    exchange: (code: string, state?: string) =>
      jpost<GoogleStatus>("/google/exchange", { code, state }),
    disconnect: () => jpost<GoogleStatus>("/google/disconnect", {}),
  },
  mail: {
    inbox: (limit = 15, force = false) =>
      jget<{ ok: boolean; connected: boolean; needs_setup?: boolean; messages: MailMessage[];
             message?: string; cached?: boolean; stale?: boolean }>(
        `/mail/inbox?limit=${limit}${force ? "&force=true" : ""}`
      ),
    message: (id: string) =>
      jget<{ ok: boolean; connected: boolean; email?: MailFull; message?: string }>(
        `/mail/message/${id}`
      ),
    // Sender rules: mute a sender out of the inbox, or VIP one to keep it focused.
    rules: {
      list: () =>
        jget<{ ok: boolean; muted: string[]; vip: string[]; message?: string }>("/mail/rules"),
      set: (action: "mute" | "unmute" | "vip" | "unvip", sender: string) =>
        jpost<{ ok: boolean; muted?: string[]; vip?: string[]; message?: string }>(
          "/mail/rules",
          { action, sender }
        ),
    },
    // A read-only, model-written brief of focused/VIP mail (cached ~6h; `force` recomputes).
    digest: (force = false) =>
      jget<{ ok: boolean; summary?: string; at?: number; cached?: boolean; stale?: boolean; message?: string }>(
        `/mail/digest${force ? "?force=true" : ""}`
      ),
  },
  calendar: {
    events: (limit = 15) =>
      jget<{ ok: boolean; connected: boolean; needs_setup?: boolean; events: CalendarEvent[]; message?: string }>(
        `/calendar/events?limit=${limit}`
      ),
    range: (timeMin: string, timeMax: string) =>
      jget<{ ok: boolean; connected: boolean; needs_setup?: boolean; events: CalendarEvent[]; message?: string }>(
        `/calendar/range?time_min=${encodeURIComponent(timeMin)}&time_max=${encodeURIComponent(timeMax)}`
      ),
    create: (e: { summary: string; start: string; end: string; all_day?: boolean; location?: string | null }) =>
      jpost<{ ok: boolean; event?: CalendarEvent; message?: string }>("/calendar/events", e),
    update: (id: string, e: { summary?: string; start?: string; end?: string; all_day?: boolean; location?: string | null }) =>
      jput<{ ok: boolean; event?: CalendarEvent; message?: string }>(`/calendar/events/${id}`, e),
    remove: (id: string) => jdelete<{ ok: boolean; message?: string }>(`/calendar/events/${id}`),
  },
};
