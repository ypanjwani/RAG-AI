"""Add a section (Quality+): a follow-up that only adds to the explainer above, never changes what is there.

  place     -> one Jev request: where does the new section go (a choice over the gaps between stages), and does
               an existing stage already cover it (a noul, plus a choice of the closest stage)?
  write     -> Haiku writes only the new stage(s), told which stage comes before and after (addition_request)
  merge     -> code inserts them at Jev's position (parse_addition); every existing stage, the page fields and
               the existing glossary entries are kept exactly as they were

The rest is the revision path (revise.py): the design is kept, and the later steps run on the new stages only.
A FollowUp of kind "revise" with add: true asks for this (followup.parse_follow_up).
"""

from __future__ import annotations

import logging
from typing import Any

from .core.spec import parse_outline
from .jev import jev_ask
from .llm import warning_reason
from .js import js_slice, js_str, js_round
from .pricing import UsageLog
from .revise import PAGE_FIELDS, _id_of, merge_revision

log = logging.getLogger(__name__)

# First guesses, to be tuned on real additions (spec["addition"] records what Jev answered).
PLACE_MIN_P = 0.35  # Jev's top position is used only with at least this probability; otherwise the section goes at the end
COVERED_AT = 0.7  # "already covered" yes at or above this: Haiku is told to write only what that stage doesn't cover
MAX_NEW_STAGES = 2
STAGE_CHARS = 300  # each stage's text shown to Jev


def _stage_text(stage: dict) -> str:
    parts = [stage.get("blurb") or ""]
    for b in stage.get("blocks") or []:
        if isinstance(b, dict) and b.get("type") in ("text", "callout") and isinstance(b.get("md"), str):
            parts.append(b["md"])
    return js_slice(" ".join(p for p in parts if p), 0, STAGE_CHARS)


def _label(stage: dict) -> str:
    return stage.get("label") or stage.get("id") or "?"


def place_state(old: dict, text: str) -> dict:
    return {
        "explainer": old.get("title") or "",
        "reader_request": text,
        "stages": [{"number": i + 1, "label": _label(s), "text": _stage_text(s)} for i, s in enumerate(old["stages"])],
    }


def place_questions(old: dict) -> dict[str, dict]:
    stages = old["stages"]
    n = len(stages)
    gaps = {"start": f'At the very start, before stage 1 "{_label(stages[0])}"'}
    for i, s in enumerate(stages):
        after = f'Right after stage {i + 1} "{_label(s)}"'
        gaps[f"after_{i + 1}"] = f'{after}, before stage {i + 2} "{_label(stages[i + 1])}"' if i + 1 < n else f"{after}, at the end"
    return {
        "position": {"type": "choice", "criteria": gaps, "instructions": (
            "A new section that answers reader_request will be added to this explainer without changing the existing "
            "stages. Where should it go so the explainer still reads in order: after the stages that teach what it "
            "builds on, and before the stages that would benefit from it?")},
        "covered": {"type": "noul", "instructions": "Does one existing stage already explain what reader_request asks for, so a new section would mostly repeat it?",
                    "criteria": {"true": "an existing stage already covers the request", "false": "the request asks for something the stages don't cover yet"}},
        "closest": {"type": "choice", "instructions": "Which existing stage is closest to what reader_request asks for?",
                    "criteria": {f"stage_{i + 1}": f'Stage {i + 1} "{_label(s)}"' for i, s in enumerate(stages)}},
    }


def decide_place(answers: dict[str, dict], old: dict) -> dict:
    """{"after": stage id | "start" | None (the end), "label", "p", "placed": "jev" | "end", "covered"?: stage id}."""
    stages = old["stages"]
    end = {"after": stages[-1]["id"], "label": f'after "{_label(stages[-1])}" (the end)', "p": 0, "placed": "end"}
    a = answers.get("position")
    out = dict(end)
    if a and a["ranking"]:
        top = a["ranking"][0]
        p = top["p"]
        if p >= PLACE_MIN_P:
            if top["option"] == "start":
                out = {"after": "start", "label": "at the start", "p": p, "placed": "jev"}
            else:
                i = int(top["option"].split("_")[1]) - 1
                out = {"after": stages[i]["id"], "label": f'after "{_label(stages[i])}"', "p": p, "placed": "jev"}
        else:
            out["p"] = p
    c, near = answers.get("covered"), answers.get("closest")
    if c and c["p"] >= COVERED_AT and near and near["ranking"]:
        out["covered"] = stages[int(near["ranking"][0]["option"].split("_")[1]) - 1]["id"]
    return out


def place_section(jev_key: str | None, old: dict, text: str, usage: UsageLog, warnings: list[str]) -> dict:
    """Never raises: without a key or on failure the section goes at the end and a warning says why."""
    if not jev_key:
        warnings.append("Add section: no Jev key, so the new section was put at the end.")
        return decide_place({}, old)
    try:
        r = jev_ask(jev_key, place_state(old, text), place_questions(old))
        usage.add("jev place", r["model"], r["usage"])
        place = decide_place(r["answers"], old)
    except Exception as e:
        log.exception("jev placement failed")
        warnings.append(f"Add section: {warning_reason(e)}, so the new section was put at the end.")
        return decide_place({}, old)
    if place["placed"] == "end":
        warnings.append(f"Add section: Jev wasn't sure where the new section fits (top choice {js_str(js_round(place['p'] * 100))}%), so it was put at the end.")
    return place


def place_detail(place: dict) -> str:
    """The plan step's detail while the section is written, e.g. 'Jev: after "Discrete distributions" (62%)'."""
    return f"Jev: {place['label']} ({js_str(js_round(place['p'] * 100))}%)" if place["placed"] == "jev" else f"Placing it {place['label']}"


ADD_RULES = "\n".join([
    "Rules for the addition:",
    '- Reply with one JSON object: {"changes": {"add": [{"stage": {a complete new stage in the usual format, with a new id}}], "glossary": [{"term": "...", "definition": "..."}]}}. "glossary" is optional and holds only new terms.',
    f"- Write one new stage; write {MAX_NEW_STAGES} only if the request clearly has two parts. They are placed together, in the order you list them.",
    "- Never repeat, rewrite or remove an existing stage, and don't change the title, subtitle, key intuition or level: they are kept exactly as they are.",
    "- The new stage may read the existing controls listed below by their variable names, but any control it adds needs a new variable name. It must use one of the listed phase ids.",
    "- Write at the explainer's current level and in the same notation and terms as the neighbouring stages shown below. Refer back to the stage before it where that helps, and don't re-teach what the earlier stages already explain.",
])

NEIGHBOUR_CHARS = 3000  # each neighbouring stage's prose shown in full, up to this
SOURCE_CHARS = 8000  # a paper's source excerpt, cut to this (the full page used to carry up to followUpChars)


def _prose(stage: dict) -> str:
    """A stage's own words: text, callouts, formulas and cards (what the new stage should read on from)."""
    lines = []
    for b in stage.get("blocks") or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("text", "callout") and isinstance(b.get("md"), str):
            lines.append(b["md"])
        elif t == "formula" and isinstance(b.get("latex"), str):
            lines.append(f"Formula: {b['latex']}" + (f" ({b['caption']})" if b.get("caption") else ""))
        elif t == "cards":
            lines.extend(f"{it.get('title', '')}: {it.get('md', '')}" for it in b.get("items") or [] if isinstance(it, dict))
    return js_slice("\n".join(lines), 0, NEIGHBOUR_CHARS)


def _controls(stages: list[dict]) -> list[str]:
    """Every slider, choice and toggle on the page: their variables are shared by the whole explainer."""
    out = []
    for n, s in enumerate(stages, 1):
        for b in s.get("blocks") or []:
            if not isinstance(b, dict) or b.get("type") not in ("slider", "choice", "toggle") or not isinstance(b.get("var"), str):
                continue
            t = b["type"]
            if t == "slider":
                what = f"{js_str(b.get('min'))} to {js_str(b.get('max'))}, starts at {js_str(b.get('value'))}"
            elif t == "choice":
                what = "values " + ", ".join(f"{js_str(o.get('value'))} = {o.get('label', '')}" for o in b.get("options") or [] if isinstance(o, dict))
            else:
                what = f"1 = {b.get('on', 'on')}, 0 = {b.get('off', 'off')}"
            out.append(f'- {b["var"]}: {t} "{b.get("label", "")}", {what} (stage {n})')
    return out


def slim_page(old: dict, place: dict) -> str:
    """What Haiku needs to write one new stage, instead of the whole outline JSON: every stage's label and blurb with
    the insertion point marked, the neighbouring stages' prose, the shared controls and the glossary terms. Blocks of
    the other stages (quizzes, calculators, plots, worked examples) are left out, so the cost doesn't grow with the page."""
    stages = old["stages"]
    at = 0 if place["after"] == "start" else [s["id"] for s in stages].index(place["after"]) + 1
    phases = ", ".join(f'{p.get("id")} ("{p.get("label", "")}")' for p in old.get("phases") or [] if isinstance(p, dict))
    marker = "   >>> THE NEW SECTION GOES HERE <<<"
    lines = []
    for i, s in enumerate(stages):
        if i == at:
            lines.append(marker)
        lines.append(f"{i + 1}. [{s.get('phase', '')}] {_label(s)}: {s.get('blurb') or ''}")
    if at == len(stages):
        lines.append(marker)
    parts = [
        f'Explainer: "{old.get("title", "")}" (level: {old.get("level", "")})',
        f"Phase ids: {phases}",
        "Stages, in order:\n" + "\n".join(lines),
    ]
    for label, i in (("The stage just before the new section", at - 1), ("The stage just after it", at)):
        if 0 <= i < len(stages):
            parts.append(f'{label}, "{_label(stages[i])}" (its text and formulas):\n{_prose(stages[i]) or "(no text)"}')
    controls = _controls(stages)
    parts.append("Existing controls (variable: kind, range):\n" + "\n".join(controls) if controls else "Existing controls: none.")
    terms = [g["term"] for g in old.get("glossary") or [] if isinstance(g, dict) and isinstance(g.get("term"), str)]
    if terms:
        parts.append("Glossary terms already defined: " + ", ".join(terms))
    return "\n\n".join(parts)


def addition_request(text: str, f: dict, old: dict, place: dict) -> str:
    """The planner's user turn for an addition (the cached system prompt is unchanged). It carries slim_page, not the
    outline JSON (the server still merges into the full outline the browser sent)."""
    stages = old["stages"]
    ids = [s["id"] for s in stages]
    if place["after"] == "start":
        where = f'It goes at the very start, before the stage "{_label(stages[0])}".'
    else:
        i = ids.index(place["after"])
        where = f'It goes right after the stage "{_label(stages[i])}"' + (f', before the stage "{_label(stages[i + 1])}".' if i + 1 < len(stages) else ", at the end.")
    covered = ""
    if place.get("covered"):
        cs = stages[ids.index(place["covered"])]
        covered = f'The stage "{_label(cs)}" may already cover part of this: write only what it doesn\'t, and build on it.'
    # A typed concept's "source" is only "Reader's request: …"; a paper's summaries are cut to SOURCE_CHARS.
    source = f.get("source") or ""
    if source.startswith("Reader's request:"):
        source = ""
    return "\n\n".join(p for p in [
        "Add a new section to an existing interactive explainer for the reader's follow-up request.",
        f"Reader's request: {text}",
        where,
        covered,
        ADD_RULES,
        f"The explainer so far (a summary; the existing stages are kept as they are):\n{slim_page(old, place)}",
        f"Source material it was built from (stay accurate to it):\n{js_slice(source, 0, SOURCE_CHARS)}" if source else "",
    ] if p)


def parse_addition(raw: Any, old: dict, after: str) -> dict:
    """The parse step for an addition (pipeline.write_outline): the new stages inserted after `after`, nothing else
    changed. {"outline", "warnings", "revision"} or {"error"} (write_outline then asks again)."""
    changes = raw.get("changes") if isinstance(raw, dict) else None
    if not isinstance(changes, dict):
        return {"error": 'the reply must be {"changes": {"add": [...]}} with only the new section'}
    warnings: list[str] = []
    ignored = [k for k in ("replace", "remove", *PAGE_FIELDS) if changes.get(k)]
    if ignored:
        warnings.append(f"Add section: the reply also tried to change {', '.join(ignored)}; only the new section was kept.")
    new = [it["stage"] for it in changes.get("add") or [] if isinstance(it, dict) and isinstance(it.get("stage"), dict)] if isinstance(changes.get("add"), list) else []
    if not new:
        return {"error": "the reply has no new stage"}
    if len(new) > MAX_NEW_STAGES:
        warnings.append(f"Add section: {len(new)} stages were written; only the first {MAX_NEW_STAGES} were kept.")
        new = new[:MAX_NEW_STAGES]
    # Ids are fixed here, so each new stage can be placed after the one before it.
    taken = {s.get("id") for s in old["stages"]}
    add, prev = [], after
    for st in new:
        sid = _id_of(st, taken, f"stage-{len(taken) + 1}")
        taken.add(sid)
        add.append({"after": prev, "stage": {**st, "id": sid}})
        prev = sid
    # New glossary terms only: an existing term keeps its definition.
    have = {str(g.get("term", "")).lower() for g in old.get("glossary", []) if isinstance(g, dict)}
    glossary = [g for g in changes.get("glossary") or [] if isinstance(g, dict) and str(g.get("term", "")).lower() not in have] if isinstance(changes.get("glossary"), list) else []
    m = merge_revision(old, {"add": add, "glossary": glossary})
    if "error" in m:
        return m
    p = parse_outline(m["outline"])
    if "error" in p:
        return p
    kept_ids = {s["id"] for s in p["outline"]["stages"]}
    changed = [i for i in m["changed"] if i in kept_ids]
    if not changed:
        return {"error": "the new stage had nothing usable"}
    return {"outline": p["outline"], "warnings": [*warnings, *m["warnings"], *p["warnings"]], "revision": {"changed": changed, "removed": 0}}
