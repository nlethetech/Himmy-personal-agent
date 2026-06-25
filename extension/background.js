// Save to Daybook — MV3 service worker.
// Click the toolbar button on any paper page → scrape its bibliographic <meta> tags →
// POST to the local Daybook backend → it adds the item and downloads the PDF.

const BACKEND = "http://127.0.0.1:8131/save";

// Runs IN the page. Reads the standard Highwire/Dublin-Core citation meta tags that arXiv,
// most journals, Google Scholar, PubMed, SSRN, etc. emit.
function scrapeCitation() {
  const all = (name) =>
    Array.from(document.querySelectorAll(`meta[name="${name}"], meta[property="${name}"]`))
      .map((e) => (e.getAttribute("content") || "").trim())
      .filter(Boolean);
  const one = (name) => all(name)[0] || "";

  let doi = one("citation_doi") || one("dc.identifier") || one("DC.identifier") || "";
  doi = doi.replace(/^doi:/i, "").replace(/^https?:\/\/(dx\.)?doi\.org\//i, "");
  // arXiv id from meta or the URL
  let arxiv = one("citation_arxiv_id");
  const am = location.href.match(/arxiv\.org\/(?:abs|pdf)\/(\d{4}\.\d{4,5})/i);
  if (!arxiv && am) arxiv = am[1];

  return {
    doi,
    arxiv,
    pdf_url: one("citation_pdf_url"),
    title: one("citation_title") || one("dc.title") || document.title,
    authors: all("citation_author").length ? all("citation_author") : all("dc.creator"),
    year: (one("citation_publication_date") || one("citation_date") || one("dc.date") || "").slice(0, 4),
    venue: one("citation_journal_title") || one("citation_conference_title") || one("citation_inbook_title") || "",
  };
}

function badge(tabId, text, color) {
  try {
    chrome.action.setBadgeText({ tabId, text });
    chrome.action.setBadgeBackgroundColor({ tabId, color });
    setTimeout(() => chrome.action.setBadgeText({ tabId, text: "" }), 3500);
  } catch (_) { /* ignore */ }
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id) return;
  badge(tab.id, "…", "#8A939E");
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: scrapeCitation,
    });
    const payload = { ...result, url: tab.url };
    const resp = await fetch(BACKEND, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({ ok: false }));
    if (data.ok) {
      badge(tab.id, "✓", "#30D158");
    } else {
      badge(tab.id, "!", "#FF9F0A");
    }
  } catch (e) {
    // Most likely: Daybook isn't running, or nothing citable on this page.
    badge(tab.id, "✕", "#FF453A");
  }
});
