import { describe, expect, it } from "vitest";
import { usesRandom } from "../shared/expr";
import { SAMPLE_DESIGN, SAMPLE_OUTLINE } from "../shared/sample";
import type { Block, Outline } from "../shared/spec";
import { assembleSpec } from "../shared/validate";

const slider = (v: string, min = 0, max = 10, value = 5): Block => ({ type: "slider", var: v, label: v.toUpperCase(), min, max, step: 1, value, unit: "" });

/** Build a one-stage outline (plus optional extra stages) and validate it. */
function run(blocks: Block[], extra: Outline["stages"] = []) {
  const outline: Outline = { ...SAMPLE_OUTLINE, stages: [{ id: "s", label: "S", phase: "read", blurb: "", blocks }, ...extra] };
  const spec = assembleSpec(outline, SAMPLE_DESIGN, "");
  return { spec, blocks: spec.stages[0]?.blocks ?? [], warnings: spec.warnings };
}

describe("usesRandom", () => {
  it("detects random calls directly and through defs", () => {
    expect(usesRandom("normal(0, 1)")).toBe(true);
    expect(usesRandom("2 * k + 1")).toBe(false);
    expect(usesRandom("mean(y)", [{ name: "y", expr: "uniform(0, 1, 10)" }])).toBe(true);
    expect(usesRandom("mean(y)", [{ name: "y", expr: "linspace(0, 1, 10)" }])).toBe(false);
  });
});

describe("experiment gate", () => {
  const exp = (trial: string, defs: { name: string; expr: string }[] = []): Block => ({
    type: "experiment", title: "E", defs, trial, valueLabel: "v", repeats: [100, 1, 1, 10, 99999], caption: "",
  });

  it("drops an experiment with nothing random in it", () => {
    const { blocks, warnings } = run([slider("k"), exp("2 * k")]);
    expect(blocks.map((b) => b.type)).not.toContain("experiment");
    expect(warnings.some((w) => w.includes("nothing in it is random"))).toBe(true);
  });

  it("keeps random experiments and cleans the repeat buttons", () => {
    const { blocks } = run([slider("k"), exp("x1 + k", [{ name: "x1", expr: "normal(0, 1)" }])]);
    const e = blocks.find((b) => b.type === "experiment") as Extract<Block, { type: "experiment" }>;
    expect(e.repeats).toEqual([1, 10, 100, 10000]);
  });

  it("falls back to numbers when outcome indices are invalid", () => {
    const { blocks } = run([{ ...exp("uniform(0, 1)"), outcomes: ["a", "b"] } as Block]);
    expect((blocks[0] as Extract<Block, { type: "experiment" }>).outcomes).toBeUndefined();
  });
});

describe("auto-graph", () => {
  it("adds a sensitivity plot when a stage has controls and readouts but no graph", () => {
    const { blocks } = run([slider("d", 64, 4096, 768), slider("r", 1, 64, 8), { type: "readout", items: [{ label: "LoRA params", expr: "2 * d * r", digits: 0 }] }]);
    const plots = blocks.filter((b) => b.type === "plot") as Extract<Block, { type: "plot" }>[];
    expect(plots.map((p) => p.sweepVar)).toEqual(["d", "r"]);
    expect(plots[0].xMin).toBe(64);
    expect(plots[0].markerX).toBe("d");
  });

  it("does nothing when the stage already has a graph", () => {
    const { blocks } = run([slider("k"), { type: "readout", items: [{ label: "y", expr: "k * 2", digits: 0 }] },
      { type: "bars", title: "", labels: ["a"], values: ["k"], normalize: false }]);
    expect(blocks.filter((b) => b.type === "plot")).toEqual([]);
  });
});

describe("variety caps", () => {
  it("keeps at most one pipeline per explainer", () => {
    const pipe: Block = { type: "pipeline", steps: [{ label: "a", input: "1", output: "2", detail: "" }] };
    const { spec, warnings } = run([pipe], [{ id: "t", label: "T", phase: "read", blurb: "", blocks: [{ type: "text", md: "x" }, pipe] }]);
    const count = spec.stages.flatMap((s) => s.blocks).filter((b) => b.type === "pipeline").length;
    expect(count).toBe(1);
    expect(warnings.some((w) => w.includes("max 1"))).toBe(true);
  });
});

describe("tool repairs", () => {
  it("drops preset/example settings for unknown variables", () => {
    const { blocks } = run([slider("k"),
      { type: "presets", label: "P", items: [{ label: "ok", set: [{ var: "k", value: 3 }, { var: "nope", value: 1 }] }, { label: "bad", set: [{ var: "nope", value: 1 }] }] },
      { type: "examples", title: "E", items: [{ title: "x", set: [{ var: "ghost", value: 1 }], focus: "" }] },
    ]);
    const p = blocks.find((b) => b.type === "presets") as Extract<Block, { type: "presets" }>;
    expect(p.items).toEqual([{ label: "ok", set: [{ var: "k", value: 3 }] }]);
    expect(blocks.some((b) => b.type === "examples")).toBe(false);
  });

  it("normalises table shape and removes broken live cells", () => {
    const { blocks, warnings } = run([slider("k"),
      { type: "table", title: "T", columns: ["a", "b"], rows: [["1"], ["=k * 2", "=ghost + 1", "extra"]], caption: "" }]);
    const t = blocks.find((b) => b.type === "table") as Extract<Block, { type: "table" }>;
    expect(t.rows).toEqual([["1", ""], ["=k * 2", "—"]]);
    expect(warnings.some((w) => w.includes("ghost"))).toBe(true);
  });

  it("maps concept-map stages to final ids and clears unknown ones", () => {
    const map: Block = { type: "conceptmap", title: "M", nodes: [
      { id: "a", label: "A", sub: "", desc: "", stage: "Second Stage!" },
      { id: "b", label: "B", sub: "", desc: "", stage: "missing" },
    ], edges: [{ from: "a", to: "b", label: "x" }, { from: "a", to: "zzz", label: "y" }] };
    const { blocks } = run([map], [{ id: "Second Stage!", label: "Two", phase: "read", blurb: "", blocks: [{ type: "text", md: "x" }] }]);
    const m = blocks[0] as Extract<Block, { type: "conceptmap" }>;
    expect(m.nodes.map((n) => n.stage)).toEqual(["second-stage", ""]);
    expect(m.edges.length).toBe(1);
  });

  it("forces toggles to 0/1", () => {
    const { blocks } = run([{ type: "toggle", var: "on1", label: "L", on: "on", off: "off", value: 7 }]);
    expect((blocks[0] as Extract<Block, { type: "toggle" }>).value).toBe(1);
  });
});

describe("worked examples", () => {
  it("computes each example's facts at its own settings", async () => {
    const { applyExampleSteps, exampleBriefs } = await import("../shared/examples");
    const spec = assembleSpec(SAMPLE_OUTLINE, SAMPLE_DESIGN, "");
    const briefs = exampleBriefs(spec);
    expect(briefs.map((b) => b.title)).toEqual(["Slow but safe: η = 0.02", "Past the edge: η = 0.28"]);
    const fact = (i: number) => briefs[i].computed.find((c) => c.label.includes("w2 after"))!;
    expect(fact(0).label).toContain("after 60 steps");
    expect(fact(1).label).toContain("after 30 steps");
    expect(Math.abs(Number(fact(1).value))).toBeGreaterThan(100); // diverging run
    expect(briefs[0].settings[0]).toEqual({ name: "lr", label: "Learning rate η", value: 0.02 });

    const filled = applyExampleSteps(spec, new Map([[briefs[0].key, ["Step one.", "Step two."]]]));
    expect(exampleBriefs(filled).length).toBe(1);
  });
});

describe("trajectory defs", () => {
  it("re-evaluates defs every step so they can use t and the state", () => {
    // A tiny denoising loop: the schedule depends on t, the estimate on the state.
    const traj: Block = { type: "trajectory", title: "denoise", caption: "", mode: "time", steps: "10", logY: false,
      defs: [{ name: "a", expr: "1 - t / 20" }, { name: "est", expr: "v * a" }],
      state: [{ name: "v", init: "1" }], update: [{ name: "v", expr: "est" }],
      series: [{ label: "v", expr: "v" }, { label: "a", expr: "a" }] };
    const { blocks, warnings } = run([traj]);
    expect(warnings).toEqual([]);
    const t = blocks[0] as Extract<Block, { type: "trajectory" }>;
    expect(t.defs!.map((d) => d.name)).toEqual(["a", "est"]);
  });

  it("computes per-step values in runTrajectory", async () => {
    const { runTrajectory } = await import("../shared/views");
    const r = runTrajectory({ steps: "3", state: [{ name: "v", init: "1" }], update: [{ name: "v", expr: "v * a" }], defs: [{ name: "a", expr: "t + 2" }] }, {});
    // v1 = 1*(0+2) = 2, v2 = 2*(1+2) = 6, v3 = 6*(2+2) = 24
    expect(r.frames.map((f) => f.v)).toEqual([1, 2, 6, 24]);
    expect(r.frames[3].a).toBe(5); // defs are also visible to series
  });
});

describe("robustness fixes from real runs", () => {
  it("lets a def named pi shadow the constant, but keeps pi elsewhere", async () => {
    const { evalNumber } = await import("../shared/expr");
    expect(evalNumber("2 * pi", {})).toBeCloseTo(6.2832, 4);
    expect(evalNumber("pi * 10", { pi: 0.3 })).toBeCloseTo(3, 10);
    const { blocks, warnings } = run([slider("k"), { type: "readout", defs: [{ name: "pi", expr: "k / 10" }], items: [{ label: "p", expr: "pi", digits: 2 }] }]);
    expect(warnings).toEqual([]);
    expect(blocks.some((b) => b.type === "readout")).toBe(true);
  });

  it("turns a card variant used as a block type into a cards block", async () => {
    const { parseOutline } = await import("../shared/spec");
    const raw = structuredClone(SAMPLE_OUTLINE) as any;
    raw.stages[0].blocks = [{ type: "tip", title: "Read the slope", md: "Steeper means faster." }];
    const r = parseOutline(raw);
    if (!("outline" in r)) throw new Error(r.error);
    expect(r.outline.stages[0].blocks).toEqual([{ type: "cards", variant: "tip", title: "", items: [{ title: "Read the slope", md: "Steeper means faster." }] }]);
  });
});

describe("cross-check (one quantity, one answer)", () => {
  // From a real Quality+ page: a readout computes the balancing loss from the router's shares q,
  // while a calculator retypes q's first share as 0.87 and gets the formula wrong around it.
  const bias = slider("bias", 0, 3, 3);
  const defs = [{ name: "be", expr: "bias * (1 - 0.85 * lb)" }, { name: "q", expr: "softmax([be, 0, 0, 0])" }];
  const toggle: Block = { type: "toggle", var: "lb", label: "Loss", on: "on", off: "off", value: 0 };
  const readout: Block = { type: "readout", defs, items: [{ label: "Balancing loss", expr: "4 * sum(q * q)", digits: 2, kind: "number" }] };
  const calc = (expr: string): Block => ({
    type: "calculator", title: "Worked check", defs: [], caption: "",
    rows: [
      { latex: "even", expr: "4 * 4 * 0.25 * 0.25", note: "", digits: 2 },
      { latex: "collapse", expr, note: "", digits: 2 },
    ],
  });

  it("drops a hand-copied row that disagrees with the page's own formula", () => {
    const { blocks, warnings } = run([bias, toggle, readout, calc("4 * (0.87 * 0.87 + 3 * 0.13 * 0.13 / 3 * 3)")]);
    const c = blocks.find((b) => b.type === "calculator") as Extract<Block, { type: "calculator" }>;
    expect(c.rows.map((r) => r.latex)).toEqual(["even"]);
    expect(warnings.some((w) => w.includes("types 0.87 by hand"))).toBe(true);
    expect(warnings.some((w) => w.includes("gives 3.23") && w.includes("gives 3.05"))).toBe(true);
  });

  it("only warns when the hand-copied row agrees", () => {
    const { blocks, warnings } = run([bias, toggle, readout, calc("4 * (0.87 * 0.87 + 3 * (0.0433 * 0.0433))")]);
    const c = blocks.find((b) => b.type === "calculator") as Extract<Block, { type: "calculator" }>;
    expect(c.rows).toHaveLength(2);
    expect(warnings.some((w) => w.includes("types 0.87 by hand"))).toBe(true);
    expect(warnings.some((w) => w.startsWith("Dropped"))).toBe(false);
  });

  it("leaves rows that use the variable alone", () => {
    const { warnings } = run([bias, toggle, readout, { ...calc("4 * sum(q * q)"), defs } as Block]);
    expect(warnings.filter((w) => w.includes("by hand"))).toEqual([]);
  });

  it("finds nothing on the sample explainer", () => {
    const spec = assembleSpec(SAMPLE_OUTLINE, SAMPLE_DESIGN, "");
    expect(spec.warnings.filter((w) => w.includes("by hand"))).toEqual([]);
  });
});
