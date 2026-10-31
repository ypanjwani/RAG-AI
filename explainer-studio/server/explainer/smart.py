"""Smart mode: each section gets the writing style and visual it needs, so the page costs only what it uses.

1. Haiku lists the sections (label, blurb, what each covers): `plan_sections`, SMART_SECTIONS_SYSTEM.
2. Jev picks, per section and in parallel, a writing style (quick | interactive | creative) and a visual
   (none | builtin | custom): `pick_styles`. No key or a Jev error means Interactive + built-in for that section.
3. The Quality+ planner writes the page with these picks in its user turn (`smart_directive`); its reply is read
   with the Freeform+ parser, which accepts built-in blocks and "interactive" briefs alike. `enforce` keeps briefs
   only in custom sections (at most one each) and strips quizzes and worked examples from quick sections.
4. Only the custom sections' briefs become AI-written widgets (pipeline.build_widgets).

Measured before building (evals/smart_route.py, 51 labelled sections, plan-level view): Jev agreed with the labels
82% on both choices, and picked custom widgets for 4 of the 5 sections labelled custom with 1 false alarm. It almost
never picks creative text and rarely "none".
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import anthropic

from . import data
from .jev import JevError, jev_ask
from .js import js_slice, json_parse
from .llm import write_text
from .pricing import UsageLog

TEXT = {
    "quick": "`section` is background, motivation, history, a definition, a practical note or a recap. One short plain read is enough; "
             "the reader does not need worked examples or a quiz here.",
    "interactive": "`section` teaches a core idea, formula, mechanism or procedure the reader must understand properly. It deserves a full "
                   "explanation with worked examples and a quiz.",
    "creative": "`section` is about building intuition, and is taught best through a story, an analogy, a puzzle or a playful "
                "\"imagine you are...\" scenario rather than a formal explanation.",
}
VISUAL = {
    "none": "Nothing in `section` needs a chart, diagram or control; text is enough.",
    "builtin": "`section` is best shown with a standard component: a curve or plot, bars, a comparison or results table, a diagram, "
               "a calculator, or a formula whose inputs a slider can change.",
    "custom": "`section` describes a process that has to be simulated step by step to be understood (an algorithm running, data being "
              "transformed, tokens merging, an agent acting, points moving in a space), which standard charts and sliders cannot show.",
}
SAFE = {"text": "interactive", "visual": "builtin"}  # when Jev can't answer: what Interactive mode would do
MAX_SECTIONS = 12
QUICK_DROPPED = {"quiz", "predict", "examples"}

READER = {
    "beginner": "beginner (no prior machine-learning knowledge)",
    "student": "intermediate reader (knows basic machine learning and linear algebra)",
    "practitioner": "advanced reader (works with machine learning)",
}


def questions() -> dict:
    return {
        "text": {"type": "choice", "instructions": "How should the text of `section` be written, given its place in `outline`?", "criteria": TEXT},
        "visual": {"type": "choice", "instructions": "What visual or interactive part does `section` need?", "criteria": VISUAL},
    }


def section_state(title: str, reader: str, labels: list[str], i: int, label: str, blurb: str, content: str) -> dict:
    """What Jev sees for one section: the plan, never block types (the eval's 'plan view')."""
    return {
        "explainer": {"title": title, "reader": reader},
        "outline": labels,
        "section": {"position": f"{i + 1} of {len(labels)}", "title": label, "summary": blurb, "content": content},
    }


def plan_sections(client: anthropic.Anthropic, request: str, usage: UsageLog, count: int | None = None) -> list[dict]:
    """Haiku's section list: [{label, blurb, covers}]. Empty when the reply is unusable."""
    user = request + (f"\n\nPlan exactly {count} sections." if count else "")
    try:
        text = write_text(client, usage, "smart sections", system=data.prompt("SMART_SECTIONS_SYSTEM"), user=user, max_tokens=3000,
                          fmt=data.formats()["smartSections"])
        rows = json_parse(text).get("sections") or []
    except (ValueError, RuntimeError, AttributeError):
        return []
    out = []
    for r in rows[:MAX_SECTIONS]:
        if isinstance(r, dict) and isinstance(r.get("label"), str) and r["label"].strip():
            out.append({"label": js_slice(r["label"].strip(), 0, 80), "blurb": js_slice(str(r.get("blurb") or ""), 0, 300),
                        "covers": js_slice(str(r.get("covers") or ""), 0, 400)})
    return out


def pick_styles(jev_key: str | None, title: str, level: str | None, sections: list[dict], usage: UsageLog, warnings: list[str]) -> list[dict]:
    """Per section {"text", "visual", "by": "jev" | "default"}. Never raises."""
    if not jev_key:
        warnings.append("Smart mode: no Jev key, so every section was written as in Interactive.")
        return [{**SAFE, "by": "default"} for _ in sections]
    reader = READER.get(level or "", READER["beginner"])
    labels = [s["label"] for s in sections]

    def ask(i: int) -> dict:
        s = sections[i]
        try:
            return jev_ask(jev_key, section_state(title, reader, labels, i, s["label"], s["blurb"], s["covers"]), questions())
        except JevError as e:
            return {"error": str(e)}

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(sections)))) as pool:
        results = list(pool.map(ask, range(len(sections))))
    picks, failed, total, model = [], 0, 0, "jev"
    for r in results:
        a = r.get("answers") or {}
        if "error" in r or "text" not in a or "visual" not in a:
            failed += 1
            picks.append({**SAFE, "by": "default"})
            continue
        total += r["usage"].get("input_tokens", 0)
        model = r["model"]
        picks.append({"text": a["text"]["ranking"][0]["option"], "visual": a["visual"]["ranking"][0]["option"], "by": "jev"})
    usage.add("jev smart", model, {"input_tokens": total, "output_tokens": 0})
    if failed:
        warnings.append(f"Smart mode: Jev couldn't judge {failed} of {len(sections)} sections; they were written as in Interactive.")
    return picks


STYLE_RULE = {
    "quick": "Quick read: short, at most about 60 words of text and callouts in all. No quiz, predict or worked examples.",
    "interactive": "Interactive: a full explanation; add worked examples and a quiz where they help.",
    "creative": "Creative: a full explanation told through a story, an analogy, a puzzle or an \"imagine you are...\" scenario; a quiz where it helps.",
}
VISUAL_RULE = {
    "none": "No visual: no chart, control or diagram; text, cards or a static table only.",
    "builtin": "Built-in visual: use the visual components from your block catalogue (plot, bars, slider, calculator, table, diagram...). No \"interactive\" block.",
    "custom": "Custom simulation: add exactly one block of type \"interactive\" (described below), and no sliders or charts for the same thing in this stage.",
}
BRIEF_RULE = (
    'An "interactive" block is not in your catalogue: it is a brief that a separate system builds into a custom simulation. '
    'Write it as {"type": "interactive", "goal": "what the reader discovers by playing with it", "math": "the exact model, concrete and '
    'self-contained: every formula, every variable with its range and default, the data values, and whether anything is random or repeated '
    'over steps", "insight": "the takeaway", "scenarios": ["optional worked-example ideas"]}. Never name a component or widget. '
    "Use it only in the stages marked Custom simulation."
)


def smart_directive(sections: list[dict], picks: list[dict]) -> str:
    """The planner's instructions: the fixed stage list, each with its writing style and visual."""
    lines = [f"# Section plan\nWrite exactly these {len(sections)} stages, in this order and with these labels. "
             "Each stage's writing style and visual were chosen for it: follow them, and keep one voice across the page."]
    for i, (s, p) in enumerate(zip(sections, picks)):
        lines.append(f"{i + 1}. \"{s['label']}\": {s['blurb']}\n   Covers: {s['covers']}\n   {STYLE_RULE[p['text']]}\n   {VISUAL_RULE[p['visual']]}")
    if any(p["visual"] == "custom" for p in picks):
        lines.append(BRIEF_RULE)
    return "\n".join(lines)


def enforce(lab: dict, picks: list[dict], warnings: list[str]) -> dict:
    """Make the planner's outline match the picks: briefs only in custom stages (one each), no quizzes or worked
    examples in quick stages. Stages are matched by position; extra stages are treated as Interactive + built-in."""
    stages, dropped = [], 0
    for i, st in enumerate(lab["stages"]):
        p = picks[i] if i < len(picks) else SAFE
        blocks, briefs = [], 0
        for b in st["blocks"]:
            if b["type"] == "interactive":
                briefs += 1
                if p["visual"] != "custom" or briefs > 1:
                    dropped += 1
                    continue
            if p["text"] == "quick" and b["type"] in QUICK_DROPPED:
                continue
            blocks.append(b)
        stages.append({**st, "blocks": blocks})
    if dropped:
        warnings.append(f"Smart mode: {dropped} custom simulation brief(s) outside the sections picked for one were left out.")
    if len(lab["stages"]) != len(picks):
        warnings.append(f"Smart mode: the page has {len(lab['stages'])} sections where {len(picks)} were planned; styles were matched by position.")
    return {**lab, "stages": stages}


def summary(picks: list[dict], widgets: int) -> dict:
    """spec["smart"]: how many sections got each writing style, how many custom widgets were built, and whether Jev chose."""
    return {
        "quick": sum(p["text"] == "quick" for p in picks),
        "interactive": sum(p["text"] == "interactive" for p in picks),
        "creative": sum(p["text"] == "creative" for p in picks),
        "custom": widgets,
        "byJev": sum(p["by"] == "jev" for p in picks),
    }


def detail(picks: list[dict]) -> str:
    """Progress note: '2 quick · 3 interactive · 1 custom simulation'."""
    parts = [f"{n} {k}" for k in ("quick", "interactive", "creative") if (n := sum(p["text"] == k for p in picks))]
    c = sum(p["visual"] == "custom" for p in picks)
    if c:
        parts.append(f"{c} custom simulation{'s' if c > 1 else ''}")
    return " · ".join(parts)
