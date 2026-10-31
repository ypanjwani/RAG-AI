// Edit chat tools: everything the reader can change in one part of a finished
// explainer without AI. Pure functions over blocks; the app's Edit chat panel
// (app/src/shell/edit.ts) offers them as options, and every result goes through
// checkEdit (validate.ts) before it is applied. Writing edits (Haiku) are held to
// writingOk: only text may change.

import { tryCompile } from "./expr";
import { BlockSchemas, type Block, type BlockType, type ExplainerSpec } from "./spec";

export type Path = (string | number)[];

/**
 * What a value in a block is. Only text and prose may change in a Writing edit;
 * id and data values are never shown in the editor.
 */
export type Kind = "text" | "prose" | "latex" | "number" | "answer" | "bool" | "enum" | "expr" | "id" | "data";

export interface Leaf { path: Path; kind: Kind; value: string | number | boolean; options?: string[] }

const EXPR_KEYS = new Set(["expr", "trial", "markerX", "init", "truth", "surface"]);
const PROSE_KEYS = new Set(["md", "back", "explain", "desc", "detail", "focus", "meaning"]);
const ID_KEYS = new Set(["var", "id", "stage", "sweepVar"]);

/** A path's last named key (array indices skipped), e.g. ["series", 0, "expr"] → "expr", ["labels", 2] → "labels". */
const keyOf = (path: Path) => [...path].reverse().find((k): k is string => typeof k === "string") ?? "";

function kindOf(type: BlockType, path: Path, value: unknown): Kind {
  const key = keyOf(path);
  const top = path[0];
  if (typeof value === "boolean") return "bool";
  if (enumOptions(type, path)) return "enum";
  if (typeof value === "number") {
    if (key === "answer") return "answer";
    if (type === "attention" || (type === "sampling" && key === "logit")) return "data";
    return "number";
  }
  const s = String(value);
  if (ID_KEYS.has(key)) return "id";
  // A chart's axes name slider variables (a heatmap's or trajectory's x/y are formulas instead).
  if (type === "chart" && (key === "x" || key === "by")) return "id";
  // A scene's ids, targets, axes and fixed choices (enums inside its lists aren't found by enumOptions); its live properties are formulas.
  if (type === "scene") {
    if (["target", "x", "by", "kind", "when", "view", "format"].includes(key)) return "id";
    if (["visible", "disabled", "highlight", "if", "values"].includes(key)) return "expr";
    if (key === "rows" && s.startsWith("=")) return "expr";
  }
  // Names of derived and state variables: formulas refer to them, so renaming one would break them.
  if (key === "name" && (top === "defs" || top === "state" || top === "update")) return "id";
  if ((type === "conceptmap" || type === "architecture") && top === "edges" && (key === "from" || key === "to")) return "id";
  if (key === "latex" || key === "symbol") return "latex";
  if (EXPR_KEYS.has(key)) return "expr";
  if (type === "trajectory" && path.length === 1 && key === "steps") return "expr";
  if ((key === "from" || key === "to") && (top === "shade" || top === "band")) return "expr";
  if ((key === "x" || key === "y") && (type === "trajectory" || type === "heatmap")) return "expr";
  if (type === "bars" && top === "values") return "expr";
  if (type === "samples" && key === "values") return "expr";
  if (type === "table" && top === "rows" && s.startsWith("=")) return "expr";
  if ((type === "tokens" && top === "tokens") || ((type === "attention" || type === "sampling") && (top === "tokens" || key === "token"))) return "data";
  if (PROSE_KEYS.has(key) || (type === "reveal" && key === "answer")) return "prose";
  return "text";
}

/** Menu options for an enum field, read from the block's zod schema so they never drift. */
export function enumOptions(type: BlockType, path: Path): string[] | undefined {
  const unwrap = (s: any) => { while (s && !Array.isArray(s.options) && typeof s.unwrap === "function") s = s.unwrap(); return s; };
  let s: any = BlockSchemas[type];
  for (const k of path) {
    s = unwrap(s);
    s = typeof k === "number" ? s?.element : s?.shape?.[k];
    if (!s) return undefined;
  }
  s = unwrap(s);
  return Array.isArray(s?.options) && s.options.every((o: unknown) => typeof o === "string") ? s.options : undefined;
}

/** Every leaf value of a block with its kind, in document order. */
export function leaves(b: Block): Leaf[] {
  const out: Leaf[] = [];
  const walk = (v: unknown, path: Path) => {
    if (v === undefined || v === null) return;
    if (Array.isArray(v)) { v.forEach((x, i) => walk(x, [...path, i])); return; }
    if (typeof v === "object") { for (const [k, x] of Object.entries(v)) if (!(path.length === 0 && k === "type")) walk(x, [...path, k]); return; }
    const kind = kindOf(b.type, path, v);
    out.push({ path, kind, value: v as Leaf["value"], ...(kind === "enum" ? { options: enumOptions(b.type, path) } : {}) });
  };
  walk(b, []);
  return out;
}

// ---------- reading and writing by path ----------

export function getAt(b: unknown, path: Path): unknown {
  return path.reduce<any>((o, k) => (o == null ? undefined : o[k]), b);
}

/** A copy of the block with the value at path replaced (structural sharing elsewhere). */
export function setAt<T>(b: T, path: Path, value: unknown): T {
  if (!path.length) return value as T;
  const [k, ...rest] = path;
  const src: any = b;
  const copy: any = Array.isArray(src) ? [...src] : { ...src };
  copy[k] = setAt(src?.[k], rest, value);
  return copy;
}

export type ListOp = "remove" | "up" | "down" | "duplicate";

/** Remove, move or duplicate the array element at path (the last path item is its index). */
export function listOp<T>(b: T, path: Path, op: ListOp): T {
  const i = path.at(-1) as number;
  const arrPath = path.slice(0, -1);
  const arr = [...(getAt(b, arrPath) as unknown[])];
  if (op === "remove") arr.splice(i, 1);
  if (op === "duplicate") arr.splice(i + 1, 0, structuredClone(arr[i]));
  const j = op === "up" ? i - 1 : op === "down" ? i + 1 : -1;
  if (j >= 0 && j < arr.length) [arr[i], arr[j]] = [arr[j], arr[i]];
  return setAt(b, arrPath, arr);
}

/** Lists whose items the reader may add, remove or reorder (columns would desync table rows). */
export function listEditable(b: Block, arrPath: Path): boolean {
  const key = keyOf(arrPath);
  if (["columns", "weights", "defs"].includes(key)) return false;
  if (b.type === "table" && arrPath.length > 1) return false; // cells inside a row
  return true;
}

// ---------- labels ----------

const NAMES: Record<string, string> = {
  md: "Text", q: "Question", expr: "Formula", markerX: "Marker at x", xMin: "X from", xMax: "X to", yMin: "Y from", yMax: "Y to",
  xLabel: "X axis label", yLabel: "Y axis label", zLabel: "Colour label", logY: "Log scale (y)", showValidation: "Show validation curve",
  normalize: "Show as % of total", diverging: "Two-colour scale", digits: "Decimals", value: "Value", valueLabel: "What one run measures",
  breaksIf: "Breaks when", front: "Front", back: "Back", explain: "Explanation", latex: "Maths", sub: "Subtitle", desc: "Description",
  init: "Start value", trial: "One run", truth: "True value", repeats: "Run buttons", on: "On label", off: "Off label",
  lesson: "What it should show", view: "Chart view", at: "Values shown",
};
const SINGULAR: Record<string, string> = { series: "Series", items: "Item", questions: "Question", cards: "Card", tabs: "Tab", options: "Option", nodes: "Node", edges: "Link", rows: "Row", steps: "Step", points: "Point", choices: "Choice", labels: "Label", values: "Value", stats: "Statistic", markers: "Marker", candidates: "Candidate", state: "State variable", update: "Update rule", terms: "Symbol", outcomes: "Outcome", tokens: "Token" };

export const humanize = (k: string) => NAMES[k] ?? (k.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/^./, (c) => c.toUpperCase()));
export const itemName = (listKey: string, i: number) => `${SINGULAR[listKey] ?? humanize(listKey)} ${i + 1}`;

/** "Plot “Loss vs rate”", for the panel header and messages. */
export function blockName(b: Block): string {
  const NICE: Partial<Record<BlockType, string>> = { plot: "Line plot", bars: "Bar chart", samples: "Histogram", heatmap: "Heatmap", trajectory: "Trajectory", flipcards: "Flip cards", cards: "Cards", text: "Paragraph", callout: "Callout", readout: "Live numbers", presets: "Preset buttons", examples: "Worked examples", conceptmap: "Concept map", calculator: "Calculator", experiment: "Experiment", chart: "Chart", scene: "Lab" };
  const title = "title" in b && b.title ? ` “${b.title}”` : "label" in b && typeof b.label === "string" && b.label ? ` “${b.label}”` : "";
  return `${NICE[b.type] ?? humanize(b.type)}${title}`;
}

// ---------- change type (no AI: only conversions where the data really fits) ----------

export interface Conversion { label: string; block: Block }

const stripMd = (s: string) => s.replace(/[*_`$]/g, "").trim();

export function conversions(b: Block): Conversion[] {
  const out: Conversion[] = [];
  switch (b.type) {
    case "bars": {
      const perLabel = b.values.length === b.labels.length && b.values.length > 1;
      if (perLabel) out.push({ label: "Live numbers", block: { type: "readout", defs: b.defs, items: b.labels.map((l, i) => ({ label: l, expr: b.values[i], digits: 2 })) } });
      if (perLabel && !b.defs?.length) out.push({ label: "Table", block: { type: "table", title: b.title, columns: ["", "Value"], rows: b.labels.map((l, i) => [l, `=${b.values[i]}`]), caption: "" } });
      break;
    }
    case "readout": {
      if (b.items.length > 1) out.push({ label: "Bar chart", block: { type: "bars", title: "", labels: b.items.map((it) => it.label), defs: b.defs, values: b.items.map((it) => it.expr), normalize: false } });
      if (!b.defs?.length) out.push({ label: "Table", block: { type: "table", title: "", columns: ["", "Value"], rows: b.items.map((it) => [it.label, `=${it.expr}`]), digits: Math.max(...b.items.map((it) => it.digits)), caption: "" } });
      break;
    }
    case "table": {
      b.columns.forEach((col, c) => {
        if (c === 0) return;
        const cells = b.rows.map((r) => r[c] ?? "");
        const numeric = cells.every((x) => x.startsWith("=") || Number.isFinite(Number(x.replace(/[,%×x]/g, ""))) && x.trim() !== "");
        if (!numeric || b.rows.length < 2) return;
        out.push({
          label: `Bar chart of “${stripMd(col) || `column ${c + 1}`}”`,
          block: { type: "bars", title: b.title || stripMd(col), labels: b.rows.map((r) => stripMd(r[0] ?? "")), values: cells.map((x) => (x.startsWith("=") ? x.slice(1) : String(Number(x.replace(/[,%×x]/g, ""))))), normalize: false },
        });
      });
      break;
    }
    case "heatmap": {
      const mid = (b.yMin + b.yMax) / 2;
      const y = b.marker ? b.marker.y : String(Number(mid.toPrecision(4)));
      out.push({
        label: `Line plot (a slice at ${b.yLabel || "y"} = ${b.marker ? "the marker" : Number(mid.toPrecision(4))})`,
        block: { type: "plot", title: b.title, xLabel: b.xLabel, yLabel: b.zLabel, xMin: b.xMin, xMax: b.xMax, yMin: 0, yMax: 0, defs: [...(b.defs ?? []), { name: "y", expr: y }], series: [{ label: b.zLabel || "value", expr: b.expr }], markerX: b.marker ? b.marker.x : "" },
      });
      break;
    }
    case "plot": {
      if (!b.shade) out.push({ label: "Line plot with the area under it shaded", block: { ...b, shade: { series: 0, from: String(b.xMin), to: String(b.xMax), label: "Area under the curve" } } });
      else out.push({ label: "Plain line plot (no shaded area)", block: { ...b, shade: undefined } });
      break;
    }
    case "flipcards":
      out.push({ label: "Cards (always open)", block: { type: "cards", variant: "definition", title: "", items: b.cards.map((c) => ({ title: c.front, md: c.back })) } });
      break;
    case "cards":
      out.push({ label: "Flip cards", block: { type: "flipcards", cards: b.items.map((it) => ({ front: it.title, back: it.md })) } });
      break;
    case "reveal":
      out.push({ label: "Flip card", block: { type: "flipcards", cards: [{ front: b.prompt, back: b.answer }] } });
      break;
    case "compare": {
      const n = Math.max(b.left.points.length, b.right.points.length);
      out.push({ label: "Table", block: { type: "table", title: "", columns: [b.left.title, b.right.title], rows: Array.from({ length: n }, (_, i) => [b.left.points[i] ?? "", b.right.points[i] ?? ""]), caption: "" } });
      break;
    }
    case "predict":
      out.push({ label: "Quiz question", block: { type: "quiz", questions: [{ q: b.question, choices: b.choices, answer: b.answer, explain: stripMd(b.explain) }] } });
      break;
    case "quiz":
      if (b.questions.length === 1) {
        const q = b.questions[0];
        out.push({ label: "Predict first (guess, then see why)", block: { type: "predict", question: q.q, choices: q.choices, answer: q.answer, explain: q.explain } });
      }
      break;
  }
  return out;
}

// ---------- add buttons: save the current slider settings as a preset ----------

const isControl = (b: Block): b is Extract<Block, { type: "slider" | "choice" | "toggle" }> =>
  b.type === "slider" || b.type === "choice" || b.type === "toggle";

/** Control variables a block reads (or sets, for a control). */
export function varsOfBlock(b: Block, declared: Set<string>): string[] {
  if (isControl(b)) return [b.var];
  const out = new Set<string>();
  for (const l of leaves(b)) {
    if (l.kind !== "expr") continue;
    const r = tryCompile(String(l.value).replace(/^=/, ""));
    if (r.ok) for (const v of r.c.vars) if (declared.has(v)) out.add(v);
  }
  return [...out];
}

/** The variables a preset made from this part would set: the stage's controls plus any this part reads. */
export function presetVars(stageBlocks: Block[], all: Block[], index: number): string[] {
  const declared = new Set(all.filter(isControl).map((b) => b.var));
  const vars = new Set(stageBlocks.filter(isControl).map((b) => b.var));
  for (const v of varsOfBlock(stageBlocks[index], declared)) vars.add(v);
  return [...vars].slice(0, 8);
}

/**
 * Add a button that loads these values: into the stage's existing preset block,
 * or a new one placed after the stage's last control (so it joins that panel).
 */
export function addPreset(stageBlocks: Block[], index: number, name: string, set: { var: string; value: number }[]): Block[] {
  const item = { label: name, set };
  const at = stageBlocks.findIndex((b) => b.type === "presets");
  if (at >= 0) return stageBlocks.map((b, i) => (i === at && b.type === "presets" ? { ...b, items: [...b.items, item] } : b));
  const lastControl = stageBlocks.reduce((k, b, i) => (isControl(b) ? i : k), -1);
  const pos = lastControl >= 0 ? lastControl + 1 : index;
  const next = [...stageBlocks];
  next.splice(pos, 0, { type: "presets", label: "Try these settings", items: [item] });
  return next;
}

// ---------- Edit chat: what Haiku sees and what its tools may do ----------

const WRITABLE = new Set<Kind>(["text", "prose"]);
/** Kinds Haiku (and the settings form) may change; ids and data stay fixed. */
const EDITABLE = new Set<Kind>(["text", "prose", "latex", "number", "answer", "bool", "enum", "expr"]);
const KIND_NAME: Record<Kind, string> = { text: "text", prose: "markdown", latex: "latex", number: "number", answer: "correct-choice index", bool: "true/false", enum: "one of", expr: "formula", id: "locked", data: "locked" };

export interface Target { stage: number; block: number }

/** Lists in a block that list_edit may change, with their sizes. */
function lists(b: Block): { path: Path; size: number }[] {
  const out: { path: Path; size: number }[] = [];
  const walk = (v: unknown, path: Path) => {
    if (Array.isArray(v)) {
      if (path.length && listEditable(b, path) && v.length) out.push({ path, size: v.length });
      v.forEach((x, i) => walk(x, [...path, i]));
    } else if (v && typeof v === "object") for (const [k, x] of Object.entries(v)) walk(x, [...path, k]);
  };
  walk(b, []);
  return out;
}

const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n)}…` : s);

/**
 * The selected part, described for Haiku: every value it may change with its
 * path and kind, the lists it may edit, the graph conversions on offer and the
 * page's controls (for add_button). Rebuilt for every turn, so it is always current.
 */
export function editContext(spec: ExplainerSpec, t: Target, value: (v: string) => number): string {
  const st = spec.stages[t.stage];
  const b = st.blocks[t.block];
  const lines: string[] = [];
  lines.push(`Explainer: "${spec.title}" (reader level: ${spec.level})`);
  lines.push(`Selected part: ${blockName(b)} (type ${b.type}), part ${t.block + 1} of ${st.blocks.length} in step ${t.stage + 1} "${st.label}".`);
  lines.push(`Parts in this step: ${st.blocks.map((x, i) => `${i + 1}. ${blockName(x)}${i === t.block ? " [selected]" : ""}`).join("; ")}`);
  lines.push("", "Values (set_values; path is a JSON array):");
  for (const l of leaves(b)) {
    if (!EDITABLE.has(l.kind)) continue;
    const v = typeof l.value === "string" ? JSON.stringify(clip(l.value, 1500)) : String(l.value);
    lines.push(`${JSON.stringify(l.path)} ${KIND_NAME[l.kind]}${l.options ? ` [${l.options.join(", ")}]` : ""}: ${v}`);
  }
  const ls = lists(b);
  lines.push("", `Lists (list_edit): ${ls.length ? ls.map((l) => `${JSON.stringify(l.path)} (${l.size} items)`).join(", ") : "none"}`);
  const convs = conversions(b);
  lines.push(`Other forms for this part (change_type option): ${convs.length ? convs.map((c, i) => `${i} = ${c.label}`).join("; ") : "none, its data doesn't fit another graph or layout"}`);
  const all = spec.stages.flatMap((s) => s.blocks);
  const vars = presetVars(st.blocks, all, t.block);
  const controls = all.filter(isControl);
  lines.push(`Page controls (add_button settings): ${vars.length ? vars.map((v) => {
    const c = controls.find((x) => x.var === v)!;
    const range = c.type === "slider" ? `slider ${c.min} to ${c.max}` : c.type === "toggle" ? "switch 0 or 1" : `choice of ${c.options.map((o) => o.value).join("/")}`;
    return `${v} "${c.label}" ${range}, now ${value(v)}`;
  }).join("; ") : "none in this step"}`);
  return lines.join("\n");
}

/**
 * Apply set_values changes to a copy of the block. Only existing, editable
 * paths may change and each value must match its kind. Returns the new block
 * and whether any text changed (text edits are shown for approval first).
 */
export function applyChanges(b: Block, changes: { path: Path; value: unknown }[]): { block: Block; textChanged: boolean } | { error: string } {
  let next = b;
  let textChanged = false;
  for (const ch of changes) {
    if (!Array.isArray(ch.path) || !ch.path.length) return { error: "Each change needs a path (a JSON array)." };
    const leaf = leaves(next).find((l) => JSON.stringify(l.path) === JSON.stringify(ch.path));
    if (!leaf) return { error: `${JSON.stringify(ch.path)} is not a value of this part. Use a path from the Values list.` };
    if (!EDITABLE.has(leaf.kind)) return { error: `${fieldLabel(ch.path)} is locked (variable names and ids can't change).` };
    let v = ch.value;
    if (leaf.kind === "number" || leaf.kind === "answer") {
      v = typeof v === "string" ? Number(v) : v;
      if (typeof v !== "number" || !Number.isFinite(v)) return { error: `${fieldLabel(ch.path)} must be a number.` };
    } else if (leaf.kind === "bool") {
      if (typeof v !== "boolean") return { error: `${fieldLabel(ch.path)} must be true or false.` };
    } else {
      if (typeof v !== "string") return { error: `${fieldLabel(ch.path)} must be text.` };
      if (leaf.kind === "enum" && !leaf.options?.includes(v)) return { error: `${fieldLabel(ch.path)} must be one of: ${leaf.options?.join(", ")}.` };
    }
    if (WRITABLE.has(leaf.kind) && v !== leaf.value) textChanged = true;
    next = setAt(next, ch.path, v);
  }
  return { block: next, textChanged };
}

/** Starter suggestions for the selected part, as plain requests the reader can click. */
export function suggestions(spec: ExplainerSpec, t: Target): string[] {
  const b = spec.stages[t.stage].blocks[t.block];
  const out: string[] = [];
  const ls = leaves(b);
  if (conversions(b).length) out.push("Show it as a different graph");
  if (ls.some((l) => l.kind === "number" && /^(x|y)(Min|Max)$|^(min|max)$/.test(keyOf(l.path)))) out.push("Change the range");
  if (presetVars(spec.stages[t.stage].blocks, spec.stages.flatMap((s) => s.blocks), t.block).length) out.push("Add a button with good settings");
  if (ls.some((l) => WRITABLE.has(l.kind))) out.push("Make the wording simpler");
  out.push("What can I change here?");
  return out.slice(0, 5);
}

/** The text that changed in a Writing edit, for the before/after preview. */
export function textChanges(before: Block, after: Block): { where: string; from: string; to: string }[] {
  const old = new Map(leaves(before).map((l) => [JSON.stringify(l.path), l]));
  const out: { where: string; from: string; to: string }[] = [];
  for (const l of leaves(after)) {
    if (!WRITABLE.has(l.kind)) continue;
    const o = old.get(JSON.stringify(l.path));
    if (o?.value === l.value) continue;
    out.push({ where: fieldLabel(l.path), from: o ? String(o.value) : "", to: String(l.value) });
  }
  // Text that disappeared (a removed card or point).
  const now = new Set(leaves(after).map((l) => JSON.stringify(l.path)));
  for (const l of leaves(before)) if (WRITABLE.has(l.kind) && !now.has(JSON.stringify(l.path))) out.push({ where: fieldLabel(l.path), from: String(l.value), to: "" });
  return out;
}

/** "Question 2 › Choice 3", "X axis label". */
export function fieldLabel(path: Path): string {
  const parts: string[] = [];
  for (let i = 0; i < path.length; i++) {
    const k = path[i];
    if (typeof k === "number") continue;
    const next = path[i + 1];
    parts.push(typeof next === "number" ? itemName(k, next) : humanize(k));
  }
  return parts.join(" › ") || "Value";
}
