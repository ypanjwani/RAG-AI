import { describe, expect, it } from "vitest";
import { cutReferences, isReferenceLine } from "../shared/references";

const body = (n: number, word = "The model learns a representation of the input and we evaluate it carefully.") =>
  Array.from({ length: n }, () => word).join("\n");
const ieee = (n: number) => Array.from({ length: n }, (_, i) =>
  `[${i + 1}] M. Islam, M. Hossain, and M. A. Rahman, "Some Method for Plant Disease," IEEE Access, vol. 11, pp. ${i}-${i + 9}, 2023.`).join("\n");
const authorYear = (n: number) => Array.from({ length: n }, (_, i) =>
  `Josh Achiam, Steven Adler, et al. 202${i % 10}. A technical report on models. arXiv preprint arXiv:2303.0${i}.`).join("\n");

describe("isReferenceLine", () => {
  it("recognises IEEE and author-year entries, including dotted abbreviations", () => {
    expect(isReferenceLine('Disease Detection, IEEE Access, vol. 11, pp. 34721-34733, 2023.')).toBe(true);
    expect(isReferenceLine("Josh Achiam, Steven Adler, et al. 2023. Gpt-4 technical report.")).toBe(true);
    expect(isReferenceLine("[7] S. A. Khan, M. Hussain, and F. R. Baig, An Effective Deep Learning")).toBe(true);
    expect(isReferenceLine("The model learns a representation of the input and we evaluate it.")).toBe(false);
  });
});

describe("cutReferences", () => {
  it("cuts at a References section heading, as before", () => {
    const s = [
      { heading: "Abstract", text: body(5) }, { heading: "Intro", text: body(5) }, { heading: "Method", text: body(5) },
      { heading: "Results", text: body(5) }, { heading: "References", text: ieee(20) }, { heading: "Appendix A", text: body(5) },
    ];
    const r = cutReferences(s);
    expect(r.by).toBe("heading");
    expect(r.sections.map((x) => x.heading)).toEqual(["Abstract", "Intro", "Method", "Results"]);
  });

  it("finds a References line inside a section and keeps the conclusion before it", () => {
    const s = [{ heading: "Intro", text: body(40) }, { heading: "Results (cont.)", text: `${body(10, "In conclusion, the method is useful for agriculture worldwide.")}\nREFERENCES\n${ieee(20)}` }];
    const r = cutReferences(s);
    expect(r.by).toBe("title line");
    expect(r.sections[1].text).toContain("In conclusion");
    expect(r.sections[1].text).not.toContain("REFERENCES");
    expect(r.removed).toBeGreaterThan(1000);
  });

  it("ignores a 'References' label that isn't followed by entries (a figure or table)", () => {
    const s = [
      { heading: "Intro", text: body(40) },
      { heading: "Methods (cont.)", text: `Noise\nDatabase\nRetrieval\nReferences\nReverse\nForward\n${body(40)}` },
      { heading: "Apps (cont.)", text: body(20) },
    ];
    expect(cutReferences(s).removed).toBe(0);
  });

  it("finds an unlabelled author-year list by its content", () => {
    const s = [{ heading: "Beginning", text: body(60) }, { heading: "Beginning (cont.)", text: `${body(5)}\n${authorYear(30)}` }];
    const r = cutReferences(s);
    expect(r.by).toBe("content");
    expect(r.sections[1].text).not.toMatch(/Achiam/);
  });

  it("never cuts early citation-heavy text (related work in the first 40%)", () => {
    const s = [{ heading: "Related work", text: authorYear(20) }, { heading: "Method", text: body(80) }];
    expect(cutReferences(s).removed).toBe(0);
  });

  it("leaves a paper without references unchanged", () => {
    const s = [{ heading: "A", text: body(30) }, { heading: "B", text: body(30) }];
    expect(cutReferences(s)).toEqual({ sections: s, removed: 0 });
  });
});
