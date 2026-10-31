// Controls and live visuals: slider, choice, plot, bars, readout.
// Visuals subscribe to the store and recompute their expressions on change.

import { evalList, evalNumber } from "../../../../shared/expr";
import { areaUnder, withDefs } from "../../../../shared/views";
import { chart as plotChart, type LineModel } from "../chartlib";
import { fmt, h } from "../dom";
import { perFrame } from "../engine";
import { cat, type Ctx, type Renderer } from "./types";

/** Format a value by kind: probability (0..1) and percent (0..100) both show as %. */
export function fmtKind(v: number, digits: number, kind?: "number" | "percent" | "probability"): string {
  if (!Number.isFinite(v)) return "—";
  if (kind === "probability") return `${fmt(v * 100, Math.max(0, digits - 2))}%`;
  if (kind === "percent") return `${fmt(v, digits)}%`;
  return fmt(v, digits);
}

/** Re-render on store changes (once per frame) and register cleanup. */
export function live(ctx: Ctx, draw: () => void) {
  ctx.onDispose(ctx.store.watch(perFrame(draw)));
  draw();
}

export const slider: Renderer<"slider"> = (b, ctx) => {
  const out = h("output.x-slider-value");
  const input = h("input.x-range", { type: "range", min: b.min, max: b.max, step: b.step, value: ctx.store.get(b.var) ?? b.value, "aria-label": b.label });
  const show = () => (out.textContent = `${fmt(Number(input.value), 4)}${b.unit ? ` ${b.unit}` : ""}`);
  input.addEventListener("input", () => {
    show();
    ctx.store.set(b.var, Number(input.value));
  });
  // Another slider bound to the same variable may move it.
  ctx.onDispose(ctx.store.watch(() => {
    const v = ctx.store.get(b.var);
    if (Number(input.value) !== v) { input.value = String(v); show(); }
  }));
  show();
  return h("div.x-control", { "data-var": b.var }, h("div.x-control-head", null, h("label", null, b.label), out), input,
    h("div.x-range-scale", { "aria-hidden": "true" }, h("span", null, fmt(b.min, 4)), h("span", null, fmt(b.max, 4))));
};

export const choice: Renderer<"choice"> = (b, ctx) => {
  const buttons = b.options.map((o) =>
    h("button.x-seg", { type: "button", onclick: () => ctx.store.set(b.var, o.value) }, o.label));
  const sync = () => buttons.forEach((btn, i) => btn.setAttribute("aria-pressed", String(b.options[i].value === ctx.store.get(b.var))));
  ctx.onDispose(ctx.store.watch(sync));
  sync();
  return h("div.x-control", { "data-var": b.var }, h("div.x-control-head", null, h("span.x-label", null, b.label)), h("div.x-segmented", { role: "group", "aria-label": b.label }, ...buttons));
};

export const readout: Renderer<"readout"> = (b, ctx) => {
  const values = b.items.map(() => h("div.x-readout-value"));
  live(ctx, () => {
    const scope = ctx.store.scope(b.defs);
    b.items.forEach((it, i) => (values[i].textContent = fmtKind(evalNumber(it.expr, scope), it.digits, it.kind)));
  });
  return h("div.x-readouts", null, ...b.items.map((it, i) => h("div.x-readout", null, values[i], h("div.x-readout-label", null, it.label))));
};

/**
 * One bar's track. When every value is ≥ 0 bars grow from the left; when any is negative
 * they grow from a centre zero line (negatives to the left), so a negative term is never shown as 0.
 */
export function barTrack(v: number, maxAbs: number, signed: boolean, color: string): HTMLElement {
  const w = maxAbs > 0 && Number.isFinite(v) ? (Math.abs(v) / maxAbs) * (signed ? 50 : 100) : 0;
  const style = signed ? `width:${w}%;left:${v < 0 ? 50 - w : 50}%;background:${color}` : `width:${w}%;background:${color}`;
  return h(signed ? "div.x-bar-track.is-signed" : "div.x-bar-track", null, h("div.x-bar-fill", { style }));
}

/** The scale for a set of bars: the largest size, and whether any bar is negative. */
export function barScale(vals: number[]): { maxAbs: number; signed: boolean } {
  return { maxAbs: Math.max(1e-9, ...vals.map((v) => Math.abs(v))), signed: vals.some((v) => v < 0) };
}

export const bars: Renderer<"bars"> = (b, ctx) => {
  const list = h("div.x-bars");
  // One expression returning a list → one bar per element; else one expression per label.
  const listMode = b.values.length === 1 && b.labels.length !== 1;
  live(ctx, () => {
    const scope = ctx.store.scope(b.defs);
    const raw = (listMode ? evalList(b.values[0], scope) : b.values.map((e) => evalNumber(e, scope))).slice(0, 24).map((v) => (Number.isFinite(v) ? v : 0));
    // Shares of a total only make sense when nothing is negative; otherwise show the values themselves.
    const asShare = b.normalize && raw.every((v) => v >= 0);
    const total = raw.reduce((a, v) => a + v, 0) || 1;
    const shown = asShare ? raw.map((v) => v / total) : raw;
    const { maxAbs, signed } = barScale(shown);
    list.replaceChildren(...shown.map((v, i) =>
      h("div.x-bar-row", null,
        h("span.x-bar-label", null, b.labels[i] ?? `#${i + 1}`),
        barTrack(v, maxAbs, signed, cat(i)),
        h("span.x-bar-val", null, asShare ? `${(v * 100).toFixed(1)}%` : fmt(v, 3)))));
  });
  return h("figure.x-chart", null, b.title ? h("figcaption.x-chart-title", null, b.title) : null, list);
};

const SAMPLES = 160;

export const plot: Renderer<"plot"> = (b, ctx) => {
  const chart = plotChart(300, b.title || "Plot");
  ctx.onDispose(() => chart.dispose());
  const legend = h("div.x-legend", null, ...b.series.map((s, i) => h("span.x-legend-item", null, h("i", { style: `background:${cat(i)}` }), s.label)));
  const shadeOut = b.shade ? h("div.x-shade-readout") : null;

  function draw() {
    const base = ctx.store.scope();
    const scope = withDefs(b.defs, base);
    // sweepVar: the x axis sweeps a slider variable, so defs are recomputed per x.
    const sv = b.sweepVar;
    const f = sv
      ? (i: number, x: number) => evalNumber(b.series[i].expr, withDefs(b.defs, { ...base, [sv]: x, x }))
      : (i: number, x: number) => evalNumber(b.series[i].expr, { ...scope, x });
    const data = b.series.map((_, i) => Array.from({ length: SAMPLES + 1 }, (_, k) => {
      const x = b.xMin + ((b.xMax - b.xMin) * k) / SAMPLES;
      return [x, f(i, x)] as [number, number];
    }));
    const m: LineModel = {
      kind: "line", xLabel: b.xLabel, yLabel: b.yLabel, x: [b.xMin, b.xMax],
      series: b.series.map((s, i) => ({ label: s.label, pts: data[i] })),
    };
    if (b.yMax > b.yMin) m.y = [b.yMin, b.yMax];
    else {
      // Auto range that always includes 0, so small changes aren't exaggerated.
      const ys = data.flat().map((p) => p[1]).filter(Number.isFinite);
      let yMin = Math.min(...ys, 0), yMax = Math.max(...ys, yMin + 1);
      const pad = (yMax - yMin) * 0.06;
      yMax += pad;
      if (yMin < 0) yMin -= pad;
      m.y = [yMin, yMax];
    }

    if (b.shade) {
      const from = evalNumber(b.shade.from, scope), to = evalNumber(b.shade.to, scope);
      const lo = Math.max(b.xMin, Math.min(from, to)), hi = Math.min(b.xMax, Math.max(from, to));
      const si = b.shade.series;
      if (Number.isFinite(lo) && Number.isFinite(hi) && hi > lo && data[si]) {
        // The band's edges sit exactly on `from` and `to`, not on the nearest sample.
        const inside = data[si].filter(([x]) => x > lo && x < hi);
        m.series[si] = { ...m.series[si], pts: [...data[si].filter(([x]) => x <= lo), [lo, f(si, lo)], ...inside, [hi, f(si, hi)], ...data[si].filter(([x]) => x >= hi)] };
        m.shade = { series: si, from: lo, to: hi };
      }
      shadeOut!.replaceChildren(h("span", null, b.shade.label || "Shaded area"), h("strong", null, fmt(areaUnder((x) => f(si, x), from, to), 4)));
    }

    if (b.markerX) {
      const mx = evalNumber(b.markerX, scope);
      if (Number.isFinite(mx) && mx >= b.xMin && mx <= b.xMax) m.marker = { x: mx, ys: b.series.map((_, i) => f(i, mx)) };
    }
    chart.draw(m);
  }

  live(ctx, draw);
  return h("figure.x-chart", null, b.title ? h("figcaption.x-chart-title", null, b.title) : null, chart.el, shadeOut, h("div.x-chart-foot", null, legend));
};
