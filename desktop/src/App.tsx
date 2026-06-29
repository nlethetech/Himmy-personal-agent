import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import {
  Sun, Sunrise, Moon, Newspaper, BookOpen, CheckSquare, Calendar, Mail,
  Search, Sparkles, Settings, ChevronDown, ArrowUp, Clock, ArrowUpRight,
  Plus, X, Trash2, Loader2, FileUp, Link, Folder, Library as LibraryIcon, Tag, Hash,
  Quote, Copy, Check, FileDown, RefreshCw, ExternalLink,
  Bookmark, BookmarkCheck, ArrowLeft, FolderPlus, Globe, Circle, CheckCircle2,
  MessageSquare, SquarePen, PanelLeft, PanelRight, Telescope, ListChecks, BookText, Link2,
  Inbox, MapPin, KeyRound, ShieldCheck, Minus, ChevronLeft, ChevronRight, Repeat,
  Coins, Zap, CalendarClock, Bell, Play, Flag,
  Star, BellOff, Users, TrendingUp, TrendingDown, Wind, Cpu, Sparkle,
  Info, StickyNote, Highlighter, Eye, EyeOff, BrainCircuit, Cpu as CpuIcon, ServerCog,
  ShoppingBag, Plane, Bus, UtensilsCrossed, ThumbsDown, ShoppingCart, Heart, ArrowRight, ConciergeBell,
  Route, Lightbulb, BedDouble, Wallet, Send, Armchair, ArrowRightLeft, Share2, Printer,
  CloudRain, Umbrella, Square,
  Paperclip, FileText, Mic, Image as ImageIcon, MessageCircle,
  Camera, Receipt, PieChart,
  type LucideIcon,
} from "lucide-react";
import {
  api, type Health, type Paper, type Collection,
  type NewsArticle, type SavedArticle, type ArticleContent, type NewsFolder, type NewsHighlight, type RecPaper,
  type Task, type ChatSession, type ResearchResult, type Pending,
  type GoogleStatus, type MailMessage, type MailFull, type CalendarEvent, type Usage, type UsageTotals,
  type TelegramStatus,
  type ModelProvider,
  type ProviderInfo, type ProviderKeysResult, type ProviderTestResult,
  type Routine, type RoutineSchedule, type NotificationItem, type ReadingStats,
  type RecThread, type TaskExtras, type Subtask,
  type UserProfile, type ProfileLayer,
  type DoBoard, type DoPick, type DoPromo, type DoSuggestion, type DoRestaurant, type DoMenuItem,
  type DoCartView, type DoCartAdd, type DoFlights, type DoFlight, type DoBuses, type DoBus,
  type PermsCatalog, type PermSurface, type ActivityItem,
  type DoTrip, type DoTripDay, type DoTripItem, type DoTripHotel, type DoTripEat,
  type DoTripTransportCompare,
  type DoWeather, type DoWeatherDay,
  type DoNepse, type DoNepseBar, type DoForex, type DoAqi,
  type ToolResult,
  type AttachmentResult, type AttachmentItem, type AssistantConfig, type AssistantStyleOpt,
  type Expense, type ExpenseDraft, type FinanceSummary,
  type Observation, type ProactiveLevel, type DayPlanItem,
} from "./lib/api";
import { apa, mla, bibtex } from "./lib/cite";
import Reader from "./Reader";
import WeekGrid from "./WeekGrid";
import PlanWeekModal from "./PlanWeekModal";
import ChatMarkdown from "./ChatMarkdown";

/* ───────────────────────────────────────── model */
// "planner" is the nav tab; "tasks"/"calendar" stay as sections so deep-links (e.g. a home
// card) can open the Planner on the right sub-tab.
type Section = "today" | "news" | "library" | "do" | "planner" | "tasks" | "calendar" | "mail" | "routines";
const NAV: { id: Section; label: string; icon: LucideIcon }[] = [
  { id: "today", label: "Today", icon: Sun },
  { id: "news", label: "News", icon: Newspaper },
  { id: "library", label: "Library", icon: BookOpen },
  { id: "do", label: "Concierge", icon: ConciergeBell },
  { id: "mail", label: "Mail", icon: Mail },
];

function ask(prompt: string) {
  window.dispatchEvent(new CustomEvent("himmy:ask", { detail: prompt }));
}

/* ── live refresh bus ──────────────────────────────────────────────────────
   When Himmy (or any actor) mutates a surface — adds a calendar event, saves an
   article, adds a paper, adds/finishes a task — it emits that surface here and
   the open view re-fetches immediately. Without this, a mutation only showed up
   after switching tabs (which remounts the view and re-fetches). */
type Surface = "calendar" | "tasks" | "library" | "news";
const _refreshBus: Record<Surface, Set<() => void>> = {
  calendar: new Set(), tasks: new Set(), library: new Set(), news: new Set(),
};
function emitRefresh(surface: Surface) {
  _refreshBus[surface].forEach((fn) => { try { fn(); } catch { /* a dead listener must not break the rest */ } });
}
// Window-event bridge so code in OTHER files (e.g. Reader.tsx) can poke the in-process bus
// without importing it. App listens for "himmy:refresh" and forwards to emitRefresh.
if (typeof window !== "undefined") {
  window.addEventListener("himmy:refresh", (e: Event) => {
    const s = (e as CustomEvent<string>).detail as Surface;
    if (s && s in _refreshBus) emitRefresh(s);
  });
}
/* Map the tools that just executed → the surfaces whose views should refresh.
   Only WRITE tools are listed; read tools (calendar_find, list_tasks, …) are ignored. */
function emitRefreshForTools(tools: string[] | undefined) {
  if (!tools || !tools.length) return;
  const hit = new Set<Surface>();
  for (const t of tools) {
    if (t === "calendar_add" || t === "calendar_edit" || t === "calendar_remove") hit.add("calendar");
    else if (t === "add_task" || t === "complete_task") hit.add("tasks");
    else if (t === "add_paper") hit.add("library");
    else if (t === "save_article") hit.add("news");
  }
  hit.forEach(emitRefresh);
}
/* Subscribe a view to its surface's refresh signal. Uses a ref so the latest
   callback (with fresh state/closures) always runs, even though we subscribe once. */
function useRefreshSignal(surface: Surface, cb: () => void) {
  const ref = useRef(cb);
  ref.current = cb;
  useEffect(() => {
    const fn = () => ref.current();
    _refreshBus[surface].add(fn);
    return () => { _refreshBus[surface].delete(fn); };
  }, [surface]);
}

/* ── "what am I looking at?" — the currently-open paper / article, so Himmy
   knows what "this" means when you ask "how does this relate to my work?".
   A module-level ref the CommandBar reads lazily at send time; additive, so a
   normal ask with nothing open still sends no context (unchanged behavior). */
type OpenItem =
  | { kind: "paper"; id: string }
  | { kind: "article"; title: string; source?: string; url?: string; text?: string }
  | null;
const openItemRef: { current: OpenItem } = { current: null };
function setOpenItem(item: OpenItem) { openItemRef.current = item; }

/* "Ask Himmy about this paper" from a recommendation card: stash the paper as the current
   context (so Himmy reads its details on send) and open the assistant with a ready question. */
function askHimmyAboutRec(p: RecPaper) {
  setOpenItem({
    kind: "article",
    title: p.title,
    source: p.venue,
    url: p.url,
    text: `${(p.authors || []).slice(0, 6).join(", ")}${p.year ? ` (${p.year})` : ""}. ${p.tldr || p.abstract || ""}`.trim(),
  });
  ask("What is this paper about, and how relevant is it to my research?");
}

// Resolve the open item into a short context string. Fetches paper metadata
// (+ a few highlights) on demand; tolerant of failures (returns undefined).
async function buildAskContext(): Promise<string | undefined> {
  const item = openItemRef.current;
  if (!item) return undefined;
  try {
    if (item.kind === "paper") {
      const r = await api.library.get(item.id);
      const p = r.item;
      if (!p) return undefined;
      const lines: string[] = [];
      lines.push(`Title: ${p.title || "(untitled)"}`);
      if (p.authors?.length) lines.push(`Authors: ${p.authors.join(", ")}`);
      const meta = [p.year, p.venue].filter(Boolean).join(" · ");
      if (meta) lines.push(meta);
      if (p.abstract) lines.push(`Abstract: ${p.abstract.slice(0, 1200)}`);
      try {
        const hs = (await api.highlights.list(item.id)).highlights || [];
        const quotes = hs.map((h) => h.text?.trim()).filter(Boolean).slice(0, 4);
        if (quotes.length) lines.push("Reader highlights:\n" + quotes.map((q) => `• ${q}`).join("\n"));
      } catch { /* highlights optional */ }
      return lines.join("\n");
    }
    // news article
    const lines: string[] = [];
    lines.push(`Article: ${item.title || "(untitled)"}`);
    if (item.source) lines.push(`Source: ${item.source}`);
    if (item.url) lines.push(`URL: ${item.url}`);
    if (item.text) lines.push(`Excerpt: ${item.text.slice(0, 1200)}`);
    return lines.join("\n");
  } catch {
    return undefined;
  }
}

function nav(section: Section) {
  window.dispatchEvent(new CustomEvent("himmy:nav", { detail: section }));
}

// Open a specific paper in the Library/Reader from anywhere (e.g. a task's paper chip).
function openPaper(id: string) {
  window.dispatchEvent(new CustomEvent("himmy:open-paper", { detail: id }));
}

// Open the notification bell / approvals panel from anywhere (e.g. a proactive "Do it" that
// parked a risky action for HITL approval). The App shell listens and flips the bell open.
function openBell() {
  window.dispatchEvent(new CustomEvent("himmy:bell"));
}

/* ───────────────────────────────────────── shell */
export default function App() {
  const [section, setSection] = useState<Section>("today");
  const [health, setHealth] = useState<Health | null>(null);
  const [err, setErr] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [notifOpen, setNotifOpen] = useState(false);
  const [routinesOpen, setRoutinesOpen] = useState(false);
  // First-run onboarding: show the warm "connect your AI brain" flow until the app can
  // actually run inference. `aiReady === false` (not null) means we've confirmed it's not
  // set up yet; `onbDismissed` lets the user close it for this session (it re-shows next
  // launch while still not ready). Settings can force it open via `onbForced`.
  const [aiReady, setAiReady] = useState<boolean | null>(null);
  const [onbDismissed, setOnbDismissed] = useState(false);
  const [onbForced, setOnbForced] = useState(false);
  const [notifs, setNotifs] = useState<NotificationItem[]>([]);
  const [unread, setUnread] = useState(0);
  // Himmy's proactive observations — the "Himmy noticed" cards, now living in the bell. Owned here
  // so the bell badge counts them and we can fire a macOS notification when a new one appears.
  const [obs, setObs] = useState<Observation[]>([]);
  const obsSeenRef = useRef<Set<string>>(new Set());
  const obsSeededRef = useRef(false);
  const { status: account } = useGoogle();   // drives the account-button avatar (connected email)
  // Track which notification ids we've already seen so we fire a native macOS notification
  // exactly once per new item — and never for the backlog present on first load.
  const seenRef = useRef<Set<string>>(new Set());
  const seededRef = useRef(false);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try { const h = await api.health(); if (alive) { setHealth(h); setErr(false); } }
      catch { if (alive) setErr(true); }
    };
    tick();
    const t = setInterval(tick, 5000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  // On launch, ask the backend whether Himmy can run inference yet. If not, the first-run
  // onboarding appears. We retry a few times while the backend is still warming up.
  useEffect(() => {
    let alive = true;
    let tries = 0;
    const check = async () => {
      try {
        const r = await api.providerStatus();
        if (!alive) return;
        setAiReady(r.ready);
        if (r.ready) return; // stop — set up
      } catch { /* backend still warming */ }
      if (alive && ++tries < 30) setTimeout(check, 2000);
    };
    check();
    // Let any surface (e.g. Settings) re-open onboarding on demand.
    const reopen = () => { setOnbForced(true); setOnbDismissed(false); };
    window.addEventListener("himmy:onboard", reopen);
    return () => { alive = false; window.removeEventListener("himmy:onboard", reopen); };
  }, []);

  // Poll the routine results inbox: keep the bell badge fresh and raise a native macOS
  // notification when a scheduled routine produces something new while the app is open.
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.notifications.list(50);
        if (!alive) return;
        setNotifs(r.notifications);
        setUnread(r.unread);
        if (!seededRef.current) {
          r.notifications.forEach((n) => seenRef.current.add(n.id));
          seededRef.current = true;
        } else {
          for (const n of r.notifications) {
            if (seenRef.current.has(n.id)) continue;
            seenRef.current.add(n.id);
            if (!n.read) {
              try { (window as any).himmy?.notify?.({ title: n.title, body: (n.body || "").slice(0, 180) }); }
              catch { /* notifications are best-effort */ }
            }
          }
        }
      } catch { /* backend warming up */ }
    };
    load();
    const t = setInterval(load, 15000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const loadNotifs = async () => {
    try { const r = await api.notifications.list(50); setNotifs(r.notifications); setUnread(r.unread); }
    catch { /* ignore */ }
  };
  const loadObs = async () => {
    try { const r = await api.proactive.list(); setObs(r.observations || []); }
    catch { /* ignore */ }
  };

  // Poll the proactive brain's observations (they live in /proactive, not the routine inbox): keep
  // the bell badge fresh and raise a native macOS notification once per NEW observation.
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.proactive.list();
        if (!alive) return;
        const list = r.observations || [];
        setObs(list);
        if (!obsSeededRef.current) {
          list.forEach((o) => obsSeenRef.current.add(o.id));
          obsSeededRef.current = true;
        } else {
          for (const o of list) {
            if (obsSeenRef.current.has(o.id)) continue;
            obsSeenRef.current.add(o.id);
            try { (window as any).himmy?.notify?.({ title: `Himmy noticed: ${o.title}`, body: (o.detail || "").slice(0, 180) }); }
            catch { /* notifications are best-effort */ }
          }
        }
      } catch { /* backend warming up */ }
    };
    load();
    const t = setInterval(load, 30000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  // Tell Himmy which paper is open (so "how does this relate to my work?" knows). Only while the
  // Reader is actually on screen — a paper kept open behind the News/Planner tab isn't "open".
  useEffect(() => {
    if (openId && section === "library") setOpenItem({ kind: "paper", id: openId });
    else if (openItemRef.current?.kind === "paper") setOpenItem(null);
  }, [openId, section]);

  // Section nav from anywhere + stop dropped files from navigating the window.
  useEffect(() => {
    const onNav = (e: Event) => setSection((e as CustomEvent<string>).detail as Section);
    // Open a paper in the Reader from anywhere (task paper chip): switch to Library + open it.
    const onOpenPaper = (e: Event) => {
      const id = (e as CustomEvent<string>).detail;
      if (!id) return;
      setSection("library");
      setOpenId(id);
    };
    // Open the bell (approvals inbox) on demand — a proactive action that parked for approval
    // refreshes the inbox first, then opens it so the Approve/Cancel card is right there.
    const onBell = () => { loadNotifs(); setNotifOpen(true); };
    const prevent = (e: DragEvent) => e.preventDefault();
    window.addEventListener("himmy:nav", onNav);
    window.addEventListener("himmy:open-paper", onOpenPaper);
    window.addEventListener("himmy:bell", onBell);
    window.addEventListener("dragover", prevent);
    window.addEventListener("drop", prevent);
    return () => {
      window.removeEventListener("himmy:nav", onNav);
      window.removeEventListener("himmy:open-paper", onOpenPaper);
      window.removeEventListener("himmy:bell", onBell);
      window.removeEventListener("dragover", prevent);
      window.removeEventListener("drop", prevent);
    };
  }, []);

  return (
    <div className="h-full w-full flex flex-col relative font-sans text-mac-ink">
      <Toolbar section={section}
        onSelect={(s) => {
          // Re-tapping the active Library tab while reading goes back to the list; switching to
          // ANY other tab now keeps the paper open, so returning to Library resumes it in place.
          if (s === "library" && section === "library" && openId) setOpenId(null);
          else setSection(s);
        }}
        online={!!health && !err} onSettings={() => setSettingsOpen(true)}
        unread={unread + obs.length} onBell={() => { loadObs(); setNotifOpen(true); }} accountEmail={account?.email} />
      <main className="flex-1 min-h-0 overflow-auto">
        {openId && section === "library"
          ? <Reader id={openId} onClose={() => setOpenId(null)} />
          : <Content section={section} health={health} onOpen={setOpenId} />}
      </main>
      <CommandBar />
      {settingsOpen && <AccountPanel onClose={() => setSettingsOpen(false)} />}
      {notifOpen && (
        <NotificationsPanel
          notifs={notifs}
          obs={obs}
          onObsRefresh={() => { loadObs(); loadNotifs(); }}
          onRefresh={loadNotifs}
          onClose={() => { setNotifOpen(false); loadNotifs(); }}
          onRoutines={() => { setNotifOpen(false); setRoutinesOpen(true); }}
        />
      )}
      {routinesOpen && <RoutinesModal onClose={() => setRoutinesOpen(false)} />}
      {(onbForced || (aiReady === false && !onbDismissed)) && (
        <OnboardingModal
          onClose={() => { setOnbDismissed(true); setOnbForced(false); }}
          onReady={() => { setAiReady(true); api.health().then(setHealth).catch(() => {}); emitRefresh("tasks"); }}
        />
      )}
    </div>
  );
}

/* Routines live behind the bell now — a roomy modal so the full manager has space. */
function RoutinesModal({ onClose }: { onClose: () => void }) {
  return (
    <div className="absolute inset-0 z-50 bg-black/30 grid place-items-center p-6" onMouseDown={onClose}>
      <div onMouseDown={(e) => e.stopPropagation()}
        className="w-[820px] max-w-full h-[82%] max-h-[720px] rounded-2xl bg-[rgba(30,31,37,0.98)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden flex flex-col">
        <div className="h-11 px-3 flex items-center justify-end border-b border-mac-stroke shrink-0">
          <button onClick={onClose} className="h-7 w-7 grid place-items-center rounded-[7px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors"><X size={16} /></button>
        </div>
        <div className="flex-1 min-h-0"><Routines /></div>
      </div>
    </div>
  );
}

/* ───────────────────────────────────────── toolbar */
function Toolbar({ section, onSelect, online, onSettings, unread, onBell, accountEmail }:
  { section: Section; onSelect: (s: Section) => void; online: boolean; onSettings: () => void;
    unread: number; onBell: () => void; accountEmail?: string | null }) {
  return (
    <header className="titlebar-drag h-[52px] shrink-0 grid grid-cols-[1fr_auto_1fr] items-center px-3 border-b border-mac-stroke">
      {/* left cell — intentionally empty (just clears the macOS traffic lights) so the nav stays centered */}
      <div className="pl-[78px]" aria-hidden />


      <nav className="no-drag justify-self-center flex items-center gap-0.5 p-0.5 rounded-[11px] bg-mac-fill border border-mac-stroke">
        {NAV.map((n) => {
          const active = section === n.id ||
            (n.id === "today" && (section === "planner" || section === "tasks" || section === "calendar"));
          const Ico = n.icon;
          return (
            <button key={n.id} onClick={() => onSelect(n.id)}
              className={`flex items-center gap-1.5 h-[30px] px-3 rounded-[9px] text-[13px] transition-colors ${
                active ? "bg-mac-fillHi text-mac-ink shadow-tab" : "text-mac-ink2 hover:text-mac-ink"
              }`}>
              <Ico size={15} strokeWidth={2} className={active ? "text-mac-accentHi" : ""} />
              {n.label}
            </button>
          );
        })}
      </nav>

      <div className="no-drag justify-self-end flex items-center gap-1.5">
        <button onClick={onBell} title="Notifications"
          className="relative h-8 w-8 grid place-items-center rounded-[9px] text-mac-ink2 hover:text-mac-ink hover:bg-mac-fill transition-colors">
          <Bell size={15} strokeWidth={2} />
          {unread > 0 && (
            <span className="absolute top-0.5 right-0.5 min-w-[15px] h-[15px] px-[3px] rounded-full bg-mac-red text-white text-[9px] font-semibold grid place-items-center leading-none">
              {unread > 9 ? "9+" : unread}
            </span>
          )}
        </button>
        <button onClick={onSettings} title="Account"
          className="h-8 w-8 grid place-items-center rounded-[9px] hover:bg-mac-fill transition-colors">
          <AccountAvatar email={accountEmail} size={22} />
        </button>
      </div>
    </header>
  );
}

/* ───────────────────────────────────────── content router */
function Content({ section, health, onOpen }:
  { section: Section; health: Health | null; onOpen: (id: string) => void }) {
  switch (section) {
    case "today": return <HomeTab health={health} />;
    case "library": return <Library onOpen={onOpen} />;
    case "news": return <News />;
    case "do": return <DoTab />;
    case "planner": return <HomeTab health={health} initialView="plan" />;
    case "tasks": return <HomeTab health={health} initialView="plan" initialPlan="tasks" />;
    case "calendar": return <HomeTab health={health} initialView="plan" initialPlan="calendar" />;
    case "routines": return <Routines />;
    case "mail": return <MailTab />;
    default: return <HomeTab health={health} />;
  }
}

/* ───────────────────────────────────────── do (concierge) */
// The "Do" tab — a smart Nepal concierge. Rails of food / shopping / flight picks come from the
// /do board: free, instant rules picks render right away; the one cheap AI pass (personal "why" +
// re-rank) lands a beat later in the background, so glancing at the page costs no model usage.
type DoRailKey = "food" | "deals" | "foryou" | "flights";
const DO_RAILS: { key: DoRailKey; label: string; icon: LucideIcon; action: string; verb: string }[] = [
  { key: "food", label: "Eat", icon: UtensilsCrossed, action: "Order", verb: "order food on Foodmandu" },
  { key: "deals", label: "Deals", icon: ShoppingBag, action: "Buy", verb: "shop on Daraz" },
  { key: "foryou", label: "For You", icon: Heart, action: "Buy", verb: "shop for your interests" },
  { key: "flights", label: "Fly", icon: Plane, action: "See flights", verb: "find a Buddha Air flight" },
];
const DO_CHIPS = ["Order momos", "Find a gift under Rs 2000", "Best headphone deals", "Newari food near me"];
// Tappable examples for the Flights / Buses / Trips modes, which otherwise show a dead screen
// until you type. Each chip drives the SAME state the real inputs do (setFlightRoute / setBusRoute
// / setTripOpen) so a tap behaves exactly like filling the fields and hitting Search/Plan.
const DO_FLIGHT_EXAMPLES: { label: string; from: string; to: string }[] = [
  { label: "KTM → PKR", from: "KTM", to: "PKR" },
  { label: "KTM → BWA", from: "KTM", to: "BWA" },
  { label: "PKR → KTM", from: "PKR", to: "KTM" },
];
const DO_BUS_EXAMPLES: { label: string; from: string; to: string }[] = [
  { label: "Kathmandu → Pokhara", from: "Kathmandu", to: "Pokhara" },
  { label: "Kathmandu → Chitwan", from: "Kathmandu", to: "Chitwan" },
  { label: "Pokhara → Kathmandu", from: "Pokhara", to: "Kathmandu" },
];
const DO_TRIP_EXAMPLES: { label: string; dest: string; days: number }[] = [
  { label: "Pokhara · 2 days", dest: "Pokhara", days: 2 },
  { label: "Chitwan · 3 days", dest: "Chitwan", days: 3 },
  { label: "Lumbini · 2 days", dest: "Lumbini", days: 2 },
];
type SearchKind = "food" | "shop" | "flights" | "buses" | "trips";
const SEARCH_MODES: { key: SearchKind; label: string; icon: LucideIcon }[] = [
  { key: "food", label: "Food", icon: UtensilsCrossed },
  { key: "shop", label: "Products", icon: ShoppingBag },
  { key: "flights", label: "Flights", icon: Plane },
  { key: "buses", label: "Buses", icon: Bus },
  { key: "trips", label: "Trips", icon: Route },
];

function doPriceNum(s?: string): number {
  const m = (s || "").replace(/,/g, "").match(/(\d+(\.\d+)?)/);
  return m ? parseFloat(m[1]) : 0;
}

function DoTab() {
  const [board, setBoard] = useState<DoBoard | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  // The board API THREW (network/backend down) — distinct from an ok-but-empty board.
  const [boardError, setBoardError] = useState(false);
  const [, tick] = useState(0);
  // tray
  const [cart, setCart] = useState<DoCartView | null>(null);
  const [trayOpen, setTrayOpen] = useState(false);
  // restaurant menu modal
  const [detail, setDetail] = useState<DoPick | null>(null);
  // flight tickets modal + flight search fields. `returnDate` is set only when the round-trip
  // toggle is on → the modal then fetches an inbound leg + a round-trip total (one-way otherwise).
  const [flightRoute, setFlightRoute] = useState<{ from: string; to: string; date: string; returnDate?: string } | null>(null);
  const defaultDate = new Date(Date.now() + 8 * 86400000).toISOString().slice(0, 10);
  const flReturnDefault = new Date(Date.now() + 11 * 86400000).toISOString().slice(0, 10);
  const [flFrom, setFlFrom] = useState("KTM");
  const [flTo, setFlTo] = useState("PKR");
  const [flDate, setFlDate] = useState(defaultDate);
  const [flRoundTrip, setFlRoundTrip] = useState(false);
  const [flReturnDate, setFlReturnDate] = useState(flReturnDefault);
  // bus tickets modal + bus search fields
  const busDefaultDate = new Date(Date.now() + 3 * 86400000).toISOString().slice(0, 10);
  const [busRoute, setBusRoute] = useState<{ from: string; to: string; date: string } | null>(null);
  const [busFrom, setBusFrom] = useState("Kathmandu");
  const [busTo, setBusTo] = useState("Pokhara");
  const [busDate, setBusDate] = useState(busDefaultDate);
  const [busCities, setBusCities] = useState<string[]>([]);
  // trip roadmap — depart date defaults a week out so the attached weather is a real in-window
  // forecast rather than a seasonal fallback.
  const tripDefaultDate = new Date(Date.now() + 7 * 86400000).toISOString().slice(0, 10);
  const [tripDest, setTripDest] = useState("");
  const [tripDays, setTripDays] = useState(2);
  const [tripStyle, setTripStyle] = useState<"budget" | "comfort" | "luxury">("comfort");
  const [tripDate, setTripDate] = useState(tripDefaultDate);
  const [tripOpen, setTripOpen] = useState<{ dest: string; days: number; style: string; date: string } | null>(null);
  // inline search
  const [searchQ, setSearchQ] = useState("");
  const [searchKind, setSearchKind] = useState<SearchKind>("food");
  const [modeMenuOpen, setModeMenuOpen] = useState(false);   // the active-mode dropdown
  const [results, setResults] = useState<DoPick[] | null>(null);
  const [searching, setSearching] = useState(false);
  // The search call THREW — show a retry, not the "0 results" empty copy.
  const [searchError, setSearchError] = useState(false);
  // Food/shop search: offer banners + smart, personalised suggestions (a rotating hint + dropdown).
  const [promos, setPromos] = useState<DoPromo[]>([]);
  const [suggestions, setSuggestions] = useState<DoSuggestion[]>([]);
  const [hintIdx, setHintIdx] = useState(0);
  const [suggOpen, setSuggOpen] = useState(false);
  const openFlight = (p: DoPick) => {
    const [f, t] = (p.key || "").split("-");
    setFlightRoute({ from: f || "KTM", to: t || "PKR", date: p.date || defaultDate });
  };

  const load = async (force = false) => {
    try {
      const b = force ? await api.do.refresh() : await api.do.board();
      setBoard(b);
      setBoardError(false);
      return b;
    } catch {
      // jget throws on a non-ok / unreachable backend — surface a real error state with Retry
      // rather than the friendly "no picks" copy (which is only for an ok-but-empty board).
      setBoardError(true);
      return null;
    }
  };
  const loadCart = async () => { try { setCart(await api.do.cart.view()); } catch { /* warming */ } };

  useEffect(() => {
    let alive = true;
    let tries = 0;
    (async () => {
      setLoading(true);
      const b = await load();
      setLoading(false);
      loadCart();
      const poll = async () => {
        if (!alive || tries >= 4) return;
        tries += 1;
        const nb = await load();
        if (alive && nb && nb.stale) setTimeout(poll, 3500);
      };
      if (b && b.stale) setTimeout(poll, 3500);
    })();
    const clock = setInterval(() => tick((n) => n + 1), 30000);
    return () => { alive = false; clearInterval(clock); };
  }, []);

  const refresh = async () => { setRefreshing(true); await load(true); setRefreshing(false); };

  const dismiss = (rail: DoRailKey, p: DoPick) => {
    setBoard((b) => b ? { ...b, [rail]: b[rail].filter((x) => x.key !== p.key) } : b);
    api.do.feedback({ kind: "down", key: p.key, rail, tags: p.tag ? [p.tag] : [] }).catch(() => {});
  };

  // add a Foodmandu DISH (from the dish search) to the tray, grouped under its restaurant
  const addDish = async (p: DoPick) => {
    try {
      const v = await api.do.cart.add({
        key: p.key, name: p.title, price: doPriceNum(p.subtitle), source: "food",
        place: p.meta || "Foodmandu", image: p.image, link: p.link, checkout_link: p.link,
      });
      setCart(v); setTrayOpen(true);
    } catch { /* ignore */ }
  };

  // add a Daraz product (deals / foryou / shop search) to the tray
  const addProduct = async (p: DoPick) => {
    try {
      const v = await api.do.cart.add({
        key: p.key, name: p.title, price: doPriceNum(p.subtitle), source: "shop",
        place: "Daraz", image: p.image, link: p.link, checkout_link: p.link,
      });
      setCart(v); setTrayOpen(true);
    } catch { /* ignore */ }
  };

  const runSearch = async (q = searchQ, kind: SearchKind = searchKind, budget?: number | null) => {
    const term = q.trim();
    if (!term || kind === "flights" || kind === "buses" || kind === "trips") { setResults(null); return; }
    setSuggOpen(false);
    setSearching(true); setSearchError(false);
    try {
      // For food, the backend defaults to the user's SAVED food budget when we don't pass one —
      // so a plain search already respects their budget. A suggestion may carry its own cap.
      const r = await api.do.search(term, kind === "shop" ? "shop" : "food", kind === "food" ? (budget ?? null) : null);
      setResults(r.results || []);
      setPromos(kind === "food" ? (r.promos || []) : []);
    }
    // A thrown call → error state (with Retry), NOT a friendly "0 results" — those mean
    // the search ran fine but matched nothing.
    catch { setResults([]); setPromos([]); setSearchError(true); } finally { setSearching(false); }
  };
  // Run one personalised suggestion (carries its own query + optional budget cap).
  const runSuggestion = (s: DoSuggestion) => {
    setSearchQ(s.query); setSuggOpen(false);
    runSearch(s.query, s.kind === "shop" ? "shop" : "food", s.max_price ?? undefined);
  };
  // Load smart suggestions whenever the user switches into Food / Products.
  useEffect(() => {
    if (searchKind === "food" || searchKind === "shop") {
      api.do.suggestions(searchKind).then((r) => { setSuggestions(r.suggestions || []); setHintIdx(0); })
        .catch(() => setSuggestions([]));
    } else { setSuggestions([]); }
  }, [searchKind]);
  // Rotate the in-bar hint while the field is empty (a gentle "Try: …" carousel).
  useEffect(() => {
    if (suggestions.length < 2 || searchQ) return;
    const t = setInterval(() => setHintIdx((i) => (i + 1) % suggestions.length), 3200);
    return () => clearInterval(t);
  }, [suggestions, searchQ]);
  // Load the bussewa city list once the user first opens the Buses tab (powers autocomplete).
  useEffect(() => {
    if (searchKind === "buses" && busCities.length === 0) {
      api.do.busCities().then((r) => setBusCities(r.cities || [])).catch(() => { /* autocomplete just won't suggest */ });
    }
  }, [searchKind, busCities.length]);

  const empty = board && !board.food.length && !board.deals.length && !board.foryou.length && !board.flights.length;
  // The board is showing, but the AI re-rank ("For You" tailoring) is still running in the
  // background — show a subtle pill + fade the personalized cards in instead of flickering.
  const personalizing = !!board && !!board.stale && !empty;
  const cartCount = cart?.count || 0;

  return (
    <div className="h-full flex flex-col">
      {/* Concierge-scoped animations: a gentle fade-in for personalized cards (positions are NOT
          animated — pick.key is a stable key so React reuses nodes and nothing jumps) + a soft
          pulse on the "Tailoring this for you…" pill while the AI re-rank is pending. */}
      <style>{`
        @keyframes do-fade-in { from { opacity: 0; } to { opacity: 1; } }
        .do-personalizing { animation: do-fade-in 420ms ease-out; }
        @keyframes do-pill-pulse { 0%,100% { opacity: 0.7; } 50% { opacity: 1; } }
        .do-shimmer-pill { animation: do-pill-pulse 1.6s ease-in-out infinite; }
      `}</style>
      {/* header */}
      <div className="relative shrink-0 px-9 pt-8 pb-4 mx-auto w-full max-w-[1180px]">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="font-display text-[28px] font-semibold leading-[1.1] tracking-[-0.025em] bg-gradient-to-b from-white to-white/75 bg-clip-text text-transparent">
              {board?.headline || "What can I get you?"}
            </h1>
            {board?.generated_at && (
              <p className="text-[11.5px] text-mac-ink3 mt-1.5">Updated {relativeAgo(board.generated_at)}</p>
            )}
          </div>
          <div className="shrink-0 flex items-center gap-2">
            <button onClick={() => { loadCart(); setTrayOpen(true); }} title="Your tray"
              className="relative h-9 px-3.5 rounded-[10px] text-[12.5px] font-medium inline-flex items-center gap-1.5 bg-white/[0.05] ring-1 ring-inset ring-white/10 backdrop-blur text-mac-ink2 hover:text-mac-ink hover:bg-white/[0.09] transition-colors">
              <ShoppingCart size={14} /> Tray
              {cartCount > 0 && (
                <span className="min-w-[17px] h-[17px] px-1 rounded-full bg-gradient-to-b from-mac-accentHi to-mac-accent text-white text-[10px] font-semibold grid place-items-center ring-1 ring-inset ring-white/20">{cartCount}</span>
              )}
            </button>
            <button onClick={refresh} disabled={refreshing} title="Refresh picks"
              className="h-9 px-3.5 rounded-[10px] text-[12.5px] font-medium inline-flex items-center gap-1.5 bg-white/[0.05] ring-1 ring-inset ring-white/10 backdrop-blur text-mac-ink2 hover:text-mac-ink hover:bg-white/[0.09] transition-colors disabled:opacity-60">
              <RefreshCw size={13} className={refreshing ? "animate-spin" : ""} /> Refresh
            </button>
          </div>
        </div>

        {/* unified command bar — mode segments + a context-aware search/flight field */}
        <div className="relative mt-5 flex items-center h-12 rounded-2xl bg-white/[0.04] ring-1 ring-inset ring-white/10 focus-within:ring-white/[0.18] transition-all pl-1.5 pr-1.5">
          {/* Active-mode DROPDOWN — one slim control showing the current mode; opens to switch. */}
          {(() => {
            const active = SEARCH_MODES.find((m) => m.key === searchKind) || SEARCH_MODES[0];
            const AI = active.icon;
            return (
              <div className="relative shrink-0">
                <button onClick={() => setModeMenuOpen((o) => !o)} title="Switch mode"
                  className="h-9 pl-2.5 pr-2 rounded-[10px] text-[12.5px] font-medium inline-flex items-center gap-1.5 bg-white/[0.06] text-mac-ink ring-1 ring-inset ring-white/10 hover:bg-white/[0.1] transition-colors">
                  <AI size={14} strokeWidth={2} className="text-mac-accentHi" /> {active.label}
                  <ChevronDown size={13} strokeWidth={2.2} className={`text-mac-ink3 transition-transform ${modeMenuOpen ? "rotate-180" : ""}`} />
                </button>
                {modeMenuOpen && (
                  <>
                    <div className="fixed inset-0 z-30" onClick={() => setModeMenuOpen(false)} />
                    <div className="absolute top-full left-0 mt-1.5 z-40 w-44 p-1 rounded-xl bg-[rgba(36,37,43,0.98)] backdrop-blur-2xl border border-mac-strokeHi shadow-pop">
                      {SEARCH_MODES.map((m) => {
                        const MI = m.icon;
                        const on = searchKind === m.key;
                        return (
                          <button key={m.key}
                            onClick={() => { setSearchKind(m.key); setResults(null); setModeMenuOpen(false); if ((m.key === "food" || m.key === "shop") && searchQ.trim()) runSearch(searchQ, m.key); }}
                            className={`w-full h-8 px-2.5 rounded-[8px] text-[12.5px] inline-flex items-center gap-2 transition-colors ${
                              on ? "bg-white/[0.1] text-mac-ink" : "text-mac-ink2 hover:text-mac-ink hover:bg-white/[0.06]"}`}>
                            <MI size={14} strokeWidth={2} className={on ? "text-mac-accentHi" : "text-mac-ink3"} /> {m.label}
                            {on && <Check size={13} className="ml-auto text-mac-accentHi" />}
                          </button>
                        );
                      })}
                    </div>
                  </>
                )}
              </div>
            );
          })()}
          <div className="w-px h-6 bg-white/10 mx-2 shrink-0" />
          {searchKind === "flights" ? (
            <div className="flex-1 min-w-0 flex items-center gap-1.5">
              <Plane size={15} className="text-mac-ink3 shrink-0" />
              <input value={flFrom} onChange={(e) => setFlFrom(e.target.value)}
                className="w-12 bg-transparent text-[13px] font-medium text-mac-ink placeholder:text-mac-ink3 outline-none uppercase" placeholder="From" />
              <ArrowRight size={12} className="text-mac-ink3 shrink-0" />
              <input value={flTo} onChange={(e) => setFlTo(e.target.value)}
                className="w-12 bg-transparent text-[13px] font-medium text-mac-ink placeholder:text-mac-ink3 outline-none uppercase" placeholder="To" />
              <div className="w-px h-5 bg-white/10 mx-1.5 shrink-0" />
              {/* dates, label-free: the native date picker already shows a calendar glyph; the ⇄
                  toggle between the two dates adds a return (round trip). */}
              <input type="date" value={flDate} onChange={(e) => setFlDate(e.target.value)} title="Depart date"
                className="bg-transparent text-[12.5px] text-mac-ink2 outline-none [color-scheme:dark] cursor-pointer" />
              <button onClick={() => setFlRoundTrip((v) => !v)}
                title={flRoundTrip ? "Round trip — click for one-way" : "Add a return (round trip)"}
                className={`h-7 w-7 shrink-0 grid place-items-center rounded-[8px] transition-colors ${
                  flRoundTrip ? "bg-[rgba(10,132,255,0.16)] text-mac-accentHi ring-1 ring-inset ring-mac-accentHi/30" : "text-mac-ink3 hover:text-mac-ink hover:bg-white/[0.06]"}`}>
                <ArrowRightLeft size={13} />
              </button>
              {flRoundTrip && (
                <input type="date" value={flReturnDate} min={flDate} onChange={(e) => setFlReturnDate(e.target.value)} title="Return"
                  className="bg-transparent text-[12.5px] text-mac-ink2 outline-none [color-scheme:dark] cursor-pointer" />
              )}
              <div className="flex-1" />
              <button onClick={() => flFrom.trim() && flTo.trim() && setFlightRoute({ from: flFrom.trim(), to: flTo.trim(), date: flDate, returnDate: flRoundTrip ? flReturnDate : undefined })}
                className="h-8 px-3.5 shrink-0 rounded-[9px] text-[12px] font-semibold text-white bg-gradient-to-b from-mac-accentHi to-mac-accent ring-1 ring-inset ring-white/15 shadow-[0_2px_8px_-2px_rgba(10,132,255,0.5)] hover:brightness-[1.06] transition-all inline-flex items-center gap-1.5">
                <Search size={13} /> Search
              </button>
            </div>
          ) : searchKind === "buses" ? (
            <div className="flex-1 flex items-center gap-2">
              <Bus size={15} className="text-mac-ink3 shrink-0" />
              <input value={busFrom} onChange={(e) => setBusFrom(e.target.value)} list="bus-cities"
                onKeyDown={(e) => { if (e.key === "Enter" && busFrom.trim() && busTo.trim()) setBusRoute({ from: busFrom.trim(), to: busTo.trim(), date: busDate }); }}
                className="w-28 bg-transparent text-[13px] font-medium text-mac-ink placeholder:text-mac-ink3 outline-none" placeholder="From" />
              <ArrowRight size={13} className="text-mac-ink3 shrink-0" />
              <input value={busTo} onChange={(e) => setBusTo(e.target.value)} list="bus-cities"
                onKeyDown={(e) => { if (e.key === "Enter" && busFrom.trim() && busTo.trim()) setBusRoute({ from: busFrom.trim(), to: busTo.trim(), date: busDate }); }}
                className="w-28 bg-transparent text-[13px] font-medium text-mac-ink placeholder:text-mac-ink3 outline-none" placeholder="To" />
              <input type="date" value={busDate} onChange={(e) => setBusDate(e.target.value)}
                className="bg-transparent text-[12.5px] text-mac-ink2 outline-none [color-scheme:dark]" />
              <div className="flex-1" />
              <button onClick={() => busFrom.trim() && busTo.trim() && setBusRoute({ from: busFrom.trim(), to: busTo.trim(), date: busDate })}
                className="h-8 px-4 rounded-[9px] text-[12px] font-semibold text-white bg-gradient-to-b from-mac-accentHi to-mac-accent ring-1 ring-inset ring-white/15 shadow-[0_2px_8px_-2px_rgba(10,132,255,0.5)] hover:brightness-[1.06] transition-all inline-flex items-center gap-1.5">
                <Search size={13} /> Search
              </button>
            </div>
          ) : searchKind === "trips" ? (
            <div className="flex-1 flex items-center gap-2">
              <Route size={15} className="text-mac-ink3 shrink-0" />
              <input value={tripDest} onChange={(e) => setTripDest(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && tripDest.trim()) setTripOpen({ dest: tripDest.trim(), days: tripDays, style: tripStyle, date: tripDate }); }}
                placeholder="Where to? e.g. Pokhara, Chitwan, Lumbini…"
                className="flex-1 min-w-0 bg-transparent text-[13px] text-mac-ink placeholder:text-mac-ink3 outline-none" />
              {/* Depart date — drives a REAL weather forecast for the trip window when it's inside
                  the ~16-day horizon; the modal leads with the season otherwise. */}
              <div className="flex items-center gap-1 shrink-0">
                <Calendar size={12} className="text-mac-ink3" />
                <input type="date" value={tripDate} onChange={(e) => setTripDate(e.target.value)} title="Depart date"
                  className="bg-transparent text-[12.5px] text-mac-ink2 outline-none [color-scheme:dark] cursor-pointer" />
              </div>
              <div className="flex items-center p-0.5 rounded-[8px] bg-black/20 ring-1 ring-inset ring-white/[0.06] shrink-0">
                {(["budget", "comfort", "luxury"] as const).map((s) => (
                  <button key={s} onClick={() => setTripStyle(s)} title={`${s} travel`}
                    className={`h-6 px-2 rounded-[6px] text-[11px] font-medium capitalize transition-colors ${
                      tripStyle === s ? "bg-white/[0.1] text-mac-ink" : "text-mac-ink3 hover:text-mac-ink"}`}>
                    {s}
                  </button>
                ))}
              </div>
              <select value={tripDays} onChange={(e) => setTripDays(+e.target.value)}
                className="bg-transparent text-[12.5px] text-mac-ink2 outline-none [color-scheme:dark] cursor-pointer shrink-0">
                {[1, 2, 3, 4, 5, 6, 7].map((n) => <option key={n} value={n}>{n} day{n > 1 ? "s" : ""}</option>)}
              </select>
              <button onClick={() => tripDest.trim() && setTripOpen({ dest: tripDest.trim(), days: tripDays, style: tripStyle, date: tripDate })}
                className="h-8 px-4 shrink-0 rounded-[9px] text-[12px] font-semibold text-white bg-gradient-to-b from-mac-accentHi to-mac-accent ring-1 ring-inset ring-white/15 shadow-[0_2px_8px_-2px_rgba(10,132,255,0.5)] hover:brightness-[1.06] transition-all inline-flex items-center gap-1.5">
                <Sparkles size={13} /> Plan
              </button>
            </div>
          ) : (
            <div className="flex-1 flex items-center gap-2.5">
              <Search size={15} className="text-mac-ink3 shrink-0" />
              <input value={searchQ} onChange={(e) => setSearchQ(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") runSearch(); }}
                onFocus={() => setSuggOpen(true)}
                onBlur={() => setTimeout(() => setSuggOpen(false), 160)}
                placeholder={!searchQ && suggestions.length
                  ? `Try: ${suggestions[hintIdx % suggestions.length].label}…`
                  : (searchKind === "food" ? "Search dishes on Foodmandu…" : "Search products on Daraz…")}
                className="flex-1 bg-transparent text-[13px] text-mac-ink placeholder:text-mac-ink3 outline-none" />
              {searchQ && (
                <button onClick={() => { setSearchQ(""); setResults(null); }} className="text-mac-ink3 hover:text-mac-ink shrink-0"><X size={14} /></button>
              )}
            </div>
          )}
          {/* smart suggestions dropdown — personalised picks (tastes + saved budget), in the bar */}
          {suggOpen && !searchQ && (searchKind === "food" || searchKind === "shop") && suggestions.length > 0 && (
            <div className="absolute left-0 right-0 top-full mt-2 z-30 rounded-xl bg-[rgba(34,35,41,0.98)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden py-1">
              <div className="px-3 py-1.5 text-[10.5px] uppercase tracking-wide text-mac-ink3 flex items-center gap-1.5">
                <Sparkles size={11} className="text-mac-accentHi" /> Suggested for you
              </div>
              {suggestions.map((s, i) => (
                <button key={i} onMouseDown={(e) => { e.preventDefault(); runSuggestion(s); }}
                  className="w-full text-left px-3 py-2 flex items-center gap-2.5 hover:bg-white/[0.06] transition-colors">
                  <Search size={13} className="text-mac-ink3 shrink-0" />
                  <span className="text-[13px] text-mac-ink">{s.label}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        {/* quick asks — mode-aware. Flights / Buses / Trips otherwise sit on a dead screen until
            you type, so we offer tappable examples that drive the real state setters. (Food/Products
            get smart in-bar suggestions instead — see the search field above.) */}
        {!results && (searchKind === "flights" || searchKind === "buses" || searchKind === "trips") && (
          <div className="flex flex-wrap items-center gap-2 mt-3.5">
            {searchKind === "flights" ? (
              <>
                <span className="text-[11px] text-mac-ink3 mr-0.5">Try</span>
                {DO_FLIGHT_EXAMPLES.map((ex) => (
                  <button key={ex.label}
                    onClick={() => { setFlFrom(ex.from); setFlTo(ex.to); setFlightRoute({ from: ex.from, to: ex.to, date: flDate, returnDate: flRoundTrip ? flReturnDate : undefined }); }}
                    className="h-8 px-3 rounded-full text-[11.5px] inline-flex items-center gap-1.5 bg-white/[0.04] ring-1 ring-inset ring-white/10 text-mac-ink2 hover:text-mac-ink hover:bg-white/[0.08] transition-colors">
                    <Plane size={11} className="text-mac-ink3" /> {ex.label}
                  </button>
                ))}
              </>
            ) : searchKind === "buses" ? (
              <>
                <span className="text-[11px] text-mac-ink3 mr-0.5">Try</span>
                {DO_BUS_EXAMPLES.map((ex) => (
                  <button key={ex.label}
                    onClick={() => { setBusFrom(ex.from); setBusTo(ex.to); setBusRoute({ from: ex.from, to: ex.to, date: busDate }); }}
                    className="h-8 px-3 rounded-full text-[11.5px] inline-flex items-center gap-1.5 bg-white/[0.04] ring-1 ring-inset ring-white/10 text-mac-ink2 hover:text-mac-ink hover:bg-white/[0.08] transition-colors">
                    <Bus size={11} className="text-mac-ink3" /> {ex.label}
                  </button>
                ))}
              </>
            ) : searchKind === "trips" ? (
              <>
                <span className="text-[11px] text-mac-ink3 mr-0.5">Try</span>
                {DO_TRIP_EXAMPLES.map((ex) => (
                  <button key={ex.label}
                    onClick={() => { setTripDest(ex.dest); setTripDays(ex.days); setTripOpen({ dest: ex.dest, days: ex.days, style: tripStyle, date: tripDate }); }}
                    className="h-8 px-3 rounded-full text-[11.5px] inline-flex items-center gap-1.5 bg-white/[0.04] ring-1 ring-inset ring-white/10 text-mac-ink2 hover:text-mac-ink hover:bg-white/[0.08] transition-colors">
                    <Route size={11} className="text-mac-ink3" /> {ex.label}
                  </button>
                ))}
              </>
            ) : (
              DO_CHIPS.map((c) => (
                <button key={c} onClick={() => ask(c)}
                  className="h-8 px-3 rounded-full text-[11.5px] bg-white/[0.04] ring-1 ring-inset ring-white/10 text-mac-ink2 hover:text-mac-ink hover:bg-white/[0.08] transition-colors">
                  {c}
                </button>
              ))
            )}
          </div>
        )}
      </div>

      {/* body — search results OR the picked-for-you rails */}
      <div className="flex-1 min-h-0 overflow-y-auto pb-12">
        <div className="mx-auto w-full max-w-[1180px] px-9">
          {results ? (
            <section className="mt-2">
              <div className="flex items-center gap-2 mb-3">
                <h2 className="font-display text-[15px] font-semibold text-mac-ink">
                  {searching ? "Searching…" : `${results.length} result${results.length === 1 ? "" : "s"} for "${searchQ}"`}
                </h2>
              </div>
              {searching ? (
                <div className="h-40 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
              ) : searchError ? (
                <DoErrorState message="Couldn't run that search — the backend didn't respond."
                  onRetry={() => runSearch(searchQ, searchKind)} />
              ) : results.length === 0 ? (
                <div className="h-40 grid place-items-center text-center">
                  <p className="text-[13px] text-mac-ink2">Nothing matched "{searchQ}" — try a different word.</p>
                </div>
              ) : (
                <>
                  {/* offers: vendor promo banners that came back with the dishes */}
                  {searchKind === "food" && promos.length > 0 && (
                    <div className="flex flex-wrap gap-2 mb-4">
                      {promos.map((pr, i) => (
                        <a key={i} href={pr.order_link} target="_blank" rel="noreferrer"
                          className="inline-flex items-center gap-1.5 h-8 pl-2.5 pr-3 rounded-full bg-mac-green/[0.12] ring-1 ring-inset ring-mac-green/25 text-[11.5px] text-mac-ink2 hover:text-mac-ink transition-colors">
                          <Tag size={11} className="text-mac-green shrink-0" />
                          <span className="font-medium text-mac-ink">{pr.restaurant}</span>
                          <span className="text-mac-ink3">·</span> {pr.promo}
                        </a>
                      ))}
                    </div>
                  )}
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3.5">
                    {results.map((p) => (
                      searchKind === "food"
                        ? <DoDishCard key={p.key} p={p} onOpen={() => setDetail(p)} onAdd={() => addDish(p)} />
                        : <DoPickCard key={p.key} p={p} rail="deals" action="Buy" onAdd={() => addProduct(p)} />
                    ))}
                  </div>
                </>
              )}
            </section>
          ) : loading && !board ? (
            <div className="h-64 grid place-items-center text-mac-ink3"><Loader2 size={20} className="animate-spin" /></div>
          ) : boardError && !board ? (
            <DoErrorState message="Couldn't reach Himmy's concierge — the backend may still be starting up."
              onRetry={() => { setLoading(true); load().finally(() => setLoading(false)); }} className="h-64" />
          ) : empty ? (
            <div className="h-64 grid place-items-center text-center">
              <div>
                <Sparkles size={22} className="text-mac-accentHi mx-auto mb-3" />
                <p className="text-[13px] text-mac-ink2">No picks right now — try Refresh, or ask Himmy directly.</p>
              </div>
            </div>
          ) : (
            <div className={personalizing ? "do-personalizing" : ""}>
            {personalizing && (
              <div className="mt-2 mb-1 flex items-center gap-2">
                <span className="inline-flex items-center gap-1.5 h-7 px-3 rounded-full text-[11.5px] font-medium text-mac-ink2 bg-white/[0.05] ring-1 ring-inset ring-white/10 do-shimmer-pill">
                  <Sparkles size={12} className="text-mac-accentHi" /> Tailoring this for you…
                </span>
              </div>
            )}
            {DO_RAILS.map((rail) => {
              const picks = board?.[rail.key] || [];
              if (!picks.length) return null;
              const Ico = rail.icon;
              return (
                <section key={rail.key} className="mt-9 first:mt-2">
                  <div className="flex items-center gap-3 mb-4">
                    <Ico size={15} strokeWidth={2} className="text-mac-accentHi shrink-0" />
                    <h2 className="text-[12px] font-semibold uppercase tracking-[0.16em] text-mac-ink2 shrink-0">{rail.label}</h2>
                    <div className="flex-1 h-px bg-gradient-to-r from-mac-stroke to-transparent" />
                    <button onClick={() => ask(rail.verb)} title={`Ask Himmy to ${rail.verb}`}
                      className="text-[11.5px] text-mac-ink3 hover:text-mac-accentHi transition-colors shrink-0">More →</button>
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3.5">
                    {picks.map((p) => (
                      <DoPickCard key={p.key} p={p} rail={rail.key} action={rail.action}
                        onDismiss={() => dismiss(rail.key, p)}
                        onOpen={rail.key === "food" ? () => setDetail(p) : rail.key === "flights" ? () => openFlight(p) : undefined}
                        onAdd={(rail.key === "deals" || rail.key === "foryou") ? () => addProduct(p) : undefined} />
                    ))}
                  </div>
                </section>
              );
            })}
            </div>
          )}
        </div>
      </div>

      {detail && (
        <RestaurantModal pick={detail} onClose={() => setDetail(null)}
          onAdded={(v) => { setCart(v); }} onOpenTray={() => { setDetail(null); setTrayOpen(true); }} />
      )}
      {flightRoute && (
        <FlightModal route={flightRoute} onClose={() => setFlightRoute(null)} />
      )}
      {/* (round-trip is carried on flightRoute.returnDate; one-way when absent) */}
      {busRoute && (
        <BusModal route={busRoute} onClose={() => setBusRoute(null)} />
      )}
      <datalist id="bus-cities">{busCities.map((c) => <option key={c} value={c} />)}</datalist>
      {tripOpen && (
        <TripModal dest={tripOpen.dest} days={tripOpen.days} style={tripOpen.style} date={tripOpen.date} onClose={() => setTripOpen(null)}
          onFlights={(from, to) => { setTripOpen(null); setFlightRoute({ from, to, date: tripOpen.date || defaultDate }); }}
          onBuses={(from, to) => { setTripOpen(null); setBusRoute({ from, to, date: tripOpen.date || busDefaultDate }); }} />
      )}
      {trayOpen && (
        <DoTray cart={cart} onClose={() => setTrayOpen(false)} onChange={setCart} />
      )}
    </div>
  );
}

// A real error state (the api call THREW) with a Retry — distinct from a friendly "nothing
// here yet" empty state. Used by the board, the inline search, and any place a fetch can fail.
function DoErrorState({ message, onRetry, className = "h-40" }: {
  message: string; onRetry: () => void; className?: string;
}) {
  return (
    <div className={`${className} grid place-items-center text-center`}>
      <div>
        <div className="mx-auto mb-3 h-9 w-9 grid place-items-center rounded-full bg-red-500/10 ring-1 ring-inset ring-red-500/25">
          <Info size={16} className="text-red-400" />
        </div>
        <p className="text-[13px] text-mac-ink2 max-w-[40ch] mx-auto leading-snug">{message}</p>
        <button onClick={onRetry}
          className="mt-3.5 h-8 px-4 rounded-[9px] text-[12px] font-medium inline-flex items-center gap-1.5 bg-white/[0.06] ring-1 ring-inset ring-white/12 text-mac-ink hover:bg-white/[0.1] transition-colors">
          <RefreshCw size={13} /> Retry
        </button>
      </div>
    </div>
  );
}

// A DISH search result (Foodmandu): the dish, its restaurant + rating, price, any discount/offer,
// and quick actions — open the restaurant's menu to order, or drop the dish into the tray.
function DoDishCard({ p, onOpen, onAdd }: { p: DoPick; onOpen: () => void; onAdd: () => void }) {
  const btn = "h-9 flex-1 px-3 rounded-[10px] text-[12px] font-semibold inline-flex items-center justify-center gap-1.5 text-white bg-gradient-to-b from-mac-accentHi to-mac-accent ring-1 ring-inset ring-white/15 shadow-[0_3px_12px_-3px_rgba(10,132,255,0.55)] hover:brightness-[1.06] transition-all";
  return (
    <div onClick={onOpen}
      className="group relative flex flex-col rounded-2xl overflow-hidden bg-gradient-to-b from-white/[0.055] to-white/[0.018] border border-white/[0.07] shadow-[0_1px_2px_rgba(0,0,0,0.35)] hover:border-white/[0.15] hover:shadow-[0_18px_40px_-16px_rgba(0,0,0,0.8)] hover:-translate-y-0.5 transition-all duration-300 cursor-pointer">
      <div className="relative h-36 w-full overflow-hidden bg-mac-fillHi">
        {p.image
          ? <img src={p.image} alt="" loading="lazy" onError={(e) => { e.currentTarget.style.display = "none"; }}
              className="h-full w-full object-cover group-hover:scale-[1.06] transition-transform duration-[600ms] ease-out" />
          : <div className="h-full w-full grid place-items-center text-mac-ink3"><UtensilsCrossed size={22} /></div>}
        <div className="absolute inset-x-0 bottom-0 h-14 bg-gradient-to-t from-black/45 to-transparent pointer-events-none" />
        {p.discount && (
          <span className="absolute top-2 left-2 px-2 py-0.5 rounded-full bg-black/55 backdrop-blur-md ring-1 ring-white/10 text-mac-green text-[10.5px] font-semibold tracking-wide">{p.discount}</span>
        )}
        <div className="absolute top-2 right-2 flex items-center gap-1.5">
          {p.tag && <span className="px-2 py-0.5 rounded-full bg-black/55 backdrop-blur-md ring-1 ring-white/10 text-amber-300 text-[10px] font-semibold">★ {p.tag}</span>}
          <span className={`px-2 py-0.5 rounded-full bg-black/55 backdrop-blur-md ring-1 ring-white/10 text-[10px] font-semibold ${p.open_now ? "text-mac-green" : "text-white/55"}`}>{p.open_now ? "Open" : "Closed"}</span>
        </div>
      </div>
      <div className="flex flex-col flex-1 p-3.5">
        <div className="text-[13.5px] text-mac-ink font-medium leading-snug line-clamp-2 tracking-[-0.005em]">{p.title}</div>
        <div className="flex items-center gap-1.5 text-[11px] text-mac-ink3 mt-1">
          {typeof p.rating === "number" && p.rating > 0 && (
            <span className="inline-flex items-center gap-1 text-mac-ink2 font-medium"><Star size={10.5} className="text-amber-400 fill-amber-400" /> {p.rating.toFixed(1)}</span>
          )}
          {p.meta && <span className="truncate">· {p.meta}</span>}
        </div>
        <div className="flex items-baseline gap-1.5 mt-1.5">
          <span className="font-semibold text-mac-ink text-[15px] tracking-[-0.01em]">{p.subtitle}</span>
          {p.was && <span className="text-[11.5px] text-mac-ink3 line-through">{p.was}</span>}
        </div>
        {p.promo && (
          <div className="flex items-start gap-1.5 mt-2">
            <Tag size={11} className="text-mac-green shrink-0 mt-0.5" />
            <p className="text-[11px] leading-snug line-clamp-2 text-mac-ink3">{p.promo}</p>
          </div>
        )}
        <div className="mt-auto pt-3.5 flex items-center gap-2">
          <button onClick={(e) => { e.stopPropagation(); onOpen(); }} className={btn}>View menu <ArrowUpRight size={13} strokeWidth={2.5} /></button>
          <button onClick={(e) => { e.stopPropagation(); onAdd(); }} title="Add this dish to your tray"
            className="h-9 w-9 shrink-0 grid place-items-center rounded-[10px] bg-white/[0.06] ring-1 ring-inset ring-white/10 text-mac-ink2 hover:text-mac-ink hover:bg-white/[0.1] transition-colors">
            <Plus size={15} strokeWidth={2.5} />
          </button>
        </div>
      </div>
    </div>
  );
}

function DoPickCard({ p, rail, action, onDismiss, onOpen, onAdd }: {
  p: DoPick; rail: DoRailKey; action: string;
  onDismiss?: () => void; onOpen?: () => void; onAdd?: () => void;
}) {
  const isFlight = rail === "flights";
  const RailIcon = rail === "food" ? UtensilsCrossed : ShoppingBag;
  const btn = "h-9 flex-1 px-3 rounded-[10px] text-[12px] font-semibold inline-flex items-center justify-center gap-1.5 text-white bg-gradient-to-b from-mac-accentHi to-mac-accent ring-1 ring-inset ring-white/15 shadow-[0_3px_12px_-3px_rgba(10,132,255,0.55)] hover:shadow-[0_5px_18px_-3px_rgba(10,132,255,0.7)] hover:brightness-[1.06] transition-all";
  return (
    <div onClick={onOpen}
      className={`group relative flex flex-col rounded-2xl overflow-hidden bg-gradient-to-b from-white/[0.055] to-white/[0.018] border border-white/[0.07] shadow-[0_1px_2px_rgba(0,0,0,0.35)] hover:border-white/[0.15] hover:shadow-[0_18px_40px_-16px_rgba(0,0,0,0.8)] hover:-translate-y-0.5 transition-all duration-300 ${onOpen ? "cursor-pointer" : ""}`}>
      {/* media — a photo for food/deals, a route banner for flights */}
      <div className="relative h-36 w-full overflow-hidden bg-mac-fillHi">
        {isFlight ? (
          <div className="relative h-full w-full bg-gradient-to-br from-[rgba(10,132,255,0.28)] via-[rgba(10,132,255,0.08)] to-transparent">
            <div className="absolute inset-0 grid place-items-center"><Plane size={22} className="text-mac-accentHi/55 -mt-4" strokeWidth={1.75} /></div>
            <div className="absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-black/55 to-transparent" />
            <div className="absolute bottom-3 left-3.5 text-[17px] font-semibold text-white tracking-[-0.01em]">{p.title}</div>
          </div>
        ) : (
          <>
            {p.image
              ? <img src={p.image} alt="" loading="lazy" onError={(e) => { e.currentTarget.style.display = "none"; }}
                  className="h-full w-full object-cover group-hover:scale-[1.06] transition-transform duration-[600ms] ease-out" />
              : <div className="h-full w-full grid place-items-center text-mac-ink3"><RailIcon size={22} /></div>}
            <div className="absolute inset-x-0 bottom-0 h-14 bg-gradient-to-t from-black/45 to-transparent pointer-events-none" />
          </>
        )}
        {onDismiss && (
          <button onClick={(e) => { e.stopPropagation(); onDismiss(); }} title="Not for me — show less like this"
            className="absolute top-2 right-2 z-10 h-6 w-6 grid place-items-center rounded-full bg-black/40 backdrop-blur-md ring-1 ring-white/10 text-white/80 hover:text-white opacity-0 group-hover:opacity-100 transition-opacity">
            <ThumbsDown size={11} />
          </button>
        )}
        {rail === "deals" && p.discount && (
          <span className="absolute top-2 left-2 px-2 py-0.5 rounded-full bg-black/45 backdrop-blur-md ring-1 ring-white/10 text-mac-green text-[10.5px] font-semibold tracking-wide">{p.discount}</span>
        )}
        {rail === "food" && (
          <span className={`absolute top-2 left-2 px-2 py-0.5 rounded-full bg-black/45 backdrop-blur-md ring-1 ring-white/10 text-[10.5px] font-semibold tracking-wide ${
            p.open_now ? "text-mac-green" : "text-white/55"}`}>
            {p.open_now ? "Open now" : "Closed"}
          </span>
        )}
      </div>

      {/* body */}
      <div className="flex flex-col flex-1 p-3.5">
        {/* rating / sold line (not for flights) */}
        {!isFlight && (typeof p.rating === "number" && p.rating > 0 || (rail === "deals" && p.meta)) && (
          <div className="flex items-center gap-1.5 text-[11px] text-mac-ink3 mb-1.5">
            {typeof p.rating === "number" && p.rating > 0 && (
              <span className="inline-flex items-center gap-1 text-mac-ink2 font-medium">
                <Star size={11} className="text-amber-400 fill-amber-400" /> {p.rating.toFixed(1)}
              </span>
            )}
            {rail === "deals" && p.meta && <span className="truncate">· {p.meta}</span>}
          </div>
        )}
        {/* title (route is already in the flight banner) */}
        {!isFlight && (
          <div className="text-[13.5px] text-mac-ink font-medium leading-snug line-clamp-2 tracking-[-0.005em]">{p.title}</div>
        )}
        {/* price / fare */}
        {p.subtitle && rail !== "food" && (
          <div className="flex items-baseline gap-1.5 mt-1.5">
            <span className={`font-semibold text-mac-ink ${isFlight ? "text-[17px]" : "text-[15px]"} tracking-[-0.01em]`}>{p.subtitle}</span>
            {rail === "deals" && p.was && <span className="text-[11.5px] text-mac-ink3 line-through">{p.was}</span>}
            {isFlight && p.meta && <span className="text-[11px] text-mac-ink3">· {p.meta}</span>}
          </div>
        )}
        {/* why — sparkle only when it's Himmy's own reasoning */}
        {p.why && (!isFlight || p.ai) && (
          <div className="flex items-start gap-1.5 mt-2">
            {p.ai && <Sparkles size={11} strokeWidth={2} className="text-mac-accentHi shrink-0 mt-0.5" />}
            <p className={`text-[11.5px] leading-snug line-clamp-2 ${p.ai ? "text-mac-ink2" : "text-mac-ink3"}`}>{p.why}</p>
          </div>
        )}
        <div className="mt-auto pt-3.5 flex items-center gap-2">
          {onOpen ? (
            <button onClick={(e) => { e.stopPropagation(); onOpen(); }} className={btn}>
              {action} <ArrowUpRight size={13} strokeWidth={2.5} />
            </button>
          ) : (
            <a href={p.link || "#"} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} className={btn}>
              {action} <ArrowUpRight size={13} strokeWidth={2.5} />
            </a>
          )}
          {onAdd && (
            <button onClick={(e) => { e.stopPropagation(); onAdd(); }} title="Add to tray"
              className="h-9 w-9 shrink-0 grid place-items-center rounded-[10px] bg-white/[0.06] ring-1 ring-inset ring-white/10 text-mac-ink2 hover:text-mac-ink hover:bg-white/[0.1] transition-colors">
              <Plus size={15} strokeWidth={2.5} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────────────────────── do: restaurant menu modal + tray */
function RestaurantModal({ pick, onClose, onAdded, onOpenTray }: {
  pick: DoPick; onClose: () => void; onAdded: (v: DoCartView) => void; onOpenTray: () => void;
}) {
  const [data, setData] = useState<DoRestaurant | null>(null);
  const [loading, setLoading] = useState(true);
  const [added, setAdded] = useState<Record<string, boolean>>({});

  useEffect(() => {
    let alive = true;
    setLoading(true);
    api.do.restaurant({ id: pick.vendor_id, name: pick.vendor_id ? undefined : pick.title })
      .then((d) => { if (alive) setData(d.ok ? d : null); })
      .catch(() => { if (alive) setData(null); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [pick]);

  const addDish = async (it: DoMenuItem) => {
    try {
      const v = await api.do.cart.add({
        key: `f${data?.vendor_id}-${it.id}`, name: it.name, price: it.price || 0, source: "food",
        place: data?.restaurant || pick.title, image: it.image,
        link: data?.order_link, checkout_link: data?.order_link,
      });
      onAdded(v);
      setAdded((m) => ({ ...m, [it.id]: true }));
      setTimeout(() => setAdded((m) => ({ ...m, [it.id]: false })), 1200);
    } catch { /* ignore */ }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6 bg-black/40 backdrop-blur-sm" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()}
        className="w-[760px] max-w-[calc(100%-2rem)] h-[80vh] max-h-[720px] flex flex-col rounded-2xl bg-[rgba(30,31,37,0.98)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden">
        {/* header */}
        <div className="relative shrink-0 h-32 bg-mac-fillHi overflow-hidden">
          {pick.image
            ? <img src={pick.image} alt="" className="h-full w-full object-cover" onError={(e) => { e.currentTarget.style.display = "none"; }} />
            : <div className="h-full w-full grid place-items-center text-mac-ink3"><UtensilsCrossed size={24} /></div>}
          <div className="absolute inset-0 bg-gradient-to-t from-[rgba(20,21,26,0.96)] to-transparent" />
          <button onClick={onClose} className="absolute top-2.5 right-2.5 h-7 w-7 grid place-items-center rounded-full bg-black/45 backdrop-blur text-white/85 hover:text-white">
            <X size={15} />
          </button>
          <div className="absolute bottom-2.5 left-4 right-4">
            <h2 className="font-display text-[19px] font-semibold text-white tracking-[-0.01em] drop-shadow">{data?.restaurant || pick.title}</h2>
            <div className="flex items-center gap-2 text-[11.5px] text-white/80 mt-0.5">
              {typeof pick.rating === "number" && pick.rating > 0 && (
                <span className="inline-flex items-center gap-0.5"><Star size={10} className="fill-amber-400 text-amber-400" /> {pick.rating.toFixed(1)}</span>
              )}
              {data?.item_count ? <span>· {data.item_count} items</span> : null}
              <span className={pick.open_now ? "text-mac-green" : "text-white/60"}>· {pick.open_now ? "Open now" : "Closed"}</span>
            </div>
          </div>
        </div>

        {/* menu */}
        <div className="flex-1 min-h-0 overflow-y-auto px-4 py-4">
          {loading ? (
            <div className="h-40 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
          ) : !data ? (
            <div className="h-40 grid place-items-center text-center text-[13px] text-mac-ink2">Couldn't load this menu. <br />Open it on Foodmandu instead.</div>
          ) : (
            <>
              {data.recommended.length > 0 && (
                <section className="mb-5">
                  <div className="flex items-center gap-1.5 mb-2 text-[12px] font-semibold text-mac-accentHi">
                    <Sparkles size={12} /> Recommended for you
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    {data.recommended.map((it) => (
                      <MenuItemRow key={`r${it.id}`} it={it} added={!!added[it.id]} onAdd={() => addDish(it)} highlight />
                    ))}
                  </div>
                </section>
              )}
              {data.categories.map((cat) => (
                <section key={cat.category} className="mb-5">
                  <h3 className="text-[13px] font-semibold text-mac-ink mb-2">{cat.category}</h3>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    {cat.items.map((it) => (
                      <MenuItemRow key={it.id} it={it} added={!!added[it.id]} onAdd={() => addDish(it)} />
                    ))}
                  </div>
                </section>
              ))}
            </>
          )}
        </div>

        {/* footer */}
        <div className="shrink-0 flex items-center justify-between gap-3 px-4 py-3 border-t border-mac-stroke">
          <button onClick={onOpenTray} className="text-[12.5px] text-mac-ink2 hover:text-mac-ink inline-flex items-center gap-1.5">
            <ShoppingCart size={14} /> View tray
          </button>
          <a href={data?.order_link || pick.link || "#"} target="_blank" rel="noreferrer"
            className="h-9 px-4 rounded-[10px] text-[12.5px] font-medium inline-flex items-center gap-1.5 bg-mac-accent text-white hover:bg-mac-accentHi transition-colors">
            Order on Foodmandu <ArrowUpRight size={14} strokeWidth={2.5} />
          </a>
        </div>
      </div>
    </div>
  );
}

function MenuItemRow({ it, added, onAdd, highlight }: {
  it: DoMenuItem; added: boolean; onAdd: () => void; highlight?: boolean;
}) {
  return (
    <div className={`flex items-center gap-2.5 p-2 rounded-lg border transition-colors ${
      highlight ? "border-mac-accentHi/30 bg-[rgba(10,132,255,0.06)]" : "border-mac-stroke bg-mac-fill"}`}>
      {it.image
        ? <img src={it.image} alt="" loading="lazy" onError={(e) => { e.currentTarget.style.display = "none"; }}
            className="h-11 w-11 shrink-0 rounded-md object-cover bg-mac-fillHi" />
        : <div className="h-11 w-11 shrink-0 rounded-md grid place-items-center bg-mac-fillHi text-mac-ink3"><UtensilsCrossed size={15} /></div>}
      <div className="min-w-0 flex-1">
        <div className="text-[12.5px] text-mac-ink font-medium leading-snug line-clamp-1">{it.name}</div>
        {it.desc && <div className="text-[11px] text-mac-ink3 leading-snug line-clamp-1">{it.desc}</div>}
        <div className="flex items-baseline gap-1.5 mt-0.5">
          <span className="text-[12.5px] font-semibold text-mac-ink">Rs {it.price}</span>
          {it.was ? <span className="text-[10.5px] text-mac-ink3 line-through">Rs {it.was}</span> : null}
        </div>
      </div>
      <button onClick={onAdd} title="Add to tray"
        className={`h-7 w-7 shrink-0 grid place-items-center rounded-lg transition-colors ${
          added ? "bg-mac-green/20 text-mac-green" : "bg-mac-accent text-white hover:bg-mac-accentHi"}`}>
        {added ? <Check size={14} strokeWidth={2.5} /> : <Plus size={14} strokeWidth={2.5} />}
      </button>
    </div>
  );
}

function tripCatTint(c: string): string {
  const m: Record<string, string> = {
    Nature: "bg-mac-green/15 text-mac-green",
    Adventure: "bg-mac-red/15 text-mac-red",
    Food: "bg-amber-400/15 text-amber-400",
    Culture: "bg-[rgba(64,156,255,0.16)] text-mac-accentHi",
    Relax: "bg-[rgba(64,156,255,0.12)] text-mac-accentHi",
    Shopping: "bg-mac-fillHi text-mac-ink2",
  };
  return m[c] || "bg-mac-fillHi text-mac-ink2";
}

function TripItem({ it }: { it: DoTripItem }) {
  return (
    <div className="relative">
      <div className="absolute -left-[21px] top-[7px] h-2 w-2 rounded-full bg-mac-accentHi ring-2 ring-[rgba(30,31,37,0.98)]" />
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[13px] font-medium text-mac-ink">{it.name}</span>
        {it.category && <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded-md ${tripCatTint(it.category)}`}>{it.category}</span>}
      </div>
      {it.desc && <p className="text-[12px] text-mac-ink2 leading-snug mt-0.5">{it.desc}</p>}
      {it.tip && (
        <p className="text-[11px] text-mac-ink3 mt-1 flex gap-1.5 leading-snug">
          <Lightbulb size={11} className="text-amber-400 shrink-0 mt-0.5" /> {it.tip}
        </p>
      )}
    </div>
  );
}

function TripDay({ day }: { day: DoTripDay }) {
  return (
    <section className="mb-5">
      <div className="flex items-center gap-2.5 mb-2.5">
        <div className="h-7 w-7 shrink-0 grid place-items-center rounded-full bg-gradient-to-b from-mac-accentHi to-mac-accent text-white text-[12px] font-semibold ring-1 ring-inset ring-white/15">{day.day}</div>
        <h3 className="text-[14px] font-semibold text-mac-ink tracking-[-0.01em]">{day.title}</h3>
      </div>
      <div className="ml-3.5 pl-4 border-l border-mac-stroke space-y-3">
        {(day.items || []).map((it, i) => <TripItem key={i} it={it} />)}
      </div>
    </section>
  );
}

function tripNum(n?: number): string { return (n || 0).toLocaleString(); }

function TripSection({ icon: Icon, label, children }: { icon: LucideIcon; label: string; children: React.ReactNode }) {
  return (
    <section className="mb-5">
      <div className="flex items-center gap-2 mb-3">
        <Icon size={14} className="text-mac-accentHi shrink-0" />
        <h3 className="text-[12px] font-semibold uppercase tracking-[0.14em] text-mac-ink2 shrink-0">{label}</h3>
        <div className="flex-1 h-px bg-gradient-to-r from-mac-stroke to-transparent" />
      </div>
      {children}
    </section>
  );
}

function TripBudget({ budget }: { budget: NonNullable<DoTrip["budget"]> }) {
  const cur = budget.currency || "NPR";
  const rows = budget.breakdown || [];
  const max = Math.max(...rows.map((r) => r.max || 0), 1);
  return (
    <div className="mb-5 rounded-2xl border border-mac-strokeHi bg-gradient-to-b from-white/[0.05] to-white/[0.012] p-4">
      <div className="flex items-center justify-between mb-3.5">
        <div className="flex items-center gap-1.5 text-[12px] font-semibold uppercase tracking-[0.14em] text-mac-ink2"><Wallet size={14} className="text-mac-accentHi" /> Estimated budget</div>
        <div className="text-right">
          <div className="text-[18px] font-semibold text-mac-ink tracking-[-0.01em]">{cur} {tripNum(budget.total_min)}–{tripNum(budget.total_max)}</div>
          {budget.per_person && <div className="text-[10.5px] text-mac-ink3">per person</div>}
        </div>
      </div>
      <div className="space-y-2.5">
        {rows.map((r, i) => (
          <div key={i}>
            <div className="flex items-center justify-between text-[11.5px] mb-1">
              <span className="text-mac-ink2 truncate pr-2">{r.label}{r.note ? <span className="text-mac-ink3"> · {r.note}</span> : null}</span>
              <span className="text-mac-ink tnum shrink-0">{cur} {tripNum(r.min)}{r.max !== r.min ? `–${tripNum(r.max)}` : ""}</span>
            </div>
            <div className="h-1.5 rounded-full bg-white/[0.05] overflow-hidden">
              <div className="h-full rounded-full bg-gradient-to-r from-mac-accentHi to-mac-accent" style={{ width: `${Math.max(6, Math.round(((r.max || 0) / max) * 100))}%` }} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function TripHotelRow({ h }: { h: DoTripHotel }) {
  return (
    <div className="flex items-center gap-3 p-3 rounded-xl border border-mac-stroke bg-mac-fill">
      <div className="h-9 w-9 shrink-0 grid place-items-center rounded-lg bg-mac-fillHi"><BedDouble size={15} className="text-mac-accentHi" /></div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[13px] font-medium text-mac-ink">{h.name}</span>
          {h.type && <span className="text-[10px] px-1.5 py-0.5 rounded-md bg-mac-fillHi text-mac-ink3 capitalize">{h.type}</span>}
          {h.area && <span className="text-[11px] text-mac-ink3">· {h.area}</span>}
        </div>
        {h.why && <p className="text-[11.5px] text-mac-ink3 leading-snug mt-0.5 line-clamp-2">{h.why}</p>}
      </div>
      {h.book_link && (
        <a href={h.book_link} target="_blank" rel="noreferrer"
          className="h-8 px-3 shrink-0 rounded-[9px] text-[12px] font-medium bg-mac-accent text-white hover:bg-mac-accentHi transition-colors inline-flex items-center gap-1.5">
          Book <ArrowUpRight size={12} strokeWidth={2.5} />
        </a>
      )}
    </div>
  );
}

function TripEatRow({ e }: { e: DoTripEat }) {
  return (
    <div className="flex items-start gap-2.5 p-2.5 rounded-xl border border-mac-stroke bg-mac-fill">
      <div className="h-7 w-7 shrink-0 grid place-items-center rounded-lg bg-mac-fillHi"><UtensilsCrossed size={13} className="text-amber-400" /></div>
      <div className="min-w-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-[12.5px] font-medium text-mac-ink">{e.name}</span>
          {e.cuisine && <span className="text-[10px] text-mac-ink3 capitalize">{e.cuisine}</span>}
        </div>
        {e.why && <p className="text-[11px] text-mac-ink3 leading-snug mt-0.5 line-clamp-2">{e.why}</p>}
      </div>
    </div>
  );
}

// A compact, premium side-by-side of flight vs bus (fare + duration) with Himmy's verdict line.
// The flight duration is door-to-door ESTIMATE (air time + airport buffer), so it's marked "est.";
// the bus journey_hours is real. Derived deterministically on the backend (no extra calls).
function TripTransportCompare({ cmp }: { cmp: DoTripTransportCompare }) {
  const flight = cmp.options.find((o) => o.mode === "flight");
  const bus = cmp.options.find((o) => o.mode === "bus");
  if (!flight || !bus) return null;
  const opt = (o: typeof flight, isWinner: boolean) => {
    const Ico = o.mode === "flight" ? Plane : Bus;
    return (
      <div className={`flex-1 min-w-0 p-3 rounded-xl border ${
        isWinner ? "border-mac-accentHi/35 bg-[rgba(10,132,255,0.07)]" : "border-mac-stroke bg-mac-fill"}`}>
        <div className="flex items-center gap-2 mb-2">
          <div className={`h-7 w-7 shrink-0 grid place-items-center rounded-lg ${o.mode === "flight" ? "bg-[rgba(10,132,255,0.12)]" : "bg-white/[0.06]"}`}>
            <Ico size={13} className={o.mode === "flight" ? "text-mac-accentHi" : "text-mac-ink2"} />
          </div>
          <div className="text-[12px] font-medium text-mac-ink truncate">{o.label}</div>
          {isWinner && <span className="ml-auto shrink-0 text-[9.5px] font-semibold uppercase tracking-[0.1em] text-mac-accentHi px-1.5 py-0.5 rounded-md bg-[rgba(10,132,255,0.12)]">Pick</span>}
        </div>
        <div className="flex items-end justify-between gap-2">
          <div>
            <div className="text-[16px] font-semibold text-mac-ink tracking-[-0.01em] tnum">Rs {Math.round(o.fare_npr).toLocaleString()}</div>
            {o.depart && <div className="text-[10px] text-mac-ink3 mt-0.5">departs {o.depart}</div>}
          </div>
          <div className="text-right shrink-0">
            <div className="text-[12.5px] text-mac-ink2 tnum inline-flex items-center gap-1">
              <Clock size={10} className="text-mac-ink3" />{o.duration_label}
            </div>
            {o.duration_is_estimate && <div className="text-[9.5px] text-mac-ink3 italic">est.</div>}
          </div>
        </div>
      </div>
    );
  };
  return (
    <div className="mb-5">
      <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-[0.16em] text-mac-ink2 mb-2.5">
        <ArrowRightLeft size={12} className="text-mac-accentHi" /> Fly vs bus
      </div>
      <div className="flex items-stretch gap-2.5">
        {opt(flight, cmp.verdict.winner === "flight")}
        {opt(bus, cmp.verdict.winner === "bus")}
      </div>
      <div className="mt-2.5 flex items-start gap-2 p-2.5 rounded-xl border border-mac-stroke bg-mac-fill">
        <Sparkles size={13} className="text-mac-accentHi shrink-0 mt-0.5" />
        <div className="min-w-0">
          <div className="text-[12px] text-mac-ink leading-snug">{cmp.verdict.reason}</div>
          {cmp.verdict.time_note && <div className="text-[11px] text-mac-ink3 leading-snug mt-0.5">{cmp.verdict.time_note}</div>}
        </div>
      </div>
      <p className="text-[10px] text-mac-ink3/80 italic mt-1.5 px-0.5">{cmp.disclaimer}</p>
    </div>
  );
}

// A short, friendly date label for a weather chip — "Mon 14".
function weatherDayLabel(iso: string): string {
  const d = new Date(iso + "T00:00:00");
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
}

// One weather chip — emoji + day, Hi/Lo, and a rain-% line that only shows when it's worth a
// raincoat. Today (first in-window day) gets a subtle accent ring.
function WeatherChip({ d, today }: { d: DoWeatherDay; today: boolean }) {
  const rainy = d.rain_pct >= 30;
  return (
    <div className={`shrink-0 w-[88px] p-2.5 rounded-xl border text-center ${
      today ? "border-mac-accentHi/35 bg-[rgba(10,132,255,0.07)]" : "border-mac-stroke bg-mac-fill"}`}>
      <div className="text-[10px] font-medium text-mac-ink3 mb-1">{weatherDayLabel(d.date)}</div>
      <div className="text-[20px] leading-none mb-1" title={d.label}>{d.emoji}</div>
      <div className="text-[12.5px] font-semibold text-mac-ink tnum leading-none">
        {Math.round(d.t_max)}°<span className="text-mac-ink3 font-normal"> / {Math.round(d.t_min)}°</span>
      </div>
      <div className={`mt-1.5 text-[10px] inline-flex items-center gap-0.5 ${rainy ? "text-mac-accentHi" : "text-mac-ink3"}`}>
        <Umbrella size={9} /> {d.rain_pct}%
      </div>
    </div>
  );
}

// The premium WEATHER STRIP shown at the top of a trip plan. When the requested dates fall inside
// Open-Meteo's ~16-day horizon it renders the real per-day forecast; otherwise it stays HONEST —
// leading with the Nepal seasonal pattern and a muted "forecast available closer to your dates".
function WeatherStrip({ w }: { w: DoWeather }) {
  const inWindow = w.in_forecast_window && (w.daily || []).length > 0;
  const cur = w.current;
  return (
    <div className="mb-5 rounded-xl border border-mac-stroke bg-mac-fill p-3.5">
      <div className="flex items-center gap-2 mb-2.5">
        <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-[0.16em] text-mac-ink2">
          {inWindow ? <Sun size={12} className="text-amber-400" /> : <CloudRain size={12} className="text-mac-accentHi" />} Weather
        </div>
        {inWindow && cur && (
          <span className="ml-auto inline-flex items-center gap-1.5 text-[11.5px] text-mac-ink2">
            <span className="text-[14px] leading-none" title={cur.label}>{cur.emoji}</span>
            <span className="font-semibold text-mac-ink tnum">{Math.round(cur.temp_c)}°</span>
            <span className="text-mac-ink3">now · {cur.humidity}% hum · {Math.round(cur.wind_kmh)} km/h</span>
          </span>
        )}
      </div>
      {/* Honest summary line — a real forecast read when in-window, else the seasonal pattern. */}
      <p className="text-[12.5px] text-mac-ink leading-snug">{w.summary}</p>
      {/* Season pattern — always shown; it's the steady context behind any forecast. */}
      {w.season && (
        <p className="text-[11px] text-mac-ink3 leading-snug mt-1 flex items-start gap-1.5">
          <Calendar size={11} className="text-mac-ink3 shrink-0 mt-[1px]" /> {w.season}
        </p>
      )}
      {inWindow ? (
        <div className="mt-3 flex gap-2 overflow-x-auto pb-1 -mx-0.5 px-0.5">
          {w.daily.map((d, i) => <WeatherChip key={d.date} d={d} today={i === 0} />)}
        </div>
      ) : (
        <div className="mt-2.5 flex items-center gap-2 text-[11px] text-mac-ink3/85 italic">
          <Clock size={11} className="shrink-0" /> Day-by-day forecast available closer to your dates.
        </div>
      )}
    </div>
  );
}

function TripModal({ dest, days, style, date, onClose, onFlights, onBuses }: {
  dest: string; days: number; style: string; date?: string; onClose: () => void;
  onFlights: (from: string, to: string) => void; onBuses: (from: string, to: string) => void;
}) {
  const [data, setData] = useState<DoTrip | null>(null);
  const [loading, setLoading] = useState(true);
  const [shareOpen, setShareOpen] = useState(false);
  useEffect(() => {
    let alive = true; setLoading(true);
    // `date` (depart) makes the backend attach a real weather forecast + round-trip totals; the
    // call stays valid without it (backend defaults to ~a week out).
    api.do.trip(dest, days, style, date)
      .then((d) => { if (alive) setData(d.ok ? d : null); })
      .catch(() => { if (alive) setData(null); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [dest, days, style, date]);
  const gt = data?.getting_there;
  const bb = data?.by_bus;
  const cur = data?.budget?.currency || "NPR";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6 bg-black/45 backdrop-blur-sm" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()}
        className="w-[680px] max-w-[calc(100%-2rem)] h-[82vh] max-h-[760px] flex flex-col rounded-2xl bg-[rgba(30,31,37,0.98)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden">
        {/* header */}
        <div className="relative shrink-0 px-5 pt-5 pb-4 border-b border-mac-stroke">
          <button onClick={onClose} className="absolute top-3 right-3 h-7 w-7 grid place-items-center rounded-full bg-mac-fillHi text-mac-ink3 hover:text-mac-ink transition-colors"><X size={15} /></button>
          <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-[0.16em] text-mac-accentHi mb-2"><Route size={12} /> Trip roadmap</div>
          <h2 className="font-display text-[23px] font-semibold text-white tracking-[-0.015em]">{dest}</h2>
          <div className="flex items-center gap-2 mt-1.5">
            <span className="text-[12px] text-white/70">{days} day{days > 1 ? "s" : ""}</span>
            <span className="capitalize text-[10.5px] font-medium px-1.5 py-0.5 rounded-md bg-white/[0.08] text-white/85">{data?.style || style}</span>
          </div>
          {data?.summary && <p className="text-[12.5px] text-white/85 mt-2.5 max-w-[62ch] leading-snug">{data.summary}</p>}
        </div>
        {/* body */}
        <div className="flex-1 min-h-0 overflow-y-auto p-4">
          {loading ? (
            <div className="h-48 grid place-items-center text-center">
              <div>
                <Loader2 size={20} className="animate-spin text-mac-accentHi mx-auto mb-3" />
                <p className="text-[12.5px] text-mac-ink2">Planning your {style} trip to {dest}…</p>
              </div>
            </div>
          ) : !data ? (
            <div className="h-40 grid place-items-center text-center text-[13px] text-mac-ink2 px-6">
              Couldn't build a plan just now. Try again, or ask Himmy directly.
            </div>
          ) : (
            <>
              {data.weather && <WeatherStrip w={data.weather} />}
              {data.budget && (data.budget.total_min || (data.budget.breakdown || []).length > 0) && (
                <TripBudget budget={data.budget} />
              )}
              {gt && (
                <div className={`${bb ? "mb-2.5" : "mb-5"} flex items-center gap-3 p-3 rounded-xl border border-mac-accentHi/25 bg-[rgba(10,132,255,0.06)]`}>
                  <div className="h-8 w-8 shrink-0 grid place-items-center rounded-lg bg-[rgba(10,132,255,0.12)]"><Plane size={15} className="text-mac-accentHi" /></div>
                  <div className="flex-1 min-w-0">
                    <div className="text-[12.5px] text-mac-ink font-medium">Getting there · {gt.from} → {gt.to}</div>
                    {gt.cheapest && <div className="text-[11.5px] text-mac-ink3">{gt.cheapest.flight} · {gt.cheapest.depart} · from Rs {Math.round(gt.cheapest.fare_npr).toLocaleString()}</div>}
                  </div>
                  <button onClick={() => onFlights(gt.from, gt.to)}
                    className="h-8 px-3 shrink-0 rounded-[9px] text-[12px] font-medium bg-mac-accent text-white hover:bg-mac-accentHi transition-colors">See flights</button>
                </div>
              )}
              {bb && (
                <div className="mb-5 flex items-center gap-3 p-3 rounded-xl border border-mac-stroke bg-mac-fill">
                  <div className="h-8 w-8 shrink-0 grid place-items-center rounded-lg bg-white/[0.06]"><Bus size={15} className="text-mac-ink2" /></div>
                  <div className="flex-1 min-w-0">
                    <div className="text-[12.5px] text-mac-ink font-medium">{gt ? "Or by bus" : "Getting there by bus"} · {bb.from} → {bb.to}{bb.via ? ` (for ${bb.via.for})` : ""}{bb.count ? ` · ${bb.count} buses` : ""}</div>
                    {bb.cheapest && <div className="text-[11.5px] text-mac-ink3">{bb.cheapest.operator} · {bb.cheapest.depart} · from Rs {Math.round(bb.cheapest.fare_npr).toLocaleString()}{bb.cheapest.bus_type ? ` · ${bb.cheapest.bus_type}` : ""}</div>}
                    {bb.via?.note && <div className="text-[10.5px] text-mac-ink3/80 italic mt-0.5">{bb.via.note}</div>}
                  </div>
                  <button onClick={() => onBuses(bb.from, bb.to)}
                    className="h-8 px-3 shrink-0 rounded-[9px] text-[12px] font-medium bg-white/[0.08] text-mac-ink hover:bg-white/[0.14] transition-colors">See buses</button>
                </div>
              )}
              {data.transport_compare && <TripTransportCompare cmp={data.transport_compare} />}
              {data.hotels && data.hotels.length > 0 && (
                <TripSection icon={BedDouble} label="Where to stay">
                  <div className="space-y-2">{data.hotels.map((h, i) => <TripHotelRow key={i} h={h} />)}</div>
                </TripSection>
              )}
              <TripSection icon={Route} label="Day by day">
                {(data.itinerary || []).map((day) => <TripDay key={day.day} day={day} />)}
              </TripSection>
              {data.eat && data.eat.length > 0 && (
                <TripSection icon={UtensilsCrossed} label="Where to eat">
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">{data.eat.map((e, i) => <TripEatRow key={i} e={e} />)}</div>
                </TripSection>
              )}
              {data.tips && data.tips.length > 0 && (
                <div className="rounded-xl border border-mac-stroke bg-mac-fill p-3.5">
                  <div className="flex items-center gap-1.5 text-[12px] font-semibold text-mac-ink mb-2"><Lightbulb size={13} className="text-amber-400" /> Good to know</div>
                  <ul className="space-y-1.5">
                    {data.tips.map((t, i) => <li key={i} className="text-[12px] text-mac-ink2 flex gap-2 leading-snug"><span className="text-mac-ink3 shrink-0">•</span>{t}</li>)}
                  </ul>
                </div>
              )}
            </>
          )}
        </div>
        {/* footer */}
        <div className="shrink-0 flex items-center justify-between gap-3 px-5 py-3 border-t border-mac-stroke">
          <span className="text-[11px] text-mac-ink3 truncate">Grounded in real local spots · personalised by Himmy</span>
          <div className="flex items-center gap-2 shrink-0">
            <button onClick={() => setShareOpen(true)} disabled={!data}
              className="h-9 px-4 rounded-[10px] text-[12.5px] font-medium bg-mac-fill border border-mac-stroke text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors inline-flex items-center gap-1.5 disabled:opacity-40 disabled:pointer-events-none">
              <Share2 size={14} /> Share
            </button>
            <button onClick={() => { onClose(); ask(`Tweak my ${dest} trip plan`); }}
              className="h-9 px-4 rounded-[10px] text-[12.5px] font-medium bg-mac-fill border border-mac-stroke text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors inline-flex items-center gap-1.5">
              <MessageSquare size={14} /> Ask Himmy to tweak
            </button>
          </div>
        </div>
      </div>
      {shareOpen && (
        <TripShareSheet dest={dest} days={days} style={style} fallbackTitle={dest} onClose={() => setShareOpen(false)} />
      )}
    </div>
  );
}

// Share / export a trip as a SANITIZED shareable itinerary. The markdown comes from the backend
// (/do/trip/export) which strips the user's name/email and any profile-/vault-derived phrasing, so
// what leaves the app reads as a generic plan. Offers copy-to-clipboard + save-to-file + print/PDF
// (the print path uses Electron's Chromium renderer → "Save as PDF" in the native dialog).
function TripShareSheet({ dest, days, style, fallbackTitle, onClose }: {
  dest: string; days: number; style: string; fallbackTitle: string; onClose: () => void;
}) {
  const [md, setMd] = useState<string | null>(null);
  const [title, setTitle] = useState(fallbackTitle);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [copied, setCopied] = useState(false);

  const fetchExport = () => {
    setLoading(true); setError(false);
    api.do.tripExport(dest, days, style)
      .then((r) => {
        if (r.ok && typeof r.markdown === "string") { setMd(r.markdown); if (r.title) setTitle(r.title); }
        else setError(true);
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  };
  useEffect(fetchExport, [dest, days, style]);

  const copy = () => {
    if (!md) return;
    navigator.clipboard.writeText(md);
    setCopied(true); setTimeout(() => setCopied(false), 1500);
  };
  const save = () => {
    if (!md) return;
    const blob = new Blob([md], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${title.replace(/[^\w\- ]+/g, "").trim() || "trip"}.md`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  // Save / print to PDF: render the sanitized markdown into an isolated print window and invoke the
  // native print dialog (macOS → "Save as PDF"). We render the SAME sanitized markdown, not the
  // on-screen modal, so nothing personal can leak into the export.
  const printPdf = () => {
    if (!md) return;
    const w = window.open("", "_blank", "width=720,height=900");
    if (!w) return;
    const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${esc(title)}</title>
      <style>
        @page { margin: 18mm; }
        body { font: 13px/1.6 -apple-system, "SF Pro Text", system-ui, sans-serif; color: #1a1a1a; max-width: 680px; margin: 0 auto; padding: 8px; }
        pre { white-space: pre-wrap; word-wrap: break-word; font: inherit; }
        h1 { font-size: 22px; margin: 0 0 12px; }
      </style></head><body><pre>${esc(md)}</pre></body></html>`);
    w.document.close();
    w.focus();
    // Let layout settle, then open the print/PDF dialog.
    setTimeout(() => { try { w.print(); } catch { /* user closed it */ } }, 250);
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-6 bg-black/45 backdrop-blur-sm" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()}
        className="w-[600px] max-w-[calc(100%-2rem)] max-h-[80vh] flex flex-col rounded-2xl bg-[rgba(30,31,37,0.98)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden">
        <div className="relative shrink-0 px-5 pt-5 pb-4 border-b border-mac-stroke">
          <button onClick={onClose} className="absolute top-3 right-3 h-7 w-7 grid place-items-center rounded-full bg-mac-fillHi text-mac-ink3 hover:text-mac-ink transition-colors"><X size={15} /></button>
          <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-[0.16em] text-mac-accentHi mb-2"><Share2 size={12} /> Share trip</div>
          <h2 className="font-display text-[19px] font-semibold text-white tracking-[-0.015em]">{title}</h2>
          <p className="text-[11.5px] text-mac-ink3 mt-1">A clean, shareable plan — no personal details.</p>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto p-4">
          {loading ? (
            <div className="h-40 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
          ) : error ? (
            <DoErrorState message="Couldn't build the shareable plan just now." onRetry={fetchExport} />
          ) : (
            <pre className="text-[12px] leading-relaxed text-mac-ink2 whitespace-pre-wrap font-mono bg-black/20 ring-1 ring-inset ring-white/[0.06] rounded-xl p-3.5">{md}</pre>
          )}
        </div>
        <div className="shrink-0 flex items-center justify-between gap-2 px-5 py-3 border-t border-mac-stroke">
          <button onClick={copy} disabled={!md}
            className="h-9 px-4 rounded-[10px] text-[12.5px] font-medium bg-mac-fill border border-mac-stroke text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors inline-flex items-center gap-1.5 disabled:opacity-40 disabled:pointer-events-none">
            {copied ? <Check size={14} className="text-mac-green" /> : <Copy size={14} />} {copied ? "Copied" : "Copy markdown"}
          </button>
          <div className="flex items-center gap-2">
            <button onClick={save} disabled={!md}
              className="h-9 px-4 rounded-[10px] text-[12.5px] font-medium bg-mac-fill border border-mac-stroke text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors inline-flex items-center gap-1.5 disabled:opacity-40 disabled:pointer-events-none">
              <FileDown size={14} /> Save .md
            </button>
            <button onClick={printPdf} disabled={!md}
              className="h-9 px-4 rounded-[10px] text-[12.5px] font-semibold text-white bg-gradient-to-b from-mac-accentHi to-mac-accent ring-1 ring-inset ring-white/15 hover:brightness-[1.06] transition-all inline-flex items-center gap-1.5 disabled:opacity-40 disabled:pointer-events-none">
              <Printer size={14} /> Save / print PDF
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function FlightModal({ route, onClose }: { route: { from: string; to: string; date: string; returnDate?: string }; onClose: () => void }) {
  const [data, setData] = useState<DoFlights | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true; setLoading(true);
    // Pass route.returnDate to fetch a round-trip — the response then carries return_flights +
    // round_trip_total_npr. Omitting it keeps the one-way path exactly as before.
    api.do.flights(route.from, route.to, route.date, route.returnDate)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setData(null); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [route]);

  const flights = data?.flights || [];
  // The response is authoritative about round-trip-ness; fall back to the request only before data
  // lands (so the header reads correctly while loading).
  const roundTrip = data ? !!data.round_trip : !!route.returnDate;
  const returnFlights = data?.return_flights || [];
  const fmtDate = (iso: string) => {
    const d = new Date(iso + "T00:00:00");
    return isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  };
  const dateLabel = fmtDate(route.date);
  const returnLabel = route.returnDate ? fmtDate(route.returnDate) : (data?.return_date || null);
  const cheapest = data?.cheapest?.fare_npr;
  const rtTotal = data?.round_trip_total_npr;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6 bg-black/45 backdrop-blur-sm" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()}
        className="w-[640px] max-w-[calc(100%-2rem)] max-h-[82vh] flex flex-col rounded-2xl bg-[rgba(30,31,37,0.98)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden">
        {/* header banner */}
        <div className="relative shrink-0 px-5 pt-5 pb-4 border-b border-mac-stroke">
          <button onClick={onClose} className="absolute top-3 right-3 h-7 w-7 grid place-items-center rounded-full bg-mac-fillHi text-mac-ink3 hover:text-mac-ink transition-colors"><X size={15} /></button>
          <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-[0.16em] text-mac-accentHi mb-2">
            {roundTrip ? <ArrowRightLeft size={12} /> : <Plane size={12} />} Buddha Air · {roundTrip ? "round trip" : "one way"}
          </div>
          <div className="flex items-center gap-3 text-[23px] font-semibold text-white tracking-[-0.015em]">
            <span>{data?.from || route.from}</span>
            {roundTrip ? <ArrowRightLeft size={18} className="text-mac-accentHi" /> : <ArrowRight size={19} className="text-mac-accentHi" />}
            <span>{data?.to || route.to}</span>
          </div>
          <div className="text-[12px] text-white/70 mt-1.5">
            {dateLabel}{roundTrip && returnLabel ? ` → ${returnLabel}` : ""}
            {flights.length ? ` · ${flights.length} flight${flights.length === 1 ? "" : "s"}` : ""}
            {roundTrip
              ? (rtTotal ? ` · round trip from Rs ${Math.round(rtTotal).toLocaleString()}` : "")
              : (cheapest ? ` · from Rs ${Math.round(cheapest).toLocaleString()}` : "")}
          </div>
        </div>
        {/* flight list */}
        <div className="flex-1 min-h-0 overflow-y-auto p-3">
          {loading ? (
            <div className="h-40 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
          ) : !flights.length ? (
            <div className="h-40 grid place-items-center text-center text-[13px] text-mac-ink2 px-6">
              {data?.message || "No live fares came back — open Buddha Air to check this route."}
            </div>
          ) : (
            <>
              {roundTrip && (
                <div className="flex items-center gap-2 px-1 mb-2 text-[10.5px] font-semibold uppercase tracking-[0.14em] text-mac-ink3">
                  <Plane size={11} className="text-mac-accentHi" /> Outbound · {data?.from || route.from} → {data?.to || route.to}
                </div>
              )}
              {flights.map((f, i) => <FlightRow key={`out-${f.flight}-${f.depart}-${i}`} f={f} best={i === 0} bookLink={data?.booking_link} />)}
              {roundTrip && (
                <>
                  <div className="flex items-center gap-2 px-1 mt-4 mb-2 text-[10.5px] font-semibold uppercase tracking-[0.14em] text-mac-ink3">
                    <Plane size={11} className="text-mac-accentHi rotate-180" /> Return · {data?.to || route.to} → {data?.from || route.from}{returnLabel ? ` · ${returnLabel}` : ""}
                  </div>
                  {returnFlights.length ? (
                    returnFlights.map((f, i) => <FlightRow key={`ret-${f.flight}-${f.depart}-${i}`} f={f} best={i === 0} bookLink={data?.booking_link} />)
                  ) : (
                    <div className="px-3 py-5 text-center text-[12px] text-mac-ink3">No live return fares came back for that date.</div>
                  )}
                </>
              )}
            </>
          )}
        </div>
        {/* footer */}
        <div className="shrink-0 flex items-center justify-between gap-3 px-5 py-3 border-t border-mac-stroke">
          {roundTrip && rtTotal ? (
            <span className="text-[12px] text-mac-ink2">
              Round-trip total <span className="font-semibold text-mac-ink tnum">Rs {Math.round(rtTotal).toLocaleString()}</span>
              <span className="text-[10.5px] text-mac-ink3"> · cheapest each way</span>
            </span>
          ) : (
            <span className="text-[11px] text-mac-ink3">Live fares · you complete the booking on Buddha Air</span>
          )}
          <a href={data?.booking_link || "#"} target="_blank" rel="noreferrer"
            className="h-9 px-4 shrink-0 rounded-[10px] text-[12.5px] font-medium inline-flex items-center gap-1.5 bg-mac-accent text-white hover:bg-mac-accentHi transition-colors">
            Open Buddha Air <ArrowUpRight size={14} strokeWidth={2.5} />
          </a>
        </div>
      </div>
    </div>
  );
}

function FlightRow({ f, best, bookLink }: { f: DoFlight; best: boolean; bookLink?: string }) {
  return (
    <div className={`flex items-center gap-3 sm:gap-4 p-3 rounded-xl border mb-2 transition-colors ${
      best ? "border-mac-accentHi/30 bg-[rgba(10,132,255,0.06)]" : "border-mac-stroke bg-mac-fill"}`}>
      <div className="flex items-center gap-2.5 sm:gap-3.5 shrink-0">
        <div className="text-center w-12">
          <div className="text-[16px] font-semibold text-mac-ink tnum leading-none">{f.depart}</div>
          <div className="text-[9.5px] text-mac-ink3 mt-1 truncate">{(f.from || "").slice(0, 3)}</div>
        </div>
        <div className="flex flex-col items-center gap-0.5 w-10">
          <div className="h-px w-full bg-mac-stroke relative"><Plane size={11} className="absolute -top-[5px] right-0 text-mac-accentHi" /></div>
        </div>
        <div className="text-center w-12">
          <div className="text-[16px] font-semibold text-mac-ink tnum leading-none">{f.arrive}</div>
          <div className="text-[9.5px] text-mac-ink3 mt-1 truncate">{(f.to || "").slice(0, 3)}</div>
        </div>
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-[12px] text-mac-ink2 font-medium">{f.flight}</div>
        <div className="text-[10.5px] text-mac-ink3">{f.class ? `Class ${f.class}` : ""}{best ? " · Cheapest" : ""}</div>
      </div>
      <div className="text-right shrink-0">
        <div className="text-[15px] font-semibold text-mac-ink tracking-[-0.01em]">Rs {Math.round(f.fare_npr).toLocaleString()}</div>
      </div>
      <a href={bookLink || "#"} target="_blank" rel="noreferrer"
        className="h-8 px-3.5 shrink-0 rounded-[9px] text-[12px] font-semibold text-white bg-gradient-to-b from-mac-accentHi to-mac-accent ring-1 ring-inset ring-white/15 hover:brightness-[1.06] transition-all inline-flex items-center gap-1.5">
        Book <ArrowUpRight size={12} strokeWidth={2.5} />
      </a>
    </div>
  );
}

function BusModal({ route, onClose }: { route: { from: string; to: string; date: string }; onClose: () => void }) {
  const [data, setData] = useState<DoBuses | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true; setLoading(true);
    api.do.buses(route.from, route.to, route.date)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setData(null); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [route]);

  const buses = data?.buses || [];
  const dateLabel = (() => {
    const d = new Date(route.date + "T00:00:00");
    return isNaN(d.getTime()) ? route.date : d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  })();
  const cheapest = data?.cheapest?.fare_npr;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6 bg-black/45 backdrop-blur-sm" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()}
        className="w-[660px] max-w-[calc(100%-2rem)] max-h-[82vh] flex flex-col rounded-2xl bg-[rgba(30,31,37,0.98)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden">
        <div className="relative shrink-0 px-5 pt-5 pb-4 border-b border-mac-stroke">
          <button onClick={onClose} className="absolute top-3 right-3 h-7 w-7 grid place-items-center rounded-full bg-mac-fillHi text-mac-ink3 hover:text-mac-ink transition-colors"><X size={15} /></button>
          <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-[0.16em] text-mac-accentHi mb-2"><Bus size={12} /> Bussewa · one way</div>
          <div className="flex items-center gap-3 text-[23px] font-semibold text-white tracking-[-0.015em]">
            <span>{data?.from || route.from}</span><ArrowRight size={19} className="text-mac-accentHi" /><span>{data?.to || route.to}</span>
          </div>
          <div className="text-[12px] text-white/70 mt-1.5">
            {dateLabel}{buses.length ? ` · ${data?.count || buses.length} buses` : ""}{cheapest ? ` · from Rs ${Math.round(cheapest).toLocaleString()}` : ""}
          </div>
          {data?.via && <div className="text-[11px] text-mac-accentHi mt-1.5">No direct bus to {data.via.for} — showing buses to {data.via.hub}. {data.via.note}</div>}
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto p-3">
          {loading ? (
            <div className="h-40 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
          ) : !buses.length ? (
            <div className="h-40 grid place-items-center text-center text-[13px] text-mac-ink2 px-6">
              {data?.message || "No buses came back — open bussewa to check this route."}
            </div>
          ) : (
            buses.map((b, i) => <BusRow key={`${b.trip_id || b.operator}-${b.depart}-${i}`} b={b} best={i === 0} bookLink={data?.booking_link} />)
          )}
        </div>
        <div className="shrink-0 flex items-center justify-between gap-3 px-5 py-3 border-t border-mac-stroke">
          <span className="text-[11px] text-mac-ink3">Live departures · you pick a seat &amp; book on bussewa</span>
          <a href={data?.booking_link || "#"} target="_blank" rel="noreferrer"
            className="h-9 px-4 rounded-[10px] text-[12.5px] font-medium inline-flex items-center gap-1.5 bg-mac-accent text-white hover:bg-mac-accentHi transition-colors">
            Open Bussewa <ArrowUpRight size={14} strokeWidth={2.5} />
          </a>
        </div>
      </div>
    </div>
  );
}

function BusRow({ b, best, bookLink }: { b: DoBus; best: boolean; bookLink?: string }) {
  return (
    <div className={`flex items-center gap-3 sm:gap-4 p-3 rounded-xl border mb-2 transition-colors ${
      best ? "border-mac-accentHi/30 bg-[rgba(10,132,255,0.06)]" : "border-mac-stroke bg-mac-fill"}`}>
      <div className="flex items-center gap-2.5 sm:gap-3.5 shrink-0">
        <div className="text-center w-14">
          <div className="text-[15px] font-semibold text-mac-ink tnum leading-none">{b.depart}</div>
          {b.arrive && <div className="text-[10px] text-mac-ink3 mt-1 tnum">{b.arrive}</div>}
        </div>
        <div className="flex flex-col items-center gap-0.5 w-10">
          <div className="h-px w-full bg-mac-stroke relative"><Bus size={11} className="absolute -top-[5px] right-0 text-mac-accentHi" /></div>
          {b.journey_hours ? <div className="text-[9px] text-mac-ink3 inline-flex items-center gap-0.5"><Clock size={8} />{b.journey_hours}h</div> : null}
        </div>
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-[12.5px] text-mac-ink font-medium truncate">{b.operator}</div>
        <div className="text-[10.5px] text-mac-ink3 truncate">
          {b.bus_type || "Bus"}
          {typeof b.seats_available === "number" ? <> · <Armchair size={9} className="inline -mt-0.5" /> {b.seats_available} seats</> : null}
          {b.amenities && b.amenities.length ? ` · ${b.amenities.slice(0, 2).join(", ")}` : ""}
        </div>
      </div>
      <div className="text-right shrink-0">
        <div className="text-[15px] font-semibold text-mac-ink tracking-[-0.01em]">Rs {Math.round(b.fare_npr).toLocaleString()}</div>
        {b.min_bargain_npr ? <div className="text-[9.5px] text-mac-accentHi">bargain Rs {Math.round(b.min_bargain_npr).toLocaleString()}</div> : (best ? <div className="text-[9.5px] text-mac-ink3">Cheapest</div> : null)}
      </div>
      <a href={bookLink || "#"} target="_blank" rel="noreferrer"
        className="h-8 px-3.5 shrink-0 rounded-[9px] text-[12px] font-semibold text-white bg-gradient-to-b from-mac-accentHi to-mac-accent ring-1 ring-inset ring-white/15 hover:brightness-[1.06] transition-all inline-flex items-center gap-1.5">
        Book <ArrowUpRight size={12} strokeWidth={2.5} />
      </a>
    </div>
  );
}

function DoTray({ cart, onClose, onChange }: {
  cart: DoCartView | null; onClose: () => void; onChange: (v: DoCartView) => void;
}) {
  const groups = cart?.groups || [];
  const setQty = async (key: string, qty: number) => { try { onChange(await api.do.cart.qty(key, qty)); } catch { /* */ } };
  const clear = async () => { try { onChange(await api.do.cart.clear()); } catch { /* */ } };

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()}
        className="w-[400px] max-w-[calc(100%-2rem)] h-full flex flex-col bg-[rgba(28,29,34,0.99)] backdrop-blur-xl border-l border-mac-strokeHi shadow-pop himmy-enter-right">
        <div className="shrink-0 flex items-center justify-between px-4 h-14 border-b border-mac-stroke">
          <div className="flex items-center gap-2 font-display text-[15px] font-semibold text-mac-ink">
            <ShoppingCart size={16} className="text-mac-accentHi" /> Your tray
            {(cart?.count || 0) > 0 && <span className="text-[12px] font-normal text-mac-ink3">· {cart?.count} item{cart?.count === 1 ? "" : "s"}</span>}
          </div>
          <button onClick={onClose} className="h-7 w-7 grid place-items-center rounded-lg text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill"><X size={15} /></button>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto p-4">
          {groups.length === 0 ? (
            <div className="h-full grid place-items-center text-center">
              <div>
                <ShoppingCart size={24} className="text-mac-ink3 mx-auto mb-3" />
                <p className="text-[13px] text-mac-ink2">Your tray is empty.</p>
                <p className="text-[11.5px] text-mac-ink3 mt-1 max-w-[24ch] mx-auto">Add dishes from a restaurant or products from a deal, then check out on the site.</p>
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              {groups.map((g) => (
                <div key={g.place} className="rounded-xl border border-mac-stroke bg-mac-fill overflow-hidden">
                  <div className="flex items-center justify-between px-3 py-2 border-b border-mac-stroke">
                    <span className="text-[12.5px] font-semibold text-mac-ink truncate">{g.place}</span>
                    <span className="text-[12px] text-mac-ink2 shrink-0">Rs {g.subtotal}</span>
                  </div>
                  <div className="divide-y divide-mac-stroke/60">
                    {g.items.map((it) => (
                      <div key={it.key} className="flex items-center gap-2 px-3 py-2">
                        <div className="min-w-0 flex-1">
                          <div className="text-[12px] text-mac-ink leading-snug line-clamp-1">{it.name}</div>
                          <div className="text-[11px] text-mac-ink3">Rs {it.price}</div>
                        </div>
                        <div className="flex items-center gap-1 shrink-0">
                          <button onClick={() => setQty(it.key, it.qty - 1)} className="h-6 w-6 grid place-items-center rounded-md bg-mac-fillHi border border-mac-stroke text-mac-ink2 hover:text-mac-ink"><Minus size={12} /></button>
                          <span className="w-5 text-center text-[12px] text-mac-ink tnum">{it.qty}</span>
                          <button onClick={() => setQty(it.key, it.qty + 1)} className="h-6 w-6 grid place-items-center rounded-md bg-mac-fillHi border border-mac-stroke text-mac-ink2 hover:text-mac-ink"><Plus size={12} /></button>
                        </div>
                      </div>
                    ))}
                  </div>
                  {g.checkout_link && (
                    <a href={g.checkout_link} target="_blank" rel="noreferrer"
                      className="flex items-center justify-center gap-1.5 h-9 text-[12px] font-medium text-mac-accentHi hover:bg-mac-fillHi border-t border-mac-stroke transition-colors">
                      Checkout {g.source === "food" ? "on Foodmandu" : "on Daraz"} <ArrowUpRight size={13} strokeWidth={2.5} />
                    </a>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {groups.length > 0 && (
          <div className="shrink-0 px-4 py-3 border-t border-mac-stroke">
            <div className="flex items-center justify-between mb-2">
              <span className="text-[12.5px] text-mac-ink2">Total</span>
              <span className="text-[16px] font-semibold text-mac-ink">Rs {cart?.total}</span>
            </div>
            <div className="flex items-center gap-2">
              <button onClick={clear} className="h-8 px-3 rounded-[9px] text-[12px] text-mac-ink2 hover:text-mac-ink bg-mac-fill border border-mac-stroke inline-flex items-center gap-1.5">
                <Trash2 size={13} /> Clear
              </button>
              <p className="flex-1 text-[10.5px] text-mac-ink3 leading-tight">Open each place to place the order — Himmy holds your list, you check out.</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ───────────────────────────────────────── today */
// Engaged reading time → a compact human label ("42m read", "3.1h read").
function fmtRead(seconds: number): string {
  const m = Math.round(seconds / 60);
  if (m < 1) return "";
  if (m < 60) return `${m}m read`;
  return `${(seconds / 3600).toFixed(1)}h read`;
}

// A local YYYY-MM-DD for `d` (wall-clock, no timezone shift) — used to test "is this today?".
function localDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// One row in the Today agenda — a calendar event or a task, with a sort key (minutes from
// midnight; all-day / undated tasks sort to the very top via -1).
type AgendaItem = {
  key: string;
  kind: "event" | "task";
  title: string;
  timeLabel: string;   // "9:30 AM" / "All day" / "Due today"
  sortMin: number;     // minutes from midnight; -1 = all-day / no specific time
  done?: boolean;
  taskId?: string;
};

// Minutes-from-midnight for an ISO local wall-clock "…THH:MM[:SS]" (or -1 if it has no time).
function minutesOfDay(iso: string | null | undefined): number {
  if (!iso || !iso.includes("T")) return -1;
  const m = iso.match(/T(\d{2}):(\d{2})/);
  if (!m) return -1;
  return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
}

// Grace (minutes) after a timed item's start before "Today" drops it — long enough that something
// just starting/ongoing doesn't vanish, short enough that the past clears off and you don't scroll.
const AGENDA_GRACE_MIN = 30;

// Build TODAY's single time-sorted agenda: events today + tasks scheduled today + tasks due
// today (de-duped so a task that's both scheduled and due appears once). Best-effort/never throws.
function buildTodayAgenda(events: CalendarEvent[], tasks: Task[]): AgendaItem[] {
  const today = localDay(new Date());
  const items: AgendaItem[] = [];

  for (const e of events) {
    if (!e.start) continue;
    const allDay = !e.start.includes("T");
    const day = allDay ? e.start.slice(0, 10) : localDay(new Date(e.start));
    if (day !== today) continue;
    items.push({
      key: "e:" + (e.id ?? "") + e.start,
      kind: "event",
      title: e.summary || "(untitled)",
      timeLabel: allDay ? "All day" : fmtTime(e.start),
      sortMin: allDay ? -1 : minutesOfDay(e.start),
    });
  }

  const seenTask = new Set<string>();
  for (const t of tasks) {
    if (t.done) continue;
    const scheduledToday = !!t.scheduled_start && t.scheduled_start.slice(0, 10) === today;
    const dueToday = !!t.due && !isNaN(new Date(t.due).getTime()) && localDay(new Date(t.due)) === today;
    if (!scheduledToday && !dueToday) continue;
    if (seenTask.has(t.id)) continue;
    seenTask.add(t.id);
    items.push({
      key: "t:" + t.id,
      kind: "task",
      title: t.title,
      timeLabel: scheduledToday ? fmtHm12(t.scheduled_start!.slice(11, 16)) : "Due today",
      sortMin: scheduledToday ? minutesOfDay(t.scheduled_start) : -1,
      done: t.done,
      taskId: t.id,
    });
  }

  return items.sort((a, b) => {
    if (a.sortMin !== b.sortMin) return a.sortMin - b.sortMin;
    return a.title.localeCompare(b.title);
  });
}

// Tasks due within ~36h (today or tomorrow) — the "due soon" nudge. Open tasks only.
function dueSoon(tasks: Task[]): Task[] {
  const now = Date.now();
  const horizon = now + 36 * 3600 * 1000;
  return tasks.filter((t) => {
    if (t.done || !t.due) return false;
    const d = new Date(t.due);
    if (isNaN(d.getTime())) return false;
    // Dates with no time land at local midnight — count anything from today through the horizon.
    const ms = d.getTime();
    return ms <= horizon && ms >= now - 36 * 3600 * 1000;
  }).sort((a, b) => new Date(a.due!).getTime() - new Date(b.due!).getTime());
}

// Bare duration for tight table cells ("<1m", "42m", "3.1h") — no "read" suffix.
function fmtReadShort(seconds: number): string {
  if (!seconds) return "";
  const m = Math.round(seconds / 60);
  if (m < 1) return "<1m";
  if (m < 60) return `${m}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

// Natural-language quick-add: "lit review fri 3pm !high" → {title, due, time, priority}.
// Strips the recognised date/time/priority tokens from the title. All best-effort.
function parseQuickAdd(raw: string): { title: string; due: string | null; priority: number; time: string | null } {
  let text = ` ${raw} `;
  const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const today = new Date(); today.setHours(0, 0, 0, 0);

  let priority = 0;
  const pr = text.match(/\s(!{1,3}|!high|!h|!med|!medium|!m|!low|!l)\s/i);
  if (pr) {
    const t = pr[1].toLowerCase();
    priority = (t === "!!!" || t === "!high" || t === "!h") ? 3
      : (t === "!!" || t === "!med" || t === "!medium" || t === "!m") ? 2 : 1;
    text = text.replace(pr[0], " ");
  }

  let time: string | null = null;
  const tm = text.match(/\s(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s/i) || text.match(/\s(\d{1,2}):(\d{2})\s/);
  if (tm) {
    let h = parseInt(tm[1], 10);
    const min = tm[2] ? parseInt(tm[2], 10) : 0;
    const ap = (tm[3] || "").toLowerCase();
    if (ap === "pm" && h < 12) h += 12;
    if (ap === "am" && h === 12) h = 0;
    if (h >= 0 && h <= 23 && min >= 0 && min < 60) {
      time = `${String(h).padStart(2, "0")}:${String(min).padStart(2, "0")}`;
      text = text.replace(tm[0], " ");
    }
  }

  let due: string | null = null;
  const days = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
  const abbr = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
  let m: RegExpMatchArray | null;
  if ((m = text.match(/\s(today|tonight)\s/i))) { due = iso(today); text = text.replace(m[0], " "); }
  else if ((m = text.match(/\s(tomorrow|tmrw|tmr)\s/i))) { const d = new Date(today); d.setDate(d.getDate() + 1); due = iso(d); text = text.replace(m[0], " "); }
  else if ((m = text.match(/\sin\s(\d{1,2})\s(?:day|days)\s/i))) { const d = new Date(today); d.setDate(d.getDate() + parseInt(m[1], 10)); due = iso(d); text = text.replace(m[0], " "); }
  else if ((m = text.match(/\snext\sweek\s/i))) { const d = new Date(today); d.setDate(d.getDate() + 7); due = iso(d); text = text.replace(m[0], " "); }
  else if ((m = text.match(/\s(\d{4}-\d{2}-\d{2})\s/))) { due = m[1]; text = text.replace(m[0], " "); }
  else {
    for (let i = 0; i < 7; i++) {
      const mm = text.match(new RegExp(`\\s(?:next\\s)?(${days[i]}|${abbr[i]})\\s`, "i"));
      if (mm) {
        const d = new Date(today);
        let delta = (i - d.getDay() + 7) % 7;
        if (delta === 0) delta = 7;          // the NEXT one, not today
        d.setDate(d.getDate() + delta);
        due = iso(d); text = text.replace(mm[0], " ");
        break;
      }
    }
  }
  const title = text.replace(/\s+/g, " ").trim();
  return { title: title || raw.trim(), due, priority, time };
}

// "15:00" → "3:00 PM"
function fmtHm12(hhmm: string): string {
  const [h, m] = hhmm.split(":").map(Number);
  return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

function dedupeByTitle<T extends { title: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const it of items) {
    const k = (it.title || "").trim().toLowerCase();
    if (k && seen.has(k)) continue;
    if (k) seen.add(k);
    out.push(it);
  }
  return out;
}

// A live, ticking clock for the home hero — its own 1s timer so the rest of the home
// doesn't re-render every second.
function LiveClock() {
  const [t, setT] = useState(() => new Date());
  useEffect(() => { const id = setInterval(() => setT(new Date()), 1000); return () => clearInterval(id); }, []);
  const [hm, ap] = t.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).split(" ");
  return (
    <div className="relative hidden sm:flex items-baseline gap-1.5 tnum">
      <span className="font-display text-[30px] font-semibold tracking-[-0.02em] leading-none bg-gradient-to-b from-white to-white/70 bg-clip-text text-transparent">{hm}</span>
      {ap && <span className="text-[12px] font-semibold uppercase tracking-[0.08em] text-mac-ink3">{ap}</span>}
    </div>
  );
}

/* The home tab now holds two views — the Today cockpit and the Planner — switched by a
   segmented control, so Planner no longer needs its own top-bar slot. */
// Himmy's proactive daily brief — the first thing you see on Today. Served from a daily cache
// (instant); a stale or cold brief generates in the background and we poll until it lands.
// Map a proactive observation's surface to its icon + accent tint, so each card reads at a glance.
function obsSurfaceIcon(o: Observation): LucideIcon {
  switch (o.surface) {
    case "finance": return Wallet;
    case "calendar": return CalendarClock;
    case "mail": return Mail;
    case "tasks": return ListChecks;
    default:
      // fall back on kind for the cross-surface "connect" / "trip" observations
      if (o.kind === "trip") return Plane;
      if (o.kind === "prep") return CalendarClock;
      if (o.kind === "budget") return Wallet;
      return Lightbulb;
  }
}

// "Himmy noticed …" — the proactive brain's actionable observations, rendered at the TOP of the
// notification centre (the bell). Presentational: the App owns the observation list (so the bell
// badge + macOS notification track it) and passes it in; this just renders + acts. Each row mirrors
// the approval-card layout (title + detail, then an action row) so it fits the 420px panel cleanly.
function HimmyNoticedList({ obs, onRefresh }: { obs: Observation[]; onRefresh: () => void }) {
  const [busy, setBusy] = useState<Record<string, "do" | "dismiss" | "snooze">>({});
  const setB = (id: string, v: "do" | "dismiss" | "snooze" | null) =>
    setBusy((b) => { const n = { ...b }; if (v) n[id] = v; else delete n[id]; return n; });

  const doIt = async (o: Observation) => {
    setB(o.id, "do");
    try {
      const r = await api.proactive.do(o.id);
      // Risky action parked for approval → the approval card appears in this same panel on refresh;
      // a completed action just clears the observation. Either way, reload + nudge the tabs.
      onRefresh(); emitRefresh("tasks");
    } catch { /* best-effort */ }
    finally { setB(o.id, null); }
  };
  const dismiss = async (o: Observation) => {
    setB(o.id, "dismiss");
    try { await api.proactive.dismiss(o.id); } finally { onRefresh(); }
  };
  const snooze = async (o: Observation) => {
    setB(o.id, "snooze");
    try { await api.proactive.snooze(o.id, 4); } finally { onRefresh(); }
  };

  if (!obs.length) return null;
  return (
    <div className="border-b border-mac-stroke">
      <div className="px-4 pt-3 pb-1.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-mac-accentHi">
        <Sparkles size={12} strokeWidth={2.2} /> Himmy noticed
        <span className="text-mac-ink4 normal-case tracking-normal font-medium">· {obs.length}</span>
      </div>
      <div className="divide-y divide-mac-stroke/60">
        {obs.map((o) => {
          const Icon = obsSurfaceIcon(o);
          const b = busy[o.id];
          return (
            <div key={o.id} className="px-4 py-3 hover:bg-mac-fill transition-colors">
              <div className="flex items-start gap-2.5">
                <div className="h-7 w-7 shrink-0 grid place-items-center rounded-[8px] bg-mac-fillHi ring-1 ring-inset ring-white/[0.06]">
                  <Icon size={14} strokeWidth={2} className="text-mac-accentHi" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="text-[13px] font-medium text-mac-ink leading-snug">{o.title}</div>
                  <div className="text-[12px] text-mac-ink3 leading-snug mt-0.5 line-clamp-2">{o.detail}</div>
                </div>
                <button onClick={() => dismiss(o)} disabled={!!b} title="Dismiss"
                  className="shrink-0 grid place-items-center h-6 w-6 rounded-[6px] text-mac-ink3 hover:text-mac-red hover:bg-mac-fillHi transition-colors disabled:opacity-50">
                  {b === "dismiss" ? <Loader2 size={12} className="animate-spin" /> : <X size={13} strokeWidth={2} />}
                </button>
              </div>
              <div className="flex gap-2 mt-2.5 pl-[38px]">
                <button onClick={() => doIt(o)} disabled={!!b}
                  className="h-7 px-3 rounded-[7px] bg-mac-accent text-white text-[12px] font-medium hover:bg-mac-accentHi transition-colors disabled:opacity-60 inline-flex items-center gap-1.5">
                  {b === "do" ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} strokeWidth={2.5} />}
                  <span className="truncate max-w-[180px]">{o.action_label || "Do it"}</span>
                </button>
                <button onClick={() => snooze(o)} disabled={!!b}
                  className="h-7 px-3 rounded-[7px] bg-mac-fill border border-mac-stroke text-[12px] text-mac-ink2 hover:text-mac-ink transition-colors disabled:opacity-60 inline-flex items-center gap-1.5">
                  {b === "snooze" ? <Loader2 size={12} className="animate-spin" /> : <Clock size={12} />} Snooze
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// HH:MM (24h) → "7:00 AM" — matches the agenda card.
function to12h(hm?: string): string {
  if (!hm) return "";
  const [h, m] = hm.split(":").map(Number);
  if (isNaN(h)) return hm;
  return `${h % 12 || 12}:${String(m || 0).padStart(2, "0")} ${h >= 12 ? "PM" : "AM"}`;
}

// True once a timed item's wall-clock has passed today — drives the live "overdue" flag, so a
// scheduled item flips to overdue the moment its time goes by (no refetch needed).
function isPastTime(hm?: string): boolean {
  if (!hm) return false;
  const [h, m] = hm.split(":").map(Number);
  if (isNaN(h)) return false;
  const now = new Date();
  return now.getHours() * 60 + now.getMinutes() > h * 60 + (m || 0);
}

// A today's-calendar item rendered as a checkable to-do row inside the "To do" card — same shape as
// HomeTaskRow, with the event's time on the right. Ticking marks it done for today (resets daily).
function PlanEventRow({ item, done, last, onToggle }:
  { item: DayPlanItem; done: boolean; last?: boolean; onToggle: () => void }) {
  const overdue = !done && isPastTime(item.time);   // past its time and not ticked → missed
  return (
    <div className={`group flex items-center gap-2.5 py-2.5 px-1 ${last ? "" : "border-b border-mac-stroke"}`}>
      <button onClick={onToggle} title={done ? "Done today — tap to undo" : "Mark done for today"}
        className="shrink-0 grid place-items-center transition-colors">
        {done
          ? <CheckCircle2 size={16} strokeWidth={1.75} className="text-mac-green" />
          : <Circle size={16} strokeWidth={1.75} className={overdue ? "text-mac-red/80 group-hover:text-mac-green" : "text-mac-ink3 group-hover:text-mac-accentHi"} />}
      </button>
      <span className={`min-w-0 flex-1 truncate text-[13.5px] ${done ? "text-mac-ink3 line-through" : "text-mac-ink"}`}>{item.title}</span>
      {overdue && (
        <span className="shrink-0 text-[10.5px] font-medium uppercase tracking-[0.04em] text-mac-red/90 bg-mac-red/10 rounded px-1.5 py-0.5">
          overdue
        </span>
      )}
      {item.time && (
        <span className={`shrink-0 inline-flex items-center gap-1 text-[12px] ${overdue ? "text-mac-red/90" : "text-mac-ink3"}`}>
          <Clock size={11} />{to12h(item.time)}
        </span>
      )}
    </div>
  );
}

function TodayBrief() {
  const [brief, setBrief] = useState<{ text: string; generating?: boolean; stale?: boolean; generated_at?: string } | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const load = async (force = false) => {
    try { const r = force ? await api.brief(true) : await api.brief(); setBrief(r); return r; }
    catch { return null; }
  };
  useEffect(() => {
    let alive = true; let tries = 0;
    const poll = async () => {
      if (!alive || tries >= 8) return;
      tries += 1;
      const r = await load();
      if (alive && r && (r.generating || r.stale || !r.text)) setTimeout(poll, 5000);
    };
    (async () => {
      const r = await load();
      if (r && (r.generating || r.stale || !r.text)) setTimeout(poll, 5000);
    })();
    return () => { alive = false; };
    /* eslint-disable-next-line */
  }, []);
  const refresh = async () => {
    setRefreshing(true);
    await load(true);
    // poll a few times for the freshly-regenerated brief
    let tries = 0;
    const spin = async () => {
      tries += 1;
      const r = await load();
      if ((r?.stale || r?.generating) && tries < 8) setTimeout(spin, 5000); else setRefreshing(false);
    };
    setTimeout(spin, 5000);
  };

  const generating = !brief || (!brief.text && (brief.generating || brief.stale));
  if (brief && !brief.text && !generating) return null;  // no brief and not generating → hide

  return (
    <div className="relative mt-4 rounded-2xl border border-white/[0.07] bg-gradient-to-b from-white/[0.05] to-white/[0.012] shadow-card p-4 overflow-hidden">
      {/* a faint accent wash in the top-left, so the brief reads as Himmy's own surface */}
      <div className="pointer-events-none absolute -top-20 -left-16 h-40 w-72 rounded-full bg-mac-accent/[0.07] blur-[64px]" />
      <div className="relative flex items-center justify-between mb-2.5">
        <div className="flex items-center text-[11px] font-semibold uppercase tracking-[0.16em] text-mac-accentHi">
          Your brief
        </div>
        <button onClick={refresh} disabled={refreshing} title="Refresh brief"
          className="h-7 w-7 grid place-items-center rounded-[8px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors disabled:opacity-60">
          <RefreshCw size={12.5} className={refreshing ? "animate-spin" : ""} />
        </button>
      </div>
      {generating ? (
        <div className="flex items-center gap-2 text-[12.5px] text-mac-ink3 py-1">
          <Loader2 size={14} className="animate-spin text-mac-accentHi" /> Putting together your day…
        </div>
      ) : (
        <div className="max-h-[168px] overflow-y-auto pr-1 text-[13px] text-mac-ink2 [&_strong]:text-mac-ink">
          <ChatMarkdown text={brief!.text} />
        </div>
      )}
    </div>
  );
}

function HomeTab({ health, initialView = "today", initialPlan = "tasks" }: {
  health: Health | null; initialView?: "today" | "plan"; initialPlan?: "tasks" | "calendar";
}) {
  const [view, setView] = useState<"today" | "plan">(initialView);
  const [plan, setPlan] = useState<"tasks" | "calendar">(initialPlan);
  // Deep-links (the Today / To-do cards, an agenda row) set the section → keep in sync.
  useEffect(() => { setView(initialView); }, [initialView]);
  useEffect(() => { setPlan(initialPlan); }, [initialPlan]);

  // The cockpit fills the tab on its own. The Plan view is reached from the Today / To-do
  // cards and carries just its Tasks/Calendar switch; the top "Today" tab returns home.
  if (view === "today") return <Today health={health} />;

  const seg = "flex items-center gap-0.5 p-0.5 rounded-[10px] bg-mac-fill border border-mac-stroke";
  const btn = (on: boolean) =>
    `flex items-center gap-1.5 h-[30px] px-3.5 rounded-[8px] text-[12.5px] transition-colors ${
      on ? "bg-mac-fillHi text-mac-ink shadow-tab" : "text-mac-ink2 hover:text-mac-ink"}`;

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 h-[52px] px-6 flex items-center">
        <div className={seg}>
          {([["tasks", "Tasks", CheckSquare], ["calendar", "Calendar", Calendar]] as const).map(([id, label, Ico]) => (
            <button key={id} onClick={() => setPlan(id)} className={btn(plan === id)}>
              <Ico size={14} strokeWidth={2} className={plan === id ? "text-mac-accentHi" : ""} />{label}
            </button>
          ))}
        </div>
      </div>
      <div className="flex-1 min-h-0">
        {plan === "tasks" ? <Tasks embedded /> : <CalendarTab embedded />}
      </div>
    </div>
  );
}

function Today({ health }: { health: Health | null }) {
  void health;
  // Live clock + greeting — re-tick each half-minute so "morning"→"afternoon" flips on its own.
  const [now, setNow] = useState(() => new Date());
  useEffect(() => { const t = setInterval(() => setNow(new Date()), 30_000); return () => clearInterval(t); }, []);
  const h = now.getHours();
  const part = h < 5 ? "Still up" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  const GreetIcon = h < 12 ? Sunrise : h < 18 ? Sun : Moon;
  const dateStr = now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });

  const { status: google, connect } = useGoogle();
  const googleConnected = !!google?.connected;

  const [events, setEvents] = useState<CalendarEvent[] | null>(null);   // today's events (for the agenda)
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [taskCount, setTaskCount] = useState<{ open: number; total: number }>({ open: 0, total: 0 });
  const [saved, setSaved] = useState<SavedArticle[] | null>(null);
  const [papers, setPapers] = useState<Paper[] | null>(null);
  const [readStats, setReadStats] = useState<ReadingStats | null>(null);
  const [newTask, setNewTask] = useState("");
  const [newDue, setNewDue] = useState("");          // YYYY-MM-DD from the date input
  const [newPriority, setNewPriority] = useState(0); // 0..3, cycled by the flag button

  // Today's CALENDAR events, surfaced as checkable rows at the top of the To-do card so the to-do
  // reflects the day you've planned in your calendar (not just free-floating tasks).
  const [planEvents, setPlanEvents] = useState<DayPlanItem[] | null>(null);
  const [eventDone, setEventDone] = useState<Set<string>>(new Set());

  const loadTasks = async () => {
    try { const r = await api.tasks.list(); setTasks(r.tasks); setTaskCount({ open: r.open, total: r.total }); }
    catch { setTasks((t) => t ?? []); }
  };
  const loadPlan = async () => {
    try {
      const r = await api.todayPlan();
      const evs = r.items.filter((i) => i.kind === "event");
      setPlanEvents(evs);
      setEventDone(new Set(evs.filter((e) => e.done).map((e) => e.id)));
    } catch { setPlanEvents((p) => p ?? []); }
  };
  const toggleEvent = async (id: string) => {
    const next = !eventDone.has(id);
    setEventDone((s) => { const n = new Set(s); next ? n.add(id) : n.delete(id); return n; });
    try { await api.todayPlanDone(id, next); } catch { loadPlan(); }
  };
  const loadReading = async () => {
    try { const r = await api.news.saved(); setSaved(dedupeByTitle(r.items).slice(0, 3)); } catch { setSaved([]); }
    try { const r = await api.library.list(); setPapers(dedupeByTitle(r.items).slice(0, 3)); } catch { setPapers([]); }
    try { setReadStats(await api.reading.stats()); } catch { /* backend warming */ }
  };
  useEffect(() => {
    loadTasks(); loadReading(); loadPlan();
    const t = setInterval(loadTasks, 5000);
    return () => { clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useRefreshSignal("tasks", loadTasks);
  useRefreshSignal("calendar", loadPlan);
  useRefreshSignal("library", loadReading);
  useRefreshSignal("news", loadReading);

  // TODAY's calendar events (for the agenda timeline) once an account is connected; refresh
  // live when Himmy changes the calendar. We pull the whole local day so earlier-today and
  // all-day events are included, not just what's still upcoming.
  const loadEvents = async () => {
    if (!googleConnected) { setEvents(null); return; }
    try {
      const start = new Date(); start.setHours(0, 0, 0, 0);
      const end = new Date(start); end.setDate(end.getDate() + 1);
      const r = await api.calendar.range(start.toISOString(), end.toISOString());
      setEvents(r.events || []);
    } catch { setEvents([]); }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { loadEvents(); loadPlan(); }, [googleConnected]);
  useRefreshSignal("calendar", loadEvents);

  // Smart sort: open first, overdue, priority desc, due asc (see compareTasks).
  const openTasks = (tasks ?? []).filter((t) => !t.done).sort(compareTasks);
  // The To-do card = today's calendar events (checkable) + your open tasks. Ticked-off events drop
  // out (like completed tasks) so the list stays tight; what's left = open tasks + not-done events.
  const todayEvents = planEvents ?? [];
  const visibleEvents = todayEvents.filter((e) => !eventDone.has(e.id));
  const todoOpenCount = openTasks.length + visibleEvents.length;
  const overdueCount = visibleEvents.filter((e) => isPastTime(e.time)).length;
  // Today agenda — a FORWARD-looking timeline: calendar + scheduled/due tasks, with anything whose
  // time has already passed (beyond a short grace) dropped, so "Today" shows the rest of your day
  // and stays scroll-free. `agendaAll` keeps the unfiltered set to tell "all done" from "nothing on".
  const nowMin = new Date().getHours() * 60 + new Date().getMinutes();
  const agendaAll = buildTodayAgenda(events ?? [], tasks ?? []);
  const agenda = agendaAll.filter((it) => it.sortMin < 0 || it.sortMin >= nowMin - AGENDA_GRACE_MIN);
  // Due-soon nudge — open tasks due today or tomorrow (within ~36h).
  const soon = dueSoon(tasks ?? []);

  const completeTask = async (id: string) => {
    setTasks((ts) => (ts ?? []).map((t) => (t.id === id ? { ...t, done: true } : t)));
    try { await api.tasks.complete(id); emitRefresh("tasks"); } catch { loadTasks(); }
  };
  const addTask = async () => {
    const title = newTask.trim(); if (!title) return;
    const due = newDue || undefined;
    const priority = newPriority || undefined;
    setNewTask(""); setNewDue(""); setNewPriority(0);
    try {
      const r = await api.tasks.add(title, { due, priority });
      setTasks((ts) => [r.task, ...(ts ?? [])]); emitRefresh("tasks");
    } catch { loadTasks(); }
  };

  return (
    // One screen, no page scroll: hero (fixed) · three cards fill the height. (Usage moved to Settings.)
    <div className="h-full flex flex-col mx-auto w-full max-w-[1180px] px-9 pt-7 pb-6">
      {/* hero — glowing time-of-day orb · greeting · date · live clock + Himmy's brief */}
      <div className="relative shrink-0 mb-5">
        <div className="pointer-events-none absolute -top-16 -left-10 h-40 w-80 rounded-full bg-mac-accent/[0.12] blur-[80px]" />
        <div className="pointer-events-none absolute -top-12 right-0 h-32 w-64 rounded-full bg-mac-accentHi/[0.06] blur-[80px]" />
        <div className="relative flex items-center justify-between gap-4">
          <div className="flex items-center gap-4">
            <div className="relative h-12 w-12 shrink-0 rounded-[15px] grid place-items-center bg-gradient-to-br from-mac-accentHi to-mac-accent shadow-[0_6px_20px_-4px_rgba(10,132,255,0.6)] ring-1 ring-inset ring-white/20">
              <div className="pointer-events-none absolute inset-0 rounded-[15px] bg-gradient-to-b from-white/25 to-transparent opacity-60" />
              <GreetIcon size={21} strokeWidth={2} className="relative text-white drop-shadow-[0_1px_2px_rgba(0,0,0,0.3)]" />
            </div>
            <div>
              <h1 className="font-display text-[28px] font-semibold tracking-[-0.02em] leading-none bg-gradient-to-b from-white to-white/65 bg-clip-text text-transparent">{part}</h1>
              <p className="text-[13px] text-mac-ink2 mt-1.5 font-medium tracking-[-0.005em]">{dateStr}</p>
            </div>
          </div>
          <LiveClock />
        </div>
        <TodayBrief />
      </div>

      {/* "Himmy noticed" lives in the notification centre (the bell). Today's calendar-aware plan
          isn't a separate block — it's folded into the "To do" card below. */}
      <div className="flex-1 min-h-0 grid grid-cols-12 grid-rows-1 gap-4">
        {/* Today — one time-sorted agenda merging calendar events + scheduled/due tasks */}
        <Card className="col-span-12 md:col-span-4 min-h-0" icon={CalendarClock} title="Today"
          onOpen={() => nav("calendar")}
          hint={agenda.length ? `${agenda.length} ${agenda.length === 1 ? "item" : "items"}` : undefined}>
          <div className="flex flex-col h-full">
            <div className="flex-1 min-h-0 overflow-auto flex flex-col gap-1.5">
              {(events === null && googleConnected) || tasks === null ? (
                <div className="h-full grid place-items-center"><Loader2 size={16} className="animate-spin text-mac-ink3" /></div>
              ) : agenda.length === 0 ? (
                <Placeholder icon={agendaAll.length ? CheckCircle2 : CalendarClock}
                  text={agendaAll.length
                    ? "That's a wrap for today — nothing left on your schedule."
                    : "Nothing on today. Schedule a task or add an event to fill your day."} />
              ) : (
                agenda.map((it) => (
                  <AgendaRow key={it.key} item={it}
                    onClick={() => nav(it.kind === "event" ? "calendar" : "tasks")}
                    onComplete={it.taskId ? () => completeTask(it.taskId!) : undefined} />
                ))
              )}
            </div>
            {!googleConnected && (
              <button onClick={() => connect()}
                className="shrink-0 mt-2 pt-2.5 border-t border-mac-stroke flex items-center justify-center gap-1 text-[12px] text-mac-accentHi hover:underline">
                <Calendar size={12} strokeWidth={2} /> Connect your calendar
              </button>
            )}
          </div>
        </Card>

        {/* To do — today's calendar (checkable) + your tasks; complete on hover, add inline */}
        <Card className="col-span-12 md:col-span-4 min-h-0" icon={ListChecks} title="To do"
          onOpen={() => nav("tasks")}
          hint={overdueCount ? `${overdueCount} overdue` : todoOpenCount ? `${todoOpenCount} to do` : undefined}>
          <div className="flex flex-col h-full">
            <div className="flex-1 min-h-0 overflow-auto -mt-1">
              {tasks === null ? (
                <div className="h-full grid place-items-center"><Loader2 size={16} className="animate-spin text-mac-ink3" /></div>
              ) : visibleEvents.length === 0 && openTasks.length === 0 ? (
                <Placeholder icon={CheckCircle2} text={(taskCount.total || todayEvents.length) ? "All caught up." : "Nothing on today — add a task below or schedule your day."} />
              ) : (
                <>
                  {visibleEvents.map((e, i) => (
                    <PlanEventRow key={`evt-${e.id}`} item={e} done={false}
                      last={i === visibleEvents.length - 1 && openTasks.length === 0}
                      onToggle={() => toggleEvent(e.id)} />
                  ))}
                  {openTasks.map((t, i, a) => (
                    <HomeTaskRow key={t.id} task={t} last={i === a.length - 1}
                      onComplete={() => completeTask(t.id)} onOpen={() => nav("tasks")} />
                  ))}
                </>
              )}
            </div>
            {soon.length > 0 && (
              <button onClick={() => nav("tasks")}
                title={soon.map((t) => t.title).join("\n")}
                className="shrink-0 mt-2 flex items-center gap-1.5 text-[11.5px] text-mac-accentHi hover:underline">
                <Clock size={11} strokeWidth={2} />
                {soon.length} due soon
                <span className="text-mac-ink3 truncate">· {soon[0].title}</span>
              </button>
            )}
            <div className="flex items-center gap-2 mt-2 pt-2.5 border-t border-mac-stroke shrink-0">
              <Plus size={15} strokeWidth={2} className="shrink-0 text-mac-ink3" />
              <input value={newTask} onChange={(e) => setNewTask(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") addTask(); }}
                placeholder="Add a task…"
                className="flex-1 min-w-0 bg-transparent text-[13px] text-mac-ink placeholder:text-mac-ink3 outline-none" />
              {/* native date picker for a deadline + a priority flag toggle (0→1→2→3→0) */}
              <input type="date" value={newDue} onChange={(e) => setNewDue(e.target.value)}
                title="Due date"
                className="shrink-0 bg-transparent text-[11.5px] text-mac-ink3 outline-none [color-scheme:dark] w-[112px]" />
              <button type="button" onClick={() => setNewPriority(nextPriority)}
                title={`Priority: ${PRIORITY_META[newPriority]?.label ?? "None"}`}
                className={`shrink-0 grid place-items-center h-6 w-6 rounded-[7px] hover:bg-mac-fillHi transition-colors ${PRIORITY_META[newPriority]?.tone ?? "text-mac-ink4"}`}>
                <Flag size={13} strokeWidth={2} />
              </button>
            </div>
          </div>
        </Card>

        {/* Jump back in — recent papers + saved reading; hint shows engaged reading this week */}
        <Card className="col-span-12 md:col-span-4 min-h-0" icon={BookText} title="Jump back in"
          hint={readStats?.week_seconds ? fmtRead(readStats.week_seconds) + " this week" : undefined}>
          {saved === null && papers === null ? (
            <div className="h-full grid place-items-center"><Loader2 size={16} className="animate-spin text-mac-ink3" /></div>
          ) : (papers?.length || saved?.length) ? (
            <div className="h-full overflow-auto -my-1">
              {(papers ?? []).map((p, i, arr) => (
                <BriefRow key={p.id} icon={BookText} title={p.title}
                  sub={[p.authors?.[0], p.year].filter(Boolean).join(" · ") || "Library"}
                  last={i === arr.length - 1 && !(saved?.length)}
                  onClick={() => nav("library")} />
              ))}
              {(saved ?? []).map((a, i, arr) => (
                <BriefRow key={a.id} icon={Newspaper} title={a.title}
                  sub={a.source || "Saved article"}
                  last={i === arr.length - 1}
                  onClick={() => nav("news")} />
              ))}
            </div>
          ) : (
            <Placeholder icon={BookText} text="Add a few papers or save some reading to pick up where you left off." />
          )}
        </Card>
      </div>

    </div>
  );
}

// One row in the Today agenda timeline — a time chip, a colored dot (event vs task), the title,
// and (for tasks) a hover-to-complete circle. Clicking jumps to the relevant tab.
function AgendaRow({ item, onClick, onComplete }:
  { item: AgendaItem; onClick: () => void; onComplete?: () => void }) {
  const isEvent = item.kind === "event";
  return (
    <div
      className="group flex items-center gap-2.5 rounded-[11px] border border-white/[0.06] bg-gradient-to-b from-white/[0.06] to-white/[0.02] px-2.5 py-2 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.04)] hover:border-white/[0.12] hover:from-white/[0.09] transition-all duration-200">
      <span className="shrink-0 w-[58px] text-right text-[11px] tnum text-mac-ink3 leading-none font-medium">
        {item.timeLabel || "—"}
      </span>
      {/* a clean outline circle for tasks (checks on hover); a small accent dot for events */}
      {item.taskId && onComplete ? (
        <button onClick={onComplete} title="Mark done"
          className="shrink-0 grid place-items-center text-mac-ink3 hover:text-mac-ink transition-colors">
          <Circle size={13} strokeWidth={2} className="group-hover:hidden" />
          <CheckCircle2 size={14} strokeWidth={2} className="hidden group-hover:block text-mac-accentHi" />
        </button>
      ) : (
        <span className={`shrink-0 h-1.5 w-1.5 rounded-full ${isEvent ? "bg-mac-accentHi" : "bg-mac-ink3"}`} />
      )}
      <button onClick={onClick} className="min-w-0 flex-1 text-left">
        <div className={`text-[12.5px] truncate leading-tight ${item.done ? "line-through text-mac-ink3" : "text-mac-ink"}`}>{item.title}</div>
      </button>
      {isEvent
        ? <Calendar size={12} className="shrink-0 text-mac-ink4" />
        : <ListChecks size={12} className="shrink-0 text-mac-ink4" />}
    </div>
  );
}

// To-do row with hover-to-complete (circle → green check).
function HomeTaskRow({ task, last, onComplete, onOpen }:
  { task: Task; last?: boolean; onComplete: () => void; onOpen: () => void }) {
  const overdue = isOverdue(task.due);
  return (
    <div className={`group flex items-center gap-2.5 py-2.5 px-1 ${last ? "" : "border-b border-mac-stroke"}`}>
      <button onClick={onComplete} title="Mark done"
        className="shrink-0 grid place-items-center text-mac-ink3 hover:text-mac-green transition-colors">
        <Circle size={16} strokeWidth={1.75} className="group-hover:hidden" />
        <CheckCircle2 size={16} strokeWidth={1.75} className="hidden group-hover:block text-mac-green" />
      </button>
      <span onClick={onOpen} className="min-w-0 flex-1 truncate text-[13.5px] text-mac-ink cursor-pointer">{task.title}</span>
      <PriorityFlag priority={task.priority} />
      {task.due && (
        <span className={`shrink-0 inline-flex items-center gap-1 text-[12px] ${overdue ? "text-mac-red" : "text-mac-ink3"}`}>
          <Clock size={11} />{dueLabel(task.due)}
        </span>
      )}
    </div>
  );
}

// ── Usage / cost meter ──────────────────────────────────────────────────────────────────
// Reads himmy's metrics registry via /usage: tokens + USD this session and an all-time tally.
function fmtCost(usd: number): string {
  if (!usd) return "$0.00";
  if (usd < 0.01) return "<$0.01";
  return "$" + usd.toFixed(usd < 1 ? 3 : 2);
}
function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 10_000 ? 0 : 1) + "K";
  return String(n);
}

function UsageStat({ label, totals, primary }: { label: string; totals: UsageTotals; primary?: boolean }) {
  return (
    <div>
      <div className="text-[11px] font-semibold uppercase tracking-[0.06em] text-mac-ink3 mb-1.5">{label}</div>
      <div className="flex items-baseline gap-2.5">
        <span className={`font-display font-semibold tnum tracking-[-0.01em] ${primary ? "text-[26px] text-mac-ink" : "text-[22px] text-mac-ink2"}`}>
          {fmtCost(totals.cost)}
        </span>
        <span className="text-[12.5px] text-mac-ink3">
          {totals.calls.toLocaleString()} {totals.calls === 1 ? "request" : "requests"}
        </span>
      </div>
      <div className="flex items-center gap-1.5 mt-1.5 text-[12px] text-mac-ink3">
        <Zap size={11} strokeWidth={2} className="text-mac-ink4" />
        <span className="tnum">{fmtTokens(totals.tokens_total)}</span> tokens
        <span className="text-mac-ink4">·</span>
        <span className="tnum">{fmtTokens(totals.tokens_in)}</span> in
        <span className="text-mac-ink4">·</span>
        <span className="tnum">{fmtTokens(totals.tokens_out)}</span> out
      </div>
    </div>
  );
}

// Quick-link row in the "Jump back in" card.
function BriefRow({ icon: Ico, title, sub, last, onClick }:
  { icon: LucideIcon; title: string; sub: string; last?: boolean; onClick: () => void }) {
  return (
    <div onClick={onClick}
      className={`group flex items-center gap-3 py-2.5 px-1 cursor-pointer ${last ? "" : "border-b border-mac-stroke"}`}>
      <div className="h-7 w-7 shrink-0 rounded-[8px] grid place-items-center bg-mac-fillHi border border-mac-stroke">
        <Ico size={13} strokeWidth={2} className="text-mac-ink2" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] text-mac-ink truncate leading-tight">{title}</div>
        <div className="text-[11.5px] text-mac-ink3 truncate">{sub}</div>
      </div>
      <ArrowUpRight size={14} strokeWidth={2} className="shrink-0 text-mac-ink4 opacity-0 group-hover:opacity-100 transition-opacity" />
    </div>
  );
}

// Best-effort overdue check for a task's free-text `due` (Himmy stores things like
// "tomorrow", a date, or an ISO string). Only flags when we can parse a real past date.
function isOverdue(due: string | null): boolean {
  if (!due) return false;
  const d = new Date(due);
  if (isNaN(d.getTime())) return false;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return d.getTime() < today.getTime();
}

// ── Task priority + smart sort ────────────────────────────────────────────────────────────
// Priority is 0 none · 1 low · 2 medium · 3 high. Each level carries a label + a mac-* tone
// for the flag chip; level 0 renders nothing.
const PRIORITY_META: Record<number, { label: string; tone: string }> = {
  1: { label: "Low", tone: "text-mac-ink3" },
  2: { label: "Medium", tone: "text-mac-accentHi" },
  3: { label: "High", tone: "text-mac-red" },
};

// Sort epoch for a `due` value — parseable dates sort ascending (soonest first); unparseable
// / missing dues sink to the end so dated tasks lead.
function dueSortKey(due: string | null): number {
  if (!due) return Number.POSITIVE_INFINITY;
  const t = new Date(due).getTime();
  return isNaN(t) ? Number.POSITIVE_INFINITY : t;
}

// The "what to do next" comparator: open before done, then overdue first, then priority desc,
// then due ascending (soonest first), with a created-at tiebreak so the order is stable.
function compareTasks(a: Task, b: Task): number {
  if (a.done !== b.done) return a.done ? 1 : -1;               // open first
  const ao = isOverdue(a.due) ? 0 : 1, bo = isOverdue(b.due) ? 0 : 1;
  if (ao !== bo) return ao - bo;                                // overdue first
  if (a.priority !== b.priority) return b.priority - a.priority; // priority desc
  const ad = dueSortKey(a.due), bd = dueSortKey(b.due);
  if (ad !== bd) return ad - bd;                                // due asc (soonest)
  return (b.created_at || "").localeCompare(a.created_at || ""); // newest-first tiebreak
}

// A compact due label: a real date renders as e.g. "Jun 25"; anything unparseable (Himmy's
// free-text like "tomorrow") is shown verbatim.
function dueLabel(due: string): string {
  const d = new Date(due);
  if (isNaN(d.getTime())) return due;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// A small flag chip for priority ≥ 1; nothing for 0 (the common case).
function PriorityFlag({ priority, size = 11 }: { priority: number; size?: number }) {
  const meta = PRIORITY_META[priority];
  if (!meta) return null;
  return (
    <span className={`shrink-0 inline-flex items-center gap-1 text-[12px] ${meta.tone}`} title={`${meta.label} priority`}>
      <Flag size={size} strokeWidth={2} />
    </span>
  );
}

// Cycle a task's priority 0→1→2→3→0 on click — a zero-chrome way to set urgency inline.
function nextPriority(p: number): number { return (p + 1) % 4; }

function Step({ icon: Ico, title, sub, action, onClick, done, doneLabel, last }:
  { icon: LucideIcon; title: string; sub: string; action: string; onClick: () => void;
    done?: boolean; doneLabel?: string; last?: boolean }) {
  return (
    <div className={`flex items-center gap-3 py-3 px-1 ${last ? "" : "border-b border-mac-stroke"}`}>
      <div className={`h-8 w-8 shrink-0 rounded-[9px] grid place-items-center border ${
        done ? "bg-mac-accentDim border-transparent" : "bg-mac-fill border-mac-stroke"}`}>
        <Ico size={15} strokeWidth={2} className={done ? "text-mac-accentHi" : "text-mac-ink2"} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-[13.5px] text-mac-ink leading-tight">{title}</div>
        <div className="text-[12px] text-mac-ink3 leading-snug mt-0.5 truncate">{sub}</div>
      </div>
      {done ? (
        <span className="text-[12px] text-mac-ink3 shrink-0">{doneLabel}</span>
      ) : (
        <button onClick={onClick}
          className="shrink-0 h-7 px-3 rounded-[8px] text-[12.5px] text-mac-ink2 border border-mac-stroke hover:text-mac-ink hover:border-mac-strokeHi transition-colors">
          {action}
        </button>
      )}
    </div>
  );
}

/* ───────────────────────────────────────── library (a real reference manager) */
function Library({ onOpen }: { onOpen: (id: string) => void }) {
  const [items, setItems] = useState<Paper[]>([]);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [tags, setTags] = useState<{ tag: string; count: number }[]>([]);
  const [allCount, setAllCount] = useState(0);
  const [reading, setReading] = useState<Record<string, number>>({}); // item_id → engaged seconds
  const [activeCol, setActiveCol] = useState<string | null>(null);
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [doiOpen, setDoiOpen] = useState(false);
  const [doi, setDoi] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [newCol, setNewCol] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  // Recommended papers (arXiv suggestions from the user's research topics) — lives in Library.
  const [recMode, setRecMode] = useState(false);
  const [recs, setRecs] = useState<RecPaper[]>([]);
  const [recThreads, setRecThreads] = useState<RecThread[]>([]);
  const [recHero, setRecHero] = useState<RecPaper | null>(null);
  const [recAdded, setRecAdded] = useState<Record<string, "pending" | "added">>({});
  const [interests, setInterests] = useState<string[]>([]);
  const [interestInput, setInterestInput] = useState("");
  const [recLoading, setRecLoading] = useState(false);

  const loadMeta = async () => {
    try {
      const [c, t, all, rd] = await Promise.all([
        api.collections.list(), api.tags(), api.library.list(), api.reading.totals(),
      ]);
      setCollections(c.collections); setTags(t.tags); setAllCount(all.count); setReading(rd.totals || {});
    } catch { /* warming up */ }
  };
  const load = async () => {
    try {
      const r = await api.library.list(q, activeCol || "");
      setItems(activeTag ? r.items.filter((x) => x.tags.includes(activeTag)) : r.items);
    } catch { /* warming up */ }
  };
  useEffect(() => { loadMeta(); }, []);
  useEffect(() => { const t = setTimeout(load, 180); return () => clearTimeout(t); }, [q, activeCol, activeTag]);
  const refresh = async () => { await Promise.all([load(), loadMeta()]); };
  // Himmy added a paper by DOI/arXiv → refresh the list + counts live.
  useRefreshSignal("library", () => { refresh(); });

  // ---- Recommended papers ----
  const loadRecs = async (force = false) => {
    setRecLoading(true);
    try {
      const r = await api.news.recommendations(force);
      setRecs(r.papers || []); setRecThreads(r.threads || []); setRecHero(r.hero || null);
      // Served from a stale cache while a fresh batch computes in the background — quietly
      // re-fetch once it's ready so the user lands on the freshest set without a spinner.
      if (r.stale) setTimeout(() => loadRecs(false), 15000);
    } catch { setRecs([]); setRecThreads([]); setRecHero(null); }
    finally { setRecLoading(false); }
  };
  const saveInterests = async (list: string[]) => {
    setInterests(list);
    try { await api.news.setInterests(list); } catch { /* */ }
    loadRecs();
  };
  const addRec = async (p: RecPaper) => {
    const ident = p.arxiv || p.doi;
    if (!ident || recAdded[ident]) return;
    setRecAdded((m) => ({ ...m, [ident]: "pending" }));
    try {
      const r = await api.library.addDoi(ident);
      if (r.ok || r.duplicate) { setRecAdded((m) => ({ ...m, [ident]: "added" })); refresh(); }
      else setRecAdded((m) => { const n = { ...m }; delete n[ident]; return n; });
    } catch { setRecAdded((m) => { const n = { ...m }; delete n[ident]; return n; }); }
  };
  const openRecommended = () => {
    setRecMode(true); setActiveCol(null); setActiveTag(null);
    loadRecs();
    api.news.interests().then((r) => setInterests(r.interests || [])).catch(() => {});
  };

  // "Not interested": drop it everywhere immediately, then tell the backend to learn from it.
  const dismissRec = async (p: RecPaper) => {
    const id = p.doi || p.arxiv || "";
    const tkey = (p.title || "").trim().toLowerCase();
    // Match the backend filter: remove by identifier AND by title (so a duplicate preprint/published
    // pair with the same title both disappear at once).
    const keep = (x: RecPaper) =>
      !((id && (x.doi === id || x.arxiv === id)) || (tkey && (x.title || "").trim().toLowerCase() === tkey));
    setRecs((rs) => rs.filter(keep));
    setRecHero((h) => (h && !keep(h) ? null : h));
    setRecThreads((ts) => ts
      .map((t) => { const papers = t.papers.filter(keep); return { ...t, papers, count: papers.length }; })
      .filter((t) => t.papers.length));
    try { await api.news.dismissRec({ doi: p.doi, title: p.title, concepts: p.concepts }); } catch { /* best effort */ }
  };

  const addFiles = async (paths: string[]) => {
    const pdfs = paths.filter((p) => p.toLowerCase().endsWith(".pdf"));
    if (!pdfs.length) return;
    setBusy(true); setNote(null);
    try { await api.library.addFiles(pdfs); await refresh(); }
    catch (e: any) { setNote(e.message); } finally { setBusy(false); }
  };
  const addPapers = async () => {
    const s = (window as any).himmy;
    if (!s?.pickPapers) { setNote("The file picker needs the desktop app."); return; }
    const paths: string[] = await s.pickPapers();
    if (paths?.length) addFiles(paths);
  };
  const addByDoi = async () => {
    const v = doi.trim();
    if (!v || busy) return;
    setBusy(true); setNote(null);
    try {
      const r = await api.library.addDoi(v);
      if (r.ok) { setDoi(""); setDoiOpen(false); await refresh(); }
      else setNote(r.message ?? "Couldn't add that one.");
    } catch (e: any) { setNote(e.message); } finally { setBusy(false); }
  };
  const onDrop = (e: React.DragEvent) => {
    e.preventDefault(); setDragOver(false);
    if (!e.dataTransfer.files.length) return; // ignore in-app item drags
    const s = (window as any).himmy;
    const paths = Array.from(e.dataTransfer.files).map((f) => s?.pathForFile?.(f) || "").filter(Boolean);
    if (paths.length) addFiles(paths);
  };
  const remove = async (id: string) => {
    setItems((it) => it.filter((x) => x.id !== id));
    try { await api.library.remove(id); } finally { refresh(); }
  };
  const addToCollection = async (cid: string, id: string) => { await api.collections.addItem(cid, id); refresh(); };
  const createCollection = async (name: string) => {
    if (name.trim()) await api.collections.create(name.trim());
    setNewCol(null); loadMeta();
  };
  const deleteCollection = async (cid: string) => {
    if (activeCol === cid) setActiveCol(null);
    await api.collections.remove(cid); refresh();
  };
  const activeName = activeCol ? collections.find((c) => c.id === activeCol)?.name : activeTag ? `#${activeTag}` : "Library";
  const shownRead = items.reduce((a, p) => a + (reading[p.id] || 0), 0); // total engaged time on the visible papers

  return (
    <div className="h-full flex relative"
      onDragOver={(e) => { if (Array.from(e.dataTransfer.types).includes("Files")) { e.preventDefault(); setDragOver(true); } }}
      onDragLeave={(e) => { if (e.currentTarget === e.target) setDragOver(false); }}
      onDrop={onDrop}>
      <CollectionsRail
        collections={collections} tags={tags} total={allCount}
        activeCol={activeCol} activeTag={activeTag} recActive={recMode}
        onPick={(c) => { setRecMode(false); setActiveCol(c); setActiveTag(null); }}
        onPickTag={(t) => { setRecMode(false); setActiveTag(t === activeTag ? null : t); setActiveCol(null); }}
        onRecommended={openRecommended}
        onDropItem={addToCollection}
        newCol={newCol} setNewCol={setNewCol} onCreate={createCollection} onDelete={deleteCollection}
      />

      {recMode ? (
        <RecommendedMain recs={recs} threads={recThreads} hero={recHero} recAdded={recAdded}
          loading={recLoading} interests={interests}
          interestInput={interestInput} setInterestInput={setInterestInput}
          onSaveInterests={saveInterests} onAdd={addRec} onRefresh={() => loadRecs(true)}
          onAsk={askHimmyAboutRec} onDismiss={dismissRec} />
      ) : (
      <div className="flex-1 flex flex-col min-w-0">
        <div className="shrink-0 h-[60px] px-6 flex items-center justify-between">
          <div className="flex items-baseline gap-2.5 min-w-0">
            <h1 className="font-display text-[19px] font-semibold tracking-[-0.01em] truncate">{activeName}</h1>
            <span className="text-[12.5px] text-mac-ink3 tnum shrink-0">{items.length} {items.length === 1 ? "paper" : "papers"}</span>
            {shownRead >= 60 && (
              <span className="text-[12.5px] text-mac-accentHi tnum shrink-0 flex items-center gap-1">
                <Clock size={12} strokeWidth={2} /> {fmtReadShort(shownRead)} read
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-2 h-8 rounded-[9px] bg-mac-fill border border-mac-stroke px-2.5 w-52">
              <Search size={13} strokeWidth={2} className="text-mac-ink3" />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search library"
                className="flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-mac-ink3" />
            </div>
            <button onClick={() => setExporting(true)} title="Cite / export bibliography"
              className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors flex items-center gap-1.5">
              <Quote size={13} /> Cite
            </button>
            <button onClick={() => { setDoiOpen((o) => !o); setNote(null); }}
              className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">
              Add by DOI
            </button>
            <button onClick={addPapers} disabled={busy}
              className="h-8 px-3.5 rounded-[9px] bg-mac-accent text-[12.5px] font-medium text-white hover:bg-mac-accentHi transition-colors flex items-center gap-1.5 disabled:opacity-50">
              {busy ? <Loader2 size={13} className="animate-spin" /> : <Plus size={14} strokeWidth={2.5} />}
              Add papers
            </button>
          </div>
        </div>

        {doiOpen && (
          <div className="shrink-0 px-6 pb-3">
            <div className="flex items-center gap-2 rounded-[10px] bg-mac-fill border border-mac-stroke px-3 h-10">
              <Link size={14} className="text-mac-ink3 shrink-0" />
              <input autoFocus value={doi} onChange={(e) => setDoi(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") addByDoi(); if (e.key === "Escape") setDoiOpen(false); }}
                placeholder="Paste a DOI or arXiv id — e.g. 10.1038/nphys1170  or  1706.03762"
                className="flex-1 bg-transparent text-[13px] outline-none placeholder:text-mac-ink3" />
              <button onClick={addByDoi} disabled={busy || !doi.trim()}
                className="h-7 px-3 rounded-[7px] bg-mac-accent text-white text-[12px] font-medium disabled:opacity-40 hover:bg-mac-accentHi transition-colors">
                {busy ? "Adding…" : "Add"}
              </button>
            </div>
            {note && <p className="text-[12px] text-mac-orange mt-1.5 px-1">{note}</p>}
          </div>
        )}

        <div className="flex-1 min-h-0 overflow-auto px-6 pb-8">
          {items.length === 0 ? (
            activeCol || activeTag
              ? <div className="h-full grid place-items-center text-[13px] text-mac-ink3">Nothing here yet. Drag papers onto a collection, or tag them.</div>
              : <EmptyLibrary onAdd={addPapers} onDoi={() => setDoiOpen(true)} dragOver={dragOver} />
          ) : (
            <PaperTable items={items} reading={reading} onRemove={remove} onOpen={onOpen} />
          )}
        </div>
      </div>
      )}

      {dragOver && (
        <div className="absolute inset-0 z-10 grid place-items-center bg-black/40 pointer-events-none">
          <div className="flex items-center gap-2 text-[13px] text-mac-ink bg-[rgba(40,41,47,0.95)] backdrop-blur-xl border border-mac-strokeHi rounded-xl px-5 py-3 shadow-pop">
            <FileUp size={18} className="text-mac-accentHi" /> Drop PDFs to add them
          </div>
        </div>
      )}

      {exporting && <CiteExport items={items} onClose={() => setExporting(false)} />}
    </div>
  );
}

function RecommendedMain({ recs, threads, hero, recAdded, loading, interests, interestInput, setInterestInput, onSaveInterests, onAdd, onRefresh, onAsk, onDismiss }: {
  recs: RecPaper[]; threads: RecThread[]; hero: RecPaper | null;
  recAdded: Record<string, "pending" | "added">; loading: boolean;
  interests: string[]; interestInput: string; setInterestInput: (v: string) => void;
  onSaveInterests: (list: string[]) => void; onAdd: (p: RecPaper) => void; onRefresh: () => void;
  onAsk: (p: RecPaper) => void; onDismiss: (p: RecPaper) => void;
}) {
  const [q, setQ] = useState("");
  const ql = q.trim().toLowerCase();
  const shown = ql ? recs.filter((p) => `${p.title} ${(p.authors || []).join(" ")} ${p.abstract}`.toLowerCase().includes(ql)) : recs;
  const cardState = (p: RecPaper) => { const ident = p.arxiv || p.doi; return ident ? recAdded[ident] : undefined; };
  const heroKey = hero ? (hero.doi || hero.arxiv || hero.title) : "";
  const hasDigest = !ql && (threads.length > 0 || !!hero);
  const subtitle = ql
    ? `${shown.length} result${shown.length === 1 ? "" : "s"}`
    : threads.length
      ? `${threads.length} research thread${threads.length === 1 ? "" : "s"} · ${recs.length} papers`
      : `${recs.length} papers matched to your reading`;

  return (
    <div className="flex-1 flex flex-col min-w-0">
      <div className="shrink-0 h-[60px] px-6 flex items-center justify-between">
        <div className="flex items-baseline gap-2.5 min-w-0">
          <h1 className="font-display text-[19px] font-semibold tracking-[-0.01em] truncate">Recommended</h1>
          <span className="text-[12.5px] text-mac-ink3 tnum shrink-0">{subtitle}</span>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-2 h-8 rounded-[9px] bg-mac-fill border border-mac-stroke px-2.5 w-52">
            <Search size={13} strokeWidth={2} className="text-mac-ink3" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search papers"
              className="flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-mac-ink3" />
          </div>
          <button onClick={onRefresh} disabled={loading}
            className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors flex items-center gap-1.5 disabled:opacity-50">
            {loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} Refresh
          </button>
        </div>
      </div>

      <div className="shrink-0 px-6 pb-2.5 flex flex-wrap items-center gap-1.5">
        <span className="text-[10px] uppercase tracking-wide text-mac-ink3 mr-1">Your topics</span>
        {interests.map((t) => (
          <span key={t} className="inline-flex items-center gap-1 text-[12px] text-mac-ink2 bg-mac-fill border border-mac-stroke rounded-full pl-2.5 pr-1.5 py-0.5">
            {t}<button onClick={() => onSaveInterests(interests.filter((x) => x !== t))} className="text-mac-ink3 hover:text-mac-red"><X size={11} /></button>
          </span>
        ))}
        <input value={interestInput} onChange={(e) => setInterestInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && interestInput.trim()) { onSaveInterests([...interests, interestInput.trim()]); setInterestInput(""); } }}
          placeholder="add a topic…" className="text-[12px] bg-transparent outline-none w-28 text-mac-ink placeholder:text-mac-ink3" />
      </div>

      <div className="flex-1 min-h-0 overflow-auto px-6 pb-9">
        {loading && recs.length === 0 ? (
          <div className="h-48 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
        ) : ql ? (
          shown.length === 0
            ? <div className="h-48 grid place-items-center text-[13px] text-mac-ink3">No papers match “{q}”.</div>
            : <div className="grid grid-cols-2 xl:grid-cols-3 gap-4 auto-rows-fr">
                {shown.map((p, i) => <RecCard key={(p.doi || p.arxiv || p.title) + i} p={p} state={cardState(p)} onAdd={() => onAdd(p)} onAsk={() => onAsk(p)} onDismiss={() => onDismiss(p)} />)}
              </div>
        ) : hasDigest ? (
          <div className="space-y-8 pt-1">
            {hero && <HeroCard p={hero} state={cardState(hero)} onAdd={() => onAdd(hero)} onAsk={() => onAsk(hero)} onDismiss={() => onDismiss(hero)} />}
            {threads.map((t) => {
              const papers = t.papers.filter((p) => (p.doi || p.arxiv || p.title) !== heroKey);
              if (!papers.length) return null;
              return <ThreadRail key={t.label} label={t.label} count={t.count} papers={papers} cardState={cardState} onAdd={onAdd} onAsk={onAsk} onDismiss={onDismiss} />;
            })}
          </div>
        ) : recs.length ? (
          <div className="grid grid-cols-2 xl:grid-cols-3 gap-4 auto-rows-fr">
            {recs.map((p, i) => <RecCard key={(p.doi || p.arxiv || p.title) + i} p={p} state={cardState(p)} onAdd={() => onAdd(p)} onAsk={() => onAsk(p)} onDismiss={() => onDismiss(p)} />)}
          </div>
        ) : (
          <RecsEmpty />
        )}
      </div>
    </div>
  );
}

/* The featured "top pick" — a wide, glowing hero above the research-thread rails. */
function HeroCard({ p, state, onAdd, onAsk, onDismiss }: {
  p: RecPaper; state?: "pending" | "added"; onAdd: () => void; onAsk: () => void; onDismiss: () => void;
}) {
  const ident = p.arxiv || p.doi;
  const authors = (p.authors || []).slice(0, 4).join(", ") + ((p.authors || []).length > 4 ? " et al." : "");
  return (
    <div className="group relative overflow-hidden rounded-2xl border border-mac-strokeHi bg-gradient-to-br from-[rgba(10,132,255,0.10)] to-mac-fill p-5">
      <div className="pointer-events-none absolute -top-16 -right-10 h-44 w-72 rounded-full bg-mac-accent/10 blur-3xl" />
      <button onClick={onDismiss} title="Not interested"
        className="absolute top-3 right-3 z-10 h-7 w-7 grid place-items-center rounded-full text-mac-ink3 hover:text-mac-ink hover:bg-mac-fillHi opacity-0 group-hover:opacity-100 transition-opacity">
        <X size={15} />
      </button>
      <div className="relative">
        <div className="flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-mac-accentHi mb-2">
          <Sparkles size={12} strokeWidth={2.2} /> TOP PICK FOR YOU
        </div>
        <div className="flex items-center gap-1.5 text-[11.5px] text-mac-ink3 mb-1">
          <span className="font-medium text-mac-ink2">{p.venue || "Working paper"}</span>
          {p.year && <span>· {p.year}</span>}
          {!!p.citations && p.citations > 0 && <span>· {p.citations.toLocaleString()} cites</span>}
        </div>
        <h2 className="font-display text-[19px] font-semibold leading-snug tracking-[-0.01em] text-mac-ink max-w-[70ch]">{p.title}</h2>
        {authors && <div className="text-[12.5px] text-mac-ink3 mt-1">{authors}</div>}
        {(p.tldr || p.abstract) && (
          <div className="flex items-start gap-1.5 mt-2.5 max-w-[80ch]">
            {p.tldr && <Sparkles size={13} strokeWidth={2} className="text-mac-accentHi shrink-0 mt-0.5" />}
            <p className="text-[13px] text-mac-ink2 leading-relaxed line-clamp-2">{p.tldr || p.abstract}</p>
          </div>
        )}
        {p.why && (
          <div className="flex items-center gap-1.5 mt-3 text-[12px] text-mac-accentHi">
            <BookText size={12} strokeWidth={2} className="shrink-0" /> <span>{p.why}</span>
          </div>
        )}
        <div className="flex items-center gap-2 mt-4">
          <button onClick={onAdd} disabled={!ident || state === "pending" || state === "added"}
            className={`h-9 px-4 rounded-[10px] text-[13px] font-medium inline-flex items-center gap-1.5 transition-colors disabled:opacity-60 ${
              state === "added" ? "bg-mac-green/15 text-mac-green border border-mac-green/30" : "bg-mac-accent text-white hover:bg-mac-accentHi"}`}>
            {state === "pending" ? <Loader2 size={14} className="animate-spin" /> : state === "added" ? <Check size={14} strokeWidth={2.5} /> : <Plus size={14} strokeWidth={2.5} />}
            {state === "added" ? "In Library" : "Add to Library"}
          </button>
          <button onClick={onAsk} title="Ask Himmy about this paper"
            className="h-9 px-3.5 rounded-[10px] bg-mac-fillHi border border-mac-stroke text-[13px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors inline-flex items-center gap-1.5">
            <MessageSquare size={13} /> Ask Himmy
          </button>
          {p.url && (
            <a href={p.url} target="_blank" rel="noreferrer"
              className="h-9 px-3.5 rounded-[10px] bg-mac-fillHi border border-mac-stroke text-[13px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors inline-flex items-center gap-1.5">
              <ExternalLink size={13} /> Open
            </a>
          )}
        </div>
      </div>
    </div>
  );
}

/* One research thread = a labelled, horizontally-scrolling rail of recommendation cards. */
function ThreadRail({ label, count, papers, cardState, onAdd, onAsk, onDismiss }: {
  label: string; count: number; papers: RecPaper[];
  cardState: (p: RecPaper) => "pending" | "added" | undefined;
  onAdd: (p: RecPaper) => void; onAsk: (p: RecPaper) => void; onDismiss: (p: RecPaper) => void;
}) {
  return (
    <section>
      <div className="flex items-baseline gap-2 mb-2.5">
        <h3 className="font-display text-[15px] font-semibold tracking-[-0.01em] text-mac-ink capitalize">{label}</h3>
        <span className="text-[11.5px] text-mac-ink3 tnum">{count} papers</span>
      </div>
      <div className="flex gap-3.5 overflow-x-auto pb-2 -mx-1 px-1 snap-x">
        {papers.map((p, i) => (
          <div key={(p.doi || p.arxiv || p.title) + i} className="min-w-[288px] max-w-[288px] snap-start">
            <RecCard p={p} state={cardState(p)} onAdd={() => onAdd(p)} onAsk={() => onAsk(p)} onDismiss={() => onDismiss(p)} />
          </div>
        ))}
      </div>
    </section>
  );
}

function CiteExport({ items, onClose }: { items: Paper[]; onClose: () => void }) {
  const [style, setStyle] = useState<"apa" | "mla" | "bibtex">("apa");
  const [copied, setCopied] = useState(false);
  const sorted = [...items].sort((a, b) => (a.authors[0] || a.title).localeCompare(b.authors[0] || b.title));
  const fmt = (p: Paper) => (style === "apa" ? apa(p) : style === "mla" ? mla(p) : bibtex(p));
  const text = sorted.map(fmt).join("\n\n");
  const copy = () => { navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500); };
  const download = () => {
    const blob = new Blob([sorted.map(bibtex).join("\n\n") + "\n"], { type: "application/x-bibtex" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "himmy.bib"; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  return (
    <div className="absolute inset-0 z-40 grid place-items-center bg-black/45" onMouseDown={onClose}>
      <div onMouseDown={(e) => e.stopPropagation()}
        className="w-[660px] max-w-[calc(100%-3rem)] max-h-[80vh] flex flex-col rounded-2xl bg-[rgba(30,31,37,0.97)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden">
        <div className="h-12 px-4 flex items-center justify-between border-b border-mac-stroke">
          <div className="flex items-center gap-2 text-[13px]">
            <Quote size={14} className="text-mac-accentHi" />
            <span className="font-medium text-mac-ink">Bibliography</span>
            <span className="text-mac-ink3">{sorted.length} {sorted.length === 1 ? "paper" : "papers"}</span>
          </div>
          <button onClick={onClose} className="text-mac-ink3 hover:text-mac-ink"><X size={16} /></button>
        </div>
        <div className="px-4 py-2.5 flex items-center gap-1.5 border-b border-mac-stroke">
          {(["apa", "mla", "bibtex"] as const).map((s) => (
            <button key={s} onClick={() => setStyle(s)}
              className={`h-7 px-3 rounded-md text-[12px] transition-colors ${style === s ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink2 hover:text-mac-ink"}`}>
              {s === "bibtex" ? "BibTeX" : s.toUpperCase()}
            </button>
          ))}
          <div className="ml-auto flex items-center gap-2">
            <button onClick={copy}
              className="h-7 px-3 rounded-md bg-mac-fill border border-mac-stroke text-[12px] text-mac-ink2 hover:text-mac-ink flex items-center gap-1.5">
              {copied ? <Check size={12} className="text-mac-green" /> : <Copy size={12} />} {copied ? "Copied" : "Copy all"}
            </button>
            <button onClick={download}
              className="h-7 px-3 rounded-md bg-mac-accent text-white text-[12px] font-medium hover:bg-mac-accentHi flex items-center gap-1.5">
              <FileDown size={12} /> Export .bib
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-auto p-4 text-[12.5px] leading-relaxed whitespace-pre-wrap font-mono text-mac-ink2">
          {text || "No papers to cite."}
        </div>
      </div>
    </div>
  );
}

function CollectionsRail({ collections, tags, total, activeCol, activeTag, recActive, onPick, onPickTag, onRecommended, onDropItem, newCol, setNewCol, onCreate, onDelete }: {
  collections: Collection[]; tags: { tag: string; count: number }[]; total: number;
  activeCol: string | null; activeTag: string | null; recActive: boolean;
  onPick: (c: string | null) => void; onPickTag: (t: string) => void; onRecommended: () => void;
  onDropItem: (cid: string, id: string) => void;
  newCol: string | null; setNewCol: (v: string | null) => void;
  onCreate: (name: string) => void; onDelete: (cid: string) => void;
}) {
  const [dropCol, setDropCol] = useState<string | null>(null);
  const allActive = !activeCol && !activeTag && !recActive;
  return (
    <aside className="w-[210px] shrink-0 border-r border-mac-stroke flex flex-col py-3 px-2 overflow-auto">
      <button onClick={() => onPick(null)}
        className={`w-full flex items-center gap-2 h-8 px-2.5 rounded-md text-[12.5px] transition-colors ${allActive ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink2 hover:bg-mac-fill"}`}>
        <LibraryIcon size={14} className={allActive ? "text-mac-accentHi" : "text-mac-ink3"} /> <span className="flex-1 text-left">All Papers</span>
        <span className="text-[11px] text-mac-ink3 tnum">{total}</span>
      </button>
      <button onClick={onRecommended}
        className={`w-full flex items-center gap-2 h-8 px-2.5 mt-0.5 rounded-md text-[12.5px] transition-colors ${recActive ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink2 hover:bg-mac-fill"}`}>
        <Sparkles size={14} className={recActive ? "text-mac-accentHi" : "text-mac-ink3"} /> <span className="flex-1 text-left">Recommended</span>
      </button>

      <div className="mt-4 mb-1 px-2 flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wide text-mac-ink3">Collections</span>
        <button onClick={() => setNewCol("")} title="New collection" className="text-mac-ink3 hover:text-mac-ink"><Plus size={13} /></button>
      </div>
      {collections.map((c) => (
        <div key={c.id}
          onDragOver={(e) => { if (Array.from(e.dataTransfer.types).includes("text/himmy-item")) { e.preventDefault(); e.stopPropagation(); setDropCol(c.id); } }}
          onDragLeave={() => setDropCol(null)}
          onDrop={(e) => { e.stopPropagation(); setDropCol(null); const id = e.dataTransfer.getData("text/himmy-item"); if (id) onDropItem(c.id, id); }}
          className={`group w-full flex items-center gap-2 h-8 px-2.5 rounded-md text-[12.5px] cursor-pointer transition-colors ${activeCol === c.id ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink2 hover:bg-mac-fill"} ${dropCol === c.id ? "ring-1 ring-mac-accent bg-mac-accentDim" : ""}`}
          onClick={() => onPick(c.id)}>
          <Folder size={14} className="text-mac-ink3 shrink-0" />
          <span className="flex-1 text-left truncate">{c.name}</span>
          <span className="text-[11px] text-mac-ink3 tnum group-hover:hidden">{c.count}</span>
          <button onClick={(e) => { e.stopPropagation(); onDelete(c.id); }} className="hidden group-hover:block text-mac-ink3 hover:text-mac-red"><X size={12} /></button>
        </div>
      ))}
      {newCol !== null && (
        <input autoFocus value={newCol} onChange={(e) => setNewCol(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") onCreate(newCol); if (e.key === "Escape") setNewCol(null); }}
          onBlur={() => onCreate(newCol)} placeholder="Collection name"
          className="mt-0.5 mx-1 h-7 px-2 rounded-md bg-mac-fill border border-mac-accent text-[12.5px] text-mac-ink outline-none" />
      )}

      {tags.length > 0 && (
        <>
          <div className="mt-4 mb-1.5 px-2 text-[10px] uppercase tracking-wide text-mac-ink3">Tags</div>
          <div className="flex flex-wrap gap-1 px-1">
            {tags.map((t) => (
              <button key={t.tag} onClick={() => onPickTag(t.tag)}
                className={`inline-flex items-center gap-1 text-[11.5px] rounded-full px-2 py-0.5 border transition-colors ${activeTag === t.tag ? "bg-mac-accentDim border-mac-accent text-mac-ink" : "bg-mac-fill border-mac-stroke text-mac-ink2 hover:text-mac-ink"}`}>
                <Hash size={9} className="text-mac-ink3" />{t.tag}
              </button>
            ))}
          </div>
        </>
      )}
    </aside>
  );
}

/* ---- "What Himmy knows about you" — the personalization profile ---- */
function ChipList({ label, items, onChange, placeholder }:
  { label: string; items: string[]; onChange: (v: string[]) => void; placeholder: string }) {
  const [draft, setDraft] = useState("");
  const add = () => { const v = draft.trim(); if (v && !items.some((x) => x.toLowerCase() === v.toLowerCase())) onChange([...items, v]); setDraft(""); };
  return (
    <div>
      <div className="text-[10.5px] uppercase tracking-wide text-mac-ink3 mb-1.5">{label}</div>
      <div className="flex flex-wrap items-center gap-1.5 rounded-lg bg-mac-fill border border-mac-stroke px-2 py-1.5 min-h-[34px]">
        {items.map((t, i) => (
          <span key={i} className="inline-flex items-center gap-1 text-[12px] text-mac-ink2 bg-mac-fillHi border border-mac-stroke rounded-full pl-2.5 pr-1.5 py-0.5">
            {t}
            <button onClick={() => onChange(items.filter((_, j) => j !== i))} className="text-mac-ink3 hover:text-mac-red"><X size={11} /></button>
          </span>
        ))}
        <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={placeholder}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }} onBlur={add}
          className="text-[12px] bg-transparent outline-none flex-1 min-w-[90px] text-mac-ink placeholder:text-mac-ink3" />
      </div>
    </div>
  );
}

function LearnedList({ label, items }: { label: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <div className="text-[12px] text-mac-ink2 leading-snug">
      <span className="text-mac-ink3">{label}: </span>{items.join(" · ")}
    </div>
  );
}

/* The "vault" — labeled facts Himmy uses when acting for you (booking, drafting, planning). */
const VAULT_FIELDS = [
  "Home airport", "Preferred airline", "Seat preference", "Budget ceiling",
  "Home address", "Dietary needs", "Loyalty numbers", "Spend limit per action",
  "Favourite food", "Food budget",
];

// How Himmy talks to you — the personality tuner (a tone preset + an optional free-text note).
function HimmyPersonality() {
  const [cfg, setCfg] = useState<AssistantConfig | null>(null);
  const [styles, setStyles] = useState<AssistantStyleOpt[]>([]);
  const [vision, setVision] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api.assistant.get().then((r) => {
      setCfg(r.assistant); setStyles(r.styles || []); setVision(!!r.vision_available);
    }).catch(() => {});
  }, []);

  if (!cfg) return <div className="text-[12px] text-mac-ink3">Loading Himmy's personality…</div>;

  const save = async (next: AssistantConfig) => {
    setCfg(next); setSaving(true); setSaved(false);
    try { await api.assistant.set(next.style, next.note); setSaved(true); setTimeout(() => setSaved(false), 1500); }
    catch { /* ignore */ } finally { setSaving(false); }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <MessageCircle size={14} className="text-mac-accentHi" />
        <span className="text-[13px] font-medium text-mac-ink">How Himmy talks to you</span>
        {saving && <Loader2 size={12} className="animate-spin text-mac-ink3" />}
        {saved && <span className="text-[11px] text-mac-accentHi flex items-center gap-1"><Check size={11} /> saved</span>}
      </div>
      <p className="text-[12px] text-mac-ink3 leading-snug -mt-1">
        Pick a vibe — it shapes Himmy's tone everywhere (here and on Telegram). Himmy stays honest
        and careful with real actions no matter what you choose.
      </p>
      <div className="grid grid-cols-2 gap-2">
        {styles.map((s) => {
          const active = cfg.style === s.id;
          return (
            <button key={s.id} onClick={() => save({ ...cfg, style: s.id })}
              className={`text-left rounded-xl border p-2.5 transition-colors ${active
                ? "border-mac-accent bg-mac-accent/10" : "border-mac-stroke bg-mac-fill hover:border-mac-strokeHi"}`}>
              <div className="flex items-center gap-1.5">
                <span className={`text-[12.5px] font-medium ${active ? "text-mac-ink" : "text-mac-ink2"}`}>{s.label}</span>
                {active && <Check size={13} className="text-mac-accentHi" />}
              </div>
              <p className="text-[11px] text-mac-ink3 leading-snug mt-0.5">{s.blurb}</p>
            </button>
          );
        })}
      </div>
      {cfg.style === "custom" && (
        <textarea value={cfg.note} onChange={(e) => setCfg({ ...cfg, note: e.target.value })}
          onBlur={() => save(cfg)} rows={2}
          placeholder="e.g. Be like a calm, witty Nepali friend who keeps me on track and roasts me a little when I procrastinate."
          className="w-full resize-none rounded-lg bg-mac-fill border border-mac-stroke px-2.5 py-1.5 text-[13px] text-mac-ink outline-none focus:border-mac-accent placeholder:text-mac-ink3" />
      )}
      {!vision && (
        <p className="text-[11px] text-mac-orange leading-snug">
          Heads up: your current model can't read images. Switch to OpenRouter (gemini-2.5-flash) in
          Preferences so Himmy can read screenshots & photos you send.
        </p>
      )}
    </div>
  );
}

// "Files Himmy has read" — the uploads (chat + Telegram) it keeps in its knowledge; remove to forget.
function FilesSection() {
  const [items, setItems] = useState<AttachmentItem[] | null>(null);
  const load = () => api.attachments.list().then((r) => setItems(r.attachments)).catch(() => setItems([]));
  useEffect(() => { load(); }, []);

  const remove = async (id: string) => {
    setItems((xs) => (xs || []).filter((x) => x.id !== id));
    try { await api.attachments.remove(id); } catch { load(); }
  };

  return (
    <div className="space-y-3">
      <SectionHeader title="Files Himmy has read"
        sub="Files, screenshots and voice notes you've sent Himmy (in chat or on Telegram). Himmy keeps their text in its knowledge so it can answer about them later. Remove one to make Himmy forget it." />
      {items === null ? (
        <div className="text-[12px] text-mac-ink3">Loading…</div>
      ) : items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-mac-stroke p-6 text-center">
          <Paperclip size={20} className="mx-auto text-mac-ink3" />
          <p className="text-[12.5px] text-mac-ink2 mt-2">No files yet.</p>
          <p className="text-[11.5px] text-mac-ink3 mt-0.5">Drop a file into the ⌘K chat, or send one on Telegram.</p>
        </div>
      ) : (
        <div className="space-y-1.5">
          {items.map((f) => (
            <div key={f.id} className="group flex items-center gap-2.5 rounded-lg bg-mac-fill border border-mac-stroke px-3 py-2">
              <span className="shrink-0 h-7 w-7 grid place-items-center rounded-md bg-mac-fillHi text-mac-accentHi">{fileKindIcon(f.kind, 14)}</span>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5">
                  <span className="truncate text-[12.5px] text-mac-ink">{f.name}</span>
                  {f.source === "telegram" && <Send size={10} className="text-mac-ink3 shrink-0" />}
                </div>
                <div className="text-[11px] text-mac-ink3 truncate">
                  {f.chars > 0 ? `${(f.chars / 1000).toFixed(f.chars < 1000 ? 0 : 1)}k chars` : "not read"}
                  {f.size ? ` · ${prettyBytes(f.size)}` : ""}
                  {f.preview ? ` · ${f.preview}` : ""}
                </div>
              </div>
              <button onClick={() => remove(f.id)} title="Forget this file"
                className="shrink-0 opacity-0 group-hover:opacity-100 h-7 w-7 grid place-items-center rounded-md text-mac-ink3 hover:text-mac-red hover:bg-mac-fillHi transition-all">
                <Trash2 size={14} strokeWidth={2} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ProfileSettings() {
  const [prof, setProf] = useState<UserProfile | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [learning, setLearning] = useState(false);
  const [learnMsg, setLearnMsg] = useState<string | null>(null);

  useEffect(() => { api.profile.get().then((r) => setProf(r.profile)).catch(() => {}); }, []);

  if (!prof) return <div className="text-[12px] text-mac-ink3">Loading what Himmy knows about you…</div>;

  const u = prof.user;
  const setU = (patch: Partial<ProfileLayer>) => setProf({ ...prof, user: { ...prof.user, ...patch } });
  const setDetail = (k: string, v: string) => {
    const details = { ...(u.details || {}) };
    if (v.trim()) details[k] = v; else delete details[k];
    setU({ details });
  };

  const save = async () => {
    setSaving(true); setSaved(false);
    try { const r = await api.profile.saveUser(prof.user); setProf(r.profile); setSaved(true); setTimeout(() => setSaved(false), 1600); }
    catch { /* ignore */ } finally { setSaving(false); }
  };
  const learn = async () => {
    setLearning(true); setLearnMsg(null);
    try {
      const r = await api.profile.learn();
      setProf(r.profile);
      if (!r.ok) setLearnMsg(r.message || "Nothing new to learn yet.");
    } catch (e: any) { setLearnMsg(e.message || "Couldn't refresh."); }
    finally { setLearning(false); }
  };

  const l = prof.learned;
  const learnedEmpty = !(l.about || l.voice || l.projects.length || l.people.length || l.topics.length || l.preferences.length);
  const ago = prof.learned_at ? fmtAgo(new Date(prof.learned_at * 1000).toISOString()) : null;

  return (
    <div className="space-y-3.5">
      <div className="flex items-center gap-2">
        <Sparkles size={14} className="text-mac-accentHi" />
        <span className="text-[13px] font-medium text-mac-ink">What Himmy knows about you</span>
      </div>
      <p className="text-[12px] text-mac-ink3 leading-snug -mt-1">
        Himmy uses this on every answer and action, so its help fits you. Tell it what matters, and
        let it learn the rest from your library, notes, and tasks.
      </p>

      <label className="block">
        <span className="block text-[10.5px] uppercase tracking-wide text-mac-ink3 mb-1">About you</span>
        <textarea value={u.about} onChange={(e) => setU({ about: e.target.value })} rows={2}
          placeholder="e.g. I'm a founder researching agricultural economics and conflict; I prefer concise, specific answers."
          className="w-full resize-none rounded-lg bg-mac-fill border border-mac-stroke px-2.5 py-1.5 text-[13px] text-mac-ink outline-none focus:border-mac-accent placeholder:text-mac-ink3" />
      </label>
      <label className="block">
        <span className="block text-[10.5px] uppercase tracking-wide text-mac-ink3 mb-1">How you write</span>
        <textarea value={u.voice} onChange={(e) => setU({ voice: e.target.value })} rows={2}
          placeholder="e.g. Concise and direct, lowercase, warm but to the point; I sign off with “thanks”."
          className="w-full resize-none rounded-lg bg-mac-fill border border-mac-stroke px-2.5 py-1.5 text-[13px] text-mac-ink outline-none focus:border-mac-accent placeholder:text-mac-ink3" />
        <span className="block text-[11px] text-mac-ink3 leading-snug mt-1">Himmy matches this voice when it drafts mail or messages for you.</span>
      </label>
      <ChipList label="Current projects" items={u.projects} onChange={(v) => setU({ projects: v })} placeholder="add a project…" />
      <ChipList label="Key people" items={u.people} onChange={(v) => setU({ people: v })} placeholder="add a name…" />
      <ChipList label="Topics you care about" items={u.topics} onChange={(v) => setU({ topics: v })} placeholder="add a topic…" />
      <ChipList label="How Himmy should help" items={u.preferences} onChange={(v) => setU({ preferences: v })} placeholder="add a preference…" />

      {/* the vault — details Himmy uses to ACT for you (book, draft, plan) without re-asking */}
      <div>
        <span className="block text-[10.5px] uppercase tracking-wide text-mac-ink3 mb-2">Your details (for booking &amp; actions)</span>
        <div className="grid grid-cols-2 gap-x-3 gap-y-2.5">
          {VAULT_FIELDS.map((f) => (
            <label key={f} className="block">
              <span className="block text-[10.5px] text-mac-ink3 mb-1">{f}</span>
              <input value={u.details?.[f] || ""} onChange={(e) => setDetail(f, e.target.value)}
                placeholder="—"
                className="w-full h-8 rounded-lg bg-mac-fill border border-mac-stroke px-2.5 text-[12.5px] text-mac-ink outline-none focus:border-mac-accent placeholder:text-mac-ink3" />
            </label>
          ))}
        </div>
        <p className="text-[11px] text-mac-ink3 leading-snug mt-2">
          Himmy uses these when it acts for you — so "book me a flight" already knows your airport, airline, and budget.
        </p>
      </div>

      <div className="flex items-center gap-2">
        <button onClick={save} disabled={saving}
          className="h-8 px-3.5 rounded-[9px] bg-mac-accent text-white text-[12.5px] font-medium hover:bg-mac-accentHi transition-colors flex items-center gap-1.5 disabled:opacity-60">
          {saving ? <Loader2 size={13} className="animate-spin" /> : saved ? <Check size={13} /> : null}
          {saved ? "Saved" : "Save"}
        </button>
      </div>

      {/* what Himmy has picked up on its own */}
      <div className="rounded-lg bg-mac-fill border border-mac-stroke p-3 space-y-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[12px] font-medium text-mac-ink2">What Himmy has picked up{ago ? ` · ${ago}` : ""}</span>
          <button onClick={learn} disabled={learning}
            className="h-7 px-2.5 rounded-md bg-mac-fillHi border border-mac-stroke text-[11.5px] text-mac-ink2 hover:text-mac-ink transition-colors flex items-center gap-1.5 disabled:opacity-60">
            {learning ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            {learning ? "Studying…" : "Refresh"}
          </button>
        </div>
        {learnedEmpty ? (
          <p className="text-[12px] text-mac-ink3 leading-snug">
            {learnMsg || "Click Refresh and Himmy will study your library, the notes & tags you've written, and your tasks to learn what you work on."}
          </p>
        ) : (
          <div className="space-y-1.5">
            {l.about && <p className="text-[12px] text-mac-ink2 leading-snug">{l.about}</p>}
            {l.voice && <p className="text-[11.5px] text-mac-ink3 leading-snug"><span className="text-mac-ink2">How you write:</span> {l.voice}</p>}
            <LearnedList label="Projects" items={l.projects} />
            <LearnedList label="People" items={l.people} />
            <LearnedList label="Topics" items={l.topics} />
            <LearnedList label="Preferences" items={l.preferences} />
            {learnMsg && <p className="text-[11.5px] text-mac-orange">{learnMsg}</p>}
          </div>
        )}
      </div>
    </div>
  );
}

/* A small account avatar — initial of the connected email on a deterministic color. */
function AccountAvatar({ email, size = 22 }: { email?: string | null; size?: number }) {
  return (
    <div className="rounded-full grid place-items-center text-white font-semibold shrink-0"
      style={{ width: size, height: size, background: avatarColor(email || "you"), fontSize: Math.round(size * 0.42) }}>
      {avatarInitial(email || "You")}
    </div>
  );
}

function SectionHeader({ title, sub }: { title: string; sub: string }) {
  return (
    <div className="mb-1">
      <h3 className="text-[14.5px] font-semibold text-mac-ink">{title}</h3>
      <p className="text-[12px] text-mac-ink3 mt-1 leading-relaxed max-w-[52ch]">{sub}</p>
    </div>
  );
}

function ConnectionsSection({ g }: { g: ReturnType<typeof useGoogle> }) {
  const s = g.status;
  return (
    <div className="space-y-4">
      <SectionHeader title="Connections" sub="Connect accounts and channels so Himmy can work across your life." />
      {!s?.connected ? (
        <GoogleConnect icon={Link2} title="Connect Google"
          blurb="Connect a Google account to read your inbox and manage your calendar from Himmy." g={g} />
      ) : (
        <div className="rounded-xl border border-mac-stroke bg-mac-fill p-4 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <AccountAvatar email={s.email} size={36} />
            <div className="min-w-0">
              <div className="text-[13px] text-mac-ink font-medium">Google</div>
              <div className="text-[12px] text-mac-ink3 truncate">{s.email} · Gmail &amp; Calendar</div>
            </div>
          </div>
          <button onClick={g.disconnect}
            className="shrink-0 h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">Disconnect</button>
        </div>
      )}
      <TelegramConnect />
    </div>
  );
}

function TelegramConnect() {
  const [st, setSt] = useState<TelegramStatus | null>(null);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const load = () => api.telegram.status().then(setSt).catch(() => {});
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);
  // While configured-but-not-yet-linked, poll so "Linked!" appears the moment the user messages it.
  useEffect(() => {
    if (st?.configured && !st?.linked) { const t = setInterval(load, 4000); return () => clearInterval(t); }
    /* eslint-disable-next-line */
  }, [st?.configured, st?.linked]);

  const connect = async () => {
    if (!token.trim()) return;
    setBusy(true); setErr("");
    try {
      const r = await api.telegram.setToken(token.trim());
      if (!r.ok) setErr(r.message || "Couldn't verify that token."); else { setToken(""); setSt(r); }
    } catch { setErr("Couldn't reach the backend."); } finally { setBusy(false); }
  };
  const unlink = async () => { try { setSt(await api.telegram.unlink()); } catch { /* */ } };
  const disconnect = async () => { try { setSt(await api.telegram.disconnect()); setToken(""); } catch { /* */ } };

  const head = (
    <div className="flex items-center gap-3 min-w-0">
      <div className="h-9 w-9 shrink-0 grid place-items-center rounded-full bg-[rgba(10,132,255,0.14)]">
        <Send size={16} className="text-mac-accentHi" />
      </div>
      <div className="min-w-0">
        <div className="text-[13px] text-mac-ink font-medium">Telegram</div>
        <div className="text-[12px] text-mac-ink3 truncate">
          {st?.configured ? (st.linked ? `Linked · @${st.username || "your bot"}` : `@${st.username || "your bot"} · waiting to link`)
            : "Chat with Himmy from your phone"}
        </div>
      </div>
    </div>
  );

  if (st?.configured) {
    return (
      <div className="rounded-xl border border-mac-stroke bg-mac-fill p-4">
        <div className="flex items-center justify-between gap-3">
          {head}
          <div className="shrink-0 flex items-center gap-1.5">
            {st.linked && (
              <button onClick={unlink} title="Forget the linked chat (re-pair on next message)"
                className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">Unlink</button>
            )}
            <button onClick={disconnect}
              className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">Disconnect</button>
          </div>
        </div>
        {!st.linked && (
          <div className="mt-3 pt-3 border-t border-mac-stroke flex items-center gap-2 text-[12px] text-mac-accentHi">
            <Loader2 size={13} className="animate-spin" />
            Open Telegram, message <span className="font-medium">@{st.username || "your bot"}</span>, and it links to you automatically.
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-mac-stroke bg-mac-fill p-4">
      <div className="flex items-center gap-3 mb-3">{head}</div>
      <ol className="text-[12px] text-mac-ink3 space-y-1 mb-3 list-decimal pl-4 marker:text-mac-ink3">
        <li>In Telegram, message <span className="text-mac-ink2 font-medium">@BotFather</span> → <span className="text-mac-ink2">/newbot</span> → pick a name.</li>
        <li>Copy the <span className="text-mac-ink2">bot token</span> it gives you and paste it below.</li>
        <li>Message your new bot — the first chat becomes you. It's private (only you).</li>
      </ol>
      <div className="flex items-center gap-2">
        <input value={token} onChange={(e) => { setToken(e.target.value); setErr(""); }}
          onKeyDown={(e) => { if (e.key === "Enter") connect(); }}
          placeholder="Paste your bot token (123456:ABC-…)"
          className="flex-1 h-9 px-3 rounded-[9px] bg-white/[0.04] ring-1 ring-inset ring-white/10 focus:ring-white/20 text-[12.5px] text-mac-ink placeholder:text-mac-ink3 outline-none font-mono" />
        <button onClick={connect} disabled={busy || !token.trim()}
          className="h-9 px-4 rounded-[9px] text-[12.5px] font-medium bg-mac-accent text-white hover:bg-mac-accentHi transition-colors disabled:opacity-60 inline-flex items-center gap-1.5">
          {busy ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />} Connect
        </button>
      </div>
      {err && <div className="text-[11.5px] text-mac-red mt-2">{err}</div>}
    </div>
  );
}

function PermissionsSection({ onGoConnections }: { onGoConnections: () => void }) {
  const [data, setData] = useState<PermsCatalog | null>(null);
  useEffect(() => { api.permissions.get().then(setData).catch(() => {}); }, []);
  const setLevel = async (key: string, value: string) => {
    setData((d) => d ? { ...d, surfaces: d.surfaces.map((s) => s.key === key ? { ...s, level: value } : s) } : d);
    try { setData(await api.permissions.set({ [key]: value })); } catch { /* keep optimistic */ }
  };
  return (
    <div className="space-y-4">
      <SectionHeader title="Permissions"
        sub="Decide exactly what Himmy can access and do. Changes take effect immediately — anything you turn off is declined gracefully." />
      {!data ? (
        <div className="h-32 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
      ) : (
        <div className="space-y-2">
          {data.surfaces.map((s) => (
            <PermissionRow key={s.key} s={s} onSet={(v) => setLevel(s.key, v)} onGoConnections={onGoConnections} />
          ))}
        </div>
      )}
    </div>
  );
}

function PermissionRow({ s, onSet, onGoConnections }: {
  s: PermSurface; onSet: (v: string) => void; onGoConnections: () => void;
}) {
  const needsGoogle = s.requires === "google" && !s.connected;
  return (
    <div className="rounded-xl border border-mac-stroke bg-mac-fill p-3.5 flex items-start justify-between gap-4">
      <div className="min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[13px] font-medium text-mac-ink">{s.label}</span>
          {s.service && <span className="text-[11px] text-mac-ink3">· {s.service}</span>}
          {needsGoogle && <Pill tone="orange">Not connected</Pill>}
        </div>
        <div className="text-[11.5px] text-mac-ink3 mt-0.5 max-w-[44ch] leading-snug">{s.desc}</div>
        {needsGoogle && (
          <button onClick={onGoConnections} className="text-[11px] text-mac-accentHi hover:underline mt-1">Connect Google →</button>
        )}
      </div>
      <div className="shrink-0 flex items-center p-0.5 rounded-[10px] bg-black/20 ring-1 ring-inset ring-white/[0.06]">
        {s.levels.map((l) => {
          const on = s.level === l.value;
          const isOff = l.value === "off";
          return (
            <button key={l.value} onClick={() => onSet(l.value)}
              className={`h-7 px-2.5 rounded-[8px] text-[11.5px] font-medium transition-colors whitespace-nowrap ${
                on ? (isOff ? "bg-mac-red/20 text-mac-red" : "bg-mac-fillHi text-mac-ink shadow-sm")
                   : "text-mac-ink3 hover:text-mac-ink"}`}>
              {l.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// How loud Himmy's proactive brain is — each level has a plain-English one-liner.
const PROACTIVE_LEVELS: { value: ProactiveLevel; label: string; desc: string }[] = [
  { value: "off",    label: "Off",    desc: "Himmy won't watch or notice anything in the background." },
  { value: "gentle", label: "Gentle", desc: "Notices things quietly — they appear on Today, but Himmy never pings you." },
  { value: "calm",   label: "Calm",   desc: "Pings you only for the urgent ones (deadlines, budgets, meeting prep)." },
  { value: "always", label: "Always", desc: "Full chief-of-staff: notices, pings, plus a morning rundown and evening recap." },
];

function ProactiveSection() {
  const [level, setLevel] = useState<ProactiveLevel | null>(null);
  useEffect(() => {
    api.proactive.getSettings().then((r) => setLevel(r.level)).catch(() => setLevel("always"));
  }, []);
  const choose = async (v: ProactiveLevel) => {
    setLevel(v);   // optimistic
    try { const r = await api.proactive.setSettings(v); setLevel(r.level); } catch { /* keep optimistic */ }
  };
  const active = PROACTIVE_LEVELS.find((l) => l.value === level);
  return (
    <div className="space-y-4">
      <SectionHeader title="Proactive"
        sub="Himmy's always-on chief-of-staff. It watches across your tasks, money, calendar and mail and surfaces a few high-quality, one-tap things worth doing. Nothing risky ever happens without your approval." />
      {!level ? (
        <div className="h-32 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
      ) : (
        <div className="rounded-xl border border-mac-stroke bg-mac-fill p-3.5 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Lightbulb size={14} className="text-mac-accentHi shrink-0" />
              <span className="text-[13px] font-medium text-mac-ink">How proactive should Himmy be?</span>
            </div>
            <div className="text-[11.5px] text-mac-ink3 mt-1 max-w-[46ch] leading-snug">{active?.desc}</div>
          </div>
          <div className="shrink-0 flex items-center p-0.5 rounded-[10px] bg-black/20 ring-1 ring-inset ring-white/[0.06]">
            {PROACTIVE_LEVELS.map((l) => {
              const on = level === l.value;
              const isOff = l.value === "off";
              return (
                <button key={l.value} onClick={() => choose(l.value)}
                  className={`h-7 px-2.5 rounded-[8px] text-[11.5px] font-medium transition-colors whitespace-nowrap ${
                    on ? (isOff ? "bg-mac-red/20 text-mac-red" : "bg-mac-fillHi text-mac-ink shadow-sm")
                       : "text-mac-ink3 hover:text-mac-ink"}`}>
                  {l.label}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

const ACT_ICON: Record<string, LucideIcon> = {
  mail: Mail, calendar: Calendar, tasks: CheckSquare, food: UtensilsCrossed,
  shopping: ShoppingBag, flights: Plane, web: Globe, live_data: MapPin,
  library: BookOpen, memory: Sparkles,
};

function ActivitySection() {
  const [items, setItems] = useState<ActivityItem[] | null>(null);
  const load = () => api.activity.get(80).then((r) => setItems(r.items || [])).catch(() => setItems([]));
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);
  const clear = async () => { await api.activity.clear().catch(() => {}); load(); };
  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <SectionHeader title="Activity"
          sub="A record of what Himmy has done for you — every action it takes is logged here. Stays on your Mac." />
        <div className="flex items-center gap-1.5 shrink-0">
          <button onClick={load} title="Refresh"
            className="h-8 w-8 grid place-items-center rounded-[9px] bg-mac-fill border border-mac-stroke text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">
            <RefreshCw size={13} />
          </button>
          {items && items.length > 0 && (
            <button onClick={clear}
              className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors inline-flex items-center gap-1.5">
              <Trash2 size={13} /> Clear
            </button>
          )}
        </div>
      </div>
      {!items ? (
        <div className="h-32 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
      ) : items.length === 0 ? (
        <div className="h-40 grid place-items-center text-center">
          <div>
            <ListChecks size={22} className="text-mac-ink3 mx-auto mb-3" />
            <p className="text-[13px] text-mac-ink2">No activity yet.</p>
            <p className="text-[11.5px] text-mac-ink3 mt-1 max-w-[34ch] mx-auto">Anything Himmy does — searches, drafts, bookings, tasks — will show up here.</p>
          </div>
        </div>
      ) : (
        <div className="space-y-1.5">
          {items.map((a, i) => <ActivityRow key={i} a={a} />)}
        </div>
      )}
    </div>
  );
}

function ActivityRow({ a }: { a: ActivityItem }) {
  const Icon = ACT_ICON[a.surface] || Sparkles;
  const tint = a.status === "blocked" ? "text-mac-orange" : a.status === "failed" ? "text-mac-red" : "text-mac-accentHi";
  return (
    <div className="flex items-center gap-3 px-3 py-2.5 rounded-xl border border-mac-stroke bg-mac-fill">
      <div className="h-8 w-8 shrink-0 grid place-items-center rounded-lg bg-mac-fillHi">
        <Icon size={14} className={tint} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-[12.5px] text-mac-ink leading-snug">
          {a.title}{a.detail ? <span className="text-mac-ink3"> — {a.detail}</span> : null}
        </div>
      </div>
      {a.status === "blocked" && <Pill tone="orange">blocked</Pill>}
      {a.status === "failed" && <Pill tone="neutral">failed</Pill>}
      <span className="text-[11px] text-mac-ink3 shrink-0 tnum">{relativeAgo(new Date(a.ts * 1000).toISOString())}</span>
    </div>
  );
}

function Pill({ tone, children }: { tone: "green" | "orange" | "neutral"; children: React.ReactNode }) {
  const cls = tone === "green" ? "bg-mac-green/15 text-mac-green"
    : tone === "orange" ? "bg-mac-orange/15 text-mac-orange" : "bg-mac-fillHi text-mac-ink3";
  return <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded-md ${cls}`}>{children}</span>;
}

// Settings → Preferences: the "AI model & key" block. Shows the active provider+model,
// a per-provider configured/Add-key row (with change + remove + an inline key field), and a
// Test button that proves the live setup. Mirrors the onboarding plumbing — never shows a key.
function AiKeysSection({ current }: { current: { provider: string; model: string | null } | null }) {
  const [providers, setProviders] = useState<ProviderInfo[] | null>(null);
  const [editing, setEditing] = useState<string | null>(null);   // provider id with its key field open
  const [keyVal, setKeyVal] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);          // "save:<id>" | "remove:<id>" | "test"
  const [rowMsg, setRowMsg] = useState<{ id: string; text: string; ok: boolean } | null>(null);
  const [testMsg, setTestMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const load = () => api.providerKeys().then((r: ProviderKeysResult) => setProviders(r.providers)).catch(() => setProviders([]));
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  const openEdit = (id: string) => { setEditing(id); setKeyVal(""); setShowKey(false); setRowMsg(null); };

  const saveKey = async (id: string) => {
    setBusy(`save:${id}`); setRowMsg(null);
    try {
      const r = await api.setProviderKey(id, keyVal.trim());
      if (!r.ok) { setRowMsg({ id, text: r.error || "That key was rejected.", ok: false }); return; }
      setEditing(null); setKeyVal("");
      setRowMsg({ id, text: "Key saved.", ok: true });
      await load();
    } catch { setRowMsg({ id, text: "Couldn't reach Himmy.", ok: false }); }
    finally { setBusy(null); }
  };

  const removeKey = async (id: string) => {
    setBusy(`remove:${id}`); setRowMsg(null);
    try { await api.clearProviderKey(id); await load(); }
    catch { /* ignore */ }
    finally { setBusy(null); }
  };

  const testNow = async () => {
    setBusy("test"); setTestMsg(null);
    try {
      const t = await api.testProvider();   // tests whatever is currently active
      setTestMsg(t.ok
        ? { text: `Working${t.latency_ms != null ? ` · ${t.latency_ms} ms` : ""}.`, ok: true }
        : { text: t.error || "That didn't work.", ok: false });
    } catch { setTestMsg({ text: "Couldn't reach Himmy.", ok: false }); }
    finally { setBusy(null); }
  };

  const keyProviders = (providers || []).filter((p) => p.needs_key);

  return (
    <div>
      <div className="flex items-center justify-between mb-2.5">
        <span className="text-[11px] uppercase tracking-[0.06em] font-semibold text-mac-ink3">AI model &amp; key</span>
        {current && (
          <span className="text-[11px] font-mono text-mac-ink3 truncate max-w-[16rem]">
            active · {current.model || current.provider}
          </span>
        )}
      </div>

      {providers === null ? (
        <div className="h-20 grid place-items-center rounded-xl border border-mac-stroke"><Loader2 size={15} className="animate-spin text-mac-ink3" /></div>
      ) : (
        <div className="rounded-xl border border-mac-stroke overflow-hidden">
          {keyProviders.map((p, i) => {
            const Icon = PROVIDER_ICON[p.id] || Sparkles;
            const isEditing = editing === p.id;
            const isActive = current?.provider === p.id;
            return (
              <div key={p.id} className={i > 0 ? "border-t border-mac-stroke" : ""}>
                <div className="flex items-center gap-3 px-3.5 py-3 bg-mac-fill">
                  <div className="h-8 w-8 shrink-0 grid place-items-center rounded-[9px] bg-mac-fillHi">
                    <Icon size={15} className="text-mac-accentHi" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-[13px] text-mac-ink font-medium truncate">{p.label}</span>
                      {isActive && <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-md bg-mac-accent/20 text-mac-accentHi">Active</span>}
                    </div>
                    <div className="text-[11.5px] mt-0.5 flex items-center gap-1.5">
                      {p.configured
                        ? <span className="text-mac-green inline-flex items-center gap-1"><Check size={12} /> Key configured</span>
                        : <span className="text-mac-ink3">No key yet</span>}
                    </div>
                  </div>
                  <div className="shrink-0 flex items-center gap-1.5">
                    {p.configured ? (
                      <>
                        <button onClick={() => openEdit(p.id)}
                          className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">Change</button>
                        <button onClick={() => removeKey(p.id)} disabled={busy === `remove:${p.id}`} title="Remove key"
                          className="h-8 w-8 grid place-items-center rounded-[9px] bg-mac-fill border border-mac-stroke text-mac-ink3 hover:text-mac-red hover:border-mac-strokeHi transition-colors disabled:opacity-60">
                          {busy === `remove:${p.id}` ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                        </button>
                      </>
                    ) : (
                      <button onClick={() => openEdit(p.id)}
                        className="h-8 px-3.5 rounded-[9px] bg-mac-accent text-white text-[12.5px] font-medium hover:bg-mac-accentHi transition-colors inline-flex items-center gap-1.5">
                        <KeyRound size={13} /> Add key
                      </button>
                    )}
                  </div>
                </div>

                {isEditing && (
                  <div className="px-3.5 pb-3.5 pt-0.5 bg-mac-fill border-t border-mac-stroke">
                    <div className="flex items-center justify-between mb-1.5">
                      <span className="text-[11.5px] text-mac-ink3">Paste your key — saved to your Mac's keychain, never shared.</span>
                      {p.key_url && (
                        <button onClick={() => openExternal(p.key_url || "")} className="text-[11.5px] text-mac-accentHi hover:underline inline-flex items-center gap-1">
                          Get a key <ExternalLink size={11} />
                        </button>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="relative flex-1">
                        <input type={showKey ? "text" : "password"} value={keyVal} autoFocus spellCheck={false} autoComplete="off"
                          onChange={(e) => { setKeyVal(e.target.value); setRowMsg(null); }}
                          onKeyDown={(e) => { if (e.key === "Enter" && keyVal.trim().length >= 8) saveKey(p.id); if (e.key === "Escape") setEditing(null); }}
                          placeholder="Paste your key here"
                          className="w-full h-9 pl-3 pr-10 rounded-[9px] bg-white/[0.04] ring-1 ring-inset ring-white/10 focus:ring-mac-accent text-[12.5px] text-mac-ink placeholder:text-mac-ink3 outline-none font-mono transition-shadow" />
                        <button onClick={() => setShowKey(!showKey)} type="button"
                          className="absolute right-1 top-1 h-7 w-7 grid place-items-center rounded-[7px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fillHi transition-colors">
                          {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
                        </button>
                      </div>
                      <button onClick={() => saveKey(p.id)} disabled={keyVal.trim().length < 8 || busy === `save:${p.id}`}
                        className="h-9 px-4 rounded-[9px] bg-mac-accent text-white text-[12.5px] font-medium hover:bg-mac-accentHi transition-colors disabled:opacity-50 inline-flex items-center gap-1.5">
                        {busy === `save:${p.id}` ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />} Save
                      </button>
                      <button onClick={() => setEditing(null)}
                        className="h-9 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">Cancel</button>
                    </div>
                  </div>
                )}

                {rowMsg?.id === p.id && (
                  <div className={`px-3.5 pb-3 -mt-0.5 text-[11.5px] ${rowMsg.ok ? "text-mac-green" : "text-mac-red"}`}>{rowMsg.text}</div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="flex items-center justify-between gap-3 mt-3">
        <p className="text-[11px] text-mac-ink3 leading-relaxed max-w-[40ch]">
          A key set here is saved securely and read automatically — no files to edit. Local
          (Ollama) needs no key; pick it in the Model list below.
        </p>
        <div className="shrink-0 flex flex-col items-end gap-1.5">
          <button onClick={testNow} disabled={busy === "test"}
            className="h-8 px-3.5 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors inline-flex items-center gap-1.5 disabled:opacity-60">
            {busy === "test" ? <Loader2 size={13} className="animate-spin" /> : <Zap size={13} />} Test connection
          </button>
          {testMsg && <span className={`text-[11.5px] ${testMsg.ok ? "text-mac-green" : "text-mac-red"}`}>{testMsg.text}</span>}
        </div>
      </div>
    </div>
  );
}

function PreferencesSection({ dir, onReveal }: { dir: string; onReveal: () => void }) {
  const [providers, setProviders] = useState<ModelProvider[] | null>(null);
  const [current, setCurrent] = useState<{ provider: string; model: string | null } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const load = () => api.models.list().then((r) => { setProviders(r.providers); setCurrent(r.current); }).catch(() => {});
  useEffect(() => { load(); }, []);
  // Live usage/cost — polled while the panel is open so the meter ticks if Himmy is working.
  useEffect(() => {
    const tick = () => api.usage().then(setUsage).catch(() => {});
    tick();
    const t = setInterval(tick, 15000);
    return () => clearInterval(t);
  }, []);

  const pick = async (provider: string, model: string, baseUrl?: string | null) => {
    setBusy(`${provider}:${model}`);
    try {
      const r = await api.models.set(provider, model, baseUrl);
      if (r.ok && r.current) { setCurrent(r.current); emitRefresh("tasks"); }
    } finally { setBusy(null); }
  };

  return (
    <div className="space-y-5">
      <SectionHeader title="Preferences" sub="The AI model Himmy runs on, and where your data lives." />

      <AiKeysSection current={current} />

      <div className="h-px bg-mac-stroke" />

      <div>
        <div className="flex items-center justify-between mb-2.5">
          <span className="text-[11px] uppercase tracking-[0.06em] font-semibold text-mac-ink3">Model</span>
          {current && <span className="text-[11px] font-mono text-mac-ink3 truncate max-w-[16rem]">now · {current.model || current.provider}</span>}
        </div>

        {providers === null ? (
          <div className="h-20 grid place-items-center rounded-xl border border-mac-stroke"><Loader2 size={15} className="animate-spin text-mac-ink3" /></div>
        ) : (
          <div className="rounded-xl border border-mac-stroke overflow-hidden">
            {providers.map((p, gi) => (
              <div key={p.id}>
                <div className={`flex items-center justify-between gap-2 px-3 h-8 bg-mac-fill ${gi > 0 ? "border-t border-mac-stroke" : ""} ${p.available ? "" : "opacity-60"}`}>
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-[11px] font-semibold uppercase tracking-[0.04em] text-mac-ink2 truncate">{p.label}</span>
                    <Pill tone={p.free ? "green" : "neutral"}>{p.free ? "Free" : "Paid"}</Pill>
                    {!p.tools && <Pill tone="orange">chat-only</Pill>}
                  </div>
                  {!p.available && <span className="text-[10.5px] text-mac-ink3 shrink-0 truncate">{p.status}</span>}
                </div>
                {p.available ? (
                  p.models.length === 0 ? (
                    <div className="px-3 py-2.5 border-t border-mac-stroke text-[12px] text-mac-ink3">No models installed yet.</div>
                  ) : (
                    p.models.map((m) => {
                      const active = current?.provider === p.id && current?.model === m.id;
                      const loading = busy === `${p.id}:${m.id}`;
                      return (
                        <button key={m.id} onClick={() => pick(p.id, m.id, m.base_url)} disabled={loading || active}
                          className={`group w-full flex items-center justify-between gap-3 px-3 h-10 border-t border-mac-stroke text-left transition-colors ${
                            active ? "bg-mac-accentDim" : "hover:bg-mac-fillHi"}`}>
                          <span className="flex items-center gap-2.5 min-w-0">
                            <span className={`h-4 w-4 rounded-full grid place-items-center shrink-0 transition-colors ${
                              active ? "bg-mac-accent" : "border border-mac-stroke group-hover:border-mac-strokeHi"}`}>
                              {active ? <Check size={10} className="text-white" /> : loading ? <Loader2 size={9} className="animate-spin text-mac-ink3" /> : null}
                            </span>
                            <span className={`text-[12.5px] truncate ${active ? "text-mac-ink font-medium" : "text-mac-ink2"}`}>{m.id}</span>
                          </span>
                          <span className="text-[10.5px] font-mono text-mac-ink3 shrink-0">{m.cost}</span>
                        </button>
                      );
                    })
                  )
                ) : (
                  <div className="px-3 py-2.5 border-t border-mac-stroke text-[11.5px] text-mac-ink3 leading-relaxed">
                    {p.id === "anthropic" ? "Add an Anthropic API key to enable Claude."
                      : p.id === "ollama" ? "Run Ollama (ollama serve) and pull a model to use it."
                      : p.id === "openai-compatible" ? "Start the HimalayaGPT gemma4 server on :8400."
                      : p.status}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        <p className="text-[11px] text-mac-ink3 leading-relaxed mt-3">
          Cost is tracked live under Usage below, so the meter always matches what's running. Local & on-device models
          are free; API models are billed per token. Himmy's tools (mail, tasks, search) work best with a tool-calling
          model — OpenRouter or the Claude API.
        </p>
      </div>

      <div className="h-px bg-mac-stroke" />
      <div>
        <div className="flex items-center justify-between mb-2.5">
          <span className="text-[11px] uppercase tracking-[0.06em] font-semibold text-mac-ink3">Usage</span>
          {usage?.model && <span className="text-[11px] font-mono text-mac-ink3 truncate max-w-[16rem]">{usage.model}</span>}
        </div>
        {!usage ? (
          <div className="h-20 grid place-items-center rounded-xl border border-mac-stroke"><Loader2 size={15} className="animate-spin text-mac-ink3" /></div>
        ) : (
          <div className="rounded-xl border border-mac-stroke p-4">
            <div className="flex flex-wrap items-end gap-x-12 gap-y-4">
              <UsageStat label="This session" totals={usage.session} primary />
              <UsageStat label="All time" totals={usage.lifetime} />
            </div>
            <p className="flex items-center gap-1.5 text-[11px] text-mac-ink3 mt-4">
              <Coins size={11} strokeWidth={2} className="text-mac-ink4" />
              Estimated from token counts at current model prices — session resets on restart, all-time is kept on this Mac.
            </p>
          </div>
        )}
      </div>

      <div className="h-px bg-mac-stroke" />
      <SettingRow title="Data folder" sub="Everything you create lives here on this Mac. Keep it in iCloud Drive or Dropbox to use Himmy on another computer.">
        <button onClick={onReveal}
          className="h-8 px-3.5 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors flex items-center gap-1.5">
          <Folder size={13} /> Reveal in Finder
        </button>
      </SettingRow>
      {dir && <p className="text-[11px] font-mono text-mac-ink3 truncate">{dir}</p>}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────────────────
   In-app AI setup — a non-coder picks a provider, pastes a key, and confirms it
   works, all inside Himmy (no .env). The key is written through himmy's writable
   secrets store (keychain on macOS / encrypted file elsewhere — the same path the
   Google sign-in uses) and read back automatically by the inference layer. The
   key VALUE is never displayed, returned, or stored in the frontend.
   ──────────────────────────────────────────────────────────────────────────── */

// Open a "get a key" page in the user's real browser (Electron shell), with a graceful
// web fallback. Used by the onboarding key step and the Settings rows.
function openExternal(url: string) {
  if (!url) return;
  const s = (window as any).himmy;
  if (s?.openExternal) s.openExternal(url);
  else window.open(url, "_blank");
}

// A small, friendly menu of sensible model choices per provider for the onboarding
// dropdown. Kept short on purpose — a non-coder shouldn't have to scan a giant list.
// The first entry is the default. Ollama is handled separately (we read whatever the
// user already has installed via /models).
const MODEL_CHOICES: Record<string, { id: string; label: string; hint?: string }[]> = {
  openrouter: [
    { id: "google/gemini-2.5-flash", label: "Gemini 2.5 Flash", hint: "fast · cheap · great default" },
    { id: "openai/gpt-4o-mini", label: "GPT-4o mini", hint: "fast · cheap" },
    { id: "anthropic/claude-haiku-4.5", label: "Claude Haiku 4.5", hint: "quick & capable" },
    { id: "anthropic/claude-sonnet-4.5", label: "Claude Sonnet 4.5", hint: "smartest · pricier" },
    { id: "openai/gpt-4o", label: "GPT-4o", hint: "smart · pricier" },
  ],
  openai: [
    { id: "gpt-4o-mini", label: "GPT-4o mini", hint: "fast · cheap · great default" },
    { id: "gpt-4o", label: "GPT-4o", hint: "smart · pricier" },
  ],
  anthropic: [
    { id: "claude-haiku-4-5-20251001", label: "Claude Haiku 4.5", hint: "fast · cheap · great default" },
    { id: "claude-sonnet-4-5-20250929", label: "Claude Sonnet 4.5", hint: "smartest · pricier" },
  ],
};

// The icon shown on each provider card / row.
const PROVIDER_ICON: Record<string, LucideIcon> = {
  openrouter: Sparkles,
  openai: BrainCircuit,
  anthropic: BrainCircuit,
  "openai-compatible": ServerCog,
  ollama: CpuIcon,
};

type AccountTab = "you" | "files" | "connections" | "permissions" | "proactive" | "activity" | "backup" | "preferences";
const ACCOUNT_SECTIONS: { id: AccountTab; label: string; icon: LucideIcon }[] = [
  { id: "you", label: "You", icon: Sparkles },
  { id: "files", label: "Files", icon: Paperclip },
  { id: "connections", label: "Connections", icon: Link2 },
  { id: "permissions", label: "Permissions", icon: ShieldCheck },
  { id: "proactive", label: "Proactive", icon: Lightbulb },
  { id: "activity", label: "Activity", icon: ListChecks },
  { id: "backup", label: "Backup & Sync", icon: FileDown },
  { id: "preferences", label: "Preferences", icon: Settings },
];

/* ─────────────────────────────────────────────────────────────────────────────
   First-run onboarding — the warm, no-jargon "connect your AI brain" flow. A non-
   coder opens Himmy, picks a provider, pastes a key (or picks a local model), and
   confirms it works — in well under a minute, no .env. Shown when /provider/status
   reports the app can't run inference yet; reopenable any time from Settings.
   ──────────────────────────────────────────────────────────────────────────── */

type OnbStep = "welcome" | "provider" | "key" | "done";

function OnboardingModal({ onClose, onReady, dismissable = true }: {
  onClose: () => void;          // user dismissed (or finished) — hide the modal
  onReady?: () => void;         // a working setup was confirmed — let the app refresh state
  dismissable?: boolean;        // first-run can be dismissed; "Add key" entry is the same flow
}) {
  const [step, setStep] = useState<OnbStep>("welcome");
  const [providers, setProviders] = useState<ProviderInfo[] | null>(null);
  const [picked, setPicked] = useState<ProviderInfo | null>(null);

  // Key + model state for the active provider.
  const [keyVal, setKeyVal] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");          // only for the custom endpoint
  const [ollamaModels, setOllamaModels] = useState<string[]>([]);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");              // friendly, inline
  const [latency, setLatency] = useState<number | null>(null);

  useEffect(() => {
    api.providerKeys().then((r: ProviderKeysResult) => setProviders(r.providers)).catch(() => setProviders([]));
  }, []);

  // When a provider is chosen, seed a sensible default model + (for Ollama) load
  // whatever's installed locally so the user just picks from a dropdown.
  const choose = (p: ProviderInfo) => {
    setPicked(p);
    setError(""); setKeyVal(""); setShowKey(false); setBaseUrl(""); setLatency(null);
    const def = p.default_model || MODEL_CHOICES[p.id]?.[0]?.id || "";
    setModel(def);
    if (p.id === "ollama") {
      api.models.list().then((r) => {
        const oll = r.providers.find((x) => x.id === "ollama");
        const ids = (oll?.models || []).map((m) => m.id);
        setOllamaModels(ids);
        if (ids.length && !def) setModel(ids[0]);
      }).catch(() => setOllamaModels([]));
    }
    setStep("key");
  };

  // Save the key (if any), persist provider+model, then run a one-token "ping" to prove it.
  const testAndFinish = async () => {
    if (!picked) return;
    setBusy(true); setError(""); setLatency(null);
    try {
      // 1) Store the pasted key (key providers only). Surfaces a friendly error inline.
      if (picked.needs_key) {
        const sk = await api.setProviderKey(picked.id, keyVal.trim());
        if (!sk.ok) { setError(sk.error || "That key was rejected — check it and try again."); setBusy(false); return; }
      }
      // 2) Persist provider + model (reuses the /models path) so every message runs on this choice.
      const bu = picked.id === "openai-compatible" ? baseUrl.trim() : undefined;
      await api.models.set(picked.id, model || null, bu).catch(() => {});
      // 3) Prove it end-to-end with a tiny ping through the real runtime.
      const t = await api.testProvider(picked.id, model || null, bu);
      if (!t.ok) { setError(t.error || "Couldn't reach the model — check your key and try again."); setBusy(false); return; }
      setLatency(t.latency_ms ?? null);
      setStep("done");
      onReady?.();
    } catch {
      setError("Something went wrong reaching Himmy. Make sure the app is running and try again.");
    } finally {
      setBusy(false);
    }
  };

  const back = () => {
    setError("");
    if (step === "provider") setStep("welcome");
    else if (step === "key") setStep("provider");
  };

  // Can we attempt the test? Key providers need a key; the custom endpoint also needs an address.
  const canTest = !!picked && (!picked.needs_key || keyVal.trim().length >= 8)
    && (picked.id !== "openai-compatible" || baseUrl.trim().length > 0);

  const modelChoices = picked ? (MODEL_CHOICES[picked.id] || []) : [];

  return (
    <div className="absolute inset-0 z-[60] grid place-items-center bg-black/55 backdrop-blur-[2px]"
      onMouseDown={() => dismissable && onClose()}>
      <div onMouseDown={(e) => e.stopPropagation()}
        className="w-[480px] max-w-[calc(100%-3rem)] rounded-[20px] bg-[rgba(28,29,35,0.98)] backdrop-blur-2xl border border-mac-strokeHi shadow-pop overflow-hidden flex flex-col">

        {/* header: a back arrow on inner steps + a dismiss (only when allowed) */}
        <div className="h-12 px-3 flex items-center justify-between shrink-0">
          {step !== "welcome" && step !== "done" ? (
            <button onClick={back} className="h-8 w-8 grid place-items-center rounded-[9px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors">
              <ArrowLeft size={16} />
            </button>
          ) : <span className="w-8" />}
          <StepDots step={step} />
          {dismissable
            ? <button onClick={onClose} className="h-8 w-8 grid place-items-center rounded-[9px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors"><X size={16} /></button>
            : <span className="w-8" />}
        </div>

        <div className="px-7 pb-7 pt-1">
          {step === "welcome" && <OnbWelcome onNext={() => setStep("provider")} />}

          {step === "provider" && (
            <OnbProviders providers={providers} onPick={choose} />
          )}

          {step === "key" && picked && (
            <OnbKey
              p={picked}
              keyVal={keyVal} setKeyVal={(v) => { setKeyVal(v); setError(""); }}
              showKey={showKey} setShowKey={setShowKey}
              model={model} setModel={setModel}
              baseUrl={baseUrl} setBaseUrl={(v) => { setBaseUrl(v); setError(""); }}
              modelChoices={modelChoices} ollamaModels={ollamaModels}
              error={error} busy={busy} canTest={canTest}
              onGetKey={() => openExternal(picked.key_url || "")}
              onTest={testAndFinish}
            />
          )}

          {step === "done" && picked && (
            <OnbDone p={picked} model={model} latency={latency} onClose={() => { onClose(); }} />
          )}
        </div>
      </div>
    </div>
  );
}

// The little progress dots in the header (welcome · pick · key · done).
function StepDots({ step }: { step: OnbStep }) {
  const order: OnbStep[] = ["welcome", "provider", "key", "done"];
  const idx = order.indexOf(step);
  return (
    <div className="flex items-center gap-1.5">
      {order.map((s, i) => (
        <span key={s} className={`h-1.5 rounded-full transition-all ${
          i === idx ? "w-5 bg-mac-accent" : i < idx ? "w-1.5 bg-mac-accentHi/60" : "w-1.5 bg-mac-stroke"}`} />
      ))}
    </div>
  );
}

function OnbWelcome({ onNext }: { onNext: () => void }) {
  return (
    <div className="text-center pt-3 pb-2">
      <div className="mx-auto h-16 w-16 rounded-[20px] grid place-items-center bg-gradient-to-b from-mac-accent to-mac-accentHi shadow-pop mb-5">
        <Sparkles size={28} className="text-white" strokeWidth={2} />
      </div>
      <h2 className="font-display text-[22px] font-semibold tracking-[-0.02em] text-mac-ink">Welcome to Himmy</h2>
      <p className="text-[13.5px] leading-relaxed text-mac-ink2 mt-2.5 max-w-[34ch] mx-auto">
        Your personal assistant for reading, planning, and getting things done. First,
        let's connect the AI brain that powers it — it takes about a minute.
      </p>
      <button onClick={onNext}
        className="mt-6 h-11 w-full rounded-[12px] bg-mac-accent text-white text-[14px] font-medium hover:bg-mac-accentHi transition-colors inline-flex items-center justify-center gap-2">
        Get started <ArrowRight size={16} strokeWidth={2.25} />
      </button>
      <p className="text-[11.5px] text-mac-ink3 mt-3.5">Your keys stay on this Mac, in the system keychain. Nothing is shared.</p>
    </div>
  );
}

function OnbProviders({ providers, onPick }: { providers: ProviderInfo[] | null; onPick: (p: ProviderInfo) => void }) {
  return (
    <div className="pt-1">
      <h2 className="font-display text-[18px] font-semibold tracking-[-0.01em] text-mac-ink text-center">Connect your AI brain</h2>
      <p className="text-[12.5px] leading-relaxed text-mac-ink2 mt-1.5 mb-5 text-center max-w-[36ch] mx-auto">
        Pick where Himmy's intelligence comes from. Not sure? OpenRouter is the easy choice.
      </p>
      {providers === null ? (
        <div className="h-40 grid place-items-center text-mac-ink3"><Loader2 size={20} className="animate-spin" /></div>
      ) : (
        <div className="space-y-2">
          {providers.map((p) => {
            const Icon = PROVIDER_ICON[p.id] || Sparkles;
            return (
              <button key={p.id} onClick={() => onPick(p)}
                className="group w-full flex items-center gap-3.5 p-3.5 rounded-[14px] bg-mac-fill border border-mac-stroke hover:border-mac-accent hover:bg-mac-fillHi transition-all text-left">
                <div className="h-10 w-10 shrink-0 grid place-items-center rounded-[11px] bg-mac-fillHi group-hover:bg-mac-accentDim transition-colors">
                  <Icon size={19} className="text-mac-accentHi" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[14px] font-medium text-mac-ink">{p.label}</span>
                    {p.recommended && <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-md bg-mac-accent/20 text-mac-accentHi">Recommended</span>}
                    {!p.needs_key && p.configured && <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-md bg-mac-green/15 text-mac-green">Running</span>}
                    {p.configured && p.needs_key && <Check size={13} className="text-mac-green" />}
                  </div>
                  <div className="text-[12px] text-mac-ink3 mt-0.5 leading-snug">{p.blurb}</div>
                </div>
                <ChevronRight size={16} className="text-mac-ink3 group-hover:text-mac-ink shrink-0 transition-colors" />
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function OnbKey({ p, keyVal, setKeyVal, showKey, setShowKey, model, setModel, baseUrl, setBaseUrl,
  modelChoices, ollamaModels, error, busy, canTest, onGetKey, onTest }: {
  p: ProviderInfo;
  keyVal: string; setKeyVal: (v: string) => void;
  showKey: boolean; setShowKey: (v: boolean) => void;
  model: string; setModel: (v: string) => void;
  baseUrl: string; setBaseUrl: (v: string) => void;
  modelChoices: { id: string; label: string; hint?: string }[];
  ollamaModels: string[];
  error: string; busy: boolean; canTest: boolean;
  onGetKey: () => void; onTest: () => void;
}) {
  const Icon = PROVIDER_ICON[p.id] || Sparkles;
  const isOllama = p.id === "ollama";
  const isCustom = p.id === "openai-compatible";

  return (
    <div className="pt-1">
      <div className="flex items-center gap-2.5 justify-center mb-1">
        <div className="h-8 w-8 grid place-items-center rounded-[10px] bg-mac-fillHi"><Icon size={16} className="text-mac-accentHi" /></div>
        <h2 className="font-display text-[18px] font-semibold tracking-[-0.01em] text-mac-ink">{p.label}</h2>
      </div>
      <p className="text-[12.5px] leading-relaxed text-mac-ink2 mt-1 mb-5 text-center max-w-[36ch] mx-auto">
        {isOllama
          ? "Local & private — runs on your computer, no key needed. Just pick a model."
          : isCustom
            ? "Point Himmy at your own OpenAI-compatible endpoint — its address and (optionally) a key."
            : "Paste your key below. We'll save it securely in your Mac's keychain — never shared."}
      </p>

      {/* key field (key providers only) */}
      {p.needs_key && (
        <div className="mb-4">
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-[11.5px] font-medium text-mac-ink2">Your API key</label>
            {p.key_url && (
              <button onClick={onGetKey} className="text-[11.5px] text-mac-accentHi hover:underline inline-flex items-center gap-1">
                Get a key <ExternalLink size={11} />
              </button>
            )}
          </div>
          <div className="relative">
            <input
              type={showKey ? "text" : "password"}
              value={keyVal}
              onChange={(e) => setKeyVal(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && canTest && !busy) onTest(); }}
              placeholder="Paste your key here"
              autoFocus
              spellCheck={false} autoComplete="off"
              className="w-full h-11 pl-3.5 pr-11 rounded-[11px] bg-white/[0.04] ring-1 ring-inset ring-white/10 focus:ring-mac-accent text-[13px] text-mac-ink placeholder:text-mac-ink3 outline-none font-mono transition-shadow" />
            <button onClick={() => setShowKey(!showKey)} type="button"
              title={showKey ? "Hide key" : "Show key"}
              className="absolute right-1.5 top-1.5 h-8 w-8 grid place-items-center rounded-[8px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors">
              {showKey ? <EyeOff size={15} /> : <Eye size={15} />}
            </button>
          </div>
        </div>
      )}

      {/* custom endpoint address */}
      {isCustom && (
        <div className="mb-4">
          <label className="text-[11.5px] font-medium text-mac-ink2 mb-1.5 block">Endpoint address</label>
          <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} spellCheck={false} autoComplete="off"
            placeholder="http://localhost:8400/v1"
            className="w-full h-11 px-3.5 rounded-[11px] bg-white/[0.04] ring-1 ring-inset ring-white/10 focus:ring-mac-accent text-[13px] text-mac-ink placeholder:text-mac-ink3 outline-none font-mono transition-shadow" />
        </div>
      )}

      {/* model picker */}
      <div className="mb-1">
        <label className="text-[11.5px] font-medium text-mac-ink2 mb-1.5 block">Model</label>
        {isOllama ? (
          ollamaModels.length > 0 ? (
            <OnbModelSelect value={model} onChange={setModel}
              options={ollamaModels.map((id) => ({ id, label: id }))} />
          ) : (
            <div className="rounded-[11px] bg-mac-fill border border-mac-stroke px-3.5 py-3 text-[12px] text-mac-ink3 leading-relaxed">
              No local models found. Install <span className="font-mono text-mac-ink2">ollama</span> and run{" "}
              <span className="font-mono text-mac-ink2">ollama pull llama3.2</span>, then come back.
            </div>
          )
        ) : isCustom ? (
          <input value={model} onChange={(e) => setModel(e.target.value)} spellCheck={false} autoComplete="off"
            placeholder="model name (e.g. gemma-3)"
            className="w-full h-11 px-3.5 rounded-[11px] bg-white/[0.04] ring-1 ring-inset ring-white/10 focus:ring-mac-accent text-[13px] text-mac-ink placeholder:text-mac-ink3 outline-none font-mono transition-shadow" />
        ) : (
          <OnbModelSelect value={model} onChange={setModel} options={modelChoices} />
        )}
      </div>

      {error && (
        <div className="mt-4 rounded-[11px] bg-mac-red/10 border border-mac-red/30 px-3.5 py-2.5 text-[12.5px] text-mac-red leading-snug flex items-start gap-2">
          <Info size={14} className="mt-0.5 shrink-0" /> <span>{error}</span>
        </div>
      )}

      <button onClick={onTest} disabled={!canTest || busy}
        className="mt-5 h-11 w-full rounded-[12px] bg-mac-accent text-white text-[14px] font-medium hover:bg-mac-accentHi transition-colors inline-flex items-center justify-center gap-2 disabled:opacity-50">
        {busy ? <><Loader2 size={16} className="animate-spin" /> Checking…</> : <>Test &amp; finish <Check size={16} strokeWidth={2.5} /></>}
      </button>
    </div>
  );
}

// A compact, native-feeling select used by the onboarding model picker.
function OnbModelSelect({ value, onChange, options }: {
  value: string; onChange: (v: string) => void; options: { id: string; label: string; hint?: string }[];
}) {
  return (
    <div className="relative">
      <select value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full h-11 pl-3.5 pr-9 rounded-[11px] bg-white/[0.04] ring-1 ring-inset ring-white/10 focus:ring-mac-accent text-[13px] text-mac-ink outline-none appearance-none cursor-pointer transition-shadow">
        {options.length === 0 && <option value="">Choose a model…</option>}
        {options.map((o) => (
          <option key={o.id} value={o.id} className="bg-[#1c1d23] text-mac-ink">
            {o.label}{o.hint ? ` — ${o.hint}` : ""}
          </option>
        ))}
      </select>
      <ChevronDown size={15} className="absolute right-3 top-1/2 -translate-y-1/2 text-mac-ink3 pointer-events-none" />
    </div>
  );
}

function OnbDone({ p, model, latency, onClose }: {
  p: ProviderInfo; model: string; latency: number | null; onClose: () => void;
}) {
  // One-time "make Himmy yours" capture: the usual food budget, saved into the profile vault so
  // Himmy only ever suggests food within it (editable later in Settings → You).
  const [budget, setBudget] = useState<number | null>(null);
  const [saved, setSaved] = useState(false);
  const pickBudget = async (v: number) => {
    setBudget(v); setSaved(false);
    try {
      const { profile } = await api.profile.get();
      const details = { ...(profile.user.details || {}), "Food budget": `Rs ${v}` };
      await api.profile.saveUser({ ...profile.user, details });
      setSaved(true);
    } catch { /* a failed save shouldn't block finishing onboarding */ }
  };
  return (
    <div className="text-center pt-3 pb-2">
      <div className="mx-auto h-16 w-16 rounded-full grid place-items-center bg-mac-green/15 mb-5">
        <CheckCircle2 size={34} className="text-mac-green" strokeWidth={2} />
      </div>
      <h2 className="font-display text-[22px] font-semibold tracking-[-0.02em] text-mac-ink">You're all set</h2>
      <p className="text-[13.5px] leading-relaxed text-mac-ink2 mt-2.5 max-w-[34ch] mx-auto">
        Himmy is connected to <span className="text-mac-ink font-medium">{p.label}</span>
        {model ? <> on <span className="text-mac-ink font-medium">{model}</span></> : null} and answered in a flash
        {latency != null ? <> ({latency} ms)</> : null}.
      </p>

      {/* food budget — a small personalisation Himmy will remember */}
      <div className="mt-5 rounded-2xl bg-mac-fill border border-mac-stroke p-4 text-left">
        <div className="flex items-center gap-2">
          <UtensilsCrossed size={14} className="text-mac-accentHi" />
          <span className="text-[13px] font-medium text-mac-ink">What's your usual food budget?</span>
          {saved && <span className="ml-auto text-[11px] text-mac-green inline-flex items-center gap-1"><Check size={11} /> saved</span>}
        </div>
        <p className="text-[11.5px] text-mac-ink3 leading-snug mt-1">So Himmy only suggests food you'd actually order. You can change it later.</p>
        <div className="flex flex-wrap gap-2 mt-3">
          {[200, 400, 600, 1000].map((v) => {
            const on = budget === v;
            return (
              <button key={v} onClick={() => pickBudget(v)}
                className={`h-8 px-3 rounded-full text-[12px] inline-flex items-center gap-1.5 ring-1 ring-inset transition-colors ${on ? "bg-mac-accent/15 ring-mac-accent/40 text-mac-ink" : "bg-mac-fillHi ring-mac-stroke text-mac-ink2 hover:text-mac-ink"}`}>
                <Wallet size={11} className={on ? "text-mac-accentHi" : "text-mac-ink3"} /> Rs {v}{v === 1000 ? "+" : ""}
              </button>
            );
          })}
        </div>
      </div>

      <button onClick={onClose}
        className="mt-5 h-11 w-full rounded-[12px] bg-mac-accent text-white text-[14px] font-medium hover:bg-mac-accentHi transition-colors inline-flex items-center justify-center gap-2">
        Start using Himmy <ArrowRight size={16} strokeWidth={2.25} />
      </button>
      <p className="text-[11.5px] text-mac-ink3 mt-3.5">You can change all of this in Settings.</p>
    </div>
  );
}

function AccountPanel({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<AccountTab>("you");
  const [dir, setDir] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const g = useGoogle();
  useEffect(() => { api.dataDir().then((r) => setDir(r.path)).catch(() => {}); }, []);

  const backup = async () => {
    setBusy("backup"); setMsg(null);
    try { const r = await api.backup(); setMsg(r.ok ? `Backed up to ${r.path}` : (r.message || "Backup failed.")); }
    catch (e: any) { setMsg(e.message); } finally { setBusy(null); }
  };
  const restore = async () => {
    const path = await (window as any).himmy?.pickZip?.();
    if (!path) return;
    setBusy("restore"); setMsg(null);
    try {
      const r = await api.restore(path);
      setMsg(r.ok ? (r.message || `Restored ${r.restored} papers — reopen the Library to see them.`) : (r.message || "Restore failed."));
    } catch (e: any) { setMsg(e.message); } finally { setBusy(null); }
  };
  const reveal = () => (window as any).himmy?.revealData?.();

  return (
    <div className="absolute inset-0 z-50 grid place-items-center bg-black/45" onMouseDown={onClose}>
      <div onMouseDown={(e) => e.stopPropagation()}
        className="w-[800px] max-w-[calc(100%-3rem)] h-[80vh] max-h-[660px] rounded-2xl bg-[rgba(30,31,37,0.97)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden flex flex-col">
        <div className="h-12 px-4 flex items-center justify-between border-b border-mac-stroke shrink-0">
          <div className="flex items-center gap-2 text-[13px]">
            <AccountAvatar email={g.status?.email} size={20} />
            <span className="font-medium text-mac-ink">Account</span>
          </div>
          <button onClick={onClose} className="text-mac-ink3 hover:text-mac-ink"><X size={16} /></button>
        </div>
        <div className="flex-1 min-h-0 flex">
          <nav className="w-[178px] shrink-0 border-r border-mac-stroke p-2.5 flex flex-col gap-0.5">
            {ACCOUNT_SECTIONS.map((sct) => (
              <button key={sct.id} onClick={() => setTab(sct.id)}
                className={`flex items-center gap-2.5 h-9 px-2.5 rounded-[9px] text-[12.5px] transition-colors ${
                  tab === sct.id ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink2 hover:text-mac-ink hover:bg-mac-fill"}`}>
                <sct.icon size={14} className={tab === sct.id ? "text-mac-accentHi" : "text-mac-ink3"} /> {sct.label}
              </button>
            ))}
          </nav>
          <div className="flex-1 min-h-0 overflow-auto p-5">
            {tab === "you" && (
              <div className="space-y-6">
                <HimmyPersonality />
                <div className="border-t border-mac-stroke" />
                <ProfileSettings />
              </div>
            )}
            {tab === "files" && <FilesSection />}
            {tab === "connections" && <ConnectionsSection g={g} />}
            {tab === "permissions" && <PermissionsSection onGoConnections={() => setTab("connections")} />}
            {tab === "proactive" && <ProactiveSection />}
            {tab === "activity" && <ActivitySection />}
            {tab === "preferences" && <PreferencesSection dir={dir} onReveal={reveal} />}
            {tab === "backup" && (
              <div className="space-y-4">
                <SectionHeader title="Backup & Sync" sub="Keep a copy of your whole workspace, or move it to another Mac." />
                <SettingRow title="Back up everything"
                  sub="One .zip with your whole workspace — papers & PDFs, notes & highlights, chats, tasks, routines, and what Himmy has learned — saved to Downloads.">
                  <button onClick={backup} disabled={busy === "backup"}
                    className="h-8 px-3.5 rounded-[9px] bg-mac-accent text-white text-[12.5px] font-medium hover:bg-mac-accentHi transition-colors flex items-center gap-1.5 disabled:opacity-60">
                    {busy === "backup" ? <Loader2 size={13} className="animate-spin" /> : <FileDown size={13} />} Back up now
                  </button>
                </SettingRow>
                <SettingRow title="Restore from a backup"
                  sub="Replace your whole workspace from a backup .zip — e.g. from another Mac. Your current data is safely copied aside first, so a restore can't lose it.">
                  <button onClick={restore} disabled={busy === "restore"}
                    className="h-8 px-3.5 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors flex items-center gap-1.5 disabled:opacity-60">
                    {busy === "restore" ? <Loader2 size={13} className="animate-spin" /> : <FileUp size={13} />} Choose backup…
                  </button>
                </SettingRow>
                {msg && <p className="text-[12px] text-mac-ink2 bg-mac-fill border border-mac-stroke rounded-md px-3 py-2 break-all">{msg}</p>}
                <p className="text-[11px] text-mac-ink3 leading-relaxed">
                  Real-time phone sync isn't available in a local Mac app — backups plus a cloud-synced
                  folder cover safe backup and using Himmy on another computer.
                </p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function SettingRow({ title, sub, children }: { title: string; sub: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="min-w-0">
        <div className="text-[13px] text-mac-ink">{title}</div>
        <div className="text-[12px] text-mac-ink3 leading-snug mt-0.5">{sub}</div>
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function PaperTable({ items, reading, onRemove, onOpen }:
  { items: Paper[]; reading: Record<string, number>; onRemove: (id: string) => void; onOpen: (id: string) => void }) {
  return (
    <div className="rounded-xl border border-mac-stroke overflow-hidden">
      <div className="grid grid-cols-[1fr_170px_54px_82px_64px_30px] gap-3 px-4 h-9 items-center bg-mac-fill border-b border-mac-stroke text-[10.5px] uppercase tracking-wide text-mac-ink3 font-medium">
        <div>Title</div><div>Authors</div><div>Year</div><div>Type</div><div>Read</div><div />
      </div>
      {items.map((p) => (
        <div key={p.id} onClick={() => onOpen(p.id)} draggable
          onDragStart={(e) => { e.dataTransfer.setData("text/himmy-item", p.id); e.dataTransfer.effectAllowed = "copyMove"; }}
          className="group grid grid-cols-[1fr_170px_54px_82px_64px_30px] gap-3 px-4 py-2.5 items-center border-b border-mac-stroke last:border-0 hover:bg-mac-fill transition-colors cursor-pointer">
          <div className="min-w-0">
            <div className="text-[13px] text-mac-ink truncate">{p.title}</div>
            {p.venue && <div className="text-[11.5px] text-mac-ink3 truncate">{p.venue}</div>}
          </div>
          <div className="text-[12.5px] text-mac-ink2 truncate">
            {p.authors.slice(0, 2).join(", ")}{p.authors.length > 2 ? " et al." : ""}
          </div>
          <div className="text-[12.5px] text-mac-ink2 tnum">{p.year || "—"}</div>
          <div>
            <span className="text-[10.5px] text-mac-ink3 bg-mac-fill border border-mac-stroke rounded px-1.5 py-0.5">
              {prettyType(p.type)}
            </span>
          </div>
          {/* engaged reading time — gold once you've actually read it, a dim dash before. */}
          <div className="text-[12px] tnum">
            {reading[p.id]
              ? <span className="text-mac-accentHi" title="Engaged reading time">{fmtReadShort(reading[p.id])}</span>
              : <span className="text-mac-ink4">—</span>}
          </div>
          <div className="flex justify-end">
            <button onClick={(e) => { e.stopPropagation(); onRemove(p.id); }} title="Remove from library"
              className="opacity-0 group-hover:opacity-100 text-mac-ink3 hover:text-mac-red transition-opacity">
              <Trash2 size={14} />
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function prettyType(t: string): string {
  const m: Record<string, string> = {
    "journal-article": "Article", "preprint": "Preprint", "document": "PDF",
    "proceedings-article": "Paper", "book": "Book", "book-chapter": "Chapter",
  };
  return m[t] || (t ? t.split("-")[0].replace(/^\w/, (c) => c.toUpperCase()) : "Item");
}

function EmptyLibrary({ onAdd, onDoi, dragOver }:
  { onAdd: () => void; onDoi: () => void; dragOver: boolean }) {
  return (
    <div className="h-full grid place-items-center">
      <div className={`w-full max-w-sm text-center rounded-2xl border border-dashed px-8 py-12 transition-colors ${
        dragOver ? "border-mac-accent bg-mac-accentDim" : "border-mac-stroke"}`}>
        <div className="mx-auto h-14 w-14 rounded-2xl grid place-items-center bg-mac-fill border border-mac-stroke mb-5 shadow-mac">
          <BookOpen size={24} strokeWidth={1.75} className="text-mac-accentHi" />
        </div>
        <h2 className="font-display text-[18px] font-semibold tracking-[-0.01em] mb-2">Start your library</h2>
        <p className="text-[13px] leading-relaxed text-mac-ink2 mb-6">
          Drag PDFs here, choose files, or add a paper by DOI or arXiv id. Himmy fetches the
          details and keeps everything in one place.
        </p>
        <div className="flex items-center justify-center gap-2.5">
          <button onClick={onAdd}
            className="h-9 px-4 rounded-[10px] bg-mac-accent text-[13px] font-medium text-white hover:bg-mac-accentHi transition-colors flex items-center gap-1.5">
            <Plus size={15} strokeWidth={2.5} /> Add papers
          </button>
          <button onClick={onDoi}
            className="h-9 px-4 rounded-[10px] bg-mac-fill border border-mac-stroke text-[13px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">
            Add by DOI
          </button>
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────────────────────── stub */
function Stub({ icon: Ico, title, body }: { icon: LucideIcon; title: string; body: string }) {
  return (
    <div className="mx-auto max-w-[1080px] px-9 pt-9 pb-12">
      <Card icon={Ico} title={title} className="max-w-2xl">
        <p className="text-[13.5px] leading-relaxed text-mac-ink2 max-w-[56ch] pt-1">{body}</p>
        <button onClick={() => ask(`What can you already do for ${title.toLowerCase()}?`)}
          className="mt-4 flex items-center gap-1 text-[13px] text-mac-accentHi hover:underline">
          Ask Himmy <ArrowUpRight size={14} strokeWidth={2} />
        </button>
      </Card>
    </div>
  );
}

/* ───────────────────────────────────────── google · mail + calendar (read-only) */
// Shared connect lifecycle for the Mail and Calendar tabs. Polls /google/status while a
// sign-in is in flight (the OAuth callback lands on the backend, not in-app), and flips to
// `connected` on its own. Honest about the one-time Google Cloud client setup.
function useGoogle() {
  const [status, setStatus] = useState<GoogleStatus | null>(null);
  const [connecting, setConnecting] = useState(false);

  const refresh = async () => {
    try { setStatus(await api.google.status()); } catch { /* backend warming */ }
  };
  useEffect(() => { refresh(); }, []);

  // While a sign-in is in flight, poll until the backend reports a connected account.
  useEffect(() => {
    if (!connecting) return;
    const t = setInterval(async () => {
      try {
        const s = await api.google.status();
        setStatus(s);
        if (s.connected) setConnecting(false);
      } catch { /* keep polling */ }
    }, 1500);
    return () => clearInterval(t);
  }, [connecting]);

  const connect = async () => {
    const r = await api.google.authUrl();
    if (r.ok && r.url) {
      setConnecting(true);
      const s = (window as any).himmy;
      if (s?.openExternal) s.openExternal(r.url);
      else window.open(r.url, "_blank");
      return { opened: true as const };
    }
    return { opened: false as const, needsSetup: !!r.needs_setup, message: r.message };
  };

  const disconnect = async () => { try { setStatus(await api.google.disconnect()); } catch { /* */ } };
  const cancel = () => setConnecting(false);

  return { status, connecting, connect, disconnect, cancel, refresh, setStatus };
}

// One-time setup card: paste the Google Cloud OAuth client_id + secret. Shown only when no
// client is configured. Honest about needing a Google Cloud project + the loopback redirect.
function GoogleSetup({ onSaved }: { onSaved: (s: GoogleStatus) => void }) {
  const [id, setId] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const redirect = `http://127.0.0.1:${(window as any).himmy?.backendPort ?? "8131"}/google/callback`;

  const save = async () => {
    if (!id.trim() || !secret.trim() || busy) return;
    setBusy(true); setMsg(null);
    try {
      const s = await api.google.setClient(id.trim(), secret.trim());
      if (s.configured) onSaved(s);
      else setMsg(s.message || "Couldn't save those credentials.");
    } catch (e: any) { setMsg(e.message); } finally { setBusy(false); }
  };

  return (
    <div className="w-full max-w-md rounded-2xl border border-mac-stroke bg-mac-fill px-7 py-7">
      <div className="flex items-center gap-2.5 mb-1.5">
        <KeyRound size={16} className="text-mac-accentHi" />
        <h2 className="font-display text-[16px] font-semibold tracking-[-0.01em]">One-time Google setup</h2>
      </div>
      <p className="text-[12.5px] leading-relaxed text-mac-ink2 mb-4">
        To connect Mail &amp; Calendar, Himmy needs a Google OAuth client from your own
        Google Cloud project — a one-time step. Create a “Web application” OAuth client and add
        this exact redirect URI:
      </p>
      <div className="flex items-center gap-2 rounded-[9px] bg-black/20 border border-mac-stroke px-3 h-9 mb-4">
        <code className="flex-1 text-[11.5px] font-mono text-mac-ink2 truncate">{redirect}</code>
        <button onClick={() => navigator.clipboard.writeText(redirect)} title="Copy"
          className="text-mac-ink3 hover:text-mac-ink"><Copy size={13} /></button>
      </div>
      <div className="space-y-2">
        <input value={id} onChange={(e) => setId(e.target.value)} placeholder="Client ID"
          className="w-full h-9 px-3 rounded-[9px] bg-mac-fillHi border border-mac-stroke text-[12.5px] text-mac-ink outline-none focus:border-mac-accent transition-colors placeholder:text-mac-ink3" />
        <input value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="Client secret" type="password"
          className="w-full h-9 px-3 rounded-[9px] bg-mac-fillHi border border-mac-stroke text-[12.5px] text-mac-ink outline-none focus:border-mac-accent transition-colors placeholder:text-mac-ink3" />
      </div>
      {msg && <p className="text-[12px] text-mac-red mt-2.5">{msg}</p>}
      <button onClick={save} disabled={busy || !id.trim() || !secret.trim()}
        className="mt-4 h-9 px-4 w-full rounded-[10px] bg-mac-accent text-[13px] font-medium text-white hover:bg-mac-accentHi transition-colors flex items-center justify-center gap-1.5 disabled:opacity-50">
        {busy ? <Loader2 size={14} className="animate-spin" /> : <ShieldCheck size={15} strokeWidth={2.25} />}
        Save credentials
      </button>
    </div>
  );
}

// Clean "Connect Google" empty-state for a tab. Routes through GoogleSetup if no client.
function GoogleConnect({ icon: Ico, title, blurb, g }: {
  icon: LucideIcon; title: string; blurb: string;
  g: ReturnType<typeof useGoogle>;
}) {
  const [note, setNote] = useState<string | null>(null);
  const [showSetup, setShowSetup] = useState(false);
  const configured = !!g.status?.configured;

  if (showSetup || (g.status && !configured)) {
    return (
      <div className="h-full grid place-items-center px-9">
        <GoogleSetup onSaved={(s) => { g.setStatus(s); setShowSetup(false); }} />
      </div>
    );
  }

  const onConnect = async () => {
    setNote(null);
    const r = await g.connect();
    if (!r.opened) {
      if (r.needsSetup) setShowSetup(true);
      else setNote(r.message || "Couldn't start sign-in.");
    }
  };

  return (
    <div className="h-full grid place-items-center px-9">
      <div className="w-full max-w-sm text-center">
        <div className="mx-auto h-14 w-14 rounded-2xl grid place-items-center bg-mac-fill border border-mac-stroke mb-5 shadow-mac">
          <Ico size={24} strokeWidth={1.75} className="text-mac-accentHi" />
        </div>
        <h2 className="font-display text-[18px] font-semibold tracking-[-0.01em] mb-2">{title}</h2>
        <p className="text-[13px] leading-relaxed text-mac-ink2 mb-6">{blurb}</p>
        {g.connecting ? (
          <div className="flex flex-col items-center gap-3">
            <div className="flex items-center gap-2 text-[13px] text-mac-ink2">
              <Loader2 size={15} className="animate-spin text-mac-accentHi" />
              Waiting for Google sign-in in your browser…
            </div>
            <button onClick={g.cancel} className="text-[12.5px] text-mac-ink3 hover:text-mac-ink">Cancel</button>
          </div>
        ) : (
          <>
            <button onClick={onConnect}
              className="h-9 px-4 rounded-[10px] bg-mac-accent text-[13px] font-medium text-white hover:bg-mac-accentHi transition-colors inline-flex items-center gap-2">
              <Globe size={15} strokeWidth={2.25} /> Connect Google
            </button>
            <p className="text-[11.5px] text-mac-ink3 mt-3 leading-relaxed">
              Read-only — Himmy can see your inbox and calendar, never send or change anything.
            </p>
            {note && <p className="text-[12px] text-mac-red mt-2">{note}</p>}
          </>
        )}
      </div>
    </div>
  );
}

// Header shared by both tabs once connected: account email + Disconnect.
function GoogleTabHeader({ title, count, label, email, onRefresh, onDisconnect, loading }: {
  title: string; count: number; label: string; email: string | null;
  onRefresh: () => void; onDisconnect: () => void; loading: boolean;
}) {
  return (
    <div className="shrink-0 h-[60px] px-7 flex items-center justify-between">
      <div className="flex items-baseline gap-2.5 min-w-0">
        <h1 className="font-display text-[19px] font-semibold tracking-[-0.01em] truncate">{title}</h1>
        <span className="text-[12.5px] text-mac-ink3 tnum shrink-0">{count} {label}{count === 1 ? "" : "s"}</span>
      </div>
      <div className="flex items-center gap-2">
        {email && (
          <span className="text-[12px] text-mac-ink3 hidden sm:inline truncate max-w-[180px]">{email}</span>
        )}
        <button onClick={onRefresh} disabled={loading}
          className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors flex items-center gap-1.5 disabled:opacity-50">
          {loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} Refresh
        </button>
        <button onClick={onDisconnect} title="Disconnect Google"
          className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">
          Disconnect
        </button>
      </div>
    </div>
  );
}

// --- Mail caches: show the inbox INSTANTLY on every open, refresh quietly in the background ---
// In-memory (survives tab switches) + localStorage (survives an app restart). Opened message
// bodies are memo-cached too, so re-opening an email is instant.
const MAIL_CACHE_KEY = "himmy.mail.inbox.v2"; // v2: rows carry category/unread/vip/automated
let mailMemCache: MailMessage[] | null = null;
const mailBodyCache = new Map<string, MailFull>();
function readMailCache(): MailMessage[] {
  if (mailMemCache) return mailMemCache;
  try {
    const raw = localStorage.getItem(MAIL_CACHE_KEY);
    if (raw) { mailMemCache = JSON.parse(raw) as MailMessage[]; return mailMemCache; }
  } catch { /* ignore */ }
  return [];
}
function writeMailCache(msgs: MailMessage[]) {
  mailMemCache = msgs;
  try { localStorage.setItem(MAIL_CACHE_KEY, JSON.stringify(msgs)); } catch { /* ignore */ }
}

// --- Mail presentation helpers ---------------------------------------------
// Strip the display name out of an RFC-5322 "Name <addr>" sender string.
function senderName(raw: string): string {
  const s = (raw || "").replace(/<[^>]*>/, "").replace(/"/g, "").trim();
  return s || (raw || "").replace(/[<>]/g, "").trim() || "Unknown";
}
// Deterministic, pleasant avatar color from a sender string (hashed → hue).
const MAIL_AVATAR_COLORS = [
  "#0A84FF", "#30D158", "#FF9F0A", "#BF5AF2", "#FF453A",
  "#64D2FF", "#FF375F", "#5E5CE6", "#FFD60A", "#AC8E68",
];
function avatarColor(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return MAIL_AVATAR_COLORS[h % MAIL_AVATAR_COLORS.length];
}
function avatarInitial(name: string): string {
  const c = (name || "").trim()[0];
  return c ? c.toUpperCase() : "?";
}
// Which date-group bucket a message falls in (Today / Yesterday / Earlier this week / Earlier).
function mailDateBucket(raw: string): "Today" | "Yesterday" | "Earlier this week" | "Earlier" {
  const d = new Date(raw);
  if (isNaN(d.getTime())) return "Earlier";
  const now = new Date();
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const dayMs = 86400000;
  const days = Math.round((startOf(now) - startOf(d)) / dayMs);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Earlier this week";
  return "Earlier";
}
const MAIL_BUCKET_ORDER = ["Today", "Yesterday", "Earlier this week", "Earlier"] as const;

// Category tabs over the list. "focused" also pulls in VIP senders; "all" shows everything.
type MailCat = "focused" | "promotions" | "social" | "updates" | "all";
const MAIL_TABS: { id: MailCat; label: string }[] = [
  { id: "focused", label: "Focused" },
  { id: "promotions", label: "Promotions" },
  { id: "social", label: "Social" },
  { id: "updates", label: "Updates" },
  { id: "all", label: "All" },
];
const MAIL_EMPTY: Record<MailCat, string> = {
  focused: "Inbox zero in Focused — nicely done.",
  promotions: "No promotions — nice and quiet.",
  social: "Nothing social right now.",
  updates: "No updates at the moment.",
  all: "Your inbox is empty.",
};
function inCat(m: MailMessage, cat: MailCat): boolean {
  if (cat === "all") return true;
  if (cat === "focused") return m.category === "focused" || m.vip;
  if (cat === "updates") return m.category === "updates" || m.category === "forums";
  return m.category === cat;
}
// Short category chip label (only shown on the All tab, where mixing is visible).
const MAIL_CAT_CHIP: Record<MailMessage["category"], string> = {
  focused: "Focused", promotions: "Promo", social: "Social", updates: "Updates", forums: "Forum",
};

function MailTab() {
  const g = useGoogle();
  // seed from cache → the list is on screen the instant you open the tab, no spinner
  const [messages, setMessages] = useState<MailMessage[]>(() => readMailCache());
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);   // the highlighted/read message

  const [cat, setCat] = useState<MailCat>("focused");
  const [query, setQuery] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [peopleOnly, setPeopleOnly] = useState(false);

  // Sender rules (muted / VIP). Mutating a rule busts the inbox cache server-side, so we reload.
  const [vip, setVip] = useState<string[]>([]);
  const [muted, setMuted] = useState<string[]>([]);
  const [showMuted, setShowMuted] = useState(false);

  // Digest state lives here (not inside MailDigestCard) so dismissing it and the already-fetched
  // summary both survive switching to another tab and back. Dismiss persists to localStorage.
  const [digestDismissed, setDigestDismissed] = useState(() => {
    try { return localStorage.getItem("himmy.mail.digest.dismissed") === "1"; } catch { return false; }
  });
  const dismissDigest = () => {
    setDigestDismissed(true);
    try { localStorage.setItem("himmy.mail.digest.dismissed", "1"); } catch { /* ignore */ }
  };
  // The digest summary is fetched once here and kept across tab switches (the card used to
  // remount on every return to Focused and re-hit the network, flashing its skeleton).
  const [digestSummary, setDigestSummary] = useState<string | null>(null);
  const [digestLoading, setDigestLoading] = useState(false);
  const fetchDigest = async (force = false) => {
    setDigestLoading(true);
    try {
      const r = await api.mail.digest(force);
      setDigestSummary(r.ok && r.summary?.trim() ? r.summary.trim() : null);
    } catch { setDigestSummary(null); } finally { setDigestLoading(false); }
  };

  const load = async (force = false) => {
    setRefreshing(true); setError(null);
    try {
      const r = await api.mail.inbox(50, force);
      if (r.messages) {
        setMessages(r.messages); writeMailCache(r.messages);
        // If the currently-open message vanished (e.g. its sender was just muted), clear the
        // selection so the reading pane doesn't silently strand a stale highlight.
        setOpenId((id) => (id && !r.messages!.some((m) => m.id === id) ? null : id));
      }
      // only surface an error if we have nothing cached to show instead
      if (r.message && !(r.messages && r.messages.length)) setError(r.message);
    } catch (e: any) { if (!mailMemCache?.length) setError(e.message); } finally { setRefreshing(false); }
  };
  const loadRules = async () => {
    try { const r = await api.mail.rules.list(); if (r.ok) { setVip(r.vip || []); setMuted(r.muted || []); } }
    catch { /* rules are best-effort */ }
  };
  useEffect(() => {
    if (g.status?.connected) { load(); loadRules(); if (!digestDismissed) fetchDigest(false); }
  }, [g.status?.connected]);

  // Mute / unmute / VIP-toggle a sender, then reload the (now rule-filtered) inbox.
  const applyRule = async (action: "mute" | "unmute" | "vip" | "unvip", sender: string) => {
    try {
      const r = await api.mail.rules.set(action, sender);
      if (r.ok) { if (r.vip) setVip(r.vip); if (r.muted) setMuted(r.muted); }
    } catch { /* ignore */ }
    load(true);
    // A rule change busts the server-side digest cache too — re-pull so the brief reflects it.
    if (!digestDismissed) fetchDigest(false);
  };
  const isVip = (m: MailMessage) =>
    m.vip || vip.includes((m.from.match(/<([^>]+)>/)?.[1] || m.from).trim().toLowerCase());

  // counts per tab (over the people/unread-filtered set, ignoring the search box so they stay stable)
  const base = useMemo(() => messages.filter((m) =>
    (!peopleOnly || !m.automated) && (!unreadOnly || m.unread)
  ), [messages, peopleOnly, unreadOnly]);
  const counts = useMemo(() => {
    const c: Record<MailCat, number> = { focused: 0, promotions: 0, social: 0, updates: 0, all: 0 };
    for (const m of base) for (const t of MAIL_TABS) if (inCat({ ...m, vip: isVip(m) }, t.id)) c[t.id]++;
    return c;
  }, [base, vip]);

  const q = query.trim().toLowerCase();
  const visible = useMemo(() => base.filter((m) => {
    if (!inCat({ ...m, vip: isVip(m) }, cat)) return false;
    if (!q) return true;
    return senderName(m.from).toLowerCase().includes(q) || (m.subject || "").toLowerCase().includes(q);
  }), [base, cat, q, vip]);

  // group the visible list by date bucket, preserving the server's (recency) order within each
  const groups = useMemo(() => {
    const byBucket = new Map<string, MailMessage[]>();
    for (const m of visible) {
      const b = mailDateBucket(m.date);
      (byBucket.get(b) ?? byBucket.set(b, []).get(b)!).push(m);
    }
    return MAIL_BUCKET_ORDER.filter((b) => byBucket.has(b)).map((b) => ({ bucket: b, items: byBucket.get(b)! }));
  }, [visible]);

  const open = openId ? messages.find((m) => m.id === openId) : undefined;

  if (!g.status || !g.status.connected) {
    return <GoogleConnect icon={Mail} title="Your mail, in Himmy" g={g}
      blurb="Connect Google to read your recent inbox here — and let the command bar answer “what’s new in my mail?” straight from your library." />;
  }

  return (
    <div className="h-full flex flex-col">
      <GoogleTabHeader title="Mail" count={messages.length} label="message" email={g.status.email}
        onRefresh={() => load(true)} onDisconnect={g.disconnect} loading={refreshing} />

      <div className="flex-1 min-h-0 flex">
        {/* ── left: list pane ─────────────────────────────────────────── */}
        <div className="w-[380px] shrink-0 border-r border-mac-stroke flex flex-col min-h-0">
          {/* tabs */}
          <div className="shrink-0 px-3 pt-1 flex items-center gap-0.5 overflow-x-auto">
            {MAIL_TABS.map((t) => (
              <button key={t.id} onClick={() => setCat(t.id)}
                className={`shrink-0 h-8 px-2.5 rounded-[8px] text-[12.5px] font-medium transition-colors flex items-center gap-1.5 ${
                  cat === t.id ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink3 hover:text-mac-ink2"}`}>
                {t.label}
                {counts[t.id] > 0 && (
                  <span className={`tnum text-[11px] ${cat === t.id ? "text-mac-ink2" : "text-mac-ink4"}`}>{counts[t.id]}</span>
                )}
              </button>
            ))}
          </div>

          {/* search + filters */}
          <div className="shrink-0 px-3 pt-2 pb-2 space-y-2">
            <div className="flex items-center gap-2 h-8 px-2.5 rounded-[9px] bg-mac-fill border border-mac-stroke">
              <Search size={13} className="text-mac-ink3 shrink-0" />
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search sender or subject"
                className="flex-1 bg-transparent text-[12.5px] text-mac-ink outline-none placeholder:text-mac-ink3" />
              {query && <button onClick={() => setQuery("")} className="text-mac-ink3 hover:text-mac-ink"><X size={13} /></button>}
            </div>
            <div className="flex items-center gap-1.5">
              <FilterChip on={unreadOnly} onClick={() => setUnreadOnly((v) => !v)} icon={Circle} label="Unread" />
              <FilterChip on={peopleOnly} onClick={() => setPeopleOnly((v) => !v)} icon={Users} label="People" />
              {muted.length > 0 && (
                <button onClick={() => setShowMuted((v) => !v)}
                  className="ml-auto h-7 px-2 rounded-[8px] text-[11.5px] text-mac-ink3 hover:text-mac-ink2 flex items-center gap-1">
                  <BellOff size={12} /> {muted.length} muted
                </button>
              )}
            </div>
            {showMuted && muted.length > 0 && (
              <div className="rounded-[9px] border border-mac-stroke bg-mac-fill p-2 space-y-1 max-h-32 overflow-auto">
                {muted.map((s) => (
                  <div key={s} className="flex items-center justify-between gap-2 text-[11.5px]">
                    <span className="text-mac-ink2 truncate">{s}</span>
                    <button onClick={() => applyRule("unmute", s)}
                      className="text-mac-accentHi hover:underline shrink-0">Unmute</button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* list */}
          <div className="flex-1 min-h-0 overflow-auto px-2 pb-6">
            {cat === "focused" && !digestDismissed && (digestLoading || digestSummary) && (
              <MailDigestCard summary={digestSummary} loading={digestLoading}
                onRefresh={() => fetchDigest(true)} onDismiss={dismissDigest} />
            )}
            {refreshing && messages.length === 0 ? (
              <div className="h-40 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
            ) : error && messages.length === 0 ? (
              <div className="px-3 pt-10 text-center text-[12.5px] text-mac-ink2">{error}</div>
            ) : visible.length === 0 ? (
              <div className="px-3 pt-16 grid place-items-center text-center">
                <Inbox size={28} strokeWidth={1.5} className="text-mac-ink3 mb-2.5" />
                <p className="text-[12.5px] text-mac-ink2">
                  {q ? "Nothing matches your search." : MAIL_EMPTY[cat]}
                </p>
              </div>
            ) : (
              groups.map((grp) => (
                <div key={grp.bucket} className="mb-1">
                  <div className="px-2.5 pt-2.5 pb-1 text-[10.5px] font-semibold uppercase tracking-[0.06em] text-mac-ink3">{grp.bucket}</div>
                  {grp.items.map((m) => (
                    <MailRow key={m.id} m={m} active={m.id === openId} vip={isVip(m)} showChip={cat === "all"}
                      onOpen={() => setOpenId(m.id)}
                      onMute={() => applyRule("mute", m.from)}
                      onToggleVip={() => applyRule(isVip(m) ? "unvip" : "vip", m.from)} />
                  ))}
                </div>
              ))
            )}
          </div>
        </div>

        {/* ── right: reading pane ─────────────────────────────────────── */}
        <div className="flex-1 min-w-0 min-h-0 overflow-auto">
          {open ? (
            <MailReader key={open.id} id={open.id} preview={open} />
          ) : (
            <div className="h-full grid place-items-center text-center px-8">
              <div className="max-w-[34ch]">
                <Mail size={32} strokeWidth={1.5} className="text-mac-ink3 mx-auto mb-3" />
                <p className="text-[14px] text-mac-ink2">Select a message to read it here.</p>
                <p className="text-[12px] text-mac-ink3 mt-1.5 leading-relaxed">
                  Himmy can draft or send a reply for any email you open.
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// A small pill toggle used for the Unread / People filters.
function FilterChip({ on, onClick, icon: Ico, label }: {
  on: boolean; onClick: () => void; icon: LucideIcon; label: string;
}) {
  return (
    <button onClick={onClick}
      className={`h-7 px-2.5 rounded-[8px] text-[11.5px] font-medium transition-colors flex items-center gap-1.5 border ${
        on ? "bg-mac-accentDim border-mac-accent/40 text-mac-accentHi"
           : "bg-mac-fill border-mac-stroke text-mac-ink3 hover:text-mac-ink2 hover:border-mac-strokeHi"}`}>
      <Ico size={12} strokeWidth={on ? 2.5 : 2} /> {label}
    </button>
  );
}

// "Today in your inbox" — a dismissible, model-written brief atop the Focused tab. State
// (summary, loading, dismissed) is owned by MailTab so it survives tab switches; this is a
// pure presentational component driven by props.
function MailDigestCard({ summary, loading, onRefresh, onDismiss }: {
  summary: string | null; loading: boolean; onRefresh: () => void; onDismiss: () => void;
}) {
  return (
    <div className="mx-1 mt-2 mb-1 rounded-[11px] border border-mac-stroke bg-mac-fill px-3.5 py-3">
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <div className="flex items-center gap-1.5 text-[11.5px] font-semibold text-mac-ink2">
          <Sparkles size={13} className="text-mac-accentHi" /> Today in your inbox
        </div>
        <div className="flex items-center gap-1">
          <button onClick={onRefresh} disabled={loading} title="Refresh"
            className="text-mac-ink3 hover:text-mac-ink disabled:opacity-50">
            {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          </button>
          <button onClick={onDismiss} title="Dismiss" className="text-mac-ink3 hover:text-mac-ink"><X size={13} /></button>
        </div>
      </div>
      {loading && !summary ? (
        <div className="space-y-1.5 py-0.5">
          <div className="h-2.5 rounded bg-mac-fillHi w-[92%] animate-pulse" />
          <div className="h-2.5 rounded bg-mac-fillHi w-[78%] animate-pulse" />
          <div className="h-2.5 rounded bg-mac-fillHi w-[64%] animate-pulse" />
        </div>
      ) : (
        <p className="text-[12.5px] leading-relaxed text-mac-ink2 whitespace-pre-wrap">{summary}</p>
      )}
    </div>
  );
}

function MailRow({ m, active, vip, showChip, onOpen, onMute, onToggleVip }: {
  m: MailMessage; active: boolean; vip: boolean; showChip: boolean;
  onOpen: () => void; onMute: () => void; onToggleVip: () => void;
}) {
  const name = senderName(m.from);
  const color = avatarColor(name || m.from);
  return (
    <div role="button" tabIndex={0} onClick={onOpen}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } }}
      className={`group relative rounded-[9px] px-2 py-2 cursor-pointer transition-colors flex gap-2.5 outline-none focus-visible:ring-2 focus-visible:ring-mac-accent ${
        active ? "bg-mac-accentDim" : "hover:bg-mac-fill"}`}>
      {/* avatar */}
      <div className="shrink-0 mt-0.5 h-7 w-7 rounded-full grid place-items-center text-[12px] font-semibold text-white"
        style={{ backgroundColor: color }}>{avatarInitial(name)}</div>

      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2">
          {m.unread && <span className="shrink-0 h-1.5 w-1.5 rounded-full bg-mac-accent" />}
          <span className={`flex-1 truncate text-[12.5px] ${m.unread ? "text-mac-ink font-medium" : "text-mac-ink2"}`}>{name}</span>
          {vip && <Star size={11} className="shrink-0 text-mac-orange" fill="currentColor" />}
          <span className="shrink-0 text-[11px] text-mac-ink3 tnum">{relTime(m.date)}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className={`flex-1 truncate text-[12.5px] ${m.unread ? "text-mac-ink font-semibold" : "text-mac-ink2"}`}>
            {m.subject || "(no subject)"}
          </span>
          {showChip && (
            <span className="shrink-0 text-[9.5px] px-1.5 py-0.5 rounded-full bg-mac-fillHi text-mac-ink3">{MAIL_CAT_CHIP[m.category]}</span>
          )}
        </div>
        <p className={`truncate text-[11.5px] leading-snug ${m.unread ? "text-mac-ink3" : "text-mac-ink4"}`}>{m.snippet}</p>
      </div>

      {/* hover quick-actions */}
      <div className="absolute right-1.5 bottom-1.5 hidden group-hover:flex items-center gap-0.5">
        <button onClick={(e) => { e.stopPropagation(); onToggleVip(); }} title={vip ? "Remove VIP" : "Mark VIP"}
          className="h-6 w-6 grid place-items-center rounded-[7px] bg-mac-fillHi border border-mac-stroke text-mac-ink3 hover:text-mac-orange">
          <Star size={12} fill={vip ? "currentColor" : "none"} className={vip ? "text-mac-orange" : ""} />
        </button>
        <button onClick={(e) => { e.stopPropagation(); onMute(); }} title="Mute sender"
          className="h-6 w-6 grid place-items-center rounded-[7px] bg-mac-fillHi border border-mac-stroke text-mac-ink3 hover:text-mac-ink">
          <BellOff size={12} />
        </button>
      </div>
    </div>
  );
}

/* Full-message reader — lives in the right pane when a Mail row is selected. Reads the body via
   mail_read's route, and offers one-tap "Reply / Draft with Himmy" that prefill the Cmd-K bar. */
function MailReader({ id, preview }: { id: string; preview?: MailMessage }) {
  const cached = mailBodyCache.get(id) || null;
  const [m, setM] = useState<MailFull | null>(cached);
  const [loading, setLoading] = useState(!cached);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const hit = mailBodyCache.get(id) || null;
    setM(hit); setErr(null); setLoading(!hit);          // cached body shows at once; refresh quietly
    api.mail.message(id)
      .then((r) => {
        if (!alive) return;
        if (r.ok && r.email) { mailBodyCache.set(id, r.email); setM(r.email); }
        else if (!hit) setErr(r.message || "Couldn't open this email.");
      })
      .catch((e) => { if (alive && !hit) setErr(e.message || "Couldn't open this email."); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [id]);

  const rawFrom = m?.from || preview?.from || "";
  const fromName = senderName(rawFrom);
  const subject = m?.subject || preview?.subject || "(no subject)";
  const ref = `the email from ${fromName} about "${subject}" (message_id ${id})`;
  const color = avatarColor(fromName || rawFrom);

  return (
    <div className="mx-auto max-w-[760px] px-8 py-6">
      <div className="flex items-center justify-end gap-1.5 mb-5">
        <button onClick={() => ask(`Draft a reply to ${ref}. Save it as a draft.`)}
          className="h-7 px-2.5 rounded-md bg-mac-fill border border-mac-stroke text-[12px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors flex items-center gap-1.5">
          <SquarePen size={13} /> Draft reply
        </button>
        <button onClick={() => ask(`Reply to ${ref} saying: `)}
          className="h-7 px-2.5 rounded-md bg-mac-accent text-white text-[12px] font-medium hover:bg-mac-accentHi transition-colors flex items-center gap-1.5">
          <Sparkles size={13} /> Reply with Himmy
        </button>
      </div>

      <h1 className="font-display text-[20px] font-semibold text-mac-ink leading-snug">{subject}</h1>
      <div className="flex items-center gap-2.5 mt-3 mb-5 pb-4 border-b border-mac-stroke">
        <div className="shrink-0 h-8 w-8 rounded-full grid place-items-center text-[13px] font-semibold text-white"
          style={{ backgroundColor: color }}>{avatarInitial(fromName)}</div>
        <div className="min-w-0">
          <div className="text-[13px] text-mac-ink truncate">{fromName}</div>
          <div className="text-[11.5px] text-mac-ink3 truncate">
            {(m?.date || preview?.date) ? relTime(m?.date || preview!.date) : ""}
            {m?.to ? ` · to ${senderName(m.to)}` : ""}
          </div>
        </div>
      </div>

      {loading ? (
        <div className="h-40 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
      ) : err ? (
        <div className="text-[13px] text-mac-orange">{err}</div>
      ) : (
        <div className="text-[13.5px] leading-relaxed text-mac-ink2 whitespace-pre-wrap break-words">
          {m?.body?.trim() || preview?.snippet || "(no text content)"}
        </div>
      )}
    </div>
  );
}

type EditorState = {
  id?: string; recurringId?: string; title: string; date: string; allDay: boolean;
  startTime: string; endTime: string; location: string;
};
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function eventDayKey(start: string): string {
  if (!start) return "";
  if (!start.includes("T")) return start.slice(0, 10); // all-day
  const d = new Date(start);
  return isNaN(d.getTime()) ? "" : dayKey(d);
}
function splitDateTime(s: string): { date: string; time: string; allDay: boolean } {
  if (!s) return { date: dayKey(new Date()), time: "09:00", allDay: false };
  if (!s.includes("T")) return { date: s.slice(0, 10), time: "09:00", allDay: true };
  const d = new Date(s);
  if (isNaN(d.getTime())) return { date: dayKey(new Date()), time: "09:00", allDay: false };
  return { date: dayKey(d), time: `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`, allDay: false };
}
function addDaysStr(date: string, n: number): string {
  const d = new Date(`${date}T00:00:00`); d.setDate(d.getDate() + n); return dayKey(d);
}
// "Jun 22 – 28" / "Jun 29 – Jul 5" — the Sun–Sat span of the week containing `anchor`.
function weekRangeLabel(anchor: Date): string {
  const s = new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate());
  s.setDate(s.getDate() - s.getDay());
  const e = new Date(s); e.setDate(s.getDate() + 6);
  const sm = s.toLocaleDateString([], { month: "short", day: "numeric" });
  const em = s.getMonth() === e.getMonth()
    ? String(e.getDate())
    : e.toLocaleDateString([], { month: "short", day: "numeric" });
  return `${sm} – ${em}`;
}

// Planner — Tasks + Calendar merged into one tab with a segmented toggle, so the top bar
// stays clean. Each view renders `embedded` (its own title bar hidden) so there's one header.
function Planner({ initial = "tasks" }: { initial?: "tasks" | "calendar" }) {
  const [tab, setTab] = useState<"tasks" | "calendar">(initial);
  useEffect(() => { setTab(initial); }, [initial]);
  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 h-[52px] px-6 flex items-center">
        <div className="inline-flex items-center gap-0.5 p-0.5 rounded-[10px] bg-mac-fill border border-mac-stroke">
          {([["tasks", "Tasks", CheckSquare], ["calendar", "Calendar", Calendar]] as const).map(([id, label, Ico]) => (
            <button key={id} onClick={() => setTab(id)}
              className={`flex items-center gap-1.5 h-[30px] px-3.5 rounded-[8px] text-[12.5px] transition-colors ${
                tab === id ? "bg-mac-fillHi text-mac-ink shadow-tab" : "text-mac-ink2 hover:text-mac-ink"}`}>
              <Ico size={14} strokeWidth={2} className={tab === id ? "text-mac-accentHi" : ""} />
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="flex-1 min-h-0">
        {tab === "tasks" ? <Tasks embedded /> : <CalendarTab embedded />}
      </div>
    </div>
  );
}

function CalendarTab({ embedded = false }: { embedded?: boolean }) {
  const g = useGoogle();
  const [view, setView] = useState<"month" | "week" | "list">("month");
  const [month, setMonth] = useState(() => { const d = new Date(); return new Date(d.getFullYear(), d.getMonth(), 1); });
  // Week view navigates independently (by 7-day steps) off its own anchor date.
  const [weekAnchor, setWeekAnchor] = useState(() => new Date());
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editor, setEditor] = useState<EditorState | null>(null);
  // "Plan my week" — when open, the assistant drafts reviewable time-blocks.
  const [planning, setPlanning] = useState(false);

  const monthIndex = month.getMonth();
  const gridStart = useMemo(() => {
    const f = new Date(month.getFullYear(), month.getMonth(), 1);
    const s = new Date(f); s.setDate(1 - f.getDay()); return s;
  }, [month]);
  const totalCells = useMemo(() => {
    const offset = new Date(month.getFullYear(), month.getMonth(), 1).getDay();
    const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
    return Math.ceil((offset + days) / 7) * 7;
  }, [month]);

  // Bumped on every calendar/tasks refresh signal — WeekGrid (which owns its own fetch)
  // watches this to reload when Himmy or a drop mutates events/tasks.
  const [refreshKey, setRefreshKey] = useState(0);

  const load = async () => {
    // The Week view fetches its own events (it also needs tasks); skip the shared fetch there.
    if (view === "week") { setLoading(false); return; }
    setLoading(true); setError(null);
    try {
      let r;
      if (view === "month") {
        const min = new Date(gridStart);
        const max = new Date(gridStart); max.setDate(max.getDate() + totalCells);
        r = await api.calendar.range(min.toISOString(), max.toISOString());
      } else {
        r = await api.calendar.events(40);
      }
      if (r.message) setError(r.message);
      setEvents(r.events || []);
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (g.status?.connected) load(); }, [g.status?.connected, view, month]);
  // Himmy added / moved / removed an event → refresh this month without leaving the tab.
  useRefreshSignal("calendar", () => { if (g.status?.connected) { load(); setRefreshKey((k) => k + 1); } });
  // A scheduled task also changes what the Week view shows (unscheduled rail + task-blocks).
  useRefreshSignal("tasks", () => { if (view === "week") setRefreshKey((k) => k + 1); });

  const byDay = useMemo(() => {
    const m: Record<string, CalendarEvent[]> = {};
    events.forEach((e) => { const k = eventDayKey(e.start); if (k) (m[k] ||= []).push(e); });
    return m;
  }, [events]);

  const newOn = (date: string) =>
    setEditor({ title: "", date, allDay: false, startTime: "09:00", endTime: "10:00", location: "" });
  const editEvent = (e: CalendarEvent) => {
    const p = splitDateTime(e.start); const pe = splitDateTime(e.end);
    setEditor({ id: e.id || undefined, recurringId: e.recurring_event_id || undefined,
      title: e.summary, date: p.date, allDay: p.allDay,
      startTime: p.time, endTime: pe.time, location: e.location || "" });
  };
  const save = async (s: EditorState) => {
    const title = s.title.trim() || "(no title)";
    let start: string, end: string;
    if (s.allDay) { start = s.date; end = addDaysStr(s.date, 1); }
    else {
      // Send LOCAL wall-clock (no timezone) — the backend attaches the user's time zone.
      start = `${s.date}T${s.startTime}:00`;
      let endStr = `${s.date}T${s.endTime}:00`;
      if (new Date(endStr) <= new Date(start)) {
        const d = new Date(`${s.date}T${s.startTime}:00`); d.setHours(d.getHours() + 1);
        endStr = `${s.date}T${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:00`;
      }
      end = endStr;
    }
    const body = { summary: title, start, end, all_day: s.allDay, location: s.location || null };
    try {
      const r = s.id ? await api.calendar.update(s.id, body) : await api.calendar.create(body);
      if (!r.ok) { setError(r.message || "Couldn't save the event."); return; }
      setEditor(null); load();
    } catch (e: any) { setError(e.message); }
  };
  const remove = async (id: string) => {
    try { await api.calendar.remove(id); setEditor(null); load(); }
    catch (e: any) { setError(e.message); }
  };

  if (!g.status || !g.status.connected) {
    return <GoogleConnect icon={Calendar} title="Your schedule, in Himmy" g={g}
      blurb="Connect Google to see and manage your calendar here — and ask Himmy to add, move, or cancel events for you." />;
  }

  return (
    <div className="h-full flex flex-col">
      {!embedded && (
        <GoogleTabHeader title="Calendar" count={events.length} label="event" email={g.status.email}
          onRefresh={load} onDisconnect={g.disconnect} loading={loading} />
      )}

      <div className="shrink-0 px-6 pb-3 flex items-center gap-2">
        <div className="flex items-center gap-0.5">
          <button onClick={() => view === "week"
              ? setWeekAnchor((d) => { const n = new Date(d); n.setDate(n.getDate() - 7); return n; })
              : setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}
            className="h-8 w-8 grid place-items-center rounded-[9px] text-mac-ink2 hover:text-mac-ink hover:bg-mac-fill transition-colors"><ChevronLeft size={16} /></button>
          <button onClick={() => view === "week"
              ? setWeekAnchor((d) => { const n = new Date(d); n.setDate(n.getDate() + 7); return n; })
              : setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}
            className="h-8 w-8 grid place-items-center rounded-[9px] text-mac-ink2 hover:text-mac-ink hover:bg-mac-fill transition-colors"><ChevronRight size={16} /></button>
        </div>
        <h2 className="font-display text-[16px] font-semibold tracking-[-0.01em] min-w-[140px]">
          {view === "week" ? weekRangeLabel(weekAnchor) : month.toLocaleDateString([], { month: "long", year: "numeric" })}
        </h2>
        <button onClick={() => { const d = new Date(); setMonth(new Date(d.getFullYear(), d.getMonth(), 1)); setWeekAnchor(new Date()); }}
          className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">Today</button>
        <div className="ml-auto flex items-center gap-2">
          <div className="flex items-center gap-0.5 p-0.5 rounded-[9px] bg-mac-fill border border-mac-stroke">
            {(["month", "week", "list"] as const).map((v) => (
              <button key={v} onClick={() => setView(v)}
                className={`h-7 px-3 rounded-[7px] text-[12.5px] capitalize transition-colors ${view === v ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink2 hover:text-mac-ink"}`}>{v}</button>
            ))}
          </div>
          {view === "week" && (
            <button onClick={() => setPlanning(true)}
              className="h-8 px-3.5 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] font-medium text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors flex items-center gap-1.5">
              <Sparkles size={14} strokeWidth={2.2} className="text-mac-accentHi" /> Plan my week
            </button>
          )}
          <button onClick={() => newOn(dayKey(new Date()))}
            className="h-8 px-3.5 rounded-[9px] bg-mac-accent text-[12.5px] font-medium text-white hover:bg-mac-accentHi transition-colors flex items-center gap-1.5">
            <Plus size={14} strokeWidth={2.5} /> New event
          </button>
        </div>
      </div>

      <div className={`flex-1 min-h-0 px-6 pb-10 ${view === "week" ? "overflow-hidden" : "overflow-auto"}`}>
        {error && view !== "week" ? (
          <div className="h-40 grid place-items-center text-center text-[13px] text-mac-ink2 max-w-[44ch] mx-auto">{error}</div>
        ) : view === "week" ? (
          <WeekGrid weekAnchor={weekAnchor} onEdit={editEvent} onMutated={() => emitRefresh("calendar")} refreshKey={refreshKey} />
        ) : view === "month" ? (
          <MonthGrid gridStart={gridStart} totalCells={totalCells} monthIndex={monthIndex}
            byDay={byDay} onNew={newOn} onEdit={editEvent} />
        ) : loading && events.length === 0 ? (
          <div className="h-40 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
        ) : events.length === 0 ? (
          <div className="pt-16 grid place-items-center text-center">
            <Calendar size={32} strokeWidth={1.5} className="text-mac-ink3 mb-3" />
            <p className="text-[14px] text-mac-ink2">Nothing on your calendar ahead.</p>
          </div>
        ) : (
          <div className="mx-auto max-w-[680px] space-y-2.5">
            {events.map((e, i) => <EventRow key={(e.id || "") + i} e={e} onClick={() => editEvent(e)} />)}
          </div>
        )}
      </div>

      {editor && (
        <EventEditor state={editor} onChange={setEditor}
          onSave={save} onDelete={remove} onClose={() => setEditor(null)} />
      )}

      {planning && (
        <PlanWeekModal onClose={() => setPlanning(false)}
          onAdded={() => { emitRefresh("calendar"); emitRefresh("tasks"); load(); setRefreshKey((k) => k + 1); }} />
      )}
    </div>
  );
}

function MonthGrid({ gridStart, totalCells, monthIndex, byDay, onNew, onEdit }: {
  gridStart: Date; totalCells: number; monthIndex: number;
  byDay: Record<string, CalendarEvent[]>;
  onNew: (date: string) => void; onEdit: (e: CalendarEvent) => void;
}) {
  const today = dayKey(new Date());
  const cells = Array.from({ length: totalCells }, (_, i) => { const d = new Date(gridStart); d.setDate(gridStart.getDate() + i); return d; });
  return (
    <div className="rounded-xl border border-mac-stroke overflow-hidden">
      <div className="grid grid-cols-7 bg-mac-fill border-b border-mac-stroke">
        {WEEKDAYS.map((w) => (
          <div key={w} className="px-2 py-2 text-[10.5px] uppercase tracking-wide text-mac-ink3 font-medium text-center">{w}</div>
        ))}
      </div>
      <div className="grid grid-cols-7">
        {cells.map((d, i) => {
          const k = dayKey(d); const inMonth = d.getMonth() === monthIndex;
          const evs = byDay[k] || []; const isToday = k === today;
          return (
            <div key={i} onClick={() => onNew(k)}
              className={`min-h-[106px] border-b border-mac-stroke p-1.5 cursor-pointer transition-colors ${i % 7 === 6 ? "" : "border-r"} ${inMonth ? "hover:bg-mac-fill/40" : "bg-black/15"}`}>
              <div className="flex justify-end">
                <span className={`text-[12px] tnum h-6 w-6 grid place-items-center rounded-full ${isToday ? "bg-mac-accent text-white font-semibold" : inMonth ? "text-mac-ink2" : "text-mac-ink4"}`}>{d.getDate()}</span>
              </div>
              <div className="mt-0.5 space-y-1">
                {evs.slice(0, 3).map((e, j) => (
                  <button key={j} onClick={(ev) => { ev.stopPropagation(); onEdit(e); }}
                    title={e.summary}
                    className="w-full text-left truncate rounded-md px-1.5 py-0.5 text-[11px] leading-tight bg-mac-accentDim text-mac-ink hover:bg-mac-accent hover:text-white transition-colors">
                    {e.start.includes("T") ? <span className="text-mac-ink3 mr-1 tnum">{fmtTime(e.start)}</span> : null}{e.summary}
                  </button>
                ))}
                {evs.length > 3 && <div className="px-1.5 text-[10.5px] text-mac-ink3">+{evs.length - 3} more</div>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function EventEditor({ state, onChange, onSave, onDelete, onClose }: {
  state: EditorState; onChange: (s: EditorState) => void;
  onSave: (s: EditorState) => void; onDelete: (id: string) => void; onClose: () => void;
}) {
  const s = state;
  const fld = "h-9 rounded-[9px] bg-mac-fill border border-mac-stroke px-2.5 text-[13px] text-mac-ink outline-none focus:border-mac-accent [color-scheme:dark]";
  return (
    <div className="absolute inset-0 z-40 grid place-items-center bg-black/45" onMouseDown={onClose}>
      <div onMouseDown={(e) => e.stopPropagation()}
        className="w-[440px] max-w-[calc(100%-3rem)] rounded-2xl bg-[rgba(30,31,37,0.98)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden">
        <div className="h-12 px-4 flex items-center justify-between border-b border-mac-stroke">
          <span className="text-[13px] font-display font-medium text-mac-ink">{s.id ? "Edit event" : "New event"}</span>
          <button onClick={onClose} className="text-mac-ink3 hover:text-mac-ink"><X size={16} /></button>
        </div>
        <div className="p-4 space-y-3">
          <input autoFocus value={s.title} onChange={(e) => onChange({ ...s, title: e.target.value })}
            onKeyDown={(e) => { if (e.key === "Enter" && s.title.trim()) onSave(s); }}
            placeholder="Event title"
            className="w-full h-10 rounded-[10px] bg-mac-fill border border-mac-stroke px-3 text-[14px] text-mac-ink outline-none focus:border-mac-accent placeholder:text-mac-ink3" />
          {s.recurringId && (
            <div className="text-[11.5px] text-mac-ink3 leading-snug">Repeating event — saving changes only this date; use “Whole series” to remove all of them.</div>
          )}
          <div className="flex items-center gap-2.5">
            <input type="date" value={s.date} onChange={(e) => onChange({ ...s, date: e.target.value })} className={`flex-1 ${fld}`} />
            <label className="flex items-center gap-1.5 text-[12.5px] text-mac-ink2 select-none cursor-pointer">
              <input type="checkbox" checked={s.allDay} onChange={(e) => onChange({ ...s, allDay: e.target.checked })} className="accent-mac-accent" /> All day
            </label>
          </div>
          {!s.allDay && (
            <div className="flex items-center gap-2.5">
              <input type="time" value={s.startTime} onChange={(e) => onChange({ ...s, startTime: e.target.value })} className={`flex-1 ${fld}`} />
              <span className="text-mac-ink3 text-[13px]">to</span>
              <input type="time" value={s.endTime} onChange={(e) => onChange({ ...s, endTime: e.target.value })} className={`flex-1 ${fld}`} />
            </div>
          )}
          <input value={s.location} onChange={(e) => onChange({ ...s, location: e.target.value })} placeholder="Location (optional)"
            className="w-full h-9 rounded-[9px] bg-mac-fill border border-mac-stroke px-3 text-[13px] text-mac-ink outline-none focus:border-mac-accent placeholder:text-mac-ink3" />
        </div>
        <div className="px-4 py-3 flex items-center justify-between border-t border-mac-stroke">
          {s.id ? (
            s.recurringId ? (
              <div className="flex items-center gap-3 text-[12px] text-mac-red">
                <button onClick={() => onDelete(s.id!)} className="hover:underline flex items-center gap-1.5"><Trash2 size={12} /> This event</button>
                <span className="text-mac-stroke">·</span>
                <button onClick={() => onDelete(s.recurringId!)} className="hover:underline">Whole series</button>
              </div>
            ) : (
              <button onClick={() => onDelete(s.id!)} className="text-[12.5px] text-mac-red hover:underline flex items-center gap-1.5">
                <Trash2 size={13} /> Delete
              </button>
            )
          ) : <span />}
          <div className="flex items-center gap-2">
            <button onClick={onClose} className="h-8 px-3.5 rounded-[9px] text-[12.5px] text-mac-ink2 hover:text-mac-ink transition-colors">Cancel</button>
            <button onClick={() => onSave(s)} disabled={!s.title.trim()}
              className="h-8 px-4 rounded-[9px] bg-mac-accent text-white text-[12.5px] font-medium hover:bg-mac-accentHi disabled:opacity-40 transition-colors">Save</button>
          </div>
        </div>
      </div>
    </div>
  );
}

function EventRow({ e, onClick }: { e: CalendarEvent; onClick?: () => void }) {
  const allDay = !!e.start && !e.start.includes("T");
  return (
    <div onClick={onClick}
      className="flex items-stretch gap-3.5 rounded-[11px] border border-mac-stroke bg-mac-fill px-4 py-3 hover:border-mac-strokeHi transition-colors cursor-pointer">
      <div className="shrink-0 w-14 flex flex-col items-center justify-center rounded-[9px] bg-mac-fillHi border border-mac-stroke py-1.5">
        <span className="text-[10px] uppercase tracking-wide text-mac-ink3">{fmtDay(e.start)}</span>
        <span className="font-display text-[18px] font-semibold leading-none tnum text-mac-ink">{fmtDate(e.start)}</span>
      </div>
      <div className="min-w-0 flex-1 flex flex-col justify-center">
        <div className="text-[13.5px] text-mac-ink truncate">{e.summary}</div>
        <div className="flex items-center gap-2.5 mt-0.5 text-[12px] text-mac-ink3">
          <span className="inline-flex items-center gap-1"><Clock size={11} />{allDay ? "All day" : `${fmtTime(e.start)}${e.end ? ` – ${fmtTime(e.end)}` : ""}`}</span>
          {e.location && <span className="inline-flex items-center gap-1 truncate"><MapPin size={11} />{e.location}</span>}
        </div>
      </div>
    </div>
  );
}

// Small date helpers — best-effort, never throw on odd inputs.
function relTime(raw: string): string {
  const d = new Date(raw);
  if (isNaN(d.getTime())) return "";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))}m`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h`;
  if (diff < 7 * 86400) return `${Math.round(diff / 86400)}d`;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
function fmtDay(raw: string): string {
  const d = new Date(raw); return isNaN(d.getTime()) ? "" : d.toLocaleDateString([], { weekday: "short" });
}
function fmtDate(raw: string): string {
  const d = new Date(raw); return isNaN(d.getTime()) ? "·" : String(d.getDate());
}
function fmtTime(raw: string): string {
  if (!raw || !raw.includes("T")) return "";
  const d = new Date(raw); return isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/* ───────────────────────────────────────── tasks (real board · shared with Himmy) */
function Tasks({ embedded = false }: { embedded?: boolean }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");          // YYYY-MM-DD from the date input
  const [priority, setPriority] = useState(0); // 0..3, cycled by the flag button
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const load = async () => {
    try { const r = await api.tasks.list(); setTasks(r.tasks); }
    catch { /* backend warming up */ }
    finally { setLoaded(true); }
  };
  // Poll so tasks added via the ⌘K command bar (Himmy → add_task) appear here too.
  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, []);
  // Instant refresh when Himmy adds/completes a task (the 4s poll is the backstop).
  useRefreshSignal("tasks", () => load());

  const parsed = parseQuickAdd(title);
  const add = async () => {
    const v = title.trim();
    if (!v || busy) return;
    setBusy(true);
    const p = parseQuickAdd(v);
    const finalDue = due || p.due || undefined;        // the manual pickers override the parse
    const finalPriority = priority || p.priority || undefined;
    try {
      const r = await api.tasks.add(p.title, { due: finalDue, priority: finalPriority });
      let task = r.task;
      if (p.time && finalDue) {  // a parsed time + date becomes a planned time-block
        try { task = (await api.tasks.setExtras(task.id, { scheduled_start: `${finalDue}T${p.time}:00` })).task; }
        catch { /* extras best-effort */ }
      }
      setTitle(""); setDue(""); setPriority(0);
      setTasks((ts) => [task, ...ts]);
    }
    catch { /* ignore */ }
    finally { setBusy(false); }
  };
  const complete = async (id: string) => {
    setTasks((ts) => ts.map((t) => (t.id === id ? { ...t, done: true } : t)));
    try { await api.tasks.complete(id); } finally { load(); }
  };
  const remove = async (id: string) => {
    setTasks((ts) => ts.filter((t) => t.id !== id));
    try { await api.tasks.remove(id); } finally { load(); }
  };
  // Edit a task's due / priority in place (optimistic, with a reload backstop).
  const patch = async (id: string, fields: { due?: string | null; priority?: number }) => {
    setTasks((ts) => ts.map((t) => (t.id === id ? { ...t, ...fields } as Task : t)));
    try { await api.tasks.patch(id, fields); emitRefresh("tasks"); } finally { load(); }
  };
  // Edit the richer sidecar fields (notes / subtasks / recurrence) — optimistic, no reload needed.
  const setExtras = async (id: string, fields: TaskExtras) => {
    setTasks((ts) => ts.map((t) => (t.id === id ? { ...t, ...fields } as Task : t)));
    try { await api.tasks.setExtras(id, fields); } catch { load(); }
  };

  const open = tasks.filter((t) => !t.done);
  const done = tasks.filter((t) => t.done);
  // Smart sort: overdue → priority desc → due asc within the open group; completed sink below.
  const ordered = [...open.sort(compareTasks), ...done];

  return (
    <div className="h-full flex flex-col">
      {!embedded && (
        <div className="shrink-0 h-[60px] px-6 flex items-center justify-between">
          <div className="flex items-baseline gap-2.5 min-w-0">
            <h1 className="font-display text-[19px] font-semibold tracking-[-0.01em] truncate">Tasks</h1>
            <span className="text-[12.5px] text-mac-ink3 tnum shrink-0">
              {open.length} open{done.length ? ` · ${done.length} done` : ""}
            </span>
          </div>
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-auto">
        <div className="mx-auto max-w-[680px] px-6 pb-12">
          {/* add-a-task — natural-language ("lit review fri 3pm !high") + manual due/priority overrides */}
          <div className="flex items-center gap-2 rounded-[10px] bg-mac-fill border border-mac-stroke px-3 h-11 focus-within:border-mac-strokeHi transition-colors">
            <Plus size={15} strokeWidth={2.5} className="text-mac-ink3 shrink-0" />
            <input autoFocus value={title} onChange={(e) => setTitle(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") add(); }}
              placeholder={'Add a task — try "lit review fri 3pm !high"'}
              className="flex-1 min-w-0 bg-transparent text-[13.5px] outline-none placeholder:text-mac-ink3" />
            <input type="date" value={due} onChange={(e) => setDue(e.target.value)}
              title="Due date"
              className="shrink-0 bg-transparent text-[12px] text-mac-ink3 outline-none [color-scheme:dark] w-[118px]" />
            <button type="button" onClick={() => setPriority(nextPriority)}
              title={`Priority: ${PRIORITY_META[priority]?.label ?? "None"}`}
              className={`shrink-0 grid place-items-center h-7 w-7 rounded-[7px] hover:bg-mac-fillHi transition-colors ${PRIORITY_META[priority]?.tone ?? "text-mac-ink4"}`}>
              <Flag size={14} strokeWidth={2} />
            </button>
            {busy && <Loader2 size={14} className="animate-spin text-mac-ink3" />}
          </div>
          {/* live preview of what we parsed from the natural-language input */}
          {title.trim() && (parsed.due || parsed.time || parsed.priority > 0) ? (
            <div className="flex items-center flex-wrap gap-1.5 mt-1.5 mb-3 px-1 text-[11.5px]">
              <span className="text-mac-ink3">→</span>
              <span className="text-mac-ink2 font-medium">{parsed.title}</span>
              {parsed.due && <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-mac-accentDim border border-mac-strokeHi text-mac-accentHi"><Calendar size={10} /> {dueLabel(parsed.due)}</span>}
              {parsed.time && <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-mac-accentDim border border-mac-strokeHi text-mac-accentHi"><Clock size={10} /> {fmtHm12(parsed.time)}</span>}
              {parsed.priority > 0 && <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-mac-fill border border-mac-stroke ${PRIORITY_META[parsed.priority]?.tone}`}><Flag size={10} /> {PRIORITY_META[parsed.priority]?.label}</span>}
            </div>
          ) : <div className="mb-3" />}

          {ordered.length === 0 ? (
            loaded ? (
              <div className="pt-20 grid place-items-center text-center">
                <CheckCircle2 size={34} strokeWidth={1.5} className="text-mac-ink3 mb-3" />
                <p className="text-[14px] text-mac-ink2">Nothing on your list yet.</p>
                <p className="text-[12.5px] text-mac-ink3 mt-1 max-w-[42ch]">
                  Add a task above, or ask Himmy — “add finish my literature review to my tasks.”
                </p>
                <button onClick={() => ask("add ’ ’ to my tasks")}
                  className="mt-4 flex items-center gap-1 text-[13px] text-mac-accentHi hover:underline">
                  Ask Himmy <ArrowUpRight size={14} strokeWidth={2} />
                </button>
              </div>
            ) : null
          ) : (
            <div className="rounded-[12px] border border-mac-stroke overflow-hidden divide-y divide-mac-stroke">
              {ordered.map((t) => (
                <TaskRow key={t.id} task={t}
                  onComplete={() => complete(t.id)} onRemove={() => remove(t.id)}
                  onPatch={(f) => patch(t.id, f)} onSetExtras={(f) => setExtras(t.id, f)} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function TaskRow({ task, onComplete, onRemove, onPatch, onSetExtras }:
  { task: Task; onComplete: () => void; onRemove: () => void;
    onPatch: (fields: { due?: string | null; priority?: number }) => void;
    onSetExtras: (fields: TaskExtras) => void }) {
  const [expanded, setExpanded] = useState(false);
  const overdue = !task.done && isOverdue(task.due);
  const dueRef = useRef<HTMLInputElement | null>(null);
  const dueValue = (() => {
    if (!task.due) return "";
    const d = new Date(task.due);
    return isNaN(d.getTime()) ? "" : d.toISOString().slice(0, 10);
  })();
  const subs: Subtask[] = task.subtasks || [];
  const subDone = subs.filter((s) => s.done).length;
  const setSubs = (next: Subtask[]) => onSetExtras({ subtasks: next });
  const RECURS = [{ id: "", label: "None" }, { id: "daily", label: "Daily" }, { id: "weekly", label: "Weekly" }, { id: "monthly", label: "Monthly" }];

  return (
    <div>
      <div className="group flex items-center gap-3 px-3.5 h-12 hover:bg-mac-fill transition-colors">
        <button onClick={() => { if (!task.done) onComplete(); }} disabled={task.done}
          title={task.done ? "Done" : "Mark done"}
          className="shrink-0 grid place-items-center text-mac-ink3 hover:text-mac-accentHi transition-colors disabled:cursor-default">
          {task.done
            ? <CheckCircle2 size={19} strokeWidth={2} className="text-mac-green" />
            : <Circle size={19} strokeWidth={1.75} className="hover:text-mac-accentHi" />}
        </button>
        {/* title — click to expand the editor; collapsed chips summarise the richer fields */}
        <button onClick={() => setExpanded((e) => !e)} className="flex-1 min-w-0 flex items-center gap-2 text-left">
          <span className={`truncate text-[13.5px] ${task.done ? "line-through text-mac-ink3" : "text-mac-ink"}`}>{task.title}</span>
          {task.recur ? <Repeat size={12} className="shrink-0 text-mac-ink3" /> : null}
          {subs.length > 0 && <span className="shrink-0 inline-flex items-center gap-0.5 text-[11px] text-mac-ink3 tnum"><ListChecks size={11} />{subDone}/{subs.length}</span>}
          {task.scheduled_start && <span className="shrink-0 inline-flex items-center gap-0.5 text-[11px] text-mac-accentHi"><Clock size={10} />{fmtHm12(task.scheduled_start.slice(11, 16))}</span>}
          {task.paper_title && (
            task.paper_id
              ? <span role="button" title={`Open “${task.paper_title}” in Library`}
                  onClick={(e) => { e.stopPropagation(); openPaper(task.paper_id!); }}
                  className="shrink-0 inline-flex items-center gap-0.5 text-[11px] text-mac-accentHi hover:underline max-w-[150px] truncate"><BookText size={10} className="shrink-0" />{task.paper_title}</span>
              : <span title={task.paper_title} className="shrink-0 inline-flex items-center gap-0.5 text-[11px] text-mac-ink3 max-w-[150px] truncate"><BookText size={10} className="shrink-0" />{task.paper_title}</span>
          )}
          {task.notes ? <SquarePen size={11} className="shrink-0 text-mac-ink3" /> : null}
        </button>
        <button type="button" onClick={() => onPatch({ priority: nextPriority(task.priority) })}
          title={`Priority: ${PRIORITY_META[task.priority]?.label ?? "None"}`}
          className={`shrink-0 grid place-items-center h-6 w-6 rounded-[6px] hover:bg-mac-fillHi transition-all ${
            PRIORITY_META[task.priority]?.tone ?? "text-mac-ink4 opacity-0 group-hover:opacity-100"}`}>
          <Flag size={13} strokeWidth={2} />
        </button>
        <button type="button" onClick={() => dueRef.current?.showPicker?.() ?? dueRef.current?.focus()}
          title="Set due date"
          className={`relative shrink-0 inline-flex items-center gap-1 text-[12px] rounded-[6px] px-1.5 h-6 hover:bg-mac-fillHi transition-all ${
            task.due ? (overdue ? "text-mac-red" : "text-mac-ink3") : "text-mac-ink4 opacity-0 group-hover:opacity-100"}`}>
          <Clock size={11} />{task.due ? dueLabel(task.due) : "Due"}
          <input ref={dueRef} type="date" value={dueValue} onChange={(e) => onPatch({ due: e.target.value })}
            className="absolute inset-0 opacity-0 cursor-pointer [color-scheme:dark]" />
        </button>
        <button onClick={() => setExpanded((e) => !e)} title="Details"
          className="shrink-0 grid place-items-center h-7 w-7 rounded-[7px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fillHi transition-all">
          <ChevronRight size={15} className={`transition-transform ${expanded ? "rotate-90" : ""}`} />
        </button>
        <button onClick={onRemove} title="Delete"
          className="shrink-0 grid place-items-center h-7 w-7 rounded-[7px] text-mac-ink3 opacity-0 group-hover:opacity-100 hover:text-mac-red hover:bg-mac-fillHi transition-all">
          <Trash2 size={14} strokeWidth={2} />
        </button>
      </div>

      {expanded && (
        <div className="pl-11 pr-4 pb-4 pt-1 space-y-3.5 bg-[rgba(255,255,255,0.012)]">
          <textarea defaultValue={task.notes || ""} onBlur={(e) => onSetExtras({ notes: e.target.value })}
            placeholder="Notes — saved when you click away" rows={2}
            className="w-full resize-none rounded-lg bg-mac-fill border border-mac-stroke px-2.5 py-2 text-[12.5px] leading-relaxed text-mac-ink2 outline-none focus:border-mac-strokeHi placeholder:text-mac-ink3" />

          <div>
            <div className="text-[10px] uppercase tracking-wide text-mac-ink3 mb-1.5 flex items-center gap-1"><ListChecks size={11} /> Subtasks</div>
            <div className="space-y-1">
              {subs.map((s, i) => (
                <div key={i} className="group/sub flex items-center gap-2">
                  <button onClick={() => setSubs(subs.map((x, j) => (j === i ? { ...x, done: !x.done } : x)))}
                    className="shrink-0 grid place-items-center text-mac-ink3 hover:text-mac-accentHi">
                    {s.done ? <CheckCircle2 size={15} className="text-mac-green" /> : <Circle size={15} />}
                  </button>
                  <span className={`flex-1 text-[12.5px] ${s.done ? "line-through text-mac-ink3" : "text-mac-ink2"}`}>{s.text}</span>
                  <button onClick={() => setSubs(subs.filter((_, j) => j !== i))}
                    className="shrink-0 text-mac-ink4 opacity-0 group-hover/sub:opacity-100 hover:text-mac-red"><X size={12} /></button>
                </div>
              ))}
              <SubtaskInput onAdd={(text) => setSubs([...subs, { text, done: false }])} />
            </div>
          </div>

          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-wide text-mac-ink3 flex items-center gap-1 shrink-0"><Repeat size={11} /> Repeat</span>
            <div className="inline-flex items-center gap-0.5 p-0.5 rounded-[8px] bg-mac-fill border border-mac-stroke">
              {RECURS.map((r) => {
                const active = (task.recur || "") === r.id;
                return (
                  <button key={r.id} onClick={() => onSetExtras({ recur: r.id })}
                    className={`h-6 px-2.5 rounded-[6px] text-[11.5px] transition-colors ${active ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink3 hover:text-mac-ink"}`}>{r.label}</button>
                );
              })}
            </div>
          </div>

          {task.paper_title && (
            <div className="flex items-center gap-2 text-[12px]">
              <span className="text-[10px] uppercase tracking-wide text-mac-ink3 flex items-center gap-1 shrink-0"><BookText size={11} /> Paper</span>
              {task.paper_id ? (
                <button onClick={() => openPaper(task.paper_id!)} title="Open in Library"
                  className="text-mac-accentHi hover:underline truncate max-w-[280px] inline-flex items-center gap-1">
                  {task.paper_title}<ArrowUpRight size={11} className="shrink-0" />
                </button>
              ) : (
                <span className="text-mac-ink2 truncate max-w-[280px]">{task.paper_title}</span>
              )}
              <button onClick={() => onSetExtras({ paper_id: "", paper_title: "" })} title="Unlink paper" className="shrink-0 text-mac-ink4 hover:text-mac-red"><X size={12} /></button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SubtaskInput({ onAdd }: { onAdd: (text: string) => void }) {
  const [v, setV] = useState("");
  return (
    <div className="flex items-center gap-2">
      <Plus size={13} className="shrink-0 text-mac-ink4" />
      <input value={v} onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && v.trim()) { onAdd(v.trim()); setV(""); } }}
        placeholder="Add a subtask…"
        className="flex-1 bg-transparent text-[12.5px] text-mac-ink2 outline-none placeholder:text-mac-ink3" />
    </div>
  );
}

/* ───────────────────────────────────────── routines (saved automations on a schedule) */
// Friendly relative/absolute time helpers for routine + notification rows.
function fmtClock(iso: string | null | undefined): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      weekday: "short", hour: "numeric", minute: "2-digit",
    });
  } catch { return ""; }
}
function fmtAgo(iso: string | null | undefined): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "";
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return "just now";
  const m = Math.round(s / 60); if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60); if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}
function statusLook(s: string | null): { label: string; cls: string } {
  switch (s) {
    case "ok": return { label: "ran ok", cls: "text-mac-green" };
    case "error": return { label: "failed", cls: "text-mac-red" };
    case "timeout": return { label: "timed out", cls: "text-mac-red" };
    case "awaiting_approval": return { label: "needs approval", cls: "text-mac-accentHi" };
    case "running": return { label: "running…", cls: "text-mac-accentHi" };
    default: return { label: "not run yet", cls: "text-mac-ink3" };
  }
}
// A human description of a routine's schedule, recognising the cron patterns we generate.
function humanSched(r: Routine): string {
  const s = r.schedule;
  if (s.kind === "daily") return `Every day at ${s.at}`;
  if (s.kind === "every") return `Every ${s.hours}h`;
  if (s.kind === "at") return `Once · ${fmtClock(s.at_datetime)}`;
  const wk = (s.expr || "").match(/^(\d{1,2}) (\d{1,2}) \* \* 1-5$/);
  if (wk) return `Weekdays at ${wk[2].padStart(2, "0")}:${wk[1].padStart(2, "0")}`;
  const dy = (s.expr || "").match(/^(\d{1,2}) (\d{1,2}) \* \* \*$/);
  if (dy) return `Every day at ${dy[2].padStart(2, "0")}:${dy[1].padStart(2, "0")}`;
  return `Cron · ${s.expr}`;
}

// ── schedule builder (UI mode ⇆ himmy Schedule payload) ──────────────────────
type SchedMode = "daily" | "weekdays" | "every" | "cron";
function deriveMode(s: RoutineSchedule): { mode: SchedMode; time: string; hours: number; cron: string } {
  if (s.kind === "every") return { mode: "every", time: "06:30", hours: s.hours || 6, cron: "30 6 * * 1-5" };
  if (s.kind === "daily") return { mode: "daily", time: s.at || "07:00", hours: 6, cron: "30 6 * * 1-5" };
  if (s.kind === "cron") {
    const wk = (s.expr || "").match(/^(\d{1,2}) (\d{1,2}) \* \* 1-5$/);
    if (wk) return { mode: "weekdays", time: `${wk[2].padStart(2, "0")}:${wk[1].padStart(2, "0")}`, hours: 6, cron: s.expr || "" };
    return { mode: "cron", time: "06:30", hours: 6, cron: s.expr || "30 6 * * *" };
  }
  return { mode: "daily", time: "07:00", hours: 6, cron: "30 6 * * 1-5" };
}
function buildSchedule(mode: SchedMode, time: string, hours: number, cron: string): RoutineSchedule {
  const [hh, mm] = (time || "07:00").split(":");
  if (mode === "weekdays") return { kind: "cron", expr: `${parseInt(mm)} ${parseInt(hh)} * * 1-5`, missed: "coalesce" };
  if (mode === "every") return { kind: "every", hours };
  if (mode === "cron") return { kind: "cron", expr: cron.trim(), missed: "coalesce" };
  return { kind: "daily", at: time, missed: "coalesce" };
}

function ScheduleBuilder({ value, onChange }: { value: RoutineSchedule; onChange: (s: RoutineSchedule) => void }) {
  const init = useMemo(() => deriveMode(value), []); // initial only; the form remounts per open
  const [mode, setMode] = useState<SchedMode>(init.mode);
  const [time, setTime] = useState(init.time);
  const [hours, setHours] = useState(init.hours);
  const [cron, setCron] = useState(init.cron);
  useEffect(() => { onChange(buildSchedule(mode, time, hours, cron)); }, [mode, time, hours, cron]); // eslint-disable-line
  const Opt = ({ id, label }: { id: SchedMode; label: string }) => (
    <button type="button" onClick={() => setMode(id)}
      className={`h-8 px-3 rounded-[8px] text-[12.5px] transition-colors ${
        mode === id ? "bg-mac-accent text-white" : "bg-mac-fill border border-mac-stroke text-mac-ink2 hover:text-mac-ink"}`}>
      {label}
    </button>
  );
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1.5">
        <Opt id="daily" label="Every day" />
        <Opt id="weekdays" label="Weekdays" />
        <Opt id="every" label="Every few hours" />
        <Opt id="cron" label="Advanced" />
      </div>
      {(mode === "daily" || mode === "weekdays") && (
        <div className="flex items-center gap-2 text-[12.5px] text-mac-ink2">
          <span>at</span>
          <input type="time" value={time} onChange={(e) => setTime(e.target.value)}
            className="h-9 px-2.5 rounded-[8px] bg-mac-fill border border-mac-stroke text-[13px] text-mac-ink outline-none focus:border-mac-strokeHi" />
          <span className="text-mac-ink3">{mode === "weekdays" ? "Mon–Fri" : "daily"} · Nepal time</span>
        </div>
      )}
      {mode === "every" && (
        <div className="flex items-center gap-2 text-[12.5px] text-mac-ink2">
          <span>every</span>
          <input type="number" min={1} max={168} value={hours}
            onChange={(e) => setHours(Math.max(1, Math.min(168, parseInt(e.target.value) || 1)))}
            className="h-9 w-[72px] px-2.5 rounded-[8px] bg-mac-fill border border-mac-stroke text-[13px] text-mac-ink outline-none focus:border-mac-strokeHi" />
          <span>hours</span>
        </div>
      )}
      {mode === "cron" && (
        <div className="space-y-1.5">
          <input value={cron} onChange={(e) => setCron(e.target.value)} placeholder="30 6 * * 1-5"
            className="w-full h-9 px-3 rounded-[8px] bg-mac-fill border border-mac-stroke text-[13px] font-mono text-mac-ink outline-none focus:border-mac-strokeHi" />
          <p className="text-[11px] text-mac-ink3">
            Cron: minute hour day month weekday — e.g. <span className="font-mono">30 6 * * 1-5</span> = 6:30am on weekdays.
          </p>
        </div>
      )}
    </div>
  );
}

function Switch({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} role="switch" aria-checked={on} title={on ? "On" : "Off"}
      className={`relative shrink-0 h-[22px] w-[38px] rounded-full transition-colors ${
        on ? "bg-mac-accent" : "bg-mac-fillHi border border-mac-stroke"}`}>
      <span className={`absolute top-[2px] h-[18px] w-[18px] rounded-full bg-white shadow transition-all ${
        on ? "left-[18px]" : "left-[2px]"}`} />
    </button>
  );
}

function Routines() {
  const [routines, setRoutines] = useState<Routine[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [sched, setSched] = useState<RoutineSchedule>({ kind: "daily", at: "07:00" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<{ id: string; preview: string; status: string } | null>(null);

  const load = async () => {
    try { const r = await api.routines.list(); setRoutines(r.routines); }
    catch { /* backend warming up */ } finally { setLoaded(true); }
  };
  useEffect(() => { load(); const t = setInterval(load, 8000); return () => clearInterval(t); }, []);

  const reset = () => { setEditingId(null); setName(""); setPrompt(""); setSched({ kind: "daily", at: "07:00" }); setErr(null); };
  const openNew = () => { reset(); setShowForm(true); };
  const openEdit = (r: Routine) => { setEditingId(r.id); setName(r.name); setPrompt(r.prompt); setSched(r.schedule); setErr(null); setShowForm(true); };

  const save = async () => {
    if (!name.trim() || !prompt.trim() || busy) return;
    setBusy(true); setErr(null);
    try {
      if (editingId) await api.routines.update(editingId, { name, prompt, schedule: sched });
      else await api.routines.create(name, prompt, sched, true);
      setShowForm(false); reset(); load();
    } catch (e: any) { setErr(e?.message || "Couldn't save this routine."); }
    finally { setBusy(false); }
  };
  const toggle = async (r: Routine) => {
    setRoutines((rs) => rs.map((x) => (x.id === r.id ? { ...x, enabled: !x.enabled } : x)));
    try { await api.routines.update(r.id, { enabled: !r.enabled }); } finally { load(); }
  };
  const runNow = async (r: Routine) => {
    setRunningId(r.id); setLastRun(null);
    try {
      const res = await api.routines.runNow(r.id);
      setLastRun({ id: r.id, preview: res.preview || res.error || "(done)", status: res.status || (res.ok ? "ok" : "error") });
    } catch (e: any) { setLastRun({ id: r.id, preview: e?.message || "Run failed.", status: "error" }); }
    finally { setRunningId(null); load(); }
  };
  const remove = async (r: Routine) => {
    setRoutines((rs) => rs.filter((x) => x.id !== r.id));
    try { await api.routines.remove(r.id); } finally { load(); }
  };

  const active = routines.filter((r) => r.enabled).length;

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 h-[60px] px-6 flex items-center justify-between">
        <div className="flex items-baseline gap-2.5 min-w-0">
          <h1 className="font-display text-[19px] font-semibold tracking-[-0.01em] truncate">Routines</h1>
          <span className="text-[12.5px] text-mac-ink3 shrink-0">
            {routines.length ? `${active} active · ${routines.length} total` : "automations"}
          </span>
        </div>
        <button onClick={openNew}
          className="h-8 px-3.5 rounded-[9px] bg-mac-accent text-white text-[12.5px] font-medium hover:bg-mac-accentHi transition-colors flex items-center gap-1.5">
          <Plus size={14} strokeWidth={2.5} /> New routine
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-auto">
        <div className="mx-auto max-w-[680px] px-6 pb-12">
          <p className="text-[12.5px] text-mac-ink3 leading-relaxed mb-4">
            Saved automations that run on a schedule — Himmy does them for you and drops the result in
            your notifications (the bell, top-right). Results run with all of Himmy’s tools; anything that
            would change your calendar or send mail pauses for your approval.
          </p>
          {routines.length === 0 ? (
            loaded ? <EmptyRoutines onNew={openNew} /> : null
          ) : (
            <div className="space-y-2.5">
              {routines.map((r) => (
                <RoutineCard key={r.id} r={r} running={runningId === r.id}
                  lastRun={lastRun && lastRun.id === r.id ? lastRun : null}
                  onToggle={() => toggle(r)} onRun={() => runNow(r)}
                  onEdit={() => openEdit(r)} onDelete={() => remove(r)} />
              ))}
            </div>
          )}
        </div>
      </div>

      {showForm && (
        <RoutineForm editing={!!editingId} name={name} setName={setName} prompt={prompt} setPrompt={setPrompt}
          sched={sched} setSched={setSched} busy={busy} err={err}
          onSave={save} onClose={() => { setShowForm(false); reset(); }} />
      )}
    </div>
  );
}

function EmptyRoutines({ onNew }: { onNew: () => void }) {
  return (
    <div className="pt-16 grid place-items-center text-center">
      <Repeat size={34} strokeWidth={1.5} className="text-mac-ink3 mb-3" />
      <p className="text-[14px] text-mac-ink2">No routines yet.</p>
      <p className="text-[12.5px] text-mac-ink3 mt-1 max-w-[46ch]">
        Set up an automation and Himmy will run it on schedule — like a weekday morning briefing,
        or an evening summary of the news you saved.
      </p>
      <button onClick={onNew}
        className="mt-4 h-8 px-3.5 rounded-[9px] bg-mac-accent text-white text-[12.5px] font-medium hover:bg-mac-accentHi transition-colors flex items-center gap-1.5">
        <Plus size={14} strokeWidth={2.5} /> New routine
      </button>
    </div>
  );
}

function RoutineCard({ r, running, lastRun, onToggle, onRun, onEdit, onDelete }:
  { r: Routine; running: boolean; lastRun: { preview: string; status: string } | null;
    onToggle: () => void; onRun: () => void; onEdit: () => void; onDelete: () => void }) {
  const look = statusLook(running ? "running" : r.last_status);
  const iconBtn = "shrink-0 grid place-items-center h-8 w-8 rounded-[8px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fillHi transition-colors";
  return (
    <div className="rounded-[12px] border border-mac-stroke bg-mac-fill overflow-hidden">
      <div className="flex items-center gap-3 px-3.5 h-[60px]">
        <div className={`shrink-0 h-9 w-9 grid place-items-center rounded-[10px] ${
          r.enabled ? "bg-mac-accentDim text-mac-accentHi" : "bg-mac-fillHi text-mac-ink3"}`}>
          <Repeat size={16} strokeWidth={2} />
        </div>
        <div className="min-w-0 flex-1">
          <div className={`text-[13.5px] truncate ${r.enabled ? "text-mac-ink" : "text-mac-ink2"}`}>{r.name}</div>
          <div className="text-[11.5px] text-mac-ink3 flex items-center gap-1.5 mt-0.5 min-w-0">
            <Clock size={11} strokeWidth={2} className="shrink-0" />
            <span className="truncate">{humanSched(r)}</span>
            <span className="text-mac-ink4">·</span>
            <span className={`${look.cls} shrink-0`}>{look.label}</span>
            {r.last_run_at && <span className="text-mac-ink3 shrink-0 hidden sm:inline">{fmtAgo(r.last_run_at)}</span>}
            {r.enabled && r.next_fire_at && (
              <><span className="text-mac-ink4">·</span><span className="shrink-0">next {fmtClock(r.next_fire_at)}</span></>
            )}
          </div>
        </div>
        <button onClick={onRun} disabled={running} title="Run now" className={iconBtn}>
          {running ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} strokeWidth={2} />}
        </button>
        <button onClick={onEdit} title="Edit" className={iconBtn}><SquarePen size={14} strokeWidth={2} /></button>
        <button onClick={onDelete} title="Delete"
          className="shrink-0 grid place-items-center h-8 w-8 rounded-[8px] text-mac-ink3 hover:text-mac-red hover:bg-mac-fillHi transition-colors">
          <Trash2 size={14} strokeWidth={2} />
        </button>
        <Switch on={r.enabled} onClick={onToggle} />
      </div>
      {lastRun && (
        <div className={`px-4 py-2.5 border-t border-mac-stroke text-[12px] whitespace-pre-wrap leading-relaxed ${
          lastRun.status === "ok" ? "text-mac-ink2" : "text-mac-red"}`}>
          {lastRun.preview}
        </div>
      )}
      {!lastRun && r.last_error && r.last_status !== "ok" && (
        <div className="px-4 py-2 border-t border-mac-stroke text-[11.5px] text-mac-red">{r.last_error}</div>
      )}
    </div>
  );
}

function RoutineForm({ editing, name, setName, prompt, setPrompt, sched, setSched, busy, err, onSave, onClose }:
  { editing: boolean; name: string; setName: (v: string) => void; prompt: string; setPrompt: (v: string) => void;
    sched: RoutineSchedule; setSched: (s: RoutineSchedule) => void; busy: boolean; err: string | null;
    onSave: () => void; onClose: () => void }) {
  return (
    <div className="absolute inset-0 z-50 grid place-items-center bg-black/45" onMouseDown={onClose}>
      <div onMouseDown={(e) => e.stopPropagation()}
        className="w-[600px] max-w-[calc(100%-3rem)] rounded-2xl bg-[rgba(30,31,37,0.97)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden">
        <div className="h-12 px-4 flex items-center justify-between border-b border-mac-stroke">
          <div className="flex items-center gap-2 text-[13px]">
            <Repeat size={14} className="text-mac-accentHi" />
            <span className="font-medium text-mac-ink">{editing ? "Edit routine" : "New routine"}</span>
          </div>
          <button onClick={onClose} className="text-mac-ink3 hover:text-mac-ink"><X size={16} /></button>
        </div>
        <div className="p-5 space-y-4">
          <div>
            <label className="text-[12px] text-mac-ink2 block mb-1.5">Name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Daily Briefing" autoFocus
              className="w-full h-10 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[13px] text-mac-ink outline-none focus:border-mac-strokeHi placeholder:text-mac-ink3" />
          </div>
          <div>
            <label className="text-[12px] text-mac-ink2 block mb-1.5">What should Himmy do?</label>
            <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={4}
              placeholder="e.g. Summarise the news I saved today and list any overdue tasks."
              className="w-full px-3 py-2.5 rounded-[9px] bg-mac-fill border border-mac-stroke text-[13px] text-mac-ink outline-none focus:border-mac-strokeHi placeholder:text-mac-ink3 resize-none leading-relaxed" />
          </div>
          <div>
            <label className="text-[12px] text-mac-ink2 block mb-1.5">When</label>
            <ScheduleBuilder value={sched} onChange={setSched} />
          </div>
          {err && <p className="text-[12px] text-mac-red bg-mac-fill border border-mac-stroke rounded-md px-3 py-2 break-all">{err}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <button onClick={onClose}
              className="h-9 px-4 rounded-[9px] bg-mac-fill border border-mac-stroke text-[13px] text-mac-ink2 hover:text-mac-ink transition-colors">Cancel</button>
            <button onClick={onSave} disabled={busy || !name.trim() || !prompt.trim()}
              className="h-9 px-4 rounded-[9px] bg-mac-accent text-white text-[13px] font-medium hover:bg-mac-accentHi transition-colors disabled:opacity-50 flex items-center gap-1.5">
              {busy && <Loader2 size={13} className="animate-spin" />} {editing ? "Save changes" : "Create routine"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── notifications inbox (routine results · errors · approval parks) ───────── */
function NotificationsPanel({ notifs, obs, onObsRefresh, onRefresh, onClose, onRoutines }:
  { notifs: NotificationItem[]; obs: Observation[]; onObsRefresh: () => void;
    onRefresh: () => void; onClose: () => void; onRoutines: () => void }) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const markRead = async (id: string) => { try { await api.notifications.read(id); } finally { onRefresh(); } };
  const remove = async (id: string) => { try { await api.notifications.remove(id); } finally { onRefresh(); } };
  const readAll = async () => { try { await api.notifications.readAll(); } finally { onRefresh(); } };
  const decide = async (n: NotificationItem, approved: boolean) => {
    if (!n.checkpoint_id) return;
    setBusyId(n.id);
    try { await api.resume(n.checkpoint_id, approved); await api.notifications.read(n.id); }
    catch { /* the run may have already resolved */ }
    finally { setBusyId(null); onRefresh(); }
  };
  return (
    <div className="absolute inset-0 z-50 bg-black/20" onMouseDown={onClose}>
      <div onMouseDown={(e) => e.stopPropagation()}
        className="absolute right-3 top-[54px] w-[420px] max-w-[calc(100%-1.5rem)] max-h-[calc(100%-78px)] rounded-2xl bg-[rgba(30,31,37,0.97)] backdrop-blur-xl border border-mac-strokeHi shadow-pop overflow-hidden flex flex-col">
        <div className="h-12 px-4 flex items-center justify-between border-b border-mac-stroke shrink-0">
          <div className="flex items-center gap-2 text-[13px]">
            <Bell size={14} className="text-mac-accentHi" />
            <span className="font-medium text-mac-ink">Notifications</span>
          </div>
          <div className="flex items-center gap-1">
            <button onClick={onRoutines} title="Manage routines"
              className="flex items-center gap-1.5 text-[11.5px] text-mac-ink2 hover:text-mac-ink px-2 h-7 rounded-[7px] hover:bg-mac-fill transition-colors">
              <Repeat size={13} /> Routines
            </button>
            {notifs.some((n) => !n.read) && (
              <button onClick={readAll}
                className="text-[11.5px] text-mac-ink3 hover:text-mac-ink px-2 h-7 rounded-[7px] hover:bg-mac-fill transition-colors">Mark all read</button>
            )}
            <button onClick={onClose} className="text-mac-ink3 hover:text-mac-ink"><X size={16} /></button>
          </div>
        </div>
        <div className="flex-1 min-h-0 overflow-auto">
          {/* "Himmy noticed" — the proactive brain's actionable observations, at the top. */}
          <HimmyNoticedList obs={obs} onRefresh={onObsRefresh} />
          {notifs.length === 0 && obs.length === 0 ? (
            <div className="p-10 text-center">
              <Inbox size={30} strokeWidth={1.5} className="text-mac-ink3 mb-2.5 mx-auto" />
              <p className="text-[12.5px] text-mac-ink3 max-w-[34ch] mx-auto">
                Nothing yet. Himmy will flag things that need you, and your scheduled routines, here.
              </p>
            </div>
          ) : (
            <div className="divide-y divide-mac-stroke">
              {notifs.map((n) => (
                <NotifRow key={n.id} n={n} busy={busyId === n.id}
                  onRead={() => markRead(n.id)} onRemove={() => remove(n.id)} onDecide={(a) => decide(n, a)} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function NotifRow({ n, busy, onRead, onRemove, onDecide }:
  { n: NotificationItem; busy: boolean; onRead: () => void; onRemove: () => void; onDecide: (approved: boolean) => void }) {
  const [open, setOpen] = useState(false);
  const Ico = n.kind === "approval" ? ShieldCheck : n.kind === "error" ? X : CheckCircle2;
  const tint = n.kind === "error" ? "text-mac-red" : n.kind === "approval" ? "text-mac-accentHi" : "text-mac-green";
  return (
    <div className={`px-4 py-3 ${n.read ? "" : "bg-mac-fill"}`}>
      <div className="flex items-start gap-2.5">
        {!n.read
          ? <span className="mt-[7px] h-1.5 w-1.5 rounded-full bg-mac-accentHi shrink-0" />
          : <span className="mt-[7px] h-1.5 w-1.5 shrink-0" />}
        <Ico size={15} strokeWidth={2} className={`mt-0.5 shrink-0 ${tint}`} />
        <div className="min-w-0 flex-1 cursor-pointer" onClick={() => { setOpen((o) => !o); if (!n.read) onRead(); }}>
          <div className="text-[13px] text-mac-ink truncate">{n.title}</div>
          <div className="text-[11px] text-mac-ink3 mt-0.5 truncate">{n.routine_name} · {fmtAgo(n.created_at)}</div>
          {n.body && (
            open ? (
              <div className="mt-1.5 text-[12px] text-mac-ink2">
                <ChatMarkdown text={n.body} />
              </div>
            ) : (
              <div className="text-[12px] text-mac-ink2 mt-1 leading-relaxed line-clamp-2">
                {n.body.replace(/\*\*/g, "").replace(/^\s*[-*]\s+/gm, "")}
              </div>
            )
          )}
        </div>
        <button onClick={onRemove} title="Dismiss"
          className="shrink-0 grid place-items-center h-6 w-6 rounded-[6px] text-mac-ink3 hover:text-mac-red hover:bg-mac-fillHi transition-colors">
          <Trash2 size={13} strokeWidth={2} />
        </button>
      </div>
      {n.kind === "approval" && n.checkpoint_id && (
        <div className="flex gap-2 mt-2.5 pl-[26px]">
          <button onClick={() => onDecide(true)} disabled={busy}
            className="h-7 px-3 rounded-[7px] bg-mac-accent text-white text-[12px] font-medium hover:bg-mac-accentHi transition-colors disabled:opacity-60 flex items-center gap-1">
            {busy ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} strokeWidth={2.5} />} Approve
          </button>
          <button onClick={() => onDecide(false)} disabled={busy}
            className="h-7 px-3 rounded-[7px] bg-mac-fill border border-mac-stroke text-[12px] text-mac-ink2 hover:text-mac-ink transition-colors disabled:opacity-60">
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

/* ───────────────────────────────────────── news (live feeds · in-app reader · saved → RAG) */
const NEWS_FEEDS = ["For You", "Nepal", "World", "Business", "Technology"];
const READER_SERIF = '"Iowan Old Style", Charter, Georgia, "Times New Roman", serif';

// Per-category icon (rail + headers). Falls back to a generic newspaper.
function newsCatIcon(cat: string): LucideIcon {
  switch (cat) {
    case "For You": return Sparkles;
    case "Nepal": return MapPin;
    case "World": return Globe;
    case "Business": return TrendingUp;
    case "Technology": return Cpu;
    default: return Newspaper;
  }
}
// "updated 2m ago" — a compact relative label from an ISO timestamp.
function relativeAgo(iso?: string): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const secs = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (secs < 45) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return `${days}d ago`;
}
type NewsView = { kind: "feed"; cat: string } | { kind: "saved"; folder: string | null };
type ReadTarget = {
  article: { url: string; title: string; source?: string; image?: string; snippet?: string };
  savedId?: string;
};

function News() {
  const [view, setView] = useState<NewsView>({ kind: "feed", cat: "Nepal" });
  const [items, setItems] = useState<NewsArticle[]>([]);
  const [saved, setSaved] = useState<SavedArticle[]>([]);
  const [folders, setFolders] = useState<NewsFolder[]>([]);
  const [savedTotal, setSavedTotal] = useState(0);
  const [savedMap, setSavedMap] = useState<Record<string, string>>({}); // url → savedId | "pending"
  const [loading, setLoading] = useState(false);
  const [fetchedAt, setFetchedAt] = useState<string | undefined>();
  const [needsInterests, setNeedsInterests] = useState(false);
  const needsInterestsRef = useRef(needsInterests);
  useEffect(() => { needsInterestsRef.current = needsInterests; }, [needsInterests]);
  const [interests, setInterests] = useState<string[]>([]);
  const [interestInput, setInterestInput] = useState("");
  const [query, setQuery] = useState("");
  const [reading, setReading] = useState<ReadTarget | null>(null);
  // Drives the live "updated Xm ago" label without refetching.
  const [, setNowTick] = useState(0);
  // Per-category timestamp of when the user last looked at it — anything newer gets a "New" dot.
  const [lastSeen, setLastSeen] = useState<Record<string, number>>({});
  // Developing stories — events several articles/sources are tracking, shown as a rail atop a feed.
  const [developing, setDeveloping] = useState<DevelopingStory[]>([]);
  const loadDeveloping = async () => {
    try { const r = await api.news.developing(); setDeveloping(r.stories || []); } catch { /* warming */ }
  };

  const isFeed = view.kind === "feed";

  // Tell Himmy which article is open while reading it (cleared on close/unmount).
  useEffect(() => {
    if (reading) {
      const a = reading.article;
      setOpenItem({ kind: "article", title: a.title, source: a.source, url: a.url, text: a.snippet });
    } else if (openItemRef.current?.kind === "article") {
      setOpenItem(null);
    }
  }, [reading]);
  useEffect(() => () => {
    if (openItemRef.current?.kind === "article") setOpenItem(null);
  }, []);

  const loadFolders = async () => {
    try {
      const [f, u] = await Promise.all([api.news.savedFolders(), api.news.savedUrls()]);
      setFolders(f.folders); setSavedTotal(f.total);
      const map: Record<string, string> = {};
      u.urls.forEach((x) => { map[x.url] = x.id; });
      setSavedMap(map);
    } catch { /* warming up */ }
  };
  const loadFeed = async (cat: string, force = false) => {
    setLoading(true); setNeedsInterests(false); setFetchedAt(undefined);
    try {
      const r = await api.news.feed(cat, force);
      setItems(r.items || []); setFetchedAt(r.fetched_at || undefined); setNeedsInterests(!!r.needs_interests);
    } catch { setItems([]); } finally { setLoading(false); }
  };
  const loadSaved = async (folder: string | null) => {
    setLoading(true);
    try { const r = await api.news.saved(folder || ""); setSaved(r.items || []); }
    catch { setSaved([]); } finally { setLoading(false); }
  };
  useEffect(() => {
    loadFolders();
    loadDeveloping();
    api.news.interests().then((r) => setInterests(r.interests || [])).catch(() => {});
  }, []);
  useEffect(() => {
    setQuery("");
    if (view.kind === "feed") loadFeed(view.cat);
    else loadSaved(view.folder);
    /* eslint-disable-next-line */
  }, [view]);
  // REAL-TIME: while a feed is open, silently re-fetch on an interval (the backend serves from its
  // ~15-min cache, so this is cheap) and tick a clock so "updated Xm ago" keeps advancing.
  useEffect(() => {
    if (view.kind !== "feed" || reading) return;
    const cat = view.cat;
    const clock = setInterval(() => setNowTick((n) => n + 1), 30_000);
    // Skip the silent refresh while the empty "Build your For You feed" prompt is showing — there's
    // nothing to refresh and it would queue no-op backend round-trips.
    const poll = setInterval(() => { if (!needsInterestsRef.current) loadFeed(cat); }, 150_000);
    return () => { clearInterval(clock); clearInterval(poll); };
    /* eslint-disable-next-line */
  }, [view, reading]);
  // Mark the current category "seen" shortly after it loads, so the "New" dots clear once read.
  useEffect(() => {
    if (view.kind !== "feed" || loading) return;
    const cat = view.cat;
    const t = setTimeout(() => setLastSeen((m) => ({ ...m, [cat]: Date.now() / 1000 })), 4_000);
    return () => clearTimeout(t);
    /* eslint-disable-next-line */
  }, [view, loading]);
  // Himmy saved an article → refresh the saved indicators (and the saved list, if it's open).
  useRefreshSignal("news", () => {
    loadFolders();
    if (view.kind !== "feed") loadSaved(view.folder);
  });

  const saveInterests = async (list: string[]) => {
    setInterests(list);
    try { await api.news.setInterests(list); } catch { /* */ }
    if (view.kind === "feed" && view.cat === "For You") loadFeed("For You", true);
  };

  const doSave = async (a: ReadTarget["article"], folder = "Reading List") => {
    setSavedMap((m) => ({ ...m, [a.url]: "pending" }));
    try {
      const r = await api.news.save({ url: a.url, title: a.title, source: a.source, image: a.image, snippet: a.snippet, folder });
      if (r.ok && r.id) setSavedMap((m) => ({ ...m, [a.url]: r.id! }));
      else setSavedMap((m) => { const n = { ...m }; delete n[a.url]; return n; });
      await loadFolders();
    } catch { setSavedMap((m) => { const n = { ...m }; delete n[a.url]; return n; }); }
  };
  const doUnsave = async (url: string) => {
    const id = savedMap[url];
    if (!id || id === "pending") return;
    setSavedMap((m) => { const n = { ...m }; delete n[url]; return n; });
    try { await api.news.unsave(id); } finally {
      await loadFolders();
      if (view.kind === "saved") loadSaved(view.folder);
    }
  };

  const refresh = () => {
    if (view.kind === "feed") { loadFeed(view.cat, true); loadDeveloping(); }
    else loadSaved(view.folder);
  };

  const q = query.trim().toLowerCase();
  const match = (a: { title: string; source: string; snippet: string }) =>
    `${a.title} ${a.source} ${a.snippet}`.toLowerCase().includes(q);
  const liveShown = q ? items.filter(match) : items;
  const savedShown = q ? saved.filter((a) => match({ title: a.title, source: a.source, snippet: a.snippet })) : saved;

  if (reading) {
    return (
      <NewsReader target={reading} onClose={() => setReading(null)}
        isSaved={!!savedMap[reading.article.url] && savedMap[reading.article.url] !== "pending"}
        folders={folders}
        onSave={(folder) => doSave(reading.article, folder)}
        onUnsave={() => doUnsave(reading.article.url)} />
    );
  }

  const heading = isFeed ? view.cat : (view.folder || "Saved");
  const count = isFeed ? liveShown.length : savedShown.length;

  return (
    <div className="h-full flex">
      <NewsRail feeds={NEWS_FEEDS} folders={folders} savedTotal={savedTotal} view={view} onPick={setView} />

      <div className="flex-1 flex flex-col min-w-0">
        <div className="shrink-0 h-[60px] px-7 flex items-center justify-between">
          <div className="flex items-baseline gap-2.5 min-w-0">
            <h1 className="font-display text-[19px] font-semibold tracking-[-0.01em] truncate">{heading}</h1>
            {isFeed
              ? fetchedAt && (
                  <span className="inline-flex items-center gap-1.5 text-[12px] text-mac-ink3 shrink-0">
                    <Clock size={10} className="text-mac-ink3" />
                    {" "}updated {relativeAgo(fetchedAt)}
                  </span>
                )
              : <span className="text-[12.5px] text-mac-ink3 tnum shrink-0">{count} {count === 1 ? "article" : "articles"}</span>}
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-2 h-8 rounded-[9px] bg-mac-fill border border-mac-stroke px-2.5 w-56 focus-within:border-mac-accent transition-colors">
              <Search size={13} strokeWidth={2} className="text-mac-ink3" />
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={isFeed ? "Search the news" : "Search saved"}
                className="flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-mac-ink3" />
              {query && <button onClick={() => setQuery("")} className="text-mac-ink3 hover:text-mac-ink"><X size={12} /></button>}
            </div>
            {isFeed && (
              <button onClick={refresh} disabled={loading}
                className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors flex items-center gap-1.5 disabled:opacity-50">
                {loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} Refresh
              </button>
            )}
          </div>
        </div>

        {isFeed && view.cat === "For You" && (
          <div className="shrink-0 px-7 pb-2.5 flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] uppercase tracking-wide text-mac-ink3 mr-1">Your topics</span>
            {interests.map((t) => (
              <span key={t} className="inline-flex items-center gap-1 text-[12px] text-mac-ink2 bg-mac-fill border border-mac-stroke rounded-full pl-2.5 pr-1.5 py-0.5">
                {t}<button onClick={() => saveInterests(interests.filter((x) => x !== t))} className="text-mac-ink3 hover:text-mac-red"><X size={11} /></button>
              </span>
            ))}
            <input value={interestInput} onChange={(e) => setInterestInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && interestInput.trim()) { saveInterests([...interests, interestInput.trim()]); setInterestInput(""); } }}
              placeholder="add a topic…" className="text-[12px] bg-transparent outline-none w-28 text-mac-ink placeholder:text-mac-ink3" />
          </div>
        )}

        <div className="flex-1 min-h-0 overflow-auto px-7 pb-9">
          {isFeed && !q && !needsInterests && developing.length > 0 && (
            <DevelopingRail stories={developing}
              onOpen={(a) => setReading({ article: a })} />
          )}
          {loading && (isFeed ? items.length === 0 : saved.length === 0) ? (
            <div className="h-48 grid place-items-center text-mac-ink3"><Loader2 size={18} className="animate-spin" /></div>
          ) : isFeed ? (
            needsInterests ? (
              <div className="h-56 grid place-items-center text-center px-8">
                <div className="max-w-sm">
                  <div className="mx-auto mb-3 h-11 w-11 grid place-items-center rounded-full bg-mac-accent/12 text-mac-accentHi">
                    <Sparkles size={18} strokeWidth={2} />
                  </div>
                  <div className="text-[14px] font-medium text-mac-ink">Build your For You feed</div>
                  <p className="mt-1.5 text-[12.5px] text-mac-ink2 leading-snug">
                    Add a few topics above, or just start reading papers and articles — Himmy learns your taste and curates from {NEWS_FEEDS.length - 1} sections.
                  </p>
                </div>
              </div>
            ) : liveShown.length === 0 ? (
              <div className="h-48 grid place-items-center text-[13px] text-mac-ink3">{query ? `No stories match “${query}”.` : "No stories right now — try Refresh."}</div>
            ) : (
              <div className="grid grid-cols-2 xl:grid-cols-3 gap-4 auto-rows-fr">
                {liveShown.map((a, i) => {
                  const st = savedMap[a.url];
                  const flag = st === "pending" ? "pending" : st ? true : false;
                  const seenTs = lastSeen[view.cat];
                  const isNew = !!seenTs && !!a.ts && a.ts > seenTs;
                  return (
                    <NewsCard key={a.url + i} a={a} saved={flag} isForYou={view.cat === "For You"} isNew={isNew}
                      onOpen={() => setReading({ article: { url: a.url, title: a.title, source: a.source, image: a.image, snippet: a.snippet } })}
                      onToggleSave={() => (flag ? doUnsave(a.url) : doSave({ url: a.url, title: a.title, source: a.source, image: a.image, snippet: a.snippet }))} />
                  );
                })}
              </div>
            )
          ) : savedShown.length === 0 ? (
            <SavedEmpty query={query} onBrowse={() => setView({ kind: "feed", cat: "Nepal" })} />
          ) : (
            <div className="grid grid-cols-2 xl:grid-cols-3 gap-4 auto-rows-fr">
              {savedShown.map((a) => (
                <SavedCard key={a.id} a={a}
                  onOpen={() => setReading({ article: { url: a.url, title: a.title, source: a.source, image: a.image, snippet: a.snippet }, savedId: a.id })}
                  onRemove={() => doUnsave(a.url)} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// "Developing now" — a horizontal rail of clustered, actively-covered stories above the feed.
function DevelopingRail({ stories, onOpen }: {
  stories: DevelopingStory[];
  onOpen: (a: { url: string; title: string; source: string; image: string; snippet: string }) => void;
}) {
  return (
    <div className="mb-5">
      <div className="flex items-center gap-1.5 mb-2.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-mac-accentHi">
        <TrendingUp size={12} strokeWidth={2.4} /> Developing now
      </div>
      <div className="flex gap-3 overflow-x-auto pb-1 -mx-1 px-1">
        {stories.map((s, i) => (
          <button key={i}
            onClick={() => onOpen({ url: s.articles[0]?.url || "", title: s.title,
                                    source: s.sources[0] || "", image: s.image, snippet: "" })}
            className="group shrink-0 w-[256px] text-left rounded-2xl border border-white/[0.07] bg-gradient-to-b from-white/[0.055] to-white/[0.018] shadow-card overflow-hidden hover:shadow-cardHover transition-shadow">
            {s.image ? (
              <div className="relative h-[104px] w-full overflow-hidden">
                <img src={s.image} alt="" className="h-full w-full object-cover group-hover:scale-[1.03] transition-transform duration-300" />
                <span className="absolute top-2 left-2 text-[9.5px] font-semibold uppercase tracking-[0.06em] text-white bg-mac-red/90 rounded px-1.5 py-0.5 shadow">Developing</span>
              </div>
            ) : (
              <div className="relative h-[104px] w-full bg-mac-fill grid place-items-center">
                <Newspaper size={20} className="text-mac-ink4" />
                <span className="absolute top-2 left-2 text-[9.5px] font-semibold uppercase tracking-[0.06em] text-white bg-mac-red/90 rounded px-1.5 py-0.5">Developing</span>
              </div>
            )}
            <div className="p-3">
              <div className="text-[13px] font-medium text-mac-ink leading-snug line-clamp-2">{s.title}</div>
              <div className="mt-2 flex items-center gap-1.5 text-[11px] text-mac-ink3">
                <Newspaper size={11} strokeWidth={2} />
                {s.count} articles · {s.sources.length} {s.sources.length === 1 ? "source" : "sources"}
              </div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

function NewsRail({ feeds, folders, savedTotal, view, onPick }: {
  feeds: string[]; folders: NewsFolder[]; savedTotal: number;
  view: NewsView; onPick: (v: NewsView) => void;
}) {
  return (
    <aside className="w-[208px] shrink-0 border-r border-mac-stroke flex flex-col py-3 px-2 overflow-auto">
      <div className="px-2 mb-1 text-[10px] uppercase tracking-wide text-mac-ink3">Feeds</div>
      {feeds.map((f) => {
        const active = view.kind === "feed" && view.cat === f;
        const Ico = newsCatIcon(f);
        return (
          <button key={f} onClick={() => onPick({ kind: "feed", cat: f })}
            className={`w-full flex items-center gap-2 h-8 px-2.5 rounded-md text-[12.5px] transition-colors ${active ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink2 hover:bg-mac-fill"}`}>
            <Ico size={14} className={active ? "text-mac-accentHi" : "text-mac-ink3"} />
            <span className="flex-1 text-left">{f}</span>
          </button>
        );
      })}
      <div className="mt-4 mb-1 px-2 text-[10px] uppercase tracking-wide text-mac-ink3">Saved</div>
      <button onClick={() => onPick({ kind: "saved", folder: null })}
        className={`w-full flex items-center gap-2 h-8 px-2.5 rounded-md text-[12.5px] transition-colors ${view.kind === "saved" && view.folder === null ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink2 hover:bg-mac-fill"}`}>
        <Bookmark size={14} className="text-mac-ink3" />
        <span className="flex-1 text-left">All Saved</span>
        <span className="text-[11px] text-mac-ink3 tnum">{savedTotal}</span>
      </button>
      {folders.map((c) => {
        const active = view.kind === "saved" && view.folder === c.name;
        return (
          <button key={c.name} onClick={() => onPick({ kind: "saved", folder: c.name })}
            className={`w-full flex items-center gap-2 h-8 px-2.5 rounded-md text-[12.5px] transition-colors ${active ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink2 hover:bg-mac-fill"}`}>
            <Folder size={14} className="text-mac-ink3 shrink-0" />
            <span className="flex-1 text-left truncate">{c.name}</span>
            <span className="text-[11px] text-mac-ink3 tnum">{c.count}</span>
          </button>
        );
      })}
      {folders.length === 0 && (
        <p className="px-2.5 mt-1 text-[11.5px] text-mac-ink3 leading-snug">Save an article to start a reading list.</p>
      )}
    </aside>
  );
}

function newsHue(s: string): number {
  let h = 0;
  for (let i = 0; i < (s || "").length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return h;
}

function CardImage({ source, image, hue }: { source: string; image?: string; hue: number }) {
  return (
    <div className="relative aspect-[16/9] overflow-hidden shrink-0">
      <div className="absolute inset-0 grid place-items-center"
        style={{ background: `linear-gradient(135deg, hsl(${hue} 30% 22%), hsl(${hue} 28% 13%))` }}>
        <div className="flex flex-col items-center gap-1.5">
          <Newspaper size={20} className="text-white/70" />
          <span className="text-[12px] font-medium text-white/80">{source || "News"}</span>
        </div>
      </div>
      {image && (
        <img src={image} loading="lazy" onError={(e) => { e.currentTarget.style.display = "none"; }}
          className="absolute inset-0 w-full h-full object-cover group-hover:scale-[1.03] transition-transform duration-300" />
      )}
    </div>
  );
}

function NewsCard({ a, saved, onOpen, onToggleSave, isForYou = false, isNew = false }: {
  a: NewsArticle; saved: boolean | "pending"; onOpen: () => void; onToggleSave: () => void;
  isForYou?: boolean; isNew?: boolean;
}) {
  const hue = newsHue(a.source || "News");
  const reason = isForYou ? (a.reason || "").trim() : "";
  return (
    <div onClick={onOpen}
      className="group relative flex flex-col h-full rounded-xl overflow-hidden bg-mac-fill border border-mac-stroke hover:border-mac-strokeHi hover:shadow-mac transition-all cursor-pointer">
      <CardImage source={a.source} image={a.image} hue={hue} />
      {isNew && (
        <span className="absolute top-2 left-2 inline-flex items-center gap-1 rounded-full bg-mac-accent/90 backdrop-blur-md px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-white">
          <Sparkle size={9} strokeWidth={2.4} /> New
        </span>
      )}
      <button onClick={(e) => { e.stopPropagation(); onToggleSave(); }}
        title={saved ? "Saved — remove from reading list" : "Save to read later"}
        className={`absolute top-2 right-2 h-7 w-7 grid place-items-center rounded-lg backdrop-blur-md border transition-all ${
          saved && saved !== "pending"
            ? "bg-mac-accent/90 border-transparent text-white"
            : "bg-black/40 border-white/15 text-white/90 hover:bg-black/65 opacity-0 group-hover:opacity-100"}`}>
        {saved === "pending" ? <Loader2 size={13} className="animate-spin" /> : saved ? <BookmarkCheck size={14} /> : <Bookmark size={14} />}
      </button>
      <div className="p-3.5 flex-1 flex flex-col">
        <div className="flex items-center gap-1.5 text-[11px] mb-1.5">
          <span className="font-medium" style={{ color: `hsl(${hue} 68% 68%)` }}>{a.source || "News"}</span>
          {a.ago && <span className="text-mac-ink3">· {a.ago}</span>}
          {(a.report_count ?? 0) > 1 && (
            <span title={`Also reported by ${(a.reports || []).map((r) => r.source).filter((s) => s !== a.source).join(", ")}`}
              className="ml-auto inline-flex items-center gap-1 rounded-full bg-mac-accent/12 text-mac-accentHi px-1.5 py-[1px] text-[10px] font-medium shrink-0">
              <Newspaper size={9} strokeWidth={2.2} /> {a.report_count} outlets
            </span>
          )}
        </div>
        <div className="text-[14px] text-mac-ink font-medium leading-snug line-clamp-2">{a.title}</div>
        {a.snippet && <div className="text-[12.5px] text-mac-ink2 mt-1.5 leading-snug line-clamp-2">{a.snippet}</div>}
        {reason && (
          <div className="mt-auto pt-2.5 flex items-center gap-1.5 text-[11px] text-mac-accentHi">
            <Sparkles size={11} strokeWidth={2} className="shrink-0" />
            <span className="truncate">{reason}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function SavedCard({ a, onOpen, onRemove }: { a: SavedArticle; onOpen: () => void; onRemove: () => void }) {
  const hue = newsHue(a.source || "News");
  return (
    <div onClick={onOpen}
      className="group relative flex flex-col h-full rounded-xl overflow-hidden bg-mac-fill border border-mac-stroke hover:border-mac-strokeHi hover:shadow-mac transition-all cursor-pointer">
      <CardImage source={a.source} image={a.image} hue={hue} />
      <button onClick={(e) => { e.stopPropagation(); onRemove(); }} title="Remove from saved"
        className="absolute top-2 right-2 h-7 w-7 grid place-items-center rounded-lg bg-black/40 border border-white/15 text-white/90 hover:bg-mac-red hover:border-transparent backdrop-blur-md opacity-0 group-hover:opacity-100 transition-all">
        <Trash2 size={13} />
      </button>
      <div className="p-3.5 flex-1 flex flex-col">
        <div className="flex items-center gap-1.5 text-[11px] mb-1.5">
          <span className="font-medium" style={{ color: `hsl(${hue} 68% 68%)` }}>{a.source || "News"}</span>
          <span className="text-mac-ink3 inline-flex items-center gap-1">· <Folder size={9} /> {a.folder}</span>
        </div>
        <div className="text-[14px] text-mac-ink font-medium leading-snug line-clamp-2">{a.title}</div>
        {a.snippet && <div className="text-[12.5px] text-mac-ink2 mt-1.5 leading-snug line-clamp-3">{a.snippet}</div>}
      </div>
    </div>
  );
}

function RecCard({ p, state, onAdd, onAsk, onDismiss }: {
  p: RecPaper; state?: "pending" | "added"; onAdd: () => void; onAsk?: () => void; onDismiss?: () => void;
}) {
  const hue = newsHue(p.venue || "arXiv");
  const ident = p.arxiv || p.doi;
  const authors = (p.authors || []).slice(0, 3).join(", ") + ((p.authors || []).length > 3 ? " et al." : "");
  return (
    <div className="group relative flex flex-col h-full rounded-xl overflow-hidden bg-mac-fill border border-mac-stroke hover:border-mac-strokeHi hover:shadow-mac transition-all">
      {onDismiss && (
        <button onClick={onDismiss} title="Not interested — show me less like this"
          className="absolute top-1.5 right-1.5 z-10 h-6 w-6 grid place-items-center rounded-full bg-mac-fill/80 backdrop-blur text-mac-ink3 hover:text-mac-ink hover:bg-mac-fillHi opacity-0 group-hover:opacity-100 transition-opacity">
          <X size={13} />
        </button>
      )}
      <div className="p-3.5 flex-1 flex flex-col">
        <div className="flex items-center gap-1.5 text-[11px] mb-1.5 pr-6">
          <span className="font-medium truncate" style={{ color: `hsl(${hue} 68% 68%)` }}>{p.venue || "Working paper"}</span>
          {p.year && <span className="text-mac-ink3 shrink-0">· {p.year}</span>}
          {!!p.citations && p.citations > 0 && <span className="text-mac-ink3 shrink-0">· {p.citations.toLocaleString()} cites</span>}
        </div>
        <div className="text-[14px] text-mac-ink font-medium leading-snug line-clamp-2">{p.title}</div>
        {authors && <div className="text-[11.5px] text-mac-ink3 mt-1 line-clamp-1">{authors}</div>}
        {(p.tldr || p.abstract) && (
          <div className="flex items-start gap-1 mt-1.5">
            {p.tldr && <Sparkles size={12} strokeWidth={2} className="text-mac-accentHi shrink-0 mt-0.5" />}
            <p className="text-[12.5px] text-mac-ink2 leading-snug line-clamp-3">{p.tldr || p.abstract}</p>
          </div>
        )}
        {p.why && (
          <div className="flex items-center gap-1 mt-2 text-[11px] text-mac-accentHi">
            <BookText size={11} strokeWidth={2} className="shrink-0" /> <span className="truncate">{p.why}</span>
          </div>
        )}
        <div className="flex items-center gap-1.5 mt-auto pt-3">
          <button onClick={onAdd} disabled={!ident || state === "pending" || state === "added"}
            title={!ident ? "No identifier to import" : "Add this paper to your Library"}
            className={`h-8 px-3 rounded-[9px] text-[12px] font-medium inline-flex items-center gap-1.5 transition-colors disabled:opacity-60 ${
              state === "added"
                ? "bg-mac-green/15 text-mac-green border border-mac-green/30"
                : "bg-mac-accent text-white hover:bg-mac-accentHi"}`}>
            {state === "pending" ? <Loader2 size={13} className="animate-spin" />
              : state === "added" ? <Check size={13} strokeWidth={2.5} />
              : <Plus size={13} strokeWidth={2.5} />}
            {state === "added" ? "In Library" : "Add"}
          </button>
          {onAsk && (
            <button onClick={onAsk} title="Ask Himmy about this paper"
              className="h-8 w-8 grid place-items-center rounded-[9px] bg-mac-fillHi border border-mac-stroke text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">
              <MessageSquare size={13} />
            </button>
          )}
          {p.url && (
            <a href={p.url} target="_blank" rel="noreferrer" title="Open in browser"
              className="h-8 w-8 grid place-items-center rounded-[9px] bg-mac-fillHi border border-mac-stroke text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">
              <ExternalLink size={13} />
            </a>
          )}
        </div>
      </div>
    </div>
  );
}

function RecsEmpty() {
  return (
    <div className="h-full grid place-items-center">
      <div className="w-full max-w-sm text-center rounded-2xl border border-dashed border-mac-stroke px-8 py-12">
        <div className="mx-auto h-14 w-14 rounded-2xl grid place-items-center bg-mac-fill border border-mac-stroke mb-5 shadow-mac">
          <LibraryIcon size={22} strokeWidth={1.75} className="text-mac-accentHi" />
        </div>
        <h2 className="font-display text-[18px] font-semibold tracking-[-0.01em] mb-2">Recommendations from your reading</h2>
        <p className="text-[13px] leading-relaxed text-mac-ink2 mb-2">
          Himmy learns the field(s) you actually read — following your papers across OpenAlex,
          Semantic Scholar and Crossref — and surfaces fresh work to drop into your Library.
        </p>
        <p className="text-[12px] text-mac-ink3">Add a few papers to your Library, or a topic above, then hit Refresh.</p>
      </div>
    </div>
  );
}

function SavedEmpty({ query, onBrowse }: { query: string; onBrowse: () => void }) {
  if (query) return <div className="h-48 grid place-items-center text-[13px] text-mac-ink3">No saved articles match “{query}”.</div>;
  return (
    <div className="h-full grid place-items-center">
      <div className="w-full max-w-sm text-center rounded-2xl border border-dashed border-mac-stroke px-8 py-12">
        <div className="mx-auto h-14 w-14 rounded-2xl grid place-items-center bg-mac-fill border border-mac-stroke mb-5 shadow-mac">
          <Bookmark size={22} strokeWidth={1.75} className="text-mac-accentHi" />
        </div>
        <h2 className="font-display text-[18px] font-semibold tracking-[-0.01em] mb-2">Nothing saved yet</h2>
        <p className="text-[13px] leading-relaxed text-mac-ink2 mb-6">
          Hit the bookmark on any story to keep it here. Saved articles read offline and Himmy can
          answer questions about them.
        </p>
        <button onClick={onBrowse}
          className="h-9 px-4 rounded-[10px] bg-mac-accent text-[13px] font-medium text-white hover:bg-mac-accentHi transition-colors inline-flex items-center gap-1.5">
          <Newspaper size={15} strokeWidth={2} /> Browse the news
        </button>
      </div>
    </div>
  );
}

function FolderMenu({ folders, onPick, onClose }: { folders: NewsFolder[]; onPick: (f: string) => void; onClose: () => void }) {
  const [name, setName] = useState("");
  const rest = folders.filter((f) => f.name !== "Reading List");
  return (
    <>
      <div className="fixed inset-0 z-10" onMouseDown={onClose} />
      <div className="absolute right-0 top-9 z-20 w-56 rounded-xl bg-[rgba(40,41,47,0.97)] backdrop-blur-xl border border-mac-strokeHi shadow-pop p-1.5">
        <div className="px-2 py-1 text-[10px] uppercase tracking-wide text-mac-ink3">Save to</div>
        <button onClick={() => onPick("Reading List")}
          className="w-full flex items-center gap-2 h-8 px-2 rounded-md text-[12.5px] text-mac-ink2 hover:bg-mac-fill hover:text-mac-ink transition-colors">
          <Bookmark size={13} className="text-mac-ink3" /> Reading List
        </button>
        {rest.map((f) => (
          <button key={f.name} onClick={() => onPick(f.name)}
            className="w-full flex items-center gap-2 h-8 px-2 rounded-md text-[12.5px] text-mac-ink2 hover:bg-mac-fill hover:text-mac-ink transition-colors">
            <Folder size={13} className="text-mac-ink3" /> <span className="flex-1 text-left truncate">{f.name}</span>
          </button>
        ))}
        <div className="flex items-center gap-1.5 mt-1 px-2 h-8 rounded-md bg-mac-fill border border-mac-stroke">
          <FolderPlus size={13} className="text-mac-ink3 shrink-0" />
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && name.trim()) onPick(name.trim()); if (e.key === "Escape") onClose(); }}
            placeholder="New folder…" className="flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-mac-ink3" />
        </div>
      </div>
    </>
  );
}

/* ── news reader sidebar (Info / Notes / Highlights — mirrors the Library reader) ── */
const NEWS_HL: Record<string, string> = {
  yellow: "rgba(250, 204, 21, 0.32)",
  green: "rgba(52, 211, 153, 0.30)",
  blue: "rgba(96, 165, 250, 0.32)",
  pink: "rgba(244, 114, 182, 0.30)",
};
function escapeRe(s: string) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
/* Wrap any highlighted snippet inside a paragraph with a colored <mark> (longest match first). */
function markText(text: string, highlights: NewsHighlight[]): React.ReactNode {
  if (!highlights.length) return text;
  const byText = new Map<string, NewsHighlight>();
  for (const h of highlights) { const t = h.text.trim(); if (t && !byText.has(t)) byText.set(t, h); }
  const keys = [...byText.keys()].sort((a, b) => b.length - a.length);
  if (!keys.length) return text;
  let re: RegExp;
  try { re = new RegExp(keys.map(escapeRe).join("|"), "g"); } catch { return text; }
  const out: React.ReactNode[] = [];
  let last = 0, m: RegExpExecArray | null, i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index === re.lastIndex) { re.lastIndex++; continue; }
    if (m.index > last) out.push(text.slice(last, m.index));
    const h = byText.get(m[0]);
    out.push(
      <mark key={i++} data-hl={h?.id} title={h?.note || undefined}
        style={{ background: NEWS_HL[h?.color || "yellow"] || NEWS_HL.yellow, color: "inherit", borderRadius: 3, padding: "0.5px 1px" }}>
        {m[0]}
      </mark>,
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function NewsTabBtn({ icon: Ico, label, active, onClick, count }: {
  icon: LucideIcon; label: string; active: boolean; onClick: () => void; count?: number;
}) {
  return (
    <button onClick={onClick}
      className={`h-8 px-2.5 rounded-[8px] text-[12.5px] flex items-center gap-1.5 transition-colors ${
        active ? "bg-mac-fill text-mac-ink" : "text-mac-ink3 hover:text-mac-ink2"}`}>
      <Ico size={13} /> {label}
      {count ? <span className="text-[11px] text-mac-ink3">{count}</span> : null}
    </button>
  );
}

function NewsInfoPanel({ summary, summarizing, onSummarize, onClearSummary, source, meta, url }: {
  summary: string | null; summarizing: boolean; onSummarize: () => void; onClearSummary: () => void;
  source: string; meta: string; url: string;
}) {
  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-mac-accent bg-mac-accentDim p-3.5">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-1.5">
            <Sparkles size={13} className="text-mac-accentHi" />
            <span className="text-[11.5px] font-semibold uppercase tracking-[0.06em] text-mac-ink">Summary</span>
            {summarizing && <Loader2 size={11} className="animate-spin text-mac-ink3" />}
          </div>
          {summary !== null && !summarizing && (
            <div className="flex items-center gap-0.5">
              <button onClick={onSummarize} title="Re-run" className="h-6 w-6 grid place-items-center rounded-md text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors"><RefreshCw size={12} /></button>
              <button onClick={onClearSummary} title="Clear" className="h-6 w-6 grid place-items-center rounded-md text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors"><X size={13} /></button>
            </div>
          )}
        </div>
        {summary === null ? (
          <button onClick={onSummarize} disabled={summarizing}
            className="w-full mt-0.5 h-8 rounded-lg bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors flex items-center justify-center gap-1.5 disabled:opacity-40">
            <Sparkles size={13} className="text-mac-accentHi" /> Summarize this article
          </button>
        ) : summary === "" && summarizing ? (
          <div className="flex items-center gap-1.5 py-1 text-[12.5px] text-mac-ink3">
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-mac-ink3 animate-pulse" /> Reading the article…
          </div>
        ) : (
          <div className="text-[12.5px] leading-[1.65] text-mac-ink2 whitespace-pre-wrap">{summary}</div>
        )}
      </div>
      <div className="space-y-2.5">
        {source && (<div><div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-mac-ink3 mb-1">Source</div><div className="text-[13px] text-mac-ink2">{source}</div></div>)}
        {meta && (<div><div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-mac-ink3 mb-1">Published</div><div className="text-[13px] text-mac-ink2">{meta}</div></div>)}
        <div>
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-mac-ink3 mb-1">Link</div>
          <a href={url} target="_blank" rel="noreferrer" className="text-[12.5px] text-mac-accentHi hover:underline inline-flex items-center gap-1 break-all">Open original <ExternalLink size={12} className="shrink-0" /></a>
        </div>
      </div>
    </div>
  );
}

function NewsHighlightsPanel({ highlights, onJump, onRemove }: {
  highlights: NewsHighlight[]; onJump: (id: string) => void; onRemove: (id: string) => void;
}) {
  if (!highlights.length) {
    return (
      <div className="h-full min-h-[140px] flex flex-col items-center justify-center text-center gap-2.5 py-6">
        <Highlighter size={20} strokeWidth={1.75} className="text-mac-ink3" />
        <p className="text-[12.5px] text-mac-ink3 max-w-[30ch] leading-relaxed">Select any text in the article to highlight it. Your highlights show up here.</p>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {highlights.map((h) => (
        <div key={h.id} className="group rounded-lg border border-mac-stroke bg-mac-fill p-2.5">
          <button onClick={() => onJump(h.id)} className="block w-full text-left">
            <div className="flex gap-2">
              <span className="mt-1 h-3.5 w-1 shrink-0 rounded-full" style={{ background: NEWS_HL[h.color] || NEWS_HL.yellow }} />
              <p className="text-[12.5px] leading-snug text-mac-ink2">{h.text}</p>
            </div>
          </button>
          <div className="flex items-center justify-end mt-1">
            <button onClick={() => onRemove(h.id)} title="Remove" className="opacity-0 group-hover:opacity-100 h-6 w-6 grid place-items-center rounded-md text-mac-ink3 hover:text-mac-orange transition-all"><Trash2 size={12} /></button>
          </div>
        </div>
      ))}
    </div>
  );
}

function NewsReader({ target, onClose, isSaved, folders, onSave, onUnsave }: {
  target: ReadTarget; onClose: () => void; isSaved: boolean; folders: NewsFolder[];
  onSave: (folder: string) => void; onUnsave: () => void;
}) {
  const [content, setContent] = useState<ArticleContent | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState<string | null>(null);
  const [pickOpen, setPickOpen] = useState(false);
  const [summary, setSummary] = useState<string | null>(null);
  const [summarizing, setSummarizing] = useState(false);
  const [tab, setTab] = useState<"info" | "notes" | "highlights">("info");
  const [note, setNote] = useState("");
  const [highlights, setHighlights] = useState<NewsHighlight[]>([]);
  const [sel, setSel] = useState<{ text: string; x: number; y: number } | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const a = target.article;

  useEffect(() => {
    let alive = true;
    setLoading(true); setFailed(null); setContent(null);
    setSummary(null); setSummarizing(false);
    setTab("info"); setNote(""); setHighlights([]); setSel(null);
    const run: Promise<ArticleContent> = target.savedId
      ? api.news.savedGet(target.savedId).then((r) => ({
          ok: true, url: r.item.url, title: r.item.title, source: r.item.source,
          image: r.item.image, author: r.item.author, date: r.item.published,
          paragraphs: r.item.paragraphs || [],
        }))
      : api.news.article(a.url);
    run.then((c) => {
      if (!alive) return;
      if (c.ok && c.paragraphs?.length) setContent(c);
      else setFailed(c.message || "This article couldn't be opened for reading.");
    }).catch(() => alive && setFailed("Couldn't reach this article."))
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [target]);

  const url = content?.url || a.url;
  const title = content?.title || a.title;
  const source = a.source || content?.source || "";
  const image = content?.image || a.image;
  const meta = [content?.author, content?.date].filter(Boolean).join(" · ");

  // load this article's saved notes + highlights once we know its url
  useEffect(() => {
    let alive = true;
    if (!url) return;
    api.news.annotations(url).then((r) => {
      if (!alive) return;
      setNote(r.note || "");
      setHighlights(r.highlights || []);
      if (r.summary) setSummary(r.summary);   // a previously-generated summary — no need to re-run it
    }).catch(() => { /* annotations are best-effort */ });
    return () => { alive = false; };
  }, [url]);

  const saveNote = () => { if (url) api.news.setNote(url, note).catch(() => { /* best effort */ }); };

  // text selection in the article → a floating "Highlight" button
  const onArticleMouseUp = () => {
    const s = window.getSelection();
    const txt = s?.toString().trim() || "";
    if (txt.length >= 4 && txt.length <= 600 && s && s.rangeCount > 0) {
      const rect = s.getRangeAt(0).getBoundingClientRect();
      setSel({ text: txt, x: rect.left + rect.width / 2, y: rect.top });
    } else {
      setSel(null);
    }
  };

  const addHighlight = async (color: string) => {
    if (!sel || !url) return;
    const text = sel.text;
    setSel(null);
    window.getSelection()?.removeAllRanges();
    try {
      const r = await api.news.addHighlight({ url, text, color });
      if (r.ok && r.highlight) setHighlights((h) => [...h, r.highlight]);
    } catch { /* ignore */ }
  };

  const removeHighlight = async (hid: string) => {
    setHighlights((h) => h.filter((x) => x.id !== hid));
    try { await api.news.removeHighlight(hid); } catch { /* ignore */ }
  };

  const jumpToHighlight = (hid: string) => {
    const el = scrollRef.current?.querySelector(`mark[data-hl="${hid}"]`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  const clearSummary = () => {
    setSummary(null);
    if (url) api.news.setSummary(url, "").catch(() => { /* best effort */ });
  };

  async function summarize() {
    if (!content || summarizing) return;
    setTab("info");
    setSummary("");
    setSummarizing(true);
    let acc = "";
    const articleText = [title, ...(content.paragraphs || [])].join("\n\n");
    try {
      await api.askStream(
        "Summarise this news article in 3–5 concise key bullet points. Use plain language. Reply with only the bullets.",
        { context: articleText, onToken: (t) => { acc += t; setSummary((s) => (s ?? "") + t); } },
      );
      // cache it so reopening the article never re-summarises
      if (url && acc.trim()) api.news.setSummary(url, acc.trim()).catch(() => { /* best effort */ });
    } catch {
      setSummary((s) => s || "Couldn't generate a summary right now. Please try again.");
    } finally {
      setSummarizing(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 h-[52px] px-4 flex items-center justify-between border-b border-mac-stroke">
        <button onClick={onClose}
          className="flex items-center gap-1.5 h-8 pl-1.5 pr-2.5 rounded-[9px] text-mac-ink2 hover:text-mac-ink hover:bg-mac-fill transition-colors">
          <ArrowLeft size={16} strokeWidth={2} /> <span className="text-[13px]">Back</span>
        </button>
        <div className="flex items-center gap-2">
          {isSaved ? (
            <button onClick={onUnsave}
              className="h-8 px-3 rounded-[9px] bg-mac-accentDim border border-mac-accent text-[12.5px] text-mac-ink flex items-center gap-1.5 hover:bg-mac-fill transition-colors">
              <BookmarkCheck size={14} className="text-mac-accentHi" /> Saved
            </button>
          ) : (
            <div className="relative">
              <button onClick={() => setPickOpen((o) => !o)}
                className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors flex items-center gap-1.5">
                <Bookmark size={14} /> Save <ChevronDown size={12} className="text-mac-ink3" />
              </button>
              {pickOpen && <FolderMenu folders={folders} onPick={(f) => { onSave(f); setPickOpen(false); }} onClose={() => setPickOpen(false)} />}
            </div>
          )}
          <a href={a.url} target="_blank" rel="noreferrer"
            className="h-8 px-3 rounded-[9px] bg-mac-fill border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors flex items-center gap-1.5">
            <Globe size={14} /> Original
          </a>
        </div>
      </div>

      <div className="flex-1 min-h-0 flex">
        {/* article */}
        <div ref={scrollRef} className="flex-1 min-w-0 overflow-auto" onMouseUp={onArticleMouseUp}>
          <article className="mx-auto max-w-[700px] px-8 py-10">
            {source && <div className="text-[12px] font-medium uppercase tracking-[0.08em] text-mac-accentHi mb-3">{source}</div>}
            <h1 className="font-semibold text-[31px] leading-[1.16] tracking-[-0.01em] text-mac-ink" style={{ fontFamily: READER_SERIF }}>{title}</h1>
            {meta && <div className="mt-3 text-[12.5px] text-mac-ink3">{meta}</div>}
            {image && (
              <img src={image} onError={(e) => { e.currentTarget.style.display = "none"; }}
                className="mt-6 w-full rounded-xl border border-mac-stroke object-cover" />
            )}
            {loading ? (
              <div className="mt-12 grid place-items-center text-mac-ink3"><Loader2 size={20} className="animate-spin" /></div>
            ) : failed ? (
              <div className="mt-10 rounded-xl border border-mac-stroke bg-mac-fill px-5 py-6 text-center">
                <p className="text-[13px] text-mac-ink2">{failed}</p>
                <a href={a.url} target="_blank" rel="noreferrer"
                  className="mt-3 inline-flex items-center gap-1.5 text-[13px] text-mac-accentHi hover:underline">
                  Open the original <ExternalLink size={13} />
                </a>
              </div>
            ) : (
              <div className="mt-7 space-y-5 text-mac-ink2" style={{ fontFamily: READER_SERIF, fontSize: "17px", lineHeight: 1.75 }}>
                {content!.paragraphs.map((p, i) => <p key={i}>{markText(p, highlights)}</p>)}
              </div>
            )}
          </article>
        </div>

        {/* sidebar: Info / Notes / Highlights */}
        <aside className="w-[340px] shrink-0 border-l border-mac-stroke flex flex-col min-h-0 bg-[rgba(255,255,255,0.015)]">
          <div className="shrink-0 flex items-center gap-1 px-2.5 h-11 border-b border-mac-stroke">
            <NewsTabBtn icon={Info} label="Info" active={tab === "info"} onClick={() => setTab("info")} />
            <NewsTabBtn icon={StickyNote} label="Notes" active={tab === "notes"} onClick={() => setTab("notes")} />
            <NewsTabBtn icon={Highlighter} label="Highlights" active={tab === "highlights"} onClick={() => setTab("highlights")} count={highlights.length} />
          </div>
          <div className="flex-1 min-h-0 overflow-auto p-4">
            {tab === "info" && (
              <NewsInfoPanel summary={summary} summarizing={summarizing} onSummarize={summarize}
                onClearSummary={clearSummary} source={source} meta={meta} url={a.url} />
            )}
            {tab === "notes" && (
              <textarea value={note} onChange={(e) => setNote(e.target.value)} onBlur={saveNote}
                placeholder="Your notes on this article — saved automatically."
                className="w-full h-[62vh] resize-none rounded-lg bg-mac-fill border border-mac-stroke px-3 py-2.5 text-[13px] leading-relaxed text-mac-ink placeholder:text-mac-ink3 outline-none focus:border-mac-strokeHi" />
            )}
            {tab === "highlights" && (
              <NewsHighlightsPanel highlights={highlights} onJump={jumpToHighlight} onRemove={removeHighlight} />
            )}
          </div>
        </aside>
      </div>

      {/* floating highlight button on a text selection */}
      {sel && (
        <div className="fixed z-50 -translate-x-1/2 -translate-y-full" style={{ left: sel.x, top: sel.y - 8 }}>
          <div className="flex items-center gap-1 rounded-[10px] bg-mac-fillHi border border-mac-strokeHi shadow-mac px-2 py-1.5">
            <Highlighter size={13} className="text-mac-ink3 mr-0.5" />
            {(["yellow", "green", "blue", "pink"] as const).map((c) => (
              <button key={c} onMouseDown={(e) => { e.preventDefault(); addHighlight(c); }}
                title={`Highlight ${c}`} className="h-5 w-5 rounded-full border border-black/25 hover:scale-110 transition-transform"
                style={{ background: NEWS_HL[c] }} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ───────────────────────────────────────── card primitives */
function Card({ icon: Ico, title, hint, action, onOpen, className = "", children }:
  { icon: LucideIcon; title: string; hint?: string;
    action?: { label: string; onClick: () => void }; onOpen?: () => void;
    className?: string; children: React.ReactNode }) {
  const head = (
    <>
      <div className="h-7 w-7 shrink-0 rounded-[9px] grid place-items-center bg-gradient-to-b from-white/[0.11] to-white/[0.035] ring-1 ring-inset ring-white/[0.07] shadow-[0_1px_2px_rgba(0,0,0,0.25)]">
        <Ico size={13.5} strokeWidth={2.25} className="text-mac-ink2 group-hover:text-mac-ink transition-colors" />
      </div>
      <span className="text-[13.5px] font-semibold text-mac-ink truncate tracking-[-0.01em]">{title}</span>
      {hint && <span className="text-[11.5px] text-mac-ink3 truncate font-medium">· {hint}</span>}
      {onOpen && <ChevronRight size={14} strokeWidth={2} className="text-mac-ink4 shrink-0 group-hover:text-mac-ink2 group-hover:translate-x-0.5 transition-all" />}
    </>
  );
  return (
    <section className={`rounded-2xl border border-white/[0.07] bg-gradient-to-b from-white/[0.055] to-white/[0.018] shadow-card p-5 flex flex-col transition-shadow duration-300 hover:shadow-cardHover ${className}`}>
      <div className="flex items-center justify-between mb-3.5">
        {onOpen
          ? <button onClick={onOpen} title={`Open ${title}`}
              className="group flex items-center gap-2.5 min-w-0 -ml-1 -my-1 pl-1 pr-2 py-1 rounded-lg hover:bg-mac-fillHi transition-colors">{head}</button>
          : <div className="group flex items-center gap-2.5 min-w-0">{head}</div>}
        {action && (
          <button onClick={action.onClick}
            className="shrink-0 text-[12.5px] text-mac-accentHi hover:underline">{action.label}</button>
        )}
      </div>
      <div className="flex-1 min-h-0">{children}</div>
    </section>
  );
}

function Placeholder({ icon: Ico, text }: { icon: LucideIcon; text: string }) {
  return (
    <div className="h-full min-h-[96px] flex flex-col items-center justify-center text-center gap-2.5 py-4">
      <Ico size={20} strokeWidth={1.75} className="text-mac-ink4" />
      <p className="text-[12.5px] text-mac-ink3 max-w-[34ch] leading-relaxed">{text}</p>
    </div>
  );
}

/* ───────────────────────────────────────── command bar (agent) */
type ApprovalState = { checkpointId: string; pending: Pending[]; status: "pending" | "approved" | "cancelled" };
type Msg = {
  who: "you" | "desk";
  text: string;
  tools?: string[];
  tool_results?: ToolResult[]; // typed, server-redacted connector outputs → rich inline cards + follow-up chips
  streaming?: boolean;
  research?: ResearchResult; // present on a deep-research result bubble
  researching?: boolean;     // a deep-research run in flight (distinct from token streaming)
  approval?: ApprovalState;  // Himmy proposed a gated action awaiting the user's OK
};
const CHIPS = ["What's in my library?", "Summarise a paper", "Find related work", "Plan my day"];

// One stable conversation id per install, persisted so refreshes keep context.
const SESSION_KEY = "daybook.chat.session";
const DOCK_KEY = "daybook.chat.dock"; // "center" (floating) | "right" (docked side panel)
type Dock = "center" | "right";
function newSessionId() {
  const id = `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  localStorage.setItem(SESSION_KEY, id);
  return id;
}
function currentSessionId() {
  return localStorage.getItem(SESSION_KEY) || newSessionId();
}

// A small icon for an attachment by its kind (image / audio / doc-or-other).
function fileKindIcon(kind: string, size = 13) {
  if (kind === "image") return <ImageIcon size={size} strokeWidth={2} />;
  if (kind === "audio") return <Mic size={size} strokeWidth={2} />;
  return <FileText size={size} strokeWidth={2} />;
}
function prettyBytes(bytes: number) {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function CommandBar() {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [sessionId, setSessionId] = useState<string>(() => currentSessionId());
  const [showHistory, setShowHistory] = useState(false);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [dock, setDock] = useState<Dock>(() => ((localStorage.getItem(DOCK_KEY) as Dock) || "center"));
  const [minimized, setMinimized] = useState(false);
  // The live tool-trace label ("Looking up flights…") shown in place of the typing dots while a
  // turn streams; cleared the moment the reply lands. Doxing-safe — labels only, never arg values.
  const [trace, setTrace] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const minimizedRef = useRef(false);
  // Abort handle for the in-flight stream — wired to the Stop button. Cancels the SSE fetch, which
  // server-side cancels the background agent task (partial-thread save in the himmy runtime).
  const abortRef = useRef<(() => void) | null>(null);
  // Files the user attached to the NEXT message: Himmy reads them on upload (the returned `text`
  // grounds this turn) and keeps them in its knowledge for later. Cleared once the message is sent.
  const [pendingFiles, setPendingFiles] = useState<AttachmentResult[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const uploadFiles = async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (list.length === 0) return;
    setUploading(true);
    for (const f of list) {
      try {
        const r = await api.attach(f, sessionId);
        if (r.ok && r.attachment) setPendingFiles((p) => [...p, r.attachment!]);
      } catch { /* a single failed upload shouldn't block the rest */ }
    }
    setUploading(false);
    requestAnimationFrame(() => inputRef.current?.focus());
  };
  const removePending = (id: string) => setPendingFiles((p) => p.filter((f) => f.id !== id));

  useEffect(() => { minimizedRef.current = minimized; }, [minimized]);
  const setDockMode = (d: Dock) => { setDock(d); localStorage.setItem(DOCK_KEY, d); };

  const loadSessions = async () => {
    try { setSessions((await api.sessions.list()).sessions); } catch { /* engine down */ }
  };

  // Summon (⌘K / Search button / card actions); dismiss (Esc / click-away). Never always-on.
  useEffect(() => {
    const onAsk = (e: Event) => {
      setOpen(true);
      setMinimized(false);
      setInput((e as CustomEvent<string>).detail || "");
      requestAnimationFrame(() => inputRef.current?.focus());
    };
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (minimizedRef.current) setMinimized(false);
        else setOpen((o) => !o);
        requestAnimationFrame(() => inputRef.current?.focus());
      } else if (e.key === "Escape") {
        setOpen(false);
        setMinimized(false);
      }
    };
    window.addEventListener("himmy:ask", onAsk);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("himmy:ask", onAsk);
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [msgs, busy]);

  // Refresh the history list whenever the sidebar is opened.
  useEffect(() => { if (showHistory) loadSessions(); }, [showHistory]);

  const newChat = () => {
    setMsgs([]);
    setSessionId(newSessionId());
    setShowHistory(false);
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const resume = async (id: string) => {
    try {
      const r = await api.sessions.get(id);
      setMsgs(r.messages.map((m) => ({ who: m.role === "user" ? "you" : "desk", text: m.content })));
      setSessionId(id);
      localStorage.setItem(SESSION_KEY, id);
      setShowHistory(false);
    } catch { /* ignore */ }
  };

  const removeSession = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try { await api.sessions.remove(id); } catch { /* ignore */ }
    setSessions((s) => s.filter((x) => x.session_id !== id));
    if (id === sessionId) newChat();
  };

  // Stop the in-flight turn — aborts the SSE stream (and, server-side, the background agent run).
  // Any tokens already streamed stay on screen; the bubble settles out of its streaming state.
  const stop = () => {
    abortRef.current?.();
    setTrace(null);
    setMsgs((m) => m.map((x, i) => (i === m.length - 1 && x.streaming ? { ...x, streaming: false } : x)));
    setBusy(false);
  };

  const send = async (text: string) => {
    const q = text.trim();
    const pend = pendingFiles;
    if ((!q && pend.length === 0) || busy) return;
    setInput("");
    setPendingFiles([]);  // consumed by this turn (the files persist in Himmy's knowledge for later)
    // The prompt the agent sees (clean); the bubble shows the typed text + a chip list of files.
    const promptText = q || "Please read the file(s) I attached and give me a short summary.";
    const fileNote = pend.length
      ? "\n\n" + pend.map((f) => `📎 ${f.name}`).join("\n")
      : "";
    setMsgs((m) => [...m, { who: "you", text: (q || "Sent a file") + fileNote }, { who: "desk", text: "", streaming: true }]);
    setBusy(true);
    setTrace(null);
    const setLast = (patch: Partial<Msg>) =>
      setMsgs((m) => m.map((x, i) => (i === m.length - 1 ? { ...x, ...patch } : x)));
    try {
      // What is the user looking at right now? (paper / article, if any.) Plus the just-attached
      // files' extracted text, so this turn is grounded on them immediately (RAG covers later turns).
      const baseCtx = await buildAskContext();
      const fileCtx = pend
        .filter((f) => f.text && !f.empty)
        .map((f) => `The user attached a file "${f.name}". Its contents:\n${f.text}`)
        .join("\n\n---\n\n");
      const context = [fileCtx, baseCtx].filter(Boolean).join("\n\n") || undefined;
      const { done, abort } = api.askStream(promptText, {
        sessionId,
        context,
        onToken: (t) => setMsgs((m) =>
          m.map((x, i) => (i === m.length - 1 ? { ...x, text: x.text + t } : x))),
        // Live tool-trace: a doxing-safe human label per tool, shown in place of the typing dots.
        onTrace: (label) => setTrace(label),
      });
      abortRef.current = abort;
      const r = await done;
      if (r.awaiting_approval && r.checkpoint_id) {
        setLast({ text: r.reply || "", tools: r.tools, tool_results: r.tool_results, streaming: false,
          approval: { checkpointId: r.checkpoint_id, pending: r.pending || [], status: "pending" } });
      } else {
        setLast({ text: r.reply, tools: r.tools, tool_results: r.tool_results, streaming: false });
      }
      // Any direct (non-gated) action that ran → refresh the matching tab live.
      emitRefreshForTools(r.tools);
    } catch (e: any) {
      setLast({ text: `Couldn't reach Himmy — ${e.message ?? "is the engine running?"}`, streaming: false });
    } finally { setBusy(false); setTrace(null); abortRef.current = null; }
  };

  // Approve (execute) or cancel a gated action Himmy proposed, then continue the run.
  const decide = async (msgIndex: number, checkpointId: string, approved: boolean) => {
    if (busy) return;
    // The tools the user just approved (e.g. calendar_add) — so we refresh the view
    // even if the backend's resume result doesn't echo the executed gated tool.
    const approvedTools = approved
      ? (msgs[msgIndex]?.approval?.pending || []).map((p) => p.tool_name)
      : [];
    setMsgs((m) => m.map((x, i) => (i === msgIndex && x.approval
      ? { ...x, approval: { ...x.approval, status: approved ? "approved" : "cancelled" } } : x)));
    setMsgs((m) => [...m, { who: "desk", text: "", streaming: true }]);
    setBusy(true);
    const setLast = (patch: Partial<Msg>) =>
      setMsgs((m) => m.map((x, i) => (i === m.length - 1 ? { ...x, ...patch } : x)));
    try {
      const r = await api.resume(checkpointId, approved, sessionId);
      if (r.awaiting_approval && r.checkpoint_id) {
        setLast({ text: r.reply || "", tools: r.tools, tool_results: r.tool_results, streaming: false,
          approval: { checkpointId: r.checkpoint_id, pending: r.pending || [], status: "pending" } });
      } else {
        setLast({ text: r.reply || (approved ? "Done." : "Okay — cancelled."), tools: r.tools, tool_results: r.tool_results, streaming: false });
      }
      // Approved action (+ any follow-on tools) executed server-side → refresh the tab live.
      emitRefreshForTools([...approvedTools, ...(r.tools || [])]);
    } catch (e: any) {
      setLast({ text: `Couldn't ${approved ? "complete that" : "cancel"} — ${e.message ?? "engine error"}`, streaming: false });
    } finally { setBusy(false); }
  };

  // Deep research: explicit, slow, multi-step. Plans → fans out library + web → synthesises
  // a cited brief → reflects. Runs only on this button, never on a normal Enter.
  const deepResearch = async (text: string) => {
    const q = text.trim();
    if (!q || busy) return;
    setInput("");
    setMsgs((m) => [...m, { who: "you", text: q }, { who: "desk", text: "", researching: true }]);
    setBusy(true);
    const setLast = (patch: Partial<Msg>) =>
      setMsgs((m) => m.map((x, i) => (i === m.length - 1 ? { ...x, ...patch } : x)));
    try {
      const r = await api.research(q);
      setLast({ text: r.brief, research: r, researching: false });
    } catch (e: any) {
      setLast({
        text: `Deep research couldn't run — ${e.message ?? "is the engine running?"}`,
        researching: false,
      });
    } finally { setBusy(false); }
  };

  if (!open) return null;

  // Minimized → a small floating pill that never obstructs the workspace; click to reopen.
  if (minimized) {
    return (
      <div className="absolute inset-0 z-30 pointer-events-none">
        <button onClick={() => { setMinimized(false); requestAnimationFrame(() => inputRef.current?.focus()); }}
          title="Open Himmy (⌘K)"
          className="himmy-enter-right pointer-events-auto absolute bottom-4 right-4 flex items-center gap-2 h-10 px-4 rounded-full bg-[rgba(36,37,43,0.97)] backdrop-blur-2xl border border-mac-strokeHi shadow-pop text-mac-ink hover:border-mac-accent transition-colors">
          <span className="text-[13px] font-display font-medium">Himmy</span>
          {busy && <Loader2 size={13} className="animate-spin text-mac-ink3" />}
        </button>
      </div>
    );
  }

  const docked = dock === "right";

  return (
    <div className={`absolute inset-0 z-30 ${docked ? "pointer-events-none" : ""}`}
      onMouseDown={docked ? undefined : () => setOpen(false)}>
      {!docked && <div className="absolute inset-0 bg-black/25" />}
      <div
        onMouseDown={(e) => e.stopPropagation()}
        className={
          docked
            ? "himmy-enter-right pointer-events-auto absolute top-[60px] right-3 bottom-3 w-[372px] flex flex-col rounded-2xl bg-[rgba(36,37,43,0.97)] backdrop-blur-2xl border border-mac-strokeHi shadow-pop overflow-hidden"
            : "himmy-enter-center absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[720px] max-w-[calc(100%-3rem)] h-[70vh] max-h-[660px] flex flex-col rounded-2xl bg-[rgba(36,37,43,0.97)] backdrop-blur-2xl border border-mac-strokeHi shadow-pop overflow-hidden"
        }
      >
        {/* Header */}
        <div className="shrink-0 flex items-center gap-1.5 h-[46px] px-3 border-b border-mac-stroke">
          <button onClick={() => setShowHistory((h) => !h)} title="Conversation history"
            className={`h-7 w-7 grid place-items-center rounded-[9px] transition-colors ${showHistory ? "text-mac-accentHi bg-mac-fill" : "text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill"}`}>
            <PanelLeft size={15} strokeWidth={2} />
          </button>
          <div className="flex items-center ml-1">
            <span className="text-[13px] font-display font-medium text-mac-ink">Himmy</span>
          </div>
          <div className="flex-1" />
          <button onClick={newChat} title="New chat"
            className="h-7 w-7 grid place-items-center rounded-[9px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors">
            <SquarePen size={15} strokeWidth={2} />
          </button>
          <button onClick={() => setDockMode(docked ? "center" : "right")}
            title={docked ? "Float in the centre" : "Dock to the side"}
            className={`h-7 w-7 grid place-items-center rounded-[9px] transition-colors ${docked ? "text-mac-accentHi bg-mac-fill" : "text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill"}`}>
            <PanelRight size={15} strokeWidth={2} />
          </button>
          <button onClick={() => setMinimized(true)} title="Minimize to a small bar"
            className="h-7 w-7 grid place-items-center rounded-[9px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors">
            <Minus size={15} strokeWidth={2} />
          </button>
          <button onClick={() => setOpen(false)} title="Close (Esc)"
            className="h-7 w-7 grid place-items-center rounded-[9px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors">
            <X size={15} strokeWidth={2} />
          </button>
        </div>

        {/* Messages — scroll above, composer pinned below */}
        <div ref={scrollRef} className="flex-1 min-h-0 overflow-auto px-4 py-4 space-y-3">
          {msgs.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-center gap-4 px-2">
              <div className="h-12 w-12 rounded-2xl grid place-items-center bg-mac-fill border border-mac-stroke shadow-mac">
                <Sparkles size={22} strokeWidth={2} className="text-mac-accentHi" />
              </div>
              <p className="text-[13px] text-mac-ink2 leading-relaxed max-w-[40ch]">
                I'm Himmy — ask about your papers, your day, or the wider world. For a thorough,
                cited answer, hit <span className="inline-flex items-center gap-1 text-mac-ink"><Telescope size={12} strokeWidth={2} />Deep research</span>.
              </p>
              <div className="flex flex-wrap justify-center gap-2">
                {CHIPS.map((c) => (
                  <button key={c} onClick={() => send(c)}
                    className="text-[12.5px] text-mac-ink2 bg-mac-fill border border-mac-stroke rounded-full px-3 py-1.5 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">
                    {c}
                  </button>
                ))}
              </div>
            </div>
          ) : msgs.map((m, i) => (
            <Bubble key={i} m={m} index={i} busy={busy} onDecide={decide}
              trace={i === msgs.length - 1 ? trace : null} onSend={send} />
          ))}
        </div>

        {/* Composer — pinned at the bottom, iMessage-style. Drop a file anywhere on it to attach. */}
        <div className="shrink-0 border-t border-mac-stroke p-2.5"
          onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); setDragOver(true); }}
          onDragLeave={(e) => { e.preventDefault(); setDragOver(false); }}
          onDrop={(e) => {
            e.preventDefault(); e.stopPropagation(); setDragOver(false);
            if (e.dataTransfer?.files?.length) uploadFiles(e.dataTransfer.files);
          }}>
          {/* Attached-file chips (+ an uploading shimmer) — what this next message will carry. */}
          {(pendingFiles.length > 0 || uploading) && (
            <div className="flex flex-wrap gap-1.5 mb-2 px-0.5">
              {pendingFiles.map((f) => (
                <div key={f.id}
                  className="group flex items-center gap-1.5 max-w-[200px] h-7 pl-2 pr-1 rounded-lg bg-mac-fillHi border border-mac-stroke text-[11.5px] text-mac-ink2">
                  <span className="text-mac-accentHi shrink-0">{fileKindIcon(f.kind, 12)}</span>
                  <span className="truncate">{f.name}</span>
                  <span className="shrink-0 text-mac-ink3 tabular-nums">
                    {f.empty ? "unread" : `${(f.chars / 1000).toFixed(f.chars < 1000 ? 0 : 1)}k`}
                  </span>
                  <button onClick={() => removePending(f.id)} title="Remove"
                    className="shrink-0 h-5 w-5 grid place-items-center rounded text-mac-ink3 hover:text-mac-red hover:bg-mac-fill transition-colors">
                    <X size={12} strokeWidth={2.5} />
                  </button>
                </div>
              ))}
              {uploading && (
                <div className="flex items-center gap-1.5 h-7 px-2 rounded-lg bg-mac-fill border border-mac-stroke text-[11.5px] text-mac-ink3">
                  <Loader2 size={12} className="animate-spin" /> Reading…
                </div>
              )}
            </div>
          )}
          <div className={`flex items-end gap-1.5 rounded-2xl bg-mac-fill border pl-1.5 pr-1.5 py-1.5 transition-colors ${dragOver ? "border-mac-accent border-dashed" : "border-mac-stroke focus-within:border-mac-accent"}`}>
            <input ref={fileInputRef} type="file" multiple className="hidden"
              onChange={(e) => { if (e.target.files) uploadFiles(e.target.files); e.target.value = ""; }} />
            <button onClick={() => fileInputRef.current?.click()} disabled={busy}
              title="Attach a file, image or voice note — Himmy will read it"
              className="shrink-0 h-8 w-8 grid place-items-center rounded-full text-mac-ink3 disabled:opacity-30 enabled:hover:text-mac-accentHi enabled:hover:bg-mac-fillHi transition-colors">
              <Paperclip size={16} strokeWidth={2} />
            </button>
            <button onClick={() => deepResearch(input)} disabled={busy || !input.trim()}
              title="Deep research — plan, search your library + the web, write a cited brief (slower)"
              className="shrink-0 h-8 w-8 grid place-items-center rounded-full text-mac-ink3 disabled:opacity-30 enabled:hover:text-mac-accentHi enabled:hover:bg-mac-fillHi transition-colors">
              <Telescope size={16} strokeWidth={2} />
            </button>
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }}
              rows={1}
              autoFocus
              placeholder={dragOver ? "Drop your file here…" : "Ask Himmy anything…"}
              className="flex-1 resize-none bg-transparent text-[14.5px] outline-none placeholder:text-mac-ink3 leading-6 max-h-28 py-1"
            />
            {busy ? (
              <button onClick={stop} title="Stop Himmy"
                className="shrink-0 h-8 w-8 grid place-items-center rounded-full bg-mac-fillHi text-mac-ink2 hover:text-mac-ink hover:bg-mac-fillHi ring-1 ring-inset ring-mac-strokeHi transition-colors">
                <Square size={13} strokeWidth={2.5} className="fill-current" />
              </button>
            ) : (
              <button onClick={() => send(input)} disabled={!input.trim() && pendingFiles.length === 0}
                className="shrink-0 h-8 w-8 grid place-items-center rounded-full bg-mac-accent text-white disabled:opacity-25 enabled:hover:bg-mac-accentHi transition-colors">
                <ArrowUp size={16} strokeWidth={2.5} />
              </button>
            )}
          </div>
          <div className="mt-1 px-1 text-[10.5px] text-mac-ink3">Enter to send · Shift+Enter for a new line · 📎 or drop a file to attach</div>
        </div>

        {/* History overlay — same in floating + docked modes */}
        {showHistory && (
          <div className="absolute inset-0 z-10 flex flex-col bg-[rgba(30,31,37,0.985)] backdrop-blur-xl himmy-enter-center">
            <div className="shrink-0 flex items-center justify-between h-[46px] px-3 border-b border-mac-stroke">
              <span className="text-[13px] font-display font-medium text-mac-ink2">History</span>
              <div className="flex items-center gap-1">
                <button onClick={newChat} title="New chat"
                  className="h-7 w-7 grid place-items-center rounded-[9px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors">
                  <SquarePen size={15} strokeWidth={2} />
                </button>
                <button onClick={() => setShowHistory(false)} title="Back to chat"
                  className="h-7 w-7 grid place-items-center rounded-[9px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fill transition-colors">
                  <X size={15} strokeWidth={2} />
                </button>
              </div>
            </div>
            <div className="flex-1 overflow-auto py-2 px-2 space-y-0.5">
              {sessions.length === 0 ? (
                <p className="px-2 py-3 text-[12px] text-mac-ink3 leading-relaxed">No saved chats yet.</p>
              ) : sessions.map((s) => (
                <button key={s.session_id} onClick={() => resume(s.session_id)}
                  className={`group w-full text-left rounded-[9px] px-2.5 py-2 transition-colors ${s.session_id === sessionId ? "bg-mac-fillHi" : "hover:bg-mac-fill"}`}>
                  <div className="flex items-center gap-1.5">
                    <MessageSquare size={12} strokeWidth={2} className="text-mac-ink3 shrink-0" />
                    <span className="flex-1 truncate text-[12.5px] text-mac-ink">{s.title}</span>
                    <span onClick={(e) => removeSession(s.session_id, e)}
                      className="opacity-0 group-hover:opacity-100 text-mac-ink3 hover:text-mac-red transition-opacity">
                      <Trash2 size={12} strokeWidth={2} />
                    </span>
                  </div>
                  <div className="mt-0.5 pl-[18px] text-[10.5px] text-mac-ink3">{s.message_count} messages</div>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// The "deep research is running" placeholder — distinct from a normal streaming bubble so
// the user knows this is the slower, multi-step path (it can take 60–150s).
function ResearchLoading() {
  return (
    <div className="flex justify-start">
      <div className="w-full max-w-[88%] rounded-2xl px-3.5 py-3 bg-mac-fill border border-mac-stroke">
        <div className="flex items-center gap-2 text-mac-accentHi">
          <Telescope size={15} strokeWidth={2} className="animate-pulse" />
          <span className="text-[13px] font-display text-mac-ink">Deep research in progress…</span>
        </div>
        <p className="mt-1.5 text-[12px] text-mac-ink3 leading-relaxed">
          Planning, searching your library and the web in parallel, then writing a cited
          brief. This is the thorough path — it can take a minute or two.
        </p>
        <div className="mt-2 flex gap-1">
          <span className="h-1.5 w-1.5 rounded-full bg-mac-ink3 animate-bounce [animation-delay:-0.2s]" />
          <span className="h-1.5 w-1.5 rounded-full bg-mac-ink3 animate-bounce [animation-delay:-0.1s]" />
          <span className="h-1.5 w-1.5 rounded-full bg-mac-ink3 animate-bounce" />
        </div>
      </div>
    </div>
  );
}

// The finished deep-research result: the plan/steps, the cited synthesis, then the sources.
function ResearchCard({ r }: { r: ResearchResult }) {
  return (
    <div className="flex justify-start">
      <div className="w-full max-w-[92%] rounded-2xl bg-mac-fill border border-mac-stroke overflow-hidden">
        <div className="flex items-center gap-2 px-3.5 py-2.5 border-b border-mac-stroke">
          <Telescope size={15} strokeWidth={2} className="text-mac-accentHi" />
          <span className="text-[12.5px] font-display text-mac-ink">Deep research</span>
        </div>

        {r.steps.length > 0 && (
          <div className="px-3.5 pt-3">
            <div className="flex items-center gap-1.5 text-[11px] font-medium text-mac-ink3 uppercase tracking-wide">
              <ListChecks size={13} strokeWidth={2} /> Plan
            </div>
            <ol className="mt-1.5 space-y-1">
              {r.steps.map((s, i) => (
                <li key={i} className="flex gap-2 text-[12.5px] text-mac-ink2 leading-relaxed">
                  <span className="shrink-0 text-mac-ink3 tabular-nums">{i + 1}.</span>
                  <span>{s}</span>
                </li>
              ))}
            </ol>
          </div>
        )}

        <div className="px-3.5 pt-3">
          <div className="flex items-center gap-1.5 text-[11px] font-medium text-mac-ink3 uppercase tracking-wide">
            <BookText size={13} strokeWidth={2} /> Brief
          </div>
          <div className="mt-1.5 text-[13.5px] text-mac-ink leading-relaxed whitespace-pre-wrap">
            {r.brief}
          </div>
        </div>

        {r.sources.length > 0 && (
          <div className="px-3.5 py-3 mt-2 border-t border-mac-stroke">
            <div className="flex items-center gap-1.5 text-[11px] font-medium text-mac-ink3 uppercase tracking-wide">
              <Link2 size={13} strokeWidth={2} /> Sources ({r.sources.length})
            </div>
            <ul className="mt-1.5 space-y-1">
              {r.sources.map((s, i) => (
                <li key={i} className="flex gap-2 text-[12px] text-mac-ink2 leading-relaxed break-words">
                  <Circle size={5} strokeWidth={3} className="mt-1.5 shrink-0 text-mac-ink3 fill-current" />
                  <span>{s}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {r.sources.length === 0 && <div className="pb-3" />}
      </div>
    </div>
  );
}

function TypingDots() {
  return (
    <span className="inline-flex items-end gap-1 py-1.5 text-mac-ink3">
      <span className="himmy-typing-dot" style={{ animationDelay: "0ms" }} />
      <span className="himmy-typing-dot" style={{ animationDelay: "180ms" }} />
      <span className="himmy-typing-dot" style={{ animationDelay: "360ms" }} />
    </span>
  );
}

function Bubble({ m, index, busy, onDecide, trace, onSend }: {
  m: Msg; index?: number; busy?: boolean;
  onDecide?: (msgIndex: number, checkpointId: string, approved: boolean) => void;
  trace?: string | null;          // live tool-trace label for the streaming bubble (last message)
  onSend?: (text: string) => void; // tapping a follow-up chip sends it as a new turn
}) {
  const mine = m.who === "you";
  const empty = !m.text && m.streaming && !m.approval;
  if (m.researching) return <ResearchLoading />;
  if (m.research) return <ResearchCard r={m.research} />;
  // Rich connector cards drawn from the typed, server-redacted tool_results. Known connectors
  // (flights/buses/food/weather) get a premium inline card; unknown tools fall back to markdown.
  const cards = !mine && !m.approval ? renderToolCards(m.tool_results) : [];
  // Deterministic, capability-aware next-step chips derived from what ran (no LLM). Only on a
  // settled assistant reply — never while streaming or on an approval prompt.
  const chips = !mine && !m.streaming && !m.approval ? followupChips(m.tool_results) : [];
  return (
    <div className={`flex flex-col himmy-bubble-in ${mine ? "items-end" : "items-start"}`}>
      <div className={`max-w-[88%] rounded-2xl px-3.5 py-2.5 text-[13.5px] leading-relaxed ${
        mine ? "bg-mac-accent text-white whitespace-pre-wrap" : "bg-mac-fill border border-mac-stroke text-mac-ink"}`}>
        {empty ? (trace ? <TraceLine label={trace} /> : <TypingDots />)
          : mine ? m.text
          : <ChatMarkdown text={m.text} />}
        {/* Rich cards sit above the bubble's tool-name footer, below the prose. */}
        {cards.length > 0 && <div className="mt-2.5 space-y-2">{cards}</div>}
        {m.approval && (
          <ApprovalCard a={m.approval} busy={!!busy}
            onApprove={() => index !== undefined && onDecide?.(index, m.approval!.checkpointId, true)}
            onCancel={() => index !== undefined && onDecide?.(index, m.approval!.checkpointId, false)} />
        )}
        {m.tools && m.tools.length > 0 && !m.approval && (
          <div className={`mt-1.5 text-[11px] ${mine ? "text-white/60" : "text-mac-ink3"}`}>
            {m.tools.join(" · ")}
          </div>
        )}
        {/* Grounding badge — only on a settled assistant answer with text. Pure function of the
            tool_results: green when a real data/retrieval tool fired, muted "best guess" otherwise. */}
        {!mine && !m.streaming && !m.approval && !!m.text && (
          <div className="mt-1.5"><GroundingBadge results={m.tool_results} /></div>
        )}
      </div>
      {/* Follow-up chips — same pill language as the empty-state suggestions. */}
      {chips.length > 0 && onSend && (
        <div className="mt-2 flex flex-wrap gap-2 max-w-[88%]">
          {chips.map((c) => (
            <button key={c.label} onClick={() => onSend(c.prompt)}
              className="text-[12px] text-mac-ink2 bg-mac-fill border border-mac-stroke rounded-full px-3 py-1.5 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">
              {c.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// The live tool-trace line that replaces the silent typing dots while a turn streams: a pulsing
// dot + the doxing-safe human label coming over SSE ("Looking up flights…", "Checking your tasks…").
function TraceLine({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-2 py-1 text-[12.5px] text-mac-ink2">
      <span className="relative flex h-2 w-2 shrink-0">
        <span className="absolute inline-flex h-full w-full rounded-full bg-mac-accentHi opacity-60 animate-ping" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-mac-accentHi" />
      </span>
      <span>{label}…</span>
    </span>
  );
}

/* ── rich connector cards ──────────────────────────────────────────────────
   Draw the typed, SERVER-REDACTED tool_results as premium inline cards in the chat. Known
   connectors render a tailored card (reusing FlightRow / BusRow / the weather strip pieces);
   anything unknown is skipped here and left to the markdown reply, so the chat never breaks. */
// The set of tools that pull REAL data/retrieval — when one of these actually fired, the answer is
// grounded in fetched facts rather than the model's own recall. A pure allow-list (no LLM); the
// grounding badge below is a function of nothing but which of these ran.
const GROUNDING_TOOLS = new Set<string>([
  "ask_papers", "web_search", "nepse_price", "nrb_forex", "air_quality",
  "weather_forecast", "buddha_air_flights", "bussewa_buses",
]);

// True when at least one retrieval/data tool in GROUNDING_TOOLS ran on this turn. We ignore the
// tool's own ok/result shape — that it *fired* is enough to say the answer was built over fetched
// data (a failed fetch still means the model wasn't free-styling; weather* matches its variants).
function isGrounded(results: ToolResult[] | undefined): boolean {
  if (!results || results.length === 0) return false;
  return results.some((tr) => GROUNDING_TOOLS.has(tr.tool_name) || tr.tool_name.startsWith("weather"));
}

// A small pill under a settled assistant answer: GREEN "grounded" when a real data/retrieval tool
// fired, else a muted "best guess". Premium SF Pro / dark — mirrors the chip/pill language.
function GroundingBadge({ results }: { results: ToolResult[] | undefined }) {
  const grounded = isGrounded(results);
  return (
    <span
      title={grounded
        ? "Grounded in data Himmy looked up for this answer."
        : "No data tool ran — this is the model's best guess from what it already knows."}
      className={`inline-flex items-center gap-1 text-[10.5px] font-medium rounded-full px-2 py-0.5 ${
        grounded
          ? "text-mac-green bg-[rgba(48,209,88,0.12)]"
          : "text-mac-ink3 bg-mac-fill border border-mac-stroke"}`}>
      {grounded ? <ShieldCheck size={10} strokeWidth={2.5} /> : <Sparkle size={10} strokeWidth={2.5} />}
      {grounded ? "grounded" : "best guess"}
    </span>
  );
}

function renderToolCards(results: ToolResult[] | undefined): React.ReactNode[] {
  if (!results || results.length === 0) return [];
  const out: React.ReactNode[] = [];
  results.forEach((tr, i) => {
    const r: any = tr.result;
    // A redacted/failed/empty connector result is not card-worthy → fall back to the prose.
    if (!r || typeof r !== "object" || r.ok === false) return;
    switch (tr.tool_name) {
      case "buddha_air_flights":
        if (Array.isArray(r.flights) && r.flights.length) out.push(<FlightCard key={`fl-${i}`} d={r as DoFlights} />);
        break;
      case "bussewa_buses":
        if (Array.isArray(r.buses) && r.buses.length) out.push(<BusCard key={`bs-${i}`} d={r as DoBuses} />);
        break;
      case "foodmandu_search":
        if (Array.isArray(r.restaurants) && r.restaurants.length) out.push(<FoodSearchCard key={`fs-${i}`} r={r} />);
        break;
      case "foodmandu_menu":
        if (Array.isArray(r.categories) && r.categories.length) out.push(<FoodMenuCard key={`fm-${i}`} r={r as DoRestaurant} />);
        break;
      case "weather_forecast":
        if (r.summary || (Array.isArray(r.daily) && r.daily.length)) out.push(<WeatherCard key={`wx-${i}`} w={r as DoWeather} />);
        break;
      case "nepse_price":
        if (typeof r.price === "number") out.push(<NepseCard key={`np-${i}`} d={r as DoNepse} />);
        break;
      case "nrb_forex":
        if (Array.isArray(r.rates) && r.rates.length) out.push(<ForexCard key={`fx-${i}`} d={r as DoForex} />);
        break;
      case "air_quality":
        if (typeof r.us_aqi === "number") out.push(<AqiCard key={`aq-${i}`} d={r as DoAqi} />);
        break;
      default:
        break; // unknown tool → markdown fallback (no card)
    }
  });
  return out;
}

// Compact flight card: cheapest fare badge, cheapest outbound leg (reusing FlightRow), the
// round-trip total when present, and a Book deep-link from result.booking_link.
function FlightCard({ d }: { d: DoFlights }) {
  const cheapest = d.cheapest || d.flights?.[0];
  const rt = d.round_trip_total_npr;
  return (
    <div className="rounded-xl border border-mac-stroke bg-mac-fillHi overflow-hidden">
      <div className="px-3 pt-2.5 pb-2 flex items-center gap-2">
        <div className="h-7 w-7 shrink-0 grid place-items-center rounded-lg bg-[rgba(10,132,255,0.12)]">
          {d.round_trip ? <ArrowRightLeft size={13} className="text-mac-accentHi" /> : <Plane size={13} className="text-mac-accentHi" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[12.5px] font-medium text-mac-ink truncate">{d.from} → {d.to}<span className="text-mac-ink3 font-normal"> · Buddha Air</span></div>
          <div className="text-[10.5px] text-mac-ink3">{d.flights.length} flight{d.flights.length === 1 ? "" : "s"}{d.round_trip ? " · round trip" : ""}</div>
        </div>
        {rt ? (
          <span className="shrink-0 text-[11px] font-semibold text-mac-accentHi bg-[rgba(10,132,255,0.1)] rounded-full px-2 py-0.5">round trip Rs {Math.round(rt).toLocaleString()}</span>
        ) : cheapest ? (
          <span className="shrink-0 text-[11px] font-semibold text-mac-accentHi bg-[rgba(10,132,255,0.1)] rounded-full px-2 py-0.5">from Rs {Math.round(cheapest.fare_npr).toLocaleString()}</span>
        ) : null}
      </div>
      <div className="px-2.5 pb-2.5">
        {cheapest && <FlightRow f={cheapest} best bookLink={d.booking_link} />}
      </div>
    </div>
  );
}

// Compact bus card: cheapest fare/seats (reusing BusRow) + a Book deep-link from result.booking_link.
function BusCard({ d }: { d: DoBuses }) {
  const cheapest = d.cheapest || d.buses?.[0];
  return (
    <div className="rounded-xl border border-mac-stroke bg-mac-fillHi overflow-hidden">
      <div className="px-3 pt-2.5 pb-2 flex items-center gap-2">
        <div className="h-7 w-7 shrink-0 grid place-items-center rounded-lg bg-[rgba(10,132,255,0.12)]"><Bus size={13} className="text-mac-accentHi" /></div>
        <div className="min-w-0 flex-1">
          <div className="text-[12.5px] font-medium text-mac-ink truncate">{d.from} → {d.to}<span className="text-mac-ink3 font-normal"> · bussewa</span></div>
          <div className="text-[10.5px] text-mac-ink3">{d.count || d.buses.length} bus{(d.count || d.buses.length) === 1 ? "" : "es"}</div>
        </div>
        {cheapest && <span className="shrink-0 text-[11px] font-semibold text-mac-accentHi bg-[rgba(10,132,255,0.1)] rounded-full px-2 py-0.5">from Rs {Math.round(cheapest.fare_npr).toLocaleString()}</span>}
      </div>
      {d.via && <div className="px-3 pb-1.5 text-[10.5px] text-mac-accentHi">No direct bus — showing buses to {d.via.hub}.</div>}
      <div className="px-2.5 pb-2.5">
        {cheapest && <BusRow b={cheapest} best bookLink={d.booking_link} />}
      </div>
    </div>
  );
}

// Restaurant search card: the top match (image + rating + open-now) with an order deep-link.
function FoodSearchCard({ r }: { r: any }) {
  const top = (r.restaurants || [])[0];
  if (!top) return null;
  return (
    <div className="rounded-xl border border-mac-stroke bg-mac-fillHi overflow-hidden">
      <div className="px-3 pt-2.5 pb-1.5 flex items-center gap-1.5 text-[11px] font-semibold text-mac-accentHi">
        <UtensilsCrossed size={12} /> Foodmandu · {r.count || (r.restaurants || []).length} place{(r.count || (r.restaurants || []).length) === 1 ? "" : "s"}
      </div>
      <a href={top.order_link || "#"} target="_blank" rel="noreferrer"
        className="mx-2.5 mb-2.5 flex items-center gap-2.5 p-2 rounded-lg border border-mac-stroke bg-mac-fill hover:border-mac-strokeHi transition-colors">
        {top.image
          ? <img src={top.image} alt="" loading="lazy" onError={(e) => { e.currentTarget.style.display = "none"; }} className="h-11 w-11 shrink-0 rounded-md object-cover bg-mac-fillHi" />
          : <div className="h-11 w-11 shrink-0 rounded-md grid place-items-center bg-mac-fillHi text-mac-ink3"><UtensilsCrossed size={15} /></div>}
        <div className="min-w-0 flex-1">
          <div className="text-[12.5px] text-mac-ink font-medium leading-snug truncate">{top.name}</div>
          <div className="text-[10.5px] text-mac-ink3 truncate">
            {top.cuisine || "Restaurant"}
            {typeof top.rating === "number" && top.rating > 0 ? <> · <Star size={9} className="inline -mt-0.5 fill-amber-400 text-amber-400" /> {top.rating.toFixed(1)}</> : null}
            <span className={top.open_now ? " text-mac-green" : ""}> · {top.open_now ? "Open now" : "Closed"}</span>
          </div>
        </div>
        <span className="shrink-0 text-[11px] font-medium text-mac-accentHi inline-flex items-center gap-1">Order <ArrowUpRight size={12} strokeWidth={2.5} /></span>
      </a>
    </div>
  );
}

// Restaurant-menu card: a couple of dishes (reusing the MenuItemRow visual) + order deep-link.
function FoodMenuCard({ r }: { r: DoRestaurant }) {
  const dishes = (r.recommended && r.recommended.length ? r.recommended : (r.categories?.[0]?.items || [])).slice(0, 3);
  return (
    <div className="rounded-xl border border-mac-stroke bg-mac-fillHi overflow-hidden">
      <div className="px-3 pt-2.5 pb-2 flex items-center gap-2">
        <div className="h-7 w-7 shrink-0 grid place-items-center rounded-lg bg-[rgba(10,132,255,0.12)]"><UtensilsCrossed size={13} className="text-mac-accentHi" /></div>
        <div className="min-w-0 flex-1">
          <div className="text-[12.5px] font-medium text-mac-ink truncate">{r.restaurant}</div>
          <div className="text-[10.5px] text-mac-ink3">{r.item_count} item{r.item_count === 1 ? "" : "s"} on the menu</div>
        </div>
        <a href={r.order_link || "#"} target="_blank" rel="noreferrer"
          className="shrink-0 text-[11px] font-medium text-mac-accentHi inline-flex items-center gap-1">Order <ArrowUpRight size={12} strokeWidth={2.5} /></a>
      </div>
      <div className="px-2.5 pb-2.5 space-y-1.5">
        {dishes.map((it) => (
          <div key={it.id} className="flex items-center gap-2.5 p-2 rounded-lg border border-mac-stroke bg-mac-fill">
            {it.image
              ? <img src={it.image} alt="" loading="lazy" onError={(e) => { e.currentTarget.style.display = "none"; }} className="h-9 w-9 shrink-0 rounded-md object-cover bg-mac-fillHi" />
              : <div className="h-9 w-9 shrink-0 rounded-md grid place-items-center bg-mac-fillHi text-mac-ink3"><UtensilsCrossed size={13} /></div>}
            <div className="min-w-0 flex-1">
              <div className="text-[12px] text-mac-ink font-medium leading-snug truncate">{it.name}</div>
              {it.desc && <div className="text-[10.5px] text-mac-ink3 leading-snug truncate">{it.desc}</div>}
            </div>
            <span className="shrink-0 text-[12px] font-semibold text-mac-ink">Rs {it.price}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// Compact weather card: today's emoji + Hi/Lo + rain%, plus the honest one-line summary. Stays
// HONEST out of the forecast window — leads with the summary (seasonal pattern) and no fake days.
function WeatherCard({ w }: { w: DoWeather }) {
  const today = w.in_forecast_window && (w.daily || []).length ? w.daily[0] : null;
  const cur = w.current;
  return (
    <div className="rounded-xl border border-mac-stroke bg-mac-fillHi p-3">
      <div className="flex items-center gap-2.5">
        <div className="text-[26px] leading-none shrink-0" title={today?.label || cur?.label}>{today?.emoji || cur?.emoji || "🌤️"}</div>
        <div className="min-w-0 flex-1">
          {today ? (
            <div className="text-[13px] font-semibold text-mac-ink tnum leading-none">
              {Math.round(today.t_max)}°<span className="text-mac-ink3 font-normal"> / {Math.round(today.t_min)}°</span>
              <span className="ml-2 text-[11px] font-normal text-mac-ink3 inline-flex items-center gap-0.5"><Umbrella size={9} /> {today.rain_pct}%</span>
            </div>
          ) : cur ? (
            <div className="text-[13px] font-semibold text-mac-ink tnum leading-none">{Math.round(cur.temp_c)}°<span className="ml-2 text-[11px] font-normal text-mac-ink3">now</span></div>
          ) : (
            <div className="text-[12px] font-semibold text-mac-ink">Weather</div>
          )}
          <div className="text-[11px] text-mac-ink3 leading-snug mt-1 line-clamp-2">{w.summary}</div>
        </div>
      </div>
    </div>
  );
}

// NEPSE stock card: symbol + the big last price in NPR, a green/red change% pill, prev close + the
// Bikram-Sambat date, and a tiny last-7 close strip (sparkline-style bars) under it. Merolagani,
// already corp-action adjusted — we just display what the connector hands back. Degrades cleanly:
// the change pill / OHLCV strip only render when their data is present.
function NepseCard({ d }: { d: DoNepse }) {
  const up = (d.change ?? 0) >= 0;
  const hasChange = typeof d.change === "number" && typeof d.change_pct === "number";
  const closes = (d.ohlcv || []).map((b) => b.c).filter((c) => Number.isFinite(c));
  const lo = closes.length ? Math.min(...closes) : 0;
  const hi = closes.length ? Math.max(...closes) : 1;
  const span = hi - lo || 1;
  const ChangeIcon = up ? TrendingUp : TrendingDown;
  return (
    <div className="rounded-xl border border-mac-stroke bg-mac-fillHi overflow-hidden">
      <div className="px-3 pt-2.5 pb-2 flex items-center gap-2">
        <div className="h-7 w-7 shrink-0 grid place-items-center rounded-lg bg-mac-accentDim"><TrendingUp size={13} className="text-mac-accentHi" /></div>
        <div className="min-w-0 flex-1">
          <div className="text-[12.5px] font-semibold text-mac-ink truncate">{d.symbol}<span className="text-mac-ink3 font-normal"> · NEPSE</span></div>
          <div className="text-[10.5px] text-mac-ink3">{d.source || "Merolagani"}{d.date_bs ? ` · ${d.date_bs} BS` : ""}</div>
        </div>
        {hasChange && (
          <span className={`shrink-0 inline-flex items-center gap-1 text-[11px] font-semibold rounded-full px-2 py-0.5 ${up ? "text-mac-green bg-[rgba(48,209,88,0.12)]" : "text-mac-red bg-[rgba(255,69,58,0.12)]"}`}>
            <ChangeIcon size={11} strokeWidth={2.5} />{up ? "+" : ""}{d.change_pct!.toFixed(2)}%
          </span>
        )}
      </div>
      <div className="px-3 pb-2.5">
        <div className="flex items-baseline gap-2">
          <span className="text-[24px] font-semibold text-mac-ink tnum leading-none font-display">{d.price!.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
          <span className="text-[11px] text-mac-ink3 font-medium">{d.currency || "NPR"}</span>
          {hasChange && (
            <span className={`text-[11px] tnum font-medium ${up ? "text-mac-green" : "text-mac-red"}`}>{up ? "+" : ""}{d.change!.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
          )}
        </div>
        {typeof d.prev_close === "number" && (
          <div className="text-[10.5px] text-mac-ink3 mt-1">Prev close <span className="text-mac-ink2 tnum">{d.prev_close.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span></div>
        )}
        {closes.length > 1 && (
          <div className="mt-2 flex items-end gap-[3px] h-7" title="Last 7 closes">
            {closes.map((c, i) => {
              const h = 18 * ((c - lo) / span) + 6; // 6–24px, always visible
              const last = i === closes.length - 1;
              return <div key={i} className={`flex-1 rounded-sm ${last ? (up ? "bg-mac-green" : "bg-mac-red") : "bg-mac-strokeHi"}`} style={{ height: `${h}px` }} />;
            })}
          </div>
        )}
      </div>
    </div>
  );
}

// NRB forex card: a tight buy/sell table of the top rates against NPR, with the publish date (AD +
// BS). Rates are NRB's official figures, quoted per the currency's own `unit` (INR per 100, JPY per
// 10) — we show the unit verbatim and never rescale. Caps the table so the card stays compact.
function ForexCard({ d }: { d: DoForex }) {
  const rates = (d.rates || []).slice(0, 7);
  if (!rates.length) return null;
  return (
    <div className="rounded-xl border border-mac-stroke bg-mac-fillHi overflow-hidden">
      <div className="px-3 pt-2.5 pb-2 flex items-center gap-2">
        <div className="h-7 w-7 shrink-0 grid place-items-center rounded-lg bg-mac-accentDim"><Coins size={13} className="text-mac-accentHi" /></div>
        <div className="min-w-0 flex-1">
          <div className="text-[12.5px] font-semibold text-mac-ink truncate">NRB Forex<span className="text-mac-ink3 font-normal"> · vs NPR</span></div>
          <div className="text-[10.5px] text-mac-ink3">{d.date || "latest"}{d.date_bs ? ` · ${d.date_bs} BS` : ""}</div>
        </div>
      </div>
      <div className="px-3 pb-2.5">
        <div className="grid grid-cols-[1fr_auto_auto] gap-x-3 gap-y-1 text-[11.5px]">
          <div className="text-[10px] uppercase tracking-wide text-mac-ink3 font-medium">Currency</div>
          <div className="text-[10px] uppercase tracking-wide text-mac-ink3 font-medium text-right">Buy</div>
          <div className="text-[10px] uppercase tracking-wide text-mac-ink3 font-medium text-right">Sell</div>
          {rates.map((rt) => (
            <Fragment key={rt.iso3}>
              <div className="text-mac-ink truncate font-medium">{rt.iso3}{rt.unit > 1 ? <span className="text-mac-ink3 font-normal"> ×{rt.unit}</span> : null}</div>
              <div className="text-mac-ink2 tnum text-right">{rt.buy.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
              <div className="text-mac-ink2 tnum text-right">{rt.sell.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
            </Fragment>
          ))}
        </div>
        {d.caption && <div className="text-[10px] text-mac-ink3 leading-snug mt-2">{d.caption}</div>}
      </div>
    </div>
  );
}

// Map a US AQI band to a chip color (matches the EPA category bands the connector reports).
function aqiTone(aqi: number): { text: string; bg: string } {
  if (aqi <= 50) return { text: "text-mac-green", bg: "bg-[rgba(48,209,88,0.14)]" };
  if (aqi <= 100) return { text: "text-mac-orange", bg: "bg-[rgba(255,159,10,0.14)]" };
  if (aqi <= 150) return { text: "text-mac-orange", bg: "bg-[rgba(255,159,10,0.18)]" };
  return { text: "text-mac-red", bg: "bg-[rgba(255,69,58,0.14)]" }; // Unhealthy & worse
}

// Air-quality card: the US AQI number on a category-colored chip + the plain-English advice line,
// with PM2.5/PM10 as a quiet footnote. Always renderable — the chip degrades gracefully on a low
// reading. (Failures never reach here: the branch only renders when us_aqi is a number.)
function AqiCard({ d }: { d: DoAqi }) {
  const aqi = d.us_aqi ?? 0;
  const tone = aqiTone(aqi);
  return (
    <div className="rounded-xl border border-mac-stroke bg-mac-fillHi p-3">
      <div className="flex items-center gap-2.5">
        <div className={`shrink-0 grid place-items-center rounded-xl px-2.5 py-1.5 ${tone.bg}`}>
          <div className={`text-[20px] font-semibold tnum leading-none font-display ${tone.text}`}>{aqi}</div>
          <div className="text-[8.5px] uppercase tracking-wide text-mac-ink3 mt-0.5">AQI</div>
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 text-[12.5px] font-semibold text-mac-ink">
            <Wind size={12} className="text-mac-ink3" />{d.category}
          </div>
          <div className="text-[11px] text-mac-ink3 leading-snug mt-1 line-clamp-2">{d.advice}</div>
          {(typeof d.pm2_5 === "number" || typeof d.pm10 === "number") && (
            <div className="text-[10px] text-mac-ink3 mt-1 tnum">
              {typeof d.pm2_5 === "number" ? `PM2.5 ${d.pm2_5}` : ""}
              {typeof d.pm2_5 === "number" && typeof d.pm10 === "number" ? " · " : ""}
              {typeof d.pm10 === "number" ? `PM10 ${d.pm10}` : ""}
              <span className="text-mac-ink4"> µg/m³</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Deterministic, capability-aware next-step chips derived from the tool_results (no LLM). Tapping
// a chip sends its prompt as a new turn. Dedup by label so two flight cards don't double the chips.
type FollowupChip = { label: string; prompt: string };
function followupChips(results: ToolResult[] | undefined): FollowupChip[] {
  if (!results || results.length === 0) return [];
  const chips: FollowupChip[] = [];
  const push = (c: FollowupChip) => { if (!chips.some((x) => x.label === c.label)) chips.push(c); };
  for (const tr of results) {
    const r: any = tr.result;
    if (!r || typeof r !== "object" || r.ok === false) continue;
    const from = r.from ? String(r.from) : "";
    const to = r.to ? String(r.to) : "";
    const route = from && to ? ` ${from} to ${to}` : "";
    switch (tr.tool_name) {
      case "buddha_air_flights":
        push({ label: "Find a bus instead", prompt: `Find a bus${route}` });
        if (to) push({ label: `Plan a 2-day trip to ${to}`, prompt: `Plan a 2-day trip to ${to}` });
        break;
      case "bussewa_buses":
        push({ label: "Check flights instead", prompt: `Find flights${route}` });
        if (to) push({ label: `Plan a 2-day trip to ${to}`, prompt: `Plan a 2-day trip to ${to}` });
        break;
      case "foodmandu_search":
      case "foodmandu_menu": {
        const place = r.restaurant ? String(r.restaurant) : ((r.restaurants || [])[0]?.name || "");
        if (place && tr.tool_name === "foodmandu_search") push({ label: `See ${place}'s menu`, prompt: `Show me the menu at ${place}` });
        push({ label: "More options nearby", prompt: "Show me a few more restaurants nearby" });
        break;
      }
      case "weather_forecast": {
        const place = r.summary && /in\s+([^,.]+)/i.exec(String(r.summary))?.[1];
        push({ label: place ? `Plan a trip to ${place.trim()}` : "Plan a trip there", prompt: place ? `Plan a 2-day trip to ${place.trim()}` : "Plan a 2-day trip there" });
        push({ label: "What should I pack?", prompt: "What should I pack for this weather?" });
        break;
      }
      default:
        break;
    }
  }
  return chips.slice(0, 3); // keep it calm — at most three next-steps
}

// A gated tool call, resolved into a typed view the approval card can render richly.
type PendingView =
  | { kind: "calendar"; verb: string; summary: string; start?: any; end?: any; location?: any; allDay?: boolean; repeats?: boolean }
  | { kind: "mail"; to: string; subject: string; body: string }
  | { kind: "delete"; what: string }
  | { kind: "generic"; label: string; detail: string };

function pendingView(p: Pending): PendingView {
  const a = p.args || {};
  switch (p.tool_name) {
    case "mail_send":
      return { kind: "mail", to: String(a.to || "—"), subject: String(a.subject || ""), body: String(a.body || "") };
    case "calendar_add":
      return { kind: "calendar", verb: "Add to your calendar", summary: String(a.summary || "Untitled event"), start: a.start, end: a.end, location: a.location, allDay: !!a.all_day, repeats: Array.isArray(a.recurrence) && a.recurrence.length > 0 };
    case "calendar_edit":
      return { kind: "calendar", verb: "Change this event", summary: String(a.summary || "This event"), start: a.start, end: a.end, location: a.location, allDay: !!a.all_day };
    case "calendar_remove":
      return { kind: "delete", what: a.recurring_event_id ? "this repeating event — every occurrence" : "this calendar event" };
    default:
      return { kind: "generic", label: p.tool_name.replace(/_/g, " "), detail: JSON.stringify(a).slice(0, 200) };
  }
}

// "JUN" + "23" badge for a calendar date (empty strings if unparseable).
function dayBadge(s: any): { m: string; d: string } {
  const dt = new Date(String(s || ""));
  if (isNaN(dt.getTime())) return { m: "", d: "" };
  return { m: dt.toLocaleDateString([], { month: "short" }).toUpperCase(), d: String(dt.getDate()) };
}
// "1:00 – 2:00 PM" style range; falls back gracefully when end is missing.
function clockRange(start: any, end: any, allDay?: boolean): string {
  const s = new Date(String(start || ""));
  if (isNaN(s.getTime())) return "";
  const wd = s.toLocaleDateString([], { weekday: "short" });
  if (allDay) return `${wd} · all day`;
  const opt = { hour: "numeric", minute: "2-digit" } as const;
  const e = new Date(String(end || ""));
  const t = s.toLocaleTimeString([], opt) + (isNaN(e.getTime()) ? "" : ` – ${e.toLocaleTimeString([], opt)}`);
  return `${wd} · ${t}`;
}

function ApprovalCard({ a, busy, onApprove, onCancel }: {
  a: ApprovalState; busy: boolean; onApprove: () => void; onCancel: () => void;
}) {
  const resolved = a.status !== "pending";
  const approved = a.status === "approved";
  const views = (a.pending || []).map(pendingView);
  const destructive = views.some((v) => v.kind === "delete");
  const tint = destructive ? "text-mac-red" : "text-mac-accentHi";

  return (
    <div className={`mt-2 rounded-xl overflow-hidden border bg-mac-fillHi shadow-pop transition-all duration-200
      ${destructive ? "border-mac-red/35" : "border-mac-accent/35"} ${resolved ? "opacity-70" : ""}`}>
      {/* header strip — tinted by action type, with a live "waiting on you" pulse */}
      <div className={`px-3.5 h-8 flex items-center gap-2 ${destructive ? "bg-mac-red/10" : "bg-mac-accent/10"}`}>
        <ShieldCheck size={13} className={tint} />
        <span className={`text-[10.5px] font-semibold uppercase tracking-[0.07em] ${tint}`}>
          {resolved ? (approved ? "Approved" : "Cancelled") : "Needs your approval"}
        </span>
        {!resolved && <span className={`ml-auto h-1.5 w-1.5 rounded-full animate-pulse ${destructive ? "bg-mac-red" : "bg-mac-accentHi"}`} />}
      </div>

      {/* body — one rich block per pending action */}
      <div className="px-3.5 py-3 space-y-3">
        {views.map((v, i) => {
          if (v.kind === "calendar") {
            const md = dayBadge(v.start);
            const when = clockRange(v.start, v.end, v.allDay);
            return (
              <div key={i}>
                <div className="text-[11px] text-mac-ink3 mb-1.5">{v.verb}</div>
                <div className="flex items-start gap-3">
                  {md.d ? (
                    <div className="shrink-0 w-11 rounded-lg border border-mac-stroke overflow-hidden text-center bg-mac-fill">
                      <div className="bg-mac-accent/15 text-mac-accentHi text-[9px] font-semibold tracking-wide py-[3px]">{md.m}</div>
                      <div className="font-display text-[17px] font-semibold leading-none py-1.5 text-mac-ink">{md.d}</div>
                    </div>
                  ) : (
                    <div className="shrink-0 h-9 w-9 rounded-lg bg-mac-accentDim grid place-items-center"><Calendar size={16} className="text-mac-accentHi" /></div>
                  )}
                  <div className="min-w-0 flex-1 pt-0.5">
                    <div className="text-[14px] text-mac-ink font-medium leading-snug">{v.summary}</div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12px] text-mac-ink2">
                      {when && <span className="inline-flex items-center gap-1"><Clock size={11} />{when}</span>}
                      {v.location && <span className="inline-flex items-center gap-1 min-w-0"><MapPin size={11} className="shrink-0" /><span className="truncate">{String(v.location)}</span></span>}
                      {v.repeats && <span className="inline-flex items-center gap-1"><Repeat size={11} />repeats</span>}
                    </div>
                  </div>
                </div>
              </div>
            );
          }
          if (v.kind === "mail") {
            return (
              <div key={i}>
                <div className="text-[11px] text-mac-ink3 mb-1.5">Send this email</div>
                <div className="flex items-start gap-3">
                  <div className="shrink-0 h-9 w-9 rounded-lg bg-mac-accentDim grid place-items-center"><Mail size={16} className="text-mac-accentHi" /></div>
                  <div className="min-w-0 flex-1">
                    <div className="text-[12.5px] text-mac-ink2 truncate"><span className="text-mac-ink3">To </span>{v.to}</div>
                    {v.subject && <div className="text-[14px] text-mac-ink font-medium leading-snug mt-0.5">{v.subject}</div>}
                  </div>
                </div>
                {v.body && (
                  <div className="mt-2 rounded-lg bg-mac-fill border border-mac-stroke px-3 py-2 text-[12px] text-mac-ink2 leading-snug whitespace-pre-wrap line-clamp-4">{v.body}</div>
                )}
              </div>
            );
          }
          if (v.kind === "delete") {
            return (
              <div key={i} className="flex items-center gap-3">
                <div className="shrink-0 h-9 w-9 rounded-lg bg-mac-red/15 grid place-items-center"><Trash2 size={16} className="text-mac-red" /></div>
                <div className="min-w-0">
                  <div className="text-[14px] text-mac-ink font-medium leading-snug">Delete {v.what}</div>
                  <div className="text-[12px] text-mac-ink3 mt-0.5">This can't be undone.</div>
                </div>
              </div>
            );
          }
          return (
            <div key={i}>
              <div className="text-[13px] text-mac-ink font-medium capitalize">{v.label}</div>
              <div className="text-[12px] text-mac-ink2 mt-0.5 break-words">{v.detail}</div>
            </div>
          );
        })}
      </div>

      {/* footer — primary action takes the width; resolved state shows a quiet receipt */}
      {resolved ? (
        <div className="px-3.5 py-2 border-t border-mac-stroke flex items-center gap-1.5 text-[12px] font-medium">
          {approved
            ? <span className="text-mac-green inline-flex items-center gap-1.5"><Check size={13} /> Done</span>
            : <span className="text-mac-ink3 inline-flex items-center gap-1.5"><X size={13} /> Cancelled</span>}
        </div>
      ) : (
        <div className="px-3 pb-3 pt-0.5 flex items-center gap-2">
          <button onClick={onCancel} disabled={busy}
            className="h-9 px-4 rounded-[10px] text-[13px] text-mac-ink2 hover:text-mac-ink border border-mac-stroke hover:border-mac-strokeHi transition-colors disabled:opacity-50">Cancel</button>
          <button onClick={onApprove} disabled={busy}
            className={`h-9 flex-1 rounded-[10px] text-[13px] font-medium text-white transition-all disabled:opacity-50 inline-flex items-center justify-center gap-1.5
              ${destructive ? "bg-mac-red hover:brightness-110" : "bg-mac-accent hover:bg-mac-accentHi"}`}>
            <Check size={15} strokeWidth={2.5} /> {destructive ? "Delete" : "Approve"}
          </button>
        </div>
      )}
    </div>
  );
}
