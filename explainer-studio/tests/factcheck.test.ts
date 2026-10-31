import { describe, expect, it } from "vitest";
import { applyFixes, claimsOf, pageValues, sentences } from "../shared/factcheck";
import { SAMPLE_DESIGN, SAMPLE_OUTLINE } from "../shared/sample";
import type { Block, ExplainerSpec, Outline } from "../shared/spec";
import { assembleSpec } from "../shared/validate";

const page = (blocks: Block[]): ExplainerSpec => {
  const outline: Outline = { ...SAMPLE_OUTLINE, stages: [{ id: "s", label: "Stage one", phase: "read", blurb: "", blocks }] };
  return assembleSpec(outline, SAMPLE_DESIGN, "");
};

const text: Block = { type: "text", md: "Dividing by p again at test time shrinks the outputs. **Dropout** zeroes units at random during training.\n\nTry the slider below to see it." };
const quiz: Block = {
  type: "quiz",
  questions: [{ q: "At step 1 with beta = 0.9, what is the correction factor?", choices: ["0.1", "10", "1"], answer: 0, explain: "It is 1/(1-0.9) = 10." }],
};
const slider: Block = { type: "slider", var: "b1", label: "Beta one", min: 0, max: 0.99, step: 0.01, value: 0.9, unit: "" };
const readout: Block = { type: "readout", defs: [], items: [{ label: "Correction factor", expr: "1/(1-b1)", digits: 1, kind: "number" }] };

describe("claims", () => {
  it("splits prose into sentences and skips instructions to the reader", () => {
    expect(sentences(text.type === "text" ? text.md : "")).toEqual([
      "Dividing by p again at test time shrinks the outputs.",
      "**Dropout** zeroes units at random during training.",
    ]);
  });

  it("turns a quiz question into one claim with the marked answer", () => {
    const c = claimsOf(page([quiz])).find((x) => x.question);
    expect(c?.text).toContain("Marked correct: A) 0.1");
    expect(c?.text).toContain("Explanation: It is 1/(1-0.9) = 10.");
  });

  it("covers the sample explainer without live table cells", () => {
    const spec = assembleSpec(SAMPLE_OUTLINE, SAMPLE_DESIGN, "");
    const claims = claimsOf(spec);
    expect(claims.length).toBeGreaterThan(10);
    expect(claims.some((c) => c.question)).toBe(true);
    expect(claims.some((c) => /\|\s*[^|]*:\s*=/.test(c.text))).toBe(false);
  });

  it("marks a mistake card's title as a misconception and never edits it", () => {
    const cards: Block = { type: "cards", variant: "mistake", title: "", items: [{ title: "Bigger is always better", md: "Only if the data grows too." }] };
    const c = claimsOf(page([cards]))[0];
    expect(c.text).toContain('Misconception (shown on purpose, to be corrected): "Bigger is always better"');
    expect(c.fields).toEqual([["items", 0, "md"]]);
  });
});

describe("page values", () => {
  it("lists controls and computed numbers at the starting settings", () => {
    const v = pageValues(page([slider, readout]));
    expect(v).toContain('control "Beta one" (b1) starts at 0.9');
    expect(v).toContain('readout "Correction factor" = 10');
  });
});

describe("applying fixes", () => {
  const spec = page([text, quiz]);
  const claims = claimsOf(spec);
  const shrink = claims.find((c) => c.text.includes("shrinks"))!;
  const q = claims.find((c) => c.question)!;

  it("replaces a sentence fragment inside the text", () => {
    const r = applyFixes(spec, claims, [{ id: shrink.id, edits: [{ find: "shrinks the outputs", replace: "makes the outputs too large" }] }]);
    expect((r.spec.stages[0].blocks[0] as Extract<Block, { type: "text" }>).md).toContain("Dividing by p again at test time makes the outputs too large.");
    expect(r.applied).toHaveLength(1);
  });

  it("changes a quiz's marked answer", () => {
    const r = applyFixes(spec, claims, [{ id: q.id, edits: [], answer: 1 }]);
    expect((r.spec.stages[0].blocks[1] as Extract<Block, { type: "quiz" }>).questions[0].answer).toBe(1);
    expect(r.applied[0].to).toBe("marked answer B");
  });

  it("skips a fix whose text isn't in the claim, and leaves the page alone", () => {
    const r = applyFixes(spec, claims, [{ id: shrink.id, edits: [{ find: "not on the page", replace: "x" }] }, { id: "nope", edits: [] }]);
    expect(r.spec.stages[0].blocks).toEqual(spec.stages[0].blocks);
    expect(r.skipped.map((s) => s.id)).toEqual([shrink.id, "nope"]);
  });

  it("rejects an answer outside the choices", () => {
    const r = applyFixes(spec, claims, [{ id: q.id, edits: [], answer: 7 }]);
    expect(r.skipped[0].why).toBe("invalid answer");
  });
});
