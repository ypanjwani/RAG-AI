// Registry: block type -> renderer. Adding a component = schema in
// shared/spec.ts + repair rule in shared/validate.ts + renderer here + CSS.

import type { Block, BlockType } from "../../../../shared/spec";
import { h } from "../dom";
import { architecture, attention, embedding, pipeline, sampling, tokens, training } from "./ai";
import { flipcards, predict, quiz, reveal } from "./cards";
import { chart } from "./chart";
import { scene } from "./scene";
import { bars, choice, plot, readout, slider } from "./charts";
import { calculator, heatmap, matrix, samples, trajectory } from "./math";
import { callout, compare, formula, tabs, text } from "./text";
import { cards, conceptmap, examples, experiment, presets, table, toggle } from "./tools";
import { widget } from "./widget";
import { tryCompile } from "../../../../shared/expr";
import type { Ctx, Renderer } from "./types";

const RENDERERS: { [T in BlockType]: Renderer<T> } = {
  text, callout, formula, tabs, compare,
  flipcards, reveal, quiz, predict,
  slider, choice, plot, bars, readout,
  samples, heatmap, trajectory, matrix, calculator,
  cards, toggle, presets, experiment, examples, table, conceptmap,
  tokens, attention, sampling, embedding, architecture, pipeline, training,
  chart, scene, widget,
};

export function renderBlock(b: Block, ctx: Ctx): HTMLElement {
  const fn = RENDERERS[b.type] as Renderer<BlockType> | undefined;
  try {
    if (!fn) throw new Error(`unknown block type ${b.type}`);
    const el = fn(b as never, ctx);
    el.classList.add("x-block");
    el.dataset.block = b.type;
    const uses = blockVars(b);
    if (uses.size) el.dataset.uses = [...uses].join(" ");
    return el;
  } catch (e) {
    console.error("block render failed", b, e);
    return h("div.x-block.x-block-error", null, "This part couldn't be displayed.");
  }
}

/** Blocks the reader must answer before the stage counts as complete. */
export function questionKeys(blocks: Block[], stageId: string): string[] {
  const keys: string[] = [];
  blocks.forEach((b, i) => {
    if (b.type === "quiz") b.questions.forEach((_, q) => keys.push(`${stageId}:${i}:${q}`));
    if (b.type === "predict") keys.push(`${stageId}:${i}:0`);
  });
  return keys;
}

export type { Ctx };

/** Variables a block reads: every string in it that compiles as an expression (lets "Try it" find its widget). */
function blockVars(b: Block): Set<string> {
  const out = new Set<string>();
  const walk = (v: unknown) => {
    if (typeof v === "string") {
      if (v.length > 300 || /\s{2}|[.!?]\s/.test(v)) return; // prose, not an expression
      const r = tryCompile(v.startsWith("=") ? v.slice(1) : v);
      if (r.ok) r.c.vars.forEach((x) => out.add(x));
    } else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
  };
  if (b.type !== "widget") walk(b); // a widget's code isn't made of our expressions
  return out;
}
