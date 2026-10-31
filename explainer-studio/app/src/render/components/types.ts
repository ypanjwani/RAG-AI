import type { Block, BlockType } from "../../../../shared/spec";
import type { Store } from "../engine";

export interface Ctx {
  store: Store;
  /** Unique key of this block: "<stageId>:<index>". */
  key: string;
  /** Saved answer for a question key, if any. */
  answer(qkey: string): number | undefined;
  /** Record an answer (persists progress and may complete the stage). */
  setAnswer(qkey: string, choice: number): void;
  /** Register cleanup (store subscriptions, timers) for when the page re-renders. */
  onDispose(fn: () => void): void;
  /** Link to a stage of this explainer. */
  href(stageId: string): string;
  /** The page has no source text (spec.fromMemory): claim and keynumber cards say so. */
  fromMemory?: boolean;
  /** Formula blocks: opens the "Go deeper" panel (absent = no button: a Quick read page, or nothing to show or fetch). */
  deeper?(button: HTMLElement): void;
}

export type Renderer<T extends BlockType> = (b: Extract<Block, { type: T }>, ctx: Ctx) => HTMLElement;

/** Categorical palette slot i as a CSS var (themes define --cat-0..5). */
export const cat = (i: number) => `var(--cat-${((i % 6) + 6) % 6})`;
