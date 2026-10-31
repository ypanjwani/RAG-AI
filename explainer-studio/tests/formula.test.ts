// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { renderExplainer } from "../app/src/render/explainer";
import { SAMPLE_DESIGN, SAMPLE_OUTLINE } from "../shared/sample";
import { formulaKey, type Block, type ExplainerSpec, type FormulaNote } from "../shared/spec";
import { assembleSpec } from "../shared/validate";

// jsdom has no IntersectionObserver (the article layout's scroll-spy), scrollIntoView or matchMedia.
globalThis.IntersectionObserver ??= class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof IntersectionObserver;
Element.prototype.scrollIntoView ??= function () {};
window.matchMedia ??= (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;

const F: Block = { type: "formula", latex: "a = b \\cdot c", caption: "A product", terms: [{ symbol: "b", meaning: "first" }] };
const NOTE: FormulaNote = {
  idea: "It multiplies two things.", terms: [{ symbol: "b", name: "First", meaning: "A number.", role: "Scales a." }],
  origin: ["Start with $b$.", "Multiply by $c$."], shape: "A product, because both grow a.", example: ["$2 \\cdot 3 = 6$."], pitfall: "Not a sum.",
};

const page = (extra: Partial<ExplainerSpec> = {}): ExplainerSpec => ({
  ...assembleSpec({ ...SAMPLE_OUTLINE, layout: "article", stages: [{ id: "s0", label: "Stage 1", phase: SAMPLE_OUTLINE.phases[0].id, blurb: "", blocks: [{ type: "text", md: "Intro." }, F] }] }, SAMPLE_DESIGN, ""),
  ...extra,
});

function render(spec: ExplainerSpec, load?: () => Promise<FormulaNote>) {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  renderExplainer(root, { spec, progress: { visited: [], answers: {} }, onProgress: () => {}, href: () => "#", stageId: "s0",
                          loadFormulaNote: load ? () => load() : undefined });
  return root;
}

const button = (root: HTMLElement) => root.querySelector<HTMLButtonElement>(".x-formula-deeper");

describe("Go deeper on formulas", () => {
  it("keys notes by the formula's LaTeX, ignoring spacing", () => {
    expect(formulaKey("  a =  b\n\\cdot c ")).toBe("a = b \\cdot c");
  });

  it("shows the button in the app, and in exports only for formulas with a saved note", () => {
    expect(button(render(page(), async () => NOTE))).not.toBeNull();
    expect(button(render(page()))).toBeNull();  // export, nothing saved
    expect(button(render(page({ formulaNotes: { [formulaKey("a = b \\cdot c")]: NOTE } })))).not.toBeNull();
    expect(button(render(page({ noControls: true }), async () => NOTE))).toBeNull();  // Quick read
  });

  it("opens a saved note at once, and writes one on the first click", async () => {
    const saved = render(page({ formulaNotes: { [formulaKey(F.type === "formula" ? F.latex : "")]: NOTE } }));
    button(saved)!.click();
    const panel = document.querySelector(".x-fd-panel")!;
    expect(panel.textContent).toContain("It multiplies two things.");
    expect(panel.textContent).toContain("Where it comes from");
    expect(panel.textContent).toContain("Common mix-up");

    let calls = 0;
    const root = render(page(), async () => { calls++; return NOTE; });
    button(root)!.click();
    await new Promise((r) => setTimeout(r, 0));
    expect(calls).toBe(1);
    expect(document.querySelector(".x-fd-panel")!.textContent).toContain("Try it with numbers");
  });
});
