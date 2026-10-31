"""Feedback v2, the last step: is each changed part better than before?

After a feedback change (pipeline.py, FollowUp.feedback) has been applied by targets.apply_targets, Jev compares every changed
section with the previous version against what the reader asked for:
  fixes      the change deals with the reader's complaint
  not_worse  apart from the requested change, nothing got less clear or less correct
Only a clear no counts (at or below LOW). Jev sees each section's text and, for an interactive widget, its real settings
(the playground spec, or the start of a code widget's html), so a change to how a widget looks or what it shows can be seen.
A failing section is changed once more from the previous version, with the reason added to the request, and checked again.
If it still fails:
  - text: the previous section is put back with a note;
  - a widget: the previous one is put back only when the change made it worse (not_worse). When Jev only doubts that it fixes
    the complaint, the change the reader asked for is kept, with a note asking them to say if it still looks wrong.
A new section the reader asked for is never removed (it only gets a note). No Jev key, or Jev failing, keeps the change with a
note: the check never blocks a page.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import anthropic

from . import codelab as cl
from . import codelab_edit as ce
from .jev import JevError, jev_ask
from .labplan import section_text
from .pricing import UsageLog
from .revise import restore_kept_widgets

log = logging.getLogger("explainer.feedback")

LOW = 0.2  # at or below: a clear no (like codelab_edit.AGREE_LOW)
SECTION_CHARS = 4000
WIDGET_CHARS = 5000  # a widget's settings for Jev: the playground spec (or a code widget's html), cut to this

QUESTIONS: dict[str, dict] = {
    "fixes": cl._noul(
        "`after` makes the change in `issue.change`, so the complaint in `issue.reader_said` no longer applies to it.",
        "Compared with `before`, `after` makes the change that `issue.change` describes, and what the reader complained about in `issue.reader_said` is dealt with in `after`. For an interactive part, compare its `settings` (its charts' draw code, options, sizes, colours and controls) in `before` and `after`.",
        "`after` does not make the change in `issue.change`, or the complaint in `issue.reader_said` applies to `after` as much as to `before`."),
    "not_worse": cl._noul(
        "Apart from the change in `issue.change`, `after` is as clear, complete and correct as `before`.",
        "Everything in `after` that `issue.change` didn't ask to change is as clear, complete and correct as in `before`: no step of the explanation, formula, correct statement or interactive part was lost or broken.",
        "`after` lost or broke something `issue.change` didn't ask to change: a step of the explanation, a formula, a correct statement, or an interactive part."),
}
PROBLEMS = {
    "fixes": "the change didn't deal with what you asked for",
    "not_worse": "the change made other parts of the section less clear or less correct",
}

Ask = Callable[[str, Callable[[Any], dict]], dict]


def widget_settings(b: dict) -> str:
    """The widget as it is built: its playground spec (components with their chart draw code, and state) as JSON, or the
    start of a code widget's html. This is where colours, sizes, shown values and controls live."""
    spec = ce.read_playground_spec(b.get("html", "")) if ce.widget_kind(b) == "playground" else None
    if spec:
        return json.dumps({k: spec[k] for k in ("state", "components") if k in spec}, ensure_ascii=False)[:WIDGET_CHARS]
    return str(b.get("html") or "")[:WIDGET_CHARS]


def stage_view(st: dict | None) -> dict:
    """What Jev reads about one version of a section: its text, and each interactive widget's purpose and real settings."""
    if st is None:
        return {"label": "", "text": "(this section is new)", "interactive": []}
    widgets = []
    for b in st["blocks"]:
        if b.get("type") == "widget":
            plan = ce.read_plan(b)
            widgets.append({"title": str(b.get("title") or ""), "lesson": str(plan.get("lesson") or ""),
                            "readerDoes": str(plan.get("readerDoes") or ""), "settings": widget_settings(b)})
    return {"label": str(st.get("label") or ""), "text": section_text(st)[:SECTION_CHARS], "interactive": widgets}


def why_of(failed: list[str]) -> str:
    return "; ".join(PROBLEMS[k] for k in failed)


def judge(jev_key: str, before: dict | None, after: dict, said: str, change: str, usage: UsageLog) -> list[str]:
    """The questions the change fails ([] = it passes): "fixes" and/or "not_worse". Raises JevError when Jev can't answer."""
    state = {"issue": {"reader_said": said, "change": change}, "before": stage_view(before), "after": stage_view(after)}
    r = jev_ask(jev_key, state, QUESTIONS)
    usage.add("jev feedback check", r["model"], {"input_tokens": r["usage"].get("input_tokens", 0), "output_tokens": 0})
    p = {k: float(r["answers"][k]["p"]) for k in QUESTIONS if k in r["answers"]}
    log.info("Feedback check (%s): %s", after.get("label", ""), cl.scores_text(p))
    return [k for k in QUESTIONS if p.get(k, 1) <= LOW]


def _stage(outline: dict, sid: str) -> dict | None:
    return next((st for st in outline["stages"] if st["id"] == sid), None)


def _put(outline: dict, sid: str, st: dict) -> dict:
    return {**outline, "stages": [st if s["id"] == sid else s for s in outline["stages"]]}


def check(client: anthropic.Anthropic, usage: UsageLog, jev_key: str | None, follow: dict, base: dict, result: dict,
          targets: list[dict], said: str, ask: Ask, apply: Callable[..., dict], feedback_id: str = "") -> dict:
    """result (targets.apply_targets' {"outline", "warnings", "revision"}) with each changed section checked: kept, changed
    once more, or put back as it was. apply: apply_targets with the pipeline's settings bound (targets, then the rest)."""
    outline, warnings = result["outline"], list(result["warnings"])
    revision = {**result["revision"], "changed": list(result["revision"]["changed"]), "widgets": list(result["revision"].get("widgets") or [])}
    changed = list(dict.fromkeys([*revision["changed"], *revision["widgets"]]))
    summary: dict[str, str] = {}
    if not changed:
        log.info("feedback %s: nothing changed", feedback_id)
        return result
    if not jev_key:
        warnings.append("The feedback change couldn't be checked, so it was kept as it is.")
        log.info("feedback %s: changed=%s verify=unchecked (no Jev key)", feedback_id, changed)
        return {**result, "warnings": warnings}
    previous = restore_kept_widgets(base, follow["widgets"]) if follow.get("widgets") else base
    new_whats = "; ".join(t["what"] for t in targets if t["part"] == "new-section")
    unchecked = False
    for sid in changed:
        before, after = _stage(previous, sid), _stage(outline, sid)
        if after is None:
            continue
        mine = [t for t in targets if t["stage"] == sid]
        change = "; ".join(t["what"] for t in mine) or new_whats
        try:
            failed = judge(jev_key, before, after, said, change, usage)
        except JevError:
            unchecked, summary[sid] = True, "unchecked"
            continue
        if not failed:
            summary[sid] = "pass"
            continue
        why = why_of(failed)
        label = str(after.get("label") or sid)
        if before is None or not mine:  # a new section the reader asked for stays, with a note
            warnings.append(f'"{label}" may not be quite what you asked for: {why}.')
            summary[sid] = "kept-new"
            continue
        # One more try, from the previous version, with the reason.
        retry = [{**t, "what": f"{t['what']} (A first attempt didn't work: {why}.)"} for t in mine]
        failed2 = failed
        try:
            again = apply(retry)
            st2 = _stage(again["outline"], sid)
            failed2 = judge(jev_key, before, st2, said, change, usage) if st2 is not None else failed
        except JevError:
            st2, failed2 = None, failed
        except Exception:
            log.exception("Feedback check: the second try at %s failed", sid)
            st2, failed2 = None, failed
        if not failed2 and st2 is not None:
            outline = _put(outline, sid, st2)
            summary[sid] = "pass-after-retry"
            continue
        # A widget the reader asked to change is kept unless it got worse: Jev only doubts that it fixes the complaint.
        if sid in revision["widgets"] and ("not_worse" not in failed2 or "not_worse" not in failed):
            keep = st2 if st2 is not None and "not_worse" not in failed2 else after
            doubt = why_of([k for k in failed2 if k != "not_worse"]) or why_of([k for k in failed if k != "not_worse"])
            outline = _put(outline, sid, keep)
            warnings.append(f'Changed "{label}" as you asked, but it may not fully fix it ({doubt}). '
                            "Send feedback again if it still looks wrong.")
            summary[sid] = "kept-unsure"
            continue
        why2 = why_of(failed2)
        outline = _put(outline, sid, before)
        revision["changed"] = [i for i in revision["changed"] if i != sid]
        revision["widgets"] = [i for i in revision["widgets"] if i != sid]
        warnings.append(f'Kept the previous "{label}": the change didn\'t work out ({why2}).')
        summary[sid] = "reverted"
    if unchecked:
        warnings.append("Some feedback changes couldn't be checked, so they were kept as they are.")
    log.info("feedback %s: changed=%s verify=%s", feedback_id, changed, summary)
    return {"outline": outline, "warnings": warnings, "revision": revision}
