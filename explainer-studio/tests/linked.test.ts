import { describe, expect, it } from "vitest";
import { linkedControls, readsVars } from "../app/src/render/linked";
import { claimsOf } from "../shared/factcheck";
import { stageFacts } from "../shared/examples";
import { SAMPLE_DESIGN, SAMPLE_OUTLINE } from "../shared/sample";
import type { Block, ExplainerSpec } from "../shared/spec";
import { assembleSpec } from "../shared/validate";

const slider = (v: string, label: string): Block => ({ type: "slider", var: v, label, min: 0.01, max: 0.99, step: 0.01, value: 0.5, unit: "" });

const page = (stages: Block[][]): ExplainerSpec =>
  assembleSpec({ ...SAMPLE_OUTLINE, stages: stages.map((blocks, i) => ({ id: `s${i}`, label: `Stage ${i + 1}`, phase: SAMPLE_OUTLINE.phases[0].id, blurb: "", blocks: [{ type: "text", md: "Intro." }, ...blocks] })) }, SAMPLE_DESIGN, "");

describe("linked controls", () => {
  it("reads formulas, not labels or prose", () => {
    const b: Block = { type: "chart", title: "p", lesson: "q", series: [{ label: "q", expr: "p * k" }], x: "p", by: "", at: [], view: "line", labels: ["q"], caption: "q" };
    expect([...readsVars(b)].sort()).toEqual(["k", "p"]);
    const table: Block = { type: "table", title: "", columns: ["p", "value"], rows: [["p", "=k * 2"]], caption: "" };
    expect([...readsVars(table)]).toEqual(["k"]);
  });

  it("doesn't count a formula's symbol list or a quiz as reading a slider", () => {
    const f: Block = { type: "formula", latex: "I(p) = -\log p", caption: "", terms: [{ symbol: "p", meaning: "the probability" }] };
    expect([...readsVars(f)]).toEqual([]);
    const quiz: Block = { type: "quiz", questions: [{ q: "p", choices: ["p", "q"], answer: 0, explain: "q" }] };
    expect([...readsVars(quiz)]).toEqual([]);
  });

  it("ignores names a part defines itself", () => {
    const b: Block = { type: "readout", defs: [{ name: "k", expr: "p * 2" }], items: [{ label: "k", expr: "k + 1", digits: 2 }] };
    expect([...readsVars(b)]).toEqual(["p"]);
  });

  it("shows a control from another section once, before the first part that reads it", () => {
    const spec = page([
      [slider("p", "P"), slider("q", "Q"), { type: "readout", items: [{ label: "KL", expr: "p * ln(p / q)", digits: 3 }] }],
      [{ type: "readout", items: [{ label: "a", expr: "p + q", digits: 2 }] }, { type: "readout", items: [{ label: "b", expr: "p * 2", digits: 2 }] }],
    ]);
    // Stage 1 owns its sliders: nothing to link.
    expect(linkedControls(spec, 0).size).toBe(0);
    const links = linkedControls(spec, 1);
    expect([...links.keys()]).toEqual([1]);
    expect(links.get(1)!.map((l) => [l.block.type === "slider" && l.block.var, l.stage])).toEqual([["p", "Stage 1"], ["q", "Stage 1"]]);
  });

  it("doesn't link a control the section has itself, or one a worked example only sets", () => {
    const spec = page([
      [slider("p", "P")],
      [slider("p", "P again"), { type: "readout", items: [{ label: "a", expr: "p", digits: 2 }] },
        { type: "examples", title: "", items: [{ title: "t", set: [{ var: "p", value: 0.2 }], focus: "f" }] }],
    ]);
    expect(linkedControls(spec, 1).size).toBe(0);
  });
});

describe("fact check context", () => {
  it("gives a nested sentence the heading it sits under", () => {
    const spec = page([[{ type: "compare", left: { title: "Forward KL", points: ["Average taken under the data distribution P."] }, right: { title: "Reverse KL", points: ["Average taken under the model distribution Q."] } }]]);
    const texts = claimsOf(spec).map((c) => c.text);
    expect(texts).toContain("Reverse KL: Average taken under the model distribution Q.");
    expect(texts).toContain("Forward KL: Average taken under the data distribution P.");
  });

  it("leaves top-level prose as it is", () => {
    const spec = page([[{ type: "text", md: "The loss falls as the learning rate grows a little." }]]);
    expect(claimsOf(spec).map((c) => c.text)).toContain("The loss falls as the learning rate grows a little.");
  });
});

describe("worked example values", () => {
  it("reads the numbers inside a lab and a bar chart", () => {
    const scene: Block = {
      type: "scene", title: "Lab", lesson: "l", caption: "", layout: "stack", state: [{ name: "n", label: "N", value: 4, min: 1, max: 10, step: 1 }],
      rules: [],
      defs: [{ name: "m", expr: "n * 2" }],
      elements: [
        { id: "r", kind: "readout", label: "Doubled", expr: "2 * k" },
        // Reads the lab's own counter (directly or through a def): an example's settings don't fix it, so it's left out.
        { id: "c", kind: "readout", label: "Counter", expr: "n * k" },
        { id: "d", kind: "readout", label: "Through a def", expr: "m + k" },
        { id: "b", kind: "bars", label: "Parts", values: "[k, -k]" },
      ],
    };
    const bars: Block = { type: "bars", title: "Terms", labels: ["a", "b"], values: ["k", "-k"], normalize: false };
    expect(stageFacts([scene, bars], { k: 0.5, seed: 0 })).toEqual([
      { label: "Doubled", value: "1" },
      { label: "Parts", value: "[0.5, -0.5]" },
      { label: "Terms: a", value: "0.5" },
      { label: "Terms: b", value: "-0.5" },
    ]);
  });
});
