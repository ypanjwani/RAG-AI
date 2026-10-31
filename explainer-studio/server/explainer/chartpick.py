"""The chart block switch, and the planner's numeric experiments turned into charts.

CHART_BLOCK (env): the Quality+ planner gets OUTLINE_CHART_SYSTEM, which offers the chart block. On by default
since Oct 2026; CHART_BLOCK=0 restores the old prompt exactly.

Even with that prompt the planner still writes some experiments for numeric random results (an average, an error),
whose running-average view can't show a lesson about spread ("averaging more models shrinks the spread": the
number of models is fixed inside one run). `experiments_to_charts` turns each such experiment into a chart before
assembly. Jev picks the view (histogram, compare, running) from the experiment's title and caption; it matched a
person's choice on 13 of 13 planner charts (evals/chart_eval.py). Experiments with named outcomes (which token,
which class) stay: the chart block draws numbers only. Never raises; without a Jev key a fixed rule picks the view.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from .core.validate import vars_of
from .jev import JevError, jev_ask
from .pricing import UsageLog

# What Jev reads for each view (the chart eval offers the same text).
VIEW_CRITERIA = {
    "line": "Each quantity drawn as a curve against the x-axis variable, with a marker at its current value: shows how the quantity changes as that one setting changes.",
    "lines": "One curve per value of the second variable, all against the x-axis variable: shows how the effect of the x setting depends on the second setting.",
    "heatmap": "A colour map over the x-axis variable and the second variable: shows how the quantity depends on both settings at once.",
    "histogram": "Many random draws at the current settings, as a histogram: shows how a random result spreads and where it centres.",
    "compare": "Two histograms of the random result overlaid, one at each of two values of the second variable: shows how its spread or centre changes between two settings.",
    "running": "The running average of repeated random runs: shows an estimate wobbling at first and settling down as runs accumulate.",
    "bars": "One bar per element of a list: shows how a total or a probability is shared out.",
}
PICK_MIN_P = 0.5  # below this, the fixed rule decides


def chart_on() -> bool:
    """The chart block switch (default on; CHART_BLOCK=0 turns it off)."""
    return os.environ.get("CHART_BLOCK", "1") != "0"


def _setting_for(b: dict, read: list[dict]) -> dict | None:
    """The slider an experiment is about: the first one its title or caption names, else the first it reads."""
    text = f"{b.get('title', '')} {b.get('caption', '')} {b.get('valueLabel', '')}".lower()
    for s in read:
        if s["var"].lower() in text.split() or s["label"].lower() in text:
            return s
    return read[0] if read else None


def _ask(jev_key: str, page: str, b: dict, setting: dict | None, fits: list[str]) -> tuple[str | None, float, dict | None]:
    state = {"page": page, "experiment": {"title": b.get("title", ""), "what one run measures": b.get("valueLabel", ""),
                                          "caption": b.get("caption", ""), "formula": b["trial"],
                                          "setting": setting["label"] if setting else "none"}}
    q = {"view": {"type": "choice", "instructions": "Which view of `experiment` shows what its title and caption are about?",
                  "criteria": {v: VIEW_CRITERIA[v] for v in fits}}}
    try:
        r = jev_ask(jev_key, state, q)
        top = r["answers"]["view"]["ranking"][0]
        return top["option"], top["p"], r
    except (JevError, KeyError, IndexError, TypeError):
        return None, 0.0, None


def experiments_to_charts(outline: dict, jev_key: str | None, usage: UsageLog, warnings: list[str]) -> dict:
    """The outline with each numeric experiment replaced by a chart (or dropped when a chart in its stage already shows it)."""
    sliders = {b["var"]: b for st in outline["stages"] for b in st["blocks"] if b.get("type") == "slider" and isinstance(b.get("var"), str)}
    todo: list[tuple[int, int, dict, dict | None, list[str]]] = []
    drop: set[tuple[int, int]] = set()
    for si, st in enumerate(outline["stages"]):
        charts = [c for c in st["blocks"] if c.get("type") == "chart"]
        for bi, b in enumerate(st["blocks"]):
            if b.get("type") != "experiment" or b.get("outcomes") or not isinstance(b.get("trial"), str):
                continue
            if any(any(s.get("expr") == b["trial"] for s in c.get("series") or []) for c in charts):
                drop.add((si, bi))
                warnings.append(f'Chart switch: dropped the experiment "{b.get("title", "")}" (a chart in the same stage already shows it).')
                continue
            read = [sliders[v] for v in vars_of(b["trial"], b.get("defs")) if v in sliders]
            setting = _setting_for(b, read)
            todo.append((si, bi, b, setting, ["histogram", "running"] + (["compare"] if setting else [])))
    if not todo and not drop:
        return outline

    page = outline.get("title", "")
    picks: list[tuple[str | None, float, dict | None]] = [(None, 0.0, None)] * len(todo)
    if jev_key and todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            picks = list(pool.map(lambda t: _ask(jev_key, page, t[2], t[3], t[4]), todo))
        tokens = sum((r or {}).get("usage", {}).get("input_tokens", 0) for _, _, r in picks)
        model = next((r["model"] for _, _, r in picks if r), "jev")
        usage.add("jev charts", model, {"input_tokens": tokens, "output_tokens": 0})

    swap: dict[tuple[int, int], dict] = {}
    for (si, bi, b, setting, fits), (pick, p, _) in zip(todo, picks):
        # Without a confident Jev pick: compare when it reads a setting (its lesson is usually how that setting changes it).
        view = pick if pick in fits and p >= PICK_MIN_P else ("compare" if setting else "running")
        swap[(si, bi)] = {
            "type": "chart", "title": b.get("title", ""), "lesson": b.get("caption") or b.get("valueLabel", ""),
            "defs": b.get("defs") or [], "series": [{"label": b.get("valueLabel", ""), "expr": b["trial"]}],
            "x": "", "by": setting["var"] if view == "compare" and setting else "", "at": [], "view": view, "labels": [],
            "caption": b.get("caption", ""),
        }
        warnings.append(f'Chart switch: the experiment "{b.get("title", "")}" is shown as a {view} chart' + (f" of {setting['label']}" if view == "compare" and setting else "") + ".")

    stages = []
    for si, st in enumerate(outline["stages"]):
        blocks = [swap.get((si, bi), b) for bi, b in enumerate(st["blocks"]) if (si, bi) not in drop]
        stages.append({**st, "blocks": blocks})
    return {**outline, "stages": stages}
