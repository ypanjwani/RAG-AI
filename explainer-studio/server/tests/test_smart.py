"""Smart mode (smart.py): Jev's per-section picks, the planner directive, enforcing the picks, and the pipeline path
(Claude and Jev faked)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from explainer import pipeline, smart
from explainer.jev import JevError
from explainer.pricing import UsageLog
from explainer.requests import parse_generate

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample.json").read_text(encoding="utf-8"))
SECTIONS = [{"label": f"Section {i}", "blurb": f"Teaches part {i}.", "covers": "A formula."} for i in range(3)]
BRIEF = {"type": "interactive", "goal": "See it run", "math": "x <- x / 2 from 8, 5 steps", "insight": "It halves."}


def jev_reply(text: str, visual: str) -> dict:
    def choice(option: str) -> dict:
        return {"type": "choice", "ranking": [{"option": option, "p": 0.8}], "confidence": 0.8}
    return {"answers": {"text": choice(text), "visual": choice(visual)}, "model": "jev-1", "usage": {"input_tokens": 100}}


def test_parse_generate_keeps_smart_without_review_rounds():
    r = parse_generate({"mode": "concept", "text": "x", "quality": "smart", "reviewRounds": 3})
    assert r["quality"] == "smart" and "reviewRounds" not in r


def test_no_jev_key_means_interactive_everywhere():
    warnings: list[str] = []
    picks = smart.pick_styles(None, "T", None, SECTIONS, UsageLog("smart"), warnings)
    assert [(p["text"], p["visual"], p["by"]) for p in picks] == [("interactive", "builtin", "default")] * 3
    assert "no Jev key" in warnings[0]


def test_jev_picks_each_section_and_a_failure_falls_back(monkeypatch):
    replies = iter([jev_reply("quick", "none"), JevError("Jev rate limited"), jev_reply("interactive", "custom")])

    def fake_ask(key, state, questions):
        assert set(questions) == {"text", "visual"} and "type" not in json.dumps(state["section"])  # plan view only
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(smart, "jev_ask", fake_ask)
    smart_pool = smart.ThreadPoolExecutor
    monkeypatch.setattr(smart, "ThreadPoolExecutor", lambda max_workers: smart_pool(max_workers=1))  # keep the replies in order
    warnings: list[str] = []
    picks = smart.pick_styles("key", "T", "student", SECTIONS, UsageLog("smart"), warnings)
    assert [(p["text"], p["visual"], p["by"]) for p in picks] == [("quick", "none", "jev"), ("interactive", "builtin", "default"), ("interactive", "custom", "jev")]
    assert "couldn't judge 1 of 3" in warnings[0]


def test_directive_explains_briefs_only_when_a_section_is_custom():
    plain = smart.smart_directive(SECTIONS, [{"text": "quick", "visual": "builtin", "by": "jev"}] * 3)
    assert "exactly these 3 stages" in plain and "Quick read" in plain and smart.BRIEF_RULE not in plain
    custom = smart.smart_directive(SECTIONS, [{"text": "interactive", "visual": "custom", "by": "jev"}] * 3)
    assert smart.BRIEF_RULE in custom


def test_enforce_keeps_briefs_only_in_custom_stages_and_trims_quick_ones():
    lab = {"stages": [
        {"id": "a", "blocks": [{"type": "text", "md": "x"}, {"type": "quiz", "questions": []}, BRIEF]},
        {"id": "b", "blocks": [BRIEF, BRIEF, {"type": "text", "md": "y"}]},
    ]}
    picks = [{"text": "quick", "visual": "builtin", "by": "jev"}, {"text": "interactive", "visual": "custom", "by": "jev"}]
    warnings: list[str] = []
    out = smart.enforce(lab, picks, warnings)
    assert [b["type"] for b in out["stages"][0]["blocks"]] == ["text"]
    assert [b["type"] for b in out["stages"][1]["blocks"]] == ["interactive", "text"]
    assert "2 custom simulation brief(s)" in warnings[0]


def test_pipeline_writes_widgets_only_where_jev_picked_custom(monkeypatch):
    stages = copy.deepcopy(SAMPLE["outline"]["stages"][:3])
    stages[0]["blocks"].append(dict(BRIEF))  # quick + builtin: brief dropped, quiz dropped
    stages[2]["blocks"].append(dict(BRIEF))  # interactive + custom: becomes a widget
    outline = {**SAMPLE["outline"], "stages": stages}
    seen: dict = {}
    monkeypatch.setattr(pipeline, "ask_brief", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "choose_design", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.smart_mode, "plan_sections", lambda client, request, usage, count=None: SECTIONS)
    monkeypatch.setattr(smart, "jev_ask", lambda key, state, q: jev_reply(*{
        "1 of 3": ("quick", "builtin"), "2 of 3": ("interactive", "builtin"), "3 of 3": ("interactive", "custom")}[state["section"]["position"]]))

    def fake_write_outline(client, request, emit, usage, system, parse, effort, *a, **k):
        seen["request"] = request
        p = parse(outline)
        return {"outline": p["outline"], "warnings": p["warnings"]}

    def fake_widgets(client, lab, usage, emit, warnings, effort):
        refs = pipeline.briefs_of(lab)
        seen["briefs"] = [r["stageIndex"] for r in refs]
        return {r["key"]: [{"type": "widget", "title": "Sim", "html": "<p id=o></p><script>o.textContent = 1</script>"}] for r in refs}

    monkeypatch.setattr(pipeline, "write_outline", fake_write_outline)
    monkeypatch.setattr(pipeline, "build_widgets", fake_widgets)
    monkeypatch.setattr(pipeline, "write_examples", lambda client, spec, usage: spec)
    checked = []
    monkeypatch.setattr(pipeline, "clarity_check", lambda client, jev_key, spec, usage, only=None: (checked.append("clarity"), (spec, "ok"))[1])
    monkeypatch.setattr(pipeline, "fact_check", lambda client, jev_key, spec, usage, section_check=False, only=None: (checked.append("fact"), (spec, "ok"))[1])

    spec = pipeline.generate(None, {"mode": "concept", "text": "Explain attention", "quality": "smart"}, lambda e: None, jev_key="jev")["spec"]
    assert "# Section plan" in seen["request"] and smart.BRIEF_RULE in seen["request"]
    assert seen["briefs"] == [2]
    types = [[b["type"] for b in st["blocks"]] for st in spec["stages"]]
    assert "quiz" not in types[0] and "interactive" not in types[0]
    assert "widget" in types[2]
    assert spec["smart"] == {"quick": 1, "interactive": 2, "creative": 0, "custom": 1, "byJev": 3}
    assert checked == ["clarity", "fact"]


def test_a_smart_revision_runs_as_quality_plus(monkeypatch):
    seen = {}
    monkeypatch.setattr(pipeline.smart_mode, "plan_sections", lambda *a, **k: seen.setdefault("planned", True) and [])

    def fake_write_outline(client, request, emit, usage, system, parse, effort, *a, **k):
        seen["parse"] = parse
        o = SAMPLE["outline"]
        return {"outline": o, "warnings": [], "revision": {"changed": [o["stages"][0]["id"]], "removed": 0}}

    monkeypatch.setattr(pipeline, "write_outline", fake_write_outline)
    monkeypatch.setattr(pipeline, "write_examples", lambda client, spec, usage: spec)
    from explainer.core.validate import default_design
    req = {"mode": "concept", "text": "Make it simpler", "quality": "smart",
           "followUp": {"kind": "revise", "outline": json.dumps(SAMPLE["outline"]), "design": default_design(SAMPLE["outline"])}}
    r = pipeline.generate(None, req, lambda e: None, jev_key=None, checks=False)
    assert "planned" not in seen and r["usage"]["quality"] == "quality-plus" and "smart" not in r["spec"]
