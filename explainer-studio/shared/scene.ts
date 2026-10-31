// Lab mode's scene block: one section's interaction as state, elements with live
// formula properties, and rules "when <trigger> [if <condition>], set <var> = <formula>".
// SceneSim is the pure rule runner shared by the checks below (a dry run) and the
// browser engine (app/src/render/components/scene.ts), so both behave the same.
// Ported in server/explainer/core/scene.py (parity-tested, warnings word for word).

import { evalNumber, evalValue, isMat, isNum, type Scope, type Value } from "./expr";
import type { Block } from "./spec";
import { withDefs } from "./views";

export type SceneBlock = Extract<Block, { type: "scene" }>;
type Element = SceneBlock["elements"][number];
type Rule = SceneBlock["rules"][number];
type StateVar = SceneBlock["state"][number];
type Def = { name: string; expr: string };
type Warn = (m: string) => void;

export const SCENE_LIMITS = { state: 8, elements: 10, rules: 12, timers: 2, minMs: 30, firings: 50, dryRunMs: 3000 };
const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;
const RESERVED = new Set(["x", "y", "seed", "i", "row"]);
/** What a click on each kind of element passes to its rules. */
const CLICK_VARS: Partial<Record<Element["kind"], string>> = { chart: "x", bars: "i", table: "row" };
const CLICKABLE = new Set<Element["kind"]>(["button", "chart", "bars", "table"]);
const VISUALS = new Set<Element["kind"]>(["chart", "readout", "bars", "table", "text"]);
const PLACEHOLDER = /\{\{([^{}]*)\}\}/g;

/** The validator's helpers the scene checks reuse (passed in to avoid an import cycle). */
export interface SceneHelpers {
  checkExpr(src: string, allowed: Set<string>): string | null;
  cleanDefs(defs: Def[] | undefined, vars: Set<string>, warn: Warn, where: string): { defs: Def[]; scope: Set<string> };
  repairBlock(b: Block, vars: Set<string>, warn: Warn): Block | null;
  sanityCheck(b: Block, scopes: Scope[], warn: Warn, sliders: Map<string, Extract<Block, { type: "slider" }>>): Block | null;
  varsOf(src: string, defs?: Def[]): Set<string>;
}

const truthy = (v: number) => Number.isFinite(v) && v !== 0;
const clamp = (v: number, s: StateVar) => Math.min(s.max, Math.max(s.min, v));
const finite = (n: number, d: number) => (Number.isFinite(n) ? n : d);

// ---------- the rule runner ----------

export interface FireResult { firings: number; overflow: boolean; bad: { rule: number; var: string }[]; culprit: number }

/**
 * Runs a scene's rules over its state. `page()` gives the page's current values (its controls).
 * A rule's assignments are all computed first, then applied (clamped to each variable's range);
 * each variable that changes raises a "change" event for the rules listening to it. At most
 * SCENE_LIMITS.firings rule runs per event, so a loop can't hang the page.
 */
export class SceneSim {
  vars: Scope = {};
  private byName = new Map<string, StateVar>();

  constructor(private b: SceneBlock, private page: () => Scope) {
    for (const s of b.state) { this.vars[s.name] = s.value; this.byName.set(s.name, s); }
  }

  /** Page values, then state, then the scene's defs, plus event variables. */
  scope(extra: Scope = {}): Scope {
    return withDefs(this.b.defs, { ...this.page(), ...this.vars, ...extra });
  }

  /** A formula property: true when absent, else when it gives a non-zero number. */
  holds(src: string | undefined, extra: Scope = {}): boolean {
    return !src ? true : truthy(evalNumber(src, this.scope(extra)));
  }

  /** Run the rules a trigger starts (and every change they cause). */
  fire(when: "click" | "change" | "start", target = "", extra: Scope = {}): FireResult {
    return this.run(this.b.rules.map((r, i) => ({ r, i })).filter(({ r }) => r.when === when && (when === "start" || r.target === target)), extra);
  }

  /** One tick of an "every" rule (by its index). */
  tick(index: number): FireResult {
    return this.run([{ r: this.b.rules[index], i: index }], {});
  }

  private run(first: { r: Rule; i: number }[], extra: Scope): FireResult {
    const out: FireResult = { firings: 0, overflow: false, bad: [], culprit: -1 };
    const count = new Map<number, number>();
    const queue: { r: Rule; i: number; extra: Scope }[] = first.map((x) => ({ ...x, extra }));
    while (queue.length) {
      const { r, i, extra: ex } = queue.shift()!;
      if (++out.firings > SCENE_LIMITS.firings) {
        // The loop's culprit: the rule that ran most often (first such rule on a tie).
        out.overflow = true;
        out.culprit = [...count.entries()].reduce((best, e) => (e[1] > best[1] ? e : best), [-1, 0])[0];
        break;
      }
      count.set(i, (count.get(i) ?? 0) + 1);
      if (!this.holds(r.if, ex)) continue;
      const sc = this.scope(ex);
      const values = r.set.map((a) => evalNumber(a.expr, sc));
      const changed: string[] = [];
      r.set.forEach((a, k) => {
        const s = this.byName.get(a.var);
        if (!s) return;
        if (!Number.isFinite(values[k])) { out.bad.push({ rule: i, var: a.var }); return; }
        const v = clamp(values[k], s);
        if (this.vars[a.var] !== v) { this.vars[a.var] = v; if (!changed.includes(a.var)) changed.push(a.var); }
      });
      for (const name of changed)
        this.b.rules.forEach((r2, i2) => { if (r2.when === "change" && r2.target === name) queue.push({ r: r2, i: i2, extra: {} }); });
    }
    return out;
  }
}

// ---------- structural repair ----------

/** The chart block a chart element stands for (it shares the scene's defs). */
export function chartOf(e: Element, defs: Def[]): Extract<Block, { type: "chart" }> {
  return {
    type: "chart", title: e.label ?? "", lesson: "", defs, series: e.series ?? [], x: e.x ?? "", by: e.by ?? "",
    at: e.at ?? [], view: e.view ?? "line", labels: e.labels ?? [], caption: "",
  };
}

const placeholders = (md: string) => [...md.matchAll(PLACEHOLDER)].map((m) => m[1]);

export function repairScene(b: SceneBlock, vars: Set<string>, warn: Warn, H: SceneHelpers): SceneBlock | null {
  const name = `Scene "${b.title}"`;
  // State: the scene's own variables (never a page control's name).
  const state: StateVar[] = [];
  for (const s of b.state.slice(0, SCENE_LIMITS.state)) {
    const why = !IDENT.test(s.name) || RESERVED.has(s.name) ? "it isn't a usable name"
      : vars.has(s.name) ? "a page control already uses that name"
      : state.some((t) => t.name === s.name) ? "it is declared twice" : "";
    if (why) { warn(`${name}: dropped state variable "${s.name}" (${why}).`); continue; }
    let min = finite(s.min, 0), max = finite(s.max, 1);
    if (min > max) [min, max] = [max, min];
    if (min === max) max = min + 1;
    let step = finite(s.step, 0);
    if (step <= 0 || step > max - min) step = (max - min) / 100;
    state.push({ ...s, min, max, step, value: Math.min(max, Math.max(min, finite(s.value, min))) });
  }
  const names = new Set(state.map((s) => s.name));
  const { defs, scope } = H.cleanDefs(b.defs, new Set([...vars, ...names]), warn, `"${b.title}"`);

  const formulaOk = (src: string | undefined, allowed: Set<string>) => !src || H.checkExpr(src, allowed) === null;
  const elements: Element[] = [];
  for (const e of b.elements.slice(0, SCENE_LIMITS.elements)) {
    const drop = (why: string) => warn(`${name}: dropped ${e.kind} "${e.id}" (${why}).`);
    if (!IDENT.test(e.id) || elements.some((o) => o.id === e.id)) { drop("its id is missing or repeated"); continue; }
    let el: Element = { ...e };
    if (e.kind === "slider" && !(e.var && names.has(e.var))) { drop("it must set one of the scene's state variables"); continue; }
    if (e.kind === "chart") {
      const c = H.repairBlock(chartOf(e, defs), scope, warn);
      if (!c || c.type !== "chart") { drop("its chart isn't valid"); continue; }
      el = { ...el, series: c.series, x: c.x, by: c.by, at: c.at, labels: c.labels, view: c.view };
    }
    if (e.kind === "readout" && !(e.expr && formulaOk(e.expr, scope))) { drop("its formula isn't valid"); continue; }
    if (e.kind === "bars" && !(e.values && formulaOk(e.values, scope))) { drop("its formula isn't valid"); continue; }
    if (e.kind === "table") {
      const cols = (e.columns ?? []).slice(0, 8);
      if (!cols.length) { drop("it has no columns"); continue; }
      const rows = (e.rows ?? []).slice(0, 24).map((r) => r.slice(0, cols.length).map((c) => (c.startsWith("=") && !formulaOk(c.slice(1), scope) ? "—" : c)));
      el = { ...el, columns: cols, rows };
    }
    if (e.kind === "text") {
      if (!e.md) { drop("it has no text"); continue; }
      el = { ...el, md: e.md.replace(PLACEHOLDER, (m, f: string) => (formulaOk(f, scope) ? m : "—")) };
    }
    // Live properties that don't compile are removed (the element then always shows / is enabled / has no highlight).
    for (const key of ["visible", "disabled", "highlight"] as const) {
      if (el[key] && !formulaOk(el[key], scope)) { warn(`${name}: removed the ${key} formula of "${e.id}".`); el = { ...el, [key]: undefined }; }
    }
    elements.push(el);
  }
  if (!elements.some((e) => VISUALS.has(e.kind))) { warn(`Dropped scene "${b.title}": nothing in it shows anything.`); return null; }

  const kindOf = new Map(elements.map((e) => [e.id, e.kind]));
  const rules: Rule[] = [];
  let timers = 0;
  b.rules.slice(0, SCENE_LIMITS.rules).forEach((r, n) => {
    const drop = (why: string) => warn(`${name}: dropped rule ${n + 1} (${why}).`);
    const target = r.target ?? "";
    if (r.when === "click" && !CLICKABLE.has(kindOf.get(target)!)) return drop(`"${target}" isn't a button, chart, bar chart or table in the scene`);
    if (r.when === "change" && !(names.has(target) || vars.has(target))) return drop(`"${target}" isn't a variable`);
    if (r.when === "every" && ++timers > SCENE_LIMITS.timers) return drop(`at most ${SCENE_LIMITS.timers} timers`);
    const event = r.when === "click" ? CLICK_VARS[kindOf.get(target)!] : undefined;
    const allowed = event ? new Set([...scope, event]) : scope;
    if (!formulaOk(r.if, allowed)) return drop("its condition isn't valid");
    const set = r.set.filter((a) => names.has(a.var) && formulaOk(a.expr, allowed));
    if (set.length < r.set.length) warn(`${name}: rule ${n + 1} lost ${r.set.length - set.length} assignment(s) (only valid formulas setting the scene's own variables are kept).`);
    if (!set.length) return drop("it sets nothing");
    rules.push({ ...r, target: r.when === "start" || r.when === "every" ? undefined : target, ms: r.when === "every" ? Math.max(SCENE_LIMITS.minMs, Math.round(finite(r.ms ?? 500, 500))) : undefined, set });
  });
  return { ...b, state, defs, elements, rules };
}

// ---------- numeric check, usage and dry run ----------

const finiteNums = (v: Value) => (isNum(v) ? [v] : isMat(v) ? v.flat() : Array.isArray(v) ? v : []).filter(Number.isFinite);

/** Formulas an element reads (what it shows, plus its live properties). */
function formulasOf(e: Element): string[] {
  const own = [e.visible, e.disabled, e.highlight, e.expr, e.values, ...(e.series ?? []).map((s) => s.expr), e.x, e.by,
    ...(e.rows ?? []).flat().filter((c) => c.startsWith("=")).map((c) => c.slice(1)), ...(e.md ? placeholders(e.md) : [])];
  return own.filter((s): s is string => !!s);
}

export function checkScene(b: SceneBlock, scopes: Scope[], warn: Warn, sliders: Map<string, Extract<Block, { type: "slider" }>>, H: SceneHelpers): SceneBlock | null {
  const name = `Scene "${b.title}"`;
  const defaults: Scope = Object.fromEntries(b.state.map((s) => [s.name, s.value]));
  // Probe at the page's scopes with the state at its defaults, and each state variable at its min and max.
  const scs: Scope[] = [...scopes.map((sc) => ({ ...sc, ...defaults }))];
  for (const s of b.state) scs.push({ ...scopes[0], ...defaults, [s.name]: s.min }, { ...scopes[0], ...defaults, [s.name]: s.max });
  const withD = scs.map((sc) => withDefs(b.defs, sc));
  const alive = (src: string) => withD.some((sc) => finiteNums(evalValue(src, sc)).length > 0);
  const merged = new Map(sliders);
  for (const s of b.state) merged.set(s.name, { type: "slider", var: s.name, label: s.label, min: s.min, max: s.max, step: s.step, value: s.value, unit: "" });

  let elements: Element[] = [];
  for (const e of b.elements) {
    const drop = (why: string) => warn(`${name}: dropped ${e.kind} "${e.id}" (${why}).`);
    if (e.kind === "chart") {
      const c = H.sanityCheck(chartOf(e, b.defs ?? []), scs, warn, merged);
      if (!c || c.type !== "chart") { drop("its numbers never work out"); continue; }
      elements.push({ ...e, series: c.series, x: c.x, by: c.by, at: c.at, view: c.view });
      continue;
    }
    if (e.kind === "readout" && !alive(e.expr!)) { drop("its numbers never work out"); continue; }
    if (e.kind === "bars" && !alive(e.values!)) { drop("its numbers never work out"); continue; }
    if (e.kind === "table") { elements.push({ ...e, rows: e.rows!.map((r) => r.map((c) => (c.startsWith("=") && !alive(c.slice(1)) ? "—" : c))) }); continue; }
    if (e.kind === "text") { elements.push({ ...e, md: e.md!.replace(PLACEHOLDER, (m, f: string) => (alive(f) ? m : "—")) }); continue; }
    elements.push(e);
  }

  // What the scene shows, and every variable that can move it (through rules, transitively).
  const shown = new Set<string>();
  for (const e of elements) if (e.kind !== "slider" && e.kind !== "button") for (const f of formulasOf(e)) for (const v of H.varsOf(f, b.defs)) shown.add(v);
  let grew = true;
  while (grew) {
    grew = false;
    for (const r of b.rules) {
      if (!r.set.some((a) => shown.has(a.var))) continue;
      const reads = [r.if ?? "", ...r.set.map((a) => a.expr)].filter(Boolean).flatMap((f) => [...H.varsOf(f, b.defs)]);
      for (const v of [...reads, ...(r.when === "change" && r.target ? [r.target] : [])]) if (!shown.has(v)) { shown.add(v); grew = true; }
    }
  }
  const movesShown = (r: Rule) => r.set.some((a) => shown.has(a.var));
  elements = elements.filter((e) => {
    if (e.kind === "slider" && !shown.has(e.var!)) { warn(`${name}: dropped slider "${e.id}": nothing in the scene shows ${e.var}.`); return false; }
    if (e.kind === "button" && !b.rules.some((r) => r.when === "click" && r.target === e.id && movesShown(r))) {
      warn(`${name}: dropped button "${e.id}": clicking it changes nothing the scene shows.`);
      return false;
    }
    return true;
  });
  const ids = new Set(elements.map((e) => e.id));
  let rules = b.rules.filter((r) => r.when !== "click" || ids.has(r.target ?? ""));
  if (!elements.some((e) => VISUALS.has(e.kind))) { warn(`Dropped scene "${b.title}": nothing in it shows anything.`); return null; }

  // Dry run: start, click each button, then 3 simulated seconds of timers.
  const scene: SceneBlock = { ...b, elements, rules };
  const sim = new SceneSim(scene, () => scopes[0]);
  const loops = new Set<number>(), bad = new Map<string, number>();
  const note = (res: FireResult, starters: number[]) => {
    if (res.overflow) loops.add(res.culprit >= 0 ? res.culprit : starters[0]);
    for (const x of res.bad) if (!bad.has(`${x.rule}:${x.var}`)) bad.set(`${x.rule}:${x.var}`, x.rule);
  };
  const startersOf = (pred: (r: Rule) => boolean) => rules.map((r, i) => (pred(r) ? i : -1)).filter((i) => i >= 0);
  note(sim.fire("start"), startersOf((r) => r.when === "start"));
  for (const e of elements)
    if (e.kind === "button") note(sim.fire("click", e.id), startersOf((r) => r.when === "click" && r.target === e.id));
  for (let t = 10; t <= SCENE_LIMITS.dryRunMs; t += 10)
    rules.forEach((r, i) => { if (r.when === "every" && t % (Math.ceil((r.ms ?? 500) / 10) * 10) === 0) note(sim.tick(i), [i]); });
  for (const [key, i] of bad) warn(`${name}: rule ${i + 1} sets ${key.split(":")[1]} to something that isn't a number.`);
  if (loops.size) {
    for (const i of [...loops].sort((p, q) => p - q)) warn(`${name}: dropped rule ${i + 1} (it set off an endless chain of updates).`);
    rules = rules.filter((_, i) => !loops.has(i));
  }
  return { ...scene, rules };
}
