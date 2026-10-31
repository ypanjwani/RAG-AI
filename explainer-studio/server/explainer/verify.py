"""Quality 2.0's verify step (deepen.py): read the page and list what looks wrong. Nothing here changes the page.

Every check returns findings {"check", "stage" (stage id, "" for the whole page), "detail"}; verify() numbers them
"f1", "f2"... for the audit. A check that fails is skipped with a note, so one broken check never stops the round.

  code      free and exact: controls nothing reads, flat plots, stages with nothing to look at or use, hand-typed
            numbers that disagree with computed ones (cross_check), raw $...$ in fields that don't render maths,
            repeated quiz choices or an answer out of range, very long stages, and the page's own build warnings
            (a chart dropped for a bad formula, a readout out of range)
  facts     Jev scores every statement (factcheck.score): wrong, or inconsistent with what the page computes
  clarity   Haiku lists the ideas each stage relies on, Jev judges them (clarity.topics/judge/flags_of)
  style     one Jev request per stage: wording, notation consistent with earlier stages, interactive parts and
            charts that teach the idea, a title and summary that match the text
  coverage  Haiku names topics the page leaves out; Jev keeps those not explained anywhere

Thresholds are first guesses, to be tuned once real audits are judged.
"""

from __future__ import annotations

import copy
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import anthropic

from . import data
from .clarity import _earlier, _prose, _reader, flags_of, judge, topics
from .core.expr import eval_number
from .core.factcheck import _default_scope, claims_of, page_values
from .core.validate import VISUAL, cross_check
from .core.views import with_defs
from .factcheck import FLAG_BELOW, score
from .jev import jev_ask
from .js import is_finite, is_integer, js_max, js_min, js_slice, js_trim, json_parse
from .llm import write_text
from .pricing import UsageLog

log = logging.getLogger(__name__)

CONTROLS = ("slider", "choice", "toggle")
NOT_READERS = (*CONTROLS, "presets")  # blocks that set variables rather than read them
PROSE_KEYS = {"md", "title", "label", "caption", "text", "q", "explain", "choices", "blurb", "definition", "note",
              "xLabel", "yLabel", "on", "off", "term", "summary", "insight", "goal"}
PLAIN_KEYS = ("label", "xLabel", "yLabel", "caption", "on", "off")  # rendered as plain text, not Markdown/KaTeX
RAW_MATH = re.compile(r"\$[^$\n]+\$")
MAX_WORDS = 450  # text and callout words in one stage
WEAK_CHANGE = 0.01  # a slider is weak when its whole range moves what reads it by less than this (relative)
# Later rounds (errors_only) look only for errors: wrong facts, broken or mismatched numbers, charts and controls.
# Clarity and coverage are skipped, and of the style questions only notation (a mismatch) and parts (weak charts).
ERROR_STYLE = ("notation", "parts")
NOT_ERRORS = ("length",)  # code findings that are about style, dropped in later rounds
# Page warnings that are notes about the run, not problems on the page (the rest become "build" findings).
BUILD_SKIP = ("Quality 2.0", "Clarity", "Fact check", "The plan's JSON", "Page brief", "Jev", "Add section", "Revision",
              "Section", "Explainers have at most", "Chart switch", "Code Lab")
HARD_AT, HARD_CONF = 0.6, 0.4  # a style score flags at norm >= HARD_AT with confidence >= HARD_CONF
MISMATCH_AT = 0.3  # the title/summary noul flags at or below this
COVERED_MAX = 0.3  # a coverage candidate is missing when "already explained?" is at or below this
MAX_CANDIDATES = 4
STAGE_CHARS, SOURCE_CHARS = 900, 4000
SPARK = ("plot", "chart", "bars", "samples", "heatmap", "trajectory", "matrix", "calculator", "experiment", "readout",
         "attention", "sampling", "training", "embedding", "tokens", "table")


def _finding(check: str, stage: dict | None, detail: str) -> dict:
    return {"check": check, "stage": stage["id"] if stage else "", "detail": detail}


# ---------- code checks ----------


def _strings(v: Any, out: list[str]) -> None:
    """Every string in a block outside its prose fields: expressions, defs, markers, sweep variables."""
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, list):
        for x in v:
            _strings(x, out)
    elif isinstance(v, dict):
        for k, x in v.items():
            if k not in PROSE_KEYS and k != "type":
                _strings(x, out)


def _strings_of(b: dict) -> list[str]:
    out: list[str] = []
    _strings(b, out)
    return out


def _plain_fields(v: Any, out: list[str]) -> None:
    if isinstance(v, list):
        for x in v:
            _plain_fields(x, out)
    elif isinstance(v, dict):
        for k, x in v.items():
            if k in PLAIN_KEYS and isinstance(x, str):
                out.append(x)
            elif isinstance(x, (dict, list)):
                _plain_fields(x, out)


def _flat(b: dict, scope: dict) -> bool:
    """Every series of a plot is the same number across its x range (a flat line nothing can move)."""
    lo, hi = b.get("xMin"), b.get("xMax")
    if not (is_finite(lo) and is_finite(hi)) or hi <= lo or not b.get("series"):
        return False
    xs = [lo + (hi - lo) * i / 8 for i in range(9)]
    for s in b["series"]:
        ys = []
        for x in xs:
            sc = {**scope, "x": x, **({b["sweepVar"]: x} if b.get("sweepVar") else {})}
            y = eval_number(s["expr"], with_defs(b.get("defs"), sc))
            if is_finite(y):
                ys.append(y)
        if len(ys) < 2 or js_max(*ys) - js_min(*ys) >= 1e-9:
            return False
    return True


def _chart_plot(b: dict, sliders: dict[str, dict]) -> dict | None:
    """A chart that draws a formula against its x slider, as that plot (over the slider's range), so the
    plot checks apply to it. Random and list charts have no such line: None."""
    if b.get("type") != "chart" or b.get("view") not in ("line", "lines", "heatmap") or b.get("x") not in sliders:
        return None
    s = sliders[b["x"]]
    return {"type": "plot", "title": b.get("title", ""), "xMin": s.get("min"), "xMax": s.get("max"),
            "series": b.get("series") or [], "defs": b.get("defs"), "sweepVar": b["x"], "markerX": ""}


def _outputs(b: dict, scope: dict) -> list[float]:
    """What a readout or plot shows at these settings: each readout item, and each plot series at 5 x positions."""
    out: list[float] = []
    if b["type"] == "readout":
        exprs, xs = [i.get("expr") for i in b.get("items") or [] if isinstance(i, dict)], [None]
    elif b["type"] == "plot" and is_finite(b.get("xMin")) and is_finite(b.get("xMax")) and b["xMax"] > b["xMin"]:
        exprs = [s.get("expr") for s in b.get("series") or [] if isinstance(s, dict)]
        xs = [b["xMin"] + (b["xMax"] - b["xMin"]) * i / 4 for i in range(5)]
    else:
        return out
    for x in xs:
        sc = scope if x is None else {**scope, "x": x, **({b["sweepVar"]: x} if b.get("sweepVar") else {})}
        sc = with_defs(b.get("defs"), sc)
        out += [eval_number(e, sc) if isinstance(e, str) else float("nan") for e in exprs]
    return out


def _weak(var: str, lo: float, hi: float, readers: list[dict], scope: dict) -> bool:
    """Moving the control from its min to its max changes every readout and plot that reads it by under WEAK_CHANGE.
    Only judged when everything reading it is a readout item or plot series (with their defs): a marker, shading,
    band, table or calculator using it moves in ways this doesn't measure, so the control isn't called weak."""
    pat = re.compile(rf"(?<![\w.]){re.escape(var)}(?![\w])")
    for b in readers:
        if b["type"] not in ("readout", "plot"):
            return False
        rest = {k: v for k, v in b.items() if k not in ("items", "series", "defs")}
        if pat.search("\n".join(_strings_of(rest))):
            return False
    biggest, compared = 0.0, 0
    for b in readers:
        at_lo, at_hi = _outputs(b, {**scope, var: lo}), _outputs(b, {**scope, var: hi})
        for a, z in zip(at_lo, at_hi):
            if is_finite(a) and is_finite(z):
                compared += 1
                biggest = max(biggest, abs(z - a) / max(abs(a), abs(z), 1e-9))
    return compared > 0 and biggest < WEAK_CHANGE


def code_checks(spec: dict) -> list[dict]:
    """The free checks, over the whole page."""
    out: list[dict] = []
    stages = spec["stages"]
    reads = []
    for st in stages:
        for b in st["blocks"]:
            if b["type"] not in NOT_READERS:
                _strings(b, reads)
    read_text = "\n".join(reads)
    scope = _default_scope(spec)
    sliders = {b["var"]: b for st in stages for b in st["blocks"] if b["type"] == "slider" and isinstance(b.get("var"), str)}
    for st in stages:
        blocks = st["blocks"]
        for b in blocks:
            t = b["type"]
            if t in CONTROLS and isinstance(b.get("var"), str) and not re.search(rf"(?<![\w.]){re.escape(b['var'])}(?![\w])", read_text):
                out.append(_finding("interactivity", st, f'The {t} "{b.get("label", b["var"])}" ({b["var"]}) changes nothing: no chart, readout or calculator on the page reads it.'))
            elif t == "slider" and isinstance(b.get("var"), str) and is_finite(b.get("min")) and is_finite(b.get("max")) and b["max"] > b["min"]:
                pat = re.compile(rf"(?<![\w.]){re.escape(b['var'])}(?![\w])")
                readers = [_chart_plot(x, sliders) or x for s2 in stages for x in s2["blocks"] if x["type"] not in NOT_READERS and pat.search("\n".join(_strings_of(x)))]
                try:
                    if _weak(b["var"], b["min"], b["max"], readers, scope):
                        out.append(_finding("interactivity", st, f'The slider "{b.get("label", b["var"])}" ({b["var"]}) barely moves anything: across its whole range every readout and plot that reads it changes by under {WEAK_CHANGE:.0%}.'))
                except Exception:  # an expression the checker can't evaluate is the validator's business
                    pass
            if t in ("plot", "chart"):
                p = b if t == "plot" else _chart_plot(b, sliders)
                try:
                    if p and _flat(p, scope):
                        out.append(_finding("graph", st, f'The {t} "{b.get("title", "")}" is a flat line over its whole x range at the starting settings.'))
                except Exception:  # an expression the checker can't evaluate is the validator's business
                    pass
            if t == "quiz":
                for q in b.get("questions") or []:
                    choices = [js_trim(str(c)).lower() for c in q.get("choices") or []]
                    if len(set(choices)) < len(choices):
                        out.append(_finding("quiz", st, f'The quiz question "{js_slice(q.get("q", ""), 0, 80)}" repeats a choice.'))
                    if not is_integer(q.get("answer")) or not 0 <= q["answer"] < len(choices):
                        out.append(_finding("quiz", st, f'The quiz question "{js_slice(q.get("q", ""), 0, 80)}" marks no valid answer.'))
            plain: list[str] = []
            _plain_fields(b, plain)
            for text in plain:
                if RAW_MATH.search(text):
                    out.append(_finding("formatting", st, f'"{js_slice(text, 0, 80)}" in a {t} shows raw $...$: that field is plain text, so the maths shows as code.'))
                    break
        # A Freeform+ widget is something to use (VISUAL lacks it).
        if not any(b["type"] in VISUAL or b["type"] == "widget" for b in blocks):
            out.append(_finding("interactivity", st, "This stage has only text: nothing to look at, try or answer."))
        words = sum(len(b["md"].split()) for b in blocks if b["type"] in ("text", "callout") and isinstance(b.get("md"), str))
        if words > MAX_WORDS:
            out.append(_finding("length", st, f"This stage has {words} words of text; it is likely too long to read in one go."))
    # Hand-typed numbers that disagree with computed ones (on a copy: cross_check drops blocks as it repairs).
    msgs: list[str] = []
    try:
        cross_check(copy.deepcopy(stages), scope, msgs.append)
    except Exception:
        log.exception("verify: cross_check failed")
    for m in msgs:
        st = next((s for s in stages if f'"{s["label"]}"' in m), None)
        out.append(_finding("mismatch", st, m))
    # What the validator repaired or found broken while building the page (a dropped chart, a readout out of range).
    for w in spec.get("warnings") or []:
        if isinstance(w, str) and not w.startswith(BUILD_SKIP) and "has no visual" not in w:  # that one is found above
            st = next((s for s in stages if f'"{s["label"]}"' in w), None)
            out.append(_finding("build", st, f"While building the page: {w}"))
    return out


# ---------- Jev and Haiku checks ----------


def fact_checks(jev_key: str, spec: dict, targets: list[int], usage: UsageLog) -> list[dict]:
    claims = [c for c in claims_of(spec) if c["stage"] in set(targets)]
    if not claims:
        return []
    scores = score(jev_key, spec, claims, page_values(spec), usage)
    return [_finding("facts", spec["stages"][c["stage"]], f'This statement may be wrong or disagree with what the page computes (Jev {scores[c["id"]]:.2f}): "{js_slice(c["text"], 0, 300)}"')
            for c in claims if scores.get(c["id"], 1) < FLAG_BELOW]


def clarity_checks(client: anthropic.Anthropic, jev_key: str, spec: dict, targets: list[int], usage: UsageLog) -> list[dict]:
    if not targets:
        return []
    flags = flags_of(judge(jev_key, spec, topics(client, spec, usage, targets), targets, usage))
    return [_finding("clarity", spec["stages"][s], f) for s, fs in sorted(flags.items()) for f in fs]


def _parts(stage: dict) -> list[dict]:
    return [{"type": b["type"], **({"title": b["title"]} if isinstance(b.get("title"), str) else {})}
            for b in stage["blocks"] if b["type"] not in ("text", "callout")]


def style_questions(reader: str, interactive: bool) -> dict[str, dict]:
    q: dict[str, dict] = {
        "wording": {"type": "score", "instructions": f"How clearly and simply is `section.text` written for a {reader}?", "criteria": [
            f"Every sentence of `section.text` is clear, direct and easy for a {reader} to follow.",
            "Some sentences of `section.text` are wordy, vague or awkward, so the reader has to read them twice.",
            "Much of `section.text` is confusing, rambling or uses words that don't fit the reader."]},
        "notation": {"type": "score", "instructions": "Does `section` use the same symbols, names and terms as `covered_so_far`?", "criteria": [
            "`section` uses the same symbols, names and terms as `covered_so_far` for the same things.",
            "One symbol, name or term in `section` differs from what `covered_so_far` used for the same thing.",
            "Several symbols, names or terms in `section` conflict with `covered_so_far`."]},
        "heading": {"type": "noul", "instructions": "Do `section.stage` (the title) and `section.summary` match what `section.text` actually covers?", "criteria": {
            "true": "The title and summary describe what `section.text` explains.",
            "false": "The title or summary promises something `section.text` doesn't cover, or misses its main point."}},
    }
    if interactive:
        q["parts"] = {"type": "score", "instructions": "Do the interactive parts and charts in `section_parts` help the reader see the idea `section.text` explains?", "criteria": [
            "Each part in `section_parts` shows or lets the reader try the idea `section.text` explains.",
            "One part in `section_parts` adds little or shows something other than the idea `section.text` explains.",
            "The parts in `section_parts` don't help with the idea `section.text` explains."]}
    return q


STYLE_TEXT = {
    "wording": "Jev finds the wording of this stage hard to follow for the reader (wordy, vague or awkward sentences).",
    "notation": "Jev finds symbols, names or terms in this stage that differ from the earlier stages.",
    "parts": "Jev finds that an interactive part or chart in this stage adds little to the idea it explains.",
    "heading": "Jev finds that this stage's title or summary doesn't match what its text covers.",
}


def style_checks(jev_key: str, spec: dict, targets: list[int], usage: UsageLog, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """kinds: ask only these style questions (None: all)."""
    reader = _reader(spec)

    def ask(s: int) -> dict:
        st = spec["stages"][s]
        parts = _parts(st)
        state = {
            "explainer": {"title": spec["title"], "reader": reader},
            "covered_so_far": _earlier(spec, s),
            "section": {"stage": st["label"], "summary": st.get("blurb") or "", "text": _prose(st, 2500)},
            "section_parts": parts,
        }
        questions = style_questions(reader, any(p["type"] in SPARK for p in parts))
        if kinds is not None:
            questions = {k: q for k, q in questions.items() if k in kinds}
        if not questions:
            return {"answers": {}, "usage": {}, "model": "jev"}
        return jev_ask(jev_key, state, questions)

    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        results = list(pool.map(ask, targets))
    out: list[dict] = []
    total, model = 0, "jev"
    for s, r in zip(targets, results):
        total += r["usage"].get("input_tokens", 0)
        model = r["model"]
        for k, a in r["answers"].items():
            bad = a["p"] <= MISMATCH_AT if a["type"] == "noul" else a["norm"] >= HARD_AT and a["confidence"] >= HARD_CONF
            if bad:
                out.append(_finding("style", spec["stages"][s], STYLE_TEXT[k]))
    usage.add("jev style", model, {"input_tokens": total, "output_tokens": 0})
    return out


def source_of(spec: dict) -> str:
    context = (spec.get("chatbot") or {}).get("context") or ""
    mark = "\n\nSource material:\n"
    i = context.find(mark)
    return "" if i < 0 else js_trim(context[i + len(mark):])


def propose_gaps(client: anthropic.Anthropic, spec: dict, asked: str, usage: UsageLog) -> list[dict]:
    """Haiku's candidate missing topics: [{"topic", "why"}], at most MAX_CANDIDATES, no repeats."""
    source = source_of(spec)
    parts = [
        f"Reader's request: {asked}",
        f"Reader: {_reader(spec)}",
        f"Explainer: {spec['title']}",
        "Stages, in order:\n\n" + "\n\n".join(
            f"[{i + 1}] {st['label']}: {st.get('blurb') or ''}\n{_prose(st, STAGE_CHARS) or '(no text)'}" for i, st in enumerate(spec["stages"])),
    ]
    if source and not source.startswith("Reader's request:"):
        parts.append(f"Source material the page was built from:\n{js_slice(source, 0, SOURCE_CHARS)}")
    text = write_text(client, usage, "deepen gaps", system=data.prompt("DEEPEN_GAPS_SYSTEM"), user="\n\n".join(parts),
                      max_tokens=1500, fmt=data.formats()["deepenGaps"])
    out: list[dict] = []
    seen: set[str] = set()
    for g in json_parse(text).get("gaps") or []:
        if not isinstance(g, dict) or not isinstance(g.get("topic"), str) or not js_trim(g["topic"]):
            continue
        topic = js_slice(js_trim(g["topic"]), 0, 80)
        if topic.lower() not in seen:
            seen.add(topic.lower())
            out.append({"topic": topic, "why": js_slice(js_trim(g["why"]) if isinstance(g.get("why"), str) else "", 0, 240)})
    return out[:MAX_CANDIDATES]


def coverage_checks(client: anthropic.Anthropic, jev_key: str, spec: dict, asked: str, usage: UsageLog) -> list[dict]:
    gaps = propose_gaps(client, spec, asked, usage)
    if not gaps:
        return []
    state = {
        "explainer": {"title": spec["title"], "reader": _reader(spec)},
        "reader_request": asked,
        "page": [{"stage": st["label"], "summary": st.get("blurb") or "", "text": _prose(st, STAGE_CHARS)} for st in spec["stages"]],
    }
    questions = {f"covered{k}": {"type": "noul", "instructions": f'Does `page` already explain "{g["topic"]}" anywhere?', "criteria": {
        "true": f'At least one stage of `page` explains "{g["topic"]}", even briefly.',
        "false": f'No stage of `page` explains "{g["topic"]}".'}} for k, g in enumerate(gaps)}
    r = jev_ask(jev_key, state, questions)
    usage.add("jev gaps", r["model"], r["usage"])
    return [_finding("coverage", None, f'Missing topic: "{g["topic"]}". {g["why"]} (Jev: covered {r["answers"][f"covered{k}"]["p"]:.2f})')
            for k, g in enumerate(gaps) if (a := r["answers"].get(f"covered{k}")) and a["p"] <= COVERED_MAX]


# ---------- the step ----------


def verify(client: anthropic.Anthropic, jev_key: str, spec: dict, asked: str, usage: UsageLog,
           only: list[int] | None = None, errors_only: bool = False) -> dict:
    """{"findings": [{"id", "check", "stage", "detail"}], "skipped": [check names that failed], "stages": n checked}.
    only: the stage indices to check closely (facts, clarity, style); the code and coverage checks always cover the
    whole page. errors_only (later rounds): no clarity or coverage check, only the error-type style questions, no
    length findings. The spec is never changed."""
    targets = list(range(len(spec["stages"]))) if only is None else sorted(i for i in set(only) if 0 <= i < len(spec["stages"]))
    jobs: dict[str, Callable[[], list[dict]]] = {
        "code": lambda: [f for f in code_checks(spec) if not (errors_only and f["check"] in NOT_ERRORS)],
        "facts": lambda: fact_checks(jev_key, spec, targets, usage),
        "style": lambda: style_checks(jev_key, spec, targets, usage, ERROR_STYLE if errors_only else None),
    }
    if not errors_only:
        jobs["clarity"] = lambda: clarity_checks(client, jev_key, spec, targets, usage)
        jobs["coverage"] = lambda: coverage_checks(client, jev_key, spec, asked, usage)
    found: list[dict] = []
    skipped: list[str] = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(job) for name, job in jobs.items()}
        for name, fut in futures.items():
            try:
                found += fut.result()
            except Exception:
                log.exception("verify: %s check failed", name)
                skipped.append(name)
    return {"findings": [{"id": f"f{i + 1}", **f} for i, f in enumerate(found)], "skipped": skipped, "stages": len(targets)}
