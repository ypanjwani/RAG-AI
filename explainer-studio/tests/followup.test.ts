import { describe, expect, it } from "vitest";
import { buildFollowUp, compactOutline, continuationNote, designOf, keptOf, revisionRequest, sourceOf, threadSummary, widgetsOf } from "../shared/followup";
import { SAMPLE_DESIGN, SAMPLE_OUTLINE } from "../shared/sample";
import { parseOutline, type ExplainerSpec } from "../shared/spec";
import { assembleSpec } from "../shared/validate";

const spec = assembleSpec(SAMPLE_OUTLINE, SAMPLE_DESIGN, "Explainer text\n\nSource material:\nPaper summary here.");

describe("follow-up helpers", () => {
  it("turns a spec back into an outline the planner's parser accepts", () => {
    const o = compactOutline(spec);
    const r = parseOutline(JSON.parse(JSON.stringify(o)));
    expect("error" in r).toBe(false);
    if (!("error" in r)) {
      expect(r.warnings).toEqual([]);
      expect(r.outline.stages.map((s) => s.id)).toEqual(spec.stages.map((s) => s.id));
    }
    expect(JSON.stringify(o)).not.toMatch(/"warnings"|"chatbot"|"icon"/);
  });

  it("replaces AI-written widgets with a short note", () => {
    const withWidget: ExplainerSpec = { ...spec, stages: [{ ...spec.stages[0], blocks: [{ type: "widget", title: "Softmax lab", html: "<script>1</script>" }] }, ...spec.stages.slice(1)] };
    expect(compactOutline(withWidget).stages[0].blocks[0]).toEqual({ type: "text", md: "(interactive widget: Softmax lab)" });
  });

  it("keeps the previous design, icons included", () => {
    const { design } = designOf(spec);
    expect(design.theme).toBe(SAMPLE_DESIGN.theme);
    expect(design.fontPairing).toBe(SAMPLE_DESIGN.fontPairing);
    expect(design.stageIcons.length).toBe(spec.stages.length);
    const custom = designOf({ ...spec, design: { ...spec.design, theme: "custom", accent: "#ff8800", mode: "dark" } });
    expect(custom).toMatchObject({ design: { theme: "ocean" }, accent: "#ff8800", colorMode: "dark" });
  });

  it("reads the source excerpt from the chatbot context", () => {
    expect(sourceOf(spec)).toBe("Paper summary here.");
    expect(sourceOf({ ...spec, chatbot: { ...spec.chatbot, context: "no marker" } })).toBe("");
  });

  it("builds revise and new follow-ups, and the planner text that uses them", () => {
    const r = buildFollowUp("revise", spec, [spec]);
    expect(r?.kind).toBe("revise");
    if (r?.kind !== "revise") return;
    expect(JSON.parse(r.outline).title).toBe(spec.title);
    const text = revisionRequest("make it more advanced", r);
    expect(text).toContain("make it more advanced");
    expect(text).toContain(r.outline);
    expect(text).toContain("Paper summary here.");

    const n = buildFollowUp("new", spec, [spec]);
    expect(n?.kind).toBe("new");
    if (n?.kind !== "new") return;
    expect(n.previous).toBe(threadSummary([spec]));
    expect(n.previous).toContain(spec.title);
    expect(continuationNote(n)).toContain(spec.stages[0].label);
  });

  it("refuses to revise an explainer too large to send", () => {
    const huge = { ...spec, glossary: Array.from({ length: 2000 }, (_, i) => ({ term: `t${i}`, definition: "x".repeat(60) })) };
    expect(buildFollowUp("revise", huge, [huge])).toBeNull();
  });

  it("asks for only the changes when the server merges them, and the whole outline otherwise", () => {
    const r = buildFollowUp("revise", spec, [spec]);
    if (r?.kind !== "revise") throw new Error("expected a revision");
    expect(revisionRequest("add a section", r, true)).toContain('{"changes": {...}}');
    expect(revisionRequest("add a section", r)).toContain("complete revised outline as one JSON object");
  });

  it("keeps what the previous version wrote, so unchanged stages don't pay for it again", () => {
    const si = spec.stages.findIndex((s) => s.blocks.some((b) => b.type === "examples"));
    const bi = spec.stages[si].blocks.findIndex((b) => b.type === "examples");
    const withSteps: ExplainerSpec = {
      ...spec,
      stages: spec.stages.map((st, i) => i !== si ? st : { ...st, blocks: st.blocks.map((b, j) => j === bi && b.type === "examples" ? { ...b, items: b.items.map((it) => ({ ...it, steps: ["One.", "Two."] })) } : b) }),
      sectionNotes: { [spec.stages[0].id]: { deeper: "More." } },
    };
    const k = keptOf(withSteps)!;
    expect(k.steps?.[`${spec.stages[si].id}:${bi}:0`]).toEqual(["One.", "Two."]);
    expect(k.notes).toEqual({ [spec.stages[0].id]: { deeper: "More." } });
    expect(keptOf(spec)).toBeNull(); // nothing written yet
    const r = buildFollowUp("revise", withSteps, [withSteps]);
    expect(r?.kind === "revise" && r.kept).toEqual(k);
    // Too large: section extras go first, then everything.
    const big = { ...withSteps, sectionNotes: { a: { deeper: "x".repeat(70_000) } } };
    expect(keptOf(big)).toEqual({ steps: k.steps });
  });

  it("sends the previous widgets with a revision, by stage, and tells Haiku to keep their placeholders", () => {
    const widget = { type: "widget" as const, title: "Drag it", html: "<div></div><script>1</script>", plan: "{}" };
    const withWidget: ExplainerSpec = { ...spec, stages: spec.stages.map((st, i) => (i === 1 ? { ...st, blocks: [...st.blocks, widget] } : st)) };
    expect(widgetsOf(spec)).toBeNull();
    expect(widgetsOf(withWidget)).toEqual({ [spec.stages[1].id]: [widget] });
    const r = buildFollowUp("revise", withWidget, [withWidget]);
    if (r?.kind !== "revise") throw new Error("expected a revision");
    expect(r.widgets).toEqual({ [spec.stages[1].id]: [widget] });
    expect(r.outline).toContain("(interactive widget: Drag it)");
    expect(revisionRequest("x", r, true)).toContain("Keep every such block exactly as it is");
    const plain = buildFollowUp("revise", spec, [spec]);
    expect(plain?.kind === "revise" && plain.widgets).toBeFalsy();
    expect(revisionRequest("x", plain as Extract<typeof plain, { kind: "revise" }>, true)).not.toContain("interactive widget");
  });
});
