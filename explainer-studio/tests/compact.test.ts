import { describe, expect, it } from "vitest";
import { parseCompact, toCompact } from "../shared/compact";
import { SAMPLE_OUTLINE } from "../shared/sample";
import { LITE_DROPPED, parseOutline } from "../shared/spec";
import { LITE_SYSTEM, LITE_TYPES } from "../prompts/prompts";

const outlineOf = (raw: unknown) => {
  const r = parseOutline(raw);
  if ("error" in r) throw new Error(r.error);
  return r;
};

describe("compact format (Lite+)", () => {
  it("round-trips the sample outline (every block type) to the same outline", () => {
    const text = toCompact(SAMPLE_OUTLINE as unknown as Record<string, unknown>);
    const back = outlineOf(parseCompact(text));
    expect(back.warnings).toEqual([]);
    expect(back.outline).toEqual(outlineOf(SAMPLE_OUTLINE).outline);
  });

  // Measured with the token-counting API (Oct 2026): the sample is 8,519 tokens as minified JSON and
  // 8,235 compact; on a real reply, 6,166 as Haiku wrote it vs 5,555 compact for the same content.
  // Indentation costs characters but few tokens, so this checks the punctuation saved, not length.
  it("drops JSON's quotes, braces and commas", () => {
    const json = JSON.stringify(SAMPLE_OUTLINE);
    const text = toCompact(SAMPLE_OUTLINE as unknown as Record<string, unknown>);
    const punct = (s: string) => (s.match(/["{},]/g) ?? []).length;
    expect(punct(text)).toBeLessThan(punct(json) * 0.25);
  });

  it("accepts bracketed lists written out of JSON/YAML habit", () => {
    const raw = parseCompact("title: T\nstages:\n- id: s\n  label: S\n  blocks:\n  - type: experiment\n    repeats: [1, 10, 100]\n    outcomes: [mat, \"rug\", sofa]\n  - type: bars\n    values: [softmax(z, T), max(a, b)]");
    const [exp, bars] = (raw.stages as any[])[0].blocks;
    expect(exp.repeats).toEqual([1, 10, 100]);
    expect(exp.outcomes).toEqual(["mat", "rug", "sofa"]);
    expect(bars.values).toEqual(["softmax(z, T)", "max(a, b)"]);
  });

  it("Lite+ prompt: its syntax example reads with no warnings and it lists only static blocks", () => {
    const example = LITE_SYSTEM.split("it shows the format, not what to teach):\n")[1].split("\n\n# Fields")[0];
    const { outline, warnings } = outlineOf(parseCompact(example));
    expect(warnings).toEqual([]);
    expect(outline.stages[0].blocks.map((b) => b.type)).toEqual(["text", "formula", "plot", "table"]);
    for (const t of ["slider", "choice", "toggle", "presets", "widget", ...LITE_DROPPED]) {
      expect(LITE_TYPES).not.toContain(t);
      expect(LITE_SYSTEM).not.toContain(`- ${t}: `);
    }
    expect(LITE_SYSTEM).toContain("- plot: ");
  });

  it("takes values literally: colons, numbers as text, quotes, LaTeX", () => {
    const raw = parseCompact([
      "title: Attention: a primer",
      "stages:",
      "- id: s",
      "  label: 2",
      "  blocks:",
      "  - type: callout",
      "    variant: key",
      '    title: "Quoted: title"',
      "    md: Use \\sum_i once; no escaping needed.",
      "  - type: bars",
      "    title: Scores",
      "    labels: A | B",
      "    values: softmax([1, 2] / T)",
      "    normalize: true",
    ].join("\n"));
    expect(raw.title).toBe("Attention: a primer");
    const st = (raw.stages as any[])[0];
    expect(st.label).toBe("2");
    expect(st.blocks[0]).toEqual({ type: "callout", variant: "key", title: "Quoted: title", md: "Use \\sum_i once; no escaping needed." });
    expect(st.blocks[1]).toEqual({ type: "bars", title: "Scores", labels: ["A", "B"], values: ["softmax([1, 2] / T)"], normalize: true });
  });

  it("accepts lists indented under their key or at its indent, and keeps Markdown lists inside text", () => {
    const deep = parseCompact("title: T\nstages:\n  - id: s\n    label: S\n    blocks:\n      - type: text\n        md: |\n          Two kinds:\n          - one\n          - two\n");
    const flat = parseCompact("title: T\nstages:\n- id: s\n  label: S\n  blocks:\n  - type: text\n    md: |\n      Two kinds:\n      - one\n      - two\n");
    expect(deep).toEqual(flat);
    expect((flat.stages as any[])[0].blocks[0].md).toBe("Two kinds:\n- one\n- two");
  });

  it("puts a list item's deeper lines under its first field", () => {
    const raw = parseCompact([
      "title: T",
      "stages:",
      "- id: s",
      "  label: S",
      "  blocks:",
      "  - type: quiz",
      "    questions:",
      "    - q: Which?",
      "      choices:",
      "      - a: with colon",
      "      - b",
      "      answer: 0",
      "      explain: Because.",
    ].join("\n"));
    expect((raw.stages as any[])[0].blocks[0].questions).toEqual([{ q: "Which?", choices: ["a: with colon", "b"], answer: 0, explain: "Because." }]);
  });

  it("fills empty defaults, drops a broken block, and strips a code fence", () => {
    const { outline, warnings } = outlineOf(parseCompact([
      "```",
      "title: T",
      "stages:",
      "- id: s",
      "  label: S",
      "  blocks:",
      "  - type: plot",
      "    title: P",
      "    xMin: 0",
      "    xMax: 1",
      "    yMin: 0",
      "    yMax: 0",
      "    series:",
      "    - label: y",
      "      expr: x",
      "  - type: slider",
      "    var: k",
      "    label: K",
      "    min: low",
      "```",
    ].join("\n")));
    expect(outline.stages[0].blocks).toEqual([{ type: "plot", title: "P", xLabel: "", yLabel: "", xMin: 0, xMax: 1, yMin: 0, yMax: 0, series: [{ label: "y", expr: "x" }], markerX: "" }]);
    expect(warnings.some((w) => w.includes('"slider"'))).toBe(true);
  });

  it("reads a matrix written as one JSON line", () => {
    const raw = parseCompact("title: T\nstages:\n- id: s\n  label: S\n  blocks:\n  - type: attention\n    tokens: a | b\n    weights: [[1, 0.2], [0.5, 1]]");
    expect((raw.stages as any[])[0].blocks[0].weights).toEqual([[1, 0.2], [0.5, 1]]);
  });

  it("moves glossary entries that landed in the stage list back to the glossary", () => {
    const misplaced = "title: T\nstages:\n- id: s\n  label: S\n  blurb: B\n  glossary:\n- term: token\n  definition: A piece of text.\n- term: logit\n  definition: A raw score.";
    const raw = parseCompact(misplaced);
    expect((raw.stages as any[]).map((s) => s.id)).toEqual(["s"]);
    expect(raw.glossary).toEqual([{ term: "token", definition: "A piece of text." }, { term: "logit", definition: "A raw score." }]);
  });

  it("rejects a reply with nothing to read", () => {
    expect(() => parseCompact("Sorry, I can't help with that.")).toThrow();
  });
});
