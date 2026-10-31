import { describe, expect, it } from "vitest";
import { SAMPLE_DESIGN, SAMPLE_OUTLINE } from "../shared/sample";
import { SceneSim, type SceneBlock } from "../shared/scene";
import type { Block, Outline } from "../shared/spec";
import { assembleSpec } from "../shared/validate";

/** "Play" B from 1 to 100 while a compare chart, a readout and live text follow it. */
const PLAY: SceneBlock = {
  type: "scene", title: "Averaging", lesson: "Averaging more models narrows the spread.",
  state: [
    { name: "B", label: "Models B", value: 1, min: 1, max: 100, step: 1 },
    { name: "running", label: "Playing", value: 0, min: 0, max: 1, step: 1 },
  ],
  elements: [
    { id: "models", kind: "slider", var: "B" },
    { id: "play", kind: "button", label: "Play", disabled: "running" },
    { id: "spread", kind: "chart", label: "Spread of the average", series: [{ label: "mean", expr: "mean(normal(0, 1, B))" }], by: "B", at: [1], view: "compare" },
    { id: "sd", kind: "readout", label: "Expected spread", expr: "1 / sqrt(B)", digits: 3 },
    { id: "note", kind: "text", md: "With **{{B}}** models the spread is about {{1 / sqrt(B)}}.", visible: "B > 1" },
  ],
  rules: [
    { when: "click", target: "play", set: [{ var: "B", expr: "1" }, { var: "running", expr: "1" }] },
    { when: "every", ms: 50, if: "running", set: [{ var: "B", expr: "B + 1" }] },
    { when: "change", target: "B", if: "B >= 100", set: [{ var: "running", expr: "0" }] },
  ],
  layout: "stack", caption: "",
};

const PAGE_SLIDER: Block = { type: "slider", var: "k", label: "k", min: 0, max: 2, step: 0.1, value: 1, unit: "" };
const outlineWith = (...blocks: Block[]): Outline => ({
  ...SAMPLE_OUTLINE, stages: [{ id: "m", label: "M", phase: "read", blurb: "", blocks: [{ type: "text", md: "x" }, PAGE_SLIDER, ...blocks] }],
});
const run = (scene: SceneBlock) => {
  const spec = assembleSpec(outlineWith(scene), SAMPLE_DESIGN, "");
  const s = (spec.stages[0]?.blocks ?? []).find((b): b is SceneBlock => b.type === "scene");
  return { scene: s, warnings: spec.warnings };
};
const scene = (over: Partial<SceneBlock>): SceneBlock => ({ ...PLAY, ...over });

describe("scene: the rule runner", () => {
  it("plays B from 1 to 100 and stops itself", () => {
    const sim = new SceneSim(PLAY, () => ({ k: 1 }));
    sim.fire("click", "play");
    expect(sim.vars).toMatchObject({ B: 1, running: 1 });
    for (let n = 0; n < 150; n++) sim.tick(1);
    expect(sim.vars).toMatchObject({ B: 100, running: 0 });
    expect(sim.holds("running")).toBe(false);
  });

  it("computes a rule's assignments first, clamps them, and stops endless chains", () => {
    const swap = scene({
      state: [{ name: "a", label: "a", value: 1, min: 0, max: 5, step: 1 }, { name: "b", label: "b", value: 2, min: 0, max: 5, step: 1 }],
      rules: [{ when: "start", set: [{ var: "a", expr: "b" }, { var: "b", expr: "a + 10" }] }],
    });
    const sim = new SceneSim(swap, () => ({}));
    sim.fire("start");
    expect(sim.vars).toEqual({ a: 2, b: 5 }); // both read the old values; b is clamped to its max
    const loop = scene({
      state: [{ name: "a", label: "a", value: 0, min: 0, max: 1000, step: 1 }, { name: "b", label: "b", value: 0, min: 0, max: 1000, step: 1 }],
      rules: [{ when: "change", target: "a", set: [{ var: "b", expr: "b + 1" }] }, { when: "change", target: "b", set: [{ var: "a", expr: "a + 1" }] },
        { when: "start", set: [{ var: "a", expr: "1" }] }],
    });
    expect(new SceneSim(loop, () => ({})).fire("start").overflow).toBe(true);
  });
});

describe("scene: checks", () => {
  it("keeps a valid scene without warnings", () => {
    const { scene: s, warnings } = run(PLAY);
    expect(warnings).toEqual([]);
    expect(s!.elements.map((e) => e.id)).toEqual(["models", "play", "spread", "sd", "note"]);
    expect(s!.rules[1].ms).toBe(50);
  });

  it("drops a slider and a button that change nothing the scene shows", () => {
    const { scene: s, warnings } = run(scene({
      state: [...PLAY.state, { name: "unused", label: "Unused", value: 1, min: 0, max: 2, step: 1 }],
      elements: [...PLAY.elements, { id: "knob", kind: "slider", var: "unused" }, { id: "idle", kind: "button", label: "Idle" }],
    }));
    expect(s!.elements.map((e) => e.id)).not.toContain("knob");
    expect(s!.elements.map((e) => e.id)).not.toContain("idle");
    expect(warnings).toContain('Scene "Averaging": dropped slider "knob": nothing in the scene shows unused.');
    expect(warnings).toContain('Scene "Averaging": dropped button "idle": clicking it changes nothing the scene shows.');
  });

  it("refuses a state variable named like a page control, and bad rules", () => {
    const { scene: s, warnings } = run(scene({
      state: [...PLAY.state, { name: "k", label: "clash", value: 0, min: 0, max: 1, step: 1 }],
      rules: [...PLAY.rules,
        { when: "click", target: "models", set: [{ var: "B", expr: "2" }] },
        { when: "every", ms: 5, set: [{ var: "B", expr: "B" }] },
        { when: "every", ms: 100, set: [{ var: "B", expr: "B" }] },
        { when: "start", set: [{ var: "k", expr: "1" }] }],
    }));
    expect(warnings).toContain('Scene "Averaging": dropped state variable "k" (a page control already uses that name).');
    expect(warnings).toContain('Scene "Averaging": dropped rule 4 ("models" isn\'t a button, chart, bar chart or table in the scene).');
    expect(warnings).toContain('Scene "Averaging": dropped rule 6 (at most 2 timers).');
    expect(warnings).toContain('Scene "Averaging": dropped rule 7 (it sets nothing).');
    expect(s!.rules.find((r) => r.when === "every" && r.ms === 30)).toBeTruthy(); // 5 ms raised to 30
  });

  it("lets click rules read what was clicked, and nothing else", () => {
    const bars: SceneBlock = scene({
      elements: [...PLAY.elements, { id: "choose", kind: "bars", label: "Pick B", labels: ["1", "10", "100"], values: "[1, 10, 100]", highlight: "i" }],
      rules: [...PLAY.rules, { when: "click", target: "choose", set: [{ var: "B", expr: "[1, 10, 100][i]" }] }],
    });
    const { scene: s, warnings } = run(bars);
    expect(s!.rules).toHaveLength(4);
    expect(s!.elements.find((e) => e.id === "choose")!.highlight).toBeUndefined(); // i isn't known outside a click rule
    expect(warnings).toContain('Scene "Averaging": removed the highlight formula of "choose".');
    const sim = new SceneSim(s!, () => ({ k: 1 }));
    sim.fire("click", "choose", { i: 2 });
    expect(sim.vars.B).toBe(100);
  });

  it("drops a rule that loops forever in the dry run, and a scene that shows nothing", () => {
    const { warnings } = run(scene({
      rules: [...PLAY.rules, { when: "change", target: "running", set: [{ var: "running", expr: "1 - running" }] }],
    }));
    expect(warnings).toContain('Scene "Averaging": dropped rule 4 (it set off an endless chain of updates).');
    const empty = run(scene({ elements: [{ id: "models", kind: "slider", var: "B" }] }));
    expect(empty.scene).toBeUndefined();
    expect(empty.warnings).toContain('Dropped scene "Averaging": nothing in it shows anything.');
  });

  it("repairs a chart element through the chart checks", () => {
    const { scene: s, warnings } = run(scene({
      elements: [...PLAY.elements.slice(0, 2), { id: "spread", kind: "chart", label: "Spread", series: [{ label: "mean", expr: "mean(normal(0, 1, B))" }], by: "B", at: [1], view: "line" }],
    }));
    expect(s!.elements[2].view).toBe("histogram");
    expect(warnings.some((w) => w.startsWith('Chart "Spread": the "line" view'))).toBe(true);
  });
});
