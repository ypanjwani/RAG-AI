"""Add section (addsection.py): Jev's placement, the add-only merge, and the pipeline path (Claude and Jev faked)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from explainer import pipeline
from explainer.addsection import addition_request, decide_place, parse_addition, place_questions, place_section, slim_page
from explainer.core.validate import default_design
from explainer.followup import parse_follow_up
from explainer.pricing import UsageLog

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample.json").read_text(encoding="utf-8"))
OLD = SAMPLE["outline"]
IDS = [s["id"] for s in OLD["stages"]]


class FakeResponse:
    def __init__(self, body: object, status: int = 200):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def new_stage(id="discrete", label="Discrete distributions", phase="math"):
    return {"id": id, "label": label, "phase": phase, "blurb": "Counting outcomes.",
            "blocks": [{"type": "text", "md": "A discrete distribution lists each outcome with its probability."}]}


def ids_of(outline):
    return [s["id"] for s in outline["stages"]]


def choice(option, p, rest="start"):
    return {"type": "choice", "ranking": [{"option": option, "p": p}, {"option": rest, "p": 1 - p}], "confidence": p}


# ---------- placement ----------


def test_place_questions_cover_every_gap():
    q = place_questions(OLD)
    assert list(q["position"]["criteria"]) == ["start", *[f"after_{i + 1}" for i in range(len(IDS))]]
    assert "at the end" in q["position"]["criteria"][f"after_{len(IDS)}"]
    assert 'before stage 2 "Tokens become vectors"' in q["position"]["criteria"]["after_1"]
    assert list(q["closest"]["criteria"]) == [f"stage_{i + 1}" for i in range(len(IDS))]


def test_decide_place():
    p = decide_place({"position": choice("after_6", 0.62)}, OLD)
    assert (p["after"], p["placed"], p["p"]) == ("uncertainty", "jev", 0.62) and "covered" not in p
    assert decide_place({"position": choice("start", 0.5, "after_1")}, OLD)["after"] == "start"
    # Unsure, or no answer: the end.
    low = decide_place({"position": choice("after_2", 0.2)}, OLD)
    assert (low["after"], low["placed"], low["p"]) == ("summary", "end", 0.2)
    assert decide_place({}, OLD)["placed"] == "end"
    # Already covered only at COVERED_AT and with a closest stage.
    cov = decide_place({"position": choice("after_6", 0.6), "covered": {"type": "noul", "p": 0.8}, "closest": choice("stage_4", 0.7, "stage_1")}, OLD)
    assert cov["covered"] == "sampling"
    assert "covered" not in decide_place({"covered": {"type": "noul", "p": 0.5}, "closest": choice("stage_4", 0.7, "stage_1")}, OLD)


def test_place_section(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(k) or FakeResponse({
        "model": "jev-1", "usage": {"input_tokens": 800, "output_tokens": 0},
        "answers": {"position": {"probabilities": {"after_6": 0.7, "after_7": 0.3}, "confidence": 0.7}, "covered": {"noul": 0.1}},
    }))
    warnings: list[str] = []
    assert place_section(None, OLD, "continuous distributions", UsageLog("quality-plus"), warnings)["placed"] == "end"
    assert not calls and "no Jev key" in warnings[0]
    usage = UsageLog("quality-plus")
    p = place_section("key", OLD, "continuous distributions", usage, [])
    assert p["after"] == "uncertainty" and [s["step"] for s in usage.summary()["steps"]] == ["jev place"]
    state = calls[0]["json"]["state"]
    assert state["reader_request"] == "continuous distributions" and [s["label"] for s in state["stages"]][0] == "Text becomes tokens"
    # Jev down: the end, with a warning.
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse({}, 529))
    warnings = []
    assert place_section("key", OLD, "x", UsageLog("quality-plus"), warnings)["placed"] == "end" and "overloaded" in warnings[0]


def test_addition_request():
    f = {"kind": "revise", "outline": json.dumps(OLD), "design": {}, "add": True}
    r = addition_request("continuous distributions", f, OLD, {"after": "uncertainty", "covered": "sampling"})
    assert 'right after the stage "Math lab: uncertainty", before the stage "Math lab: optimization"' in r
    assert 'The stage "Picking the next token" may already cover part' in r and "Never repeat, rewrite or remove" in r
    assert "at the very start" in addition_request("x", f, OLD, {"after": "start"})
    assert "at the end." in addition_request("x", f, OLD, {"after": "summary"})
    # The slim page, not the outline JSON; a typed concept's "Reader's request" source isn't repeated.
    assert f["outline"] not in r and '"blocks"' not in r
    assert "Source material" not in addition_request("x", {**f, "source": "Reader's request: x"}, OLD, {"after": "start"})
    paper = addition_request("x", {**f, "source": "S" * 20_000}, OLD, {"after": "start"})
    assert "Source material" in paper and "S" * 8000 in paper and "S" * 8001 not in paper


def test_slim_page():
    s = slim_page(OLD, {"after": "uncertainty"})
    lines = s.split("\n")
    # Every stage's label in order, with the insertion point between stage 6 and 7.
    at = lines.index("   >>> THE NEW SECTION GOES HERE <<<")
    assert lines[at - 1].startswith("6. [math] Math lab: uncertainty: ") and lines[at + 1].startswith("7. [math] Math lab: optimization: ")
    assert 'The stage just before the new section, "Math lab: uncertainty"' in s and 'The stage just after it, "Math lab: optimization"' in s
    assert f'level: {OLD["level"]}' in s and "Phase ids: " in s
    # Every shared control, with its stage; glossary terms without definitions.
    for st_n, st in enumerate(OLD["stages"], 1):
        for b in st["blocks"]:
            if b["type"] in ("slider", "choice", "toggle"):
                assert f'- {b["var"]}: {b["type"]} "{b["label"]}"' in s and f"(stage {st_n})" in s
    assert "Glossary terms already defined: Token, " in s and OLD["glossary"][0]["definition"] not in s
    # Far stages' blocks are left out: no quiz questions.
    quiz = next(b for st in OLD["stages"] for b in st["blocks"] if b["type"] == "quiz")
    assert quiz["questions"][0]["q"] not in s
    # At the start there is no stage before; at the end, none after.
    assert "just before" not in slim_page(OLD, {"after": "start"})
    end = slim_page(OLD, {"after": "summary"})
    assert "just after" not in end and end.split("Stages, in order:\n")[1].split("\n\n")[0].endswith("<<<")


# ---------- the add-only merge ----------


def test_parse_addition_places_and_keeps_everything():
    p = parse_addition({"changes": {"add": [{"after": "tokens", "stage": new_stage()}, {"stage": new_stage("cont", "Continuous", "math")}],
                                    "glossary": [{"term": "PDF", "definition": "density"}, {"term": "token", "definition": "changed!"}]}},
                       OLD, "uncertainty")
    # Both go at Jev's position, in order; the planner's own "after" is ignored.
    assert ids_of(p["outline"]) == [*IDS[:6], "discrete", "cont", *IDS[6:]]
    assert p["revision"] == {"changed": ["discrete", "cont"], "removed": 0}
    terms = {g["term"]: g["definition"] for g in p["outline"]["glossary"]}
    assert terms["PDF"] == "density" and terms["Token"] == next(g["definition"] for g in OLD["glossary"] if g["term"] == "Token")
    assert not p["warnings"]


def test_parse_addition_ignores_other_changes():
    p = parse_addition({"changes": {"add": [{"stage": new_stage(id="tokens")}], "replace": [new_stage("sampling")], "remove": ["loop"], "title": "New"}},
                       OLD, "start")
    assert ids_of(p["outline"]) == ["tokens-2", *IDS] and p["outline"]["title"] == OLD["title"]
    assert "replace, remove, title" in p["warnings"][0]
    three = parse_addition({"changes": {"add": [{"stage": new_stage(f"n{i}")} for i in range(3)]}}, OLD, "summary")
    assert ids_of(three["outline"])[-2:] == ["n0", "n1"] and "only the first 2" in three["warnings"][0]


def test_parse_addition_errors():
    assert "error" in parse_addition({"stages": OLD["stages"]}, OLD, "start")  # a full outline isn't an addition
    assert "error" in parse_addition({"changes": {"remove": ["loop"]}}, OLD, "start")
    assert "error" in parse_addition("nope", OLD, "start")


def test_parse_follow_up_add_flag():
    base = {"kind": "revise", "outline": json.dumps(OLD), "design": default_design(OLD)}
    assert parse_follow_up({**base, "add": True})["add"] is True
    assert "add" not in parse_follow_up({**base, "add": "yes"})


# ---------- the pipeline ----------


def test_pipeline_adds_at_jevs_position(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse({
        "model": "jev-1", "usage": {"input_tokens": 800, "output_tokens": 0},
        "answers": {"position": {"probabilities": {"after_6": 0.8, "after_7": 0.2}, "confidence": 0.8}, "covered": {"noul": 0.1}},
    }))

    def fake_write_outline(client, request, emit, usage, system, parse, effort, *a, **k):
        seen["request"] = request
        p = parse({"changes": {"add": [{"after": "start", "stage": new_stage()}]}})
        return {"outline": p["outline"], "warnings": p["warnings"], "revision": p["revision"]}

    def fake_check(name):
        def check(client, jev_key, spec, usage, *a, only=None, **k):
            seen[name] = only
            return spec, "checked"
        return check

    monkeypatch.setattr(pipeline, "write_outline", fake_write_outline)
    monkeypatch.setattr(pipeline, "clarity_check", fake_check("clarity"))
    monkeypatch.setattr(pipeline, "fact_check", fake_check("fact"))
    monkeypatch.setattr(pipeline, "write_examples", lambda client, spec, usage: spec)
    req = {"mode": "concept", "text": "Also explain discrete distributions. 3 stages", "quality": "quality-plus",
           "followUp": {"kind": "revise", "outline": json.dumps(OLD), "design": default_design(OLD), "add": True}}
    events: list[dict] = []
    spec = pipeline.generate(None, req, events.append, jev_key="jev")["spec"]

    assert "Add a new section" in seen["request"] and "write exactly" not in seen["request"]
    assert [s["id"] for s in spec["stages"]] == [*IDS[:6], "discrete", *IDS[6:]]
    assert seen["clarity"] == [6] and seen["fact"] == [6]
    assert spec["addition"] == {"after": 'after "Math lab: uncertainty"', "p": 0.8, "placed": "jev"}
    assert spec["revision"]["added"] == 1 and spec["revision"]["kept"] == len(IDS)
    details = [e.get("detail") for e in events if e.get("step") == "plan"]
    assert 'Jev: after "Math lab: uncertainty" (80%)' in details
