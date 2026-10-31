// Reactive state store for one explainer: controls write variables, visuals
// subscribe and recompute. Expressions go through the safe evaluator only.

import { evalNumber, type Scope } from "../../../shared/expr";
import type { ExplainerSpec } from "../../../shared/spec";
import { withDefs } from "../../../shared/views";

export type SliderInfo = { label: string; min: number; max: number; unit: string };

export class Store {
  private vars: Scope = { seed: 0 };
  private subs = new Set<() => void>();
  private sliderInfo = new Map<string, SliderInfo>();

  /**
   * `parent`: a scene's store sits on the page's store. Its scope is the page's values plus its own,
   * it hears the page's changes, and slider() falls back to the page's sliders.
   */
  constructor(spec: ExplainerSpec, private parent?: Store) {
    // Every slider/choice/toggle declares its variable up front, so a visual in a
    // later stage still works when its control lives in an earlier stage.
    for (const st of spec.stages)
      for (const b of st.blocks)
        if ((b.type === "slider" || b.type === "choice" || b.type === "toggle") && !(b.var in this.vars)) {
          this.vars[b.var] = b.value;
          if (b.type === "slider") this.sliderInfo.set(b.var, { label: b.label, min: b.min, max: b.max, unit: b.unit });
        }
  }

  /** A slider's label and range (charts take their axes from sliders). */
  slider(name: string): SliderInfo | undefined {
    return this.sliderInfo.get(name) ?? this.parent?.slider(name);
  }

  get(name: string): number {
    const v = name in this.vars ? this.vars[name] : this.parent?.scope()[name];
    return typeof v === "number" ? v : NaN;
  }

  /** Current variables, plus block-local defs and extras (e.g. a block's own seed). */
  scope(defs?: { name: string; expr: string }[], extra?: Scope): Scope {
    const own = this.parent ? { ...this.parent.scope(), ...this.vars } : this.vars;
    const base = extra ? { ...own, ...extra } : own;
    return withDefs(defs, base);
  }

  set(name: string, value: number) {
    if (this.vars[name] === value) return;
    this.vars[name] = value;
    for (const fn of this.subs) fn();
  }

  /** Set several variables, then notify once (a scene rule's assignments). */
  setMany(values: Scope) {
    let changed = false;
    for (const [k, v] of Object.entries(values)) if (this.vars[k] !== v) { this.vars[k] = v; changed = true; }
    if (changed) for (const fn of this.subs) fn();
  }

  /** Subscribe (to this store and its parent); returns an unsubscribe function. */
  watch(fn: () => void): () => void {
    this.subs.add(fn);
    const off = this.parent?.watch(fn);
    return () => { this.subs.delete(fn); off?.(); };
  }
}

/** Number-valued evaluation (NaN for anything else). */
export const evalExpr = evalNumber;

/** Coalesce rapid calls (slider drags) into one per animation frame. */
export function perFrame(fn: () => void): () => void {
  let queued = false;
  const run = () => { queued = false; fn(); };
  return () => {
    if (queued) return;
    queued = true;
    // Browsers pause animation frames in hidden tabs; don't let updates stall there.
    if (document.hidden) setTimeout(run, 0);
    else requestAnimationFrame(run);
  };
}
