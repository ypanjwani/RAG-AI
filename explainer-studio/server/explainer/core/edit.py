"""Port of the parts of shared/edit.ts the server needs (the fact check applies fixes through them):
leaves (every value of a block with its kind), get_at / set_at, apply_changes and field_label."""

from __future__ import annotations

import re
from typing import Any

from .. import data
from ..js import is_finite, is_num, js_number, js_str, json_stringify

Path = list  # of str | int

EXPR_KEYS = {"expr", "trial", "markerX", "init", "truth", "surface"}
PROSE_KEYS = {"md", "back", "explain", "desc", "detail", "focus", "meaning"}
ID_KEYS = {"var", "id", "stage", "sweepVar"}
WRITABLE = {"text", "prose"}
EDITABLE = {"text", "prose", "latex", "number", "answer", "bool", "enum", "expr"}


def key_of(path: Path) -> str:
    """A path's last named key (array indices skipped)."""
    for k in reversed(path):
        if isinstance(k, str):
            return k
    return ""


def enum_options(btype: str, path: Path) -> list[str] | None:
    """Menu options for an enum field. Like the TypeScript version (zod 4's unwrap() also steps into an
    array's element), a path through a list never finds one."""

    def unwrap(s):
        while s and s["t"] not in ("enum", "union") and s["t"] in ("optional", "array"):
            s = s["inner"] if s["t"] == "optional" else s["element"]
        return s

    s = data.schemas()["blocks"].get(btype)
    for k in path:
        s = unwrap(s)
        if isinstance(k, int):
            s = s.get("element") if s and s["t"] == "array" else None
        else:
            s = s["shape"].get(k) if s and s["t"] == "object" else None
        if not s:
            return None
    s = unwrap(s)
    return list(s["values"]) if s and s["t"] == "enum" else None


def _kind_of(btype: str, path: Path, value: Any) -> str:
    key = key_of(path)
    top = path[0] if path else None
    if isinstance(value, bool):
        return "bool"
    if enum_options(btype, path):
        return "enum"
    if is_num(value):
        if key == "answer":
            return "answer"
        if btype == "attention" or (btype == "sampling" and key == "logit"):
            return "data"
        return "number"
    s = value if isinstance(value, str) else js_str(value)
    if key in ID_KEYS:
        return "id"
    # A chart's axes name slider variables (a heatmap's or trajectory's x/y are formulas instead).
    if btype == "chart" and key in ("x", "by"):
        return "id"
    # A scene's ids, targets, axes and fixed choices (enums inside its lists aren't found by enum_options); its live properties are formulas.
    if btype == "scene":
        if key in ("target", "x", "by", "kind", "when", "view", "format"):
            return "id"
        if key in ("visible", "disabled", "highlight", "if", "values"):
            return "expr"
        if key == "rows" and s.startswith("="):
            return "expr"
    # Names of derived and state variables: formulas refer to them, so renaming one would break them.
    if key == "name" and top in ("defs", "state", "update"):
        return "id"
    if btype in ("conceptmap", "architecture") and top == "edges" and key in ("from", "to"):
        return "id"
    if key in ("latex", "symbol"):
        return "latex"
    if key in EXPR_KEYS:
        return "expr"
    if btype == "trajectory" and len(path) == 1 and key == "steps":
        return "expr"
    if key in ("from", "to") and top in ("shade", "band"):
        return "expr"
    if key in ("x", "y") and btype in ("trajectory", "heatmap"):
        return "expr"
    if btype == "bars" and top == "values":
        return "expr"
    if btype == "samples" and key == "values":
        return "expr"
    if btype == "table" and top == "rows" and s.startswith("="):
        return "expr"
    if (btype == "tokens" and top == "tokens") or (btype in ("attention", "sampling") and (top == "tokens" or key == "token")):
        return "data"
    if key in PROSE_KEYS or (btype == "reveal" and key == "answer"):
        return "prose"
    return "text"


def leaves(b: dict) -> list[dict]:
    """Every leaf value of a block with its kind, in document order: {"path", "kind", "value"[, "options"]}."""
    out: list[dict] = []

    def walk(v: Any, path: Path) -> None:
        if v is None:
            return
        if isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, [*path, i])
            return
        if isinstance(v, dict):
            for k, x in v.items():
                if not (not path and k == "type"):
                    walk(x, [*path, k])
            return
        kind = _kind_of(b["type"], path, v)
        leaf = {"path": path, "kind": kind, "value": v}
        if kind == "enum":
            leaf["options"] = enum_options(b["type"], path)
        out.append(leaf)

    walk(b, [])
    return out


def get_at(b: Any, path: Path) -> Any:
    o = b
    for k in path:
        if o is None:
            return None
        if isinstance(o, dict):
            o = o.get(k) if isinstance(k, str) else o.get(js_str(k))
        elif isinstance(o, list):
            o = o[k] if isinstance(k, int) and 0 <= k < len(o) else None
        elif isinstance(o, str) and isinstance(k, int):
            o = o[k] if 0 <= k < len(o) else None
        else:
            o = None
    return o


def set_at(b: Any, path: Path, value: Any) -> Any:
    """A copy of the block with the value at path replaced (structural sharing elsewhere)."""
    if not path:
        return value
    k, rest = path[0], path[1:]
    if isinstance(b, list):
        copy = list(b)
        while len(copy) <= k:
            copy.append(None)
        copy[k] = set_at(b[k] if k < len(b) else None, rest, value)
        return copy
    copy = dict(b) if isinstance(b, dict) else {}
    copy[k] = set_at(copy.get(k), rest, value)
    return copy


NAMES = {
    "md": "Text", "q": "Question", "expr": "Formula", "markerX": "Marker at x", "xMin": "X from", "xMax": "X to", "yMin": "Y from", "yMax": "Y to",
    "xLabel": "X axis label", "yLabel": "Y axis label", "zLabel": "Colour label", "logY": "Log scale (y)", "showValidation": "Show validation curve",
    "normalize": "Show as % of total", "diverging": "Two-colour scale", "digits": "Decimals", "value": "Value", "valueLabel": "What one run measures",
    "breaksIf": "Breaks when", "front": "Front", "back": "Back", "explain": "Explanation", "latex": "Maths", "sub": "Subtitle", "desc": "Description",
    "init": "Start value", "trial": "One run", "truth": "True value", "repeats": "Run buttons", "on": "On label", "off": "Off label",
    "lesson": "What it should show", "view": "Chart view", "at": "Values shown",
}
SINGULAR = {
    "series": "Series", "items": "Item", "questions": "Question", "cards": "Card", "tabs": "Tab", "options": "Option", "nodes": "Node", "edges": "Link",
    "rows": "Row", "steps": "Step", "points": "Point", "choices": "Choice", "labels": "Label", "values": "Value", "stats": "Statistic", "markers": "Marker",
    "candidates": "Candidate", "state": "State variable", "update": "Update rule", "terms": "Symbol", "outcomes": "Outcome", "tokens": "Token",
}


def humanize(k: str) -> str:
    if k in NAMES:
        return NAMES[k]
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", k)
    return s[:1].upper() + s[1:] if s else s


def item_name(list_key: str, i: int) -> str:
    return f"{SINGULAR.get(list_key) or humanize(list_key)} {i + 1}"


def field_label(path: Path) -> str:
    parts: list[str] = []
    for i, k in enumerate(path):
        if not isinstance(k, str):
            continue
        nxt = path[i + 1] if i + 1 < len(path) else None
        parts.append(item_name(k, nxt) if isinstance(nxt, int) and not isinstance(nxt, bool) else humanize(k))
    return " › ".join(parts) or "Value"


def apply_changes(b: dict, changes: list[dict]) -> dict:
    """{"block", "textChanged"} or {"error"}. Only existing, editable values; each checked against its kind."""
    nxt = b
    text_changed = False
    for ch in changes:
        path = ch.get("path")
        if not isinstance(path, list) or not path:
            return {"error": "Each change needs a path (a JSON array)."}
        key = json_stringify(path)
        leaf = next((l for l in leaves(nxt) if json_stringify(l["path"]) == key), None)
        if leaf is None:
            return {"error": f"{key} is not a value of this part. Use a path from the Values list."}
        if leaf["kind"] not in EDITABLE:
            return {"error": f"{field_label(path)} is locked (variable names and ids can't change)."}
        v = ch.get("value")
        if leaf["kind"] in ("number", "answer"):
            v = js_number(v) if isinstance(v, str) else v
            if not is_finite(v):
                return {"error": f"{field_label(path)} must be a number."}
        elif leaf["kind"] == "bool":
            if not isinstance(v, bool):
                return {"error": f"{field_label(path)} must be true or false."}
        else:
            if not isinstance(v, str):
                return {"error": f"{field_label(path)} must be text."}
            if leaf["kind"] == "enum" and v not in (leaf.get("options") or []):
                return {"error": f"{field_label(path)} must be one of: {', '.join(leaf.get('options') or [])}."}
        if leaf["kind"] in WRITABLE and v != leaf["value"]:
            text_changed = True
        nxt = set_at(nxt, path, v)
    return {"block": nxt, "textChanged": text_changed}
