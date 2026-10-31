// Cut a paper's reference list (and everything after it, usually the
// appendix) before anything is summarized: it costs tokens and teaches
// nothing. Runs in the browser after PDF extraction and again on the server
// (old records, other clients). Pure code, no model calls.
//
// The PDF reader only finds headings by font size, which fails on some
// layouts (two-column journals): then "References" is just a line in the text
// and the list is split into "(cont.)" chunks. So besides a "References"
// section heading, we look for the line where the list starts, by content.

import type { PaperSection } from "./spec";

const HEADING = /^\s*(\d+\.?\s*)?(references|bibliography|works cited)\b/i;
const TITLE_LINE = /^\s*(\d+\.?\s*)?(references|bibliography|works cited)\s*$/i;
const YEAR = /\b(19|20)\d{2}[a-z]?\b/;
// Dotted abbreviations get no trailing \b: "vol." is followed by a space, not a word character.
const VENUE = /\b(arXiv|Proceedings|Conference|Journal|Transactions|PMLR|NeurIPS|ICML|ICLR|CVPR|ICCV|ECCV|ACL|EMNLP|NAACL|AAAI|IJCAI|preprint|IEEE|ACM|Springer|Elsevier)\b|\b(pp|vol|Proc|doi|no)\.|\bIn Advances/;
const AUTHOR = /([A-Z][a-z]+,\s+[A-Z][a-z]*\.?|\b[A-Z]\.\s?[A-Z][a-z]+|\bet al\.)/;
const ENTRY = /^\s*(\[\d{1,3}\]|\d{1,3}\.?\s+[A-Z][a-z]+,?\s)/;

/** Only look this far into the paper or later: body text cites too, and related work comes early. */
const START_SHARE = 0.4;
/** A list starts where at least half of the next WINDOW text lines look like reference entries. */
const WINDOW = 12, MIN_WINDOW = 6, MIN_SHARE = 0.5, MIN_LINE = 20;

/** Does this line look like (part of) a reference entry? */
export function isReferenceLine(l: string): boolean {
  return ENTRY.test(l) || (YEAR.test(l) && (VENUE.test(l) || AUTHOR.test(l))) || (VENUE.test(l) && AUTHOR.test(l));
}

export interface ReferenceCut {
  sections: PaperSection[];
  /** Characters removed (0 = nothing found). */
  removed: number;
  /** How the start was found. */
  by?: "heading" | "title line" | "content";
}

export function cutReferences(sections: PaperSection[]): ReferenceCut {
  const total = sections.reduce((a, s) => a + s.text.length, 0);
  const result = (keep: PaperSection[], by: ReferenceCut["by"]): ReferenceCut =>
    ({ sections: keep, removed: total - keep.reduce((a, s) => a + s.text.length, 0), by });

  // 1. A real "References" section heading (big font): trust it, as before.
  const byHeading = sections.findIndex((s, i) => i > 2 && HEADING.test(s.heading));
  if (byHeading > 0) return result(sections.slice(0, byHeading), "heading");

  // 2. The line where the list starts. A lone "References" line counts only if
  // entries follow (a figure label or table column can say "References" too).
  const lines: { si: number; off: number; pos: number; text: string; long: boolean }[] = [];
  let pos = 0;
  sections.forEach((s, si) => {
    let off = 0;
    for (const text of s.text.split("\n")) {
      lines.push({ si, off, pos: pos + off, text, long: text.trim().length >= MIN_LINE });
      off += text.length + 1;
    }
    pos += s.text.length;
  });
  const entriesFollow = (from: number) => {
    const win = [];
    for (let i = from; i < lines.length && win.length < WINDOW; i++) if (lines[i].long) win.push(lines[i]);
    return win.length >= MIN_WINDOW && win.filter((w) => isReferenceLine(w.text)).length / win.length >= MIN_SHARE;
  };
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (l.pos < total * START_SHARE) continue;
    const by = TITLE_LINE.test(l.text) && entriesFollow(i + 1) ? "title line" : l.long && isReferenceLine(l.text) && entriesFollow(i) ? "content" : null;
    if (!by) continue;
    const head = sections[l.si].text.slice(0, l.off).trim();
    const keep = [...sections.slice(0, l.si), ...(head ? [{ ...sections[l.si], text: head }] : [])];
    return result(keep, by);
  }
  return { sections, removed: 0 };
}
