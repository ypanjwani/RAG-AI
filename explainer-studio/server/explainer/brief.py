"""Page brief (Quality+/Lite+): before Haiku plans, Jev answers fixed-menu questions about the
request (level, approach, look...) in one call.

Teaching decisions become one-line hints in the planner's user turn (the cached
system prompt never changes); look-and-feel decisions are stamped onto the spec
by code (design.apply_brief). The reader's own choice (the Level toggle) always
wins; an unconfident Jev answer, a missing key or a failed call falls back to
each menu's default (its first key). How much to write (stage count, quizzes,
worked examples) is left to the planner: from the request sentence alone Jev
only ever shrank pages (Oct 2026).
"""

from __future__ import annotations

import logging

from . import data
from .jev import jev_ask
from .llm import warning_reason
from .js import js_slice
from .pricing import UsageLog

log = logging.getLogger(__name__)

MIN_P = 0.45  # a choice is used only when its top option has at least this probability
YES_P = 0.6  # a yes/no shape check counts as yes at or above this...
NO_P = 0.2  # ...and as a confident no at or below this
EXCERPT_CHARS = 3000

SHAPES = {
    "process": "Is the topic itself a strict step-by-step data-processing sequence that a reader should walk through in order (input, then step 1, step 2, ... output)?",
    "random": "Does the topic centrally involve a genuinely random process (sampling, random seeds, noise, dropout, stochastic environments)?",
    "equations": "Does the topic have at least one central equation or formula a learner should understand?",
    "architecture": "Is the topic about a specific model architecture made of named building blocks?",
    "results": "Does the paper report concrete experimental results (benchmarks, numbers, comparisons)?",
}

LEVEL_LABEL = {"beginner": "beginner", "student": "intermediate", "practitioner": "advanced"}


def _m(name: str) -> dict:
    return data.menu(name)


def _first(menu: dict) -> str:
    return next(iter(menu))


def brief_state(req: dict) -> dict:
    """What Jev sees: the request, not the whole paper (so it can run alongside the summaries)."""
    f = req.get("followUp")
    previous = js_slice(f["previous"], 0, 1000) if f and f["kind"] == "new" else None
    if req["mode"] == "paper":
        opening = "\n\n".join(f"{s['heading']}\n{s['text']}" for s in req["sections"][:2])
        state = {"kind": "research paper", "title": req["title"], "readerNote": req["note"] or None,
                 "opening": js_slice(opening, 0, EXCERPT_CHARS), "previous": previous}
    else:
        state = {"kind": "AI concept request", "request": js_slice(req["text"], 0, EXCERPT_CHARS), "previous": previous}
    return {k: v for k, v in state.items() if v is not None}  # undefined fields are left out of the JSON


def _choice(instructions: str, criteria: dict) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def brief_questions(req: dict) -> dict[str, dict]:
    """One request: a choice per menu (Level only when the reader left it on Auto) plus the yes/no shape checks."""
    qs: dict[str, dict] = {}
    if not req.get("level") or req["level"] == "auto":
        qs["level"] = _choice("Which reader level fits this request best?", _m("LEVELS"))
    qs.update({
        "math": _choice("How much mathematics should the explainer show?", _m("MATH_DEPTH")),
        "approach": _choice("Which teaching approach suits this topic best?", _m("APPROACH")),
        "goal": _choice("What does the reader most likely want from the explainer?", _m("GOAL")),
        "analogies": _choice("How many everyday analogies should the explainer use?", _m("ANALOGIES")),
        "tone": _choice("Which tone suits this explainer?", _m("TONE")),
        "area": _choice("Which area of AI is this topic in?", _m("TOPIC_AREA")),
        "textSize": _choice("Which text size suits this explainer's readers?", _m("TEXT_SIZES")),
        "density": _choice("Which spacing suits this explainer?", _m("DENSITY")),
        "emphasis": _choice("How should important text stand out?", _m("EMPHASIS")),
        "emphasisAmount": _choice("How much text should be emphasised?", _m("EMPHASIS_AMOUNT")),
        "theme": _choice("Which colour theme suits this topic?", _m("THEMES")),
        "fonts": _choice("Which font pairing suits this topic and its readers?", _m("FONT_PAIRINGS")),
        "heading": _choice("Which stage heading style suits this explainer?", _m("HEADING_STYLES")),
    })
    for k, q in SHAPES.items():
        if k == "results" and req["mode"] != "paper":
            continue
        qs[f"shape_{k}"] = {"type": "noul", "instructions": q}
    return qs


def _confident(menu: dict, a: dict | None) -> str | None:
    if not a or a["type"] != "choice":
        return None
    best = a["ranking"][0] if a["ranking"] else None
    return best["option"] if best and best["p"] >= MIN_P and best["option"] in menu else None


def decide_brief(answers: dict[str, dict], req: dict) -> dict:
    """Jev's answers (possibly none) and the reader's choices -> the brief."""

    def pick(menu_name: str, qid: str) -> str:
        menu = _m(menu_name)
        return _confident(menu, answers.get(qid)) or _first(menu)

    shape: dict[str, bool] = {}
    for k in SHAPES:
        a = answers.get(f"shape_{k}")
        if a and a["type"] == "noul":
            if a["p"] >= YES_P:
                shape[k] = True
            elif a["p"] <= NO_P:
                shape[k] = False
    f = req.get("followUp")
    revise = bool(f) and f["kind"] == "revise"
    jev_level = _confident(_m("LEVELS"), answers.get("level"))
    fixed = req["level"] if req.get("level") and req["level"] != "auto" else None
    level = fixed or jev_level or (None if revise else "beginner")
    brief = {
        "level": level,
        "levelBy": "reader" if fixed else "jev" if jev_level else "default" if level else None,
        "math": pick("MATH_DEPTH", "math"),
        "approach": pick("APPROACH", "approach"),
        "goal": pick("GOAL", "goal"),
        "analogies": pick("ANALOGIES", "analogies"),
        "tone": pick("TONE", "tone"),
        "area": pick("TOPIC_AREA", "area"),
        "textSize": pick("TEXT_SIZES", "textSize"),
        "density": pick("DENSITY", "density"),
        "emphasis": pick("EMPHASIS", "emphasis"),
        "emphasisAmount": pick("EMPHASIS_AMOUNT", "emphasisAmount"),
        "theme": _confident(_m("THEMES"), answers.get("theme")),
        "fontPairing": _confident(_m("FONT_PAIRINGS"), answers.get("fonts")),
        "headingStyle": _confident(_m("HEADING_STYLES"), answers.get("heading")),
        "shape": shape,
        "byJev": len(answers) > 0,
    }
    return brief


def brief_hint(b: dict) -> str:
    """The planner hint: one short line per decision that differs from the default. Empty when nothing does."""
    lines: list[str] = []
    sh = b["shape"]
    if b["level"]:
        lines.append(f'Write for a {LEVEL_LABEL[b["level"]]} reader: use level "{b["level"]}".')
    if b["math"] == "none":
        lines.append("Use almost no formulas: carry the ideas with words, visuals and numbers.")
    if b["math"] == "derivations":
        lines.append("Show the full formulas and the steps between them.")
    if b["approach"] == "example":
        lines.append("Open each idea with a concrete example, then generalise.")
    if b["approach"] == "problem":
        lines.append("Start from the problem the idea solves: what goes wrong without it.")
    if b["approach"] == "story":
        lines.append("Tell it as a story of how the idea evolved, each step fixing the last one's limits.")
    if b["approach"] == "compare":
        lines.append("Teach it by comparison with the alternatives it competes with.")
    if b["goal"] == "implement":
        lines.append("The reader wants to use it: include concrete steps, settings and pitfalls.")
    if b["goal"] == "judge":
        lines.append("The reader wants to judge the claims: show the evidence, results and limitations.")
    if b["goal"] == "revise":
        lines.append("The reader is revising: favour definitions, key facts and practice questions.")
    if b["analogies"] == "none":
        lines.append("Don't use analogies; explain the real mechanism.")
    if b["analogies"] == "many":
        lines.append("Use plenty of everyday analogies.")
    if b["tone"] == "friendly":
        lines.append("Use a warm, encouraging tone.")
    if b["tone"] == "academic":
        lines.append("Use a precise, formal tone.")
    if b["area"] != "general":
        lines.append(f"Topic area: {_m('TOPIC_AREA')[b['area']]}")
    if sh.get("process") is True:
        lines.append("The topic is a strict step-by-step sequence: the pipeline layout or a pipeline block may fit.")
    if sh.get("random") is True:
        lines.append("The topic involves a genuinely random process: an experiment block fits.")
    if sh.get("random") is False:
        lines.append("The topic is deterministic: don't use an experiment block.")
    if sh.get("equations") is False:
        lines.append("The topic has no central equation: don't force formulas in.")
    if sh.get("architecture") is True:
        lines.append("The topic is a model architecture: an architecture block fits.")
    if sh.get("results") is True:
        lines.append("Include the paper's reported results, in a table.")
    if b["emphasis"] == "bold":
        lines.append("Emphasis: bold only; don't use ==highlight== or [[term]] marks.")
    if b["emphasis"] == "highlight":
        lines.append("Emphasis: mark key sentences with ==highlight==; don't use [[term]] marks.")
    if b["emphasis"] == "colour":
        lines.append("Emphasis: mark key terms with [[term]]; don't use ==highlight==.")
    if b["emphasis"] != "bold" and b["emphasisAmount"] == "sparing":
        lines.append("Emphasise sparingly: only the single most important point per stage.")
    if b["emphasis"] != "bold" and b["emphasisAmount"] == "heavy":
        lines.append("Emphasise generously, for readers who skim.")
    return "Page brief:\n" + "\n".join(f"- {line}" for line in lines) if lines else ""


def brief_summary(b: dict) -> str:
    """A short progress line: "intermediate · example first · ocean"."""
    parts = [
        (LEVEL_LABEL[b["level"]] + ("" if b["levelBy"] == "reader" else " (Jev)" if b["levelBy"] == "jev" else " (default)")) if b["level"] else "",
        f"{b['approach']} first" if b["approach"] != "intuition" else "",
        f"maths: {b['math']}" if b["math"] != "intuition" else "",
        b["theme"] or "",
    ]
    return " · ".join(p for p in parts if p)


def ask_brief(jev_key: str | None, req: dict, usage: UsageLog, warnings: list[str]) -> dict:
    """Never raises: without a key or on failure the defaults are used and a warning says why."""
    # A revision keeps the previous version's structure and look: only an explicit Level applies.
    f = req.get("followUp")
    if f and f["kind"] == "revise":
        return decide_brief({}, req)
    if not jev_key:
        warnings.append("Page brief: no Jev key; defaults used.")
        return decide_brief({}, req)
    try:
        r = jev_ask(jev_key, brief_state(req), brief_questions(req))
        usage.add("jev brief", r["model"], r["usage"])
        return decide_brief(r["answers"], req)
    except Exception as e:
        log.exception("jev brief failed")
        warnings.append(f"Page brief: {warning_reason(e)}; defaults used.")
        return decide_brief({}, req)
