"""Lab mode's plan step and the checks around it (server only; scenes.py runs the pass).

1. gate: Jev judges each section with four narrow yes/no questions (does a quantity vary with a setting? does a
   process unfold over steps? does it compare two settings or methods? is it mainly definitions/advice?).
2. Haiku writes a plan (LAB_PLAN_SYSTEM, format labPlan) before anything is built; check_plan validates its shape.
3. After Haiku builds the scene from the plan, check_build moves each variable from its min to its max and compares
   what every visual part actually draws (drawn_values mirrors app/src/render/components/chart.ts), so a plan's
   "moving rho changes the chart" is verified, and so is "every slider moves every visual that reads it".

The thresholds are first guesses, to be tuned on hand-labelled sections like the fact check's were.
"""

from __future__ import annotations

import re
from typing import Any

from .core.expr import eval_number, eval_value
from .core.scene import LIMITS, PLACEHOLDER, RESERVED
from .core.validate import vars_of
from .core.views import with_defs
from .js import is_finite, is_num, js_slice

GATE_AT = 0.6  # a lab when the strongest of varies/process/compare is at or above this
STATIC_AT = 0.7  # ... unless "mainly definitions, history or advice" is at or above this
SECTION_CHARS = 2500
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.A)
PATTERN_OF = {"varies": "sweep", "process": "play or step", "compare": "compare", "structure": "diagram"}
HINT_OF = {
    "varies": "a quantity that changes when a setting changes",
    "process": "a process that unfolds over steps, rounds or time",
    "compare": "a comparison of two settings or two methods",
    "structure": "a structure of connected parts (layers, steps, tokens linking to each other) the reader can explore",
}

# Sampling for drawn_values: enough to tell whether a part changed, not to draw it.
SAMPLES, GRID, DRAWS, BAND_DRAWS = 24, 8, 200, 40
SEEN = ("chart", "readout", "bars", "table", "text")
# Words from the designer's instructions that mean nothing to a reader.
JARGON = re.compile(r"\b(page[- ]controls?|state variables?|rule variables?|reader variables?|scene state)\b", re.I)
# Chart views that draw a second variable as several lines, histograms or boxes.
BY_VIEWS = ("lines", "compare", "box", "heatmap")


# ---------- 1. the Jev gate ----------


FULL_SECTION_CHARS = 8000  # the whole section as text (full=True): tables and cards can be long
# Reading blocks whose words full=True passes to Jev as text, not just as "type: title".
READ_BLOCKS = ("text", "callout", "table", "cards", "tabs", "compare", "reveal", "flipcards")


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def block_text(b: dict) -> str:
    """One reading block's words as plain text, for Jev (tables row by row, cards and tabs item by item).
    Empty for blocks that aren't READ_BLOCKS."""
    t = b.get("type")
    if t == "text":
        return _s(b.get("md"))
    if t == "callout":
        return f"{_s(b.get('title'))}: {_s(b.get('md'))}".strip(": ")
    if t == "table":
        cols = [_s(c) for c in b.get("columns") or []]
        rows = [" | ".join(_s(c) for c in r) for r in b.get("rows") or [] if isinstance(r, list)]
        head = f"Table: {_s(b.get('title'))}".rstrip(": ")
        return "\n".join(x for x in [head, " | ".join(cols), *rows, _s(b.get("caption"))] if x)
    if t == "cards":
        items = []
        for it in b.get("items") or []:
            if not isinstance(it, dict):
                continue
            extra = "; ".join(f"{k}: {_s(it.get(k))}" for k in ("value", "breaksIf", "evidence") if _s(it.get(k)))
            items.append(f"- {_s(it.get('title'))}: {_s(it.get('md'))}" + (f" ({extra})" if extra else ""))
        return "\n".join(x for x in [_s(b.get("title")), *items] if x)
    if t == "tabs":
        return "\n".join(f"{_s(x.get('label'))}: {_s(x.get('md'))}" for x in b.get("tabs") or [] if isinstance(x, dict))
    if t == "compare":
        sides = [b.get("left") or {}, b.get("right") or {}]
        return "\n".join(f"{_s(s.get('title'))}: " + "; ".join(_s(p) for p in s.get("points") or []) for s in sides if isinstance(s, dict))
    if t == "reveal":
        return f"Question: {_s(b.get('prompt'))}\nAnswer: {_s(b.get('answer'))}"
    if t == "flipcards":
        return "\n".join(f"{_s(c.get('front'))}: {_s(c.get('back'))}" for c in b.get("cards") or [] if isinstance(c, dict))
    return ""


def section_text(st: dict, full: bool = True) -> str:
    """A section's words for Jev: its text and callouts, and with full=True also its tables, cards, tabs, comparisons,
    reveals and flip cards (so a term the section explains only in a table still counts as something it talks about)."""
    kinds = READ_BLOCKS if full else ("text", "callout")
    return "\n\n".join(x for x in (block_text(b) for b in st["blocks"] if b.get("type") in kinds) if x)


def gate_state(outline: dict, si: int, full: bool = False) -> dict:
    """What Jev's gate reads about one section. full=True (Code Lab) passes tables, cards and the other reading blocks
    as text too; Lab mode keeps text and callouts only, with the other blocks named by type and title."""
    st = outline["stages"][si]
    prose = section_text(st, full)
    read = READ_BLOCKS if full else ("text", "callout")
    parts = []
    for b in st["blocks"]:
        if b["type"] in read:
            continue
        if b["type"] == "formula":
            parts.append(f"formula: {b.get('latex', '')}")
        else:
            parts.append(b["type"] + (f": {b['title']}" if b.get("title") else ""))
    return {
        "explainer": {"title": outline.get("title", ""), "level": outline.get("level", "")},
        "section": {"label": st.get("label", ""), "summary": st.get("blurb", ""), "text": js_slice(prose, 0, FULL_SECTION_CHARS if full else SECTION_CHARS)},
        "section_parts": parts,
    }


GATE_QUESTIONS: dict[str, dict] = {
    "varies": {
        "type": "noul",
        "instructions": "Does `section` describe a quantity whose value changes when a setting, input or parameter changes?",
        "criteria": {
            "true": "`section` explains how some number (an error, a probability, a score, a size) depends on a setting the reader could vary, for example through a formula or a described relationship.",
            "false": "`section` names no quantity that depends on a setting; it defines terms, gives facts or gives advice.",
        },
    },
    "process": {
        "type": "noul",
        "instructions": "Does `section` describe a process that unfolds over steps, rounds, iterations or time?",
        "criteria": {
            "true": "`section` describes something done repeatedly or in stages (training rounds, iterations, a sequence of updates, an algorithm's steps) whose state changes from step to step.",
            "false": "`section` describes nothing that proceeds step by step.",
        },
    },
    "compare": {
        "type": "noul",
        "instructions": "Does `section` compare two or more settings, methods or cases whose results differ in a way that could be shown side by side?",
        "criteria": {
            "true": "`section` contrasts methods or settings (for example two algorithms, a small and a large value) and says how their results differ.",
            "false": "`section` does not compare methods or settings, or only lists their features without results that differ.",
        },
    },
    "static": {
        "type": "noul",
        "instructions": "Is `section` mainly definitions, history, a summary or practical advice?",
        "criteria": {
            "true": "Most of `section` is definitions, background, a recap or when-to-use advice, with no mechanism to explore.",
            "false": "`section` explains a mechanism, a formula or a process that the reader could explore.",
        },
    },
}

# Playground only (codelab.gate_one(playground=True)): sections about connected parts were closed by the three questions
# above, so Playground never drew a network, attention links or a pipeline (Oct 30).
PG_GATE_QUESTIONS: dict[str, dict] = {
    **GATE_QUESTIONS,
    "structure": {
        "type": "noul",
        "instructions": "Does `section` describe a structure of connected parts, such as layers of a network, tokens that attend to each other, the stages of a pipeline or a tree of decisions?",
        "criteria": {
            "true": "`section` explains how parts are connected or how something flows between them (an architecture, layers and weights, links between tokens, a pipeline, a tree), which the reader could explore in a diagram.",
            "false": "`section` describes no connected parts; it is about a single quantity, facts or advice.",
        },
    },
}


def gate_verdict(answers: dict) -> dict:
    """{"open", "why" (varies|process|compare|None), "p": {question: p}} from Jev's answers."""
    p = {k: answers[k]["p"] for k in GATE_QUESTIONS if k in answers}
    best = max(("varies", "process", "compare"), key=lambda k: p.get(k, 0))
    opened = p.get(best, 0) >= GATE_AT and p.get("static", 0) < STATIC_AT
    return {"open": opened, "why": best if opened else None, "p": p}


def gate_hint(why: str | None) -> str:
    return f"{HINT_OF[why]} (a {PATTERN_OF[why]} lab may fit)" if why in HINT_OF else "a quantity that varies or a process that unfolds"


# ---------- 2. the plan's shape ----------


def check_plan(plan: Any, page_vars: set[str], n_blocks: int) -> tuple[dict | None, list[str]]:
    """(plan or None, problems). None with no problems means the planner chose no lab."""
    if not isinstance(plan, dict):
        return None, ["the plan isn't an object"]
    if not plan.get("build"):
        return None, []
    problems: list[str] = []
    # A new variable named like a page control is that control: the lab reads it instead of copying it.
    variables = [{**v, "role": "page"} if v.get("name") in page_vars else v for v in plan.get("variables") or [] if isinstance(v, dict)]
    names: set[str] = set()
    for v in variables:
        n = v.get("name")
        if v.get("role") == "page":
            if n in page_vars and n not in names:
                names.add(n)
            else:
                problems.append(f'variable "{n}" isn\'t a page control')
        elif not (isinstance(n, str) and IDENT.fullmatch(n)) or n in RESERVED:
            problems.append(f'variable "{n}" isn\'t a usable name')
        elif n in names:
            problems.append(f'variable "{n}" is declared twice')
        elif not all(is_num(v.get(k)) for k in ("min", "max", "value")) or v["min"] >= v["max"]:
            problems.append(f'variable "{n}" has no usable range')
        else:
            names.add(n)
    if not variables:
        problems.append("it has no variables")
    if sum(v.get("role") != "page" for v in variables) > LIMITS["state"]:
        problems.append(f"it has more than {LIMITS['state']} variables of its own")
    ids = [x.get("id") for x in [*(plan.get("shows") or []), *(plan.get("buttons") or [])] if isinstance(x, dict)]
    for i in ids:
        if not (isinstance(i, str) and IDENT.fullmatch(i)):
            problems.append(f'part id "{i}" isn\'t an identifier')
    if len(set(ids)) < len(ids):
        problems.append("two parts share an id")
    shows = {x["id"] for x in plan.get("shows") or [] if isinstance(x, dict) and isinstance(x.get("id"), str)}
    if not shows:
        problems.append("it shows nothing")
    expects = [e for e in plan.get("expects") or [] if isinstance(e, dict)]
    covered = {e.get("var") for e in expects}
    for v in variables:
        if v.get("role") == "reader" and v.get("name") not in covered:
            problems.append(f'reader variable "{v.get("name")}" has no expects entry')
    for e in expects:
        if e.get("var") not in names:
            problems.append(f'expects names "{e.get("var")}", which isn\'t a plan variable')
        for c in e.get("changes") or []:
            if c not in shows:
                problems.append(f'expects "{e.get("var")}" to change "{c}", which isn\'t a visual part')
        if not e.get("changes"):
            problems.append(f'expects "{e.get("var")}" to change nothing')
    raw = plan.get("contrast") if isinstance(plan.get("contrast"), dict) else {}
    contrast = {"items": [x for x in raw.get("items") or [] if isinstance(x, str) and x.strip()], "part": raw.get("part") if isinstance(raw.get("part"), str) else ""}
    if len(contrast["items"]) >= 2 and contrast["part"] not in shows:
        problems.append(f'the contrast is shown by "{contrast["part"]}", which isn\'t a visual part')
    replaces = [i for i in plan.get("replaces") or [] if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n_blocks]
    return {**plan, "variables": variables, "expects": expects, "contrast": contrast, "replaces": replaces}, problems


# ---------- 3. the build against the plan ----------


def _nums(v: Any) -> list[float]:
    if is_num(v):
        return [v]
    if isinstance(v, list):
        return [x for row in v for x in (row if isinstance(row, list) else [row]) if is_num(x)]
    return []


def lines_values(at: list[float], now: Any) -> list[float]:
    """The by values a lines chart draws: its at values, plus the current one (linesValues in chart.ts)."""
    if not is_finite(now) or any(abs(v - now) <= 1e-9 * max(1, abs(now)) for v in at):
        return list(at)
    return [*at, now]


def _span(lo: float, hi: float, n: int) -> list[float]:
    return [lo + (hi - lo) * k / n for k in range(n + 1)]


def drawn_values(e: dict, defs: list[dict], scope: dict, ranges: dict[str, tuple[float, float]]) -> list[float]:
    """The numbers element e draws at this scope (NaN for a gap), plus its visible/highlight state."""
    def ev(src: str, extra: dict | None = None) -> float:
        return eval_number(src, with_defs(defs, {**scope, **(extra or {})}))

    out: list[float] = []
    for key in ("visible", "highlight"):
        if e.get(key):
            out.append(ev(e[key]))
    k = e["kind"]
    if k == "readout":
        out.append(ev(e["expr"]))
    elif k == "bars":
        out += _nums(eval_value(e["values"], with_defs(defs, scope)))
    elif k == "table":
        for r in e.get("rows") or []:
            out += [ev(c[1:]) for c in r if c.startswith("=")]
    elif k == "text":
        out += [ev(f) for f in (m.group(1) for m in PLACEHOLDER.finditer(e.get("md") or ""))]
    elif k == "chart":
        series = e.get("series") or []
        view, x, by, at = e.get("view", "line"), e.get("x", ""), e.get("by", ""), e.get("at") or []
        xr, byr = ranges.get(x), ranges.get(by)
        if not series:
            return out
        s0 = series[0]["expr"]
        if view in ("bars", "groupedbars"):
            for s in series if view == "groupedbars" else series[:1]:
                out += _nums(eval_value(s["expr"], with_defs(defs, scope)))
        elif view == "line" and xr:
            for s in series:
                out += [ev(s["expr"], {x: v}) for v in _span(*xr, SAMPLES)] + [ev(s["expr"])]
            out.append(scope.get(x, float("nan")))
        elif view == "lines" and xr and byr:
            for b in lines_values(at, scope.get(by)):
                out += [ev(s0, {x: v, by: b}) for v in _span(*xr, SAMPLES)] + [ev(s0, {by: b})]
            out.append(scope.get(x, float("nan")))
        elif view == "heatmap" and xr and byr:
            out += [ev(s0, {x: u, by: w}) for w in _span(*byr, GRID) for u in _span(*xr, GRID)]
            out += [scope.get(x, float("nan")), scope.get(by, float("nan"))]
        elif view in ("histogram", "running"):
            out += [ev(s0, {"seed": n}) for n in range(1, DRAWS + 1)]
        elif view == "scatter" and xr:
            # Draws spread across x (a non-random series is a line there), plus the marker at x.
            for s in series:
                out += [ev(s["expr"], {x: v, "seed": k + 1}) for k, v in enumerate(_span(*xr, DRAWS // 4))]
            out.append(scope.get(x, float("nan")))
        elif view == "scatter":
            # Without x: the first two series are each point's position (same draw of the defs).
            for n in range(1, DRAWS // 2 + 1):
                out += [ev(s["expr"], {"seed": n}) for s in series[:2]]
        elif view == "band" and xr:
            # Average and spread of the draws at each step across x, plus the marker at x.
            for v in _span(*xr, GRID):
                vals = [ev(s0, {x: v, "seed": n}) for n in range(1, BAND_DRAWS + 1)]
                vals = [w for w in vals if is_finite(w)]
                m = sum(vals) / len(vals) if vals else float("nan")
                out += [m, (sum((w - m) ** 2 for w in vals) / max(1, len(vals) - 1)) ** 0.5 if vals else float("nan")]
            out.append(scope.get(x, float("nan")))
        elif view == "box" and byr:
            # One box per at value plus one at by's current value.
            for b in lines_values(at, scope.get(by)):
                out += [ev(s0, {by: b, "seed": n}) for n in range(1, DRAWS + 1)]
        elif view == "compare" and at and byr:
            for b in (at[0], scope.get(by)):
                out += [ev(s0, {by: b, "seed": n}) for n in range(1, DRAWS + 1)]
    return out


def _differs(a: list[float], b: list[float]) -> bool:
    if len(a) != len(b):
        return True
    for p, q in zip(a, b):
        fp, fq = is_finite(p), is_finite(q)
        if fp != fq or (fp and abs(p - q) > 1e-9 * max(1, abs(p), abs(q))):
            return True
    return False


def _reads(e: dict, defs: list[dict]) -> set[str]:
    srcs = [e.get("visible"), e.get("highlight"), e.get("expr"), e.get("values"), e.get("x"), e.get("by"),
            *[s["expr"] for s in e.get("series") or []],
            *[c[1:] for r in e.get("rows") or [] for c in r if c.startswith("=")],
            *[m.group(1) for m in PLACEHOLDER.finditer(e.get("md") or "")]]
    out: set[str] = set()
    for s in srcs:
        if s:
            out.update(vars_of(s, defs))
    return out


def _shows_several(e: dict, defs: list[dict], scope: dict) -> bool:
    """Whether one visual part draws two or more things side by side (series, a by variable, bars or rows)."""
    if e["kind"] == "chart":
        series = e.get("series") or []
        if len(series) >= 2 or (e.get("by") and e.get("view") in BY_VIEWS):
            return True
        return e.get("view") in ("bars", "groupedbars") and bool(series) and len(_nums(eval_value(series[0]["expr"], with_defs(defs, scope)))) >= 2
    if e["kind"] == "bars":
        return len(_nums(eval_value(e.get("values") or "0", with_defs(defs, scope)))) >= 2
    if e["kind"] == "table":
        return len(e.get("rows") or []) >= 2
    return False


def _reader_text(scene: dict) -> list[tuple[str, str]]:
    """Every string the reader sees in a scene, with where it is."""
    out = [("the lab's title", scene.get("title") or ""), ("the lab's caption", scene.get("caption") or "")]
    for e in scene.get("elements") or []:
        out += [(f'"{e.get("id")}"', e.get(k) or "") for k in ("label", "md")]
        out += [(f'"{e.get("id")}"', s.get("label") or "") for s in e.get("series") or []]
        out += [(f'"{e.get("id")}"', x) for x in [*(e.get("labels") or []), *(e.get("columns") or [])] if isinstance(x, str)]
    return [(w, t) for w, t in out if t]


def check_build(scene: dict, plan: dict, page_scope: dict, page_ranges: dict[str, tuple[float, float]],
                page_labels: dict[str, str] | None = None) -> list[str]:
    """What the built scene gets wrong against its plan (empty = it keeps every promise).
    page_labels: the page sliders' labels by variable, to catch a lab slider that copies one."""
    problems: list[str] = []
    copied = {(lbl or "").strip().lower(): var for var, lbl in (page_labels or {}).items() if (lbl or "").strip()}
    defs = scene.get("defs") or []
    state = {s["name"]: s for s in scene["state"]}
    by_id = {e["id"]: e for e in scene["elements"]}
    for v in plan["variables"]:
        if v.get("role") == "page":
            continue  # a page control: its slider is on the page, the scene only reads it
        if v["name"] not in state:
            problems.append(f'the plan\'s variable "{v["name"]}" is missing from the scene\'s state')
        elif v.get("role") == "reader" and not any(e["kind"] == "slider" and e.get("var") == v["name"] for e in scene["elements"]):
            problems.append(f'the reader variable "{v["name"]}" has no slider')
    for x in [*(plan.get("shows") or []), *(plan.get("buttons") or [])]:
        if x["id"] not in by_id:
            problems.append(f'the plan\'s part "{x["id"]}" is missing from the scene')
    ranges = {**page_ranges, **{n: (s["min"], s["max"]) for n, s in state.items()}}
    base = {**page_scope, **{n: s["value"] for n, s in state.items()}}

    def moves(name: str, e: dict) -> bool:
        # The ends, the middle and the default: a lines chart whose fixed lines sit at both ends only moves in between.
        lo, hi = ranges[name]
        draws = [drawn_values(e, defs, {**base, name: v}, ranges) for v in (lo, (lo + hi) / 2, hi, base.get(name, lo))]
        return any(_differs(draws[0], d) for d in draws[1:])

    def why(e: dict, name: str) -> str:
        if name not in _reads(e, defs):
            return f" (none of its formulas reads {name})"
        if e["kind"] == "chart":
            return f' (it is a {e.get("view")} chart with x = {e.get("x") or "none"} and by = {e.get("by") or "none"})'
        return ""

    for exp in plan["expects"]:
        name = exp["var"]
        if name not in ranges:  # a choice control, or a variable the build lost (reported above)
            continue
        for cid in exp.get("changes") or []:
            e = by_id.get(cid)
            if e and e["kind"] in SEEN and not moves(name, e):
                problems.append(f'moving {name} from {ranges[name][0]:g} to {ranges[name][1]:g} doesn\'t change "{cid}"{why(e, name)}')
    # A chart drawn by a second variable whose formula ignores it draws the same picture for every value.
    for e in scene["elements"]:
        by = e.get("by")
        if e["kind"] == "chart" and by and e.get("view") in ("lines", "heatmap", "compare", "box"):
            reads = set().union(*(vars_of(s["expr"], defs) for s in e.get("series") or [])) if e.get("series") else set()
            if by not in reads:
                problems.append(f'chart "{e["id"]}" is drawn by {by} ({e["view"]} view), but its formula never reads {by}, so every {by} looks the same: drop by or use it in the formula')
    # A contrast must be drawn together in its one part, not as separate numbers.
    contrast = plan.get("contrast") or {}
    items = contrast.get("items") or []
    part = by_id.get(contrast.get("part") or "")
    if len(items) >= 2 and part and not _shows_several(part, defs, base):
        problems.append(f'the lesson compares {" and ".join(items)}, but "{part["id"]}" shows only one thing: draw them together (one series each in one chart, a by variable, or one bar or row each)')
    # Reader-facing words: the instructions' terms mean nothing to a reader.
    for where, text in _reader_text(scene):
        m = JARGON.search(text)
        if m:
            problems.append(f'{where} says "{m.group(0)}", which readers don\'t know: name the slider or part by what it shows instead (e.g. "the temperature slider above")')
            break
    # Plan or not: a slider must move every visual part that reads it, and must not copy a page slider.
    for sl in scene["elements"]:
        name = sl.get("var")
        if sl["kind"] != "slider" or name not in state:
            continue
        twin = copied.get((sl.get("label") or state[name]["label"] or "").strip().lower())
        if twin:
            problems.append(f'slider "{sl["id"]}" copies the page\'s slider for {twin}: read {twin} by name instead of a new variable')
        for e in scene["elements"]:
            if e["kind"] in SEEN and name in _reads(e, defs) and not moves(name, e):
                msg = f'moving {name} from {ranges[name][0]:g} to {ranges[name][1]:g} doesn\'t change "{e["id"]}"{why(e, name)}'
                if msg not in problems:
                    problems.append(msg)
    return problems
