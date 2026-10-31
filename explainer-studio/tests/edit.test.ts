import { describe, expect, it } from "vitest";
import { addPreset, applyChanges, conversions, editContext, leaves, listOp, setAt, textChanges } from "../shared/edit";
import { SAMPLE_DESIGN, SAMPLE_OUTLINE } from "../shared/sample";
import type { Block } from "../shared/spec";
import { assembleSpec, checkEdit } from "../shared/validate";

const spec = assembleSpec(SAMPLE_OUTLINE, SAMPLE_DESIGN, "");
const find = <T extends Block["type"]>(type: T) => {
  for (const [s, st] of spec.stages.entries()) {
    const b = st.blocks.findIndex((x) => x.type === type);
    if (b >= 0) return { s, b, block: st.blocks[b] as Extract<Block, { type: T }> };
  }
  throw new Error(`sample has no ${type}`);
};

describe("checkEdit", () => {
  it("accepts every stage of the sample unchanged (edits start from a valid page)", () => {
    spec.stages.forEach((st, i) => expect(checkEdit(spec, i, st.blocks)).toMatchObject({ ok: true, notes: [] }));
  });

  it("applies a valid parameter change and leaves other stages untouched", () => {
    const { s, b, block } = find("plot");
    const r = checkEdit(spec, s, spec.stages[s].blocks.map((x, i) => (i === b ? setAt(block, ["title"], "New title") : x)));
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect((r.spec.stages[s].blocks[b] as typeof block).title).toBe("New title");
    r.spec.stages.forEach((st, i) => { if (i !== s) expect(st).toBe(spec.stages[i]); });
  });

  it("refuses an edit that breaks a part, with a reason", () => {
    const { s, b, block } = find("plot");
    const broken = setAt(block, ["series", 0, "expr"], "nosuchvar * x");
    const r = checkEdit(spec, s, spec.stages[s].blocks.map((x, i) => (i === b ? broken : x)));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toMatch(/nosuchvar|valid/);
  });

  it("refuses removing a slider that a chart still reads", () => {
    const { s, block } = find("slider");
    const users = spec.stages.flatMap((st) => st.blocks).filter((x) => x !== block && JSON.stringify(x).includes(block.var));
    expect(users.length).toBeGreaterThan(0);
    expect(checkEdit(spec, s, spec.stages[s].blocks.filter((x) => x !== block)).ok).toBe(false);
  });

  it("pulls an out-of-range slider value back and says so", () => {
    const { s, b, block } = find("slider");
    const r = checkEdit(spec, s, spec.stages[s].blocks.map((x, i) => (i === b ? { ...block, value: block.max + 100 } : x)));
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect((r.spec.stages[s].blocks[b] as typeof block).value).toBe(block.max);
      expect(r.notes.length).toBe(1);
    }
  });
});

describe("edit tools", () => {
  it("classifies values: formulas and variables are not text", () => {
    const { block } = find("plot");
    const kinds = new Map(leaves(block).map((l) => [l.path.join("."), l.kind]));
    expect(kinds.get("title")).toBe("text");
    expect(kinds.get("series.0.expr")).toBe("expr");
    expect(kinds.get("xMin")).toBe("number");
    const { block: callout } = find("callout");
    expect(leaves(callout).find((l) => l.path[0] === "variant")).toMatchObject({ kind: "enum", options: ["info", "analogy", "warning", "key"] });
    const { block: slider } = find("slider");
    expect(leaves(slider).find((l) => l.path[0] === "var")?.kind).toBe("id");
  });

  it("offers only conversions that pass the checker on the sample", () => {
    let offered = 0;
    spec.stages.forEach((st, s) => st.blocks.forEach((b, i) => {
      for (const c of conversions(b)) {
        offered++;
        const r = checkEdit(spec, s, st.blocks.map((x, j) => (j === i ? c.block : x)));
        expect(r.ok, `${b.type} → ${c.label}: ${r.ok ? "" : r.reason}`).toBe(true);
      }
    }));
    expect(offered).toBeGreaterThan(3);
  });

  it("list operations remove, move and duplicate items", () => {
    const { block } = find("quiz");
    const two = listOp(block, ["questions", 0], "duplicate");
    expect(two.questions.length).toBe(block.questions.length + 1);
    expect(listOp(two, ["questions", 0], "remove").questions.length).toBe(block.questions.length);
    const swapped = listOp({ ...block, questions: [block.questions[0], { ...block.questions[0], q: "B" }] }, ["questions", 1], "up");
    expect(swapped.questions[0].q).toBe("B");
  });

  it("adds a preset button after the stage's last control", () => {
    const { s, block } = find("slider");
    const blocks = spec.stages[s].blocks;
    const next = addPreset(blocks, 0, "Mine", [{ var: block.var, value: block.min }]);
    const at = next.findIndex((b) => b.type === "presets" && b.items.some((it) => it.label === "Mine"));
    expect(at).toBeGreaterThan(-1);
    expect(checkEdit(spec, s, next).ok).toBe(true);
  });
});

describe("Edit chat tools", () => {
  it("describes the selected part with editable paths, lists, forms and controls", () => {
    const { s, b, block } = find("plot");
    const ctx = editContext(spec, { stage: s, block: b }, () => 0.5);
    expect(ctx).toContain('["title"] text');
    expect(ctx).toContain('["series",0,"expr"] formula');
    expect(ctx).not.toMatch(/"var"|sweepVar/);
    expect(ctx).toContain("Other forms for this part (change_type option): 0 = ");
    expect(ctx).toContain(block.title);
  });

  it("applies set_values only to existing, editable paths with the right kind of value", () => {
    const { block: plot } = find("plot");
    const ok = applyChanges(plot, [{ path: ["xMax"], value: 9 }, { path: ["title"], value: "New" }]);
    expect(ok).toMatchObject({ textChanged: true });
    if ("block" in ok) expect(ok.block).toMatchObject({ xMax: 9, title: "New" });
    expect(applyChanges(plot, [{ path: ["xMax"], value: 9 }])).toMatchObject({ textChanged: false });
    expect(applyChanges(plot, [{ path: ["xMax"], value: "lots" }])).toHaveProperty("error");
    expect(applyChanges(plot, [{ path: ["nope"], value: 1 }])).toHaveProperty("error");
    const { block: slider } = find("slider");
    expect(applyChanges(slider, [{ path: ["var"], value: "z" }])).toHaveProperty("error");
    const { block: callout } = find("callout");
    expect(applyChanges(callout, [{ path: ["variant"], value: "loud" }])).toHaveProperty("error");
    expect(applyChanges(callout, [{ path: ["variant"], value: "key" }])).not.toHaveProperty("error");
  });

  it("lists text changes for the preview, including removed items", () => {
    const { block: cards } = find("cards");
    const fewer = { ...cards, items: cards.items.slice(1) };
    expect(textChanges(cards, fewer).some((c) => c.to === "")).toBe(true);
  });
});
