"""Port of shared/scene.ts: Lab mode's scene block (state, elements with live formula properties, rules).

SceneSim runs the rules (the checks' dry run uses it); repair_scene and check_scene are called from
core/validate.py. Warnings are word for word the TypeScript ones (the parity tests compare them). The
validator's helpers come in as `H` (check_expr, clean_defs, repair_block, sanity_check, vars_of) to avoid an
import cycle.
"""

from __future__ import annotations

import math
import re
from collections import deque
from typing import Any, Callable

from ..js import is_finite, js_max, js_min, js_round
from .expr import eval_number, eval_value
from .views import with_defs

LIMITS = {"state": 8, "elements": 10, "rules": 12, "timers": 2, "minMs": 30, "firings": 50, "dryRunMs": 3000}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.A)
RESERVED = {"x", "y", "seed", "i", "row"}
CLICK_VARS = {"chart": "x", "bars": "i", "table": "row"}
CLICKABLE = {"button", "chart", "bars", "table"}
VISUALS = {"chart", "readout", "bars", "table", "text"}
PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")

Warn = Callable[[str], None]


def _ident(s: Any) -> bool:
    return isinstance(s, str) and IDENT.fullmatch(s) is not None


def _truthy(v: float) -> bool:
    return is_finite(v) and v != 0


def _finite(n: Any, d: float) -> float:
    return n if is_finite(n) else d


class SceneSim:
    """Runs a scene's rules over its state. See SceneSim in scene.ts."""

    def __init__(self, b: dict, page: Callable[[], dict]):
        self.b = b
        self.page = page
        self.vars: dict = {}
        self.by_name: dict = {}
        for s in b["state"]:
            self.vars[s["name"]] = s["value"]
            self.by_name[s["name"]] = s

    def scope(self, extra: dict | None = None) -> dict:
        return with_defs(self.b.get("defs"), {**self.page(), **self.vars, **(extra or {})})

    def holds(self, src: str | None, extra: dict | None = None) -> bool:
        return True if not src else _truthy(eval_number(src, self.scope(extra)))

    def fire(self, when: str, target: str = "", extra: dict | None = None) -> dict:
        first = [(r, i) for i, r in enumerate(self.b["rules"]) if r["when"] == when and (when == "start" or r.get("target") == target)]
        return self._run(first, extra or {})

    def tick(self, index: int) -> dict:
        return self._run([(self.b["rules"][index], index)], {})

    def _run(self, first: list, extra: dict) -> dict:
        out = {"firings": 0, "overflow": False, "bad": [], "culprit": -1}
        count: dict[int, int] = {}
        queue = deque((r, i, extra) for r, i in first)
        while queue:
            r, i, ex = queue.popleft()
            out["firings"] += 1
            if out["firings"] > LIMITS["firings"]:
                # The loop's culprit: the rule that ran most often (first such rule on a tie).
                out["overflow"] = True
                best = (-1, 0)
                for k, v in count.items():
                    if v > best[1]:
                        best = (k, v)
                out["culprit"] = best[0]
                break
            count[i] = count.get(i, 0) + 1
            if not self.holds(r.get("if"), ex):
                continue
            sc = self.scope(ex)
            values = [eval_number(a["expr"], sc) for a in r["set"]]
            changed: list[str] = []
            for k, a in enumerate(r["set"]):
                s = self.by_name.get(a["var"])
                if s is None:
                    continue
                if not is_finite(values[k]):
                    out["bad"].append({"rule": i, "var": a["var"]})
                    continue
                v = js_min(s["max"], js_max(s["min"], values[k]))
                if self.vars[a["var"]] != v:
                    self.vars[a["var"]] = v
                    if a["var"] not in changed:
                        changed.append(a["var"])
            for name in changed:
                for i2, r2 in enumerate(self.b["rules"]):
                    if r2["when"] == "change" and r2.get("target") == name:
                        queue.append((r2, i2, {}))
        return out


def _assign(b: dict, **kw: Any) -> dict:
    out = dict(b)
    for k, v in kw.items():
        if v is UNDEF_KEY:
            out.pop(k, None)
        else:
            out[k] = v
    return out


UNDEF_KEY = object()  # "remove this key" (JS: a property set to undefined, dropped by JSON.stringify)


def chart_of(e: dict, defs: list[dict]) -> dict:
    """The chart block a chart element stands for (it shares the scene's defs)."""
    return {"type": "chart", "title": e.get("label", ""), "lesson": "", "defs": defs, "series": e.get("series", []),
            "x": e.get("x", ""), "by": e.get("by", ""), "at": e.get("at", []), "view": e.get("view", "line"),
            "labels": e.get("labels", []), "caption": ""}


def _placeholders(md: str) -> list[str]:
    return [m.group(1) for m in PLACEHOLDER.finditer(md)]


def repair_scene(b: dict, vars_: set[str], warn: Warn, H: Any) -> dict | None:
    name = f'Scene "{b["title"]}"'
    state: list[dict] = []
    for s in b["state"][: LIMITS["state"]]:
        if not _ident(s["name"]) or s["name"] in RESERVED:
            why = "it isn't a usable name"
        elif s["name"] in vars_:
            why = "a page control already uses that name"
        elif any(t["name"] == s["name"] for t in state):
            why = "it is declared twice"
        else:
            why = ""
        if why:
            warn(f'{name}: dropped state variable "{s["name"]}" ({why}).')
            continue
        lo, hi = _finite(s["min"], 0), _finite(s["max"], 1)
        if lo > hi:
            lo, hi = hi, lo
        if lo == hi:
            hi = lo + 1
        step = _finite(s["step"], 0)
        if step <= 0 or step > hi - lo:
            step = (hi - lo) / 100
        state.append(_assign(s, min=lo, max=hi, step=step, value=js_min(hi, js_max(lo, _finite(s["value"], lo)))))
    names = {s["name"] for s in state}
    defs, scope = H.clean_defs(b.get("defs"), set(vars_) | names, warn, f'"{b["title"]}"')

    def ok(src: str | None, allowed: set[str]) -> bool:
        return not src or H.check_expr(src, allowed) is None

    elements: list[dict] = []
    for e in b["elements"][: LIMITS["elements"]]:
        def drop(why: str, e=e) -> None:
            warn(f'{name}: dropped {e["kind"]} "{e["id"]}" ({why}).')

        if not _ident(e["id"]) or any(o["id"] == e["id"] for o in elements):
            drop("its id is missing or repeated")
            continue
        el = dict(e)
        k = e["kind"]
        if k == "slider" and not (e.get("var") and e["var"] in names):
            drop("it must set one of the scene's state variables")
            continue
        if k == "chart":
            c = H.repair_block(chart_of(e, defs), scope, warn)
            if not c or c["type"] != "chart":
                drop("its chart isn't valid")
                continue
            el = _assign(el, series=c["series"], x=c["x"], by=c["by"], at=c["at"], labels=c["labels"], view=c["view"])
        if k == "readout" and not (e.get("expr") and ok(e["expr"], scope)):
            drop("its formula isn't valid")
            continue
        if k == "bars" and not (e.get("values") and ok(e["values"], scope)):
            drop("its formula isn't valid")
            continue
        if k == "table":
            cols = (e.get("columns") or [])[:8]
            if not cols:
                drop("it has no columns")
                continue
            rows = [["—" if c.startswith("=") and not ok(c[1:], scope) else c for c in r[: len(cols)]] for r in (e.get("rows") or [])[:24]]
            el = _assign(el, columns=cols, rows=rows)
        if k == "text":
            if not e.get("md"):
                drop("it has no text")
                continue
            el = _assign(el, md=PLACEHOLDER.sub(lambda m: m.group(0) if ok(m.group(1), scope) else "—", e["md"]))
        # Live properties that don't compile are removed.
        for key in ("visible", "disabled", "highlight"):
            if el.get(key) and not ok(el[key], scope):
                warn(f'{name}: removed the {key} formula of "{e["id"]}".')
                el = _assign(el, **{key: UNDEF_KEY})
        elements.append(el)
    if not any(e["kind"] in VISUALS for e in elements):
        warn(f'Dropped scene "{b["title"]}": nothing in it shows anything.')
        return None

    kind_of = {e["id"]: e["kind"] for e in elements}
    rules: list[dict] = []
    timers = 0
    for n, r in enumerate(b["rules"][: LIMITS["rules"]]):
        def drop_rule(why: str, n=n) -> None:
            warn(f"{name}: dropped rule {n + 1} ({why}).")

        target = r.get("target") or ""
        if r["when"] == "click" and kind_of.get(target) not in CLICKABLE:
            drop_rule(f'"{target}" isn\'t a button, chart, bar chart or table in the scene')
            continue
        if r["when"] == "change" and not (target in names or target in vars_):
            drop_rule(f'"{target}" isn\'t a variable')
            continue
        if r["when"] == "every":
            timers += 1
            if timers > LIMITS["timers"]:
                drop_rule(f"at most {LIMITS['timers']} timers")
                continue
        event = CLICK_VARS.get(kind_of.get(target)) if r["when"] == "click" else None
        allowed = scope | {event} if event else scope
        if not ok(r.get("if"), allowed):
            drop_rule("its condition isn't valid")
            continue
        sets = [a for a in r["set"] if a["var"] in names and ok(a["expr"], allowed)]
        if len(sets) < len(r["set"]):
            warn(f"{name}: rule {n + 1} lost {len(r['set']) - len(sets)} assignment(s) (only valid formulas setting the scene's own variables are kept).")
        if not sets:
            drop_rule("it sets nothing")
            continue
        ms = js_max(LIMITS["minMs"], js_round(_finite(r.get("ms", 500), 500))) if r["when"] == "every" else UNDEF_KEY
        rules.append(_assign(r, target=UNDEF_KEY if r["when"] in ("start", "every") else target, ms=ms, set=sets))
    return _assign(b, state=state, defs=defs, elements=elements, rules=rules)


def _formulas_of(e: dict) -> list[str]:
    own = [e.get("visible"), e.get("disabled"), e.get("highlight"), e.get("expr"), e.get("values"),
           *[s["expr"] for s in e.get("series") or []], e.get("x"), e.get("by"),
           *[c[1:] for r in e.get("rows") or [] for c in r if c.startswith("=")], *(_placeholders(e["md"]) if e.get("md") else [])]
    return [s for s in own if s]


def check_scene(b: dict, scopes: list[dict], warn: Warn, sliders: dict, H: Any) -> dict | None:
    name = f'Scene "{b["title"]}"'
    defaults = {s["name"]: s["value"] for s in b["state"]}
    scs = [{**sc, **defaults} for sc in scopes]
    for s in b["state"]:
        scs.append({**scopes[0], **defaults, s["name"]: s["min"]})
        scs.append({**scopes[0], **defaults, s["name"]: s["max"]})
    with_d = [with_defs(b.get("defs"), sc) for sc in scs]

    def alive(src: str) -> bool:
        return any(len(H.finite_nums(eval_value(src, sc))) > 0 for sc in with_d)

    merged = dict(sliders)
    for s in b["state"]:
        merged[s["name"]] = {"type": "slider", "var": s["name"], "label": s["label"], "min": s["min"], "max": s["max"],
                             "step": s["step"], "value": s["value"], "unit": ""}

    elements: list[dict] = []
    for e in b["elements"]:
        def drop(why: str, e=e) -> None:
            warn(f'{name}: dropped {e["kind"]} "{e["id"]}" ({why}).')

        k = e["kind"]
        if k == "chart":
            c = H.sanity_check(chart_of(e, b.get("defs") or []), scs, warn, merged)
            if not c or c["type"] != "chart":
                drop("its numbers never work out")
                continue
            elements.append(_assign(e, series=c["series"], x=c["x"], by=c["by"], at=c["at"], view=c["view"]))
            continue
        if k == "readout" and not alive(e["expr"]):
            drop("its numbers never work out")
            continue
        if k == "bars" and not alive(e["values"]):
            drop("its numbers never work out")
            continue
        if k == "table":
            elements.append(_assign(e, rows=[["—" if c.startswith("=") and not alive(c[1:]) else c for c in r] for r in e["rows"]]))
            continue
        if k == "text":
            elements.append(_assign(e, md=PLACEHOLDER.sub(lambda m: m.group(0) if alive(m.group(1)) else "—", e["md"])))
            continue
        elements.append(e)

    # What the scene shows, and every variable that can move it (through rules, transitively).
    shown: set[str] = set()
    for e in elements:
        if e["kind"] not in ("slider", "button"):
            for f in _formulas_of(e):
                shown.update(H.vars_of(f, b.get("defs")))
    grew = True
    while grew:
        grew = False
        for r in b["rules"]:
            if not any(a["var"] in shown for a in r["set"]):
                continue
            reads = [v for f in [r.get("if") or "", *[a["expr"] for a in r["set"]]] if f for v in H.vars_of(f, b.get("defs"))]
            for v in [*reads, *([r["target"]] if r["when"] == "change" and r.get("target") else [])]:
                if v not in shown:
                    shown.add(v)
                    grew = True

    def moves_shown(r: dict) -> bool:
        return any(a["var"] in shown for a in r["set"])

    kept = []
    for e in elements:
        if e["kind"] == "slider" and e["var"] not in shown:
            warn(f'{name}: dropped slider "{e["id"]}": nothing in the scene shows {e["var"]}.')
            continue
        if e["kind"] == "button" and not any(r["when"] == "click" and r.get("target") == e["id"] and moves_shown(r) for r in b["rules"]):
            warn(f'{name}: dropped button "{e["id"]}": clicking it changes nothing the scene shows.')
            continue
        kept.append(e)
    elements = kept
    ids = {e["id"] for e in elements}
    rules = [r for r in b["rules"] if r["when"] != "click" or (r.get("target") or "") in ids]
    if not any(e["kind"] in VISUALS for e in elements):
        warn(f'Dropped scene "{b["title"]}": nothing in it shows anything.')
        return None

    # Dry run: start, click each button, then 3 simulated seconds of timers.
    scene = _assign(b, elements=elements, rules=rules)
    sim = SceneSim(scene, lambda: scopes[0])
    loops: set[int] = set()
    bad: dict[str, int] = {}

    def note(res: dict, starters: list[int]) -> None:
        if res["overflow"]:
            loops.add(res["culprit"] if res["culprit"] >= 0 else starters[0])
        for x in res["bad"]:
            bad.setdefault(f'{x["rule"]}:{x["var"]}', x["rule"])

    def starters_of(pred) -> list[int]:
        return [i for i, r in enumerate(rules) if pred(r)]

    note(sim.fire("start"), starters_of(lambda r: r["when"] == "start"))
    for e in elements:
        if e["kind"] == "button":
            note(sim.fire("click", e["id"]), starters_of(lambda r, e=e: r["when"] == "click" and r.get("target") == e["id"]))
    for t in range(10, LIMITS["dryRunMs"] + 1, 10):
        for i, r in enumerate(rules):
            if r["when"] == "every" and t % (math.ceil(r.get("ms", 500) / 10) * 10) == 0:
                note(sim.tick(i), [i])
    for key, i in bad.items():
        warn(f'{name}: rule {i + 1} sets {key.split(":")[1]} to something that isn\'t a number.')
    if loops:
        for i in sorted(loops):
            warn(f"{name}: dropped rule {i + 1} (it set off an endless chain of updates).")
        rules = [r for i, r in enumerate(rules) if i not in loops]
    return _assign(scene, rules=rules)
