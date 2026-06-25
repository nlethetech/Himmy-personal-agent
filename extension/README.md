# Save to Himmy — browser extension

One-click save of the paper you're viewing (arXiv, journals, Google Scholar, PubMed, SSRN…)
straight into your Himmy library — with the full PDF.

## Install (Chrome / Edge / Brave / Arc — any Chromium browser)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and choose this `extension/` folder.
4. (Optional) Pin "Save to Himmy" to your toolbar.

## Use

1. Make sure **Himmy is running** (it serves the local API on port 8131).
2. Open a paper's page (e.g. an arXiv abstract or a journal article).
3. Click the **Save to Himmy** toolbar button.
   - ✓ green badge → saved (PDF downloaded if available)
   - ! amber → page had no citable metadata
   - ✕ red → couldn't reach Himmy (is it running?)

It reads the page's standard `citation_*` metadata (DOI / arXiv id / PDF url / title /
authors), so it works on most academic sites without any per-site configuration.
