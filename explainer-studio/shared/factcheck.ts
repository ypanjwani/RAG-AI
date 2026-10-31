// Fact check (Quality+ and Freeform+): the checkable statements of a finished explainer, the
// numbers its code computes, and how to apply a reviewer's corrections. Pure
// functions (ported to server/explainer/core/factcheck.py); server/explainer/factcheck.py asks Jev which claims look wrong and Haiku
// how to fix them. A fix may only replace text inside the claim's own fields (or
// change a quiz's marked answer), so a mistaken reviewer can't touch anything else.

import { applyChanges, getAt, leaves, type Path } from "./edit";
import { evalNumber, type Scope } from "./expr";
import type { Block, ExplainerSpec } from "./spec";
import { withDefs } from "./views";

export interface Claim {
  id: string;
  stage: number;
  block: number;
  /** What the checker reads. */
  text: string;
  /** The text fields a fix may edit (paths inside the block). */
  fields: Path[];
  /** Quiz and predict questions: the path of the question object (whose "answer" a fix may change). */
  question?: Path;
}

/** A reviewer's fix for one claim: replace text inside its fields, and/or change a quiz's marked answer. */
export interface Fix { id: string; edits: { find: string; replace: string }[]; answer?: number }

export const MAX_CLAIMS = 120;

// Fields that state things. Labels, titles of charts, axis names and ids don't.
const PROSE = new Set(["md", "back", "meaning", "caption", "evidence", "breaksIf", "detail", "desc", "points"]);
// Sentences that only tell the reader what to do with the page.
const NAVIGATION = /^(try|move|drag|click|press|use the|set the|change the|turn|flip|toggle|scroll|look at|see the|watch)\b/i;

/** Split Markdown prose into sentences (and list items), keeping each as written so it can be found again. */
export function sentences(md: string): string[] {
  return md
    .split(/\n+/)
    .flatMap((line) => line.replace(/^\s*(?:[-*]|\d+\.)\s+/, "").split(/(?<=[.!?])\s+(?=[A-Z*$("'[=])/))
    .map((s) => s.trim())
    .filter((s) => s.length >= 25 && /[a-z]/i.test(s) && !NAVIGATION.test(s.replace(/^\*+/, "")));
}

const keyOf = (path: Path) => [...path].reverse().find((k): k is string => typeof k === "string") ?? "";

/** One quiz or predict question as a claim: the question, its choices, the marked answer and the explanation. */
function questionClaim(q: { q?: string; question?: string; choices: string[]; answer: number; explain: string }, base: Path): Omit<Claim, "id" | "stage" | "block"> {
  const letter = (i: number) => String.fromCharCode(65 + i);
  const qKey = q.q !== undefined ? "q" : "question";
  return {
    text: `Question: ${q.q ?? q.question}\nChoices: ${q.choices.map((c, i) => `${letter(i)}) ${c}`).join("  ")}\nMarked correct: ${letter(q.answer)}) ${q.choices[q.answer] ?? "?"}\nExplanation: ${q.explain}`,
    fields: [[...base, qKey], ...q.choices.map((_, i) => [...base, "choices", i]), [...base, "explain"]],
    question: base,
  };
}

/**
 * The heading a nested sentence sits under (a compare column's title, a tab's label, a flip card's
 * label), so it is judged in context: "Average taken under Q" is right under "Reverse KL" and wrong
 * on its own. The block's own title is not used (top-level prose reads fine alone).
 */
function headingOf(b: Block, path: Path): string {
  for (let n = path.length - 1; n >= 1; n--) {
    const o = getAt(b, path.slice(0, n));
    if (o && typeof o === "object" && !Array.isArray(o)) {
      const rec = o as Record<string, unknown>;
      for (const k of ["title", "label"]) {
        const t = rec[k];
        if (typeof t === "string" && t.trim()) return t.trim();
      }
    }
  }
  return "";
}

/** The claims of one block, in page order. */
function blockClaims(b: Block): Omit<Claim, "id" | "stage" | "block">[] {
  if (b.type === "quiz") return b.questions.map((q, i) => questionClaim(q, ["questions", i]));
  if (b.type === "predict") return [questionClaim(b, [])];
  if (b.type === "cards") {
    // A card is checked as a whole: its figure, title, text and evidence belong together.
    return b.items.map((it, i) => {
      const keys = (["value", "title", "md", "evidence", "breaksIf"] as const).filter((k) => it[k]);
      // A mistake card's title is a misconception on purpose; only its correction must be true.
      const text = b.variant === "mistake"
        ? `Misconception (shown on purpose, to be corrected): "${it.title}". Correction: ${it.md}`
        : keys.map((k) => (k === "md" || k === "title" ? it[k] : `${k}: ${it[k]}`)).join(" — ");
      return { text, fields: keys.filter((k) => b.variant !== "mistake" || k !== "title").map((k) => ["items", i, k]) };
    }).filter((c) => c.text.length >= 25);
  }
  if (b.type === "table") {
    return b.rows.flatMap((r, ri) => {
      const cells = r.map((c, ci) => ({ c, ci })).filter(({ c }) => c && !c.startsWith("="));
      if (!cells.length) return [];
      const text = cells.map(({ c, ci }) => `${b.columns[ci] || "—"}: ${c}`).join(" | ");
      return text.length >= 25 ? [{ text: `${b.title}: ${text}`, fields: cells.map(({ ci }) => ["rows", ri, ci]) }] : [];
    });
  }
  // Widgets are sandboxed code. A worked example's numbers are computed at its own settings, not the page's
  // starting values, so checking them against pageValues "corrects" right numbers (measured: it did).
  if (b.type === "widget" || b.type === "examples") return [];
  return leaves(b)
    .filter((l) => (l.kind === "prose" || l.kind === "text") && typeof l.value === "string" && PROSE.has(keyOf(l.path)))
    .flatMap((l) => {
      const heading = headingOf(b, l.path);
      return sentences(l.value as string).map((s) => ({ text: heading ? `${heading}: ${s}` : s, fields: [l.path] }));
    });
}

/** Every checkable statement on the page, capped at MAX_CLAIMS (earlier stages first). */
export function claimsOf(spec: ExplainerSpec): Claim[] {
  const out: Claim[] = [];
  spec.stages.forEach((st, si) => st.blocks.forEach((b, bi) => {
    for (const c of blockClaims(b)) out.push({ ...c, id: `c${out.length + 1}`, stage: si, block: bi });
  }));
  return out.slice(0, MAX_CLAIMS);
}

/** Every control at its starting value: the state the page opens in. */
function defaultScope(spec: ExplainerSpec): Scope {
  const scope: Scope = { seed: 0 };
  for (const st of spec.stages) for (const b of st.blocks)
    if ((b.type === "slider" || b.type === "choice" || b.type === "toggle") && !(b.var in scope)) scope[b.var] = b.value;
  return scope;
}

const show = (v: number) => String(Number(v.toPrecision(4)));

/**
 * The numbers the page shows when it opens, computed by code (controls, readouts,
 * calculator rows, live table cells), as lines like `readout "Recall" = 0.6914`.
 * The checker compares the text against these, so a quiz that contradicts a readout is caught.
 */
export function pageValues(spec: ExplainerSpec, max = 60): string[] {
  const scope = defaultScope(spec);
  const out: string[] = [];
  const val = (expr: string, defs: { name: string; expr: string }[] = []) => evalNumber(expr, withDefs(defs, scope));
  for (const st of spec.stages) for (const b of st.blocks) {
    if (b.type === "slider" || b.type === "toggle") out.push(`control "${b.label}" (${b.var}) starts at ${b.value}`);
    if (b.type === "choice") out.push(`control "${b.label}" (${b.var}) starts at ${b.value}`);
    if (b.type === "readout") for (const it of b.items) {
      const v = val(it.expr, b.defs);
      if (Number.isFinite(v)) out.push(`readout "${it.label}" = ${show(v)}${it.kind === "probability" ? ` (shown as ${show(v * 100)}%)` : ""}`);
    }
    if (b.type === "calculator") b.rows.forEach((r, i) => {
      const v = val(r.expr, b.defs);
      if (Number.isFinite(v)) out.push(`calculator "${b.title}" row ${i + 1} (${r.latex}${r.note ? `, ${r.note}` : ""}) = ${show(v)}`);
    });
    if (b.type === "table") b.rows.forEach((r, ri) => r.forEach((c, ci) => {
      if (!c.startsWith("=")) return;
      const v = val(c.slice(1));
      if (Number.isFinite(v)) out.push(`table "${b.title}", row "${r[0]}", column "${b.columns[ci]}" = ${show(v)}`);
    }));
  }
  return out.slice(0, max);
}

/**
 * Apply fixes to a copy of the spec. Each edit's "find" must occur exactly once in
 * one of its claim's fields; a quiz answer must be a valid choice. A fix that breaks
 * either rule is skipped whole (reported in `skipped`), never applied in part.
 */
export function applyFixes(spec: ExplainerSpec, claims: Claim[], fixes: Fix[]): {
  spec: ExplainerSpec; applied: { id: string; where: string; from: string; to: string }[]; skipped: { id: string; why: string }[];
} {
  const byId = new Map(claims.map((c) => [c.id, c]));
  const stages = spec.stages.map((s) => ({ ...s, blocks: [...s.blocks] }));
  const applied: { id: string; where: string; from: string; to: string }[] = [];
  const skipped: { id: string; why: string }[] = [];

  for (const fix of fixes) {
    const c = byId.get(fix.id);
    if (!c) { skipped.push({ id: fix.id, why: "unknown claim" }); continue; }
    const block = stages[c.stage].blocks[c.block];
    const changes: { path: Path; value: unknown }[] = [];
    const notes: { from: string; to: string }[] = [];
    let why = "";
    // A field's text including the edits already queued for it.
    const current = (p: Path) => {
      let v = getAt(block, p) as string;
      for (const ch of changes) if (JSON.stringify(ch.path) === JSON.stringify(p)) v = ch.value as string;
      return v;
    };
    for (const e of fix.edits) {
      if (!e.find || e.find === e.replace) continue;
      const hits = c.fields.filter((p) => typeof getAt(block, p) === "string" && current(p).split(e.find).length === 2);
      if (hits.length !== 1) { why = `"${e.find.slice(0, 60)}" ${hits.length ? "is ambiguous" : "isn't in the claim"}`; break; }
      changes.push({ path: hits[0], value: current(hits[0]).replace(e.find, e.replace) });
      notes.push({ from: e.find, to: e.replace });
    }
    if (!why && fix.answer !== undefined) {
      const q = c.question && (getAt(block, c.question) as { answer: number; choices: string[] } | undefined);
      if (!q || !Number.isInteger(fix.answer) || fix.answer < 0 || fix.answer >= q.choices.length) why = "invalid answer";
      else if (fix.answer !== q.answer) {
        changes.push({ path: [...c.question!, "answer"], value: fix.answer });
        notes.push({ from: `marked answer ${String.fromCharCode(65 + q.answer)}`, to: `marked answer ${String.fromCharCode(65 + fix.answer)}` });
      }
    }
    if (why) { skipped.push({ id: fix.id, why }); continue; }
    if (!changes.length) continue;
    const r = applyChanges(block, changes);
    if ("error" in r) { skipped.push({ id: fix.id, why: r.error }); continue; }
    stages[c.stage].blocks[c.block] = r.block;
    for (const n of notes) applied.push({ id: fix.id, where: stages[c.stage].label, ...n });
  }
  return { spec: { ...spec, stages }, applied, skipped };
}
