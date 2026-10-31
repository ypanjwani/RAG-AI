import { describe, expect, it } from "vitest";
import { compile, evalDefs, evalNumber, evalValue, tryCompile, type Scope } from "../shared/expr";

const ev = (s: string, scope: Scope = {}) => compile(s).eval(scope);

describe("scalars", () => {
  it("handles precedence, power and unary minus", () => {
    expect(ev("1 + 2 * 3")).toBe(7);
    expect(ev("2 ^ 3 ^ 2")).toBe(512);
    expect(ev("-2 ^ 2")).toBe(-4);
    expect(ev("2 ^ -1")).toBe(0.5);
  });

  it("supports variables, functions, constants and ternaries", () => {
    expect(ev("sigmoid(0)")).toBe(0.5);
    expect(ev("max(a, b, 3)", { a: 1, b: 5 })).toBe(5);
    expect(ev("clamp(x, 0, 1)", { x: 4 })).toBe(1);
    expect(ev("mode == 0 ? 10 : 20", { mode: 1 })).toBe(20);
    expect(ev("2 * pi")).toBeCloseTo(6.283, 3);
    expect(compile("a * x + exp(b)").vars).toEqual(new Set(["a", "x", "b"]));
  });

  it("has accurate normal functions", () => {
    expect(ev("normal_cdf(1.96)") as number).toBeCloseTo(0.975, 3);
    expect(ev("2 * normal_cdf(1.96) - 1") as number).toBeCloseTo(0.95, 3);
    expect(ev("normal_cdf(110, 100, 10)") as number).toBeCloseTo(0.8413, 3);
    expect(ev("normal_pdf(0)") as number).toBeCloseTo(0.3989, 3);
    expect(ev("erf(0.5)") as number).toBeCloseTo(0.5205, 4);
  });
});

describe("lists and matrices", () => {
  it("broadcasts arithmetic and functions", () => {
    expect(ev("[1, 2, 3] * 2 + 1")).toEqual([3, 5, 7]);
    expect(ev("[1, 2] + [10, 20]")).toEqual([11, 22]);
    expect(ev("exp([0, 0])")).toEqual([1, 1]);
    expect(ev("[1, 5, 3] > 2")).toEqual([0, 1, 1]);
    expect(ev("clip([-1, 0.5, 2], 0, 1)")).toEqual([0, 0.5, 1]);
    expect(evalValue("[1, 2] + [1, 2, 3]", {})).toBeNaN(); // shape mismatch → NaN, not a throw
  });

  it("reduces and transforms lists", () => {
    expect(ev("sum([1, 2, 3])")).toBe(6);
    expect(ev("mean([2, 4])")).toBe(3);
    expect(ev("std([2, 4, 4, 4, 5, 5, 7, 9])") as number).toBeCloseTo(2.138, 3);
    expect(ev("len(linspace(0, 1, 5))")).toBe(5);
    expect(ev("range(0, 5)")).toEqual([0, 1, 2, 3, 4]);
    expect(ev("cumsum([1, 2, 3])")).toEqual([1, 3, 6]);
    expect(ev("argmax([3, 9, 2])")).toBe(1);
    expect(ev("max([3, 9, 2])")).toBe(9);
    expect(ev("dot([1, 2], [3, 4])")).toBe(11);
    expect(ev("sum(softmax([1, 2, 3]))") as number).toBeCloseTo(1, 10);
    expect(ev("v[1]", { v: [5, 6, 7] })).toBe(6);
    expect(ev("at(M, 1, 0)", { M: [[1, 2], [3, 4]] })).toBe(3);
  });

  it("does matrix algebra", () => {
    expect(ev("matmul([[1, 2], [3, 4]], [[5], [6]])")).toEqual([[17], [39]]);
    expect(ev("matmul([[1, 2], [3, 4]], [1, 1])")).toEqual([3, 7]);
    expect(ev("transpose([[1, 2, 3]])")).toEqual([[1], [2], [3]]);
    expect(ev("outer([1, 2], [3, 4])")).toEqual([[3, 4], [6, 8]]);
    expect(ev("W + matmul(B, A)", { W: [[1, 0], [0, 1]], B: [[1], [2]], A: [[1, 1]] })).toEqual([[2, 1], [2, 3]]);
    expect(ev("eye(2)")).toEqual([[1, 0], [0, 1]]);
  });

  it("enforces size caps", () => {
    expect(evalValue("zeros(20000)", {})).toBeNaN();
    expect(evalValue("ones(100, 100)", {})).toBeNaN();
    expect(evalValue("normal(0, 1, 1e9)", {})).toBeNaN();
  });
});

describe("seeded randomness", () => {
  it("is deterministic per seed and changes with it", () => {
    const a = ev("normal(0, 1, 5)", { seed: 1 });
    expect(ev("normal(0, 1, 5)", { seed: 1 })).toEqual(a);
    expect(ev("normal(0, 1, 5)", { seed: 2 })).not.toEqual(a);
  });

  it("gives independent streams to different call sites", () => {
    const [x, y] = ev("[mean(normal(0, 1, 50)), mean(normal(0, 1, 50))]") as number[];
    expect(x).not.toBe(y);
  });

  it("has roughly the right statistics", () => {
    const s = { seed: 7 };
    expect(evalNumber("mean(normal(10, 2, 5000))", s)).toBeCloseTo(10, 0);
    expect(evalNumber("std(normal(10, 2, 5000))", s)).toBeCloseTo(2, 0);
    expect(evalNumber("mean(bernoulli(0.3, 5000))", s)).toBeCloseTo(0.3, 1);
  });
});

describe("defs", () => {
  it("evaluates in order so later defs see earlier ones (MC-Dropout example)", () => {
    const scope = evalDefs(
      [
        { name: "yhat", expr: "normal(mu, sigma, T)" },
        { name: "m", expr: "mean(yhat)" },
        { name: "half", expr: "z * std(yhat)" },
        { name: "coverage", expr: "2 * normal_cdf(z) - 1" },
      ],
      { mu: 120, sigma: 7, T: 2000, z: 1.96, seed: 3 },
    );
    expect(scope.m as number).toBeCloseTo(120, 0);
    expect(scope.half as number).toBeCloseTo(13.7, 0);
    expect(scope.coverage as number).toBeCloseTo(0.95, 3);
  });
});

describe("safety", () => {
  it("rejects anything outside the language", () => {
    for (const bad of ["constructor(1)", "toString()", "alert(1)", "a.b", "1 +", "foo(2)", "`x`", "x = 1", "[1, 2"]) {
      expect(tryCompile(bad).ok, bad).toBe(false);
    }
  });

  it("never reaches the object prototype through variables", () => {
    expect(ev("toString")).toBeNaN();
    expect(ev("__proto__", {})).toBeNaN();
    expect(ev("constructor")).toBeNaN();
  });
});
