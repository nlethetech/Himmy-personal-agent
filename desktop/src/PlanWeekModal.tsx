// PlanWeekModal — "Himmy, plan my week". Calls the planner (real LLM), then shows each proposed
// time-block with a checkbox (default checked) so the user can review and approve before anything
// touches their calendar. On confirm we create a calendar event per checked block and, for blocks
// tied to a task, link the task (scheduled_start/end + event_id) so the Week grid styles it as a
// task-block and won't re-suggest it.
import { useEffect, useState } from "react";
import { CalendarCheck, Loader2, Sparkles, X } from "lucide-react";
import { api, type PlanBlock } from "./lib/api";

function fmtBlockDay(day: string): string {
  const d = new Date(`${day}T00:00:00`);
  return isNaN(d.getTime()) ? day : d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}
function fmtBlockTime(hhmm: string): string {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm);
  if (!m) return hhmm;
  let h = Number(m[1]);
  const ampm = h < 12 ? "AM" : "PM";
  h = h % 12 === 0 ? 12 : h % 12;
  return `${h}:${m[2]} ${ampm}`;
}

export default function PlanWeekModal({ onClose, onAdded }: {
  onClose: () => void;
  // Called after blocks are added so the host can refresh calendar/task buses.
  onAdded: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [blocks, setBlocks] = useState<PlanBlock[]>([]);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let live = true;
    (async () => {
      setLoading(true); setMessage(null);
      try {
        const r = await api.planner.suggest();
        if (!live) return;
        if (!r.ok || !r.blocks.length) {
          setMessage(r.message || "The planner didn't return any blocks.");
          setBlocks([]);
        } else {
          setBlocks(r.blocks);
          setChecked(new Set(r.blocks.map((_, i) => i)));  // default: all checked
        }
      } catch (e: any) {
        if (live) setMessage(e?.message || "Couldn't plan your week.");
      } finally {
        if (live) setLoading(false);
      }
    })();
    return () => { live = false; };
  }, []);

  const toggle = (i: number) =>
    setChecked((s) => { const n = new Set(s); n.has(i) ? n.delete(i) : n.add(i); return n; });

  const addToCalendar = async () => {
    const picks = blocks.filter((_, i) => checked.has(i));
    if (!picks.length) return;
    setSaving(true); setMessage(null);
    let added = 0;
    try {
      for (const b of picks) {
        const start = `${b.day}T${b.start}:00`;
        const end = `${b.day}T${b.end}:00`;
        const r = await api.calendar.create({ summary: b.title, start, end });
        if (!r.ok || !r.event) continue;
        added += 1;
        if (b.task_id && r.event.id) {
          await api.tasks.setExtras(b.task_id, {
            scheduled_start: start, scheduled_end: end, event_id: r.event.id,
          }).catch(() => {});
        }
      }
      onAdded();
      onClose();
    } catch (e: any) {
      setMessage(e?.message || `Added ${added} block${added === 1 ? "" : "s"}, then hit an error.`);
    } finally {
      setSaving(false);
    }
  };

  const checkedCount = checked.size;

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/30 backdrop-blur-sm p-6" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[520px] max-h-[80vh] flex flex-col rounded-[16px] bg-mac-fill border border-mac-strokeHi shadow-2xl overflow-hidden">
        <div className="shrink-0 flex items-center gap-2 px-5 py-4 border-b border-mac-stroke">
          <Sparkles size={16} className="text-mac-accentHi" />
          <h3 className="font-display text-[15px] font-semibold tracking-[-0.01em]">Plan my week</h3>
          <button onClick={onClose} className="ml-auto h-7 w-7 grid place-items-center rounded-[8px] text-mac-ink3 hover:text-mac-ink hover:bg-mac-fillHi transition-colors">
            <X size={15} />
          </button>
        </div>

        <div className="flex-1 min-h-0 overflow-auto px-5 py-4">
          {loading ? (
            <div className="py-12 grid place-items-center text-center gap-3">
              <Loader2 size={20} className="animate-spin text-mac-accentHi" />
              <p className="text-[13px] text-mac-ink2">Himmy is drafting your week…</p>
            </div>
          ) : blocks.length === 0 ? (
            <div className="py-10 grid place-items-center text-center text-[13px] text-mac-ink2 max-w-[40ch] mx-auto">
              {message || "Nothing to schedule right now."}
            </div>
          ) : (
            <div className="space-y-1.5">
              <p className="text-[12px] text-mac-ink3 pb-1.5">
                Himmy proposes {blocks.length} block{blocks.length === 1 ? "" : "s"}. Uncheck any you don't want.
              </p>
              {blocks.map((b, i) => {
                const on = checked.has(i);
                return (
                  <button key={i} onClick={() => toggle(i)}
                    className={`w-full text-left flex items-start gap-3 rounded-[11px] border px-3 py-2.5 transition-colors ${
                      on ? "border-mac-accent/40 bg-mac-accentDim" : "border-mac-stroke bg-mac-fillHi/40 opacity-60"
                    }`}>
                    <span className={`mt-0.5 h-[18px] w-[18px] shrink-0 grid place-items-center rounded-[6px] border transition-colors ${
                      on ? "bg-mac-accent border-mac-accent text-white" : "border-mac-strokeHi text-transparent"
                    }`}>
                      <CalendarCheck size={12} strokeWidth={2.5} />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-[13.5px] text-mac-ink font-medium leading-snug">{b.title}</span>
                      <span className="block text-[12px] text-mac-ink2 tnum mt-0.5">
                        {fmtBlockDay(b.day)} · {fmtBlockTime(b.start)} – {fmtBlockTime(b.end)}
                      </span>
                      {b.reason && <span className="block text-[12px] text-mac-ink3 leading-snug mt-0.5">{b.reason}</span>}
                    </span>
                  </button>
                );
              })}
              {message && <div className="text-[12px] text-mac-red px-1 pt-1">{message}</div>}
            </div>
          )}
        </div>

        {blocks.length > 0 && (
          <div className="shrink-0 flex items-center gap-2 px-5 py-3.5 border-t border-mac-stroke">
            <span className="text-[12px] text-mac-ink3">{checkedCount} selected</span>
            <div className="ml-auto flex items-center gap-2">
              <button onClick={onClose}
                className="h-8 px-3.5 rounded-[9px] border border-mac-stroke text-[12.5px] text-mac-ink2 hover:text-mac-ink hover:border-mac-strokeHi transition-colors">
                Cancel
              </button>
              <button onClick={addToCalendar} disabled={saving || checkedCount === 0}
                className="h-8 px-3.5 rounded-[9px] bg-mac-accent text-[12.5px] font-medium text-white hover:bg-mac-accentHi transition-colors flex items-center gap-1.5 disabled:opacity-50">
                {saving ? <Loader2 size={14} className="animate-spin" /> : <CalendarCheck size={14} strokeWidth={2.5} />}
                Add to calendar
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
