import { describe, expect, it } from "vitest";
import { briefsOf, mergeBundles, parseLabOutline } from "../shared/lab";
import { SAMPLE_DESIGN, SAMPLE_OUTLINE } from "../shared/sample";
import { Outline, type Block } from "../shared/spec";
import { assembleSpec } from "../shared/validate";

describe("briefsOf / mergeBundles", () => {
  it("swaps each brief for its built blocks", () => {
    const raw = { ...SAMPLE_OUTLINE, stages: [{ id: "s", label: "S", phase: "read", blurb: "", blocks: [
      { type: "text", md: "Intro." },
      { type: "interactive", goal: "g1", math: "m1", insight: "i1" },
      { type: "interactive", goal: "g2", math: "m2", insight: "i2" },
    ] }] };
    const parsed = parseLabOutline(raw);
    if (!("outline" in parsed)) throw new Error(parsed.error);
    const refs = briefsOf(parsed.outline);
    expect(refs.map((r) => r.key)).toEqual(["b1", "b2"]);
    const built: Block[] = [{ type: "text", md: "Built one." }];
    const merged = mergeBundles(parsed.outline, new Map([["b1", built]]));
    expect(Outline.safeParse(merged).success).toBe(true);
    expect(merged.stages[0].blocks).toEqual([{ type: "text", md: "Intro." }, built[0]]);
    const spec = assembleSpec(merged, SAMPLE_DESIGN, "");
    expect(spec.stages[0].blocks.length).toBe(2);
  });
});

describe("parseLabOutline", () => {
  it("accepts briefs, written blocks and direct visuals; drops junk", () => {
    const raw = { ...SAMPLE_OUTLINE, stages: [{ id: "s", label: "S", phase: "read", blurb: "", blocks: [
      { type: "interactive", goal: "g", math: "m", insight: "i", scenarios: ["a"] },
      { type: "tip", title: "Read it", md: "x" },
      { type: "bars", title: "", labels: ["a"], values: ["1"], normalize: false },
      { type: "hologram" },
    ] }] };
    const r = parseLabOutline(raw);
    if (!("outline" in r)) throw new Error(r.error);
    expect(r.outline.stages[0].blocks.map((b) => b.type)).toEqual(["interactive", "cards", "bars"]);
    expect(r.warnings.length).toBe(1);
  });

  it("reports the block's own error for a bad non-brief block, not the brief's", () => {
    const raw = { ...SAMPLE_OUTLINE, stages: [{ id: "s", label: "S", phase: "read", blurb: "", blocks: [{ type: "table", rows: "nope" }] }] };
    const r = parseLabOutline(raw);
    if (!("outline" in r)) throw new Error(r.error);
    expect(r.warnings[0]).toContain('"table"');
    expect(r.warnings[0]).not.toContain("interactive");
  });
});
