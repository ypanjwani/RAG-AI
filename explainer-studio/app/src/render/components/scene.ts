// Lab mode's scene: one section's interaction, wired by its rules. The rules run in
// shared/scene.ts's SceneSim (the same runner the checks dry-run); this file builds
// the elements from the prebuilt renderers, feeds them events (clicks, slider moves,
// page changes, timers) and keeps their live properties (visible, disabled,
// highlight, {{formula}} text) in step. The scene's state lives in a child store on
// the page's store, so its charts see both the page's controls and the scene's own.

import { evalList, evalNumber, type Scope } from "../../../../shared/expr";
import { chartOf, SceneSim, type SceneBlock } from "../../../../shared/scene";
import type { ExplainerSpec } from "../../../../shared/spec";
import { fmt, h, md } from "../dom";
import { perFrame, Store } from "../engine";
import { chart } from "./chart";
import { barScale, barTrack, readout, slider } from "./charts";
import { cat, type Ctx, type Renderer } from "./types";

type Element = SceneBlock["elements"][number];

const PLACEHOLDER = /\{\{([^{}]*)\}\}/g;
/** A live number for text and table cells. */
const show = (v: number) => (Number.isFinite(v) ? fmt(v, 3) : "—");

export const scene: Renderer<"scene"> = (b, ctx) => {
  // The scene's state as slider-like variables in a child store (charts take axis ranges and titles from it).
  const stateSpec = { stages: [{ blocks: b.state.map((s) => ({ type: "slider", var: s.name, label: s.label, min: s.min, max: s.max, step: s.step, value: s.value, unit: "" })) }] };
  const store = new Store(stateSpec as unknown as ExplainerSpec, ctx.store);
  const sim = new SceneSim(b, () => ctx.store.scope());
  const childCtx = (id: string): Ctx => ({ ...ctx, store, key: `${ctx.key}:${id}` });
  const defs = b.defs ?? [];
  const names = b.state.map((s) => s.name);

  // ---- state flow: sim → store (one notification), store/page → sim (change events) ----
  let syncing = false;
  const publish = () => { syncing = true; store.setMany({ ...sim.vars }); syncing = false; refresh(); };
  let lastPage: Scope = { ...ctx.store.scope() };
  const onStore = () => {
    if (syncing) return;
    const changed: string[] = [];
    for (const n of names) {
      const v = store.get(n);
      if (v !== sim.vars[n]) { sim.vars[n] = v; changed.push(n); } // a scene slider moved
    }
    const page = ctx.store.scope();
    for (const k of Object.keys(page)) if (page[k] !== lastPage[k]) changed.push(k); // a page control moved
    lastPage = { ...page };
    if (!changed.length) { refresh(); return; }
    for (const n of changed) sim.fire("change", n);
    publish();
  };
  ctx.onDispose(store.watch(onStore));

  // ---- elements ----
  const live: (() => void)[] = []; // live property updaters, run once per frame
  const wrap = (e: Element, el: HTMLElement) => {
    const box = h("div.x-scene-el", { "data-id": e.id, "data-kind": e.kind }, el);
    if (e.visible) live.push(() => { box.hidden = !sim.holds(e.visible); });
    return box;
  };
  const build = (e: Element): HTMLElement => {
    switch (e.kind) {
      case "slider": {
        const s = b.state.find((x) => x.name === e.var)!;
        return slider({ type: "slider", var: s.name, label: e.label || s.label, min: s.min, max: s.max, step: s.step, value: s.value, unit: "" }, childCtx(e.id));
      }
      case "button": {
        const btn = h("button.x-btn.x-btn-primary.x-btn-sm", { type: "button", onclick: () => { sim.fire("click", e.id); publish(); } }, e.label || "Go");
        if (e.disabled) live.push(() => { btn.disabled = sim.holds(e.disabled); });
        return h("div.x-scene-button", null, btn);
      }
      case "chart": {
        const el = chart(chartOf(e, defs), childCtx(e.id));
        el.addEventListener("x-chart-click", (ev) => { sim.fire("click", e.id, { x: (ev as CustomEvent).detail.x }); publish(); });
        return el;
      }
      case "readout":
        return readout({ type: "readout", defs, items: [{ label: e.label ?? "", expr: e.expr!, digits: e.digits ?? 2, kind: e.format }] }, childCtx(e.id));
      case "bars": {
        const list = h("div.x-bars");
        const draw = () => {
          const sc = sim.scope();
          const vals = evalList(e.values!, sc).slice(0, 24).map((v) => (Number.isFinite(v) ? v : 0));
          const { maxAbs, signed } = barScale(vals);
          const hi = e.highlight ? evalNumber(e.highlight, sc) : NaN;
          list.replaceChildren(...vals.map((v, i) => h("button.x-bar-row.x-scene-bar", { type: "button", class: i === hi ? "is-hi" : "", onclick: () => { sim.fire("click", e.id, { i }); publish(); } },
            h("span.x-bar-label", null, e.labels?.[i] ?? `#${i + 1}`),
            barTrack(v, maxAbs, signed, cat(i)),
            h("span.x-bar-val", null, fmt(v, 3)))));
        };
        live.push(draw);
        return h("figure.x-chart", null, e.label ? h("figcaption.x-chart-title", null, e.label) : null, list);
      }
      case "table": {
        const body = h("tbody");
        const draw = () => {
          const sc = sim.scope();
          const hi = e.highlight ? evalNumber(e.highlight, sc) : NaN;
          body.replaceChildren(...(e.rows ?? []).map((r, row) => h("tr", { class: row === hi ? "is-hi" : "", onclick: () => { sim.fire("click", e.id, { row }); publish(); } },
            ...r.map((c) => h("td", null, c.startsWith("=") ? show(evalNumber(c.slice(1), sc)) : c)))));
        };
        live.push(draw);
        return h("figure.x-chart.x-table-wrap", null, e.label ? h("figcaption.x-chart-title", null, e.label) : null,
          h("table.x-table.x-scene-table", null, h("thead", null, h("tr", null, ...(e.columns ?? []).map((c) => h("th", null, c)))), body));
      }
      case "text": {
        const box = h("div.x-scene-text");
        live.push(() => {
          const sc = sim.scope();
          box.replaceChildren(md(e.md!.replace(PLACEHOLDER, (_, f: string) => show(evalNumber(f, sc))), "md x-text"));
        });
        return box;
      }
    }
  };
  const els = b.elements.map((e) => wrap(e, build(e)));
  const refresh = perFrame(() => { for (const fn of live) fn(); });

  // ---- timers and start ----
  b.rules.forEach((r, i) => {
    if (r.when !== "every") return;
    const t = window.setInterval(() => {
      if (!sim.holds(r.if)) return; // idle timers cost nothing
      sim.tick(i);
      publish();
    }, Math.max(30, r.ms ?? 500));
    ctx.onDispose(() => window.clearInterval(t));
  });
  sim.fire("start");
  publish();

  return h("figure.x-scene", { class: b.layout === "grid" ? "is-grid" : "" },
    b.title ? h("figcaption.x-chart-title", null, b.title) : null,
    h("div.x-scene-body", null, ...els),
    b.caption ? h("p.x-caption", null, b.caption) : null);
};

