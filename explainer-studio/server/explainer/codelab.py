"""Code Lab mode (code-lab): Haiku writes the page text, and each section gets its own interaction.

For a new page, a section starts as soon as it is complete in Haiku's streamed text (Streamed), not after the whole
page; the sections run in parallel. Prebuilt interactive blocks the planner wrote anyway are stripped. Per section:
1. Gate (Jev, labplan's yes/no questions, codelab_gate_verdict): a section is closed only on a clear no (mostly
   definitions or advice, or no quantity that varies, no process and no comparison); unsure sections go on to the
   plan, where Haiku can still decline. With no Jev key, or if Jev fails, every section goes on to the plan.
2. Plan (Haiku, High effort, CODE_LAB_PLAN_SYSTEM, format codeLabPlan): the lesson, the controls, the visuals, which
   control moves which visual, a simpler "backup" idea, and number checks (a readout's formula as an expression).
3. Build (Haiku, High effort, CODE_LAB_SYSTEM, format codeLab): the whole HTML/JS, following the plan.
4. Code checks (inspect): static rules, then scripts/check-widget.mjs runs it in jsdom, moves every control, runs the
   number checks (expected values computed here by the page's formula engine) and reports the ids on the page and
   facts for the reviewer; plan_problems() compares those ids and slider ranges with the plan. A failing widget is
   repaired by Haiku at medium effort with the exact problems, up to MAX_REPAIRS times; still failing, it stays text.
5. Review (Jev, following its docs: literal one-condition questions about the section, the plan and what the test run
   saw, never code or math). Kept unless a score is a clear no (<= REVIEW_LOW); then: clearly the same idea -> one
   medium repair and a re-review; else the backup clearly fits -> Haiku (medium) builds it, checks, re-review; else
   text. Without a Jev key, facts or an answer, a widget that passes the code checks is kept. Scores go on the
   widget's plan and in the notes.
Code Lab 2.0 (CodeLabSettings.for_effort): the page's one Effort for every step, and every widget is a Vega-Lite chart
the planner writes as a spec (CODE_LAB_CHART_NOTE): no build step, no custom code. Charts get the same code checks
(drawn by XW.vega in jsdom), repairs (CODE_LAB_VEGA_REPAIR) and review; the backup is a simpler chart
(CODE_LAB_VEGA_BACKUP). A section a chart can't show stays text.
Code Lab 3.0 (code-lab-3, CodeLabSettings.slim): Code Lab 2.0 with a chart-only plan (CODE_LAB_3_PLAN_SYSTEM, format
codeLabPlanSlim: lesson, title, place, backup, checks and the spec). Its controls are read from the spec's params
(controls_from_spec) instead of written twice, which cuts the plan's output; everything after the plan is 2.0's.
Since Oct 22 its charts are full Vega, not Vega-Lite (CodeLabSettings.full_vega: CODE_LAB_VG_PLAN_SYSTEM, format
codeLabPlanVg, CODE_LAB_VG_REPAIR / CODE_LAB_VG_BACKUP): signals, scales, axes and marks written out, which can draw
what Vega-Lite can't (arrows, shapes placed anywhere, free layouts). Controls are bound signals; on-chart events are not
used yet (the check can't move them).
Code Lab 7.0 (code-lab-7, CodeLabSettings.for_effort(free=True)): the page writer decides which sections get an interactive
part while it writes (PLANNER_NOTE_4: an "interaction" field per stage, at most half the stages and MAX_MARKED per page,
never intros or summaries, never the same quantity twice). Streamed reads the marks from the raw stream (read_mark):
marked sections skip Jev's gate and plan from the writer's idea, unmarked ones cost nothing, a section without a mark goes
through the gate. Each marked section's interactive part is Haiku's own code (the Code Lab plan, build, repairs and
backup) at the page's Effort, designed freely like Creative's (CODE_LAB_FREE_SYSTEM). The review fixes first (fix_first),
and after all sections finish one Jev call checks every pair of widgets for a repeat (audit_duplicates, on code-made
summaries); the later of a clear repeat becomes text. At most MAX_MARKED widgets stay on the page, never on a recap
(_cap_unmarked).
The code checks fail closed: a check that can't run is a failure (run_checker), every control must change what is
drawn rather than only its value label or the axis numbers, and a widget may not show notes meant for us (leaked_note).
A "Code Lab (section): ..." note says why a section stayed text or used its backup.
Playground (playground, CodeLabSettings.for_effort(playground=True)): Code Lab 7.0's writer marks, gate, cap and repeat
check, but each section's interactive part is a playground spec (the ai-playground experiment: fixed components plus
short JS snippets, drawn by shared/playground). One Haiku call writes the spec (PLAYGROUND_SYSTEM, with its own "plan"
field, no separate plan call), scripts/check-playground.mjs dry-runs it (every snippet, control and tick), failures are
repaired with the checker's errors (MAX_REPAIRS), then Jev reviews it like a Code Lab widget (fix first); all at the
page's Effort. No backup. The widget's html is <div id="pg"></div><script>XW.playground(spec)</script> (_design_playground).
The widget block and its sandboxed iframe are the Freeform+ ones (app/src/render/components/widget.ts).
Never raises: any failure leaves the section as it was.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import anthropic

from . import data
from .core.spec import parse_outline
from .core.validate import WIDGET_MAX_CHARS, without_blocks
from .json_util import extract_json
from .jev import JevError, jev_ask
from .js import js_len, json_parse
from .core.expr import eval_value
from .labplan import GATE_QUESTIONS, PG_GATE_QUESTIONS, gate_hint, gate_state, section_text
from .llm import HAIKU, friendly_error, write_text
from .pricing import UsageLog

log = logging.getLogger(__name__)

MAX_REPAIRS = 2
# Playground: one extra repair for soft layout problems (check_playground's "layout"), kept only if it is better.
LAYOUT_REPAIRS = 1
# Playground's screenshot check (_pg_look): off by default (Oct 30 run: +37% cost, 2 of 13 repairs kept); PG_LOOK=1 turns it on.
PG_LOOK = os.environ.get("PG_LOOK", "0") == "1"
PG_LOOK_LEAD = ("The playground works and passed every check. Fix only how it looks, as described below from screenshots, "
                "keeping the same controls, state, maths and views.")
PG_LOOK_CHECKED = ("overlapping or cut-off shapes, arrows with no room, labels cut inside shapes, link labels on shapes, links "
                   "through shapes, lines outside or capped at the y range, a mostly empty x or y range, text marks and heatmaps past "
                   "the chart's edge, a y-axis name on heatmap cells")
LAYOUT_LEAD = ("The playground works and passed every check. Fix only these on-screen layout problems, keeping the same "
               "controls, state, maths and views; move, resize or relabel only what is listed.")
PLAN_TOKENS = 8_000  # the plan is small; most of a High-effort call is thinking
MAX_TOKENS = 32_000  # builds are streamed (the SDK refuses a non-streaming call above ~21k); a High-effort build used 13-16k+, mostly thinking
CUTOFF = "cut off"  # check_stop's wording when a reply hit max_tokens
MAX_WORKERS = 8
MAX_CHECKS = 3  # number checks per widget
MIN_FONT = 10  # px; smaller labels are unreadable once the frame scales the SVG
CHECK_TIMEOUT = 25  # seconds for one node check
CHECK_RUNS = 2  # a check that crashes is run once more; failing twice, the widget fails (fail closed)
CHECK_FAILED = "the widget check could not run, so the widget can't be trusted: keep it simple and standard"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-widget.mjs"
# Code Lab 7.0's look check: screenshots of each finished widget in headless Chromium (Playwright), read by Haiku.
SNAP_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "snap-widget.mjs"
SNAP_TIMEOUT = 45  # seconds for one capture (browser start + up to 6 frames)
_SNAP_SLOTS = threading.Semaphore(2)  # sections run in parallel; at most 2 browsers at once (memory on the server)
LOOK_TOKENS = 8_000  # the answer is short; most of it is thinking
# Blocks a prebuilt-widget planner would write; Code Lab strips them so Haiku's code is the only interaction.
PREBUILT = {"slider", "choice", "toggle", "presets", "plot", "chart", "bars", "readout", "samples", "heatmap", "trajectory",
            "calculator", "experiment", "examples", "sampling", "training", "scene", "widget"}
# A widget added after the section's last non-question block goes before its closing questions.
QUESTIONS = {"quiz", "predict", "reveal"}

PLANNER_NOTE = (
    "Interactive parts are added later by a separate interaction designer who reads your finished text. "
    "So write only the explanation: text, callouts, formulas (written out, no sliders), cards, tabs, tables, quizzes and diagrams. "
    "Do not write sliders, choices, toggles, plots, charts, readouts, bars, calculators, experiments or other live parts."
)
# Code Lab 4.0: the writer also says which sections get an interactive chart, while it writes the page in order (so it
# knows its earlier choices: a budget, no repeats, no recap charts). Read from the stream by read_mark, then dropped by
# the outline parser, so the page format is unchanged.
PLANNER_NOTE_4 = PLANNER_NOTE + (
    "\n\nAlso decide, for every stage, whether the reader needs an interactive chart there, and add this field to each "
    'stage object: "interaction": {"chart": true or false, "idea": "one line: what the reader moves and what changes"} '
    '(idea empty when chart is false). Charts go only where moving something teaches what the text cannot: a quantity '
    "that changes with a setting, a trade-off, or a process over steps. Most sections need none. Mark at most half of "
    "the stages and at most 4 on the page. Never mark an introduction, motivation, summary, recap or cheat-sheet stage. "
    "Every idea must show something no earlier chart on this page shows: never chart the same quantity twice."
)
# Playground: the same marks, but for any interactive part, not only charts. PLANNER_NOTE_4 only named quantities,
# trade-offs and processes, so sections about connected parts (layers, attention, pipelines) were never marked and
# every hint read "slide X, watch Y" (Oct 30: no saved playground had a diagram or network). "kind" goes into the hint.
PLANNER_NOTE_PG = PLANNER_NOTE + (
    "\n\nAlso decide, for every stage, whether the reader needs an interactive part there, and add this field to each "
    'stage object: "interaction": {"chart": true or false, "kind": "quantity" | "process" | "compare" | "structure", '
    '"idea": "one line: what the reader explores and what it shows"} (kind and idea empty when chart is false). '
    "Interactive parts go only where exploring teaches what the text cannot: a quantity that changes with a setting, a "
    "trade-off or comparison, a process over steps, or a structure of connected parts the reader can explore (network "
    "layers and weights, tokens attending to each other, a pipeline, a tree of decisions). A static diagram in your text "
    "does not rule a section out: mark it when exploring that structure would teach more. Most sections need none. Mark "
    "at most half of the stages and at most 4 on the page. Never mark an introduction, motivation, summary, recap or "
    "cheat-sheet stage. Every idea must show something no earlier interactive part on this page shows."
)
MAX_MARKED = 4
MARK_KINDS = ("quantity", "process", "compare", "structure")

_STATIC_RULES = [
    (re.compile(r"\bfetch\s*\(|XMLHttpRequest|\bimport\s*\(|\bimportScripts\b|WebSocket|navigator\.sendBeacon"), "it uses the network"),
    (re.compile(r"""<(?:script|link|img|iframe|source|video|audio)\b[^>]*\b(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""", re.I), "it loads an external resource"),
    (re.compile(r"\b(?:alert|confirm|prompt)\s*\("), "it calls alert, confirm or prompt"),
    (re.compile(r"\b(?:localStorage|sessionStorage|indexedDB)\b|document\.cookie"), "it uses browser storage"),
    (re.compile(r"<form\b|\bwindow\.open\s*\(|location\s*(?:\.href)?\s*=", re.I), "it submits a form or navigates"),
]
# Notes meant for us, not the reader, that a widget can leak into its visible text (seen: "Note: the brief's example
# (s_w=2, s_l=-0.5) gives d=2.5 ..."). Narrow on purpose: "the plan" alone is normal in agent topics.
_LEAKED_NOTE = re.compile(r"\b(?:the|this|your) brief\b|\bbrief'?s\b|\bthe plan says\b|\bplan\.(?:lesson|controls|visuals|checks|expects)\b"
                          r"|\bthe section'?s (?:formula|text) (?:says|gives)\b", re.I)
_JS_COMMENT = re.compile(r"/\*[\s\S]*?\*/|(?:^|(?<=[\s;{}(),]))//[^\n]*")  # block comments, and // comments not inside a URL
_STRING_LIT = re.compile(r""""((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|`((?:[^`\\]|\\.)*)`""")
_FONT_SIZE = re.compile(r"""font-size\s*[:=]\s*["']?(\d+(?:\.\d+)?)\s*(?:px)?(?![\d.]*\s*(?:em|rem|%|pt|vw))""", re.I)
_CURLY = re.compile("[‘’“”]")
_SCRIPT = re.compile(r"<script\b[^>]*>([\s\S]*?)</script>", re.I)
_BODY = re.compile(r"<body\b[^>]*>([\s\S]*?)(?:</body>|\Z)", re.I)
_STYLE = re.compile(r"<style\b[\s\S]*?</style>", re.I)
_FENCE = re.compile(r"^```[a-z]*\s*\n([\s\S]*?)\n?```\s*$", re.I)


# ---------- the check ----------


def _node() -> str | None:
    return shutil.which("node")


# On Windows each node check gets its own hidden console (CREATE_NO_WINDOW; 0 elsewhere). Attached to the server's
# console instead, node exited at once with no output and no error once the window that started the dev server was
# gone (Oct 24: every Playground check on a page came back empty within milliseconds, so every section stayed text).
_NODE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_node(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess:
    """One node script run for the checks: stdin in, stdout/stderr captured as text. Raises TimeoutExpired."""
    return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout, encoding="utf-8", creationflags=_NODE_FLAGS)


def node_failure(proc: Any) -> str:
    """Why a node run gave nothing usable, for the log: its exit code (as hex on Windows, e.g. 0xc0000142) and stderr."""
    if proc is None:
        return "it did not start"
    code = getattr(proc, "returncode", None)
    shown = f"{code} ({code & 0xFFFFFFFF:#x})" if isinstance(code, int) else "unknown"
    return f"exit code {shown}; stderr: {(getattr(proc, 'stderr', '') or '')[-500:]!r}"


def clean_html(html: str) -> str:
    """The widget as the page shows it: a fragment. A full document becomes its head styles plus its body."""
    html = html.strip()
    m = _FENCE.match(html)
    if m:
        html = m.group(1).strip()
    if re.search(r"<(?:!doctype|html|body)\b", html, re.I):
        body = _BODY.search(html)
        head = "".join(_STYLE.findall(html.split("<body", 1)[0])) if re.search(r"<body\b", html, re.I) else ""
        html = (head + (body.group(1) if body else re.sub(r"</?(?:!doctype|html|head|body)[^>]*>", "", html, flags=re.I))).strip()
    return html


def static_problems(html: str) -> list[str]:
    """Cheap checks that need no browser: shape (the assembly rule), curly quotes in code, forbidden APIs."""
    if not html.strip():
        return ["the widget is empty"]
    out: list[str] = []
    limit = data.constants()["WIDGET_MAX_CHARS"]
    if len(html) > limit:
        out.append(f"the widget is too long ({len(html)} characters; the limit is {limit}): make it simpler")
    scripts = _SCRIPT.findall(html)
    if not scripts:
        out.append("it has no script, so nothing is interactive")
    code = "\n".join(scripts)
    if _CURLY.search(code):
        out.append("a script contains curly quotes (“ ” ‘ ’); use plain ASCII quotes in all code")
    small = sorted({float(m.group(1)) for m in _FONT_SIZE.finditer(html) if float(m.group(1)) < MIN_FONT})
    if small:
        out.append(f"text is drawn at font-size {small[0]:g}, which is too small to read; use {MIN_FONT:g} or more for every label "
                   "(window.XW.chart already draws readable axes)")
    for rx, why in _STATIC_RULES:
        if rx.search(html if "external" in why else code):
            out.append(f"{why}, which the frame forbids")
    leaked = leaked_note(html)
    if leaked:
        out.append(f'the reader would see a note meant for you ("{leaked}"): the widget must speak only to the reader, '
                   "so remove any mention of the brief, the plan or the section's instructions")
    return out


def leaked_note(html: str) -> str | None:
    """The first piece of reader-visible text (HTML text or a string in a script; code comments are skipped) that talks
    about the brief, the plan or the section's instructions, or None."""
    visible = re.sub(r"<[^>]+>", " ", _STYLE.sub(" ", _SCRIPT.sub(" ", html)))
    code = "\n".join(_SCRIPT.findall(html))
    code = _JS_COMMENT.sub(" ", code)
    strings = [next(g for g in m.groups() if g is not None) for m in _STRING_LIT.finditer(code)]
    for text in [visible, *strings]:
        m = _LEAKED_NOTE.search(text)
        if m:
            s = max(0, m.start() - 30)
            return re.sub(r"\s+", " ", text[s:m.end() + 40]).strip()
    return None


def run_checker(html: str, checks: list[dict] | dict | None = None) -> dict:
    """Run the widget (scripts/check-widget.mjs): {"problems", "ids", "facts", "runtime", "math"}. checks: the number checks
    (a list), or {"checks", "claims", "sanity"}; "math" is a Vega chart's evidence for Jev's maths review (or None). Without node at all (a dev
    machine without it) only the static checks run and the plan check reads the source. Otherwise it fails closed: a
    check that crashes twice, or that couldn't run the widget (jsdom failed to load), is a problem (CHECK_FAILED), so an
    unchecked widget never reaches a page."""
    none = {"problems": [], "ids": None, "facts": None, "runtime": False}
    node = _node()
    if not node or not SCRIPT.exists():
        log.warning("Code Lab: node or %s is missing, so widgets get only the static checks", SCRIPT.name)
        return none
    args = [node, str(SCRIPT)] + ([json.dumps(checks)] if checks else [])
    result: dict | None = None
    for attempt in range(CHECK_RUNS):
        proc = None
        try:
            proc = run_node(args, html, CHECK_TIMEOUT)
            result = json.loads(proc.stdout.strip().splitlines()[-1])
            break
        except subprocess.TimeoutExpired:
            return {**none, "problems": ["running it took too long: it may loop forever or redraw nonstop"]}
        except Exception:
            # The check itself crashed (no output, unreadable output): never pass a widget nobody ran. A blank chart once
            # reached a page this way, while several checks ran at once.
            log.exception("Code Lab: the widget check failed to run (attempt %d): %s", attempt + 1, node_failure(proc))
    if not isinstance(result, dict):
        return {**none, "problems": [CHECK_FAILED]}
    ran = bool(result.get("runtime"))
    if not ran and not result.get("problems"):
        # Node ran but couldn't run the widget (jsdom failed to load): nothing was moved or drawn, so nothing is known
        # to work. Fail closed, as for a crash; the reason goes to the log.
        log.error("Code Lab: the widget check could not run the widget: %s", result.get("runtimeError") or "no reason given")
        return {**none, "problems": [CHECK_FAILED]}
    return {"problems": [str(p) for p in result.get("problems", [])], "ids": result.get("ids") if ran else None,
            "facts": result.get("facts") if ran else None, "runtime": ran,
            "math": result.get("math") if ran and isinstance(result.get("math"), dict) else None}


def inspect(html: str, checks: list[dict] | dict | None = None) -> dict:
    """Everything the code checks find: {"problems" (in the words sent back to Haiku; empty = it passes), "ids",
    "facts", "runtime", "math"}. checks: number checks from number_checks(), or {"checks", "claims", "sanity"}."""
    problems = static_problems(html)
    if problems:  # fix the cheap ones first; running broken code only adds noise
        return {"problems": problems, "ids": None, "facts": None, "runtime": False}
    return run_checker(html, checks)


def check_widget(html: str) -> list[str]:
    """Everything wrong with a widget, in the words sent back to Haiku. Empty = it passes."""
    return inspect(html)["problems"]


def number_checks(plan: dict) -> list[dict]:
    """The plan's number checks with their expected values computed here, from the section's formula, by the page's
    formula engine (Jev and Haiku never do this math). A check whose formula doesn't give one finite number is dropped."""
    out: list[dict] = []
    for c in plan.get("checks") or []:
        if not isinstance(c, dict) or not isinstance(c.get("readout"), str) or not isinstance(c.get("expr"), str):
            continue
        setting = {s["control"]: s["value"] for s in c.get("set") or []
                   if isinstance(s, dict) and isinstance(s.get("control"), str) and isinstance(s.get("value"), (int, float)) and not isinstance(s.get("value"), bool)}
        if not setting:
            continue
        want = eval_value(c["expr"], {"seed": 0.0, **{k: float(v) for k, v in setting.items()}})
        if not isinstance(want, (int, float)) or isinstance(want, bool) or math.isnan(want):
            log.info("Code Lab: number check dropped, %r doesn't give one number at %s", c["expr"], setting)
            continue
        out.append({"set": setting, "readout": c["readout"], "expect": float(want), "expr": c["expr"]})
    return out[:MAX_CHECKS]


CLAIM_EXPECT = ("rises", "falls", "same")
SANITY_KINDS = ("probability", "percent", "nonnegative", "sums_to_one", "sums_to_100")
MAX_CLAIMS = 3


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def claim_checks(plan: dict) -> list[dict]:
    """The plan's claims (Code Lab 4.0): "moving `move` from `from` to `to` makes `field` rise, fall or stay the same",
    optionally read at the rows matching `at`. Tested in code by moving the control (check-widget.mjs)."""
    out: list[dict] = []
    for c in plan.get("claims") or []:
        if not isinstance(c, dict) or not isinstance(c.get("move"), str) or not isinstance(c.get("field"), str):
            continue
        if c.get("expect") not in CLAIM_EXPECT:
            continue
        lo, hi = c.get("from"), c.get("to")
        if not ((_num(lo) and _num(hi)) or (isinstance(lo, bool) and isinstance(hi, bool))) or lo == hi:
            continue
        raw_at = c.get("at")
        if isinstance(raw_at, list):  # the plan format's [{control, value}] (a field of the row and its value)
            raw_at = {a.get("control"): a.get("value") for a in raw_at if isinstance(a, dict)}
        at = {k: v for k, v in (raw_at or {}).items() if isinstance(k, str) and k and _num(v)} if isinstance(raw_at, dict) else {}
        out.append({"move": c["move"], "from": lo, "to": hi, "field": c["field"], "expect": c["expect"], **({"at": at} if at else {})})
    return out[:MAX_CLAIMS]


def sanity_checks(plan: dict) -> list[dict]:
    """The plan's sanity rules (Code Lab 4.0): a computed field is a probability, a percentage, never negative, or
    shares adding up to 1 or 100 (per `by` group). Tested in code on every row the chart computes."""
    out: list[dict] = []
    for s in plan.get("sanity") or []:
        if isinstance(s, dict) and isinstance(s.get("field"), str) and s["field"] and s.get("kind") in SANITY_KINDS:
            out.append({"field": s["field"], "kind": s["kind"], **({"by": s["by"]} if isinstance(s.get("by"), str) and s["by"] else {})})
    return out[:MAX_CLAIMS]


_ID_IN_SOURCE = r"""\bid\s*[=:]\s*["']?{n}(?![\w-])|\.id\s*=\s*["']{n}["']|setAttribute\(\s*["']id["']\s*,\s*["']{n}["']"""


def plan_problems(html: str, plan: dict, ids: dict | None = None) -> list[str]:
    """Where the widget departs from the plan: a planned control or visual id that isn't on the page, a slider that
    isn't a range input, or a range that isn't the planned one. ids: what the test run found on the page (however the
    element was made, kit calls included); without it, the source is read instead, leniently."""
    out: list[str] = []

    vega = ids is None and "XW.vega(" in html  # a chart's controls are its params, given their ids when drawn

    def uses(name: str) -> bool:
        if ids is not None:
            return name in ids
        if vega and re.search(rf'"name":"{re.escape(name)}"', html):
            return True
        return bool(re.search(_ID_IN_SOURCE.format(n=re.escape(name)), html))

    def slider_attrs(cid: str) -> dict | None:
        """{"type", "min"?, "max"?} of the control, or None if unknown (built from script and not run)."""
        if ids is not None:
            e = ids.get(cid) or {}
            return {"type": e.get("type") if e.get("tag") == "input" else e.get("tag"), "min": e.get("min"), "max": e.get("max")}
        tag = re.search(rf"""<input\b[^>]*\bid\s*=\s*["']?{re.escape(cid)}(?![\w-])[^>]*>""", html, re.I)
        if not tag:
            return None
        t = tag.group(0)
        typ = re.search(r"""type\s*=\s*["']?(\w+)""", t, re.I)
        found: dict = {"type": typ.group(1).lower() if typ else "text"}
        for attr in ("min", "max"):
            m = re.search(rf"""\b{attr}\s*=\s*["']?(-?\d+(?:\.\d+)?)""", t, re.I)
            found[attr] = float(m.group(1)) if m else None
        return found

    for c in plan.get("controls") or []:
        cid = c.get("id") if isinstance(c, dict) else None
        if not isinstance(cid, str) or not cid:
            continue
        if not uses(cid):
            out.append(f'the plan has a control "{cid}" but the widget has no element with that id'
                       + (' (for a kit control pass it as the id option, e.g. XW.slider(parent, {id: "' + cid + '", ...}))' if c.get("kind") == "slider" else ""))
            continue
        if c.get("kind") != "slider":
            continue
        found = slider_attrs(cid)
        if not found:
            continue
        if found["type"] != "range":
            out.append(f'the plan says "{cid}" is a slider, but it is not an <input type="range">')
            continue
        for attr in ("min", "max"):
            got, want = found.get(attr), c.get(attr)
            if got is not None and isinstance(want, (int, float)) and not isinstance(want, bool) and abs(float(got) - float(want)) > 1e-9:
                out.append(f'the slider "{cid}" has {attr} {float(got):g} but the plan says {want:g}')
    visuals = [v["id"] for v in plan.get("visuals") or [] if isinstance(v, dict) and isinstance(v.get("id"), str) and v["id"]]
    if visuals and not any(uses(v) for v in visuals):
        out.append("none of the plan's visuals (" + ", ".join(visuals) + ") has an element with that id on the page")
    return out


def plan_text(plan: dict, review: dict | None = None, math_scores: dict | None = None) -> str:
    """The plan as compact JSON (plus Jev's review scores), kept on the widget block so a weak widget can be traced."""
    keep = ("lesson", "readerDoes", "controls", "visuals", "expects", "contrast", "backup", "checks", "claims", "sanity")
    out = {k: plan[k] for k in keep if k in plan}
    if review:
        out["review"] = review
    if math_scores:
        out["math"] = math_scores
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def check_built(html: str, plan: dict | None) -> tuple[list[str], dict]:
    """(problems, report) for a built widget: the code checks (with the plan's number checks, claims and sanity rules),
    then the plan check.
    plan None = code checks only (the backup, which has its own design)."""
    report = inspect(html, {"checks": number_checks(plan), "claims": claim_checks(plan), "sanity": sanity_checks(plan)} if plan else None)
    problems = report["problems"]
    if not problems and plan:
        problems = plan_problems(html, plan, report.get("ids"))
    return problems, report


# ---------- the gate ----------


def gate_one(jev_key: str | None, outline: dict, si: int, usage: UsageLog, playground: bool = False) -> tuple[dict, str | None]:
    """({"open", "why"}, note or None) for one section. Without a key, or if Jev fails, the section is open (Haiku's
    plan can still decline). playground=True also asks about a structure of connected parts (PG_GATE_QUESTIONS)."""
    if not jev_key:
        return {"open": True, "why": None}, None
    try:
        r = jev_ask(jev_key, gate_state(outline, si, full=True), PG_GATE_QUESTIONS if playground else GATE_QUESTIONS)
    except JevError:
        r = None
    if r is None or not r["answers"]:
        return {"open": True, "why": None}, f'Code Lab ({outline["stages"][si].get("label", "")}): Jev didn\'t answer, so the section went to the plan step.'
    usage.add("jev code lab gate", r["model"], {"input_tokens": r["usage"].get("input_tokens", 0), "output_tokens": 0})
    verdict = codelab_gate_verdict(r["answers"])
    if verdict["why"]:  # how clearly the section has something to explore, to rank open sections (Code Lab 7.0/Playground)
        verdict["score"] = r["answers"].get(verdict["why"], {}).get("p", 0.5)
    return verdict, None


GATE_CLOSED_STATIC = 0.7  # mostly definitions, history, a recap or advice: a clear yes closes the section
GATE_CLOSED_NONE = 0.3  # no quantity that varies, no process and no comparison: all three a clear no closes it
GATE_OPEN_ANYWAY = 0.7  # ... but a clear yes on any of the three keeps it open even if it also reads as advice


def codelab_gate_verdict(answers: dict) -> dict:
    """{"open", "why"}: Code Lab closes a section only on a clear no (the planner can still decline an unsure one),
    unlike Lab mode's gate_verdict, which opens only on a clear yes."""
    p = {k: answers[k]["p"] for k in PG_GATE_QUESTIONS if k in answers}
    # "structure" is only asked in Playground (PG_GATE_QUESTIONS); elsewhere it is absent and never wins.
    best = max(("varies", "process", "compare", "structure"), key=lambda k: p.get(k, -1))
    # "Mostly definitions or advice" closes only when nothing to explore is a clear yes: a section can be judged both
    # advice-like and clearly comparing two settings, and that conflict is for the planner to settle.
    closed = (p.get("static", 0) >= GATE_CLOSED_STATIC and p.get(best, 0) < GATE_OPEN_ANYWAY) or p.get(best, 1) <= GATE_CLOSED_NONE
    return {"open": not closed, "why": None if closed else best}


def gate(jev_key: str | None, outline: dict, targets: list[int], usage: UsageLog, notes: list[str], playground: bool = False) -> dict[int, dict]:
    """Per target section: {"open", "why"} (gate_one for each, in parallel)."""
    if not targets:
        return {}
    if not jev_key:
        notes.append("Code Lab: no Jev key, so every section went to the plan step.")
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(targets))) as pool:
        results = list(pool.map(lambda si: gate_one(jev_key, outline, si, usage, playground=playground), targets))
    out: dict[int, dict] = {}
    for si, (verdict, note) in zip(targets, results):
        out[si] = verdict
        if note:
            notes.append(note)
    return out


# ---------- the review (Jev reads the section, the plan and what the test run saw: no code, no math) ----------
# Per the Jev docs: one literal condition per question, only the state each question needs, math kept in code (the
# number checks above), and act only on a clear answer: a widget Jev is unsure about is kept.

REVIEW_HIGH = 0.7  # at or above: a clear yes
REVIEW_LOW = 0.3  # at or below: a clear no
REVIEW_SECTION_CHARS = 12_000
REVIEW_QUESTIONS: dict[str, dict] = {
    "controls_on_topic": {
        "type": "noul",
        "instructions": "The controls in `widget.controls` set quantities that `section` talks about.",
        "criteria": {
            "true": "Each control in `widget.controls` (by its label) is a setting, input or parameter that `section` names or describes.",
            "false": "A control in `widget.controls` sets something `section` never mentions.",
        },
    },
    "change_shows_lesson": {
        "type": "noul",
        "instructions": "What `widget.changes` says changes when a control moves is the thing `plan.lesson` says the reader should see.",
        "criteria": {
            "true": "The parts that change (their ids, the before → after samples, a redrawn chart) are the quantity or picture `plan.lesson` is about.",
            "false": "The parts that change are unrelated to `plan.lesson`, or nothing that `plan.lesson` is about changes.",
        },
    },
    "uses_section_terms": {
        "type": "noul",
        "instructions": "The labels in `widget.labels` and `widget.controls` use the names and symbols that `section` uses.",
        "criteria": {
            "true": "The labels name things the way `section` does (the same terms and symbols).",
            "false": "The labels use names or symbols that `section` does not use for these things.",
        },
    },
    "same_idea": {
        "type": "noul",
        "instructions": "`widget` shows the same idea as `plan.lesson`, and only labels, readouts or small details are missing.",
        "criteria": {
            "true": "The controls and the changing parts of `widget` are about `plan.lesson`; what is wrong is a missing label, readout, line or highlight.",
            "false": "`widget` is about a different idea than `plan.lesson`.",
        },
    },
    "backup_direct": {
        "type": "noul",
        "instructions": "`plan.backup` names one control and one visual that directly show `plan.lesson`.",
        "criteria": {
            "true": "`plan.backup` says which control the reader moves and what is drawn, and that drawing is what `plan.lesson` is about.",
            "false": "`plan.backup` is empty, vague, or describes something other than `plan.lesson`.",
        },
    },
}
QUALITY = ("controls_on_topic", "change_shows_lesson", "uses_section_terms")
REVIEW_PROBLEMS = {
    "controls_on_topic": "A reviewer judged that a control sets something the section never talks about. Make every control a "
                         "setting the section describes, labeled the way the section labels it.",
    "change_shows_lesson": "A reviewer judged that what changes when the controls move is not what the lesson (plan.lesson) is about. "
                           "Make the controls change the visual that shows the lesson directly.",
    "uses_section_terms": "A reviewer judged that the labels don't use the section's names and symbols. Label controls, axes and "
                          "readouts with exactly the section's terms and symbols.",
}


# Playground mode's review. Today's questions judged every control, so a Play, Step or Resample button "set something the
# section never mentions", and a label like "Routing skew" for the section's imbalance was "not the section's term":
# offline on 11 rejected playgrounds (Oct 24), all of them on topic, controls_on_topic was 0.12-0.73 and
# uses_section_terms 0.14-0.78. With these questions (buttons that only run the playground marked role "runs it"): 0.67-0.96
# and 0.37-0.83, while the same playgrounds checked against another topic's section still scored at most 0.04. So
# PG_REVIEW_LOW sits between the two.
PG_REVIEW_LOW = 0.2
PG_REVIEW_QUESTIONS: dict[str, dict] = {
    **REVIEW_QUESTIONS,
    "controls_on_topic": {
        "type": "noul",
        "instructions": 'The setting controls in `widget.controls` (role "setting"; ignore role "runs it") change quantities that `section` is about.',
        "criteria": {
            "true": "Each setting control changes a quantity, input or parameter that `section` names, describes, or uses in its formulas or "
                    "worked example (a made-up value of something `section` discusses counts).",
            "false": "A setting control changes something unrelated to what `section` discusses.",
        },
    },
    "uses_section_terms": {
        "type": "noul",
        "instructions": "The labels in `widget.labels` and `widget.controls` refer to things `section` discusses, using its names, symbols or short forms of them.",
        "criteria": {
            "true": "Each label names something `section` discusses, with the section's term, its symbol, or a short or plain form of it.",
            "false": "A label names something `section` never discusses, or uses a name that contradicts the section's.",
        },
    },
}
PG_RUNS = ("button", "play")  # control kinds that run the playground (Play, Step, Resample) rather than set a quantity


def pg_review_problems(failed: list[str], controls: list[dict]) -> list[str]:
    """The reviewer's problems for a playground repair. An off-topic control can't be fixed by renaming it, so the
    message names the setting controls and asks for a different one where needed."""
    settings = [c.get("label", "") for c in controls if c.get("kind") not in PG_RUNS]
    out = []
    for k in failed:
        if k == "controls_on_topic":
            out.append(f"A reviewer judged that a setting control ({', '.join(settings) or 'none'}) changes something the section doesn't "
                       "discuss. Replace that control with a quantity the section describes (named with its term or symbol); "
                       "renaming it is not enough.")
        else:
            out.append(REVIEW_PROBLEMS[k])
    return out


def review_state(outline: dict, si: int, plan: dict, facts: dict, playground: bool = False) -> dict:
    """What the reviewer reads: the whole section (its text, callouts, tables, cards, tabs, comparisons, reveals and flip
    cards, and its formulas), the plan's intent and what the test run saw. Before Oct 2026 only text and callouts were
    sent, so a control the section explained only in a table or card was judged "never talked about"."""
    st = outline["stages"][si]
    prose = section_text(st)
    formulas = [b.get("latex", "") for b in st["blocks"] if b["type"] == "formula" and b.get("latex")]
    controls = [dict(c) for c in facts.get("controls") or [] if isinstance(c, dict)]
    if playground:  # Playground mode: say which controls only run it (see PG_REVIEW_QUESTIONS)
        for c in controls:
            c["role"] = "runs it" if c.get("kind") in PG_RUNS else "setting"
    return {
        "section": {"label": st.get("label", ""), "summary": st.get("blurb", ""), "text": prose[:REVIEW_SECTION_CHARS], "formulas": formulas},
        "plan": {k: plan.get(k, "") for k in ("lesson", "readerDoes", "backup")},
        "widget": {"controls": controls, "labels": facts.get("labels") or [], "changes": facts.get("changes") or []},
    }


def review(jev_key: str | None, outline: dict, si: int, plan: dict, facts: dict | None, usage: UsageLog, full: bool = True,
           fix_first: bool = False, playground: bool = False) -> dict | None:
    """Jev's verdict on a widget that passed the code checks: {"reject", "fix", "backup", "unsure", "problems", "p"}, or
    None when there is no key, no facts (the widget couldn't run) or Jev didn't answer: the widget is then kept.
    full=False asks only the quality questions (a re-review). fix_first: see review_verdict (Code Lab 4.0).
    playground=True: Playground mode's questions and threshold (PG_REVIEW_QUESTIONS, PG_REVIEW_LOW)."""
    if not jev_key or not facts:
        return None
    base = PG_REVIEW_QUESTIONS if playground else REVIEW_QUESTIONS
    questions = base if full else {k: base[k] for k in QUALITY}
    try:
        r = jev_ask(jev_key, review_state(outline, si, plan, facts, playground), questions)
    except JevError:
        return None
    a = r["answers"]
    if not all(k in a for k in QUALITY):
        return None
    usage.add("jev code lab review", r["model"], {"input_tokens": r["usage"].get("input_tokens", 0), "output_tokens": 0})
    v = review_verdict(a, bool(str(plan.get("backup") or "").strip()), fix_first, PG_REVIEW_LOW if playground else REVIEW_LOW)
    if playground and v["reject"]:
        failed = [k for k in QUALITY if v["p"].get(k, 1) <= PG_REVIEW_LOW]
        v["problems"] = pg_review_problems(failed, facts.get("controls") or [])
    log.info("Code Lab review (%s): %s", outline["stages"][si].get("label", ""), scores_text(v["p"]))
    return v


def review_verdict(answers: dict, has_backup: bool, fix_first: bool = False, low: float = REVIEW_LOW) -> dict:
    """Rejected only on a clear no (a quality score at or below REVIEW_LOW). Then: one fix if it is clearly the same
    idea, else the backup if it clearly fits, else text. Anything else is kept ("unsure" if a score is below REVIEW_HIGH).
    Off-topic controls are never sent to a fix: the plan chose them and a repair must keep the plan's controls (the
    first live run's fixes were rejected again for exactly this), so that case goes to the backup's fresh design.
    fix_first (Code Lab 4.0): every rejected chart gets one fix first (the reviewer now reads the whole section, so most
    rejections are labels to rename or a control to refocus); the backup, when it clearly fits, is the next try.
    low: the clear-no line (Playground mode uses PG_REVIEW_LOW)."""
    p = {k: round(float(answers[k]["p"]), 3) for k in REVIEW_QUESTIONS if k in answers}
    failed = [k for k in QUALITY if p.get(k, 1) <= low]
    reject = bool(failed)
    if fix_first:
        fix = reject
        backup = reject and has_backup and p.get("backup_direct", 0) >= REVIEW_HIGH
        unsure = not reject and any(p.get(k, 1) < REVIEW_HIGH for k in QUALITY)
        return {"reject": reject, "fix": fix, "backup": backup, "unsure": unsure, "problems": [REVIEW_PROBLEMS[k] for k in failed], "p": p}
    fix = reject and "controls_on_topic" not in failed and p.get("same_idea", 0) >= REVIEW_HIGH
    backup = reject and not fix and has_backup and p.get("backup_direct", 0) >= REVIEW_HIGH
    unsure = not reject and any(p.get(k, 1) < REVIEW_HIGH for k in QUALITY)
    return {"reject": reject, "fix": fix, "backup": backup, "unsure": unsure, "problems": [REVIEW_PROBLEMS[k] for k in failed], "p": p}


# ---------- Jev's maths review (Code Lab 4.0) ----------
# Jev judges, it doesn't compute: code hands it the chart's formulas, controls, axes and the numbers it draws at a few
# settings (check-widget.mjs "math"), and Jev compares them with the section. Tested offline on 26 saved charts (Oct 22):
# 7 of 11 wrong charts caught, 2 of 15 good ones flagged at p <= 0.3, the same answers on two runs. So a flag is acted on
# only when it is very clear (MATH_CLEAR), or when a flat line is also seen by code; softer flags are only logged.


def _noul(instructions: str, true: str, false: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": true, "false": false}}


MATH_QUESTIONS: dict[str, dict] = {
    "formula_matches": _noul(
        "Each formula in `chart.formulas` computes its quantity the way `section.formulas` and `section.text` define that quantity.",
        "Every drawn quantity in `chart.formulas` uses the same operations, signs and terms as the section's formula for it, with each control used where the section uses that symbol. Example numbers for quantities the section leaves general (a starting size, a per-step cost) are fine.",
        "A formula in `chart.formulas` differs from the section's formula for the same quantity: a different operation or function, a flipped sign, a missing or extra term, or a constant where the section's formula uses a quantity the reader changes."),
    "direction_matches": _noul(
        "When a control changes between the settings in `chart.samples`, the drawn numbers move the way `section.text` says that quantity moves.",
        "For each control, the numbers in `chart.samples` rise or fall with the control exactly as `section.text` describes.",
        "For some control, the numbers in `chart.samples` move the opposite way to `section.text`, or do not move where `section.text` says they change."),
    "varies_along_axis": _noul(
        "Within one setting in `chart.samples`, a quantity that `section.text` says depends on the x-axis variable takes different values in rows with different x values.",
        "Every quantity that the section says depends on the x-axis variable changes from row to row within each setting.",
        "A quantity that the section says depends on the x-axis variable has the same value in every row of a setting (a flat line where the section expects a curve)."),
    "values_plausible": _noul(
        "Every number in `chart.samples` is possible for what `chart.axes` and `chart.controls` call it.",
        "Probabilities and shares lie between 0 and 1 (or 0 and 100 for percentages), counts are not negative, and parts that should add up to a whole do.",
        "Some number in `chart.samples` is impossible for its name (a probability above 1 or below 0, a negative count, shares that should sum to the whole but do not)."),
    "controls_do_what_labels_say": _noul(
        "Each control in `chart.controls` changes the formulas in `chart.formulas` the way its label says.",
        "Every control acts as its label states: a slider labelled with a symbol sets that symbol, and a toggle labelled 'tie A to B' or 'use B' makes the formulas use B.",
        "Some control does something other than its label says: a toggle that ties A to B sets A to a fixed number instead of B, or a slider labelled with one symbol sets a different one."),
    "labels_match": _noul(
        "The labels in `chart.controls` and `chart.axes` describe the quantities that `chart.formulas` actually compute.",
        "Each control label and axis title names the quantity its formula computes, with the right meaning and scale.",
        "A control label or axis title names something different from what its formula computes (for example a label says one gap while the formula uses twice that)."),
}
# What a repair is told for each clear "no" (Haiku reads these, with the chart's spec).
MATH_PROBLEMS = {
    "formula_matches": "A reviewer judged that a formula in the chart differs from the section's formula for the same quantity (a different operation, sign or term, or a fixed number where the section uses a quantity the reader changes). Compute every drawn value exactly as the section's formulas do.",
    "direction_matches": "A reviewer judged that when a control moves, the drawn numbers move the opposite way to what the section says (or don't move where it says they change). Make each control change the values the way the section describes.",
    "varies_along_axis": "A reviewer judged that a quantity the section says depends on the x-axis variable is drawn as a flat line: it uses a control or a constant instead of each row's own x value. Compute it from the row's field (datum.<x>), not the control.",
    "values_plausible": "A reviewer judged that some drawn numbers are impossible for what they are called (a probability outside 0 to 1, a negative count, shares that don't add up). Fix the formula so every value is possible.",
    "controls_do_what_labels_say": "A reviewer judged that a control does something other than its label says (for example a toggle that ties A to B sets A to a fixed number instead of B). Make each control do exactly what its label says.",
    "labels_match": "A reviewer judged that a control label or axis title names something different from what its formula computes (for example a different quantity or twice the value). Rename the label, or change the formula, so they match.",
}
MATH_CLEAR = 0.1  # a clear no, acted on (one repair, else the original chart is kept)
MATH_SOFT = 0.3  # a likely no: logged, not acted on (on the offline test, flags between 0.1 and 0.3 included good charts)


def math_state(outline: dict, si: int, plan: dict, math_ev: dict) -> dict:
    """What Jev reads for the maths review: the section (its text and formulas) and the chart's evidence from the test run
    (formulas, controls, axes, numbers at a few settings), never the spec or code."""
    st = outline["stages"][si]
    formulas = [b.get("latex", "") for b in st["blocks"] if b["type"] == "formula" and b.get("latex")]
    return {
        "section": {"label": st.get("label", ""), "text": section_text(st)[:REVIEW_SECTION_CHARS], "formulas": formulas},
        "chart": {"title": str(plan.get("title") or ""), "lesson": str(plan.get("lesson") or ""),
                  "formulas": math_ev.get("formulas") or [], "controls": math_ev.get("controls") or [],
                  "axes": math_ev.get("axes") or [], "samples": math_ev.get("samples") or []},
    }


def math_verdict(p: dict, flat: list) -> dict:
    """{"act", "soft", "failed", "problems", "p"}: act on a clear no (<= MATH_CLEAR), or on a likely flat line
    (varies_along_axis <= MATH_SOFT) that code also sees (a computed field with one value across rows). Flags between
    MATH_CLEAR and MATH_SOFT are only reported (soft)."""
    failed = [k for k in MATH_QUESTIONS if k in p and p[k] <= MATH_CLEAR]
    if "varies_along_axis" not in failed and flat and p.get("varies_along_axis", 1) <= MATH_SOFT:
        failed.append("varies_along_axis")
    soft = [k for k in MATH_QUESTIONS if k in p and MATH_CLEAR < p[k] <= MATH_SOFT and k not in failed]
    return {"act": bool(failed), "soft": soft, "failed": failed, "problems": [MATH_PROBLEMS[k] for k in failed], "p": p}


def math_review(jev_key: str | None, outline: dict, si: int, plan: dict, math_ev: dict | None, usage: UsageLog) -> dict | None:
    """Jev's maths review of a chart that passed the code checks (math_verdict), or None when there is no key, no
    evidence (not a Vega chart, or it couldn't run) or Jev didn't answer: the chart is then kept as it is."""
    if not jev_key or not math_ev or not math_ev.get("formulas"):
        return None
    has_x = any(" runs from " in f for f in math_ev.get("formulas") or [])
    questions = {k: q for k, q in MATH_QUESTIONS.items() if k != "varies_along_axis" or has_x}
    try:
        r = jev_ask(jev_key, math_state(outline, si, plan, math_ev), questions)
    except JevError:
        return None
    usage.add("jev code lab math", r["model"], {"input_tokens": r["usage"].get("input_tokens", 0), "output_tokens": 0})
    p = {k: round(float(a["p"]), 3) for k, a in r["answers"].items() if k in questions and isinstance(a, dict) and "p" in a}
    if not p:
        return None
    v = math_verdict(p, math_ev.get("flat") or [])
    log.info("Code Lab maths review (%s): %s", outline["stages"][si].get("label", ""), scores_text(p))
    return v


def scores_text(p: dict) -> str:
    return ", ".join(f"{k} {v:.2f}" for k, v in p.items())


# ---------- Haiku ----------


def _section(outline: dict, si: int) -> dict:
    st = outline["stages"][si]
    return {
        "page": outline.get("title", ""), "level": outline.get("level", ""),
        "section": {"label": st.get("label", ""), "summary": st.get("blurb", ""), "blocks": [{"index": i, **b} for i, b in enumerate(st["blocks"])]},
    }


def _ask(client: anthropic.Anthropic, usage: UsageLog, step: str, payload: dict, repair: dict | None = None, why: list[str] | None = None,
         effort: str = "high", backup: bool = False, think: bool = True, free: bool = False, edit: dict | None = None) -> dict | None:
    """One Haiku call: the section's widget (a first build, a repair, the backup or, with `edit`, a change the reader asked
    for to a finished widget: codelab_edit.py). None if the call failed or the
    reply is unusable (the reason goes into `why`, for the run note). think=False: Haiku's thinking off (the plan
    already decided what to build; thinking was ~85-90% of a build's output tokens in Code Lab 5.0)."""
    system = data.prompt("CODE_LAB_FREE_SYSTEM" if free else "CODE_LAB_SYSTEM")  # Code Lab 7.0: Creative's freedom
    user = dict(payload)
    if backup:
        system += "\n\n" + data.prompt("CODE_LAB_BACKUP")
    if repair:
        system += "\n\n" + data.prompt("CODE_LAB_REPAIR")
        user.update(repair)
    if edit:
        system += "\n\n" + data.prompt("CODE_LAB_EDIT")
        user.update(edit)
    try:
        text = write_text(client, usage, step, system=system, user=json.dumps(user, ensure_ascii=False), max_tokens=MAX_TOKENS,
                          fmt=data.formats()["codeLab"], effort=effort, model=HAIKU, stream=True, thinking=think)
        reply, _ = extract_json(text)
    except Exception as e:
        log.exception("Code Lab: Haiku call failed (%s)", step)
        if why is not None:
            why.append(friendly_error(e))
        return None
    if not isinstance(reply, dict):
        if why is not None:
            why.append("the reply wasn't a widget")
        return None
    return reply


def _plan(client: anthropic.Anthropic, usage: UsageLog, payload: dict, hint: str, why: list[str],
          settings: "CodeLabSettings | None" = None, edit: bool = False) -> dict | None:
    """One Haiku call: the section's interaction plan. None if the call failed or the reply is unusable
    (the reason goes into `why`). edit: the payload holds a finished widget's currentPlan and the reader's request, and the
    reply is the plan it must have after the change (codelab_edit.py)."""
    try:
        st = settings or CodeLabSettings()
        if st.full_vega:  # Code Lab 3.0: the chart-only plan, as a full Vega spec
            system, fmt = data.prompt("CODE_LAB_VG_PLAN_SYSTEM"), "codeLabPlanVg"
        elif st.slim:  # Code Lab 4.0: the chart-only plan (Vega-Lite)
            system, fmt = data.prompt("CODE_LAB_3_PLAN_SYSTEM"), "codeLabPlanSlim"
        else:
            system = data.prompt("CODE_LAB_PLAN_SYSTEM") + ("\n\n" + data.prompt("CODE_LAB_CHART_NOTE") if st.vega else "")
            if st.free:  # Code Lab 7.0: the plan says what must be seen; the builder picks the form
                system += "\n\n" + data.prompt("CODE_LAB_FREE_PLAN_NOTE")
            fmt = "codeLabPlanChart" if st.vega else "codeLabPlan"
        if edit:
            system += "\n\n" + data.prompt("CODE_LAB_EDIT_PLAN_NOTE")
        text = write_text(client, usage, "code lab plan", system=system,
                          user=json.dumps({**payload, "hint": hint}, ensure_ascii=False), max_tokens=PLAN_TOKENS,
                          fmt=data.formats()[fmt], effort=st.plan, model=HAIKU, thinking=st.think)
        plan, _ = extract_json(text)
        if isinstance(plan, dict) and "vega" in plan:  # one key for the spec from here on, whichever kind it is
            plan["vegaLite"] = plan.pop("vega")
        if st.slim and isinstance(plan, dict) and plan.get("build") is True:
            plan["controls"] = controls_from_spec(read_vega(plan.get("vegaLite")))
    except Exception as e:
        log.exception("Code Lab: Haiku plan call failed")
        why.append(friendly_error(e))
        return None
    if not isinstance(plan, dict):
        why.append("the reply wasn't a plan")
        return None
    return plan


def _place(outline: dict, si: int, widget: dict, after: Any) -> dict:
    """The outline with the widget in stage si: right after block `after` if that is a real block, else before the closing questions."""
    st = outline["stages"][si]
    blocks = list(st["blocks"])
    if isinstance(after, int) and not isinstance(after, bool) and 0 <= after < len(blocks):
        at = after + 1
    else:
        at = len(blocks)
        while at > 0 and blocks[at - 1].get("type") in QUESTIONS:
            at -= 1
    blocks.insert(at, widget)
    stages = list(outline["stages"])
    stages[si] = {**st, "blocks": blocks}
    return {**outline, "stages": stages}


@dataclass(frozen=True)
class CodeLabSettings:
    """How hard Haiku thinks at each interaction step, and whether widgets are Vega-Lite charts.
    Code Lab (the default): custom code; plan and build on High, repairs and the backup on medium.
    Code Lab 2.0 (for_effort): Vega-Lite charts only (no custom code: a section a chart can't show stays text), with the
    page's one Effort setting for every step. think=False plans, repairs and backs up charts with Haiku's thinking off
    (cheaper: thinking is billed as output); in Code Lab 7.0 and Playground it also builds, repairs and backs up the widget code
    without thinking. Code Lab 1.0 keeps the default think=True. Code Lab 3.0 (slim=True): the same, with the chart-only plan
    (CODE_LAB_3_PLAN_SYSTEM, format codeLabPlanSlim), whose controls are read from the chart's params."""
    plan: str = "high"
    build: str = "high"
    fix: str = "medium"
    vega: bool = False
    think: bool = True
    slim: bool = False
    marks: bool = False  # Code Lab 7.0/Playground: the writer's per-section marks replace Jev's gate
    fix_first: bool = False  # Code Lab 7.0/Playground: a rejected chart always gets one fix before its backup or text
    full_vega: bool = False  # Code Lab 3.0: charts are full Vega specs (signals, scales, axes, marks), not Vega-Lite
    free: bool = False  # Code Lab 7.0: the builder gets Creative's freedom (CODE_LAB_FREE_SYSTEM)
    look: bool = False  # Code Lab 7.0: screenshots of each kept widget are judged by Haiku; problems get one repair (look_check)
    playground: bool = False  # Playground mode: each interactive part is a playground spec (_design_playground)

    @staticmethod
    def for_effort(effort: str, think: bool = True, slim: bool = False, full_vega: bool = False, free: bool = False,
                   playground: bool = False) -> "CodeLabSettings":
        """free=True is Code Lab 7.0: the writer's marks, fix-first review and repeat check, with every interactive part
        Haiku's own code (Code Lab's plan, build, repairs and backup) at the page's Effort, the builder free to design the
        widget its own way, like Creative (CODE_LAB_FREE_SYSTEM: kit optional, ~300 lines, no fixed-axis rule).
        playground=True is Playground mode: the same marks, gate, cap and review, with playground specs."""
        e = effort if effort in EFFORTS else "medium"
        if playground:
            return CodeLabSettings(plan=e, build=e, fix=e, vega=False, think=think, marks=True, fix_first=True, playground=True,
                                   look=PG_LOOK)
        if free:
            return CodeLabSettings(plan=e, build=e, fix=e, vega=False, think=think, marks=True, fix_first=True, free=True, look=True)
        return CodeLabSettings(plan=e, build=e, fix=e, vega=True, think=think, slim=slim or full_vega, full_vega=full_vega)


EFFORTS = ("low", "medium", "high")


def _lower(effort: str) -> str | None:
    """One effort level down (for a build that spent its whole budget thinking), or None below low."""
    i = EFFORTS.index(effort) if effort in EFFORTS else 1
    return EFFORTS[i - 1] if i > 0 else None


# ---------- Vega-Lite charts (Code Lab 2.0: every widget is one) ----------

VEGA_ID = "xw-vega"
VEGA_MAX_CHARS = 20_000
VEGA_FULL_MAX_CHARS = 40_000  # a full Vega spec writes out its scales, axes and marks, so it runs longer
VEGA_SCHEMA = "https://vega.github.io/schema/vega/v6.json"
_VEGA_FORBIDDEN = ("url", "href")  # loading anything from outside (data, images, links)


def read_vega(spec: Any) -> dict | None:
    """The plan's Vega-Lite spec (a JSON string, or already an object) as a dict, or None."""
    if isinstance(spec, dict):
        return spec
    if not isinstance(spec, str) or not spec.strip():
        return None
    try:
        value, _ = extract_json(spec)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


_BIND_KIND = {"range": "slider", "select": "select", "radio": "select", "checkbox": "checkbox"}


def is_full_vega(spec: dict | None) -> bool:
    """A full Vega spec (Code Lab 3.0) rather than Vega-Lite: by its $schema, or by its shape when that is missing
    (the same rule as XW.vega in shared/widget-kit.js)."""
    if not isinstance(spec, dict):
        return False
    schema = str(spec.get("$schema") or "")
    if "/schema/vega-lite/" in schema:
        return False
    if "/schema/vega/" in schema:
        return True
    return isinstance(spec.get("marks"), list) and not any(k in spec for k in ("mark", "layer", "hconcat", "vconcat", "concat"))


def controls_from_spec(spec: dict | None) -> list[dict]:
    """Code Lab 3.0/4.0: the plan's controls, read from the chart's top-level params (Vega-Lite) or signals (full Vega)
    that the reader can set (those with a bind), so the plan can't disagree with the chart. A slider keeps its min, max
    and step; every control its default."""
    out: list[dict] = []
    spec = spec if isinstance(spec, dict) else {}
    found = spec.get("signals") if is_full_vega(spec) else spec.get("params")
    for p in found if isinstance(found, list) else []:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"]:
            continue
        bind = p.get("bind")
        kind = _BIND_KIND.get(bind.get("input")) if isinstance(bind, dict) else None
        if not kind:
            continue
        c: dict = {"id": p["name"], "kind": kind, "label": str(bind.get("name") or p["name"]).strip()}
        for k in ("min", "max", "step"):
            v = bind.get(k)
            if kind == "slider" and isinstance(v, (int, float)) and not isinstance(v, bool):
                c[k] = v
        if "value" in p:
            c["value"] = p["value"]
        out.append(c)
    return out


def vega_problems(spec: dict | None, full: bool = False) -> list[str]:
    """What is wrong with a spec before it is drawn: not an object, too long, or loading something from outside.
    full (Code Lab 3.0): it must be a full Vega spec, and every bound control must be used somewhere in it."""
    kind = "Vega" if full else "Vega-Lite"
    if spec is None:
        return [f"the {kind} spec is missing or isn't one valid JSON object"]
    out: list[str] = []
    limit = VEGA_FULL_MAX_CHARS if full else VEGA_MAX_CHARS
    if len(json.dumps(spec)) > limit:
        out.append(f"the {kind} spec is too long (the limit is {limit} characters): make it simpler")
    if full:
        if not is_full_vega(spec):
            out.append('this is a Vega-Lite spec ("mark"/"encoding"); write a full Vega spec: "$schema" '
                       f'"{VEGA_SCHEMA}", with "signals", "data", "scales", "axes" and "marks"')
        else:
            out.extend(signal_problems(spec))
    found: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if k in _VEGA_FORBIDDEN:
                    found.add(k)
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(spec)
    if found:
        out.append('the spec loads something from outside ("' + '", "'.join(sorted(found)) + '"); use inline "values" or a "sequence" and no links or images')
    return out


_EXPR_KEYS = ("expr", "signal", "test", "update")  # where a Vega spec writes expressions


def _vega_strings(v: Any, keys: tuple[str, ...], skip: Any = None) -> list[str]:
    """Every string value under one of `keys`, anywhere in v (except inside the object `skip`)."""
    out: list[str] = []
    if v is skip and skip is not None:
        return out
    if isinstance(v, dict):
        for k, x in v.items():
            if k in keys and isinstance(x, str):
                out.append(x)
            else:
                out.extend(_vega_strings(x, keys, skip))
    elif isinstance(v, list):
        for x in v:
            out.extend(_vega_strings(x, keys, skip))
    return out


def _field_names(spec: dict) -> set[str]:
    """The data fields a full Vega spec makes: transform outputs ("as") and the keys of inline rows."""
    names: set[str] = set()
    for d in spec.get("data") or []:
        if not isinstance(d, dict):
            continue
        values = d.get("values") if isinstance(d.get("values"), list) else []
        for row in values:
            if isinstance(row, dict):
                names.update(k for k in row if isinstance(k, str))
        transforms = d.get("transform") if isinstance(d.get("transform"), list) else []
        for t in transforms:
            a = t.get("as") if isinstance(t, dict) else None
            if isinstance(a, str):
                names.add(a)
            elif isinstance(a, list):
                names.update(x for x in a if isinstance(x, str))
    return names


def signal_problems(spec: dict) -> list[str]:
    """Full Vega, two slips that pass every run check (both seen in live charts):
    - a control with the same name as a data field: a formula meant to read each row's datum.r reads the slider r
      instead (a flat line);
    - a control no expression reads: a slider that changes nothing (the widget check only fails a chart when every
      control is dead)."""
    out: list[str] = []
    signals = spec.get("signals") if isinstance(spec.get("signals"), list) else []
    fields = _field_names(spec)
    for sig in signals:
        if not (isinstance(sig, dict) and isinstance(sig.get("name"), str) and isinstance(sig.get("bind"), dict)):
            continue
        name = sig["name"]
        if name in fields:
            out.append(f'the control "{name}" has the same name as a data field ("{name}"), so formulas mix up the slider and '
                       f'each row\'s datum.{name}. Rename the control (for example "{name}_sel") everywhere the slider is meant')
            continue
        uses = _vega_strings(spec, _EXPR_KEYS, skip=sig)
        if not any(re.search(r"(?<![\w.$])" + re.escape(name) + r"(?![\w$])", u) for u in uses):
            out.append(f'the control "{name}" is never used: no formula, scale, filter or mark reads the signal "{name}", '
                       "so moving it changes nothing. Use it where the lesson needs it, or remove it")
    return out


def vega_html(spec: dict) -> str:
    """The widget for a chart: the kit draws the spec (ASCII-only JSON, and no "</" that could close the script)."""
    src = json.dumps(spec, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    return f'<div id="{VEGA_ID}"></div><script>XW.vega("{VEGA_ID}", {src})</script>'


def _vega_fix(client: anthropic.Anthropic, usage: UsageLog, section: dict, plan: dict, spec: Any, problems: list[str],
              effort: str, think: bool = True, full: bool = False) -> dict | None:
    """One Haiku call: the chart's spec corrected for these problems. None if the call failed or the reply is unusable."""
    try:
        text = write_text(client, usage, "code lab chart repair", system=data.prompt("CODE_LAB_VG_REPAIR" if full else "CODE_LAB_VEGA_REPAIR"),
                          user=json.dumps({**section, "plan": _chart_plan(plan), "yourPreviousSpec": spec, "problems": problems}, ensure_ascii=False),
                          max_tokens=PLAN_TOKENS, fmt=data.formats()["codeLabVega"], effort=effort, model=HAIKU, thinking=think)
        reply, _ = extract_json(text)
    except Exception:
        log.exception("Code Lab: Haiku chart repair failed")
        return None
    return read_vega(reply.get("spec")) if isinstance(reply, dict) else None


def _vega_backup(client: anthropic.Anthropic, usage: UsageLog, section: dict, plan: dict, effort: str,
                 think: bool = True, full: bool = False) -> dict | None:
    """One Haiku call: the plan's simpler backup idea as a new chart. None if the call failed or the reply is unusable."""
    try:
        text = write_text(client, usage, "code lab chart backup", system=data.prompt("CODE_LAB_VG_BACKUP" if full else "CODE_LAB_VEGA_BACKUP"),
                          user=json.dumps({**section, "plan": {k: plan.get(k) for k in ("lesson", "backup", "title")}}, ensure_ascii=False),
                          max_tokens=PLAN_TOKENS, fmt=data.formats()["codeLabVega"], effort=effort, model=HAIKU, thinking=think)
        reply, _ = extract_json(text)
    except Exception:
        log.exception("Code Lab: Haiku chart backup failed")
        return None
    return read_vega(reply.get("spec")) if isinstance(reply, dict) else None


def _chart_plan(plan: dict) -> dict:
    """What a chart repair needs from the plan: the design, without the long spec it is fixing."""
    return {k: plan.get(k) for k in ("lesson", "readerDoes", "title", "controls", "expects", "checks", "claims", "sanity")}


def _vega_until_clean(client: anthropic.Anthropic, usage: UsageLog, section: dict, plan: dict, spec: dict | None, res: dict,
                      effort: str, think: bool = True, full: bool = False) -> tuple[dict | None, str, list[str], dict]:
    """Check a chart (its spec, then the same code checks as any widget, with the plan's controls and number checks)
    and repair its spec until it passes, up to MAX_REPAIRS times. (spec, html, [], report) or (None, html, problems, report)."""
    report: dict = {}
    html = ""
    chart_plan = {**plan, "visuals": []}  # a chart's visuals are drawn by Vega, not elements with ids
    for attempt in range(MAX_REPAIRS + 1):
        problems = vega_problems(spec, full)
        if not problems and spec is not None:
            html = vega_html(spec)
            problems, report = check_built(html, chart_plan)
            if not problems:
                return spec, html, [], report
        if attempt == MAX_REPAIRS:
            return None, html, problems, report
        res["repaired"] += 1
        fixed = _vega_fix(client, usage, section, plan, spec, problems, effort, think, full)
        if fixed is None:
            return None, html, problems, report
        spec = fixed
    return None, html, ["no attempts"], report


def _until_clean(client: anthropic.Anthropic, usage: UsageLog, payload: dict, reply: dict, plan: dict | None, res: dict,
                 backup: bool = False, effort: str = "medium", think: bool = True, free: bool = False) -> tuple[dict | None, str, list[str], dict]:
    """Check a widget and repair it (at `effort`) until it passes, up to MAX_REPAIRS times.
    (reply, html, [], report) when it passes; (None, html, problems, report) when it doesn't. plan None = code checks
    only (the backup). report is the last test run's (its facts feed Jev's review)."""
    report: dict = {}
    html = ""
    for attempt in range(MAX_REPAIRS + 1):
        html = clean_html(str(reply.get("html") or ""))
        problems, report = check_built(html, plan)
        if not problems:
            return reply, html, [], report
        if attempt == MAX_REPAIRS:
            return None, html, problems, report
        res["repaired"] += 1
        fixed = _ask(client, usage, "code lab repair", payload, {"yourPreviousHtml": html, "problems": problems}, effort=effort, backup=backup, think=think, free=free)
        if fixed is None or fixed.get("build") is not True:
            return None, html, problems, report
        reply = {**reply, **fixed}
    return None, html, ["no attempts"], report


def _rejected(res: dict, label: str, kind: str, plan: dict, html: str, scores: dict) -> None:
    """Keep a widget Jev's review rejected (it had passed the code checks), so test runs can inspect it (debugRejected).
    kind: "first" (the planned widget), "fix" (after the reviewer's fix) or "backup". Never shown on the page."""
    res.setdefault("rejected", []).append({"section": label, "kind": kind, "title": str(plan.get("title") or ""),
                                           "lesson": str(plan.get("lesson") or ""), "html": html, "scores": scores})


# ---------- Code Lab 7.0: the look check (screenshots judged by Haiku) ----------

def snap_widget(html: str) -> list[dict] | None:
    """Screenshots of the widget as the page shows it (scripts/snap-widget.mjs: start, each slider at min and max, a
    menu's last option, ~1 s after Play): [{"label", "png"}] (png base64), or None when they can't be taken (no node,
    no Playwright or browser, a crash). This check is an extra on top of the code checks, so it fails open: no frames =
    the widget is kept as it is."""
    node = _node()
    if not node or not SNAP_SCRIPT.exists():
        return None
    with _SNAP_SLOTS:
        try:
            proc = run_node([node, str(SNAP_SCRIPT)], html, SNAP_TIMEOUT)
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            log.exception("Code Lab: the widget screenshots failed")
            return None
    if not isinstance(out, dict) or out.get("error"):
        log.warning("Code Lab: no widget screenshots: %s", (out or {}).get("error") if isinstance(out, dict) else "no output")
        return None
    frames = [f for f in out.get("frames") or [] if isinstance(f, dict) and f.get("png")]
    return frames or None


def look_review(client: anthropic.Anthropic, usage: UsageLog, frames: list[dict], plan: dict | None, title: str,
                checked: str | None = None, step: str = "code lab look") -> list[str] | None:
    """Haiku reads the frames (in order, each with its caption) and lists what a reader would see is wrong (up to 4).
    [] = it looks fine; None = the call failed. Thinking on, medium: on saved widgets it removed the false alarms that
    thinking-off answers had, for ~$0.0004 a widget (a false alarm costs a repair of a working widget)."""
    head = {"title": title, "lesson": str((plan or {}).get("lesson") or "")}
    if checked:  # Playground: what code already measured, so the judge doesn't spend the repair on it again
        head["alreadyChecked"] = checked
    content: list[dict] = [{"type": "text", "text": json.dumps(head, ensure_ascii=False)}]
    for i, f in enumerate(frames, 1):
        content.append({"type": "text", "text": f"Frame {i}: {f.get('label', '')}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": f["png"]}})
    try:
        text = write_text(client, usage, step, system=data.prompt("CODE_LAB_LOOK_SYSTEM"), user=content,
                          max_tokens=LOOK_TOKENS, fmt=data.formats()["codeLabLook"], effort="medium", model=HAIKU, thinking=True)
        reply, _ = extract_json(text)
    except Exception:
        log.exception("Code Lab: the look check failed")
        return None
    problems = reply.get("problems") if isinstance(reply, dict) else None
    if not isinstance(problems, list):
        return None
    return [re.sub(r"\s+", " ", str(p)).strip()[:300] for p in problems if str(p).strip()][:4]


def look_check(client: anthropic.Anthropic, usage: UsageLog, payload: dict, reply: dict, plan: dict | None, html: str, res: dict,
               settings: "CodeLabSettings", label: str, backup: bool = False) -> tuple[dict, str]:
    """Code Lab 7.0: screenshots of a widget that passed every other check, judged by Haiku. Problems get ONE repair (then
    the usual code checks); the repaired widget replaces the original only if it passes them and its screenshots show
    fewer problems. Otherwise, or when the screenshots or the judge fail, the original stays: a look flag never drops a
    working widget. Returns the (reply, html) to keep; res["look"] records what happened."""
    title = str(reply.get("title") or (plan or {}).get("title") or "")
    frames = snap_widget(html)
    if not frames:
        res["look"] = {"checked": False}
        return reply, html
    problems = look_review(client, usage, frames, plan, title)
    if not problems:
        res["look"] = {"checked": problems is not None, "frames": len(frames), "problems": []}
        return reply, html
    look = {"checked": True, "frames": len(frames), "problems": problems, "repaired": False}
    res["look"] = look
    res["repaired"] += 1
    fixed = _ask(client, usage, "code lab look repair", payload,
                 {"yourPreviousHtml": html, "problems": [f"How it looks (from screenshots): {p}" for p in problems]},
                 effort=settings.fix, backup=backup, think=settings.think, free=settings.free)
    if fixed is None or fixed.get("build") is not True:
        res["notes"].append(f"Code Lab ({label}): the screenshots showed problems and the repair failed, so the widget is kept as it was. ({problems[0]})")
        return reply, html
    # A backup is checked without the plan (it has its own controls), as in _design_one; the plan still gives the lesson.
    fixed, html2, _, _ = _until_clean(client, usage, payload, {**reply, **fixed}, None if backup else plan, res, backup=backup, effort=settings.fix,
                                      think=settings.think, free=settings.free)
    after = None
    if fixed is not None:
        frames2 = snap_widget(html2)
        after = look_review(client, usage, frames2, plan, title) if frames2 else None
    if after is None or len(after) >= len(problems):
        look["after"] = after
        res["notes"].append(f"Code Lab ({label}): the screenshots showed problems and the repair didn't look better, so the widget is kept as it was. ({problems[0]})")
        return reply, html
    look.update(repaired=True, after=after)
    res["notes"].append(f"Code Lab ({label}): the screenshots showed {len(problems)} problem(s); the repaired widget shows {len(after)}, so it was used.")
    return fixed, html2


def _accept(res: dict, reply: dict, plan: dict, html: str, scores: dict | None = None) -> dict:
    title = re.sub(r"\s+", " ", str(reply.get("title") or plan.get("title") or "")).strip()[:80] or "Interactive demo"
    after = reply.get("afterBlock")
    if not (isinstance(after, int) and not isinstance(after, bool) and after >= 0):
        after = plan.get("afterBlock")
    res.update(widget={"type": "widget", "title": title, "html": html, "plan": plan_text(plan, scores, (res.get("math") or {}).get("p"))},
               after=after, built=True)
    return res


def _design_one(client: anthropic.Anthropic, usage: UsageLog, outline: dict, si: int, verdict: dict, jev_key: str | None = None,
                settings: CodeLabSettings = CodeLabSettings()) -> dict:
    """{"widget"?, "after"?, "notes", "built", "repaired", "backup", "chart"} for one section: plan, then a Vega chart
    (Code Lab 2.0, charts only) or a built widget (Code Lab), code checks with repairs, then Jev's review: keep it,
    repair it, build the plan's backup (as a chart in Code Lab 2.0), or leave text."""
    if settings.playground:  # Playground mode: a playground spec instead of a plan and custom code
        return _design_playground(client, usage, outline, si, verdict, jev_key, settings)
    label = outline["stages"][si].get("label", "")
    res: dict = {"notes": [], "built": False, "repaired": 0, "backup": False, "chart": False}
    if not verdict.get("open"):
        return res
    section = _section(outline, si)
    why: list[str] = []
    # Code Lab 4.0: the writer's own idea for this section's chart; otherwise what Jev's gate saw.
    plan = _plan(client, usage, section, verdict.get("hint") or gate_hint(verdict.get("why")), why, settings)
    if plan is None:
        res["notes"].append(f"Code Lab ({label}): the planner failed, so the section stays text. ({why[0] if why else 'no answer'})")
        return res
    if plan.get("build") is not True:
        return res
    chart = settings.vega  # Code Lab 2.0: every widget is a chart (the planner declines sections a chart can't show)
    payload = {**section, "plan": {k: v for k, v in plan.items() if k != "vegaLite"}}
    if chart:
        res["chart"] = True
        spec, html, problems, report = _vega_until_clean(client, usage, section, plan, read_vega(plan.get("vegaLite")), res, settings.fix,
                                                         settings.think, settings.full_vega)
        if spec is None:
            res["notes"].append(f"Code Lab ({label}): the chart failed its checks and couldn't be repaired, so the section stays text. ({problems[0]})")
            return res
        reply: dict = {"title": plan.get("title"), "afterBlock": plan.get("afterBlock")}
    else:
        built = _ask(client, usage, "code lab write", payload, why=why, effort=settings.build, think=settings.think, free=settings.free)
        lower = _lower(settings.build)
        if built is None and why and CUTOFF in why[-1] and lower:
            # The whole budget went on thinking: once more with less thinking, so the section isn't lost.
            why.clear()
            built = _ask(client, usage, "code lab write", payload, why=why, effort=lower, think=settings.think, free=settings.free)
        if built is None:
            res["notes"].append(f"Code Lab ({label}): the builder failed, so the section stays text. ({why[0] if why else 'no answer'})")
            return res
        if built.get("build") is not True:
            res["notes"].append(f"Code Lab ({label}): the builder couldn't build the plan, so the section stays text. ({str(built.get('why') or '')[:120]})")
            return res
        fixed_reply, html, problems, report = _until_clean(client, usage, payload, built, plan, res, effort=settings.fix, think=settings.think, free=settings.free)
        if fixed_reply is None:
            res["notes"].append(f"Code Lab ({label}): the widget failed its checks and couldn't be repaired, so the section stays text. ({problems[0]})")
            return res
        reply = fixed_reply

    look = settings.look and not chart  # Code Lab 7.0: screenshots of the widget about to be kept (look_check)
    rv = review(jev_key, outline, si, plan, report.get("facts"), usage, fix_first=settings.fix_first)
    if rv is None or not rv["reject"]:
        if look:
            reply, html = look_check(client, usage, payload, reply, plan, html, res, settings, label)
        return _accept(res, reply, plan, html, rv["p"] if rv else None)
    _rejected(res, label, "first", plan, html, rv["p"])
    scores = scores_text(rv["p"])
    first_html = html  # what the backup path starts from if a fix-first repair fails (Code Lab 4.0)
    if rv["fix"]:
        res["repaired"] += 1
        if chart:
            fixed_spec = _vega_fix(client, usage, section, plan, spec, rv["problems"], settings.fix, settings.think, settings.full_vega)
            spec2, html2, _, report2 = _vega_until_clean(client, usage, section, plan, fixed_spec, res, settings.fix, settings.think,
                                                         settings.full_vega)
            ok = spec2 is not None
        else:
            fixed = _ask(client, usage, "code lab repair", payload, {"yourPreviousHtml": html, "problems": rv["problems"]}, effort=settings.fix, think=settings.think, free=settings.free)
            ok = fixed is not None and fixed.get("build") is True
            if ok:
                fixed, html2, _, report2 = _until_clean(client, usage, payload, {**reply, **fixed}, plan, res, effort=settings.fix, think=settings.think, free=settings.free)
                ok = fixed is not None
                reply = fixed or reply
        if ok:
            again = review(jev_key, outline, si, plan, report2.get("facts"), usage, full=False)
            if again is None or not again["reject"]:
                if look:
                    reply, html2 = look_check(client, usage, payload, reply, plan, html2, res, settings, label)
                return _accept(res, reply, plan, html2, again["p"] if again else rv["p"])
            _rejected(res, label, "fix", plan, html2, again["p"])
        if not (settings.fix_first and rv["backup"]):
            res["notes"].append(f"Code Lab ({label}): the reviewer's fix didn't pass review, so the section stays text. ({rv['problems'][0]} Scores: {scores})")
            return res
        html = first_html  # fix-first (4.0 charts, 5.0 widgets): the fix failed, so the backup is the next try
    if rv["backup"] and chart:
        # The backup idea as a new, simpler chart: it has its own controls, so only the code checks apply, not the plan's.
        bplan = {k: plan.get(k) for k in ("lesson", "readerDoes", "backup", "title", "afterBlock")}
        bplan.update(controls=[], checks=[])
        bspec = _vega_backup(client, usage, section, plan, settings.fix, settings.think, settings.full_vega)
        if bspec is not None:
            bspec, html, problems, report = _vega_until_clean(client, usage, section, bplan, bspec, res, settings.fix, settings.think,
                                                              settings.full_vega)
            if bspec is not None:
                again = review(jev_key, outline, si, plan, report.get("facts"), usage, full=False)
                if again is None or not again["reject"]:
                    res["backup"] = True
                    res["notes"].append(f"Code Lab ({label}): the first chart was rejected, so the simpler backup chart was used. (Scores: {scores})")
                    return _accept(res, reply, plan, html, again["p"] if again else None)
                _rejected(res, label, "backup", plan, html, again["p"])
        res["notes"].append(f"Code Lab ({label}): the chart and its backup were both rejected, so the section stays text. (Scores: {scores})")
        return res
    if rv["backup"]:
        bpayload = {**section, "plan": {k: plan.get(k) for k in ("lesson", "backup", "title", "afterBlock")}}
        b = _ask(client, usage, "code lab backup", bpayload, effort=settings.fix, backup=True, think=settings.think, free=settings.free)
        if b is not None and b.get("build") is True:
            b, html, problems, report = _until_clean(client, usage, bpayload, b, None, res, backup=True, effort=settings.fix, think=settings.think, free=settings.free)
            if b is not None:
                again = review(jev_key, outline, si, plan, report.get("facts"), usage, full=False)
                if again is None or not again["reject"]:
                    res["backup"] = True
                    res["notes"].append(f"Code Lab ({label}): the first widget was rejected, so the simpler backup was used. (Scores: {scores})")
                    if look:
                        b, html = look_check(client, usage, bpayload, b, plan, html, res, settings, label, backup=True)
                    return _accept(res, b, plan, html, again["p"] if again else None)
                _rejected(res, label, "backup", plan, html, again["p"])
        res["notes"].append(f"Code Lab ({label}): the widget and its backup were both rejected, so the section stays text. (Scores: {scores})")
        return res
    res["notes"].append(f"Code Lab ({label}): the reviewer rejected the widget and saw no fix or better backup, so the section stays text. ({rv['problems'][0]} Scores: {scores})")
    return res


def _safe_design(client: anthropic.Anthropic, usage: UsageLog, outline: dict, si: int, verdict: dict, jev_key: str | None = None,
                 settings: CodeLabSettings = CodeLabSettings()) -> dict:
    try:
        return _design_one(client, usage, outline, si, verdict, jev_key, settings)
    except Exception:
        log.exception("Code Lab: section %s failed", si)
        label = outline["stages"][si].get("label", "")
        return {"notes": [f"Code Lab ({label}): something went wrong, so the section stays text."], "built": False, "repaired": 0, "backup": False, "chart": False}


def read_mark(raw: Any) -> dict | None:
    """Code Lab 4.0: a stage's "interaction" field as {"chart", "idea"}, or None when the writer left it out or it is
    malformed (that section then goes through Jev's gate, as in Code Lab 3.0)."""
    m = raw.get("interaction") if isinstance(raw, dict) else None
    if not isinstance(m, dict) or not isinstance(m.get("chart"), bool):
        return None
    idea = m.get("idea") if isinstance(m.get("idea"), str) else ""
    out = {"chart": m["chart"], "idea": re.sub(r"\s+", " ", idea).strip()[:300]}
    if m.get("kind") in MARK_KINDS:  # Playground's note (PLANNER_NOTE_PG) also says what kind of part it is
        out["kind"] = m["kind"]
    return out


def mark_hint(mark: dict) -> str | None:
    """The hint Haiku gets from a writer's mark: its idea, led by its kind when the writer gave one."""
    idea = mark.get("idea") or ""
    if mark.get("kind") and idea:
        return f"{mark['kind']}: {idea}"
    return idea or None


def _closed(label: str = "") -> dict:
    """A section that gets no widget and costs no call (the writer marked it as text)."""
    return {"notes": [], "built": False, "repaired": 0, "backup": False, "chart": False}


def _gate_and_design(client: anthropic.Anthropic, usage: UsageLog, outline: dict, si: int, jev_key: str | None,
                     settings: CodeLabSettings = CodeLabSettings(), mark: dict | None = None) -> dict:
    """One section start to finish: Jev's gate (or, in Code Lab 4.0, the writer's mark), then _design_one. Never raises."""
    if settings.marks and mark is not None:
        if not mark["chart"]:
            return _closed()
        return _safe_design(client, usage, outline, si, {"open": True, "why": None, "hint": mark_hint(mark)}, jev_key, settings)
    try:
        verdict, note = gate_one(jev_key, outline, si, usage, playground=settings.playground)
    except Exception:
        log.exception("Code Lab: gate for section %s failed", si)
        verdict, note = {"open": True, "why": None}, None
    res = _safe_design(client, usage, outline, si, verdict, jev_key, settings)
    if note:
        res["notes"].insert(0, note)
    return res


def _gate_safe(jev_key: str | None, outline: dict, si: int, usage: UsageLog, playground: bool = False) -> tuple[dict, str | None]:
    """gate_one that never raises (a failed gate opens the section, as in _gate_and_design)."""
    try:
        return gate_one(jev_key, outline, si, usage, playground=playground)
    except Exception:
        log.exception("Code Lab: gate for section %s failed", si)
        return {"open": True, "why": None}, None


def _design_gated(client: anthropic.Anthropic, usage: UsageLog, outline: dict, si: int, verdict: dict, note: str | None,
                  jev_key: str | None, settings: CodeLabSettings) -> dict:
    res = _safe_design(client, usage, outline, si, verdict, jev_key, settings)
    if note:
        res["notes"].insert(0, note)
    return res


def _done(res: dict) -> Future:
    f: Future = Future()
    f.set_result(res)
    return f


def _targets(outline: dict) -> list[int]:
    return [i for i, st in enumerate(outline["stages"]) if not any(b.get("type") == "widget" for b in st["blocks"])]


def _place_all(outline: dict, targets: list[int], results: list[dict], notes: list[str], stats: dict) -> dict:
    for si, res in zip(targets, results):
        notes.extend(res["notes"])
        stats["repaired"] += res["repaired"]
        stats["backup"] += 1 if res.get("backup") else 0
        stats["charts"] += 1 if res.get("widget") and res.get("chart") else 0
        if res.get("rejected"):  # only when the review rejected something (kept for test runs, debugRejected)
            stats.setdefault("rejected", []).extend(res["rejected"])
        if res.get("math"):  # Code Lab 4.0: each chart's maths review (scores and what happened), for test runs
            stats.setdefault("math", []).append(res["math"])
        if res.get("look"):  # Code Lab 7.0: what the screenshots showed and whether the repair was used, for test runs
            stats.setdefault("look", []).append({"section": outline["stages"][si].get("label", ""), **res["look"]})
        if res.get("widget"):
            outline = _place(outline, si, res["widget"], res.get("after"))
            stats["built"] += 1
    return outline


def design_code_widgets(client: anthropic.Anthropic, outline: dict, usage: UsageLog, notes: list[str], jev_key: str | None = None,
                        settings: CodeLabSettings = CodeLabSettings()) -> tuple[dict, dict]:
    """The outline with a Haiku-written widget in each section where Jev opened the gate, Haiku planned one and it passed
    (used when the page wasn't streamed, e.g. an added section). Returns (outline, stats): {"sections", "built", "repaired", "backup"}."""
    targets = _targets(outline)
    stats = {"sections": len(targets), "built": 0, "repaired": 0, "backup": 0, "charts": 0}
    if not targets:
        return outline, stats
    verdicts = gate(jev_key, outline, targets, usage, notes, playground=settings.playground)
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(targets))) as pool:
        results = list(pool.map(lambda si: _safe_design(client, usage, outline, si, verdicts[si], jev_key, settings), targets))
    return _place_all(outline, targets, results, notes, stats), stats


# ---------- Playground mode: one section's playground spec (shared/playground) ----------

PG_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-playground.mjs"
PG_TOKENS = 32_000  # streamed; most of a call is thinking, the spec itself is a few thousand tokens
PG_NO_PLAYGROUND = "no_playground"  # Haiku's answer when no playground can show the section honestly
_PG_STORED = ("title", "plan", "state", "init", "components")  # what the frame needs (afterBlock is only for placing it)


def playground_html(spec: dict) -> str:
    """The widget block's html for a playground: widget.ts sees XW.playground( and loads the runtime into the frame."""
    body = {k: spec[k] for k in _PG_STORED if k in spec}
    js = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    js = js.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return f'<div id="pg"></div><script>XW.playground({js})</script>'


def check_playground(spec: Any) -> dict:
    """{"problems", "facts", "layout"} from scripts/check-playground.mjs (the experiment's validator: shape, every snippet
    compiles, a dry run with no errors or NaN, every control changes state that a view shows). Fails closed: no node, a
    crash or unreadable output is a problem (CHECK_FAILED), so an unchecked playground never reaches a page. "layout"
    (only on a pass) lists soft on-screen problems: overlapping or cut-off shapes, cut labels, capped or off-range lines,
    empty space. Measured in every state the dry run reaches (a later-only problem ends with "after the controls move").
    _pg_fix_layout fixes what it can by code, then gives the rest one repair; they never fail a playground."""
    if not isinstance(spec, dict):
        return {"problems": ["the reply must be one JSON object (the playground spec)"], "facts": None}
    text = json.dumps(spec, ensure_ascii=False)
    size = js_len(playground_html(spec))  # the page drops a widget block over WIDGET_MAX_CHARS
    if size > WIDGET_MAX_CHARS:
        return {"problems": [f"the spec is too long ({size} characters, max {WIDGET_MAX_CHARS}): use fewer components, less "
                             "inline data (generate it in init) and shorter snippets"], "facts": None}
    node = _node()
    if not node or not PG_SCRIPT.exists():
        log.error("Playground: node or %s is missing, so playgrounds can't be checked", PG_SCRIPT.name)
        return {"problems": [CHECK_FAILED], "facts": None}
    for attempt in range(CHECK_RUNS):
        proc = None
        try:
            proc = run_node([node, str(PG_SCRIPT)], text, CHECK_TIMEOUT)
            result = json.loads(proc.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            return {"problems": ["checking it took too long: a snippet may loop forever; keep loops bounded"], "facts": None}
        except Exception:
            log.error("Playground: the check failed to run (attempt %d): %s", attempt + 1, node_failure(proc))
            continue
        if not isinstance(result, dict):
            continue
        if result.get("ok") is True:
            facts = result.get("facts")
            layout = [str(m) for m in result.get("layout") or [] if m][:10]
            return {"problems": [], "facts": facts if isinstance(facts, dict) else None, "layout": layout}
        errors = [str(e) for e in result.get("errors") or []]
        return {"problems": errors or ["the checker rejected it without saying why: keep it simple and standard"], "facts": None}
    return {"problems": [CHECK_FAILED], "facts": None}


def playground_plan(spec: dict) -> dict:
    """What we keep and what Jev reads about a playground, in Code Lab's plan words: lesson (the spec's insight),
    readerDoes, and its controls and visuals (so chart_summary and review_state work unchanged)."""
    p = spec.get("plan") if isinstance(spec.get("plan"), dict) else {}
    comps = [c for c in spec.get("components") or [] if isinstance(c, dict)]
    controls = []
    for c in comps:
        if c.get("type") in ("slider", "select", "toggle", "button", "play") and c.get("action") != "reset":
            ctl = {"id": str(c.get("id") or ""), "kind": c["type"], "label": str(c.get("label") or c.get("id") or "")[:80]}
            if c["type"] == "slider" and _num(c.get("min")) and _num(c.get("max")):
                ctl.update(min=c["min"], max=c["max"])
            controls.append(ctl)
        elif c.get("type") == "chart" and (c.get("onDrag") or c.get("onClick")):
            controls.append({"id": str(c.get("id") or ""), "kind": "drag on chart" if c.get("onDrag") else "click on chart",
                             "label": str(c.get("title") or c.get("id") or "")[:80]})
    visuals = [{"what": str(c.get("title") or c.get("label") or c.get("id") or "")[:120]} for c in comps if c.get("type") in ("chart", "readout")]
    after = spec.get("afterBlock")
    return {"lesson": str(p.get("insight") or "")[:600], "readerDoes": str(p.get("user_changes") or "")[:400],
            "mechanism": str(p.get("mechanism") or "")[:600], "reacts": str(p.get("reacts") or "")[:400],
            "title": str(spec.get("title") or "")[:80], "afterBlock": after if isinstance(after, int) and not isinstance(after, bool) else None,
            "controls": controls[:12], "visuals": visuals[:12], "backup": ""}


def _pg_ask(client: anthropic.Anthropic, usage: UsageLog, step: str, payload: dict, settings: CodeLabSettings, why: list[str],
            repair: dict | None = None, effort: str | None = None, edit: dict | None = None) -> Any:
    """One Haiku call: the section's playground spec (a first build, a repair or, with `edit`, a change the reader asked
    for to a finished playground: codelab_edit.py). None if the call failed or the reply has no JSON (the reason goes into `why`)."""
    system = (data.prompt("PLAYGROUND_SYSTEM") + ("\n\n" + data.prompt("PLAYGROUND_REPAIR") if repair else "")
              + ("\n\n" + data.prompt("PLAYGROUND_EDIT") if edit else ""))
    user = {**payload, **(repair or {}), **(edit or {})}
    try:
        text = write_text(client, usage, step, system=system, user=json.dumps(user, ensure_ascii=False), max_tokens=PG_TOKENS,
                          effort=effort or settings.build, model=HAIKU, stream=True, thinking=settings.think)
        spec, _ = extract_json(text)
    except Exception as e:
        log.exception("Playground: Haiku call failed (%s)", step)
        why.append(friendly_error(e))
        return None
    return spec


def _no_playground(spec: Any) -> bool:
    return isinstance(spec, dict) and spec.get("error") == PG_NO_PLAYGROUND


# Checker errors code can fix without a Haiku call (messages from shared/playground/validator.cjs). Seen Oct 24: a
# draggable point that started just below the chart, and a Step button whose onClick returned nothing; both cost the
# section its playground after the repairs.
_NUM = r"(-?[\d.]+(?:e[-+]?\d+)?)"
_PG_DRAG_OUT = re.compile(rf"^([\w-]+): draggable mark .*? starts at \({_NUM}, {_NUM}\), outside the chart range")
_PG_BAR_OUT = re.compile(rf"^([\w-]+): a bar has value {_NUM}, outside the chart's y range")
_PG_DEAD = re.compile(r"^([\w-]+) (?:never changes state|changes \[.*\] but no chart or readout depends on that)")


def _widen(rng: Any, v: float) -> list | None:
    """rng ([lo, hi]) grown to hold v with a 10% margin, or None when rng isn't a numeric range."""
    if not (isinstance(rng, list) and len(rng) == 2 and all(_num(x) for x in rng) and rng[0] < rng[1]):
        return None
    lo, hi = float(rng[0]), float(rng[1])
    pad = 0.1 * (max(hi, v) - min(lo, v))
    return [round(v - pad, 6) if v < lo else lo, round(v + pad, 6) if v > hi else hi]


def _pg_autofix(spec: dict, problems: list[str]) -> dict | None:
    """A copy of spec with the checker's mechanical problems fixed by code: a chart range widened to hold a draggable
    point or a bar, a control that changes nothing removed (only while another control stays). None if nothing applied."""
    out = json.loads(json.dumps(spec))
    comps = out.get("components") if isinstance(out.get("components"), list) else []
    by_id = {c.get("id"): c for c in comps if isinstance(c, dict)}
    # Counted like the validator does: a Reset button is not a control, so it can't be the one that "stays".
    controls = lambda: [c for c in comps if isinstance(c, dict) and (c.get("type") in ("slider", "select", "toggle", "play")  # noqa: E731
                                                                    or (c.get("type") == "button" and c.get("action") != "reset")
                                                                    or (c.get("type") == "chart" and (c.get("onDrag") or c.get("onClick"))))]
    changed = False
    for msg in problems:
        if m := _PG_DRAG_OUT.match(msg):
            c = by_id.get(m.group(1))
            if c is not None:
                x, y = _widen(c.get("x"), float(m.group(2))), _widen(c.get("y"), float(m.group(3)))
                if x and y:
                    c["x"], c["y"], changed = x, y, True
        elif m := _PG_BAR_OUT.match(msg):
            c = by_id.get(m.group(1))
            y = _widen(c.get("y"), float(m.group(2))) if c is not None else None
            if y:
                c["y"], changed = y, True
        elif m := _PG_DEAD.match(msg):
            c = by_id.get(m.group(1))
            if c is not None and c.get("type") in ("button", "play", "slider", "select", "toggle") and len(controls()) > 1:
                comps.remove(c)
                by_id.pop(m.group(1), None)
                changed = True
    return out if changed else None


# Layout problems code can fix in the spec itself (messages from layoutProblems in shared/playground/validator.cjs).
# Seen in 167 saved playgrounds (Oct 31): a y-axis name on heatmap cells, a mostly empty x or y range, a probability curve
# running along the top edge of [0, 1], a line just outside y. Text past the edge is fixed by the renderer (placeText).
_PG_HEAT_YLABEL = re.compile(r"^([\w-]+): the y-axis name '(.*)' is drawn over the heatmap's top-left cells")
_PG_EMPTY = re.compile(rf"^([\w-]+): the data never fills more than \d+% of the ([xy]) range \[{_NUM},{_NUM}\].*?narrow \2 to about \[{_NUM}, {_NUM}\]")
_PG_CAPPED = re.compile(rf"^([\w-]+): .*? sits on the top edge of the chart \(y = {_NUM}\)")
_PG_Y_OUT = re.compile(rf"^([\w-]+): .*? lies outside the y range \[{_NUM},{_NUM}\] for \d+ of \d+ points \(values {_NUM} to {_NUM}\)")
_PG_HEAT_OUT = re.compile(rf"^([\w-]+): the heatmap covers ([xy]) .*?set the chart's \2 to \[{_NUM},{_NUM}\]")
PG_TITLE_MAX = 60
PG_WIDEN_MAX = 3  # a line outside y is fitted only if y grows at most 3x; more would squash the rest (left to Haiku)


def _range(v: Any) -> list[float] | None:
    ok = isinstance(v, list) and len(v) == 2 and all(_num(x) for x in v) and v[0] < v[1]
    return [float(v[0]), float(v[1])] if ok else None


def _pg_layout_autofix(spec: dict, layout: list[str]) -> dict | None:
    """A copy of spec with the layout problems code can fix applied (see _PG_HEAT_YLABEL...), or None if none applied.
    The caller keeps it only if every check still passes and fewer layout problems are left."""
    out = json.loads(json.dumps(spec))
    comps = out.get("components") if isinstance(out.get("components"), list) else []
    charts = {c.get("id"): c for c in comps if isinstance(c, dict) and c.get("type") == "chart"}
    changed = False
    for msg in layout:
        if m := _PG_HEAT_YLABEL.match(msg):
            c = charts.get(m.group(1))
            if c is None or not c.get("yLabel"):
                continue
            name = str(c.pop("yLabel"))
            title = str(c.get("title") or "").strip()
            c["title"] = (f"{title} (rows: {name})" if title else f"Rows: {name}")[:PG_TITLE_MAX]
            changed = True
        elif m := _PG_EMPTY.match(msg):  # x or y
            c, axis = charts.get(m.group(1)), m.group(2)
            old = _range(c.get(axis)) if c is not None else None
            if old is None:
                continue
            lo, hi = float(m.group(5)), float(m.group(6))
            if old[0] >= 0 > lo:  # a range that started at 0 or above doesn't get negative space
                lo = old[0]
            if lo < hi:
                c[axis], changed = [round(lo, 6), round(hi, 6)], True
        elif m := _PG_CAPPED.match(msg):
            c = charts.get(m.group(1))
            y = _range(c.get("y")) if c is not None else None
            # only probabilities and percentages that really reach 1 or 100; a clamped value stays with Haiku
            if y and y[1] == float(m.group(2)) and y[1] in (1.0, 100.0) and y[0] >= 0:
                c["y"], changed = [y[0], round(y[1] + 0.05 * (y[1] - y[0]), 6)], True
        elif m := _PG_Y_OUT.match(msg):
            c = charts.get(m.group(1))
            y = _range(c.get("y")) if c is not None else None
            new = _widen(y, float(m.group(4))) if y else None
            new = _widen(new, float(m.group(5))) if new else None
            if y and new and new[1] - new[0] <= PG_WIDEN_MAX * (y[1] - y[0]):
                c["y"], changed = new, True
        elif m := _PG_HEAT_OUT.match(msg):
            c = charts.get(m.group(1))
            lo, hi = float(m.group(3)), float(m.group(4))
            if c is not None and lo < hi:
                c[m.group(2)], changed = [lo, hi], True
    return out if changed else None


def _pg_until_clean(client: anthropic.Anthropic, usage: UsageLog, payload: dict, spec: Any, res: dict, settings: CodeLabSettings,
                    why: list[str]) -> tuple[dict | None, dict]:
    """(spec, report) once the checker passes it, repairing with its exact errors up to MAX_REPAIRS times; (None, report)
    if it still fails. A repair that answers {"error": "no_playground"} ends it too. Mechanical problems (_pg_autofix)
    are fixed by code first, before any repair call."""
    def by_code(spec: Any, report: dict) -> tuple[Any, dict]:
        if not report["problems"] or report["problems"] == [CHECK_FAILED] or not isinstance(spec, dict):
            return spec, report
        fixed = _pg_autofix(spec, report["problems"])
        if fixed is None:
            return spec, report
        again = check_playground(fixed)
        return (spec, report) if again["problems"] == [CHECK_FAILED] else (fixed, again)

    spec, report = by_code(spec, check_playground(spec))
    for _ in range(MAX_REPAIRS):
        if not report["problems"]:
            break
        if report["problems"] == [CHECK_FAILED]:  # the checker itself can't run: a repair can't help
            break
        res["repaired"] += 1
        fixed = _pg_ask(client, usage, "playground repair", payload, settings, why,
                        {"yourPreviousSpec": spec, "problems": report["problems"]}, effort=settings.fix)
        if fixed is None or _no_playground(fixed):
            break
        spec, report = by_code(fixed, check_playground(fixed))
    if report["problems"]:
        return None, report
    return _pg_fix_layout(client, usage, payload, spec, report, res, settings, why)


def _pg_fix_layout(client: anthropic.Anthropic, usage: UsageLog, payload: dict, spec: dict, report: dict, res: dict,
                   settings: CodeLabSettings, why: list[str]) -> tuple[dict, dict]:
    """A working playground with on-screen layout problems: code fixes what it can first (_pg_layout_autofix), then the
    rest (overlapping shapes, cut labels...) gets LAYOUT_REPAIRS Haiku tries. Each step is kept only if it still passes
    every check and has fewer layout problems; otherwise the version before stays, so a layout problem never costs a
    working playground."""
    seen = report.get("layout") or []
    if seen:
        res.setdefault("layout_seen", []).extend(seen)  # first-draft messages, for test-run stats
        fixed = _pg_layout_autofix(spec, seen)
        again = check_playground(fixed) if fixed is not None else None
        if again is not None and not again["problems"] and len(again.get("layout") or []) < len(seen):
            res["layout_code_fixed"] = res.get("layout_code_fixed", 0) + 1
            spec, report = fixed, again
        elif again is not None:
            log.info("Playground: layout code fix not kept (%d layout problems before, %s after)", len(seen),
                     "a failed check" if again["problems"] else len(again.get("layout") or []))
    for _ in range(LAYOUT_REPAIRS):
        layout = report.get("layout") or []
        if not layout:
            break
        res["layout_repairs"] = res.get("layout_repairs", 0) + 1
        fixed = _pg_ask(client, usage, "playground layout", payload, settings, why,
                        {"yourPreviousSpec": spec, "problems": [LAYOUT_LEAD, *layout]}, effort=settings.fix)
        if fixed is None or _no_playground(fixed) or not isinstance(fixed, dict):
            break
        again = check_playground(fixed)
        if again["problems"] or len(again.get("layout") or []) >= len(layout):
            log.info("Playground: layout repair not kept (%d layout problems before, %s after)", len(layout),
                     "a failed check" if again["problems"] else len(again.get("layout") or []))
            break
        res["layout_fixed"] = res.get("layout_fixed", 0) + 1
        spec, report = fixed, again
    return spec, report


def _control_labels(facts: dict | None) -> list[str]:
    return sorted(str(c.get("label", "")) for c in (facts or {}).get("controls") or [] if isinstance(c, dict))


def _pg_look(client: anthropic.Anthropic, usage: UsageLog, payload: dict, spec: dict, plan: dict, res: dict,
             settings: CodeLabSettings, why: list[str], label: str) -> tuple[dict, dict]:
    """Playground's screenshot check (settings.look, PG_LOOK): the playground about to be kept is screenshotted like Code
    Lab 7.0's look_check and Haiku lists what looks wrong, told what the layout check already measured. Problems get ONE
    repair, kept only if it passes every check, has no more layout problems, keeps the same controls (so Jev's verdict
    still holds) and its screenshots show fewer problems. Fails open: no frames or no judge answer = kept as it is."""
    if not settings.look:
        return spec, plan
    frames = snap_widget(playground_html(spec))
    if not frames:
        res["look"] = {"checked": False}
        return spec, plan
    title = str(spec.get("title") or "")
    problems = look_review(client, usage, frames, plan, title, checked=PG_LOOK_CHECKED, step="playground look")
    look = {"checked": problems is not None, "frames": len(frames), "problems": problems or [], "repaired": False}
    res["look"] = look
    if not problems:
        return spec, plan
    res["repaired"] += 1
    fixed = _pg_ask(client, usage, "playground look repair", payload, settings, why,
                    {"yourPreviousSpec": spec, "problems": [PG_LOOK_LEAD, *[f"How it looks (from screenshots): {p}" for p in problems]]},
                    effort=settings.fix)
    if not isinstance(fixed, dict) or _no_playground(fixed):
        res["notes"].append(f"Playground ({label}): the screenshots showed problems and the repair failed, so it is kept as it was. ({problems[0]})")
        return spec, plan
    before, after_check = check_playground(spec), check_playground(fixed)
    same_controls = _control_labels(before.get("facts")) == _control_labels(after_check.get("facts"))
    if after_check["problems"] or len(after_check.get("layout") or []) > len(before.get("layout") or []) or not same_controls:
        why_not = "it failed a check" if after_check["problems"] else "it changed the controls" if not same_controls else "it had more layout problems"
        res["notes"].append(f"Playground ({label}): the screenshots showed problems but the repair wasn't kept ({why_not}). ({problems[0]})")
        return spec, plan
    frames2 = snap_widget(playground_html(fixed))
    after = look_review(client, usage, frames2, plan, title, checked=PG_LOOK_CHECKED, step="playground look") if frames2 else None
    look["after"] = after
    if after is None or len(after) >= len(problems):
        res["notes"].append(f"Playground ({label}): the screenshots showed problems and the repair didn't look better, so it is kept as it was. ({problems[0]})")
        return spec, plan
    look["repaired"] = True
    res["notes"].append(f"Playground ({label}): the screenshots showed {len(problems)} problem(s); the repaired playground shows {len(after)}, so it was used.")
    return fixed, playground_plan(fixed)


def _pg_accept(res: dict, spec: dict, plan: dict, scores: dict | None = None) -> dict:
    title = re.sub(r"\s+", " ", str(spec.get("title") or "")).strip()[:80] or "Playground"
    res.update(widget={"type": "widget", "title": title, "html": playground_html(spec), "plan": plan_text(plan, scores)},
               after=plan.get("afterBlock"), built=True)
    return res


def _design_playground(client: anthropic.Anthropic, usage: UsageLog, outline: dict, si: int, verdict: dict, jev_key: str | None,
                       settings: CodeLabSettings) -> dict:
    """One section's playground: Haiku writes the spec, the checker dry-runs it (repairs with its errors), then Jev's
    review (one fix on a clear no, re-checked and re-reviewed). Falls back to text, with a note, at any step."""
    label = outline["stages"][si].get("label", "")
    res: dict = {"notes": [], "built": False, "repaired": 0, "backup": False, "chart": False}
    if not verdict.get("open"):
        return res
    why: list[str] = []
    payload = {**_section(outline, si), "hint": verdict.get("hint") or gate_hint(verdict.get("why")) or ""}
    spec = _pg_ask(client, usage, "playground write", payload, settings, why)
    lower = _lower(settings.build)
    if spec is None and why and CUTOFF in why[-1] and lower:  # the whole budget went on thinking: once more, thinking less
        why.clear()
        spec = _pg_ask(client, usage, "playground write", payload, settings, why, effort=lower)
    if spec is None:
        res["notes"].append(f"Playground ({label}): the builder failed, so the section stays text. ({why[0] if why else 'no answer'})")
        return res
    if _no_playground(spec):
        res["notes"].append(f"Playground ({label}): the builder found nothing to play with here, so the section stays text.")
        return res
    spec, report = _pg_until_clean(client, usage, payload, spec, res, settings, why)
    if spec is None and report["problems"] == [CHECK_FAILED]:
        res["notes"].append(f"Playground ({label}): the playground was written, but the server couldn't run its checker (node), so it "
                            "wasn't shown and the section stays text. The server log says why.")
        return res
    if spec is None:
        res["notes"].append(f"Playground ({label}): the playground failed its checks and couldn't be repaired, so the section stays text. "
                            f"({(report['problems'] or ['no answer'])[0]})")
        return res
    plan = playground_plan(spec)
    rv = review(jev_key, outline, si, plan, report.get("facts"), usage, fix_first=True, playground=True)
    if rv is None or not rv["reject"]:
        spec, plan = _pg_look(client, usage, payload, spec, plan, res, settings, why, label)
        return _pg_accept(res, spec, plan, rv["p"] if rv else None)
    _rejected(res, label, "first", plan, playground_html(spec), rv["p"])
    scores = scores_text(rv["p"])
    res["repaired"] += 1
    fixed = _pg_ask(client, usage, "playground repair", payload, settings, why,
                    {"yourPreviousSpec": spec, "problems": rv["problems"]}, effort=settings.fix)
    if fixed is not None and not _no_playground(fixed):
        spec2, report2 = _pg_until_clean(client, usage, payload, fixed, res, settings, why)
        if spec2 is not None:
            plan2 = playground_plan(spec2)
            again = review(jev_key, outline, si, plan2, report2.get("facts"), usage, full=False, playground=True)
            if again is None or not again["reject"]:
                spec2, plan2 = _pg_look(client, usage, payload, spec2, plan2, res, settings, why, label)
                return _pg_accept(res, spec2, plan2, again["p"] if again else rv["p"])
            _rejected(res, label, "fix", plan2, playground_html(spec2), again["p"])
    res["notes"].append(f"Playground ({label}): the reviewer rejected the playground and its fix didn't pass, so the section stays text. "
                        f"({rv['problems'][0]} Scores: {scores})")
    return res


# ---------- starting sections while the page text streams ----------


class StageScanner:
    """Reads the planner's JSON reply as it streams and returns each stage object's source text as soon as it is
    complete (the objects in the top-level "stages" array). Also remembers the page's title and level if they come first."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.text = ""
        self.pos = 0
        self.depth = 0
        self.in_str = False
        self.esc = False
        self.str_start = 0
        self.key = ""
        self.after_colon = False
        self.stages_depth = 0  # depth inside the stages array, 0 = not in it
        self.done = False  # the stages array has closed
        self.start = -1  # where the current stage object began
        self.head: dict[str, str] = {}

    def feed(self, chunk: str) -> list[str]:
        self.text += chunk
        out: list[str] = []
        t = self.text
        while self.pos < len(t):
            c = t[self.pos]
            if self.in_str:
                if self.esc:
                    self.esc = False
                elif c == "\\":
                    self.esc = True
                elif c == '"':
                    self.in_str = False
                    if self.depth == 1:
                        self._top_string(t[self.str_start:self.pos + 1])
            elif c == '"':
                self.in_str = True
                self.str_start = self.pos
            elif c in "{[":
                if c == "[" and self.depth == 1 and self.after_colon and self.key == "stages" and not self.done:
                    self.stages_depth = 2
                if c == "{" and self.stages_depth and self.depth == self.stages_depth:
                    self.start = self.pos
                self.depth += 1
            elif c in "}]":
                self.depth -= 1
                if c == "}" and self.stages_depth and self.depth == self.stages_depth and self.start >= 0:
                    out.append(t[self.start:self.pos + 1])
                    self.start = -1
                elif c == "]" and self.stages_depth and self.depth < self.stages_depth:
                    self.stages_depth = 0
                    self.done = True
            elif self.depth == 1 and c == ":":
                self.after_colon = True
            elif self.depth == 1 and c == ",":
                self.after_colon = False
            self.pos += 1
        return out

    def _top_string(self, src: str) -> None:
        try:
            s = json.loads(src)
        except ValueError:
            return
        if self.after_colon:
            if self.key in ("title", "level") and isinstance(s, str):
                self.head[self.key] = s
            self.after_colon = False
        else:
            self.key = s


def _stage_key(st: dict) -> str:
    """What a section's widget depends on, to tell whether an early start still matches the final page."""
    return json.dumps({"label": st.get("label"), "blurb": st.get("blurb"), "blocks": st.get("blocks")}, sort_keys=True, ensure_ascii=False)


class Streamed:
    """Code Lab for a new page: each section's gate, plan, build and review start as soon as the section is complete in
    the planner's stream (feed), instead of after the whole page. finish() reuses each early start whose section came
    out the same in the final outline and runs the rest. Feeding never raises, so the planner is never disturbed."""

    def __init__(self, client: anthropic.Anthropic, usage: UsageLog, jev_key: str | None, settings: "CodeLabSettings | None" = None) -> None:
        self.client, self.usage, self.jev_key = client, usage, jev_key
        self.settings = settings or CodeLabSettings()
        self.scanner = StageScanner()
        self.pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self.index = 0  # the next stage's index in the current stream
        # si -> (stage key, future, kind): "design" runs the whole section; "gate" only Jev's gate (Code Lab 7.0/Playground
        # sections the writer didn't mark: finish() builds only as many of them as the page has room for)
        self.early: dict[int, tuple[str, Future, str]] = {}
        self.marks: dict[str, dict] = {}  # Code Lab 4.0: the writer's chart mark per section label (read_mark)
        self.closed = False

    def feed(self, chunk: str | None) -> None:
        if self.closed:
            return
        try:
            if chunk is None:
                self.scanner.reset()
                self.index = 0
                self.marks = {}
                return
            for src in self.scanner.feed(chunk):
                si = self.index
                self.index += 1
                self._start(si, src)
        except Exception:
            log.exception("Code Lab: reading the streamed outline failed")

    def _mark(self, label: str, raw: Any) -> dict | None:
        """Code Lab 4.0: the writer's mark for this section, with at most MAX_MARKED charts on the page (later marks
        past the limit become text). None when the writer left it out (the section then goes through Jev's gate)."""
        if not self.settings.marks:
            return None
        m = read_mark(raw)
        if m and m["chart"] and sum(1 for k, v in self.marks.items() if k != label and v["chart"]) >= MAX_MARKED:
            m = {"chart": False, "idea": m["idea"], "capped": True}
        if m is not None:
            self.marks[label] = m
        return m

    def _start(self, si: int, src: str) -> None:
        raw = json_parse(src)
        parsed = parse_outline({"title": self.scanner.head.get("title", ""), "level": self.scanner.head.get("level", ""), "stages": [raw]})
        if "outline" not in parsed:
            return
        mini = strip_prebuilt(parsed["outline"])
        st = mini["stages"][0]
        mark = self._mark(st.get("label", ""), raw)
        key = _stage_key(st)
        kind = "gate" if self._gate_first() and mark is None else "design"
        old = self.early.get(si)
        if old and old[0] == key and old[2] == kind:
            return  # a new stream wrote the same section again: keep the run already going
        if old:
            old[1].cancel()
        if kind == "gate":
            self.early[si] = (key, self.pool.submit(_gate_safe, self.jev_key, mini, 0, self.usage, self.settings.playground), kind)
        else:
            self.early[si] = (key, self.pool.submit(_gate_and_design, self.client, self.usage, mini, 0, self.jev_key, self.settings, mark), kind)

    def _gate_first(self) -> bool:
        """Code Lab 7.0/Playground: an unmarked section only runs Jev's gate until the page's room is known."""
        return self.settings.marks

    def _build_gated(self, outline: dict, targets: list[int], gates: dict[int, Future]) -> tuple[dict[int, Future], list[tuple[int, dict, str | None]]]:
        """Code Lab 7.0/Playground: build only as many gate-opened (unmarked) sections as the page has room for after the
        writer's marks (MAX_MARKED parts in all), never a recap, clearest gate answers first. Before, every open section
        was built and _cap_unmarked threw the extras away after paying for them. Returns the started sections and the
        waiting ones (in gate order), which finish() builds only if a part failed (_refill)."""
        label = lambda si: outline["stages"][si].get("label", "")  # noqa: E731
        marked = sum(1 for si in targets if (self.marks.get(label(si)) or {}).get("chart"))
        room = max(MAX_MARKED - marked, 0)
        verdicts: dict[int, tuple[dict, str | None]] = {}
        for si, f in gates.items():
            try:
                verdicts[si] = f.result()
            except Exception:  # cancelled: run the gate again (one cheap Jev call)
                verdicts[si] = _gate_safe(self.jev_key, outline, si, self.usage, self.settings.playground)
        open_ = [si for si, (v, _) in verdicts.items() if v.get("open") and not _RECAP_LABEL.search(label(si))]
        ranked = sorted(open_, key=lambda si: (-verdicts[si][0].get("score", 0.5), si))
        chosen = set(ranked[:room])
        out: dict[int, Future] = {}
        for si, (v, note) in verdicts.items():
            if si in chosen:
                out[si] = self.pool.submit(_design_gated, self.client, self.usage, outline, si, v, note, self.jev_key, self.settings)
            elif not v.get("open") or _RECAP_LABEL.search(label(si)):
                section_notes = [note] if note else []
                if v.get("open"):
                    section_notes.append(f"Code Lab ({label(si)}): this is a recap section, so it stays text.")
                out[si] = _done({"notes": section_notes, "built": False, "repaired": 0, "backup": False, "chart": False})
        return out, [(si, verdicts[si][0], verdicts[si][1]) for si in ranked[room:]]

    def _refill(self, outline: dict, futures: dict[int, Future], waiting: list[tuple[int, dict, str | None]],
                progress: Callable[[str], None] | None) -> None:
        """The cap counts kept parts, not tries: while the page has fewer than MAX_MARKED widgets and gate-opened
        sections are waiting, build the next ones (Oct 24: a rejected or failed part still used one of the 4 places,
        so 5 open sections on 5 pages stayed text while those pages kept only about 2 parts). The rest stay text."""
        def kept() -> int:
            n = 0
            for f in futures.values():
                try:
                    n += 1 if f.result().get("widget") else 0
                except Exception:
                    pass
            return n

        while waiting and (need := MAX_MARKED - kept()) > 0:
            batch, waiting[:] = waiting[:need], waiting[need:]
            if progress:
                progress(f"building {len(batch)} more section{'s' if len(batch) > 1 else ''}")
            started = [self.pool.submit(_design_gated, self.client, self.usage, outline, si, v, note, self.jev_key, self.settings)
                       for si, v, note in batch]
            for (si, _, _), f in zip(batch, started):
                futures[si] = f
            for _ in as_completed(started):
                pass
        for si, _, note in waiting:
            label = outline["stages"][si].get("label", "")
            futures[si] = _done({"notes": ([note] if note else []) + [f"Code Lab ({label}): the page already has its {MAX_MARKED} interactive parts, so the section stays text."],
                                 "built": False, "repaired": 0, "backup": False, "chart": False})

    def finish(self, outline: dict, notes: list[str], progress: Callable[[str], None] | None = None) -> tuple[dict, dict]:
        """The outline with its widgets placed (like design_code_widgets). progress gets "k of n sections done"."""
        self.closed = True
        targets = _targets(outline)
        stats = {"sections": len(targets), "built": 0, "repaired": 0, "backup": 0, "charts": 0, "early": 0}
        futures: dict[int, Future] = {}
        gates: dict[int, Future] = {}  # Code Lab 7.0/Playground: unmarked sections, gate only for now
        for si in targets:
            mark = self.marks.get(outline["stages"][si].get("label", ""))
            kind = "gate" if self._gate_first() and mark is None else "design"
            old = self.early.get(si)
            if old and old[0] == _stage_key(outline["stages"][si]) and old[2] == kind:
                (gates if kind == "gate" else futures)[si] = old[1]
                stats["early"] += 1
            else:
                if old:
                    old[1].cancel()
                if kind == "gate":
                    gates[si] = self.pool.submit(_gate_safe, self.jev_key, outline, si, self.usage, self.settings.playground)
                else:
                    futures[si] = self.pool.submit(_gate_and_design, self.client, self.usage, outline, si, self.jev_key, self.settings, mark)
        kept = {id(f) for f in futures.values()} | {id(f) for f in gates.values()}
        for _, f, _ in self.early.values():
            if id(f) not in kept:
                f.cancel()
        waiting: list[tuple[int, dict, str | None]] = []
        if gates:
            started, waiting = self._build_gated(outline, targets, gates)
            futures.update(started)
        if targets and not self.jev_key:
            notes.append("Code Lab: no Jev key, so every section went to the plan step.")
        log.info("Code Lab: %d of %d sections started while the page text streamed", stats["early"], len(targets))
        done = 0
        for _ in as_completed(futures.values()):
            done += 1
            if progress and done < len(futures):
                progress(f"{done} of {len(futures)} sections done")
        self._refill(outline, futures, waiting, progress)
        self.pool.shutdown(wait=False, cancel_futures=True)
        results = []
        for si in targets:
            try:
                results.append(futures[si].result())
            except Exception:  # cancelled or crashed: _gate_and_design itself never raises
                results.append({"notes": [f'Code Lab ({outline["stages"][si].get("label", "")}): something went wrong, so the section stays text.'],
                                "built": False, "repaired": 0, "backup": False, "chart": False})
        if self.settings.marks:  # Code Lab 7.0/Playground: the page-level budget and repeat check, and what the writer marked
            try:
                _cap_unmarked(outline, targets, results, self.marks)
            except Exception:
                log.exception("Code Lab: capping unmarked sections failed")
            try:
                _drop_duplicates(self.jev_key, outline, targets, results, self.usage)
            except Exception:
                log.exception("Code Lab: the duplicate audit failed")
            stats["marks"] = dict(self.marks)
            if not self.marks:
                notes.append("Code Lab: the writer marked no sections, so Jev's gate chose them.")
        return _place_all(outline, targets, results, notes, stats), stats


def chart_summary(widget: dict) -> dict:
    """What Jev reads about a finished chart (Code Lab 4.0's duplicate audit): its title, lesson, controls (from the
    spec's params) and axis and legend titles. Built by code from the chart, never the raw spec."""
    html = str(widget.get("html") or "")
    spec = None
    i = html.find("XW.vega(")
    if i >= 0:
        start, end = html.find("{", i), html.rfind(")</script>")
        try:
            spec = json.loads(html[start:end].replace("<\\/", "</")) if 0 <= start < end else None
        except ValueError:
            spec = None
    titles: list[str] = []

    def walk(v: Any, key: str = "") -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "title" and isinstance(x, str) and key in ("x", "y", "color", "x2", "y2", "theta", "size", "axes", "legends") and x.strip():
                    titles.append(x.strip())
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)

    walk(spec)
    try:
        plan = json.loads(widget.get("plan") or "{}")
    except ValueError:
        plan = {}
    plan = plan if isinstance(plan, dict) else {}
    lesson = plan.get("lesson", "")
    if spec is not None:
        controls = controls_from_spec(spec)
    else:
        # Code Lab 5.0's own code: no spec to read, so the plan it was built and checked against (its controls and
        # what each visual draws) says what the reader moves and sees.
        controls = [c for c in plan.get("controls") or [] if isinstance(c, dict) and isinstance(c.get("label"), str)]
        titles = [str(v["what"])[:120] for v in plan.get("visuals") or [] if isinstance(v, dict) and isinstance(v.get("what"), str) and v["what"].strip()]

    def rng(c: dict) -> str:
        lo, hi = c.get("min"), c.get("max")
        ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lo, hi))
        return f" ({lo:g} to {hi:g})" if ok else ""

    return {"title": str(widget.get("title") or ""), "lesson": str(lesson or ""),
            "controls": [f'{c["label"]}{rng(c)}' for c in controls],
            "axes": list(dict.fromkeys(titles))[:8]}


def _same_question(a: str, b: str) -> dict:
    return {
        "type": "noul",
        "instructions": f"`charts.{b}` shows the same quantity changing with the same kind of setting as `charts.{a}`.",
        "criteria": {
            "true": f"`charts.{a}` and `charts.{b}` plot the same quantity (their lessons and axes are about the same thing) and the reader moves the same kind of setting.",
            "false": f"`charts.{a}` and `charts.{b}` plot different quantities, or the reader moves a different kind of setting.",
        },
    }


def audit_duplicates(jev_key: str | None, labels: list[str], widgets: list[dict], usage: UsageLog) -> list[tuple[int, int]]:
    """Code Lab 4.0: (later, earlier) index pairs where Jev clearly says the later chart repeats the earlier one
    (p >= REVIEW_HIGH). One Jev call with one literal question per pair. No key, one chart, or a Jev error: nothing."""
    if not jev_key or len(widgets) < 2:
        return []
    names = [f"c{i + 1}" for i in range(len(widgets))]
    state = {"charts": {n: {"section": labels[i], **chart_summary(w)} for i, (n, w) in enumerate(zip(names, widgets))}}
    pairs = [(a, b) for b in range(len(widgets)) for a in range(b)]
    questions = {f"same_{names[a]}_{names[b]}": _same_question(names[a], names[b]) for a, b in pairs}
    try:
        r = jev_ask(jev_key, state, questions)
    except JevError:
        return []
    usage.add("jev code lab duplicates", r["model"], {"input_tokens": r["usage"].get("input_tokens", 0), "output_tokens": 0})
    out: list[tuple[int, int]] = []
    dropped: set[int] = set()
    for a, b in pairs:
        ans = r["answers"].get(f"same_{names[a]}_{names[b]}")
        if b in dropped or a in dropped or not isinstance(ans, dict):
            continue
        if float(ans.get("p", 0)) >= REVIEW_HIGH:
            out.append((b, a))
            dropped.add(b)
    return out


def _drop_duplicates(jev_key: str | None, outline: dict, targets: list[int], results: list[dict], usage: UsageLog) -> None:
    """Code Lab 4.0: after every section is done, ask Jev which finished charts repeat an earlier one and turn those
    sections back into text (the dropped chart is kept for test runs as a "duplicate")."""
    kept = [k for k, res in enumerate(results) if res.get("widget")]
    labels = [outline["stages"][targets[k]].get("label", "") for k in kept]
    for later, earlier in audit_duplicates(jev_key, labels, [results[k]["widget"] for k in kept], usage):
        res = results[kept[later]]
        w = res.pop("widget")
        res["built"] = False
        _rejected(res, labels[later], "duplicate", {"title": w.get("title", ""), "lesson": chart_summary(w)["lesson"]}, w.get("html", ""),
                  {"repeats": labels[earlier]})
        res["notes"].append(f'Code Lab ({labels[later]}): its chart repeated the one in "{labels[earlier]}", so the section stays text.')


_RECAP_LABEL = re.compile(r"\b(?:intro(?:duction)?|overview|motivation|summary|recap|cheat[\s-]?sheet|wrap[\s-]?up|putting it (?:all )?together|takeaways?|conclusion)\b", re.I)


def _review_score(widget: dict) -> float:
    """How clearly the reviewer liked a finished widget: its lowest quality score (0.5 when it wasn't reviewed)."""
    try:
        p = (json.loads(widget.get("plan") or "{}") or {}).get("review") or {}
    except (ValueError, AttributeError):
        p = {}
    scores = [float(p[k]) for k in QUALITY if isinstance(p.get(k), (int, float))]
    return min(scores) if scores else 0.5


def _cap_unmarked(outline: dict, targets: list[int], results: list[dict], marks: dict[str, dict]) -> None:
    """Code Lab 7.0/Playground: sections the writer didn't mark went through Jev's gate, which opens far more of them (seen: a
    page with no marks got 5 charts, 3 about the same beta). Keep the page within MAX_MARKED: never on an intro or
    summary, and the best-reviewed first. The rest become text (kept for test runs as "capped")."""
    built = [k for k, res in enumerate(results) if res.get("widget")]
    label = lambda k: outline["stages"][targets[k]].get("label", "")  # noqa: E731
    unmarked = [k for k in built if label(k) not in marks]
    room = MAX_MARKED - (len(built) - len(unmarked))
    drop = [k for k in unmarked if _RECAP_LABEL.search(label(k))]
    rest = sorted((k for k in unmarked if k not in drop), key=lambda k: -_review_score(results[k]["widget"]))
    drop += rest[max(room, 0):]
    for k in drop:
        res = results[k]
        w = res.pop("widget")
        res["built"] = False
        _rejected(res, label(k), "capped", {"title": w.get("title", ""), "lesson": chart_summary(w)["lesson"]}, w.get("html", ""), {})
        res["notes"].append(f"Code Lab ({label(k)}): the page already has its {MAX_MARKED} interactive parts"
                            + (" and this is a recap section" if _RECAP_LABEL.search(label(k)) else "") + ", so the section stays text.")


def strip_prebuilt(outline: dict) -> dict:
    """The planner's outline without prebuilt interactive blocks (a safety net for the PLANNER_NOTE)."""
    return without_blocks(outline, PREBUILT)
