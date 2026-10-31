// Worked examples: code computes the numbers, Haiku only writes the steps.
// exampleBriefs() evaluates each stage's quantities (readouts, calculator
// rows, sample stats, live table cells) at every example's settings;
// applyExampleSteps() merges the written steps back into the spec.

import { evalValue, isMat, isNum, tryCompile, type Scope, type Value } from "./expr";
import type { Block, ExplainerSpec } from "./spec";
import { runTrajectory, withDefs } from "./views";

export interface ExampleBrief {
  key: string;
  title: string;
  focus: string;
  settings: { name: string; label: string; value: number }[];
  computed: { label: string; value: string }[];
}

function show(v: Value): string {
  const n = (x: number) => (Number.isFinite(x) ? String(+x.toPrecision(4)) : "undefined");
  if (isNum(v)) return n(v);
  if (isMat(v)) return `${v.length}×${v[0].length} matrix`;
  return `[${v.slice(0, 6).map(n).join(", ")}${v.length > 6 ? ", …" : ""}]`;
}

/** Default value of every control in the explainer (what the page starts with). */
function defaults(spec: ExplainerSpec): { scope: Scope; labels: Map<string, string> } {
  const scope: Scope = { seed: 0 };
  const labels = new Map<string, string>();
  for (const st of spec.stages)
    for (const b of st.blocks)
      if ((b.type === "slider" || b.type === "choice" || b.type === "toggle") && !(b.var in scope)) {
        scope[b.var] = b.value;
        labels.set(b.var, b.label);
      }
  return { scope, labels };
}

/** Whether a formula reads one of `names`, directly or through defs. */
function readsAny(src: string, defs: { name: string; expr: string }[], names: Set<string>): boolean {
  const byName = new Map(defs.map((d) => [d.name, d.expr]));
  const seen = new Set<string>();
  const visit = (s: string): boolean => {
    const r = tryCompile(s);
    if (!r.ok) return false;
    for (const v of r.c.vars) {
      if (names.has(v)) return true;
      const d = byName.get(v);
      if (d !== undefined && !seen.has(v)) {
        seen.add(v);
        if (visit(d)) return true;
      }
    }
    return false;
  };
  return visit(src);
}

/** The quantities a stage shows, evaluated in a given scope. */
export function stageFacts(blocks: Block[], scope: Scope): { label: string; value: string }[] {
  const out: { label: string; value: string }[] = [];
  for (const b of blocks) {
    if (b.type === "readout") {
      const s = withDefs(b.defs, scope);
      for (const it of b.items) out.push({ label: it.label, value: show(evalValue(it.expr, s)) });
    } else if (b.type === "calculator") {
      const s = withDefs(b.defs, scope);
      for (const r of b.rows) out.push({ label: r.latex, value: show(evalValue(r.expr, s)) });
    } else if (b.type === "samples") {
      const s = withDefs(b.defs, scope);
      for (const st of b.stats) out.push({ label: st.label, value: show(evalValue(st.expr, s)) });
    } else if (b.type === "trajectory") {
      const run = runTrajectory(b, scope);
      const last = run.frames[run.frames.length - 1];
      const tag = run.divergedAt > 0 ? ` (diverged at step ${run.divergedAt})` : "";
      for (const st of b.state) out.push({ label: `${b.title}: ${st.name} after ${run.frames.length - 1} steps${tag}`, value: show(last[st.name]) });
    } else if (b.type === "table") {
      for (const row of b.rows)
        row.forEach((cell, c) => {
          if (cell.startsWith("=")) out.push({ label: `${row[0]} (${b.columns[c]})`, value: show(evalValue(cell.slice(1), scope)) });
        });
    } else if (b.type === "bars") {
      const s = withDefs(b.defs, scope);
      if (b.values.length === 1 && b.labels.length !== 1) out.push({ label: b.title || "bars", value: show(evalValue(b.values[0], s)) });
      else b.values.forEach((v, i) => out.push({ label: `${b.title}: ${b.labels[i] ?? `#${i + 1}`}`, value: show(evalValue(v, s)) }));
    } else if (b.type === "scene") {
      // A lab's numbers (readouts, bars, live table cells): a lab may have taken over the stage's readouts. Only those set
      // by the page's controls: one that reads the lab's own variables (a Play counter) isn't fixed by an example's settings.
      const own = new Set(b.state.map((v) => v.name));
      const defs = b.defs ?? [];
      const s = withDefs(defs, { ...scope, ...Object.fromEntries(b.state.map((v) => [v.name, v.value])) });
      const fixed = (src: string) => !readsAny(src, defs, own);
      for (const e of b.elements) {
        if (e.kind === "readout" && e.expr && fixed(e.expr)) out.push({ label: e.label || "value", value: show(evalValue(e.expr, s)) });
        else if (e.kind === "bars" && e.values && fixed(e.values)) out.push({ label: e.label || "bars", value: show(evalValue(e.values, s)) });
        else if (e.kind === "table")
          for (const row of e.rows ?? [])
            row.forEach((cell, c) => {
              if (cell.startsWith("=") && fixed(cell.slice(1))) out.push({ label: `${row[0]} (${e.columns?.[c]})`, value: show(evalValue(cell.slice(1), s)) });
            });
      }
    }
  }
  return out.slice(0, 24);
}

export function exampleBriefs(spec: ExplainerSpec): ExampleBrief[] {
  const { scope, labels } = defaults(spec);
  const briefs: ExampleBrief[] = [];
  for (const st of spec.stages)
    st.blocks.forEach((b, bi) => {
      if (b.type !== "examples") return;
      b.items.forEach((it, ii) => {
        if (it.steps?.length) return; // already written
        const s = { ...scope };
        for (const x of it.set) s[x.var] = x.value;
        briefs.push({
          key: `${st.id}:${bi}:${ii}`,
          title: it.title,
          focus: it.focus,
          settings: it.set.map((x) => ({ name: x.var, label: labels.get(x.var) ?? x.var, value: x.value })),
          computed: stageFacts(st.blocks, s),
        });
      });
    });
  return briefs;
}

export function applyExampleSteps(spec: ExplainerSpec, steps: Map<string, string[]>): ExplainerSpec {
  return {
    ...spec,
    stages: spec.stages.map((st) => ({
      ...st,
      blocks: st.blocks.map((b, bi) => b.type !== "examples" ? b : {
        ...b,
        items: b.items.map((it, ii) => {
          const s = steps.get(`${st.id}:${bi}:${ii}`);
          return s?.length ? { ...it, steps: s.slice(0, 6) } : it;
        }),
      }),
    })),
  };
}
