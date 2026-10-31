import { describe, expect, it } from "vitest";
import { SAMPLE_DESIGN, SAMPLE_OUTLINE } from "../shared/sample";
import { Block, PromptBlock, type Outline } from "../shared/spec";
import { assembleSpec, WIDGET_MAX_CHARS } from "../shared/validate";
import { LAB_SYSTEM, OUTLINE_SYSTEM } from "../prompts/prompts";

const W = '<div id="o"></div><script>document.getElementById("o").textContent = "1";</script>';

describe("freeform widgets", () => {
  it("keeps a valid widget and drops empty, script-less or oversized ones", () => {
    const stageWith = (blocks: Block[]): Outline => ({ ...SAMPLE_OUTLINE, stages: [{ ...SAMPLE_OUTLINE.stages[0], blocks }] });
    const kept = (b: Block) => assembleSpec(stageWith([b]), SAMPLE_DESIGN, "").stages[0]?.blocks.some((x) => x.type === "widget") ?? false;
    expect(kept({ type: "widget", title: "ok", html: W })).toBe(true);
    expect(kept({ type: "widget", title: "no script", html: "<div>static</div>" })).toBe(false);
    expect(kept({ type: "widget", title: "empty", html: "  " })).toBe(false);
    expect(kept({ type: "widget", title: "big", html: W + "x".repeat(WIDGET_MAX_CHARS) })).toBe(false);
  });

  it("never offers widgets to the planner", () => {
    expect(PromptBlock.safeParse({ type: "widget", title: "t", html: W }).success).toBe(false);
    expect(OUTLINE_SYSTEM).not.toContain('"widget"');
    expect(LAB_SYSTEM).not.toContain('"widget"');
  });
});
