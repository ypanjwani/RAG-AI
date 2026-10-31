// Playground mode: the runtime copied from the ai-playground experiment (shared/playground), its validator and prompt.
import { createRequire } from "node:module";
import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";
import { PLAYGROUND_SYSTEM } from "../prompts/prompts";
// The runtime exactly as the frame gets it (same files and order).
import { PLAYGROUND_SOURCES as RUNTIME } from "../app/src/render/components/playground-src";

// The validator and helpers as the server's checker loads them (CommonJS; they require each other).
const require = createRequire(import.meta.url);
const { validate } = require("../shared/playground/validator.cjs");
const helpers = require("../shared/playground/helpers.cjs");

// The ai-playground experiment's hand-written examples.
const files = import.meta.glob("./fixtures/playground/*.json", { eager: true, import: "default" }) as Record<string, unknown>;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const specs: [string, any][] = Object.entries(files).map(([path, spec]) => [path.split("/").pop() ?? path, spec]);

/** A frame like widget.ts builds: the runtime, then the widget's html (the server's playground_html shape). */
function frame(spec: unknown) {
  const dom = new JSDOM(`<!doctype html><html><body><div id="pg"></div></body></html>`, { runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window as unknown as { eval: (s: string) => unknown; XW: { playground: (s: unknown) => unknown }; document: Document };
  w.eval("window.XW = {};");  // the widget kit sets XW first in the real frame
  for (const src of RUNTIME) w.eval(src);
  const errors: string[] = [];
  (dom.window as unknown as { console: Console }).console.warn = (...a: unknown[]) => { errors.push(a.join(" ")); };
  w.XW.playground(spec);
  return { doc: w.document, errors, dom };
}

describe("playground runtime", () => {
  it("has at least one example spec", () => expect(specs.length).toBeGreaterThan(5));

  it.each(specs)("validator passes %s and reports facts for the reviewer", (_, spec) => {
    const r = validate(spec);
    expect(r.errors).toEqual([]);
    expect(r.ok).toBe(true);
    expect(r.facts.controls.length).toBeGreaterThan(0);
    expect(r.facts.changes.some((c: { redrawn: boolean }) => c.redrawn)).toBe(true);
  });

  it("rejects a control that changes nothing anyone sees, in words for the retry", () => {
    const spec = {
      title: "t", state: { a: 1, b: 2 },
      components: [
        { id: "s", type: "slider", label: "A", key: "a", min: 0, max: 5 },
        { id: "r", type: "readout", label: "B", value: "return state.b;" },
      ],
    };
    const r = validate(spec);
    expect(r.ok).toBe(false);
    expect(r.errors.join(" ")).toMatch(/no chart or readout depends on that/);
    expect(r.facts).toBeUndefined();
  });

  it("rejects a chart that never draws anything, but not one that fills in after a control", () => {
    const withDraw = (draw: string) => ({
      title: "t", state: { a: 0 },
      components: [
        { id: "s", type: "slider", label: "A", key: "a", min: 0, max: 5 },
        { id: "r", type: "readout", label: "A", value: "return state.a;" },
        { id: "c", type: "chart", title: "C", x: [0, 5], y: [0, 5], draw },
      ],
    });
    for (const draw of ["return [];", "return [line([[state.a, 1]])];", "return [points([]), text(1, 1, ' ')];"]) {
      const r = validate(withDraw(draw));
      expect(r.ok).toBe(false);
      expect(r.errors.join(" ")).toMatch(/c: the chart stays empty/);
    }
    // empty at a = 0, drawn once the slider moves
    expect(validate(withDraw("return state.a > 0 ? [points([[state.a, 1]])] : [];")).errors).toEqual([]);
  });

  it.each(specs)("draws %s in a frame without errors", (_, spec) => {
    const { doc, errors } = frame(spec);
    expect(errors).toEqual([]);
    expect(doc.querySelector(".pg")).not.toBeNull();
    expect(doc.querySelectorAll(".pg-chart svg, .pg-readout").length).toBeGreaterThan(0);
  });

  it("redraws when a slider moves", () => {
    const spec = specs.find(([f]) => f === "gini-impurity.json")![1];
    const { doc, dom } = frame(spec);
    const slider = doc.querySelector<HTMLInputElement>('.pg-slider input[type="range"]')!;
    const before = doc.querySelector(".pg-charts")!.innerHTML;
    slider.value = "8";
    slider.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    expect(doc.querySelector(".pg-charts")!.innerHTML).not.toBe(before);
  });
});

// A diagram made only of shapes and links: a slider moves one node, a button-free chart drag moves another.
const DIAGRAM = {
  title: "Diagram",
  plan: { insight: "i", mechanism: "m", user_changes: "u", reacts: "r" },
  state: { w: 3, ax: 2, ay: 2 },
  components: [
    { id: "s1", type: "slider", label: "Width", key: "w", min: 1, max: 5, step: 1 },
    {
      id: "d1", type: "chart", title: "Flow", x: [0, 10], y: [0, 6],
      onDrag: "return { ax: pos.x, ay: pos.y };",
      draw: [
        "return [",
        "  node('a', state.ax, state.ay, { label: 'In', color: 'blue', draggable: true }),",
        "  box(4, 2, 4 + state.w, 4, { id: 'b', label: 'Layer' }),",
        "  diamond(8, 1, 9.5, 3, { id: 'c', label: 'ok?', color: 'orange' }),",
        "  circle(8, 4, 9.5, 5.5, { id: 'e', dashed: true }),",
        "  link('a', 'b'), link('b', 'c', { label: 'go', curve: 0.3 }), link('c', 'e', { directed: false }), link('e', 'e'),",
        "];",
      ].join("\n"),
    },
  ],
};

describe("playground diagram marks (shapes and links)", () => {
  it("passes the validator, including the drag probe on a node", () => {
    const r = validate(DIAGRAM);
    expect(r.errors).toEqual([]);
    expect(r.ok).toBe(true);
    expect(r.facts.labels).toEqual(expect.arrayContaining(["In", "Layer", "ok?", "go"]));
  });

  it("draws each shape and every link, with no axes", () => {
    const { doc, errors } = frame(DIAGRAM);
    expect(errors).toEqual([]);
    const svg = doc.querySelector(".pg-chart svg")!;
    expect(svg.querySelectorAll("ellipse.st-node").length).toBe(2);   // node a, circle e
    expect(svg.querySelectorAll("rect.st-node").length).toBe(1);      // box
    expect(svg.querySelectorAll("polygon.st-node").length).toBe(1);   // diamond
    expect(svg.querySelectorAll("path.st-link").length).toBe(4);      // incl. the self-loop
    expect(svg.querySelectorAll("path.st-link[marker-end]").length).toBe(3);  // the plain link has no arrowhead
    expect(svg.querySelector(".ax-line")).toBeNull();
  });

  it("starts a link on the outline, not the centre", () => {
    const { doc } = frame(DIAGRAM);
    const link = doc.querySelectorAll("path.st-link")[0]!;                    // a -> b
    const [x1] = link.getAttribute("d")!.match(/-?[\d.]+/g)!.map(Number);
    const node = doc.querySelector("ellipse[data-drag]")!;                    // node a (draggable shapes are drawn on top)
    const cx = Number(node.getAttribute("cx")), rx = Number(node.getAttribute("rx"));
    expect(Math.abs(x1 - cx)).toBeGreaterThan(rx * 0.9);                      // about rx away from the centre
    expect(Math.abs(x1 - cx)).toBeLessThan(rx * 1.1);
  });

  it("moves a link with its shape when the slider changes the box", () => {
    const { doc, dom } = frame(DIAGRAM);
    const before = doc.querySelectorAll("path.st-link")[1]!.getAttribute("d");  // b -> c
    const slider = doc.querySelector<HTMLInputElement>('.pg-slider input[type="range"]')!;
    slider.value = "5";
    slider.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    expect(doc.querySelectorAll("path.st-link")[1]!.getAttribute("d")).not.toBe(before);
  });

  it.each([
    ["a link to an id that doesn't exist", "return [node('a', 1, 1), link('a', 'zzz')];", /no shape in this draw has that id/],
    ["two shapes with one id", "return [node('a', 1, 1), box(2, 2, 3, 3, { id: 'a' })];", /repeats the id/],
    ["a shape with no position", "return [{ type: 'shape', shape: 'box', id: 'a' }];", /needs a position/],
    ["a size in the wrong unit", "return [node('a', 1, 1, { r: 0.5 })];", /size in pixels/],
    ["a curve out of range", "return [node('a', 1, 1), node('b', 2, 2), link('a', 'b', { curve: 3 })];", /curve must be/],
  ])("rejects %s", (_, draw, message) => {
    const spec = {
      ...DIAGRAM,
      components: [DIAGRAM.components[0], { ...DIAGRAM.components[1], draw, onDrag: undefined, onClick: "return { w: 2 };" }],
    };
    const r = validate(spec);
    expect(r.ok).toBe(false);
    expect(r.errors.join(" ")).toMatch(message);
  });
});

describe("playground prompt", () => {
  it("lists exactly the runtime's helpers", () => {
    const line = PLAYGROUND_SYSTEM.split("\n").find((l) => l.startsWith("seed, rand,"));
    expect(line?.split(", ")).toEqual(Object.keys(helpers));
  });

  it("stays topic-neutral and has no template leftovers", () => {
    expect(PLAYGROUND_SYSTEM).not.toContain("${");
    expect(PLAYGROUND_SYSTEM).toContain('{"error": "no_playground"}');
  });
});

describe("playground views beyond plotted charts (Oct 30)", () => {
  it("prompt names all three view families and its diagram skeleton passes the checker", () => {
    expect(PLAYGROUND_SYSTEM).toContain("## Your views");
    for (const w of ["mlpGraph", "sequence with links", "matrix", "node, box, circle, diamond and link"]) expect(PLAYGROUND_SYSTEM).toContain(w);
    const line = PLAYGROUND_SYSTEM.split("\n").find((l) => l.includes('"id": "c2"'));
    const chart = JSON.parse(line!.trim());
    const spec = {
      title: "t", state: { a: 2 },
      components: [{ id: "s1", type: "slider", label: "a", key: "a", min: 0, max: 5, step: 0.5 }, chart],
    };
    const r = validate(spec);
    expect(r.errors).toEqual([]);
    expect(r.ok).toBe(true);
  });

  it("gives the reviewer the labels inside graph, sequence and tree marks", () => {
    const spec = {
      title: "t", state: { k: 1 },
      components: [
        { id: "s1", type: "slider", label: "k", key: "k", min: 0, max: 2, step: 1 },
        {
          id: "c1", type: "chart", title: "Net",
          draw: "return [graph({ nodes: [{ label: 'input', layer: 0 }, { label: 'hidden', layer: 1 }], edges: [[0, 1, state.k]] }), sequence(['the', 'cat'], { box: [0, 0.8, 1, 1], highlight: [state.k % 2] }), tree({ label: 'root', children: [{ label: 'leaf', edge: 'yes' }] }, { box: [0, 0, 0.3, 0.3] })];",
        },
      ],
    };
    const r = validate(spec);
    expect(r.errors).toEqual([]);
    expect(r.facts.labels).toEqual(expect.arrayContaining(["input", "hidden", "the", "cat", "root", "leaf", "yes"]));
  });

  it("thins bar labels when many bars would make them run together", () => {
    const n = 30;
    const labelled = (count: number) => ({
      title: "t", state: { a: 1 },
      components: [
        { id: "s1", type: "slider", label: "a", key: "a", min: 0, max: 2, step: 1 },
        { id: "c1", type: "chart", title: "Bars", x: [-1, count], y: [0, 3],
          draw: `return [bars(range(${count}).map((i) => [i, state.a]), { labels: range(${count}).map(String) })];` },
      ],
    });
    const many = frame(labelled(n)).doc.querySelectorAll(".mk-barlabel").length;
    expect(many).toBeGreaterThan(3);
    expect(many).toBeLessThan(n);
    expect(frame(labelled(4)).doc.querySelectorAll(".mk-barlabel").length).toBe(4);
  });
});

// Soft layout check (validator layoutProblems): measured with the renderer's own geometry (structures.chartFrame).
const layoutFixture = (name: string) => require(`./fixtures/playground-layout/${name}.json`);

describe("playground layout check", () => {
  it("reports overlapping shapes, a shape past the edge and a hidden arrow, but still passes", () => {
    const r = validate(layoutFixture("pipeline-overlap"));
    expect(r.ok).toBe(true);
    const all = r.layout.join("\n");
    expect(all).toMatch(/'ret' and 'rr' overlap/);
    expect(all).toMatch(/'rr' and 'pk' overlap/);
    expect(all).toMatch(/'gen' sticks out \d+ px past the right edge/);
    expect(all).toMatch(/link from 'q' to 'ret' has only \d+ px/);
    expect(all).toMatch(/1 x unit = 41\.6 px/);  // 416 px of drawing area over x [0, 10]
  });

  it("is clean once the diagram is spread out", () => {
    expect(validate(layoutFixture("pipeline-fixed")).layout).toEqual([]);
  });

  it("is clean for every hand-written example (no false alarms)", () => {
    for (const [name, spec] of specs) expect([name, validate(spec).layout]).toEqual([name, []]);
  });

  const chartSpec = (draw: string, extra: Record<string, unknown> = {}) => ({
    title: "t", state: { a: 1 },
    components: [
      { id: "s1", type: "slider", label: "a", key: "a", min: 0, max: 2, step: 1 },
      { id: "c1", type: "chart", title: "C", x: [0, 10], y: [0, 6], draw, ...extra },
    ],
  });

  it("reports a label cut by a narrow shape and a link label on a shape", () => {
    const r = validate(chartSpec("return [node('a', 2, 3, { label: 'Concat and heads', w: 60 }), node('b', 4, 3, { label: 'b', w: 60 + 10 * state.a }), link('a', 'b', { label: 'long link text' })];"));
    const all = r.layout.join("\n");
    expect(all).toMatch(/'Concat and heads' shows as 'Concat …' because shape 'a' is too narrow; make it wider \(w: 116\)/);
    expect(all).toMatch(/label 'long link text' on the link from 'a' to 'b' sits on shape/);
  });

  it("reports a line outside the y range and a line capped at the top edge", () => {
    // (a line wholly outside the range is already a hard error; this one is mostly outside)
    const off = validate(chartSpec("return [line(range(11).map((i) => [i, i < 4 ? 2 : 50 + state.a]), { label: 'res' })];"));
    expect(off.layout.join("\n")).toMatch(/line 'res' lies outside the y range \[0,6\] for 7 of 11 points/);
    const capped = validate(chartSpec("return [line(range(11).map((i) => [i, Math.min(6, 20 * state.a + i)]), { label: 'res' })];"));
    expect(capped.layout.join("\n")).toMatch(/line 'res' sits on the top edge/);
  });

  it("reports empty space only when no state ever fills the x range", () => {
    // data reaches x 0..2 at most: mostly empty
    const empty = validate(chartSpec("return [bars(range(3).map((i) => [i, 1 + state.a]))];"));
    expect(empty.layout.join("\n")).toMatch(/never fills more than \d+% of the x range/);
    // a slider grows the data to the whole range: fine even though it starts small
    const grows = {
      ...chartSpec("return [bars(range(state.a).map((i) => [i, 5]))];"),
      state: { a: 3 },
    };
    grows.components[0] = { id: "s1", type: "slider", label: "a", key: "a", min: 1, max: 11, step: 1 };
    expect(validate(grows).layout).toEqual([]);
  });
});

describe("playground layout check: text, heatmaps, axis names, links through shapes", () => {
  const spec = (draw: string, extra: Record<string, unknown> = {}) => ({
    title: "t", state: { a: 1 },
    components: [
      { id: "s1", type: "slider", label: "a", key: "a", min: 0, max: 2, step: 1 },
      { id: "c1", type: "chart", title: "C", x: [0, 10], y: [0, 6], draw, ...extra },
    ],
  });

  it("reports a text mark wider than the chart (one near an edge is moved inside instead)", () => {
    const r = validate(spec("return [line([[0, state.a], [10, 5]]), text(1, 3, 'a sentence far too long to fit in this chart even when moved inside')];"));
    expect(r.layout.join("\n")).toMatch(/the text 'a sentence far too long.*runs \d+ px past the edge/);
    expect(validate(spec("return [line([[0, state.a], [10, 5]]), text(5, 3, 'short')];")).layout).toEqual([]);
    // centred on the left edge, at the top, or following a point past the right edge: placeText moves it inside
    expect(validate(spec("return [line([[0, state.a], [10, 5]]), text(0, 3, 'sigmoid'), text(5, 6, 'top'), text(10 + state.a, 2, 'moving')];")).layout).toEqual([]);
  });

  it("nudges a label off the line it is drawn across, and reports one with no free spot", () => {
    const structures = require("../shared/playground/structures.cjs");
    const across = spec("return [line([[0, state.a], [10, 5]]), hline(2, { dashed: true }), text(1, 1.8, 'mode 2')];");
    expect(validate(across).layout).toEqual([]);
    const c = across.components[1] as { x: number[]; y: number[] };
    const marks = [{ type: "hline", y: 2 }, { type: "text", x: 1, y: 1.8, text: "mode 2" }];
    const f = structures.chartFrame(c, marks);
    const t = structures.placeLabels(marks, f).get(marks[1]);
    expect(t.y).not.toBeCloseTo(f.sy(1.8));  // moved up or down, off the rule
    expect(t.onLine).toBeUndefined();
    // rules every 0.2: no spot near the label is free
    const crowded = spec("return [line([[0, state.a], [10, 5]]), ...range(31).map((i) => hline(i * 0.2)), text(5, 3, 'stuck')];");
    expect(validate(crowded).layout.join("\n")).toMatch(/the text 'stuck' is drawn across a line/);
  });

  it("reports a y range the data never fills, like the x range", () => {
    const r = validate(spec("return [area(range(11).map((i) => [i, 0.2 * state.a * Math.exp(-((i - 5) ** 2) / 8)]))];", { y: [0, 6] }));
    expect(r.layout.join("\n")).toMatch(/never fills more than \d+% of the y range \[0,6\] in any state.*narrow y to about \[/);
  });

  it("reports a problem that appears only after a control moves, marked as later", () => {
    const capped = validate(spec("return [line(range(11).map((i) => [i, Math.min(6, state.a * state.a * i)]))];"));
    expect(capped.layout.join("\n")).toMatch(/sits on the top edge.*\(after the controls move, not at the start\)/);
    const first = validate(spec("return [heatmap([[state.a, 2], [3, 4]], { x: [0, 10], y: [0, 6] })];", { yLabel: "query" }));
    expect(first.layout).toHaveLength(1);
    expect(first.layout[0]).not.toMatch(/after the controls move/);
  });

  it("reports a heatmap bigger than the fixed range, and a y-axis name on its cells", () => {
    const r = validate(spec("return [heatmap([[state.a, 2], [3, 4]], { x: [0, 12], y: [0, 6] })];", { yLabel: "query token" }));
    const all = r.layout.join("\n");
    expect(all).toMatch(/the heatmap covers x \[0,12\] but the chart's x range is \[0,10\]/);
    expect(all).toMatch(/y-axis name 'query token' is drawn over the heatmap/);
    expect(validate(spec("return [heatmap([[state.a, 2], [3, 4]], { x: [0, 10], y: [0, 6] })];")).layout).toEqual([]);
  });

  it("reports a link that runs through a third shape, not one that bends around it", () => {
    const shapes = "node('a', 1, 3, { w: 50 }), node('mid', 5, 3, { w: 50, label: 'mid' }), node('b', 9, 3, { w: 50 + state.a })";
    const straight = validate(spec(`return [${shapes}, link('a', 'b')];`));
    expect(straight.layout.join("\n")).toMatch(/link from 'a' to 'b' runs through shape 'mid'/);
    const bent = validate(spec(`return [${shapes}, link('a', 'b', { curve: 0.6 })];`));
    expect(bent.layout.join("\n")).not.toMatch(/runs through/);
  });
});
