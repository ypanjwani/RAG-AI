"""Feedback v2 through the pipeline: the confirmed parts replace the target finder, saved preferences stay out, an unmatched
change is an error (never a silent rewrite), and feedback_check.py keeps, retries or puts back each changed part.
Claude, the widget builder and Jev are faked (test_targets.py's fakes)."""

from __future__ import annotations

import json

import pytest

from explainer import feedback_check, pipeline
from explainer.core.validate import default_design
from explainer.jev import JevError
from explainer.llm import ReaderError
from test_targets import FOLLOW, IDS, OLD, W, Edits, Pipe, blocks_of, rewritten, target, widget_of

FID = "f" * 32


class Jev:
    """feedback_check's Jev: each call answers with the next {fixes, not_worse} pair (or raises)."""

    def __init__(self, monkeypatch, *answers):
        self.answers, self.states = list(answers), []

        def ask(key, state, questions):
            self.states.append(state)
            a = self.answers.pop(0) if self.answers else (0.9, 0.9)
            if a == "error":
                raise JevError("Jev unreachable")
            return {"answers": {"fixes": {"p": a[0]}, "not_worse": {"p": a[1]}}, "model": "jev", "usage": {"input_tokens": 50}}

        monkeypatch.setattr(feedback_check, "jev_ask", ask)


class FeedbackPipe(Pipe):
    def __init__(self, monkeypatch, *changes: dict):
        super().__init__(monkeypatch, {"pageWide": False, "targets": []}, *changes)
        self.finder = []
        monkeypatch.setattr(pipeline, "find_targets", lambda *a: self.finder.append(a))

    def run_fb(self, fb: dict, quality: str = "playground", jev_key: str | None = "jev", **req):
        follow = {**FOLLOW, "design": default_design(OLD), "feedback": {"id": FID, "keep": [], "pageWide": False, **fb}}
        events: list[dict] = []
        out = pipeline.generate(None, {"mode": "concept", "text": "The descent chart is confusing.", "quality": quality, "followUp": follow, **req},
                                events.append, jev_key=jev_key)
        return out["spec"], events


def test_the_confirmed_part_is_changed_without_the_finder(monkeypatch):
    edits, jev = Edits(monkeypatch), Jev(monkeypatch)
    pipe = FeedbackPipe(monkeypatch)
    spec, events = pipe.run_fb({"targets": [target("widget", "loop", "use a log scale")]})
    assert pipe.finder == [] and edits.edited == [("loop", "use a log scale")]
    assert widget_of(spec, "loop")["html"].endswith("<!-- edited -->") and widget_of(spec, "sampling") == W["sampling"]
    (state,) = jev.states
    assert state["issue"] == {"reader_said": "The descent chart is confusing.", "change": "use a log scale"}
    assert state["before"]["label"] == state["after"]["label"] == "Generate and repeat"
    assert any(e.get("detail") == "Checking the changes" for e in events)


def test_a_kept_section_is_never_changed_and_an_unmatched_change_is_an_error(monkeypatch):
    Edits(monkeypatch), Jev(monkeypatch)
    pipe = FeedbackPipe(monkeypatch)
    with pytest.raises(ReaderError, match="couldn't be matched"):
        pipe.run_fb({"targets": [target("widget", "loop", "x")], "keep": ["loop"]})
    with pytest.raises(ReaderError, match="couldn't be matched"):
        pipe.run_fb({"targets": [target("text", "ghost", "x")]})
    assert pipe.seen["planner"] == [] and pipe.finder == []  # no silent whole-page rewrite


def test_a_lab_page_can_only_change_as_a_whole(monkeypatch):
    Edits(monkeypatch), Jev(monkeypatch)
    with pytest.raises(ReaderError, match="whole"):
        FeedbackPipe(monkeypatch).run_fb({"targets": [target("text", "tokens", "x")]}, quality="scene-plus")


def test_a_whole_page_change_takes_the_old_path_without_saved_preferences(monkeypatch):
    Edits(monkeypatch), Jev(monkeypatch)
    pipe = FeedbackPipe(monkeypatch, {"replace": [rewritten("tokens", "Simpler.")]})
    spec, _ = pipe.run_fb({"targets": [], "pageWide": True}, quality="quality-plus", prefs=["Prefer fewer sliders."])
    (call,) = pipe.seen["planner"]
    assert "Prefer fewer sliders." not in call["request"] and "Change ONLY" not in call["request"]
    assert blocks_of(spec, "tokens")[0]["md"] == "Simpler." and pipe.finder == []


def test_preferences_still_reach_an_ordinary_revision(monkeypatch):
    Edits(monkeypatch)
    pipe = Pipe(monkeypatch, {"pageWide": True, "targets": []}, {"replace": [rewritten("tokens", "Simpler.")]})
    pipe.run(quality="quality-plus", prefs=["Prefer fewer sliders."])
    assert "Prefer fewer sliders." in pipe.seen["planner"][0]["request"]


def test_a_change_that_fails_the_check_is_tried_once_more(monkeypatch):
    edits, jev = Edits(monkeypatch), Jev(monkeypatch, (0.1, 0.9), (0.9, 0.9))
    spec, _ = FeedbackPipe(monkeypatch).run_fb({"targets": [target("widget", "loop", "use a log scale")]})
    assert [w for _, w in edits.edited] == ["use a log scale", "use a log scale (A first attempt didn't work: the change didn't deal with what you asked for.)"]
    assert widget_of(spec, "loop")["html"].endswith("<!-- edited -->") and len(jev.states) == 2
    assert not any("Kept the previous" in w for w in spec.get("warnings") or [])


def test_a_change_that_fails_twice_is_put_back_and_no_empty_version_is_made(monkeypatch):
    Edits(monkeypatch), Jev(monkeypatch, (0.9, 0.1), (0.1, 0.1))
    with pytest.raises(ReaderError, match='Nothing on the page was changed: Kept the previous "Generate and repeat"'):
        FeedbackPipe(monkeypatch).run_fb({"targets": [target("widget", "loop", "use a log scale")]})


def test_a_change_that_fails_to_apply_makes_no_new_version(monkeypatch):
    Edits(monkeypatch, fail=True), Jev(monkeypatch)
    with pytest.raises(ReaderError, match="Nothing on the page was changed: Couldn't change the interactive part"):
        FeedbackPipe(monkeypatch).run_fb({"targets": [target("widget", "loop", "use a log scale")]})


def test_only_the_part_that_got_worse_is_put_back(monkeypatch):
    Edits(monkeypatch), Jev(monkeypatch, (0.9, 0.9), (0.9, 0.1), (0.9, 0.1))
    spec, _ = FeedbackPipe(monkeypatch).run_fb({"targets": [target("widget", "sampling", "fewer lines"), target("widget", "loop", "log scale")]})
    assert widget_of(spec, "sampling")["html"].endswith("<!-- edited -->") and widget_of(spec, "loop") == W["loop"]


def test_a_widget_change_jev_only_doubts_is_kept_with_a_note(monkeypatch):
    Edits(monkeypatch), Jev(monkeypatch, (0.1, 0.9), (0.1, 0.9))
    spec, _ = FeedbackPipe(monkeypatch).run_fb({"targets": [target("widget", "loop", "show the values in the cells")]})
    assert widget_of(spec, "loop")["html"].endswith("<!-- edited -->")  # the change the reader asked for stays
    assert any('Changed "Generate and repeat" as you asked, but it may not fully fix it' in w for w in spec["warnings"])
    assert spec["revision"]["kept"] == len(IDS) - 1


def test_a_doubtful_first_try_is_kept_when_the_second_try_got_worse(monkeypatch):
    edits, _ = Edits(monkeypatch), Jev(monkeypatch, (0.1, 0.9), (0.9, 0.1))
    spec, _ = FeedbackPipe(monkeypatch).run_fb({"targets": [target("widget", "loop", "bigger matrix")]})
    assert len(edits.edited) == 2 and widget_of(spec, "loop")["html"].endswith("<!-- edited -->")
    assert any("may not fully fix it (the change didn't deal with what you asked for)" in w for w in spec["warnings"])


def test_a_text_change_that_fails_twice_is_still_put_back(monkeypatch):
    Edits(monkeypatch), Jev(monkeypatch, (0.1, 0.9), (0.1, 0.9))
    pipe = FeedbackPipe(monkeypatch, {"replace": [rewritten("tokens", "Changed.")]}, {"replace": [rewritten("tokens", "Changed again.")]})
    with pytest.raises(ReaderError, match='Kept the previous "Text becomes tokens"'):
        pipe.run_fb({"targets": [target("text", "tokens", "simpler words")]})


def test_jev_sees_each_widgets_real_settings(monkeypatch):
    _, jev = Edits(monkeypatch), Jev(monkeypatch)
    FeedbackPipe(monkeypatch).run_fb({"targets": [target("widget", "loop", "use a log scale")]})
    (state,) = jev.states
    before, after = state["before"]["interactive"][0], state["after"]["interactive"][0]
    assert before["settings"] == W["loop"]["html"] and after["settings"].endswith("<!-- edited -->")
    spec = {"state": {"n": 8}, "components": [{"id": "c", "type": "chart", "draw": "return [heatmap(v, {showValues: true})];"}]}
    pg = {"type": "widget", "title": "M", "plan": "{}", "html": f'<div id="pg"></div><script>XW.playground({json.dumps(spec)});</script>'}
    assert json.loads(feedback_check.widget_settings(pg)) == spec  # a playground: its spec, where colours and values live


def test_without_jev_the_change_is_kept_with_a_note(monkeypatch):
    Edits(monkeypatch), Jev(monkeypatch, "error")
    spec, _ = FeedbackPipe(monkeypatch).run_fb({"targets": [target("widget", "loop", "log scale")]})
    assert widget_of(spec, "loop")["html"].endswith("<!-- edited -->")
    assert any("couldn't be checked" in w for w in spec["warnings"])
    spec, _ = FeedbackPipe(monkeypatch).run_fb({"targets": [target("widget", "loop", "log scale")]}, jev_key=None)
    assert any("couldn't be checked" in w for w in spec["warnings"])
