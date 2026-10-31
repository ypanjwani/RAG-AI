// Math shared by the renderers and the validator's sanity check, so both see
// exactly the same numbers.

import { evalDefs, evalNumber, isNum, type Def, type Scope } from "./expr";

export const MAX_STEPS = 2000;
export const DIVERGE_LIMIT = 1e6;

export function withDefs(defs: Def[] | undefined, scope: Scope): Scope {
  return defs?.length ? evalDefs(defs, scope) : scope;
}

export interface TrajectoryInput {
  state: { name: string; init: string }[];
  update: { name: string; expr: string }[];
  steps: string;
  /** Re-evaluated at every step, so they may use t and the state (e.g. a noise schedule ᾱ(t)). */
  defs?: Def[];
}

export interface TrajectoryRun {
  /** One scope per step (state values + t + defs), starting at t = 0. */
  frames: Scope[];
  /** Step at which values became non-finite or exploded, or -1. */
  divergedAt: number;
}

/** Run x_{t+1} = f(x_t) with simultaneous updates. Stops early on divergence. `base` excludes the block's defs. */
export function runTrajectory(b: TrajectoryInput, base: Scope, maxSteps = MAX_STEPS): TrajectoryRun {
  const at = (s: Scope) => withDefs(b.defs, s);
  const steps = Math.max(0, Math.min(maxSteps, Math.round(evalNumber(b.steps, at(base)) || 0)));
  const start: Scope = { ...base, t: 0 };
  for (const s of b.state) start[s.name] = evalNumber(s.init, at(start));
  let cur = at(start);
  const frames: Scope[] = [cur];
  let divergedAt = -1;
  for (let t = 1; t <= steps; t++) {
    const next: Scope = { ...cur, t };
    for (const u of b.update) next[u.name] = evalNumber(u.expr, { ...cur, t: t - 1 });
    const bad = b.state.some((s) => {
      const v = next[s.name];
      return !isNum(v) || !Number.isFinite(v) || Math.abs(v) > DIVERGE_LIMIT;
    });
    if (bad) { divergedAt = t; break; }
    cur = at(next);
    frames.push(cur);
  }
  return { frames, divergedAt };
}

/** Trapezoid area under f between a and b. */
export function areaUnder(f: (x: number) => number, a: number, b: number, n = 200): number {
  if (!(Number.isFinite(a) && Number.isFinite(b)) || a === b) return 0;
  const lo = Math.min(a, b), hi = Math.max(a, b), h = (hi - lo) / n;
  let s = 0;
  for (let i = 0; i <= n; i++) {
    const y = f(lo + i * h);
    if (!Number.isFinite(y)) return NaN;
    s += (i === 0 || i === n ? 0.5 : 1) * y;
  }
  return s * h * Math.sign(b - a);
}
