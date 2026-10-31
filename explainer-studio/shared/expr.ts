// Safe math language for AI-written expressions. A recursive-descent parser
// compiles to a closure tree: no eval/Function, no property access, and only
// the whitelisted functions below exist. Values are numbers, vectors
// (number[]) or matrices (number[][]); arithmetic broadcasts element-wise.
//
// Grammar: numbers, identifiers (variables or constants pi/e), [a, b, ...]
// literals (vectors; a list of equal-length vectors is a matrix), postfix
// indexing v[i] / M[i][j] (0-based), + - * / % ^, unary -, comparisons
// (< <= > >= == !=, giving 1/0), cond ? a : b, and calls to FUNCS.
//
// Randomness (normal, uniform, bernoulli) is seeded from the scope's `seed`,
// the expression text and the call site, so a redraw is deterministic and a
// "Resample" button just changes `seed`.

export type Value = number | number[] | number[][];
export type Scope = Record<string, Value>;
/** `random`: the expression itself calls normal/uniform/bernoulli. */
export type Compiled = { eval: (scope: Scope) => Value; vars: Set<string>; random: boolean };
const RANDOM_FUNCS = new Set(["normal", "uniform", "bernoulli"]);

export class ExprError extends Error {}

export const MAX_LEN = 10_000;
export const MAX_CELLS = 64 * 64;
const CONSTS: Record<string, number> = { pi: Math.PI, e: Math.E };

// ---------- value helpers ----------

export const isNum = (v: Value): v is number => typeof v === "number";
export const isVec = (v: Value): v is number[] => Array.isArray(v) && (v.length === 0 || typeof v[0] === "number");
export const isMat = (v: Value): v is number[][] => Array.isArray(v) && v.length > 0 && Array.isArray(v[0]);

function fail(msg: string): never {
  throw new ExprError(msg);
}
function checkLen(n: number): number {
  if (!Number.isFinite(n) || n < 0) fail("bad length");
  if (n > MAX_LEN) fail(`list longer than ${MAX_LEN}`);
  return Math.floor(n);
}
function checkMat(m: number[][]): number[][] {
  if (m.length * (m[0]?.length ?? 0) > MAX_CELLS) fail("matrix larger than 64×64");
  return m;
}
function num(v: Value, what = "argument"): number {
  if (!isNum(v)) fail(`${what} must be a single number`);
  return v;
}
function vec(v: Value, what = "argument"): number[] {
  if (isNum(v)) return [v];
  if (isMat(v)) fail(`${what} must be a list, not a matrix`);
  return v as number[];
}
function flat(v: Value): number[] {
  return isNum(v) ? [v] : isMat(v) ? v.flat() : (v as number[]);
}

/** Element-wise unary map. */
function map1(v: Value, f: (x: number) => number): Value {
  if (isNum(v)) return f(v);
  if (isMat(v)) return v.map((r) => r.map(f));
  return (v as number[]).map(f);
}

/** Element-wise binary map with scalar broadcasting. */
function map2(a: Value, b: Value, f: (x: number, y: number) => number): Value {
  if (isNum(a) && isNum(b)) return f(a, b);
  if (isNum(a)) return map1(b, (y) => f(a, y));
  if (isNum(b)) return map1(a, (x) => f(x, b));
  if (isMat(a) !== isMat(b)) fail("can't combine a list with a matrix (use matmul)");
  if (isMat(a)) {
    const B = b as number[][];
    if (a.length !== B.length || a[0].length !== B[0].length) fail("matrix shapes differ");
    return a.map((r, i) => r.map((x, j) => f(x, B[i][j])));
  }
  const A = a as number[], B = b as number[];
  if (A.length !== B.length) fail(`list lengths differ (${A.length} vs ${B.length})`);
  return A.map((x, i) => f(x, B[i]));
}

/** Apply a scalar function across arguments that are numbers or same-shaped lists/matrices. */
function elementwise(args: Value[], f: (...xs: number[]) => number): Value {
  const shaped = args.find((a) => !isNum(a));
  if (!shaped) return f(...(args as number[]));
  const at = (a: Value, i: number, j?: number) => (isNum(a) ? a : j === undefined ? (a as number[])[i] : (a as number[][])[i][j]);
  if (isMat(shaped)) {
    for (const a of args) if (!isNum(a) && (!isMat(a) || a.length !== shaped.length || a[0].length !== shaped[0].length)) fail("shapes differ");
    return shaped.map((r, i) => r.map((_, j) => f(...args.map((a) => at(a, i, j)))));
  }
  const n = (shaped as number[]).length;
  for (const a of args) if (!isNum(a) && (isMat(a) || (a as number[]).length !== n)) fail("list lengths differ");
  return Array.from({ length: n }, (_, i) => f(...args.map((a) => at(a, i))));
}

// ---------- special functions ----------

/** Abramowitz–Stegun 7.1.26 (|error| < 1.5e-7). */
export function erf(x: number): number {
  const s = Math.sign(x), a = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * a);
  const y = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-a * a);
  return s * y;
}
const normalPdf = (x: number, mu = 0, sd = 1) => Math.exp(-0.5 * ((x - mu) / sd) ** 2) / (sd * Math.sqrt(2 * Math.PI));
const normalCdf = (x: number, mu = 0, sd = 1) => 0.5 * (1 + erf((x - mu) / (sd * Math.SQRT2)));

function mean(xs: number[]) {
  return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : NaN;
}
function variance(xs: number[]) {
  if (xs.length < 2) return 0;
  const m = mean(xs);
  return xs.reduce((a, x) => a + (x - m) ** 2, 0) / (xs.length - 1);
}
function softmax(xs: number[], T = 1) {
  const t = Math.max(T, 1e-6);
  const m = Math.max(...xs.map((x) => x / t));
  const e = xs.map((x) => Math.exp(x / t - m));
  const s = e.reduce((a, b) => a + b, 0);
  return e.map((v) => v / s);
}

// mulberry32: small, fast, good enough for teaching visuals.
function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function strHash(s: string): number {
  let x = 2166136261;
  for (let i = 0; i < s.length; i++) x = Math.imul(x ^ s.charCodeAt(i), 16777619);
  return x >>> 0;
}

// ---------- function table ----------

type Ctx = { rand: (site: number) => () => number };
type Impl = (args: Value[], ctx: Ctx, site: number) => Value;

const unary = (f: (x: number) => number): Impl => (a) => {
  if (a.length !== 1) fail("expects 1 argument");
  return map1(a[0], f);
};
const scalarN = (f: (...xs: number[]) => number, min: number, max: number): Impl => (a) => {
  if (a.length < min || a.length > max) fail(`expects ${min === max ? min : `${min}-${max}`} arguments`);
  return elementwise(a, f);
};
const reduce = (f: (xs: number[]) => number): Impl => (a) => {
  if (a.length !== 1) fail("expects 1 list");
  return f(flat(a[0]));
};
const randomN = (draw: (r: () => number, a: number[]) => number, nArgs: number): Impl => (a, ctx, site) => {
  if (a.length !== nArgs && a.length !== nArgs + 1) fail(`expects ${nArgs} or ${nArgs + 1} arguments`);
  const params = a.slice(0, nArgs).map((v) => num(v, "parameter"));
  const r = ctx.rand(site);
  if (a.length === nArgs) return draw(r, params);
  const n = checkLen(num(a[nArgs], "count"));
  return Array.from({ length: n }, () => draw(r, params));
};
const gauss = (r: () => number) => {
  let u = 0;
  while (u === 0) u = r();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * r());
};

function minMax(pick: (...xs: number[]) => number): Impl {
  return (a) => {
    if (a.length === 1) return pick(...flat(a[0]));
    if (a.every(isNum)) return pick(...(a as number[]));
    return elementwise(a, pick);
  };
}

function matmul(A: Value, B: Value): Value {
  const a = isNum(A) ? fail("matmul needs lists or matrices") : isMat(A) ? A : [A as number[]];
  const bIsVec = !isNum(B) && !isMat(B);
  const b = isNum(B) ? fail("matmul needs lists or matrices") : isMat(B) ? B : (B as number[]).map((x) => [x]);
  if (a[0].length !== b.length) fail(`matmul shapes don't match (${a.length}×${a[0].length} · ${b.length}×${b[0].length})`);
  const out = checkMat(a.map((row) => b[0].map((_, j) => row.reduce((s, x, k) => s + x * b[k][j], 0))));
  if (bIsVec) return out.map((r) => r[0]);
  return !isMat(A) ? out[0] : out;
}

const FUNCS: Record<string, Impl> = {
  // element-wise
  abs: unary(Math.abs), sqrt: unary(Math.sqrt), exp: unary(Math.exp), log: unary(Math.log), ln: unary(Math.log),
  log2: unary(Math.log2), log10: unary(Math.log10), sin: unary(Math.sin), cos: unary(Math.cos), tan: unary(Math.tan),
  tanh: unary(Math.tanh), floor: unary(Math.floor), ceil: unary(Math.ceil), round: unary(Math.round), sign: unary(Math.sign),
  sigmoid: unary((x) => 1 / (1 + Math.exp(-x))), relu: unary((x) => Math.max(0, x)), erf: unary(erf),
  pow: scalarN(Math.pow, 2, 2),
  clamp: scalarN((x, lo, hi) => Math.min(hi, Math.max(lo, x)), 3, 3),
  clip: scalarN((x, lo, hi) => Math.min(hi, Math.max(lo, x)), 3, 3),
  normal_pdf: scalarN(normalPdf, 1, 3),
  normal_cdf: scalarN(normalCdf, 1, 3),
  min: minMax(Math.min), max: minMax(Math.max),
  // reductions
  sum: reduce((xs) => xs.reduce((a, b) => a + b, 0)),
  prod: reduce((xs) => xs.reduce((a, b) => a * b, 1)),
  mean: reduce(mean),
  var: reduce(variance),
  std: reduce((xs) => Math.sqrt(variance(xs))),
  len: (a) => (a.length === 1 ? (isNum(a[0]) ? 1 : a[0].length) : fail("expects 1 list")),
  norm: reduce((xs) => Math.sqrt(xs.reduce((a, x) => a + x * x, 0))),
  argmax: reduce((xs) => xs.indexOf(Math.max(...xs))),
  argmin: reduce((xs) => xs.indexOf(Math.min(...xs))),
  // lists
  cumsum: (a) => { let s = 0; return vec(a[0]).map((x) => (s += x)); },
  softmax: (a) => softmax(vec(a[0], "softmax input"), a[1] === undefined ? 1 : num(a[1], "temperature")),
  dot: (a) => {
    const x = vec(a[0]), y = vec(a[1]);
    if (x.length !== y.length) fail("dot needs equal lengths");
    return x.reduce((s, v, i) => s + v * y[i], 0);
  },
  outer: (a) => { const x = vec(a[0]), y = vec(a[1]); return checkMat(x.map((u) => y.map((v) => u * v))); },
  linspace: (a) => {
    const lo = num(a[0]), hi = num(a[1]), n = checkLen(num(a[2] ?? 50));
    return Array.from({ length: n }, (_, i) => (n === 1 ? lo : lo + ((hi - lo) * i) / (n - 1)));
  },
  range: (a) => {
    const lo = num(a[0]), hi = num(a[1]), step = a[2] === undefined ? 1 : num(a[2]);
    if (step === 0 || (hi - lo) / step < 0) return [];
    const n = checkLen(Math.ceil((hi - lo) / step));
    return Array.from({ length: n }, (_, i) => lo + i * step);
  },
  at: (a) => index(a[0], num(a[1], "index"), a[2] === undefined ? undefined : num(a[2], "index")),
  zeros: (a) => shaped(a, 0),
  ones: (a) => shaped(a, 1),
  eye: (a) => { const n = checkLen(num(a[0])); return checkMat(Array.from({ length: n }, (_, i) => Array.from({ length: n }, (_, j) => +(i === j)))); },
  // matrices
  matmul: (a) => matmul(a[0], a[1]),
  transpose: (a) => {
    const m = isMat(a[0]) ? a[0] : [vec(a[0])];
    return m[0].map((_, j) => m.map((r) => r[j]));
  },
  // random (seeded)
  normal: randomN((r, [mu, sd]) => mu + sd * gauss(r), 2),
  uniform: randomN((r, [lo, hi]) => lo + (hi - lo) * r(), 2),
  bernoulli: randomN((r, [p]) => +(r() < p), 1),
};

function shaped(a: Value[], fill: number): Value {
  const n = checkLen(num(a[0]));
  if (a[1] === undefined) return Array(n).fill(fill);
  const m = checkLen(num(a[1]));
  return checkMat(Array.from({ length: n }, () => Array(m).fill(fill)));
}

function index(v: Value, i: number, j?: number): Value {
  if (isNum(v)) fail("can't index a number");
  const k = Math.floor(i);
  if (k < 0 || k >= v.length) fail(`index ${k} out of range`);
  const row = v[k];
  if (j === undefined) return row;
  return index(row as Value, j);
}

// ---------- lexer / parser ----------

type Tok = { t: "num"; v: number } | { t: "id"; v: string } | { t: "op"; v: string };

function lex(src: string): Tok[] {
  const out: Tok[] = [];
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (/\s/.test(c)) { i++; continue; }
    const rest = src.slice(i);
    const n = /^(\d+\.?\d*|\.\d+)(e[+-]?\d+)?/i.exec(rest);
    if (n) { out.push({ t: "num", v: parseFloat(n[0]) }); i += n[0].length; continue; }
    const id = /^[A-Za-z_][A-Za-z0-9_]*/.exec(rest);
    if (id) { out.push({ t: "id", v: id[0] }); i += id[0].length; continue; }
    const op = /^(<=|>=|==|!=|\*\*|[-+*/%^(),<>?:[\]])/.exec(rest);
    if (op) { out.push({ t: "op", v: op[0] === "**" ? "^" : op[0] }); i += op[0].length; continue; }
    fail(`Unexpected character "${c}"`);
  }
  return out;
}

type Node = (s: Scope, ctx: Ctx) => Value;

const truthy = (v: Value) => num(v, "condition") !== 0;
const CMP: Record<string, (x: number, y: number) => number> = {
  "<": (x, y) => +(x < y), "<=": (x, y) => +(x <= y), ">": (x, y) => +(x > y),
  ">=": (x, y) => +(x >= y), "==": (x, y) => +(x === y), "!=": (x, y) => +(x !== y),
};
const ARITH: Record<string, (x: number, y: number) => number> = {
  "+": (x, y) => x + y, "-": (x, y) => x - y, "*": (x, y) => x * y, "/": (x, y) => x / y, "%": (x, y) => x % y,
};

export function compile(src: string): Compiled {
  if (src.length > 1000) fail("Expression too long");
  const toks = lex(src);
  let p = 0;
  let sites = 0;
  let random = false;
  const vars = new Set<string>();
  const srcSeed = strHash(src);
  const peek = () => toks[p];
  const isOp = (v: string) => peek()?.t === "op" && peek()!.v === v;
  const expect = (v: string) => { if (!isOp(v)) fail(`Expected "${v}"`); p++; };

  function ternary(): Node {
    const c = comparison();
    if (!isOp("?")) return c;
    p++;
    const a = ternary();
    expect(":");
    const b = ternary();
    return (s, x) => (truthy(c(s, x)) ? a(s, x) : b(s, x));
  }
  function comparison(): Node {
    let l = additive();
    while (peek()?.t === "op" && peek()!.v in CMP) {
      const f = CMP[toks[p++].v as string], r = additive(), a = l;
      l = (s, x) => map2(a(s, x), r(s, x), f);
    }
    return l;
  }
  function additive(): Node {
    let l = term();
    while (isOp("+") || isOp("-")) {
      const f = ARITH[toks[p++].v as string], r = term(), a = l;
      l = (s, x) => map2(a(s, x), r(s, x), f);
    }
    return l;
  }
  function term(): Node {
    let l = unaryOp();
    while (isOp("*") || isOp("/") || isOp("%")) {
      const f = ARITH[toks[p++].v as string], r = unaryOp(), a = l;
      l = (s, x) => map2(a(s, x), r(s, x), f);
    }
    return l;
  }
  function unaryOp(): Node {
    if (isOp("-")) { p++; const u = unaryOp(); return (s, x) => map1(u(s, x), (v) => -v); }
    if (isOp("+")) { p++; return unaryOp(); }
    return power();
  }
  function power(): Node {
    const b = postfix();
    if (!isOp("^")) return b;
    p++;
    const ex = unaryOp(); // right-associative, allows 2^-x
    return (s, x) => map2(b(s, x), ex(s, x), Math.pow);
  }
  function postfix(): Node {
    let node = primary();
    while (isOp("[")) {
      p++;
      const i = ternary();
      expect("]");
      const base = node;
      node = (s, x) => index(base(s, x), num(i(s, x), "index"));
    }
    return node;
  }
  function primary(): Node {
    const t = toks[p++];
    if (!t) fail("Unexpected end of expression");
    if (t.t === "num") { const v = t.v; return () => v; }
    if (t.t === "op" && t.v === "(") { const e = ternary(); expect(")"); return e; }
    if (t.t === "op" && t.v === "[") {
      const items: Node[] = [];
      if (!isOp("]")) {
        items.push(ternary());
        while (isOp(",")) { p++; items.push(ternary()); }
      }
      expect("]");
      return (s, x) => {
        const vals = items.map((it) => it(s, x));
        if (vals.every(isNum)) return vals as number[];
        if (vals.every((v) => isVec(v) && v.length === (vals[0] as number[]).length)) return checkMat(vals as number[][]);
        return fail("a [...] literal must hold numbers, or lists of equal length");
      };
    }
    if (t.t === "id") {
      if (isOp("(")) {
        const f = Object.hasOwn(FUNCS, t.v) ? FUNCS[t.v] : undefined;
        if (!f) fail(`Unknown function "${t.v}"`);
        p++;
        const args: Node[] = [];
        if (!isOp(")")) {
          args.push(ternary());
          while (isOp(",")) { p++; args.push(ternary()); }
        }
        expect(")");
        const site = sites++;
        if (RANDOM_FUNCS.has(t.v)) random = true;
        return (s, x) => f(args.map((a) => a(s, x)), x, site);
      }
      if (Object.hasOwn(CONSTS, t.v)) {
        // A def or control with the same name (e.g. "pi" for a policy) shadows the constant.
        const name = t.v, v = CONSTS[name];
        return (s) => {
          const own = Object.hasOwn(s, name) ? s[name] : undefined;
          return typeof own === "number" || Array.isArray(own) ? own : v;
        };
      }
      const name = t.v;
      vars.add(name);
      return (s) => {
        if (!Object.hasOwn(s, name)) return NaN;
        const v = s[name];
        return typeof v === "number" || Array.isArray(v) ? v : NaN;
      };
    }
    return fail(`Unexpected "${t.v}"`);
  }

  const root = ternary();
  if (p < toks.length) fail(`Unexpected "${(toks[p] as Tok).v}"`);
  return {
    vars,
    random,
    eval: (scope) => {
      const seed = Object.hasOwn(scope, "seed") && isNum(scope.seed) ? scope.seed : 0;
      return root(scope, { rand: (site) => rng(srcSeed ^ Math.imul(site + 1, 0x9e3779b1) ^ Math.imul(seed | 0, 0x85ebca6b)) });
    },
  };
}

/** Compile, or return the error message. */
export function tryCompile(src: string): { ok: true; c: Compiled } | { ok: false; error: string } {
  try {
    return { ok: true, c: compile(src) };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

const cache = new Map<string, Compiled | null>();
function cached(src: string): Compiled | null {
  if (!cache.has(src)) {
    if (cache.size > 2000) cache.clear();
    const r = tryCompile(src);
    cache.set(src, r.ok ? r.c : null);
  }
  return cache.get(src)!;
}

/** Evaluate to any value; NaN on any compile or runtime error. */
export function evalValue(src: string, scope: Scope): Value {
  const c = cached(src);
  if (!c) return NaN;
  try {
    return c.eval(scope);
  } catch {
    return NaN;
  }
}

/** Evaluate expecting one finite number; NaN otherwise. */
export function evalNumber(src: string, scope: Scope): number {
  const v = evalValue(src, scope);
  return typeof v === "number" && Number.isFinite(v) ? v : NaN;
}

/** Evaluate expecting a list of numbers (a single number becomes [n]). */
export function evalList(src: string, scope: Scope): number[] {
  const v = evalValue(src, scope);
  if (isNum(v)) return [v];
  return isMat(v) ? v.flat() : v;
}

/** Evaluate expecting a matrix (a list becomes one row, a number 1×1). */
export function evalMatrix(src: string, scope: Scope): number[][] {
  const v = evalValue(src, scope);
  return isNum(v) ? [[v]] : isMat(v) ? v : [v];
}

export interface Def { name: string; expr: string }

/** Evaluate ordered definitions on top of a scope (later defs see earlier ones). */
export function evalDefs(defs: Def[], scope: Scope): Scope {
  const out: Scope = { ...scope };
  for (const d of defs) out[d.name] = evalValue(d.expr, out);
  return out;
}

/** True if the expression, or any def it (transitively) uses, draws random numbers. */
export function usesRandom(src: string, defs: Def[] = []): boolean {
  const byName = new Map(defs.map((d) => [d.name, d.expr]));
  const seen = new Set<string>();
  const visit = (s: string): boolean => {
    const c = cached(s);
    if (!c) return false;
    if (c.random) return true;
    for (const v of c.vars) {
      const e = byName.get(v);
      if (e !== undefined && !seen.has(v)) {
        seen.add(v);
        if (visit(e)) return true;
      }
    }
    return false;
  };
  return visit(src);
}
