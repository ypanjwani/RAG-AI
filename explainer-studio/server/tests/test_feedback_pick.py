"""Feedback: section summaries built by code from the page's JSON, Haiku picking the sections, the scope the plan step gets,
and the widget limit. Haiku is faked."""

from __future__ import annotations

import copy
import json

from conftest import signup
from explainer import feedback, feedback_pick as fp
from test_feedback import change, fake_haiku, reply, send
from test_targets import FULL, IDS, widget

PLAY = {"type": "widget", "title": "Heads and the n² Cost", "html": "<div></div><script>1</script>",
        "plan": json.dumps({"lesson": "the score matrix grows with n squared",
                            "controls": [{"id": "s1", "kind": "slider", "label": "Sequence length n"}, {"id": "p1", "kind": "play", "label": "Play"}],
                            "visuals": [{"what": "n² curve"}, {"what": "head bars"}], "review": {"x": 0.9}})}


def playground(title: str, components: list[dict]) -> dict:
    spec = {"title": title, "plan": {}, "state": {}, "components": components}
    return {"type": "widget", "title": title, "html": f'<div id="pg"></div><script>XW.playground({json.dumps(spec)});</script>',
            "plan": json.dumps({"lesson": "the idea", "visuals": [{"what": "weights"}]})}


DRAG_ONLY = playground("Weighted mixing", [{"id": "c1", "type": "chart", "onDrag": "return null;"}, {"id": "r1", "type": "readout", "label": "alpha"}])
WITH_SLIDER = playground("Scores", [{"id": "s1", "type": "slider", "label": "d_k"}, {"id": "b1", "type": "button", "label": "New vectors"},
                                    {"id": "x", "type": "button", "action": "reset", "label": "Reset"}, {"id": "c1", "type": "chart"}])
CHART_STAGE = {"id": "scale", "label": "How the scale sets sharpness", "blurb": "Dividing by the root keeps weights soft.", "blocks": [
    {"type": "text", "md": "Drag the key dimension and watch two things."},
    {"type": "chart", "title": "Spread against key dimension", "series": [{"label": "Unscaled", "expr": "sqrt(dk)"}]},
    {"type": "slider", "var": "dk", "label": "Key dimension d_k", "min": 1, "max": 512},
    {"type": "bars", "title": "Attention weights", "labels": ["The", "cat", "sat"], "values": ["x"]}]}
PAGE2 = {"title": "Attention", "level": "beginner", "stages": [CHART_STAGE, {"id": "cost", "label": "Why it works", "blurb": "", "blocks": [PLAY]},
                                                               {"id": "mix", "label": "Mixing", "blurb": "", "blocks": [DRAG_ONLY]},
                                                               {"id": "scores", "label": "Scores", "blurb": "", "blocks": [WITH_SLIDER]}]}


def pick_reply(sections=(), unsure=(), kind="parts"):
    return {"sections": list(sections), "unsure": list(unsure), "kind": kind, "why": "matched"}


def req(**kw):
    return {"text": "The play button should move the bars.", "answer": "", "round": 1, "wholeOnly": False, "warnings": [], **kw}


# ---------- the summaries ----------


def test_cards_show_each_widgets_controls_and_visuals_and_each_ordinary_block():
    cards = fp.section_cards(PAGE2, {"stages": ["cost"], "what": "Added a Play button that grows n."})
    scale, cost = cards[0], cards[1]
    assert scale["number"] == 1 and scale["label"] == "How the scale sets sharpness"
    assert scale["parts"] == ["text: Drag the key dimension and watch two things.", 'line chart "Spread against key dimension" (lines: Unscaled)',
                              'slider "Key dimension d_k"', 'bar chart "Attention weights" (bars: The, cat, sat)']
    assert cost["parts"] == ['interactive widget "Heads and the n² Cost"; controls: Sequence length n, Play (play); shows: n² curve, head bars; teaches: the score matrix grows with n squared']
    assert "lastChange" not in scale and "Added a Play button" in cost["lastChange"]
    text = fp.cards_text(cards)
    assert 'Section 2, id "cost": "Why it works"' in text and "* The reader's previous feedback changed this section" in text


def test_a_playgrounds_card_shows_the_automatic_reset_and_flags_reset_only_widgets():
    mix, scores = fp.section_cards(PAGE2)[2:]
    assert mix["parts"][0].startswith('interactive widget "Weighted mixing"; controls: dragging on a chart, Reset (added automatically); '
                                      "its only button is Reset: the reader can only drag on its chart")
    assert "controls: d_k, New vectors (button), Reset (added automatically); shows" in scores["parts"][0]
    assert "only button" not in scores["parts"][0]


def test_a_playgrounds_charts_say_what_they_draw():
    assert fp.chart_kind("return [bars(range(1, 9).map(i => [i, 2]))];") == "bar"
    assert fp.chart_kind("return [line(pts), point(n, n * n)];") == "line and point"
    assert fp.chart_kind("return [];") == ""
    w = playground("Heads", [{"id": "c", "type": "chart", "title": "Per-head d_k", "draw": "return [bars(x)];"}, {"id": "r", "type": "readout", "label": "d_k"}])
    assert "shows: Per-head d_k (bar chart), d_k (readout)" in fp.widget_line(w)


# ---------- the pick ----------


def test_haiku_picks_from_the_cards_and_its_reply_is_checked():
    sent = []

    def create(**kw):
        sent.append(kw)
        return type("M", (), {"model": "claude-haiku-5-5", "usage": {"input_tokens": 900, "output_tokens": 40},
                              "content": [type("C", (), {"type": "text", "text": json.dumps(pick_reply(["mix", "ghost", "scores"], ["scale", "mix"]))})()]})()

    client = type("Cl", (), {"messages": type("Ms", (), {"create": staticmethod(create)})()})()
    pick, usd = fp.pick_sections(client, fp.section_cards(PAGE2), "The widgets with only a reset button need more.", "")
    assert pick == {"chosen": ["mix", "scores"], "maybe": ["scale"], "kind": "parts", "why": "matched"} and usd > 0
    user = sent[0]["messages"][0]["content"]
    assert "its only button is Reset" in user and "<feedback>" in user and sent[0]["output_config"]["effort"] == "low"
    assert fp.decide({"sections": ["mix"], "unsure": [], "kind": "maybe"}, fp.section_cards(PAGE2)) is None  # unknown kind


# ---------- the scope ----------


def pick(chosen=(), maybe=(), kind="parts"):
    return {"chosen": list(chosen), "maybe": list(maybe), "kind": kind, "why": ""}


def test_the_scope_follows_the_pick():
    s = feedback.decide_scope(pick(["loop"]), FULL, req())
    assert s["allowed"] == {"loop"} and s["work"] == ["loop"] and not s["allowNew"] and not s["pageWide"]
    assert feedback.decide_scope(None, FULL, req())["allowed"] is None  # unusable pick: any section
    assert feedback.decide_scope(pick(kind="page"), FULL, req())["pageWide"]
    many = feedback.decide_scope(pick(IDS[:7]), FULL, req())  # more than a few sections: ask about them
    assert not many["pageWide"] and many["ask"] == IDS[:7] and many["work"] == IDS[:6]
    none = feedback.decide_scope(pick(kind="none"), FULL, req())
    assert none["allowed"] == set() and not none["needsChange"]
    new = feedback.decide_scope(pick(kind="new"), FULL, req())
    assert new["allowNew"] and new["allowed"] == set()
    ask = feedback.decide_scope(pick(maybe=["sampling", "loop"]), FULL, req())
    assert ask["ask"] == ["sampling", "loop"] and ask["work"] == ["sampling", "loop"] and ask["allowed"] == set() and ask["needsChange"]
    two = feedback.decide_scope(pick(maybe=["sampling", "loop"]), FULL, req(round=2, answer="the second one"))
    assert two["allowed"] == {"sampling", "loop"} and two["ask"] is None


def test_changes_outside_the_scope_are_dropped():
    s = feedback.decide_scope(pick(["loop"]), FULL, req())
    p = feedback.clean_plan(reply([change(stage="sampling", quote="the bars"), change(stage="loop", quote="The play button")]), FULL, req(), s)
    assert [c["stage"] for c in p["changes"]] == ["loop"]


def test_unsure_sections_become_the_writers_concrete_choices():
    s = feedback.decide_scope(pick(maybe=["sampling", "loop"]), FULL, req())
    q = {"text": "Which interactive part?", "options": [
        {"label": "Temperature explorer", "part": "widget", "stage": "sampling", "after": "", "what": "Add a slider for temperature."},
        {"label": "Tokens", "part": "widget", "stage": "tokens", "after": "", "what": "Not unsure, so dropped."},
        {"label": "Descent path", "part": "widget", "stage": "loop", "after": "", "what": "Add a step-size slider."}]}
    p = feedback.clean_plan(reply([change(stage="loop", quote="The play button")], question=q), FULL, req(), s)
    assert p["changes"] == []  # nothing is changed before the reader picks
    assert [(o["stage"], o["what"]) for o in p["question"]["options"]] == [("sampling", "Add a slider for temperature."), ("loop", "Add a step-size slider."), ("", "")]
    assert p["question"]["options"][-1]["part"] == "page"  # the whole page stays a choice
    m = feedback.writer_message(FULL, fp.section_cards(FULL), s, req(), [])
    assert 'Section "sampling" (full JSON)' in m and 'Section "loop" (full JSON)' in m and "one option per section" in m
    # the writer wrote no usable options: the code asks about the unsure sections itself
    p = feedback.clean_plan(reply(), FULL, req(), s)
    assert [o["label"] for o in p["question"]["options"]] == ["Picking the next token", "Picking the next token · interactive part",
                                                              "Generate and repeat", "Generate and repeat · interactive part", "The whole page"]


def test_praise_only_asks_nothing():
    s = feedback.decide_scope(pick(kind="none"), FULL, req())
    p = feedback.clean_plan(reply(pref="Prefer short pages."), FULL, req(), s)
    assert p["changes"] == [] and p["question"] is None and not p["unmatched"]


def test_a_widget_past_the_limit_becomes_a_change_to_the_sections_own_blocks():
    page = copy.deepcopy(FULL)
    page["stages"][IDS.index("summary")]["blocks"].append(widget("Fourth"))  # 4 widgets: the limit
    p = feedback.clean_plan(reply([change(stage="attention", quote="The play button")]), page, req())
    assert [(c["part"], c["stage"]) for c in p["changes"]] == [("text", "attention")]
    p = feedback.clean_plan(reply([change(stage="attention", quote="The play button")]), FULL, req())  # 3 widgets: adding is fine
    assert [(c["part"], c["stage"]) for c in p["changes"]] == [("widget", "attention")]


def test_the_writer_gets_every_summary_but_only_the_chosen_sections_json():
    s = feedback.decide_scope(pick(["loop"]), FULL, req())
    m = feedback.writer_message(FULL, fp.section_cards(FULL), s, req(), [])
    assert m.count('Section "loop" (full JSON)') == 1 and 'Section "sampling" (full JSON)' not in m
    assert 'Section 4, id "sampling"' in m and 'What you may change: Only these sections: "loop"' in m
    assert "3 interactive widgets, at most 4" in m and '"review"' not in m


# ---------- the route ----------


def test_the_route_uses_the_pick(client, app):
    a = signup(client)
    sent = fake_haiku(app, reply([change(stage="sampling", quote="The temperature chart is too busy"),
                                  change(stage="loop", quote="The temperature chart is too busy")]), picks=[pick_reply(["loop"])])
    body = send(client, a).get_json()
    assert [c["stage"] for c in body["plan"]["changes"]] == ["loop"]  # only what was picked
    assert 'Section "loop" (full JSON)' in sent[0]["messages"][0]["content"] and body["usd"] > 0
    assert len(sent.picks) == 1


def test_the_route_sends_the_previous_change_to_the_pick(client, app):
    a = signup(client)
    sent = fake_haiku(app, reply(), picks=[pick_reply(kind="none")])
    send(client, a, lastChange={"stages": ["loop"], "what": "Added a Play button."})
    assert "* The reader's previous feedback changed this section: Added a Play button." in sent.picks[0]["messages"][0]["content"]
