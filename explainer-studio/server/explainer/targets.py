"""Follow-ups that change only what the reader asked about, instead of rewriting the page.

A follow-up that revises the page used to send the whole outline to the planner and take back whole stages (or a whole new
outline), and a widget was only a placeholder in it. Here:

1. find_targets (one cheap Haiku call) reads a digest of the page and says which parts the request is about: a section's
   interactive part ("widget"), its wording ("text"), a whole section ("section") or a new section ("new-section"). A request that
   changes the whole page ("explain it simpler", "make it shorter") is pageWide and takes the old path.
2. apply_targets changes just those parts: one planner call for the text targets, scoped to them (edit_text), and
   codelab_edit.edit_widget for each widget (the mode's own build, checks and Jev review), and adds a widget to a section that
   has none (codelab_edit.add_widget, which respects the page's cap).
3. Text and widget must still agree: Jev is asked whether a changed section's text still describes its widget (and the other
   way round), and one more edit fixes the side that no longer matches. One round, never a loop.
4. Everything else is copied over as it was (guard_untouched), and only the changed text is clarity- and fact-checked.

It returns what pipeline.write_outline returns for a revision ({"outline", "warnings", "revision"}), so the pipeline after it is
unchanged. Anything that goes wrong falls back to the old path (find_targets returns None) or keeps the old part with a warning:
a follow-up is never worse than before, and an edit that fails never loses a widget or half-changes a section.

FOLLOWUP_TARGETS=0 turns the finder off (every follow-up then takes the old path).
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import anthropic

from . import data
from . import codelab_edit as ce
from .codelab import PLANNER_NOTE, strip_prebuilt
from .core.followup import revision_request
from .js import js_slice, json_parse
from .labplan import block_text
from .llm import HAIKU, cached_system, text_of
from .pricing import UsageLog
from .revise import parse_revision, restore_kept_widgets

log = logging.getLogger("explainer.targets")

PARTS = ("widget", "text", "section", "new-section")
MAX_TARGETS = 6  # more than this is a request about the page, not about a part of it
SNIPPET = 200
MAX_WORKERS = 4
PAGE_FIELDS = ("title", "subtitle", "keyIntuition", "level")


def enabled() -> bool:
    return os.environ.get("FOLLOWUP_TARGETS") != "0"


# ---------- finding the targets ----------


def outline_digest(outline: dict) -> str:
    """The page for the finder: each section's id, label and blurb, then its blocks as "[index] type: start of the text" and
    its widgets as "[index] widget "title": what it teaches". A few thousand tokens instead of the whole page."""
    lines = [f'Page: "{outline.get("title", "")}" (level: {outline.get("level", "")})']
    for st in outline["stages"]:
        lines.append(f'Section id "{st["id"]}", "{st.get("label", "")}": {st.get("blurb", "")}'.rstrip(": "))
        for bi, b in enumerate(st["blocks"]):
            if b.get("type") == "widget":
                lines.append(f"  [{bi}] {ce.widget_summary(b)}")
                continue
            words = block_text(b) or str(b.get("latex") or b.get("title") or b.get("prompt") or "")
            words = js_slice(re.sub(r"\s+", " ", words).strip(), 0, SNIPPET)
            lines.append(f"  [{bi}] {b.get('type')}" + (f": {words}" if words else ""))
    return "\n".join(lines)


def clean_targets(raw: Any, outline: dict) -> dict | None:
    """{"targets", "pageWide"} from the finder's reply, or None when it can't be used (the old path then runs). Targets for a
    section the page doesn't have, with no change described, or repeated are dropped; more than MAX_TARGETS means the
    request is about the page as a whole."""
    if not isinstance(raw, dict):
        return None
    if raw.get("pageWide") is True:
        return {"targets": [], "pageWide": True}
    ids = [s["id"] for s in outline["stages"]]
    out: list[dict] = []
    for t in raw.get("targets") if isinstance(raw.get("targets"), list) else []:
        if not isinstance(t, dict) or t.get("part") not in PARTS:
            continue
        what = re.sub(r"\s+", " ", str(t.get("what") or "")).strip()
        if not what:
            continue
        if t["part"] == "new-section":
            after = t.get("after") if t.get("after") in ids or t.get("after") == "start" else ids[-1]
            target = {"part": "new-section", "stage": "", "after": after, "what": what}
        elif t.get("stage") in ids:
            target = {"part": t["part"], "stage": t["stage"], "after": "", "what": what}
        else:
            continue
        same = next((x for x in out if x["part"] == target["part"] and x["stage"] == target["stage"] and x["after"] == target["after"]), None)
        if same:  # one target per part: its changes are joined
            same["what"] += "; " + what
        else:
            out.append(target)
    if not out:
        return None
    if len(out) > MAX_TARGETS:
        return {"targets": [], "pageWide": True}
    return {"targets": out, "pageWide": False}


def find_targets(client: anthropic.Anthropic, usage: UsageLog, outline: dict, text: str) -> dict | None:
    """One small structured-output call (Low effort): which parts of the page the request is about. None (take the old
    path) when disabled, when the call fails or when the reply is unusable."""
    if not enabled():
        return None
    try:
        msg = client.messages.create(
            model=HAIKU,
            max_tokens=1500,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": data.formats()["targets"]}},
            system=cached_system(data.prompt("TARGET_SYSTEM")),
            messages=[{"role": "user", "content": f"{outline_digest(outline)}\n\nThe reader's request: {text}"}],
        )
        usage.add_message("follow-up targets", msg)
        return clean_targets(json_parse(text_of(msg)), outline)
    except Exception:
        log.exception("Follow-up target finder failed; the request takes the old path")
        return None


# ---------- changing the text ----------


def guard_untouched(old: dict, new: dict, allowed: set[str], new_stages: int) -> tuple[dict, list[str]]:
    """`new` with everything the targets didn't name put back as it was in `old`: a changed stage that wasn't a target, a
    stage that was removed, the page's title, subtitle, key intuition and level, and any new stage past `new_stages`.
    Returns (outline, warnings)."""
    old_by = {s["id"]: s for s in old["stages"]}
    warnings: list[str] = []
    stages: list[dict] = []
    added = 0
    for st in new["stages"]:
        before = old_by.get(st["id"])
        if before is None:
            added += 1
            if added > new_stages:
                warnings.append(f'Follow-up: "{st.get("label", st["id"])}" was not added (it wasn\'t asked for).')
                continue
        elif st["id"] not in allowed and st != before:
            warnings.append(f'Follow-up: "{before.get("label", before["id"])}" was changed without being asked; it was put back.')
            st = before
        stages.append(st)
    present = {s["id"] for s in stages}
    for i, st in enumerate(old["stages"]):
        if st["id"] not in present:  # removed without being asked: back after the stage that preceded it
            warnings.append(f'Follow-up: "{st.get("label", st["id"])}" was removed without being asked; it was put back.')
            prev = old["stages"][i - 1]["id"] if i else None
            at = next((j + 1 for j, s in enumerate(stages) if s["id"] == prev), 0)
            stages.insert(at, st)
            present.add(st["id"])
    out = {**new, "stages": stages}
    for k in PAGE_FIELDS:
        if k in old:
            out[k] = old[k]
    return out, warnings


def scope_changes(raw: Any, allowed: set[str], new_stages: int) -> tuple[Any, list[str]]:
    """The planner's {"changes": ...} cut down to the targets before it is merged: only replacements of the named stages, the first
    `new_stages` additions, and no removals. Anything else (a full outline, say) passes through for guard_untouched to deal with."""
    if not (isinstance(raw, dict) and isinstance(raw.get("changes"), dict) and "stages" not in raw):
        return raw, []
    ch, warnings = dict(raw["changes"]), []
    replace = [x for x in ch.get("replace") or [] if isinstance(x, dict)]
    kept = [x for x in replace if x.get("id") in allowed]
    add = [x for x in ch.get("add") or [] if isinstance(x, dict)]
    if len(kept) < len(replace):
        warnings.append("Follow-up: the planner also rewrote stages that weren't asked about; they were left as they were.")
    if len(add) > new_stages:
        warnings.append("Follow-up: the planner added more stages than were asked for; the extra ones were left out.")
    if ch.get("remove"):
        warnings.append("Follow-up: the planner tried to remove stages; none were removed.")
    ch.pop("remove", None)
    return {**raw, "changes": {**ch, "replace": kept, "add": add[:new_stages]}}, warnings


def scope_text(request: str, targets: list[dict], old: dict, plans: dict[str, str]) -> str:
    """The reader's request, narrowed to the targets: which stages to change and what to do, and which of them hold an
    interactive part (so the new text stays consistent with it)."""
    label = {s["id"]: s.get("label", "") for s in old["stages"]}
    lines = []
    for t in targets:
        if t["part"] == "new-section":
            lines.append(f'- add one new stage {"at the start" if t["after"] == "start" else "after stage " + json.dumps(t["after"])}: {t["what"]}')
        else:
            how = "rewrite it" if t["part"] == "section" else "change its wording or blocks, keeping its idea"
            lines.append(f'- stage {json.dumps(t["stage"])} ("{label.get(t["stage"], "")}"): {how}: {t["what"]}')
    out = [request, "Change ONLY these parts of the page. Leave every other stage out of your reply:", *lines]
    held = [f'- stage {json.dumps(sid)} has the interactive part {plan}' for sid, plan in plans.items()]
    if held:
        out += ["The interactive parts on the page (each is a text block \"(interactive widget: ...)\" in the outline; keep those blocks as they are):", *held]
    return "\n".join(out)


def _plans(outline: dict) -> dict[str, str]:
    """stage id -> '"title", which teaches: ...' for the stages that hold a widget."""
    out: dict[str, str] = {}
    for st in outline["stages"]:
        for b in st["blocks"]:
            if b.get("type") == "widget":
                lesson = str(ce.read_plan(b).get("lesson") or "")
                out[st["id"]] = (out[st["id"]] + "; " if st["id"] in out else "") + f'"{b.get("title", "")}"' + (f", which teaches: {lesson}" if lesson else "")
    return out


def _compact_json(outline: dict) -> str:
    return json.dumps(ce.viewed(outline), ensure_ascii=False, separators=(",", ":"))


Ask = Callable[[str, Callable[[Any], dict]], dict]  # (planner request, parse) -> {"outline", "warnings", "revision"?}


def edit_text(ask: Ask, follow: dict, current: dict, targets: list[dict], request: str, prose_only: bool = False) -> tuple[dict, set[str], list[str]]:
    """(outline, changed stage ids, warnings): `current` with the text targets changed by one planner call (the change format of
    revise.py, scoped to the targets). `current` may hold real widgets; the planner sees them as placeholders and they come
    back where their placeholders are. prose_only (a Code Lab 7.0 or Playground page, whose interactive parts are widgets): the planner
    writes explanation only, as for a new page of those modes, and any prebuilt interactive block it writes anyway is dropped from the
    stages it changed. On any failure: (current, set(), [a warning]) - nothing changes."""
    compact = ce.viewed(current)
    asked = {**follow, "outline": _compact_json(current)}
    req = revision_request(scope_text(request, targets, compact, _plans(current)), asked, changes=True)
    if prose_only:
        req += "\n\n" + PLANNER_NOTE
    allowed = {t["stage"] for t in targets if t["part"] != "new-section"}
    new_stages = sum(1 for t in targets if t["part"] == "new-section")
    scoped: list[str] = []

    def parse(raw: Any) -> dict:
        cut, scoped[:] = scope_changes(raw, allowed, new_stages)
        return parse_revision(cut, compact)

    try:
        r = ask(req, parse)
    except Exception:
        log.exception("Follow-up text edit failed")
        return current, set(), ["Couldn't change the text as asked; it was kept as it was."]
    guarded, warnings = guard_untouched(compact, r["outline"], allowed, new_stages)
    warnings = [*scoped, *warnings]
    before = {s["id"]: s for s in compact["stages"]}
    if prose_only:
        stripped = {s["id"]: s for s in strip_prebuilt(guarded)["stages"]}
        guarded = {**guarded, "stages": [stripped[s["id"]] if s["id"] not in before or s != before[s["id"]] else s for s in guarded["stages"]]}
    changed = {s["id"] for s in guarded["stages"] if before.get(s["id"]) != s}
    # The real widgets go back where their placeholders are (parse_revision only ever saw placeholders).
    widgets: dict[str, list[dict]] = {}
    for st in current["stages"]:
        for b in st["blocks"]:
            if b.get("type") == "widget":
                widgets.setdefault(st["id"], []).append(b)
    return restore_kept_widgets(guarded, widgets), changed, [*r.get("warnings", []), *warnings]


# ---------- changing the widgets ----------


def _widget_at(outline: dict, sid: str, what: str) -> tuple[int, int] | None:
    """(stage index, block index) of the widget a target means: the stage's only widget, or the one whose title the change
    names, else its first. None if the stage has none."""
    si = next((i for i, s in enumerate(outline["stages"]) if s["id"] == sid), None)
    if si is None:
        return None
    found = [(bi, b) for bi, b in enumerate(outline["stages"][si]["blocks"]) if b.get("type") == "widget"]
    if not found:
        return None
    named = next((bi for bi, b in found if str(b.get("title") or "").lower() in what.lower() and b.get("title")), None)
    return si, named if named is not None else found[0][0]


def _as_blocks(targets: list[dict], page: dict, quality: str | None) -> list[dict]:
    """A "widget" target on a page that can't have widgets (Interactive and Quick read pages build their interactive parts from
    ready-made blocks: sliders, plots, experiments) is a request to change that section, so it becomes a "section" target and the
    planner may use those blocks. A widget target on a section of a page that has widgets stays: edited if the section has one,
    otherwise added (or refused, at the cap)."""
    if ce.page_kind(page, quality) is not None or any(b.get("type") == "widget" for st in page["stages"] for b in st["blocks"]):
        return targets
    return [{**t, "part": "section"} if t["part"] == "widget" else t for t in targets]


def _run_edits(client, usage, jev_key, outline, jobs: list[tuple[int, int, str]], effort: str, think: bool) -> list[dict]:
    """edit_widget for each (stage, block, what), side by side."""
    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(jobs))) as pool:
        return list(pool.map(lambda j: ce.edit_widget(client, usage, jev_key, outline, j[0], j[1], j[2], effort, think), jobs))


def apply_targets(client: anthropic.Anthropic, usage: UsageLog, jev_key: str | None, follow: dict, base: dict, targets: list[dict],
                  request: str, ask: Ask, effort: str = "medium", think: bool = True, quality: str | None = None, prose_only: bool = False) -> dict:
    """{"outline", "warnings", "revision": {"changed", "removed", "widgets"}}: the page with just the targets changed.
    base: the previous outline as the follow-up carries it (widgets as placeholders); follow["widgets"] holds the real ones."""
    warnings: list[str] = []
    targets = _as_blocks(targets, restore_kept_widgets(base, follow["widgets"]) if follow.get("widgets") else base, quality)
    text_targets = [t for t in targets if t["part"] != "widget"]
    widget_targets = [t for t in targets if t["part"] == "widget"]

    # The page with its real widgets back (the follow-up's outline only has placeholders); edit_text keeps them in place.
    current, text_changed = (restore_kept_widgets(base, follow["widgets"]) if follow.get("widgets") else base), set()
    if text_targets:
        current, text_changed, w = edit_text(ask, follow, current, text_targets, request, prose_only)
        warnings += w

    # 1. widgets that exist: changed side by side; the page keeps the old one when a change fails
    widgets_changed: set[str] = set()
    fresh: dict[str, tuple[dict | None, dict | None]] = {}  # stage id -> (facts, plan) of a widget just rebuilt
    jobs, adds = [], []
    for t in widget_targets:
        at = _widget_at(current, t["stage"], t["what"])
        (jobs if at else adds).append((*at, t["what"]) if at else t)

    def apply(results: list[dict], where: list[tuple[int, int, str]]) -> None:
        nonlocal current
        for res, (si, bi, _) in zip(results, where):
            sid = current["stages"][si]["id"]
            warnings.extend(res["notes"])
            if res["changed"]:
                current = ce.replace_block(current, si, bi, res["block"])
                widgets_changed.add(sid)
                fresh[sid] = (res["facts"], res["plan"])

    apply(_run_edits(client, usage, jev_key, current, jobs, effort, think), jobs)

    # 2. widgets to add, one after another (each counts against the page's cap)
    for t in adds:
        si = next(i for i, s in enumerate(current["stages"]) if s["id"] == t["stage"])
        r = ce.add_widget(client, usage, jev_key, current, si, t["what"], ce.page_kind(current, quality), effort, think)
        warnings.extend(r["notes"])
        if r["built"]:
            current = r["outline"]
            widgets_changed.add(t["stage"])
            fresh[t["stage"]] = (r["facts"], r["plan"])

    # 3. text and widget must still agree: one round, fixing whichever side no longer matches
    fixes = []
    for sid, (facts, plan) in fresh.items():
        si = next(i for i, s in enumerate(current["stages"]) if s["id"] == sid)
        why = ce.agrees(jev_key, current, si, plan, facts, usage)
        if why:
            lesson = str((plan or {}).get("lesson") or "")
            fixes.append({"part": "text", "stage": sid, "after": "", "what": f"Make the text match the interactive part: {why}." + (f" The interactive part teaches: {lesson}" if lesson else "")})
    if fixes:
        current, more, w = edit_text(ask, follow, current, fixes, request, prose_only)
        text_changed |= more
        warnings += w
    back = []
    for sid in sorted(text_changed - set(fresh)):
        at = _widget_at(current, sid, "")
        if not at:
            continue
        block = current["stages"][at[0]]["blocks"][at[1]]
        if ce.widget_kind(block) is None:
            continue
        why = ce.agrees(jev_key, current, at[0], ce.read_plan(block), ce.facts_of(block), usage)
        if why:
            back.append((at[0], at[1], f"Make the interactive part match the revised text: {why}."))
    if back:
        results = _run_edits(client, usage, jev_key, current, back, effort, think)
        apply(results, back)
        for res, (si, bi, _) in zip(results, back):
            if not res["changed"]:
                warnings.append(f'The text and the interactive part in "{current["stages"][si].get("label", "")}" may not match after this change.')

    ids = [s["id"] for s in current["stages"]]
    revision = {"changed": [i for i in ids if i in text_changed], "removed": 0, "widgets": [i for i in ids if i in widgets_changed]}
    return {"outline": current, "warnings": warnings, "revision": revision}
