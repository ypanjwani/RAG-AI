import { describe, expect, it } from "vitest";
import { LITE_TYPES, OUTLINE_CHART_SYSTEM, OUTLINE_SYSTEM } from "../prompts/prompts";
import { claimsOf } from "../shared/factcheck";
import { SAMPLE_DESIGN, SAMPLE_OUTLINE } from "../shared/sample";
import type { Block, Outline } from "../shared/spec";
import { assembleSpec, checkEdit } from "../shared/validate";

type ChartB = Extract<Block, { type: "chart" }>;

const SLIDERS: Block[] = [
  { type: "slider", var: "k", label: "Rate k", min: 0, max: 2, step: 0.1, value: 1, unit: "" },
  { type: "slider", var: "n", label: "Rows n", min: 5, max: 500, step: 5, value: 100, unit: "" },
  { type: "choice", var: "mode", label: "Mode", options: [{ label: "A", value: 0 }, { label: "B", value: 1 }], value: 0 },
];

const chart = (over: Partial<ChartB>): ChartB => ({
  type: "chart", title: "c", lesson: "what to notice", series: [{ label: "y", expr: "k * n" }],
  x: "", by: "", at: [], view: "line", labels: [], caption: "", ...over,
});

const outlineWith = (blocks: Block[]): Outline => ({
  ...SAMPLE_OUTLINE,
  stages: [{ id: "m", label: "M", phase: "read", blurb: "", blocks: [{ type: "text", md: "x" }, ...SLIDERS, ...blocks] }],
});

const run = (...blocks: Block[]) => {
  const spec = assembleSpec(outlineWith(blocks), SAMPLE_DESIGN, "");
  const charts = (spec.stages[0]?.blocks ?? []).filter((b): b is ChartB => b.type === "chart");
  return { charts, warnings: spec.warnings };
};

describe("chart block", () => {
  it("keeps a valid line chart without warnings", () => {
    const { charts, warnings } = run(chart({ x: "k", view: "line" }));
    expect(warnings).toEqual([]);
    expect(charts[0].view).toBe("line");
  });

  it("replaces a view that doesn't fit the formula's shape, with a warning", () => {
    const { charts, warnings } = run(chart({ x: "k", view: "histogram" }));
    expect(charts[0].view).toBe("line");
    expect(warnings).toContain('Chart "c": the "histogram" view doesn\'t fit a formula that gives one number; showing "line".');
  });

  it("sends a random formula to a random view", () => {
    const { charts } = run(chart({ series: [{ label: "draw", expr: "normal(k, 1)" }], x: "k", view: "line" }));
    expect(charts[0].view).toBe("histogram");
  });

  it("drops a number chart with no slider for its x axis", () => {
    const { charts, warnings } = run(chart({ view: "line" }));
    expect(charts).toEqual([]);
    expect(warnings).toContain('Dropped chart "c": a formula that gives one number needs a slider variable on its x axis.');
  });

  it("only accepts sliders as axes", () => {
    const { charts, warnings } = run(chart({ x: "mode", view: "line" }));
    expect(charts).toEqual([]);
    expect(warnings).toContain('Chart "c": "mode" is not a slider, so it can\'t be the x axis.');
    const unknown = run(chart({ x: "nope", by: "k", view: "lines" }));
    expect(unknown.warnings).toContain('Chart "c": "nope" is not a control variable, so it can\'t be the x axis.');
  });

  it("fills and clamps the values a compare or lines chart shows", () => {
    const random = [{ label: "mean", expr: "mean(normal(k, 1, n))" }];
    expect(run(chart({ series: random, by: "n", view: "compare" })).charts[0].at).toEqual([5, 500]);
    expect(run(chart({ series: random, by: "n", at: [1, 50, 9000], view: "compare" })).charts[0].at).toEqual([5, 500]);
    expect(run(chart({ series: random, by: "n", at: [20], view: "compare" })).charts[0].at).toEqual([20, 500]);
    expect(run(chart({ x: "k", by: "n", view: "lines" })).charts[0].at).toEqual([5, 252.5, 500]);
    expect(run(chart({ x: "k", by: "n", at: [10, 20, 30, 40, 50], view: "lines" })).charts[0].at).toEqual([10, 20, 30, 40]);
  });

  it("warns about a typed-in answer and about a chart that ignores the page", () => {
    const typed = run(chart({ series: [{ label: "left out", expr: "mean(bernoulli(0.366032, n))" }], view: "running" }));
    expect(typed.charts[0].view).toBe("running");
    expect(typed.warnings).toContain('The chart "c" types the number 0.366032; computing it from the variables keeps it in step with the page.');
    const ignores = run(chart({ series: [{ label: "left out", expr: "mean(bernoulli(0.37, 100))" }], view: "histogram" }));
    expect(ignores.warnings).toContain('Chart "c" reads none of the page\'s controls, so nothing on the page changes it.');
  });

  it("warns on a flat chart and drops series that never work", () => {
    const { charts, warnings } = run(chart({ x: "k", series: [{ label: "flat", expr: "3" }, { label: "bad", expr: "nope + 1" }] }));
    expect(charts[0].series.map((s) => s.label)).toEqual(["flat"]);
    expect(warnings).toContain('Dropped chart series "bad": unknown variable "nope"');
    expect(warnings.some((w) => w.startsWith('Chart "c" is flat'))).toBe(true);
  });

  it("draws a list formula as bars", () => {
    const { charts, warnings } = run(chart({ series: [{ label: "p", expr: "softmax([1, 2, 3], k)" }], labels: ["a", "b", "c"], view: "line" }));
    expect(charts[0].view).toBe("bars");
    expect(warnings[0]).toContain('showing "bars"');
  });

  it("keeps scatter, band and box when they have what they need", () => {
    const random = [{ label: "draw", expr: "normal(k, 1)" }];
    expect(run(chart({ series: random, x: "k", view: "scatter" })).charts[0].view).toBe("scatter");
    expect(run(chart({ series: [...random, { label: "other", expr: "normal(0, k)" }], view: "scatter" })).charts[0].view).toBe("scatter");
    expect(run(chart({ series: random, x: "k", view: "band" })).charts[0].view).toBe("band");
    const box = run(chart({ series: [{ label: "mean", expr: "mean(normal(k, 1, n))" }], by: "n", view: "box" }));
    expect(box.charts[0].view).toBe("box");
    expect(box.warnings).toEqual([]);
  });

  it("falls back when scatter, band or box lack an axis", () => {
    const random = [{ label: "draw", expr: "normal(k, 1)" }];
    // One series and no x: nothing to put on the horizontal axis.
    const scatter = run(chart({ series: random, view: "scatter" }));
    expect(scatter.charts[0].view).toBe("histogram");
    expect(scatter.warnings).toContain('Chart "c": the "scatter" view doesn\'t fit a random formula; showing "histogram".');
    expect(run(chart({ series: random, view: "band" })).charts[0].view).toBe("histogram");
    expect(run(chart({ series: random, x: "k", view: "box" })).charts[0].view).toBe("histogram");
    // A number formula can't be a random view.
    expect(run(chart({ x: "k", view: "band" })).charts[0].view).toBe("line");
  });

  it("fills and clamps the values a box chart shows, like lines", () => {
    const random = [{ label: "mean", expr: "mean(normal(k, 1, n))" }];
    expect(run(chart({ series: random, by: "n", view: "box" })).charts[0].at).toEqual([5, 252.5, 500]);
    expect(run(chart({ series: random, by: "n", at: [1, 50, 100, 200, 9000], view: "box" })).charts[0].at).toEqual([5, 50, 100, 200]);
  });

  it("draws two lists side by side as grouped bars, and one list as bars", () => {
    const two = [{ label: "T = 1", expr: "softmax([1, 2, 3], 1)" }, { label: "your T", expr: "softmax([1, 2, 3], k)" }];
    const grouped = run(chart({ series: two, labels: ["a", "b", "c"], view: "groupedbars" }));
    expect(grouped.charts[0].view).toBe("groupedbars");
    expect(grouped.warnings).toEqual([]);
    const one = run(chart({ series: two.slice(1), labels: ["a", "b", "c"], view: "groupedbars" }));
    expect(one.charts[0].view).toBe("bars");
    expect(one.warnings[0]).toContain('showing "bars"');
  });

  it("Edit chat refuses an edit that breaks a chart's view", () => {
    const spec = assembleSpec(outlineWith([chart({ x: "k", by: "n", view: "lines" })]), SAMPLE_DESIGN, "");
    const blocks = spec.stages[0].blocks;
    const ok = checkEdit(spec, 0, blocks.map((b) => (b.type === "chart" ? { ...b, title: "renamed" } : b)));
    expect(ok.ok).toBe(true);
    const bad = checkEdit(spec, 0, blocks.map((b) => (b.type === "chart" ? { ...b, view: "histogram" as const } : b)));
    expect(bad.ok).toBe(false);
  });

  it("keeps the lesson out of the fact check (it isn't shown to readers)", () => {
    const spec = assembleSpec(outlineWith([chart({ x: "k", lesson: "Secret lesson sentence.", caption: "A caption the reader sees." })]), SAMPLE_DESIGN, "");
    const texts = claimsOf(spec).map((c) => c.text).join("\n");
    expect(texts).toContain("A caption the reader sees.");
    expect(texts).not.toContain("Secret lesson sentence.");
  });

  it("is offered to the planner only behind the CHART_BLOCK switch", () => {
    expect(OUTLINE_SYSTEM).not.toContain('"chart"');
    expect(OUTLINE_CHART_SYSTEM).toContain('"chart"');
    expect(OUTLINE_CHART_SYSTEM).toContain("- chart: use it for EVERY graph of a formula and EVERY random process");
    // The chart variant sends numeric random results to chart; the normal prompt keeps the old guidance.
    expect(OUTLINE_CHART_SYSTEM).toContain("- experiment: only for a random process with categorical outcomes");
    expect(OUTLINE_CHART_SYSTEM).toContain("→ chart: histogram for one setting");
    expect(OUTLINE_SYSTEM).toContain("- experiment: a repeatable random process");
    expect(OUTLINE_SYSTEM).toContain("→ samples (defs draw and summarise");
    expect(LITE_TYPES).not.toContain("chart");
  });
});
