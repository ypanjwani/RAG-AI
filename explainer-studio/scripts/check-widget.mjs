// Code Lab widget check (server/explainer/codelab.py). Reads one widget's HTML fragment on stdin, and optionally number
// checks as a JSON argument ([{set: {controlId: value}, readout: id, expect: number}]). Prints on stdout:
// {"problems": [...], "controls": n, "runtime": bool, "ids": {id: {tag, type?, min?, max?}}, "checks": [...],
//  "facts": {controls, labels, changes}} (ids, checks and facts only when the widget ran).
//   1. Syntax: every inline <script> must compile (catches curly quotes, stray braces).
//   2. Runtime (needs jsdom): load the page (with the shared helper kit, window.XW, loaded first, as in the real
//      frame), collect thrown errors, then move every control and require that at least one of them changes what the
//      widget shows (DOM text/attributes or canvas drawing).
//   3. Layout: two different SVG labels drawn at exactly the same x and y, in the same svg and group, overlap (checked at load
//      and after every control).
//   4. Number checks: set the listed controls and require the readout's text to show the expected number (computed by
//      the server from the section's formula), within 0.5% or the rounding shown.
//   5. Facts for the reviewer (no code): every control with its label, the static text the reader sees, and for each
//      control what changed when it moved.
// Without jsdom it still does the syntax check and says runtime:false.
import vm from "node:vm";
import { readFileSync } from "node:fs";

const html = await new Promise((resolve) => {
  let s = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (c) => (s += c));
  process.stdin.on("end", () => resolve(s));
});

const problems = [];
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map((m) => m[1]);
scripts.forEach((code, i) => {
  try {
    new vm.Script(code, { filename: `script ${i + 1}` });
  } catch (e) {
    problems.push(`JavaScript syntax error in script ${i + 1}: ${e.message}`);
  }
});

const out = (extra = {}) => {
  console.log(JSON.stringify({ problems, controls: 0, runtime: false, ...extra }));
  process.exit(0);
};
if (problems.length) out();

let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = await import("jsdom"));
} catch (e) {
  out({ runtimeError: `jsdom could not be loaded: ${String(e?.message || e).slice(0, 200)}` });
}

const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push(String(e.detail?.message || e.message || e)));
vc.on("error", (...a) => errors.push(a.map(String).join(" ")));

const KIT = readFileSync(new URL("../shared/widget-kit.js", import.meta.url), "utf8");
// Code Lab 2.0's chart route: a Vega-Lite chart drawn by XW.vega, with Vega loaded first as in the real frame.
const USES_VEGA = /\bXW\.vega\s*\(/.test(html);
const VEGA = USES_VEGA
  ? ["../node_modules/vega/build/vega.min.js", "../node_modules/vega-lite/build/vega-lite.min.js", "../shared/vega-interpreter.js"]
      .map((f) => readFileSync(new URL(f, import.meta.url), "utf8"))
  : [];

let drawCalls = 0;
const ctxProxy = () =>
  new Proxy(function () {}, {
    get: (_t, k) => (k === "canvas" ? undefined : k === "measureText" ? () => ({ width: 10 }) : ctxProxy()),
    set: () => true,
    apply: () => { drawCalls++; return ctxProxy(); },
  });

const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(window) {
    window.HTMLCanvasElement.prototype.getContext = function () { return ctxProxy(); };
    if (USES_VEGA) {
      // jsdom lacks structuredClone, which Vega-Lite uses on plain JSON specs.
      window.structuredClone = (v) => window.JSON.parse(window.JSON.stringify(v));
      VEGA.forEach((src) => window.eval(src));
    }
    window.eval(KIT);
    // As in the real frame, whose Content-Security-Policy has no 'unsafe-eval': nothing may turn a string into code. (A
    // chart once passed here and stayed blank in the browser, because Vega compiled its formulas with new Function.)
    // jsdom itself builds inline handlers (onclick="...") with Function, which the real frame allows ('unsafe-inline'):
    // let a call through only when jsdom's own code is the direct caller.
    const refuse = () => { throw new window.EvalError("the frame's security policy blocks eval and new Function: compute without them"); };
    const byJsdom = () => /[\\/]jsdom[\\/]lib[\\/]/.test(new Error().stack.split("\n")[3] || "");  // [1] here, [2] the trap, [3] the caller
    window.eval = refuse;
    window.Function = new Proxy(window.Function, {
      apply: (fn, self, args) => (byJsdom() ? Reflect.apply(fn, self, args) : refuse()),
      construct: (fn, args) => (byJsdom() ? Reflect.construct(fn, args) : refuse()),
    });
    window.addEventListener("error", (e) => errors.push(String(e.message)));
    window.addEventListener("unhandledrejection", (e) => errors.push(String(e.reason)));
  },
});
const { window } = dom;
const { document } = window;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
await wait(USES_VEGA ? 400 : 120);  // a Vega view renders asynchronously

if (errors.length) {
  problems.push(`It throws when it loads: ${errors[0]}`);
  out({ runtime: true });
}

// Two different labels at the same spot (the failure the model can't see): SVG <text> with equal x and y and no transform,
// in the same coordinate space. Labels in different <svg>s (two charts) or under different transformed groups are
// drawn in different places even when their x and y are equal, so they are compared only within their own space.
const spaceOf = (t) => {
  for (let e = t.parentElement; e; e = e.parentElement) {
    if (e.tagName.toLowerCase() === "svg" || e.getAttribute("transform")) return e;
  }
  return null;
};
const overlap = () => {
  const seen = new Map();
  const ids = new Map();
  for (const t of document.querySelectorAll("svg text")) {
    const x = t.getAttribute("x"), y = t.getAttribute("y"), txt = (t.textContent || "").trim();
    if (x == null || y == null || !txt || t.getAttribute("transform")) continue;
    const space = spaceOf(t);
    if (!ids.has(space)) ids.set(space, ids.size);
    const k = `${ids.get(space)}:${x},${y}`;
    if (seen.has(k) && seen.get(k) !== txt) return [seen.get(k), txt];
    seen.set(k, txt);
  }
  return null;
};
let overlapped = overlap();

// A Vega chart that draws nothing: its axes and controls render, but every data mark has no visible size (bars with
// y and y2 on the same field, bars on a log scale, data filtered away). Read from Vega's scenegraph. Vega's own warnings
// for that chart are quoted as hints only: some are harmless, so they never fail a chart alone.
const MARK_NAMES = { rect: "bars (rect marks)", symbol: "points", line: "lines", area: "areas", rule: "rules", text: "text labels", arc: "arcs", trail: "trails" };
const vegaDrawn = (after) => {
  for (const [id, view] of Object.entries(window.XW_VIEWS || {})) {
    let sg;
    try { sg = view.scenegraph().root; } catch { continue; }
    const marks = [];
    const walk = (node) => {
      if (!node || !node.items) return;
      if (node.marktype && node.role === "mark") {
        const visible = node.items.filter((it) => it.bounds && it.bounds.x2 - it.bounds.x1 > 0.5 && it.bounds.y2 - it.bounds.y1 > 0.5).length;
        marks.push({ type: node.marktype, items: node.items.length, visible });
      }
      node.items.forEach(walk);
    };
    walk(sg);
    // A mark drawn far outside the chart (seen in full Vega: {"scale": "x", "value": 300} meant 300 pixels but was read
    // as the data value 300, so a label landed 11,000 px away and the chart looked empty). Top-level marks only, in the
    // view's own coordinates; the slack leaves room for axis titles and a legend.
    let w = 0, h = 0;
    try { w = view.width(); h = view.height(); } catch { /* not sized */ }
    const top = sg?.items?.[0]?.items || [];
    if (w > 0 && h > 0) {
      for (const m of top) {
        const b = m.bounds;
        if (!b || !m.items?.length || !Number.isFinite(b.x1) || b.x2 < b.x1) continue;
        if (b.x1 < -200 || b.x2 > w + 320 || b.y1 < -150 || b.y2 > h + 150) {
          const what = MARK_NAMES[m.marktype] || m.marktype;
          return `The ${what} are drawn far outside the chart (around x ${Math.round(b.x1)} to ${Math.round(b.x2)}, y ${Math.round(b.y1)} to ${Math.round(b.y2)}; the chart is ${w}×${h})${after ? ` after ${after}` : ""}. A position written as {"scale": ..., "value": N} is the data value N on that scale, not N pixels: for a pixel position drop "scale" ({"value": N}), or give a value inside the scale's domain.`;
        }
      }
    }
    const hints = (window.XW_WARNINGS?.[id] || []).slice(0, 3);
    const said = hints.length ? ` (Vega said: ${hints.map((h) => clip(h, 120)).join("; ")})` : "";
    const when = after ? ` after ${after}` : "";
    if (!marks.some((m) => m.items > 0)) return `The chart draws no data${when}: its marks have nothing to show, so only axes appear. Check the data, filters and fields${said}.`;
    if (!marks.some((m) => m.visible > 0)) {
      const kinds = [...new Set(marks.filter((m) => m.items > 0).map((m) => MARK_NAMES[m.type] || m.type))].join(" and ");
      return `The ${kinds} have no visible size${when}, so the chart shows only axes. Bars need a value on y that differs from their baseline: never set y and y2 to the same value, never put bars on a log scale, and in Vega-Lite let it stack bars (y plus color)${said}.`;
    }
  }
  return null;
};

const snap = () => document.body.innerHTML + "|" + drawCalls;

// What a control must change: the drawn data, not the axes. For a Vega chart, the geometry and text of its data marks
// (scenegraph items with role "mark"; axes, grids and legends have their own roles), in pixels: a chart that rescales
// its axis to fit (KL penalty vs beta) redraws the same curve with new tick numbers, and that is caught here.
// For custom code, every SVG shape outside the kit's axes group (g.xw-axes) and outside <text>, plus canvas drawing.
const ROUND = (v) => (typeof v === "number" ? Math.round(v * 10) / 10 : v);
const vegaMarksPrint = () => {
  const out = [];
  for (const view of Object.values(window.XW_VIEWS || {})) {
    let root;
    try { root = view.scenegraph().root; } catch { continue; }
    const walk = (node) => {
      if (!node || !node.items) return;
      if (node.marktype && node.role === "mark") {
        for (const it of node.items) out.push([node.marktype, ROUND(it.x), ROUND(it.y), ROUND(it.x2), ROUND(it.y2), ROUND(it.width), ROUND(it.height),
          typeof it.path === "string" ? it.path.length : "", it.text ?? "", it.fill ?? "", it.stroke ?? "", ROUND(it.opacity), ROUND(it.size)].join(","));
      }
      node.items.forEach(walk);
    };
    walk(root);
  }
  return out.join("|");
};
const SHAPE_ATTRS = ["d", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "points", "transform", "fill", "stroke", "opacity", "fill-opacity", "stroke-dasharray", "style", "class"];
const shapesPrint = () => [...document.querySelectorAll("svg *")]
  .filter((e) => e.tagName.toLowerCase() !== "text" && e.tagName.toLowerCase() !== "tspan" && !e.closest(".xw-axes"))
  .map((e) => e.tagName + ":" + SHAPE_ATTRS.map((a) => e.getAttribute(a) ?? "").join(",")).join("|");
const drawnPrint = () => (USES_VEGA ? vegaMarksPrint() : shapesPrint() + "|" + drawCalls);
// A control's own row (its label and live value): changing it is not an effect. Kit sliders (.xw-ctl) and Vega's
// bound controls (.vega-bind).
const rowOf = (el) => el.closest(".xw-ctl, .vega-bind");
// Buttons that only restore a starting state may change nothing when everything is already at its start.
const RESETISH = /\b(reset|clear|default|restart)\b/i;
const controls = [...document.querySelectorAll("input, select, textarea, button, [role=slider]")]
  .filter((el) => !(el.tagName === "INPUT" && ["hidden", "submit"].includes(el.type)));
if (controls.length === 0) problems.push("It has no controls (sliders, buttons or menus), so the reader can't interact with it.");

const fire = (el, type) => el.dispatchEvent(new window.Event(type, { bubbles: true }));
const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
// An element's text with its parts kept apart ("Label" + "0.5" reads "Label 0.5", not "Label0.5").
const textOf = (el) => {
  const parts = [];
  const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 3) parts.push(c.nodeValue); else if (c.nodeType === 1 && c.tagName !== "SCRIPT" && c.tagName !== "STYLE") walk(c); } };
  walk(el);
  return clean(parts.join(" "));
};

// ---------- what the page has (ids and facts, read after loading, before anything is moved) ----------

const ids = {};
for (const el of document.querySelectorAll("[id]")) {
  const e = { tag: el.tagName.toLowerCase() };
  if (el.tagName === "INPUT") {
    e.type = el.type;
    if (el.min !== "") e.min = Number(el.min);
    if (el.max !== "") e.max = Number(el.max);
  }
  ids[el.id] = e;
}

const labelOf = (el) => {
  const byFor = el.id ? document.querySelector(`label[for="${el.id.replace(/"/g, '\\"')}"]`) : null;
  const vegaName = el.closest(".vega-bind")?.querySelector(".vega-bind-name");  // a Vega control: its name, not its live value
  return clean(vegaName?.textContent || byFor?.textContent || el.getAttribute("aria-label") || el.closest("label")?.textContent || (el.tagName === "BUTTON" ? el.textContent : "") || el.getAttribute("title") || "");
};
const kindOf = (el) => (el.tagName === "INPUT" ? el.type : el.tagName === "SELECT" ? "select" : el.tagName === "BUTTON" ? "button" : el.getAttribute("role") || el.tagName.toLowerCase());
const factControls = controls.slice(0, 12).map((el) => {
  const c = { id: el.id || "", kind: kindOf(el), label: clip(labelOf(el), 80) };
  if (el.tagName === "INPUT" && (el.type === "range" || el.type === "number")) { c.min = Number(el.min || 0); c.max = Number(el.max || 100); c.value = Number(el.value); }
  if (el.tagName === "SELECT") c.options = [...el.options].slice(0, 8).map((o) => clip(clean(o.textContent), 40));
  return c;
});

// Static text the reader sees: titles, axis titles, legends, labels, the "what to notice" line (tick numbers skipped).
const NUMBERISH = /^[\s\-+−0-9.,%∞e×]*$/i;
const labelSeen = new Set();
const factLabels = [];
for (const el of document.querySelectorAll("svg text, h1, h2, h3, h4, h5, h6, label, button, caption, legend, .xw-legend *, .xw-note, .xw-read-label, figcaption, th")) {
  const t = textOf(el);
  if (!t || NUMBERISH.test(t) || labelSeen.has(t)) continue;
  labelSeen.add(t);
  factLabels.push(clip(t, 80));
  if (factLabels.length >= 40) break;
}

// Does each Vega chart draw its data as loaded? (Checked again after every control move below.)
let undrawn = USES_VEGA ? vegaDrawn("") : null;

// ---------- number checks (the server computed each expected value from the section's formula) ----------

const NUM = /[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:e[-+]?\d+)?%?|[-−]?∞/gi;
const numbersIn = (text) => [...(text || "").matchAll(NUM)].map((m) => {
  const s = m[0].replace("−", "-").replace(/,/g, "");
  if (/∞/.test(s)) return { v: s.startsWith("-") ? -Infinity : Infinity, half: 0 };
  const pct = s.endsWith("%");
  const body = pct ? s.slice(0, -1) : s;
  const dec = (body.split("e")[0].split(".")[1] || "").length;
  const v = Number(body);
  return pct ? { v: v / 100, half: 0.5 * 10 ** -dec / 100, raw: v } : { v, half: 0.5 * 10 ** -dec };
});
const close = (n, want) => {
  if (!Number.isFinite(want)) return n.v === want;
  const tol = Math.max(Math.abs(want) * 0.005, n.half, 1e-9);
  return Math.abs(n.v - want) <= tol || (n.raw != null && Math.abs(n.raw - want) <= Math.max(Math.abs(want) * 0.005, n.half * 100, 1e-9));
};
const shown = (v) => (Number.isFinite(v) ? String(Number(v.toPrecision(4))) : v > 0 ? "infinity" : "-infinity");

// The argument: {checks, claims, sanity} (a bare list is the number checks alone, as before).
let argIn = {};
try { argIn = process.argv[2] ? JSON.parse(process.argv[2]) : {}; } catch { argIn = {}; }
const checksIn = Array.isArray(argIn) ? argIn : Array.isArray(argIn?.checks) ? argIn.checks : [];
const claimsIn = !Array.isArray(argIn) && Array.isArray(argIn?.claims) ? argIn.claims.slice(0, 3) : [];
const sanityIn = !Array.isArray(argIn) && Array.isArray(argIn?.sanity) ? argIn.sanity.slice(0, 3) : [];
const checkResults = [];
// A Vega chart's number: set the params, then find the field's value in the chart's data, in rows matching any other
// keys of `set` (e.g. the row where r = 8). Its readout is a data field, not an element.
const vegaCheck = async (chk) => {
  const views = Object.values(window.XW_VIEWS || {});
  if (!views.length) return null;
  const view = views[0];
  const rowKeys = {};
  for (const [k, v] of Object.entries(chk.set || {})) {
    try { view.signal(k, v); } catch { rowKeys[k] = v; }  // not a param: a data field to match
  }
  await view.runAsync();
  const names = Object.keys(view.getState({ data: window.vega.truthy, signals: window.vega.falsy, recurse: true }).data || {});
  const found = [];
  for (const n of names) {
    let rows = [];
    try { rows = view.data(n) || []; } catch { continue; }
    for (const row of rows) {
      if (!(chk.readout in row)) continue;
      if (Object.entries(rowKeys).every(([k, v]) => Number(row[k]) === Number(v))) found.push(Number(row[chk.readout]));
    }
  }
  return found;
};

for (const chk of Array.isArray(checksIn) ? checksIn.slice(0, 6) : []) {
  const out1 = document.getElementById(chk.readout);
  if (!out1 && USES_VEGA) {
    let found = null;
    try { found = await vegaCheck(chk); } catch (e) { errors.push(String(e.message || e)); }
    if (!found || !found.length) { checkResults.push({ readout: chk.readout, skipped: true }); continue; }
    const ok = found.some((v) => close({ v, half: 0 }, chk.expect));
    checkResults.push({ readout: chk.readout, ok, text: clip(found.slice(0, 5).map(shown).join(", "), 120) });
    if (!ok) {
      const where = Object.entries(chk.set || {}).map(([k, v]) => `${k} = ${v}`).join(", ");
      problems.push(`With ${where}, the chart's field "${chk.readout}" is ${shown(found[0])} but the section's formula gives ${shown(chk.expect)}: compute it exactly as the section does.`);
    }
    continue;
  }
  const setting = Object.entries(chk.set || {});
  const els = setting.map(([id]) => document.getElementById(id));
  if (!out1 || els.some((e) => !e)) { checkResults.push({ readout: chk.readout, skipped: true }); continue; }
  try {
    setting.forEach(([, v], i) => { els[i].value = String(v); fire(els[i], "input"); fire(els[i], "change"); });
  } catch (e) {
    errors.push(String(e.message || e));
  }
  await wait(40);
  // A kit readout row: read its values, not its labels (a label like "1 / sqrt(d_k)" holds numbers too).
  const vals = out1.querySelectorAll(".xw-read-val");
  const text = vals.length ? [...vals].map(textOf).join(" | ") : textOf(out1);
  const ok = numbersIn(text).some((n) => close(n, chk.expect));
  checkResults.push({ readout: chk.readout, ok, text: clip(text, 120) });
  if (!ok) {
    const where = setting.map(([id, v]) => `${id} = ${v}`).join(", ");
    problems.push(`With ${where}, "${chk.readout}" shows "${clip(text, 80)}" but the section's formula gives ${shown(chk.expect)}: compute it exactly as the section does.`);
  }
}

// ---------- maths: evidence for Jev's maths review, the plan's claims and sanity rules (Vega charts) ----------
// The chart's own formulas (never its whole spec), its controls, what each axis shows, and the numbers it computes at a
// few control settings. Jev judges these against the section; claims and sanity rules are tested here in code.

const specOfHtml = () => {
  const i = html.indexOf("XW.vega(");
  if (i < 0) return null;
  const start = html.indexOf("{", i), end = html.lastIndexOf(")</script>");
  try { return JSON.parse(html.slice(start, end).replace(/<\\\//g, "</")); } catch { return null; }
};
const isFullSpec = (s) => /\/schema\/vega\//.test(String(s.$schema || "")) || (Array.isArray(s.marks) && !s.mark && !s.layer && !s.hconcat && !s.vconcat && !s.concat);
const sig4 = (v) => (typeof v === "number" ? Number(v.toPrecision(4)) : v);

function formulasOf(spec) {
  const found = [];
  const walk = (v) => {
    if (Array.isArray(v)) return v.forEach(walk);
    if (!v || typeof v !== "object") return;
    if (typeof v.calculate === "string") found.push(`${v.as} = ${v.calculate}`);
    if (v.type === "formula" && typeof v.expr === "string") found.push(`${v.as} = ${v.expr}`);
    if (typeof v.filter === "string") found.push(`keep rows where ${v.filter}`);
    if (typeof v.update === "string" && v.name) found.push(`${v.name} = ${v.update}`);
    // Where the drawing itself reads a control: an encoding's signal or expr, a condition's test (highlights, opacity).
    if (typeof v.signal === "string" && !/^xw_/.test(v.signal)) found.push(`drawn using: ${v.signal}`);
    if (typeof v.test === "string") found.push(`shown differently when: ${v.test}`);
    if (typeof v.expr === "string" && v.type !== "formula") found.push(`drawn using: ${v.expr}`);
    if (v.sequence) found.push(`${v.sequence.as || "data"} runs from ${v.sequence.start} to ${v.sequence.stop} in steps of ${v.sequence.step ?? 1}`);
    if (Array.isArray(v.values) && v.values.length && typeof v.values[0] === "object") found.push(`table rows: ${JSON.stringify(v.values).slice(0, 400)}`);
    Object.values(v).forEach(walk);
  };
  walk(spec);
  return [...new Set(found)].slice(0, 25);
}
const controlsOfSpec = (spec, full) => ((full ? spec.signals : spec.params) || []).filter((p) => p && p.bind && p.name).map((p) => ({
  name: p.name, label: String(p.bind.name || p.name).trim(), kind: p.bind.input, value: p.value, min: p.bind.min, max: p.bind.max, options: p.bind.options,
}));
function axesOfSpec(spec) {
  const found = [];
  const walk = (v, key) => {
    if (Array.isArray(v)) return v.forEach((x) => walk(x, key));
    if (!v || typeof v !== "object") return;
    if (["x", "y", "color", "theta", "size"].includes(key) && (v.field || v.title)) found.push(`${key}: ${v.field ?? ""}${v.title ? ` ("${v.title}")` : ""}`);
    if (key === "axes" || key === "scales") return;
    for (const [k, x] of Object.entries(v)) walk(x, k);
  };
  walk(spec, "");
  return [...new Set(found)].slice(0, 12);
}
// Every dataset row of the first chart, as plain {field: value} objects.
const allRows = () => {
  const view = Object.values(window.XW_VIEWS || {})[0];
  if (!view) return [];
  const names = Object.keys(view.getState({ data: window.vega.truthy, signals: window.vega.falsy, recurse: true }).data || {});
  const rows = [];
  for (const n of names) {
    let data = [];
    try { data = view.data(n) || []; } catch { continue; }
    for (const r of data) rows.push({ dataset: n, row: r });
  }
  return rows;
};
const setSignals = async (set) => {
  const view = Object.values(window.XW_VIEWS || {})[0];
  if (!view) return;
  for (const [k, v] of Object.entries(set)) { try { view.signal(k, v); } catch { /* not a signal */ } }
  await view.runAsync();
};

let math = null;
const claimResults = [];
const sanityResults = [];
const mathSpec = USES_VEGA ? specOfHtml() : null;
if (mathSpec && Object.values(window.XW_VIEWS || {}).length && !errors.length) {
  const full = isFullSpec(mathSpec), mctl = controlsOfSpec(mathSpec, full), formulas = formulasOf(mathSpec);
  const defaults = Object.fromEntries(mctl.map((c) => [c.name, c.value]));
  const computed = new Set(formulas.map((f) => f.split(" = ")[0]).filter((f) => f && !f.includes(" ")));
  const seqFields = new Set(formulas.map((f) => f.match(/^(\S+) runs from /)?.[1]).filter(Boolean));
  const settings = [{}];
  for (const c of mctl.slice(0, 4)) {
    const vals = c.kind === "checkbox" ? (typeof c.value === "boolean" ? [false, true] : [0, 1])
      : Array.isArray(c.options) ? c.options.slice(0, 3)
      : [c.min, (Number(c.min) + Number(c.max)) / 2, c.max].filter((x) => x !== undefined && x !== null && !Number.isNaN(Number(x)));
    for (const v of vals) settings.push({ [c.name]: v });
  }
  const samples = [];
  // A flat line: per computed field, how often it was read in rows where a sequence field changes, and how often it
  // had one value across all of them.
  const flatSeen = new Map(), flatSame = new Map();
  const sanityRows = [];
  for (const set of settings.slice(0, 13)) {
    await setSignals({ ...defaults, ...set });
    const rows = allRows();
    sanityRows.push({ set, rows });
    // Sample rows from datasets that hold computed fields: first, middle and last.
    const byData = new Map();
    for (const { dataset, row } of rows) if (Object.keys(row).some((k) => computed.has(k))) (byData.get(dataset) ?? byData.set(dataset, []).get(dataset)).push(row);
    const picked = [];
    for (const data of byData.values()) {
      for (const i of [...new Set([0, Math.floor(data.length / 2), data.length - 1])]) {
        picked.push(Object.fromEntries(Object.entries(data[i]).filter(([k, v]) => typeof v !== "object" && typeof v !== "function").map(([k, v]) => [k, sig4(v)])));
      }
      // A flat line: a computed field with one value across rows while a sequence field changes.
      const seq = [...seqFields].find((f) => f in data[0]);
      if (seq && data.length > 2 && new Set(data.map((r) => r[seq])).size > 2) {
        for (const f of computed) {
          if (!(f in data[0]) || typeof data[0][f] !== "number") continue;
          flatSeen.set(f, (flatSeen.get(f) || 0) + 1);
          if (new Set(data.map((r) => sig4(r[f]))).size === 1) flatSame.set(f, (flatSame.get(f) || 0) + 1);
        }
      }
    }
    const seen = new Set();
    if (samples.length < 12 && Object.keys(set).length) samples.push({ set, rows: picked.filter((r) => { const k = JSON.stringify(r); return !seen.has(k) && seen.add(k); }).slice(0, 6) });
  }
  const flat = [...flatSeen].filter(([f, n]) => flatSame.get(f) === n && !seqFields.has(f)).map(([f]) => f);

  // Sanity rules: every row holding the field, at the default and at each sampled setting.
  const KINDS = { probability: [0, 1], percent: [0, 100], nonnegative: [0, Infinity] };
  for (const s of sanityIn) {
    if (!s || typeof s.field !== "string" || typeof s.kind !== "string") continue;
    let bad = null;
    for (const { set, rows } of sanityRows) {
      const vals = rows.filter(({ row }) => typeof row[s.field] === "number" && Number.isFinite(row[s.field]));
      if (!vals.length) continue;
      if (KINDS[s.kind]) {
        const [lo, hi] = KINDS[s.kind];
        const off = vals.find(({ row }) => row[s.field] < lo - 1e-9 || row[s.field] > hi + 1e-9);
        if (off) { bad = { set, value: off.row[s.field] }; break; }
      } else if (s.kind === "sums_to_one" || s.kind === "sums_to_100") {
        const want = s.kind === "sums_to_one" ? 1 : 100;
        const groups = new Map();
        for (const { dataset, row } of vals) {
          const k = dataset + "|" + (typeof s.by === "string" && s.by ? String(row[s.by]) : "");
          groups.set(k, (groups.get(k) || 0) + row[s.field]);
        }
        const wrong = [...groups.values()].find((sum) => Math.abs(sum - want) > want * 1e-3);
        if (wrong !== undefined) { bad = { set, value: wrong, sum: true }; break; }
      }
    }
    sanityResults.push({ field: s.field, kind: s.kind, ok: !bad });
    if (bad) {
      const where = Object.entries(bad.set).map(([k, v]) => `${k} = ${v}`).join(", ") || "the default settings";
      const what = { probability: "a probability (between 0 and 1)", percent: "a percentage (between 0 and 100)", nonnegative: "never negative",
        sums_to_one: "shares that add up to 1", sums_to_100: "percentages that add up to 100" }[s.kind] || s.kind;
      problems.push(bad.sum
        ? `"${s.field}" should be ${what}${s.by ? ` within each ${s.by}` : ""}, but at ${where} they add up to ${shown(bad.value)}: compute it exactly as the section does.`
        : `"${s.field}" should be ${what}, but at ${where} it is ${shown(bad.value)}: compute it exactly as the section does.`);
    }
  }

  // Claims: the lesson's "as X changes, Y moves" statement, tested by moving X.
  const readField = (field, at) => {
    const vals = allRows().map(({ row }) => row).filter((r) => typeof r[field] === "number" && Number.isFinite(r[field])
      && Object.entries(at || {}).every(([k, v]) => Number(r[k]) === Number(v))).map((r) => r[field]);
    return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  };
  for (const c of claimsIn) {
    if (!c || typeof c.move !== "string" || typeof c.field !== "string" || !["rises", "falls", "same"].includes(c.expect)) continue;
    const at = c.at && typeof c.at === "object" ? c.at : null;
    await setSignals({ ...defaults, [c.move]: c.from });
    const before = readField(c.field, at);
    await setSignals({ ...defaults, [c.move]: c.to });
    const after = readField(c.field, at);
    if (before === null || after === null) { claimResults.push({ ...c, skipped: true }); continue; }
    const tol = Math.max(Math.abs(before), Math.abs(after), 1e-9) * 1e-6;
    const got = after > before + tol ? "rises" : after < before - tol ? "falls" : "same";
    claimResults.push({ move: c.move, field: c.field, expect: c.expect, got, before: sig4(before), after: sig4(after) });
    if (got !== c.expect) {
      const verb = { rises: "rise", falls: "fall", same: "stay the same" };
      problems.push(`Moving "${c.move}" from ${c.from} to ${c.to} should make "${c.field}" ${verb[c.expect]} (the lesson says so), but it ${got === "same" ? "stayed at" : got === "rises" ? "rose from" : "fell from"} ${shown(before)}${got === "same" ? "" : ` to ${shown(after)}`}. Make the chart compute what the lesson describes.`);
    }
  }
  await setSignals(defaults);
  math = { formulas: formulas.filter((f) => !/^xw_/.test(f)), controls: mctl, axes: axesOfSpec(mathSpec), samples, flat };
}

// ---------- move every control: does anything change, and what ----------

// Each element's own text (its direct text nodes), to see which parts change when a control moves.
const ownTexts = () => {
  const m = new Map();
  for (const el of document.body.querySelectorAll("*")) {
    if (el.tagName === "SCRIPT" || el.tagName === "STYLE") continue;
    let t = "";
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    t = clean(t);
    if (t) m.set(el, t);
  }
  return m;
};
const svgSnap = () => [...document.querySelectorAll("svg")].map((s) => s.innerHTML.length + ":" + s.innerHTML.slice(-200)).join("|");
const anchorOf = (el) => el.closest("[id]")?.id || el.tagName.toLowerCase();

let changed = 0;
const factChanges = [];
const dead = [];  // controls that change nothing drawn: {name, axisOnly}
// Text that changed outside the control's own row: a readout or a label on the data counts; axis tick numbers don't
// (the kit's axes group, or any text inside a Vega chart, whose data labels are counted with its marks).
const textEffect = (el, before, after) => {
  const row = rowOf(el);
  for (const [node, t] of after) {
    if (before.get(node) === t || node === el || el.contains(node) || (row && row.contains(node))) continue;
    if (node.closest(".xw-axes") || (USES_VEGA && node.closest("svg"))) continue;
    return true;
  }
  return false;
};
for (const el of controls) {
  const before = snap();
  const texts = ownTexts(), svgs = svgSnap(), draws = drawCalls;
  const drawnBefore = drawnPrint();
  try {
    if (el.tagName === "INPUT" && (el.type === "range" || el.type === "number")) {
      const min = Number(el.min || 0), max = Number(el.max || 100);
      const cur = Number(el.value);
      el.value = String(cur === max ? min : max);
      fire(el, "input"); fire(el, "change");
    } else if (el.tagName === "INPUT" && (el.type === "checkbox" || el.type === "radio")) {
      el.click();
    } else if (el.tagName === "SELECT") {
      const i = el.selectedIndex;
      el.selectedIndex = (i + 1) % Math.max(el.options.length, 1);
      fire(el, "input"); fire(el, "change");
    } else if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      el.value = "1"; fire(el, "input"); fire(el, "change");
    } else {
      el.click();
    }
  } catch (e) {
    errors.push(String(e.message || e));
  }
  // A button may start something (Play, Step, Resample) that draws a moment later.
  await wait(el.tagName === "BUTTON" ? 250 : USES_VEGA ? 120 : 40);
  const anyChange = snap() !== before;
  if (anyChange) changed++;
  const after = ownTexts();
  if (!el.disabled && drawnPrint() === drawnBefore && !textEffect(el, texts, after) && !(el.tagName === "BUTTON" && RESETISH.test(el.textContent || ""))) {
    dead.push({ el, name: el.id || clip(labelOf(el), 40), axisOnly: anyChange && svgSnap() !== svgs });
  }
  overlapped = overlapped || overlap();
  if (USES_VEGA && !undrawn) undrawn = vegaDrawn(`moving "${el.id || clip(labelOf(el), 40)}" to ${el.tagName === "SELECT" ? `"${clip(clean(el.selectedOptions?.[0]?.textContent || el.value), 30)}"` : el.value}`);
  if (factChanges.length < 12) {
    const parts = new Set(), samples = [];
    for (const [node, t] of after) {
      const was = texts.get(node);
      const row = rowOf(el);
      if (was === t || node === el || el.contains(node) || (row && row.contains(node))) continue;  // the control's own value label
      parts.add(anchorOf(node));
      if (samples.length < 3) samples.push(clip(`${was ?? "(new)"} → ${t}`, 90));
    }
    factChanges.push({ control: el.id || clip(labelOf(el), 40), changes: [...parts].slice(0, 8), samples,
                       redrawn: svgSnap() !== svgs || drawCalls !== draws });
  }
}
// A control can matter only in some states of the others (d_k does nothing while scaling is on; a step limit does
// nothing above the finish step). Every control has now moved, so try each dead one once more, from this new state,
// to each end of its range: if it changes the drawing now, it is a real control. Not for an axis that rescales: that
// hides the effect at every setting (only a degenerate end, such as beta = 0 flattening the curve, would show it).
for (let i = dead.length - 1; i >= 0 && !errors.length; i--) {
  if (dead[i].axisOnly) continue;
  const el = dead[i].el;
  if (!(el.tagName === "INPUT" && (el.type === "range" || el.type === "number")) && el.tagName !== "SELECT") continue;
  const ends = el.tagName === "SELECT" ? [...el.options].map((_, k) => k) : [Number(el.min || 0), Number(el.max || 100)];
  for (const v of ends) {
    const drawnBefore = drawnPrint(), texts = ownTexts();
    try {
      if (el.tagName === "SELECT") el.selectedIndex = v; else el.value = String(v);
      fire(el, "input"); fire(el, "change");
    } catch (e) { errors.push(String(e.message || e)); }
    await wait(USES_VEGA ? 120 : 40);
    if (drawnPrint() !== drawnBefore || textEffect(el, texts, ownTexts())) { dead.splice(i, 1); break; }
  }
}

if (errors.length) problems.push(`It throws when a control is used: ${errors[0]}`);
else if (controls.length && changed === 0) problems.push("Moving the controls changes nothing visible, so the widget isn't interactive.");
else if (!undrawn) {
  // Every control must change what is drawn, not just its own value label or the axis numbers. (A chart that draws
  // nothing is reported once, below: its controls can't show anything either.)
  for (const d of dead.slice(0, 3)) {
    problems.push(d.axisOnly
      ? `Moving "${d.name}" changes only the axis numbers: the axis rescales to fit, so the data look exactly the same and the reader sees no effect. Fix the axis range (a fixed scale domain covering every setting) so the change is visible, or drop the control.`
      : `Moving "${d.name}" changes nothing in the chart (only its own value label). Make the drawn data depend on it, or drop the control.`);
  }
}
// Vega-Lite silently drops an encoding it can't use (e.g. x2: {"expr": ...}), so part of the chart never draws: the
// cosine chart lost both vector arrows this way and its length slider did nothing.
for (const [id, list] of Object.entries(window.XW_WARNINGS || {})) {
  const dropped = list.find((w) => /Dropping .* from channel/i.test(w));
  if (dropped) {
    problems.push(`Part of the chart is ignored, so it never draws (Vega-Lite said: ${clip(dropped, 160)}). An encoding needs a field, datum or value: compute the number in a "calculate" transform and use it as a field, or use {"value": ...} for a fixed pixel position.`);
    break;
  }
}

if (undrawn) problems.push(undrawn);
if (overlapped) problems.push(`Two labels are drawn on top of each other at the same spot ("${overlapped[0]}" and "${overlapped[1]}"): move one so both can be read.`);

out({ controls: controls.length, runtime: true, ids, checks: checkResults, claims: claimResults, sanity: sanityResults, ...(math ? { math } : {}),
      facts: { controls: factControls, labels: factLabels, changes: factChanges } });
