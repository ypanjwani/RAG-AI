"""Port of the reading half of shared/compact.ts: Lite+'s compact outline format back to JSON.

Format:
  name: value         everything after the first ": " is the value, literally
  name: |             multi-line text on the following, deeper-indented lines
  name:               followed by "- " items (deeper or at the same indent)
  - name: value       a list item that is an object: its first field; the
    other: value      other fields follow, aligned with the first
  name: a | b | c     a short list of plain values on one line (numbers may
  name: 1, 10, 100    also be separated by commas); table rows likewise
Fields left out get empty defaults ("" / [] / false) when the schema needs them.

Reading is schema-directed (the schema trees exported from shared/spec.ts), so a
value is never guessed: "2" stays text where the schema wants text. Anything
unknown is kept as text, and parse_outline drops what doesn't validate.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .. import data
from ..js import JS_WS_CHARS, WS, js_number, js_str, js_trim, js_trim_end, js_trim_start, json_parse

INF = math.inf
_LINE_CHARS = r"[^\n\r  ]"  # JS "." without the s flag
KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_]*):(?:" + WS + r"+(" + _LINE_CHARS + r"*)|" + WS + r"*)", re.A)
_DASH = re.compile(r"^-" + WS + r"*", re.A)


class _Line:
    __slots__ = ("indent", "text", "raw")

    def __init__(self, indent: int, text: str, raw: str):
        self.indent, self.text, self.raw = indent, text, raw


class _Node:
    __slots__ = ("line", "depth", "children", "blank")

    def __init__(self, line: _Line, depth: float, blank: bool):
        self.line, self.depth, self.children, self.blank = line, depth, [], blank


def _lines(text: str) -> list[_Line]:
    out = []
    for raw in re.split(r"\r?\n", text.replace("\t", "  ")):
        out.append(_Line(len(raw) - len(js_trim_start(raw)), js_trim(raw), raw))
    return out


def _tree(all_lines: list[_Line]) -> list[_Node]:
    """The indentation tree. Blank lines join the deepest open node (they matter only inside text)."""
    root = _Node(_Line(-1, "", ""), -2, False)
    stack = [root]
    for line in all_lines:
        if not line.text:
            stack[-1].children.append(_Node(line, INF, True))
            continue
        depth = line.indent + (1 if line.text.startswith("- ") or line.text == "-" else 0)
        while len(stack) > 1 and stack[-1].depth >= depth:
            stack.pop()
        node = _Node(line, depth, False)
        stack[-1].children.append(node)
        stack.append(node)
    return root.children


def _raw_text(nodes: list[_Node]) -> str:
    flat: list[_Line] = []

    def walk(ns):
        for n in ns:
            flat.append(n.line)
            walk(n.children)

    walk(nodes)
    inds = [ln.indent for ln in flat if ln.text]
    ind = min(inds) if inds else 0
    text = "\n".join(js_trim_end(ln.raw[ind:]) if ln.text else "" for ln in flat)
    return re.sub(r"^\n+|\n+\Z", "", text)


def _unquote(v: str) -> str:
    """Strip quotes the model may add out of habit: a JSON string is decoded, 'single' quotes removed."""
    if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
        try:
            s = json_parse(v)
            if isinstance(s, str):
                return s
        except ValueError:
            pass
        return v[1:-1]
    if len(v) >= 2 and v.startswith("'") and v.endswith("'") and "'" not in v[1:-1]:
        return v[1:-1]
    return v


def _real(ns: list[_Node]) -> list[_Node]:
    return [n for n in ns if not n.blank]


class _Entry:
    __slots__ = ("value", "children")

    def __init__(self, value: str | None, children: list[_Node]):
        self.value, self.children = value, children


def _key(text: str):
    return KEY.fullmatch(text)


def _fields(nodes: list[_Node]) -> dict[str, _Entry]:
    out: dict[str, _Entry] = {}
    for n in _real(nodes):
        m = _key(n.line.text)
        if m and m.group(1) not in out:
            out[m.group(1)] = _Entry(js_trim(m.group(2)) if m.group(2) is not None else None, n.children)
    return out


def _item_fields(n: _Node) -> dict[str, _Entry]:
    """A "- " list item as an object: the first field is on the dash line; deeper lines that follow belong to it."""
    first = _DASH.sub("", n.line.text, count=1)
    col = n.line.indent + (len(n.line.text) - len(first))
    own: list[_Node] = []
    i = 0
    while i < len(n.children):
        c = n.children[i]
        if not c.blank and c.depth <= col:
            break
        own.append(c)
        i += 1
    rest = _fields(n.children[i:])
    m = _key(first)
    if not m:
        return rest
    out = {m.group(1): _Entry(js_trim(m.group(2)) if m.group(2) is not None else None, own)}
    out.update(rest)
    return out


def _kind(s: dict | None) -> str:
    return s["t"] if s else "unknown"


def _unwrap(s: dict) -> dict:
    while s and s["t"] == "optional":
        s = s["inner"]
    return s


def _top_level_commas(v: str) -> list[str]:
    out: list[str] = []
    depth = 0
    cur = ""
    for ch in v:
        if ch in "([{":
            depth += 1
        if ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return [*out, cur]


def _js_string(x: Any) -> str:
    """String(x) for a JSON scalar."""
    if x is None:
        return "null"
    if isinstance(x, bool) or isinstance(x, (int, float)):
        return js_str(x)
    return x


def _split_inline(v: str, element: dict) -> list[str]:
    t = _kind(_unwrap(element))
    if re.fullmatch(r"\[" + _LINE_CHARS + r"*\]", v) and t != "array":
        try:
            a = json_parse(v)
            if isinstance(a, list) and all(not isinstance(x, (dict, list)) and x is not None for x in a):
                return [_js_string(x) for x in a]
        except ValueError:
            pass
        return [p for p in (_unquote(js_trim(p)) for p in _top_level_commas(v[1:-1])) if p]
    if t == "number":
        parts = re.split(WS + r"*[,|]" + WS + r"*", v)
    else:
        parts = re.split(WS + r"+\|" + WS + r"+", v)
    parts = [js_trim(p) for p in parts]
    return [p for p in parts if p != "" or len(parts) > 1]


_TRUE = re.compile(r"(true|yes|on|1)", re.I | re.A)
_FALSE = re.compile(r"(false|no|off|0)", re.I | re.A)


def _scalar(schema: dict, e: _Entry) -> Any:
    t = _kind(schema)
    if t == "string":
        inline = "" if e.value in ("|", ">") else _unquote(e.value if e.value is not None else "")
        more = _raw_text(e.children) if _real(e.children) else ""
        return "\n".join(x for x in (inline, more) if x)
    v = _unquote(js_trim(e.value if e.value is not None else ""))
    if t == "number":
        n = js_number(v)
        return n if v != "" and math.isfinite(n) else v
    if t == "boolean":
        if _TRUE.fullmatch(v):
            return True
        if _FALSE.fullmatch(v):
            return False
        return v
    return v  # enum, literal


def _read(schema: dict, e: _Entry) -> Any:
    s = _unwrap(schema)
    t = _kind(s)
    if t == "object":
        return _read_object(s, _fields(e.children))
    if t == "array":
        items = [n for n in _real(e.children) if n.line.text.startswith("-")]
        if items:
            return [_read_item(s["element"], n) for n in items]
        # A whole list written as JSON on one line, e.g. weights: [[1, 0.2], [0.5, 1]].
        if e.value and re.fullmatch(r"\[[\s\S]*\]", e.value):
            try:
                a = json_parse(e.value)
                if isinstance(a, list):
                    return a
            except ValueError:
                pass
        return [_read(s["element"], _Entry(p, [])) for p in _split_inline(e.value, s["element"])] if e.value else []
    if t == "union":
        return _read_block(_fields(e.children))
    if t == "unknown":
        return _generic(e)
    return _scalar(s, e)


def _read_item(element: dict, n: _Node) -> Any:
    s = _unwrap(element)
    t = _kind(s)
    text = _DASH.sub("", n.line.text, count=1)
    if t == "object":
        return _read_object(s, _item_fields(n))
    if t == "union":
        return _read_block(_item_fields(n))
    return _read(s, _Entry(text, n.children))


def _read_object(s: dict, f: dict[str, _Entry]) -> dict:
    out: dict = {}
    shape = s["shape"]
    for k, fs in shape.items():
        e = f.get(k)
        if e is not None:
            out[k] = _read(fs, e)
            continue
        if fs["t"] == "optional":
            continue
        t = _kind(_unwrap(fs))
        if t == "string":
            out[k] = ""  # left out = empty
        elif t == "array":
            out[k] = []
        elif t == "boolean":
            out[k] = False
    # Unknown fields are kept (as text or nested), so validation reports them instead of silently losing them.
    for k, e in f.items():
        if k not in out and k not in shape:
            out[k] = _generic(e)
    return out


def _read_block(f: dict[str, _Entry]) -> Any:
    """A block: its "type" picks the schema; an unknown type is read generically (normalize_block may still fix it)."""
    te = f.get("type")
    btype = _unquote(te.value if te is not None and te.value is not None else "")
    schema = data.schemas()["blocks"].get(btype)
    if schema and btype != "widget":
        return _read_object(schema, f)
    return {k: _generic(e) for k, e in f.items()}


def _generic(e: _Entry) -> Any:
    """No schema: text, a list, or an object, judged from the lines."""
    kids = _real(e.children)
    if not kids:
        return _unquote(e.value if e.value is not None else "")
    if all(n.line.text.startswith("-") for n in kids):
        out = []
        for n in kids:
            f = _item_fields(n)
            out.append({k: _generic(x) for k, x in f.items()} if f else _unquote(_DASH.sub("", n.line.text, count=1)))
        return out
    if all(_key(n.line.text) for n in kids):
        return {k: _generic(x) for k, x in _fields(e.children).items()}
    return _scalar({"t": "string"}, e)


def _body(text: str) -> str:
    """Drop a code fence or a short preamble the model may put around the outline."""
    t = js_trim(text)
    t = re.sub(r"^```[a-z]*" + WS + r"*\n", "", t, count=1, flags=re.I | re.A)
    t = re.sub(r"\n```" + WS + r"*\Z", "", t, count=1)
    m = re.search(r"^title:", t, re.M)
    if m and m.start() > 0:
        t = t[m.start():]
    return t


def parse_compact(text: str) -> dict:
    """The planner's compact-format outline as its JSON shape (not yet validated). Raises when there is nothing to read."""
    nodes = _tree(_lines(_body(text)))
    f = _fields(nodes)
    if not f:
        raise ValueError('the reply is not in the compact format (no "name: value" lines)')
    return _rescue_glossary(_read_object(data.schemas()["promptOutline"], f))


def _rescue_glossary(o: dict) -> dict:
    """A misplaced "glossary:" turns the glossary entries into extra label-less "stages". Move them back."""
    stages = o.get("stages") or []

    def is_term(s: Any) -> bool:
        return isinstance(s, dict) and not s.get("label") and not s.get("id") and isinstance(s.get("term"), str)

    terms = [s for s in stages if is_term(s)]
    if not terms:
        return o
    glossary = [*(o.get("glossary") or []), *({"term": t["term"], "definition": t["definition"] if t.get("definition") is not None else ""} for t in terms)]
    return {**o, "stages": [s for s in stages if not is_term(s)], "glossary": glossary}


__all__ = ["parse_compact", "JS_WS_CHARS"]
