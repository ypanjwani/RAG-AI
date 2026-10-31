"""Port of shared/validate.ts: semantic validation and repair of AI output.

parse_outline already checked the JSON shape; this checks what a schema can't
(ranges, cross-references, expression syntax and scope, matrix sizes), then
evaluates every live expression over the slider ranges. Bad parts are repaired
or dropped with a warning; it never throws. Warnings are word for word the
TypeScript ones (the parity tests compare them).

Blocks are dicts. `_assign(b, k=v)` is the JS spread `{...b, k: v}`: an existing
key keeps its place, a new one goes last, and a value of UNDEF removes the key
(JSON.stringify drops undefined).
"""

from __future__ import annotations

import math
import re
from types import SimpleNamespace
from typing import Any, Callable

from .. import data
from ..js import (
    JS_WS_CHARS, NAN, WS, is_finite, is_integer, is_num, js_len, js_lower, js_max, js_min, js_number, js_pow, js_round, js_str,
    js_trim, json_stringify, round_num, to_fixed,
)
from .expr import eval_number, eval_value, is_mat, try_compile, uses_random
from .scene import check_scene, repair_scene
from .views import run_trajectory, with_defs

UNDEF = object()

RESERVED = {"x", "y", "seed"}
DEF_RESERVED = {"seed"}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.A)
MAX_STAGES = 20
MAX_BLOCKS = 14
FALLBACK_ICON = "🧩"
WIDGET_MAX_CHARS = 40_000

NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
                "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"]
# "steps" is left out on purpose: "50 denoising steps" or "the 3 steps of backprop" describe the topic, not the page.
STAGE_COUNT = re.compile(
    r"\b(\d{1,3}|" + "|".join(NUMBER_WORDS) + r")[" + re.escape(JS_WS_CHARS) + r"\-]+(?:stages?|parts?|sections?|chapters?|lessons?|modules?)\b",
    re.I | re.A,
)

Warn = Callable[[str], None]


def _ident(s: Any) -> bool:
    return isinstance(s, str) and IDENT.fullmatch(s) is not None


def _assign(b: dict, **kw: Any) -> dict:
    out = dict(b)
    for k, v in kw.items():
        if v is UNDEF:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def _or_undef(v: Any) -> Any:
    return UNDEF if v is None else v


def requested_stages(text: str) -> dict | None:
    """{"count", "clamped"} for "14 stages", "a 12-part explainer", "fourteen sections"; None when none (or fewer than 2)."""
    m = STAGE_COUNT.search(text)
    if not m:
        return None
    word = js_lower(m.group(1))
    n = int(word) if word.isdigit() else NUMBER_WORDS.index(word)
    if n < 2:
        return None
    return {"count": min(n, MAX_STAGES), "clamped": n > MAX_STAGES}


def without_blocks(outline: dict, drop: set[str]) -> dict:
    return {**outline, "stages": [{**s, "blocks": [b for b in s["blocks"] if b["type"] not in drop]} for s in outline["stages"]]}


def slug(s: str, fallback: str) -> str:
    v = re.sub(r"[^a-z0-9]+", "-", s.lower())
    v = re.sub(r"^-+|-+$", "", v)[:40]
    return v or fallback


def clamp_int(n: float, lo: float, hi: float) -> float:
    return js_min(hi, js_max(lo, js_round(n)))


def finite(n: Any, d: float) -> float:
    return n if is_finite(n) else d


def _is_control(b: dict) -> bool:
    return b["type"] in ("slider", "choice", "toggle")


def declared_vars(stages: list[dict]) -> set[str]:
    out = {"seed"}  # seed: set by Resample/Run buttons, 0 by default
    for st in stages:
        for b in st["blocks"]:
            if _is_control(b) and _ident(b["var"]) and b["var"] not in RESERVED:
                out.add(b["var"])
    return out


def clean_set(items: list[dict], vars_: set[str]) -> list[dict]:
    return [s for s in items if s["var"] in vars_ and s["var"] != "seed" and is_finite(s["value"])][:8]


VISUAL = {
    "plot", "chart", "scene", "bars", "samples", "heatmap", "trajectory", "matrix", "calculator", "experiment", "table", "conceptmap",
    "tokens", "attention", "sampling", "embedding", "architecture", "pipeline", "training",
    "flipcards", "reveal", "predict", "quiz", "cards", "readout", "tabs", "compare",
}
GRAPH = {"plot", "chart", "scene", "bars", "samples", "heatmap", "trajectory", "matrix", "experiment", "attention", "sampling", "training", "embedding"}
CAPS = {"pipeline": 1, "architecture": 1, "conceptmap": 1, "training": 1, "flipcards": 2, "reveal": 3, "cards": 5, "tokens": 2}


def check_expr(src: str, allowed: set[str]) -> str | None:
    r = try_compile(src)
    if not r["ok"]:
        return r["error"]
    for v in r["c"].vars:
        if v not in allowed:
            return f'unknown variable "{v}"'
    return None


def clean_defs(defs: list[dict] | None, vars_: set[str], warn: Warn, where: str, view_vars: list[str] = ()) -> tuple[list[dict], set[str]]:
    scope = set(vars_)
    out: list[dict] = []
    for d in (defs or [])[:20]:
        bad = not _ident(d["name"]) or d["name"] in DEF_RESERVED or d["name"] in view_vars
        err = f'"{d["name"]}" is reserved' if bad else check_expr(d["expr"], scope)
        if err:
            warn(f'Dropped definition "{d["name"]}" in {where}: {err}')
            continue
        out.append(d)
        scope.add(d["name"])
    return out, scope


def _plus(s: set[str], *names: str) -> set[str]:
    return s | set(names)


def _title(b: dict) -> str:
    return b.get("title", "")


def repair_block(b: dict, vars_: set[str], warn: Warn) -> dict | None:
    t = b["type"]
    if t == "quiz":
        questions = []
        for q in [q for q in b["questions"] if len(q["choices"]) >= 2][:4]:
            questions.append({**q, "choices": q["choices"][:5], "answer": clamp_int(q["answer"], 0, min(len(q["choices"]), 5) - 1)})
        return _assign(b, questions=questions) if questions else None
    if t == "predict":
        if len(b["choices"]) < 2:
            return None
        return _assign(b, answer=clamp_int(b["answer"], 0, len(b["choices"]) - 1))
    if t == "flipcards":
        return _assign(b, cards=b["cards"][:8]) if b["cards"] else None
    if t == "tabs":
        return _assign(b, tabs=b["tabs"][:5]) if b["tabs"] else None
    if t == "compare":
        return b if (b["left"]["points"] or b["right"]["points"]) else None
    if t == "formula":
        return _assign(b, terms=[_assign(x, var=UNDEF) if x.get("var") and x["var"] not in vars_ else x for x in b["terms"]])
    if t == "slider":
        if not _ident(b["var"]) or b["var"] in RESERVED:
            return None
        lo, hi = finite(b["min"], 0), finite(b["max"], 1)
        if lo > hi:
            lo, hi = hi, lo
        if lo == hi:
            hi = lo + 1
        step = finite(b["step"], 0)
        if step <= 0 or step > hi - lo:
            step = (hi - lo) / 100
        value = js_min(hi, js_max(lo, finite(b["value"], lo)))
        return _assign(b, min=lo, max=hi, step=step, value=value)
    if t == "choice":
        if not _ident(b["var"]) or b["var"] in RESERVED or len(b["options"]) < 2:
            return None
        options = b["options"][:5]
        value = b["value"] if any(o["value"] == b["value"] for o in options) else options[0]["value"]
        return _assign(b, options=options, value=value)
    if t == "plot":
        defs, scope = clean_defs(b.get("defs"), vars_, warn, f'"{b["title"]}"', ["x"])
        with_x = _plus(scope, "x")
        series = []
        for s in b["series"][:4]:
            err = check_expr(s["expr"], with_x)
            if err:
                warn(f'Dropped plot series "{s["label"]}": {err}')
            else:
                series.append(s)
        if not series:
            return None
        x_min, x_max = b["xMin"], b["xMax"]
        if not (x_min < x_max):
            x_min, x_max = 0, 1
        marker_x = b["markerX"] if b["markerX"] and not check_expr(b["markerX"], scope) else ""
        sh = b.get("shade")
        shade = sh if sh and sh["series"] >= 0 and sh["series"] < len(series) and not check_expr(sh["from"], scope) and not check_expr(sh["to"], scope) else UNDEF
        sweep = b.get("sweepVar")
        sweep_var = sweep if sweep and sweep in vars_ and sweep != "seed" else UNDEF
        return _assign(b, defs=defs, series=series, xMin=x_min, xMax=x_max, markerX=marker_x, shade=shade, sweepVar=sweep_var)
    if t == "scene":
        return repair_scene(b, vars_, warn, SCENE_HELPERS)
    if t == "chart":
        where = f'chart "{b["title"]}"'
        defs, scope = clean_defs(b.get("defs"), vars_, warn, f'"{b["title"]}"')
        series = []
        for s in b["series"][:3]:
            err = check_expr(s["expr"], scope)
            if err:
                warn(f'Dropped chart series "{s["label"]}": {err}')
            else:
                series.append(s)
        if not series:
            return None

        def axis(v: str, name: str) -> str:
            if not v:
                return ""
            if v in vars_ and v != "seed":
                return v
            warn(f'Chart "{b["title"]}": "{v}" is not a control variable, so it can\'t be the {name} axis.')
            return ""

        x = axis(b["x"], "x")
        by = axis(b["by"], "second")
        if by and by == x:
            by = ""
        at = sorted(dict.fromkeys(v for v in b["at"] if is_finite(v)))[:4]
        # A typed-in answer (0.366032) makes the chart ignore the page; it should be computed from the variables.
        lits: dict[str, None] = {}
        for e in [s["expr"] for s in series] + [d["expr"] for d in defs]:
            for lit in LITERAL.findall(e):
                lits.setdefault(lit, None)
        typed = [lit for lit in lits if _sig_digits(lit) >= 4]
        if typed:
            warn(f"The {where} types the number {typed[0]}; computing it from the variables keeps it in step with the page.")
        return _assign(b, defs=defs, series=series, x=x, by=by, at=at, labels=b["labels"][:24])
    if t == "toggle":
        if not _ident(b["var"]) or b["var"] in RESERVED:
            return None
        return _assign(b, value=1 if b["value"] else 0)
    if t == "presets":
        items = [it for it in (_assign(it, set=clean_set(it["set"], vars_)) for it in b["items"]) if it["set"]][:5]
        return _assign(b, items=items) if items else None
    if t == "examples":
        items = []
        for it in b["items"]:
            steps = it.get("steps")
            n = _assign(it, set=clean_set(it["set"], vars_), steps=steps[:6] if steps is not None else UNDEF)
            if n["set"] or n.get("steps"):
                items.append(n)
        items = items[:4]
        return _assign(b, items=items) if items else None
    if t == "cards":
        items = [it for it in b["items"] if it["title"] or it["md"]][:6]
        return _assign(b, items=items) if items else None
    if t == "experiment":
        defs, scope = clean_defs(b.get("defs"), vars_, warn, f'"{b["title"]}"')
        err = check_expr(b["trial"], scope)
        if err:
            warn(f'Dropped experiment "{b["title"]}": {err}')
            return None
        if not uses_random(b["trial"], defs):
            warn(f'Dropped experiment "{b["title"]}": nothing in it is random, so repeating it would show the same result.')
            return None
        seen: list[float] = []
        for n in b["repeats"]:
            c = clamp_int(n, 1, 10_000)
            if c not in seen:
                seen.append(c)
        repeats = sorted(seen)[:4]
        truth = b["truth"] if b.get("truth") and not check_expr(b["truth"], scope) else UNDEF
        outcomes = b["outcomes"][:12] if b.get("outcomes") else UNDEF
        return _assign(b, defs=defs, repeats=repeats if repeats else [1], truth=truth, outcomes=outcomes)
    if t == "table":
        cols = b["columns"][:8]
        if not cols:
            return None
        rows = []
        for r in b["rows"][:24]:
            cells = []
            for c in r[: len(cols)]:
                if not c.startswith("="):
                    cells.append(c)
                    continue
                e = check_expr(c[1:], vars_)
                if e:
                    warn(f'Table "{b["title"]}": live cell "{c}" removed ({e}).')
                cells.append("—" if e else c)
            while len(cells) < len(cols):
                cells.append("")
            rows.append(cells)
        if not rows:
            return None
        return _assign(b, columns=cols, rows=rows, digits=UNDEF if b.get("digits") is None else clamp_int(b["digits"], 0, 6))
    if t == "conceptmap":
        seen_ids: set[str] = set()
        nodes = []
        for n in b["nodes"]:
            if n["id"] and n["id"] not in seen_ids:
                seen_ids.add(n["id"])
                nodes.append(n)
        nodes = nodes[:9]
        ids = {n["id"] for n in nodes}
        edges = [e for e in b["edges"] if e["from"] in ids and e["to"] in ids and e["from"] != e["to"]][:16]
        return _assign(b, nodes=nodes, edges=edges) if len(nodes) >= 2 else None
    if t == "bars":
        defs, scope = clean_defs(b.get("defs"), vars_, warn, f'"{b["title"]}"')
        if len(b["values"]) == 1 and len(b["labels"]) != 1:
            err = check_expr(b["values"][0], scope)
            if err:
                warn(f'Dropped bars "{b["title"]}": {err}')
                return None
            return _assign(b, defs=defs, labels=b["labels"][:24])
        n = min(len(b["labels"]), len(b["values"]), 12)
        labels: list[str] = []
        values: list[str] = []
        for i in range(n):
            err = check_expr(b["values"][i], scope)
            if err:
                warn(f'Dropped bar "{b["labels"][i]}": {err}')
                continue
            labels.append(b["labels"][i])
            values.append(b["values"][i])
        return _assign(b, defs=defs, labels=labels, values=values) if labels else None
    if t == "readout":
        defs, scope = clean_defs(b.get("defs"), vars_, warn, "a readout")
        items = [_assign(it, digits=clamp_int(it["digits"], 0, 6)) for it in b["items"] if not check_expr(it["expr"], scope)]
        return _assign(b, defs=defs, items=items) if items else None
    if t == "samples":
        defs, scope = clean_defs(b.get("defs"), vars_, warn, f'"{b["title"]}"')
        err = check_expr(b["values"], scope)
        if err:
            warn(f'Dropped samples "{b["title"]}": {err}')
            return None
        markers = [m for m in b["markers"] if not check_expr(m["expr"], scope)][:4]
        band = b.get("band")
        band = band if band and not check_expr(band["from"], scope) and not check_expr(band["to"], scope) else UNDEF
        stats = [_assign(m, digits=clamp_int(m["digits"], 0, 6)) for m in [m for m in b["stats"] if not check_expr(m["expr"], scope)][:4]]
        return _assign(b, defs=defs, markers=markers, band=band, stats=stats)
    if t == "heatmap":
        defs, scope = clean_defs(b.get("defs"), vars_, warn, f'"{b["title"]}"', ["x", "y"])
        err = check_expr(b["expr"], _plus(scope, "x", "y"))
        if err:
            warn(f'Dropped heatmap "{b["title"]}": {err}')
            return None
        if not (b["xMin"] < b["xMax"]) or not (b["yMin"] < b["yMax"]):
            return None
        mk = b.get("marker")
        marker = mk if mk and not check_expr(mk["x"], scope) and not check_expr(mk["y"], scope) else UNDEF
        return _assign(b, defs=defs, marker=marker)
    if t == "trajectory":
        names = [s["name"] for s in b["state"]]
        defs, scope = clean_defs(b.get("defs"), _plus(vars_, *names, "t"), warn, f'"{b["title"]}"', ["t", *names])

        def fail_(why: str) -> None:
            warn(f'Trajectory "{b["title"]}": {why}')
            return None

        bad_name = next((n for n in names if not _ident(n) or n in DEF_RESERVED or n == "t"), None)
        if not names or bad_name is not None:
            return fail_(f'invalid state name "{bad_name or ""}" (t is the step counter).')
        inner = _plus(scope, *names, "t")
        for s in b["state"]:
            e = check_expr(s["init"], scope)
            if e:
                return fail_(f"start value of {s['name']}: {e}")
        update = []
        for u in b["update"]:
            e = check_expr(u["expr"], inner) if u["name"] in names else "not a state variable"
            if e:
                warn(f'Trajectory "{b["title"]}": dropped update of {u["name"]} ({e}).')
            else:
                update.append(u)
        if not update:
            return fail_("no valid update rules.")
        steps_err = check_expr(b["steps"], scope)
        if steps_err:
            return fail_(f"steps: {steps_err}")
        if b["mode"] == "path":
            p = b.get("path")
            if not p:
                return fail_("path mode needs a path.")
            e = check_expr(p["x"], inner)
            if e is None:
                e = check_expr(p["y"], inner)
            if e:
                return fail_(f"path: {e}")
            if not (p["xMin"] < p["xMax"]) or not (p["yMin"] < p["yMax"]):
                return fail_("path range is empty.")
            surface = p["surface"] if p["surface"] and not check_expr(p["surface"], _plus(scope, "x", "y")) else ""
            return _assign(b, defs=defs, update=update, path=_assign(p, surface=surface))
        series = []
        for s in b["series"]:
            e = check_expr(s["expr"], inner)
            if e:
                warn(f'Trajectory "{b["title"]}": dropped series "{s["label"]}" ({e}).')
            else:
                series.append(s)
        series = series[:4]
        return _assign(b, defs=defs, update=update, series=series) if series else fail_("no valid series to plot.")
    if t == "matrix":
        defs, scope = clean_defs(b.get("defs"), vars_, warn, f'"{b["title"]}"')
        items = [it for it in b["items"] if not check_expr(it["expr"], scope)][:7]
        return _assign(b, defs=defs, items=items, digits=clamp_int(b["digits"], 0, 4)) if items else None
    if t == "calculator":
        defs, scope = clean_defs(b.get("defs"), vars_, warn, f'"{b["title"]}"')
        rows = [_assign(r, digits=clamp_int(r["digits"], 0, 6)) for r in [r for r in b["rows"] if not check_expr(r["expr"], scope)][:12]]
        return _assign(b, defs=defs, rows=rows) if rows else None
    if t == "tokens":
        return _assign(b, tokens=b["tokens"][:60]) if b["tokens"] else None
    if t == "attention":
        tokens = b["tokens"][:10]
        n = len(tokens)
        if n < 2:
            return None
        w = b["weights"]
        weights = [[finite(w[i][j] if i < len(w) and j < len(w[i]) else 0, 0) for j in range(n)] for i in range(n)]
        return _assign(b, tokens=tokens, weights=weights)
    if t == "sampling":
        candidates = [c for c in b["candidates"] if c["token"] and is_finite(c["logit"])][:10]
        return _assign(b, candidates=candidates) if len(candidates) >= 2 else None
    if t == "embedding":
        points = [p for p in b["points"] if is_finite(p["x"]) and is_finite(p["y"])][:40]
        return _assign(b, points=points) if len(points) >= 2 else None
    if t == "architecture":
        seen_ids = set()
        nodes = []
        for n in b["nodes"]:
            if n["id"] and n["id"] not in seen_ids:
                seen_ids.add(n["id"])
                nodes.append(n)
        nodes = nodes[:14]
        ids = {n["id"] for n in nodes}
        edges = [e for e in b["edges"] if e["from"] in ids and e["to"] in ids and e["from"] != e["to"]]
        return _assign(b, nodes=nodes, edges=edges) if nodes else None
    if t == "pipeline":
        return _assign(b, steps=b["steps"][:10]) if b["steps"] else None
    if t == "widget":
        # AI-written code can't be checked numerically; only its shape. It runs sandboxed (render/components/widget.ts).
        html = js_trim(b["html"])
        if not html or js_len(html) > WIDGET_MAX_CHARS:
            warn(f'Dropped widget "{b["title"]}": {"too large" if html else "empty"}.')
            return None
        if not re.search(r"<script(?:" + WS + r"|>)", html, re.I):
            warn(f'Dropped widget "{b["title"]}": it has no script, so nothing is interactive.')
            return None
        return _assign(b, html=html)
    return b


# ---------- numeric sanity check ----------


def probe_scopes(blocks: list[dict]) -> list[dict]:
    """All sliders at their defaults, then each slider at its min and max."""
    base: dict = {"seed": 0.0}
    sliders: list[tuple[str, float, float]] = []
    for b in blocks:
        t = b["type"]
        if t == "slider" and b["var"] not in base:
            base[b["var"]] = b["value"]
            sliders.append((b["var"], b["min"], b["max"]))
        if t == "choice" and b["var"] not in base:
            base[b["var"]] = b["value"]
        if t == "toggle" and b["var"] not in base:
            base[b["var"]] = b["value"]
            sliders.append((b["var"], 0, 1))
    scopes = [base]
    for name, lo, hi in sliders[:8]:
        scopes.append({**base, name: lo})
        scopes.append({**base, name: hi})
    return scopes


def _finite_nums(v: Any) -> list[float]:
    if is_num(v):
        xs = [v]
    elif is_mat(v):
        xs = [x for r in v for x in r]
    elif isinstance(v, list):
        xs = v
    else:  # JS undefined (v[NaN]): the browser would throw here; treat it as no number
        xs = []
    return [x for x in xs if is_finite(x)]


def _lin(lo: float, hi: float, n: int) -> list[float]:
    return [lo + ((hi - lo) * i) / (n - 1) for i in range(n)]


def _label(b: dict, block_word: bool = True) -> str:
    return f'"{b["title"]}"' if b.get("title") else (f"a {b['type']} block" if block_word else f"a {b['type']}")


def slider_map(blocks: list[dict]) -> dict[str, dict]:
    return {b["var"]: b for b in blocks if b["type"] == "slider"}


# The views each formula shape can use, in fallback order (the first that fits replaces a view that doesn't).
VIEWS_FOR = {
    "number": ["line", "lines", "heatmap"],
    "random": ["histogram", "compare", "running", "scatter", "band", "box"],
    "list": ["bars", "groupedbars"],
}
SHAPE_NAME = {"number": "a formula that gives one number", "random": "a random formula", "list": "a formula that gives a list"}


def _view_fits(view: str, shape: str, x: str, by: str, n: int) -> bool:
    """Whether a view can draw this chart: right shape, the axis variables it needs, and enough series (n)."""
    if view not in VIEWS_FOR[shape]:
        return False
    if view in ("line", "band"):
        return bool(x)
    if view in ("lines", "heatmap"):
        return bool(x) and bool(by)
    if view in ("compare", "box"):
        return bool(by)
    # Scatter: draws against x, or the first two series as each point's position.
    if view == "scatter":
        return bool(x) or n >= 2
    if view == "groupedbars":
        return n >= 2
    return True


def fit_chart(b: dict, scopes: list[dict], sliders: dict[str, dict], warn: Warn) -> dict | None:
    """The chart checks that need the sliders and real values. See fitChart in validate.ts."""

    def not_slider(v: str, name: str) -> str:
        if not v or v in sliders:
            return v
        warn(f'Chart "{b["title"]}": "{v}" is not a slider, so it can\'t be the {name} axis.')
        return ""

    x = not_slider(b["x"], "x")
    by = not_slider(b["by"], "second")
    first = b["series"][0]["expr"]
    at0 = with_defs(b.get("defs"), scopes[0])
    shape = "random" if uses_random(first, b.get("defs") or []) else ("number" if is_num(eval_value(first, at0)) else "list")
    view = b["view"]
    n = len(b["series"])
    if not _view_fits(view, shape, x, by, n):
        fit = next((v for v in VIEWS_FOR[shape] if _view_fits(v, shape, x, by, n)), None)
        if fit is None:
            need = "a slider variable on its x axis" if shape == "number" else "a different view"
            warn(f'Dropped chart "{b["title"]}": {SHAPE_NAME[shape]} needs {need}.')
            return None
        warn(f'Chart "{b["title"]}": the "{view}" view doesn\'t fit {SHAPE_NAME[shape]}; showing "{fit}".')
        view = fit
    # Values of `by` to show: inside its slider's range; by default the ends (and the middle for lines and box).
    at: list = []
    if by and view in ("lines", "compare", "box"):
        s = sliders[by]
        at = sorted(dict.fromkeys(js_min(s["max"], js_max(s["min"], v)) for v in b["at"]))
        if view == "compare":
            # The first value is the fixed baseline (the renderer draws it next to the slider's current value).
            at = [at[0], at[-1] if len(at) > 1 else s["max"]] if at else [s["min"], s["max"]]
        else:
            if len(at) < 2:
                at = [s["min"], (s["min"] + s["max"]) / 2, s["max"]]
            at = at[:4]
    controls = {k for k in (scopes[0] if scopes else {}) if k != "seed"}
    reads = any(any(v in controls for v in vars_of(s["expr"], b.get("defs"))) for s in b["series"])
    if controls and not reads:
        warn(f'Chart "{b["title"]}" reads none of the page\'s controls, so nothing on the page changes it.')
    return _assign(b, x=x, by=by, at=at, view=view)


def sanity_check(b: dict, scopes: list[dict], warn: Warn, sliders: dict[str, dict] | None = None) -> dict | None:
    label = _label(b)

    def ss(defs):
        return [with_defs(defs, s) for s in scopes]

    def alive(src: str, scs: list[dict]) -> bool:
        return any(len(_finite_nums(eval_value(src, s))) > 0 for s in scs)

    def kind_ok(src: str, kind: str | None, scs: list[dict]) -> bool:
        if kind not in ("probability", "percent"):
            return True
        hi = 1 + 1e-9 if kind == "probability" else 100 + 1e-7
        return all(all(-1e-9 <= v <= hi for v in _finite_nums(eval_value(src, s))) for s in scs)

    t = b["type"]
    if t == "plot":
        scs = ss(b.get("defs"))
        xs = _lin(b["xMin"], b["xMax"], 9)
        sweep = b.get("sweepVar")

        def at(sc, x):
            return with_defs(b.get("defs"), {**sc, sweep: x, "x": x}) if sweep else {**sc, "x": x}

        flat = 0
        series = []
        for s in b["series"]:
            ys = [y for y in (eval_number(s["expr"], at(sc, x)) for sc in scs for x in xs) if is_finite(y)]
            if not ys:
                warn(f'Dropped plot series "{s["label"]}": it never produces a number.')
                continue
            if js_max(*ys) - js_min(*ys) < 1e-12:
                flat += 1
            series.append(s)
        # A flat reference line next to a moving curve is fine; a plot where nothing moves is not.
        if series and flat == len(series):
            warn(f"Plot {label} is flat: nothing changes it.")
        return _assign(b, series=series) if series else None
    if t == "scene":
        return check_scene(b, scopes, warn, sliders or {}, SCENE_HELPERS)
    if t == "chart":
        sl = sliders or {}
        fit = fit_chart(b, scopes, sl, warn)
        if not fit:
            return None
        # Numbers: every series over x (or at the probe scopes); random: a few seeds; lists: the elements.
        xv = fit["x"]
        xs = _lin(sl[xv]["min"], sl[xv]["max"], 9) if xv else [None]
        seeds = (1.0, 2.0, 3.0) if fit["view"] in VIEWS_FOR["random"] else (0.0,)

        def at_(sc, x, seed):
            return with_defs(fit.get("defs"), {**sc, xv: x, "seed": seed} if xv and x is not None else {**sc, "seed": seed})

        flat = 0
        series = []
        for s in fit["series"]:
            vals = [v for sc in scopes for x in xs for seed in seeds for v in _finite_nums(eval_value(s["expr"], at_(sc, x, seed)))]
            if not vals:
                warn(f'Dropped chart series "{s["label"]}": it never produces a number.')
                continue
            if js_max(*vals) - js_min(*vals) < 1e-12:
                flat += 1
            series.append(s)
        if not series:
            return None
        if flat == len(series) and fit["view"] not in VIEWS_FOR["random"]:
            warn(f"Chart {label} is flat: nothing changes it.")
        return _assign(fit, series=series)
    if t == "bars":
        scs = ss(b.get("defs"))
        return b if any(alive(v, scs) for v in b["values"]) else None
    if t == "readout":
        scs = ss(b.get("defs"))
        items = []
        for it in b["items"]:
            if not alive(it["expr"], scs):
                warn(f'Dropped readout "{it["label"]}": it never produces a number.')
                continue
            if not kind_ok(it["expr"], it.get("kind"), scs):
                warn(f'Readout "{it["label"]}" goes outside the valid range for a {it.get("kind")}.')
            items.append(it)
        return _assign(b, items=items) if items else None
    if t == "samples":
        scs = ss(b.get("defs"))
        if not alive(b["values"], scs):
            return None
        for st in b["stats"]:
            if not kind_ok(st["expr"], st.get("kind"), scs):
                warn(f'Statistic "{st["label"]}" in {label} goes outside the valid range for a {st.get("kind")}.')
        return _assign(b, stats=[st for st in b["stats"] if alive(st["expr"], scs)], markers=[m for m in b["markers"] if alive(m["expr"], scs)])
    if t == "heatmap":
        scs = ss(b.get("defs"))
        grid = [(x, y) for x in _lin(b["xMin"], b["xMax"], 5) for y in _lin(b["yMin"], b["yMax"], 5)]
        zs = [z for z in (eval_number(b["expr"], {**sc, "x": x, "y": y}) for sc in scs for x, y in grid) if is_finite(z)]
        if not zs:
            return None
        if js_max(*zs) - js_min(*zs) < 1e-12:
            warn(f"Heatmap {label} is a single flat colour.")
        return b
    if t == "trajectory":
        run = run_trajectory(b, scopes[0], 300)
        first = run["frames"][0]
        return b if all(is_finite(first.get(s["name"])) for s in b["state"]) else None
    if t == "matrix":
        scs = ss(b.get("defs"))
        items = [it for it in b["items"] if alive(it["expr"], scs)]
        return _assign(b, items=items) if items else None
    if t == "calculator":
        scs = ss(b.get("defs"))
        rows = [r for r in b["rows"] if alive(r["expr"], scs)]
        return _assign(b, rows=rows) if rows else None
    if t == "experiment":
        # Probe a few seeds: the trial must produce numbers, and category indices must be valid.
        runs = [eval_number(b["trial"], with_defs(b.get("defs"), {**s, "seed": float(seed)})) for seed in (1, 2, 3) for s in ss(b.get("defs"))]
        ok = [v for v in runs if is_finite(v)]
        if not ok:
            return None
        outcomes = b.get("outcomes")
        if outcomes and any(not is_integer(v) or v < 0 or v >= len(outcomes) for v in ok):
            warn(f"Experiment {label}: results aren't valid outcome indices, so they're shown as numbers.")
            return _assign(b, outcomes=UNDEF)
        return b
    if t == "table":
        rows = [["—" if c.startswith("=") and not alive(c[1:], scopes) else c for c in r] for r in b["rows"]]
        return _assign(b, rows=rows)
    return b


def vars_of(src: str, defs: list[dict] | None = None) -> dict[str, None]:
    """Variables an expression reads, following defs transitively (an ordered set)."""
    by_name = {d["name"]: d["expr"] for d in (defs or [])}
    out: dict[str, None] = {}

    def visit(s: str) -> None:
        r = try_compile(s)
        if not r["ok"]:
            return
        for v in r["c"].vars:
            if v in out:
                continue
            out[v] = None
            e = by_name.get(v)
            if e is not None:
                visit(e)

    visit(src)
    return out


def _is_flat(p: dict, scope: dict) -> bool:
    ys = [y for y in (eval_number(p["series"][0]["expr"], with_defs(p.get("defs"), {**scope, p["sweepVar"]: x, "x": x})) for x in _lin(p["xMin"], p["xMax"], 9)) if is_finite(y)]
    return len(ys) < 2 or js_max(*ys) - js_min(*ys) < 1e-12


def auto_graphs(blocks: list[dict], sliders: dict[str, dict], scopes: list[dict]) -> list[dict]:
    """A stage with controls and readouts but no graph gets a sensitivity plot per readout."""
    if any(b["type"] in GRAPH for b in blocks) or not any(_is_control(b) for b in blocks):
        return blocks
    out: list[dict] = []
    for b in blocks:
        out.append(b)
        if b["type"] != "readout":
            continue
        item = b["items"][0]
        used = [v for v in vars_of(item["expr"], b.get("defs")) if v in sliders][:2]
        for v in used:
            s = sliders[v]
            plot: dict = {
                "type": "plot", "title": f'{item["label"]} as {s["label"]} changes', "xLabel": s["label"], "yLabel": item["label"],
                "xMin": s["min"], "xMax": s["max"], "yMin": 0, "yMax": 0, "defs": b.get("defs"),
                "series": [{"label": item["label"], "expr": item["expr"]}], "markerX": v, "sweepVar": v,
            }
            if plot["defs"] is None:
                del plot["defs"]
            if sanity_check(plot, scopes, lambda m: None) and not _is_flat(plot, scopes[0]):
                out.append(plot)
    return out


def check_edit(spec: dict, stage_index: int, blocks: list[dict]) -> dict:
    """{"ok": True, "spec", "notes"} or {"ok": False, "reason"}. See checkEdit in validate.ts."""
    problems: list[str] = []
    stages = [{**s, "blocks": blocks} if i == stage_index else s for i, s in enumerate(spec["stages"])]
    vars_ = declared_vars(stages)
    repaired: list[dict] = []
    for b in blocks:
        r = repair_block(b, vars_, problems.append)
        # The first problem is the cause (a bad definition); later ones are its knock-on effects.
        if not r:
            return {"ok": False, "reason": problems[0] if problems else f'This "{b["type"]}" part is no longer valid.'}
        repaired.append(r)
    if problems:
        return {"ok": False, "reason": problems[0]}
    notes: list[str] = []
    for i, b in enumerate(blocks):
        if json_stringify(b) != json_stringify(repaired[i]):
            n = f"Adjusted the {b['type']} to stay valid (for example a value pulled back into its range)."
            if n not in notes:
                notes.append(n)
    nxt = [{**s, "blocks": repaired} if i == stage_index else s for i, s in enumerate(stages)]
    scopes = probe_scopes([b for s in nxt for b in s["blocks"]])
    sliders = slider_map([b for s in nxt for b in s["blocks"]])
    for s in nxt:
        for b in s["blocks"]:
            label = _label(b, block_word=False)
            r = sanity_check(b, scopes, lambda m: None, sliders)
            if not r:
                return {"ok": False, "reason": f'That would break {label} in "{s["label"]}": its numbers would never work out.'}
            if json_stringify(b) != json_stringify(r):
                return {"ok": False, "reason": f'That would break part of {label} in "{s["label"]}" (some of its values would stop producing numbers).'}
    return {"ok": True, "spec": {**spec, "stages": nxt}, "notes": notes}


# ---------- cross-check: one quantity, one answer ----------

LITERAL = re.compile(r"(?<![\w.])\d*\.\d{2,}(?![\w.])", re.A)
CONSTANT = re.compile(r"(?:" + WS + r"|[\d.,+\-\[\]])*", re.A)


def _sig_digits(lit: str) -> int:
    s = re.sub(r"^[0.]+", "", lit).replace(".", "", 1)
    return len(re.sub(r"0+$", "", s))


def _is_round(v: float) -> bool:
    return abs(v * 20 - js_round(v * 20)) < 1e-9


def _hand_typed(text: str) -> list[str]:
    seen: list[str] = []
    for lit in LITERAL.findall(text):
        if lit not in seen:
            seen.append(lit)
    return [lit for lit in seen if _sig_digits(lit) >= 2 and not _is_round(js_number(lit))]


def fmt_num(v: float) -> str:
    return js_str(round_num(v, 3))


def cross_check(stages: list[dict], scope: dict, warn: Warn) -> None:
    """Hand-typed copies of computed values: warn (A2); drop one that disagrees with an output from the same value (A1)."""
    outputs: list[dict] = []
    computed: list[dict] = []

    def nums(v: Any) -> list[float]:
        xs = [v] if is_num(v) else ([] if is_mat(v) or not isinstance(v, list) else v[:64])
        return [x for x in xs if is_finite(x)]

    def shown(v: float, kind: str | None) -> float:
        return v * 100 if kind == "probability" else v

    for si, st in enumerate(stages):
        for bi, b in enumerate(st["blocks"]):
            title = f'"{b["title"]}"' if b.get("title") else f"a {b['type']}"

            def add(item: int, label: str, expr: str, defs: list[dict], digits: float, kind: str | None = None) -> None:
                value = eval_number(expr, with_defs(defs, scope))
                if not is_finite(value):
                    return
                outputs.append({"stage": si, "block": bi, "item": item, "label": label, "expr": expr, "defs": defs, "value": value, "shown": shown(value, kind), "digits": digits})

            if b["type"] == "calculator":
                for i, r in enumerate(b["rows"]):
                    add(i, f"row {i + 1} of calculator {title}", r["expr"], b.get("defs") or [], r["digits"])
            if b["type"] == "readout":
                for i, it in enumerate(b["items"]):
                    add(i, f'readout "{it["label"]}"', it["expr"], b.get("defs") or [], it["digits"], it.get("kind"))
            if b["type"] == "table":
                for ri, r in enumerate(b["rows"]):
                    for ci, c in enumerate(r):
                        if c.startswith("="):
                            add(ri * 100 + ci, f"cell ({ri + 1}, {ci + 1}) of table {title}", c[1:], [], b["digits"] if b.get("digits") is not None else 3)

    # Defs that compute something, linked to the outputs in their block that read them.
    for si, st in enumerate(stages):
        for bi, b in enumerate(st["blocks"]):
            if b["type"] == "trajectory" or not b.get("defs"):
                continue
            sc = with_defs(b["defs"], scope)
            title = f'"{b["title"]}"' if b.get("title") else f"a {b['type']}"
            for d in b["defs"]:
                if CONSTANT.fullmatch(d["expr"]):
                    continue
                readers = {oi for oi, o in enumerate(outputs) if o["stage"] == si and o["block"] == bi and d["name"] in vars_of(o["expr"], o["defs"])}
                v = sc.get(d["name"])
                for value in nums(NAN if v is None else v):
                    computed.append({"value": value, "where": f'{d["name"]} in {title}', "readers": readers})

    copies_of: dict[int, list[tuple[str, dict]]] = {}
    for oi, o in enumerate(outputs):
        text = " ".join([o["expr"], *[d["expr"] for d in o["defs"]]])
        copies: list[tuple[str, dict]] = []
        for lit in _hand_typed(text):
            v = js_number(lit)
            tol = 0.5 * js_pow(10, -len(lit.split(".")[1])) + 1e-12
            # An exact match is the same constant written twice, not a rounded copy.
            copies.extend((lit, c) for c in computed if oi not in c["readers"] and c["value"] != v and abs(c["value"] - v) <= tol)
        if copies:
            copies_of[oi] = copies

    drop: set[int] = set()
    for oi, copies in copies_of.items():
        o = outputs[oi]
        lit, c = copies[0]
        warn(f'The {o["label"]} types {lit} by hand, a rounded copy of {c["where"]} ({fmt_num(c["value"])}); using the variable keeps them in step.')
        # Outputs computed from the copied value that hold no hand-typed copies themselves.
        rivals = [r for x in copies for r in _ordered(x[1]["readers"]) if r != oi and r not in copies_of]

        def clash_with(ri: int) -> bool:
            r = outputs[ri]
            d = int(min(o["digits"], r["digits"]))
            denom = max(abs(o["value"]), abs(r["value"]))
            rel = abs(o["value"] - r["value"]) / denom if denom else NAN
            return rel > 0.005 and rel <= 0.2 and to_fixed(o["shown"], d) != to_fixed(r["shown"], d)

        clash = next((ri for ri in rivals if clash_with(ri)), None)
        if clash is not None:
            drop.add(oi)
            warn(f'Dropped the {o["label"]} in "{stages[o["stage"]]["label"]}": with its hand-typed {lit} it gives {fmt_num(o["value"])}, but the {outputs[clash]["label"]}, computed from the same value, gives {fmt_num(outputs[clash]["value"])}.')
    if not drop:
        return

    gone = {(outputs[oi]["stage"], outputs[oi]["block"], outputs[oi]["item"]) for oi in drop}
    for si, st in enumerate(stages):
        new_blocks: list[dict] = []
        for bi, b in enumerate(st["blocks"]):
            if b["type"] == "calculator":
                rows = [r for i, r in enumerate(b["rows"]) if (si, bi, i) not in gone]
                if rows:
                    new_blocks.append({**b, "rows": rows})
            elif b["type"] == "readout":
                items = [it for i, it in enumerate(b["items"]) if (si, bi, i) not in gone]
                if items:
                    new_blocks.append({**b, "items": items})
            elif b["type"] == "table":
                new_blocks.append({**b, "rows": [["—" if (si, bi, ri * 100 + ci) in gone else c for ci, c in enumerate(r)] for ri, r in enumerate(b["rows"])]})
            else:
                new_blocks.append(b)
        st["blocks"] = new_blocks


def _ordered(s: set[int]) -> list[int]:
    """A JS Set of outputs iterates in the order they were added, which is output order."""
    return sorted(s)


def default_design(outline: dict) -> dict:
    return {
        **data.menu("DEFAULT_DESIGN"),
        "stageIcons": [],
        "chatbot": {
            "systemPrompt": f'You are a friendly tutor helping a reader understand "{outline["title"]}". Answer clearly at a {outline["level"]} level, using the explainer\'s content. If something is outside it, say so briefly.',
            "starters": [f'Can you explain "{s["label"]}" more simply?' for s in outline["stages"][:3]],
        },
    }


def assemble_spec(outline: dict, design: dict | None, context: str, parse_warnings: list[str] | None = None) -> dict:
    """Turn validated AI pieces into the spec the app renders."""
    warnings: list[str] = list(parse_warnings or [])
    warn = warnings.append
    d = design if design is not None else default_design(outline)
    if design is None:
        warn("Design step failed; default design used.")

    vars_ = declared_vars(outline["stages"])
    phases = outline["phases"][:4] if outline["phases"] else [{"id": "main", "label": "How it works"}]
    phase_ids = {p["id"] for p in phases}
    icons_menu = data.menu("ICONS")
    icons = {s["stageId"]: icons_menu[s["icon"]] for s in d["stageIcons"]}
    used_ids: set[str] = set()

    # Pass 1: structural repair.
    repaired: list[tuple[dict, str, list[dict]]] = []
    for i, st in enumerate(outline["stages"][:MAX_STAGES]):
        sid = slug(st["id"] or st["label"], f"stage-{i + 1}")
        while sid in used_ids:
            sid += "-2"
        used_ids.add(sid)
        blocks = []
        for b in st["blocks"]:
            r = repair_block(b, vars_, warn)
            if not r:
                warn(f'Dropped an invalid "{b["type"]}" block in "{st["label"]}".')
            else:
                blocks.append(r)
        repaired.append((st, sid, blocks[:MAX_BLOCKS]))

    # Pass 2: numeric sanity check, variety caps, auto-graphs and the per-stage visual check.
    every = [b for _, _, bs in repaired for b in bs]
    scopes = probe_scopes(every)
    sliders = slider_map(every)
    counts: dict[str, int] = {}
    stages: list[dict] = []
    for st, sid, raw in repaired:
        checked = []
        for b in raw:
            r = sanity_check(b, scopes, warn, sliders)
            if not r:
                warn(f'Dropped a "{b["type"]}" block in "{st["label"]}": its numbers never work out.')
                continue
            checked.append(r)
        capped = []
        for b in checked:
            n = counts.get(b["type"], 0) + 1
            counts[b["type"]] = n
            cap = CAPS.get(b["type"])
            if cap is not None and n > cap:
                warn(f'Dropped an extra "{b["type"]}" in "{st["label"]}" (max {cap} per explainer).')
                continue
            capped.append(b)
        blocks = auto_graphs(capped, sliders, scopes)
        if not blocks:
            warn(f'Dropped empty stage "{st["label"]}".')
            continue
        if not any(b["type"] in VISUAL for b in blocks):
            warn(f'Stage "{st["label"]}" has no visual or interactive part.')
        stages.append({
            **st,
            "id": sid,
            "phase": st["phase"] if st["phase"] in phase_ids else phases[0]["id"],
            "icon": icons.get(st["id"]) or icons.get(sid) or FALLBACK_ICON,
            "blocks": blocks,
        })

    # Concept-map nodes point at stages by the AI's ids; map them to the final ones.
    final_id = {st["id"]: sid for st, sid, _ in repaired}
    kept = {s["id"] for s in stages}
    for s in stages:
        s["blocks"] = [b if b["type"] != "conceptmap" else {
            **b,
            "nodes": [{**n, "stage": t if t in kept else ""} for n in b["nodes"] for t in [final_id.get(n["stage"], n["stage"])]],
        } for b in s["blocks"]]

    cross_check(stages, scopes[0], warn)

    used_phases = [p for p in phases if any(s["phase"] == p["id"] for s in stages)]
    return {
        "version": 1,
        "title": js_trim(outline["title"]) or "Untitled explainer",
        "subtitle": outline["subtitle"],
        "level": outline["level"],
        "layout": outline["layout"],
        "keyIntuition": outline["keyIntuition"],
        "design": {"theme": d["theme"], "fontPairing": d["fontPairing"], "headingStyle": d["headingStyle"]},
        "phases": used_phases,
        "stages": stages,
        "glossary": outline["glossary"][:30],
        "chatbot": {
            "systemPrompt": d["chatbot"]["systemPrompt"] or default_design(outline)["chatbot"]["systemPrompt"],
            "starters": [x for x in d["chatbot"]["starters"] if x][:4],
            "context": context,
        },
        "warnings": warnings,
    }


# What the scene checks (core/scene.py) reuse from here.
SCENE_HELPERS = SimpleNamespace(check_expr=check_expr, clean_defs=clean_defs, repair_block=repair_block, sanity_check=sanity_check,
                                vars_of=vars_of, finite_nums=_finite_nums)
