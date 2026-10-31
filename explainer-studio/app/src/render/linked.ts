// Linked controls: when a part reads a slider, choice or toggle that lives in another
// section, that section shows a synced copy of the control right above the part, so the
// reader can drive it without scrolling away. Both copies set the same variable in the
// page's store, so moving either moves both. Pure functions (tested in tests/linked.test.ts).

import { tryCompile } from "../../../shared/expr";
import type { Block, ExplainerSpec } from "../../../shared/spec";

type Control = Extract<Block, { type: "slider" | "choice" | "toggle" }>;

const isControl = (b: Block): b is Control => b.type === "slider" || b.type === "choice" || b.type === "toggle";

// The fields that hold formulas (shared/spec.ts), plus a chart's axis variables and a lab's live
// properties. Everything else (labels, prose, a formula's symbol list) is words for the reader.
const FORMULAS = new Set([
  "expr", "values", "from", "to", "init", "steps", "x", "y", "by", "trial", "truth", "markerX", "sweepVar",
  "visible", "disabled", "highlight", "if", "rows",
]);
// Parts that set controls rather than read them (and their "Try it" buttons already move the real slider).
const SETTERS = new Set(["examples", "presets"]);

/** The variables a part's formulas read (labels and text are skipped; table cells count only when they start with "="). */
export function readsVars(b: Block): Set<string> {
  const out = new Set<string>();
  const walk = (v: unknown, key: string) => {
    if (typeof v === "string") {
      if (!FORMULAS.has(key)) return;
      if (key === "rows" && !v.startsWith("=")) return;
      const src = v.startsWith("=") ? v.slice(1) : v;
      if (!src.trim() || src.length > 300) return;
      const r = tryCompile(src);
      if (r.ok) r.c.vars.forEach((x) => out.add(x));
    } else if (Array.isArray(v)) v.forEach((x) => walk(x, key));
    else if (v && typeof v === "object") for (const [k, x] of Object.entries(v)) walk(x, k);
  };
  walk(b, "");
  // Names the part defines itself (defs, a lab's own variables) aren't page controls.
  const own = [
    ...("defs" in b && Array.isArray(b.defs) ? b.defs.map((d) => d.name) : []),
    ...(b.type === "scene" ? b.state.map((s) => s.name) : []),
  ];
  for (const n of own) out.delete(n);
  return out;
}

/**
 * For one section: block index -> the controls from other sections to show above that block
 * (each control at most once per section, before the first part that reads it).
 */
export function linkedControls(spec: ExplainerSpec, stageIndex: number): Map<number, { block: Control; stage: string }[]> {
  const stage = spec.stages[stageIndex];
  const here = new Set(stage.blocks.filter(isControl).map((b) => b.var));
  const elsewhere = new Map<string, { block: Control; stage: string }>();
  spec.stages.forEach((st, si) => {
    if (si === stageIndex) return;
    for (const b of st.blocks) if (isControl(b) && !elsewhere.has(b.var)) elsewhere.set(b.var, { block: b, stage: st.label });
  });
  const out = new Map<number, { block: Control; stage: string }[]>();
  const shown = new Set<string>();
  stage.blocks.forEach((b, i) => {
    if (isControl(b) || SETTERS.has(b.type) || b.type === "widget") return;
    const need = [...readsVars(b)].filter((v) => elsewhere.has(v) && !here.has(v) && !shown.has(v));
    need.forEach((v) => shown.add(v));
    if (need.length) out.set(i, need.map((v) => elsewhere.get(v)!));
  });
  return out;
}
