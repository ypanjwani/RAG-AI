// The chart block: the AI gives the formula, the slider variables on the axes
// and a view (checked against the formula's shape in shared/validate.ts); this
// file computes the numbers for that view and chartlib.ts draws them. Axis
// ranges and titles come from the sliders, never from the AI.

import { evalList, evalNumber, usesRandom, type Scope } from "../../../../shared/expr";
import type { Block } from "../../../../shared/spec";
import { withDefs } from "../../../../shared/views";
import { chart as plotChart, type BoxModel, type HistModel, type LineModel, type Pt } from "../chartlib";
import { fmt, h } from "../dom";
import type { SliderInfo } from "../engine";
import { bars, barScale, barTrack, live } from "./charts";
import { averageLabel, runningModel, statRow } from "./tools";
import { cat, type Ctx, type Renderer } from "./types";

type ChartBlock = Extract<Block, { type: "chart" }>;

const SAMPLES = 160; // points per line
const GRID = 48; // heatmap cells per side
const DRAWS = 1000; // random draws per histogram
const MAX_RUNS = 20_000;
const POINTS = 240; // scatter: points per random series
const BAND_X = 30; // band: steps across the x slider's range
const BAND_DRAWS = 80; // band: draws at each step
const BOX_DRAWS = 400; // box: draws per box
const RANDOM_VIEWS = new Set<ChartBlock["view"]>(["histogram", "compare", "running", "scatter", "band", "box"]);

const axisTitle = (s: SliderInfo | undefined, name: string) => (s ? `${s.label}${s.unit ? ` (${s.unit.trim()})` : ""}` : name);
const span = (s: SliderInfo, n: number) => Array.from({ length: n + 1 }, (_, k) => s.min + ((s.max - s.min) * k) / n);
const mean = (xs: number[]) => xs.reduce((a, v) => a + v, 0) / xs.length;
const std = (xs: number[]) => { const m = mean(xs); return Math.sqrt(xs.reduce((a, v) => a + (v - m) ** 2, 0) / Math.max(1, xs.length - 1)); };

/** The y range of some values with a little padding (no forced zero: a cloud or a band should fill the chart). */
function padded(vals: number[]): [number, number] | undefined {
  const ys = vals.filter(Number.isFinite);
  if (!ys.length) return undefined;
  let lo = Math.min(...ys), hi = Math.max(...ys);
  const pad = (hi - lo || Math.abs(hi) || 1) * 0.08;
  lo -= pad; hi += pad;
  return [lo, hi];
}

/** A box plot's numbers: quartiles (linear interpolation), mean, and whiskers at the furthest draws within 1.5 × IQR. */
export function boxStats(vals: number[]): Omit<BoxModel["boxes"][number], "label" | "color"> | null {
  const xs = vals.filter(Number.isFinite).sort((p, q) => p - q);
  if (!xs.length) return null;
  const q = (p: number) => {
    const i = (xs.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i);
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo);
  };
  const q1 = q(0.25), median = q(0.5), q3 = q(0.75), iqr = q3 - q1;
  const inside = xs.filter((v) => v >= q1 - 1.5 * iqr && v <= q3 + 1.5 * iqr);
  return { q1, median, q3, lo: inside[0] ?? q1, hi: inside[inside.length - 1] ?? q3, mean: mean(xs) };
}

/** Shared bin edges for one or two sets of samples (so overlaid histograms line up). */
function binsOf(sets: number[][]): { range: [number, number]; bins: HistModel["bins"][] } {
  const all = sets.flat();
  let lo = Math.min(...all), hi = Math.max(...all);
  if (!all.length) { lo = 0; hi = 1; }
  const pad = (hi - lo || Math.abs(hi) || 1) * 0.06;
  lo -= pad; hi += pad;
  const n = Math.min(40, Math.max(10, Math.round(Math.sqrt(Math.max(...sets.map((s) => s.length))))));
  const w = (hi - lo) / n;
  return {
    range: [lo, hi],
    bins: sets.map((xs) => {
      const counts = new Array(n).fill(0);
      for (const x of xs) counts[Math.min(n - 1, Math.max(0, Math.floor((x - lo) / w)))]++;
      return counts.map((count, i) => ({ x0: lo + i * w, x1: lo + (i + 1) * w, count }));
    }),
  };
}

export const chart: Renderer<"chart"> = (b, ctx) => {
  // A list formula is drawn as bars, exactly like the bars block.
  if (b.view === "bars") {
    return bars({ type: "bars", title: b.title, labels: b.labels, values: [b.series[0].expr], defs: b.defs, normalize: false }, ctx);
  }
  if (b.view === "groupedbars") return groupedBars(b, ctx);

  const random = RANDOM_VIEWS.has(b.view);
  const tall = b.view === "heatmap" ? 380 : b.view === "histogram" || b.view === "compare" || b.view === "running" ? 260 : 300;
  const plot = plotChart(tall, b.title || "Chart");
  ctx.onDispose(() => plot.dispose());
  const xs = ctx.store.slider(b.x), bys = ctx.store.slider(b.by);
  const legend = h("div.x-legend");
  const stats = h("div");
  const series = b.series;
  let seed = 0; // Resample moves to fresh draws
  // Band and scatter-over-x curves don't depend on their own x variable (only the marker does): keep the last one and
  // recompute it only when another value changes (a Play counter on x would otherwise redraw thousands of draws per tick).
  let cache: { key: string; value: unknown } | null = null;
  const cached = <T>(compute: () => T): T => {
    const sc = ctx.store.scope();
    const key = JSON.stringify([seed, Object.keys(sc).sort().filter((k) => k !== b.x).map((k) => [k, sc[k]])]);
    if (cache?.key !== key) cache = { key, value: compute() };
    return cache.value as T;
  };
  const legendOf = (items: { label: string; color: number }[]) =>
    legend.replaceChildren(...items.map((it) => h("span.x-legend-item", null, h("i", { style: `background:${cat(it.color)}` }), it.label)));

  /** One random series' values at the current settings (plus overrides), seeds 1..n after `from`. */
  const draws = (expr: string, n: number, from: number, extra: Scope = {}) => {
    const base = { ...ctx.store.scope(), ...extra };
    const out: number[] = [];
    for (let k = 1; k <= n; k++) {
      const v = evalNumber(expr, withDefs(b.defs, { ...base, seed: from + k }));
      if (Number.isFinite(v)) out.push(v);
    }
    return out;
  };

  // ---- running: Run buttons and a running average (the experiment's view) ----
  let runs: number[] = [];
  let total = 0;
  const run = (n: number) => {
    runs.push(...draws(series[0].expr, n, total));
    total += n;
    if (runs.length > MAX_RUNS) runs = runs.slice(-MAX_RUNS);
    draw();
  };

  function draw() {
    const base = ctx.store.scope();
    const at = (extra: Scope) => withDefs(b.defs, { ...base, ...extra });

    if (b.view === "line" && xs) {
      const pts = series.map((s) => span(xs, SAMPLES).map((x) => [x, evalNumber(s.expr, at({ [b.x]: x }))] as Pt));
      const cur = base[b.x] as number;
      plot.draw(withZero({
        kind: "line", xLabel: axisTitle(xs, b.x), yLabel: series.length === 1 ? series[0].label : "", x: [xs.min, xs.max],
        series: series.map((s, i) => ({ label: s.label, pts: pts[i] })),
        marker: { x: cur, ys: series.map((s) => evalNumber(s.expr, at({}))) },
      }));
      legendOf(series.map((s, i) => ({ label: s.label, color: i })));
    } else if (b.view === "lines" && xs && bys) {
      // The fixed `at` lines, plus a "now" line at the by slider's current value, so moving that slider moves the chart
      // (server/explainer/core/scene.py's drawn_values mirrors this).
      const s = series[0];
      const values = linesValues(b.at, base[b.by] as number);
      const names = values.map((v, i) => `${bys.label} = ${fmt(v, 3)}${i === b.at.length ? " (now)" : ""}`);
      plot.draw(withZero({
        kind: "line", xLabel: axisTitle(xs, b.x), yLabel: s.label, x: [xs.min, xs.max],
        series: values.map((v, i) => ({ label: names[i], dashed: i === b.at.length, pts: span(xs, SAMPLES).map((x) => [x, evalNumber(s.expr, at({ [b.x]: x, [b.by]: v }))] as Pt) })),
        marker: { x: base[b.x] as number, ys: values.map((v) => evalNumber(s.expr, at({ [b.by]: v }))) },
      }));
      legendOf(names.map((label, i) => ({ label, color: i })));
    } else if (b.view === "heatmap" && xs && bys) {
      const s = series[0];
      const z = span(bys, GRID - 1).map((y) => span(xs, GRID - 1).map((x) => evalNumber(s.expr, at({ [b.x]: x, [b.by]: y }))));
      plot.draw({
        kind: "heat", xLabel: axisTitle(xs, b.x), yLabel: axisTitle(bys, b.by), zLabel: s.label, x: [xs.min, xs.max], y: [bys.min, bys.max], z,
        diverging: z.flat().some((v) => v < 0) && z.flat().some((v) => v > 0),
        marker: { x: base[b.x] as number, y: base[b.by] as number, label: "now" },
      });
    } else if (b.view === "histogram") {
      const vals = draws(series[0].expr, DRAWS, seed * DRAWS);
      const { range, bins } = binsOf([vals]);
      const m = mean(vals);
      plot.draw({ kind: "hist", xLabel: series[0].label, x: range, bins: bins[0], dots: false, markers: vals.length ? [{ label: "mean", x: m, color: 1 }] : [] });
      stats.replaceChildren(statRow([{ label: "draws", value: String(vals.length) }, { label: "mean", value: fmt(m, 4) }, { label: "spread (std)", value: fmt(std(vals), 3) }]));
    } else if (b.view === "compare" && bys && b.at.length >= 1) {
      // A fixed baseline (the first value of `at`) against the slider's current value, so moving the slider moves the chart.
      const now = base[b.by] as number;
      const values = [b.at[0], now];
      const sets = values.map((v) => draws(series[0].expr, DRAWS, seed * DRAWS, { [b.by]: v }));
      const { range, bins } = binsOf(sets);
      const names = [`${bys.label} = ${fmt(values[0], 3)} (baseline)`, `${bys.label} = ${fmt(now, 3)} (now)`];
      plot.draw({
        kind: "hist", xLabel: series[0].label, x: range, dots: false, bins: bins[0], color: 0, label: names[0],
        overlay: { bins: bins[1], color: 1, label: names[1] }, markers: [],
      });
      legendOf(names.map((label, i) => ({ label, color: i })));
      stats.replaceChildren(statRow(sets.flatMap((xs, i) => [{ label: `spread at ${names[i]}`, value: fmt(std(xs), 3) }])));
    } else if (b.view === "scatter") {
      // Fresh points on Resample: seeds 1..POINTS after seed * POINTS.
      const seedOf = (k: number) => seed * POINTS + k + 1;
      const rnd = series.map((s) => usesRandom(s.expr, b.defs ?? []));
      if (xs) {
        // Each random series as a cloud of draws spread across x's range; a non-random series as a line (e.g. the true curve).
        const { dots, lines } = cached(() => ({
          dots: series.flatMap((s, i) => (rnd[i] ? [{
            label: s.label, color: i,
            pts: Array.from({ length: POINTS }, (_, k) => {
              const x = xs.min + ((xs.max - xs.min) * (k + 0.5)) / POINTS;
              return [x, evalNumber(s.expr, at({ [b.x]: x, seed: seedOf(k) }))] as Pt;
            }),
          }] : [])),
          lines: series.flatMap((s, i) => (rnd[i] ? [] : [{ label: s.label, color: i, pts: span(xs, SAMPLES).map((x) => [x, evalNumber(s.expr, at({ [b.x]: x }))] as Pt) }])),
        }));
        plot.draw({
          kind: "line", xLabel: axisTitle(xs, b.x), yLabel: series.length === 1 ? series[0].label : "", x: [xs.min, xs.max],
          y: padded([...dots, ...lines].flatMap((d) => d.pts.map((p) => p[1]))),
          series: lines, dots,
          marker: { x: base[b.x] as number, ys: lines.map((l) => evalNumber(series[l.color].expr, at({}))) },
        });
      } else {
        // The first two series are each point's position; both read the same draw of the defs (shared randomness goes there).
        const two = series.length >= 2;
        const pts = Array.from({ length: POINTS }, (_, k) => {
          const sc = at({ seed: seedOf(k) });
          return (two ? [evalNumber(series[0].expr, sc), evalNumber(series[1].expr, sc)] : [k + 1, evalNumber(series[0].expr, sc)]) as Pt;
        }).filter(([px, py]) => Number.isFinite(px) && Number.isFinite(py));
        plot.draw({
          kind: "line", xLabel: two ? series[0].label : "draw", yLabel: two ? series[1].label : series[0].label, x: padded(pts.map((p) => p[0])) ?? [0, 1],
          y: padded(pts.map((p) => p[1])), series: [], dots: [{ label: two ? `${series[0].label} vs ${series[1].label}` : series[0].label, color: 0, pts }],
        });
      }
      legendOf(xs && series.length > 1 ? series.map((s, i) => ({ label: s.label, color: i })) : []);
    } else if (b.view === "band" && xs) {
      // The average of the draws at each x, with ± one standard deviation shaded around the first series.
      const rnd = series.map((s) => usesRandom(s.expr, b.defs ?? []));
      const stat = (i: number, x: number) => {
        const v = rnd[i] ? draws(series[i].expr, BAND_DRAWS, seed * BAND_DRAWS, { [b.x]: x }) : [evalNumber(series[i].expr, at({ [b.x]: x }))].filter(Number.isFinite);
        return v.length ? { m: mean(v), sd: v.length > 1 ? std(v) : 0 } : { m: NaN, sd: NaN };
      };
      const { lines, band } = cached(() => {
        const grid = span(xs, BAND_X);
        const stats0 = grid.map((x) => stat(0, x));
        return {
          lines: series.map((s, i) => ({
            label: rnd[i] ? averageLabel(s.label) : s.label, color: i,
            pts: i === 0 ? stats0.map((st, k) => [grid[k], st.m] as Pt) : grid.map((x) => [x, stat(i, x).m] as Pt),
          })),
          band: { hi: stats0.map((st, k) => [grid[k], st.m + st.sd] as Pt), lo: stats0.map((st, k) => [grid[k], st.m - st.sd] as Pt), color: 0 },
        };
      });
      const cur = base[b.x] as number;
      const now = stat(0, cur);
      plot.draw({
        kind: "line", xLabel: axisTitle(xs, b.x), yLabel: series.length === 1 ? series[0].label : "", x: [xs.min, xs.max],
        y: padded([...lines.flatMap((l) => l.pts.map((p) => p[1])), ...band.hi.map((p) => p[1]), ...band.lo.map((p) => p[1])]),
        series: lines, band,
        marker: { x: cur, ys: series.map((_, i) => (i === 0 ? now.m : stat(i, cur).m)) },
      });
      legendOf([...lines.map((l) => ({ label: l.label, color: l.color })), { label: "± one standard deviation", color: 0 }]);
      stats.replaceChildren(statRow([
        { label: `average at ${xs.label} = ${fmt(cur, 3)}`, value: fmt(now.m, 4) },
        { label: "spread (std)", value: fmt(now.sd, 3) },
      ]));
    } else if (b.view === "box" && bys) {
      // One box per fixed `at` value plus one at the slider's current value (in order along the axis).
      const now = base[b.by] as number;
      const values = linesValues(b.at, now);
      const boxes = values
        .map((v, i) => {
          const st = boxStats(draws(series[0].expr, BOX_DRAWS, seed * BOX_DRAWS, { [b.by]: v }));
          return st && { ...st, v, label: `${fmt(v, 3)}${i === b.at.length ? " (now)" : ""}`, color: i === b.at.length ? 1 : 0 };
        })
        .filter((x): x is NonNullable<typeof x> => !!x)
        .sort((p, q) => p.v - q.v);
      plot.draw({ kind: "box", xLabel: axisTitle(bys, b.by), yLabel: series[0].label, boxes });
      legendOf([{ label: `fixed values of ${bys.label}`, color: 0 }, ...(values.length > b.at.length ? [{ label: `${bys.label} now (the slider)`, color: 1 }] : [])]);
      stats.replaceChildren(statRow(boxes.map((bx) => ({ label: `middle half at ${bx.label}`, value: fmt(bx.q3 - bx.q1, 3) }))));
    } else if (b.view === "running") {
      plot.el.style.display = runs.length > 1 ? "" : "none";
      if (runs.length > 1) plot.draw(runningModel(runs));
      stats.replaceChildren(statRow([{ label: "runs", value: String(runs.length) }, { label: "running average", value: runs.length ? fmt(mean(runs), 4) : "—" }]));
    }
  }

  // Random views get Resample (fresh draws) or Run buttons; any setting change starts a running chart over.
  const buttons = b.view === "running"
    ? h("div.x-exp-controls", null,
        ...[1, 10, 100].map((n) => h("button.x-btn.x-btn-primary.x-btn-sm", { type: "button", onclick: () => run(n) }, n === 1 ? "▶ Run once" : `Run ×${n}`)),
        h("button.x-btn.x-btn-ghost.x-btn-sm", { type: "button", onclick: () => { runs = []; draw(); } }, "Clear"))
    : random ? h("button.x-btn.x-btn-ghost.x-btn-sm", { type: "button", onclick: () => { seed++; draw(); } }, "🎲 Resample") : null;
  if (b.view === "running") ctx.onDispose(ctx.store.watch(() => { runs = []; draw(); }));
  live(ctx, draw);

  return h("figure.x-chart", null,
    h("div.x-chart-head", null, b.title ? h("figcaption.x-chart-title", null, b.title) : h("span"), b.view === "running" ? null : buttons),
    b.view === "running" ? buttons : null,
    plot.el, legend, stats,
    b.caption ? h("p.x-caption", null, b.caption) : null);
};

/** groupedbars: 2-3 list formulas side by side for each label, on one shared scale (plain HTML, like the bars block). */
function groupedBars(b: ChartBlock, ctx: Ctx): HTMLElement {
  const list = h("div.x-bars.x-gbars");
  const legend = h("div.x-legend", null, ...b.series.map((s, i) => h("span.x-legend-item", null, h("i", { style: `background:${cat(i)}` }), s.label)));
  live(ctx, () => {
    const scope = ctx.store.scope(b.defs);
    const rows = b.series.map((s) => evalList(s.expr, scope).slice(0, 24).map((v) => (Number.isFinite(v) ? v : 0)));
    const n = Math.max(0, ...rows.map((r) => r.length));
    const { maxAbs, signed } = barScale(rows.flat());
    list.replaceChildren(...Array.from({ length: n }, (_, i) =>
      h("div.x-gbar-group", null,
        h("span.x-bar-label", null, b.labels[i] ?? `#${i + 1}`),
        h("div.x-gbar-stack", null, ...rows.map((r, si) =>
          h("div.x-gbar-row", { title: b.series[si].label },
            barTrack(r[i] ?? 0, maxAbs, signed, cat(si)),
            h("span.x-bar-val", null, i < r.length ? fmt(r[i], 3) : "—")))))));
  });
  return h("figure.x-chart", null, b.title ? h("figcaption.x-chart-title", null, b.title) : null, list, legend,
    b.caption ? h("p.x-caption", null, b.caption) : null);
}

/** The by values a lines chart draws: its `at` values, plus the current one unless it is already one of them. */
export function linesValues(at: number[], now: number): number[] {
  const near = (v: number) => Math.abs(v - now) <= 1e-9 * Math.max(1, Math.abs(now));
  return Number.isFinite(now) && !at.some(near) ? [...at, now] : at;
}

/** Auto y range that includes 0 (like the plot block), so small changes aren't exaggerated. */
function withZero(m: LineModel): LineModel {
  const ys = m.series.flatMap((s) => s.pts.map((p) => p[1])).filter(Number.isFinite);
  let lo = Math.min(...ys, 0), hi = Math.max(...ys, lo + 1e-9);
  const pad = (hi - lo || 1) * 0.06;
  hi += pad;
  if (lo < 0) lo -= pad;
  return { ...m, y: [lo, hi] };
}
