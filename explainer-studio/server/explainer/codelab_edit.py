"""Follow-ups that change a finished widget (Code Lab 7.0 and Playground), instead of rewriting the page.

The reader asks for a change to one chart ("let me change the learning rate", "make this a log scale"); the follow-up
path (targets.py) finds the widget and calls edit_widget. Haiku gets the widget it wrote (its code or spec), its plan and the
request, writes the changed widget, and the mode's own checks run on it: the code checks and repairs, Jev's review, and for
Code Lab 7.0 the look check. No gate (the section already earned its widget, and the reader asked for this change) and no cap
(an edit adds no part), but a widget added to a section that has none counts against MAX_MARKED.

Nothing here raises, and a failed edit never loses anything: the old widget comes back unchanged, with a note for the
reader. Text edits belong to targets.py; agrees() only says whether a section's text and its widget still match.

Which kind of widget a block is comes from the block itself (widget_kind), not from the composer: a Creative widget has no
plan and a retired Code Lab chart is a Vega-Lite spec, and neither can be edited (the old widget is kept).
"""

from __future__ import annotations

import json
import logging
import re

import anthropic

from . import codelab as cl
from .jev import JevError, jev_ask
from .pricing import UsageLog

log = logging.getLogger("explainer.codelab_edit")

CAP_NOTE = "This page already has 4 interactive parts. Remove one first, or I can change an existing one."
NOT_EDITABLE = "this kind of interactive part can't be edited yet"
AGREE_LOW = 0.2  # at or below: a clear no (like PG_REVIEW_LOW)


# ---------- reading a finished widget ----------


def widget_kind(block: dict) -> str | None:
    """"playground" | "code-lab" | None (a widget this module can't edit: Creative's, which has no plan, or a retired chart)."""
    html = block.get("html") if isinstance(block.get("html"), str) else ""
    if not isinstance(block.get("plan"), str):
        return None
    if "XW.playground(" in html:
        return "playground"
    if "XW.vega(" in html:
        return None
    return "code-lab"


def read_playground_spec(html: str) -> dict | None:
    """The spec inside a playground block's html (playground_html writes it as XW.playground({...}); the escapes it adds are
    valid JSON), or None."""
    i = html.find("XW.playground(")
    if i < 0:
        return None
    try:
        spec, _ = json.JSONDecoder().raw_decode(html[i + len("XW.playground("):])
    except ValueError:
        return None
    return spec if isinstance(spec, dict) else None


def read_plan(block: dict) -> dict:
    """The plan stored on a widget block (plan_text's JSON) without Jev's scores; {} if it can't be read."""
    try:
        plan = json.loads(block.get("plan") or "")
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in plan.items() if k not in ("review", "math")} if isinstance(plan, dict) else {}


def widgets_in(outline: dict) -> list[tuple[int, int]]:
    """(stage index, block index) of every widget on the page."""
    return [(si, bi) for si, st in enumerate(outline["stages"]) for bi, b in enumerate(st["blocks"]) if b.get("type") == "widget"]


def page_kind(outline: dict, quality: str | None = None) -> str | None:
    """What a widget added to this page should be: the kind its other widgets are (a playground if any is), else the request's
    own mode; None when the page can't build one."""
    kinds = [widget_kind(outline["stages"][si]["blocks"][bi]) for si, bi in widgets_in(outline)]
    if "playground" in kinds:
        return "playground"
    if "code-lab" in kinds:
        return "code-lab"
    return {"playground": "playground", "code-lab-7": "code-lab"}.get(quality or "")


def settings_for(kind: str, effort: str, think: bool = True) -> cl.CodeLabSettings:
    return cl.CodeLabSettings.for_effort(effort, think=think, free=kind == "code-lab", playground=kind == "playground")


def viewed(outline: dict) -> dict:
    """The outline as Haiku reads a section: each widget is its title, not its code (the widget being edited is sent
    separately), so a section's payload doesn't carry every widget's html."""
    return {**outline, "stages": [{**st, "blocks": [{"type": "text", "md": f"(interactive widget: {b.get('title', '')})"} if b.get("type") == "widget" else b
                                                   for b in st["blocks"]]} for st in outline["stages"]]}


def replace_block(outline: dict, si: int, bi: int, block: dict) -> dict:
    stages = list(outline["stages"])
    blocks = list(stages[si]["blocks"])
    blocks[bi] = block
    stages[si] = {**stages[si], "blocks": blocks}
    return {**outline, "stages": stages}


# ---------- editing one widget ----------


def _kept(old: dict, why: str, label: str) -> dict:
    return {"block": old, "changed": False, "facts": None, "plan": None,
            "notes": [f"Couldn't change the interactive part in \"{label}\": {why}. It was kept as it was."]}


def edit_widget(client: anthropic.Anthropic, usage: UsageLog, jev_key: str | None, outline: dict, si: int, bi: int, what: str,
                effort: str = "medium", think: bool = True) -> dict:
    """{"block", "changed", "facts", "plan", "notes"}: the widget at outline[si].blocks[bi] changed as `what` says, or the
    old widget when the change failed (changed False, a note saying why). facts and plan are the new widget's, for agrees()."""
    old = outline["stages"][si]["blocks"][bi]
    label = outline["stages"][si].get("label", "")
    try:
        kind = widget_kind(old)
        if kind is None:
            return _kept(old, NOT_EDITABLE, label)
        view = viewed(outline)
        res: dict = {"notes": [], "built": False, "repaired": 0, "backup": False, "chart": False}
        settings = settings_for(kind, effort, think)
        done = (_edit_playground if kind == "playground" else _edit_code_lab)(client, usage, jev_key, view, si, old, what, settings, res)
        if isinstance(done, str):  # why it failed
            return _kept(old, done, label)
        return {"block": done["block"], "changed": True, "facts": done["facts"], "plan": done["plan"], "notes": res["notes"]}
    except Exception:
        log.exception("Follow-up widget edit failed (%s)", label)
        return _kept(old, "something went wrong while changing it", label)


def _edit_code_lab(client: anthropic.Anthropic, usage: UsageLog, jev_key: str | None, view: dict, si: int, old: dict, what: str,
                   st: cl.CodeLabSettings, res: dict) -> dict | str:
    """Code Lab 7.0: update the plan, rebuild the widget from its code and the request, then the usual checks."""
    label = view["stages"][si].get("label", "")
    section = cl._section(view, si)
    why: list[str] = []
    plan = cl._plan(client, usage, {**section, "currentPlan": read_plan(old), "request": what}, "", why, st, edit=True)
    if plan is None:
        return f"the plan couldn't be updated ({why[0] if why else 'no answer'})"
    if plan.get("build") is not True:
        return "the section doesn't support that change"
    payload = {**section, "plan": {k: v for k, v in plan.items() if k != "vegaLite"}}
    edit = {"yourPreviousHtml": old.get("html", ""), "request": what}
    built = cl._ask(client, usage, "code lab edit", payload, why=why, effort=st.build, think=st.think, free=True, edit=edit)
    lower = cl._lower(st.build)
    if built is None and why and cl.CUTOFF in why[-1] and lower:  # the whole budget went on thinking: once more, thinking less
        why.clear()
        built = cl._ask(client, usage, "code lab edit", payload, why=why, effort=lower, think=st.think, free=True, edit=edit)
    if built is None:
        return f"the change couldn't be written ({why[0] if why else 'no answer'})"
    if built.get("build") is not True:
        return f"the change couldn't be built ({str(built.get('why') or '')[:120]})"
    reply, html, problems, report = cl._until_clean(client, usage, payload, built, plan, res, effort=st.fix, think=st.think, free=True)
    if reply is None:
        return f"the changed widget failed its checks ({problems[0]})"
    rv = cl.review(jev_key, view, si, plan, report.get("facts"), usage, fix_first=st.fix_first)
    scores = rv["p"] if rv else None
    if rv and rv["reject"]:  # one fix on a clear no, as for a new widget
        res["repaired"] += 1
        fixed = cl._ask(client, usage, "code lab repair", payload, {"yourPreviousHtml": html, "problems": rv["problems"]}, effort=st.fix, think=st.think, free=True)
        if fixed is None or fixed.get("build") is not True:
            return f"the reviewer rejected the changed widget ({rv['problems'][0]})"
        reply, html, problems, report = cl._until_clean(client, usage, payload, {**reply, **fixed}, plan, res, effort=st.fix, think=st.think, free=True)
        if reply is None:
            return f"the reviewer's fix failed its checks ({problems[0]})"
        again = cl.review(jev_key, view, si, plan, report.get("facts"), usage, full=False)
        if again is not None and again["reject"]:
            return f"the reviewer rejected the changed widget ({rv['problems'][0]})"
        scores = again["p"] if again else scores
    if st.look:
        reply, html = cl.look_check(client, usage, payload, reply, plan, html, res, st, label)
    cl._accept(res, reply, plan, html, scores)
    block = {**res["widget"], "title": old.get("title") or res["widget"]["title"]}
    return {"block": block, "facts": report.get("facts"), "plan": plan}


def _edit_playground(client: anthropic.Anthropic, usage: UsageLog, jev_key: str | None, view: dict, si: int, old: dict, what: str,
                     st: cl.CodeLabSettings, res: dict) -> dict | str:
    """Playground: the spec back out of the block, changed by Haiku, then the checker and repairs, and Jev's review."""
    spec = read_playground_spec(old.get("html", ""))
    if spec is None:
        return "its playground couldn't be read back"
    why: list[str] = []
    payload = {**cl._section(view, si), "hint": ""}
    new = cl._pg_ask(client, usage, "playground edit", payload, st, why, edit={"yourPreviousSpec": spec, "request": what})
    lower = cl._lower(st.build)
    if new is None and why and cl.CUTOFF in why[-1] and lower:
        why.clear()
        new = cl._pg_ask(client, usage, "playground edit", payload, st, why, effort=lower, edit={"yourPreviousSpec": spec, "request": what})
    if new is None:
        return f"the change couldn't be written ({why[0] if why else 'no answer'})"
    if cl._no_playground(new):
        return "the section doesn't support that change"
    new, report = cl._pg_until_clean(client, usage, payload, new, res, st, why)
    if new is None:
        return f"the changed playground failed its checks ({(report['problems'] or ['no answer'])[0]})"
    plan = cl.playground_plan(new)
    rv = cl.review(jev_key, view, si, plan, report.get("facts"), usage, fix_first=True, playground=True)
    scores = rv["p"] if rv else None
    if rv and rv["reject"]:
        res["repaired"] += 1
        fixed = cl._pg_ask(client, usage, "playground repair", payload, st, why, {"yourPreviousSpec": new, "problems": rv["problems"]}, effort=st.fix)
        if fixed is None or cl._no_playground(fixed):
            return f"the reviewer rejected the changed playground ({rv['problems'][0]})"
        new, report = cl._pg_until_clean(client, usage, payload, fixed, res, st, why)
        if new is None:
            return f"the reviewer's fix failed its checks ({(report['problems'] or ['no answer'])[0]})"
        plan = cl.playground_plan(new)
        again = cl.review(jev_key, view, si, plan, report.get("facts"), usage, full=False, playground=True)
        if again is not None and again["reject"]:
            return f"the reviewer rejected the changed playground ({rv['problems'][0]})"
        scores = again["p"] if again else scores
    cl._pg_accept(res, new, plan, scores)
    block = {**res["widget"], "title": old.get("title") or res["widget"]["title"]}
    return {"block": block, "facts": report.get("facts"), "plan": plan}


# ---------- adding a widget to a section that has none ----------


def add_widget(client: anthropic.Anthropic, usage: UsageLog, jev_key: str | None, outline: dict, si: int, what: str, kind: str | None,
               effort: str = "medium", think: bool = True) -> dict:
    """{"outline", "built", "facts", "plan", "notes"}: the section given a new widget, built like any other (plan, build, checks,
    Jev's review, look check) but with no gate: the reader asked for it. Refused, with a note, at MAX_MARKED widgets or when
    the page has no kind of widget to build."""
    label = outline["stages"][si].get("label", "")
    no = lambda why: {"outline": outline, "built": False, "facts": None, "plan": None,  # noqa: E731
                      "notes": [f"Couldn't add an interactive part to \"{label}\": {why}."]}
    if len(widgets_in(outline)) >= cl.MAX_MARKED:
        return no(CAP_NOTE.rstrip("."))
    if kind is None:
        return no("this page type can't build one")
    try:
        view = viewed(outline)
        res = cl._design_one(client, usage, view, si, {"open": True, "hint": what}, jev_key, settings_for(kind, effort, think))
    except Exception:
        log.exception("Follow-up widget add failed (%s)", label)
        return no("something went wrong while building it")
    if not res.get("widget"):
        why = (res["notes"] or ["the builder found nothing to show here"])[0]
        return no(why[0].lower() + why[1:].rstrip("."))
    placed = cl._place(outline, si, res["widget"], res.get("after"))
    return {"outline": placed, "built": True, "facts": None, "plan": read_plan(res["widget"]), "notes": res["notes"]}


# ---------- does the section's text still describe its widget? ----------

AGREE_QUESTIONS: dict[str, dict] = {
    "controls_in_text": cl._noul(
        "The quantities that `widget.controls` set are ones `section.text` names or describes.",
        "Each control in `widget.controls` (by its label) sets a quantity, input or parameter that `section.text` names or describes.",
        "A control in `widget.controls` sets something `section.text` never mentions."),
    "text_not_contradicted": cl._noul(
        "Nothing `section.text` says about the widget's quantities contradicts the widget.",
        "What `section.text` says is fixed, adjustable, increasing or decreasing, or has a given value, agrees with `widget.controls`, `widget.changes` and `plan.lesson`.",
        "`section.text` says a quantity is fixed that `widget.controls` lets the reader change, or says something about the quantities that `widget.changes` shows is not true."),
    "lesson_in_text": cl._noul(
        "`section.text` explains or sets up the idea in `plan.lesson`.",
        "`section.text` discusses the idea `plan.lesson` says the reader should see.",
        "`section.text` never discusses the idea `plan.lesson` is about."),
}
AGREE_PROBLEMS = {
    "controls_in_text": "the text doesn't mention a quantity the interactive part lets the reader change",
    "text_not_contradicted": "the text says something about the quantities that the interactive part now contradicts (for example a quantity the text calls fixed is adjustable)",
    "lesson_in_text": "the text doesn't explain what the interactive part now teaches",
}


def agrees(jev_key: str | None, outline: dict, si: int, plan: dict | None, facts: dict | None, usage: UsageLog) -> str | None:
    """None when the section's text and its widget still match, or when that can't be told (no key, no plan or facts, Jev
    didn't answer); otherwise one sentence saying what no longer matches. Only a clear no counts (AGREE_LOW)."""
    if not jev_key or not plan or not facts:
        return None
    try:
        r = jev_ask(jev_key, cl.review_state(outline, si, plan, facts), AGREE_QUESTIONS)
    except JevError:
        return None
    usage.add("jev follow-up agree", r["model"], {"input_tokens": r["usage"].get("input_tokens", 0), "output_tokens": 0})
    p = {k: float(r["answers"][k]["p"]) for k in AGREE_QUESTIONS if k in r["answers"]}
    failed = [k for k in AGREE_QUESTIONS if p.get(k, 1) <= AGREE_LOW]
    log.info("Follow-up text/widget agreement (%s): %s", outline["stages"][si].get("label", ""), cl.scores_text(p))
    return "; ".join(AGREE_PROBLEMS[k] for k in failed) or None


def facts_of(block: dict) -> dict | None:
    """What the test run saw in a finished widget (controls, labels, what changes), for agrees() when the widget wasn't just
    rebuilt. None if it can't be run."""
    try:
        kind = widget_kind(block)
        if kind == "playground":
            spec = read_playground_spec(block.get("html", ""))
            return cl.check_playground(spec)["facts"] if spec else None
        if kind == "code-lab":
            return cl.inspect(cl.clean_html(block.get("html", "")), None).get("facts")
    except Exception:
        log.exception("Follow-up: couldn't run a widget to read its facts")
    return None


def widget_summary(block: dict) -> str:
    """One line for the target finder's digest: the widget's title and what it teaches."""
    lesson = re.sub(r"\s+", " ", str(read_plan(block).get("lesson") or "")).strip()[:200]
    return f'widget "{block.get("title", "")}"' + (f": {lesson}" if lesson else "")
