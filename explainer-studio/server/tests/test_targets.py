"""Follow-ups that change only what was asked about (targets.py): the finder says which parts of the page a request is about,
apply_targets changes just those (one scoped planner call for the text, codelab_edit for each widget), checks that text and
widget still agree, and puts back everything else. Claude and Jev are faked."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from explainer import codelab, codelab_edit as ce, pipeline, targets as T
from explainer.core.validate import default_design
from explainer.pricing import UsageLog
from explainer.revise import parse_revision

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample.json").read_text(encoding="utf-8"))
OLD = SAMPLE["outline"]
IDS = [s["id"] for s in OLD["stages"]]
HTML = '<div id="a"></div><script>1</script>'


def widget(title: str, lesson: str = "the idea", html: str = HTML) -> dict:
    return {"type": "widget", "title": title, "html": html, "plan": json.dumps({"lesson": lesson, "controls": [{"id": "a"}]})}


W = {"tokens": widget("Token counter", "how text splits into tokens"), "sampling": widget("Temperature explorer", "temperature reshapes the odds"),
     "loop": widget("Descent path", "steps follow the slope")}


def placeholder(w: dict) -> dict:
    return {"type": "text", "md": f"(interactive widget: {w['title']})"}


def with_blocks(outline: dict, at: dict[str, dict], real: bool) -> dict:
    return {**outline, "stages": [{**s, "blocks": [*s["blocks"], at[s["id"]] if real else placeholder(at[s["id"]])] if s["id"] in at else s["blocks"]} for s in outline["stages"]]}


COMPACT = with_blocks(OLD, W, real=False)  # what the follow-up's outline holds
FULL = with_blocks(OLD, W, real=True)  # the page with its real widgets
FOLLOW = {"kind": "revise", "outline": json.dumps(COMPACT), "widgets": {k: [v] for k, v in W.items()}}


def stage(outline: dict, sid: str) -> dict:
    return next(s for s in outline["stages"] if s["id"] == sid)


class Planner:
    """The planner call: each call answers with the next changes dict, parsed like write_outline does. Records the requests."""

    def __init__(self, *changes: dict):
        self.changes, self.requests = list(changes), []

    def __call__(self, request: str, parse):
        self.requests.append(request)
        p = parse({"changes": self.changes.pop(0)})
        if "error" in p:
            raise ValueError(p["error"])
        return {"outline": p["outline"], "warnings": p["warnings"], **({"revision": p["revision"]} if "revision" in p else {})}


def rewritten(sid: str, md: str) -> dict:
    """The compact stage with its first text block replaced (the widget placeholder is kept)."""
    st = copy.deepcopy(stage(COMPACT, sid))
    st["blocks"][0] = {"type": "text", "md": md}
    return st


class Edits:
    """Stands in for codelab_edit: edit_widget changes a widget's html, agrees answers from a queue."""

    def __init__(self, monkeypatch, agree=(), fail: bool = False):
        self.edited, self.agree_asked, self.added = [], [], []
        self.agree = list(agree)

        def edit_widget(client, usage, jev_key, outline, si, bi, what, effort="medium", think=True):
            old = outline["stages"][si]["blocks"][bi]
            self.edited.append((outline["stages"][si]["id"], what))
            if fail:
                return {"block": old, "changed": False, "facts": None, "plan": None, "notes": [f'Couldn\'t change the interactive part in "{outline["stages"][si]["label"]}": nope.']}
            new = {**old, "html": old["html"] + "<!-- edited -->"}
            return {"block": new, "changed": True, "facts": {"controls": []}, "plan": ce.read_plan(old), "notes": []}

        def agrees(jev_key, outline, si, plan, facts, usage):
            self.agree_asked.append(outline["stages"][si]["id"])
            return self.agree.pop(0) if self.agree else None

        def add_widget(client, usage, jev_key, outline, si, what, kind, effort="medium", think=True):
            self.added.append((outline["stages"][si]["id"], what, kind))
            block = widget("New chart", "something new")
            stages = list(outline["stages"])
            stages[si] = {**stages[si], "blocks": [*stages[si]["blocks"], block]}
            return {"outline": {**outline, "stages": stages}, "built": True, "facts": {"controls": []}, "plan": ce.read_plan(block), "notes": []}

        monkeypatch.setattr(ce, "edit_widget", edit_widget)
        monkeypatch.setattr(ce, "agrees", agrees)
        monkeypatch.setattr(ce, "add_widget", add_widget)
        monkeypatch.setattr(ce, "facts_of", lambda block: {"controls": []})


def run(targets: list[dict], ask, request: str = "do it"):
    return T.apply_targets(None, UsageLog("quality-plus", "medium"), "k", FOLLOW, COMPACT, targets, request, ask)


def target(part: str, stage: str = "", what: str = "change it", after: str = "") -> dict:
    return {"part": part, "stage": stage, "after": after, "what": what}


# ---------- the finder ----------


def test_the_digest_names_every_section_block_and_widget():
    d = T.outline_digest(FULL)
    assert 'Section id "tokens"' in d and 'Section id "summary"' in d
    assert '[7] widget "Token counter": how text splits into tokens' in d
    assert "[0] text: " in d and len(d) < 8000


def test_targets_are_cleaned():
    raw = {"pageWide": False, "targets": [
        {"part": "widget", "stage": "sampling", "after": "", "what": "log scale"},
        {"part": "widget", "stage": "sampling", "after": "", "what": "bigger dots"},  # the same part: joined
        {"part": "text", "stage": "ghost", "after": "", "what": "x"},  # no such section
        {"part": "text", "stage": "tokens", "after": "", "what": "  "},  # nothing described
        {"part": "bogus", "stage": "tokens", "after": "", "what": "x"},
        {"part": "new-section", "stage": "", "after": "nowhere", "what": "add metrics"},  # unknown anchor: the end
    ]}
    assert T.clean_targets(raw, OLD) == {"pageWide": False, "targets": [
        target("widget", "sampling", "log scale; bigger dots"), target("new-section", "", "add metrics", after=IDS[-1])]}


def test_a_page_wide_request_or_too_many_targets_take_the_old_path():
    assert T.clean_targets({"pageWide": True, "targets": [target("text", "tokens")]}, OLD) == {"targets": [], "pageWide": True}
    many = {"pageWide": False, "targets": [target("text", sid, f"change {sid}") for sid in IDS[:7]]}
    assert T.clean_targets(many, OLD) == {"targets": [], "pageWide": True}
    assert T.clean_targets({"pageWide": False, "targets": []}, OLD) is None
    assert T.clean_targets("nonsense", OLD) is None and T.clean_targets({"targets": [target("text", "ghost")]}, OLD) is None


def fake_client(reply):
    sent = []

    def create(**kw):
        sent.append(kw)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(model="claude-haiku-5-5", usage=SimpleNamespace(input_tokens=900, output_tokens=60),
                               content=[SimpleNamespace(type="text", text=json.dumps(reply))])

    return SimpleNamespace(messages=SimpleNamespace(create=create)), sent


def test_the_finder_asks_once_at_low_effort_with_the_digest():
    client, sent = fake_client({"pageWide": False, "targets": [target("widget", "sampling", "log scale")]})
    usage = UsageLog("quality-plus", "medium")
    r = T.find_targets(client, usage, FULL, "make the temperature chart log scale")
    assert r == {"pageWide": False, "targets": [target("widget", "sampling", "log scale")]}
    (call,) = sent
    assert call["output_config"]["effort"] == "low" and call["output_config"]["format"]["schema"]["required"] == ["targets", "pageWide"]
    assert "make the temperature chart log scale" in call["messages"][0]["content"] and 'Section id "sampling"' in call["messages"][0]["content"]
    assert [s["step"] for s in usage.summary()["steps"]] == ["follow-up targets"]


def test_the_finder_never_breaks_a_follow_up(monkeypatch):
    client, _ = fake_client(RuntimeError("down"))
    assert T.find_targets(client, UsageLog("q", "low"), FULL, "x") is None
    assert T.find_targets(None, UsageLog("q", "low"), FULL, "x") is None
    client, sent = fake_client({"pageWide": False, "targets": [target("text", "tokens")]})
    monkeypatch.setenv("FOLLOWUP_TARGETS", "0")
    assert T.find_targets(client, UsageLog("q", "low"), FULL, "x") is None and sent == []


# ---------- the guard ----------


def test_the_guard_puts_back_what_was_not_asked_for():
    new = copy.deepcopy(COMPACT)
    new["title"], new["level"] = "A different title", "expert"
    new["stages"] = [s for s in new["stages"] if s["id"] != "summary"]  # removed
    stage(new, "loop")["blocks"][0] = {"type": "text", "md": "tampered"}  # changed
    stage(new, "tokens")["blocks"][0] = {"type": "text", "md": "asked for"}  # fine
    new["stages"].insert(2, {"id": "extra", "label": "Extra", "phase": new["stages"][0]["phase"], "blurb": "", "blocks": []})  # not asked for
    out, warnings = T.guard_untouched(COMPACT, new, {"tokens"}, 0)
    assert out["title"] == COMPACT["title"] and out["level"] == COMPACT["level"]
    assert [s["id"] for s in out["stages"]] == IDS
    assert stage(out, "loop") == stage(COMPACT, "loop") and stage(out, "tokens")["blocks"][0]["md"] == "asked for"
    assert len(warnings) == 3 and any("removed without being asked" in w for w in warnings)


# ---------- changing the page ----------


def test_a_widget_request_changes_only_that_widget_and_no_text_is_written(monkeypatch):
    edits = Edits(monkeypatch)
    ask = Planner()
    r = run([target("widget", "sampling", "use a log scale")], ask)
    assert ask.requests == [] and edits.edited == [("sampling", "use a log scale")]
    out = r["outline"]
    assert stage(out, "sampling")["blocks"][-1]["html"].endswith("<!-- edited -->")
    for sid in IDS:  # every other stage and widget is the previous one, byte for byte
        if sid != "sampling":
            assert stage(out, sid) == stage(FULL, sid)
    assert r["revision"] == {"changed": [], "removed": 0, "widgets": ["sampling"]} and r["warnings"] == []
    assert edits.agree_asked == ["sampling"]  # the edited widget was compared with its text


def test_a_text_request_changes_only_that_stage_and_keeps_every_widget(monkeypatch):
    edits = Edits(monkeypatch)
    ask = Planner({"replace": [rewritten("tokens", "A rewritten explanation of tokens.")]})
    r = run([target("text", "tokens", "explain it with a number")], ask)
    out = r["outline"]
    assert stage(out, "tokens")["blocks"][0]["md"] == "A rewritten explanation of tokens."
    assert stage(out, "tokens")["blocks"][-1] == W["tokens"]  # the placeholder became the real widget again
    for sid in IDS:
        if sid != "tokens":
            assert stage(out, sid) == stage(FULL, sid)
    assert r["revision"] == {"changed": ["tokens"], "removed": 0, "widgets": []}
    # The planner was told to change only this stage, was shown what the widget teaches, and never saw widget code
    req = ask.requests[0]
    assert 'Change ONLY these parts' in req and '"tokens"' in req and "explain it with a number" in req
    assert "how text splits into tokens" in req and "<script>" not in req and "(interactive widget: Token counter)" in req
    assert edits.edited == [] and edits.agree_asked == ["tokens"]  # the widget was checked against the new text, and agreed


def test_a_text_change_the_widget_no_longer_matches_changes_the_widget_too(monkeypatch):
    edits = Edits(monkeypatch, agree=["the text now calls the count fixed"])
    r = run([target("text", "tokens", "say the token count is fixed")], Planner({"replace": [rewritten("tokens", "The count is fixed.")]}))
    assert edits.edited == [("tokens", "Make the interactive part match the revised text: the text now calls the count fixed.")]
    assert r["revision"] == {"changed": ["tokens"], "removed": 0, "widgets": ["tokens"]}
    assert stage(r["outline"], "tokens")["blocks"][-1]["html"].endswith("<!-- edited -->")


def test_a_widget_change_the_text_no_longer_matches_changes_the_text_too(monkeypatch):
    edits = Edits(monkeypatch, agree=["the text calls temperature fixed"])
    ask = Planner({"replace": [rewritten("sampling", "Temperature is now adjustable.")]})
    r = run([target("widget", "sampling", "add a temperature slider")], ask)
    assert edits.edited == [("sampling", "add a temperature slider")]
    assert len(ask.requests) == 1 and "Make the text match the interactive part: the text calls temperature fixed." in ask.requests[0]
    assert "temperature reshapes the odds" in ask.requests[0]
    assert r["revision"] == {"changed": ["sampling"], "removed": 0, "widgets": ["sampling"]}
    assert stage(r["outline"], "sampling")["blocks"][0]["md"] == "Temperature is now adjustable."
    assert r["outline"]["stages"][IDS.index("sampling")]["blocks"][-1]["html"].endswith("<!-- edited -->")


def test_a_failed_widget_change_keeps_the_old_widget_and_says_so(monkeypatch):
    Edits(monkeypatch, fail=True)
    r = run([target("widget", "sampling", "do the impossible")], Planner())
    assert r["outline"] == FULL and r["revision"]["widgets"] == []
    assert r["warnings"] == ['Couldn\'t change the interactive part in "' + stage(OLD, "sampling")["label"] + '": nope.']


def test_a_planner_that_strays_is_put_back_in_line(monkeypatch):
    Edits(monkeypatch)
    ask = Planner({"replace": [rewritten("tokens", "asked for"), rewritten("loop", "not asked for")], "remove": ["summary"]})
    r = run([target("text", "tokens", "x")], ask)
    assert [s["id"] for s in r["outline"]["stages"]] == IDS and stage(r["outline"], "loop") == stage(FULL, "loop")
    assert r["revision"]["changed"] == ["tokens"] and any("weren't asked about" in w for w in r["warnings"]) and any("none were removed" in w for w in r["warnings"])


def test_a_full_outline_reply_is_cut_back_by_the_guard(monkeypatch):
    Edits(monkeypatch)
    whole = copy.deepcopy(COMPACT)
    stage(whole, "tokens")["blocks"][0] = {"type": "text", "md": "asked for"}
    stage(whole, "loop")["blocks"][0] = {"type": "text", "md": "not asked for"}
    whole["stages"] = [s for s in whole["stages"] if s["id"] != "summary"]

    def ask(request, parse):
        p = parse(whole)  # the planner replied with a complete outline instead of changes
        return {"outline": p["outline"], "warnings": p["warnings"]}

    r = run([target("text", "tokens", "x")], ask)
    assert [s["id"] for s in r["outline"]["stages"]] == IDS and stage(r["outline"], "loop") == stage(FULL, "loop")
    assert stage(r["outline"], "tokens")["blocks"][0]["md"] == "asked for" and r["revision"]["changed"] == ["tokens"]
    assert any("put back" in w for w in r["warnings"])


def test_a_new_section_is_added_where_asked_and_only_one(monkeypatch):
    Edits(monkeypatch)
    one = {"id": "metrics", "label": "Metrics", "phase": OLD["stages"][0]["phase"], "blurb": "How to score it.", "blocks": [{"type": "text", "md": "Scores."}]}
    two = {**one, "id": "extra", "label": "Extra"}
    r = run([target("new-section", "", "add a metrics section", after="sampling")], Planner({"add": [{"after": "sampling", "stage": one}, {"after": "sampling", "stage": two}]}))
    assert [s["id"] for s in r["outline"]["stages"]] == [*IDS[:4], "metrics", *IDS[4:]]  # the first one written
    assert r["revision"] == {"changed": ["metrics"], "removed": 0, "widgets": []} and any("more stages than were asked" in w for w in r["warnings"])


def test_a_widget_is_added_to_a_section_that_has_none(monkeypatch):
    edits = Edits(monkeypatch)
    ask = Planner()
    r = T.apply_targets(None, UsageLog("q", "medium"), "k", FOLLOW, COMPACT, [target("widget", "attention", "a chart of the weights")], "x", ask, quality="playground")
    assert edits.added == [("attention", "a chart of the weights", "playground")] or edits.added == [("attention", "a chart of the weights", "code-lab")]
    assert stage(r["outline"], "attention")["blocks"][-1]["title"] == "New chart" and r["revision"]["widgets"] == ["attention"] and ask.requests == []


def test_a_request_with_no_widgets_on_the_page_still_works(monkeypatch):
    Edits(monkeypatch)
    follow = {"kind": "revise", "outline": json.dumps(OLD)}
    r = T.apply_targets(None, UsageLog("q", "low"), None, follow, OLD, [target("text", "tokens", "shorter")], "x",
                        Planner({"replace": [{**copy.deepcopy(stage(OLD, "tokens")), "blurb": "Shorter."}]}))
    assert stage(r["outline"], "tokens")["blurb"] == "Shorter." and r["revision"]["changed"] == ["tokens"]
    assert [s for s in r["outline"]["stages"] if s["id"] != "tokens"] == [s for s in OLD["stages"] if s["id"] != "tokens"]


def test_a_planner_that_fails_changes_nothing(monkeypatch):
    Edits(monkeypatch)

    def broken(request, parse):
        raise RuntimeError("the reply was not usable")

    r = run([target("text", "tokens", "x")], broken)
    assert r["outline"] == FULL and r["revision"] == {"changed": [], "removed": 0, "widgets": []}
    assert r["warnings"] == ["Couldn't change the text as asked; it was kept as it was."]


# ---------- through the pipeline ----------


class Pipe:
    """pipeline.generate with the finder, the planner, the later checks and the section extras faked; records what each saw."""

    def __init__(self, monkeypatch, found, *changes: dict):
        self.seen: dict = {"planner": [], "clarity": "unset", "fact": "unset", "examples": None, "notes": "unset"}
        self.planner = Planner(*changes)

        def write_outline(client, request, emit, usage, system, parse, effort, *a, **k):
            self.seen["planner"].append({"request": request, "system": system, "effort": effort})
            p = parse({"changes": self.planner.changes.pop(0)}) if self.planner.changes else parse(copy.deepcopy(OLD))
            return {"outline": p["outline"], "warnings": p["warnings"], **({"revision": p["revision"]} if "revision" in p else {})}

        def check(name):
            def run_check(client, jev_key, spec, usage, *a, only=None, **k):
                self.seen[name] = only
                return spec, "checked"
            return run_check

        monkeypatch.setattr(pipeline, "find_targets", lambda client, usage, outline, text: found)
        monkeypatch.setattr(pipeline, "write_outline", write_outline)
        monkeypatch.setattr(pipeline, "clarity_check", check("clarity"))
        monkeypatch.setattr(pipeline, "fact_check", check("fact"))
        monkeypatch.setattr(pipeline, "write_notes", lambda client, spec, extras, usage, kept=None: (self.seen.update(notes=kept), (spec, ""))[1])
        monkeypatch.setattr(pipeline, "write_examples", lambda client, spec, usage: self.seen.update(examples=pipeline.example_briefs(spec)) or spec)

    def run(self, quality: str = "playground", **req):
        bi, block = next((bi, b) for bi, b in enumerate(stage(OLD, "optimization")["blocks"]) if b["type"] == "examples")
        steps = {f"optimization:{bi}:{ii}": ["Step one.", "Step two."] for ii in range(len(block["items"]))}
        follow = {**FOLLOW, "design": default_design(OLD),
                  "kept": {"steps": steps, "notes": {"sampling": {"deeper": "Deeper on sampling."}, "tokens": {"deeper": "stale"}}}}
        events: list[dict] = []
        out = pipeline.generate(None, {"mode": "concept", "text": "change it", "quality": quality, "extras": ["examples", "depth"], "followUp": follow, **req},
                                events.append, jev_key="jev")
        return out["spec"], events


def blocks_of(spec: dict, sid: str) -> list[dict]:
    return next(s for s in spec["stages"] if s["id"] == sid)["blocks"]


def widget_of(spec: dict, sid: str) -> dict:
    return next(b for b in blocks_of(spec, sid) if b["type"] == "widget")  # assembly moves the closing quiz after it


def test_a_chart_request_changes_one_chart_and_nothing_is_rewritten_or_rechecked(monkeypatch):
    Edits(monkeypatch)
    pipe = Pipe(monkeypatch, {"pageWide": False, "targets": [target("widget", "loop", "use a log scale")]})
    spec, events = pipe.run()
    assert pipe.seen["planner"] == []  # no planner call: nothing about the text was asked
    assert [s["id"] for s in spec["stages"]] == IDS
    assert widget_of(spec, "loop")["html"].endswith("<!-- edited -->") and widget_of(spec, "sampling") == W["sampling"]
    assert spec["revision"] == {"added": 0, "replaced": 0, "removed": 0, "kept": 7, "charts": 1}
    assert pipe.seen["clarity"] == [] and pipe.seen["fact"] == []  # no text changed, so no text is checked
    # The stage's worked examples and every stage's section notes are kept, not written again
    assert pipe.seen["notes"] == {"sampling": {"deeper": "Deeper on sampling."}, "tokens": {"deeper": "stale"}}
    assert not any(b["key"].startswith("optimization:") for b in pipe.seen["examples"] or [])
    assert next(b for b in blocks_of(spec, "optimization") if b["type"] == "examples")["items"][0]["steps"] == ["Step one.", "Step two."]
    plan_done = [e for e in events if e.get("step") == "plan" and e.get("status") == "done"][0]
    assert plan_done["detail"] == "1 interactive part changed, 7 kept as they were"


def test_a_text_request_is_scoped_and_only_the_changed_text_is_checked(monkeypatch):
    Edits(monkeypatch)
    pipe = Pipe(monkeypatch, {"pageWide": False, "targets": [target("text", "tokens", "explain with a number")]},
                {"replace": [rewritten("tokens", "A rewritten explanation.")]})
    spec, _ = pipe.run()
    (call,) = pipe.seen["planner"]
    # The page's interactive parts are widgets, so the planner writes explanation only, like the first time
    assert "Change ONLY these parts" in call["request"] and call["system"] == pipeline._prompt("OUTLINE_SYSTEM") and codelab.PLANNER_NOTE in call["request"]
    assert blocks_of(spec, "tokens")[0]["md"] == "A rewritten explanation." and widget_of(spec, "tokens") == W["tokens"]
    assert pipe.seen["clarity"] == [IDS.index("tokens")] and pipe.seen["fact"] == [IDS.index("tokens")]
    assert pipe.seen["notes"] == {"sampling": {"deeper": "Deeper on sampling."}}  # the changed stage's old note isn't reused
    assert spec["revision"] == {"added": 0, "replaced": 1, "removed": 0, "kept": 7}


def test_a_page_wide_request_takes_the_old_path_and_still_keeps_the_widgets(monkeypatch):
    Edits(monkeypatch)
    pipe = Pipe(monkeypatch, {"pageWide": True, "targets": []}, {"replace": [rewritten("tokens", "Simpler.")]})
    spec, _ = pipe.run(quality="quality-plus")
    (call,) = pipe.seen["planner"]
    assert "Change ONLY" not in call["request"] and '{"changes": {...}}' in call["request"]  # today's request
    assert widget_of(spec, "sampling") == W["sampling"] and widget_of(spec, "tokens") == W["tokens"]  # Step 1's restore


def test_a_finder_that_fails_takes_the_old_path(monkeypatch):
    Edits(monkeypatch)
    pipe = Pipe(monkeypatch, None, {"replace": [rewritten("tokens", "Simpler.")]})
    spec, _ = pipe.run(quality="quality-plus")
    assert "Change ONLY" not in pipe.seen["planner"][0]["request"] and blocks_of(spec, "tokens")[0]["md"] == "Simpler."


def test_review_rounds_and_add_section_never_use_the_finder(monkeypatch):
    Edits(monkeypatch)
    called = []
    pipe = Pipe(monkeypatch, {"pageWide": False, "targets": [target("text", "tokens")]}, {"replace": [rewritten("tokens", "x")]})
    monkeypatch.setattr(pipeline, "find_targets", lambda *a: called.append(a))
    follow = {**FOLLOW, "design": default_design(OLD), "improve": "Fix these:", "kept": {}}
    try:
        pipeline.generate(None, {"mode": "concept", "text": "x", "quality": "quality-plus", "followUp": follow}, lambda e: None)
    except Exception:
        pass  # the improve path needs more than this test fakes; only that the finder was not called matters
    assert called == []


def test_a_quick_read_follow_up_changes_text_only_and_stays_quick(monkeypatch):
    Edits(monkeypatch)
    quiz = {"type": "quiz", "questions": [{"q": "q", "options": ["a", "b"], "answer": 0, "why": "w"}]}
    st = rewritten("tokens", "A shorter explanation.")
    st["blocks"] = [*st["blocks"], quiz]
    pipe = Pipe(monkeypatch, {"pageWide": False, "targets": [target("text", "tokens", "shorter")]}, {"replace": [st]})
    spec, _ = pipe.run(quality="lite-plus")
    assert pipe.seen["planner"][0]["effort"] == "low" and "quiz" not in [b["type"] for b in blocks_of(spec, "tokens")]
    assert blocks_of(spec, "tokens")[0]["md"] == "A shorter explanation." and spec.get("noAsk") is True


def test_a_code_lab_page_gets_prose_only_text_and_no_prebuilt_blocks(monkeypatch):
    Edits(monkeypatch)
    slider = next(b for b in stage(OLD, "embeddings")["blocks"] if b["type"] == "slider")
    st = rewritten("tokens", "A rewritten explanation.")
    st["blocks"] = [*st["blocks"], slider]
    asked = []

    def ask(request, parse):
        asked.append(request)
        p = parse({"changes": {"replace": [st]}})
        return {"outline": p["outline"], "warnings": p["warnings"]}

    r = run_prose(ask)
    assert codelab.PLANNER_NOTE in asked[0]
    types = [b["type"] for b in stage(r["outline"], "tokens")["blocks"]]
    assert "slider" not in types and "widget" in types  # the prebuilt blocks were dropped; the widget is back
    assert stage(r["outline"], "loop") == stage(FULL, "loop")  # other stages are not re-stripped or touched


def run_prose(ask):
    return T.apply_targets(None, UsageLog("q", "low"), "k", FOLLOW, COMPACT, [target("text", "tokens", "x")], "do it", ask, prose_only=True)


def test_an_interactive_page_still_gets_interactive_blocks(monkeypatch):
    Edits(monkeypatch)
    slider = next(b for b in stage(OLD, "embeddings")["blocks"] if b["type"] == "slider")  # a real, valid one
    st = rewritten("tokens", "A rewritten explanation.")
    st["blocks"] = [*st["blocks"], slider]
    r = T.apply_targets(None, UsageLog("q", "low"), "k", {"kind": "revise", "outline": json.dumps(OLD)}, OLD, [target("text", "tokens", "x")], "do it",
                        Planner({"replace": [st]}))
    assert "slider" in [b["type"] for b in stage(r["outline"], "tokens")["blocks"]]


def test_a_widget_request_on_a_page_without_widgets_becomes_a_section_rewrite(monkeypatch):
    """Interactive and Quick read pages build their interactive parts from ready-made blocks, so "make this chart interactive"
    is a section rewrite there, not a widget to build."""
    edits = Edits(monkeypatch)
    slider = next(b for b in stage(OLD, "embeddings")["blocks"] if b["type"] == "slider")
    st = rewritten("tokens", "Now with a slider.")
    st["blocks"] = [*st["blocks"], slider]
    ask = Planner({"replace": [st]})
    follow = {"kind": "revise", "outline": json.dumps(OLD)}
    r = T.apply_targets(None, UsageLog("q", "low"), "k", follow, OLD, [target("widget", "tokens", "make the plot interactive")], "do it", ask, quality="quality-plus")
    assert edits.added == [] and edits.edited == [] and "make the plot interactive" in ask.requests[0]
    assert "slider" in [b["type"] for b in stage(r["outline"], "tokens")["blocks"]] and r["revision"]["changed"] == ["tokens"]


def test_a_widget_request_on_a_page_with_widgets_still_adds_or_edits_a_widget(monkeypatch):
    edits = Edits(monkeypatch)
    r = run([target("widget", "attention", "a chart of the weights")], Planner())
    assert edits.added and r["revision"]["widgets"] == ["attention"]
