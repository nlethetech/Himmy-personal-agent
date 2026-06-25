import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft, Highlighter, StickyNote, Copy, Check, ZoomIn, ZoomOut,
  Info, Trash2, Quote, X, Tag, Sparkles, Loader2, Download, ListPlus,
} from "lucide-react";
import * as pdfjsLib from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { api, type Paper, type Highlight, type Rect } from "./lib/api";
import { apa, mla, bibtex, inText } from "./lib/cite";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

const COLORS: Record<string, string> = {
  yellow: "rgba(255, 214, 10, 0.40)",
  green: "rgba(48, 209, 88, 0.38)",
  blue: "rgba(10, 132, 255, 0.34)",
  pink: "rgba(255, 55, 95, 0.34)",
};
type Tab = "info" | "notes" | "highlights";

// Engaged reading-time tuning. The clock only runs while the window is focused/visible AND the
// reader has been interacted with in the last IDLE_MS; accumulated seconds flush every FLUSH_MS.
const IDLE_MS = 90_000;   // ~1.5 min of zero interaction → assume you've stepped away, pause.
const FLUSH_MS = 15_000;  // push engaged seconds to the backend this often (and on close).

export default function Reader({ id, onClose }: { id: string; onClose: () => void }) {
  const [item, setItem] = useState<Paper | null>(null);
  const [highlights, setHighlights] = useState<Highlight[]>([]);
  const [scale, setScale] = useState(1.3);
  const [tab, setTab] = useState<Tab>("info");
  const [fetching, setFetching] = useState(false);
  const [fetchMsg, setFetchMsg] = useState<string | null>(null);
  const [pending, setPending] = useState<{ page: number; rects: Rect[]; text: string; x: number; y: number } | null>(null);
  const [addedTask, setAddedTask] = useState(false);   // brief "Added to tasks" confirmation
  const [addingTask, setAddingTask] = useState(false);
  const [curPage, setCurPage] = useState(1);           // the page you're on now (toolbar label)
  const [numPages, setNumPages] = useState(0);         // total pages, shown as "Page X / Y"

  const scrollRef = useRef<HTMLDivElement>(null);
  const pagesRef = useRef<HTMLDivElement>(null);
  const hlLayers = useRef<Map<number, HTMLDivElement>>(new Map());
  const highlightsRef = useRef<Highlight[]>([]);
  const renderToken = useRef(0);
  const numPagesRef = useRef(0);                              // current total pages (for save calls)
  const restoreRef = useRef<{ page: number; frac: number } | null>(null);  // pending resume point
  const restoredRef = useRef<string | null>(null);           // id we've already restored, once

  // --- load item + highlights -----------------------------------------------------------
  useEffect(() => {
    let alive = true;
    (async () => {
      const [it, hl] = await Promise.all([api.library.get(id), api.highlights.list(id)]);
      if (!alive) return;
      setItem(it.item);
      setHighlights(hl.highlights);
    })();
    return () => { alive = false; };
  }, [id]);

  // --- resume where you left off --------------------------------------------------------
  // The current scroll position as a stable anchor: the page you're sitting on + how far you've
  // scrolled into it. Page-anchored (not raw pixels) so it survives zoom and lazy re-rendering.
  const readingAnchor = (): { page: number; frac: number } | null => {
    const sc = scrollRef.current, host = pagesRef.current;
    if (!sc || !host) return null;
    const kids = Array.from(host.children) as HTMLElement[];
    if (!kids.length) return null;
    const top = sc.scrollTop;
    let cur = kids[0];
    for (const k of kids) { if (k.offsetTop <= top + 1) cur = k; else break; }
    const page = Number(cur.dataset.page) || 1;
    const frac = Math.min(0.999, Math.max(0, (top - cur.offsetTop) / (cur.offsetHeight || 1)));
    return { page, frac };
  };
  // Scroll back to the saved anchor — once per open. No-ops until both the saved point has
  // arrived AND the page placeholders exist, so it's safe to call from either side of that race.
  const applyRestore = () => {
    const r = restoreRef.current;
    if (!r || restoredRef.current === id) return;
    const host = pagesRef.current, sc = scrollRef.current;
    if (!host || !sc) return;
    const wrap = host.querySelector(`[data-page="${r.page}"]`) as HTMLElement | null;
    if (!wrap) return;                 // pages not built yet — the render effect will retry
    sc.scrollTop = Math.max(0, wrap.offsetTop + r.frac * wrap.offsetHeight);
    setCurPage(r.page);
    restoredRef.current = id;
  };

  // Fetch the saved resume point whenever the open paper changes.
  useEffect(() => {
    restoreRef.current = null;
    restoredRef.current = null;
    setCurPage(1);
    let alive = true;
    api.reading.getPosition(id).then((r) => {
      if (!alive || !r.ok || !r.position) return;
      restoreRef.current = { page: r.position.page, frac: r.position.frac };
      applyRestore();                  // in case the PDF finished building before this resolved
    }).catch(() => { /* first read of this paper — start at the top */ });
    return () => { alive = false; };
  }, [id]);

  // --- highlight overlays (decoupled from page rendering, so adding one never re-renders) -
  const drawPageHighlights = (page: number, layer: HTMLDivElement) => {
    layer.innerHTML = "";
    const w = layer.clientWidth, h = layer.clientHeight;
    highlightsRef.current.filter((x) => x.page === page).forEach((x) => {
      x.rects.forEach((r) => {
        const d = document.createElement("div");
        d.style.cssText = `position:absolute;left:${r.x * w}px;top:${r.y * h}px;width:${r.w * w}px;height:${r.h * h}px;background:${COLORS[x.color] || COLORS.yellow};border-radius:2px;`;
        layer.appendChild(d);
      });
    });
  };
  useEffect(() => {
    highlightsRef.current = highlights;
    hlLayers.current.forEach((layer, page) => drawPageHighlights(page, layer));
  }, [highlights]);

  // --- render the PDF LAZILY: white placeholders up front, real pages render as they near
  //     the viewport (smooth fast-scroll, no blank). Highlights draw separately. -----------
  useEffect(() => {
    if (!item?.has_pdf || !pagesRef.current) return;
    const token = ++renderToken.current;
    const container = pagesRef.current;
    let pdf: any = null;
    let observer: IntersectionObserver | null = null;
    const rendered = new Set<number>();

    const renderPage = async (wrap: HTMLElement, n: number) => {
      if (rendered.has(n) || token !== renderToken.current || !pdf) return;
      rendered.add(n);
      try {
        const page = await pdf.getPage(n);
        const viewport = page.getViewport({ scale });
        if (token !== renderToken.current) return;
        wrap.style.width = `${viewport.width}px`;
        wrap.style.height = `${viewport.height}px`;
        const canvas = document.createElement("canvas");
        canvas.width = viewport.width; canvas.height = viewport.height;
        wrap.appendChild(canvas);
        const hl = document.createElement("div"); hl.className = "pdf-hl-layer"; wrap.appendChild(hl);
        const tl = document.createElement("div"); tl.className = "textLayer";
        tl.style.setProperty("--scale-factor", String(scale));
        tl.style.width = `${viewport.width}px`; tl.style.height = `${viewport.height}px`;
        wrap.appendChild(tl);
        await page.render({ canvasContext: canvas.getContext("2d")!, viewport }).promise;
        if (token !== renderToken.current) return;
        const tc = await page.getTextContent();
        await new (pdfjsLib as any).TextLayer({ textContentSource: tc, container: tl, viewport }).render();
        hlLayers.current.set(n, hl);
        drawPageHighlights(n, hl);
      } catch {
        rendered.delete(n); // allow a retry next time it intersects
      }
    };

    (async () => {
      try {
        pdf = await pdfjsLib.getDocument({ url: api.library.pdfUrl(id) }).promise;
        if (token !== renderToken.current) return;
        container.innerHTML = "";
        hlLayers.current.clear();
        const vp1 = (await pdf.getPage(1)).getViewport({ scale });
        observer = new IntersectionObserver((entries) => {
          entries.forEach((e) => {
            if (e.isIntersecting) {
              const el = e.target as HTMLElement;
              renderPage(el, Number(el.dataset.page));
            }
          });
        }, { root: scrollRef.current, rootMargin: "1200px 0px" });
        for (let n = 1; n <= pdf.numPages; n++) {
          const wrap = document.createElement("div");
          wrap.dataset.page = String(n);
          wrap.style.cssText = `position:relative;width:${vp1.width}px;height:${vp1.height}px;margin:0 auto 14px;border-radius:3px;overflow:hidden;box-shadow:0 1px 8px rgba(0,0,0,0.4);background:#fff;`;
          container.appendChild(wrap);
          observer.observe(wrap);
        }
        // Placeholders now span the full document height, so we can jump straight to the saved
        // page. Re-apply on the next frame once layout settles.
        numPagesRef.current = pdf.numPages;
        setNumPages(pdf.numPages);
        applyRestore();
        requestAnimationFrame(applyRestore);
      } catch (e) {
        if (token === renderToken.current) console.error("PDF load failed", e);
      }
    })();
    return () => { observer?.disconnect(); try { pdf?.destroy?.(); } catch { /* ignore */ } };
  }, [item?.has_pdf, item?.id, scale]);

  // --- text selection → highlight (popup on mouse-up; H key for the keyboard) ------------
  const computeSelection = () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
    const range = sel.getRangeAt(0);
    let node: Node | null = range.startContainer;
    let pageEl: HTMLElement | null = null;
    while (node) {
      if (node instanceof HTMLElement && node.dataset.page) { pageEl = node; break; }
      node = node.parentNode;
    }
    if (!pageEl) return null;
    const pr = pageEl.getBoundingClientRect();
    const clientRects = Array.from(range.getClientRects()).filter((r) => r.width > 1 && r.height > 1);
    const rects: Rect[] = clientRects.map((r) => ({
      x: (r.left - pr.left) / pr.width, y: (r.top - pr.top) / pr.height,
      w: r.width / pr.width, h: r.height / pr.height,
    }));
    if (!rects.length) return null;
    const last = clientRects[clientRects.length - 1];
    const wrapRect = scrollRef.current!.getBoundingClientRect();
    return {
      page: Number(pageEl.dataset.page), rects, text: sel.toString(),
      x: last.left - wrapRect.left + last.width / 2, y: last.bottom - wrapRect.top + 6,
    };
  };

  const saveSelection = async (sel: NonNullable<ReturnType<typeof computeSelection>>, color: string) => {
    const r = await api.highlights.add(id, { page: sel.page, color, text: sel.text, note: "", rects: sel.rects });
    setHighlights((h) => [...h, r.highlight]);
    setPending(null);
    window.getSelection()?.removeAllRanges();
  };

  const onMouseUp = () => setPending(computeSelection());
  const saveHighlight = (color: string) => { if (pending) saveSelection(pending, color); };

  // Press H to highlight the current selection in yellow.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if ((e.key === "h" || e.key === "H") && !e.metaKey && !e.ctrlKey) {
        const s = computeSelection();
        if (s) { e.preventDefault(); saveSelection(s, "yellow"); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [id]);

  // --- engaged reading-time tracking ----------------------------------------------------
  // Honest "dwell time": accumulate seconds ONLY while this window is focused + visible AND the
  // user interacted (mouse / keys / scroll-wheel) within IDLE_MS. Switch apps or step away and
  // the clock pauses at once — a paper left open over lunch logs nothing. We flush engaged
  // seconds every FLUSH_MS and on close; the backend clamps each beat so it can't be inflated.
  useEffect(() => {
    if (!id) return;
    const sessionId = `${id}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    let engaged = 0;                 // accumulated, not-yet-flushed seconds
    let lastActivity = Date.now();
    let lastTick = Date.now();
    const markActive = () => { lastActivity = Date.now(); };

    const tick = () => {
      const now = Date.now();
      const dt = (now - lastTick) / 1000;
      lastTick = now;
      const active =
        document.visibilityState === "visible" &&
        document.hasFocus() &&
        now - lastActivity < IDLE_MS;
      // dt < 5 guards against a throttled/background timer crediting one big jump at wake-up.
      if (active && dt > 0 && dt < 5) engaged += dt;
    };
    const flush = (final: boolean) => {
      tick();                        // capture the tail before sending
      if (engaged < 1) return;
      const secs = Math.round(engaged);
      engaged = 0;
      if (final) api.reading.beacon(sessionId, id, secs);
      else api.reading.heartbeat(sessionId, id, secs).catch(() => { engaged += secs; });
    };
    const onVisibility = () => { if (document.visibilityState === "hidden") flush(false); };
    const onBlur = () => flush(false);

    const ticker = window.setInterval(tick, 1000);
    const flusher = window.setInterval(() => flush(false), FLUSH_MS);
    // Window-level activity sources cover the whole reader without depending on render timing.
    window.addEventListener("mousemove", markActive);
    window.addEventListener("keydown", markActive);
    window.addEventListener("wheel", markActive, { passive: true });
    window.addEventListener("pointerdown", markActive);
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      window.clearInterval(ticker);
      window.clearInterval(flusher);
      window.removeEventListener("mousemove", markActive);
      window.removeEventListener("keydown", markActive);
      window.removeEventListener("wheel", markActive);
      window.removeEventListener("pointerdown", markActive);
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVisibility);
      flush(true);                   // final flush when the reader closes or the paper changes
    };
  }, [id]);

  // --- save the reading position as you scroll ------------------------------------------
  // Keep the toolbar page label live (cheap, once per frame) and persist the resume point
  // (debounced). A keepalive beacon on teardown captures the final spot even as the reader
  // unmounts on a tab switch or the window closes.
  useEffect(() => {
    if (!item?.has_pdf) return;
    const sc = scrollRef.current;
    if (!sc) return;
    let saveTimer = 0;
    let raf = 0;
    const onScroll = () => {
      if (!raf) raf = requestAnimationFrame(() => {
        raf = 0;
        const a = readingAnchor();
        if (a) setCurPage(a.page);
      });
      if (saveTimer) window.clearTimeout(saveTimer);
      saveTimer = window.setTimeout(() => {
        const a = readingAnchor();
        if (a) api.reading.setPosition(id, a.page, a.frac, numPagesRef.current || null).catch(() => {});
      }, 600);
    };
    sc.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      sc.removeEventListener("scroll", onScroll);
      if (saveTimer) window.clearTimeout(saveTimer);
      if (raf) cancelAnimationFrame(raf);
      const a = readingAnchor();
      if (a) api.reading.positionBeacon(id, a.page, a.frac, numPagesRef.current || null);
    };
  }, [item?.has_pdf, item?.id]);

  const removeHighlight = async (hid: string) => {
    setHighlights((h) => h.filter((x) => x.id !== hid));
    await api.highlights.remove(hid);
  };
  const noteHighlight = async (hid: string, note: string) => {
    setHighlights((h) => h.map((x) => (x.id === hid ? { ...x, note } : x)));
    await api.highlights.update(hid, { note });
  };

  const jumpToPage = (page: number) => {
    pagesRef.current?.querySelector(`[data-page="${page}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  // Create a "Read: <title>" task linked back to this paper, so it shows up in the Planner /
  // Today agenda. Briefly confirms inline, then nudges open views to refresh.
  const addToTasks = async () => {
    if (!item || addingTask) return;
    setAddingTask(true);
    try {
      const r = await api.tasks.add(`Read: ${item.title}`);
      if (r.ok && r.task) {
        await api.tasks.setExtras(r.task.id, { paper_id: item.id, paper_title: item.title });
      }
      window.dispatchEvent(new CustomEvent("himmy:refresh", { detail: "tasks" }));
      setAddedTask(true);
      setTimeout(() => setAddedTask(false), 1800);
    } catch { /* best-effort */ }
    finally { setAddingTask(false); }
  };

  const fetchPdf = async () => {
    setFetching(true); setFetchMsg(null);
    try {
      const r = await api.library.fetchPdf(id);
      if (r.ok && r.item) setItem(r.item);
      else setFetchMsg(r.message || "No free full-text PDF found.");
    } catch (e: any) { setFetchMsg(e.message); }
    finally { setFetching(false); }
  };

  if (!item) {
    return <div className="h-full grid place-items-center text-mac-ink3 text-[13px]">Loading…</div>;
  }

  return (
    <div className="h-full flex flex-col">
      {/* reader toolbar */}
      <div className="shrink-0 h-12 px-4 flex items-center gap-3 border-b border-mac-stroke">
        <button onClick={onClose} className="flex items-center gap-1.5 text-[13px] text-mac-ink2 hover:text-mac-ink transition-colors">
          <ArrowLeft size={16} strokeWidth={2} /> Library
        </button>
        <div className="mx-2 h-4 w-px bg-mac-stroke" />
        <div className="min-w-0 flex-1 text-[13px] text-mac-ink truncate">{item.title}</div>
        <button onClick={addToTasks} disabled={addingTask || addedTask}
          title="Add a reading task for this paper"
          className={`shrink-0 inline-flex items-center gap-1.5 h-7 px-2.5 rounded-md text-[12px] transition-colors ${
            addedTask ? "text-mac-green" : "text-mac-ink2 hover:text-mac-ink hover:bg-mac-fill"}`}>
          {addedTask ? <Check size={14} /> : <ListPlus size={14} />}
          {addedTask ? "Added to tasks" : "Add to tasks"}
        </button>
        {item.has_pdf && (
          <div className="mx-1 h-4 w-px bg-mac-stroke" />
        )}
        {item.has_pdf && (
          <div className="flex items-center gap-1 text-mac-ink2">
            <button onClick={() => setScale((s) => Math.max(0.6, +(s - 0.15).toFixed(2)))}
              className="h-7 w-7 grid place-items-center rounded-md hover:bg-mac-fill"><ZoomOut size={15} /></button>
            <span className="text-[11px] tnum w-9 text-center text-mac-ink3">{Math.round(scale * 100)}%</span>
            <button onClick={() => setScale((s) => Math.min(2.4, +(s + 0.15).toFixed(2)))}
              className="h-7 w-7 grid place-items-center rounded-md hover:bg-mac-fill"><ZoomIn size={15} /></button>
          </div>
        )}
        {item.has_pdf && numPages > 0 && (
          <>
            <div className="mx-1 h-4 w-px bg-mac-stroke" />
            <span className="text-[11px] tnum text-mac-ink3 select-none whitespace-nowrap"
              title="Himmy remembers this spot — you'll come back here next time">
              Page {curPage} / {numPages}
            </span>
          </>
        )}
      </div>

      <div className="flex-1 min-h-0 grid" style={{ gridTemplateColumns: "1fr 340px" }}>
        {/* PDF / reading area */}
        <div ref={scrollRef} onMouseUp={onMouseUp} className="relative overflow-auto bg-[#1a1b1f] py-5">
          {item.has_pdf ? (
            <div ref={pagesRef} />
          ) : (
            <div className="mx-auto max-w-[680px] px-8 text-mac-ink">
              <h1 className="font-display text-[22px] font-semibold mb-2">{item.title}</h1>
              <p className="text-[13px] text-mac-ink2 mb-5">{item.authors.join(", ")}{item.year ? ` · ${item.year}` : ""}{item.venue ? ` · ${item.venue}` : ""}</p>
              <button onClick={fetchPdf} disabled={fetching}
                className="mb-5 inline-flex items-center gap-2 h-9 px-4 rounded-lg bg-mac-accent text-white text-[13px] font-medium hover:bg-mac-accentHi transition-colors disabled:opacity-60">
                {fetching ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}
                {fetching ? "Fetching…" : "Fetch full PDF"}
              </button>
              {fetchMsg && <p className="text-[12px] text-mac-orange mb-4">{fetchMsg}</p>}
              {item.abstract
                ? <p className="text-[14px] leading-relaxed text-mac-ink2 whitespace-pre-wrap">{item.abstract}</p>
                : <p className="text-[13px] text-mac-ink3">No abstract on file. Fetch the full PDF above, drag a PDF onto the Library, or edit the details on the right.</p>}
            </div>
          )}

          {pending && (
            <div className="absolute z-20 -translate-x-1/2 flex items-center gap-1 p-1 rounded-lg bg-[rgba(40,41,47,0.96)] backdrop-blur-xl border border-mac-strokeHi shadow-pop"
              style={{ left: pending.x, top: pending.y }}>
              {Object.entries(COLORS).map(([name, css]) => (
                <button key={name} onClick={() => saveHighlight(name)} title={`Highlight ${name}`}
                  className="h-6 w-6 rounded-md border border-white/10 hover:scale-110 transition-transform" style={{ background: css }} />
              ))}
              <span className="text-[10px] font-mono text-mac-ink3 px-1.5 border-l border-mac-stroke ml-0.5 select-none" title="Tip: select text and press H">H</span>
              <button onClick={() => { setPending(null); window.getSelection()?.removeAllRanges(); }}
                className="h-6 w-6 grid place-items-center text-mac-ink3 hover:text-mac-ink"><X size={13} /></button>
            </div>
          )}
        </div>

        {/* detail panel */}
        <aside className="border-l border-mac-stroke flex flex-col min-h-0 bg-[rgba(255,255,255,0.015)]">
          <div className="shrink-0 flex items-center gap-1 px-2 h-10 border-b border-mac-stroke">
            <TabBtn icon={Info} label="Info" active={tab === "info"} onClick={() => setTab("info")} />
            <TabBtn icon={StickyNote} label="Notes" active={tab === "notes"} onClick={() => setTab("notes")} />
            <TabBtn icon={Highlighter} label="Highlights" active={tab === "highlights"} onClick={() => setTab("highlights")} count={highlights.length} />
          </div>
          <div className="flex-1 min-h-0 overflow-auto">
            {tab === "info" && <InfoPanel item={item} onSaved={setItem} />}
            {tab === "notes" && <NotesPanel item={item} onSaved={setItem} />}
            {tab === "highlights" && (
              <HighlightsPanel itemId={id} hasNotes={!!(item?.notes || "").trim()} highlights={highlights} onJump={jumpToPage} onNote={noteHighlight} onRemove={removeHighlight} />
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}

function TabBtn({ icon: Ico, label, active, onClick, count }: any) {
  return (
    <button onClick={onClick}
      className={`flex items-center gap-1.5 h-7 px-2.5 rounded-md text-[12px] transition-colors ${active ? "bg-mac-fillHi text-mac-ink" : "text-mac-ink2 hover:text-mac-ink"}`}>
      <Ico size={13} /> {label}
      {count ? <span className="text-mac-ink3 tnum">{count}</span> : null}
    </button>
  );
}

/* ---- Info: editable metadata + cite + tags ---- */
function InfoPanel({ item, onSaved }: { item: Paper; onSaved: (p: Paper) => void }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const save = async (fields: Record<string, unknown>) => {
    const r = await api.library.update(item.id, fields);
    if (r.ok) onSaved(r.item);
  };
  const enrich = async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await api.library.enrich(item.id);
      if (r.ok && r.item) { onSaved(r.item); setMsg("Updated from " + (r.source === "crossref" ? "Crossref" : "Himmy") + "."); }
      else setMsg(r.message || "Couldn't identify this paper.");
    } catch (e: any) { setMsg(e.message); }
    finally { setBusy(false); }
  };
  return (
    <div className="p-4 space-y-4">
      <div>
        <button onClick={enrich} disabled={busy}
          className="w-full flex items-center justify-center gap-2 h-9 rounded-lg bg-mac-accentDim border border-mac-strokeHi text-[12.5px] text-mac-accentHi font-medium hover:bg-[rgba(10,132,255,0.22)] transition-colors disabled:opacity-60">
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
          {busy ? "Identifying…" : "Auto-fill with Himmy"}
        </button>
        {msg && <p className="text-[11.5px] text-mac-ink3 mt-1.5 text-center">{msg}</p>}
      </div>
      <Field label="Title" value={item.title} onSave={(v) => save({ title: v })} multiline />
      <Field label="Authors" value={item.authors.join(", ")} onSave={(v) => save({ authors: v.split(",").map((s) => s.trim()) })} />
      <div className="grid grid-cols-2 gap-3">
        <Field label="Year" value={item.year} onSave={(v) => save({ year: v })} />
        <Field label="DOI" value={item.doi} onSave={(v) => save({ doi: v })} />
      </div>
      <Field label="Venue" value={item.venue} onSave={(v) => save({ venue: v })} />
      <TagsEditor item={item} onSaved={onSaved} />
      <CiteBlock item={item} />
    </div>
  );
}

function Field({ label, value, onSave, multiline }: { label: string; value: string; onSave: (v: string) => void; multiline?: boolean }) {
  const [v, setV] = useState(value);
  useEffect(() => setV(value), [value]);
  const commit = () => { if (v !== value) onSave(v); };
  return (
    <label className="block">
      <span className="block text-[10.5px] uppercase tracking-wide text-mac-ink3 mb-1">{label}</span>
      {multiline ? (
        <textarea value={v} onChange={(e) => setV(e.target.value)} onBlur={commit} rows={2}
          className="w-full resize-none rounded-md bg-mac-fill border border-mac-stroke px-2.5 py-1.5 text-[13px] text-mac-ink outline-none focus:border-mac-accent" />
      ) : (
        <input value={v} onChange={(e) => setV(e.target.value)} onBlur={commit}
          className="w-full rounded-md bg-mac-fill border border-mac-stroke px-2.5 h-8 text-[13px] text-mac-ink outline-none focus:border-mac-accent" />
      )}
    </label>
  );
}

function TagsEditor({ item, onSaved }: { item: Paper; onSaved: (p: Paper) => void }) {
  const [adding, setAdding] = useState("");
  const save = async (tags: string[]) => { const r = await api.library.update(item.id, { tags }); if (r.ok) onSaved(r.item); };
  return (
    <div>
      <span className="block text-[10.5px] uppercase tracking-wide text-mac-ink3 mb-1.5">Tags</span>
      <div className="flex flex-wrap gap-1.5">
        {item.tags.map((t) => (
          <span key={t} className="group inline-flex items-center gap-1 text-[12px] text-mac-ink2 bg-mac-fill border border-mac-stroke rounded-full pl-2.5 pr-1.5 py-0.5">
            <Tag size={10} className="text-mac-ink3" />{t}
            <button onClick={() => save(item.tags.filter((x) => x !== t))} className="text-mac-ink3 hover:text-mac-red"><X size={11} /></button>
          </span>
        ))}
        <input value={adding} onChange={(e) => setAdding(e.target.value)} placeholder="+ tag"
          onKeyDown={(e) => { if (e.key === "Enter" && adding.trim()) { save([...item.tags, adding.trim()]); setAdding(""); } }}
          className="text-[12px] bg-transparent outline-none w-16 text-mac-ink placeholder:text-mac-ink3" />
      </div>
    </div>
  );
}

function CiteBlock({ item }: { item: Paper }) {
  const [copied, setCopied] = useState("");
  const copy = (style: string, text: string) => { navigator.clipboard.writeText(text); setCopied(style); setTimeout(() => setCopied(""), 1400); };
  const rows: [string, string][] = [["In-text", inText(item)], ["APA", apa(item)], ["MLA", mla(item)], ["BibTeX", bibtex(item)]];
  return (
    <div>
      <span className="flex items-center gap-1.5 text-[10.5px] uppercase tracking-wide text-mac-ink3 mb-1.5"><Quote size={11} /> Cite</span>
      <div className="space-y-1.5">
        {rows.map(([style, text]) => (
          <button key={style} onClick={() => copy(style, text)}
            className="w-full flex items-center justify-between gap-2 rounded-md bg-mac-fill border border-mac-stroke px-2.5 py-2 text-left hover:border-mac-strokeHi transition-colors">
            <div className="min-w-0">
              <div className="text-[10.5px] text-mac-accentHi font-medium">{style}</div>
              <div className="text-[11.5px] text-mac-ink2 truncate font-mono">{text}</div>
            </div>
            {copied === style ? <Check size={14} className="text-mac-green shrink-0" /> : <Copy size={13} className="text-mac-ink3 shrink-0" />}
          </button>
        ))}
      </div>
    </div>
  );
}

/* ---- Notes ---- */
function NotesPanel({ item, onSaved }: { item: Paper; onSaved: (p: Paper) => void }) {
  const [note, setNote] = useState(item.notes || "");
  useEffect(() => setNote(item.notes || ""), [item.id]);
  const commit = async () => { await api.library.setNote(item.id, note); onSaved({ ...item, notes: note }); };
  return (
    <div className="p-4 h-full">
      <textarea value={note} onChange={(e) => setNote(e.target.value)} onBlur={commit}
        placeholder="Your notes on this paper — saved automatically."
        className="w-full h-[calc(100%-1rem)] resize-none rounded-lg bg-mac-fill border border-mac-stroke p-3 text-[13px] leading-relaxed text-mac-ink outline-none focus:border-mac-accent placeholder:text-mac-ink3" />
    </div>
  );
}

/* ---- Highlights list ---- */
function HighlightsPanel({ itemId, hasNotes, highlights, onJump, onNote, onRemove }: any) {
  const [exporting, setExporting] = useState(false);
  const [exportMsg, setExportMsg] = useState<string | null>(null);
  const canExport = highlights.length > 0 || hasNotes;

  const doExport = async () => {
    setExporting(true);
    setExportMsg(null);
    try {
      const r = await api.highlights.exportMarkdown(itemId);
      setExportMsg(r.ok && r.path ? `Saved to ${r.path}` : (r.message || "Couldn't export."));
    } catch {
      setExportMsg("Couldn't export.");
    } finally {
      setExporting(false);
    }
  };

  const header = (
    <div className="shrink-0 px-3 pt-3">
      <button
        onClick={doExport}
        disabled={!canExport || exporting}
        className="w-full flex items-center justify-center gap-1.5 rounded-[9px] bg-mac-fill border border-mac-stroke px-2.5 py-1.5 text-[12px] font-medium text-mac-ink2 hover:bg-mac-fillHi hover:text-mac-ink transition-colors disabled:opacity-40 disabled:cursor-not-allowed">
        {exporting ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
        Export to Markdown
      </button>
      {exportMsg && (
        <div className="mt-1.5 text-[10.5px] text-mac-ink3 break-all leading-snug">{exportMsg}</div>
      )}
    </div>
  );

  if (!highlights.length) {
    return (
      <div className="flex flex-col">
        {header}
        <div className="p-6 text-center text-[12.5px] text-mac-ink3">Select text in the PDF to highlight it. Your highlights collect here.</div>
      </div>
    );
  }
  return (
    <div>
      {header}
      <div className="p-3 space-y-2">
      {highlights.map((h: Highlight) => (
        <div key={h.id} className="group rounded-lg bg-mac-fill border border-mac-stroke p-2.5">
          <div className="flex items-start gap-2">
            <span className="mt-1 h-3 w-3 shrink-0 rounded-sm" style={{ background: (COLORS[h.color] || COLORS.yellow).replace("0.3", "0.8") }} />
            <button onClick={() => onJump(h.page)} className="min-w-0 flex-1 text-left">
              <div className="text-[12.5px] text-mac-ink leading-snug line-clamp-3">{h.text}</div>
              <div className="text-[10.5px] text-mac-ink3 mt-1">p.{h.page}</div>
            </button>
            <button onClick={() => onRemove(h.id)} className="opacity-0 group-hover:opacity-100 text-mac-ink3 hover:text-mac-red transition-opacity"><Trash2 size={13} /></button>
          </div>
          <input defaultValue={h.note} placeholder="Add a note…" onBlur={(e) => onNote(h.id, e.target.value)}
            className="mt-2 w-full bg-transparent border-t border-mac-stroke pt-1.5 text-[12px] text-mac-ink2 outline-none placeholder:text-mac-ink3" />
        </div>
      ))}
      </div>
    </div>
  );
}
