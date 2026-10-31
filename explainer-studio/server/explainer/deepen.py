"""Quality 2.0 (quality-deep; the composer's "Review rounds" setting): generate, then verify -> audit -> improve
rounds until an audit finds nothing real, or the round limit: the request's reviewRounds (1-ROUNDS_CEILING, the reader's
choice; "Until clean" sends the ceiling), or MAX_ROUNDS when it has none (old records, scripts).
Interactive, Creative, Lab and Code Lab 4.0 can have review rounds (not Quick read or the other Code Labs): quality-deep
is Quality+ with rounds, and freeform-plus / scene-plus / code-lab-7 / playground requests with reviewRounds come here too (Lab's
scenes and Code Lab 4.0's charts are kept like Creative's widgets). The first page is written in the request's own mode;
improves are always Quality+ revisions as changes (the only revision format that keeps unnamed stages). Freeform+
widgets, which an improve sees only as "(interactive widget: title)" placeholders, are then put back in place
(restore_widgets).

  generate -> pipeline.generate as Quality+ with checks=False (no inline clarity or fact check; verifying is this
              module's job). Version 1.
  round    -> 1. verify (verify.py): every check reads the page and lists findings; nothing changes
              2. audit: once verification is complete, one Haiku call decides for every finding whether it is real
                 and what to do (modify, reword, adjust_graph, add_section, remove_section, none). No real item ends
                 the loop ("clean").
              3. improve: a revision as changes (pipeline.generate with followUp.improve, checks=False) applying
                 only the audit's real items. Code guards it: stages the audit didn't name are put back, an unasked
                 removal or a full rewrite keeps the previous version, at most MAX_ADD_ROUND new stages.
              The next round verifies closely only the stages this one changed.
              Round 1 looks at everything (coverage and clarity too). Rounds 2+ look only for errors: wrong facts,
              mismatches, wrong formulas or calculations, broken, mismatched or weak charts and controls (no clarity
              or coverage check, no new sections). A finding an audit already dismissed isn't sent again.
  finish   -> the stages the last improve changed haven't been verified, so the usual clarity and fact checks
              (the fixing ones) run on them; then section notes, once.

Caching: the audit and the improve plan send the same tools, system prompt (the planner's) and first user block
(revise.page_block, the page), so the improve plan reads the page from the cache the audit wrote. Every version is
returned in "versions" with its findings and audit (sent to the browser only with debugVersions; the app shows only
the final explainer).
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from . import data
from .clarity import clarity_check
from .factcheck import fact_check
from .followup import parse_follow_up
from .js import js_len, js_slice, js_trim, json_stringify
from .json_util import extract_json
from .llm import HAIKU, cached_system, check_stop, text_of, warning_reason
from .notes import note_fields, write_notes
from .pipeline import Emit, _step, generate, planner_system
from .pricing import UsageLog
from .revise import page_block
from .verify import BUILD_SKIP, source_of, verify

log = logging.getLogger(__name__)

# Rounds repeat until an audit finds nothing real to fix, at most the request's reviewRounds. MAX_ROUNDS is the limit
# for requests without one; ROUNDS_CEILING caps any request, so a page whose audits keep finding new problems can't
# loop (and spend) forever.
MAX_ROUNDS = 3  # in a 6-topic test most of the value came in rounds 1-2; later rounds added one extra each
ROUNDS_CEILING = 10
MAX_ADD_ROUND = 2  # new stages one improve may add
MAX_STAGES_DEEP = 12
NOTE_EXTRAS = ("depth", "rephrase")
ACTIONS = ("modify", "reword", "adjust_graph", "add_section", "remove_section")
INNER_STEP = {"plan": "rewriting", "design": "rewriting", "build": "rebuilding", "examples": "writing worked examples"}


# ---------- the follow-up a revision needs (ports of shared/followup.ts) ----------


def compact_outline(spec: dict) -> dict:
    """compactOutline: the spec as the planner's outline, without design, chatbot, warnings, icons or example steps."""
    def block(b: dict) -> dict:
        if b.get("type") in BUILT_BLOCKS:
            return {"type": "text", "md": placeholder(b)}
        if b.get("type") == "examples":
            return {**b, "items": [{k: v for k, v in it.items() if k != "steps"} for it in b.get("items") or []]}
        return b
    out = {k: spec.get(k) for k in ("title", "subtitle", "level", "layout", "keyIntuition", "phases")}
    out["stages"] = [{"id": s["id"], "label": s["label"], "phase": s.get("phase"), "blurb": s.get("blurb"), "blocks": [block(b) for b in s["blocks"]]}
                     for s in spec["stages"]]
    out["glossary"] = spec.get("glossary")
    return {k: v for k, v in out.items() if v is not None}


def design_of(spec: dict) -> dict:
    """designOf: the page's design, so a revision keeps its look and skips the design call."""
    d = spec["design"]
    names = {emoji: name for name, emoji in data.menu("ICONS").items()}
    out: dict = {"design": {
        "theme": "ocean" if d["theme"] == "custom" else d["theme"],
        "fontPairing": d["fontPairing"],
        "headingStyle": d["headingStyle"],
        "stageIcons": [{"stageId": s["id"], "icon": names[s["icon"]]} for s in spec["stages"] if s.get("icon") in names],
        "chatbot": {"systemPrompt": spec["chatbot"]["systemPrompt"], "starters": spec["chatbot"]["starters"]},
    }}
    if d["theme"] == "custom" and d.get("accent"):
        out["accent"] = d["accent"]
    if d.get("mode"):
        out["colorMode"] = d["mode"]
    return out


def kept_of(spec: dict) -> dict | None:
    """keptOf: worked-example steps already written ("<stage id>:<block>:<item>"), so kept stages don't pay for them
    again. Section notes are written only at the end, so there are none to keep."""
    steps = {f"{st['id']}:{bi}:{ii}": it["steps"]
             for st in spec["stages"] for bi, b in enumerate(st["blocks"]) if b.get("type") == "examples"
             for ii, it in enumerate(b.get("items") or []) if it.get("steps")}
    if not steps or js_len(json_stringify({"steps": steps})) > data.limits()["keptChars"]:
        return None
    return {"steps": steps}


def follow_up_of(spec: dict) -> dict | str:
    """The revise follow-up for this page (buildFollowUp's revise branch), validated like a browser's. A string
    when it can't be built (the page is too large)."""
    outline = json_stringify(compact_outline(spec))
    limit = data.limits()["followUpChars"]
    if js_len(outline) > limit:
        return "the page is too large to revise"
    room = limit - js_len(outline)
    source = source_of(spec)
    kept = kept_of(spec)
    f = parse_follow_up({
        "kind": "revise", "outline": outline, **design_of(spec),
        **({"source": js_slice(source, 0, room)} if source and room > 500 else {}),
        **({"kept": kept} if kept else {}),
    })
    return f if f is not None else "no follow-up"


def asked_for(req: dict) -> str:
    if req["mode"] == "paper":
        return f'An explainer of the research paper "{req["title"]}"' + (f". The reader's note: {req['note']}" if req.get("note") else "")
    return req["text"]


# ---------- audit ----------


def audit_task(findings: list[dict], labels: dict[str, str], later: bool = False) -> str:
    lines = [f'{f["id"]} [{f["check"]}] ' + (f'stage "{f["stage"]}" ("{labels.get(f["stage"], "?")}")' if f["stage"] else "whole page") + f': {f["detail"]}'
             for f in findings]
    return data.prompt("AUDIT_TASK") + "\n\nFindings:\n" + "\n".join(lines) + ("\n\n" + data.prompt("AUDIT_LATER") if later else "")


def audit(client: anthropic.Anthropic, f: dict, spec: dict, findings: list[dict], effort: str, usage: UsageLog,
          later: bool = False) -> list[dict]:
    """The audit's items, cleaned: [{"findings", "verdict", "action", "stage", "after", "instruction"}].

    Sent with the improve plan's exact tools, tool_choice, effort, system prompt and first user block, so the page
    it writes to the cache is read back by the improve plan (prompt caching is a prefix match, and tool_choice or
    effort changes would invalidate the messages cache)."""
    labels = {s["id"]: s["label"] for s in spec["stages"]}
    msg = client.beta.messages.create(
        model=HAIKU,
        max_tokens=8000,
        output_config={"effort": effort},
        system=cached_system(planner_system()),
        tools=[data.formats()["computeTool"]],
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": [page_block(f), {"type": "text", "text": audit_task(findings, labels, later)}]}],
    )
    usage.add_message("audit", msg)
    check_stop(msg, "audit")
    if msg.stop_reason == "tool_use":
        log.warning("audit: the model called a tool; reading any text it wrote")
    raw, _ = extract_json(text_of(msg))
    known = {x["id"] for x in findings}
    items = []
    listed = raw.get("items") if isinstance(raw, dict) else None
    for it in listed if isinstance(listed, list) else []:
        if not isinstance(it, dict):
            continue
        action = it.get("action") if it.get("action") in ACTIONS else "none"
        real = it.get("verdict") == "real" and action != "none"
        stage = it.get("stage") if isinstance(it.get("stage"), str) else ""
        if real and action != "add_section" and stage not in labels:
            real = False  # a change to a stage the page doesn't have can't be applied
        if real and later and action == "add_section":
            real = False  # later rounds fix errors only; new sections belong to round 1
        items.append({
            "findings": [x for x in it.get("findings") or [] if x in known],
            "verdict": "real" if real else "false_alarm",
            "action": action if real else "none",
            "stage": stage,
            "after": it["after"] if isinstance(it.get("after"), str) and it["after"] in labels else "",
            "instruction": js_slice(js_trim(it["instruction"]), 0, 500) if isinstance(it.get("instruction"), str) else "",
        })
    return items


# Appended to an improve's fix list on Freeform+ pages (Quality+ needs none).
_WIDGET_NOTE = ('A text block that reads "(interactive widget: ...)" stands for a finished interactive widget '
                "written for this page. Keep every such block exactly as it is and where it is; widgets can't be "
                "changed here, so fix the text around them instead.")
MODE_NOTE = {"freeform-plus": _WIDGET_NOTE, "scene-plus": _WIDGET_NOTE, "code-lab-7": _WIDGET_NOTE, "playground": _WIDGET_NOTE}  # Creative widgets, Lab scenes, Code Lab 7.0 widgets, Playground
# Modes whose first page is written in their own mode and whose built parts an improve must keep (restore_widgets).
OWN_MODES = ("freeform-plus", "scene-plus", "code-lab-7", "playground")


BUILT_BLOCKS = ("widget", "scene")  # blocks an improve can't rewrite: Creative's widgets and Lab's scenes


def placeholder(b: dict) -> str:
    """The text an improve sees in place of a widget (compact_outline)."""
    return f"(interactive widget: {b.get('title', '')})"


def restore_widgets(old: dict, new: dict) -> dict:
    """Put each Freeform+ widget back where its placeholder text block is (matched by title, in order)."""
    widgets: dict[str, list[dict]] = {}
    for st in old["stages"]:
        for b in st["blocks"]:
            if b.get("type") in BUILT_BLOCKS:
                widgets.setdefault(placeholder(b), []).append(b)

    def block(b: dict) -> dict:
        if b.get("type") == "text" and isinstance(b.get("md"), str) and widgets.get(js_trim(b["md"])):
            return widgets[js_trim(b["md"])].pop(0)
        return b
    return {**new, "stages": [{**st, "blocks": [block(b) for b in st["blocks"]]} for st in new["stages"]]}


def fix_list(items: list[dict], labels: dict[str, str]) -> str:
    lines = []
    for n, it in enumerate(i for i in items if i["verdict"] == "real"):
        if it["action"] == "add_section":
            where = f'a new stage after "{it["after"]}"' if it["after"] else "a new stage, where it fits best"
        else:
            where = f'stage "{it["stage"]}" ("{labels.get(it["stage"], "")}")'
        lines.append(f'{n + 1}. [{it["action"]}] {where}: {it["instruction"]}')
    return data.prompt("IMPROVE_TASK") + "\n\nAudit items to apply:\n" + "\n".join(lines)


def refit(mode: str, old: dict, new: dict) -> dict:
    """An improve (always Quality+) made to fit the page's own mode again."""
    if mode in OWN_MODES:
        return restore_widgets(old, new)
    return new


# ---------- improve guards ----------


def _shape(stage: dict) -> str:
    """A stage as the planner wrote it (no icon, no example steps), to tell a changed stage from a kept one."""
    return json_stringify(compact_outline({"stages": [stage]})["stages"][0])


def guard(old: dict, new: dict, items: list[dict]) -> tuple[dict | None, list[int], list[str]]:
    """(spec to keep, indices of the stages that really changed, warnings). None: keep the previous version."""
    if not isinstance(new.get("revision"), dict):
        return None, [], ["Quality 2.0: an improve rewrote the whole page instead of the audited parts; the previous version was kept."]
    real = [i for i in items if i["verdict"] == "real"]
    named = {i["stage"] for i in real if i["stage"]}
    may_remove = {i["stage"] for i in real if i["action"] == "remove_section"}
    old_by = {s["id"]: s for s in old["stages"]}
    new_ids = [s["id"] for s in new["stages"]]
    removed = [sid for sid in old_by if sid not in new_ids]
    if any(sid not in may_remove for sid in removed) or len(removed) > len(old["stages"]) // 2:
        return None, [], [f"Quality 2.0: an improve removed {len(removed)} stage(s) the audit didn't ask to remove; the previous version was kept."]
    warnings: list[str] = []
    stages: list[dict] = []
    added = 0
    for st in new["stages"]:
        before = old_by.get(st["id"])
        if before is None:
            added += 1
            if added > MAX_ADD_ROUND or len(stages) >= MAX_STAGES_DEEP:
                warnings.append(f'Quality 2.0: "{st["label"]}" was not added (at most {MAX_ADD_ROUND} new stages per round, {MAX_STAGES_DEEP} in all).')
                continue
        elif st["id"] not in named and _shape(st) != _shape(before):
            warnings.append(f'Quality 2.0: "{before["label"]}" was changed without the audit asking; it was put back.')
            st = before
        stages.append(st)
    # Changed = new, or different from before. spec["revision"] holds only counts, so compare the stages themselves.
    out = {**new, "stages": stages}
    changed = [i for i, st in enumerate(stages) if st["id"] not in old_by or _shape(st) != _shape(old_by[st["id"]])]
    return out, changed, warnings


def _carry(spec: dict, base: dict) -> dict:
    """What a revision doesn't keep by itself: the brief's look (textSize/density/emphasis aren't in designOf, and a
    revision skips apply_brief), fromMemory (a typed concept's "Reader's request" source turns it off) and the
    section tool buttons. The last revision's own summary describes only that revision, so it goes."""
    out = {k: v for k, v in spec.items() if k not in ("revision", "addition")}
    out["design"] = base["design"]
    for k in ("fromMemory", "sectionTools"):
        if k in base:
            out[k] = base[k]
        else:
            out.pop(k, None)
    return out


# ---------- the loop ----------


def generate_deep(client: anthropic.Anthropic, req: dict, emit: Emit, jev_key: str | None = None, plan_effort: str = "medium",
                  paper_cache: Any = None, usage: UsageLog | None = None, usd_budget: float | None = None) -> dict:
    """{"spec", "usage", "versions"} for a request with review rounds (quality-deep, or another mode with reviewRounds).

    usage: log into this one (the route's, so a failed run's spend is still known) instead of a new log.
    usd_budget: what the user may still spend today; no new round (or improve) starts once the run has spent it (audit F07)."""

    def over_budget() -> bool:
        return usd_budget is not None and usage.summary()["totalUsd"] >= usd_budget

    mode = req["quality"] if req["quality"] in OWN_MODES else "quality-plus"
    effort = req.get("effort") or plan_effort or "medium"
    if usage is None:
        usage = UsageLog("", effort)
    usage.label("quality-deep" if mode == "quality-plus" else mode, effort)
    extras = req.get("extras")
    options = {k: req[k] for k in ("effort", "interactivity", "level") if k in req}
    if isinstance(extras, list):
        options["extras"] = [e for e in extras if e not in NOTE_EXTRAS]

    base = generate(client, {**req, "quality": mode, **options}, emit,
                    jev_key=jev_key, plan_effort=plan_effort, paper_cache=paper_cache, usage=usage, checks=False)["spec"]
    # The page's own warnings travel with each version (a revision re-validates the whole page, so a fixed problem's
    # warning goes); `warnings` holds only this loop's notes, added to the final page's at the end.
    spec, warnings = base, []
    versions: list[dict] = [{"label": "generated", "spec": base}]
    asked = asked_for(req)
    pending: list[int] | None = None  # stages changed and not yet verified; None = the whole page
    dismissed: set[tuple[str, str]] = set()  # (stage, detail) of findings an audit already called false alarms

    if not jev_key:
        warnings.append("Quality 2.0: no Jev key, so the page couldn't be verified; this is the first version, unchecked.")
    chosen = req.get("reviewRounds")
    limit = min(chosen, ROUNDS_CEILING) if isinstance(chosen, int) and not isinstance(chosen, bool) and chosen >= 1 else MAX_ROUNDS
    budget_note = "Quality 2.0: stopped improving because today's AI allowance is used up; the page so far was kept."
    out_of_budget = False
    for rnd in range(1, limit + 1) if jev_key else ():
        if over_budget():
            warnings.append(budget_note)
            out_of_budget = True
            break
        usage.tag = f"round {rnd}"
        where = f"Round {rnd}"
        later = rnd > 1  # after round 1, only errors: no clarity, coverage or new sections
        n = len(spec["stages"]) if pending is None else len(pending)
        _step(emit, "improve", "running", f"{where} · verifying {n} section{'s' if n != 1 else ''}")
        v = verify(client, jev_key, spec, asked, usage, only=pending, errors_only=later)
        warnings += [f"Quality 2.0: the {name} check failed in round {rnd} and was skipped." for name in v["skipped"]]
        # A finding an earlier audit dismissed comes back word for word when its stage is untouched; don't re-judge it.
        findings = [x for x in v["findings"] if (x["stage"], x["detail"]) not in dismissed]
        entry: dict = {"label": f"round {rnd}", "findings": findings, "audit": []}
        versions.append(entry)
        pending = []  # everything checked this round has been verified
        if not findings:
            entry["spec"] = spec
            break
        f = follow_up_of(spec)
        if isinstance(f, str):
            warnings.append(f"Quality 2.0: stopped improving ({f}).")
            entry["spec"] = spec
            break
        _step(emit, "improve", "running", f"{where} · auditing {len(findings)} findings")
        try:
            items = audit(client, f, spec, findings, effort, usage, later=later)
        except Exception as e:
            log.exception("deepen: audit failed")
            warnings.append(f"Quality 2.0: the round {rnd} audit failed ({warning_reason(e)}); the page so far was kept.")
            entry["spec"] = spec
            break
        entry["audit"] = items
        by_id = {x["id"]: x for x in findings}
        dismissed |= {(by_id[fid]["stage"], by_id[fid]["detail"]) for i in items if i["verdict"] != "real" for fid in i["findings"]}
        real = [i for i in items if i["verdict"] == "real"]
        if not real:
            entry["spec"] = spec
            break
        if over_budget():  # the improve rewrites the page: the costliest step of a round
            warnings.append(budget_note)
            entry["spec"] = spec
            break
        labels = {s["id"]: s["label"] for s in spec["stages"]}
        doing = f"{where} · improving {len(real)} item{'s' if len(real) != 1 else ''}"
        _step(emit, "improve", "running", doing)

        def inner(e: dict, doing: str = doing) -> None:
            if e.get("type") == "step" and e.get("status") == "running" and e.get("step") in INNER_STEP:
                _step(emit, "improve", "running", f"{doing} · {INNER_STEP[e['step']]}")

        try:
            new = generate(client, {"mode": "concept", "text": f"Apply the audit of {spec['title']}", "quality": "quality-plus", **options,
                                    "followUp": {**f, "improve": fix_list(items, labels) + (f"\n\n{MODE_NOTE[mode]}" if mode in MODE_NOTE else "")}},
                           inner, jev_key=jev_key, plan_effort=plan_effort, usage=usage, checks=False)["spec"]
        except Exception as e:
            log.exception("deepen: improve failed")
            warnings.append(f"Quality 2.0: the round {rnd} improve failed ({warning_reason(e)}); the page so far was kept.")
            entry["spec"] = spec
            break
        kept, changed, guard_notes = guard(spec, refit(mode, spec, new), items)
        warnings += guard_notes
        if kept is None:
            entry["spec"] = spec
            break
        spec = _carry(kept, base)
        entry["spec"] = spec
        if not changed:  # nothing changed, so another round would find the same problems again
            warnings.append(f"Quality 2.0: the round {rnd} improve changed nothing, so the loop stopped.")
            break
        pending = changed
    usage.tag = ""

    if jev_key and pending and not out_of_budget:
        if chosen == limit:
            warnings.append(f"Quality 2.0: stopped at your limit of {limit} review round{'s' if limit != 1 else ''} with the audit still finding problems.")
        else:
            warnings.append(f"Quality 2.0: stopped at the safety cap of {limit} rounds with the audit still finding problems.")
    # The final page's own warnings (not the first version's, which may be fixed by now), the first version's notes
    # about the run (a revision doesn't repeat them), then this loop's notes.
    run_notes = [w for w in base["warnings"] if w.startswith(BUILD_SKIP)]
    spec = {**spec, "warnings": list(dict.fromkeys([*spec["warnings"], *run_notes, *warnings]))}
    if jev_key and pending:
        # The last improve's changes haven't been verified: the usual checks, which fix, on those stages only.
        _step(emit, "improve", "running", f"Final check of {len(pending)} changed section{'s' if len(pending) != 1 else ''}")
        spec, _ = clarity_check(client, jev_key, spec, usage, only=pending)
        spec, _ = fact_check(client, jev_key, spec, usage, only=pending)
        versions.append({"label": "final check", "spec": spec})
    rounds = sum(1 for v in versions if v["label"].startswith("round"))
    if jev_key:
        fixed = sum(1 for v in versions[1:] for i in v.get("audit") or [] if i["verdict"] == "real")
        _step(emit, "improve", "done", f"{rounds} round{'s' if rounds != 1 else ''} · {fixed} improvement{'s' if fixed != 1 else ''}")
    if note_fields(extras):
        _step(emit, "notes", "running")
        spec, detail = write_notes(client, spec, extras, usage)
        _step(emit, "notes", "done", detail)
    return {"spec": spec, "usage": usage.summary(), "versions": versions}
