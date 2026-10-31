// Semantic validation + repair of AI output. parseOutline (spec.ts) already
// checked the JSON shape; this checks what a schema can't (ranges,
// cross-references, expression syntax and scope, matrix sizes), then runs a
// numeric sanity check that evaluates every live expression over the slider
// ranges. Bad parts are repaired or dropped with a warning; it never throws.

import { DEFAULT_DESIGN, ICONS } from "./menus";
import { evalNumber, evalValue, isMat, isNum, tryCompile, usesRandom, type Scope, type Value } from "./expr";
import type { Block, Design, ExplainerSpec, Outline } from "./spec";
import { checkScene, repairScene, type SceneHelpers } from "./scene";
import { runTrajectory, withDefs } from "./views";

// Control names can't shadow the plot/heatmap axes or constants. "t" is
// allowed (timesteps are common); inside a trajectory the step counter wins.
// pi/e may be reused as names (e.g. a policy π); a def or control shadows the constant.
const RESERVED = new Set(["x", "y", "seed"]);
const DEF_RESERVED = new Set(["seed"]);
const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;
// A stage is about 1k output tokens, so 20 stays well inside the planner's 48k budget.
export const MAX_STAGES = 20;
const MAX_BLOCKS = 14;
const FALLBACK_ICON = "🧩";

const NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
  "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"];
// "steps" is left out on purpose: "50 denoising steps" or "the 3 steps of backprop" describe the topic, not the page.
const STAGE_COUNT = new RegExp(`\\b(\\d{1,3}|${NUMBER_WORDS.join("|")})[\\s-]+(?:stages?|parts?|sections?|chapters?|lessons?|modules?)\\b`, "i");

/**
 * The stage count the reader asked for ("14 stages", "a 12-part explainer", "fourteen sections"),
 * clamped to MAX_STAGES; `clamped` marks a request above it. Undefined when the text names none (or fewer than 2).
 */
export function requestedStages(text: string): { count: number; clamped: boolean } | undefined {
  const m = STAGE_COUNT.exec(text);
  if (!m) return undefined;
  const word = m[1].toLowerCase();
  const n = /^\d+$/.test(word) ? Number(word) : NUMBER_WORDS.indexOf(word);
  if (n < 2) return undefined;
  return { count: Math.min(n, MAX_STAGES), clamped: n > MAX_STAGES };
}

/** The outline without blocks of the given types (Lite+ drops quizzes, questions and worked examples). */
export function withoutBlocks(outline: Outline, drop: Set<string>): Outline {
  return { ...outline, stages: outline.stages.map((s) => ({ ...s, blocks: s.blocks.filter((b) => !drop.has(b.type)) })) };
}

export function slug(s: string, fallback: string): string {
  const v = s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  return v || fallback;
}

const clampInt = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, Math.round(n)));
const finite = (n: number, d: number) => (Number.isFinite(n) ? n : d);

type Warn = (m: string) => void;
type Def = { name: string; expr: string };

const isControl = (b: Block): b is Extract<Block, { type: "slider" | "choice" | "toggle" }> =>
  b.type === "slider" || b.type === "choice" || b.type === "toggle";

/** Collect every variable declared by sliders/choices/toggles across the explainer. */
function declaredVars(outline: Outline): Set<string> {
  const vars = new Set<string>(["seed"]); // seed: set by Resample/Run buttons, 0 by default
  for (const st of outline.stages)
    for (const b of st.blocks)
      if (isControl(b) && IDENT.test(b.var) && !RESERVED.has(b.var)) vars.add(b.var);
  return vars;
}

/** Keep only {var, value} pairs that set a declared control variable. */
function cleanSet(set: { var: string; value: number }[], vars: Set<string>) {
  return set.filter((s) => vars.has(s.var) && s.var !== "seed" && Number.isFinite(s.value)).slice(0, 8);
}

// Blocks that show something visual or interactive (a stage should have at least one).
const VISUAL = new Set([
  "plot", "chart", "scene", "bars", "samples", "heatmap", "trajectory", "matrix", "calculator", "experiment", "table", "conceptmap",
  "tokens", "attention", "sampling", "embedding", "architecture", "pipeline", "training",
  "flipcards", "reveal", "predict", "quiz", "cards", "readout", "tabs", "compare",
]);
// Blocks that count as a graph for the auto-graph rule.
const GRAPH = new Set(["plot", "chart", "scene", "bars", "samples", "heatmap", "trajectory", "matrix", "experiment", "attention", "sampling", "training", "embedding"]);
// Variety caps per explainer.
const CAPS: Partial<Record<Block["type"], number>> = {
  pipeline: 1, architecture: 1, conceptmap: 1, training: 1, flipcards: 2, reveal: 3, cards: 5, tokens: 2,
};

/** Check an expression compiles and only uses known variables. */
function checkExpr(src: string, allowed: Set<string>): string | null {
  const r = tryCompile(src);
  if (!r.ok) return r.error;
  for (const v of r.c.vars) if (!allowed.has(v)) return `unknown variable "${v}"`;
  return null;
}

/**
 * Keep valid defs (in order); returns them plus the names now in scope.
 * `viewVars` are names the view itself sets (x for plots, x/y for heatmaps,
 * t for trajectories), so a def may not use them there but may elsewhere.
 */
function cleanDefs(defs: Def[] | undefined, vars: Set<string>, warn: Warn, where: string, viewVars: string[] = []): { defs: Def[]; scope: Set<string> } {
  const scope = new Set(vars);
  const out: Def[] = [];
  for (const d of (defs ?? []).slice(0, 20)) {
    const bad = !IDENT.test(d.name) || DEF_RESERVED.has(d.name) || viewVars.includes(d.name);
    const err = bad ? `"${d.name}" is reserved` : checkExpr(d.expr, scope);
    if (err) { warn(`Dropped definition "${d.name}" in ${where}: ${err}`); continue; }
    out.push(d);
    scope.add(d.name);
  }
  return { defs: out, scope };
}

const plus = (s: Set<string>, ...names: string[]) => new Set([...s, ...names]);

/** Largest AI-written widget kept (characters of HTML). */
export const WIDGET_MAX_CHARS = 40_000;

function repairBlock(b: Block, vars: Set<string>, warn: Warn): Block | null {
  switch (b.type) {
    case "quiz": {
      const questions = b.questions
        .filter((q) => q.choices.length >= 2)
        .slice(0, 4)
        .map((q) => ({ ...q, choices: q.choices.slice(0, 5), answer: clampInt(q.answer, 0, Math.min(q.choices.length, 5) - 1) }));
      return questions.length ? { ...b, questions } : null;
    }
    case "predict":
      if (b.choices.length < 2) return null;
      return { ...b, answer: clampInt(b.answer, 0, b.choices.length - 1) };
    case "flipcards": return b.cards.length ? { ...b, cards: b.cards.slice(0, 8) } : null;
    case "tabs": return b.tabs.length ? { ...b, tabs: b.tabs.slice(0, 5) } : null;
    case "compare": return b.left.points.length || b.right.points.length ? b : null;
    case "formula":
      return { ...b, terms: b.terms.map((t) => (t.var && !vars.has(t.var) ? { ...t, var: undefined } : t)) };
    case "slider": {
      if (!IDENT.test(b.var) || RESERVED.has(b.var)) return null;
      let min = finite(b.min, 0), max = finite(b.max, 1);
      if (min > max) [min, max] = [max, min];
      if (min === max) max = min + 1;
      let step = finite(b.step, 0);
      if (step <= 0 || step > max - min) step = (max - min) / 100;
      const value = Math.min(max, Math.max(min, finite(b.value, min)));
      return { ...b, min, max, step, value };
    }
    case "choice": {
      if (!IDENT.test(b.var) || RESERVED.has(b.var) || b.options.length < 2) return null;
      const options = b.options.slice(0, 5);
      const value = options.some((o) => o.value === b.value) ? b.value : options[0].value;
      return { ...b, options, value };
    }
    case "plot": {
      const { defs, scope } = cleanDefs(b.defs, vars, warn, `"${b.title}"`, ["x"]);
      const withX = plus(scope, "x");
      const series = b.series.slice(0, 4).filter((s) => {
        const err = checkExpr(s.expr, withX);
        if (err) warn(`Dropped plot series "${s.label}": ${err}`);
        return !err;
      });
      if (!series.length) return null;
      let { xMin, xMax } = b;
      if (!(xMin < xMax)) { xMin = 0; xMax = 1; }
      const markerX = b.markerX && !checkExpr(b.markerX, scope) ? b.markerX : "";
      const sh = b.shade;
      const shade = sh && sh.series >= 0 && sh.series < series.length && !checkExpr(sh.from, scope) && !checkExpr(sh.to, scope) ? sh : undefined;
      const sweepVar = b.sweepVar && vars.has(b.sweepVar) && b.sweepVar !== "seed" ? b.sweepVar : undefined;
      return { ...b, defs, series, xMin, xMax, markerX, shade, sweepVar };
    }
    case "scene": return repairScene(b, vars, warn, SCENE_HELPERS);
    case "chart": {
      const where = `chart "${b.title}"`;
      const { defs, scope } = cleanDefs(b.defs, vars, warn, `"${b.title}"`);
      const series = b.series.slice(0, 3).filter((s) => {
        const err = checkExpr(s.expr, scope);
        if (err) warn(`Dropped chart series "${s.label}": ${err}`);
        return !err;
      });
      if (!series.length) return null;
      const axis = (v: string, name: string) => {
        if (!v) return "";
        if (vars.has(v) && v !== "seed") return v;
        warn(`Chart "${b.title}": "${v}" is not a control variable, so it can't be the ${name} axis.`);
        return "";
      };
      const x = axis(b.x, "x");
      let by = axis(b.by, "second");
      if (by && by === x) by = "";
      const at = [...new Set(b.at.filter(Number.isFinite))].sort((p, q) => p - q).slice(0, 4);
      // A typed-in answer (0.366032) makes the chart ignore the page; it should be computed from the variables.
      const typed = [...new Set([...series.map((s) => s.expr), ...defs.map((d) => d.expr)].flatMap((e) => e.match(LITERAL) ?? []))]
        .filter((lit) => sigDigits(lit) >= 4);
      if (typed.length) warn(`The ${where} types the number ${typed[0]}; computing it from the variables keeps it in step with the page.`);
      return { ...b, defs, series, x, by, at, labels: b.labels.slice(0, 24) };
    }
    case "toggle": {
      if (!IDENT.test(b.var) || RESERVED.has(b.var)) return null;
      return { ...b, value: b.value ? 1 : 0 };
    }
    case "presets": {
      const items = b.items.map((it) => ({ ...it, set: cleanSet(it.set, vars) })).filter((it) => it.set.length).slice(0, 5);
      return items.length ? { ...b, items } : null;
    }
    case "examples": {
      const items = b.items
        .map((it) => ({ ...it, set: cleanSet(it.set, vars), steps: it.steps?.slice(0, 6) }))
        .filter((it) => it.set.length || it.steps?.length)
        .slice(0, 4);
      return items.length ? { ...b, items } : null;
    }
    case "cards": {
      const items = b.items.filter((it) => it.title || it.md).slice(0, 6);
      return items.length ? { ...b, items } : null;
    }
    case "experiment": {
      const { defs, scope } = cleanDefs(b.defs, vars, warn, `"${b.title}"`);
      const err = checkExpr(b.trial, scope);
      if (err) { warn(`Dropped experiment "${b.title}": ${err}`); return null; }
      if (!usesRandom(b.trial, defs)) {
        warn(`Dropped experiment "${b.title}": nothing in it is random, so repeating it would show the same result.`);
        return null;
      }
      const repeats = [...new Set(b.repeats.map((n) => clampInt(n, 1, 10_000)))].sort((a, c) => a - c).slice(0, 4);
      const truth = b.truth && !checkExpr(b.truth, scope) ? b.truth : undefined;
      const outcomes = b.outcomes?.length ? b.outcomes.slice(0, 12) : undefined;
      return { ...b, defs, repeats: repeats.length ? repeats : [1], truth, outcomes };
    }
    case "table": {
      const cols = b.columns.slice(0, 8);
      if (!cols.length) return null;
      const rows = b.rows.slice(0, 24).map((r) => {
        const cells = r.slice(0, cols.length).map((c) => {
          if (!c.startsWith("=")) return c;
          const e = checkExpr(c.slice(1), vars);
          if (e) warn(`Table "${b.title}": live cell "${c}" removed (${e}).`);
          return e ? "—" : c;
        });
        while (cells.length < cols.length) cells.push("");
        return cells;
      });
      return rows.length ? { ...b, columns: cols, rows, digits: b.digits === undefined ? undefined : clampInt(b.digits, 0, 6) } : null;
    }
    case "conceptmap": {
      const seen = new Set<string>();
      const nodes = b.nodes.filter((n) => n.id && !seen.has(n.id) && seen.add(n.id)).slice(0, 9);
      const ids = new Set(nodes.map((n) => n.id));
      const edges = b.edges.filter((e) => ids.has(e.from) && ids.has(e.to) && e.from !== e.to).slice(0, 16);
      return nodes.length >= 2 ? { ...b, nodes, edges } : null;
    }
    case "bars": {
      const { defs, scope } = cleanDefs(b.defs, vars, warn, `"${b.title}"`);
      // A single expression returning a list: one bar per element.
      if (b.values.length === 1 && b.labels.length !== 1) {
        const err = checkExpr(b.values[0], scope);
        if (err) { warn(`Dropped bars "${b.title}": ${err}`); return null; }
        return { ...b, defs, labels: b.labels.slice(0, 24) };
      }
      const n = Math.min(b.labels.length, b.values.length, 12);
      const labels: string[] = [], values: string[] = [];
      for (let i = 0; i < n; i++) {
        const err = checkExpr(b.values[i], scope);
        if (err) { warn(`Dropped bar "${b.labels[i]}": ${err}`); continue; }
        labels.push(b.labels[i]);
        values.push(b.values[i]);
      }
      return labels.length ? { ...b, defs, labels, values } : null;
    }
    case "readout": {
      const { defs, scope } = cleanDefs(b.defs, vars, warn, "a readout");
      const items = b.items
        .filter((it) => !checkExpr(it.expr, scope))
        .map((it) => ({ ...it, digits: clampInt(it.digits, 0, 6) }));
      return items.length ? { ...b, defs, items } : null;
    }
    case "samples": {
      const { defs, scope } = cleanDefs(b.defs, vars, warn, `"${b.title}"`);
      const err = checkExpr(b.values, scope);
      if (err) { warn(`Dropped samples "${b.title}": ${err}`); return null; }
      const markers = b.markers.filter((m) => !checkExpr(m.expr, scope)).slice(0, 4);
      const band = b.band && !checkExpr(b.band.from, scope) && !checkExpr(b.band.to, scope) ? b.band : undefined;
      const stats = b.stats.filter((m) => !checkExpr(m.expr, scope)).slice(0, 4).map((m) => ({ ...m, digits: clampInt(m.digits, 0, 6) }));
      return { ...b, defs, markers, band, stats };
    }
    case "heatmap": {
      const { defs, scope } = cleanDefs(b.defs, vars, warn, `"${b.title}"`, ["x", "y"]);
      const err = checkExpr(b.expr, plus(scope, "x", "y"));
      if (err) { warn(`Dropped heatmap "${b.title}": ${err}`); return null; }
      if (!(b.xMin < b.xMax) || !(b.yMin < b.yMax)) return null;
      const marker = b.marker && !checkExpr(b.marker.x, scope) && !checkExpr(b.marker.y, scope) ? b.marker : undefined;
      return { ...b, defs, marker };
    }
    case "trajectory": {
      const names = b.state.map((s) => s.name);
      // Trajectory defs are re-evaluated every step, so they may read t and the state.
      const { defs, scope } = cleanDefs(b.defs, plus(vars, ...names, "t"), warn, `"${b.title}"`, ["t", ...names]);
      const fail = (why: string) => { warn(`Trajectory "${b.title}": ${why}`); return null; };
      const badName = names.find((n) => !IDENT.test(n) || DEF_RESERVED.has(n) || n === "t"); // x/y are fine: only the surface uses them
      if (!names.length || badName !== undefined) return fail(`invalid state name "${badName ?? ""}" (t is the step counter).`);
      const inner = plus(scope, ...names, "t");
      for (const s of b.state) { const e = checkExpr(s.init, scope); if (e) return fail(`start value of ${s.name}: ${e}`); }
      const update = b.update.filter((u) => {
        const e = names.includes(u.name) ? checkExpr(u.expr, inner) : "not a state variable";
        if (e) warn(`Trajectory "${b.title}": dropped update of ${u.name} (${e}).`);
        return !e;
      });
      if (!update.length) return fail("no valid update rules.");
      const stepsErr = checkExpr(b.steps, scope);
      if (stepsErr) return fail(`steps: ${stepsErr}`);
      if (b.mode === "path") {
        const p = b.path;
        if (!p) return fail("path mode needs a path.");
        const e = checkExpr(p.x, inner) ?? checkExpr(p.y, inner);
        if (e) return fail(`path: ${e}`);
        if (!(p.xMin < p.xMax) || !(p.yMin < p.yMax)) return fail("path range is empty.");
        const surface = p.surface && !checkExpr(p.surface, plus(scope, "x", "y")) ? p.surface : "";
        return { ...b, defs, update, path: { ...p, surface } };
      }
      const series = b.series.filter((s) => {
        const e = checkExpr(s.expr, inner);
        if (e) warn(`Trajectory "${b.title}": dropped series "${s.label}" (${e}).`);
        return !e;
      }).slice(0, 4);
      return series.length ? { ...b, defs, update, series } : fail("no valid series to plot.");
    }
    case "matrix": {
      const { defs, scope } = cleanDefs(b.defs, vars, warn, `"${b.title}"`);
      const items = b.items.filter((it) => !checkExpr(it.expr, scope)).slice(0, 7);
      return items.length ? { ...b, defs, items, digits: clampInt(b.digits, 0, 4) } : null;
    }
    case "calculator": {
      const { defs, scope } = cleanDefs(b.defs, vars, warn, `"${b.title}"`);
      const rows = b.rows.filter((r) => !checkExpr(r.expr, scope)).slice(0, 12).map((r) => ({ ...r, digits: clampInt(r.digits, 0, 6) }));
      return rows.length ? { ...b, defs, rows } : null;
    }
    case "tokens": return b.tokens.length ? { ...b, tokens: b.tokens.slice(0, 60) } : null;
    case "attention": {
      const tokens = b.tokens.slice(0, 10);
      const n = tokens.length;
      if (n < 2) return null;
      const weights = Array.from({ length: n }, (_, i) =>
        Array.from({ length: n }, (_, j) => finite(b.weights[i]?.[j] ?? 0, 0)));
      return { ...b, tokens, weights };
    }
    case "sampling": {
      const candidates = b.candidates.filter((c) => c.token && Number.isFinite(c.logit)).slice(0, 10);
      return candidates.length >= 2 ? { ...b, candidates } : null;
    }
    case "embedding": {
      const points = b.points.filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y)).slice(0, 40);
      return points.length >= 2 ? { ...b, points } : null;
    }
    case "architecture": {
      const seen = new Set<string>();
      const nodes = b.nodes.filter((n) => n.id && !seen.has(n.id) && seen.add(n.id)).slice(0, 14);
      const ids = new Set(nodes.map((n) => n.id));
      const edges = b.edges.filter((e) => ids.has(e.from) && ids.has(e.to) && e.from !== e.to);
      return nodes.length ? { ...b, nodes, edges } : null;
    }
    case "pipeline": return b.steps.length ? { ...b, steps: b.steps.slice(0, 10) } : null;
    case "widget": {
      // AI-written code can't be checked numerically; only its shape. It runs sandboxed (render/components/widget.ts).
      const html = b.html.trim();
      if (!html || html.length > WIDGET_MAX_CHARS) { warn(`Dropped widget "${b.title}": ${html ? "too large" : "empty"}.`); return null; }
      if (!/<script[\s>]/i.test(html)) { warn(`Dropped widget "${b.title}": it has no script, so nothing is interactive.`); return null; }
      return { ...b, html };
    }
    default: return b;
  }
}

// ---------- numeric sanity check ----------

/** Scopes to probe: all sliders at their defaults, then each slider at its min and max. */
function probeScopes(blocks: Block[]): Scope[] {
  const base: Scope = { seed: 0 };
  const sliders: { name: string; min: number; max: number }[] = [];
  for (const b of blocks) {
    if (b.type === "slider" && !(b.var in base)) { base[b.var] = b.value; sliders.push({ name: b.var, min: b.min, max: b.max }); }
    if (b.type === "choice" && !(b.var in base)) base[b.var] = b.value;
    if (b.type === "toggle" && !(b.var in base)) { base[b.var] = b.value; sliders.push({ name: b.var, min: 0, max: 1 }); }
  }
  const scopes = [base];
  for (const s of sliders.slice(0, 8)) scopes.push({ ...base, [s.name]: s.min }, { ...base, [s.name]: s.max });
  return scopes;
}

const finiteNums = (v: Value) => (isNum(v) ? [v] : isMat(v) ? v.flat() : v).filter(Number.isFinite);
const lin = (lo: number, hi: number, n: number) => Array.from({ length: n }, (_, i) => lo + ((hi - lo) * i) / (n - 1));

type SliderBlock = Extract<Block, { type: "slider" }>;
type Sliders = Map<string, SliderBlock>;
type ChartBlock = Extract<Block, { type: "chart" }>;
type ChartShape = "number" | "random" | "list";

const sliderMap = (blocks: Block[]): Sliders => new Map(blocks.filter((b): b is SliderBlock => b.type === "slider").map((s) => [s.var, s]));

/** The views each formula shape can use, in fallback order (the first that fits replaces a view that doesn't). */
const VIEWS_FOR: Record<ChartShape, ChartBlock["view"][]> = {
  number: ["line", "lines", "heatmap"],
  random: ["histogram", "compare", "running", "scatter", "band", "box"],
  list: ["bars", "groupedbars"],
};
const SHAPE_NAME: Record<ChartShape, string> = { number: "a formula that gives one number", random: "a random formula", list: "a formula that gives a list" };

/** Whether a view can draw this chart: right shape, the axis variables it needs, and enough series (n). */
function viewFits(view: ChartBlock["view"], shape: ChartShape, b: { x: string; by: string; n: number }): boolean {
  if (!VIEWS_FOR[shape].includes(view)) return false;
  if (view === "line" || view === "band") return !!b.x;
  if (view === "lines" || view === "heatmap") return !!b.x && !!b.by;
  if (view === "compare" || view === "box") return !!b.by;
  // Scatter: draws against x, or the first two series as each point's position.
  if (view === "scatter") return !!b.x || b.n >= 2;
  if (view === "groupedbars") return b.n >= 2;
  return true;
}

/**
 * The chart checks that need the sliders and real values: axes must be sliders, the view
 * must fit the formula's shape (else the first view that fits, with a warning), the
 * compared values sit inside the slider's range, and a chart should read the page's controls.
 */
function fitChart(b: ChartBlock, scopes: Scope[], sliders: Sliders, warn: Warn): ChartBlock | null {
  const notSlider = (v: string, name: string) => {
    if (!v || sliders.has(v)) return v;
    warn(`Chart "${b.title}": "${v}" is not a slider, so it can't be the ${name} axis.`);
    return "";
  };
  const x = notSlider(b.x, "x");
  const by = notSlider(b.by, "second");
  const first = b.series[0].expr;
  const at0 = withDefs(b.defs, scopes[0]);
  const shape: ChartShape = usesRandom(first, b.defs ?? []) ? "random" : isNum(evalValue(first, at0)) ? "number" : "list";
  let view = b.view;
  const axes = { x, by, n: b.series.length };
  if (!viewFits(view, shape, axes)) {
    const fit = VIEWS_FOR[shape].find((v) => viewFits(v, shape, axes));
    if (!fit) {
      warn(`Dropped chart "${b.title}": ${SHAPE_NAME[shape]} needs ${shape === "number" ? "a slider variable on its x axis" : "a different view"}.`);
      return null;
    }
    warn(`Chart "${b.title}": the "${view}" view doesn't fit ${SHAPE_NAME[shape]}; showing "${fit}".`);
    view = fit;
  }
  // Values of `by` to show: inside its slider's range; by default the ends (and the middle for lines and box).
  let at: number[] = [];
  if (by && (view === "lines" || view === "compare" || view === "box")) {
    const s = sliders.get(by)!;
    at = [...new Set(b.at.map((v) => Math.min(s.max, Math.max(s.min, v))))].sort((p, q) => p - q);
    if (view === "compare") {
      // The first value is the fixed baseline (the renderer draws it next to the slider's current value).
      at = at.length ? [at[0], at.length > 1 ? at[at.length - 1] : s.max] : [s.min, s.max];
    } else {
      if (at.length < 2) at = [s.min, (s.min + s.max) / 2, s.max];
      at = at.slice(0, 4);
    }
  }
  const controls = new Set(Object.keys(scopes[0] ?? {}).filter((k) => k !== "seed"));
  const reads = b.series.some((s) => [...varsOf(s.expr, b.defs)].some((v) => controls.has(v)));
  if (controls.size && !reads) warn(`Chart "${b.title}" reads none of the page's controls, so nothing on the page changes it.`);
  return { ...b, x, by, at, view };
}

/**
 * Evaluate a block's expressions over the probe scopes. Returns the block with
 * dead parts removed (or null if nothing numeric survives); warns on flat
 * plots and out-of-range probabilities. `sliders` (every slider on the page)
 * is needed for charts, whose axes are slider variables.
 */
function sanityCheck(b: Block, scopes: Scope[], warn: Warn, sliders: Sliders = new Map()): Block | null {
  const label = "title" in b && b.title ? `"${b.title}"` : `a ${b.type} block`;
  const ss = (defs: Def[] | undefined) => scopes.map((s) => withDefs(defs, s));
  const alive = (src: string, scs: Scope[]) => scs.some((s) => finiteNums(evalValue(src, s)).length > 0);
  const kindOk = (src: string, kind: string | undefined, scs: Scope[]) => {
    if (kind !== "probability" && kind !== "percent") return true;
    const hi = kind === "probability" ? 1 + 1e-9 : 100 + 1e-7;
    return scs.every((s) => finiteNums(evalValue(src, s)).every((v) => v >= -1e-9 && v <= hi));
  };

  switch (b.type) {
    case "plot": {
      const scs = ss(b.defs);
      const xs = lin(b.xMin, b.xMax, 9);
      const at = (sc: Scope, x: number) => (b.sweepVar ? withDefs(b.defs, { ...sc, [b.sweepVar]: x, x }) : { ...sc, x });
      let flat = 0;
      const series = b.series.filter((s) => {
        const ys = scs.flatMap((sc) => xs.map((x) => evalNumber(s.expr, at(sc, x)))).filter(Number.isFinite);
        if (!ys.length) { warn(`Dropped plot series "${s.label}": it never produces a number.`); return false; }
        if (Math.max(...ys) - Math.min(...ys) < 1e-12) flat++;
        return true;
      });
      // A flat reference line next to a moving curve is fine; a plot where nothing moves is not.
      if (series.length && flat === series.length) warn(`Plot ${label} is flat: nothing changes it.`);
      return series.length ? { ...b, series } : null;
    }
    case "scene": return checkScene(b, scopes, warn, sliders, SCENE_HELPERS);
    case "chart": {
      const fit = fitChart(b, scopes, sliders, warn);
      if (!fit) return null;
      // Numbers: every series over x (or at the probe scopes); random: a few seeds; lists: the elements.
      const xs = fit.x ? lin(sliders.get(fit.x)!.min, sliders.get(fit.x)!.max, 9) : [NaN];
      const at = (sc: Scope, x: number, seed: number) => withDefs(fit.defs, fit.x && Number.isFinite(x) ? { ...sc, [fit.x]: x, seed } : { ...sc, seed });
      const seeds = VIEWS_FOR.random.includes(fit.view) ? [1, 2, 3] : [0];
      let flat = 0;
      const series = fit.series.filter((s) => {
        const vals = scopes.flatMap((sc) => xs.flatMap((x) => seeds.flatMap((seed) => finiteNums(evalValue(s.expr, at(sc, x, seed))))));
        if (!vals.length) { warn(`Dropped chart series "${s.label}": it never produces a number.`); return false; }
        if (Math.max(...vals) - Math.min(...vals) < 1e-12) flat++;
        return true;
      });
      if (!series.length) return null;
      if (flat === series.length && !VIEWS_FOR.random.includes(fit.view)) warn(`Chart ${label} is flat: nothing changes it.`);
      return { ...fit, series };
    }
    case "bars": {
      const scs = ss(b.defs);
      return b.values.some((v) => alive(v, scs)) ? b : null;
    }
    case "readout": {
      const scs = ss(b.defs);
      const items = b.items.filter((it) => {
        if (!alive(it.expr, scs)) { warn(`Dropped readout "${it.label}": it never produces a number.`); return false; }
        if (!kindOk(it.expr, it.kind, scs)) warn(`Readout "${it.label}" goes outside the valid range for a ${it.kind}.`);
        return true;
      });
      return items.length ? { ...b, items } : null;
    }
    case "samples": {
      const scs = ss(b.defs);
      if (!alive(b.values, scs)) return null;
      for (const st of b.stats) if (!kindOk(st.expr, st.kind, scs)) warn(`Statistic "${st.label}" in ${label} goes outside the valid range for a ${st.kind}.`);
      return { ...b, stats: b.stats.filter((st) => alive(st.expr, scs)), markers: b.markers.filter((m) => alive(m.expr, scs)) };
    }
    case "heatmap": {
      const scs = ss(b.defs);
      const grid = lin(b.xMin, b.xMax, 5).flatMap((x) => lin(b.yMin, b.yMax, 5).map((y) => [x, y]));
      const zs = scs.flatMap((sc) => grid.map(([x, y]) => evalNumber(b.expr, { ...sc, x, y }))).filter(Number.isFinite);
      if (!zs.length) return null;
      if (Math.max(...zs) - Math.min(...zs) < 1e-12) warn(`Heatmap ${label} is a single flat colour.`);
      return b;
    }
    case "trajectory": {
      const run = runTrajectory(b, scopes[0], 300);
      return b.state.every((s) => Number.isFinite(run.frames[0][s.name] as number)) ? b : null;
    }
    case "matrix": {
      const scs = ss(b.defs);
      const items = b.items.filter((it) => alive(it.expr, scs));
      return items.length ? { ...b, items } : null;
    }
    case "calculator": {
      const scs = ss(b.defs);
      const rows = b.rows.filter((r) => alive(r.expr, scs));
      return rows.length ? { ...b, rows } : null;
    }
    case "experiment": {
      // Probe a few seeds: the trial must produce numbers, and category indices must be valid.
      const runs = [1, 2, 3].flatMap((seed) => ss(b.defs).map((s) => evalNumber(b.trial, withDefs(b.defs, { ...s, seed }))));
      const ok = runs.filter(Number.isFinite);
      if (!ok.length) return null;
      if (b.outcomes && ok.some((v) => !Number.isInteger(v) || v < 0 || v >= b.outcomes!.length)) {
        warn(`Experiment ${label}: results aren't valid outcome indices, so they're shown as numbers.`);
        return { ...b, outcomes: undefined };
      }
      return b;
    }
    case "table": {
      const rows = b.rows.map((r) => r.map((c) => (c.startsWith("=") && !alive(c.slice(1), scopes) ? "—" : c)));
      return { ...b, rows };
    }
    default:
      return b;
  }
}

/** What the scene checks (shared/scene.ts) reuse from here. */
const SCENE_HELPERS: SceneHelpers = {
  checkExpr, cleanDefs: (defs, vars, warn, where) => cleanDefs(defs, vars, warn, where),
  repairBlock: (b, vars, warn) => repairBlock(b, vars, warn),
  sanityCheck: (b, scopes, warn, sliders) => sanityCheck(b, scopes, warn, sliders),
  varsOf: (src, defs) => varsOf(src, defs),
};

/** Variables an expression reads, following defs transitively. */
function varsOf(src: string, defs: Def[] = []): Set<string> {
  const byName = new Map(defs.map((d) => [d.name, d.expr]));
  const out = new Set<string>();
  const visit = (s: string) => {
    const r = tryCompile(s);
    if (!r.ok) return;
    for (const v of r.c.vars) {
      if (out.has(v)) continue;
      out.add(v);
      const e = byName.get(v);
      if (e !== undefined) visit(e);
    }
  };
  visit(src);
  return out;
}

/**
 * Auto-graph: a stage with controls and readouts but no graph gets a
 * sensitivity plot per readout (first item vs. each slider it depends on).
 */
function autoGraphs(blocks: Block[], sliders: Map<string, Extract<Block, { type: "slider" }>>, scopes: Scope[]): Block[] {
  if (blocks.some((b) => GRAPH.has(b.type)) || !blocks.some(isControl)) return blocks;
  const out: Block[] = [];
  for (const b of blocks) {
    out.push(b);
    if (b.type !== "readout") continue;
    const item = b.items[0];
    const used = [...varsOf(item.expr, b.defs)].filter((v) => sliders.has(v)).slice(0, 2);
    for (const v of used) {
      const s = sliders.get(v)!;
      const plot: Block = {
        type: "plot", title: `${item.label} as ${s.label} changes`, xLabel: s.label, yLabel: item.label,
        xMin: s.min, xMax: s.max, yMin: 0, yMax: 0, defs: b.defs, series: [{ label: item.label, expr: item.expr }],
        markerX: v, sweepVar: v,
      };
      // Only add it if the curve actually moves.
      if (sanityCheck(plot, scopes, () => {}) && !isFlat(plot, scopes[0])) out.push(plot);
    }
  }
  return out;
}

/**
 * Edit chat: check a reader's edit to one stage before it is applied. The edited
 * stage's blocks go through the same repair and numeric checks as generation;
 * every other block is re-checked too, because changing a slider's range or
 * removing a control can break a chart elsewhere. Small repairs (a slider value
 * pulled back into its range) are applied and reported in `notes`; anything that
 * would drop or trim a part refuses the edit, with the reason.
 */
export function checkEdit(spec: ExplainerSpec, stageIndex: number, blocks: Block[]): { ok: true; spec: ExplainerSpec; notes: string[] } | { ok: false; reason: string } {
  const problems: string[] = [];
  const stages = spec.stages.map((s, i) => (i === stageIndex ? { ...s, blocks } : s));
  const vars = declaredVars({ stages } as unknown as Outline);
  const repaired: Block[] = [];
  for (const b of blocks) {
    const r = repairBlock(b, vars, (m) => problems.push(m));
    // The first problem is the cause (a bad definition); later ones are its knock-on effects.
    if (!r) return { ok: false, reason: problems[0] ?? `This "${b.type}" part is no longer valid.` };
    repaired.push(r);
  }
  if (problems.length) return { ok: false, reason: problems[0] };
  const changed = (a: Block, b: Block) => JSON.stringify(a) !== JSON.stringify(b);
  const notes = blocks.flatMap((b, i) => (changed(b, repaired[i]) ? [`Adjusted the ${b.type} to stay valid (for example a value pulled back into its range).`] : []));

  const next = stages.map((s, i) => (i === stageIndex ? { ...s, blocks: repaired } : s));
  const scopes = probeScopes(next.flatMap((s) => s.blocks));
  const sliders = sliderMap(next.flatMap((s) => s.blocks));
  for (const s of next)
    for (const b of s.blocks) {
      const label = "title" in b && b.title ? `"${b.title}"` : `a ${b.type}`;
      const r = sanityCheck(b, scopes, () => {}, sliders);
      if (!r) return { ok: false, reason: `That would break ${label} in "${s.label}": its numbers would never work out.` };
      if (changed(b, r)) return { ok: false, reason: `That would break part of ${label} in "${s.label}" (some of its values would stop producing numbers).` };
    }
  return { ok: true, spec: { ...spec, stages: next }, notes: [...new Set(notes)] };
}

// ---------- cross-check: one quantity, one answer ----------

/** A number a block shows (calculator row, readout item or live table cell), at the default settings. */
interface Output { stage: number; block: number; item: number; label: string; expr: string; defs: Def[]; value: number; shown: number; digits: number }
/**
 * A value a def computes with a formula, and the outputs in its block that read it. Only defs:
 * an output's own result can sit near an unrelated input by chance (a 0.315 probability next to
 * a typed similarity of 0.31), while a def is what a copy would have been taken from.
 */
interface Computed { value: number; where: string; readers: Set<Output> }

// A hand-typed decimal (0.87): at least 2 decimal places and 2 significant digits, so 0.5 or 1.2 never count.
const LITERAL = /(?<![\w.])\d*\.\d{2,}(?![\w.])/g;
const sigDigits = (lit: string) => lit.replace(/^[0.]+/, "").replace(".", "").replace(/0+$/, "").length;
// Round values (0.25, 0.75, 0.05) are chosen on purpose (an even split, a threshold), not copied.
const isRound = (v: number) => Math.abs(v * 20 - Math.round(v * 20)) < 1e-9;
const handTyped = (text: string) => [...new Set(text.match(LITERAL) ?? [])].filter((lit) => sigDigits(lit) >= 2 && !isRound(Number(lit)));
// A def whose expression is only numbers (a constant or a list of them) computes nothing.
const CONSTANT = /^[\s\d.,+\-[\]]*$/;

/**
 * A2 and A1. A2: an output that types in a rounded copy of a value the page computes
 * (0.87 for softmax(...)[0]) gets a warning; it isn't substituted, since a coincidence would
 * then change a correct formula. A1: when such an output lands near, but not on, an output
 * computed from that same value (3.23 vs 3.05), the two disagree about one quantity, and the
 * hand-copied one is the suspect: it is dropped.
 */
function crossCheck(stages: ExplainerSpec["stages"], scope: Scope, warn: Warn): void {
  const outputs: Output[] = [];
  const computed: Computed[] = [];
  const nums = (v: Value) => (isNum(v) ? [v] : isMat(v) ? [] : v.slice(0, 64)).filter(Number.isFinite);
  const shown = (v: number, kind?: string) => (kind === "probability" ? v * 100 : v);

  stages.forEach((st, si) => st.blocks.forEach((b, bi) => {
    const title = "title" in b && b.title ? `"${b.title}"` : `a ${b.type}`;
    const add = (item: number, label: string, expr: string, defs: Def[], digits: number, kind?: string) => {
      const value = evalNumber(expr, withDefs(defs, scope));
      if (!Number.isFinite(value)) return;
      outputs.push({ stage: si, block: bi, item, label, expr, defs, value, shown: shown(value, kind), digits });
    };
    if (b.type === "calculator") b.rows.forEach((r, i) => add(i, `row ${i + 1} of calculator ${title}`, r.expr, b.defs ?? [], r.digits));
    if (b.type === "readout") b.items.forEach((it, i) => add(i, `readout "${it.label}"`, it.expr, b.defs ?? [], it.digits, it.kind));
    if (b.type === "table") b.rows.forEach((r, ri) => r.forEach((c, ci) => {
      if (c.startsWith("=")) add(ri * 100 + ci, `cell (${ri + 1}, ${ci + 1}) of table ${title}`, c.slice(1), [], b.digits ?? 3);
    }));
  }));

  // Defs that compute something, linked to the outputs in their block that read them.
  stages.forEach((st, si) => st.blocks.forEach((b, bi) => {
    if (b.type === "trajectory" || !("defs" in b) || !b.defs?.length) return;
    const sc = withDefs(b.defs, scope);
    const title = "title" in b && b.title ? `"${b.title}"` : `a ${b.type}`;
    for (const d of b.defs) {
      if (CONSTANT.test(d.expr)) continue;
      const readers = new Set(outputs.filter((o) => o.stage === si && o.block === bi && varsOf(o.expr, o.defs).has(d.name)));
      for (const value of nums(sc[d.name] ?? NaN)) computed.push({ value, where: `${d.name} in ${title}`, readers });
    }
  }));

  const copiesOf = new Map<Output, { lit: string; c: Computed }[]>();
  for (const o of outputs) {
    const text = [o.expr, ...o.defs.map((d) => d.expr)].join(" ");
    const copies = handTyped(text).flatMap((lit) => {
      const v = Number(lit), tol = 0.5 * 10 ** -(lit.split(".")[1].length) + 1e-12;
      // An exact match is the same constant written twice, not a rounded copy.
      return computed.filter((c) => !c.readers.has(o) && c.value !== v && Math.abs(c.value - v) <= tol).map((c) => ({ lit, c }));
    });
    if (copies.length) copiesOf.set(o, copies);
  }

  const drop = new Set<Output>();
  for (const [o, copies] of copiesOf) {
    const { lit, c } = copies[0];
    warn(`The ${o.label} types ${lit} by hand, a rounded copy of ${c.where} (${fmtNum(c.value)}); using the variable keeps them in step.`);

    // Outputs computed from the copied value that hold no hand-typed copies themselves.
    const rivals = copies.flatMap((x) => [...x.c.readers]).filter((r) => r !== o && !copiesOf.has(r));
    const digits = (r: Output) => Math.min(o.digits, r.digits);
    const clash = rivals.find((r) => {
      const rel = Math.abs(o.value - r.value) / Math.max(Math.abs(o.value), Math.abs(r.value));
      return rel > 0.005 && rel <= 0.2 && o.shown.toFixed(digits(r)) !== r.shown.toFixed(digits(r));
    });
    if (clash) {
      drop.add(o);
      warn(`Dropped the ${o.label} in "${stages[o.stage].label}": with its hand-typed ${lit} it gives ${fmtNum(o.value)}, but the ${clash.label}, computed from the same value, gives ${fmtNum(clash.value)}.`);
    }
  }
  if (!drop.size) return;

  const gone = (si: number, bi: number, item: number) => [...drop].some((o) => o.stage === si && o.block === bi && o.item === item);
  for (const [si, st] of stages.entries())
    st.blocks = st.blocks.flatMap((b, bi): Block[] => {
      if (b.type === "calculator") { const rows = b.rows.filter((_, i) => !gone(si, bi, i)); return rows.length ? [{ ...b, rows }] : []; }
      if (b.type === "readout") { const items = b.items.filter((_, i) => !gone(si, bi, i)); return items.length ? [{ ...b, items }] : []; }
      if (b.type === "table") return [{ ...b, rows: b.rows.map((r, ri) => r.map((c, ci) => (gone(si, bi, ri * 100 + ci) ? "—" : c))) }];
      return [b];
    });
}

const fmtNum = (v: number) => String(Number(v.toPrecision(3)));

function isFlat(p: Extract<Block, { type: "plot" }>, scope: Scope): boolean {
  const ys = lin(p.xMin, p.xMax, 9)
    .map((x) => evalNumber(p.series[0].expr, withDefs(p.defs, { ...scope, [p.sweepVar!]: x, x })))
    .filter(Number.isFinite);
  return ys.length < 2 || Math.max(...ys) - Math.min(...ys) < 1e-12;
}

export function defaultDesign(outline: Outline): Design {
  return {
    ...DEFAULT_DESIGN,
    stageIcons: [],
    chatbot: {
      systemPrompt: `You are a friendly tutor helping a reader understand "${outline.title}". Answer clearly at a ${outline.level} level, using the explainer's content. If something is outside it, say so briefly.`,
      starters: outline.stages.slice(0, 3).map((s) => `Can you explain "${s.label}" more simply?`),
    },
  };
}

/** Turn validated AI pieces into the spec the app renders. */
export function assembleSpec(outline: Outline, design: Design | null, context: string, parseWarnings: string[] = []): ExplainerSpec {
  const warnings: string[] = [...parseWarnings];
  const warn = (m: string) => warnings.push(m);
  const d = design ?? defaultDesign(outline);
  if (!design) warn("Design step failed; default design used.");

  const vars = declaredVars(outline);
  const phases = outline.phases.length ? outline.phases.slice(0, 4) : [{ id: "main", label: "How it works" }];
  const phaseIds = new Set(phases.map((p) => p.id));
  const icons = new Map<string, string>(d.stageIcons.map((s) => [s.stageId, ICONS[s.icon]]));
  const usedIds = new Set<string>();

  // Pass 1: structural repair.
  const repaired: { st: Outline["stages"][number]; id: string; blocks: Block[] }[] = [];
  for (const [i, st] of outline.stages.slice(0, MAX_STAGES).entries()) {
    let id = slug(st.id || st.label, `stage-${i + 1}`);
    while (usedIds.has(id)) id += "-2";
    usedIds.add(id);
    const blocks = st.blocks
      .map((b) => {
        const r = repairBlock(b, vars, warn);
        if (!r) warn(`Dropped an invalid "${b.type}" block in "${st.label}".`);
        return r;
      })
      .filter((b): b is Block => b !== null)
      .slice(0, MAX_BLOCKS);
    repaired.push({ st, id, blocks });
  }

  // Pass 2: numeric sanity check (needs every slider's repaired range),
  // variety caps, auto-graphs and the per-stage visual check.
  const all = repaired.flatMap((r) => r.blocks);
  const scopes = probeScopes(all);
  const sliders = sliderMap(all);
  const counts = new Map<string, number>();
  const stages: ExplainerSpec["stages"] = [];
  for (const { st, id, blocks: raw } of repaired) {
    const checked = raw
      .map((b) => {
        const r = sanityCheck(b, scopes, warn, sliders);
        if (!r) warn(`Dropped a "${b.type}" block in "${st.label}": its numbers never work out.`);
        return r;
      })
      .filter((b): b is Block => b !== null)
      .filter((b) => {
        const n = (counts.get(b.type) ?? 0) + 1;
        counts.set(b.type, n);
        const cap = CAPS[b.type];
        if (cap !== undefined && n > cap) { warn(`Dropped an extra "${b.type}" in "${st.label}" (max ${cap} per explainer).`); return false; }
        return true;
      });
    const blocks = autoGraphs(checked, sliders, scopes);
    if (!blocks.length) { warn(`Dropped empty stage "${st.label}".`); continue; }
    if (!blocks.some((b) => VISUAL.has(b.type))) warn(`Stage "${st.label}" has no visual or interactive part.`);
    stages.push({
      ...st,
      id,
      phase: phaseIds.has(st.phase) ? st.phase : phases[0].id,
      icon: icons.get(st.id) ?? icons.get(id) ?? FALLBACK_ICON,
      blocks,
    });
  }

  // Concept-map nodes point at stages by the AI's ids; map them to the final ones.
  const finalId = new Map(repaired.map((r) => [r.st.id, r.id]));
  const kept = new Set(stages.map((s) => s.id));
  for (const s of stages)
    s.blocks = s.blocks.map((b) => b.type !== "conceptmap" ? b : {
      ...b,
      nodes: b.nodes.map((n) => {
        const target = finalId.get(n.stage) ?? n.stage;
        return { ...n, stage: kept.has(target) ? target : "" };
      }),
    });

  crossCheck(stages, scopes[0], warn);

  // Drop phases that ended up with no stages.
  const usedPhases = phases.filter((p) => stages.some((s) => s.phase === p.id));

  return {
    version: 1,
    title: outline.title.trim() || "Untitled explainer",
    subtitle: outline.subtitle,
    level: outline.level,
    layout: outline.layout,
    keyIntuition: outline.keyIntuition,
    design: { theme: d.theme, fontPairing: d.fontPairing, headingStyle: d.headingStyle },
    phases: usedPhases,
    stages,
    glossary: outline.glossary.slice(0, 30),
    chatbot: {
      systemPrompt: d.chatbot.systemPrompt || defaultDesign(outline).chatbot.systemPrompt,
      starters: d.chatbot.starters.filter(Boolean).slice(0, 4),
      context,
    },
    warnings,
  };
}
