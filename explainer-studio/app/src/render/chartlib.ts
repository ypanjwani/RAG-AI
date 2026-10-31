// Draws charts with Plotly. The only file that imports it: renderers compute
// every number (shared/expr.ts, shared/views.ts) and hand a ChartModel here.
// Plotly is loaded on first use, so the app's first load stays small; the
// export viewer's single-file build inlines it, so exported pages work offline.

import { watchTheme } from "./theme";

export type Pt = [number, number];

/** Line chart: curves, running averages, loss curves. Colours are palette slots (--cat-N). */
export type LineModel = {
  kind: "line";
  xLabel: string; yLabel: string;
  x: [number, number];
  /** Fixed y range; auto when absent. */
  y?: [number, number];
  logX?: boolean; logY?: boolean;
  series: { label: string; pts: Pt[]; color?: number; dashed?: boolean }[];
  /** Shaded band between two edges (e.g. ± one standard error). */
  band?: { hi: Pt[]; lo: Pt[]; color?: number };
  /** Vertical marker (a slider's value) with a dot on each series. */
  marker?: { x: number; ys: number[] };
  /** Shaded area under one series between two x values. */
  shade?: { series: number; from: number; to: number };
  /** Horizontal reference lines (e.g. a true value). */
  hlines?: { y: number; color: number }[];
  /** Scatter points (random draws), drawn under the lines. */
  dots?: { label: string; pts: Pt[]; color?: number }[];
};

/** Histogram (or a dot stack when there are few samples). */
export type HistModel = {
  kind: "hist";
  xLabel: string;
  x: [number, number];
  bins: { x0: number; x1: number; count: number }[];
  dots: boolean;
  /** Bar colour (palette slot) and name, used when a second histogram is overlaid. */
  color?: number; label?: string;
  /** A second histogram drawn over the first (same bin edges), e.g. the same quantity at another setting. */
  overlay?: { bins: { x0: number; x1: number; count: number }[]; color: number; label: string };
  markers: { label: string; x: number; color: number }[];
  band?: { label: string; from: number; to: number };
};

/** z = f(x, y) as a colour map; z[row][col], rows from low y to high y. */
export type HeatModel = {
  kind: "heat";
  xLabel: string; yLabel: string; zLabel: string;
  x: [number, number]; y: [number, number];
  z: number[][];
  diverging: boolean;
  marker?: { x: number; y: number; label: string };
};

/** A path through the plane (an update rule's steps), optionally over a surface. */
export type PathModel = {
  kind: "path";
  xLabel: string; yLabel: string;
  x: [number, number]; y: [number, number];
  surface?: number[][];
  pts: Pt[];
  showSteps: boolean;
};

/** Box plots from precomputed numbers, one per category along the x axis (in the given order). */
export type BoxModel = {
  kind: "box";
  xLabel: string; yLabel: string;
  boxes: { label: string; q1: number; median: number; q3: number; lo: number; hi: number; mean: number; color?: number }[];
};

export type ChartModel = LineModel | HistModel | HeatModel | PathModel | BoxModel;

export interface Chart {
  /** The element to put in the page (sized before Plotly loads, so nothing jumps). */
  el: HTMLElement;
  /** Draw or redraw; cheap to call on every slider move. */
  draw(m: ChartModel): void;
  dispose(): void;
}

type PlotlyApi = typeof import("plotly.js-cartesian-dist-min").default;
let plotly: PlotlyApi | null = null;
let loading: Promise<PlotlyApi> | null = null;
function load(): Promise<PlotlyApi> {
  loading ??= import("plotly.js-cartesian-dist-min").then((m) => (plotly = (m.default ?? m) as PlotlyApi));
  return loading;
}

// ---------- theme ----------

type Theme = { cats: string[]; text: string; text2: string; muted: string; grid: string; surface: string; surface2: string; neg: string; pos: string; accent: string; font: string; mono: string };

function themeOf(el: Element): Theme {
  const cs = getComputedStyle(el);
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return {
    cats: [0, 1, 2, 3, 4, 5].map((i) => v(`--cat-${i}`, "#2563eb")),
    text: v("--text", "#111"), text2: v("--text-2", "#444"), muted: v("--muted", "#777"), grid: v("--border", "#e5e5e5"),
    surface: v("--surface", "#fff"), surface2: v("--surface-2", "#f4f4f4"), neg: v("--neg", "#2563eb"), pos: v("--pos", "#ea580c"),
    accent: v("--accent", "#2563eb"), font: v("--font-body", "system-ui, sans-serif"), mono: v("--font-mono", "monospace"),
  };
}

/** Any CSS colour with an alpha (theme colours may be hex, rgb() or named). */
let probe: CanvasRenderingContext2D | null = null;
function alpha(color: string, a: number): string {
  probe ??= document.createElement("canvas").getContext("2d");
  if (!probe) return color;
  probe.fillStyle = "#000";
  probe.fillStyle = color;
  const c = probe.fillStyle as string;
  const hex = /^#([0-9a-f]{6})$/i.exec(c);
  const rgb = hex ? [0, 2, 4].map((i) => parseInt(hex[1].slice(i, i + 2), 16)) : (/rgba?\(([^)]+)\)/.exec(c)?.[1].split(",").slice(0, 3).map(Number) ?? [0, 0, 0]);
  return `rgba(${rgb.join(",")},${a})`;
}

/** Plotly renders a small HTML subset in text; AI-written labels must stay plain text. */
const esc = (s: string) => (s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const finiteOrNull = (y: number) => (Number.isFinite(y) ? y : null);

// ---------- figures ----------

function figure(m: ChartModel, t: Theme, width: number, height: number): { data: object[]; layout: object } {
  const axis = (title: string, extra: object = {}) => ({
    title: title ? { text: esc(title), font: { size: 12, color: t.text2 }, standoff: 8 } : undefined,
    gridcolor: t.grid, zerolinecolor: t.grid, linecolor: t.grid, showline: true, ticks: "",
    tickfont: { size: 11, color: t.muted, family: t.mono }, automargin: true, fixedrange: true,
    exponentformat: "power", hoverformat: ".4~g",
    spikecolor: t.muted, spikethickness: 1, spikedash: "dot", spikemode: "across", spikesnap: "cursor",
    ...extra,
  });
  const cat = (i = 0) => t.cats[((i % 6) + 6) % 6];
  const data: object[] = [];
  const shapes: object[] = [];
  const annotations: object[] = [];
  const layout: Record<string, unknown> = {
    width, height, margin: { l: 4, r: 16, t: 10, b: 4 }, autosize: false,
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: t.font, color: t.text2, size: 12 }, showlegend: false, dragmode: false,
    hovermode: "x unified",
    hoverlabel: { bgcolor: t.surface, bordercolor: t.grid, font: { color: t.text, family: t.font, size: 12 } },
    shapes, annotations,
  };

  if (m.kind === "line") {
    const logRange = (r: [number, number]) => [Math.log10(Math.max(r[0], 1e-12)), Math.log10(Math.max(r[1], 1e-12))];
    // Log x axes count things (runs, steps): plain 1, 10, 100 reads better than powers of ten.
    layout.xaxis = axis(m.xLabel, { type: m.logX ? "log" : "linear", range: m.logX ? logRange(m.x) : m.x, ...(m.logX ? { dtick: 1, exponentformat: "none" } : {}) });
    layout.yaxis = axis(m.yLabel, { type: m.logY ? "log" : "linear", ...(m.logY ? { dtick: 1 } : {}), ...(m.y ? { range: m.logY ? logRange(m.y) : m.y } : {}) });
    if (m.band) {
      const c = cat(m.band.color);
      data.push({ x: m.band.lo.map((p) => p[0]), y: m.band.lo.map((p) => finiteOrNull(p[1])), mode: "lines", line: { width: 0 }, hoverinfo: "skip" });
      data.push({ x: m.band.hi.map((p) => p[0]), y: m.band.hi.map((p) => finiteOrNull(p[1])), mode: "lines", line: { width: 0 }, fill: "tonexty", fillcolor: alpha(c, 0.15), hoverinfo: "skip" });
    }
    for (const d of m.dots ?? []) {
      const pts = d.pts.filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
      data.push({
        x: pts.map((p) => p[0]), y: pts.map((p) => p[1]), mode: "markers",
        marker: { size: 6, color: alpha(cat(d.color ?? 0), 0.55), line: { width: 0 } },
        hovertemplate: `${esc(d.label)}: (%{x:.3~g}, %{y:.3~g})<extra></extra>`,
      });
    }
    // A cloud of points: hover one point, not every point at that x.
    if (m.dots?.length) layout.hovermode = "closest";
    if (m.shade && m.series[m.shade.series]) {
      const s = m.series[m.shade.series];
      const inside = s.pts.filter(([x, y]) => x >= m.shade!.from && x <= m.shade!.to && Number.isFinite(y));
      data.push({ x: inside.map((p) => p[0]), y: inside.map((p) => p[1]), mode: "lines", line: { width: 0 }, fill: "tozeroy", fillcolor: alpha(cat(s.color ?? m.shade.series), 0.25), hoverinfo: "skip" });
    }
    m.series.forEach((s, i) => {
      data.push({
        x: s.pts.map((p) => p[0]), y: s.pts.map((p) => finiteOrNull(p[1])), mode: "lines", connectgaps: false,
        line: { color: cat(s.color ?? i), width: 2.5, dash: s.dashed ? "dash" : "solid" },
        hovertemplate: `${esc(s.label)}: %{y:.4~g}<extra></extra>`,
      });
    });
    for (const hl of m.hlines ?? []) {
      if (Number.isFinite(hl.y)) shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1, y0: hl.y, y1: hl.y, line: { color: cat(hl.color), width: 2, dash: "dash" } });
    }
    if (m.marker && Number.isFinite(m.marker.x)) {
      shapes.push({ type: "line", x0: m.marker.x, x1: m.marker.x, yref: "paper", y0: 0, y1: 1, line: { color: t.text2, width: 1.5, dash: "dot" } });
      const ys = m.marker.ys.map((y, i) => ({ y, c: cat(m.series[i]?.color ?? i) })).filter((p) => Number.isFinite(p.y));
      data.push({ x: ys.map(() => m.marker!.x), y: ys.map((p) => p.y), mode: "markers", marker: { size: 10, color: ys.map((p) => p.c), line: { color: t.surface, width: 2 } }, hoverinfo: "skip" });
    }
  } else if (m.kind === "hist") {
    layout.xaxis = axis(m.xLabel, { range: m.x });
    layout.hovermode = "closest";
    if (m.dots) {
      const xs: number[] = [], ys: number[] = [];
      for (const b of m.bins) for (let k = 0; k < b.count; k++) { xs.push((b.x0 + b.x1) / 2); ys.push(k + 0.5); }
      const top = Math.max(6, ...m.bins.map((b) => b.count));
      data.push({ x: xs, y: ys, mode: "markers", marker: { size: Math.max(5, Math.min(11, 220 / m.bins.length)), color: alpha(cat(0), 0.85) }, hoverinfo: "skip" });
      layout.yaxis = axis("", { range: [0, top * 1.1], showticklabels: false, showgrid: false });
    } else {
      const bars = (bins: HistModel["bins"], color: number, label: string | undefined, a: number) => ({
        type: "bar", x: bins.map((b) => (b.x0 + b.x1) / 2), y: bins.map((b) => b.count), width: bins.map((b) => (b.x1 - b.x0) * 0.92),
        marker: { color: alpha(cat(color), a) }, hovertemplate: `${label ? `${esc(label)} · ` : ""}%{x:.4~g}: %{y}<extra></extra>`,
      });
      const over = m.overlay;
      data.push(bars(m.bins, m.color ?? 0, m.label, over ? 0.55 : 0.8));
      if (over) {
        data.push(bars(over.bins, over.color, over.label, 0.55));
        layout.barmode = "overlay";
      }
      layout.yaxis = axis("count", { rangemode: "tozero" });
    }
    if (m.band && Number.isFinite(m.band.from) && Number.isFinite(m.band.to)) {
      shapes.push({ type: "rect", x0: Math.min(m.band.from, m.band.to), x1: Math.max(m.band.from, m.band.to), yref: "paper", y0: 0, y1: 1, fillcolor: alpha(t.accent, 0.12), line: { width: 0 }, layer: "below" });
      annotations.push({ x: (m.band.from + m.band.to) / 2, yref: "paper", y: 1, yanchor: "bottom", text: esc(m.band.label), showarrow: false, font: { size: 11, color: t.text2 } });
    }
    m.markers.forEach((mk, i) => {
      if (!Number.isFinite(mk.x)) return;
      shapes.push({ type: "line", x0: mk.x, x1: mk.x, yref: "paper", y0: 0, y1: 1, line: { color: cat(mk.color), width: 2.5 } });
      // Alternate sides and rows so neighbouring labels don't overlap.
      annotations.push({ x: mk.x, yref: "paper", y: 0.98 - Math.floor(i / 2) * 0.09, yanchor: "top", xanchor: i % 2 ? "right" : "left", xshift: i % 2 ? -4 : 4,
        text: esc(`${mk.label} ${fmtNum(mk.x)}`), showarrow: false, font: { size: 11, color: cat(mk.color), family: t.mono } });
    });
    layout.margin = { l: 4, r: 12, t: m.band ? 24 : 10, b: 4 };
  } else if (m.kind === "box") {
    layout.hovermode = "closest";
    layout.xaxis = axis(m.xLabel, { type: "category", categoryorder: "array", categoryarray: m.boxes.map((b) => esc(b.label)) });
    layout.yaxis = axis(m.yLabel);
    for (const b of m.boxes) {
      if (![b.q1, b.median, b.q3, b.lo, b.hi].every(Number.isFinite)) continue;
      const c = cat(b.color ?? 0);
      data.push({
        type: "box", name: esc(b.label), x: [esc(b.label)],
        q1: [b.q1], median: [b.median], q3: [b.q3], lowerfence: [b.lo], upperfence: [b.hi], mean: [b.mean],
        boxmean: true, boxpoints: false, fillcolor: alpha(c, 0.28), line: { color: c, width: 2 }, marker: { color: c },
      });
    }
  } else if (m.kind === "heat" || m.kind === "path") {
    const surface = m.kind === "heat" ? m.z : m.surface;
    if (surface?.length) {
      const rows = surface.length, cols = surface[0].length;
      const xs = Array.from({ length: cols }, (_, k) => m.x[0] + ((m.x[1] - m.x[0]) * k) / Math.max(1, cols - 1));
      const ys = Array.from({ length: rows }, (_, k) => m.y[0] + ((m.y[1] - m.y[0]) * k) / Math.max(1, rows - 1));
      const diverging = m.kind === "heat" && m.diverging;
      const heat: Record<string, unknown> = {
        type: "heatmap", x: xs, y: ys, z: surface.map((r) => r.map(finiteOrNull)), zsmooth: "best",
        colorscale: diverging ? [[0, t.neg], [0.5, t.surface2], [1, t.pos]] : [[0, t.surface2], [1, t.accent]],
        hoverinfo: "skip", showscale: m.kind === "heat",
      };
      if (diverging) heat.zmid = 0;
      if (m.kind === "heat") {
        heat.hoverinfo = undefined;
        heat.hovertemplate = `${esc(m.xLabel || "x")} %{x:.3~g}, ${esc(m.yLabel || "y")} %{y:.3~g} → ${esc(m.zLabel || "z")} %{z:.4~g}<extra></extra>`;
        heat.colorbar = { thickness: 10, outlinewidth: 0, tickfont: { size: 10, color: t.muted, family: t.mono }, title: { text: esc(m.zLabel), side: "right", font: { size: 11, color: t.text2 } } };
      }
      data.push(heat);
    }
    layout.xaxis = axis(m.xLabel, { range: m.x, showgrid: !surface?.length });
    layout.yaxis = axis(m.yLabel, { range: m.y, showgrid: !surface?.length });
    layout.hovermode = "closest";
    if (m.kind === "heat" && m.marker && Number.isFinite(m.marker.x) && Number.isFinite(m.marker.y)) {
      data.push({ x: [m.marker.x], y: [m.marker.y], mode: "markers+text", text: [esc(m.marker.label)], textposition: "top right", textfont: { color: t.text, size: 12 },
        marker: { size: 13, color: alpha(t.surface, 0.9), line: { color: t.text, width: 2.5 } }, hoverinfo: "skip", cliponaxis: false });
    }
    if (m.kind === "path") {
      const pts = m.pts.filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
      data.push({ x: pts.map((p) => p[0]), y: pts.map((p) => p[1]), mode: m.showSteps ? "lines+markers" : "lines", line: { color: t.text, width: 2.2 },
        marker: { size: 5, color: alpha(t.text, 0.6) }, hovertemplate: "(%{x:.3~g}, %{y:.3~g})<extra></extra>" });
      if (m.pts.length) {
        const [sx, sy] = m.pts[0];
        data.push({ x: [sx], y: [sy], mode: "markers", marker: { size: 12, color: t.surface, line: { color: t.text, width: 2.5 } }, hoverinfo: "skip", cliponaxis: false });
      }
      const end = pts[pts.length - 1];
      if (end) data.push({ x: [end[0]], y: [end[1]], mode: "markers", marker: { size: 12, color: t.accent, line: { color: t.surface, width: 2 } }, hoverinfo: "skip" });
    }
  }
  return { data, layout };
}

/** Numbers in labels: up to 3 significant digits, no trailing zeros. */
function fmtNum(v: number): string {
  if (!Number.isFinite(v)) return "—";
  return Math.abs(v) >= 1000 || (Math.abs(v) < 0.001 && v !== 0) ? v.toPrecision(3) : String(+v.toPrecision(3));
}

// ---------- chart handles ----------

const live = new Set<ChartHandle>();
let stopTheme: (() => void) | null = null;

class ChartHandle implements Chart {
  el: HTMLElement;
  private model: ChartModel | null = null;
  private theme: Theme | null = null;
  private width = 0;
  private ro: ResizeObserver;
  private drawn = false;
  private clickWired = false;

  constructor(private height: number, label: string) {
    this.el = document.createElement("div");
    this.el.className = "x-plot";
    this.el.style.height = `${height}px`;
    this.el.setAttribute("role", "img");
    this.el.setAttribute("aria-label", label);
    // Draws once the element is in the page and has a width, and on every width change.
    this.ro = new ResizeObserver((entries) => {
      const w = Math.round(entries[entries.length - 1].contentRect.width);
      if (w > 0 && w !== this.width) { this.width = w; this.render(); }
    });
    this.ro.observe(this.el);
    live.add(this);
    stopTheme ??= watchTheme(() => {
      for (const c of live) {
        if (!c.el.isConnected) continue;
        c.theme = null;
        c.render();
      }
    });
    void load().then(() => this.render());
  }

  draw(m: ChartModel) {
    this.model = m;
    this.render();
  }

  rethemeIfNeeded() {
    this.theme ??= themeOf(this.el);
    return this.theme;
  }

  render() {
    if (!plotly || !this.model || !this.width || !this.el.isConnected) return;
    const { data, layout } = figure(this.model, this.rethemeIfNeeded(), this.width, this.height);
    void plotly.react(this.el, data, layout, { displayModeBar: false, responsive: false, scrollZoom: false, doubleClick: false, showAxisDragHandles: false }).then(() => {
      // Pass Plotly's click on as a DOM event ("x-chart-click", detail { x }), so a scene's rules can use it.
      const div = this.el as HTMLElement & { on?: (ev: string, fn: (d: { points?: { x: unknown }[] }) => void) => void };
      if (this.clickWired || !div.on) return;
      this.clickWired = true;
      div.on("plotly_click", (d) => {
        const x = Number(d.points?.[0]?.x);
        if (Number.isFinite(x)) this.el.dispatchEvent(new CustomEvent("x-chart-click", { detail: { x }, bubbles: true }));
      });
    });
    this.drawn = true;
  }

  dispose() {
    this.ro.disconnect();
    live.delete(this);
    if (this.drawn && plotly) plotly.purge(this.el);
    if (!live.size && stopTheme) { stopTheme(); stopTheme = null; }
  }
}

/** A chart of fixed height that fills its container's width. */
export function chart(height: number, label: string): Chart {
  return new ChartHandle(height, label);
}
