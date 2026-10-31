"""Port of shared/expr.ts: the safe math language for AI-written expressions.

A recursive-descent parser compiles to a closure tree: no eval, no attribute
access, only the whitelisted functions below. Values are numbers (float),
vectors (list[float]) or matrices (list[list[float]]); arithmetic broadcasts
element-wise. Randomness is seeded from the scope's `seed`, the expression text
and the call site, with the same 32-bit generator as the browser, so the
server's numeric checks see the same draws the page will.

Every number, message and edge case follows the TypeScript version (the parity
tests compare them); JS math semantics come from ..js.
"""

from __future__ import annotations

import math
import re
import sys
from typing import Any, Callable

from ..js import (
    INF, NAN, WS, imul, is_num, js_ceil, js_cos, js_div, js_exp, js_floor, js_index_of, js_log, js_log2, js_log10, js_max,
    js_min, js_mod, js_pow, js_round, js_sign, js_sin, js_sqrt, js_str, js_sum, js_tan, js_tanh, js_len, to_int32, u32, utf16_units,
)

sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))

Value = Any  # float | list[float] | list[list[float]]
Scope = dict[str, Any]

RANDOM_FUNCS = {"normal", "uniform", "bernoulli"}
MAX_LEN = 10_000
MAX_CELLS = 64 * 64
CONSTS = {"pi": math.pi, "e": math.e}


class ExprError(Exception):
    pass


class Compiled:
    __slots__ = ("eval", "vars", "random")

    def __init__(self, ev: Callable[[Scope], Value], vars_: dict[str, None], random: bool):
        self.eval = ev
        self.vars = vars_
        self.random = random


# ---------- value helpers ----------


def is_vec(v: Any) -> bool:
    return isinstance(v, list) and (len(v) == 0 or is_num(v[0]))


def is_mat(v: Any) -> bool:
    return isinstance(v, list) and len(v) > 0 and isinstance(v[0], list)


def fail(msg: str):
    raise ExprError(msg)


def check_len(n: float) -> int:
    if not is_num(n) or not math.isfinite(n) or n < 0:
        fail("bad length")
    if n > MAX_LEN:
        fail(f"list longer than {js_str(MAX_LEN)}")
    return int(math.floor(n))


def check_mat(m: list) -> list:
    if len(m) * (len(m[0]) if m else 0) > MAX_CELLS:
        fail("matrix larger than 64×64")
    return m


def num(v: Value, what: str = "argument") -> float:
    if not is_num(v):
        fail(f"{what} must be a single number")
    return v


def vec(v: Value, what: str = "argument") -> list:
    if is_num(v):
        return [v]
    if is_mat(v):
        fail(f"{what} must be a list, not a matrix")
    return v  # undefined (None) passes through, as in JS: it fails only where it is used


def flat(v: Value) -> list:
    if is_num(v):
        return [v]
    if is_mat(v):
        return [x for r in v for x in r]
    if not isinstance(v, list):
        raise TypeError("not a list")
    return v


def map1(v: Value, f: Callable[[float], float]) -> Value:
    if is_num(v):
        return f(v)
    if is_mat(v):
        return [[f(x) for x in r] for r in v]
    return [f(x) for x in v]


def map2(a: Value, b: Value, f: Callable[[float, float], float]) -> Value:
    if is_num(a) and is_num(b):
        return f(a, b)
    if is_num(a):
        return map1(b, lambda y: f(a, y))
    if is_num(b):
        return map1(a, lambda x: f(x, b))
    if is_mat(a) != is_mat(b):
        fail("can't combine a list with a matrix (use matmul)")
    if is_mat(a):
        if len(a) != len(b) or len(a[0]) != len(b[0]):
            fail("matrix shapes differ")
        return [[f(x, b[i][j]) for j, x in enumerate(r)] for i, r in enumerate(a)]
    if len(a) != len(b):
        fail(f"list lengths differ ({len(a)} vs {len(b)})")
    return [f(x, b[i]) for i, x in enumerate(a)]


def elementwise(args: list, f: Callable[..., float]) -> Value:
    shaped = next((a for a in args if not is_num(a)), None)
    if shaped is None:
        return f(*args)

    def at(a, i, j=None):
        if is_num(a):
            return a
        return a[i] if j is None else a[i][j]

    if is_mat(shaped):
        for a in args:
            if not is_num(a) and (not is_mat(a) or len(a) != len(shaped) or len(a[0]) != len(shaped[0])):
                fail("shapes differ")
        return [[f(*[at(a, i, j) for a in args]) for j in range(len(r))] for i, r in enumerate(shaped)]
    n = len(shaped)
    for a in args:
        if not is_num(a) and (is_mat(a) or len(a) != n):
            fail("list lengths differ")
    return [f(*[at(a, i) for a in args]) for i in range(n)]


# ---------- special functions ----------


def erf(x: float) -> float:
    """Abramowitz-Stegun 7.1.26 (|error| < 1.5e-7)."""
    s = js_sign(x)
    a = abs(x)
    t = js_div(1, 1 + 0.3275911 * a)
    y = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * js_exp(-a * a)
    return s * y


def normal_pdf(x: float, mu: float = 0, sd: float = 1) -> float:
    return js_div(js_exp(-0.5 * js_pow(js_div(x - mu, sd), 2)), sd * js_sqrt(2 * math.pi))


def normal_cdf(x: float, mu: float = 0, sd: float = 1) -> float:
    return 0.5 * (1 + erf(js_div(x - mu, sd * math.sqrt(2))))


def mean(xs: list) -> float:
    return js_div(js_sum(xs), len(xs)) if xs else NAN


def variance(xs: list) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return js_div(js_sum(js_pow(x - m, 2) for x in xs), len(xs) - 1)


def softmax(xs: list, T: float = 1) -> list:
    t = js_max(T, 1e-6)
    m = js_max(*[js_div(x, t) for x in xs])
    e = [js_exp(js_div(x, t) - m) for x in xs]
    s = js_sum(e)
    return [js_div(v, s) for v in e]


def rng(seed: int) -> Callable[[], float]:
    """mulberry32, on unsigned 32-bit values (the same bits as the JS version)."""
    state = [u32(seed)]

    def nxt() -> float:
        a = (state[0] + 0x6D2B79F5) & 0xFFFFFFFF
        state[0] = a
        t = imul(a ^ (a >> 15), a | 1)
        t = (t ^ ((t + imul(t ^ (t >> 7), t | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    return nxt


def str_hash(s: str) -> int:
    x = 2166136261
    for c in utf16_units(s):
        x = imul(x ^ c, 16777619)
    return x & 0xFFFFFFFF


# ---------- function table ----------

Impl = Callable[[list, Any, int], Value]


def _arg(a: list, i: int):
    """a[i], or JS undefined (None) past the end."""
    return a[i] if i < len(a) else None


def unary(f: Callable[[float], float]) -> Impl:
    def impl(a, ctx, site):
        if len(a) != 1:
            fail("expects 1 argument")
        return map1(a[0], f)
    return impl


def scalar_n(f: Callable[..., float], lo: int, hi: int) -> Impl:
    def impl(a, ctx, site):
        if len(a) < lo or len(a) > hi:
            fail(f"expects {lo if lo == hi else f'{lo}-{hi}'} arguments")
        return elementwise(a, f)
    return impl


def reduce_(f: Callable[[list], float]) -> Impl:
    def impl(a, ctx, site):
        if len(a) != 1:
            fail("expects 1 list")
        return f(flat(a[0]))
    return impl


def random_n(draw: Callable[[Callable[[], float], list], float], n_args: int) -> Impl:
    def impl(a, ctx, site):
        if len(a) != n_args and len(a) != n_args + 1:
            fail(f"expects {n_args} or {n_args + 1} arguments")
        params = [num(v, "parameter") for v in a[:n_args]]
        r = ctx(site)
        if len(a) == n_args:
            return draw(r, params)
        n = check_len(num(a[n_args], "count"))
        return [draw(r, params) for _ in range(n)]
    return impl


def gauss(r: Callable[[], float]) -> float:
    u = 0.0
    while u == 0:
        u = r()
    return js_sqrt(-2 * js_log(u)) * js_cos(2 * math.pi * r())


def min_max(pick: Callable[..., float]) -> Impl:
    def impl(a, ctx, site):
        if len(a) == 1:
            return pick(*flat(a[0]))
        if all(is_num(x) for x in a):
            return pick(*a)
        return elementwise(a, pick)
    return impl


def matmul(A: Value, B: Value) -> Value:
    if is_num(A):
        fail("matmul needs lists or matrices")
    a = A if is_mat(A) else [A]
    b_is_vec = not is_num(B) and not is_mat(B)
    if is_num(B):
        fail("matmul needs lists or matrices")
    if B is None:
        raise TypeError("undefined")
    b = B if is_mat(B) else [[x] for x in B]
    if a[0] is None:
        raise TypeError("undefined")
    if len(a[0]) != len(b):
        fail(f"matmul shapes don't match ({len(a)}×{len(a[0])} · {len(b)}×{len(b[0])})")
    out = check_mat([[js_sum((x * b[k][j] for k, x in enumerate(row)), 0.0) for j in range(len(b[0]))] for row in a])
    if b_is_vec:
        return [r[0] for r in out]
    return out if is_mat(A) else out[0]


def _cumsum(a, ctx, site):
    s = 0.0
    out = []
    for x in vec(_arg(a, 0)):
        s = s + x
        out.append(s)
    return out


def _softmax(a, ctx, site):
    t = _arg(a, 1)
    return softmax(vec(_arg(a, 0), "softmax input"), 1 if t is None else num(t, "temperature"))


def _dot(a, ctx, site):
    x, y = vec(_arg(a, 0)), vec(_arg(a, 1))
    if len(x) != len(y):
        fail("dot needs equal lengths")
    return js_sum((v * y[i] for i, v in enumerate(x)), 0.0)


def _outer(a, ctx, site):
    x, y = vec(_arg(a, 0)), vec(_arg(a, 1))
    return check_mat([[u * v for v in y] for u in x])


def _linspace(a, ctx, site):
    lo, hi = num(_arg(a, 0)), num(_arg(a, 1))
    third = _arg(a, 2)
    n = check_len(num(50 if third is None else third))
    return [lo if n == 1 else lo + js_div((hi - lo) * i, n - 1) for i in range(n)]


def _range(a, ctx, site):
    lo, hi = num(_arg(a, 0)), num(_arg(a, 1))
    third = _arg(a, 2)
    step = 1 if third is None else num(third)
    if step == 0 or js_div(hi - lo, step) < 0:
        return []
    n = check_len(js_ceil(js_div(hi - lo, step)))
    return [lo + i * step for i in range(n)]


def _at(a, ctx, site):
    third = _arg(a, 2)
    return index(_arg(a, 0), num(_arg(a, 1), "index"), None if third is None else num(third, "index"))


def _len(a, ctx, site):
    if len(a) != 1:
        fail("expects 1 list")
    return 1 if is_num(a[0]) else len(a[0])


def shaped(a: list, fill: float) -> Value:
    n = check_len(num(_arg(a, 0)))
    second = _arg(a, 1)
    if second is None:
        return [fill] * n
    m = check_len(num(second))
    return check_mat([[fill] * m for _ in range(n)])


def _eye(a, ctx, site):
    n = check_len(num(_arg(a, 0)))
    return check_mat([[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)])


def _transpose(a, ctx, site):
    first = _arg(a, 0)
    m = first if is_mat(first) else [vec(first)]
    return [[r[j] for r in m] for j in range(len(m[0]))]


def _variance_impl(xs):
    return variance(xs)


FUNCS: dict[str, Impl] = {
    # element-wise
    "abs": unary(abs), "sqrt": unary(js_sqrt), "exp": unary(js_exp), "log": unary(js_log), "ln": unary(js_log),
    "log2": unary(js_log2), "log10": unary(js_log10), "sin": unary(js_sin), "cos": unary(js_cos), "tan": unary(js_tan),
    "tanh": unary(js_tanh), "floor": unary(js_floor), "ceil": unary(js_ceil), "round": unary(js_round), "sign": unary(js_sign),
    "sigmoid": unary(lambda x: js_div(1, 1 + js_exp(-x))), "relu": unary(lambda x: js_max(0, x)), "erf": unary(erf),
    "pow": scalar_n(js_pow, 2, 2),
    "clamp": scalar_n(lambda x, lo, hi: js_min(hi, js_max(lo, x)), 3, 3),
    "clip": scalar_n(lambda x, lo, hi: js_min(hi, js_max(lo, x)), 3, 3),
    "normal_pdf": scalar_n(normal_pdf, 1, 3),
    "normal_cdf": scalar_n(normal_cdf, 1, 3),
    "min": min_max(js_min), "max": min_max(js_max),
    # reductions
    "sum": reduce_(lambda xs: js_sum(xs, 0.0)),
    "prod": reduce_(lambda xs: _prod(xs)),
    "mean": reduce_(mean),
    "var": reduce_(_variance_impl),
    "std": reduce_(lambda xs: js_sqrt(variance(xs))),
    "len": _len,
    "norm": reduce_(lambda xs: js_sqrt(js_sum((x * x for x in xs), 0.0))),
    "argmax": reduce_(lambda xs: float(js_index_of(xs, js_max(*xs)))),
    "argmin": reduce_(lambda xs: float(js_index_of(xs, js_min(*xs)))),
    # lists
    "cumsum": _cumsum,
    "softmax": _softmax,
    "dot": _dot,
    "outer": _outer,
    "linspace": _linspace,
    "range": _range,
    "at": _at,
    "zeros": lambda a, ctx, site: shaped(a, 0.0),
    "ones": lambda a, ctx, site: shaped(a, 1.0),
    "eye": _eye,
    # matrices
    "matmul": lambda a, ctx, site: matmul(_arg(a, 0), _arg(a, 1)),
    "transpose": _transpose,
    # random (seeded)
    "normal": random_n(lambda r, p: p[0] + p[1] * gauss(r), 2),
    "uniform": random_n(lambda r, p: p[0] + (p[1] - p[0]) * r(), 2),
    "bernoulli": random_n(lambda r, p: 1.0 if r() < p[0] else 0.0, 1),
}


def _prod(xs: list) -> float:
    s = 1.0
    for x in xs:
        s = s * x
    return s


def index(v: Value, i: float, j: float | None = None) -> Value:
    if is_num(v):
        fail("can't index a number")
    if not isinstance(v, list):
        raise TypeError("undefined")
    k = js_floor(i)
    if k != k:  # v[NaN] is undefined in JS, not an error
        return None
    if k < 0 or k >= len(v):
        fail(f"index {js_str(k)} out of range")
    row = v[int(k)]
    if j is None:
        return row
    return index(row, j)


# ---------- lexer / parser ----------

_NUM = re.compile(r"(\d+\.?\d*|\.\d+)(e[+-]?\d+)?", re.I | re.A)
_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.A)
_OP = re.compile(r"(<=|>=|==|!=|\*\*|[-+*/%^(),<>?:\[\]])", re.A)
_SPACE = re.compile(WS)


def lex(src: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if _SPACE.match(c):
            i += 1
            continue
        m = _NUM.match(src, i)
        if m:
            out.append(("num", float(m.group(0))))
            i = m.end()
            continue
        m = _ID.match(src, i)
        if m:
            out.append(("id", m.group(0)))
            i = m.end()
            continue
        m = _OP.match(src, i)
        if m:
            out.append(("op", "^" if m.group(0) == "**" else m.group(0)))
            i = m.end()
            continue
        # JS reports one UTF-16 code unit (the high surrogate of an astral character).
        ch = c if ord(c) <= 0xFFFF else chr(0xD800 + ((ord(c) - 0x10000) >> 10))
        fail(f'Unexpected character "{ch}"')
    return out


def _truthy(v: Value) -> bool:
    return num(v, "condition") != 0


CMP: dict[str, Callable[[float, float], float]] = {
    "<": lambda x, y: 1.0 if x < y else 0.0, "<=": lambda x, y: 1.0 if x <= y else 0.0, ">": lambda x, y: 1.0 if x > y else 0.0,
    ">=": lambda x, y: 1.0 if x >= y else 0.0, "==": lambda x, y: 1.0 if x == y else 0.0, "!=": lambda x, y: 1.0 if x != y else 0.0,
}
ARITH: dict[str, Callable[[float, float], float]] = {
    "+": lambda x, y: x + y, "-": lambda x, y: x - y, "*": lambda x, y: x * y, "/": js_div, "%": js_mod,
}


def _neg(v: float) -> float:
    return -v


def _scope_value(v: Any) -> Value:
    """A scope entry as an expression value: numbers become floats; anything else is NaN."""
    if is_num(v):
        return float(v)
    if isinstance(v, list):
        return _floats(v)
    return NAN


def _floats(v: list) -> list:
    """JSON integers inside a list as floats (JS numbers are all doubles: -0 must stay -0)."""
    if all(type(x) is float for x in v):
        return v
    return [_floats(x) if isinstance(x, list) else (float(x) if is_num(x) else x) for x in v]


def compile_expr(src: str) -> Compiled:
    if js_len(src) > 1000:
        fail("Expression too long")
    toks = lex(src)
    p = 0
    sites = 0
    random = False
    vars_: dict[str, None] = {}  # an ordered set: JS Sets iterate in insertion order
    src_seed = str_hash(src)

    def peek():
        return toks[p] if p < len(toks) else None

    def is_op(v: str) -> bool:
        t = peek()
        return t is not None and t[0] == "op" and t[1] == v

    def expect(v: str) -> None:
        nonlocal p
        if not is_op(v):
            fail(f'Expected "{v}"')
        p += 1

    def ternary():
        nonlocal p
        c = comparison()
        if not is_op("?"):
            return c
        p += 1
        a = ternary()
        expect(":")
        b = ternary()
        return lambda s, x: a(s, x) if _truthy(c(s, x)) else b(s, x)

    def comparison():
        nonlocal p
        left = additive()
        while (t := peek()) is not None and t[0] == "op" and t[1] in CMP:
            f = CMP[toks[p][1]]
            p += 1
            r = additive()
            left = (lambda a, r, f: lambda s, x: map2(a(s, x), r(s, x), f))(left, r, f)
        return left

    def additive():
        nonlocal p
        left = term()
        while is_op("+") or is_op("-"):
            f = ARITH[toks[p][1]]
            p += 1
            r = term()
            left = (lambda a, r, f: lambda s, x: map2(a(s, x), r(s, x), f))(left, r, f)
        return left

    def term():
        nonlocal p
        left = unary_op()
        while is_op("*") or is_op("/") or is_op("%"):
            f = ARITH[toks[p][1]]
            p += 1
            r = unary_op()
            left = (lambda a, r, f: lambda s, x: map2(a(s, x), r(s, x), f))(left, r, f)
        return left

    def unary_op():
        nonlocal p
        if is_op("-"):
            p += 1
            u = unary_op()
            return lambda s, x: map1(u(s, x), _neg)
        if is_op("+"):
            p += 1
            return unary_op()
        return power()

    def power():
        nonlocal p
        b = postfix()
        if not is_op("^"):
            return b
        p += 1
        ex = unary_op()  # right-associative, allows 2^-x
        return lambda s, x: map2(b(s, x), ex(s, x), js_pow)

    def postfix():
        nonlocal p
        node = primary()
        while is_op("["):
            p += 1
            i = ternary()
            expect("]")
            node = (lambda base, i: lambda s, x: index(base(s, x), num(i(s, x), "index")))(node, i)
        return node

    def primary():
        nonlocal p, sites, random
        t = toks[p] if p < len(toks) else None
        p += 1
        if t is None:
            fail("Unexpected end of expression")
        kind, v = t
        if kind == "num":
            return lambda s, x: v
        if kind == "op" and v == "(":
            e = ternary()
            expect(")")
            return e
        if kind == "op" and v == "[":
            items = []
            if not is_op("]"):
                items.append(ternary())
                while is_op(","):
                    p += 1
                    items.append(ternary())
            expect("]")

            def lit(s, x):
                vals = [it(s, x) for it in items]
                if all(is_num(w) for w in vals):
                    return vals
                if all(is_vec(w) and len(w) == len(vals[0]) for w in vals):
                    return check_mat(vals)
                return fail("a [...] literal must hold numbers, or lists of equal length")
            return lit
        if kind == "id":
            if is_op("("):
                f = FUNCS.get(v)
                if f is None:
                    fail(f'Unknown function "{v}"')
                p += 1
                args = []
                if not is_op(")"):
                    args.append(ternary())
                    while is_op(","):
                        p += 1
                        args.append(ternary())
                expect(")")
                site = sites
                sites += 1
                if v in RANDOM_FUNCS:
                    random = True
                return lambda s, x: f([a(s, x) for a in args], x, site)
            if v in CONSTS:
                # A def or control with the same name (e.g. "pi" for a policy) shadows the constant.
                name, c = v, CONSTS[v]

                def const(s, x):
                    own = s.get(name, None) if name in s else None
                    return _scope_value(own) if is_num(own) or isinstance(own, list) else c
                return const
            name = v
            vars_[name] = None
            return lambda s, x: _scope_value(s[name]) if name in s else NAN
        return fail(f'Unexpected "{js_str(v) if kind == "num" else v}"')

    root = ternary()
    if p < len(toks):
        k, v = toks[p]
        fail(f'Unexpected "{js_str(v) if k == "num" else v}"')

    def ev(scope: Scope) -> Value:
        seed = scope["seed"] if "seed" in scope and is_num(scope["seed"]) else 0
        base = src_seed

        def rand(site: int):
            return rng(base ^ imul(site + 1, 0x9E3779B1) ^ imul(to_int32(seed), 0x85EBCA6B))
        return root(scope, rand)

    return Compiled(ev, vars_, random)


def try_compile(src: str) -> dict:
    """{"ok": True, "c": Compiled} or {"ok": False, "error": message}."""
    try:
        return {"ok": True, "c": compile_expr(src)}
    except ExprError as e:
        return {"ok": False, "error": str(e)}
    except RecursionError:
        return {"ok": False, "error": "Maximum call stack size exceeded"}
    except Exception as e:  # pragma: no cover - a parser bug, not a model slip
        return {"ok": False, "error": str(e)}


_cache: dict[str, Compiled | None] = {}


def cached(src: str) -> Compiled | None:
    if src not in _cache:
        if len(_cache) > 2000:
            _cache.clear()
        r = try_compile(src)
        _cache[src] = r["c"] if r["ok"] else None
    return _cache[src]


def eval_value(src: str, scope: Scope) -> Value:
    """Evaluate to any value; NaN on any compile or runtime error."""
    c = cached(src)
    if c is None:
        return NAN
    try:
        return c.eval(scope)
    except Exception:
        return NAN


def eval_number(src: str, scope: Scope) -> float:
    v = eval_value(src, scope)
    return v if is_num(v) and math.isfinite(v) else NAN


def eval_list(src: str, scope: Scope) -> list:
    v = eval_value(src, scope)
    if is_num(v):
        return [v]
    return [x for r in v for x in r] if is_mat(v) else v


def eval_defs(defs: list[dict], scope: Scope) -> Scope:
    out = dict(scope)
    for d in defs:
        out[d["name"]] = eval_value(d["expr"], out)
    return out


def uses_random(src: str, defs: list[dict] | None = None) -> bool:
    by_name = {d["name"]: d["expr"] for d in (defs or [])}
    seen: set[str] = set()

    def visit(s: str) -> bool:
        c = cached(s)
        if c is None:
            return False
        if c.random:
            return True
        for v in c.vars:
            e = by_name.get(v)
            if e is not None and v not in seen:
                seen.add(v)
                if visit(e):
                    return True
        return False

    return visit(src)


__all__ = [
    "Compiled", "ExprError", "INF", "NAN", "compile_expr", "try_compile", "eval_value", "eval_number", "eval_list",
    "eval_defs", "uses_random", "is_num", "is_vec", "is_mat", "erf",
]
