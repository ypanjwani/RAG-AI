import { describe, expect, it } from "vitest";
import { assembleSpec, MAX_STAGES, requestedStages, withoutBlocks } from "../shared/validate";
import { SAMPLE_DESIGN, SAMPLE_OUTLINE } from "../shared/sample";
import { Design, LITE_DROPPED, Outline, parseOutline, type Block } from "../shared/spec";

describe("assembleSpec", () => {
  it("the sample outline and design match the AI schemas", () => {
    expect(Outline.safeParse(SAMPLE_OUTLINE).success).toBe(true);
    expect(Design.safeParse(SAMPLE_DESIGN).success).toBe(true);
  });

  it("keeps every block of the valid sample", () => {
    const spec = assembleSpec(SAMPLE_OUTLINE, SAMPLE_DESIGN, "ctx");
    expect(spec.warnings).toEqual([]);
    expect(spec.stages.map((s) => s.blocks.length)).toEqual(SAMPLE_OUTLINE.stages.map((s) => s.blocks.length));
    expect(spec.stages[0].icon).toBe("✂️");
  });

  it("falls back to the default design when the design step failed", () => {
    const spec = assembleSpec(SAMPLE_OUTLINE, null, "");
    expect(spec.design).toEqual({ theme: "ocean", fontPairing: "modern", headingStyle: "gradient-banner" });
    expect(spec.chatbot.starters.length).toBeGreaterThan(0);
    expect(spec.warnings.length).toBe(1);
  });

  it("repairs or drops invalid blocks", () => {
    const blocks: Block[] = [
      { type: "text", md: "ok" },
      { type: "slider", var: "k", label: "k", min: 10, max: 1, step: -1, value: 99, unit: "" },
      { type: "plot", title: "", xLabel: "", yLabel: "", xMin: 0, xMax: 1, yMin: 0, yMax: 0, markerX: "nope", series: [{ label: "good", expr: "k * x" }, { label: "bad", expr: "q * x" }] },
      { type: "quiz", questions: [{ q: "?", choices: ["a", "b"], answer: 7, explain: "" }] },
      { type: "attention", tokens: ["a", "b", "c"], weights: [[1]], caption: "" },
      { type: "architecture", nodes: [{ id: "a", label: "A", detail: "" }], edges: [{ from: "a", to: "zzz" }] },
      { type: "bars", title: "", labels: ["one"], values: ["eval(1)"], normalize: false },
      { type: "slider", var: "x", label: "reserved name", min: 0, max: 1, step: 0.1, value: 0, unit: "" },
    ];
    const outline: Outline = { ...SAMPLE_OUTLINE, stages: [{ id: "s 1!", label: "S", phase: "missing", blurb: "", blocks }] };
    const spec = assembleSpec(outline, null, "");
    const st = spec.stages[0];
    expect(st.id).toBe("s-1");
    expect(st.phase).toBe("read");
    const types = st.blocks.map((b) => b.type);
    expect(types).toEqual(["text", "slider", "plot", "quiz", "attention", "architecture"]);
    const slider = st.blocks[1] as Extract<Block, { type: "slider" }>;
    expect([slider.min, slider.max, slider.value]).toEqual([1, 10, 10]);
    expect(slider.step).toBeCloseTo(0.09);
    const plot = st.blocks[2] as Extract<Block, { type: "plot" }>;
    expect(plot.series.map((s) => s.label)).toEqual(["good"]);
    expect(plot.markerX).toBe("");
    expect((st.blocks[3] as Extract<Block, { type: "quiz" }>).questions[0].answer).toBe(1);
    expect((st.blocks[4] as Extract<Block, { type: "attention" }>).weights).toEqual([[1, 0, 0], [0, 0, 0], [0, 0, 0]]);
    expect((st.blocks[5] as Extract<Block, { type: "architecture" }>).edges).toEqual([]);
    expect(spec.phases.map((p) => p.id)).toEqual(["read"]);
  });
});

describe("requestedStages", () => {
  it("reads an explicit stage count in digits or words", () => {
    expect(requestedStages("Explain diffusion models in 14 stages")).toEqual({ count: 14, clamped: false });
    expect(requestedStages("a 12-part explainer on transformers")).toEqual({ count: 12, clamped: false });
    expect(requestedStages("Attention, in fourteen sections please")).toEqual({ count: 14, clamped: false });
    expect(requestedStages("RLHF in 10 chapters")).toEqual({ count: 10, clamped: false });
  });

  it("ignores step counts and texts without a count", () => {
    expect(requestedStages("Diffusion with 50 denoising steps")).toBeUndefined();
    expect(requestedStages("Explain how diffusion models remove noise")).toBeUndefined();
    expect(requestedStages("the one part everyone skips")).toBeUndefined();
  });

  it("clamps counts above the maximum", () => {
    expect(requestedStages("40 stages on LLMs")).toEqual({ count: MAX_STAGES, clamped: true });
  });

  it("assembleSpec keeps up to MAX_STAGES stages", () => {
    const stage = SAMPLE_OUTLINE.stages[0];
    const many = { ...SAMPLE_OUTLINE, stages: Array.from({ length: 14 }, (_, i) => ({ ...stage, id: `s${i}`, label: `Stage ${i + 1}` })) };
    expect(assembleSpec(many, SAMPLE_DESIGN, "").stages).toHaveLength(14);
  });
});

describe("Lite+", () => {
  it("strips quizzes, questions and worked examples and keeps everything else", () => {
    const types = (o: typeof SAMPLE_OUTLINE) => o.stages.flatMap((s) => s.blocks.map((b) => b.type));
    const before = types(SAMPLE_OUTLINE);
    expect(before.some((t) => LITE_DROPPED.has(t))).toBe(true);
    const after = types(withoutBlocks(SAMPLE_OUTLINE, LITE_DROPPED));
    expect(after).toEqual(before.filter((t) => !LITE_DROPPED.has(t)));
    expect(assembleSpec(withoutBlocks(SAMPLE_OUTLINE, LITE_DROPPED), SAMPLE_DESIGN, "").warnings).toEqual([]);
  });
});

describe("parseOutline", () => {
  it("drops only the malformed block and reports it", () => {
    const raw = structuredClone(SAMPLE_OUTLINE) as any;
    raw.stages[0].blocks.push({ type: "slider", var: "z" }, { type: "hologram" });
    const r = parseOutline(raw);
    if (!("outline" in r)) throw new Error(r.error);
    expect(r.outline.stages[0].blocks.length).toBe(SAMPLE_OUTLINE.stages[0].blocks.length);
    expect(r.warnings.length).toBe(2);
  });

  it("returns an error when the outline itself is malformed", () => {
    expect("error" in parseOutline({ title: "x" })).toBe(true);
    expect("error" in parseOutline([1, 2])).toBe(true);
    expect("error" in parseOutline({ title: "x", stages: [null, { blocks: [] }] })).toBe(true);
  });

  it("fills missing outline fields instead of rejecting the whole outline", () => {
    const raw = structuredClone(SAMPLE_OUTLINE) as any;
    raw.level = "intermediate";
    delete raw.glossary;
    delete raw.phases;
    delete raw.keyIntuition;
    delete raw.stages[0].blurb;
    raw.stages.push("not a stage");
    const r = parseOutline(raw);
    if (!("outline" in r)) throw new Error(r.error);
    expect(r.outline.level).toBe("student");
    expect(r.outline.glossary).toEqual([]);
    expect(r.outline.phases).toEqual([]);
    expect(r.outline.keyIntuition).toBe(SAMPLE_OUTLINE.subtitle);
    expect(r.outline.stages).toHaveLength(SAMPLE_OUTLINE.stages.length);
    expect(r.outline.stages[0].blurb).toBe("");
    expect(r.warnings.some((w) => w.includes('"level"'))).toBe(true);
    expect(r.warnings.some((w) => w.includes("Dropped stage"))).toBe(true);
    // The repaired outline still assembles into a page.
    expect(assembleSpec(r.outline, SAMPLE_DESIGN, "").stages).toHaveLength(SAMPLE_OUTLINE.stages.length);
  });
});

describe("equation views: validation and sanity check", () => {
  const stageWith = (blocks: Block[]): Outline => ({
    ...SAMPLE_OUTLINE,
    stages: [{ id: "m", label: "M", phase: "read", blurb: "", blocks: [{ type: "slider", var: "k", label: "k", min: 0, max: 2, step: 0.1, value: 1, unit: "" }, ...blocks] }],
  });
  const run = (blocks: Block[]) => {
    const spec = assembleSpec(stageWith(blocks), SAMPLE_DESIGN, "");
    return { blocks: spec.stages[0]?.blocks.slice(1) ?? [], warnings: spec.warnings };
  };

  it("scopes defs in order and drops ones that use later or unknown names", () => {
    const { blocks, warnings } = run([{ type: "calculator", title: "c", caption: "",
      defs: [{ name: "a", expr: "b + 1" }, { name: "b", expr: "k * 2" }, { name: "c", expr: "b + 1" }],
      rows: [{ latex: "c", expr: "c", note: "", digits: 2 }, { latex: "a", expr: "a", note: "", digits: 2 }] }]);
    const calc = blocks[0] as Extract<Block, { type: "calculator" }>;
    expect(calc.defs!.map((d) => d.name)).toEqual(["b", "c"]);
    expect(calc.rows.map((r) => r.latex)).toEqual(["c"]);
    expect(warnings.some((w) => w.includes('"a"'))).toBe(true);
  });

  it("drops blocks whose numbers never work and warns on flat plots", () => {
    const { blocks, warnings } = run([
      { type: "readout", items: [{ label: "bad", expr: "log(-1 - k)", digits: 2 }] },
      { type: "plot", title: "flat", xLabel: "", yLabel: "", xMin: 0, xMax: 1, yMin: 0, yMax: 0, markerX: "", series: [{ label: "const", expr: "3" }] },
    ]);
    expect(blocks.map((b) => b.type)).toEqual(["plot"]);
    expect(warnings.some((w) => w.includes("flat"))).toBe(true);
  });

  it("warns when a probability leaves 0..1", () => {
    const { warnings } = run([{ type: "readout", items: [{ label: "p", expr: "k", digits: 2, kind: "probability" }] }]);
    expect(warnings.some((w) => w.includes("probability"))).toBe(true);
  });

  it("repairs trajectories: keeps valid updates, drops bad paths", () => {
    const { blocks } = run([
      { type: "trajectory", title: "t", caption: "", mode: "time", steps: "20", logY: false,
        state: [{ name: "w", init: "1" }], update: [{ name: "w", expr: "w - k * w" }, { name: "zz", expr: "1" }],
        series: [{ label: "w", expr: "w" }] },
      { type: "trajectory", title: "bad", caption: "", mode: "path", steps: "20", logY: false,
        state: [{ name: "w", init: "1" }], update: [{ name: "w", expr: "w" }], series: [] },
    ]);
    expect(blocks.length).toBe(1);
    expect((blocks[0] as Extract<Block, { type: "trajectory" }>).update.map((u) => u.name)).toEqual(["w"]);
  });
});

describe("def names", () => {
  it("allows y in a calculator but not in a heatmap", () => {
    const outline: Outline = { ...SAMPLE_OUTLINE, stages: [{ id: "d", label: "D", phase: "read", blurb: "", blocks: [
      { type: "calculator", title: "c", caption: "", defs: [{ name: "y", expr: "[1, 2, 3]" }], rows: [{ latex: "\bar y", expr: "mean(y)", note: "", digits: 2 }] },
      { type: "heatmap", title: "h", caption: "", defs: [{ name: "y", expr: "2" }], expr: "x + y", xMin: 0, xMax: 1, yMin: 0, yMax: 1, xLabel: "", yLabel: "", zLabel: "", diverging: false },
    ] }] };
    const spec = assembleSpec(outline, SAMPLE_DESIGN, "");
    const [calc, heat] = spec.stages[0].blocks as [Extract<Block, { type: "calculator" }>, Extract<Block, { type: "heatmap" }>];
    expect(calc.defs!.map((d) => d.name)).toEqual(["y"]);
    expect(heat.defs).toEqual([]);
  });
});
