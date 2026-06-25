// WeekGrid — a unified 7-day time-grid where you drag an unscheduled task onto the
// calendar to time-block it. Calendar events render as positioned blocks; tasks that have
// been scheduled show up as accent-tinted task-blocks. Dropping a task creates a calendar
// event AND stamps the task's scheduled_start/end + event_id (so the two stay linked).
import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, Loader2 } from "lucide-react";
import { api, type CalendarEvent, type Task } from "./lib/api";

// Visible working-hours window. Rows are one hour tall.
const DAY_START = 7;   // 7:00
const DAY_END = 21;    // 21:00 (last row label)
const HOURS = Array.from({ length: DAY_END - DAY_START + 1 }, (_, i) => DAY_START + i);
const ROW_H = 44;      // px per hour
const GRID_H = (DAY_END - DAY_START) * ROW_H;

// Match dayKey / WEEKDAYS in App.tsx (week starts Sunday).
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function pad(n: number): string { return String(n).padStart(2, "0"); }
function fmtHour(h: number): string {
  const ampm = h < 12 ? "AM" : "PM";
  const hr = h % 12 === 0 ? 12 : h % 12;
  return `${hr} ${ampm}`;
}
function fmtTime(raw: string): string {
  if (!raw || !raw.includes("T")) return "";
  const d = new Date(raw);
  return isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

// Fractional hour-of-day for a wall-clock-ish ISO string (local). Returns null if unparseable
// or if it's an all-day (date-only) value.
function hourOfDay(raw: string): number | null {
  if (!raw || !raw.includes("T")) return null;
  const d = new Date(raw);
  if (isNaN(d.getTime())) return null;
  return d.getHours() + d.getMinutes() / 60;
}

type Positioned = { e: CalendarEvent; top: number; height: number; isTask: boolean };

// Lay out one day's events into top/height within the visible window, clamped to the grid.
function layoutDay(events: CalendarEvent[], taskEventIds: Set<string>): Positioned[] {
  const out: Positioned[] = [];
  for (const e of events) {
    if (!e.start || !e.start.includes("T")) continue; // skip all-day here (shown as a strip)
    const sh = hourOfDay(e.start);
    if (sh == null) continue;
    let eh = hourOfDay(e.end);
    if (eh == null || eh <= sh) eh = sh + 1; // default / guard against bad end
    const top = (Math.max(sh, DAY_START) - DAY_START) * ROW_H;
    const bottom = (Math.min(eh, DAY_END) - DAY_START) * ROW_H;
    const height = Math.max(18, bottom - top);
    out.push({ e, top, height, isTask: !!(e.id && taskEventIds.has(e.id)) });
  }
  return out.sort((a, b) => a.top - b.top);
}

export default function WeekGrid({ weekAnchor, onEdit, onMutated, refreshKey = 0 }: {
  // Any date inside the week to show (we snap to that week's Sunday).
  weekAnchor: Date;
  // Edit an existing calendar event (reuses CalendarTab's editor flow).
  onEdit: (e: CalendarEvent) => void;
  // After we create an event / schedule a task, ask the host to refresh its buses.
  onMutated: () => void;
  // Bumped by the host on external calendar/task mutations → triggers a reload.
  refreshKey?: number;
}) {
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dropHint, setDropHint] = useState<{ key: string; hour: number } | null>(null);
  const [, forceTick] = useState(0);

  // The week's Sunday → Saturday days.
  const weekStart = useMemo(() => {
    const d = new Date(weekAnchor.getFullYear(), weekAnchor.getMonth(), weekAnchor.getDate());
    d.setDate(d.getDate() - d.getDay());
    return d;
  }, [weekAnchor]);
  const days = useMemo(
    () => Array.from({ length: 7 }, (_, i) => { const d = new Date(weekStart); d.setDate(weekStart.getDate() + i); return d; }),
    [weekStart],
  );

  const load = async () => {
    setLoading(true); setError(null);
    try {
      const min = new Date(weekStart);
      const max = new Date(weekStart); max.setDate(max.getDate() + 7);
      const [ev, ts] = await Promise.all([
        api.calendar.range(min.toISOString(), max.toISOString()),
        api.tasks.list().catch(() => ({ tasks: [] as Task[] })),
      ]);
      if ((ev as any).message) setError((ev as any).message);
      setEvents(ev.events || []);
      setTasks(ts.tasks || []);
    } catch (e: any) { setError(e?.message || "Couldn't load this week."); }
    finally { setLoading(false); }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [weekStart.getTime(), refreshKey]);

  // Tick the "now" line every minute so it drifts down through the day.
  useEffect(() => { const t = setInterval(() => forceTick((n) => n + 1), 60_000); return () => clearInterval(t); }, []);

  // Tasks already linked to a calendar event (so we can style those event-blocks as tasks).
  const taskEventIds = useMemo(() => {
    const s = new Set<string>();
    tasks.forEach((t) => { if (t.event_id) s.add(t.event_id); });
    return s;
  }, [tasks]);

  // Open tasks with no time-block yet → the draggable "Unscheduled" rail.
  const unscheduled = useMemo(
    () => tasks.filter((t) => !t.done && !t.scheduled_start),
    [tasks],
  );

  // Events grouped by day-key, plus the all-day strip per day.
  const { byDay, allDayByDay } = useMemo(() => {
    const m: Record<string, CalendarEvent[]> = {};
    const ad: Record<string, CalendarEvent[]> = {};
    for (const e of events) {
      if (!e.start) continue;
      if (!e.start.includes("T")) { (ad[e.start.slice(0, 10)] ||= []).push(e); continue; }
      const d = new Date(e.start);
      if (isNaN(d.getTime())) continue;
      (m[dayKey(d)] ||= []).push(e);
    }
    return { byDay: m, allDayByDay: ad };
  }, [events]);

  const onDropTask = async (day: Date, hour: number, taskId: string) => {
    setDropHint(null);
    const task = tasks.find((t) => t.id === taskId);
    if (!task) return;
    const key = dayKey(day);
    const startH = Math.max(DAY_START, Math.min(DAY_END - 1, hour));
    const start = `${key}T${pad(startH)}:00:00`;
    const end = `${key}T${pad(startH + 1)}:00:00`;
    try {
      const r = await api.calendar.create({ summary: task.title, start, end });
      if (!r.ok || !r.event) { setError(r.message || "Couldn't create the time-block."); return; }
      await api.tasks.setExtras(task.id, {
        scheduled_start: start, scheduled_end: end, event_id: r.event.id || undefined,
      });
      onMutated();
      load();
    } catch (e: any) { setError(e?.message || "Couldn't schedule that task."); }
  };

  const todayKey = dayKey(new Date());
  const now = new Date();
  const nowFrac = now.getHours() + now.getMinutes() / 60;
  const nowTop = (nowFrac - DAY_START) * ROW_H;
  const nowVisible = nowFrac >= DAY_START && nowFrac <= DAY_END;

  return (
    <div className="flex gap-4 h-full min-h-0">
      {/* Unscheduled rail — drag a chip onto the grid to time-block it. */}
      <div className="shrink-0 w-[200px] flex flex-col min-h-0">
        <div className="text-[10.5px] uppercase tracking-wide text-mac-ink3 font-medium px-1 pb-2">
          Unscheduled
        </div>
        <div className="flex-1 min-h-0 overflow-auto space-y-1.5 pr-1">
          {unscheduled.length === 0 ? (
            <div className="text-[12px] text-mac-ink3 px-1 leading-snug">
              No open tasks to schedule. Drop tasks here once you add them.
            </div>
          ) : unscheduled.map((t) => (
            <div key={t.id} draggable
              onDragStart={(ev) => { ev.dataTransfer.setData("text/himmy-task", t.id); ev.dataTransfer.effectAllowed = "move"; }}
              title="Drag onto the week to time-block it"
              className="cursor-grab active:cursor-grabbing rounded-[9px] border border-mac-stroke bg-mac-fill px-2.5 py-2 text-[12.5px] text-mac-ink hover:border-mac-strokeHi transition-colors flex items-start gap-1.5">
              <CheckCircle2 size={13} className="text-mac-accentHi mt-[1px] shrink-0" />
              <span className="leading-snug line-clamp-2">{t.title}</span>
            </div>
          ))}
        </div>
      </div>

      {/* The week time-grid. */}
      <div className="flex-1 min-w-0 flex flex-col min-h-0">
        {error && (
          <div className="shrink-0 mb-2 text-[12px] text-mac-ink2 px-1">{error}</div>
        )}
        {/* Day headers */}
        <div className="shrink-0 grid pl-12" style={{ gridTemplateColumns: "repeat(7, minmax(0, 1fr))" }}>
          {days.map((d) => {
            const k = dayKey(d); const isToday = k === todayKey;
            return (
              <div key={k} className="px-1 pb-2 text-center">
                <div className="text-[10.5px] uppercase tracking-wide text-mac-ink3 font-medium">{WEEKDAYS[d.getDay()]}</div>
                <span className={`mt-0.5 inline-grid place-items-center h-6 w-6 rounded-full text-[12.5px] tnum ${isToday ? "bg-mac-accent text-white font-semibold" : "text-mac-ink2"}`}>{d.getDate()}</span>
              </div>
            );
          })}
        </div>

        {/* All-day strip (only when something is all-day this week). */}
        {days.some((d) => (allDayByDay[dayKey(d)] || []).length) && (
          <div className="shrink-0 grid pl-12 border-b border-mac-stroke pb-1.5 mb-1" style={{ gridTemplateColumns: "repeat(7, minmax(0, 1fr))" }}>
            {days.map((d) => {
              const ad = allDayByDay[dayKey(d)] || [];
              return (
                <div key={dayKey(d)} className="px-1 space-y-1">
                  {ad.map((e, j) => (
                    <button key={(e.id || "") + j} onClick={() => onEdit(e)} title={e.summary}
                      className="w-full text-left truncate rounded-md px-1.5 py-0.5 text-[10.5px] leading-tight bg-mac-fillHi border border-mac-stroke text-mac-ink2 hover:text-mac-ink transition-colors">
                      {e.summary}
                    </button>
                  ))}
                </div>
              );
            })}
          </div>
        )}

        {/* Scrolling time-grid. */}
        <div className="flex-1 min-h-0 overflow-auto">
          <div className="relative flex">
            {/* Hour labels gutter */}
            <div className="shrink-0 w-12 relative" style={{ height: GRID_H }}>
              {HOURS.slice(0, -1).map((h) => (
                <div key={h} className="absolute right-1.5 text-[10px] text-mac-ink3 tnum -translate-y-1/2"
                  style={{ top: (h - DAY_START) * ROW_H }}>{fmtHour(h)}</div>
              ))}
            </div>

            {/* Day columns */}
            <div className="flex-1 grid" style={{ gridTemplateColumns: "repeat(7, minmax(0, 1fr))" }}>
              {days.map((d) => {
                const k = dayKey(d);
                const positioned = layoutDay(byDay[k] || [], taskEventIds);
                const isToday = k === todayKey;
                return (
                  <div key={k} className="relative border-l border-mac-stroke" style={{ height: GRID_H }}>
                    {/* Hour cells (drop targets) */}
                    {HOURS.slice(0, -1).map((h) => {
                      const hinted = dropHint && dropHint.key === k && dropHint.hour === h;
                      return (
                        <div key={h}
                          onDragOver={(ev) => { ev.preventDefault(); ev.dataTransfer.dropEffect = "move"; setDropHint({ key: k, hour: h }); }}
                          onDragLeave={() => setDropHint((cur) => (cur && cur.key === k && cur.hour === h ? null : cur))}
                          onDrop={(ev) => {
                            ev.preventDefault();
                            const id = ev.dataTransfer.getData("text/himmy-task");
                            if (id) onDropTask(d, h, id);
                          }}
                          className={`absolute left-0 right-0 border-b border-mac-stroke/60 transition-colors ${hinted ? "bg-mac-accentDim" : "hover:bg-mac-fill/30"}`}
                          style={{ top: (h - DAY_START) * ROW_H, height: ROW_H }} />
                      );
                    })}

                    {/* now line */}
                    {isToday && nowVisible && (
                      <div className="absolute left-0 right-0 z-20 pointer-events-none" style={{ top: nowTop }}>
                        <div className="h-px bg-mac-red" />
                        <div className="absolute -left-1 -top-[3px] h-1.5 w-1.5 rounded-full bg-mac-red" />
                      </div>
                    )}

                    {/* event / task blocks */}
                    {positioned.map(({ e, top, height, isTask }, j) => (
                      <button key={(e.id || "") + j} onClick={() => onEdit(e)} title={e.summary}
                        style={{ top: top + 1, height: height - 2 }}
                        className={`absolute left-0.5 right-0.5 z-10 overflow-hidden rounded-[7px] px-1.5 py-0.5 text-left text-[10.5px] leading-tight border transition-colors ${
                          isTask
                            ? "bg-mac-accentDim border-mac-accent/40 text-mac-ink hover:border-mac-accent"
                            : "bg-mac-fillHi border-mac-stroke text-mac-ink hover:border-mac-strokeHi"
                        }`}>
                        <div className="flex items-center gap-1">
                          {isTask && <CheckCircle2 size={10} className="text-mac-accentHi shrink-0" />}
                          <span className="truncate font-medium">{e.summary}</span>
                        </div>
                        {height > 30 && <div className="text-mac-ink3 tnum truncate">{fmtTime(e.start)}</div>}
                      </button>
                    ))}
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {loading && events.length === 0 && (
          <div className="shrink-0 py-3 grid place-items-center text-mac-ink3"><Loader2 size={16} className="animate-spin" /></div>
        )}
      </div>
    </div>
  );
}
