"""JavaScript semantics the port depends on.

The validator, the math language and the prompts were written in TypeScript, and
the browser still runs those versions. Where Python and JavaScript differ in a
way that changes a number, a string or a warning, the Python port calls these
helpers instead of the built-ins:

- Numbers: JS has one number type (a double). JSON integers stay Python ints
  here, so "is a number" means int or float but never bool, and all math goes
  through float.
- Number to text: String(n), n.toFixed(d) and n.toPrecision(p) round and print
  differently from Python's repr/format.
- Math: JS returns NaN or Infinity where Python raises (log(0), exp(1000),
  pow(-8, 1/3)...), Math.round rounds halves up, Math.max propagates NaN, and
  Python 3.12's sum() uses compensated summation, which JS doesn't.
- Strings: JS lengths and slices count UTF-16 code units, and JS whitespace
  (\\s, trim) is a fixed Unicode set, not Python's.
- JSON: JSON.parse rejects NaN/Infinity, and JSON.stringify prints numbers the
  JS way and NaN as null.
"""

from __future__ import annotations

import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

NAN = float("nan")
INF = float("inf")
M32 = 0xFFFFFFFF

# ---------- numbers ----------


def is_num(v: Any) -> bool:
    """typeof v === "number"."""
    return (type(v) is float) or (type(v) is int)


def is_finite(v: Any) -> bool:
    """Number.isFinite(v): a number that is not NaN or ±Infinity."""
    return is_num(v) and math.isfinite(v)


def is_integer(v: Any) -> bool:
    """Number.isInteger(v)."""
    return is_finite(v) and float(v).is_integer()


def to_int32(x: float) -> int:
    """ToInt32 (x | 0)."""
    if not is_finite(x):
        return 0
    n = int(math.trunc(x)) & M32
    return n - 0x100000000 if n & 0x80000000 else n


def u32(x: int) -> int:
    return x & M32


def imul(a: int, b: int) -> int:
    """Math.imul, returned as an unsigned 32-bit value (the bits are what matter)."""
    return ((a & M32) * (b & M32)) & M32


# ---------- number to text ----------


def _digits(x: float) -> tuple[str, int]:
    """Shortest round-trip digits of |x| and n such that |x| = 0.digits × 10^n."""
    d = Decimal(repr(abs(float(x)))).normalize()
    t = d.as_tuple()
    digits = "".join(map(str, t.digits))
    return digits, len(digits) + t.exponent


def js_str(x: Any) -> str:
    """String(x) for a number (Number::toString)."""
    if type(x) is bool:
        return "true" if x else "false"
    if type(x) is int and abs(x) < 2**53:
        return str(x)
    x = float(x)
    if x != x:
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    digits, n = _digits(x)
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    mant = digits[0] + ("." + digits[1:] if k > 1 else "")
    return f"{sign}{mant}e{'+' if e >= 0 else '-'}{abs(e)}"


def to_fixed(x: float, d: int) -> str:
    """x.toFixed(d): exact decimal value, halves rounded up (away from zero for the magnitude)."""
    x = float(x)
    if x != x:
        return "NaN"
    if abs(x) >= 1e21:
        return js_str(x)
    neg = x < 0
    q = Decimal(abs(x)).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP)
    s = format(q, "f")
    return ("-" if neg else "") + s


def to_precision(x: float, p: int) -> str:
    """x.toPrecision(p)."""
    x = float(x)
    if not math.isfinite(x):
        return js_str(x)
    if x == 0:
        return "0" + ("." + "0" * (p - 1) if p > 1 else "")
    neg = x < 0
    d = Decimal(abs(x))
    q = d.quantize(Decimal(1).scaleb(d.adjusted() - p + 1), rounding=ROUND_HALF_UP)
    if q.adjusted() != d.adjusted():  # 9.99 -> 10.0: one more digit; re-round at the new exponent
        q = d.quantize(Decimal(1).scaleb(q.adjusted() - p + 1), rounding=ROUND_HALF_UP)
    e = q.adjusted()
    digits = "".join(map(str, q.as_tuple().digits))
    digits = (digits + "0" * p)[:p]
    if e < -6 or e >= p:
        m = digits[0] + ("." + digits[1:] if p > 1 else "")
        s = f"{m}e{'+' if e >= 0 else '-'}{abs(e)}"
    elif e == p - 1:
        s = digits
    elif e >= 0:
        s = digits[: e + 1] + "." + digits[e + 1 :]
    else:
        s = "0." + "0" * (-(e + 1)) + digits
    return ("-" if neg else "") + s


def js_number(s: str) -> float:
    """Number(s) for the decimal strings the port converts (literals the model wrote)."""
    t = js_trim(s)
    if t == "":
        return 0.0
    try:
        if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", t, re.A):
            return float(t)
        if re.fullmatch(r"[+-]?Infinity", t):
            return -INF if t.startswith("-") else INF
        if re.fullmatch(r"0[xX][0-9a-fA-F]+", t):
            return float(int(t, 16))
        if re.fullmatch(r"0[oO][0-7]+", t):
            return float(int(t[2:], 8))
        if re.fullmatch(r"0[bB][01]+", t):
            return float(int(t[2:], 2))
    except (ValueError, OverflowError):
        return NAN
    return NAN


def round_num(x: float, sig: int) -> float:
    """Number(x.toPrecision(sig))."""
    return float(to_precision(x, sig))


# ---------- Math ----------


def js_round(x: float) -> float:
    """Math.round: nearest, halves toward +Infinity."""
    x = float(x)
    if not math.isfinite(x) or x == 0 or abs(x) >= 2**52:
        return x
    r = math.floor(x)
    if x - r >= 0.5:
        r += 1
    if r == 0 and x < 0:
        return -0.0
    return float(r)


def js_floor(x: float) -> float:
    x = float(x)
    if not math.isfinite(x) or x == 0:
        return x
    return float(math.floor(x))


def js_ceil(x: float) -> float:
    x = float(x)
    if not math.isfinite(x) or x == 0:
        return x
    r = math.ceil(x)
    return -0.0 if r == 0 and x < 0 else float(r)


def js_sign(x: float) -> float:
    x = float(x)
    if x != x or x == 0:
        return x
    return 1.0 if x > 0 else -1.0


def js_max(*xs: float) -> float:
    """Math.max: NaN wins, -Infinity for no arguments, +0 beats -0."""
    r = -INF
    for x in xs:
        x = float(x)
        if x != x:
            return NAN
        if x > r or (x == 0 and r == 0 and math.copysign(1, r) < 0):
            r = x
    return r


def js_min(*xs: float) -> float:
    r = INF
    for x in xs:
        x = float(x)
        if x != x:
            return NAN
        if x < r or (x == 0 and r == 0 and math.copysign(1, x) < 0):
            r = x
    return r


def _odd_int(y: float) -> bool:
    return math.isfinite(y) and y.is_integer() and int(y) % 2 == 1


def js_pow(x: float, y: float) -> float:
    """Math.pow / **."""
    x, y = float(x), float(y)
    if y != y:
        return NAN
    if y == 0:
        return 1.0
    if x != x:
        return NAN
    if abs(x) == 1 and math.isinf(y):
        return NAN
    try:
        return math.pow(x, y)
    except OverflowError:
        return -INF if x < 0 and _odd_int(y) else INF
    except ValueError:
        if x == 0:  # 0 to a negative power
            return -INF if math.copysign(1, x) < 0 and _odd_int(y) else INF
        return NAN


def js_exp(x: float) -> float:
    try:
        return math.exp(x)
    except OverflowError:
        return INF


def _log(f, x: float) -> float:
    x = float(x)
    if x != x or x < 0:
        return NAN
    if x == 0:
        return -INF
    if math.isinf(x):
        return INF
    return f(x)


def js_log(x: float) -> float:
    return _log(math.log, x)


def js_log2(x: float) -> float:
    return _log(math.log2, x)


def js_log10(x: float) -> float:
    return _log(math.log10, x)


def js_sqrt(x: float) -> float:
    x = float(x)
    if x != x or x < 0:
        return NAN
    return math.sqrt(x)


def _trig(f, x: float) -> float:
    x = float(x)
    if not math.isfinite(x):
        return NAN
    return f(x)


def js_sin(x: float) -> float:
    return _trig(math.sin, x)


def js_cos(x: float) -> float:
    return _trig(math.cos, x)


def js_tan(x: float) -> float:
    return _trig(math.tan, x)


def js_tanh(x: float) -> float:
    return math.tanh(float(x))


def js_div(x: float, y: float) -> float:
    x, y = float(x), float(y)
    if y == 0:
        if x == 0 or x != x:
            return NAN
        return math.copysign(INF, x) * math.copysign(1, y)
    return x / y


def js_mod(x: float, y: float) -> float:
    x, y = float(x), float(y)
    if y == 0 or x != x or y != y or math.isinf(x):
        return NAN
    if math.isinf(y) or x == 0:
        return x
    return math.fmod(x, y)


def js_sum(xs, start: float = 0.0) -> float:
    """xs.reduce((a, b) => a + b, start): plain left-to-right addition (Python 3.12's sum() compensates)."""
    s = start
    for x in xs:
        s = s + x
    return s


def js_index_of(xs: list, v: float) -> int:
    """Array.prototype.indexOf (NaN is never found)."""
    for i, x in enumerate(xs):
        if x == v:
            return i
    return -1


# ---------- strings ----------

# JS \s and String.prototype.trim: Unicode White_Space plus line terminators and U+FEFF.
JS_WS_CHARS = "\t\n\v\f\r                  　﻿"
WS = "[" + re.escape(JS_WS_CHARS) + "]"
"""Regex class equivalent to JS \\s (use with re.A so \\d, \\w and \\b stay ASCII as in JS)."""
NOT_WS = "[^" + re.escape(JS_WS_CHARS) + "]"


def js_trim(s: str) -> str:
    return s.strip(JS_WS_CHARS)


def js_trim_start(s: str) -> str:
    return s.lstrip(JS_WS_CHARS)


def js_trim_end(s: str) -> str:
    return s.rstrip(JS_WS_CHARS)


def js_len(s: str) -> int:
    """s.length: UTF-16 code units."""
    if s.isascii():
        return len(s)
    return len(s) + sum(1 for c in s if ord(c) > 0xFFFF)


def js_slice(s: str, start: int, end: int | None = None) -> str:
    """s.slice(start, end) in UTF-16 code units (a cut inside a surrogate pair leaves a lone surrogate, as in JS)."""
    if s.isascii() or not any(ord(c) > 0xFFFF for c in s):
        return s[start:end]
    b = s.encode("utf-16-le", "surrogatepass")
    n = len(b) // 2
    a = start if start >= 0 else max(0, n + start)
    z = n if end is None else (end if end >= 0 else max(0, n + end))
    return b[2 * min(a, n) : 2 * min(max(z, 0), n)].decode("utf-16-le", "surrogatepass")


def utf16_units(s: str) -> list[int]:
    """charCodeAt for every index."""
    b = s.encode("utf-16-le", "surrogatepass")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def js_lower(s: str) -> str:
    """toLowerCase for the ASCII-only uses in the port (slugs, keywords)."""
    return s.lower()


# ---------- JSON ----------


def _reject_constant(name: str):
    raise ValueError(f"Unexpected token {name} in JSON")


def _parse_int(s: str):
    n = int(s)
    return n if abs(n) < 2**53 else float(n)


def json_parse(text: str) -> Any:
    """JSON.parse: no NaN/Infinity; integers past 2^53 lose precision as in JS."""
    return json.loads(text, parse_constant=_reject_constant, parse_int=_parse_int)


_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_NEEDS_ESC = re.compile(r'["\\\x00-\x1f\ud800-\udfff]')


def _quote(s: str) -> str:
    def rep(m: re.Match) -> str:
        c = m.group(0)
        if c in _ESC:
            return _ESC[c]
        return "\\u%04x" % ord(c)

    if not _NEEDS_ESC.search(s):
        return '"' + s + '"'
    # Paired surrogates can't occur in a Python str (they are one code point), so any surrogate here is lone.
    return '"' + _NEEDS_ESC.sub(rep, s) + '"'


def json_stringify(v: Any, indent: int | None = None) -> str:
    """JSON.stringify(v[, null, indent]): JS number formatting, NaN/Infinity as null."""
    out: list[str] = []

    def w(x: Any, level: int) -> None:
        if x is None:
            out.append("null")
        elif x is True:
            out.append("true")
        elif x is False:
            out.append("false")
        elif is_num(x):
            out.append(js_str(x) if math.isfinite(x) else "null")
        elif isinstance(x, str):
            out.append(_quote(x))
        elif isinstance(x, (list, tuple)):
            if not x:
                out.append("[]")
                return
            out.append("[")
            for i, item in enumerate(x):
                if i:
                    out.append(",")
                if indent:
                    out.append("\n" + " " * (indent * (level + 1)))
                w(item, level + 1)
            if indent:
                out.append("\n" + " " * (indent * level))
            out.append("]")
        elif isinstance(x, dict):
            items = list(x.items())
            if not items:
                out.append("{}")
                return
            out.append("{")
            for i, (k, item) in enumerate(items):
                if i:
                    out.append(",")
                if indent:
                    out.append("\n" + " " * (indent * (level + 1)))
                out.append(_quote(str(k)) + (": " if indent else ":"))
                w(item, level + 1)
            if indent:
                out.append("\n" + " " * (indent * level))
            out.append("}")
        else:
            raise TypeError(f"not JSON: {type(x).__name__}")

    w(v, 0)
    return "".join(out)


def deep_equal(a: Any, b: Any) -> bool:
    """JSON.stringify(a) === JSON.stringify(b) for values built from JSON (key order counts)."""
    return json_stringify(a) == json_stringify(b)


def js_string(v: Any) -> str:
    """String(v) for a JSON value."""
    if v is None:
        return "null"
    if isinstance(v, str):
        return v
    if isinstance(v, bool) or is_num(v):
        return js_str(v)
    if isinstance(v, list):
        return ",".join("" if x is None else js_string(x) for x in v)
    return "[object Object]"
