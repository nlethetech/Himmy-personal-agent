import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";

/* Frontier-grade markdown for Himmy's chat bubbles.
   Renders GFM (tables, lists, links, bold/italic, code) with native macOS-app styling, so a
   model reply that comes back as markdown — a flight table, a ranked list, a booking link —
   looks designed instead of showing raw `**` and `*`. Links open externally (Electron).
   Margins are collapsed at the edges so a bubble never gets stray top/bottom padding. */

const components: Components = {
  // paragraphs & spacing
  p: ({ children }) => <p className="my-1.5 leading-relaxed">{children}</p>,

  // links — clickable, open in the browser, accent-coloured
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noreferrer"
      className="text-mac-accentHi underline decoration-mac-accentHi/40 underline-offset-2 hover:decoration-mac-accentHi break-words">
      {children}
    </a>
  ),

  // emphasis
  strong: ({ children }) => <strong className="font-semibold text-mac-ink">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  del: ({ children }) => <del className="opacity-60">{children}</del>,

  // headings (kept compact for chat)
  h1: ({ children }) => <h1 className="font-display text-[16px] font-semibold mt-3 mb-1.5 tracking-[-0.01em]">{children}</h1>,
  h2: ({ children }) => <h2 className="font-display text-[15px] font-semibold mt-3 mb-1.5 tracking-[-0.01em]">{children}</h2>,
  h3: ({ children }) => <h3 className="font-display text-[13.5px] font-semibold mt-2.5 mb-1">{children}</h3>,

  // lists
  ul: ({ children }) => <ul className="my-1.5 pl-4 space-y-1 list-disc marker:text-mac-ink3">{children}</ul>,
  ol: ({ children }) => <ol className="my-1.5 pl-4 space-y-1 list-decimal marker:text-mac-ink3">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed pl-0.5">{children}</li>,

  // tables — the headline feature: a real bordered table for any list-shaped answer
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto rounded-lg border border-mac-stroke">
      <table className="w-full border-collapse text-[12.5px]">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-mac-fillHi">{children}</thead>,
  tbody: ({ children }) => <tbody className="divide-y divide-mac-stroke/60">{children}</tbody>,
  tr: ({ children }) => <tr>{children}</tr>,
  th: ({ children }) => (
    <th className="text-left font-semibold text-mac-ink px-2.5 py-1.5 border-b border-mac-stroke whitespace-nowrap">{children}</th>
  ),
  td: ({ children }) => <td className="px-2.5 py-1.5 text-mac-ink2 align-top">{children}</td>,

  // code
  code: ({ className, children }) => {
    const isBlock = /language-/.test(className || "");
    if (isBlock) return <code className="font-mono">{children}</code>;
    return <code className="font-mono text-[12px] bg-mac-fillHi rounded px-1 py-0.5">{children}</code>;
  },
  pre: ({ children }) => (
    <pre className="my-2 overflow-x-auto rounded-lg bg-black/25 border border-mac-stroke p-3 text-[12px] font-mono leading-relaxed">{children}</pre>
  ),

  // misc
  blockquote: ({ children }) => (
    <blockquote className="my-2 pl-3 border-l-2 border-mac-stroke text-mac-ink2 italic">{children}</blockquote>
  ),
  hr: () => <hr className="my-3 border-mac-stroke" />,
};

export default function ChatMarkdown({ text }: { text: string }) {
  return (
    <div className="himmy-md text-[13.5px] [&>*:first-child]:mt-0 [&>*:last-child]:mb-0 break-words">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
