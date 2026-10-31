"""Code Lab mode (code-lab, codelab.py): Jev gates each section, Haiku plans the interaction, then Haiku writes its code,
which is checked by running it (static rules, then scripts/check-widget.mjs), compared with the plan and repaired, then
reviewed by Jev (keep, repair, backup or text). Sections start while the page text streams.
Claude and Jev are faked; the node checker is real."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from explainer import codelab, pipeline
from explainer.core.validate import assemble_spec
from explainer.llm import ReaderError
from explainer.pricing import UsageLog
from explainer.requests import parse_generate

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample.json").read_text(encoding="utf-8"))
needs_node = pytest.mark.skipif(not shutil.which("node") or not codelab.SCRIPT.exists(), reason="node not available")
try:  # the runtime check needs jsdom (a devDependency)
    import subprocess as _sp
    _jsdom = shutil.which("node") and _sp.run(["node", "-e", "require.resolve('jsdom')"], cwd=codelab.SCRIPT.parent, capture_output=True).returncode == 0
except Exception:
    _jsdom = False
needs_jsdom = pytest.mark.skipif(not _jsdom, reason="jsdom not installed")

GOOD = ('<label for=a>Amount</label><input id=a type=range min=0 max=10 value=3><span id=o></span>'
        '<script>const a=document.getElementById("a"),o=document.getElementById("o");'
        'function f(){o.textContent=a.value*2}a.addEventListener("input",f);f();</script>')
CURLY = GOOD.replace('"a"', "“a”")
DEAD = '<input id=a type=range><span>hi</span><script>var x = 1;</script>'
CANVAS = ('<input id=a type=range min=0 max=10 value=3><canvas id=c></canvas>'
          '<script>const a=document.getElementById("a"),c=document.getElementById("c").getContext("2d");'
          'function f(){c.clearRect(0,0,10,10);c.fillRect(0,0,a.value,5)}a.addEventListener("input",f);f();</script>')


def outline(*stages: list[dict]) -> dict:
    return {"title": "Averaging", "subtitle": "", "level": "student", "layout": "story", "keyIntuition": "k",
            "phases": [{"id": "p", "label": "P"}], "glossary": [],
            "stages": [{"id": f"s{i}", "label": f"S{i}", "phase": "p", "blurb": "", "blocks": b} for i, b in enumerate(stages)]}


TEXT = {"type": "text", "md": "More samples make the average steadier."}
QUIZ = {"type": "quiz", "questions": []}


def reply(html: str = GOOD, **kw) -> str:
    return json.dumps({"build": True, "why": "w", "title": "Average steadier", "afterBlock": 0, "html": html, **kw})


def plan(**kw) -> str:
    """A plan whose ids match GOOD (control a, visual o)."""
    return json.dumps({
        "build": True, "lesson": "more samples, steadier average", "readerDoes": "drags the amount", "title": "Average steadier", "afterBlock": 0,
        "controls": [{"id": "a", "kind": "slider", "label": "Amount", "min": 0, "max": 10, "step": 1, "value": 3}],
        "visuals": [{"id": "o", "kind": "readout", "what": "twice the amount"}],
        "expects": [{"control": "a", "changes": ["o"]}], "contrast": {"items": [], "part": ""},
        "backup": "one slider for the amount and a readout of twice it", **kw,
    })


NO_PLAN = json.dumps({"build": False, "lesson": "", "readerDoes": "", "title": "", "afterBlock": -1, "controls": [], "visuals": [], "expects": [],
                      "contrast": {"items": [], "part": ""}, "backup": ""})


def fake_writer(monkeypatch, replies: list[str], plans: list[str] | None = None) -> list[dict]:
    """Haiku plan calls return the next plan; build and repair calls return the next reply. Records (step, sent payload)."""
    sent: list[dict] = []
    plan_it = iter(plans if plans is not None else [plan()] * 20)
    build_it = iter(replies)

    def write_text(client, usage, step, *, system, user, max_tokens, **kw):
        assert kw["model"] == "claude-haiku-5-5" and kw.get("fmt")
        is_plan = step == "code lab plan"
        assert kw["effort"] == ("high" if step in ("code lab plan", "code lab write") else "medium")  # repairs and the backup think less
        assert kw.get("stream", False) == (not is_plan)  # builds are streamed, so the token cap can exceed what a plain call allows
        assert ("plan the interactive widget" in system) == is_plan
        assert ("You are the programmer" in system) == (not is_plan)
        assert ("was checked, and it failed" in system) == (step == "code lab repair")
        if step in ("code lab write", "code lab backup"):
            assert ("build the backup" in system) == (step == "code lab backup")
        sent.append({"step": step, "user": json.loads(user)})
        return next(plan_it if is_plan else build_it)

    monkeypatch.setattr(codelab, "write_text", write_text)
    return sent


FACTS = {"controls": [{"id": "a", "kind": "range", "label": "Amount", "min": 0, "max": 10, "value": 3}],
         "labels": ["Amount"], "changes": [{"control": "a", "changes": ["o"], "samples": ["6 → 20"], "redrawn": False}]}


def fake_checks(monkeypatch, problems_of) -> None:
    """The code checks: problems_of(html) gives the problems; a passing widget reports FACTS (as if it ran)."""
    def inspect(html, checks=None):
        problems = problems_of(html)
        return {"problems": problems, "ids": None, "facts": None if problems else FACTS, "runtime": not problems}

    monkeypatch.setattr(codelab, "inspect", inspect)


class JevLog(list):
    """The gate states Jev was asked about; .reviews holds the review states (with the questions asked)."""
    reviews: list[dict]


GOOD_REVIEW = {"controls_on_topic": 0.9, "change_shows_lesson": 0.9, "uses_section_terms": 0.9, "same_idea": 0.5, "backup_direct": 0.1}
UNSURE = {**GOOD_REVIEW, "change_shows_lesson": 0.5}
FIXABLE = {**GOOD_REVIEW, "change_shows_lesson": 0.1, "same_idea": 0.9}
USE_BACKUP = {**GOOD_REVIEW, "change_shows_lesson": 0.1, "same_idea": 0.1, "backup_direct": 0.9}
HOPELESS = {"controls_on_topic": 0.1, "change_shows_lesson": 0.1, "uses_section_terms": 0.2, "same_idea": 0.1, "backup_direct": 0.1}


def fake_jev(monkeypatch, open_for: set[int] | None = None, fail: bool = False, reviews: list[dict] | None = None,
             same: float = 0.1) -> JevLog:
    """Jev opens the gate for every section (or only those whose label is S<i> for i in open_for), answers reviews
    with the next of `reviews` (the last one repeats; default: good), and Code Lab 4.0's repeat questions with `same`."""
    asked = JevLog()
    asked.reviews = []
    asked.dupes = []
    queue = list(reviews or [GOOD_REVIEW])

    def jev_ask(key, state, questions):
        if fail:
            raise codelab.JevError("Jev unreachable")
        p = lambda v: {"p": v}  # noqa: E731
        usage = {"input_tokens": 100, "output_tokens": 0}
        if isinstance(state, dict) and "charts" in state:  # Code Lab 4.0's repeat check (not the review's same_idea)
            asked.dupes.append({"state": state, "questions": sorted(questions)})
            return {"answers": {k: p(same) for k in questions}, "model": "jev-test", "usage": usage}
        if "controls_on_topic" in questions:
            asked.reviews.append({**state, "questions": sorted(questions)})
            r = queue.pop(0) if len(queue) > 1 else queue[0]
            return {"answers": {k: p(v) for k, v in r.items() if k in questions}, "model": "jev-test", "usage": usage}
        asked.append(state)
        yes = open_for is None or int(state["section"]["label"][1:]) in open_for
        return {"answers": {"varies": p(0.9 if yes else 0.1), "process": p(0.1), "compare": p(0.1), "static": p(0.1 if yes else 0.9)},
                "model": "jev-test", "usage": usage}

    monkeypatch.setattr(codelab, "jev_ask", jev_ask)
    return asked


# ---------- the checks ----------


def custom_settings(think: bool = True) -> "codelab.CodeLabSettings":
    """The marks + fix-first + own-code settings Code Lab 7.0 and Playground build on (no free builder, no look check)."""
    return codelab.CodeLabSettings(plan="low", build="low", fix="low", vega=False, think=think, marks=True, fix_first=True)


def test_static_problems_catch_the_forbidden_things():
    assert codelab.static_problems("") == ["the widget is empty"]
    assert any("no script" in p for p in codelab.static_problems("<p>hi</p>"))
    assert any("curly quotes" in p for p in codelab.static_problems(CURLY))
    for bad in ("fetch('/x')", "alert(1)", "localStorage.x = 1", "new XMLHttpRequest()"):
        assert codelab.static_problems(f"<script>{bad}</script>"), bad
    assert any("external" in p for p in codelab.static_problems('<script src="https://cdn.example.com/x.js"></script><script>1</script>'))
    assert codelab.static_problems(GOOD) == []


def test_tiny_text_is_a_static_problem():
    tiny = GOOD.replace("<script>", "<style>text{font-size:9px}</style><script>")
    assert any("font-size 9" in p for p in codelab.static_problems(tiny))
    assert any("font-size 8" in p for p in codelab.static_problems(GOOD.replace("<script>", '<svg><text font-size="8">x</text></svg><script>')))
    assert codelab.static_problems(GOOD.replace("<script>", "<style>p{font-size:12px} q{font-size:0.9em} r{font-size:.8rem}</style><script>")) == []


def test_clean_html_makes_a_fragment():
    full = f"<!doctype html><html><head><style>p{{color:red}}</style></head><body>{GOOD}</body></html>"
    out = codelab.clean_html(full)
    assert out.startswith("<style>") and GOOD in out and "<html" not in out
    assert codelab.clean_html(f"```html\n{GOOD}\n```") == GOOD


@needs_node
def test_a_syntax_error_is_found():
    assert any("syntax" in p for p in codelab.check_widget(GOOD.replace("function f()", "function f(")))


KIT_WIDGET = ('<div id=root></div><script>var s=XW.slider(document.getElementById("root"),{id:"a",label:"Amount",min:0,max:10,step:1,value:3,onInput:f});'
              'var c=XW.chart(document.getElementById("root"),{xmin:0,xmax:1,ymin:0,ymax:10,xlabel:"x",ylabel:"y",id:"o"});'
              'function f(){XW.clear(c.g);XW.el("rect",{x:c.x(0.2),y:c.y(s.get()),width:30,height:c.y(0)-c.y(s.get())},c.g)}f();</script>')


@needs_node
@needs_jsdom
def test_the_widget_kit_is_loaded_in_the_check():
    assert codelab.check_widget(KIT_WIDGET) == []  # window.XW exists, and moving its slider redraws


@needs_node
@needs_jsdom
def test_overlapping_labels_are_found():
    two = '<input type=range id=a><svg><text x="10" y="10">a</text><text x="10" y="10">b</text></svg><script>a.oninput=()=>{document.body.append("x")}</script>'
    assert any("on top of each other" in p for p in codelab.check_widget(two))


@needs_node
@needs_jsdom
def test_labels_in_two_charts_do_not_overlap():
    # Two charts drawn by the kit put their axis titles and ticks at the same x and y, each in its own svg: not an overlap.
    two_charts = ('<div id=r></div><script>const r=document.getElementById("r");'
                  'const s=XW.slider(r,{id:"a",label:"a",min:0,max:1,step:0.1,value:0.5,onInput:()=>draw()});'
                  'const A=XW.chart(r,{xmin:0,xmax:1,ymin:0,ymax:1,xlabel:"x first",ylabel:"y first"});'
                  'const B=XW.chart(r,{xmin:0,xmax:1,ymin:0,ymax:1,xlabel:"x second",ylabel:"y second"});'
                  'function draw(){XW.clear(A.g);XW.el("circle",{cx:A.x(s.get()),cy:A.y(0.5),r:4},A.g)}draw();</script>')
    assert not any("on top of each other" in p for p in codelab.check_widget(two_charts))
    grouped = ('<input type=range id=a><svg><g transform="translate(0,0)"><text x="10" y="10">a</text></g>'
               '<g transform="translate(100,0)"><text x="10" y="10">b</text></g></svg><script>a.oninput=()=>{document.body.append("x")}</script>')
    assert not any("on top of each other" in p for p in codelab.check_widget(grouped))


@needs_node
@needs_jsdom
def test_runtime_checks():
    assert codelab.check_widget(GOOD) == []
    assert codelab.check_widget(CANVAS) == []
    assert any("changes nothing" in p for p in codelab.check_widget(DEAD))
    assert any("throws when it loads" in p for p in codelab.check_widget('<input type=range><script>document.getElementById("zz").value=1</script>'))
    assert any("no controls" in p for p in codelab.check_widget("<p id=o></p><script>o.textContent=1</script>"))


# ---------- the pass ----------


def run(monkeypatch, out: dict, replies: list[str], checker=None, plans: list[str] | None = None, jev: bool = True,
        reviews: list[dict] | None = None):
    sent = fake_writer(monkeypatch, replies, plans)
    fake_jev(monkeypatch, reviews=reviews)
    fake_checks(monkeypatch, checker or (lambda html: [] if html == GOOD else ["it is broken"]))
    notes: list[str] = []
    result, stats = codelab.design_code_widgets(None, out, UsageLog("code-lab", "low"), notes, jev_key="k" if jev else None)
    return result, stats, notes, sent


def test_a_good_widget_is_placed_after_the_block_the_model_named(monkeypatch):
    out = outline([TEXT, {"type": "text", "md": "b"}, QUIZ])
    result, stats, notes, sent = run(monkeypatch, out, [reply(afterBlock=0)])
    assert [b["type"] for b in result["stages"][0]["blocks"]] == ["text", "widget", "text", "quiz"]
    w = result["stages"][0]["blocks"][1]
    assert w["title"] == "Average steadier" and w["html"] == GOOD
    assert stats == {"sections": 1, "built": 1, "repaired": 0, "backup": 0, "charts": 0} and notes == []
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write"]
    assert sent[0]["user"]["section"]["blocks"][0] == {"index": 0, **TEXT}  # the planner reads the finished text
    assert "hint" in sent[0]["user"]
    assert sent[1]["user"]["plan"]["lesson"] == "more samples, steadier average"  # the builder gets the plan


def test_the_plan_is_kept_on_the_widget(monkeypatch):
    out = outline([TEXT, QUIZ])
    result, *_ = run(monkeypatch, out, [reply()])
    w = next(b for b in result["stages"][0]["blocks"] if b["type"] == "widget")
    kept = json.loads(w["plan"])
    assert kept["lesson"] == "more samples, steadier average" and kept["controls"][0]["id"] == "a" and "title" not in kept
    assert assemble_spec(result, None, "")["stages"][0]["blocks"][1]["plan"] == w["plan"]  # survives assembly


def test_a_cut_off_build_is_retried_once_with_less_thinking(monkeypatch):
    out = outline([TEXT])
    sent: list[dict] = []
    seq = iter([ReaderError("The code lab write was too long and got cut off."), reply()])

    def write_text(client, usage, step, *, system, user, max_tokens, **kw):
        sent.append({"step": step, "effort": kw["effort"]})
        if step == "code lab plan":
            return plan()
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(codelab, "write_text", write_text)
    fake_jev(monkeypatch)
    fake_checks(monkeypatch, lambda html: [])
    notes: list[str] = []
    result, stats = codelab.design_code_widgets(None, out, UsageLog("code-lab", "low"), notes, jev_key="k")
    assert stats["built"] == 1 and notes == []
    assert [(s["step"], s["effort"]) for s in sent] == [("code lab plan", "high"), ("code lab write", "high"), ("code lab write", "medium")]


def test_a_bad_after_block_goes_before_the_closing_quiz(monkeypatch):
    out = outline([TEXT, QUIZ])
    result, *_ = run(monkeypatch, out, [reply(afterBlock=-1)])
    assert [b["type"] for b in result["stages"][0]["blocks"]] == ["text", "widget", "quiz"]


def test_the_planner_can_decide_not_to_build(monkeypatch):
    out = outline([TEXT])
    result, stats, notes, sent = run(monkeypatch, out, [], plans=[NO_PLAN])
    assert result == out and stats["built"] == 0 and notes == [] and [s["step"] for s in sent] == ["code lab plan"]


def test_the_builder_can_say_the_plan_cannot_be_built(monkeypatch):
    out = outline([TEXT])
    result, stats, notes, sent = run(monkeypatch, out, [json.dumps({"build": False, "why": "needs a library", "title": "", "afterBlock": -1, "html": ""})])
    assert result == out and stats["built"] == 0 and len(notes) == 1 and "needs a library" in notes[0]


def test_a_closed_gate_costs_no_haiku_call(monkeypatch):
    out = outline([TEXT], [TEXT])
    sent = fake_writer(monkeypatch, [reply()])
    asked = fake_jev(monkeypatch, open_for={1})
    fake_checks(monkeypatch, lambda html: [])
    usage = UsageLog("code-lab", "low")
    result, stats = codelab.design_code_widgets(None, out, usage, [], jev_key="k")
    assert len(asked) == 2 and len(sent) == 2 and {s["step"] for s in sent} == {"code lab plan", "code lab write"}
    assert not any(b["type"] == "widget" for b in result["stages"][0]["blocks"])
    assert any(b["type"] == "widget" for b in result["stages"][1]["blocks"])
    assert stats == {"sections": 2, "built": 1, "repaired": 0, "backup": 0, "charts": 0}
    assert any(st["step"] == "jev code lab gate" for st in usage.summary()["steps"])


def test_without_jev_every_section_goes_to_the_plan(monkeypatch):
    out = outline([TEXT])
    result, stats, notes, sent = run(monkeypatch, out, [reply()], jev=False)
    assert stats["built"] == 1 and len(notes) == 1 and "no Jev key" in notes[0]


def test_a_jev_error_sends_the_section_to_the_plan(monkeypatch):
    out = outline([TEXT])
    sent = fake_writer(monkeypatch, [reply()])
    fake_jev(monkeypatch, fail=True)
    fake_checks(monkeypatch, lambda html: [])
    notes: list[str] = []
    result, stats = codelab.design_code_widgets(None, out, UsageLog("code-lab", "low"), notes, jev_key="k")
    assert stats["built"] == 1 and len(notes) == 1 and "Jev didn't answer" in notes[0]


def test_plan_problems():
    pl = json.loads(plan())
    assert codelab.plan_problems(GOOD, pl) == []
    assert any('control "a"' in p for p in codelab.plan_problems("<p id=o></p>", pl))
    assert any("not an <input" in p for p in codelab.plan_problems(GOOD.replace("type=range", "type=text"), pl))
    assert any("max 10" in p and "plan says 20" in p for p in codelab.plan_problems(GOOD, json.loads(plan(controls=[{**pl["controls"][0], "max": 20}]))))
    assert any("visuals" in p for p in codelab.plan_problems(GOOD.replace("id=o", "id=zz").replace('"o"', '"zz"'), pl))
    assert codelab.plan_problems("<script>x.id='a';y.id='o'</script>", pl) == []  # ids set from script are allowed


def test_a_widget_that_breaks_the_plan_is_repaired(monkeypatch):
    out = outline([TEXT])
    other = GOOD.replace("id=a ", "id=zzz ").replace('"a"', '"zzz"')
    result, stats, notes, sent = run(monkeypatch, out, [reply(other), reply(GOOD)], checker=lambda html: [])
    assert stats == {"sections": 1, "built": 1, "repaired": 1, "backup": 0, "charts": 0} and any('control "a"' in p for p in sent[-1]["user"]["problems"])
    assert sent[-1]["user"]["plan"]["title"] == "Average steadier"


def test_a_broken_widget_is_repaired_with_the_exact_problems(monkeypatch):
    out = outline([TEXT])
    result, stats, notes, sent = run(monkeypatch, out, [reply(CURLY), reply(GOOD)])
    assert stats == {"sections": 1, "built": 1, "repaired": 1, "backup": 0, "charts": 0}
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write", "code lab repair"]
    assert sent[2]["user"]["problems"] == ["it is broken"] and sent[2]["user"]["yourPreviousHtml"] == CURLY
    assert result["stages"][0]["blocks"][-1]["html"] == GOOD


def test_an_unrepairable_widget_leaves_the_section_as_text_with_a_note(monkeypatch):
    out = outline([TEXT])
    result, stats, notes, sent = run(monkeypatch, out, [reply(CURLY)] * 3)
    assert result == out and stats["built"] == 0 and stats["repaired"] == codelab.MAX_REPAIRS
    assert len(sent) == 2 + codelab.MAX_REPAIRS  # the plan, the build and the repairs
    assert len(notes) == 1 and notes[0].startswith("Code Lab (S0)") and "it is broken" in notes[0]


def test_the_token_cap_needs_streaming_and_the_sdk_allows_it_streamed():
    # A non-streaming call above ~21k tokens raises a ValueError before anything is sent (every section once stayed text);
    # the builds stream, so the cap may be higher. The streamed request must get as far as trying to connect.
    import anthropic
    c = anthropic.Anthropic(api_key="x", base_url="http://127.0.0.1:9", max_retries=0)
    with pytest.raises(ValueError):
        c.messages.create(model="claude-haiku-5-5", max_tokens=codelab.MAX_TOKENS, messages=[{"role": "user", "content": "hi"}])
    with pytest.raises(anthropic.APIConnectionError):
        with c.messages.stream(model="claude-haiku-5-5", max_tokens=codelab.MAX_TOKENS, messages=[{"role": "user", "content": "hi"}]) as st:
            st.get_final_message()


def test_write_text_streams_when_asked():
    from types import SimpleNamespace as NS
    from explainer import llm

    calls: list[str] = []
    final = NS(content=[NS(type="text", text="hi")], stop_reason="end_turn", model="claude-haiku-5-5",
               usage=NS(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0))

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return final

    client = NS(messages=NS(stream=lambda **kw: (calls.append("stream"), Stream())[1], create=lambda **kw: (calls.append("create"), final)[1]))
    usage = UsageLog("code-lab", "low")
    assert llm.write_text(client, usage, "s", system="x", user="y", max_tokens=100, stream=True) == "hi"
    assert llm.write_text(client, usage, "s", system="x", user="y", max_tokens=100) == "hi"
    assert calls == ["stream", "create"]


def test_write_text_can_turn_thinking_off():
    from types import SimpleNamespace as NS
    from explainer import llm

    sent: list[dict] = []
    final = NS(content=[NS(type="text", text="hi")], stop_reason="end_turn", model="claude-haiku-5-5",
               usage=NS(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0))
    client = NS(messages=NS(create=lambda **kw: (sent.append(kw), final)[1]))
    usage = UsageLog("code-lab-2", "low")
    llm.write_text(client, usage, "s", system="x", user="y", max_tokens=100)
    llm.write_text(client, usage, "s", system="x", user="y", max_tokens=100, thinking=False)
    assert "thinking" not in sent[0]  # the default: Haiku's own adaptive thinking
    assert sent[1]["thinking"] == {"type": "disabled"}


def test_code_lab_2_chart_steps_follow_the_thinking_setting(monkeypatch):
    seen: list[tuple[str, object]] = []

    def write_text(client, usage, step, **kw):
        seen.append((step, kw.get("thinking", True)))
        return '{"build": false}'

    monkeypatch.setattr(codelab, "write_text", write_text)
    usage = UsageLog("code-lab-2", "low")
    off = codelab.CodeLabSettings.for_effort("low", think=False)
    assert codelab.CodeLabSettings.for_effort("low").think is True and off.think is False
    codelab._plan(None, usage, {}, "", [], off)
    codelab._vega_fix(None, usage, {}, {}, SPEC, ["x"], "low", off.think)
    codelab._vega_backup(None, usage, {}, {}, "low", off.think)
    codelab._plan(None, usage, {}, "", [], codelab.CodeLabSettings.for_effort("low"))
    assert seen == [("code lab plan", False), ("code lab chart repair", False), ("code lab chart backup", False), ("code lab plan", True)]


def test_a_failed_call_says_why_in_the_note(monkeypatch):
    def boom(*a, **k):
        raise ValueError("Streaming is required")

    monkeypatch.setattr(codelab, "write_text", boom)
    fake_jev(monkeypatch)
    notes: list[str] = []
    codelab.design_code_widgets(None, outline([TEXT]), UsageLog("code-lab", "low"), notes, jev_key="k")
    assert len(notes) == 1 and "the planner failed" in notes[0] and "Something went wrong" in notes[0] and "Streaming is required" not in notes[0]


def test_a_failed_build_says_why_in_the_note(monkeypatch):
    def write_text(client, usage, step, **k):
        if step == "code lab plan":
            return plan()
        raise ValueError("Streaming is required")

    monkeypatch.setattr(codelab, "write_text", write_text)
    fake_jev(monkeypatch)
    notes: list[str] = []
    codelab.design_code_widgets(None, outline([TEXT]), UsageLog("code-lab", "low"), notes, jev_key="k")
    assert len(notes) == 1 and "the builder failed" in notes[0] and "Something went wrong" in notes[0] and "Streaming is required" not in notes[0]


def test_a_failed_call_never_raises(monkeypatch):
    out = outline([TEXT], [TEXT])

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(codelab, "write_text", boom)
    fake_jev(monkeypatch)
    result, stats, notes = codelab.design_code_widgets(None, out, UsageLog("code-lab", "low"), [], jev_key="k")[0], None, None
    assert result == out


def test_sections_run_independently(monkeypatch):
    out = outline([TEXT], [TEXT])
    result, stats, notes, _ = run(monkeypatch, out, [reply(GOOD), reply(GOOD)])
    assert stats["built"] == 2 and all(any(b["type"] == "widget" for b in st["blocks"]) for st in result["stages"])


def test_the_prebuilt_parts_are_stripped():
    slider = {"type": "slider", "var": "n", "label": "n", "min": 0, "max": 1, "step": 0.1, "value": 0.5, "unit": ""}
    stripped = codelab.strip_prebuilt(outline([TEXT, slider, {"type": "readout", "title": "r", "items": []}, QUIZ]))
    assert [b["type"] for b in stripped["stages"][0]["blocks"]] == ["text", "quiz"]


def test_a_widget_block_survives_assembly(monkeypatch):
    out = outline([TEXT, QUIZ])
    result, *_ = run(monkeypatch, out, [reply()])
    spec = assemble_spec(result, None, "")
    assert any(b["type"] == "widget" for st in spec["stages"] for b in st["blocks"])


# ---------- Jev's review ----------


def test_a_widget_the_reviewer_likes_is_kept(monkeypatch):
    asked = None
    sent = fake_writer(monkeypatch, [reply()])
    asked = fake_jev(monkeypatch)
    fake_checks(monkeypatch, lambda html: [])
    notes: list[str] = []
    result, stats = codelab.design_code_widgets(None, outline([TEXT]), UsageLog("code-lab", "low"), notes, jev_key="k")
    assert stats["built"] == 1 and notes == [] and [s["step"] for s in sent] == ["code lab plan", "code lab write"]
    assert len(asked.reviews) == 1 and asked.reviews[0]["questions"] == sorted(codelab.REVIEW_QUESTIONS)
    r = asked.reviews[0]
    assert r["widget"] == {k: FACTS[k] for k in ("controls", "labels", "changes")} and "html" not in json.dumps(r)  # facts, never code
    assert r["plan"]["lesson"] == "more samples, steadier average" and r["plan"]["backup"]
    assert "More samples" in r["section"]["text"]


def test_a_fixable_widget_is_repaired_at_medium_then_reviewed_again(monkeypatch):
    fixed = GOOD.replace("*2", "*3")
    result, stats, notes, sent = run(monkeypatch, outline([TEXT]), [reply(), reply(fixed)], checker=lambda html: [], reviews=[FIXABLE, GOOD_REVIEW])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write", "code lab repair"]
    assert any("not what the lesson" in p for p in sent[2]["user"]["problems"])
    assert [r["kind"] for r in stats.pop("rejected")] == ["first"]  # the first widget, kept for test runs
    assert stats == {"sections": 1, "built": 1, "repaired": 1, "backup": 0, "charts": 0} and notes == []
    assert result["stages"][0]["blocks"][-1]["html"] == fixed


def test_the_re_review_asks_only_whether_it_is_good(monkeypatch):
    sent = fake_writer(monkeypatch, [reply(), reply()])
    asked = fake_jev(monkeypatch, reviews=[FIXABLE, GOOD_REVIEW])
    fake_checks(monkeypatch, lambda html: [])
    codelab.design_code_widgets(None, outline([TEXT]), UsageLog("code-lab", "low"), [], jev_key="k")
    assert [r["questions"] for r in asked.reviews] == [sorted(codelab.REVIEW_QUESTIONS), sorted(codelab.QUALITY)]


def test_a_fix_that_still_fails_review_leaves_text(monkeypatch):
    result, stats, notes, sent = run(monkeypatch, outline([TEXT]), [reply(), reply()], checker=lambda html: [], reviews=[FIXABLE, HOPELESS])
    assert stats["built"] == 0 and len(notes) == 1 and "fix didn't pass review" in notes[0]
    assert not any(b["type"] == "widget" for b in result["stages"][0]["blocks"])


def test_an_unfixable_widget_is_replaced_by_the_backup(monkeypatch):
    backup = GOOD.replace("*2", "+1")
    result, stats, notes, sent = run(monkeypatch, outline([TEXT]), [reply(), reply(backup)], checker=lambda html: [], reviews=[USE_BACKUP, GOOD_REVIEW])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write", "code lab backup"]
    assert sent[2]["user"]["plan"] == {"lesson": "more samples, steadier average", "backup": "one slider for the amount and a readout of twice it",
                                       "title": "Average steadier", "afterBlock": 0}
    assert [r["kind"] for r in stats.pop("rejected")] == ["first"]  # the first widget, kept for test runs
    assert stats == {"sections": 1, "built": 1, "repaired": 0, "backup": 1, "charts": 0}
    assert result["stages"][0]["blocks"][-1]["html"] == backup
    assert len(notes) == 1 and "backup was used" in notes[0]


def test_a_backup_is_checked_by_running_it_not_by_the_plan(monkeypatch):
    # The backup has its own ids, so the plan's ids aren't required; a broken backup gets medium repairs, then text.
    other = GOOD.replace("id=a ", "id=zzz ").replace('"a"', '"zzz"')
    result, stats, notes, sent = run(monkeypatch, outline([TEXT]), [reply(), reply(other)], checker=lambda html: [], reviews=[USE_BACKUP, GOOD_REVIEW])
    assert stats["built"] == 1 and stats["backup"] == 1
    result, stats, notes, sent = run(monkeypatch, outline([TEXT]), [reply(), reply(CURLY), reply(CURLY), reply(CURLY)], reviews=[USE_BACKUP])
    assert stats["built"] == 0 and stats["repaired"] == codelab.MAX_REPAIRS and "backup were both rejected" in notes[0]
    assert [s["step"] for s in sent][2:] == ["code lab backup"] + ["code lab repair"] * codelab.MAX_REPAIRS


def test_a_hopeless_widget_leaves_text_without_more_haiku_calls(monkeypatch):
    result, stats, notes, sent = run(monkeypatch, outline([TEXT]), [reply()], reviews=[HOPELESS])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write"] and stats["built"] == 0
    assert len(notes) == 1 and "saw no fix or better backup" in notes[0]


def test_no_backup_idea_means_no_backup(monkeypatch):
    result, stats, notes, sent = run(monkeypatch, outline([TEXT]), [reply()], plans=[plan(backup="")], reviews=[USE_BACKUP])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write"] and stats["built"] == 0


def test_without_jev_there_is_no_review(monkeypatch):
    sent = fake_writer(monkeypatch, [reply()])
    asked = fake_jev(monkeypatch, reviews=[HOPELESS])
    fake_checks(monkeypatch, lambda html: [])
    _, stats = codelab.design_code_widgets(None, outline([TEXT]), UsageLog("code-lab", "low"), [], jev_key=None)
    assert stats["built"] == 1 and asked.reviews == [] and list(asked) == []


def test_the_review_is_priced(monkeypatch):
    fake_writer(monkeypatch, [reply()])
    fake_jev(monkeypatch)
    fake_checks(monkeypatch, lambda html: [])
    usage = UsageLog("code-lab", "low")
    codelab.design_code_widgets(None, outline([TEXT]), usage, [], jev_key="k")
    assert [s["step"] for s in usage.summary()["steps"]].count("jev code lab review") == 1


def test_an_unsure_review_keeps_the_widget_with_its_scores(monkeypatch):
    result, stats, notes, sent = run(monkeypatch, outline([TEXT]), [reply()], reviews=[UNSURE])
    assert stats["built"] == 1 and notes == [] and [s["step"] for s in sent] == ["code lab plan", "code lab write"]
    w = next(b for b in result["stages"][0]["blocks"] if b["type"] == "widget")
    assert json.loads(w["plan"])["review"]["change_shows_lesson"] == 0.5


def test_off_topic_controls_go_to_the_backup_not_a_fix(monkeypatch):
    # A repair must keep the plan's controls, so it can't fix controls the plan chose badly: build the backup instead.
    off_topic = {**GOOD_REVIEW, "controls_on_topic": 0.2, "same_idea": 0.9, "backup_direct": 0.9}
    result, stats, notes, sent = run(monkeypatch, outline([TEXT]), [reply(), reply()], checker=lambda html: [], reviews=[off_topic, GOOD_REVIEW])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write", "code lab backup"] and stats["backup"] == 1


def test_a_rejection_note_carries_the_scores(monkeypatch):
    _, _, notes, _ = run(monkeypatch, outline([TEXT]), [reply()], reviews=[HOPELESS])
    assert "Scores: controls_on_topic 0.10" in notes[0]


def test_no_facts_means_no_review(monkeypatch):
    # The widget couldn't run (no jsdom): nothing literal for Jev to judge, so it is kept as it passed the checks.
    fake_writer(monkeypatch, [reply()])
    asked = fake_jev(monkeypatch, reviews=[HOPELESS])
    monkeypatch.setattr(codelab, "inspect", lambda html, checks=None: {"problems": [], "ids": None, "facts": None, "runtime": False})
    _, stats = codelab.design_code_widgets(None, outline([TEXT]), UsageLog("code-lab", "low"), [], jev_key="k")
    assert stats["built"] == 1 and asked.reviews == []


def test_the_reviewer_reads_the_whole_section_and_its_formulas():
    long = {"type": "text", "md": "word " * 1500}
    out = outline([long, {"type": "formula", "latex": r"\frac{1}{\sqrt{d_k}}"}])
    st = codelab.review_state(out, 0, json.loads(plan()), FACTS)
    assert len(st["section"]["text"]) > 2500 and st["section"]["formulas"] == [r"\frac{1}{\sqrt{d_k}}"]
    assert set(st["widget"]) == {"controls", "labels", "changes"}


TABLE = {"type": "table", "title": "Knobs", "columns": ["Setting", "Too high"], "rows": [["KL coefficient $\\beta$", "Policy barely moves"]], "caption": ""}
CARDS = {"type": "cards", "variant": "mistake", "title": "Misreadings",
         "items": [{"title": "The KL penalty is just regularisation", "md": "Changing $\\beta$ changes what the optimum is."}]}
OTHER = [{"type": "tabs", "tabs": [{"label": "Math", "md": "ratio r"}]},
         {"type": "compare", "left": {"title": "SFT", "points": ["imitates"]}, "right": {"title": "RL", "points": ["optimises reward"]}},
         {"type": "reveal", "prompt": "Why clip?", "answer": "To keep updates small."},
         {"type": "flipcards", "cards": [{"front": "advantage", "back": "how much better than expected"}]}]


def test_the_reviewer_also_reads_tables_cards_and_other_reading_blocks():
    # A control the section explains only in a table or a card is something the section talks about.
    out = outline([TEXT, TABLE, CARDS, *OTHER])
    text = codelab.review_state(out, 0, json.loads(plan()), FACTS)["section"]["text"]
    for want in ("More samples", "KL coefficient $\\beta$ | Policy barely moves", "Setting | Too high",
                 "The KL penalty is just regularisation: Changing $\\beta$", "Math: ratio r", "SFT: imitates", "RL: optimises reward",
                 "Question: Why clip?", "Answer: To keep updates small.", "advantage: how much better than expected"):
        assert want in text, want


def test_the_code_lab_gate_reads_tables_and_cards_but_lab_mode_is_unchanged():
    from explainer import labplan
    out = outline([TEXT, TABLE, CARDS])
    full = labplan.gate_state(out, 0, full=True)
    assert "KL coefficient" in full["section"]["text"] and "Changing $\\beta$" in full["section"]["text"] and full["section_parts"] == []
    lab = labplan.gate_state(out, 0)  # Lab mode: text and callouts only, the rest named by type and title
    assert "KL coefficient" not in lab["section"]["text"] and lab["section_parts"] == ["table: Knobs", "cards: Misreadings"]


def test_a_summary_section_of_only_tables_and_cards_is_not_empty_for_jev():
    out = outline([TABLE, CARDS])
    assert codelab.review_state(out, 0, json.loads(plan()), FACTS)["section"]["text"].strip()


def test_the_code_lab_gate_closes_only_on_a_clear_no():
    v = lambda **p: codelab.codelab_gate_verdict({k: {"p": p.get(k, 0.5)} for k in ("varies", "process", "compare", "static")})  # noqa: E731
    assert v(varies=0.5, process=0.4, compare=0.2, static=0.4) == {"open": True, "why": "varies"}  # unsure: open
    assert v(varies=0.6, static=0.8)["open"] is False  # mostly definitions or advice
    assert v(varies=0.86, compare=0.96, static=0.86) == {"open": True, "why": "compare"}  # advice-like but clearly comparing
    assert v(varies=0.2, process=0.1, compare=0.3, static=0.4)["open"] is False  # clearly nothing to explore
    assert v(varies=0.2, process=0.35, compare=0.1, static=0.4) == {"open": True, "why": "process"}


def test_playground_gate_opens_a_structure_section():
    keys = ("varies", "process", "compare", "static", "structure")
    v = lambda **p: codelab.codelab_gate_verdict({k: {"p": p.get(k, 0.5)} for k in keys})  # noqa: E731
    # an architecture section: no quantity, process or comparison, but clearly connected parts
    assert v(varies=0.2, process=0.2, compare=0.1, static=0.4, structure=0.9) == {"open": True, "why": "structure"}
    assert v(varies=0.2, process=0.2, compare=0.1, static=0.4, structure=0.2)["open"] is False
    assert "structure" in codelab.PG_GATE_QUESTIONS and "structure" not in codelab.GATE_QUESTIONS
    assert "connected parts" in codelab.gate_hint("structure")


# ---------- what the test run reports: ids, number checks, facts ----------

KIT = ('<div id=root></div><script>const r=document.getElementById("root");'
       'const s=XW.slider(r,{id:"dk",label:"Key size d_k",min:1,max:64,step:1,value:16,onInput:()=>draw()});'
       'const ch=XW.chart(r,{id:"curve",xmin:0,xmax:64,ymin:0,ymax:1,xlabel:"d_k",ylabel:"scale"});'
       'const rd=XW.readouts(r,["1 / sqrt(d_k)"]);rd.el.id="scale";XW.note(r,"Bigger d_k shrinks the scale.");'
       'function draw(){XW.clear(ch.g);const v=1/Math.sqrt(s.get());XW.el("circle",{cx:ch.x(s.get()),cy:ch.y(v),r:4},ch.g);'
       'rd.set(0,XW.fmt(v,3));}draw();</script>')


def kit_plan(**kw) -> dict:
    return {"controls": [{"id": "dk", "kind": "slider", "label": "d_k", "min": 1, "max": 64, "step": 1, "value": 16}],
            "visuals": [{"id": "scale", "kind": "readout", "what": "1/sqrt(d_k)"}], **kw}


def test_kit_ids_are_found_in_the_source_too():
    assert codelab.plan_problems(KIT, kit_plan()) == []  # id: "dk" inside the kit call counts
    assert any('control "dk"' in p for p in codelab.plan_problems(KIT.replace('id:"dk"', 'id:"zz"'), kit_plan()))


@needs_node
@needs_jsdom
def test_kit_made_controls_pass_the_plan_check():
    problems, report = codelab.check_built(KIT, kit_plan())
    assert problems == [] and report["ids"]["dk"] == {"tag": "input", "type": "range", "min": 1, "max": 64}
    problems, _ = codelab.check_built(KIT, kit_plan(controls=[{**kit_plan()["controls"][0], "max": 128}]))
    assert any("max 64" in p and "plan says 128" in p for p in problems)


def test_number_checks_are_computed_by_the_formula_engine():
    checks = codelab.number_checks({"checks": [
        {"set": [{"control": "dk", "value": 64}], "readout": "scale", "expr": "1 / sqrt(dk)"},
        {"set": [{"control": "dk", "value": 4}], "readout": "scale", "expr": "1 / sqrt(other)"},  # unknown name: dropped
        {"set": [], "readout": "scale", "expr": "1"},  # nothing set: dropped
    ]})
    assert checks == [{"set": {"dk": 64.0}, "readout": "scale", "expect": 0.125, "expr": "1 / sqrt(dk)"}]


@needs_node
@needs_jsdom
def test_a_number_check_catches_wrong_math():
    good = kit_plan(checks=[{"set": [{"control": "dk", "value": 64}], "readout": "scale", "expr": "1 / sqrt(dk)"}])
    assert codelab.check_built(KIT, good)[0] == []
    wrong = kit_plan(checks=[{"set": [{"control": "dk", "value": 4}], "readout": "scale", "expr": "1 / dk"}])
    problems, report = codelab.check_built(KIT, wrong)
    assert len(problems) == 1 and "dk = 4" in problems[0] and '"0.500"' in problems[0] and "0.25" in problems[0]


@needs_node
@needs_jsdom
def test_the_test_run_reports_facts_for_the_reviewer():
    facts = codelab.inspect(KIT)["facts"]
    assert facts["controls"] == [{"id": "dk", "kind": "range", "label": "Key size d_k", "min": 1, "max": 64, "value": 16}]
    assert "Bigger d_k shrinks the scale." in facts["labels"] and "1 / sqrt(d_k)" in facts["labels"]
    assert not any(l.replace(".", "").isdigit() for l in facts["labels"])  # tick numbers left out
    ch = facts["changes"][0]
    assert ch["control"] == "dk" and "scale" in ch["changes"] and ch["redrawn"] is True
    assert any("0.250" in s for s in ch["samples"])


# ---------- starting sections while the text streams ----------


def chunks(text: str, n: int = 7) -> list[str]:
    return [text[i:i + n] for i in range(0, len(text), n)]


def test_the_scanner_finds_each_stage_as_soon_as_it_closes():
    out = outline([{"type": "text", "md": 'tricky "quotes" {braces} [brackets] and a \\\\ backslash'}], [TEXT])
    text = "```json\n" + json.dumps(out) + "\n```"
    sc = codelab.StageScanner()
    found: list[tuple[int, str]] = []
    for i, c in enumerate(chunks(text, 3)):
        found += [(i, s) for s in sc.feed(c)]
    assert [json.loads(s) for _, s in found] == out["stages"]
    assert found[0][0] < found[1][0]  # the first stage came out before the second was written
    assert sc.head == {"title": "Averaging", "level": "student"}
    sc.reset()
    assert sc.feed(json.dumps(out)) and sc.head["title"] == "Averaging"


def test_the_scanner_ignores_nested_stage_like_keys():
    text = json.dumps({"title": "t", "phases": [{"id": "p", "stages": [{"x": 1}]}], "stages": [{"label": "A", "blocks": []}]})
    assert [json.loads(s) for s in codelab.StageScanner().feed(text)] == [{"label": "A", "blocks": []}]


def spy_sections(monkeypatch) -> list[int]:
    """How many stages each section run saw (1 = started from the stream)."""
    seen: list[int] = []
    real = codelab._gate_and_design

    def spy(client, usage, outl, si, jev_key, *rest):
        seen.append(len(outl["stages"]))
        return real(client, usage, outl, si, jev_key, *rest)

    monkeypatch.setattr(codelab, "_gate_and_design", spy)
    return seen


def test_streamed_sections_start_early_and_are_reused(monkeypatch):
    out = outline([TEXT], [TEXT, QUIZ])
    sent = fake_writer(monkeypatch, [reply(), reply()])
    fake_jev(monkeypatch)
    fake_checks(monkeypatch, lambda html: [])
    seen = spy_sections(monkeypatch)
    st = codelab.Streamed(None, UsageLog("code-lab", "low"), "k")
    st.feed(None)
    for c in chunks(json.dumps(out)):
        st.feed(c)
    assert len(st.early) == 2
    progress: list[str] = []
    notes: list[str] = []
    result, stats = st.finish(out, notes, progress.append)
    assert seen == [1, 1] and stats["early"] == 2 and stats["built"] == 2 and notes == []
    assert len(sent) == 4  # one plan and one build per section: nothing ran twice
    assert all(any(b["type"] == "widget" for b in s["blocks"]) for s in result["stages"])
    assert progress == ["1 of 2 sections done"]


def test_a_section_that_changed_after_streaming_is_run_again(monkeypatch):
    streamed = outline([{"type": "text", "md": "first draft"}])
    final = outline([TEXT])
    fake_writer(monkeypatch, [reply(), reply()])
    fake_jev(monkeypatch)
    fake_checks(monkeypatch, lambda html: [])
    seen = spy_sections(monkeypatch)
    st = codelab.Streamed(None, UsageLog("code-lab", "low"), "k")
    st.feed(json.dumps(streamed))
    result, stats = st.finish(final, [])
    assert stats["early"] == 0 and stats["built"] == 1 and seen[-1] == 1 and len(seen) >= 1
    assert result["stages"][0]["blocks"][0] == TEXT


def test_a_new_stream_resets_the_scanner(monkeypatch):
    out = outline([TEXT])
    fake_writer(monkeypatch, [reply(), reply()])
    fake_jev(monkeypatch)
    fake_checks(monkeypatch, lambda html: [])
    st = codelab.Streamed(None, UsageLog("code-lab", "low"), "k")
    st.feed(json.dumps(out)[:40])  # a compute round that stopped early
    st.feed(None)
    st.feed(json.dumps(out))
    _, stats = st.finish(out, [])
    assert stats["early"] == 1 and stats["built"] == 1


# ---------- Code Lab 4.0: the writer marks chart sections, Jev checks the page for repeats ----------


def marked(*marks) -> dict:
    """An outline whose stage i carries marks[i] as its "interaction" field (None = the writer left it out)."""
    out = outline(*[[TEXT] for _ in marks])
    for st, m in zip(out["stages"], marks):
        if m is not None:
            st["interaction"] = m
    return out


def run4(monkeypatch, out: dict, plans: list[str], jev_key: str | None = "k", same: float = 0.1, reviews=None):
    sent = fake_writer2(monkeypatch, [], plans)
    asked = fake_jev(monkeypatch, same=same, reviews=reviews)
    fake_checks(monkeypatch, lambda html: [])
    st = codelab.Streamed(None, UsageLog("code-lab-4", "low"), jev_key, custom_settings())
    st.feed(None)
    for c in chunks(json.dumps(out)):
        st.feed(c)
    notes: list[str] = []
    result, stats = st.finish(out, notes)
    return result, stats, notes, sent, asked


def test_read_mark():
    assert codelab.read_mark({"interaction": {"chart": True, "idea": "  drag  beta,\n watch the curve "}}) == {"chart": True, "idea": "drag beta, watch the curve"}
    assert codelab.read_mark({"interaction": {"chart": False}}) == {"chart": False, "idea": ""}
    for bad in ({}, {"interaction": "yes"}, {"interaction": {"chart": "true"}}, None, []):
        assert codelab.read_mark(bad) is None
    # Playground's note also gives a kind, which leads the hint Haiku gets
    m = codelab.read_mark({"interaction": {"chart": True, "kind": "structure", "idea": "click a layer, see its weights"}})
    assert m == {"chart": True, "idea": "click a layer, see its weights", "kind": "structure"}
    assert codelab.mark_hint(m) == "structure: click a layer, see its weights"
    assert "kind" not in codelab.read_mark({"interaction": {"chart": True, "kind": "poster", "idea": "x"}})
    assert codelab.mark_hint({"chart": True, "idea": ""}) is None


def test_playground_writer_note_allows_every_kind_of_part():
    for kind in codelab.MARK_KINDS:
        assert kind in codelab.PLANNER_NOTE_PG
    assert "interactive chart" not in codelab.PLANNER_NOTE_PG and "structure of connected parts" in codelab.PLANNER_NOTE_PG


def test_fix_first_sends_off_topic_controls_to_a_fix():
    off = {**GOOD_REVIEW, "controls_on_topic": 0.1, "same_idea": 0.1, "backup_direct": 0.9}
    old = codelab.review_verdict({k: {"p": v} for k, v in off.items()}, True)
    new = codelab.review_verdict({k: {"p": v} for k, v in off.items()}, True, fix_first=True)
    assert (old["fix"], old["backup"]) == (False, True)  # 2.0/3.0: off-topic controls skip the fix
    assert (new["fix"], new["backup"]) == (True, True)  # 4.0: a fix first, the backup next


def test_feeding_garbage_never_raises(monkeypatch):
    st = codelab.Streamed(None, UsageLog("code-lab", "low"), None)
    st.feed('{"stages": [{"label": }, {"oops"')
    st.feed("}]}")
    _, stats = st.finish(outline(), [])
    assert stats["sections"] == 0


# ---------- Code Lab 2.0: one Effort, and Vega-Lite charts ----------

SPEC = {
    "params": [{"name": "d", "value": 1024, "bind": {"input": "range", "min": 64, "max": 4096, "step": 64, "name": "Layer size d "}}],
    "data": {"sequence": {"start": 1, "stop": 65, "as": "r"}},
    "transform": [{"calculate": "datum.r * (d + d)", "as": "LoRA"}, {"calculate": "d * d", "as": "Full"}, {"fold": ["LoRA", "Full"]}],
    "mark": "line", "width": 480, "height": 260,
    "encoding": {"x": {"field": "r", "type": "quantitative", "title": "Rank r"},
                 "y": {"field": "value", "type": "quantitative", "title": "Trainable parameters"}, "color": {"field": "key", "title": ""}},
}
CHART_PLAN = {"controls": [{"id": "d", "kind": "slider", "label": "Layer size d", "min": 64, "max": 4096, "step": 64, "value": 1024}], "visuals": []}


def chart_plan(spec=SPEC, **kw) -> str:
    return plan(vegaLite=json.dumps(spec) if isinstance(spec, dict) else spec,
                controls=CHART_PLAN["controls"], visuals=[{"id": "lines", "kind": "curve", "what": "parameters against rank"}], **kw)


def fake_writer2(monkeypatch, replies: list[str], plans: list[str]) -> list[dict]:
    """Code Lab 2.0's Haiku calls: records (step, effort, system, fmt, user)."""
    sent: list[dict] = []
    plan_it, other_it = iter(plans), iter(replies)

    def write_text(client, usage, step, *, system, user, max_tokens, **kw):
        sent.append({"step": step, "effort": kw["effort"], "system": system, "fmt": kw.get("fmt"), "user": json.loads(user),
                     "thinking": kw.get("thinking", True)})
        return next(plan_it if step == "code lab plan" else other_it)

    monkeypatch.setattr(codelab, "write_text", write_text)
    return sent


def run2(monkeypatch, replies, plans, effort="low", checker=None, reviews=None):
    sent = fake_writer2(monkeypatch, replies, plans)
    fake_jev(monkeypatch, reviews=reviews)
    fake_checks(monkeypatch, checker or (lambda html: []))
    notes: list[str] = []
    result, stats = codelab.design_code_widgets(None, outline([TEXT]), UsageLog("code-lab-2", effort), notes, jev_key="k",
                                                settings=codelab.CodeLabSettings.for_effort(effort))
    return result, stats, notes, sent


def test_code_lab_keeps_its_efforts_and_no_charts():
    s = codelab.CodeLabSettings()
    assert (s.plan, s.build, s.fix, s.vega) == ("high", "high", "medium", False)
    assert codelab.CodeLabSettings.for_effort("nonsense").build == "medium"


FIXED_SPEC = json.dumps({"spec": json.dumps(SPEC)})


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_code_lab_2_uses_the_page_effort_for_every_step(monkeypatch, effort):
    # A broken chart repaired, then judged fixable and repaired again: every call at the page's effort.
    _, stats, _, sent = run2(monkeypatch, [FIXED_SPEC, FIXED_SPEC], [chart_plan("{not json")], effort, reviews=[FIXABLE, GOOD_REVIEW])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab chart repair", "code lab chart repair"]
    assert {s["effort"] for s in sent} == {effort} and stats["built"] == 1 and stats["charts"] == 1
    assert "Every widget on this page is a Vega-Lite chart" in sent[0]["system"] and "vegaLite" in sent[0]["fmt"]["properties"]
    assert "route" not in sent[0]["fmt"]["properties"]


def test_code_lab_2_never_writes_custom_code(monkeypatch):
    # Every planned widget is drawn as a chart; a plan that declines (nothing a chart can show) leaves text with no other call.
    _, stats, notes, sent = run2(monkeypatch, [], [chart_plan()])
    assert [s["step"] for s in sent] == ["code lab plan"] and stats["charts"] == 1
    _, stats, notes, sent = run2(monkeypatch, [], [NO_PLAN])
    assert [s["step"] for s in sent] == ["code lab plan"] and stats["built"] == 0 and notes == []


def test_code_lab_2_backup_is_a_simpler_chart(monkeypatch):
    backup = {**SPEC, "transform": [{"calculate": "d * d", "as": "Full"}], "encoding": {**SPEC["encoding"], "y": {"field": "Full", "type": "quantitative"}}}
    result, stats, notes, sent = run2(monkeypatch, [json.dumps({"spec": json.dumps(backup)})], [chart_plan()], reviews=[USE_BACKUP, GOOD_REVIEW])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab chart backup"] and stats["backup"] == 1 and stats["built"] == 1
    assert sent[1]["user"]["plan"]["backup"] and sent[1]["fmt"] == codelab.data.formats()["codeLabVega"]
    w = next(b for b in result["stages"][0]["blocks"] if b["type"] == "widget")
    assert '"Full"' in w["html"] and "backup chart was used" in notes[0]


def test_controls_are_read_from_the_chart_params():
    spec = {"params": [
        {"name": "d", "value": 1024, "bind": {"input": "range", "min": 64, "max": 4096, "step": 64, "name": "Layer size d "}},
        {"name": "mode", "value": "a", "bind": {"input": "select", "options": ["a", "b"], "name": "Mode"}},
        {"name": "log", "value": False, "bind": {"input": "checkbox", "name": "Log scale"}},
        {"name": "hidden", "value": 3},  # no bind: not a control
        {"name": "pick", "select": "point"},  # a selection, not a control
    ]}
    assert codelab.controls_from_spec(spec) == [
        {"id": "d", "kind": "slider", "label": "Layer size d", "min": 64, "max": 4096, "step": 64, "value": 1024},
        {"id": "mode", "kind": "select", "label": "Mode", "value": "a"},
        {"id": "log", "kind": "checkbox", "label": "Log scale", "value": False},
    ]
    assert codelab.controls_from_spec(None) == [] and codelab.controls_from_spec({"params": "x"}) == []


SLIM_PLAN = json.dumps({"build": True, "lesson": "parameters grow with rank", "title": "Rank and size", "afterBlock": 0,
                        "backup": "one rank slider and the LoRA line", "checks": [], "vegaLite": json.dumps(SPEC)})


def test_the_slim_vega_lite_plan_code_lab_4_uses(monkeypatch):
    sent = fake_writer2(monkeypatch, [], [SLIM_PLAN])
    fake_jev(monkeypatch)
    fake_checks(monkeypatch, lambda html: [])
    notes: list[str] = []
    result, stats = codelab.design_code_widgets(None, outline([TEXT]), UsageLog("code-lab-3", "low"), notes, jev_key="k",
                                                settings=codelab.CodeLabSettings.for_effort("low", slim=True))
    assert [s["step"] for s in sent] == ["code lab plan"] and stats["charts"] == 1 and stats["built"] == 1
    assert sent[0]["system"] == codelab.data.prompt("CODE_LAB_3_PLAN_SYSTEM") and sent[0]["fmt"] == codelab.data.formats()["codeLabPlanSlim"]
    assert "controls" not in sent[0]["fmt"]["properties"] and "visuals" not in sent[0]["fmt"]["properties"]
    w = next(b for b in result["stages"][0]["blocks"] if b["type"] == "widget")
    assert json.loads(w["plan"])["controls"][0]["id"] == "d"  # read from the chart's params, not written by the model


def test_code_lab_2_keeps_its_full_plan():
    assert codelab.CodeLabSettings.for_effort("low").slim is False


def test_rejected_charts_are_kept_for_test_runs(monkeypatch):
    # The first chart is rejected and its backup unusable: the page gets text, the rejected chart is kept in the stats.
    result, stats, _, _ = run2(monkeypatch, [json.dumps({"spec": "nope"})], [chart_plan()], reviews=[USE_BACKUP])
    assert stats["built"] == 0 and len(stats["rejected"]) == 1
    r = stats["rejected"][0]
    assert r["kind"] == "first" and r["section"] == "S0" and "XW.vega(" in r["html"] and r["scores"] and r["lesson"]
    assert not any(b["type"] == "widget" for st in result["stages"] for b in st["blocks"])  # never on the page


def test_kept_charts_leave_nothing_rejected(monkeypatch):
    _, stats, _, _ = run2(monkeypatch, [], [chart_plan()])
    assert stats["built"] == 1 and "rejected" not in stats


def test_debug_rejected_is_code_lab_only():
    base = {"mode": "concept", "text": "bagging", "debugRejected": True}
    assert parse_generate({**base, "quality": "code-lab-3"})["debugRejected"] is True
    assert "debugRejected" not in parse_generate({**base, "quality": "quality-plus"})
    assert "debugRejected" not in parse_generate({**base, "quality": "code-lab-2", "debugRejected": "yes"})


def test_code_lab_2_unusable_backup_leaves_text(monkeypatch):
    _, stats, notes, sent = run2(monkeypatch, [json.dumps({"spec": "nope"})], [chart_plan()], reviews=[USE_BACKUP])
    assert stats["built"] == 0 and "chart and its backup were both rejected" in notes[0]


def test_code_lab_plans_without_the_chart_route(monkeypatch):
    sent = fake_writer(monkeypatch, [reply()])
    fake_jev(monkeypatch)
    fake_checks(monkeypatch, lambda html: [])
    holder: list = []
    real = codelab.write_text

    def spy(client, usage, step, **kw):
        holder.append((step, kw["system"], kw.get("fmt")))
        return real(client, usage, step, **kw)

    monkeypatch.setattr(codelab, "write_text", spy)
    codelab.design_code_widgets(None, outline([TEXT]), UsageLog("code-lab", "low"), [], jev_key="k")
    assert "Vega-Lite chart" not in holder[0][1] and "vegaLite" not in holder[0][2]["properties"]


def test_a_chart_section_needs_no_build_call(monkeypatch):
    result, stats, notes, sent = run2(monkeypatch, [], [chart_plan()])
    assert [s["step"] for s in sent] == ["code lab plan"] and stats["built"] == 1 and stats["charts"] == 1 and notes == []
    w = next(b for b in result["stages"][0]["blocks"] if b["type"] == "widget")
    assert w["html"].startswith('<div id="xw-vega"></div><script>XW.vega("xw-vega", ') and '"datum.r * (d + d)"' in w["html"]
    assert "vegaLite" not in json.loads(w["plan"])  # the plan kept on the page doesn't repeat the spec


def test_a_broken_spec_gets_a_chart_repair_at_the_page_effort(monkeypatch):
    result, stats, notes, sent = run2(monkeypatch, [json.dumps({"spec": json.dumps(SPEC)})], [chart_plan("{not json")], effort="medium")
    assert [s["step"] for s in sent] == ["code lab plan", "code lab chart repair"] and sent[1]["effort"] == "medium"
    assert "isn't one valid JSON object" in sent[1]["user"]["problems"][0] and "vegaLite" not in sent[1]["user"]["plan"]
    assert sent[1]["fmt"] == codelab.data.formats()["codeLabVega"] and stats["built"] == 1 and stats["repaired"] == 1


def test_a_chart_that_never_passes_stays_text(monkeypatch):
    # A repair that comes back unusable ends the tries (as for custom widgets); one that comes back still failing gets another.
    bad = json.dumps({"spec": "{still not json"})
    result, stats, notes, sent = run2(monkeypatch, [bad], [chart_plan("{not json")])
    assert stats["built"] == 0 and stats["repaired"] == 1 and "the chart failed its checks" in notes[0]
    failing = json.dumps({"spec": json.dumps(SPEC)})
    result, stats, notes, sent = run2(monkeypatch, [failing, failing], [chart_plan()], checker=lambda html: ["it is broken"])
    assert stats["built"] == 0 and stats["repaired"] == codelab.MAX_REPAIRS and "it is broken" in notes[0]


def test_a_rejected_chart_is_fixed_as_a_chart(monkeypatch):
    result, stats, notes, sent = run2(monkeypatch, [json.dumps({"spec": json.dumps(SPEC)})], [chart_plan()], reviews=[FIXABLE, GOOD_REVIEW])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab chart repair"] and stats["built"] == 1
    assert any("not what the lesson" in p for p in sent[1]["user"]["problems"])


def test_vega_problems_and_html():
    assert codelab.vega_problems(SPEC) == []
    assert codelab.vega_problems(None)[0].startswith("the Vega-Lite spec is missing")
    hostile = {**SPEC, "data": {"url": "https://example.com/x.csv"}, "mark": {"type": "image", "href": "x"}}
    assert '"href", "url"' in codelab.vega_problems(hostile)[0]
    html = codelab.vega_html({**SPEC, "title": "</script><b>“quoted”</b>"})
    assert "</script><b>" not in html.split("<script>", 1)[1][:-len("</script>")] and "“" not in html  # can't close the tag; ASCII only
    assert codelab.static_problems(html) == []
    assert codelab.read_vega(json.dumps(SPEC)) == SPEC and codelab.read_vega("nope") is None


@needs_node
@needs_jsdom
def test_a_vega_chart_runs_in_the_check():
    html = codelab.vega_html(SPEC)
    good = {**CHART_PLAN, "checks": [{"set": [{"control": "d", "value": 512}, {"control": "r", "value": 8}], "readout": "LoRA", "expr": "r * (d + d)"}]}
    problems, report = codelab.check_built(html, good)
    assert problems == [] and report["ids"]["d"]["type"] == "range"
    assert report["facts"]["controls"][0]["label"] == "Layer size d" and "Rank r" in report["facts"]["labels"]
    assert report["facts"]["changes"][0]["redrawn"] is True
    wrong = {**CHART_PLAN, "checks": [{"set": [{"control": "d", "value": 512}, {"control": "r", "value": 8}], "readout": "LoRA", "expr": "r * d"}]}
    problems, _ = codelab.check_built(html, wrong)
    assert len(problems) == 1 and 'field "LoRA" is 8192' in problems[0] and "4096" in problems[0]
    broken = codelab.vega_html({**SPEC, "mark": "nonsense"})
    assert any("throws when it loads" in p for p in codelab.check_built(broken, CHART_PLAN)[0])


BARS = {
    "params": [{"name": "n", "value": 7, "bind": {"input": "range", "min": 1, "max": 70, "step": 1, "name": "Parameters N (billions) "}}],
    "data": {"values": [{"part": "Weights", "mult": 1}, {"part": "Gradients", "mult": 1}, {"part": "Adam moments", "mult": 2}]},
    "transform": [{"calculate": "datum.mult * n", "as": "gb"}],
    "mark": "bar", "width": 400, "height": 240,
    # A fixed y range: bars that all scale with n would otherwise look identical as the axis rescales (checked below).
    "encoding": {"x": {"field": "part", "type": "nominal", "title": "Part"},
                 "y": {"field": "gb", "type": "quantitative", "title": "Stored numbers (billions)", "scale": {"domain": [0, 150]}}},
}


@needs_node
@needs_jsdom
def test_a_chart_that_draws_nothing_fails():
    # Seen live: axes, legend and slider drew, but every bar had no height. Vega's own warning is quoted as a hint.
    assert codelab.check_widget(codelab.vega_html(BARS)) == []
    same_field = {**BARS, "encoding": {**BARS["encoding"], "y2": {"field": "gb"}}}
    problems = codelab.check_widget(codelab.vega_html(same_field))
    assert len(problems) == 1 and "bars (rect marks) have no visible size" in problems[0] and "after" not in problems[0]
    log_bars = {**BARS, "encoding": {**BARS["encoding"], "y": {**BARS["encoding"]["y"], "scale": {"type": "log"}}}}
    problems = codelab.check_widget(codelab.vega_html(log_bars))
    assert "no visible size" in problems[0] and "Log scale domain includes zero" in problems[0]
    log_line = {**SPEC, "encoding": {**SPEC["encoding"], "y": {**SPEC["encoding"]["y"], "scale": {"type": "log"}}}}
    assert codelab.check_widget(codelab.vega_html(log_line)) == []  # a log scale is fine for lines


@needs_node
@needs_jsdom
def test_a_chart_that_empties_when_a_control_moves_fails():
    # Fine at load; moving the slider to its other end filters every row away.
    filtered = {**BARS, "transform": [*BARS["transform"], {"filter": "n < 50"}]}
    problems = codelab.check_widget(codelab.vega_html(filtered))
    assert len(problems) == 1 and "draws no data after moving \"n\" to 70" in problems[0]


@needs_node
@needs_jsdom
def test_the_check_blocks_eval_like_the_real_frame():
    # The frame's Content-Security-Policy has no 'unsafe-eval'. A chart once passed the check and stayed blank in the
    # browser, because Vega compiled its formulas with new Function; the kit now uses Vega's interpreter.
    uses_eval = '<input id=a type=range><span id=o></span><script>var f=new Function("x","return x*2");a.oninput=function(){o.textContent=f(+a.value)};</script>'
    assert any("blocks eval" in p for p in codelab.check_widget(uses_eval))
    inline = """<button id=b onclick="document.getElementById('o').textContent='clicked'">Go</button><span id=o>no</span><script>var x=1;</script>"""
    assert codelab.check_widget(inline) == []  # inline handlers are allowed in the frame ('unsafe-inline')
    assert codelab.check_widget(codelab.vega_html(SPEC)) == []


def test_the_code_lab_2_request():
    r = parse_generate({"mode": "concept", "text": "bagging", "quality": "code-lab-2", "effort": "low", "reviewRounds": 3})
    assert r["quality"] == "code-lab-2" and r["effort"] == "low" and "reviewRounds" not in r


@pytest.mark.parametrize("quality,slim,marks", [("code-lab-2", False, False), ("code-lab-3", True, False)])
def test_pipeline_gives_code_lab_3_the_chart_only_plan(monkeypatch, quality, slim, marks):
    seen: dict = {}
    monkeypatch.setattr(pipeline, "ask_brief", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "choose_design", lambda *a, **k: None)

    class Spy:
        def __init__(self, client, usage, jev_key, settings):
            seen["settings"] = settings

        def feed(self, chunk):
            pass

        def finish(self, outline, notes, progress=None):
            return outline, {"built": 0, "sections": 0, "repaired": 0}

    def fake_write_outline(client, request, emit, usage, system, parse, effort, *a, **k):
        seen["request"] = request
        p = parse(SAMPLE["outline"])
        return {"outline": p["outline"], "warnings": p["warnings"]}

    monkeypatch.setattr(pipeline, "Streamed", Spy)
    monkeypatch.setattr(pipeline, "write_outline", fake_write_outline)
    monkeypatch.setattr(pipeline, "clarity_check", lambda client, jev_key, spec, usage, only=None: (spec, "ok"))
    monkeypatch.setattr(pipeline, "fact_check", lambda client, jev_key, spec, usage, section_check=False, only=None: (spec, "ok"))
    result = pipeline.generate(None, {"mode": "concept", "text": "x", "quality": quality, "effort": "low", "planThinking": False}, lambda e: None)
    s = seen["settings"]
    assert (s.vega, s.slim, s.think, s.plan) == (True, slim, False, "low") and result["usage"]["quality"] == quality
    assert (s.marks, s.fix_first) == (marks, marks)
    assert ('"interaction": {"chart"' in seen["request"]) == marks and codelab.PLANNER_NOTE in seen["request"]  # 4.0: the writer also marks charts


def test_the_code_lab_3_request():
    r = parse_generate({"mode": "concept", "text": "bagging", "quality": "code-lab-3", "effort": "low", "extras": ["examples"], "reviewRounds": 3})
    assert r["quality"] == "code-lab-3" and r["extras"] == ["examples"] and "reviewRounds" not in r


def test_plan_thinking_is_code_lab_2_only():
    base = {"mode": "concept", "text": "bagging", "effort": "low"}
    assert parse_generate({**base, "quality": "code-lab-2", "planThinking": False})["planThinking"] is False
    assert parse_generate({**base, "quality": "code-lab-2", "planThinking": True})["planThinking"] is True
    assert "planThinking" not in parse_generate({**base, "quality": "code-lab-2"})  # absent = thinking on
    assert "planThinking" not in parse_generate({**base, "quality": "code-lab-2", "planThinking": "no"})
    assert parse_generate({**base, "quality": "code-lab-3", "planThinking": False})["planThinking"] is False
    assert "planThinking" not in parse_generate({**base, "quality": "code-lab", "planThinking": False})
    assert "planThinking" not in parse_generate({**base, "quality": "quality-plus", "planThinking": False})


# ---------- the mode ----------


def test_the_code_lab_request():
    r = parse_generate({"mode": "concept", "text": "bagging", "quality": "code-lab", "extras": ["examples"], "reviewRounds": 3})
    assert r["quality"] == "code-lab" and r["extras"] == ["examples"] and "reviewRounds" not in r


def test_pipeline_plans_prose_only_then_the_model_writes_the_widgets(monkeypatch):
    stages = copy.deepcopy(SAMPLE["outline"]["stages"][:2])
    outl = {**SAMPLE["outline"], "stages": stages}
    seen: dict = {}
    monkeypatch.setattr(pipeline, "ask_brief", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "choose_design", lambda *a, **k: None)

    def fake_write_outline(client, request, emit, usage, system, parse, effort, *a, **k):
        seen["request"], seen["system"] = request, system
        k["on_text"](None)  # the planner's reply streams in; Code Lab starts each section as it closes
        for c in chunks(json.dumps(outl), 11):
            k["on_text"](c)
        p = parse(outl)
        return {"outline": p["outline"], "warnings": p["warnings"]}

    monkeypatch.setattr(pipeline, "write_outline", fake_write_outline)
    monkeypatch.setattr(pipeline, "write_examples", lambda client, spec, usage: spec)
    monkeypatch.setattr(pipeline, "clarity_check", lambda client, jev_key, spec, usage, only=None: (spec, "ok"))
    monkeypatch.setattr(pipeline, "fact_check", lambda client, jev_key, spec, usage, section_check=False, only=None: (spec, "ok"))
    sent = fake_writer(monkeypatch, [reply(afterBlock=0), reply(afterBlock=0)])
    asked = fake_jev(monkeypatch)
    monkeypatch.setattr(pipeline, "choose_design", lambda *a, **k: None)
    fake_checks(monkeypatch, lambda html: [])
    started = spy_sections(monkeypatch)
    steps: list[tuple] = []

    result = pipeline.generate(None, {"mode": "concept", "text": "Explain averaging", "quality": "code-lab"}, lambda e: steps.append((e["type"], e.get("step"), e.get("status"), e.get("detail"))), jev_key="k")
    spec = result["spec"]
    assert codelab.PLANNER_NOTE in seen["request"] and seen["system"] == pipeline._prompt("OUTLINE_SYSTEM")
    assert sum(b["type"] == "widget" for st in spec["stages"] for b in st["blocks"]) == 2
    assert len(sent) == 4 and len(asked) == 2 and len(asked.reviews) == 2  # a Jev gate, a plan, a build and a review per section
    assert started == [1, 1]  # both sections started from the stream and were reused
    assert ("step", "interaction", "done", "2 of 2 sections got a custom interaction") in steps
    models = {s["model"] for s in result["usage"]["steps"]} if result["usage"]["steps"] and "model" in result["usage"]["steps"][0] else None
    assert result["usage"]["quality"] == "code-lab"


# ---------- Code Lab 3.0: full Vega charts ----------

VG = {
    "$schema": "https://vega.github.io/schema/vega/v6.json", "width": 400, "height": 240, "padding": 8,
    "signals": [{"name": "d", "value": 1024, "bind": {"input": "range", "min": 64, "max": 4096, "step": 64, "name": "Layer size d "}}],
    "data": [{"name": "t", "transform": [{"type": "sequence", "start": 1, "stop": 65, "as": "r"},
                                          {"type": "formula", "as": "p", "expr": "datum.r * (d + d)"}]}],
    "scales": [{"name": "x", "type": "linear", "domain": {"data": "t", "field": "r"}, "range": "width"},
               {"name": "y", "type": "linear", "domain": {"data": "t", "field": "p"}, "range": "height", "nice": True, "zero": True}],
    "axes": [{"orient": "bottom", "scale": "x", "title": "Rank r"}, {"orient": "left", "scale": "y", "title": "Trainable parameters"}],
    "marks": [{"type": "line", "from": {"data": "t"},
               "encode": {"update": {"x": {"scale": "x", "field": "r"}, "y": {"scale": "y", "field": "p"}, "stroke": {"signal": "xw_c0"}}}}],
}
VG_PLAN = json.dumps({"build": True, "lesson": "parameters grow with rank", "title": "Rank and size", "afterBlock": 0,
                      "backup": "one rank slider and one line", "checks": [], "vega": json.dumps(VG)})


def test_code_lab_3_settings_and_mode_wiring():
    s = codelab.CodeLabSettings.for_effort("low", full_vega=True)
    assert s.full_vega and s.slim and s.vega
    assert custom_settings().full_vega is False  # Code Lab 4.0 stays Vega-Lite


def test_code_lab_3_plans_a_full_vega_chart(monkeypatch):
    sent = fake_writer2(monkeypatch, [], [VG_PLAN])
    fake_jev(monkeypatch)
    fake_checks(monkeypatch, lambda html: [])
    notes: list[str] = []
    result, stats = codelab.design_code_widgets(None, outline([TEXT]), UsageLog("code-lab-3", "low"), notes, jev_key="k",
                                                settings=codelab.CodeLabSettings.for_effort("low", full_vega=True))
    assert [s["step"] for s in sent] == ["code lab plan"] and stats["charts"] == 1 and stats["built"] == 1
    assert sent[0]["system"] == codelab.data.prompt("CODE_LAB_VG_PLAN_SYSTEM") and sent[0]["fmt"] == codelab.data.formats()["codeLabPlanVg"]
    assert "Vega-Lite" not in sent[0]["system"].split("# Vega rules")[0].replace("not Vega-Lite", "")
    w = next(b for b in result["stages"][0]["blocks"] if b["type"] == "widget")
    assert '"signals"' in w["html"] and json.loads(w["plan"])["controls"][0]["id"] == "d"  # read from the bound signal
    assert "vega" not in json.loads(w["plan"]) and "vegaLite" not in json.loads(w["plan"])


def test_code_lab_3_repairs_with_the_full_vega_prompt(monkeypatch):
    # A Vega-Lite spec where full Vega was asked: caught before drawing, repaired with the full Vega prompt.
    bad = json.dumps({**json.loads(VG_PLAN), "vega": json.dumps(SPEC)})
    sent = fake_writer2(monkeypatch, [json.dumps({"spec": json.dumps(VG)})], [bad])
    fake_jev(monkeypatch)
    fake_checks(monkeypatch, lambda html: [])
    _, stats = codelab.design_code_widgets(None, outline([TEXT]), UsageLog("code-lab-3", "low"), [], jev_key="k",
                                           settings=codelab.CodeLabSettings.for_effort("low", full_vega=True))
    assert [s["step"] for s in sent] == ["code lab plan", "code lab chart repair"] and stats["built"] == 1
    assert sent[1]["system"] == codelab.data.prompt("CODE_LAB_VG_REPAIR") and "write a full Vega spec" in sent[1]["user"]["problems"][0]


def test_full_vega_controls_and_spec_problems():
    assert codelab.is_full_vega(VG) and not codelab.is_full_vega(SPEC)
    assert codelab.is_full_vega({k: v for k, v in VG.items() if k != "$schema"})  # by shape
    assert codelab.controls_from_spec(VG) == [{"id": "d", "kind": "slider", "label": "Layer size d", "min": 64, "max": 4096, "step": 64, "value": 1024}]
    assert codelab.vega_problems(VG, full=True) == []
    assert codelab.vega_problems(None, full=True)[0].startswith("the Vega spec is missing")
    # A control nothing reads.
    extra = {"name": "k", "value": 2, "bind": {"input": "range", "min": 1, "max": 5, "step": 1, "name": "k "}}
    dead = {**VG, "signals": VG["signals"] + [extra]}
    assert len(codelab.vega_problems(dead, full=True)) == 1 and 'control "k" is never used' in codelab.vega_problems(dead, full=True)[0]
    # A field called "k" doesn't count as reading the control k: only expressions do.
    fielded = {**dead, "marks": dead["marks"] + [{"type": "text", "encode": {"update": {"text": {"field": "k"}}}}]}
    assert 'control "k" is never used' in codelab.vega_problems(fielded, full=True)[0]
    used = {**dead, "marks": dead["marks"] + [{"type": "rule", "encode": {"update": {"x": {"scale": "x", "signal": "k"}}}}]}
    assert codelab.vega_problems(used, full=True) == []
    # The LoRA bug: a slider r next to a sequence field r, so a formula reads the slider instead of each row.
    clash = {**VG, "signals": VG["signals"] + [{"name": "r", "value": 8, "bind": {"input": "range", "min": 1, "max": 64, "step": 1, "name": "Rank r "}}]}
    assert 'same name as a data field ("r")' in codelab.vega_problems(clash, full=True)[0]
    assert codelab.vega_problems(SPEC) == []  # Vega-Lite (Code Lab 2.0/4.0) is unchanged


def test_chart_summary_reads_full_vega_axes():
    w = {"title": "Rank and size", "html": codelab.vega_html(VG), "plan": json.dumps({"lesson": "x"})}
    s = codelab.chart_summary(w)
    assert s["axes"] == ["Rank r", "Trainable parameters"] and s["controls"] == ["Layer size d (64 to 4096)"]


@needs_node
@needs_jsdom
def test_a_full_vega_chart_runs_in_the_check():
    html = codelab.vega_html(VG)
    plan_ = {"controls": codelab.controls_from_spec(VG), "visuals": [],
             "checks": [{"set": [{"control": "d", "value": 512}, {"control": "r", "value": 8}], "readout": "p", "expr": "r * (d + d)"}]}
    problems, report = codelab.check_built(html, plan_)
    assert problems == [] and report["ids"]["d"]["type"] == "range"
    assert "Rank r" in report["facts"]["labels"] and report["facts"]["changes"][0]["redrawn"] is True
    wrong = {**plan_, "checks": [{**plan_["checks"][0], "expr": "r * d"}]}
    assert 'field "p" is 8192' in codelab.check_built(html, wrong)[0][0]
    empty = {**VG, "marks": [{"type": "rect", "from": {"data": "t"}, "encode": {"update": {
        "x": {"scale": "x", "field": "r"}, "width": {"value": 4}, "y": {"scale": "y", "field": "p"}, "y2": {"scale": "y", "field": "p"}}}}]}
    assert any("no visible size" in p for p in codelab.check_built(codelab.vega_html(empty), plan_)[0])


@needs_node
@needs_jsdom
def test_a_mark_drawn_far_outside_the_chart_fails():
    # Seen live: {"scale": "x", "value": 300} meant 300 pixels, read as the data value 300, so the label landed 11,000 px away.
    stray = {**VG, "marks": VG["marks"] + [{"type": "text", "encode": {"update": {
        "x": {"scale": "x", "value": 3000}, "y": {"value": 20}, "text": {"value": "label"}}}}]}
    problems, _ = codelab.check_built(codelab.vega_html(stray), {"controls": codelab.controls_from_spec(VG), "visuals": []})
    assert any("far outside the chart" in p and "not N pixels" in p for p in problems)
    corner = {**VG, "marks": VG["marks"] + [{"type": "text", "encode": {"update": {"x": {"value": 8}, "y": {"value": 12}, "text": {"value": "label"}}}}]}
    assert codelab.check_built(codelab.vega_html(corner), {"controls": codelab.controls_from_spec(VG), "visuals": []})[0] == []


# ---------- the checks fail closed, and every control must change what is drawn (Oct 22 audit) ----------


def test_a_check_that_cannot_run_fails_closed(monkeypatch):
    # Seen live: a blank chart and two charts that threw on load reached pages without a working check.
    calls = []

    class Proc:
        def __init__(self, out):
            self.stdout, self.stderr = out, "boom"

    def crash(args, **kw):
        calls.append(1)
        return Proc("")  # no output: nothing to read

    monkeypatch.setattr(codelab, "_node", lambda: "node")
    monkeypatch.setattr(codelab.subprocess, "run", crash)
    assert codelab.run_checker(GOOD)["problems"] == [codelab.CHECK_FAILED] and len(calls) == codelab.CHECK_RUNS
    # Node ran, but the widget couldn't be run (jsdom failed to load): nothing is known to work.
    ran_nothing = json.dumps({"problems": [], "runtime": False, "runtimeError": "x"})
    monkeypatch.setattr(codelab.subprocess, "run", lambda args, **kw: Proc(ran_nothing))
    assert codelab.run_checker(GOOD)["problems"] == [codelab.CHECK_FAILED]
    # A syntax error is still reported as itself.
    syntax = json.dumps({"problems": ["JavaScript syntax error"], "runtime": False})
    monkeypatch.setattr(codelab.subprocess, "run", lambda args, **kw: Proc(syntax))
    assert codelab.run_checker(GOOD)["problems"] == ["JavaScript syntax error"]
    # No node at all (a dev machine): the static checks only, as before.
    monkeypatch.setattr(codelab, "_node", lambda: None)
    assert codelab.run_checker(GOOD)["problems"] == []


def test_notes_meant_for_us_are_static_problems():
    leak = GOOD.replace("f();</script>", "f();</script><p class=xw-note>Note: the brief's example gives d = 2.5</p>")
    assert any("note meant for you" in p for p in codelab.static_problems(leak))
    in_js = GOOD.replace("f();</script>", 'f();XW.note(document.body, "As the plan says, d = 2.5");</script>')
    assert codelab.leaked_note(in_js)
    # Comments are not shown, and "the plan" is normal reading in agent topics.
    assert codelab.leaked_note(GOOD.replace("<script>", "<script>// the brief's example\n")) is None
    assert codelab.leaked_note('<p>The agent updates the plan after each observation.</p><script>var a = "plan";</script>') is None


KIT_SCALE = ('<div id=r></div><script>var r=document.getElementById("r");var box;'
             'var s=XW.slider(r,{id:"k",label:"Scale",min:1,max:10,value:2,onInput:draw});box=XW.el("div",{},r);'
             'function draw(){var k=s.get();box.innerHTML="";'
             'var c=XW.chart(box,{xmin:0,xmax:3,ymin:0,ymax:YMAX,xlabel:"x",ylabel:"y"});'
             '[1,2,3].forEach(function(v){XW.el("circle",{cx:c.x(v),cy:c.y(v*k),r:4},c.g)})}draw();</script>')


@needs_node
@needs_jsdom
def test_a_control_that_only_rescales_the_axis_fails():
    # Seen live (KL penalty vs beta): the axis rescaled with the control, so the curve looked the same at every setting.
    rescaled = codelab.check_widget(KIT_SCALE.replace("YMAX", "3*k"))
    assert len(rescaled) == 1 and 'Moving "k" changes only the axis numbers' in rescaled[0]
    assert codelab.check_widget(KIT_SCALE.replace("YMAX", "30")) == []  # a fixed axis shows the change
    # The same for a Vega chart: bars that all scale with n, on an axis that fits them.
    bars = {**BARS, "encoding": {**BARS["encoding"], "y": {k: v for k, v in BARS["encoding"]["y"].items() if k != "scale"}}}
    assert any('Moving "n" changes only the axis numbers' in p for p in codelab.check_widget(codelab.vega_html(bars)))


@needs_node
@needs_jsdom
def test_every_control_must_change_something_drawn():
    unused = GOOD.replace("<script>", "<label for=b>Unused</label><input id=b type=range min=0 max=5 value=1><script>")
    problems = codelab.check_widget(unused)
    assert len(problems) == 1 and 'Moving "b" changes nothing in the chart' in problems[0]
    # A disabled control is not the reader's to move.
    assert codelab.check_widget(unused.replace("value=1>", "value=1 disabled>")) == []
    # A control that matters only once another has moved (b counts only when a is above 5) is a real control.
    gated = ('<label for=b>B</label><input id=b type=range min=0 max=5 value=1><label for=a>A</label>'
             '<input id=a type=range min=0 max=10 value=3><span id=o></span><script>var a=document.getElementById("a"),'
             'b=document.getElementById("b"),o=document.getElementById("o");function f(){o.textContent=a.value>5?b.value:0}'
             'a.addEventListener("input",f);b.addEventListener("input",f);f();</script>')
    assert codelab.check_widget(gated) == []


@needs_node
@needs_jsdom
def test_an_encoding_vega_lite_drops_fails():
    # Seen live: x2: {"expr": ...} was dropped, so the vector arrows never drew and their slider did nothing.
    arrow = {"params": [{"name": "t", "value": 1, "bind": {"input": "range", "min": 0, "max": 3, "step": 0.1, "name": "t "}}],
             "data": {"values": [{"x": 0, "y": 0}]}, "width": 200, "height": 200,
             "layer": [{"mark": "point", "encoding": {"x": {"field": "x", "type": "quantitative"}, "y": {"field": "y", "type": "quantitative"}}},
                       {"mark": "rule", "encoding": {"x": {"datum": 0}, "y": {"datum": 0}, "x2": {"expr": "t"}, "y2": {"expr": "t"}}}]}
    assert any("Part of the chart is ignored" in p and "x2" in p for p in codelab.check_widget(codelab.vega_html(arrow)))


# ---------- Code Lab 5.0: 4.0's writer marks, Haiku's own code at the page's Effort ----------


def run5(monkeypatch, out: dict, replies: list[str], plans: list[str], reviews=None):
    sent = fake_writer2(monkeypatch, replies, plans)
    fake_jev(monkeypatch, reviews=reviews)
    fake_checks(monkeypatch, lambda html: [])
    st = codelab.Streamed(None, UsageLog("code-lab-5", "low"), "k", custom_settings())
    st.feed(json.dumps(out))
    notes: list[str] = []
    result, stats = st.finish(out, notes)
    return result, stats, notes, sent


def test_code_lab_5_writes_its_own_code_at_the_page_effort(monkeypatch):
    out = marked({"chart": True, "idea": "drag the amount, watch twice it"}, {"chart": False, "idea": ""})
    result, stats, _, sent = run5(monkeypatch, out, [reply()], [plan()])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write"] and {s["effort"] for s in sent} == {"low"}
    assert "plan the interactive widget" in sent[0]["system"] and "vegaLite" not in sent[0]["fmt"]["properties"]
    assert "You are the programmer" in sent[1]["system"] and sent[0]["user"]["hint"] == "drag the amount, watch twice it"
    has = [any(b["type"] == "widget" for b in s["blocks"]) for s in result["stages"]]
    assert has == [True, False] and stats["built"] == 1 and stats["charts"] == 0


def test_code_lab_5_thinking_switch_reaches_the_build(monkeypatch):
    for think in (False, True):
        out = marked({"chart": True, "idea": "a"})
        sent = fake_writer2(monkeypatch, [reply()], [plan()])
        fake_jev(monkeypatch)
        fake_checks(monkeypatch, lambda html: [])
        st = codelab.Streamed(None, UsageLog("code-lab-5", "low"), "k", custom_settings(think=think))
        st.feed(json.dumps(out))
        st.finish(out, [])
        assert [(s["step"], s["thinking"]) for s in sent] == [("code lab plan", think), ("code lab write", think)]


def test_code_lab_5_tries_the_backup_when_its_fix_fails(monkeypatch):
    off = {**GOOD_REVIEW, "controls_on_topic": 0.1, "same_idea": 0.1, "backup_direct": 0.9}
    out = marked({"chart": True, "idea": "a"})
    _, stats, _, sent = run5(monkeypatch, out, [reply(), reply(), reply()], [plan()], reviews=[off, off, GOOD_REVIEW])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write", "code lab repair", "code lab backup"]
    assert stats["built"] == 1 and stats["backup"] == 1 and [r["kind"] for r in stats["rejected"]] == ["first", "fix"]


def test_unmarked_sections_are_capped_and_recaps_stay_text(monkeypatch):
    # Seen live: the writer marked nothing, Jev's gate opened 5 sections, 3 charts were about the same beta.
    out = marked(*[None] * 6)
    out["stages"][5]["label"] = "Summary and cheat sheet"
    result, stats, notes, sent = run5(monkeypatch, out, [reply()] * 6, [plan()] * 6)
    has = [any(b["type"] == "widget" for b in s["blocks"]) for s in result["stages"]]
    assert has == [True, True, True, True, False, False] and stats["built"] == codelab.MAX_MARKED
    # capped before building: only the 4 kept sections were planned and built, nothing paid for and thrown away
    assert len([s for s in sent if s["step"] == "code lab plan"]) == codelab.MAX_MARKED and "rejected" not in stats
    assert any("recap section" in n for n in notes) and any("already has its 4 interactive parts" in n for n in notes)


def test_marked_sections_leave_less_room_for_gate_opened_ones(monkeypatch):
    marks = [{"chart": True, "idea": "a"}, {"chart": True, "idea": "b"}, {"chart": True, "idea": "c"}, None, None, None]
    out = marked(*marks)
    result, stats, _, sent = run5(monkeypatch, out, [reply()] * 6, [plan()] * 6)
    has = [any(b["type"] == "widget" for b in s["blocks"]) for s in result["stages"]]
    assert has == [True, True, True, True, False, False] and len([s for s in sent if s["step"] == "code lab plan"]) == 4


def test_the_cap_counts_kept_parts_so_a_failed_part_frees_its_place(monkeypatch):
    # Oct 24 (Playground): a rejected or failed part still used one of the 4 places, so open sections stayed text
    # while the page kept only about 2 parts. Now the next gate-opened sections are built until 4 are kept.
    fake_jev(monkeypatch)
    built: list[int] = []
    fail = {"S0", "S1"}

    def design(client, usage, outline, si, verdict, note, jev_key, settings):
        label = outline["stages"][si]["label"]
        built.append(si)
        if label in fail:
            return {"notes": [f"{label} failed"], "built": False, "repaired": 0, "backup": False, "chart": False}
        return {"notes": [], "built": True, "repaired": 0, "backup": False, "chart": False, "after": None,
                "widget": {"type": "widget", "title": label, "html": GOOD, "plan": "{}"}}

    monkeypatch.setattr(codelab, "_design_gated", design)
    out = marked(*[None] * 6)
    st = codelab.Streamed(None, UsageLog("code-lab-5", "low"), "k", custom_settings())
    st.feed(json.dumps(out))
    notes: list[str] = []
    result, stats = st.finish(out, notes)
    has = [any(b["type"] == "widget" for b in s["blocks"]) for s in result["stages"]]
    assert has == [False, False, True, True, True, True] and sorted(built) == [0, 1, 2, 3, 4, 5]
    assert not any("already has its 4 interactive parts" in n for n in notes)


def test_the_cap_refill_stops_at_four_and_notes_the_rest(monkeypatch):
    fake_jev(monkeypatch)
    built: list[int] = []

    def design(client, usage, outline, si, verdict, note, jev_key, settings):
        built.append(si)
        label = outline["stages"][si]["label"]
        if si == 0:
            return {"notes": [], "built": False, "repaired": 0, "backup": False, "chart": False}
        return {"notes": [], "built": True, "repaired": 0, "backup": False, "chart": False, "after": None,
                "widget": {"type": "widget", "title": label, "html": GOOD, "plan": "{}"}}

    monkeypatch.setattr(codelab, "_design_gated", design)
    out = marked(*[None] * 7)
    st = codelab.Streamed(None, UsageLog("code-lab-5", "low"), "k", custom_settings())
    st.feed(json.dumps(out))
    notes: list[str] = []
    result, _ = st.finish(out, notes)
    has = [any(b["type"] == "widget" for b in s["blocks"]) for s in result["stages"]]
    assert has == [False, True, True, True, True, False, False] and sorted(built) == [0, 1, 2, 3, 4]
    assert sum("already has its 4 interactive parts" in n for n in notes) == 2


def test_a_custom_widget_is_summarised_from_its_plan_for_the_repeat_check():
    w = {"title": "Average steadier", "html": GOOD, "plan": codelab.plan_text(json.loads(plan()))}
    s = codelab.chart_summary(w)
    assert s["controls"] == ["Amount (0 to 10)"] and s["axes"] == ["twice the amount"] and s["lesson"] == "more samples, steadier average"


def run6(monkeypatch, out: dict, replies: list[str], plans: list[str]):
    """Code Lab 6.0 on a streamed page. The node checker and Jev fail the test if they are ever called."""
    sent = fake_writer2(monkeypatch, replies, plans)

    def no_call(*a, **kw):
        raise AssertionError("Code Lab 6.0 must not run the widget check or ask Jev")

    monkeypatch.setattr(codelab, "run_checker", no_call)
    monkeypatch.setattr(codelab, "jev_ask", no_call)
    st = codelab.Streamed(None, UsageLog("code-lab-6", "low"), "k", codelab.CodeLabSettings.for_effort("low", unchecked=True))
    st.feed(json.dumps(out))
    notes: list[str] = []
    result, stats = st.finish(out, notes)
    return result, stats, notes, sent


FLAT_SPEC = {**SPEC, "transform": [{"calculate": "(d + d)", "as": "LoRA"}, {"calculate": "d * d", "as": "Full"}, {"fold": ["LoRA", "Full"]}]}


def run_math_check(html: str, claims=(), sanity=()) -> dict:
    return codelab.run_checker(html, {"checks": [], "claims": list(claims), "sanity": list(sanity)})


@needs_node
@needs_jsdom
def test_the_check_reports_a_charts_maths_evidence():
    report = run_math_check(codelab.vega_html(SPEC))
    m = report["math"]
    assert "LoRA = datum.r * (d + d)" in m["formulas"] and any(" runs from " in f for f in m["formulas"])
    assert m["controls"][0]["name"] == "d" and m["samples"] and all("rows" in s for s in m["samples"])
    assert "Full" in m["flat"] and "LoRA" not in m["flat"]  # a constant reference line is flat; the LoRA line is not
    assert "LoRA" in run_math_check(codelab.vega_html(FLAT_SPEC))["math"]["flat"]  # the live bug: no row value used


@needs_node
@needs_jsdom
def test_claims_are_tested_by_moving_the_control():
    html = codelab.vega_html(SPEC)
    rises = {"move": "d", "from": 64, "to": 4096, "field": "LoRA", "at": {"r": 8}, "expect": "rises"}
    assert run_math_check(html, [rises])["problems"] == []
    problems = run_math_check(html, [{**rises, "expect": "falls"}])["problems"]
    assert len(problems) == 1 and 'should make "LoRA" fall' in problems[0] and "rose from 1024 to 65540" in problems[0]  # 4 significant digits
    # On the flat chart, LoRA does not depend on the row's rank: a claim about the rank axis catches it.
    flat = run_math_check(codelab.vega_html(FLAT_SPEC), [{"move": "d", "from": 64, "to": 64.5, "field": "LoRA", "at": {"r": 1}, "expect": "rises"}])
    assert flat["problems"] == []  # (moving d still changes it: claims test what the lesson says, the flat check is Jev's)


@needs_node
@needs_jsdom
def test_sanity_rules_catch_impossible_values():
    html = codelab.vega_html(SPEC)
    assert run_math_check(html, sanity=[{"field": "Full", "kind": "nonnegative"}])["problems"] == []
    problems = run_math_check(html, sanity=[{"field": "LoRA", "kind": "probability"}])["problems"]
    assert len(problems) == 1 and '"LoRA" should be a probability' in problems[0]
    shares = {"params": [], "data": {"values": [{"k": "a", "w": 0.5}, {"k": "b", "w": 0.6}]}, "mark": "bar",
              "encoding": {"x": {"field": "k", "type": "nominal"}, "y": {"field": "w", "type": "quantitative"}}}
    assert any("add up to 1.1" in p for p in run_math_check(codelab.vega_html(shares), sanity=[{"field": "w", "kind": "sums_to_one"}])["problems"])


def test_claims_and_sanity_rules_are_validated():
    plan_ = {"claims": [
        {"move": "beta", "from": 0.1, "to": 1, "field": "kl", "at": [{"control": "x", "value": 2}], "expect": "falls"},
        {"move": "beta", "from": 1, "to": 1, "field": "kl", "expect": "falls"},  # no move
        {"move": "beta", "from": 0.1, "to": 1, "field": "kl", "expect": "up"},  # unknown expectation
        {"move": "tie", "from": 0, "to": 1, "field": "a", "at": [], "expect": "rises"},
    ], "sanity": [{"field": "p", "kind": "probability", "by": ""}, {"field": "s", "kind": "sums_to_one", "by": "group"}, {"field": "x", "kind": "odd"}]}
    assert codelab.claim_checks(plan_) == [
        {"move": "beta", "from": 0.1, "to": 1, "field": "kl", "expect": "falls", "at": {"x": 2}},
        {"move": "tie", "from": 0, "to": 1, "field": "a", "expect": "rises"}]
    assert codelab.sanity_checks(plan_) == [{"field": "p", "kind": "probability"}, {"field": "s", "kind": "sums_to_one", "by": "group"}]


def test_the_maths_verdict_acts_only_on_a_clear_no():
    good = {k: 0.8 for k in codelab.MATH_QUESTIONS}
    assert codelab.math_verdict(good, [])["act"] is False
    clear = codelab.math_verdict({**good, "formula_matches": 0.06}, [])
    assert clear["act"] and clear["failed"] == ["formula_matches"] and "differs from the section's formula" in clear["problems"][0]
    soft = codelab.math_verdict({**good, "labels_match": 0.25}, [])
    assert soft["act"] is False and soft["soft"] == ["labels_match"]  # logged only: such flags included good charts
    # A likely flat line is acted on only when code also sees a flat computed field.
    assert codelab.math_verdict({**good, "varies_along_axis": 0.2}, [])["act"] is False
    assert codelab.math_verdict({**good, "varies_along_axis": 0.2}, ["lora"])["failed"] == ["varies_along_axis"]


MATH_EV = {"formulas": ["r runs from 1 to 65 in steps of 1", "lora = r*(1024+1024)"], "controls": [{"name": "r", "label": "Rank r"}],
           "axes": ["x: r", "y: lora"], "samples": [{"set": {"r": 1}, "rows": [{"r": 1, "lora": 2048}, {"r": 64, "lora": 2048}]}], "flat": ["lora"]}


def run_math_flow(monkeypatch, math_answers: list[dict], repair_ok: bool = True, jev_key: str | None = "k"):
    """Code Lab 4.0 on one marked section: the checks report maths evidence; Jev answers the maths review with the next
    of `math_answers` and every quality review as good."""
    sent = fake_writer2(monkeypatch, [FIXED_SPEC] * 4, [SLIM_PLAN])
    fake_checks(monkeypatch, lambda html: [])
    real_inspect = codelab.inspect
    monkeypatch.setattr(codelab, "inspect", lambda html, checks=None: {**real_inspect(html, checks), "math": MATH_EV})
    asked = {"math": 0}
    queue = list(math_answers)

    def jev_ask(key, state, questions):
        usage = {"input_tokens": 100, "output_tokens": 0}
        if "formula_matches" in questions:
            asked["math"] += 1
            a = queue.pop(0) if len(queue) > 1 else queue[0]
            return {"answers": {k: {"p": a.get(k, 0.8)} for k in questions}, "model": "jev-test", "usage": usage}
        return {"answers": {k: {"p": GOOD_REVIEW.get(k, 0.1)} for k in questions}, "model": "jev-test", "usage": usage}

    monkeypatch.setattr(codelab, "jev_ask", jev_ask)
    if not repair_ok:  # the repaired chart fails its code checks every time
        monkeypatch.setattr(codelab, "inspect", lambda html, checks=None: {"problems": ["broken"], "ids": None, "facts": None, "runtime": False, "math": None}
                            if sent and sent[-1]["step"] == "code lab chart repair" else {**real_inspect(html, checks), "math": MATH_EV})
    out = marked({"chart": True, "idea": "move the rank"})
    st = codelab.Streamed(None, UsageLog("code-lab-4", "low"), jev_key, custom_settings())
    st.feed(json.dumps(out))
    notes: list[str] = []
    result, stats = st.finish(out, notes)
    return result, stats, notes, sent, asked


def test_code_lab_7_settings_are_code_lab_5_plus_free():
    s5 = custom_settings()
    s7 = codelab.CodeLabSettings.for_effort("low", free=True)
    assert s7.free is True and s5.free is False and s7.look is True and s5.look is False
    assert {k: v for k, v in vars(s7).items() if k not in ("free", "look")} == {k: v for k, v in vars(s5).items() if k not in ("free", "look")}


def test_code_lab_7_builds_and_repairs_with_the_free_prompt(monkeypatch):
    off = {**GOOD_REVIEW, "controls_on_topic": 0.1, "same_idea": 0.1, "backup_direct": 0.9}
    out = marked({"chart": True, "idea": "a"})
    sent = fake_writer2(monkeypatch, [reply(), reply(), reply()], [plan()])
    fake_jev(monkeypatch, reviews=[off, off, GOOD_REVIEW])
    fake_checks(monkeypatch, lambda html: [])
    monkeypatch.setattr(codelab, "snap_widget", lambda html: None)  # no screenshots: the look check is skipped
    st = codelab.Streamed(None, UsageLog("code-lab-7", "low"), "k", codelab.CodeLabSettings.for_effort("low", free=True, think=False))
    st.feed(json.dumps(out))
    _, stats = st.finish(out, [])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write", "code lab repair", "code lab backup"]
    free = [s for s in sent if s["step"] != "code lab plan"]
    assert all("the design is yours" in s["system"] for s in free) and all(s["thinking"] is False for s in free)
    assert "plan the interactive widget" in sent[0]["system"]  # Code Lab 5.0's plan prompt...
    assert "The programmer also designs the look" in sent[0]["system"]  # ...plus 7.0's note
    assert stats["built"] == 1


def test_code_lab_5_keeps_its_own_builder_prompt(monkeypatch):
    out = marked({"chart": True, "idea": "a"})
    _, _, _, sent = run5(monkeypatch, out, [reply()], [plan()])
    assert "You are the programmer for an AI explainer page" in sent[1]["system"] and "the design is yours" not in sent[1]["system"]
    assert "The programmer also designs the look" not in sent[0]["system"]  # 7.0's plan note is 7.0's only


def test_code_lab_7_is_a_request():
    req = parse_generate({"mode": "concept", "text": "LoRA", "quality": "code-lab-7", "planThinking": False, "reviewRounds": 2})
    assert req["quality"] == "code-lab-7" and req["planThinking"] is False and req["reviewRounds"] == 2


# ---------- Code Lab 7.0: the look check (screenshots judged by Haiku, one repair, the original kept unless better) ----------

FRAMES = [{"label": "start (the reader's first view)", "png": "iVBORw0K"}, {"label": "slider #a at its maximum (10)", "png": "iVBORw0L"}]


def run7(monkeypatch, replies: list[str], looks: list, frames=FRAMES, problems_of=lambda html: []):
    """A one-section Code Lab 7.0 page: Haiku's look answers come from `looks` (a list of problems, or None = the call failed)."""
    sent = fake_writer2(monkeypatch, replies, [plan()])
    fake_jev(monkeypatch)
    fake_checks(monkeypatch, problems_of)
    snaps: list[str] = []
    monkeypatch.setattr(codelab, "snap_widget", lambda html: snaps.append(html) or frames)
    queue = list(looks)
    monkeypatch.setattr(codelab, "look_review", lambda client, usage, fr, pl, title: queue.pop(0))
    out = marked({"chart": True, "idea": "a"})
    st = codelab.Streamed(None, UsageLog("code-lab-7", "low"), "k", codelab.CodeLabSettings.for_effort("low", free=True, think=False))
    st.feed(json.dumps(out))
    notes: list[str] = []
    result, stats = st.finish(out, notes)
    return result, stats, notes, sent, snaps


def widget_html(result: dict) -> str:
    return next(b["html"] for st in result["stages"] for b in st["blocks"] if b.get("type") == "widget")


def test_a_widget_that_looks_fine_is_kept_without_a_repair(monkeypatch):
    result, stats, notes, sent, snaps = run7(monkeypatch, [reply()], [[]])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write"]
    assert widget_html(result) == GOOD and snaps == [GOOD]
    assert stats["look"] == [{"section": "S0", "checked": True, "frames": 2, "problems": []}]


def test_look_problems_get_one_repair_used_only_when_it_looks_better(monkeypatch):
    better = GOOD.replace("Amount", "Amount ")
    result, stats, notes, sent, snaps = run7(monkeypatch, [reply(), reply(better)], [["labels overlap", "bar too thin"], ["bar too thin"]])
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write", "code lab look repair"]
    repair = sent[-1]["user"]
    assert repair["yourPreviousHtml"] == GOOD and repair["problems"][0] == "How it looks (from screenshots): labels overlap"
    assert widget_html(result) == better and snaps == [GOOD, better]
    assert stats["look"][0]["repaired"] is True and stats["look"][0]["after"] == ["bar too thin"]
    assert any("so it was used" in n for n in notes)


def test_a_repair_that_doesnt_look_better_keeps_the_original(monkeypatch):
    worse = GOOD.replace("Amount", "Amount ")
    result, stats, notes, _, _ = run7(monkeypatch, [reply(), reply(worse)], [["labels overlap"], ["text cut off"]])
    assert widget_html(result) == GOOD and stats["look"][0]["repaired"] is False
    assert any("kept as it was" in n for n in notes)


def test_a_repair_that_fails_the_code_checks_keeps_the_original(monkeypatch):
    broken = "<div>broken</div>"
    result, stats, notes, sent, snaps = run7(monkeypatch, [reply(), reply(broken), reply(broken), reply(broken)], [["labels overlap"]],
                                             problems_of=lambda html: ["it throws"] if html == broken else [])
    assert widget_html(result) == GOOD and snaps == [GOOD]  # the broken repair is never photographed
    assert stats["built"] == 1 and stats["look"][0]["repaired"] is False


def test_no_screenshots_or_a_failed_judge_keeps_the_widget(monkeypatch):
    result, stats, _, sent, _ = run7(monkeypatch, [reply()], [], frames=None)
    assert widget_html(result) == GOOD and stats["look"] == [{"section": "S0", "checked": False}]
    result, stats, _, sent, _ = run7(monkeypatch, [reply()], [None])
    assert widget_html(result) == GOOD and stats["look"][0]["checked"] is False
    assert [s["step"] for s in sent] == ["code lab plan", "code lab write"]


def test_code_lab_5_has_no_look_check(monkeypatch):
    monkeypatch.setattr(codelab, "snap_widget", lambda html: pytest.fail("Code Lab 5.0 took screenshots"))
    _, stats, _, _ = run5(monkeypatch, marked({"chart": True, "idea": "a"}), [reply()], [plan()])
    assert "look" not in stats and stats["built"] == 1


def test_the_look_judge_reads_each_frame_with_its_caption(monkeypatch):
    seen: dict = {}

    def write_text(client, usage, step, *, system, user, **kw):
        seen.update(step=step, system=system, user=user, fmt=kw.get("fmt"), thinking=kw.get("thinking"))
        return json.dumps({"problems": ["  labels   overlap ", "", "a", "b", "c", "d"]})

    monkeypatch.setattr(codelab, "write_text", write_text)
    got = codelab.look_review(None, UsageLog("code-lab-7", "low"), FRAMES, json.loads(plan()), "Average steadier")
    assert got == ["labels overlap", "a", "b", "c"]  # cleaned, empties dropped, at most 4
    assert seen["step"] == "code lab look" and seen["thinking"] is True and "screenshots" in seen["system"]
    kinds = [c["type"] for c in seen["user"]]
    assert kinds == ["text", "text", "image", "text", "image"]
    assert seen["user"][1]["text"] == "Frame 1: start (the reader's first view)" and seen["user"][2]["source"]["data"] == "iVBORw0K"
    assert json.loads(seen["user"][0]["text"])["lesson"] == "more samples, steadier average"

