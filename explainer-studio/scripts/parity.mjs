// Parity corpus: runs the TypeScript versions of the logic the Flask server
// ported to Python (shared/*.ts) over many inputs and saves inputs + outputs
// to server/tests/parity/*.json. server/tests/test_parity.py runs the Python
// versions on the same inputs and requires the same outputs.
//
//   node scripts/parity.mjs        (then: npm run test:server)
//
// Inputs: shared/sample.ts, any saved specs in .variety/ (optional), and
// seeded random mutations of them (broken blocks, bad expressions, odd ranges,
// hand-typed copies...), random formula-language expressions, synthetic papers
// for the reference cut, and numbers for JS number formatting. Re-run it after
// changing any of shared/{spec,validate,expr,views,compact,references,examples,
// factcheck,edit,lab}.ts and update the Python port until the tests pass.

import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createServer } from "vite";

const OUT = "server/tests/parity";
const vite = await createServer({ configFile: false, root: process.cwd(), logLevel: "error", server: { middlewareMode: true, hmr: false }, appType: "custom", optimizeDeps: { noDiscovery: true } });
const load = (p) => vite.ssrLoadModule(p);

// Non-finite numbers survive JSON as tagged strings.
const enc = (v) => (v === undefined ? "__undefined" : JSON.parse(JSON.stringify(v, (_, x) => (typeof x === "number" && !Number.isFinite(x) ? `__num:${x}` : x))));
const write = (name, data) => writeFileSync(join(OUT, `${name}.json`), JSON.stringify(data) + "\n");

// mulberry32: the corpus is the same on every run.
let seedState = 12345;
const rand = () => {
  seedState = (seedState + 0x6d2b79f5) >>> 0;
  let t = seedState;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const pick = (a) => a[Math.floor(rand() * a.length)];
const chance = (p) => rand() < p;
const clone = (x) => structuredClone(x);

try {
  const spec = await load("/shared/spec.ts");
  const validate = await load("/shared/validate.ts");
  const expr = await load("/shared/expr.ts");
  const compact = await load("/shared/compact.ts");
  const refs = await load("/shared/references.ts");
  const examples = await load("/shared/examples.ts");
  const fc = await load("/shared/factcheck.ts");
  const lab = await load("/shared/lab.ts");
  const sample = await load("/shared/sample.ts");
  const followup = await load("/shared/followup.ts");
  mkdirSync(OUT, { recursive: true });

  // ---------- base outlines ----------
  const outlineOf = (s) => ({
    title: s.title, subtitle: s.subtitle, level: s.level, layout: s.layout, keyIntuition: s.keyIntuition, phases: s.phases,
    stages: s.stages.map((st) => ({ id: st.id, label: st.label, phase: st.phase, blurb: st.blurb, blocks: st.blocks })), glossary: s.glossary,
  });
  const bases = [{ name: "sample", outline: clone(sample.SAMPLE_OUTLINE), design: clone(sample.SAMPLE_DESIGN) }];
  const walk = (dir) => (existsSync(dir) ? readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? walk(p) : p.endsWith(".json") ? [p] : [];
  }) : []);
  for (const f of walk(".variety").sort()) {
    const j = JSON.parse(readFileSync(f, "utf8"));
    const s = j.spec ?? j;
    if (!s?.stages) continue;
    bases.push({ name: f, outline: outlineOf(s), design: followup.designOf(s).design });
  }

  // ---------- mutations ----------
  const EXPR_FIELDS = ["expr", "trial", "markerX", "values", "steps", "init", "truth"];
  const breakExpr = (e) => pick([`${e} +`, `${e} * zz_unknown`, `(${e}`, `${e} $`, `foo(${e})`, `${e}[5]`, "", `${e} ? 1`]);
  const mutateBlock = (b) => {
    const keys = Object.keys(b).filter((k) => k !== "type");
    const op = Math.floor(rand() * 18);
    switch (op) {
      case 0: if (keys.length) delete b[pick(keys)]; break;
      case 1: if (keys.length) b[pick(keys)] = pick(["x", 5, null, [], {}, true, 1.5]); break;
      case 2: { const k = keys.find((x) => EXPR_FIELDS.includes(x) && typeof b[x] === "string"); if (k) b[k] = breakExpr(b[k]); break; }
      case 3: if (b.type === "slider") { [b.min, b.max] = [b.max, b.min]; b.step = pick([0, -1, 1e9, b.step]); b.value = pick([b.max * 10, -5, b.value]); } break;
      case 4: if (b.type === "slider") { b.max = b.min; b.var = pick(["x", "seed", "2bad", b.var]); } break;
      case 5: b.type = pick(["tip", "rule", "mistake", "hologram", "warning", "text"]); break;
      case 6: if (b.type === "quiz" && Array.isArray(b.questions)) for (const q of b.questions) { q.answer = pick([9, -1, 1.5, q.answer]); if (chance(0.3)) q.choices = q.choices.slice(0, 1); } break;
      case 7: if (b.defs?.length) { b.defs[0].name = pick(["seed", "x", "1a", b.defs[0].name]); b.defs.push({ name: "copy", expr: "0.8731 * 2" }); } break;
      case 8: if (b.type === "calculator") b.rows.push({ latex: "c", expr: `${pick(["0.87", "3.23", "0.3333", "12.57"])} * 1`, note: "", digits: pick([2, 9, -1]) }); break;
      case 9: if (b.type === "table") { b.rows.push(["x", "=zz + 1", "=1/3", "plain"]); b.columns = b.columns.slice(0, pick([0, 1, 3, 9])); b.digits = pick([undefined, 12, 2]); } break;
      case 10: if (b.type === "experiment") b.trial = pick(["1 + 1", "bernoulli(0.5)", "uniform(0, 3)", b.trial]); break;
      case 11: if (b.type === "toggle") b.value = pick([5, 0, 1]); break;
      case 12: b.extraKey = "kept?"; break;
      case 13: if (b.type === "plot") { b.xMin = pick([b.xMax, 5]); b.shade = { series: pick([0, 3]), from: "0", to: pick(["1", "zz"]), label: "a" }; b.sweepVar = pick(["seed", "nope", undefined]); } break;
      case 14: if (b.type === "bars") { b.values = pick([["softmax([1,2,3])"], b.values.concat(["zz"]), []]); b.labels = pick([b.labels, ["one"], []]); } break;
      case 15: if (b.type === "readout") for (const it of b.items) { it.kind = pick(["probability", "percent", undefined]); it.expr = pick([it.expr, "1.7", "50", "-0.2"]); } break;
      case 16: if (b.type === "chart") {
        b.view = pick(["line", "lines", "heatmap", "histogram", "compare", "running", "bars", "pie"]);
        b.x = pick(["", b.x, "nope", "seed", "mode"]); b.by = pick(["", b.by, b.x]);
        b.at = pick([[], [1e9, -5], [1, 2, 3, 4, 5, 6], b.at]);
      } break;
      case 17: if (b.type === "scene") {
        const r = pick(b.rules);
        if (r) { r.target = pick([r.target, "nope", "N", undefined]); r.ms = pick([r.ms, 1, 1e6, undefined]); r.if = pick([r.if, "zz", "1 +", undefined]); }
        const e = pick(b.elements);
        if (e) { e.id = pick([e.id, "", "dup", "dup"]); e.visible = pick([e.visible, "zz > 1", undefined]); e.var = pick([e.var, "nope", "N"]); }
        b.state = pick([b.state, b.state.concat([{ name: "B", label: "clash", value: 0, min: 1, max: 0, step: -1 }]), []]);
      } break;
    }
    return b;
  };
  const mutateOutline = (o) => {
    const n = 1 + Math.floor(rand() * 6);
    for (let i = 0; i < n; i++) {
      const st = pick(o.stages);
      if (!st) break;
      if (st.blocks.length && chance(0.8)) st.blocks[Math.floor(rand() * st.blocks.length)] = mutateBlock(st.blocks[Math.floor(rand() * st.blocks.length)]);
      else if (chance(0.3)) o.stages.push(clone(st));
      else if (chance(0.3)) delete st.label;
      else st.id = pick(["", "Same Id", st.id, "x y z"]);
    }
    if (chance(0.15)) o.title = pick(["", "  ", 5]);
    if (chance(0.15)) o.level = pick(["expert", undefined]);
    if (chance(0.15)) o.phases = pick([[], [{ id: "a" }], undefined]);
    if (chance(0.1)) o.glossary = pick([[{ term: "a" }], "x"]);
    if (chance(0.1)) o.keyIntuition = undefined;
    return o;
  };

  // Hand-built outlines for paths random mutation rarely reaches.
  const S = (id, label, blocks, extra = {}) => ({ id, label, phase: "p", blurb: "b", blocks, ...extra });
  const slider = (v, min, max, value, extra = {}) => ({ type: "slider", var: v, label: `L ${v}`, min, max, step: 0.1, value, unit: "", ...extra });
  const crafted = {
    title: "  Crafted  ", subtitle: "s", level: "practitioner", layout: "pipeline", keyIntuition: "k", phases: [{ id: "p", label: "P" }, { id: "q", label: "Q" }],
    glossary: Array.from({ length: 35 }, (_, i) => ({ term: `t${i}`, definition: "d" })),
    stages: [
      S("Cross Check!", "Cross", [
        slider("T", 0.1, 3, 1), slider("z", 0, 10, 2),
        { type: "calculator", title: "Softmax", defs: [{ name: "p", expr: "softmax([2, 1, 0.5] / T)" }, { name: "c", expr: "0.5" }],
          rows: [{ latex: "p_0", expr: "p[0]", note: "", digits: 3 }, { latex: "copy", expr: "0.63 * 1", note: "", digits: 2 }, { latex: "big", expr: "p[0] * 10", note: "n", digits: 9 }], caption: "" },
        { type: "readout", defs: [{ name: "q", expr: "z / 3" }], items: [{ label: "Q", expr: "q", digits: 2, kind: "number" }, { label: "Copy", expr: "0.6667 * 3 + 0.2", digits: 3 }, { label: "P", expr: "q / 10", digits: 2, kind: "probability" }] },
        { type: "calculator", title: "Clash", defs: [{ name: "q", expr: "z / 3" }], rows: [{ latex: "a", expr: "q * 3", note: "", digits: 2 }, { latex: "b", expr: "0.6667 * 3.2", note: "", digits: 2 }], caption: "" },
        { type: "table", title: "Live", columns: ["a", "b"], rows: [["one", "=z / 3"], ["two", "=0.6667 + 0"], ["three", "=1 +"], ["four", "=2 3"]], caption: "", digits: 9 },
      ]),
      S("auto", "Auto graph", [slider("n", 1, 100, 10), slider("m", 0, 0, 0), { type: "readout", items: [{ label: "Params", expr: "n * n * 4", digits: 0 }, { label: "Flat", expr: "1", digits: 0 }] }]),
      S("toys", "Toys", [
        { type: "experiment", title: "E1", trial: "1 + 1", valueLabel: "v", repeats: [1], caption: "" },
        { type: "experiment", title: "E2", defs: [{ name: "r", expr: "uniform(0, 1)" }], trial: "r < 0.5 ? 0 : 3", outcomes: ["a", "b"], valueLabel: "v", repeats: [100, 1, 1, 50000, 10, 3], truth: "zz", caption: "" },
        { type: "widget", title: "W1", html: "<div>no script</div>" }, { type: "widget", title: "W2", html: "   " }, { type: "widget", title: "W3", html: "<script>x</script>" },
        { type: "attention", tokens: ["a", "b", "c"], weights: [[1], [1, 2, 3, 4]], caption: "" },
        { type: "sampling", context: "c", candidates: [{ token: "", logit: 1 }, { token: "a", logit: 2 }] },
        { type: "embedding", points: [{ label: "a", x: 0, y: 0, group: "g" }, { label: "b", x: 1, y: 1, group: "g" }], caption: "" },
        { type: "architecture", nodes: [{ id: "a", label: "A", detail: "" }, { id: "a", label: "A2", detail: "" }, { id: "", label: "E", detail: "" }, { id: "b", label: "B", detail: "" }], edges: [{ from: "a", to: "b" }, { from: "a", to: "a" }, { from: "a", to: "zz" }] },
        { type: "choice", var: "mode", label: "Mode", options: [{ label: "A", value: 0 }, { label: "B", value: 1 }], value: 7 },
        { type: "toggle", var: "on", label: "On", on: "on", off: "off", value: 3 },
        { type: "presets", label: "P", items: [{ label: "x", set: [{ var: "T", value: 2 }, { var: "seed", value: 1 }, { var: "nope", value: 1 }] }, { label: "y", set: [{ var: "zz", value: 1 }] }] },
        { type: "examples", title: "Ex", items: [{ title: "a", set: [{ var: "T", value: 0.5 }], focus: "f" }, { title: "b", set: [], focus: "f", steps: ["s1"] }, { title: "c", set: [{ var: "q", value: 1 }], focus: "f" }] },
      ]),
      S("traj", "Trajectories", [
        slider("lr", 0.01, 1, 0.1),
        { type: "trajectory", title: "GD", defs: [{ name: "g", expr: "2 * w" }], state: [{ name: "w", init: "3" }], update: [{ name: "w", expr: "w - lr * g" }, { name: "zz", expr: "1" }], steps: "50", mode: "time", series: [{ label: "w", expr: "w" }, { label: "bad", expr: "w +" }], logY: false, caption: "" },
        { type: "trajectory", title: "Path", state: [{ name: "x", init: "1" }, { name: "y", init: "1" }], update: [{ name: "x", expr: "x - lr * x" }, { name: "y", expr: "y - lr * 3 * y" }], steps: "30", mode: "path", series: [], logY: false,
          path: { x: "x", y: "y", xMin: -2, xMax: 2, yMin: -2, yMax: 2, xLabel: "x", yLabel: "y", surface: "x^2 + 3*y^2" }, caption: "" },
        { type: "trajectory", title: "Diverge", state: [{ name: "a", init: "1" }], update: [{ name: "a", expr: "a * 100" }], steps: "lr * 1000", mode: "time", series: [{ label: "a", expr: "a" }], logY: true, caption: "" },
        { type: "trajectory", title: "BadName", state: [{ name: "t", init: "1" }], update: [], steps: "5", mode: "time", series: [], logY: false, caption: "" },
        { type: "samples", title: "S", defs: [{ name: "d", expr: "normal(1, 2, 200)" }], values: "d", xLabel: "x", markers: [{ label: "m", expr: "mean(d)" }, { label: "bad", expr: "zz" }],
          band: { label: "b", from: "mean(d) - 1", to: "mean(d) + 1" }, stats: [{ label: "p", expr: "mean(d)", digits: 9, kind: "probability" }, { label: "s", expr: "std(d)", digits: 2 }], caption: "" },
        { type: "heatmap", title: "H", expr: "1", xMin: 0, xMax: 1, yMin: 0, yMax: 1, xLabel: "x", yLabel: "y", zLabel: "z", diverging: false, marker: { x: "lr", y: "zz", label: "m" }, caption: "" },
        { type: "matrix", title: "M", defs: [{ name: "W", expr: "eye(3)" }], items: [{ op: "", label: "W", expr: "W" }, { op: "×", label: "bad", expr: "W[" }], digits: 9, caption: "" },
        { type: "plot", title: "Sweep", xLabel: "lr", yLabel: "y", xMin: 0, xMax: 1, yMin: 0, yMax: 0, series: [{ label: "y", expr: "lr * 2" }, { label: "never", expr: "log(-1)" }], markerX: "lr", sweepVar: "lr",
          shade: { series: 0, from: "0.2", to: "0.4", label: "s" } },
        { type: "bars", title: "B", labels: ["a", "b", "c"], values: ["softmax([1, 2, 3] / lr)"], normalize: true },
        { type: "conceptmap", title: "Map", nodes: [{ id: "n1", label: "N1", sub: "", desc: "", stage: "Cross Check!" }, { id: "n2", label: "N2", sub: "", desc: "", stage: "gone" }, { id: "n1", label: "dup", sub: "", desc: "", stage: "" }], edges: [{ from: "n1", to: "n2", label: "e" }] },
        { type: "quiz", questions: [{ q: "Which?", choices: ["a", "b", "c", "d", "e", "f"], answer: 5, explain: "e" }, { q: "One choice", choices: ["a"], answer: 0, explain: "" }] },
        { type: "formula", latex: "a", caption: "c", terms: [{ symbol: "a", meaning: "m", var: "lr" }, { symbol: "b", meaning: "m", var: "gone" }] },
      ], { phase: "missing" }),
      S("charts", "Charts", ((c) => [
        slider("B", 1, 100, 10), slider("p", 0, 1, 0.5),
        c({ title: "Line", x: "B", view: "line", series: [{ label: "v", expr: "1 / B" }, { label: "flat", expr: "1" }, { label: "bad", expr: "B +" }] }),
        c({ title: "Lines", x: "B", by: "p", at: [0.2, 2, -1, 0.2], view: "lines", series: [{ label: "v", expr: "p + (1 - p) / B" }] }),
        c({ title: "Lines default", x: "B", by: "p", view: "lines", series: [{ label: "v", expr: "p * B" }] }),
        c({ title: "Heat", x: "B", by: "p", view: "heatmap", series: [{ label: "v", expr: "p + (1 - p) / B" }] }),
        c({ title: "Wrong view", x: "B", view: "histogram", series: [{ label: "v", expr: "B * 2" }] }),
        c({ title: "No axis", view: "line", series: [{ label: "v", expr: "B * 2" }] }),
        c({ title: "Hist", view: "line", defs: [{ name: "d", expr: "normal(0, 1, B)" }], series: [{ label: "m", expr: "mean(d)" }] }),
        c({ title: "Compare", by: "B", at: [], view: "compare", series: [{ label: "m", expr: "mean(normal(0, 1, B))" }] }),
        c({ title: "Compare clamp", by: "B", at: [500, 1, 50], view: "compare", series: [{ label: "m", expr: "mean(normal(0, 1, B))" }] }),
        c({ title: "Typed", view: "running", series: [{ label: "m", expr: "mean(bernoulli(0.366032, B))" }] }),
        c({ title: "Ignores", view: "histogram", series: [{ label: "m", expr: "mean(bernoulli(0.37, 100))" }] }),
        c({ title: "Bars", view: "line", labels: ["a", "b", "c"], series: [{ label: "p", expr: "softmax([1, 2, 3] / B)" }] }),
        c({ title: "Choice axis", x: "mode", by: "B", view: "lines", series: [{ label: "v", expr: "B" }] }),
        c({ title: "Same axes", x: "B", by: "B", view: "lines", series: [{ label: "v", expr: "B" }] }),
        c({ title: "Unknown axis", x: "zz", view: "line", series: [{ label: "v", expr: "zz + 1" }] }),
      ])((o) => ({ type: "chart", title: "", lesson: "l", series: [], x: "", by: "", at: [], view: "line", labels: [], caption: "", ...o }))),
      S("scenes", "Scenes", ((sc) => [
        sc({ title: "Play" }),
        sc({ title: "Loop", rules: [{ when: "click", target: "go", set: [{ var: "N", expr: "1" }, { var: "run", expr: "1" }] },
          { when: "change", target: "run", set: [{ var: "run", expr: "1 - run" }] }] }),
        sc({ title: "Bars", elements: [{ id: "pick", kind: "bars", label: "Pick", labels: ["a", "b", "c"], values: "[1, 10, 100]", highlight: "i" },
          { id: "out", kind: "readout", label: "N", expr: "N", digits: 0, format: "number" }],
          rules: [{ when: "click", target: "pick", set: [{ var: "N", expr: "[1, 10, 100][i]" }] }] }),
        sc({ title: "Messy", state: [{ name: "N", label: "N", value: 500, min: 100, max: 1, step: 0 }, { name: "B", label: "clash", value: 0, min: 0, max: 1, step: 1 },
          { name: "1bad", label: "x", value: 0, min: 0, max: 1, step: 1 }, { name: "N", label: "twice", value: 0, min: 0, max: 1, step: 1 }, { name: "spare", label: "S", value: 0, min: 0, max: 1, step: 1 }],
          elements: [{ id: "s1", kind: "slider", var: "spare" }, { id: "s2", kind: "slider", var: "nope" }, { id: "s1", kind: "button", label: "dup" }, { id: "idle", kind: "button", label: "Idle" },
            { id: "c", kind: "chart", label: "Wrong view", series: [{ label: "m", expr: "mean(normal(0, 1, N))" }], by: "N", at: [5], view: "line" },
            { id: "c2", kind: "chart", label: "Bad", series: [{ label: "m", expr: "zz" }], view: "line" },
            { id: "t", kind: "table", label: "T", columns: ["a", "b"], rows: [["=N", "=log(-1)", "extra"], ["=zz", "x"]], highlight: "zz" },
            { id: "tx", kind: "text", md: "N is {{N}}, bad {{zz}}, dead {{log(-1)}}", visible: "N > 2" },
            { id: "r", kind: "readout", label: "dead", expr: "log(-1)", digits: 2 }, { id: "nr", kind: "readout", label: "no expr" }],
          rules: [{ when: "click", target: "s1", set: [{ var: "N", expr: "1" }] }, { when: "click", target: "c", set: [{ var: "N", expr: "x" }] },
            { when: "change", target: "nope", set: [{ var: "N", expr: "1" }] }, { when: "change", target: "B", set: [{ var: "N", expr: "B" }, { var: "B", expr: "1" }, { var: "N", expr: "i" }] },
            { when: "every", ms: 5, set: [{ var: "N", expr: "N + 1" }] }, { when: "every", set: [{ var: "N", expr: "N" }] }, { when: "every", ms: 100, set: [{ var: "N", expr: "N" }] },
            { when: "start", if: "zz", set: [{ var: "N", expr: "1" }] }, { when: "start", set: [{ var: "N", expr: "log(-1)" }] }, { when: "click", target: "idle", set: [] }] }),
        sc({ title: "Nothing", elements: [{ id: "go", kind: "button", label: "Go" }] }),
      ])((o) => ({
        type: "scene", title: "", lesson: "l", caption: "", layout: "stack",
        state: [{ name: "N", label: "Models N", value: 1, min: 1, max: 100, step: 1 }, { name: "run", label: "Playing", value: 0, min: 0, max: 1, step: 1 }],
        elements: [{ id: "n", kind: "slider", var: "N" }, { id: "go", kind: "button", label: "Play", disabled: "run" },
          { id: "c", kind: "chart", label: "Spread", series: [{ label: "m", expr: "mean(normal(0, 1, N))" }], by: "N", at: [1], view: "compare" },
          { id: "sd", kind: "readout", label: "sd", expr: "1 / sqrt(N)", digits: 3 }, { id: "tx", kind: "text", md: "N = {{N}}", visible: "N > 1" }],
        rules: [{ when: "click", target: "go", set: [{ var: "N", expr: "1" }, { var: "run", expr: "1" }] },
          { when: "every", ms: 50, if: "run", set: [{ var: "N", expr: "N + 1" }] },
          { when: "change", target: "N", if: "N >= 100", set: [{ var: "run", expr: "0" }] }],
        ...o,
      }))),
      S("Cross Check!", "Duplicate id", [{ type: "text", md: "Hi." }]),
      S("", "", [{ type: "text", md: "no label" }]),
      ...Array.from({ length: 22 }, (_, i) => S(`more-${i}`, `More ${i}`, [{ type: "pipeline", steps: [{ label: "s", input: "i", output: "o", detail: "d" }] }, { type: "cards", variant: "tip", title: "", items: [{ title: "", md: "" }, { title: "T", md: "M" }] }])),
    ],
  };
  const outlines = [];
  outlines.push({ name: "crafted", raw: crafted, design: bases[0].design }, { name: "crafted-nodesign", raw: crafted, design: null });
  for (const b of bases) {
    outlines.push({ name: b.name, raw: clone(b.outline), design: b.design });
    for (let k = 0; k < 5; k++) outlines.push({ name: `${b.name}#${k}`, raw: mutateOutline(clone(b.outline)), design: chance(0.2) ? null : b.design });
  }
  outlines.push({ name: "junk-1", raw: "not an object", design: null }, { name: "junk-2", raw: { stages: [{}] }, design: null }, { name: "junk-3", raw: [], design: null });

  // ---------- outline parse, assembly, and everything that reads a spec ----------
  const outlineCases = [];
  const specs = [];
  for (const o of outlines) {
    const raw = JSON.parse(JSON.stringify(o.raw)); // JSON round trip: undefined fields disappear, as from the model
    const parsed = spec.parseOutline(raw);
    const c = { name: o.name, raw, design: o.design, parsed: enc(parsed) };
    if ("outline" in parsed) {
      const s = validate.assembleSpec(parsed.outline, o.design, "Context for the chatbot.", parsed.warnings);
      c.spec = enc(s);
      specs.push({ name: o.name, spec: JSON.parse(JSON.stringify(s)) });
    }
    outlineCases.push(c);
  }
  write("outlines", outlineCases);

  const specCases = [];
  for (const { name, spec: s } of specs) {
    const claims = fc.claimsOf(s);
    // Fixes: real substrings of claim fields, ambiguous or missing ones, and quiz answers.
    const fixes = [];
    for (const cl of claims.slice(0, 30)) {
      if (!chance(0.4) || !cl.fields.length) continue;
      const field = pick(cl.fields);
      let v = field.reduce((x, k) => x?.[k], s.stages[cl.stage].blocks[cl.block]);
      if (typeof v !== "string") v = "";
      const words = v.split(/\s+/).filter((w) => w.length > 3);
      const find = pick([words.length ? pick(words) : "the", "the", "zz-not-there", v.slice(0, 12), ""]);
      fixes.push({ id: cl.id, edits: [{ find, replace: pick(["CHANGED", "$$x$$ & $&", find]) }], ...(cl.question && chance(0.5) ? { answer: pick([0, 1, 2, 7, 1.5]) } : {}) });
    }
    fixes.push({ id: "c999", edits: [] });
    const applied = fc.applyFixes(s, claims, fixes);
    // checkEdit: change one block of one stage.
    const edits = [];
    for (let k = 0; k < 3 && s.stages.length; k++) {
      const si = Math.floor(rand() * s.stages.length);
      const blocks = clone(s.stages[si].blocks);
      if (blocks.length) {
        const bi = Math.floor(rand() * blocks.length);
        const b = blocks[bi];
        const op = Math.floor(rand() * 4);
        if (op === 0 && b.type === "slider") { b.min = b.max + 1; b.value = -1e9; }
        else if (op === 1 && b.defs?.length) b.defs = [];
        else if (op === 2) blocks.splice(bi, 1);
        else if (b.type === "text") b.md = `${b.md} Edited.`;
      }
      const r = validate.checkEdit(s, si, blocks);
      edits.push({ stage: si, blocks, result: r.ok ? { ok: true, notes: r.notes, stageBlocks: r.spec.stages[si].blocks } : r });
    }
    const changed = applied.spec.stages.flatMap((st, i) => (JSON.stringify(st) === JSON.stringify(s.stages[i]) ? [] : [{ i, blocks: st.blocks }]));
    const stepped = examples.applyExampleSteps(s, new Map(examples.exampleBriefs(s).map((b) => [b.key, ["one", "two", "three", "four", "five", "six", "seven"]])));
    specCases.push({
      name,
      claims, pageValues: fc.pageValues(s), exampleBriefs: examples.exampleBriefs(s),
      fixes, applied: { applied: applied.applied, skipped: applied.skipped, changed }, edits,
      stepped: stepped.stages.flatMap((st) => st.blocks.filter((b) => b.type === "examples")),
    });
  }
  write("specs", specCases);

  // ---------- compact format ----------
  const compactCases = [];
  const prompts = await load("/prompts/prompts.ts");
  const liteExample = prompts.LITE_SYSTEM.split("it shows the format, not what to teach):\n")[1].split("\n\n# Fields")[0];
  const texts = [liteExample, "no fields here", "```\ntitle: Fenced\nstages:\n- id: a\n  label: A\n  blocks:\n  - type: bars\n    values: 0.6, 0.3, 0.1\n    labels: a | b | c\n```",
    "Here is the outline:\ntitle: T\nlevel: student\nstages:\n- id: s\n  label: S\n  blocks:\n  - type: experiment\n    repeats: [1, 10, 100]\n    outcomes: [mat, \"rug\", sofa]\n  - type: bars\n    values: [softmax(z, T), max(a, b)]\n- term: misplaced\n  definition: glossary entry",
    "title: Attention: a primer\nstages:\n- id: s\n  label: 2\n  blocks:\n  - type: formula\n    latex: \\frac{a}{b}\n    caption: \"quoted \\\"text\\\"\"\n    terms:\n    - symbol: 'a'\n      meaning: |\n        line one\n\n        line two\n  - type: toggle\n    var: on\n    label: L\n    on: yes\n    off: no\n    value: on\n  - type: slider\n    var: k\n    min: 0x10\n    max: Infinity\n    step: .5\n    value:  12 \n  - type: attention\n    tokens: a | b\n    weights: [[1, 0.2], [0.5, 1]]\n  - type: unknownthing\n    nested:\n      deep: 1\n    list:\n    - a\n    - b: c\n"];
  for (const { outline } of bases.slice(0, 12)) texts.push(compact.toCompact(outline));
  for (const t of texts) {
    try { compactCases.push({ text: t, result: enc(compact.parseCompact(t)) }); }
    catch (e) { compactCases.push({ text: t, error: e.message }); }
  }
  write("compact", compactCases);

  // ---------- Freeform+ briefs ----------
  const labCases = [];
  for (const { outline } of bases.slice(0, 10)) {
    for (let k = 0; k < 3; k++) {
      const o = clone(outline);
      for (const st of o.stages) {
        if (!st.blocks.length) continue;
        const i = Math.floor(rand() * st.blocks.length);
        st.blocks.splice(i, 0, pick([
          { type: "interactive", goal: "g", math: "m", insight: "i" },
          { type: "interactive", goal: "g", math: "m", insight: "i", scenarios: ["a", "b"] },
          { type: "interactive", goal: 5, math: "m" },
          { type: "interactive", goal: "g", math: "m", insight: "i", scenarios: "x" },
          { type: "table", rows: "nope" },
        ]));
      }
      const parsed = lab.parseLabOutline(JSON.parse(JSON.stringify(o)));
      const refsOf = "outline" in parsed ? lab.briefsOf(parsed.outline) : [];
      const bundles = new Map(refsOf.filter(() => chance(0.6)).map((r) => [r.key, [{ type: "text", md: `Built ${r.key}` }]]));
      labCases.push({ raw: o, parsed: enc(parsed), briefs: refsOf, bundles: [...bundles], merged: "outline" in parsed ? lab.mergeBundles(parsed.outline, bundles) : null });
    }
  }
  write("lab", labCases);

  // ---------- references ----------
  const body = (n, word = "The model learns a representation of the input and we evaluate it carefully.") => Array.from({ length: n }, () => word).join("\n");
  const ieee = (n) => Array.from({ length: n }, (_, i) => `[${i + 1}] M. Islam, M. Hossain, and M. A. Rahman, "Some Method for Plant Disease," IEEE Access, vol. 11, pp. ${i}-${i + 9}, 2023.`).join("\n");
  const authorYear = (n) => Array.from({ length: n }, (_, i) => `Josh Achiam, Steven Adler, et al. 202${i % 10}. A technical report on models. arXiv preprint arXiv:2303.0${i}.`).join("\n");
  const papers = [
    [{ heading: "Abstract", text: body(5) }, { heading: "Intro", text: body(5) }, { heading: "Method", text: body(5) }, { heading: "Results", text: body(5) }, { heading: "References", text: ieee(20) }, { heading: "Appendix A", text: body(5) }],
    [{ heading: "Intro", text: body(40) }, { heading: "Results (cont.)", text: `${body(10, "In conclusion, the method is useful for agriculture worldwide.")}\nREFERENCES\n${ieee(20)}` }],
    [{ heading: "Intro", text: body(40) }, { heading: "Methods (cont.)", text: `Noise\nDatabase\nRetrieval\nReferences\nReverse\nForward\n${body(40)}` }, { heading: "Apps (cont.)", text: body(20) }],
    [{ heading: "Beginning", text: body(60) }, { heading: "Beginning (cont.)", text: `${body(5)}\n${authorYear(30)}` }],
    [{ heading: "Related work", text: authorYear(20) }, { heading: "Method", text: body(80) }],
    [{ heading: "Résumé 🧠", text: `${body(50, "Unicode text 🧠 with emoji and accents é, plus more words to be long.")}\n7. References\n${authorYear(25)}` }],
    [{ heading: "1 Introduction", text: body(3) }, { heading: "2 Bibliography", text: ieee(3) }, { heading: "3 More", text: body(2) }, { heading: "4. References", text: ieee(4) }],
  ];
  const lines = ["Disease Detection, IEEE Access, vol. 11, pp. 34721-34733, 2023.", "Josh Achiam, Steven Adler, et al. 2023. Gpt-4 technical report.",
    "[7] S. A. Khan, M. Hussain, and F. R. Baig, An Effective Deep Learning", "The model learns a representation of the input and we evaluate it.", "In Advances in Neural Information Processing Systems, 2017", "Smith, J. 1999"];
  write("references", { papers: papers.map((p) => ({ sections: p, result: refs.cutReferences(p) })), lines: lines.map((l) => ({ line: l, result: refs.isReferenceLine(l) })) });

  // ---------- requested stage counts ----------
  const asks = ["Make 14 stages about attention", "a 12-part explainer", "fourteen sections please", "one stage", "50 denoising steps", "explain in 3 parts",
    "25 chapters", "a twenty-lesson course", "7\u00a0modules", "no number here", "1 part", "100 stages", "Twelve-Part guide"];
  write("stages", asks.map((t) => ({ text: t, result: validate.requestedStages(t) ?? null })));

  // ---------- expressions ----------
  const FUNCS = ["abs", "sqrt", "exp", "log", "ln", "log2", "log10", "sin", "cos", "tan", "tanh", "floor", "ceil", "round", "sign", "sigmoid", "relu", "erf",
    "pow", "clamp", "clip", "normal_pdf", "normal_cdf", "min", "max", "sum", "prod", "mean", "var", "std", "len", "norm", "argmax", "argmin", "cumsum",
    "softmax", "dot", "outer", "linspace", "range", "at", "zeros", "ones", "eye", "matmul", "transpose", "normal", "uniform", "bernoulli", "nope"];
  const ATOMS = ["1", "0", "-1", "2.5", "1e3", ".5", "3.", "1e-7", "1e309", "a", "b", "v", "M", "T", "pi", "e", "x", "seed", "[1, 2, 3]", "[[1, 2], [3, 4]]", "[]", "[1, [2]]"];
  const OPS = ["+", "-", "*", "/", "%", "^", "**", "<", "<=", ">", ">=", "==", "!="];
  const gen = (d) => {
    if (d <= 0 || chance(0.3)) return pick(ATOMS);
    const r = rand();
    if (r < 0.35) return `${gen(d - 1)} ${pick(OPS)} ${gen(d - 1)}`;
    if (r < 0.65) { const n = Math.floor(rand() * 4); return `${pick(FUNCS)}(${Array.from({ length: n }, () => gen(d - 1)).join(", ")})`; }
    if (r < 0.75) return `-${gen(d - 1)}`;
    if (r < 0.85) return `(${gen(d - 1)}) ? ${gen(d - 1)} : ${gen(d - 1)}`;
    if (r < 0.92) return `${pick(["v", "M", "[4, 5, 6]"])}[${gen(d - 1)}]`;
    return `(${gen(d - 1)})`;
  };
  const scopes = [{ a: 2, b: -0.5, v: [1, 2, 3], M: [[1, 2], [3, 4]], T: 0.7, seed: 0 }, { a: 0, b: 1e6, v: [], M: [[0]], T: 0, seed: 7 }, { pi: 3, e: [1, 2], a: -3.25, seed: 2.9 }];
  const exprs = [];
  const fixed = ["1 +", "2 3", "foo(1)", "a $ b", "((1)", "[1, 2", "🧠", "x".repeat(1001), "normal(0, 1, 20)", "mean(normal(a, 1, 1000))", "uniform(0, 1) + uniform(0, 1)",
    "bernoulli(0.3, 10)", "range(0, 1, 0.1)", "linspace(0, 1)", "matmul(v, transpose(v))", "softmax(v / T)", "argmax([1, NaN_, 3])", "1/0", "-1/0", "0/0", "5 % 0", "-5 % 3",
    "(-8) ^ (1/3)", "0 ^ -1", "pow(1, 1e400)", "round(-0.5)", "round(2.5)", "floor(-0.5)", "ceil(-0.5)", "exp(1000)", "log(0)", "sqrt(-1)", "sin(1e400)",
    "max()", "min([])", "mean([])", "var([1])", "at(M, 1, 0)", "at(v, 5)", "v[1.9]", "M[1][1]", "zeros(65, 65)", "eye(3)", "outer(v, v)", "len(M)", "len(5)",
    "cumsum(M)", "dot(v, [1, 2])", "clamp(v, 1, 2)", "normal_cdf(1.96)", "normal_pdf(0, 0, 0)", "erf(-0.3)", "1 == 1 ? 2 : 3", "a > 1 ? v : M", "2^-1", "-2^2",
    "1e3e3", "range(10, 0)", "range(0, 10, -1)", "linspace(0, 1, 1)", "sum(M)", "prod(v)", "norm(M)", "argmin(v)", "transpose(5)", "matmul(M, M)", "matmul(M, v)",
    "matmul(v, M)", "matmul([1, 2, 3], M)", "relu(-v)", "sigmoid(-1000)", "tanh(1e3)", "sign(-0)", "abs(-v)", "log2(8) + log10(1000) + ln(e)"];
  for (let i = 0; i < 500; i++) exprs.push(gen(3));
  const exprCases = [];
  for (const src of [...fixed, ...exprs]) {
    const c = expr.tryCompile(src);
    exprCases.push({
      src,
      compile: c.ok ? { ok: true, vars: [...c.c.vars], random: c.c.random } : { ok: false, error: c.error },
      values: scopes.map((s) => enc(expr.evalValue(src, s))),
      usesRandom: expr.usesRandom(src, [{ name: "a", expr: "normal(0, 1)" }]),
    });
  }
  write("expressions", { scopes, cases: exprCases });

  // ---------- JS number formatting ----------
  const nums = [0, -0, 1, -1, 0.1, 0.5, 1.5, 2.5, -2.5, 0.125, 1e21, 1e-7, 123456789012345680000, 1 / 3, 2 / 3, 0.000001234, 1.005, 1.255, 8.345, -1.235, 99.995, 9.995,
    0.045, 1e-10, 123.456, 5e-324, 1.7976931348623157e308, 3.05, 3.23, 0.87, 0.6914, 1e15, 1e16, 12345.6789, 0.0005, 0.00049999, -0.0004];
  for (let i = 0; i < 300; i++) nums.push((rand() - 0.5) * 10 ** Math.floor(rand() * 30 - 12));
  write("numbers", nums.map((n) => ({
    n, str: String(n),
    fixed: [0, 1, 2, 3, 4, 6].map((d) => n.toFixed(d)),
    precision: [1, 2, 3, 4, 6].map((p) => n.toPrecision(p)),
  })));

  console.log(`Wrote parity corpus to ${OUT}: ${outlineCases.length} outlines, ${specCases.length} specs, ${compactCases.length} compact texts, ${labCases.length} lab outlines, ${exprCases.length} expressions.`);
} finally {
  await vite.close();
}
