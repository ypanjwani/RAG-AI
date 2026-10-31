"""Revisions made as changes (revise.py): merging the planner's changes into the previous outline, and the
pipeline running the later steps on the changed stages only (Claude and Jev faked)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from explainer import pipeline
from explainer.core.followup import revision_request
from explainer.core.validate import default_design
from explainer.followup import parse_follow_up, parse_kept, parse_widgets
from explainer.revise import merge_revision, parse_revision, restore_kept_widgets, revision_summary

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample.json").read_text(encoding="utf-8"))
OLD = SAMPLE["outline"]
IDS = [s["id"] for s in OLD["stages"]]


def new_stage(id="discrete", label="Discrete distributions", phase="math"):
    return {"id": id, "label": label, "phase": phase, "blurb": "Counting outcomes.",
            "blocks": [{"type": "text", "md": "A discrete distribution lists each outcome with its probability."}]}


def ids_of(outline):
    return [s["id"] for s in outline["stages"]]


# ---------- merging ----------


def test_add_after_a_stage_and_at_the_start():
    m = merge_revision(OLD, {"add": [{"after": "uncertainty", "stage": new_stage()},
                                     {"after": "start", "stage": new_stage("intro", "Intro", "read")}]})
    assert ids_of(m["outline"]) == ["intro", *IDS[:6], "discrete", *IDS[6:]]
    assert m["changed"] == ["discrete", "intro"] and m["removed"] == 0 and not m["warnings"]
    # Every other stage is the previous one, untouched.
    for s in OLD["stages"]:
        assert next(x for x in m["outline"]["stages"] if x["id"] == s["id"]) == s


def test_add_edge_cases():
    m = merge_revision(OLD, {"add": [{"after": "no-such-stage", "stage": new_stage()}]})
    assert ids_of(m["outline"])[-1] == "discrete" and "added at the end" in m["warnings"][0]
    # A new stage reusing a kept id, or with no id, gets a unique one.
    m = merge_revision(OLD, {"add": [{"after": "tokens", "stage": new_stage("tokens")}, {"stage": {**new_stage(), "id": ""}}]})
    assert m["changed"] == ["tokens-2", "discrete-distributions"]
    assert ids_of(m["outline"])[:3] == ["discrete-distributions", "tokens", "tokens-2"]


def test_replace_and_remove():
    rewritten = {**new_stage("sampling", "Sampling, rewritten", "write")}
    m = merge_revision(OLD, {"replace": [rewritten], "remove": ["loop", "nope"]})
    assert ids_of(m["outline"]) == [i for i in IDS if i != "loop"]
    assert m["outline"]["stages"][IDS.index("sampling")]["label"] == "Sampling, rewritten"
    assert m["changed"] == ["sampling"] and m["removed"] == 1 and 'no stage "nope"' in m["warnings"][0]
    m = merge_revision(OLD, {"replace": [new_stage("ghost")]})
    assert ids_of(m["outline"])[-1] == "ghost" and "added at the end" in m["warnings"][0]


def test_page_fields_and_glossary():
    old = {**OLD, "glossary": [{"term": "Token", "definition": "old"}]}
    m = merge_revision(old, {"title": "New title", "level": "student", "subtitle": "  ",
                             "glossary": [{"term": "token", "definition": "new"}, {"term": "PMF", "definition": "probability mass function"}]})
    o = m["outline"]
    assert (o["title"], o["level"], o["subtitle"]) == ("New title", "student", OLD["subtitle"])
    assert o["glossary"] == [{"term": "Token", "definition": "new"}, {"term": "PMF", "definition": "probability mass function"}]
    assert m["changed"] == [] and o["stages"] == OLD["stages"]


@pytest.mark.parametrize("changes,error", [
    ("add a stage", "not an object"),
    ({}, "no changes"),
    ({"remove": IDS}, "every stage"),
    ({"add": [{"after": "tokens"}]}, "no changes"),  # nothing usable
])
def test_unusable_changes(changes, error):
    assert error in merge_revision(OLD, changes)["error"]


def test_parse_revision():
    p = parse_revision({"changes": {"add": [{"after": "uncertainty", "stage": new_stage()}]}}, OLD)
    assert p["revision"] == {"changed": ["discrete"], "removed": 0}
    assert ids_of(p["outline"]) == [*IDS[:6], "discrete", *IDS[6:]]
    # A complete outline is a full rewrite: no "revision".
    full = parse_revision(copy.deepcopy(OLD), OLD)
    assert "revision" not in full and ids_of(full["outline"]) == IDS
    # Unusable changes are an error, so write_outline asks again.
    assert "error" in parse_revision({"changes": {}}, OLD)
    # A new stage whose blocks are all invalid keeps its place but has nothing to show; parse_outline reports it.
    bad = parse_revision({"changes": {"add": [{"after": "tokens", "stage": {**new_stage(), "blocks": [{"type": "nonsense"}]}}]}}, OLD)
    assert "discrete" in bad["revision"]["changed"] and bad["warnings"]


def test_revision_summary():
    p = parse_revision({"changes": {"add": [{"after": "tokens", "stage": new_stage()}], "replace": [new_stage("loop", "Loop", "write")],
                                    "remove": ["summary"]}}, OLD)
    s = revision_summary(p["revision"], p["outline"], OLD)
    assert s["counts"] == {"added": 1, "replaced": 1, "removed": 1, "kept": 6}
    assert s["text"] == "1 section added, 1 section rewritten, 1 section removed, 6 kept as they were"


def test_revision_request_modes():
    f = {"kind": "revise", "outline": json.dumps(OLD), "design": {}}
    assert '{"changes": {...}}' in revision_request("add X", f, changes=True)
    assert "complete revised outline as one JSON object" in revision_request("add X", f)


def test_parse_kept():
    k = parse_kept({"steps": {"s1:2:0": ["a", "b", 3], "bad": ["x"], "s2:1:0": []}, "notes": {"s1": {"deeper": "d", "another": " "}, "s2": "x"}})
    assert k == {"steps": {"s1:2:0": ["a", "b"]}, "notes": {"s1": {"deeper": "d"}}}
    assert parse_kept({"steps": {}, "notes": {}}) is None and parse_kept("x") is None
    assert parse_kept({"notes": {"s1": {"deeper": "x" * 70_000}}}) is None  # past keptChars: dropped, not refused
    f = parse_follow_up({"kind": "revise", "outline": json.dumps(OLD), "design": default_design(OLD), "kept": {"notes": {"tokens": {"deeper": "d"}}}})
    assert f["kept"] == {"notes": {"tokens": {"deeper": "d"}}}


# ---------- the pipeline ----------


def test_pipeline_runs_later_steps_on_changed_stages_only(monkeypatch):
    seen: dict = {}

    def fake_write_outline(client, request, emit, usage, system, parse, effort, *a, **k):
        seen["request"] = request
        seen["whole"] = k.get("whole")
        p = parse({"changes": {"add": [{"after": "uncertainty", "stage": new_stage()}]}})
        return {"outline": p["outline"], "warnings": p["warnings"], **({"revision": p["revision"]} if "revision" in p else {})}

    def fake_check(name):
        def check(client, jev_key, spec, usage, *a, only=None, **k):
            seen[name] = only
            return spec, "checked"
        return check

    def fake_notes(client, spec, extras, usage, kept=None):
        seen["kept_notes"] = kept
        return spec, ""

    monkeypatch.setattr(pipeline, "write_outline", fake_write_outline)
    monkeypatch.setattr(pipeline, "clarity_check", fake_check("clarity"))
    monkeypatch.setattr(pipeline, "fact_check", fake_check("fact"))
    monkeypatch.setattr(pipeline, "write_notes", fake_notes)
    monkeypatch.setattr(pipeline, "write_examples", lambda client, spec, usage: seen.setdefault("examples", pipeline.example_briefs(spec)) and spec)

    bi, block = next((bi, b) for bi, b in enumerate(OLD["stages"][6]["blocks"]) if b["type"] == "examples")
    kept_steps = {f"optimization:{bi}:{ii}": ["Step one.", "Step two."] for ii in range(len(block["items"]))}
    req = {"mode": "concept", "text": "Just add discrete distributions", "quality": "quality-plus", "extras": ["examples", "depth"],
           "followUp": {"kind": "revise", "outline": json.dumps(OLD), "design": default_design(OLD),
                        "kept": {"steps": kept_steps,
                                 "notes": {"tokens": {"deeper": "Deeper on tokens."}, "discrete": {"deeper": "stale"}}}}}
    events: list[dict] = []
    out = pipeline.generate(None, req, events.append, jev_key="jev")
    spec = out["spec"]

    assert '{"changes": {...}}' in seen["request"] and seen["whole"] == "your reply"
    assert [s["id"] for s in spec["stages"]] == [*IDS[:6], "discrete", *IDS[6:]]
    # Only the new stage (index 6) is clarity- and fact-checked.
    assert seen["clarity"] == [6] and seen["fact"] == [6]
    # The kept stage's worked example keeps its steps, so no example is written again.
    ex = next(b for b in spec["stages"][7]["blocks"] if b["type"] == "examples")
    assert ex["items"][0]["steps"] == ["Step one.", "Step two."]
    assert not any(b["key"].startswith("optimization:") for b in seen.get("examples") or [])
    # Section extras: kept stages reuse theirs; the changed stage's old note isn't reused.
    assert seen["kept_notes"] == {"tokens": {"deeper": "Deeper on tokens."}}
    assert spec["revision"] == {"added": 1, "replaced": 0, "removed": 0, "kept": 8}
    plan_done = [e for e in events if e.get("step") == "plan" and e.get("status") == "done"][0]
    assert plan_done["detail"] == "1 section added, 8 kept as they were"


def test_lite_revisions_still_rewrite(monkeypatch):
    seen = {}

    def fake_write_outline(client, request, emit, usage, system, parse, effort, *a, **k):
        seen["request"] = request
        p = parse(copy.deepcopy(OLD))
        return {"outline": p["outline"], "warnings": []}

    monkeypatch.setattr(pipeline, "write_outline", fake_write_outline)
    req = {"mode": "concept", "text": "shorter", "quality": "lite-plus",
           "followUp": {"kind": "revise", "outline": json.dumps(OLD), "design": default_design(OLD)}}
    spec = pipeline.generate(None, req, lambda e: None)["spec"]
    assert "complete revised outline" in seen["request"] and "revision" not in spec


def test_write_notes_reuses_kept(monkeypatch):
    from explainer import notes as notes_mod

    calls = []
    monkeypatch.setattr(notes_mod, "_chunk", lambda client, spec, idx, fields, usage: calls.append(idx) or {spec["stages"][i]["id"]: {"deeper": "new"} for i in idx})
    spec = {"title": "T", "level": "beginner", "stages": [{"id": "a", "label": "A", "blocks": []}, {"id": "b", "label": "B", "blocks": []}], "warnings": []}
    out, detail = notes_mod.write_notes(None, spec, ["depth"], None, kept={"a": {"deeper": "old"}, "gone": {"deeper": "x"}})
    assert calls == [[1]] and out["sectionNotes"] == {"a": {"deeper": "old"}, "b": {"deeper": "new"}} and "(1 kept)" in detail
    out, detail = notes_mod.write_notes(None, spec, ["depth", "rephrase"], None, kept={"a": {"deeper": "old"}})
    assert calls[-1] == [0, 1]  # "a" lacks the other wording, so it is written again


# ---------- widgets survive a revision ----------


def widget(title, html='<div id="a"></div><script>XW.vega("a", {})</script>'):
    return {"type": "widget", "title": title, "html": html, "plan": "{}"}


def with_widgets(outline, at: dict):
    """OLD with a widget placeholder (or the widget) added to each stage in `at` ({stage id: block})."""
    return {**outline, "stages": [{**s, "blocks": [*s["blocks"], at[s["id"]]] if s["id"] in at else s["blocks"]} for s in outline["stages"]]}


def placeholder(title):
    return {"type": "text", "md": f"(interactive widget: {title})"}


def test_parse_widgets_keeps_only_valid_widgets():
    ok, no_script, big = widget("A"), widget("B", "<div>static</div>"), widget("C", "<script></script>" + "x" * 40_001)
    assert parse_widgets({"s1": [ok, no_script, big, {"type": "text", "md": "x"}], "s2": "x", 3: []}) == {"s1": [ok]}
    assert parse_widgets({"s1": [no_script]}) is None and parse_widgets("x") is None
    f = parse_follow_up({"kind": "revise", "outline": json.dumps(OLD), "design": default_design(OLD), "widgets": {"tokens": [ok]}})
    assert f["widgets"] == {"tokens": [ok]}


def test_widgets_go_back_where_their_placeholder_is():
    a, b = widget("Rank"), widget("Heads")
    outline = with_widgets(OLD, {"tokens": placeholder("Rank"), "sampling": placeholder("Heads")})
    out = restore_kept_widgets(outline, {"tokens": [a], "sampling": [b]})
    assert out["stages"][IDS.index("tokens")]["blocks"][-1] == a and out["stages"][IDS.index("sampling")]["blocks"][-1] == b
    # Nothing else changed, and the input is untouched.
    assert [s for s in out["stages"] if s["id"] not in ("tokens", "sampling")] == [s for s in outline["stages"] if s["id"] not in ("tokens", "sampling")]
    assert outline["stages"][IDS.index("tokens")]["blocks"][-1] == placeholder("Rank")


def test_a_placeholder_that_moved_stage_still_gets_its_widget_and_a_dropped_one_is_not_forced_back():
    a, b = widget("Rank"), widget("Heads")
    outline = with_widgets(OLD, {"sampling": placeholder("Rank")})  # "Rank" moved from tokens to sampling; "Heads" was dropped
    out = restore_kept_widgets(outline, {"tokens": [a], "loop": [b]})
    assert out["stages"][IDS.index("sampling")]["blocks"][-1] == a
    assert not any(bl.get("title") == "Heads" for s in out["stages"] for bl in s["blocks"])


def test_two_widgets_with_one_title_go_back_in_order():
    a, b = widget("Same", '<script>1</script>'), widget("Same", '<script>2</script>')
    outline = with_widgets(with_widgets(OLD, {"tokens": placeholder("Same")}), {"sampling": placeholder("Same")})
    out = restore_kept_widgets(outline, {"tokens": [a], "sampling": [b]})
    assert out["stages"][IDS.index("tokens")]["blocks"][-1] == a and out["stages"][IDS.index("sampling")]["blocks"][-1] == b


def test_a_revision_keeps_the_widgets_of_the_stages_it_did_not_change(monkeypatch):
    chart, play = widget("Rank"), widget("Heads", '<div id="pg"></div><script>XW.playground({})</script>')
    compact = with_widgets(with_widgets(OLD, {"tokens": placeholder("Rank")}), {"sampling": placeholder("Heads")})
    seen: dict = {}

    def fake_write_outline(client, request, emit, usage, system, parse, effort, *a, **k):
        seen["request"] = request
        p = parse({"changes": {"add": [{"after": "uncertainty", "stage": new_stage()}]}})
        return {"outline": p["outline"], "warnings": p["warnings"], "revision": p["revision"]}

    monkeypatch.setattr(pipeline, "write_outline", fake_write_outline)
    monkeypatch.setattr(pipeline, "clarity_check", lambda client, jev_key, spec, usage, *a, only=None, **k: (spec, "checked"))
    monkeypatch.setattr(pipeline, "fact_check", lambda client, jev_key, spec, usage, *a, only=None, **k: (spec, "checked"))
    monkeypatch.setattr(pipeline, "write_notes", lambda client, spec, extras, usage, kept=None: (spec, ""))
    req = {"mode": "concept", "text": "Add discrete distributions", "quality": "playground", "extras": [],
           "followUp": parse_follow_up({"kind": "revise", "outline": json.dumps(compact), "design": default_design(OLD),
                                        "widgets": {"tokens": [chart], "sampling": [play]}})}
    spec = pipeline.generate(None, req, lambda e: None, jev_key="jev")["spec"]
    by_id = {s["id"]: s for s in spec["stages"]}
    assert by_id["tokens"]["blocks"][-1] == chart and by_id["sampling"]["blocks"][-1] == play
    assert "interactive widget:" in seen["request"] and "Keep every such block" in seen["request"]  # Haiku is told to keep the placeholders
    assert spec["revision"]["added"] == 1
