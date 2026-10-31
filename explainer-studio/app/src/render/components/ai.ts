// AI-specific components, reused across every topic. Their interactive logic
// (softmax, sampling, nearest neighbours, layout, training simulation) is
// fixed code; the AI only supplies data.

import { fmt, h, hash, md, svg } from "../dom";
import { chart as plotChart } from "../chartlib";
import { cat, type Renderer } from "./types";

const softmax = (xs: number[], T: number) => {
  const t = Math.max(T, 1e-3);
  const m = Math.max(...xs.map((x) => x / t));
  const e = xs.map((x) => Math.exp(x / t - m));
  const s = e.reduce((a, b) => a + b, 0);
  return e.map((v) => v / s);
};

function rangeControl(label: string, min: number, max: number, step: number, value: number, onInput: (v: number) => void, show = (v: number) => fmt(v, 2)) {
  const out = h("output.x-slider-value", null, show(value));
  const input = h("input.x-range", { type: "range", min, max, step, value, "aria-label": label });
  input.addEventListener("input", () => {
    out.textContent = show(Number(input.value));
    onInput(Number(input.value));
  });
  return h("div.x-control.is-compact", null, h("div.x-control-head", null, h("label", null, label), out), input);
}

// ---------- tokens ----------

export const tokens: Renderer<"tokens"> = (b) => {
  const chips = b.tokens.map((t, i) =>
    h("span.x-token", { style: `--tok:${cat(i)}`, title: `${JSON.stringify(t)} · ${t.length} chars` },
      h("span.x-token-text", null, t.replace(/ /g, "␣")),
      h("span.x-token-id", null, String(hash(t) % 50000))));
  return h("figure.x-tokens", null,
    h("div.x-tokens-src", null, h("span.x-muted", null, "Text: "), b.text),
    h("div.x-token-row", null, ...chips),
    h("figcaption", null, `${b.tokens.length} tokens · ${b.text.length} characters. `, b.caption, h("span.x-muted", null, " (IDs are illustrative.)")));
};

// ---------- attention heatmap ----------

export const attention: Renderer<"attention"> = (b) => {
  const n = b.tokens.length;
  let T = 1, focus = Math.min(n - 1, Math.max(0, b.tokens.length - 3));
  const grid = h("div.x-attn-grid", { style: `grid-template-columns: auto repeat(${n}, minmax(28px, 1fr))` });
  const focusRow = h("div.x-attn-focus");

  function draw() {
    const probs = b.weights.map((row) => softmax(row, T));
    const cells: HTMLElement[] = [h("span")];
    b.tokens.forEach((t) => cells.push(h("span.x-attn-col", null, t)));
    probs.forEach((row, i) => {
      cells.push(h("button.x-attn-rowlabel", { type: "button", "aria-pressed": String(i === focus), onclick: () => { focus = i; draw(); } }, b.tokens[i]));
      row.forEach((p, j) =>
        cells.push(h("span.x-attn-cell", {
          class: i === focus ? "is-focus" : "",
          style: `--a:${p.toFixed(3)};color:${p > 0.55 ? "var(--on-accent)" : "var(--text)"}`,
          title: `${b.tokens[i]} → ${b.tokens[j]}: ${(p * 100).toFixed(1)}%`,
          onclick: () => { focus = i; draw(); },
        }, p >= 0.995 ? "1" : p.toFixed(2).replace(/^0/, ""))));
    });
    grid.replaceChildren(...cells);
    const row = probs[focus];
    focusRow.replaceChildren(
      h("div.x-muted", null, "“", h("strong", null, b.tokens[focus]), "” pays attention to:"),
      h("div.x-attn-bars", null, ...b.tokens.map((t, j) =>
        h("div.x-attn-bar", { style: `--a:${row[j].toFixed(3)}` }, h("span", null, t), h("i"), h("b", null, `${Math.round(row[j] * 100)}%`)))));
  }
  draw();
  return h("figure.x-attn", null,
    rangeControl("Attention temperature (sharpness)", 0.2, 3, 0.05, T, (v) => { T = v; draw(); }),
    h("div.x-attn-wrap", null, grid, focusRow),
    h("figcaption", null, b.caption, " Each row sums to 100% (softmax). Click a row to focus it."));
};

// ---------- next-token sampling lab ----------

export const sampling: Renderer<"sampling"> = (b) => {
  const cands = [...b.candidates].sort((a, c) => c.logit - a.logit);
  let T = 1, K = cands.length, P = 1;
  const list = h("div.x-samp-list");
  const out = h("div.x-samp-out");
  const generated: string[] = [];

  function dist() {
    const probs = softmax(cands.map((c) => c.logit), T);
    const keep = probs.map((_, i) => i < K);
    let cum = 0;
    probs.forEach((p, i) => {
      if (!keep[i]) return;
      if (cum >= P) keep[i] = false;
      cum += p;
    });
    const kept = probs.reduce((a, p, i) => a + (keep[i] ? p : 0), 0) || 1;
    return probs.map((p, i) => ({ p, final: keep[i] ? p / kept : 0, keep: keep[i] }));
  }
  function draw() {
    const d = dist();
    list.replaceChildren(...cands.map((c, i) =>
      h("div.x-samp-row", { class: d[i].keep ? "" : "is-cut" },
        h("code", null, JSON.stringify(c.token)),
        h("div.x-bar-track", null, h("div.x-bar-fill", { style: `width:${(d[i].final * 100).toFixed(1)}%;background:${cat(i)}` })),
        h("span.x-bar-val", null, d[i].keep ? `${(d[i].final * 100).toFixed(1)}%` : "cut"))));
    out.replaceChildren(h("span.x-muted", null, b.context), ...generated.map((t) => h("mark.x-samp-new", null, t)));
  }
  function sample() {
    const d = dist();
    let r = Math.random(), pick = 0;
    for (let i = 0; i < d.length; i++) {
      r -= d[i].final;
      if (r <= 0 && d[i].keep) { pick = i; break; }
    }
    generated.push(cands[pick].token);
    if (generated.length > 6) generated.shift();
    draw();
  }
  draw();
  return h("figure.x-samp", null,
    h("div.x-samp-controls", null,
      rangeControl("Temperature", 0.05, 2.5, 0.05, T, (v) => { T = v; draw(); }),
      rangeControl("Top-k", 1, cands.length, 1, K, (v) => { K = v; draw(); }, (v) => String(v)),
      rangeControl("Top-p", 0.05, 1, 0.05, P, (v) => { P = v; draw(); })),
    list,
    h("div.x-samp-gen", null,
      h("button.x-btn.x-btn-primary", { type: "button", onclick: sample }, "🎲 Sample next token"),
      h("button.x-btn.x-btn-ghost", { type: "button", onclick: () => { generated.length = 0; draw(); } }, "Reset")),
    out,
    h("figcaption", null, "Each sample is drawn independently from the same distribution, so you can see how often each token wins."));
};

// ---------- embedding map ----------

export const embedding: Renderer<"embedding"> = (b) => {
  const groups = [...new Set(b.points.map((p) => p.group))];
  const xs = b.points.map((p) => p.x), ys = b.points.map((p) => p.y);
  const pad = 0.15;
  const [x0, x1] = [Math.min(...xs) - pad, Math.max(...xs) + pad];
  const [y0, y1] = [Math.min(...ys) - pad, Math.max(...ys) + pad];
  const W = 520, H = 340;
  const sx = (x: number) => 20 + ((x - x0) / (x1 - x0 || 1)) * (W - 40);
  const sy = (y: number) => H - 20 - ((y - y0) / (y1 - y0 || 1)) * (H - 40);
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "x-emb-svg", role: "img", "aria-label": "Embedding map" });
  const info = h("div.x-emb-info", null, h("span.x-muted", null, "Click a point to find its nearest neighbours."));
  let sel = -1;

  const dist = (i: number, j: number) => Math.hypot(b.points[i].x - b.points[j].x, b.points[i].y - b.points[j].y);
  function draw() {
    const near = sel < 0 ? [] : b.points.map((_, j) => j).filter((j) => j !== sel).sort((a, c) => dist(sel, a) - dist(sel, c)).slice(0, 3);
    const parts: SVGElement[] = [];
    for (const j of near) parts.push(svg("line", { x1: sx(b.points[sel].x), y1: sy(b.points[sel].y), x2: sx(b.points[j].x), y2: sy(b.points[j].y), class: "x-emb-link" }));
    b.points.forEach((p, i) => {
      const g = svg("g", { class: `x-emb-pt${i === sel ? " is-sel" : ""}${near.includes(i) ? " is-near" : ""}`, tabindex: 0, role: "button", "aria-label": p.label });
      g.append(svg("circle", { cx: sx(p.x), cy: sy(p.y), r: i === sel ? 9 : 7, fill: cat(groups.indexOf(p.group)) }));
      g.append(svg("text", { x: sx(p.x) + 11, y: sy(p.y) + 4 }, p.label));
      const pick = () => { sel = i === sel ? -1 : i; draw(); };
      g.addEventListener("click", pick);
      g.addEventListener("keydown", (e) => { if ((e as KeyboardEvent).key === "Enter" || (e as KeyboardEvent).key === " ") { e.preventDefault(); pick(); } });
      parts.push(g);
    });
    chart.replaceChildren(...parts);
    info.replaceChildren(sel < 0
      ? h("span.x-muted", null, "Click a point to find its nearest neighbours.")
      : h("div", null, h("strong", null, b.points[sel].label), " is closest to: ",
          ...near.flatMap((j, k) => [k ? ", " : "", h("span.x-emb-chip", null, b.points[j].label), h("span.x-muted", null, ` (${dist(sel, j).toFixed(2)})`)])));
  }
  draw();
  return h("figure.x-emb", null, chart,
    h("div.x-legend", null, ...groups.map((g, i) => h("span.x-legend-item", null, h("i", { style: `background:${cat(i)}` }), g))),
    info, b.caption ? h("figcaption", null, b.caption) : null);
};

// ---------- architecture diagram ----------

export const architecture: Renderer<"architecture"> = (b) => {
  // Layer = longest path from a source (cycles are cut by the iteration cap).
  const layer = new Map(b.nodes.map((n) => [n.id, 0]));
  for (let iter = 0; iter < b.nodes.length; iter++)
    for (const e of b.edges) layer.set(e.to, Math.max(layer.get(e.to)!, layer.get(e.from)! + 1));
  const layers: string[][] = [];
  for (const n of b.nodes) (layers[layer.get(n.id)!] ??= []).push(n.id);
  const rows = layers.filter(Boolean);

  const BW = 168, BH = 46, GX = 24, GY = 54;
  const W = Math.max(...rows.map((r) => r.length)) * (BW + GX) - GX + 20;
  const H = rows.length * (BH + GY) - GY + 20;
  const pos = new Map<string, { x: number; y: number }>();
  rows.forEach((r, li) => {
    const rowW = r.length * (BW + GX) - GX;
    r.forEach((id, k) => pos.set(id, { x: (W - rowW) / 2 + k * (BW + GX), y: 10 + li * (BH + GY) }));
  });

  const detail = h("div.x-arch-detail");
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "x-arch-svg", style: `max-width:${W}px`, role: "img", "aria-label": "Architecture diagram" });
  let sel = b.nodes[0].id;
  const wrap = (s: string, max = 22) => (s.length > max ? s.slice(0, max - 1) + "…" : s);

  function draw() {
    const parts: SVGElement[] = [svg("defs", {}, svg("marker", { id: "x-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, svg("path", { d: "M0,0 L10,5 L0,10 z", class: "x-arch-arrowhead" })))];
    for (const e of b.edges) {
      const a = pos.get(e.from)!, c = pos.get(e.to)!;
      const down = c.y > a.y;
      const x1 = a.x + BW / 2, y1 = down ? a.y + BH : a.y, x2 = c.x + BW / 2, y2 = down ? c.y : c.y + BH;
      const my = (y1 + y2) / 2;
      parts.push(svg("path", { d: `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`, class: "x-arch-edge", "marker-end": "url(#x-arrow)" }));
    }
    b.nodes.forEach((n) => {
      const p = pos.get(n.id)!;
      const g = svg("g", { class: `x-arch-node${n.id === sel ? " is-sel" : ""}`, tabindex: 0, role: "button", "aria-label": n.label });
      g.append(svg("rect", { x: p.x, y: p.y, width: BW, height: BH, rx: 10 }));
      g.append(svg("text", { x: p.x + BW / 2, y: p.y + BH / 2 + 5, "text-anchor": "middle" }, wrap(n.label)));
      const pick = () => { sel = n.id; draw(); };
      g.addEventListener("click", pick);
      g.addEventListener("keydown", (e) => { if ((e as KeyboardEvent).key === "Enter" || (e as KeyboardEvent).key === " ") { e.preventDefault(); pick(); } });
      parts.push(g);
    });
    chart.replaceChildren(...parts);
    const node = b.nodes.find((n) => n.id === sel)!;
    detail.replaceChildren(h("strong", null, node.label), md(node.detail));
  }
  draw();
  return h("figure.x-arch", null, h("div.x-arch-scroll", null, chart), detail, h("figcaption", null, "Click a block to see what it does."));
};

// ---------- step-through pipeline ----------

export const pipeline: Renderer<"pipeline"> = (b, ctx) => {
  let cur = 0, timer: number | undefined;
  const chips = b.steps.map((s, i) => h("button.x-pipe-chip", { type: "button", onclick: () => { stop(); go(i); } }, h("span", null, String(i + 1)), s.label));
  const card = h("div.x-pipe-card");
  const play = h("button.x-btn.x-btn-primary", { type: "button" }, "▶ Play");
  const prev = h("button.x-btn.x-btn-ghost", { type: "button", onclick: () => { stop(); go(cur - 1); } }, "← Prev");
  const next = h("button.x-btn.x-btn-ghost", { type: "button", onclick: () => { stop(); go(cur + 1); } }, "Next →");

  function go(i: number) {
    cur = Math.max(0, Math.min(b.steps.length - 1, i));
    const s = b.steps[cur];
    chips.forEach((c, j) => { c.classList.toggle("is-cur", j === cur); c.classList.toggle("is-done", j < cur); });
    card.replaceChildren(
      h("div.x-pipe-title", null, h("span.x-badge", null, `Step ${cur + 1} of ${b.steps.length}`), h("strong", null, s.label)),
      h("div.x-pipe-io", null,
        h("div.x-pipe-box", null, h("div.x-muted", null, "Input"), h("code", null, s.input)),
        h("div.x-pipe-arrow", { "aria-hidden": "true" }, "→"),
        h("div.x-pipe-box.is-out", null, h("div.x-muted", null, "Output"), h("code", null, s.output))),
      md(s.detail));
    prev.disabled = cur === 0;
    next.disabled = cur === b.steps.length - 1;
  }
  function stop() {
    if (timer !== undefined) clearInterval(timer);
    timer = undefined;
    play.textContent = "▶ Play";
  }
  play.addEventListener("click", () => {
    if (timer !== undefined) return stop();
    if (cur === b.steps.length - 1) go(0);
    play.textContent = "⏸ Pause";
    timer = window.setInterval(() => (cur >= b.steps.length - 1 ? stop() : go(cur + 1)), 1800);
  });
  ctx.onDispose(stop);
  go(0);
  return h("figure.x-pipe", null, h("div.x-pipe-chips", null, ...chips), card, h("div.x-pipe-nav", null, prev, play, next));
};

// ---------- training playground ----------

/** Simulated GD on a quadratic bowl: factor r = 1 - 2·lr per epoch; val loss adds an overfitting term. */
export function simulateTraining(lr: number, epochs: number, withVal: boolean) {
  const train: number[] = [], val: number[] = [];
  let w = 2;
  const r = 1 - 2 * lr;
  for (let t = 0; t <= epochs; t++) {
    const noise = 0.015 * Math.sin(t * 1.7) * Math.exp(-t / 80);
    const tl = Math.min(w * w + 0.05 + noise, 50);
    train.push(tl);
    if (withVal) val.push(Math.min(tl + 0.04 + 0.00004 * Math.max(0, t - 40) ** 2, 50));
    w *= r;
  }
  return { train, val, diverged: Math.abs(r) >= 1 };
}

export const training: Renderer<"training"> = (b, ctx) => {
  let lr = 0.05, epochs = 120;
  const chart = plotChart(260, "Loss curve");
  ctx.onDispose(() => chart.dispose());
  const status = h("div.x-train-status", { "aria-live": "polite" });

  function draw() {
    const { train, val, diverged } = simulateTraining(lr, epochs, b.showValidation);
    const yMax = Math.min(Math.max(...train, ...val, 1), 8);
    // Values above the top of the chart are drawn at the top (an exploding loss stays visible).
    const pts = (ys: number[]) => ys.map((v, t) => [t, Math.min(v, yMax)] as [number, number]);
    chart.draw({
      kind: "line", xLabel: "epoch", yLabel: "", x: [0, epochs], y: [0, yMax],
      series: [
        { label: "training loss", pts: pts(train) },
        ...(b.showValidation ? [{ label: "validation loss", pts: pts(val), color: 1, dashed: true }] : []),
      ],
    });

    const bestVal = b.showValidation ? val.indexOf(Math.min(...val)) : -1;
    const msg = diverged ? "💥 Learning rate too high: each step overshoots, so the loss explodes."
      : lr < 0.01 ? "🐢 Learning rate very low: the loss barely moves. Training would take forever."
      : lr > 0.75 ? "〰️ Learning rate high: the loss zig-zags as steps overshoot the minimum."
      : "✅ A healthy learning rate: the loss drops quickly and settles.";
    status.replaceChildren(
      h("div", null, msg),
      h("div.x-muted", null, `Final training loss: ${fmt(train[train.length - 1], 3)}`,
        b.showValidation ? ` · validation: ${fmt(val[val.length - 1], 3)} (best at epoch ${bestVal}${epochs > bestVal + 10 ? ", so later epochs overfit" : ""})` : ""));
  }
  draw();
  return h("figure.x-train", null,
    b.title ? h("figcaption.x-chart-title", null, b.title) : null,
    h("div.x-samp-controls", null,
      rangeControl("Learning rate", 0.001, 1.1, 0.001, lr, (v) => { lr = v; draw(); }, (v) => fmt(v, 3)),
      rangeControl("Epochs", 10, 300, 5, epochs, (v) => { epochs = v; draw(); }, (v) => String(v))),
    chart.el,
    h("div.x-legend", null,
      h("span.x-legend-item", null, h("i", { style: "background:var(--cat-0)" }), "training loss"),
      b.showValidation ? h("span.x-legend-item", null, h("i", { style: "background:var(--cat-1)" }), "validation loss") : null),
    status,
    b.caption ? h("p.x-muted", null, b.caption) : null);
};
