import type { Paper } from "./api";

function lastFirst(name: string): { last: string; first: string } {
  const parts = name.trim().split(/\s+/);
  if (parts.length < 2) return { last: name.trim(), first: "" };
  return { last: parts[parts.length - 1], first: parts.slice(0, -1).join(" ") };
}

function initials(first: string): string {
  return first.split(/\s+/).filter(Boolean).map((p) => p[0].toUpperCase() + ".").join(" ");
}

function apaAuthors(authors: string[]): string {
  const list = authors.map((n) => { const { last, first } = lastFirst(n); return first ? `${last}, ${initials(first)}` : last; });
  if (list.length === 0) return "";
  if (list.length === 1) return list[0];
  if (list.length <= 20) return list.slice(0, -1).join(", ") + ", & " + list[list.length - 1];
  return list.slice(0, 19).join(", ") + ", … " + list[list.length - 1];
}

export function apa(p: Paper): string {
  const a = apaAuthors(p.authors);
  const year = p.year ? ` (${p.year}).` : " (n.d.).";
  const title = p.title ? ` ${p.title}.` : "";
  const venue = p.venue ? ` ${p.venue}.` : "";
  const link = p.doi ? ` https://doi.org/${p.doi}` : p.url ? ` ${p.url}` : "";
  return `${a}${year}${title}${venue}${link}`.trim();
}

function mlaAuthors(authors: string[]): string {
  if (!authors.length) return "";
  const { last, first } = lastFirst(authors[0]);
  const first0 = first ? `${last}, ${first}` : last;
  if (authors.length === 1) return first0;
  if (authors.length === 2) return `${first0}, and ${authors[1]}`;
  return `${first0}, et al`;
}

export function mla(p: Paper): string {
  const a = mlaAuthors(p.authors);
  const title = p.title ? ` "${p.title}."` : "";
  const venue = p.venue ? ` ${p.venue},` : "";
  const year = p.year ? ` ${p.year},` : "";
  const link = p.doi ? ` doi:${p.doi}.` : p.url ? ` ${p.url}.` : "";
  return `${a}.${title}${venue}${year}${link}`.replace(/,(\s*)$/, ".").trim();
}

export function inText(p: Paper): string {
  const year = p.year || "n.d.";
  const a = p.authors;
  if (!a.length) return `(${(p.title || "Untitled").split(/\s+/).slice(0, 2).join(" ")}, ${year})`;
  const last = (n: string) => lastFirst(n).last;
  if (a.length === 1) return `(${last(a[0])}, ${year})`;
  if (a.length === 2) return `(${last(a[0])} & ${last(a[1])}, ${year})`;
  return `(${last(a[0])} et al., ${year})`;
}

export function bibtex(p: Paper): string {
  const lastName = (p.authors[0] || "unknown").trim().split(/\s+/).pop() || "unknown";
  const firstWord = (p.title || "ref").split(/\s+/)[0].toLowerCase().replace(/[^a-z0-9]/g, "");
  const key = `${lastName.toLowerCase()}${p.year || ""}${firstWord}`;
  const entryType = p.type === "book" ? "book" : p.type === "preprint" ? "misc" : "article";
  const fields = [
    p.title && `  title = {${p.title}}`,
    p.authors.length > 0 && `  author = {${p.authors.join(" and ")}}`,
    p.year && `  year = {${p.year}}`,
    p.venue && `  journal = {${p.venue}}`,
    p.doi && `  doi = {${p.doi}}`,
    p.url && `  url = {${p.url}}`,
  ].filter(Boolean).join(",\n");
  return `@${entryType}{${key},\n${fields}\n}`;
}
