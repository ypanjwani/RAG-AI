// PDF -> sections, in the browser (the file never leaves the device; only
// its text is sent). Headings are guessed from font size: lines noticeably
// larger than the body text start a new section.

import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { cutReferences } from "../../shared/references";
import { LIMITS, type PaperSection } from "../../shared/spec";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

const MAX_PAGES = 60;
const SECTION_CHARS = 8000; // long sections are split so summaries stay focused

interface Line { text: string; size: number }

export interface ExtractedPaper {
  title: string; pages: number; sections: PaperSection[]; chars: number;
  /** Characters of reference list (and what followed it) removed. */
  referencesCut: number;
}

export async function extractPaper(file: File): Promise<ExtractedPaper> {
  if (file.type && file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
    throw new Error("Please attach a PDF file.");
  }
  let doc: pdfjs.PDFDocumentProxy;
  try {
    doc = await pdfjs.getDocument({ data: new Uint8Array(await file.arrayBuffer()) }).promise;
  } catch {
    throw new Error("That PDF couldn't be opened. It may be damaged or password-protected.");
  }

  const lines: Line[] = [];
  const pages = Math.min(doc.numPages, MAX_PAGES);
  for (let p = 1; p <= pages; p++) {
    const content = await (await doc.getPage(p)).getTextContent();
    let cur: Line | null = null;
    let lastY: number | null = null;
    for (const item of content.items) {
      if (!("str" in item)) continue;
      const y = item.transform[5];
      const size = Math.round(Math.hypot(item.transform[2], item.transform[3]) * 10) / 10;
      if (cur && lastY !== null && Math.abs(y - lastY) < 2) {
        cur.text += item.str;
        cur.size = Math.max(cur.size, size);
      } else {
        if (cur?.text.trim()) lines.push(cur);
        cur = { text: item.str, size };
      }
      lastY = y;
      if (item.hasEOL) { if (cur?.text.trim()) lines.push(cur); cur = null; lastY = null; }
    }
    if (cur?.text.trim()) lines.push(cur);
  }
  if (!lines.length) throw new Error("No text found in this PDF. Scanned (image-only) papers aren't supported yet.");

  // Body size = the most common size, weighted by characters.
  const weight = new Map<number, number>();
  for (const l of lines) weight.set(l.size, (weight.get(l.size) ?? 0) + l.text.length);
  const body = [...weight.entries()].sort((a, b) => b[1] - a[1])[0][0];
  const isHeading = (l: Line) => l.size >= body * 1.15 && l.text.trim().length <= 120 && /[A-Za-z]/.test(l.text);

  const title = (lines.filter((l) => l.text.trim().length > 3).slice(0, 12).sort((a, b) => b.size - a.size)[0]?.text ?? file.name).trim().slice(0, 200);

  const sections: PaperSection[] = [];
  let cur: PaperSection = { heading: "Beginning", text: "" };
  const flush = () => {
    const text = cur.text.replace(/-\n(?=[a-z])/g, "").replace(/\s+\n/g, "\n").trim();
    for (let i = 0; i < text.length; i += SECTION_CHARS) {
      sections.push({ heading: i ? `${cur.heading} (cont.)` : cur.heading, text: text.slice(i, i + SECTION_CHARS) });
    }
  };
  for (const l of lines) {
    if (isHeading(l)) { flush(); cur = { heading: l.text.trim(), text: "" }; }
    else cur.text += l.text + "\n";
  }
  flush();

  // Stop at the references (by heading, or by content when headings weren't found).
  const cut = cutReferences(sections);
  let kept = cut.sections.slice(0, LIMITS.sections);
  let chars = 0;
  kept = kept.filter((s) => (chars += s.text.length) <= LIMITS.paperChars);
  if (!kept.length) throw new Error("Couldn't find readable text in this PDF.");
  return { title, pages: doc.numPages, sections: kept, chars: kept.reduce((a, s) => a + s.text.length, 0), referencesCut: cut.removed };
}
