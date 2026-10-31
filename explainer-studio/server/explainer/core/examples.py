"""Port of shared/examples.ts: worked examples, where code computes the numbers and Haiku only writes the steps."""

from __future__ import annotations

from typing import Any

from ..js import is_finite, is_num, js_str, round_num
from .expr import eval_value, is_mat, try_compile
from .views import run_trajectory, with_defs


def _show(v: Any) -> str:
    def n(x):
        return js_str(round_num(x, 4)) if is_finite(x) else "undefined"

    if is_num(v):
        return n(v)
    if is_mat(v):
        return f"{len(v)}×{len(v[0])} matrix"
    if v is None:
        return "undefined"
    return "[" + ", ".join(n(x) for x in v[:6]) + (", …" if len(v) > 6 else "") + "]"


def _defaults(spec: dict) -> tuple[dict, dict]:
    scope: dict = {"seed": 0.0}
    labels: dict = {}
    for st in spec["stages"]:
        for b in st["blocks"]:
            if b["type"] in ("slider", "choice", "toggle") and b["var"] not in scope:
                scope[b["var"]] = b["value"]
                labels[b["var"]] = b["label"]
    return scope, labels


def _reads_any(src: str, defs: list[dict], names: set) -> bool:
    """Whether a formula reads one of names, directly or through defs (see readsAny in examples.ts)."""
    by_name = {d["name"]: d["expr"] for d in defs}
    seen: set = set()

    def visit(s: str) -> bool:
        r = try_compile(s)
        if not r["ok"]:
            return False
        for v in r["c"].vars:
            if v in names:
                return True
            if v in by_name and v not in seen:
                seen.add(v)
                if visit(by_name[v]):
                    return True
        return False

    return visit(src)


def stage_facts(blocks: list[dict], scope: dict) -> list[dict]:
    """The quantities a stage shows, evaluated in a given scope."""
    out: list[dict] = []
    for b in blocks:
        t = b["type"]
        if t == "readout":
            s = with_defs(b.get("defs"), scope)
            out.extend({"label": it["label"], "value": _show(eval_value(it["expr"], s))} for it in b["items"])
        elif t == "calculator":
            s = with_defs(b.get("defs"), scope)
            out.extend({"label": r["latex"], "value": _show(eval_value(r["expr"], s))} for r in b["rows"])
        elif t == "samples":
            s = with_defs(b.get("defs"), scope)
            out.extend({"label": st["label"], "value": _show(eval_value(st["expr"], s))} for st in b["stats"])
        elif t == "trajectory":
            run = run_trajectory(b, scope)
            last = run["frames"][-1]
            tag = f" (diverged at step {run['divergedAt']})" if run["divergedAt"] > 0 else ""
            for st in b["state"]:
                out.append({"label": f"{b['title']}: {st['name']} after {len(run['frames']) - 1} steps{tag}", "value": _show(last.get(st["name"]))})
        elif t == "table":
            for row in b["rows"]:
                for c, cell in enumerate(row):
                    if cell.startswith("="):
                        col = b["columns"][c] if c < len(b["columns"]) else "undefined"
                        out.append({"label": f"{row[0]} ({col})", "value": _show(eval_value(cell[1:], scope))})
        elif t == "bars":
            s = with_defs(b.get("defs"), scope)
            if len(b["values"]) == 1 and len(b["labels"]) != 1:
                out.append({"label": b["title"] or "bars", "value": _show(eval_value(b["values"][0], s))})
            else:
                for i, v in enumerate(b["values"]):
                    lbl = b["labels"][i] if i < len(b["labels"]) else f"#{i + 1}"
                    out.append({"label": f"{b['title']}: {lbl}", "value": _show(eval_value(v, s))})
        elif t == "scene":
            # A lab's numbers set by the page's controls, not by its own variables (see stageFacts in examples.ts).
            own = {v["name"] for v in b["state"]}
            defs = b.get("defs") or []
            s = with_defs(defs, {**scope, **{v["name"]: v["value"] for v in b["state"]}})
            for e in b["elements"]:
                if e["kind"] == "readout" and e.get("expr") and not _reads_any(e["expr"], defs, own):
                    out.append({"label": e.get("label") or "value", "value": _show(eval_value(e["expr"], s))})
                elif e["kind"] == "bars" and e.get("values") and not _reads_any(e["values"], defs, own):
                    out.append({"label": e.get("label") or "bars", "value": _show(eval_value(e["values"], s))})
                elif e["kind"] == "table":
                    cols = e.get("columns") or []
                    for row in e.get("rows") or []:
                        for c, cell in enumerate(row):
                            if cell.startswith("=") and not _reads_any(cell[1:], defs, own):
                                col = cols[c] if c < len(cols) else "undefined"
                                out.append({"label": f"{row[0]} ({col})", "value": _show(eval_value(cell[1:], s))})
    return out[:24]


def example_briefs(spec: dict) -> list[dict]:
    scope, labels = _defaults(spec)
    briefs: list[dict] = []
    for st in spec["stages"]:
        for bi, b in enumerate(st["blocks"]):
            if b["type"] != "examples":
                continue
            for ii, it in enumerate(b["items"]):
                if it.get("steps"):
                    continue  # already written
                s = dict(scope)
                for x in it["set"]:
                    s[x["var"]] = x["value"]
                briefs.append({
                    "key": f"{st['id']}:{bi}:{ii}",
                    "title": it["title"],
                    "focus": it["focus"],
                    "settings": [{"name": x["var"], "label": labels.get(x["var"], x["var"]), "value": x["value"]} for x in it["set"]],
                    "computed": stage_facts(st["blocks"], s),
                })
    return briefs


def apply_example_steps(spec: dict, steps: dict[str, list[str]]) -> dict:
    stages = []
    for st in spec["stages"]:
        blocks = []
        for bi, b in enumerate(st["blocks"]):
            if b["type"] != "examples":
                blocks.append(b)
                continue
            items = []
            for ii, it in enumerate(b["items"]):
                s = steps.get(f"{st['id']}:{bi}:{ii}")
                items.append({**it, "steps": s[:6]} if s else it)
            blocks.append({**b, "items": items})
        stages.append({**st, "blocks": blocks})
    return {**spec, "stages": stages}
