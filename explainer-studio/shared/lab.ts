// Freeform+ planning: the planner writes an `interactive` brief (goal + math + insight)
// wherever the page needs live math or a visual, and Haiku then writes each brief as a
// sandboxed widget. This file holds the pure parts: the brief schema, the planner's
// outline schema, finding briefs and merging the built blocks back.

import { z } from "zod";
import { Block, BlockSchemas, coerceOutline, normalizeBlock, Outline, Stage, type BlockType } from "./spec";

export const Brief = z.object({
  type: z.literal("interactive"),
  goal: z.string().describe("What the reader should discover by playing with it."),
  math: z.string().describe("The model behind it, concretely: formulas, every variable with its range and default, data values, random draws or update rules. Self-contained: name every variable it needs."),
  insight: z.string().describe("The takeaway the reader should end with."),
  scenarios: z.array(z.string()).optional().describe("Optional worked-example ideas, e.g. 'the paper's setting r = 8' or 'a learning rate that diverges'."),
}).describe("A live, interactive part of the page described by its purpose and math; the component is chosen and built for you.");
export type Brief = z.infer<typeof Brief>;

/** Blocks the planner still writes itself in Freeform+ (prose, cards, quizzes, static tables, maps). */
export const LAB_WRITTEN: BlockType[] = [
  "text", "callout", "formula", "flipcards", "reveal", "tabs", "compare", "quiz", "predict", "cards", "table", "conceptmap",
];

const LabBlock = z.union([...LAB_WRITTEN.map((t) => BlockSchemas[t]), Brief] as unknown as [z.ZodTypeAny, z.ZodTypeAny, ...z.ZodTypeAny[]]);

/** The outline schema shown to the Freeform+ planner (much smaller than the full one). */
export const LabOutline = Outline.extend({
  stages: z.array(Stage.extend({ blocks: z.array(LabBlock) })).describe("4-8 stages in teaching order, or the number the request asks for"),
});

export type LabStageBlock = Block | Brief;
export type LabOutlineT = Omit<Outline, "stages"> & { stages: (Omit<Outline["stages"][number], "blocks"> & { blocks: LabStageBlock[] })[] };

/** Parse the Freeform+ planner's reply block by block (bad blocks are dropped with a warning). */
export function parseLabOutline(raw: unknown): { outline: LabOutlineT; warnings: string[] } | { error: string } {
  const loose = coerceOutline(raw);
  if ("error" in loose) return loose;
  const warnings = loose.warnings;
  const stages = loose.data.stages.map((st) => ({
    ...st,
    blocks: st.blocks.flatMap((b, i): LabStageBlock[] => {
      const n = normalizeBlock(b);
      const brief = Brief.safeParse(n);
      if (brief.success) return [brief.data];
      // Any regular block is accepted too (e.g. the planner wrote a plot directly).
      const block = Block.safeParse(n);
      if (block.success) return [block.data];
      const type = (b as { type?: unknown })?.type;
      // Report the error that matters: the brief's for briefs, the block's for everything else.
      const err = type === "interactive" ? brief.error : block.error;
      const issue = err.issues[0];
      warnings.push(`Dropped block ${i + 1} ("${typeof type === "string" ? type : "unknown"}") in "${st.label}": ${issue ? `${issue.path.join(".") || "block"}: ${issue.message}` : "invalid"}.`);
      return [];
    }),
  }));
  return { outline: { ...loose.data, stages }, warnings };
}

export interface BriefRef { key: string; stageIndex: number; blockIndex: number; stageLabel: string; brief: Brief }

export function briefsOf(o: LabOutlineT): BriefRef[] {
  const out: BriefRef[] = [];
  o.stages.forEach((st, si) => st.blocks.forEach((b, bi) => {
    if (b.type === "interactive") out.push({ key: `b${out.length + 1}`, stageIndex: si, blockIndex: bi, stageLabel: st.label, brief: b as Brief });
  }));
  return out;
}

/** Swap every brief for its built blocks (or drop it) to get a normal outline. */
export function mergeBundles(o: LabOutlineT, bundles: Map<string, Block[]>): Outline {
  const refs = briefsOf(o);
  const keyAt = new Map(refs.map((r) => [`${r.stageIndex}:${r.blockIndex}`, r.key]));
  return {
    ...o,
    stages: o.stages.map((st, si) => ({
      ...st,
      blocks: st.blocks.flatMap((b, bi) => (b.type === "interactive" ? bundles.get(keyAt.get(`${si}:${bi}`)!) ?? [] : [b as Block])),
    })),
  };
}
