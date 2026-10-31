"""Follow-ups that change a finished widget (codelab_edit.py): the widget's code or spec, its plan and the reader's request go to
Haiku; the mode's own checks, repairs and Jev's review run on the result; a failed change keeps the old widget byte for byte.
Claude and Jev are faked; the playground checker (node) is real."""

from __future__ import annotations

import json
import shutil

import pytest

from explainer import codelab, codelab_edit as ce
from explainer.pricing import UsageLog

from test_codelab import FACTS, GOOD, GOOD_REVIEW, HOPELESS, QUIZ, TEXT, fake_checks, fake_jev, outline, plan
from test_playground import GINI

needs_node = pytest.mark.skipif(not shutil.which("node") or not codelab.PG_SCRIPT.exists(), reason="node not available")

GOOD_V2 = GOOD.replace("a.value*2", "a.value*3")  # the widget after the reader's change
PLAN_V1 = json.loads(plan())
OLD = {"type": "widget", "title": "Average steadier", "html": GOOD, "plan": codelab.plan_text(PLAN_V1)}


def page(*blocks: dict) -> dict:
    return outline([TEXT, *blocks, QUIZ])


def fake_haiku(monkeypatch, plans: list[str] | None = None, builds: list = ()) -> list[dict]:
    """Haiku for Code Lab 7.0 edits: plan-update calls get the next plan, every other call the next build. Records each call."""
    sent: list[dict] = []
    plan_it, build_it = iter(plans or []), iter(builds)

    def write_text(client, usage, step, *, system, user, max_tokens, **kw):
        sent.append({"step": step, "system": system, "user": json.loads(user), "effort": kw["effort"]})
        return next(plan_it) if step == "code lab plan" else (lambda r: r if isinstance(r, str) else json.dumps(r))(next(build_it))

    monkeypatch.setattr(codelab, "write_text", write_text)
    monkeypatch.setattr(codelab, "snap_widget", lambda html: None)  # no browser: the look check fails open
    return sent


def built(html: str, **kw) -> dict:
    return {"build": True, "why": "w", "title": "Average steadier", "afterBlock": 0, "html": html, **kw}


def usage() -> UsageLog:
    return UsageLog("code-lab-7", "medium")


# ---------- reading a finished widget ----------


def test_widget_kinds():
    assert ce.widget_kind(OLD) == "code-lab"
    assert ce.widget_kind({**OLD, "html": codelab.playground_html(GINI)}) == "playground"
    assert ce.widget_kind({"type": "widget", "title": "Creative", "html": GOOD}) is None  # no plan: a Creative widget
    assert ce.widget_kind({**OLD, "html": '<div id="xw-vega"></div><script>XW.vega("xw-vega", {})</script>'}) is None  # a retired chart


def test_the_playground_spec_reads_back_out_of_the_block():
    spec = {**GINI, "title": "</script><b>x</b>"}
    html = codelab.playground_html(spec)
    assert html.count("</script>") == 1  # escaped inside, so the page doesn't end the script early
    back = ce.read_playground_spec(html)
    assert back == {k: spec[k] for k in codelab._PG_STORED if k in spec}
    assert ce.read_playground_spec("<p>no</p>") is None and ce.read_playground_spec("XW.playground({broken") is None


def test_the_plan_reads_back_without_jevs_scores():
    block = {**OLD, "plan": codelab.plan_text(PLAN_V1, {"controls_on_topic": 0.9}, {"p": 1})}
    assert "review" not in ce.read_plan(block) and ce.read_plan(block)["controls"][0]["id"] == "a"
    assert ce.read_plan({"plan": "not json"}) == {} and ce.read_plan({}) == {}


def test_page_kind_follows_the_pages_own_widgets():
    pg = {**OLD, "html": codelab.playground_html(GINI)}
    assert ce.page_kind(page(OLD)) == "code-lab" and ce.page_kind(page(OLD, pg)) == "playground"
    assert ce.page_kind(page(), "code-lab-7") == "code-lab" and ce.page_kind(page(), "playground") == "playground"
    assert ce.page_kind(page(), "quality-plus") is None and ce.page_kind(page({"type": "widget", "title": "C", "html": GOOD})) is None


# ---------- Code Lab 7.0 ----------


def test_a_code_lab_widget_is_changed_from_its_code_plan_and_the_request(monkeypatch):
    new_plan = plan(lesson="triple the amount")
    sent = fake_haiku(monkeypatch, [new_plan], [built(GOOD_V2)])
    fake_checks(monkeypatch, lambda html: [])
    asked = fake_jev(monkeypatch)
    out = page(OLD)
    r = ce.edit_widget(None, usage(), "k", out, 0, 1, "triple the amount instead of doubling it", effort="medium")
    assert r["changed"] is True and r["block"]["html"] == GOOD_V2 and r["notes"] == []
    assert r["block"]["title"] == "Average steadier" and json.loads(r["block"]["plan"])["lesson"] == "triple the amount"
    assert json.loads(r["block"]["plan"])["review"]["controls_on_topic"] == 0.9  # Jev reviewed the new widget
    assert [s["step"] for s in sent] == ["code lab plan", "code lab edit"]
    plan_call, edit_call = sent
    assert "update the plan of a finished widget" in plan_call["system"]
    assert plan_call["user"]["currentPlan"]["controls"][0]["id"] == "a" and plan_call["user"]["request"] == "triple the amount instead of doubling it"
    assert "change a finished widget" in edit_call["system"] and "You are the programmer" in edit_call["system"]
    assert edit_call["user"]["yourPreviousHtml"] == GOOD and edit_call["user"]["request"].startswith("triple")
    assert edit_call["user"]["plan"]["lesson"] == "triple the amount"
    # Haiku isn't sent every widget's code: the section shows the widget as a title
    assert not any("<script>" in json.dumps(b) for b in edit_call["user"]["section"]["blocks"])
    assert asked.reviews and "section" in asked.reviews[0]
    assert out["stages"][0]["blocks"][1] == OLD  # the page it was given is untouched


def test_a_broken_change_is_repaired_with_the_exact_problems(monkeypatch):
    sent = fake_haiku(monkeypatch, [plan()], [built(GOOD_V2.replace("<script>", "<script>oops(")), built(GOOD_V2)])
    fake_checks(monkeypatch, lambda html: ["it throws when it loads"] if "oops" in html else [])
    fake_jev(monkeypatch)
    r = ce.edit_widget(None, usage(), "k", page(OLD), 0, 1, "triple it")
    assert r["changed"] is True and r["block"]["html"] == GOOD_V2
    assert [s["step"] for s in sent] == ["code lab plan", "code lab edit", "code lab repair"]
    assert sent[2]["user"]["problems"] == ["it throws when it loads"] and "oops" in sent[2]["user"]["yourPreviousHtml"]


def test_a_change_that_cannot_be_made_keeps_the_old_widget(monkeypatch):
    out = page(OLD)
    # the builder says no
    fake_haiku(monkeypatch, [plan()], [built("", build=False, why="the section has no such quantity")])
    fake_checks(monkeypatch, lambda html: [])
    fake_jev(monkeypatch)
    r = ce.edit_widget(None, usage(), "k", out, 0, 1, "add a temperature slider")
    assert r["changed"] is False and r["block"] is OLD
    assert "Couldn't change the interactive part in \"S0\"" in r["notes"][0] and "no such quantity" in r["notes"][0] and "kept as it was" in r["notes"][0]
    # the planner says the section can't support it
    fake_haiku(monkeypatch, [plan(build=False)], [])
    r = ce.edit_widget(None, usage(), "k", out, 0, 1, "add a temperature slider")
    assert r["changed"] is False and r["block"] is OLD and "doesn't support" in r["notes"][0]
    # it never passes its checks
    fake_haiku(monkeypatch, [plan()], [built(GOOD_V2)] * 5)
    fake_checks(monkeypatch, lambda html: ["it is broken"])
    r = ce.edit_widget(None, usage(), "k", out, 0, 1, "triple it")
    assert r["changed"] is False and r["block"] is OLD and "failed its checks (it is broken)" in r["notes"][0]


def test_a_rejected_change_gets_one_fix_and_is_then_kept_out(monkeypatch):
    sent = fake_haiku(monkeypatch, [plan()], [built(GOOD_V2), built(GOOD_V2)])
    fake_checks(monkeypatch, lambda html: [])
    fake_jev(monkeypatch, reviews=[HOPELESS])
    r = ce.edit_widget(None, usage(), "k", page(OLD), 0, 1, "triple it")
    assert r["changed"] is False and r["block"] is OLD and "reviewer rejected" in r["notes"][0]
    assert [s["step"] for s in sent] == ["code lab plan", "code lab edit", "code lab repair"]  # exactly one fix


def test_a_rejected_change_that_the_fix_rescues_is_used(monkeypatch):
    fake_haiku(monkeypatch, [plan()], [built(GOOD_V2), built(GOOD_V2)])
    fake_checks(monkeypatch, lambda html: [])
    fake_jev(monkeypatch, reviews=[HOPELESS, GOOD_REVIEW])
    r = ce.edit_widget(None, usage(), "k", page(OLD), 0, 1, "triple it")
    assert r["changed"] is True and r["block"]["html"] == GOOD_V2


def test_a_creative_widget_is_kept_without_asking_haiku(monkeypatch):
    sent = fake_haiku(monkeypatch)
    creative = {"type": "widget", "title": "Drag it", "html": GOOD}
    r = ce.edit_widget(None, usage(), "k", page(creative), 0, 1, "change it")
    assert r["changed"] is False and r["block"] is creative and sent == [] and "can't be edited yet" in r["notes"][0]


def test_edit_widget_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(codelab, "write_text", boom)
    r = ce.edit_widget(None, usage(), "k", page(OLD), 0, 1, "triple it")
    assert r["changed"] is False and r["block"] is OLD and r["notes"]


# ---------- Playground ----------


def playground_block(spec: dict) -> dict:
    return {"type": "widget", "title": spec["title"], "html": codelab.playground_html(spec), "plan": codelab.plan_text(codelab.playground_plan(spec))}


@needs_node
def test_a_playground_is_changed_from_its_spec_and_the_request(monkeypatch):
    sent = fake_haiku(monkeypatch, builds=[{**GINI, "title": "Gini, new data"}])
    fake_jev(monkeypatch)
    block = playground_block(GINI)
    out = outline([TEXT, block])
    r = ce.edit_widget(None, usage(), "k", out, 0, 1, "relabel it", effort="medium")
    assert r["changed"] is True and "Gini, new data" in r["block"]["html"] and r["block"]["title"] == GINI["title"]
    (call,) = sent
    assert call["step"] == "playground edit" and "change a finished playground" in call["system"]
    assert call["user"]["yourPreviousSpec"]["title"] == GINI["title"] and call["user"]["request"] == "relabel it"
    assert out["stages"][0]["blocks"][1] is block


@needs_node
def test_a_playground_that_fails_its_checks_keeps_the_old_one(monkeypatch):
    broken = {"title": "Broken", "state": {"a": 1},
              "components": [{"id": "s", "type": "slider", "label": "B", "key": "b", "min": 0, "max": 5},
                             {"id": "r", "type": "readout", "label": "A", "value": "return state.a;"}]}
    sent = fake_haiku(monkeypatch, builds=[broken] * 5)
    fake_jev(monkeypatch)
    block = playground_block(GINI)
    r = ce.edit_widget(None, usage(), "k", outline([TEXT, block]), 0, 1, "break it")
    assert r["changed"] is False and r["block"] is block and "failed its checks" in r["notes"][0]
    assert [s["step"] for s in sent][0] == "playground edit" and sent[-1]["step"] == "playground repair"


def test_a_playground_that_cannot_be_read_is_kept(monkeypatch):
    sent = fake_haiku(monkeypatch)
    block = {"type": "widget", "title": "P", "html": "<div></div><script>XW.playground({broken</script>", "plan": "{}"}
    r = ce.edit_widget(None, usage(), "k", outline([TEXT, block]), 0, 1, "change it")
    assert r["changed"] is False and r["block"] is block and sent == [] and "couldn't be read back" in r["notes"][0]


# ---------- adding a widget ----------


def test_a_widget_is_added_to_a_section_that_has_none(monkeypatch):
    sent = fake_haiku(monkeypatch, [plan()], [built(GOOD)])
    fake_checks(monkeypatch, lambda html: [])
    fake_jev(monkeypatch)
    out = page()
    r = ce.add_widget(None, usage(), "k", out, 0, "a slider for the amount", "code-lab")
    assert r["built"] is True and [b["type"] for b in r["outline"]["stages"][0]["blocks"]] == ["text", "widget", "quiz"]
    assert sent[0]["user"]["hint"] == "a slider for the amount"  # no gate: the request is the hint
    assert [b["type"] for b in out["stages"][0]["blocks"]] == ["text", "quiz"]


def test_a_full_page_gets_no_more_widgets(monkeypatch):
    sent = fake_haiku(monkeypatch)
    full = outline(*[[TEXT, {**OLD, "title": f"W{i}"}] for i in range(codelab.MAX_MARKED)], [TEXT])
    r = ce.add_widget(None, usage(), "k", full, codelab.MAX_MARKED, "add a chart", "code-lab")
    assert r["built"] is False and r["outline"] is full and sent == []
    assert r["notes"] == ['Couldn\'t add an interactive part to "S4": This page already has 4 interactive parts. Remove one first, or I can change an existing one.']


def test_a_page_with_no_kind_of_widget_cannot_build_one(monkeypatch):
    sent = fake_haiku(monkeypatch)
    r = ce.add_widget(None, usage(), "k", page(), 0, "add a chart", None)
    assert r["built"] is False and sent == [] and "this page type can't build one" in r["notes"][0]


def test_a_widget_that_cannot_be_built_leaves_the_section_text(monkeypatch):
    fake_haiku(monkeypatch, [plan(build=False)])
    fake_jev(monkeypatch)
    out = page()
    r = ce.add_widget(None, usage(), "k", out, 0, "add a chart", "code-lab")
    assert r["built"] is False and r["outline"] is out and r["notes"]


# ---------- text and widget agreement ----------


def fake_agree(monkeypatch, p: dict[str, float]) -> list:
    asked: list = []

    def jev_ask(key, state, questions):
        asked.append((state, sorted(questions)))
        return {"answers": {k: {"p": p.get(k, 0.9)} for k in questions}, "model": "jev-test", "usage": {"input_tokens": 50, "output_tokens": 0}}

    monkeypatch.setattr(ce, "jev_ask", jev_ask)
    return asked


def test_agreement_is_judged_from_the_text_the_plan_and_what_the_widget_does(monkeypatch):
    asked = fake_agree(monkeypatch, {})
    out = page(OLD)
    assert ce.agrees("k", out, 0, PLAN_V1, FACTS, usage()) is None
    state, questions = asked[0]
    assert questions == ["controls_in_text", "lesson_in_text", "text_not_contradicted"]
    assert state["section"]["text"].startswith("More samples") and state["plan"]["lesson"] == "more samples, steadier average"
    assert state["widget"]["controls"][0]["label"] == "Amount"


def test_only_a_clear_no_is_a_mismatch(monkeypatch):
    fake_agree(monkeypatch, {"text_not_contradicted": 0.5, "lesson_in_text": 0.25})  # unsure, not a no
    assert ce.agrees("k", page(OLD), 0, PLAN_V1, FACTS, usage()) is None
    fake_agree(monkeypatch, {"text_not_contradicted": 0.1})
    why = ce.agrees("k", page(OLD), 0, PLAN_V1, FACTS, usage())
    assert why and "contradicts" in why and "doesn't mention" not in why


def test_agreement_is_not_judged_without_a_key_a_plan_or_facts(monkeypatch):
    asked = fake_agree(monkeypatch, {"text_not_contradicted": 0.0})
    assert ce.agrees(None, page(OLD), 0, PLAN_V1, FACTS, usage()) is None
    assert ce.agrees("k", page(OLD), 0, None, FACTS, usage()) is None
    assert ce.agrees("k", page(OLD), 0, PLAN_V1, None, usage()) is None
    assert asked == []

    def down(key, state, questions):
        raise ce.JevError("Jev unreachable")

    monkeypatch.setattr(ce, "jev_ask", down)
    assert ce.agrees("k", page(OLD), 0, PLAN_V1, FACTS, usage()) is None


def test_the_digest_line_for_a_widget_says_what_it_teaches():
    assert ce.widget_summary(OLD) == 'widget "Average steadier": more samples, steadier average'
    assert ce.widget_summary({"type": "widget", "title": "T", "html": GOOD}) == 'widget "T"'
