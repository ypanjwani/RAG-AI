// Lite+: the planner writes the outline in a compact, indented text
// format instead of JSON, and this file turns it back into the same JSON
// object (then parseOutline validates it as usual). It saves the output
// tokens JSON spends on braces, quotes, commas and escaping (LaTeX
// backslashes are written once, not doubled).
//
// Format (compactFormat in prompts/prompts.ts teaches it to the model; the
// server reads it with its Python port, server/explainer/core/compact.py):
//   name: value         everything after the first ": " is the value, literally
//   name: |             multi-line text on the following, deeper-indented lines
//   name:               followed by "- " items (deeper or at the same indent)
//   - name: value       a list item that is an object: its first field; the
//     other: value      other fields follow, aligned with the first
//   name: a | b | c     a short list of plain values on one line (numbers may
//   name: 1, 10, 100    also be separated by commas); table rows likewise
// Fields left out get empty defaults ("" / [] / false) when the schema needs them.
//
// Reading is schema-directed (field types come from the zod schemas), so a
// value is never guessed: "2" stays text where the schema wants text. Anything
// unknown is kept as text, and parseOutline drops what doesn't validate, just
// as it does for JSON.

import { z } from "zod";
import { BlockSchemas, PromptOutline } from "./spec";

// ---------- reading ----------

interface Line { indent: number; text: string; raw: string }
interface Node { line: Line; /** indent used for nesting: "- " lines count one deeper, so a list may sit at its key's indent */ depth: number; children: Node[]; blank: boolean }

/** The value and nested lines of one field (or one list item). */
interface Entry { value: string | undefined; children: Node[] }

const KEY = /^([A-Za-z_][A-Za-z0-9_]*):(?:\s+(.*)|\s*)$/;

function lines(text: string): Line[] {
  return text.replace(/\t/g, "  ").split(/\r?\n/).map((raw) => {
    const indent = raw.length - raw.trimStart().length;
    return { indent, text: raw.trim(), raw };
  });
}

/** Build the indentation tree. Blank lines join the deepest open node (they matter only inside text). */
function tree(all: Line[]): Node[] {
  const root: Node = { line: { indent: -1, text: "", raw: "" }, depth: -2, children: [], blank: false };
  const stack: Node[] = [root];
  for (const line of all) {
    if (!line.text) { stack[stack.length - 1].children.push({ line, depth: Infinity, children: [], blank: true }); continue; }
    const depth = line.indent + (line.text.startsWith("- ") || line.text === "-" ? 1 : 0);
    while (stack.length > 1 && stack[stack.length - 1].depth >= depth) stack.pop();
    const node: Node = { line, depth, children: [], blank: false };
    stack[stack.length - 1].children.push(node);
    stack.push(node);
  }
  return root.children;
}

/** The raw text of nodes and everything under them, dedented, blank lines kept, ends trimmed. */
function rawText(nodes: Node[]): string {
  const flat: Line[] = [];
  const walk = (ns: Node[]) => { for (const n of ns) { flat.push(n.line); walk(n.children); } };
  walk(nodes);
  const ind = Math.min(...flat.filter((l) => l.text).map((l) => l.indent));
  return flat.map((l) => (l.text ? l.raw.slice(Number.isFinite(ind) ? ind : 0).trimEnd() : "")).join("\n").replace(/^\n+|\n+$/g, "");
}

/** Strip quotes the model may add out of habit: a JSON string is decoded, 'single' quotes removed. */
function unquote(v: string): string {
  if (v.length >= 2 && v.startsWith('"') && v.endsWith('"')) {
    try { const s = JSON.parse(v); if (typeof s === "string") return s; } catch { /* not a JSON string */ }
    return v.slice(1, -1);
  }
  if (v.length >= 2 && v.startsWith("'") && v.endsWith("'") && !v.slice(1, -1).includes("'")) return v.slice(1, -1);
  return v;
}

const real = (ns: Node[]) => ns.filter((n) => !n.blank);

/** The fields of an object, from "name: value" lines. */
function fields(nodes: Node[]): Map<string, Entry> {
  const out = new Map<string, Entry>();
  for (const n of real(nodes)) {
    const m = KEY.exec(n.line.text);
    if (m && !out.has(m[1])) out.set(m[1], { value: m[2]?.trim(), children: n.children });
  }
  return out;
}

/** A "- " list item as an object: the first field is on the dash line; deeper lines that follow belong to it. */
function itemFields(n: Node): Map<string, Entry> {
  const first = n.line.text.replace(/^-\s*/, "");
  const col = n.line.indent + (n.line.text.length - first.length); // where the item's fields start
  const own: Node[] = [];
  let i = 0;
  for (; i < n.children.length; i++) {
    const c = n.children[i];
    if (!c.blank && c.depth <= col) break;
    own.push(c);
  }
  const rest = fields(n.children.slice(i));
  const m = KEY.exec(first);
  if (!m) return rest;
  return new Map([[m[1], { value: m[2]?.trim(), children: own }], ...rest]);
}

const kind = (s: any): string => s?.def?.type ?? "unknown";
const unwrap = (s: any): any => (kind(s) === "optional" ? unwrap(s.unwrap()) : s);
const isOptional = (s: any) => kind(s) === "optional";

/** Split at commas outside brackets and parentheses (for a JSON/YAML-style [a, b(c, d)] list). */
function topLevelCommas(v: string): string[] {
  const out: string[] = [];
  let depth = 0, cur = "";
  for (const ch of v) {
    if ("([{".includes(ch)) depth++;
    if (")]}".includes(ch)) depth--;
    if (ch === "," && depth === 0) { out.push(cur); cur = ""; } else cur += ch;
  }
  return [...out, cur];
}

/** Split a one-line list. Numbers may use commas; text uses " | ". A bracketed [a, b] list (a JSON/YAML habit) also works. */
function splitInline(v: string, element: any): string[] {
  const t = kind(unwrap(element));
  if (/^\[.*\]$/.test(v) && t !== "array") {
    try {
      const a = JSON.parse(v);
      if (Array.isArray(a) && a.every((x) => typeof x !== "object")) return a.map(String);
    } catch { /* not JSON: unquoted items */ }
    return topLevelCommas(v.slice(1, -1)).map((p) => unquote(p.trim())).filter(Boolean);
  }
  const parts = t === "number" ? v.split(/\s*[,|]\s*/) : v.split(/\s+\|\s+/);
  return parts.map((p) => p.trim()).filter((p, i, a) => p !== "" || a.length > 1);
}

function scalar(schema: any, e: Entry): unknown {
  const t = kind(schema);
  if (t === "string") {
    const inline = e.value === "|" || e.value === ">" ? "" : unquote(e.value ?? "");
    const more = real(e.children).length ? rawText(e.children) : "";
    return [inline, more].filter(Boolean).join("\n");
  }
  const v = unquote((e.value ?? "").trim());
  if (t === "number") { const n = Number(v); return v !== "" && Number.isFinite(n) ? n : v; }
  if (t === "boolean") return /^(true|yes|on|1)$/i.test(v) ? true : /^(false|no|off|0)$/i.test(v) ? false : v;
  return v; // enum, literal
}

/** Read an Entry as the given schema. */
function read(schema: any, e: Entry): unknown {
  const s = unwrap(schema);
  const t = kind(s);
  if (t === "object") return readObject(s, fields(e.children));
  if (t === "array") {
    const items = real(e.children).filter((n) => n.line.text.startsWith("-"));
    if (items.length) return items.map((n) => readItem(s.element, n));
    // A whole list written as JSON on one line, e.g. weights: [[1, 0.2], [0.5, 1]].
    if (e.value && /^\[[\s\S]*\]$/.test(e.value)) {
      try { const a = JSON.parse(e.value); if (Array.isArray(a)) return a; } catch { /* not JSON: split below */ }
    }
    return e.value ? splitInline(e.value, s.element).map((p) => read(s.element, { value: p, children: [] })) : [];
  }
  if (t === "union") return readBlock(fields(e.children));
  if (t === "unknown" || t === "any") return generic(e);
  return scalar(s, e);
}

/** One "- " list item, read as the list's element schema. */
function readItem(element: any, n: Node): unknown {
  const s = unwrap(element);
  const t = kind(s);
  const text = n.line.text.replace(/^-\s*/, "");
  if (t === "object") return readObject(s, itemFields(n));
  if (t === "union") return readBlock(itemFields(n));
  if (t === "array") return read(s, { value: text, children: n.children });
  return read(s, { value: text, children: n.children });
}

function readObject(s: any, f: Map<string, Entry>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, fs] of Object.entries(s.shape as Record<string, any>)) {
    const e = f.get(k);
    if (e) { out[k] = read(fs, e); continue; }
    if (isOptional(fs)) continue;
    const t = kind(unwrap(fs));
    if (t === "string") out[k] = ""; // left out = empty
    else if (t === "array") out[k] = [];
    else if (t === "boolean") out[k] = false;
  }
  // Unknown fields are kept (as text or nested), so validation reports them instead of silently losing them.
  for (const [k, e] of f) if (!(k in out) && !(k in s.shape)) out[k] = generic(e);
  return out;
}

/** A block: its "type" picks the schema; an unknown type is read generically (normalizeBlock may still fix it). */
function readBlock(f: Map<string, Entry>): unknown {
  const type = unquote(f.get("type")?.value ?? "");
  const schema = (BlockSchemas as Record<string, z.ZodTypeAny>)[type];
  if (schema && type !== "widget") return readObject(schema, f);
  return Object.fromEntries([...f].map(([k, e]) => [k, generic(e)]));
}

/** No schema: text, a list, or an object, judged from the lines. */
function generic(e: Entry): unknown {
  const kids = real(e.children);
  if (!kids.length) return unquote(e.value ?? "");
  if (kids.every((n) => n.line.text.startsWith("-"))) {
    return kids.map((n) => {
      const f = itemFields(n);
      return f.size ? Object.fromEntries([...f].map(([k, x]) => [k, generic(x)])) : unquote(n.line.text.replace(/^-\s*/, ""));
    });
  }
  if (kids.every((n) => KEY.test(n.line.text))) return Object.fromEntries([...fields(e.children)].map(([k, x]) => [k, generic(x)]));
  return scalar(z.string(), e);
}

/** Drop a code fence or a short preamble the model may put around the outline. */
function body(text: string): string {
  let t = text.trim().replace(/^```[a-z]*\s*\n/i, "").replace(/\n```\s*$/, "");
  const start = t.search(/^title:/m);
  if (start > 0) t = t.slice(start);
  return t;
}

/**
 * Read a compact-format outline into the planner's JSON shape (not yet
 * validated: pass it to parseOutline). Throws when there is nothing to read.
 */
export function parseCompact(text: string): Record<string, unknown> {
  const nodes = tree(lines(body(text)));
  const f = fields(nodes);
  if (!f.size) throw new Error("the reply is not in the compact format (no \"name: value\" lines)");
  return rescueGlossary(readObject(PromptOutline, f));
}

/**
 * A misplaced "glossary:" (indented into the last stage, or left out) turns the
 * glossary entries into extra "stages" with no label. Move them back.
 */
function rescueGlossary(o: Record<string, unknown>): Record<string, unknown> {
  const stages = (o.stages ?? []) as Record<string, unknown>[];
  const isTerm = (s: Record<string, unknown>) => !s.label && !s.id && typeof s.term === "string";
  const terms = stages.filter(isTerm);
  if (!terms.length) return o;
  const glossary = [...((o.glossary as unknown[]) ?? []), ...terms.map((t) => ({ term: t.term, definition: t.definition ?? "" }))];
  return { ...o, stages: stages.filter((s) => !isTerm(s)), glossary };
}

// ---------- writing (tests, measurement, and revision requests) ----------

const plain = (v: unknown) => typeof v === "number" || typeof v === "boolean" || typeof v === "string";
/** A one-line value that would read back differently as written (edge spaces, its own quotes, "|"): write it as a JSON string. */
const needsQuotes = (v: string) => v !== v.trim() || v === "|" || v === ">" || /^".*"$|^'.*'$/.test(v);
const one = (v: string) => (needsQuotes(v) ? JSON.stringify(v) : v);
const inlineable = (a: unknown[]) =>
  a.length > 0 && (a.every((x) => typeof x === "number") || a.every((x) => typeof x === "string" && x !== "" && !x.includes("\n") && !x.includes(" | ") && x.length <= 80 && !needsQuotes(x) && !x.startsWith("- ")));

function writeValue(key: string, v: unknown, pad: string, out: string[]) {
  if (v === undefined || v === null || v === "" || v === false || (Array.isArray(v) && !v.length)) return;
  if (typeof v === "string") {
    if (v.includes("\n")) out.push(`${pad}${key}: |`, ...v.split("\n").map((l) => (l ? `${pad}  ${l}` : "")));
    else out.push(`${pad}${key}: ${one(v)}`);
  } else if (plain(v)) out.push(`${pad}${key}: ${v}`);
  else if (Array.isArray(v)) {
    if (inlineable(v)) { out.push(`${pad}${key}: ${v.join(typeof v[0] === "number" ? ", " : " | ")}`); return; }
    out.push(`${pad}${key}:`);
    for (const x of v) writeItem(x, pad, out);
  } else {
    out.push(`${pad}${key}:`);
    writeObject(v as Record<string, unknown>, `${pad}  `, out);
  }
}

function writeItem(x: unknown, pad: string, out: string[]) {
  if (Array.isArray(x)) { out.push(`${pad}- ${x.join(x.every((c) => typeof c === "number") ? ", " : " | ")}`); return; }
  if (typeof x === "string") { out.push(`${pad}- ${x.includes("\n") ? JSON.stringify(x) : one(x)}`); return; }
  if (!x || typeof x !== "object") { out.push(`${pad}- ${x}`); return; }
  const sub: string[] = [];
  writeObject(x as Record<string, unknown>, `${pad}  `, sub);
  if (!sub.length) return;
  sub[0] = `${pad}- ${sub[0].slice(pad.length + 2)}`;
  out.push(...sub);
}

function writeObject(o: Record<string, unknown>, pad: string, out: string[]) {
  // "type" first, so a block's schema is known from its first line.
  const keys = Object.keys(o).sort((a, b) => (b === "type" ? 1 : 0) - (a === "type" ? 1 : 0));
  for (const k of keys) writeValue(k, o[k], pad, out);
}

/** Write an outline (or any JSON object) in the compact format. */
export function toCompact(o: Record<string, unknown>): string {
  const out: string[] = [];
  writeObject(o, "", out);
  return out.join("\n");
}
