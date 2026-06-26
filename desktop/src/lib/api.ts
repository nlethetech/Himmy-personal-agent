// Tiny client for Himmy's local backend (the himmy agent + connectors).
const PORT = (window as any).himmy?.backendPort ?? "8131";
const BASE = `http://127.0.0.1:${PORT}`;

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
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

async function jpost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

async function jdelete<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

async function jput<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

async function jpatch<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
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
  link?: string; rating?: number; open_now?: boolean;
  discount?: string; was?: string; tag?: string; date?: string;
};
export type DoBoard = {
  ok: boolean; headline: string; ai?: boolean; stale?: boolean; generated_at?: string;
  food: DoPick[]; deals: DoPick[]; flights: DoPick[];
};

// "What Himmy knows about you" — a user-authored layer + a layer Himmy learns from activity.
export type ProfileLayer = {
  about: string; projects: string[]; people: string[]; topics: string[]; preferences: string[];
  details: Record<string, string>;
};
export type UserProfile = { user: ProfileLayer; learned: ProfileLayer; learned_at: number };

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
// A finished turn. When `awaiting_approval`, the run is PAUSED on a gated tool — approve/cancel
// via api.resume(checkpoint_id, …) to continue it.
export type TurnResult = AskResult & {
  awaiting_approval?: boolean; checkpoint_id?: string; pending?: Pending[]; session_id?: string;
};

// A streamed turn: token deltas, then a terminal `done`. `onToken` fires per delta.
export type StreamEvent =
  | { type: "token"; text: string }
  | { type: "approval"; checkpoint_id: string; pending: Pending[] }
  | { type: "done"; reply: string; tools: string[]; session_id: string; awaiting_approval?: boolean; checkpoint_id?: string }
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

export const api = {
  health: () => jget<Health>("/health"),
  // Token + cost usage — what Himmy has used (this session + an all-time tally).
  usage: () => jget<Usage>("/usage"),
  ask: (message: string, history?: string[], context?: string, session_id?: string) =>
    jpost<TurnResult>("/ask", { message, history, context, session_id }),
  // Approve (execute) or cancel (reject) the gated tool that paused a run, and continue it.
  resume: (checkpoint_id: string, approved: boolean, session_id?: string) =>
    jpost<TurnResult>("/ask/resume", { checkpoint_id, approved, session_id }),
  // SSE streaming of one turn. Calls onToken per delta; resolves with the final answer (or an
  // awaiting-approval result). Falls back to /ask automatically when the stream can't be opened.
  askStream: async (
    message: string,
    opts: { sessionId?: string; context?: string; onToken: (t: string) => void },
  ): Promise<TurnResult> => {
    const res = await fetch(`${BASE}/ask/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: opts.sessionId, context: opts.context }),
    });
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
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const frames = buf.split("\n\n");
      buf = frames.pop() ?? ""; // keep the trailing partial frame
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        let ev: StreamEvent;
        try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
        if (ev.type === "token") opts.onToken(ev.text);
        else if (ev.type === "approval")
          final = { ...final, awaiting_approval: true, checkpoint_id: ev.checkpoint_id, pending: ev.pending };
        else if (ev.type === "done")
          final = { ...final, ok: true, reply: ev.reply, tools: ev.tools, session_id: ev.session_id,
                    awaiting_approval: ev.awaiting_approval ?? final.awaiting_approval,
                    checkpoint_id: ev.checkpoint_id ?? final.checkpoint_id };
        else if (ev.type === "error")
          final = { ...final, ok: false, reply: `Error: ${ev.message}`, tools: [] };
      }
    }
    return final;
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
  },
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
