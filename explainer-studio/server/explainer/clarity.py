"""Clarity check (Quality+ and Freeform+, after worked examples, before the fact check): Jev judges every stage, Haiku
fixes the flagged ones. Never raises: without a Jev key, or on any failure, the page is returned as it was
with a warning.

1. Haiku lists the ideas each stage relies on (CLARITY_TOPICS_SYSTEM). Jev can't name a missing idea, so
   something has to propose the candidates.
2. Jev, one request per stage, in parallel. The state holds what the earlier stages covered and the stage
   itself; the questions ask one thing each, as Jev's docs recommend:
   - per idea (noul): has it been explained by this point?
   - per stage (noul): can a reader at this level follow it with only what came before? (catches gaps
     the idea list missed)
   - per stage (3 scores): unexplained jargon, skipped steps, abstract with no example.
3. Haiku (CLARITY_SYSTEM) may only insert text/callout blocks into a flagged stage or make exact-once
   {find, replace} edits in its text/callout blocks. Every changed stage goes through check_edit.
4. Jev re-judges the changed stages once; what is still flagged becomes a warning (no second fix round).

The thresholds below are first guesses, to be tuned on hand-labelled pages like the fact check's were.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import anthropic

from . import data
from .core.factcheck import js_replace_first
from .core.validate import check_edit
from .jev import jev_ask
from .js import js_slice, json_parse
from .llm import warning_reason, write_text
from .pricing import UsageLog

log = logging.getLogger(__name__)

MISSING_AT = 0.25  # an idea is missing when Jev's "explained by this point?" is at or below this
GAP_AT = 0.3  # a stage has an unnamed gap when "can a reader follow it?" is at or below this
HARD_AT = 0.6  # a difficulty score (scaled to 0..1) at or above this, with HARD_CONF, flags the stage
HARD_CONF = 0.4
MAX_TOPICS = 6
FIX_CHUNK = 3  # flagged stages per Haiku fix call
MAX_INSERTS, MAX_EDITS = 2, 3  # per stage
SECTION_CHARS, EARLIER_CHARS = 2500, 600

READER = {
    "beginner": "beginner (no prior machine-learning knowledge)",
    "student": "intermediate reader (knows basic machine learning and linear algebra)",
    "practitioner": "advanced reader (works with machine learning)",
}
PROSE = ("text", "callout")


def _reader(spec: dict) -> str:
    return READER.get(spec.get("level"), READER["beginner"])


def _prose(stage: dict, limit: int) -> str:
    """The stage's own explanation: its text and callout blocks."""
    parts = [f"{b['title']}: {b['md']}" if b["type"] == "callout" else b["md"] for b in stage["blocks"] if b["type"] in PROSE]
    return js_slice("\n\n".join(parts), 0, limit)


def _earlier(spec: dict, s: int) -> list[dict]:
    return [{"stage": st["label"], "explains": _prose(st, EARLIER_CHARS)} for st in spec["stages"][:s]]


# ---------- 1. the ideas each stage relies on ----------


def topics(client: anthropic.Anthropic, spec: dict, usage: UsageLog, only: list[int] | None = None) -> dict[int, list[str]]:
    """only: the stage indices to ask about (a revision's changed stages); default every stage."""
    targets = set(range(len(spec["stages"])) if only is None else only)
    user = "\n\n".join([
        f"Explainer: {spec['title']} (reader: {_reader(spec)})",
        *(f"[{i}] {st['label']}\n{_prose(st, 1200) or '(no text)'}" for i, st in enumerate(spec["stages"]) if i in targets),
    ])
    text = write_text(client, usage, "clarity topics", system=data.prompt("CLARITY_TOPICS_SYSTEM"), user=user, max_tokens=3000,
                      fmt=data.formats()["clarityTopics"])
    out: dict[int, list[str]] = {}
    for e in json_parse(text)["stages"]:
        i = e.get("index")
        if not isinstance(i, int) or i not in targets:
            continue
        names = [js_slice(t.strip(), 0, 60) for t in e.get("relies_on") or [] if isinstance(t, str) and t.strip()]
        out[i] = list(dict.fromkeys(names))[:MAX_TOPICS]
    return out


# ---------- 2. Jev's judgement ----------


def _questions(reader: str, ideas: list[str]) -> dict[str, dict]:
    q: dict[str, dict] = {}
    for k, idea in enumerate(ideas):
        q[f"t{k}"] = {
            "type": "noul",
            "instructions": f"Has the idea \"{idea}\" been explained by the point `section` uses it?",
            "criteria": {
                "true": f"\"{idea}\" is explained in `covered_so_far` or in `section` itself, or it is everyday knowledge for a {reader}.",
                "false": f"`section` relies on \"{idea}\", but neither `covered_so_far` nor `section` explains it, and a {reader} would not already know it.",
            },
        }
    q["gap"] = {
        "type": "noul",
        "instructions": f"Can a {reader} follow `section` using only what `covered_so_far` explained?",
        "criteria": {
            "true": "Every idea `section` relies on is explained in `covered_so_far`, in `section` itself, or is everyday knowledge for this reader.",
            "false": "`section` relies on an idea that was never explained and this reader would not already know.",
        },
    }
    q["jargon"] = {
        "type": "score",
        "instructions": f"How well does `section` explain its technical terms for a {reader}?",
        "criteria": [
            f"Every technical term in `section` is explained where it appears, explained in `covered_so_far`, or familiar to a {reader}.",
            f"One technical term in `section` is used without explanation and a {reader} may not know it.",
            f"Several technical terms in `section` are used without explanation and a {reader} would not know them.",
        ],
    }
    q["steps"] = {
        "type": "score",
        "instructions": f"Can a {reader} follow each step of the reasoning or maths in `section`?",
        "criteria": [
            "Each step of the reasoning or maths in `section` follows from the one before it.",
            f"`section` skips one step a {reader} would need to see to follow it.",
            f"`section` jumps to its conclusion, skipping several steps a {reader} would need.",
        ],
    }
    q["example"] = {
        "type": "score",
        "instructions": "Do the abstract ideas in `section` come with something concrete?",
        "criteria": [
            "Each abstract idea in `section` comes with a concrete example, a number, an analogy, or an interactive part or chart listed in `section_parts`.",
            "One abstract idea in `section` has no concrete example, number, analogy or visual.",
            "`section` is mostly abstract, with no concrete example, number, analogy or visual.",
        ],
    }
    return q


HARD_NAMES = {"jargon": "unexplained jargon", "steps": "skipped reasoning steps", "example": "abstract ideas with no example"}


def judge(jev_key: str, spec: dict, ideas: dict[int, list[str]], stage_ids: list[int], usage: UsageLog) -> dict[int, dict]:
    """Per stage: {"missing": [idea...], "gap": bool, "hard": [dimension...]} (only stages Jev answered)."""
    reader = _reader(spec)

    def ask(s: int) -> dict:
        st = spec["stages"][s]
        state = {
            "explainer": {"title": spec["title"], "reader": reader},
            "covered_so_far": _earlier(spec, s),
            "section": {"stage": st["label"], "text": _prose(st, SECTION_CHARS)},
            "section_parts": [b["type"] for b in st["blocks"] if b["type"] not in PROSE],
        }
        return jev_ask(jev_key, state, _questions(reader, ideas.get(s, [])))

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(stage_ids)))) as pool:
        results = list(pool.map(ask, stage_ids))
    out: dict[int, dict] = {}
    total_input, model = 0, "jev"
    for s, r in zip(stage_ids, results):
        total_input += r["usage"].get("input_tokens", 0)
        model = r["model"]
        a = r["answers"]
        missing = [idea for k, idea in enumerate(ideas.get(s, [])) if (x := a.get(f"t{k}")) and x["p"] <= MISSING_AT]
        gap = bool(a.get("gap")) and a["gap"]["p"] <= GAP_AT
        hard = [d for d in HARD_NAMES if (x := a.get(d)) and x["norm"] >= HARD_AT and x["confidence"] >= HARD_CONF]
        out[s] = {"missing": missing, "gap": gap, "hard": hard}
    usage.add("jev clarity", model, {"input_tokens": total_input, "output_tokens": 0})
    return out


def flags_of(verdicts: dict[int, dict]) -> dict[int, list[str]]:
    """What to tell Haiku per flagged stage. An idea is flagged only at the first stage it is missing from
    (the fix there covers the later ones), and an unnamed gap only when no idea is named."""
    seen: set[str] = set()
    out: dict[int, list[str]] = {}
    for s in sorted(verdicts):
        v = verdicts[s]
        lines = []
        for idea in v["missing"]:
            if idea.lower() not in seen:
                seen.add(idea.lower())
                lines.append(f'relies on "{idea}", which the page has not explained by this point')
        if v["gap"] and not v["missing"]:
            lines.append("relies on some idea the page has not explained by this point (not named: find it)")
        lines += [f"too hard for this reader: {HARD_NAMES[d]}" for d in v["hard"]]
        if lines:
            out[s] = lines
    return out


# ---------- 3. Haiku's fixes ----------


def _blocks_list(stage: dict) -> str:
    rows = []
    for i, b in enumerate(stage["blocks"]):
        if b["type"] == "text":
            rows.append(f"[{i}] text: {b['md']}")
        elif b["type"] == "callout":
            rows.append(f"[{i}] callout ({b['variant']}) \"{b['title']}\": {b['md']}")
        else:
            rows.append(f"[{i}] {b['type']} (not editable)")
    rows.append(f"[{len(stage['blocks'])}] (end of stage)")
    return "\n".join(rows)


def fix(client: anthropic.Anthropic, spec: dict, flags: dict[int, list[str]], usage: UsageLog) -> list[dict]:
    """Haiku's {index, inserts, edits} for some flagged stages."""
    last = max(flags)
    user = "\n\n".join([
        f"Explainer: {spec['title']} (reader: {_reader(spec)})",
        "What the earlier stages explain:\n" + ("\n\n".join(f"[{i}] {e['stage']}: {e['explains']}" for i, e in enumerate(_earlier(spec, last))) or "(nothing: the first stage)"),
        "Flagged stages:\n\n" + "\n\n".join(
            f"Stage index {s}: \"{spec['stages'][s]['label']}\"\nFlags:\n" + "\n".join(f"- {f}" for f in fl) + "\nBlocks:\n" + _blocks_list(spec["stages"][s])
            for s, fl in sorted(flags.items())),
    ])
    text = write_text(client, usage, "clarity fix", system=data.prompt("CLARITY_SYSTEM"), user=user, max_tokens=6000,
                      fmt=data.formats()["clarify"])
    return [r for r in json_parse(text)["results"] if isinstance(r.get("index"), int) and r["index"] in flags]


def apply_clarity(stage: dict, result: dict) -> dict:
    """{"blocks", "notes", "skipped"}: edits first (on the original indices), then inserts, back to front."""
    blocks = [dict(b) for b in stage["blocks"]]
    notes: list[str] = []
    skipped: list[str] = []
    for e in (result.get("edits") or [])[:MAX_EDITS]:
        find, rep = e.get("find") or "", e.get("replace") or ""
        if not find or find == rep:
            continue
        hits = [i for i, b in enumerate(blocks) if b["type"] in PROSE and find in b["md"]]
        total = sum(blocks[i]["md"].count(find) for i in hits)
        if total != 1:
            skipped.append(f'"{js_slice(find, 0, 60)}" ' + ("is ambiguous" if total else "isn't in the stage"))
            continue
        blocks[hits[0]]["md"] = js_replace_first(blocks[hits[0]]["md"], find, rep)
        notes.append(f'"{js_slice(find, 0, 80)}" → "{js_slice(rep, 0, 80)}"')
    inserts = [x for x in (result.get("inserts") or []) if isinstance(x.get("md"), str) and x["md"].strip()][:MAX_INSERTS]
    for x in sorted(inserts, key=lambda x: -x.get("before", 0)):
        at = min(max(int(x.get("before", 0)), 0), len(blocks))
        if x.get("type") == "callout" and (x.get("title") or "").strip():
            b = {"type": "callout", "variant": x.get("variant") if x.get("variant") in ("info", "key", "analogy") else "info", "title": x["title"].strip(), "md": x["md"].strip()}
            notes.append(f'added "{js_slice(b["title"], 0, 80)}"')
        else:
            b = {"type": "text", "md": x["md"].strip()}
            notes.append(f'added a paragraph ("{js_slice(b["md"], 0, 60)}…")')
        blocks.insert(at, b)
    return {"blocks": blocks, "notes": notes, "skipped": skipped}


# ---------- the step ----------


def clarity_check(client: anthropic.Anthropic, jev_key: str | None, spec: dict, usage: UsageLog,
                  only: list[int] | None = None) -> tuple[dict, str]:
    """Check every stage (or only these indices: a revision's changed stages, judged against everything before
    them) and fix what Jev flags: (spec, progress detail). spec["clarity"] holds
    {stages, flagged, added, edited, remaining}; each change becomes a warning."""
    if not jev_key:
        return {**spec, "warnings": [*spec["warnings"], "Clarity check skipped: no Jev key."]}, "skipped (no Jev key)"
    targets = list(range(len(spec["stages"]))) if only is None else sorted(i for i in set(only) if 0 <= i < len(spec["stages"]))
    n = len(targets)
    if not n:
        return spec, "nothing to check"
    try:
        ideas = topics(client, spec, usage, targets)
        flags = flags_of(judge(jev_key, spec, ideas, targets, usage))
        stats = {"stages": n, "flagged": len(flags), "added": 0, "edited": 0, "remaining": 0}
        if not flags:
            return {**spec, "clarity": stats}, f"{n} sections, none flagged"

        items = sorted(flags.items())
        chunks = [dict(items[i:i + FIX_CHUNK]) for i in range(0, len(items), FIX_CHUNK)]
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            results = [r for rs in pool.map(lambda ch: fix(client, spec, ch, usage), chunks) for r in rs]

        nxt = spec
        changed: list[int] = []
        warnings: list[str] = []
        for r in sorted(results, key=lambda r: r["index"]):
            s = r["index"]
            a = apply_clarity(nxt["stages"][s], r)
            for why in a["skipped"]:
                log.warning("clarity: edit in stage %s not applied (%s)", s, why)
            if not a["notes"]:
                continue
            c = check_edit(nxt, s, a["blocks"])
            if not c["ok"]:
                log.warning("clarity: stage %s change reverted (%s)", s, c["reason"])
                continue
            nxt = c["spec"]
            changed.append(s)
            label = nxt["stages"][s]["label"]
            warnings += [f"Clarity ({label}): {note}" for note in a["notes"]]
            stats["added"] += sum(1 for note in a["notes"] if note.startswith("added"))
            stats["edited"] += sum(1 for note in a["notes"] if not note.startswith("added"))

        # Re-judge the changed stages once, with their new text and ideas; report what is still flagged.
        if changed:
            again = flags_of(judge(jev_key, nxt, ideas, changed, usage))
            stats["remaining"] = len(again)
            warnings += [f"Clarity ({nxt['stages'][s]['label']}): still flagged after the fix: {'; '.join(fl)}." for s, fl in sorted(again.items())]
        detail = f"{n} sections · Jev flagged {len(flags)} · {stats['added']} added, {stats['edited']} edited"
        return {**nxt, "clarity": stats, "warnings": [*nxt["warnings"], *warnings]}, detail
    except Exception as e:
        log.exception("clarity check failed")
        return {**spec, "warnings": [*spec["warnings"], f"Clarity check skipped: {warning_reason(e)}."]}, "skipped (error)"
