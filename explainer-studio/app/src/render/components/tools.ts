// Teaching tools: cards, toggle, presets, experiment, examples, table,
// conceptmap. Designs and behaviour are fixed here; the AI supplies content,
// variable values and expressions only.

import { evalNumber, evalValue, isMat, isNum, type Scope } from "../../../../shared/expr";
import { withDefs } from "../../../../shared/views";
import { chart as plotChart, type LineModel, type Pt } from "../chartlib";
import { fmt, h, md, mdInline, svg } from "../dom";
import { fmtKind, live } from "./charts";
import { cat, type Ctx, type Renderer } from "./types";

// ---------- cards ----------

const CARD_KICKER = {
  definition: "Definition", rule: "Rule", tip: "Tip", mistake: "Common mistake", claim: "Claim", keynumber: "",
} as const;

// Claims and headline figures on a page with no source text were recalled by the AI, not read from a paper.
const MEMORY_NOTE = "From the AI's memory, not a source: check before relying on it.";

export const cards: Renderer<"cards"> = (b, ctx) =>
  h("section.x-cards", { "data-variant": b.variant },
    b.title ? h("h3.x-cards-title", null, b.title) : null,
    h("div.x-card-grid", null, ...b.items.map((it) =>
      h("article.x-card", null,
        b.variant === "keynumber"
          ? h("div.x-card-figure", null, it.value || it.title)
          : h("div.x-card-kicker", null, CARD_KICKER[b.variant]),
        h("h4.x-card-title", null, it.title),
        md(it.md),
        b.variant === "rule" && it.breaksIf ? h("div.x-card-breaks", null, h("strong", null, "Breaks if: "), mdInline(it.breaksIf)) : null,
        b.variant === "claim" && it.evidence ? h("div.x-card-evidence", null, h("strong", null, "Evidence: "), mdInline(it.evidence)) : null,
        ctx.fromMemory && (b.variant === "claim" || b.variant === "keynumber") ? h("div.x-card-memory", null, MEMORY_NOTE) : null))));

// ---------- toggle / presets ----------

export const toggle: Renderer<"toggle"> = (b, ctx) => {
  const sw = h("button.x-switch", { type: "button", role: "switch", "aria-label": b.label });
  const state = h("span.x-switch-state");
  const sync = () => {
    const on = ctx.store.get(b.var) === 1;
    sw.setAttribute("aria-checked", String(on));
    state.textContent = on ? b.on : b.off;
  };
  sw.addEventListener("click", () => ctx.store.set(b.var, ctx.store.get(b.var) === 1 ? 0 : 1));
  ctx.onDispose(ctx.store.watch(sync));
  sync();
  return h("div.x-control.x-toggle", { "data-var": b.var }, h("span.x-label", null, b.label), h("div.x-toggle-row", null, sw, state));
};

/** Set several store variables at once (one notification per variable). */
export function applySet(ctx: Ctx, set: { var: string; value: number }[]) {
  for (const s of set) ctx.store.set(s.var, s.value);
}

export const presets: Renderer<"presets"> = (b, ctx) => {
  const buttons = b.items.map((it) => h("button.x-seg", { type: "button", onclick: () => applySet(ctx, it.set) }, it.label));
  const sync = () => buttons.forEach((btn, i) =>
    btn.setAttribute("aria-pressed", String(b.items[i].set.every((s) => ctx.store.get(s.var) === s.value))));
  ctx.onDispose(ctx.store.watch(sync));
  sync();
  return h("div.x-control.x-presets", { "data-var": b.items.flatMap((i) => i.set.map((s) => s.var)).join(" ") },
    h("div.x-control-head", null, h("span.x-label", null, b.label)), h("div.x-segmented", { role: "group", "aria-label": b.label }, ...buttons));
};

// ---------- experiment runner ----------

const MAX_KEEP = 20_000;

/**
 * The running average of repeated runs, with a ±1 standard-error band, on a log
 * run-count axis (experiment and the chart block's "running" view). At most ~300 points.
 */
export function runningModel(xs: number[], truth?: number): LineModel {
  const n = xs.length;
  const step = Math.max(1, Math.floor(n / 300));
  const pts: Pt[] = [], hi: Pt[] = [], lo: Pt[] = [];
  let s = 0, s2 = 0;
  xs.forEach((v, k) => {
    s += v; s2 += v * v;
    if (k % step && k !== n - 1) return;
    const m = s / (k + 1);
    const se = k > 0 ? Math.sqrt(Math.max(0, (s2 - (k + 1) * m * m) / k) / (k + 1)) : 0;
    pts.push([k + 1, m]); hi.push([k + 1, m + se]); lo.push([k + 1, m - se]);
  });
  const ys = [...hi, ...lo].map((p) => p[1]).concat(truth !== undefined ? [truth] : []).filter(Number.isFinite);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0 || Math.abs(y1) || 1) * 0.1;
  y0 -= pad; y1 += pad;
  return {
    kind: "line", xLabel: "number of runs (log scale)", yLabel: "running average", x: [1, Math.max(10, n)], y: [y0, y1], logX: true,
    series: [{ label: "running average", pts }], band: { hi, lo },
    hlines: truth !== undefined ? [{ y: truth, color: 1 }] : [],
  };
}

/** "average error of B trees", without doubling a label that already says "average". */
export function averageLabel(valueLabel: string): string {
  return /^\s*(the\s+)?(average|mean)\b/i.test(valueLabel) ? valueLabel : `average ${valueLabel}`;
}

export const experiment: Renderer<"experiment"> = (b, ctx) => {
  let results: number[] = [];
  let showTruth = false;
  let runs = 0; // total runs ever, so seeds never repeat after Clear
  const chart = plotChart(260, b.title || "Experiment");
  ctx.onDispose(() => chart.dispose());
  const tally = h("div");
  const stats = h("div");
  const empty = h("p.x-exp-empty", null, "Press Run to start the experiment.");
  const cats = b.outcomes;

  const trialScope = (seed: number): Scope => withDefs(b.defs, { ...ctx.store.scope(), seed });
  function run(n: number) {
    for (let k = 0; k < n; k++) results.push(evalNumber(b.trial, trialScope(++runs)));
    if (results.length > MAX_KEEP) results = results.slice(-MAX_KEEP);
    draw();
  }
  const truthValue = () => evalValue(b.truth ?? "", withDefs(b.defs, ctx.store.scope()));

  function draw() {
    const xs = results.filter(Number.isFinite);
    const n = xs.length;
    empty.hidden = n > 0;
    chart.el.style.display = n > 1 ? "" : "none";
    const truth = b.truth && showTruth ? truthValue() : NaN;

    if (cats) {
      // Share of each outcome so far, plus a running-share line per outcome.
      const counts = cats.map((_, i) => xs.filter((v) => v === i).length);
      const truthList = Array.isArray(truth) ? (truth as number[]) : [];
      tally.replaceChildren(h("div.x-bars", null, ...cats.map((c, i) => {
        const share = n ? counts[i] / n : 0;
        const tv = truthList[i];
        return h("div.x-bar-row", null,
          h("span.x-bar-label", null, c),
          h("div.x-bar-track.x-exp-track", null,
            h("div.x-bar-fill", { style: `width:${share * 100}%;background:${cat(i)}` }),
            Number.isFinite(tv) ? h("i.x-exp-truth", { style: `left:${Math.min(100, tv * 100)}%`, title: `true ${fmt(tv * 100, 1)}%` }) : null),
          h("span.x-bar-val", null, `${counts[i]} · ${(share * 100).toFixed(1)}%`));
      })));
      if (n > 1) {
        const step = Math.max(1, Math.floor(n / 300));
        const running = cats.map((_, i) => {
          const pts: [number, number][] = [];
          let c = 0;
          xs.forEach((v, k) => { if (v === i) c++; if (k % step === 0 || k === n - 1) pts.push([k + 1, c / (k + 1)]); });
          return pts;
        });
        chart.draw({
          kind: "line", xLabel: "number of runs (log scale)", yLabel: "share", x: [1, Math.max(10, n)], y: [0, 1], logX: true,
          series: cats.map((c, i) => ({ label: c, pts: running[i] })),
          hlines: truthList.map((y, i) => ({ y, color: i })),
        });
      }
      stats.replaceChildren(statRow([{ label: "runs", value: String(n) }, { label: "last result", value: n ? cats[xs[n - 1]] ?? "—" : "—" }]));
      return;
    }

    // Numeric outcomes: running mean with a ±1 standard-error band.
    const mean = n ? xs.reduce((a, v) => a + v, 0) / n : NaN;
    const sd = n > 1 ? Math.sqrt(xs.reduce((a, v) => a + (v - mean) ** 2, 0) / (n - 1)) : NaN;
    if (n > 1) chart.draw(runningModel(xs, Number.isFinite(truth as number) ? (truth as number) : undefined));
    tally.replaceChildren(n ? h("div.x-exp-recent", null, h("span.x-muted", null, "Latest: "), ...xs.slice(-8).map((v) => h("span.x-exp-chip", null, fmt(v, 3)))) : "");
    stats.replaceChildren(statRow([
      { label: "runs", value: String(n) },
      { label: averageLabel(b.valueLabel), value: fmt(mean, 4) },
      { label: "spread (std)", value: fmt(sd, 3) },
      ...(showTruth && b.truth ? [{ label: "true value", value: fmtKind(truthValue() as number, 4) }] : []),
    ]));
  }

  // Changing a setting changes the experiment, so start over.
  ctx.onDispose(ctx.store.watch(() => { results = []; draw(); }));
  const buttons = b.repeats.map((r) => h("button.x-btn.x-btn-primary.x-btn-sm", { type: "button", onclick: () => run(r) }, r === 1 ? "▶ Run once" : `Run ×${r.toLocaleString()}`));
  const reveal = b.truth
    ? h("button.x-btn.x-btn-ghost.x-btn-sm", { type: "button", "aria-pressed": "false", onclick: (e: Event) => {
        showTruth = !showTruth;
        (e.currentTarget as HTMLElement).setAttribute("aria-pressed", String(showTruth));
        (e.currentTarget as HTMLElement).textContent = showTruth ? "Hide true value" : "Reveal true value";
        draw();
      } }, "Reveal true value")
    : null;
  draw();
  return h("figure.x-chart.x-experiment", null,
    h("div.x-chart-head", null, h("figcaption.x-chart-title", null, h("span.x-badge", null, "Experiment"), " ", b.title)),
    h("div.x-exp-controls", null, ...buttons, h("button.x-btn.x-btn-ghost.x-btn-sm", { type: "button", onclick: () => { results = []; draw(); } }, "Clear"), reveal),
    empty, chart.el, tally, stats,
    b.caption ? h("p.x-caption", null, b.caption) : null);
};

export function statRow(items: { label: string; value: string }[]) {
  return h("div.x-stat-row", null, ...items.map((s) => h("div.x-stat", null, h("strong", null, s.value), h("span", null, s.label))));
}

// ---------- worked examples (side panel + Try it) ----------

/** Scroll to the first widget in this stage that reads one of these variables, and flash it. */
function focusWidget(from: HTMLElement, names: string[]) {
  const stage = from.closest(".x-blocks") ?? from.closest(".explainer");
  if (!stage) return;
  const blocks = [...stage.querySelectorAll<HTMLElement>(".x-block")];
  const uses = (el: HTMLElement) => (el.dataset.uses ?? "").split(" ").some((v) => names.includes(v));
  // Prefer a visual that reads the variables; the examples/presets blocks only set them.
  const isVisual = (el: HTMLElement) => !el.matches(".x-control") && el.dataset.block !== "examples" && el.dataset.block !== "presets";
  const target = blocks.find((el) => isVisual(el) && uses(el)) ?? blocks.find((el) => (el.dataset.var ?? "").split(" ").some((v) => names.includes(v)));
  if (!target) return;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.remove("x-flash");
  void target.offsetWidth; // restart the animation
  target.classList.add("x-flash");
}

export const examples: Renderer<"examples"> = (b, ctx) => {
  const trigger = h("button.x-btn.x-btn-soft", { type: "button", "aria-haspopup": "dialog" }, `📝 Worked examples (${b.items.length})`);
  const root = h("div.x-examples", null, trigger);
  let panel: HTMLElement | null = null;
  let scrim: HTMLElement | null = null;

  const close = (restoreFocus = true) => {
    panel?.remove(); scrim?.remove();
    panel = scrim = null;
    document.removeEventListener("keydown", onKey);
    if (restoreFocus) trigger.focus();
  };
  const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };

  trigger.addEventListener("click", () => {
    const host = root.closest(".explainer") ?? document.body;
    scrim = h("div.x-ex-scrim", { onclick: () => close() });
    const closeBtn = h("button.x-icon-btn", { type: "button", "aria-label": "Close worked examples", onclick: () => close() }, "✕");
    panel = h("aside.x-ex-panel", { role: "dialog", "aria-modal": "true", "aria-label": b.title || "Worked examples" },
      h("div.x-ex-head", null, h("strong", null, b.title || "Worked examples"), closeBtn),
      h("div.x-ex-list", null, ...b.items.map((it, i) =>
        h("section.x-ex-item", null,
          h("div.x-ex-num", null, `Example ${i + 1}`),
          h("h4", null, it.title),
          it.steps?.length ? h("ol.x-ex-steps", null, ...it.steps.map((s) => h("li", null, mdInline(s)))) : null,
          h("div.x-ex-focus", null, md(it.focus)),
          it.set.length
            ? h("button.x-btn.x-btn-primary.x-btn-sm", { type: "button", onclick: () => {
                close(false);
                applySet(ctx, it.set);
                focusWidget(root, it.set.map((s) => s.var));
              } }, "Try it →")
            : null))));
    host.append(scrim, panel);
    document.addEventListener("keydown", onKey);
    closeBtn.focus();
  });
  ctx.onDispose(() => close(false));
  return root;
};

// ---------- table ----------

function cellValue(src: string, scope: Scope, digits: number): string {
  const v = evalValue(src, scope);
  if (isNum(v)) return fmt(v, digits);
  if (isMat(v)) return `${v.length}×${v[0].length}`;
  return v.slice(0, 5).map((x) => fmt(x, digits)).join(", ") + (v.length > 5 ? ", …" : "");
}

export const table: Renderer<"table"> = (b, ctx) => {
  const digits = b.digits ?? 2;
  const hl = b.highlight;
  const isHl = (r: number, c: number) => !!hl && (hl.row === r || hl.row === -1) && (hl.col === c || hl.col === -1);
  const liveCells: { el: HTMLElement; src: string }[] = [];
  const body = b.rows.map((row, r) =>
    h("tr", null, ...row.map((cell, c) => {
      const td = h(c === 0 ? "th" : "td", { scope: c === 0 ? "row" : null, class: isHl(r, c) ? "is-hl" : "" });
      if (cell.startsWith("=")) {
        td.classList.add("is-live");
        liveCells.push({ el: td, src: cell.slice(1) });
      } else td.append(mdInline(cell));
      return td;
    })));
  if (liveCells.length) live(ctx, () => {
    const scope = ctx.store.scope();
    for (const lc of liveCells) lc.el.textContent = cellValue(lc.src, scope, digits);
  });
  return h("figure.x-table-wrap", null,
    b.title ? h("figcaption.x-chart-title", null, b.title) : null,
    h("div.x-table-scroll", null, h("table.x-table", null,
      h("thead", null, h("tr", null, ...b.columns.map((c, i) => h("th", { scope: "col", class: hl && hl.row === -1 && hl.col === i ? "is-hl" : "" }, c)))),
      h("tbody", null, ...body))),
    liveCells.length ? h("p.x-caption", null, "Values marked ● update with the sliders. ", b.caption) : b.caption ? h("p.x-caption", null, b.caption) : null);
};

// ---------- concept map ----------

export const conceptmap: Renderer<"conceptmap"> = (b, ctx) => {
  const COLS = [95, 320, 545], NH = 50, NW = 160;
  const rows = Math.ceil(b.nodes.length / 3);
  const VH = rows * 125 + 10;
  const pos = new Map(b.nodes.map((n, i) => [n.id, { x: COLS[i % 3], y: 45 + Math.floor(i / 3) * 125 }]));
  const map = svg("svg", { viewBox: `0 0 640 ${VH}`, class: "x-cmap-svg", role: "group", "aria-label": b.title || "Concept map" });
  const info = h("div.x-cmap-info", null, h("span.x-muted", null, "Click an idea to see how it connects."));

  // Clip a line from a box centre to its border.
  const clip = (p: { x: number; y: number }, dx: number, dy: number) => {
    const t = Math.min((NW / 2 + 6) / Math.abs(dx || 1e-9), (NH / 2 + 6) / Math.abs(dy || 1e-9));
    return [p.x + dx * t, p.y + dy * t];
  };
  const defs = svg("defs", {}, ...["x-cm-arrow", "x-cm-arrow-on"].map((id) =>
    svg("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, svg("path", { d: "M0,0 L10,5 L0,10 z" }))));
  map.append(defs);
  const edges = b.edges.map((e) => {
    const A = pos.get(e.from)!, B = pos.get(e.to)!;
    const dx = B.x - A.x, dy = B.y - A.y;
    const [x1, y1] = clip(A, dx, dy), [x2, y2] = clip(B, -dx, -dy);
    const line = svg("line", { class: "x-cmap-edge", x1, y1, x2, y2, "marker-end": "url(#x-cm-arrow)" });
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2, w = e.label.length * 7 + 12;
    const bg = svg("rect", { class: "x-cmap-elbg", x: mx - w / 2, y: my - 10, width: w, height: 20, rx: 4 });
    const txt = svg("text", { class: "x-cmap-elbl", x: mx, y: my + 4, "text-anchor": "middle" }, e.label);
    map.append(line, bg, txt);
    return { e, line, txt };
  });
  const nodeEls = new Map<string, SVGElement>();
  for (const n of b.nodes) {
    const p = pos.get(n.id)!;
    const g = svg("g", { class: "x-cmap-node", tabindex: 0, role: "button", "aria-label": n.label });
    g.append(
      svg("rect", { x: p.x - NW / 2, y: p.y - NH / 2, width: NW, height: NH, rx: 10 }),
      svg("text", { x: p.x, y: p.y - 3, "text-anchor": "middle" }, n.label.length > 22 ? n.label.slice(0, 21) + "…" : n.label),
      svg("text", { class: "s", x: p.x, y: p.y + 14, "text-anchor": "middle" }, n.sub.length > 26 ? n.sub.slice(0, 25) + "…" : n.sub));
    g.addEventListener("click", () => select(n.id));
    g.addEventListener("keydown", (ev) => {
      const k = (ev as KeyboardEvent).key;
      if (k === "Enter" || k === " ") { ev.preventDefault(); select(n.id); }
    });
    map.append(g);
    nodeEls.set(n.id, g);
  }
  const label = (id: string) => b.nodes.find((n) => n.id === id)?.label ?? id;
  function select(id: string) {
    const n = b.nodes.find((x) => x.id === id)!;
    nodeEls.forEach((g, k) => g.classList.toggle("is-on", k === id));
    const links: HTMLElement[] = [];
    for (const { e, line, txt } of edges) {
      const on = e.from === id || e.to === id;
      line.classList.toggle("is-on", on);
      txt.classList.toggle("is-on", on);
      line.setAttribute("marker-end", on ? "url(#x-cm-arrow-on)" : "url(#x-cm-arrow)");
      if (e.from === id) links.push(h("span", null, "→ ", h("b", null, label(e.to)), ` (${e.label})`));
      if (e.to === id) links.push(h("span", null, "← ", h("b", null, label(e.from)), ` (${e.label})`));
    }
    info.replaceChildren(h("h4", null, n.label), h("p", null, n.desc));
    if (links.length) info.append(h("p.x-cmap-links", null, ...links.flatMap((l, i) => (i ? [" · ", l] : [l]))));
    if (n.stage) info.append(h("a", { href: ctx.href(n.stage), "data-stage": n.stage }, "Go to section →"));
  }
  return h("figure.x-cmap", null, b.title ? h("figcaption.x-chart-title", null, b.title) : null, h("div.x-cmap-scroll", null, map), info);
};
