"""Validation the way zod's safeParse does it, over the schema trees exported from shared/spec.ts.

Each node is {"t": kind, ...}: object (shape), optional (inner), array (element),
union (options), number (int), enum (values), literal (values), string, boolean,
unknown. Like zod 4: unknown object keys are dropped, output objects follow the
schema's key order, a union returns its first matching option or one
"Invalid input" issue, and the issue messages match zod's word for word (they
end up in the warnings the reader sees).
"""

from __future__ import annotations

import math
from typing import Any

from ..js import is_num

MISSING = object()
MAX_SAFE = 9007199254740991

Issue = dict[str, Any]  # {"path": [...], "message": str}


def _received(v: Any) -> str:
    if v is MISSING:
        return "undefined"
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if is_num(v):
        if v != v:
            return "NaN"
        if math.isinf(v):
            return "Infinity"
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def _bad_type(expected: str, v: Any, path: list) -> Issue:
    return {"path": path, "message": f"Invalid input: expected {expected}, received {_received(v)}"}


def parse(node: dict, v: Any, path: list | None = None) -> tuple[Any, list[Issue]]:
    """Returns (value, issues); the value is only meaningful when issues is empty."""
    path = path or []
    t = node["t"]
    if t == "object":
        if not isinstance(v, dict):
            return None, [_bad_type("object", v, path)]
        out: dict = {}
        issues: list[Issue] = []
        for k, child in node["shape"].items():
            x = v.get(k, MISSING)
            if child["t"] == "optional" and x is MISSING:
                continue
            val, iss = parse(child, x, path + [k])
            if iss:
                issues.extend(iss)
            elif val is not MISSING:
                out[k] = val
        return out, issues
    if t == "optional":
        if v is MISSING:
            return MISSING, []
        return parse(node["inner"], v, path)
    if t == "array":
        if not isinstance(v, list):
            return None, [_bad_type("array", v, path)]
        out_l: list = []
        issues = []
        for i, x in enumerate(v):
            val, iss = parse(node["element"], x, path + [i])
            if iss:
                issues.extend(iss)
            else:
                out_l.append(val)
        return out_l, issues
    if t == "union":
        for opt in node["options"]:
            val, iss = parse(opt, v, path)
            if not iss:
                return val, []
        return None, [{"path": path, "message": "Invalid input"}]
    if t == "string":
        return (v, []) if isinstance(v, str) else (None, [_bad_type("string", v, path)])
    if t == "boolean":
        return (v, []) if isinstance(v, bool) else (None, [_bad_type("boolean", v, path)])
    if t == "number":
        if not is_num(v) or not math.isfinite(v):
            return None, [_bad_type("number", v, path)]
        if node.get("int"):
            if not float(v).is_integer():
                return None, [_bad_type("int", v, path)]
            if v > MAX_SAFE:
                return None, [{"path": path, "message": f"Too big: expected int to be <={MAX_SAFE}"}]
            if v < -MAX_SAFE:
                return None, [{"path": path, "message": f"Too small: expected int to be >=-{MAX_SAFE}"}]
        return v, []
    if t == "enum":
        if isinstance(v, str) and v in node["values"]:
            return v, []
        return None, [{"path": path, "message": "Invalid option: expected one of " + "|".join(f'"{x}"' for x in node["values"])}]
    if t == "literal":
        for lit in node["values"]:
            if type(v) is type(lit) and v == lit:
                return v, []
        lits = node["values"]
        shown = f'"{lits[0]}"' if len(lits) == 1 and isinstance(lits[0], str) else "|".join(f'"{x}"' for x in lits)
        return None, [{"path": path, "message": f"Invalid input: expected {shown}"}]
    if t == "unknown":
        return v, []
    raise ValueError(f"unknown schema node {t!r}")


def safe_parse(node: dict, v: Any) -> tuple[bool, Any]:
    """(True, data) or (False, issues)."""
    val, issues = parse(node, v)
    return (False, issues) if issues else (True, val)
