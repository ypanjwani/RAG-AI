// Equation views, picked by the shape of an equation:
//   samples     sums / means / std over random draws (Monte Carlo, MC-Dropout)
//   heatmap     z = f(x, y)
//   trajectory  update rules x_{t+1} = f(x_t)
//   matrix      matrix operations with live cells
//   calculator  any equation, evaluated step by step
// All math goes through shared/expr.ts; the AI only supplies expressions.

import { evalList, evalMatrix, evalNumber, evalValue, isMat, isNum, usesRandom, type Scope } from "../../../../shared/expr";
import { runTrajectory } from "../../../../shared/views";
import { chart as plotChart, type HeatModel, type Pt } from "../chartlib";
import { fmt, h, katexHtml } from "../dom";
import { fmtKind, live } from "./charts";
import { cat, type Ctx, type Renderer } from "./types";

function resampleButton(onClick: () => void) {
  return h("button.x-btn.x-btn-ghost.x-btn-sm", { type: "button", onclick: onClick }, "🎲 Resample");
}

function statRow(items: { label: string; value: string }[]) {
  return h("div.x-stat-row", null, ...items.map((s) => h("div.x-stat", null, h("strong", null, s.value), h("span", null, s.label))));
}

// ---------- samples ----------

export const samples: Renderer<"samples"> = (b, ctx) => {
  let seed = 1;
  const chart = plotChart(260, b.title || "Samples");
  ctx.onDispose(() => chart.dispose());
  const stats = h("div");

  function draw() {
    const scope = ctx.store.scope(b.defs, { seed });
    const xs = evalList(b.values, scope).filter(Number.isFinite).slice(0, 10_000);
    const markers = b.markers.map((m) => ({ label: m.label, x: evalNumber(m.expr, scope) }));
    const band = b.band ? { label: b.band.label, from: evalNumber(b.band.from, scope), to: evalNumber(b.band.to, scope) } : null;
    const all = [...xs, ...markers.map((m) => m.x), ...(band ? [band.from, band.to] : [])].filter(Number.isFinite);
    let x0 = Math.min(...all), x1 = Math.max(...all);
    if (!all.length) { x0 = 0; x1 = 1; }
    const pad = (x1 - x0 || 1) * 0.06;
    x0 -= pad; x1 += pad;

    // Histogram counts (dot stacks when there are few samples).
    const dots = xs.length <= 80;
    const bins = dots ? 36 : Math.min(40, Math.max(10, Math.round(Math.sqrt(xs.length))));
    const counts = new Array(bins).fill(0);
    const bw = (x1 - x0) / bins;
    for (const x of xs) counts[Math.min(bins - 1, Math.max(0, Math.floor((x - x0) / bw)))]++;
    chart.draw({
      kind: "hist", xLabel: b.xLabel, x: [x0, x1], dots,
      bins: counts.map((count, i) => ({ x0: x0 + i * bw, x1: x0 + (i + 1) * bw, count })),
      markers: markers.map((m, i) => ({ label: m.label, x: m.x, color: i + 1 })),
      band: band ?? undefined,
    });

    stats.replaceChildren(statRow([
      { label: "samples", value: String(xs.length) },
      ...b.stats.map((st) => ({ label: st.label, value: fmtKind(evalNumber(st.expr, scope), st.digits, st.kind) })),
    ]));
  }

  live(ctx, draw);
  return h("figure.x-chart.x-samples", null,
    h("div.x-chart-head", null, b.title ? h("figcaption.x-chart-title", null, b.title) : h("span"),
      // Resampling only means something when the samples are random.
      usesRandom(b.values, b.defs ?? []) ? resampleButton(() => { seed++; draw(); }) : null),
    chart.el, stats,
    b.caption ? h("p.x-caption", null, b.caption) : null);
};

// ---------- heatmap ----------

function gridValues(expr: string, scope: Scope, xr: [number, number], yr: [number, number], n: number): number[][] {
  return Array.from({ length: n }, (_, r) => {
    const y = yr[0] + ((yr[1] - yr[0]) * r) / (n - 1);
    return Array.from({ length: n }, (_, k) => evalNumber(expr, { ...scope, x: xr[0] + ((xr[1] - xr[0]) * k) / (n - 1), y }));
  });
}

export const heatmap: Renderer<"heatmap"> = (b, ctx) => {
  const N = 64;
  const chart = plotChart(380, b.title || "Heatmap");
  ctx.onDispose(() => chart.dispose());

  function draw() {
    const scope = ctx.store.scope(b.defs);
    const m: HeatModel = {
      kind: "heat", xLabel: b.xLabel, yLabel: b.yLabel, zLabel: b.zLabel, x: [b.xMin, b.xMax], y: [b.yMin, b.yMax],
      z: gridValues(b.expr, scope, [b.xMin, b.xMax], [b.yMin, b.yMax], N), diverging: b.diverging,
    };
    if (b.marker) m.marker = { x: evalNumber(b.marker.x, scope), y: evalNumber(b.marker.y, scope), label: b.marker.label };
    chart.draw(m);
  }

  live(ctx, draw);
  return h("figure.x-chart", null, b.title ? h("figcaption.x-chart-title", null, b.title) : null, chart.el,
    b.caption ? h("p.x-caption", null, b.caption) : null);
};

// ---------- trajectory ----------

export const trajectory: Renderer<"trajectory"> = (b, ctx) => {
  const path = b.mode === "path" ? b.path : undefined;
  const chart = plotChart(path ? 380 : 300, b.title || "Trajectory");
  ctx.onDispose(() => chart.dispose());
  const status = h("div.x-train-status", { "aria-live": "polite" });
  const legend = h("div.x-legend");
  let reveal = 1; // fraction of steps drawn (animation)
  let anim = 0;

  function draw() {
    const scope = ctx.store.scope(b.defs); // for the surface (defs that need the state are NaN here)
    const run = runTrajectory(b, ctx.store.scope()); // applies the defs at every step itself
    const shown = run.frames.slice(0, Math.max(1, Math.ceil(run.frames.length * reveal)));

    if (path) {
      const pts = shown.map((f) => [evalNumber(path.x, f), evalNumber(path.y, f)] as Pt);
      chart.draw({
        kind: "path", xLabel: path.xLabel, yLabel: path.yLabel, x: [path.xMin, path.xMax], y: [path.yMin, path.yMax],
        surface: path.surface ? gridValues(path.surface, scope, [path.xMin, path.xMax], [path.yMin, path.yMax], 48) : undefined,
        pts, showSteps: pts.length <= 80,
      });
      legend.replaceChildren(
        h("span.x-legend-item", null, h("i.is-start"), "start"),
        h("span.x-legend-item", null, h("i.is-end"), `after ${shown.length - 1} steps`));
    } else {
      const data = b.series.map((s) => shown.map((f, t) => [t, evalNumber(s.expr, f)] as Pt));
      const ys = data.flat().map((p) => p[1]).filter((v) => Number.isFinite(v) && (!b.logY || v > 0));
      let y0 = Math.min(...ys), y1 = Math.max(...ys);
      if (!ys.length) { y0 = 0; y1 = 1; }
      if (!b.logY) { const pad = (y1 - y0 || 1) * 0.06; y0 = Math.min(y0 - pad, y0 >= 0 ? 0 : y0 - pad); y1 += pad; }
      else { y0 = 10 ** Math.floor(Math.log10(y0)); y1 = 10 ** Math.ceil(Math.log10(y1)); if (y1 === y0) y1 = y0 * 10; }
      chart.draw({
        kind: "line", xLabel: "step t", yLabel: "", x: [0, Math.max(1, run.frames.length - 1)], y: [y0, y1], logY: b.logY,
        series: b.series.map((s, i) => ({ label: s.label, pts: data[i] })),
      });
      legend.replaceChildren(...b.series.map((s, i) => h("span.x-legend-item", null, h("i", { style: `background:${cat(i)}` }), s.label)));
    }

    const first = run.frames[0], last = run.frames[run.frames.length - 1];
    const finals = b.state.map((s) => `${s.name} = ${fmt(last[s.name] as number, 4)}`).join(" · ");
    const mag = (f: Scope) => Math.max(...b.state.map((s) => Math.abs(f[s.name] as number)));
    const growing = run.frames.length > 5 && mag(last) > 10 * Math.max(1, mag(first));
    status.replaceChildren(run.divergedAt > 0
      ? h("div", null, `💥 Diverged at step ${run.divergedAt}: the values blew up. `, h("span.x-muted", null, `Last stable: ${finals}`))
      : growing
        ? h("div", null, "📈 Diverging: the values grow every step instead of settling. ", h("span.x-muted", null, finals))
        : h("div", null, `After ${run.frames.length - 1} steps: `, h("strong", null, finals)));
  }

  const animate = () => {
    cancelAnimationFrame(anim);
    const start = performance.now();
    const tick = (now: number) => {
      reveal = Math.min(1, (now - start) / 1800);
      draw();
      if (reveal < 1) anim = requestAnimationFrame(tick);
    };
    anim = requestAnimationFrame(tick);
  };
  ctx.onDispose(() => cancelAnimationFrame(anim));
  live(ctx, () => { reveal = 1; draw(); });
  return h("figure.x-chart", null,
    h("div.x-chart-head", null, b.title ? h("figcaption.x-chart-title", null, b.title) : h("span"),
      h("button.x-btn.x-btn-ghost.x-btn-sm", { type: "button", onclick: animate }, "▶ Animate")),
    chart.el, legend, status, b.caption ? h("p.x-caption", null, b.caption) : null);
};

// ---------- matrix ----------

const SHOW = 8; // max rows/cols drawn per matrix

export const matrix: Renderer<"matrix"> = (b, ctx) => {
  const row = h("div.x-mat-row");
  live(ctx, () => {
    const scope = ctx.store.scope(b.defs);
    const mats = b.items.map((it) => evalMatrix(it.expr, scope));
    const vecCol = b.items.map((it) => { const v = evalValue(it.expr, scope); return !isNum(v) && !isMat(v); });
    const maxAbs = Math.max(1e-9, ...mats.flatMap((m) => m.flat().filter(Number.isFinite).map(Math.abs)));
    row.replaceChildren(...b.items.flatMap((it, i) => {
      // A plain list is shown as a column vector.
      const m = vecCol[i] ? mats[i][0].map((v) => [v]) : mats[i];
      const rows = m.length, cols = m[0]?.length ?? 0;
      const grid = h("div.x-mat-grid", { style: `grid-template-columns: repeat(${Math.min(cols, SHOW) + (cols > SHOW ? 1 : 0)}, auto)` });
      m.slice(0, SHOW).forEach((r, ri) => {
        r.slice(0, SHOW).forEach((v) => {
          const a = Number.isFinite(v) ? Math.min(1, Math.abs(v) / maxAbs) : 0;
          grid.append(h("span.x-mat-cell", { style: `background:color-mix(in srgb, var(${v < 0 ? "--neg" : "--pos"}) ${Math.round(a * 70)}%, var(--surface))` }, Number.isFinite(v) ? fmt(v, b.digits) : "—"));
        });
        if (cols > SHOW) grid.append(h("span.x-mat-more", null, ri === 0 ? "…" : ""));
      });
      if (rows > SHOW) for (let k = 0; k < Math.min(cols, SHOW) + (cols > SHOW ? 1 : 0); k++) grid.append(h("span.x-mat-more", null, "⋮"));
      const box = h("div.x-mat", null, (/[\^_{}]/.test(it.label) ? h("div.x-mat-label", { html: katexHtml(it.label, false) }) : h("div.x-mat-label.is-text", null, it.label)), h("div.x-mat-bracket", null, grid), h("div.x-mat-shape", null, `${rows}×${cols}`));
      return it.op ? [h("div.x-mat-op", null, it.op), box] : [box];
    }));
  });
  return h("figure.x-chart", null, b.title ? h("figcaption.x-chart-title", null, b.title) : null, h("div.x-mat-scroll", null, row), b.caption ? h("p.x-caption", null, b.caption) : null);
};

// ---------- calculator ----------

function fmtValue(src: string, scope: Scope, digits: number): string {
  const v = evalValue(src, scope);
  if (isNum(v)) return fmt(v, digits);
  if (isMat(v)) return `${v.length}×${v[0].length} matrix`;
  const shown = v.slice(0, 6).map((x) => fmt(x, digits)).join(", ");
  return `[${shown}${v.length > 6 ? `, … (${v.length})` : ""}]`;
}

export const calculator: Renderer<"calculator"> = (b, ctx: Ctx) => {
  const values = b.rows.map(() => h("output.x-calc-value"));
  live(ctx, () => {
    const scope = ctx.store.scope(b.defs);
    b.rows.forEach((r, i) => (values[i].textContent = fmtValue(r.expr, scope, r.digits)));
  });
  return h("figure.x-calc", null,
    h("div.x-chart-head", null, h("figcaption.x-chart-title", null, h("span.x-badge", null, "Worked example"), " ", b.title)),
    h("ol.x-calc-rows", null, ...b.rows.map((r, i) =>
      h("li.x-calc-row", null,
        h("div.x-calc-math", { html: katexHtml(r.latex, false) }),
        h("div.x-calc-eq", { "aria-hidden": "true" }, "="),
        values[i],
        r.note ? h("div.x-calc-note", null, r.note) : null))),
    b.caption ? h("p.x-caption", null, b.caption) : null);
};
